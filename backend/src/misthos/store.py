"""The lifecycle store and the seeded simulation.

Every change to an issue's state goes through this module, which is what the
architecture means by a single writer: the API, the demo stepper and the sweeper all
call it, none of them assigns a state, and every move is checked against the domain's
transition table and leaves a decision behind.

Where the result is kept is the repository's business. With no database configured
it is memory, so the demo needs no infrastructure and resets on restart; with
`MISTHOS_DATABASE_URL` set it is Postgres or a SQLite file, and it survives. The seed
data at the bottom is the simulation either way: prices from the real engine, and
fake GitHub and chain calls.
"""

from __future__ import annotations

import random
import threading
from collections.abc import Collection
from datetime import UTC, datetime, timedelta

from misthos.config import settings
from misthos.domain import compliance, pricing, timers
from misthos.domain import issue as lifecycle
from misthos.domain.compliance import (
    ComplianceRefusal,
    IdentityStatus,
    PartyKind,
    PayoutGate,
    Screening,
    ScreeningOutcome,
    ScreeningReason,
)
from misthos.domain.issue import IssueState
from misthos.domain.money import Usdc
from misthos.domain.pricing import ComplexitySignals, UnfundableIssue, propose
from misthos.domain.timers import TimedAction
from misthos.models.records import IssueRecord
from misthos.repositories import MemoryRepository, Repository, StaleIssue, build_repository
from misthos.schemas import (
    Claim,
    Contributor,
    Decision,
    EscrowCommitment,
    IssueOut,
    IssueSummaryOut,
    MetricsOut,
    Publisher,
    Review,
    Submission,
    TimelineEntry,
    Wallet,
    money,
)
from misthos.services.compliance import (
    IdentityProvider,
    ScreeningProvider,
    SimulatedIdentity,
    SimulatedScreening,
)

__all__ = ["IssueRecord", "Store", "store"]

ESCROW_CONTRACT = "0x7A3f19bE5c2D80416aB9e0C7d3F5a12B6c8E4d90"
CHAIN = "arc-testnet"

REPO_POOL = [
    "acme/ledger-core",
    "northwind/httpx-retry",
    "contoso/schema-cli",
    "acme/ledger-adapters",
    "globex/parse-locale",
]

_PRE_FUNDING = frozenset({IssueState.DRAFT, IssueState.PRICED, IssueState.AWAITING_APPROVAL})


def _tx_hash(seq: int) -> str:
    return f"0x{seq:08x}{random.getrandbits(180):045x}"


def _now() -> datetime:
    return datetime.now(UTC)


def _budget(publisher: Publisher) -> Usdc:
    return Usdc.from_decimal(publisher.budget_remaining_usdc.replace(",", ""))


# Why a payout waits, in the words the decision log uses for it.
_HOLDS: dict[PayoutGate, tuple[str, str]] = {
    PayoutGate.AWAIT_IDENTITY: (
        "identity_at_first_payout",
        "the payout waits for {handle}'s identity verification, the only step a first "
        "payout needs",
    ),
    PayoutGate.BLOCKED_SANCTIONS: (
        "sanctions_screening",
        "{handle}'s wallet is on a sanctions list, so the payout is blocked until "
        "compliance reviews it",
    ),
    PayoutGate.BLOCKED_IDENTITY: (
        "identity_at_first_payout",
        "{handle}'s identity verification failed, so the payout waits for a person to "
        "review it",
    ),
}


class Store:
    def __init__(
        self,
        repository: Repository | None = None,
        *,
        screening: ScreeningProvider | None = None,
        identity: IdentityProvider | None = None,
    ) -> None:
        self.repo: Repository = repository or MemoryRepository()
        self.screening: ScreeningProvider = screening or SimulatedScreening(
            settings.screening_denylist.split(",")
        )
        self.identity: IdentityProvider = identity or SimulatedIdentity()
        self._ready = False
        self._ready_guard = threading.Lock()

    # ---------------------------------------------------------------- seeding

    def ensure_ready(self) -> None:
        """Seed an empty repository once, so a fresh database starts as the demo does.

        A database that already holds issues is left exactly as it is: that is the
        point of having one.
        """
        if self._ready:
            return
        with self._ready_guard:
            if self._ready:
                return
            with self.repo.try_lock("seed") as held:
                if held and self.repo.is_empty():
                    self._seed()
            self._ready = True

    def reset(self) -> None:
        with self._ready_guard:
            self.repo.reset()
            self._seed()
            self._ready = True

    def _seed(self) -> None:
        random.seed(7)
        for publisher in _seed_publishers().values():
            self.repo.save_publisher(publisher)
        for contributor in _seed_contributors().values():
            self.repo.save_contributor(contributor)
        for spec in _ISSUE_SPECS:
            self.repo.save_issues(self._build(spec))

    def _build(self, spec: dict) -> IssueRecord:
        publisher = self.repo.get_publisher(spec["publisher_id"])
        assert publisher is not None, spec["publisher_id"]
        signals = ComplexitySignals(**spec["signals"])
        proposal = propose(
            signals,
            compliance_driven=spec.get("compliance_driven", False),
            comparables=spec.get("comparables", 0),
            urgency=Usdc.from_decimal(spec["urgency"]) if spec.get("urgency") else None,
            risk_premium=Usdc.from_decimal(spec["risk"]) if spec.get("risk") else None,
            # The publisher's remaining budget actually constrains the price, which
            # is the whole point of reading it. Seeded demo issues declare none.
            affordability_ceiling=spec.get("affordability_ceiling"),
            # The floor depends on the tier's take rate, so the tier has to reach
            # the engine rather than being assumed.
            tier=publisher.tier,
        )
        target = IssueState(spec["state"])
        now = _now()
        rec = IssueRecord(
            id=self._next_issue_id(),
            repo=spec["repo"],
            number=spec["number"],
            title=spec["title"],
            summary=spec["summary"],
            # Seeded history is replayed through the real transitions from the price
            # approval onward, so a seeded issue is one the lifecycle could produce.
            state=target if target in _PRE_FUNDING else IssueState.AWAITING_APPROVAL,
            labels=spec["labels"],
            compliance_driven=spec.get("compliance_driven", False),
            acceptance_criteria=spec["criteria"],
            publisher_id=spec["publisher_id"],
            created_at=now - timedelta(days=spec["age_days"]),
            proposal=proposal,
        )

        self._log(
            rec,
            actor="agent",
            action="price_proposed",
            rule="complexity_score_v1",
            outcome=f"proposed {proposal.recommended} for {proposal.estimated_hours}h",
            cost="0.00",
        )

        if target in _PRE_FUNDING:
            return rec

        # Everything from FUNDED onward has approved money behind it.
        self._fund(rec, when=rec.created_at + timedelta(hours=6))
        self._log(
            rec,
            actor="publisher",
            action="price_approved",
            rule="human_checkpoint",
            outcome=f"approved {proposal.recommended} and committed the funds",
        )

        if target is IssueState.FUNDED:
            return rec

        if target is IssueState.REFUNDED:
            self._refund(rec, rule="deadline_passed")
            return rec

        # Claim onward.
        contributor_id = spec["contributor_id"]
        self._claim(rec, contributor_id, when=rec.created_at + timedelta(hours=20))
        self._log(
            rec,
            actor="contributor",
            action="claimed",
            rule="first_claim_wins",
            outcome=f"{self._handle(contributor_id)} took an exclusive 72h claim",
        )

        if target is IssueState.CLAIMED:
            return rec

        self._submit(rec, spec["pr"])

        if target is IssueState.IN_REVIEW:
            return rec

        self._review(
            rec,
            verdict=spec["verdict"],
            findings=spec["findings"],
            when=rec.created_at + timedelta(hours=44),
        )

        if target is IssueState.REWORK:
            return rec

        # No human confirms the verdict, so a passing one lands in ACCEPTED: the
        # grace window between the platform's decision and the publisher's merge.
        self._log(
            rec,
            actor="agent",
            action="verdict_passed",
            rule="review_agent_v1",
            outcome="verdict passed, awaiting the publisher's merge",
            when=rec.created_at + timedelta(hours=45),
        )

        if target is IssueState.ACCEPTED:
            return rec

        rec.accepted_by = "merge"
        self._release(rec, when=rec.created_at + timedelta(hours=45, minutes=2))
        return rec

    # -------------------------------------------------------------- reading

    def get(self, issue_id: str) -> IssueRecord | None:
        self.ensure_ready()
        return self.repo.get_issue(issue_id)

    def list_issues(self, states: Collection[IssueState] | None = None) -> list[IssueRecord]:
        self.ensure_ready()
        return self.repo.list_issues(states)

    def count_issues(self) -> int:
        self.ensure_ready()
        return self.repo.count_issues()

    def get_publisher(self, publisher_id: str) -> Publisher | None:
        self.ensure_ready()
        return self.repo.get_publisher(publisher_id)

    def list_publishers(self) -> list[Publisher]:
        self.ensure_ready()
        return self.repo.list_publishers()

    def save_publisher(self, publisher: Publisher) -> None:
        self.ensure_ready()
        self.repo.save_publisher(publisher)

    def get_contributor(self, contributor_id: str) -> Contributor | None:
        self.ensure_ready()
        return self.repo.get_contributor(contributor_id)

    def list_contributors(self) -> list[Contributor]:
        self.ensure_ready()
        return self.repo.list_contributors()

    def save_contributor(self, contributor: Contributor) -> None:
        self.ensure_ready()
        self.repo.save_contributor(contributor)

    def list_decisions(self, limit: int) -> list[Decision]:
        self.ensure_ready()
        return self.repo.list_decisions(limit)

    def save(self, *records: IssueRecord) -> None:
        """Persist records changed outside the lifecycle methods. Tests use this."""
        self.ensure_ready()
        self.repo.save_issues(*records)

    # ------------------------------------------------------------- primitives

    def _next_issue_id(self) -> str:
        return f"ISS-{self.repo.next_value('issue', 1001)}"

    def _handle(self, contributor_id: str) -> str:
        found = {c.id: c.handle for c in self.repo.list_contributors()}
        return found.get(contributor_id, contributor_id)

    def _log(
        self,
        rec: IssueRecord,
        *,
        actor: str,
        action: str,
        rule: str,
        outcome: str,
        cost: str | None = None,
        when: datetime | None = None,
    ) -> Decision:
        decision = Decision(
            id=f"DEC-{self.repo.next_value('decision', 1):04d}",
            issue_id=rec.id,
            actor=actor,  # type: ignore[arg-type]
            action=action,
            rule=rule,
            outcome=outcome,
            cost_usdc=cost,
            created_at=when or _now(),
        )
        rec.decisions.append(decision)
        return decision

    @staticmethod
    def _move(rec: IssueRecord, to: IssueState) -> None:
        """The only assignment of an issue's state, and it asks the domain first."""
        rec.state = lifecycle.transition(rec.state, to)

    def _fund(self, rec: IssueRecord, when: datetime | None = None) -> None:
        assert rec.proposal is not None
        self._move(rec, IssueState.FUNDED)
        committed = rec.proposal.recommended
        rec.escrow = EscrowCommitment(
            issue_id=rec.id,
            contract=ESCROW_CONTRACT,
            chain=CHAIN,
            tx_hash=_tx_hash(self.repo.next_value("tx", 0x4A1)),
            amount=money(committed),
            deadline=(when or _now()) + lifecycle.ESCROW_TERM,
        )
        rec.deadline = rec.escrow.deadline

    def _claim(self, rec: IssueRecord, contributor_id: str, when: datetime | None = None) -> None:
        self._move(rec, IssueState.CLAIMED)
        at = when or _now()
        rec.claim = Claim(
            contributor_id=contributor_id, issued_at=at, expires_at=at + lifecycle.CLAIM_WINDOW
        )
        rec.contributor_id = contributor_id

    def _submit(self, rec: IssueRecord, pr: dict) -> None:
        self._move(rec, IssueState.IN_REVIEW)
        rec.submission = Submission(**pr)

    def _review(
        self,
        rec: IssueRecord,
        *,
        verdict: str,
        findings: list[str],
        when: datetime | None = None,
    ) -> None:
        """Record the platform's verdict. It is the decision, not a draft for one."""
        self._move(
            rec,
            {
                "accept": IssueState.ACCEPTED,
                "rework": IssueState.REWORK,
                "reject": IssueState.REJECTED,
            }[verdict],
        )
        rec.review = Review(
            verdict=verdict,  # type: ignore[arg-type]
            findings=findings,
            decided_at=when or _now(),
        )

    def _release(
        self,
        rec: IssueRecord,
        *,
        rule: str = "escrow_acceptance_attestation",
        when: datetime | None = None,
    ) -> None:
        assert rec.escrow is not None and rec.proposal is not None
        self._move(rec, IssueState.PAID)
        rec.escrow.released = True
        rec.paid = rec.proposal.recommended
        rec.paid_at = when or _now()
        rec.payout_tx_hash = _tx_hash(self.repo.next_value("tx", 0x4A1))
        rec.payout_hold = None
        self._log(
            rec,
            actor="system",
            action="released",
            rule=rule,
            outcome=f"released {rec.paid} to contributor",
            cost="0.01",
            when=when,
        )

    def _refund(self, rec: IssueRecord, *, rule: str, when: datetime | None = None) -> None:
        assert rec.escrow is not None
        self._move(rec, IssueState.REFUNDED)
        rec.escrow.refunded = True
        self._log(
            rec,
            actor="system",
            action="refunded",
            rule=rule,
            outcome="no acceptable work arrived, funds returned",
            when=when,
        )

    def _expire_claim(self, rec: IssueRecord, *, rule: str, when: datetime) -> None:
        assert rec.claim is not None
        holder = self._handle(rec.claim.contributor_id)
        self._move(rec, IssueState.FUNDED)
        rec.claim = None
        rec.contributor_id = None
        self._log(
            rec,
            actor="system",
            action="claim_expired",
            rule=rule,
            outcome=f"{holder} opened no pull request in time, the issue is back in the pool",
            when=when,
        )

    def _relist(self, rec: IssueRecord, when: datetime) -> IssueRecord | None:
        """Offer a refunded, never-claimed issue again at a higher band.

        A silent empty listing teaches the publisher to stop funding, so the issue
        comes back priced to draw a claim. It comes back awaiting approval: a higher
        price is a new obligation, and only the publisher can take that on.
        """
        if rec.proposal is None or any(d.action == "claimed" for d in rec.decisions):
            return None
        publisher = self.repo.get_publisher(rec.publisher_id)
        assert publisher is not None
        proposal = pricing.relist(rec.proposal, ceiling=_budget(publisher))
        if proposal is None:
            self._log(
                rec,
                actor="agent",
                action="relist_declined",
                rule="affordability_ceiling",
                outcome=f"the remaining budget of {_budget(publisher)} cannot carry a "
                "higher price, so the issue was not re-listed",
                when=when,
            )
            return None

        relisted = IssueRecord(
            id=self._next_issue_id(),
            repo=rec.repo,
            number=rec.number,
            title=rec.title,
            summary=rec.summary,
            state=IssueState.PRICED,
            labels=list(rec.labels),
            compliance_driven=rec.compliance_driven,
            acceptance_criteria=list(rec.acceptance_criteria),
            publisher_id=rec.publisher_id,
            created_at=when,
            proposal=proposal,
            relisted_from=rec.id,
        )
        self._log(
            relisted,
            actor="agent",
            action="price_proposed",
            rule="relist_uplift",
            outcome=f"re-priced {rec.id} at {proposal.recommended}, up from "
            f"{rec.proposal.recommended}, after it drew no claim",
            when=when,
        )
        self._move(relisted, IssueState.AWAITING_APPROVAL)
        self._log(
            rec,
            actor="agent",
            action="relisted",
            rule="unclaimed_until_deadline",
            outcome=f"nobody claimed it, so it is re-listed as {relisted.id} at "
            f"{proposal.recommended} for the publisher to approve",
            when=when,
        )
        return relisted

    # ------------------------------------------------------------- compliance

    def _screen(
        self, kind: PartyKind, party_id: str, wallet: str, reason: ScreeningReason, now: datetime
    ) -> Screening:
        outcome, list_name = self.screening.screen(wallet)
        screening = Screening(
            party_kind=kind,
            party_id=party_id,
            wallet_address=wallet,
            outcome=outcome,
            provider=self.screening.name,
            reason=reason,
            checked_at=now,
            list_name=list_name,
        )
        self.repo.record_screening(screening)
        return screening

    def _verify_identity(
        self, rec: IssueRecord, contributor: Contributor, now: datetime
    ) -> IdentityStatus:
        """Verify at first payout and never before. Opens a provider session the first
        time a contributor is owed money, and reads its outcome after that."""
        current = IdentityStatus(contributor.identity_status)
        if current in {IdentityStatus.VERIFIED, IdentityStatus.FAILED}:
            return current

        outcome = (
            self.identity.status(contributor.identity_reference)
            if contributor.identity_reference
            else IdentityStatus.UNVERIFIED
        )
        if outcome is IdentityStatus.UNVERIFIED:
            # Never started, or the provider has no record of it: open a session now.
            contributor.identity_reference = self.identity.start(contributor.id)
            self._log(
                rec,
                actor="system",
                action="identity_check_started",
                rule="identity_at_first_payout",
                outcome=f"{contributor.handle} is owed a first payout, so {self.identity.name} "
                "verifies their identity now; the documents go to the provider, never to us",
                when=now,
            )
            outcome = self.identity.status(contributor.identity_reference)

        if outcome is IdentityStatus.VERIFIED:
            contributor.identity_verified_at = now
            self._log(
                rec,
                actor="system",
                action="identity_verified",
                rule="identity_at_first_payout",
                outcome=f"{self.identity.name} verified {contributor.handle}",
                when=now,
            )
        contributor.identity_status = (
            IdentityStatus.PENDING if outcome is IdentityStatus.UNVERIFIED else outcome
        ).value
        self.repo.save_contributor(contributor)
        return IdentityStatus(contributor.identity_status)

    def _pay(self, rec: IssueRecord, now: datetime) -> bool:
        """Release an entitled payout if compliance allows it, or record why it waits.

        Every payout screens the contributor again, not only the first, so someone
        listed after they were verified is caught before money moves.
        """
        assert rec.accepted_by is not None and rec.contributor_id is not None
        contributor = self.repo.get_contributor(rec.contributor_id)
        assert contributor is not None
        screening = self._screen(
            PartyKind.CONTRIBUTOR,
            contributor.id,
            contributor.wallet.address,
            ScreeningReason.PAYOUT,
            now,
        )
        identity = (
            self._verify_identity(rec, contributor, now)
            if screening.outcome is ScreeningOutcome.CLEAR
            else IdentityStatus(contributor.identity_status)
        )
        gate = compliance.payout_gate(screening.outcome, identity)
        rec.payout_checked_at = now

        if gate is PayoutGate.RELEASE:
            rule = (
                "silent_publisher_grace_period"
                if rec.accepted_by == "grace"
                else "escrow_acceptance_attestation"
            )
            self._release(rec, rule=rule, when=now)
            return True

        if rec.payout_hold != gate.value:
            rule, outcome = _HOLDS[gate]
            self._log(
                rec,
                actor="system",
                action="payout_held",
                rule=rule,
                outcome=outcome.format(handle=contributor.handle),
                when=now,
            )
        rec.payout_hold = gate.value
        return False

    def _screen_publisher_for_funding(self, rec: IssueRecord, now: datetime) -> None:
        """Screen the publisher before the first money is committed, and refuse a hit."""
        publisher = self.repo.get_publisher(rec.publisher_id)
        assert publisher is not None
        screening = self._screen(
            PartyKind.PUBLISHER,
            publisher.id,
            publisher.wallet.address,
            ScreeningReason.FUNDING,
            now,
        )
        if screening.outcome is ScreeningOutcome.CLEAR:
            return
        reason = (
            f"{publisher.name}'s wallet is on {screening.list_name}, so the commitment is "
            "refused until compliance reviews it"
        )
        self._log(
            rec,
            actor="system",
            action="funding_refused",
            rule="sanctions_screening",
            outcome=reason,
            when=now,
        )
        self.repo.save_issues(rec)
        raise ComplianceRefusal(reason)

    def rescreen(self, now: datetime | None = None) -> list[Screening]:
        """Screen every live counterparty whose last check is a day old.

        Live means party to an issue with money committed and the work not settled. A
        new hit is written on each of their open issues; the payout gate is what stops
        the money, at the moment it would move.
        """
        self.ensure_ready()
        now = now or _now()
        live = self.repo.list_issues(lifecycle.OPEN_STATES)
        parties: dict[tuple[PartyKind, str], list[IssueRecord]] = {}
        for rec in live:
            parties.setdefault((PartyKind.PUBLISHER, rec.publisher_id), []).append(rec)
            if rec.contributor_id:
                parties.setdefault((PartyKind.CONTRIBUTOR, rec.contributor_id), []).append(rec)

        screened: list[Screening] = []
        for (kind, party_id), issues in parties.items():
            last = self.repo.latest_screening(kind, party_id)
            if not compliance.rescreen_due(last.checked_at if last else None, now):
                continue
            party = (
                self.repo.get_publisher(party_id)
                if kind is PartyKind.PUBLISHER
                else self.repo.get_contributor(party_id)
            )
            assert party is not None
            screening = self._screen(
                kind, party_id, party.wallet.address, ScreeningReason.SCHEDULED, now
            )
            screened.append(screening)
            newly_listed = screening.outcome is ScreeningOutcome.HIT and (
                last is None or last.outcome is ScreeningOutcome.CLEAR
            )
            if newly_listed:
                for rec in issues:
                    self._log(
                        rec,
                        actor="system",
                        action="counterparty_flagged",
                        rule="continuous_screening",
                        outcome=f"the {kind.value} {party_id} is now on {screening.list_name}; "
                        "no money moves on this issue until compliance clears it",
                        when=now,
                    )
                    try:
                        self.repo.save_issues(rec)
                    except StaleIssue:
                        # Someone acted on it meanwhile. The payout gate still screens.
                        continue
        return screened

    def purge_expired(self, now: datetime | None = None) -> int:
        """Delete screening records past the published retention period."""
        self.ensure_ready()
        return self.repo.purge_screenings((now or _now()) - compliance.SCREENING_RETENTION)

    # ----------------------------------------------------------------- timers

    @staticmethod
    def _clocks(rec: IssueRecord) -> timers.Clocks:
        # The grace period only runs until something accepts the work; after that the
        # payout is owed, and what is timed is retrying it when compliance held it.
        passed = rec.review is not None and rec.review.verdict == "accept"
        awaiting_acceptance = passed and rec.accepted_by is None
        return timers.Clocks(
            state=rec.state,
            deadline=rec.deadline,
            claim_expires_at=rec.claim.expires_at if rec.claim else None,
            verdict_passed_at=rec.review.decided_at if awaiting_acceptance and rec.review else None,
            payout_held_since=rec.payout_checked_at if rec.payout_hold else None,
        )

    def _apply_due(
        self, rec: IssueRecord, now: datetime
    ) -> tuple[list[TimedAction], list[IssueRecord]]:
        """Carry out every timer due on `rec` at `now`. Returns what ran and any new
        records it created. Each action moves the state, so this ends in at most two."""
        applied: list[TimedAction] = []
        created: list[IssueRecord] = []
        while (action := timers.due(self._clocks(rec), now)) is not None:
            match action:
                case TimedAction.RELEASE_AFTER_GRACE:
                    # The verdict passed and the publisher went quiet past the grace
                    # window. Release rather than strand finished work.
                    rec.accepted_by = "grace"
                    self._pay(rec, now)
                case TimedAction.RETRY_PAYOUT:
                    self._pay(rec, now)
                case TimedAction.EXPIRE_CLAIM:
                    rule = (
                        "deadline_passed"
                        if timers.deadline_passed(self._clocks(rec), now)
                        else "claim_window_elapsed"
                    )
                    self._expire_claim(rec, rule=rule, when=now)
                case TimedAction.REFUND:
                    self._refund(rec, rule="deadline_passed", when=now)
                    relisted = self._relist(rec, when=now)
                    if relisted is not None:
                        created.append(relisted)
            applied.append(action)
        return applied, created

    def run_timers(self, rec: IssueRecord, now: datetime | None = None) -> list[TimedAction]:
        """Apply what is due on one issue and save it, refusing a stale copy.

        The sweeper's entry point. Raises StaleIssue when someone saved the issue
        after `rec` was loaded, so a person's action is never overwritten by a timer.
        """
        self.ensure_ready()
        applied, created = self._apply_due(rec, now or _now())
        if applied:
            self.repo.save_issues(rec, *created)
        return applied

    # ---------------------------------------------------------------- actions

    def advance(self, issue_id: str) -> IssueRecord:
        """Move an issue one step forward along the demo path.

        A timer that is already due is the step: the demo cannot claim an issue
        whose deadline has passed or merge past a silent publisher's grace window,
        any more than real time would let it.
        """
        self.ensure_ready()
        rec = self.repo.get_issue(issue_id)
        if rec is None:
            raise KeyError(issue_id)
        if rec.state in lifecycle.TERMINAL_STATES:
            raise lifecycle.IllegalTransition(rec.state, IssueState.PAID)

        created: list[IssueRecord] = []
        now = _now()
        if timers.due(self._clocks(rec), now) is not None:
            _, created = self._apply_due(rec, now)
            self.repo.save_issues(rec, *created)
            return rec

        match rec.state:
            case IssueState.AWAITING_APPROVAL:
                self._screen_publisher_for_funding(rec, now)
                self._fund(rec)
                self._log(
                    rec,
                    actor="publisher",
                    action="price_approved",
                    rule="human_checkpoint",
                    outcome="approved the price and committed funds",
                )
            case IssueState.FUNDED:
                contributors = [c.id for c in self.repo.list_contributors()]
                cid = contributors[0]
                self._claim(rec, cid)
                self._log(
                    rec,
                    actor="contributor",
                    action="claimed",
                    rule="first_claim_wins",
                    outcome=f"{self._handle(cid)} claimed the issue",
                )
            case IssueState.CLAIMED:
                self._submit(
                    rec,
                    {
                        "pr_number": rec.number + 400,
                        "head_sha": f"{random.getrandbits(160):040x}",
                        "checks_passed": True,
                        "files_changed": 3,
                        "additions": 62,
                        "deletions": 14,
                    },
                )
                assert rec.submission is not None
                self._log(
                    rec,
                    actor="contributor",
                    action="submitted",
                    rule="pull_request_opened",
                    outcome=f"opened PR #{rec.submission.pr_number} with checks passing",
                )
            case IssueState.IN_REVIEW:
                self._review(
                    rec,
                    verdict="accept",
                    findings=[
                        "Acceptance criteria 1 and 3 are covered by new tests.",
                        "No changes outside the files the criteria named.",
                    ],
                )
                self._log(
                    rec,
                    actor="agent",
                    action="verdict_issued",
                    rule="review_agent_v1",
                    outcome="accepted: criteria met, checks passing, diff in scope",
                    cost="6.00",
                )
            case IssueState.ACCEPTED if rec.accepted_by is None:
                # Merging is acceptance, and acceptance is what releases the money,
                # so the merge goes on the record before the release does.
                self._log(
                    rec,
                    actor="publisher",
                    action="merged",
                    rule="merge_is_acceptance",
                    outcome="publisher merged the pull request, which is acceptance",
                )
                rec.accepted_by = "merge"
                self._pay(rec, now)
            case IssueState.ACCEPTED:
                # Accepted, and the payout is held: the step is checking it again.
                self._pay(rec, now)
            case IssueState.REWORK:
                self._submit(
                    rec,
                    {
                        "pr_number": rec.number + 400,
                        "head_sha": f"{random.getrandbits(160):040x}",
                        "checks_passed": True,
                        "files_changed": 4,
                        "additions": 88,
                        "deletions": 21,
                    },
                )
            case _:
                # DRAFT, PRICED and REJECTED are legal states with no demo leg out of
                # them. Refuse with the state the lifecycle declares next rather than
                # returning an unchanged record behind a 200.
                nxt = next(iter(lifecycle.TRANSITIONS[rec.state]), rec.state)
                raise lifecycle.IllegalTransition(rec.state, nxt)
        self.repo.save_issues(rec, *created)
        return rec

    def approve_and_accept(self, issue_id: str) -> IssueRecord:
        """Run the remainder of the happy path in one call, for demos."""
        rec = self.get(issue_id)
        if rec is None:
            raise KeyError(issue_id)
        for _ in range(12):
            if rec.state in lifecycle.TERMINAL_STATES:
                break
            rec = self.advance(issue_id)
        return rec

    def publish(self, payload) -> IssueRecord:
        self.ensure_ready()
        publisher = self.repo.get_publisher(payload.publisher_id)
        if publisher is None:
            raise KeyError(payload.publisher_id)
        spec = {
            "repo": payload.repo,
            "number": payload.number or random.randint(100, 999),
            "title": payload.title,
            "summary": payload.summary or "Published through the web application.",
            "state": IssueState.PRICED,
            "labels": payload.labels or ["needs-price"],
            "compliance_driven": payload.compliance_driven,
            "criteria": [
                "The reported behaviour is reproduced by a test.",
                "The fix is covered by a test that fails before and passes after.",
                "Public behaviour is documented in the changelog.",
            ],
            "publisher_id": payload.publisher_id,
            "affordability_ceiling": _budget(publisher),
            "age_days": 0,
            "comparables": random.randint(0, 7),
            "signals": payload.signals
            or {
                "code_surface": 2.0,
                "requirement_clarity": 2.0,
                "test_coverage": 3.0,
                "dependency_depth": 2.0,
                "prior_attempts": 2.0,
                "blast_radius": 2.0,
            },
        }
        rec = self._build(spec)
        if rec.proposal is not None and not rec.proposal.fundable:
            # Refuse rather than publish a price the platform would lose money on.
            raise UnfundableIssue(rec.proposal.justification)
        self._move(rec, IssueState.AWAITING_APPROVAL)
        self._log(
            rec,
            actor="system",
            action="published",
            rule="publisher_created_issue",
            outcome="issue published and price proposed",
        )
        self.repo.save_issues(rec)
        return rec

    # ------------------------------------------------------------ projections

    def to_out(self, rec: IssueRecord) -> IssueOut:
        publisher = self.get_publisher(rec.publisher_id)
        assert publisher is not None
        proposal_out = None
        if rec.proposal:
            p = rec.proposal
            proposal_out = {
                "band_low": money(p.band_low),
                "band_high": money(p.band_high),
                "recommended": money(p.recommended),
                "estimated_hours": p.estimated_hours,
                "fundable": p.fundable,
                "complexity_score": p.complexity_score,
                "confidence": p.confidence,
                "signals": p.signals,
                "justification": p.justification,
                "comparables_note": p.comparables_note,
            }
        return IssueOut(
            id=rec.id,
            repo=rec.repo,
            number=rec.number,
            title=rec.title,
            summary=rec.summary,
            state=rec.state.value,
            labels=rec.labels,
            compliance_driven=rec.compliance_driven,
            acceptance_criteria=rec.acceptance_criteria,
            publisher_id=rec.publisher_id,
            publisher_name=publisher.name,
            created_at=rec.created_at,
            deadline=rec.deadline,
            proposal=proposal_out,
            escrow=rec.escrow,
            claim=rec.claim,
            submission=rec.submission,
            review=rec.review,
            contributor_id=rec.contributor_id,
            paid_usdc=str(rec.paid.decimal) if rec.paid else None,
            github_url=rec.github_url,
        )

    def summaries(self, records: list[IssueRecord]) -> list[IssueSummaryOut]:
        names = {p.id: p.name for p in self.list_publishers()}
        return [
            IssueSummaryOut(
                id=rec.id,
                repo=rec.repo,
                number=rec.number,
                title=rec.title,
                state=rec.state.value,
                labels=rec.labels,
                compliance_driven=rec.compliance_driven,
                publisher_name=names[rec.publisher_id],
                price_usdc=f"{rec.proposal.recommended.decimal:.2f}" if rec.proposal else None,
                confidence=rec.proposal.confidence if rec.proposal else None,
                deadline=rec.deadline,
                github_url=rec.github_url,
            )
            for rec in records
        ]

    def timeline(self, rec: IssueRecord) -> list[TimelineEntry]:
        return [
            TimelineEntry(
                id=d.id,
                actor=d.actor,
                action=d.action,
                outcome=d.outcome,
                created_at=d.created_at,
                cost_usdc=d.cost_usdc,
            )
            for d in rec.decisions
        ]

    def metrics(self) -> MetricsOut:
        records = self.list_issues()
        settled = [r for r in records if r.state is IssueState.PAID]
        funded = [r for r in records if r.deadline is not None]
        claimed_in_time = [
            r
            for r in funded
            if r.claim and (r.claim.issued_at - r.created_at) <= timedelta(hours=72)
        ]
        reviews = [r.review for r in records if r.review]
        publishers_with_issues = {r.publisher_id for r in funded}
        repeat = [
            p
            for p in publishers_with_issues
            if len([r for r in funded if r.publisher_id == p]) > 1
        ]

        matched = sum((r.paid.base_units for r in settled if r.paid), start=0)

        durations = [
            (r.decisions[-1].created_at - r.claim.issued_at).total_seconds() / 3600
            for r in settled
            if r.claim and r.decisions
        ]

        by_state: dict[str, int] = {}
        for r in records:
            by_state[r.state.value] = by_state.get(r.state.value, 0) + 1

        return MetricsOut(
            settled_issues=len(settled),
            funded_issues_published=len(funded),
            claim_rate_72h=round(len(claimed_in_time) / len(funded), 2) if funded else 0.0,
            acceptance_rate_first_review=(
                round(
                    len([x for x in reviews if x and x.verdict == "accept"]) / len(reviews), 2
                )
                if reviews
                else 0.0
            ),
            repeat_publisher_rate=(
                round(len(repeat) / len(publishers_with_issues), 2) if publishers_with_issues else 0.0
            ),
            matched_volume_usdc=f"{Usdc(matched).decimal:.2f}",
            median_hours_to_payout=round(sorted(durations)[len(durations) // 2], 1) if durations else None,
            dispute_rate=0.0,
            # Not modelled yet: a publisher who declines a passing verdict has no
            # transition, because silence past the grace window releases instead.
            publisher_overturn_rate=0.0,
            open_issues=sum(1 for r in records if lifecycle.is_open(r.state)),
            by_state=by_state,
        )


# ------------------------------------------------------------------ seed data


def _seed_publishers() -> dict[str, Publisher]:
    rows = [
        ("PUB-1", "Acme Payments", "company", "enterprise", "48,500.00", "0xAcme00000000000000000000000000000000A1"),
        ("PUB-2", "Northwind Data", "company", "team", "12,300.00", "0xN0rthw1nd0000000000000000000000000000b2"),
        ("PUB-3", "R. Okafor", "maintainer", "open", "480.00", "0x0kaf0r000000000000000000000000000000c3"),
        ("PUB-4", "Globex Logistics", "company", "enterprise", "91,000.00", "0xGl0bex00000000000000000000000000000004"),
        ("PUB-5", "Contoso Tooling", "company", "team", "6,750.00", "0xC0nt0s000000000000000000000000000000d5"),
    ]
    return {
        r[0]: Publisher(
            id=r[0],
            name=r[1],
            kind=r[2],  # type: ignore[arg-type]
            tier=r[3],  # type: ignore[arg-type]
            wallet=Wallet(address=r[5], chain=CHAIN),
            budget_remaining_usdc=r[4],
        )
        for r in rows
    }


def _seed_contributors() -> dict[str, Contributor]:
    rows = [
        ("CON-1", "amara.dev", 38, 21, "8,420.00", True),
        ("CON-2", "jonas_k", 27, 14, "5,110.00", True),
        ("CON-3", "priya.codes", 19, 9, "3,050.00", True),
        ("CON-4", "lin.wei", 12, 6, "1,880.00", True),
        ("CON-5", "tom_e", 5, 2, "540.00", False),
        ("CON-6", "sara_k", 2, 1, "180.00", False),
    ]
    return {
        r[0]: Contributor(
            id=r[0],
            handle=r[1],
            wallet=Wallet(address=f"0x{r[1].replace('.', ''):0<40}"[:42], chain=CHAIN),
            reputation=r[2],
            settled_issues=r[3],
            earned_usdc=r[4],
            # Seeded contributors who have been paid before were verified then.
            identity_status="verified" if r[5] else "unverified",
            identity_reference="seeded" if r[5] else None,
        )
        for r in rows
    }


_ISSUE_SPECS: list[dict] = [
    {
        "repo": "acme/ledger-core",
        "number": 412,
        "title": "Currency rounding is off by one cent on half-up values",
        "summary": "Rounding a half-cent value rounds down instead of up, so totals drift by a cent on long invoices.",
        "state": "FUNDED",
        "labels": ["bug", "payments"],
        "compliance_driven": False,
        "criteria": [
            "A test reproduces the round-down on a half-cent value.",
            "Rounding is half-up for every currency exponent the library supports.",
            "The existing rounding API keeps its signature.",
        ],
        "publisher_id": "PUB-1",
        "age_days": 2,
        "comparables": 7,
        "signals": {
            "code_surface": 2.0,
            "requirement_clarity": 1.0,
            "test_coverage": 2.0,
            "dependency_depth": 1.0,
            "prior_attempts": 3.0,
            "blast_radius": 2.0,
        },
    },
    {
        "repo": "acme/ledger-core",
        "number": 428,
        "title": "Add a plugin hook for custom settlement adapters",
        "summary": "Every new rail needs a fork. A documented hook would let publishers ship their own adapter.",
        "state": "IN_REVIEW",
        "labels": ["feature", "extensibility"],
        "compliance_driven": False,
        "criteria": [
            "A settlement adapter can be registered without touching core.",
            "The hook is documented with a working example.",
            "Core has no import of any concrete adapter.",
        ],
        "publisher_id": "PUB-1",
        "age_days": 9,
        "comparables": 4,
        "contributor_id": "CON-2",
        "pr": {
            "pr_number": 903,
            "head_sha": "9f2c1b7d4e6a8c0f3b5d7e9a1c3f5b7d9e1a3c5f",
            "checks_passed": True,
            "files_changed": 7,
            "additions": 214,
            "deletions": 38,
        },
        "signals": {
            "code_surface": 4.0,
            "requirement_clarity": 3.0,
            "test_coverage": 3.0,
            "dependency_depth": 3.0,
            "prior_attempts": 2.0,
            "blast_radius": 3.0,
        },
    },
    {
        "repo": "globex/parse-locale",
        "number": 87,
        "title": "Memory safety: unterminated locale string reachable from the network parser",
        "summary": "A crafted locale tag reaches an unsafe read in the parser. Reachable from any HTTP caller.",
        "state": "CLAIMED",
        "labels": ["security", "cve"],
        "compliance_driven": True,
        "criteria": [
            "A fuzz case reproduces the out-of-bounds read.",
            "The parser rejects unterminated input before allocation.",
            "The fix ships with a regression test and a CVE reference in the changelog.",
        ],
        "publisher_id": "PUB-4",
        # Claimed 52 hours ago, so the 72-hour claim is still live when the demo
        # starts and the sweeper only returns it to the pool if nobody acts for a day.
        "age_days": 3,
        "comparables": 2,
        "contributor_id": "CON-1",
        "risk": "1200.00",
        "urgency": "800.00",
        "signals": {
            "code_surface": 3.0,
            "requirement_clarity": 2.0,
            "test_coverage": 4.0,
            "dependency_depth": 4.0,
            "prior_attempts": 1.0,
            "blast_radius": 5.0,
        },
    },
    {
        "repo": "northwind/httpx-retry",
        "number": 219,
        "title": "Retry budget ignores jitter, causing thundering herds",
        "summary": "All clients retry on the same schedule, so a blip becomes an outage.",
        "state": "PAID",
        "labels": ["bug", "reliability"],
        "compliance_driven": False,
        "criteria": [
            "Backoff includes jitter by default.",
            "The old fixed schedule is available behind a flag.",
            "A test asserts the jittered spread is non-zero.",
        ],
        "publisher_id": "PUB-2",
        "age_days": 21,
        "comparables": 6,
        "contributor_id": "CON-3",
        "pr": {
            "pr_number": 611,
            "head_sha": "3a5c7e9b1d3f5a7c9e1b3d5f7a9c1e3b5d7f9a1c",
            "checks_passed": True,
            "files_changed": 4,
            "additions": 96,
            "deletions": 31,
        },
        "verdict": "accept",
        "findings": ["Criteria met.", "Jitter spread verified in the new test."],
        "signals": {
            "code_surface": 2.0,
            "requirement_clarity": 2.0,
            "test_coverage": 3.0,
            "dependency_depth": 2.0,
            "prior_attempts": 1.0,
            "blast_radius": 3.0,
        },
    },
    {
        "repo": "contoso/schema-cli",
        "number": 55,
        "title": "Add a dry-run flag to schema migrations",
        "summary": "Operators have no way to see what a migration will do before running it.",
        "state": "PAID",
        "labels": ["feature", "dx"],
        "compliance_driven": False,
        "criteria": [
            "A dry run prints the statements without executing them.",
            "The flag is documented in the CLI help.",
        ],
        "publisher_id": "PUB-5",
        "age_days": 30,
        "comparables": 3,
        "contributor_id": "CON-4",
        "pr": {
            "pr_number": 302,
            "head_sha": "c1e3a5b7d9f1c3e5a7b9d1f3c5e7a9b1d3f5c7e9",
            "checks_passed": True,
            "files_changed": 3,
            "additions": 74,
            "deletions": 12,
        },
        "verdict": "accept",
        "findings": ["Both criteria covered.", "Help text is accurate."],
        "signals": {
            "code_surface": 2.0,
            "requirement_clarity": 1.0,
            "test_coverage": 2.0,
            "dependency_depth": 1.0,
            "prior_attempts": 1.0,
            "blast_radius": 1.0,
        },
    },
    {
        "repo": "acme/ledger-adapters",
        "number": 178,
        "title": "EU Cyber Resilience Act: produce a maintenance record for shipped dependencies",
        "summary": "The 2027 obligations need evidence that dependencies are maintained, not a snapshot of versions.",
        "state": "AWAITING_APPROVAL",
        "labels": ["compliance", "reporting"],
        "compliance_driven": True,
        "criteria": [
            "A report lists each shipped dependency with its last fix and its maintainer.",
            "The report exports to a format an auditor accepts.",
            "Generation is deterministic and repeatable.",
        ],
        "publisher_id": "PUB-4",
        "age_days": 1,
        "comparables": 1,
        "signals": {
            "code_surface": 4.0,
            "requirement_clarity": 4.0,
            "test_coverage": 3.0,
            "dependency_depth": 3.0,
            "prior_attempts": 2.0,
            "blast_radius": 4.0,
        },
    },
    {
        "repo": "northwind/httpx-retry",
        "number": 231,
        "title": "Support per-request timeouts that override the client default",
        "summary": "A single slow endpoint forces a global timeout increase for everyone.",
        "state": "REWORK",
        "labels": ["feature", "api"],
        "compliance_driven": False,
        "criteria": [
            "A per-request timeout overrides the client default.",
            "The override is documented.",
            "Backwards compatibility is preserved for callers that pass nothing.",
        ],
        "publisher_id": "PUB-2",
        "age_days": 11,
        "comparables": 4,
        "contributor_id": "CON-5",
        "pr": {
            "pr_number": 640,
            "head_sha": "5b7d9f1a3c5e7b9d1f3a5c7e9b1d3f5a7c9e1b3d",
            "checks_passed": True,
            "files_changed": 2,
            "additions": 41,
            "deletions": 6,
        },
        "verdict": "rework",
        "findings": [
            "Criterion 3 is not covered: a caller passing no timeout changes behaviour.",
            "Add a regression test for the default path before resubmitting.",
        ],
        "signals": {
            "code_surface": 3.0,
            "requirement_clarity": 3.0,
            "test_coverage": 4.0,
            "dependency_depth": 2.0,
            "prior_attempts": 4.0,
            "blast_radius": 2.0,
        },
    },
    {
        "repo": "contoso/schema-cli",
        "number": 61,
        "title": "Migrate the config loader from TOML to a pluggable format",
        "summary": "Teams keep asking for YAML. A pluggable loader would end the argument.",
        "state": "REFUNDED",
        "labels": ["feature"],
        "compliance_driven": False,
        "criteria": ["Loader is pluggable.", "Existing TOML configs keep working."],
        "publisher_id": "PUB-5",
        "age_days": 40,
        "comparables": 2,
        "signals": {
            "code_surface": 5.0,
            "requirement_clarity": 5.0,
            "test_coverage": 4.0,
            "dependency_depth": 3.0,
            "prior_attempts": 3.0,
            "blast_radius": 4.0,
        },
    },
]


store = Store(build_repository(settings.database_url))
