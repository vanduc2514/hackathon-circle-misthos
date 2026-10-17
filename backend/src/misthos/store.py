"""The lifecycle store.

Every change to an issue's state goes through this module, which is what the
architecture means by a single writer: the API and the sweeper both
call it, none of them assigns a state, and every move is checked against the domain's
transition table and leaves a decision behind.

Where the result is kept is the repository's business. With no database configured
it is memory, lost on restart; with `MISTHOS_DATABASE_URL` set it is Postgres or a
SQLite file, and it survives.

Money moves only through the chain gateway, and every movement is appended to the
issue's ledger with the transaction the chain returned. Each action on an issue holds
that issue's lock, so a person, a second API process and the sweeper take turns
rather than racing between the chain call and the save.

GitHub is read when an issue is published or edited, and its pull request events
drive the lifecycle (see `services/github/events.py`). What the platform posts back,
the acceptance criteria, the review, the commit status and the settlement, is queued
during an action and sent only once the action has been saved, so GitHub never
shows a step the database does not have. A failed post is logged and never undoes
the step.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from misthos.config import settings
from misthos.domain import comparables, compliance, ledger, timers
from misthos.domain import criteria as acceptance
from misthos.domain import issue as lifecycle
from misthos.domain.comparables import Comparable, SettledWork
from misthos.domain.compliance import (
    ComplianceRefusal,
    IdentityStatus,
    PartyKind,
    PayoutGate,
    Screening,
    ScreeningOutcome,
    ScreeningReason,
)
from misthos.domain.issue import FundingTerms, IssueState
from misthos.domain.ledger import Divergence, MoneyEvent, MoneyEventKind, OnChain
from misthos.domain.money import Usdc
from misthos.domain.paid import PaidCall
from misthos.domain.policy import (
    PolicyRefusal,
    SpendingPolicy,
    approves_own_payout,
    category_breaches,
    committed_this_month,
    login,
    may_approve,
    needs_approval,
)
from misthos.domain.pricing import (
    WEIGHTS,
    ComplexitySignals,
    PriceProposal,
    PriceRefused,
    check_price,
    effort,
    fee_bps,
    propose,
)
from misthos.domain.review import (
    Judgement,
    Submitted,
    Verdict,
    decide,
    ready_for_review,
)
from misthos.domain.signals import IssueFacts, Reading
from misthos.domain.signals import read as read_signals
from misthos.domain.timers import TimedAction
from misthos.models.records import IssueRecord
from misthos.observability.metrics import (
    ADOPTED,
    DIVERGENCE_ALERTS,
    DIVERGENCES,
    MONEY_EVENTS,
    PRICE_SECONDS,
    REVIEW_COST_USDC,
    REVIEW_SECONDS,
)
from misthos.repositories import (
    MemoryRepository,
    Repository,
    StaleIssue,
    build_repository,
)
from misthos.schemas import (
    Account,
    Claim,
    Contributor,
    Decision,
    EscrowCommitment,
    IssueOut,
    IssueSummaryOut,
    MetricsOut,
    Publisher,
    RepoConnection,
    Review,
    Submission,
    TimelineEntry,
    Wallet,
    money,
)
from misthos.services import metrics, reputation
from misthos.services.chain import (
    ChainGateway,
    ChainRevert,
    ChainUnavailable,
    load_deployment,
)
from misthos.services.chain.factory import build_chain
from misthos.services.compliance import (
    IdentityProvider,
    ScreeningProvider,
    build_identity,
    build_screening,
)
from misthos.services.coordination import ISSUE_LOCK_TTL, Busy, Coordinator, build_coordinator
from misthos.services.github import (
    GitHubError,
    GitHubGateway,
    PullRequest,
    ReviewEvent,
    StatusState,
    build_github,
)
from misthos.services.review import Reviewer, build_reviewer

__all__ = ["IssueRecord", "Store", "store"]


# Recorded on every commitment: the address `mise run contracts:deploy` recorded under
# contracts/deployments/, or MISTHOS_ESCROW_CONTRACT.
ESCROW = load_deployment(settings)
ESCROW_CONTRACT = ESCROW.address

REPO_POOL = [
    "acme/ledger-core",
    "northwind/httpx-retry",
    "contoso/schema-cli",
    "acme/ledger-adapters",
    "globex/parse-locale",
]

_PRE_FUNDING = frozenset({IssueState.DRAFT, IssueState.PRICED, IssueState.AWAITING_APPROVAL})


log = logging.getLogger("misthos.store")

_REVIEW_EVENTS = {
    "accept": (ReviewEvent.APPROVE, StatusState.SUCCESS),
    "rework": (ReviewEvent.REQUEST_CHANGES, StatusState.FAILURE),
    "reject": (ReviewEvent.REQUEST_CHANGES, StatusState.FAILURE),
}


class UnreadableIssue(Exception):
    """GitHub would not give us the issue the publisher asked to price."""


class NotTheSubmission(Exception):
    """A pull request event about a pull request this issue is not waiting on."""


class AlreadyListed(Exception):
    """A GitHub issue has one open listing at a time.

    A second listing of the same issue lets one pull request be reviewed, accepted and
    paid on each of them (#120). Open is every state short of PAID or REFUNDED, the
    ones `find_open` routes GitHub's events to.
    """

    def __init__(self, existing: IssueRecord) -> None:
        super().__init__(
            f"{existing.repo}#{existing.number} is already on Misthos as {existing.id}, "
            f"which is {existing.state.value}; a GitHub issue is listed once while it is open"
        )
        self.issue_id = existing.id


class DisputeRefused(Exception):
    """A dispute the verdict cannot take: one dispute per verdict."""


class AccountExists(Exception):
    """A wallet chooses its role once."""


class CriteriaNotApproved(Exception):
    """No funding before the publisher approves the acceptance criteria (#21)."""


class UntestableCriteria(Exception):
    """Criteria a reviewer could not judge are not approved (#38)."""

    def __init__(self, problems: list[acceptance.Problem]) -> None:
        super().__init__("; ".join(f"criterion {p.index} {p.reason}" for p in problems))
        self.problems = problems


class DeclineRefused(Exception):
    """A publisher declines a passing verdict once, and only inside the grace period;
    after that the merge or the grace period settles it."""



# How long a publish waits for another publish of the same GitHub issue to finish. The
# other one holds the listing only to check it and save, which takes well under this.
LISTING_WAIT = timedelta(seconds=5)
_LISTING_POLL_SECONDS = 0.02


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
    # No person clears this one: the retry screens the wallet again, and pays once it
    # is off the list (compliance.PAYOUT_RETRY_INTERVAL).
    PayoutGate.BLOCKED_SANCTIONS: (
        "sanctions_screening",
        "{handle}'s wallet is on a sanctions list, so the payout is held; it is screened "
        "again every hour and released once the wallet is no longer listed",
    ),
    PayoutGate.BLOCKED_IDENTITY: (
        "identity_at_first_payout",
        "{handle}'s identity verification failed, so the payout waits for a person to "
        "review it",
    ),
}

def _release_refusal(exc: Exception) -> str:
    """Why a release did not land, in words fit for the decision log, which is public:
    the contract's error by name, never an RPC address or a key reference."""
    if isinstance(exc, ChainRevert):
        return f"the escrow refused it: {str(exc).split(':', 1)[0]}"
    return "the chain could not be reached"


# What a new listing stops, in the words written on each open issue of the party. Only
# what the code enforces: a publisher is screened at funding, not at payout.
_FLAGGED: dict[PartyKind, str] = {
    PartyKind.CONTRIBUTOR: "no payout is released to them while their wallet is listed, "
    "since every payout screens it again",
    PartyKind.PUBLISHER: "new commitments from them are refused while their wallet is "
    "listed; the money already committed here is not held, and still goes to the "
    "contributor on acceptance or back to the publisher at the deadline",
}


class Store:
    def __init__(
        self,
        repository: Repository | None = None,
        *,
        screening: ScreeningProvider | None = None,
        identity: IdentityProvider | None = None,
        chain: ChainGateway | None = None,
        coordinator: Coordinator | None = None,
        github: GitHubGateway | None = None,
        reviewer: Reviewer | None = None,
        second_reviewer: Reviewer | None = None,
    ) -> None:
        self.repo: Repository = repository or MemoryRepository()
        # The escrow is the deployed MisthosEscrow (#69).
        self.chain: ChainGateway = chain or build_chain(
            settings, lambda: [rec.id for rec in self.repo.list_issues()]
        )
        self.coordinator: Coordinator = coordinator or build_coordinator(
            settings.redis_url, self.repo
        )
        self.github: GitHubGateway = github or build_github(
            settings.github_app_id, settings.github_app_private_key, settings.github_api_url
        )
        # GitHub posts queued by the action running on this thread, sent after it saves.
        self._outbox = threading.local()
        self.reviewer: Reviewer = reviewer or _reviewer(settings.review_model)
        # A disputed verdict is reviewed again, by a stronger model when one is set.
        self.second_reviewer: Reviewer = second_reviewer or (
            _reviewer(settings.review_dispute_model)
            if settings.review_dispute_model
            else self.reviewer
        )
        self.screening: ScreeningProvider = screening or build_screening(settings)
        self.identity: IdentityProvider = identity or build_identity(settings)

    # -------------------------------------------------------------- reading

    def get(self, issue_id: str) -> IssueRecord | None:
        return self.repo.get_issue(issue_id)

    def list_issues(self, states: Collection[IssueState] | None = None) -> list[IssueRecord]:
        return self.repo.list_issues(states)

    def get_publisher(self, publisher_id: str) -> Publisher | None:
        return self.repo.get_publisher(publisher_id)

    def list_publishers(self) -> list[Publisher]:
        return self.repo.list_publishers()

    def save_publisher(self, publisher: Publisher) -> None:
        self.repo.save_publisher(publisher)

    def get_contributor(self, contributor_id: str) -> Contributor | None:
        return self.repo.get_contributor(contributor_id)

    def list_contributors(self) -> list[Contributor]:
        return self.repo.list_contributors()

    def save_contributor(self, contributor: Contributor) -> None:
        self.repo.save_contributor(contributor)

    def list_decisions(self, limit: int) -> list[Decision]:
        return self.repo.list_decisions(limit)

    def save(self, *records: IssueRecord) -> None:
        """Persist records changed outside the lifecycle methods. Tests use this."""
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

    def _book(
        self,
        rec: IssueRecord,
        kind: MoneyEventKind,
        amount: Usdc,
        counterparty_id: str,
        tx_hash: str,
        at: datetime,
    ) -> None:
        event = MoneyEvent(
            issue_id=rec.id,
            kind=kind,
            amount=amount,
            counterparty_id=counterparty_id,
            tx_hash=tx_hash,
            occurred_at=at,
        )
        rec.money_events.append(event)
        # Refuse here, before the save, any history money cannot have taken.
        ledger.position(rec.money_events)
        self._later(f"{kind} on {rec.id} recorded", lambda: _observe_money(event))

    @staticmethod
    def _committed(rec: IssueRecord) -> Usdc:
        held = ledger.position(rec.money_events)
        assert held is not None, f"{rec.id} has no commitment to settle"
        return held.committed

    def _funding_terms(
        self, rec: IssueRecord, publisher: Publisher, at: datetime
    ) -> lifecycle.FundingTerms:
        """The terms this approval funds the issue on: the ones already approved, or new.

        Once the escrow holds a commitment, the terms it was committed under are the
        only ones it is booked against, whatever the clock says now: a deadline
        recomputed from `now` is later than the one the wallet committed to, because the
        escrow's term starts at the commit, so the check could never be satisfied again
        and the money would sit in escrow unbooked (#126).

        With nothing committed, terms approved in the last `COMMIT_WINDOW` stand, so
        approving twice does not move the deadline under a publisher who is mid-click;
        after that, approving again sets new ones.
        """
        assert rec.price is not None
        current = rec.funding
        if current is not None and (
            current.stand(at)
            or self.chain.commitment(rec.id) is not None
        ):
            return current
        return lifecycle.FundingTerms(
            amount=check_price(rec.price),
            # The rate is fixed with the money. The escrow enforces the rate it was given
            # on release, so the recorded fee always matches the transfer (#32).
            fee_bps=fee_bps(),
            # The wallet the publisher funds from, which is the one that signs in and
            # commits. A Circle wallet they set up never replaces it (#123).
            wallet=publisher.wallet.address,
            # Whole seconds, as the escrow holds it, so the plan, the escrow and the
            # booking all name the same instant.
            deadline=(at + lifecycle.ESCROW_TERM).replace(microsecond=0),
            approved_at=at,
        )

    def _fund(self, rec: IssueRecord, when: datetime | None = None) -> None:
        assert rec.proposal is not None
        if rec.criteria_approved_at is None:
            raise CriteriaNotApproved(
                f"{rec.id} cannot be funded until the publisher approves its acceptance criteria"
            )
        publisher = self.repo.get_publisher(rec.publisher_id)
        assert publisher is not None
        # The state moves first so an illegal step never reaches the chain. If the
        # chain refuses, nothing is saved and the copy in hand is thrown away.
        self._move(rec, IssueState.FUNDED)
        at = when or _now()
        # The publisher's own price, set when the issue was published and bound here
        # without a second opinion (#142). The engine is not consulted: what the buyer
        # said the work is worth is what the contributor is owed.
        if rec.price is None:
            raise PriceRefused(f"{rec.id} has no price, so there is nothing to commit")
        committed = check_price(rec.price)
        # The terms the approval fixed, or new ones if this is the first approval or the
        # last ones have lapsed. Booking checks the commitment against these rather than
        # against the clock, which is what let a commitment be refused for good once an
        # hour had passed between the plan and the booking (#126).
        terms = self._funding_terms(rec, publisher, at)
        rec.funding = terms
        deadline = terms.deadline
        # The approved price goes to the escrow as its ceiling first: the escrow
        # refuses any commitment without one or above it, so what an agent can commit
        # is bounded by what a person approved, on chain and not in this code.
        self.chain.set_ceiling(rec.id, terms.amount, at)
        # The rate is fixed here, with the money. The escrow enforces the rate it was
        # given on release, so the record always matches the transfer (#32, #142).
        rate_bps = terms.fee_bps
        tx = self.chain.commit(rec.id, terms.wallet, committed, deadline, at, rate_bps)
        self._book(rec, MoneyEventKind.COMMITTED, committed, publisher.id, tx, at)
        rec.escrow = EscrowCommitment(
            issue_id=rec.id,
            contract=ESCROW_CONTRACT,
            chain=settings.chain,
            tx_hash=tx,
            amount=money(committed),
            deadline=deadline,
            fee_bps=rate_bps,
        )
        rec.deadline = rec.escrow.deadline
        # The amount committed, not the engine's suggestion: the comment is the public
        # record of what is in escrow, and the two differ on any issue re-priced after
        # it was published (#150).
        criteria = _criteria_comment(rec, committed)
        self._post(
            f"criteria on {rec.repo}#{rec.number}",
            lambda gh: gh.comment(rec.repo, rec.number, criteria),
        )

    def _claim(self, rec: IssueRecord, contributor_id: str, when: datetime | None = None) -> None:
        self._move(rec, IssueState.CLAIMED)
        at = when or _now()
        rec.claim = Claim(
            contributor_id=contributor_id, issued_at=at, expires_at=at + lifecycle.CLAIM_WINDOW
        )
        rec.contributor_id = contributor_id

    def _submit(self, rec: IssueRecord, pr: dict) -> None:
        if rec.review is not None and rec.review.head_sha == pr["head_sha"]:
            # A commit gets one verdict, and the review only takes a commit without
            # one. Submitted again, it would leave the issue in IN_REVIEW, where no
            # review takes it and no timer runs, with the money held there (#119).
            raise NotTheSubmission(
                f"the review already judged {pr['head_sha'][:7]}, the head of "
                f"#{pr['pr_number']}; push the requested changes as a new commit, then "
                "submit again"
            )
        self._move(rec, IssueState.IN_REVIEW)
        rec.submission = Submission(**pr)
        sha = rec.submission.head_sha
        self._post(
            f"pending status on {rec.repo}@{sha[:7]}",
            lambda gh: gh.status(
                rec.repo,
                sha,
                StatusState.PENDING,
                "Misthos is reviewing this against the acceptance criteria",
            ),
        )

    def _review(
        self,
        rec: IssueRecord,
        *,
        verdict: str,
        findings: list[str],
        when: datetime | None = None,
        reviewer: str | None = None,
        seconds: float | None = None,
        cost: Usdc | None = None,
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
            head_sha=rec.submission.head_sha if rec.submission else None,
            reviewer=reviewer,
            seconds=seconds,
            cost_usdc=_cost_text(cost) if cost is not None else None,
        )
        if rec.submission is not None:
            pr, sha = rec.submission.pr_number, rec.submission.head_sha
            event, state = _REVIEW_EVENTS[verdict]
            body = _review_comment(verdict, findings)
            self._post(
                f"review on {rec.repo}#{pr}",
                lambda gh: gh.review(rec.repo, pr, sha, event, body),
            )
            self._post(
                f"{state.value} status on {rec.repo}@{sha[:7]}",
                lambda gh: gh.status(rec.repo, sha, state, f"Misthos verdict: {verdict}"),
            )

    def _release(
        self,
        rec: IssueRecord,
        *,
        rule: str = "escrow_acceptance_attestation",
        when: datetime | None = None,
    ) -> None:
        assert rec.escrow is not None and rec.contributor_id is not None
        contributor = self.repo.get_contributor(rec.contributor_id)
        assert contributor is not None
        at = when or _now()
        amount = self._committed(rec)
        # The state moves only once the chain has taken the release. If it reverts or
        # cannot be reached, the issue stays ACCEPTED and `_pay` holds the payout rather
        # than losing the acceptance with it (#125).
        tx = self.chain.release(rec.id, contributor.wallet.address, amount, at)
        self._move(rec, IssueState.PAID)
        self._book(rec, MoneyEventKind.RELEASED, amount, contributor.id, tx, at)
        rec.escrow.released = True
        # The fee comes out of the commitment rather than being added to it: the
        # publisher paid the fix price and nothing else. The rate is the one the escrow
        # holds for this issue, read back so the recorded fee is the transfer.
        held = self.chain.commitment(rec.id)
        rate = held.fee_bps if held is not None else rec.escrow.fee_bps
        fee = Usdc(amount.base_units * rate // 10_000)
        rec.platform_fee = fee
        rec.paid = amount - fee
        rec.paid_at = at
        rec.payout_tx_hash = tx
        rec.payout_hold = None
        # Public: the amount and who earned it, as the issue's price already was. The
        # transfer and the wallet stay private (docs/PRIVACY.md). What the contributor
        # received is the commitment less the platform's fee.
        note = (
            f"Paid **{rec.paid} USDC** to @{contributor.handle}. The payment was released "
            f"from escrow on {settings.chain} when the work was accepted."
        )
        self._post(
            f"settlement on {rec.repo}#{rec.number}",
            lambda gh: gh.comment(rec.repo, rec.number, note),
        )
        self._later(
            f"standing of {contributor.id}", lambda: self.refresh_standing(contributor.id)
        )
        self._log(
            rec,
            actor="system",
            action="released",
            rule=rule,
            outcome=(
                f"released {rec.paid} to contributor and {fee} platform fee at {rate} bps"
            ),
            cost="0.01",
            when=when,
        )

    def _refund(self, rec: IssueRecord, *, rule: str, when: datetime | None = None) -> None:
        assert rec.escrow is not None
        self._move(rec, IssueState.REFUNDED)
        at = when or _now()
        amount = self._committed(rec)
        tx = self.chain.refund(rec.id, at)
        self._book(rec, MoneyEventKind.REFUNDED, amount, rec.publisher_id, tx, at)
        rec.escrow.refunded = True
        note = (
            f"The deadline passed without accepted work, so the {amount} USDC "
            "commitment went back to the publisher."
        )
        self._post(
            f"refund on {rec.repo}#{rec.number}",
            lambda gh: gh.comment(rec.repo, rec.number, note),
        )
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

        publisher = self.repo.get_publisher(rec.publisher_id)
        assert publisher is not None
        policy = _policy(publisher)
        if (
            gate is PayoutGate.RELEASE
            and needs_approval(policy, self._committed(rec))
            and not any(d.action == "release_approved" for d in rec.decisions)
        ):
            gate = PayoutGate.AWAIT_APPROVER

        if gate is PayoutGate.RELEASE:
            rule = (
                "silent_publisher_grace_period"
                if rec.accepted_by == "grace"
                else "escrow_acceptance_attestation"
            )
            try:
                self._release(rec, rule=rule, when=now)
                return True
            except (ChainRevert, ChainUnavailable) as exc:
                # The acceptance stands whatever the chain says. Raising here used to
                # throw the merge away with it, so the publisher could then decline work
                # they had merged (#125); the payout is held and tried again instead.
                log.warning("release of %s did not land, holding the payout: %s", rec.id, exc)
                gate, refused = PayoutGate.RELEASE_FAILED, _release_refusal(exc)

        if rec.payout_hold != gate.value:
            if gate is PayoutGate.RELEASE_FAILED:
                rule = "escrow_release"
                outcome = (
                    f"the release did not land ({refused}); the acceptance stands, and the "
                    "release is tried again on the sweeper's next pass"
                )
            elif gate is PayoutGate.AWAIT_APPROVER:
                rule = "release_threshold"
                outcome = (
                    f"the release of {self._committed(rec)} is over {publisher.name}'s "
                    f"{policy.approval_threshold} threshold, so it waits for one of "
                    f"{', '.join(policy.approvers)} to approve it"
                )
            else:
                rule, template = _HOLDS[gate]
                outcome = template.format(handle=contributor.handle)
            self._log(
                rec,
                actor="system",
                action="payout_held",
                rule=rule,
                outcome=outcome,
                when=now,
            )
        rec.payout_hold = gate.value
        return False

    def _check_category_limits(self, rec: IssueRecord, now: datetime) -> None:
        """Refuse a commitment that would take a category past the publisher's own
        monthly limit, and say which and by how much."""
        publisher = self.repo.get_publisher(rec.publisher_id)
        assert publisher is not None
        policy = _policy(publisher)
        if not policy.category_limits:
            return
        committed = committed_this_month(
            (
                (other.labels, other.money_events)
                for other in self.repo.list_issues()
                if other.publisher_id == publisher.id and other.id != rec.id
            ),
            now,
        )
        # The money about to be committed is the publisher's price. The engine's suggestion
        # is a different number on any issue re-priced after publishing, so a limit held
        # against the suggestion would let a commitment past the line the publisher set, or
        # refuse one that fits (#150). An issue with no price is refused by `_fund`, which
        # follows this with the reason; there is no amount here to hold against a limit.
        if rec.price is None:
            return
        breaches = category_breaches(policy, rec.labels, check_price(rec.price), committed)
        if not breaches:
            return
        reason = "; ".join(breaches)
        self._log(
            rec,
            actor="system",
            action="funding_refused",
            rule="category_limit",
            outcome=f"{publisher.name}'s own policy refuses this commitment: {reason}",
            when=now,
        )
        self.repo.save_issues(rec)
        raise PolicyRefusal(reason)

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
            "refused while it is listed"
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
        new hit is written on each of their open issues, saying what it stops: the
        payout gate stops a listed contributor's payout at the moment it would move,
        and funding stops a listed publisher's next commitment. Money a listed
        publisher has already committed is not held (ARCHITECTURE.md, Compliance).
        """
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
                    try:
                        with self._exclusive(rec.id):
                            fresh = self.repo.get_issue(rec.id)
                            assert fresh is not None
                            self._log(
                                fresh,
                                actor="system",
                                action="counterparty_flagged",
                                rule="continuous_screening",
                                outcome=f"the {kind.value} {party_id} is now on "
                                f"{screening.list_name}; {_FLAGGED[kind]}",
                                when=now,
                            )
                            self.repo.save_issues(fresh)
                    except (Busy, StaleIssue):
                        # Someone is acting on it. The payout gate still screens.
                        continue
        return screened

    def purge_expired(self, now: datetime | None = None) -> int:
        """Delete screening records past the published retention period."""
        return self.repo.purge_screenings((now or _now()) - compliance.SCREENING_RETENTION)

    # ------------------------------------------------------------- GitHub posts

    def _post(self, what: str, send: Callable[[GitHubGateway], None]) -> None:
        """Queue a post for after the save. Outside an action it is dropped: history was
        posted when it happened."""
        self._later(what, lambda: send(self.github))

    def _later(self, what: str, run: Callable[[], None]) -> None:
        """Queue work that must follow the save, such as a post or a derived record."""
        pending = getattr(self._outbox, "pending", None)
        if pending is not None:
            pending.append((what, run))

    @contextmanager
    def _posting(self) -> Iterator[None]:
        """Send what the enclosed action queued, once it has returned, which is after
        it saved. An action that raises saved nothing, so its posts are dropped."""
        self._outbox.pending = []
        try:
            yield
            queued = list(self._outbox.pending)
        finally:
            self._outbox.pending = None
        for what, run in queued:
            try:
                run()
            except Exception:
                # The step is saved and stands. The work can be redone; the outbox
                # that retries it arrives with the settlement orchestrator (#69).
                log.exception("after-save step failed: %s", what)

    # ------------------------------------------------------------ money

    @contextmanager
    def _exclusive(self, issue_id: str) -> Iterator[None]:
        """Hold the issue for one action, or refuse at once with Busy.

        It never waits: the caller is a person who can press again or a sweeper that
        comes back next pass, and either is better than a queue of stale intentions.
        """
        with self.coordinator.lock(f"issue:{issue_id}", ISSUE_LOCK_TTL) as held:
            if not held:
                raise Busy(issue_id)
            yield

    @contextmanager
    def _publisher_exclusive(self, publisher_id: str) -> Iterator[None]:
        """Hold the publisher for one commitment, or refuse at once with Busy.

        A monthly category limit is a sum over the publisher's issues, so reading what
        the month already holds and writing the new commitment have to be one step.
        The issue lock cannot do that: two fundings of *different* issues of the same
        publisher take different locks and both see the same untouched month.

        Taken after the issue lock and never the other way round, so the two cannot
        deadlock.
        """
        with self.coordinator.lock(f"publisher:{publisher_id}", ISSUE_LOCK_TTL) as held:
            if not held:
                raise Busy(publisher_id)
            yield

    @contextmanager
    def _pull_request_exclusive(self, repo: str, number: int) -> Iterator[None]:
        """Hold one pull request while it is checked against every other issue and
        submitted, or refuse at once with Busy. Taken after the issue lock."""
        name = f"{repo.lower()}#{number}"
        with self.coordinator.lock(f"pull:{name}", ISSUE_LOCK_TTL) as held:
            if not held:
                raise Busy(f"pull request {name}")
            yield

    @contextmanager
    def _listing_exclusive(self, repo: str, number: int) -> Iterator[None]:
        """Hold one GitHub issue's listing while it is checked and saved.

        Two publishes of the same issue at once, a double click or GitHub's `opened`
        and `labeled` arriving together, would otherwise both find it unlisted. Unlike
        an issue's lock this waits a moment, because the holder is saved or refused in
        well under a second, and what the second publish has to say is which.
        """
        name = f"{repo.lower()}#{number}"
        give_up = time.monotonic() + LISTING_WAIT.total_seconds()
        while True:
            with self.coordinator.lock(f"listing:{name}", ISSUE_LOCK_TTL) as held:
                if held:
                    yield
                    return
            if time.monotonic() >= give_up:
                raise Busy(name)
            time.sleep(_LISTING_POLL_SECONDS)

    def reconcile(self, now: datetime | None = None) -> list[Divergence]:
        """Compare every issue's ledger with what the chain holds, and raise the alarm.

        A divergence found in the first pass is checked again under the issue's lock,
        because an action caught between its chain call and its save looks divergent
        for an instant and is not. What survives is logged as an alert and written to
        the issue's decision log once.
        """
        now = now or _now()
        records = {r.id: r for r in self.repo.list_issues()}
        suspects = ledger.reconcile(
            {i: r.money_events for i, r in records.items()}, self.chain.commitments()
        )
        confirmed: list[Divergence] = []
        for suspect in suspects:
            if suspect.issue_id not in records:
                # Money on chain for an issue we have no record of at all.
                _alert(suspect)
                confirmed.append(suspect)
                continue
            try:
                with self._exclusive(suspect.issue_id):
                    found = self._record_divergence(suspect.issue_id, now)
            except (Busy, StaleIssue):
                continue  # Someone is acting on it; the next pass looks again.
            confirmed.extend(found)
        DIVERGENCES.set(len(confirmed))
        return confirmed

    def _record_divergence(self, issue_id: str, now: datetime) -> list[Divergence]:
        rec = self.repo.get_issue(issue_id)
        assert rec is not None
        theirs = self.chain.commitments().get(issue_id)
        # A commitment the escrow holds and the ledger does not, for an issue we know, is
        # the booking that failed after the publisher's wallet had already moved the
        # money: the one place that records `escrow` and `deadline` sits after the chain
        # call that can fail, so they stayed None and with them every timer -- the refund
        # included -- became unreachable (#161). Adopting it here is what gives the issue
        # a lifecycle again: inside the window it books, past it the deadline refunds.
        if theirs is not None and not rec.money_events:
            return self._adopt_commitment(rec, theirs, now)
        found = ledger.reconcile(
            {issue_id: rec.money_events}, {issue_id: theirs} if theirs else {}
        )
        for divergence in found:
            _alert(divergence)
            outcome = f"ledger says {divergence.ledger}, chain says {divergence.chain}"
            if any(
                d.action == "ledger_divergence" and d.outcome == outcome for d in rec.decisions
            ):
                continue
            self._log(
                rec,
                actor="system",
                action="ledger_divergence",
                rule="chain_reconciliation",
                outcome=outcome,
                when=now,
            )
            self.repo.save_issues(rec)
        return found

    def _adopt_commitment(self, rec: IssueRecord, theirs: OnChain, now: datetime) -> list[Divergence]:
        """Book a commitment the chain holds and the platform never recorded.

        The transaction reference is read back from the escrow, because the platform did
        not send it: the publisher's wallet did. That is what lets the ledger say what the
        chain already says.

        The state is moved through the lifecycle rather than set: `AWAITING_APPROVAL` is
        the record of an approval the escrow was still waiting on, and `FUNDED` follows
        it, exactly as the approval would have. A record in any other state is not one
        this can place -- it is alerted and left alone rather than forced.
        """
        if rec.state is not IssueState.AWAITING_APPROVAL:
            divergence = Divergence(
                rec.id,
                "no commitment",
                f"{theirs.status} {theirs.amount} held, but the issue is {rec.state}",
            )
            _alert(divergence)
            self._log(
                rec,
                actor="system",
                action="ledger_divergence",
                rule="chain_reconciliation",
                outcome=(
                    f"the escrow holds {theirs.amount} for {rec.id}, which is {rec.state} "
                    "and cannot be booked from there"
                ),
                when=now,
            )
            self.repo.save_issues(rec)
            return [divergence]

        publisher = self.repo.get_publisher(rec.publisher_id)
        assert publisher is not None
        try:
            tx = self.chain.commitment_tx(rec.id, theirs.publisher or publisher.wallet.address)
        except (ChainUnavailable, ChainRevert) as exc:
            # Nothing is written without the reference: a money event without one is not
            # the record this ledger keeps. The next pass tries again.
            log.warning("could not read the commitment reference for %s: %s", rec.id, exc)
            return []

        self._move(rec, IssueState.FUNDED)
        self._book(rec, MoneyEventKind.COMMITTED, theirs.amount, publisher.id, tx, now)
        rec.escrow = EscrowCommitment(
            issue_id=rec.id,
            contract=ESCROW_CONTRACT,
            chain=settings.chain,
            tx_hash=tx,
            amount=money(theirs.amount),
            deadline=theirs.deadline or now,
            fee_bps=theirs.fee_bps,
        )
        rec.deadline = rec.escrow.deadline
        rec.funding = rec.funding or FundingTerms(
            amount=theirs.amount,
            fee_bps=theirs.fee_bps,
            wallet=theirs.publisher or publisher.wallet.address,
            deadline=rec.escrow.deadline,
            approved_at=now,
        )
        self._log(
            rec,
            actor="system",
            action="commitment_adopted",
            rule="chain_reconciliation",
            outcome=(
                f"the escrow held {theirs.amount} that was never booked; recorded against "
                f"{tx}, and the issue is FUNDED until {rec.deadline:%d %B %Y}"
            ),
            when=now,
        )
        self.repo.save_issues(rec)
        ADOPTED.inc()
        return []


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
            applied.append(action)
        return applied, created

    def run_timers(self, issue_id: str, now: datetime | None = None) -> list[TimedAction]:
        """Apply what is due on one issue and save it.

        The sweeper's entry point. It holds the issue's lock and reads the issue
        afresh inside it, so a timer acts on what a person last saved rather than on
        the copy the sweeper listed. Raises Busy when someone else holds the issue.
        """
        with self._exclusive(issue_id), self._posting():
            rec = self.repo.get_issue(issue_id)
            if rec is None:
                return []
            applied, created = self._apply_due(rec, now or _now())
            if applied:
                self.repo.save_issues(rec, *created)
            return applied

    # ---------------------------------------------------------------- actions

    def link_wallet(self, party: str, party_id: str, wallet: Wallet) -> Wallet:
        """Record the address a party's own Circle wallet reported. Payouts go there.

        The party's own wallet, read back from Circle rather than typed into a form,
        so a payout cannot be redirected by whoever can call the route.
        """
        if party == "publisher":
            publisher = self.repo.get_publisher(party_id)
            if publisher is None:
                raise KeyError(party_id)
            self.repo.save_publisher(publisher.model_copy(update={"wallet": wallet}))
        else:
            contributor = self.repo.get_contributor(party_id)
            if contributor is None:
                raise KeyError(party_id)
            self.repo.save_contributor(contributor.model_copy(update={"wallet": wallet}))
        return wallet
    def publish(self, payload) -> IssueRecord:
        """List a GitHub issue and price it, once while it is open (#120).

        A second publish of an open issue is refused with AlreadyListed, naming the
        listing it has. The check runs first, so a refusal reads nothing from GitHub,
        and again with the save under the listing's lock, which is what stops two
        publishes at once from both being saved.
        """
        publisher = self.repo.get_publisher(payload.publisher_id)
        if publisher is None:
            raise KeyError(payload.publisher_id)
        if payload.number:
            self._refuse_listed(payload.repo, payload.number)
        started = time.perf_counter()
        facts = None
        number = payload.number
        platform_label = settings.github_label
        if number is None and not payload.signals and self.github.creates_issues:
            # The publisher is describing work GitHub does not hold yet, so the issue
            # is opened first and the price is put on the issue that now exists. A
            # number of our own would price something no contributor could open.
            labels = list(payload.labels)
            named = {name.lower() for name in labels}
            if platform_label and platform_label.lower() not in named:
                labels.append(platform_label)
            try:
                number = self.github.create_issue(
                    payload.repo, payload.title, payload.summary, labels
                )
            except GitHubError as exc:
                raise UnreadableIssue(
                    f"could not open an issue in {payload.repo}: {exc}"
                ) from exc
        if number and not payload.signals:
            try:
                facts = self.github.read_issue(payload.repo, number)
            except GitHubError as exc:
                raise UnreadableIssue(
                    f"could not read {payload.repo}#{number} from GitHub: {exc}"
                ) from exc
        if payload.number is not None and not payload.signals and platform_label:
            # Linking an issue that already exists puts the platform label on it, so
            # the label-driven flow and the web app price the same issue (#199). The
            # add is idempotent, and a failure aborts before anything is listed. It
            # happens after the read so the listing keeps the labels the issue had
            # when the publisher chose it.
            try:
                self.github.add_labels(payload.repo, payload.number, [platform_label])
            except GitHubError as exc:
                raise UnreadableIssue(
                    f"could not add the {platform_label} label to "
                    f"{payload.repo}#{payload.number}: {exc}"
                ) from exc
        # The publisher's price, and the only thing that bounds what the escrow commits
        # (#142). Absent on an issue the GitHub flow listed, which is priced later by
        # `/misthos price`; nothing is funded until it is named.
        price = check_price(Usdc.from_decimal(payload.price)) if payload.price is not None else None
        spec = {
            "repo": payload.repo,
            "number": number or self._unlisted_number(payload.repo),
            "title": payload.title,
            "summary": payload.summary or "Published through the web application.",
            "state": IssueState.PRICED,
            "labels": list(payload.labels),
            "price": str(price.decimal) if price is not None else None,
            "compliance_driven": payload.compliance_driven,
            # Drafted from the issue, every one checkable (#38); the publisher edits
            # and approves them before any money is committed.
            "criteria": acceptance.draft(payload.title, payload.summary, payload.labels),
            "publisher_id": payload.publisher_id,
            "age_days": 0,
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
        if facts is not None:
            # The issue itself, not the form, says what the work is.
            reading = read_signals(facts)
            spec["title"] = facts.title
            spec["summary"] = _summary(facts.body) or spec["summary"]
            spec["labels"] = list(facts.labels) or spec["labels"]
            spec["criteria"] = acceptance.draft(facts.title, facts.body, facts.labels)
            spec["signals"] = reading.signals.as_dict()
            spec["signals_read"] = f"read {facts.repo}#{facts.number}: {reading.summary()}"
        rec = self._build(spec)
        self._move(rec, IssueState.AWAITING_APPROVAL)
        self._log(
            rec,
            actor="publisher",
            action="price_set",
            rule="publisher_names_the_price",
            outcome=(
                f"published at the publisher's price of {price}"
                if price is not None
                else "published with no price yet; nothing is funded until the publisher "
                "names what the work is worth"
            ),
        )
        with self._listing_exclusive(rec.repo, rec.number):
            self._refuse_listed(rec.repo, rec.number)
            self.repo.save_issues(rec)
        _observe_price(rec, "github" if facts is not None else "form", started)
        return rec

    def _build(self, spec: dict) -> IssueRecord:
        """The listing a publisher described, at the price they named.

        Publishing is the only caller. The engine's reading of the issue is recorded
        beside the publisher's number as advice, never as the amount that binds
        (#142).
        """
        publisher = self.repo.get_publisher(spec["publisher_id"])
        assert publisher is not None, spec["publisher_id"]
        signals = ComplexitySignals(**spec["signals"])
        proposal = propose(
            signals,
            compliance_driven=spec.get("compliance_driven", False),
            # Settled issues of the same shape, from the platform's own history (#42).
            comparables=self._comparables_for(signals, spec["repo"]),
        )
        rec = IssueRecord(
            id=self._next_issue_id(),
            repo=spec["repo"],
            number=spec["number"],
            title=spec["title"],
            summary=spec["summary"],
            state=IssueState(spec["state"]),
            labels=spec["labels"],
            compliance_driven=spec.get("compliance_driven", False),
            acceptance_criteria=spec["criteria"],
            publisher_id=spec["publisher_id"],
            created_at=_now() - timedelta(days=spec["age_days"]),
            proposal=proposal,
            # Named by the publisher, or None on an issue the GitHub flow listed
            # without one: it stays unpriced until they name a number (#142).
            price=Usdc.from_decimal(spec["price"]) if spec.get("price") else None,
        )
        if spec.get("signals_read"):
            self._log(
                rec,
                actor="agent",
                action="signals_read",
                rule="github_read_path",
                outcome=spec["signals_read"],
            )
        self._log(
            rec,
            actor="agent",
            action="price_suggested",
            rule="complexity_score_v1",
            outcome=(
                f"suggested {proposal.recommended} for {proposal.estimated_hours}h; "
                "advice only, the publisher's price is what binds"
            ),
            cost="0.00",
        )
        return rec

    def _refuse_listed(self, repo: str, number: int) -> None:
        existing = self.find_open(repo, number)
        if existing is not None:
            raise AlreadyListed(existing)

    def _unlisted_number(self, repo: str) -> int:
        """A number for a locally described issue when the form names none: one no open
        listing in the repository has, so it is never refused as a duplicate of an issue
        the publisher did not mean."""
        taken = {
            r.number
            for r in self.repo.list_issues()
            if r.repo.lower() == repo.lower() and r.state not in lifecycle.TERMINAL_STATES
        }
        number = random.randint(100, 999)
        if number in taken:
            # Of len(taken) + 1 numbers, at least one is free.
            number = min(set(range(100, 101 + len(taken))) - taken)
        return number

    # ------------------------------------------------------------- pricing

    def _settled_history(self) -> list[SettledWork]:
        """Every issue the platform settled, as the pricing engine's comparables read it."""
        works = []
        for rec in self.repo.list_issues({IssueState.PAID}):
            if rec.proposal is None or rec.paid is None or rec.paid_at is None:
                continue
            works.append(
                SettledWork(
                    issue_id=rec.id,
                    repo=rec.repo,
                    title=rec.title,
                    signals=dict(rec.proposal.signals),
                    effort=effort(ComplexitySignals(**rec.proposal.signals)),
                    price=rec.paid,
                    settled_at=rec.paid_at,
                )
            )
        return works

    def _comparables_for(
        self, signals: ComplexitySignals, repo: str, *, exclude: str | None = None
    ) -> list[Comparable]:
        """Settled issues of the same shape as this one, closest first (#42)."""
        return comparables.find(
            signals.as_dict(),
            effort(signals),
            self._settled_history(),
            weights=WEIGHTS,
            now=_now(),
            repo=repo,
            exclude=exclude,
        )

    def settled_history(self) -> list[SettledWork]:
        """The settled work the engine compares against, for the paid comparables."""
        return self._settled_history()

    def price_advice(
        self,
        facts: IssueFacts,
        *,
        signals: dict[str, float] | None = None,
        compliance_driven: bool = False,
    ) -> tuple[Reading, ComplexitySignals, PriceProposal, list[Comparable]]:
        """What the engine would suggest for an issue described by `facts`, without
        listing it (#203): the signals read from it, any the caller set over them, the
        proposal, and every comparable that moved it. Reads, and writes nothing."""
        reading = read_signals(facts)
        chosen = ComplexitySignals(**{**reading.signals.as_dict(), **(signals or {})})
        chosen.validate()
        found = self._comparables_for(chosen, facts.repo)
        proposal = propose(chosen, compliance_driven=compliance_driven, comparables=found)
        return reading, chosen, proposal, found

    # ------------------------------------------------------------- review

    @staticmethod
    def _awaiting_review(rec: IssueRecord, now: datetime) -> bool:
        """In review, the commit under review has no verdict yet, and its checks have
        reported or had their time to (`CHECKS_WAIT`)."""
        return (
            rec.state is IssueState.IN_REVIEW
            and rec.submission is not None
            and (rec.review is None or rec.review.head_sha != rec.submission.head_sha)
            and ready_for_review(rec.submission.checks_passed, _submitted_at(rec), now)
        )

    def reviews_due(self, now: datetime | None = None) -> list[str]:
        now = now or _now()
        return [
            r.id for r in self.list_issues({IssueState.IN_REVIEW}) if self._awaiting_review(r, now)
        ]

    @staticmethod
    def _judged_already(rec: IssueRecord) -> bool:
        """In review on a commit the review has already judged.

        Only a resubmission of the same commit could do this, before #119 refused it.
        No review takes the commit again and no timer runs in review, so nothing would
        ever move the issue or the money it holds.
        """
        return (
            rec.state is IssueState.IN_REVIEW
            and rec.submission is not None
            and rec.review is not None
            and rec.review.head_sha == rec.submission.head_sha
        )

    def return_judged_to_rework(self, now: datetime | None = None) -> list[str]:
        """Put each issue left in review on an already judged commit back in REWORK,
        the state it was resubmitted from, so a new commit can be pushed and reviewed.

        The sweeper runs it on every pass. An issue someone is acting on is left for the
        next pass. Returns the issues it moved.
        """
        moved: list[str] = []
        for listed in self.repo.list_issues({IssueState.IN_REVIEW}):
            if not self._judged_already(listed):
                continue
            try:
                with self._exclusive(listed.id), self._posting():
                    rec = self.repo.get_issue(listed.id)
                    if rec is None or not self._judged_already(rec):
                        continue
                    assert rec.submission is not None
                    self._move(rec, IssueState.REWORK)
                    self._log(
                        rec,
                        actor="system",
                        action="returned_to_rework",
                        rule="commit_already_judged",
                        outcome=f"{rec.submission.head_sha[:7]} on PR "
                        f"#{rec.submission.pr_number} was submitted again after its verdict, "
                        "so no review could take it; the issue is back in rework until a "
                        "new commit is pushed",
                        when=now,
                    )
                    self.repo.save_issues(rec)
            except (Busy, StaleIssue):
                continue
            moved.append(listed.id)
        return moved

    def _submitted(self, rec: IssueRecord) -> Submitted:
        assert rec.submission is not None
        files = self.github.read_files(rec.repo, rec.submission.pr_number)
        return Submitted(
            repo=rec.repo,
            pr_number=rec.submission.pr_number,
            head_sha=rec.submission.head_sha,
            title=rec.title,
            criteria=tuple(rec.acceptance_criteria),
            files=tuple(files),
            checks_passed=rec.submission.checks_passed,
        )

    @staticmethod
    def _judge(reviewer: Reviewer, submitted: Submitted) -> tuple[Judgement, float]:
        started = time.monotonic()
        judgement = reviewer.judge(submitted)
        return judgement, round(time.monotonic() - started, 2)

    def review(self, issue_id: str, now: datetime | None = None) -> IssueRecord | None:
        """Have the review agent judge the submitted commit, and record the verdict.

        Reading the diff and judging it can take minutes with a model, so it happens
        outside the issue's lock; the verdict is applied under the lock only if the
        same commit is still the one under review. Returns None when there was
        nothing to review, or the submission moved on while it was being judged.
        """
        rec = self.repo.get_issue(issue_id)
        if rec is None:
            raise KeyError(issue_id)
        if not self._awaiting_review(rec, now or _now()):
            return None
        submitted = self._submitted(rec)
        judgement, seconds = self._judge(self.reviewer, submitted)

        with self._exclusive(issue_id), self._posting():
            fresh = self.repo.get_issue(issue_id)
            if (
                fresh is None
                or not self._awaiting_review(fresh, now or _now())
                or fresh.submission is None
                or fresh.submission.head_sha != submitted.head_sha
            ):
                return None
            decided = decide(
                judgement,
                checks_passed=fresh.submission.checks_passed,
                files_changed=len(submitted.files),
                rework_rounds=_rework_rounds(fresh),
            )
            when = now or _now()
            self._review(
                fresh,
                verdict=decided.verdict.value,
                findings=decided.findings,
                when=when,
                reviewer=judgement.reviewer,
                seconds=seconds,
                cost=judgement.cost,
            )
            self._log(
                fresh,
                actor="agent",
                action="verdict_issued",
                rule=decided.rule,
                outcome=f"{decided.verdict.value} by {judgement.reviewer} on "
                f"{submitted.head_sha[:7]} in {seconds:g}s: {judgement.summary}",
                cost=_cost_text(judgement.cost),
                when=when,
            )
            self.repo.save_issues(fresh)
            _observe_review(fresh.id, judgement, decided.verdict.value, seconds)
            return fresh

    def dispute(self, issue_id: str, reason: str, now: datetime | None = None) -> IssueRecord:
        """The contributor challenges a rework or reject verdict.

        The same commit is reviewed again against the published criteria, by the
        second reviewer, and the outcome is recorded either way. Overturned, the
        work is accepted and the merge or the grace period pays it as usual; upheld,
        the verdict stands. Each verdict can be disputed once.
        """
        rec = self.repo.get_issue(issue_id)
        if rec is None:
            raise KeyError(issue_id)
        if rec.state not in {IssueState.REWORK, IssueState.REJECTED} or rec.review is None:
            raise lifecycle.IllegalTransition(rec.state, IssueState.ACCEPTED)
        disputed_at = rec.review.decided_at
        if _disputed_since(rec, disputed_at):
            raise DisputeRefused(f"the {rec.review.verdict} verdict on {rec.id} was already disputed")
        submitted = self._submitted(rec)
        judgement, seconds = self._judge(self.second_reviewer, submitted)

        with self._exclusive(issue_id), self._posting():
            fresh = self.repo.get_issue(issue_id)
            if (
                fresh is None
                or fresh.review is None
                or fresh.review.decided_at != disputed_at
                or _disputed_since(fresh, disputed_at)
            ):
                raise DisputeRefused(f"the verdict on {issue_id} changed while it was disputed")
            when = now or _now()
            self._log(
                fresh,
                actor="contributor",
                action="dispute_opened",
                rule="contributor_dispute",
                outcome=f"disputed the {fresh.review.verdict} verdict: {reason.strip()[:300]}",
                when=when,
            )
            assert fresh.submission is not None
            decided = decide(
                judgement,
                checks_passed=fresh.submission.checks_passed,
                files_changed=len(submitted.files),
                # The second review judges the same commit, so it gets no extra round.
                rework_rounds=0,
            )
            if decided.verdict is Verdict.ACCEPT:
                if fresh.state is IssueState.REWORK:
                    self._move(fresh, IssueState.IN_REVIEW)
                self._review(
                    fresh,
                    verdict=Verdict.ACCEPT.value,
                    findings=decided.findings,
                    when=when,
                    reviewer=judgement.reviewer,
                    seconds=seconds,
                    cost=judgement.cost,
                )
                action, outcome = "dispute_overturned", "the second review accepted the work"
            else:
                action, outcome = (
                    "dispute_upheld",
                    f"the second review agreed the work is not acceptable ({decided.rule})",
                )
            self._log(
                fresh,
                actor="agent",
                action=action,
                rule="second_review",
                outcome=f"{outcome}; {judgement.reviewer}: {judgement.summary}",
                cost=_cost_text(judgement.cost),
                when=when,
            )
            self.repo.save_issues(fresh)
            return fresh

    def decline(self, issue_id: str, reason: str, now: datetime | None = None) -> IssueRecord:
        """The publisher declines work the review passed, and says why.

        Distinct from going silent, which releases the payment after the grace
        period. The work goes back for rework with the publisher's reason as its
        finding, and counts as a rework round. A publisher declines once per issue,
        and before the grace period ends: after that the merge or the grace period
        settles it, so declining cannot be a way to keep finished work without paying
        for it.
        """
        with self._exclusive(issue_id), self._posting():
            rec = self.repo.get_issue(issue_id)
            if rec is None:
                raise KeyError(issue_id)
            # Before the state check, so a retried decline, which finds the issue in
            # REWORK, is told why rather than that REWORK cannot move to REWORK.
            if any(d.action == "publisher_declined" for d in rec.decisions):
                raise DeclineRefused(
                    f"the publisher already declined {rec.id} once; merging it or the grace "
                    "period settles it now"
                )
            if rec.state is not IssueState.ACCEPTED or rec.accepted_by is not None:
                raise lifecycle.IllegalTransition(rec.state, IssueState.REWORK)
            if timers.due(self._clocks(rec), now or _now()) is TimedAction.RELEASE_AFTER_GRACE:
                # The silence already accepted the work and the release is owed; the
                # sweeper's next pass pays it. Declining now would drop that payout.
                raise DeclineRefused(
                    f"the grace period on {rec.id} has ended, so the publisher's silence "
                    "accepted the work and the payment is released on the next sweep"
                )
            self._move(rec, IssueState.REWORK)
            reason = reason.strip()[:500]
            self._log(
                rec,
                actor="publisher",
                action="publisher_declined",
                rule="publisher_overturn",
                outcome=f"declined the passing verdict: {reason}",
                when=now,
            )
            if rec.submission is not None:
                pr, sha = rec.submission.pr_number, rec.submission.head_sha
                body = f"**The publisher declined this.** {reason}"
                self._post(
                    f"decline on {rec.repo}#{pr}",
                    lambda gh: gh.review(rec.repo, pr, sha, ReviewEvent.REQUEST_CHANGES, body),
                )
            self.repo.save_issues(rec)
            return rec

    # ------------------------------------------------------------- policy

    def set_policy(
        self,
        publisher_id: str,
        *,
        approval_threshold_usdc: str | None,
        approvers: list[str],
        category_limits: dict[str, str],
    ) -> Publisher:
        """Replace a publisher's spending policy, after checking it can work."""
        publisher = self.repo.get_publisher(publisher_id)
        if publisher is None:
            raise KeyError(publisher_id)
        try:
            updated = publisher.model_copy(
                update={
                    "approval_threshold_usdc": (
                        _display(Usdc.from_decimal(approval_threshold_usdc))
                        if approval_threshold_usdc
                        else None
                    ),
                    "approvers": [login(a) for a in approvers if login(a)],
                    "category_limits": {
                        label.strip(): _display(Usdc.from_decimal(limit))
                        for label, limit in category_limits.items()
                        if label.strip()
                    },
                }
            )
        except (ArithmeticError, ValueError) as exc:
            raise PolicyRefusal(f"not an amount: {exc}") from exc
        _policy(updated).validate()
        self.repo.save_publisher(updated)
        return updated

    def approve_release(
        self, issue_id: str, approver: str, now: datetime | None = None
    ) -> IssueRecord:
        """A named approver approves a release held over the publisher's threshold,
        and the release goes ahead at once if nothing else holds it.

        `approver` is a GitHub login the caller has proved, which the API takes from
        the session (domain/policy.py says why), and it is what the log records.
        """
        with self._exclusive(issue_id), self._posting():
            rec = self.repo.get_issue(issue_id)
            if rec is None:
                raise KeyError(issue_id)
            if rec.state is not IssueState.ACCEPTED or rec.payout_hold != (
                PayoutGate.AWAIT_APPROVER.value
            ):
                raise lifecycle.IllegalTransition(rec.state, IssueState.PAID)
            publisher = self.repo.get_publisher(rec.publisher_id)
            assert publisher is not None and rec.contributor_id is not None
            if not may_approve(_policy(publisher), approver):
                raise PolicyRefusal(f"{approver} is not one of {publisher.name}'s approvers")
            # A contributor's handle is their linked GitHub login (link_github).
            payee = self.repo.get_contributor(rec.contributor_id)
            if payee is not None and approves_own_payout(approver, payee.handle):
                raise PolicyRefusal(
                    f"{approver} is the contributor and cannot approve their own payout"
                )
            when = now or _now()
            self._log(
                rec,
                actor="publisher",
                action="release_approved",
                rule="named_approver",
                outcome=f"{login(approver)} approved the release of {self._committed(rec)}",
                when=when,
            )
            self._pay(rec, when)
            self.repo.save_issues(rec)
            return rec

    # ------------------------------------------------------------- standing

    def refresh_standing(self, contributor_id: str) -> None:
        """Recompute one contributor's standing from the ledger and save it."""
        self.rebuild_standing(only=contributor_id)

    def rebuild_standing(
        self, *, write: bool = True, only: str | None = None
    ) -> dict[str, tuple[dict[str, object], dict[str, object]]]:
        """Derive every contributor's reputation, settled issues and earnings from the
        ledger. Returns what differed from the stored record, before and after."""
        totals = reputation.standing(self.repo.list_issues())
        drift: dict[str, tuple[dict[str, object], dict[str, object]]] = {}
        for contributor in self.repo.list_contributors():
            if only is not None and contributor.id != only:
                continue
            after = reputation.as_fields(totals.get(contributor.id, reputation.Standing()))
            before = {k: getattr(contributor, k) for k in after}
            if before == after:
                continue
            drift[contributor.id] = (before, after)
            if write:
                self.repo.save_contributor(contributor.model_copy(update=after))
        return drift

    def reputation_events(self, contributor_id: str) -> list[reputation.ReputationEvent]:
        return [
            e
            for e in reputation.events(self.repo.list_issues())
            if e.contributor_id == contributor_id
        ]

    # ----------------------------------------------------------- paid calls

    def record_paid_call(
        self,
        *,
        route: str,
        request_digest: str,
        payer: str,
        amount: Usdc,
        network: str,
        asset: str,
        transaction: str,
        ref: str | None = None,
        at: datetime | None = None,
    ) -> tuple[PaidCall, bool]:
        """Write down one settled x402 call (#201). The edge may send the same settlement
        twice; the second is recognised, and the call on record is returned with False."""
        call = PaidCall(
            id=f"PAY-{self.repo.next_value('paid_call', 1):06d}",
            route=route,
            request_digest=request_digest,
            payer=payer.lower(),
            amount=amount,
            network=network,
            asset=asset,
            transaction=transaction,
            ref=ref,
            settled_at=at or datetime.now(UTC),
        )
        kept = self.repo.record_paid_call(call)
        return kept, kept.id == call.id

    def list_paid_calls(
        self, *, since: datetime | None = None, route: str | None = None, limit: int = 500
    ) -> list[PaidCall]:
        return self.repo.list_paid_calls(since=since, route=route, limit=limit)

    # ------------------------------------------------------------- accounts

    def create_account(
        self,
        address: str,
        role: str,
        name: str,
        *,
        budget_usdc: str = "5000",
        now: datetime | None = None,
    ) -> Account:
        """A wallet's first sign-in: it becomes a publisher or a contributor, once.

        A GitHub login the wallet linked before choosing (#181) is adopted onto the new
        account. A contributor's handle is then that login, as `link_github` makes it.
        """
        address = address.lower()
        # One side per wallet, even for two requests at once: without the hold both
        # would find no account and each create a party.
        with self.coordinator.lock(f"account:{address}", ISSUE_LOCK_TTL) as free:
            if not free:
                raise Busy(address)
            return self._create_account(address, role, name, budget_usdc, now)

    def _create_account(
        self, address: str, role: str, name: str, budget_usdc: str, now: datetime | None
    ) -> Account:
        if self.repo.get_account(address) is not None:
            raise AccountExists(f"{address} already has a role")
        held = self.repo.get_pending_github_login(address)
        name = name.strip()
        if role == "publisher":
            try:
                budget = Usdc.from_decimal(budget_usdc.replace(",", ""))
            except (ArithmeticError, ValueError) as exc:
                raise AccountExists(f"not a budget: {budget_usdc}") from exc
            party_id = f"PUB-{self.repo.next_value('publisher', 100)}"
            self.repo.save_publisher(
                Publisher(
                    id=party_id,
                    name=name,
                    kind="company",
                    wallet=Wallet(address=address, chain=settings.chain),
                    budget_remaining_usdc=_display(budget),
                )
            )
        else:
            party_id = f"CON-{self.repo.next_value('contributor', 100)}"
            self.repo.save_contributor(
                Contributor(
                    id=party_id,
                    handle=held or name,
                    wallet=Wallet(address=address, chain=settings.chain),
                    reputation=0,
                    settled_issues=0,
                    earned_usdc="0.00",
                )
            )
        account = Account(
            address=address,
            role=role,  # type: ignore[arg-type]
            party_id=party_id,
            github_login=held,
            created_at=now or _now(),
        )
        self.repo.save_account(account)
        if held is not None:
            # Only once the account carries it: until then the hold is what keeps the
            # login this wallet's.
            self.repo.drop_pending_github_login(address)
        return account

    def pending_github_login(self, address: str) -> str | None:
        """The login a wallet linked before choosing its side, until the account adopts it."""
        return self.repo.get_pending_github_login(address.lower())

    def hold_github_login(self, address: str, login: str, now: datetime | None = None) -> None:
        """The sign-in gate's second step (#181): link a login, with or without a side.

        A wallet that already has an account gets the login linked onto it, as
        `link_github` links it. One without an account yet has it held until choosing a
        side creates the account, which adopts it. Either way the login is this wallet's
        from now on: a held login is refused to every other wallet with AccountConflict,
        exactly as a linked one is.
        """
        address = address.lower()
        with self._login_exclusive(login):
            if self.repo.get_account(address) is not None:
                self._link_github(address, login)
                return
            self.repo.hold_github_login(address, login, now or _now())

    def link_github(self, address: str, login: str) -> Account:
        """Link the wallet's GitHub account (#80). A contributor's handle is their login."""
        with self._login_exclusive(login):
            return self._link_github(address.lower(), login)

    def _link_github(self, address: str, login: str) -> Account:
        account = self.repo.get_account(address)
        if account is None:
            raise KeyError(address)
        linked = account.model_copy(update={"github_login": login})
        self.repo.save_account(linked)
        if account.role == "contributor":
            contributor = self.repo.get_contributor(account.party_id)
            assert contributor is not None
            self.repo.save_contributor(contributor.model_copy(update={"handle": login}))
        return linked

    @contextmanager
    def _login_exclusive(self, login: str) -> Iterator[None]:
        """Hold a GitHub login for one link, or refuse at once with Busy.

        A login can be taken on two tables, linked on an account or held for a wallet
        with none, and each write checks the other table before it lands. Two writes of
        the same login to different tables, at the same moment, could each pass the
        other's check; holding the login for the write makes them take turns.
        """
        with self.coordinator.lock(f"github-login:{login.lower()}", ISSUE_LOCK_TTL) as held:
            if not held:
                raise Busy(login)
            yield

    # ------------------------------------------------------- explicit actions

    def approve_criteria(
        self, issue_id: str, criteria: list[str], by: str, now: datetime | None = None
    ) -> IssueRecord:
        """The publisher edits the drafted acceptance criteria and approves them (#21).
        Until the money is committed they can be revised and approved again."""
        cleaned = [" ".join(c.split()) for c in criteria if c.strip()]
        if not cleaned:
            raise CriteriaNotApproved("approve at least one acceptance criterion")
        # A criterion a reviewer cannot judge makes every verdict on it arguable, so it
        # is refused here, with the reason, rather than discovered at review (#38).
        found = acceptance.problems(cleaned)
        if found:
            raise UntestableCriteria(found)
        with self._exclusive(issue_id), self._posting():
            rec = self.repo.get_issue(issue_id)
            if rec is None:
                raise KeyError(issue_id)
            if rec.state not in _PRE_FUNDING:
                raise lifecycle.IllegalTransition(rec.state, IssueState.FUNDED)
            when = now or _now()
            rec.acceptance_criteria = cleaned
            rec.criteria_approved_at = when
            self._log(
                rec,
                actor="publisher",
                action="criteria_approved",
                rule="human_checkpoint",
                outcome=f"{by} approved {len(cleaned)} acceptance criteria",
                when=when,
            )
            self.repo.save_issues(rec)
            return rec

    def approve_price(
        self, issue_id: str, by: str, now: datetime | None = None
    ) -> IssueRecord:
        """The publisher approves the price they set, and the money is committed.

        There is no number to pass. The price was named when the issue was published
        and can be changed with `set_price` until someone claims it, so approving is
        the publisher's signature on what the escrow takes and on nothing else (#142).
        """
        with self._exclusive(issue_id), self._posting():
            rec = self.repo.get_issue(issue_id)
            if rec is None:
                raise KeyError(issue_id)
            if rec.state is not IssueState.AWAITING_APPROVAL:
                raise lifecycle.IllegalTransition(rec.state, IssueState.FUNDED)
            self._approve_and_fund(
                rec,
                now or _now(),
                f"{by} approved the price of {rec.price} and committed the funds",
            )
            self.repo.save_issues(rec)
            return rec

    def record_terms(self, issue_id: str, now: datetime | None = None) -> IssueRecord:
        """Fix the terms this approval funds the issue on, without touching the escrow.

        The approval and the booking are one action, but they can fail apart: on Arc the
        first approval is refused with `NotCommitted` so the publisher's wallet can send
        the commitment, and that refusal discards the save it happened in. The plan then
        hands the wallet the deadline, amount and rate that were fixed here, and the
        booking checks the commitment against the same values, so they have to survive
        the refusal (#126). Called before `_fund`, and idempotent: terms already standing
        are kept.
        """
        with self._exclusive(issue_id), self._posting():
            rec = self.repo.get_issue(issue_id)
            if rec is None:
                raise KeyError(issue_id)
            if rec.price is None:
                # Nothing to fix terms on: `_fund` refuses this with the reason a price
                # is needed, which is a 422 rather than a fault here.
                return rec
            publisher = self.repo.get_publisher(rec.publisher_id)
            assert publisher is not None
            rec.funding = self._funding_terms(rec, publisher, now or _now())
            self.repo.save_issues(rec)
            return rec

    def set_price(
        self, issue_id: str, amount: Usdc, by: str, now: datetime | None = None
    ) -> IssueRecord:
        """The publisher names a new price, while the issue is still unclaimed.

        Allowed from the moment the issue is published until a contributor holds it.
        A price that could move after a claim is a promise to the contributor that the
        publisher can break, so the claim closes the door: past it the price is what the
        escrow holds, and only a refund moves the money. Replaces the automatic
        re-listing that used to raise the price on the publisher's behalf (#142).
        """
        with self._exclusive(issue_id), self._posting():
            rec = self.repo.get_issue(issue_id)
            if rec is None:
                raise KeyError(issue_id)
            if rec.state not in _PRE_FUNDING:
                raise lifecycle.IllegalTransition(rec.state, IssueState.PRICED)
            price = check_price(amount)
            previous = rec.price
            if previous == price:
                return rec
            rec.price = price
            self._log(
                rec,
                actor="publisher",
                action="price_set",
                rule="publisher_names_the_price",
                outcome=f"{by} moved the price from {previous} to {price}",
                when=now or _now(),
            )
            self.repo.save_issues(rec)
            return rec

    def _approve_and_fund(self, rec: IssueRecord, now: datetime, outcome: str) -> None:
        if rec.criteria_approved_at is None:
            raise CriteriaNotApproved(
                f"{rec.id} cannot be funded until the publisher approves its acceptance criteria"
            )
        self._screen_publisher_for_funding(rec, now)
        # The limit is a sum over the publisher's other issues, so the read and the
        # commitment it authorises are one step under one lock. Without it two
        # concurrent fundings of different issues both pass the check.
        with self._publisher_exclusive(rec.publisher_id):
            self._check_category_limits(rec, now)
            self._fund(rec)
        self._log(
            rec,
            actor="publisher",
            action="price_approved",
            rule="human_checkpoint",
            outcome=outcome,
        )

    def claim(self, issue_id: str, contributor_id: str, now: datetime | None = None) -> IssueRecord:
        """A contributor takes the exclusive, time-boxed claim. First claim wins."""
        with self._exclusive(issue_id), self._posting():
            rec = self.repo.get_issue(issue_id)
            if rec is None:
                raise KeyError(issue_id)
            if rec.state is not IssueState.FUNDED:
                raise lifecycle.IllegalTransition(rec.state, IssueState.CLAIMED)
            when = now or _now()
            self._claim(rec, contributor_id, when=when)
            self._log(
                rec,
                actor="contributor",
                action="claimed",
                rule="first_claim_wins",
                outcome=f"{self._handle(contributor_id)} took an exclusive "
                f"{int(lifecycle.CLAIM_WINDOW.total_seconds() // 3600)}h claim",
                when=when,
            )
            self.repo.save_issues(rec)
            return rec

    # ------------------------------------------------------- GitHub connections

    def connect_repositories(
        self, installation_id: int, installed_by: str, repos: list[str], now: datetime | None = None
    ) -> list[RepoConnection]:
        """The App was installed on these repositories. They belong to the publisher whose
        linked GitHub login installed it, now or once that login is linked (#6)."""
        now = now or _now()
        publisher_id = self._publisher_with_login(installed_by)
        connected = []
        for repo in repos:
            connection = RepoConnection(
                repo=repo.lower(),
                installation_id=installation_id,
                installed_by=installed_by,
                publisher_id=publisher_id,
                connected_at=now,
            )
            self.repo.save_connection(connection)
            connected.append(connection)
        return connected

    def disconnect_repositories(
        self, repos: list[str] | None = None, *, installation_id: int | None = None
    ) -> list[str]:
        """The App was removed from these repositories, or uninstalled altogether. An
        empty list removes nothing: it is not an uninstall."""
        targets = (
            repos
            if repos is not None
            else [
                c.repo
                for c in self.repo.list_connections()
                if installation_id is not None and c.installation_id == installation_id
            ]
        )
        self.repo.delete_connections(targets)
        return [t.lower() for t in targets]

    def _publisher_with_login(self, login: str) -> str | None:
        account = self.repo.get_account_by_github_login(login)
        return account.party_id if account is not None and account.role == "publisher" else None

    def connection(self, repo: str) -> RepoConnection | None:
        """Where the App is installed, with its publisher resolved if it can be now."""
        found = self.repo.get_connection(repo)
        if found is not None and found.publisher_id is None:
            publisher_id = self._publisher_with_login(found.installed_by)
            if publisher_id is not None:
                found.publisher_id = publisher_id
                self.repo.save_connection(found)
        return found

    def connection_from_installation(self, repo: str) -> RepoConnection | None:
        """The connection GitHub's own installation implies for this repository (#180).

        A connection row is written by the `installation_repositories` webhook and by
        nothing else, and GitHub does not redeliver a delivery that failed -- so a
        repository the App is on can have no row and no way to get one. GitHub still
        knows which installation covers it, so ask, and record what it says through the
        same writer the webhook uses. Returns None when the App is not installed there.
        """
        found = self.github.installation_for(repo)
        if found is None:
            return None
        connected = self.connect_repositories(found.id, found.login, [repo])
        return connected[0] if connected else None

    def reconcile_connections(self, now: datetime | None = None) -> list[str]:
        """Record every repository the App is installed on and has no connection for.

        The webhook is the account of record for a change GitHub reports, but a delivery
        that fails is never redelivered, so the table can be missing a repository
        silently and for good. This makes the table agree with what GitHub says the
        installations cover, so a missed delivery heals within a sweep (#180).

        Only additions: a repository that left an installation is disconnected by the
        webhook that says so, and a partial read here must not delete a live row.
        """
        now = now or _now()
        known = {c.repo.lower() for c in self.repo.list_connections()}
        added: list[str] = []
        for installation in self.github.list_installations():
            missing = [
                repo for repo in installation.repositories if repo.lower() not in known
            ]
            if not missing:
                continue
            for connection in self.connect_repositories(
                installation.id, installation.login, missing, now
            ):
                added.append(connection.repo)
                known.add(connection.repo)
        return added

    def connections_of(self, publisher_id: str) -> list[RepoConnection]:
        return sorted(
            (
                c
                for c in (self.connection(r.repo) for r in self.repo.list_connections())
                if c is not None and c.publisher_id == publisher_id
            ),
            key=lambda c: c.repo,
        )

    def account_with_login(self, login: str) -> Account | None:
        return self.repo.get_account_by_github_login(login)

    # ------------------------------------------------------------ GitHub events

    def find_open(self, repo: str, number: int) -> IssueRecord | None:
        """The live listing of a GitHub issue. A re-list shares the issue with the
        refunded original, so the newest one that is not finished wins."""
        found = [
            r
            for r in self.list_issues()
            if r.repo.lower() == repo.lower()
            and r.number == number
            and r.state not in lifecycle.TERMINAL_STATES
        ]
        return max(found, key=lambda r: r.created_at) if found else None

    def find_by_pull_request(self, repo: str, pr_number: int) -> IssueRecord | None:
        for rec in self.list_issues():
            if (
                rec.repo.lower() == repo.lower()
                and rec.submission is not None
                and rec.submission.pr_number == pr_number
                and rec.state not in lifecycle.TERMINAL_STATES
            ):
                return rec
        return None

    def reprice(self, issue_id: str, now: datetime | None = None) -> IssueRecord:
        """Read the issue again and refresh the engine's suggestion, while unfunded.

        A changed issue is a changed job, so the advice changes with it. **The price
        does not.** That is the publisher's number, and an edit to the issue body on
        GitHub is not the publisher changing their mind about what they will pay
        (#142). Once money is committed nothing here moves at all.
        """
        with self._exclusive(issue_id), self._posting():
            rec = self.repo.get_issue(issue_id)
            if rec is None:
                raise KeyError(issue_id)
            if rec.state not in {IssueState.PRICED, IssueState.AWAITING_APPROVAL}:
                raise lifecycle.IllegalTransition(rec.state, IssueState.PRICED)
            started = time.perf_counter()
            facts = self.github.read_issue(rec.repo, rec.number)
            if facts is None or rec.proposal is None:
                return rec
            reading = read_signals(facts)
            proposal = propose(
                reading.signals,
                compliance_driven=rec.compliance_driven,
                comparables=self._comparables_for(reading.signals, rec.repo, exclude=rec.id),
            )
            rec.title, rec.labels = facts.title, list(facts.labels) or rec.labels
            if proposal.recommended == rec.proposal.recommended:
                self.repo.save_issues(rec)
                return rec
            previous = rec.proposal.recommended
            rec.proposal = proposal
            when = now or _now()
            self._log(
                rec,
                actor="agent",
                action="signals_read",
                rule="github_read_path",
                outcome=f"read {facts.repo}#{facts.number}: {reading.summary()}",
                when=when,
            )
            self._log(
                rec,
                actor="agent",
                action="price_suggested",
                rule="issue_changed",
                outcome=f"the issue changed, so the suggestion moved to "
                f"{proposal.recommended} from {previous}. The publisher's price of "
                f"{rec.price} is unchanged",
                when=when,
            )
            self.repo.save_issues(rec)
            _observe_price(rec, "reprice", started)
            return rec

    def submit_pull_request(
        self, issue_id: str, pr: PullRequest, now: datetime | None = None
    ) -> IssueRecord:
        """A pull request that closes the issue was opened, or pushed to after rework.

        Only the contributor holding the claim submits: anyone can open a pull
        request that mentions an issue, and only one of them is owed money. A pull
        request counts toward one issue only, and a commit the review already judged
        is not submitted again: the contributor pushes a new one.
        """
        with self._exclusive(issue_id), self._posting():
            rec = self.repo.get_issue(issue_id)
            if rec is None:
                raise KeyError(issue_id)
            if rec.contributor_id is None or rec.state not in {
                IssueState.CLAIMED,
                IssueState.REWORK,
            }:
                raise lifecycle.IllegalTransition(rec.state, IssueState.IN_REVIEW)
            claimant = self._handle(rec.contributor_id)
            if pr.author.lower() != claimant.lower():
                raise NotTheSubmission(
                    f"{pr.author} opened #{pr.number}, but {claimant} holds the claim"
                )
            if rec.state is IssueState.REWORK and rec.submission is not None:
                if rec.submission.pr_number != pr.number:
                    raise NotTheSubmission(
                        f"rework continues on #{rec.submission.pr_number}, not #{pr.number}"
                    )
            # Two issues take two different issue locks, so the pull request has one of
            # its own: without it, the same pull request submitted to both at once
            # would pass the check below twice.
            with self._pull_request_exclusive(rec.repo, pr.number):
                elsewhere = self._submitted_elsewhere(rec, pr.number)
                if elsewhere is not None:
                    raise NotTheSubmission(
                        f"#{pr.number} was already submitted for {elsewhere.id}; a pull "
                        "request counts toward one issue only"
                    )
                resubmitted = rec.state is IssueState.REWORK
                history = self.github.merged_pull_requests(rec.repo, pr.author)
                self._submit(
                    rec,
                    {
                        "pr_number": pr.number,
                        "head_sha": pr.head_sha,
                        # None until the project's own checks finish, and check_run says
                        # when they do. A repository with no CI never says.
                        "checks_passed": self.github.checks_passed(rec.repo, pr.head_sha),
                        "files_changed": pr.files_changed,
                        "additions": pr.additions,
                        "deletions": pr.deletions,
                    },
                )
                pushed = (
                    f"pushed a new commit, {pr.head_sha[:7]}, to PR #{pr.number}"
                    if resubmitted
                    else f"opened PR #{pr.number}"
                )
                self._log(
                    rec,
                    actor="contributor",
                    action="resubmitted" if resubmitted else "submitted",
                    rule="pull_request_synchronized" if resubmitted else "pull_request_opened",
                    outcome=f"{claimant} {pushed}; {history} earlier pull request(s) of "
                    f"theirs merged in {rec.repo}",
                    when=now,
                )
                self.repo.save_issues(rec)
            return rec

    def _submitted_elsewhere(self, rec: IssueRecord, pr_number: int) -> IssueRecord | None:
        """Another issue this pull request was submitted for, in any state, paid ones
        included: the same work counted twice is reviewed, accepted and paid twice."""
        for other in self.repo.list_issues():
            if (
                other.id != rec.id
                and other.repo.lower() == rec.repo.lower()
                and other.submission is not None
                and other.submission.pr_number == pr_number
            ):
                return other
        return None

    def record_checks(self, issue_id: str, head_sha: str, now: datetime | None = None) -> bool:
        """Read the project's own checks on the submitted commit. True if that changed
        what the submission records."""
        with self._exclusive(issue_id), self._posting():
            rec = self.repo.get_issue(issue_id)
            if rec is None or rec.submission is None or rec.submission.head_sha != head_sha:
                return False
            passed = self.github.checks_passed(rec.repo, head_sha)
            if passed is None or passed == rec.submission.checks_passed:
                return False
            rec.submission = rec.submission.model_copy(update={"checks_passed": passed})
            self._log(
                rec,
                actor="system",
                action="checks_completed",
                rule="project_checks",
                outcome=f"the project's checks on {head_sha[:7]} "
                f"{'passed' if passed else 'failed'}",
                when=now,
            )
            self.repo.save_issues(rec)
            return True

    def pull_request_closed(
        self, issue_id: str, pr_number: int, *, merged: bool, now: datetime | None = None
    ) -> IssueRecord:
        """The submitted pull request was closed. Merged is acceptance, and acceptance
        is what releases the money; closed without a merge changes nothing, because
        the claim and the deadline already decide what happens next."""
        with self._exclusive(issue_id), self._posting():
            rec = self.repo.get_issue(issue_id)
            if rec is None:
                raise KeyError(issue_id)
            if rec.submission is None or rec.submission.pr_number != pr_number:
                raise NotTheSubmission(f"{rec.id} is not waiting on #{pr_number}")
            when = now or _now()
            if not merged:
                self._log(
                    rec,
                    actor="publisher",
                    action="pull_request_closed",
                    rule="merge_is_acceptance",
                    outcome=f"PR #{pr_number} was closed without merging; nothing is owed "
                    "for it",
                    when=when,
                )
            elif rec.state is IssueState.REJECTED:
                # Taking a rejected patch is the move escrow exists to make costly.
                # REJECTED has no road to PAID, so a person settles it (#39).
                self._log(
                    rec,
                    actor="publisher",
                    action="merged_after_rejection",
                    rule="merge_is_acceptance",
                    outcome=f"the publisher merged PR #{pr_number} after the verdict "
                    "rejected it; flagged for a person to settle",
                    when=when,
                )
            else:
                self._accept_by_merge(rec, pr_number, when)
            self.repo.save_issues(rec)
            return rec

    def _accept_by_merge(self, rec: IssueRecord, pr_number: int, now: datetime) -> None:
        if rec.state is IssueState.REWORK:
            # The publisher merged what was there, so that is the submission.
            self._move(rec, IssueState.IN_REVIEW)
        if rec.state is IssueState.IN_REVIEW:
            self._move(rec, IssueState.ACCEPTED)
            outcome = (
                f"the publisher merged PR #{pr_number} before the review finished, "
                "which is acceptance"
            )
        elif rec.state is IssueState.ACCEPTED:
            outcome = f"the publisher merged PR #{pr_number}, which is acceptance"
        else:
            raise lifecycle.IllegalTransition(rec.state, IssueState.PAID)
        if rec.accepted_by is None:
            self._log(
                rec,
                actor="publisher",
                action="merged",
                rule="merge_is_acceptance",
                outcome=outcome,
                when=now,
            )
            rec.accepted_by = "merge"
        self._pay(rec, now)

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
                "comparables": [
                    {
                        "issue_id": c.issue_id,
                        "repo": c.repo,
                        "title": c.title,
                        "price_usdc": str(c.price.decimal),
                        "settled_at": c.settled_at,
                    }
                    for c in p.comparables
                ],
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
            price_usdc=_display_price(rec),
            proposal=proposal_out,
            escrow=rec.escrow,
            claim=rec.claim,
            submission=rec.submission,
            review=rec.review,
            contributor_id=rec.contributor_id,
            paid_usdc=str(rec.paid.decimal) if rec.paid else None,
            platform_fee_usdc=str(rec.platform_fee.decimal) if rec.platform_fee else None,
            github_url=rec.github_url,
            criteria_approved_at=rec.criteria_approved_at,
            awaiting_approver=rec.payout_hold == PayoutGate.AWAIT_APPROVER.value,
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
                price_usdc=_display_price(rec),
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

    def metrics(self, now: datetime | None = None) -> MetricsOut:
        return metrics.compute(self.list_issues(), now or _now())


# ------------------------------------------------------------------ policy


def _policy(publisher: Publisher) -> SpendingPolicy:
    return SpendingPolicy(
        approval_threshold=(
            Usdc.from_decimal(publisher.approval_threshold_usdc.replace(",", ""))
            if publisher.approval_threshold_usdc
            else None
        ),
        approvers=tuple(publisher.approvers),
        category_limits={
            label: Usdc.from_decimal(limit.replace(",", ""))
            for label, limit in publisher.category_limits.items()
        },
    )


def _display(amount: Usdc) -> str:
    return f"{amount.decimal:,.2f}"


_SECURITY_WORDS = ("security", "cve", "vulnerability")


def _security(label: str) -> bool:
    return any(word in label.lower() for word in _SECURITY_WORDS)


# ------------------------------------------------------------------ observed


def _observe_money(event: MoneyEvent) -> None:
    MONEY_EVENTS.labels(kind=event.kind.value).inc()
    log.info(
        "money event",
        extra={
            "issue_id": event.issue_id,
            "kind": event.kind.value,
            "amount_usdc": f"{event.amount.decimal:.6f}",
            "counterparty_id": event.counterparty_id,
            "tx_hash": event.tx_hash,
        },
    )


def _observe_review(issue_id: str, judgement: Judgement, verdict: str, seconds: float) -> None:
    REVIEW_SECONDS.labels(reviewer=judgement.reviewer, verdict=verdict).observe(seconds)
    REVIEW_COST_USDC.labels(reviewer=judgement.reviewer).observe(float(judgement.cost.decimal))
    log.info(
        "verdict issued",
        extra={
            "issue_id": issue_id,
            "verdict": verdict,
            "reviewer": judgement.reviewer,
            "seconds": seconds,
            "cost_usdc": _cost_text(judgement.cost),
        },
    )


def _observe_price(rec: IssueRecord, source: str, started: float) -> None:
    seconds = time.perf_counter() - started
    PRICE_SECONDS.labels(source=source).observe(seconds)
    log.info(
        "price set",
        extra={
            "issue_id": rec.id,
            "source": source,
            "seconds": round(seconds, 3),
            # The publisher's number, which is the one that binds. The suggestion is
            # logged beside it so a divergence between the two stays visible (#142).
            "price_usdc": f"{rec.price.decimal:.2f}" if rec.price else None,
            "suggested_usdc": (
                f"{rec.proposal.recommended.decimal:.2f}" if rec.proposal else None
            ),
        },
    )


def _display_price(rec: IssueRecord) -> str | None:
    """The price to show for an issue: the publisher's, or the suggestion on an issue
    published before #142 and never re-priced."""
    if rec.price is not None:
        return f"{rec.price.decimal:.2f}"
    if rec.proposal is not None:
        return f"{rec.proposal.recommended.decimal:.2f}"
    return None


def _alert(divergence: Divergence) -> None:
    DIVERGENCE_ALERTS.inc()
    log.error(
        "ALERT ledger divergence: %s",
        divergence,
        extra={
            "alert": "ledger_divergence",
            "issue_id": divergence.issue_id,
            "ledger": divergence.ledger,
            "chain": divergence.chain,
        },
    )


# ------------------------------------------------------------------ review


def _reviewer(model: str) -> Reviewer:
    return build_reviewer(
        settings.anthropic_api_key,
        model,
        input_usd_per_mtok=settings.review_input_usd_per_mtok,
        output_usd_per_mtok=settings.review_output_usd_per_mtok,
        api_url=settings.anthropic_api_url,
    )


def _rework_rounds(rec: IssueRecord) -> int:
    """Rounds of rework so far: the agent's rework verdicts and a publisher's decline."""
    return sum(
        1
        for d in rec.decisions
        if (d.action == "verdict_issued" and d.outcome.startswith(Verdict.REWORK.value))
        or d.action == "publisher_declined"
    )


def _submitted_at(rec: IssueRecord) -> datetime | None:
    """When the commit under review was submitted: the decision log's latest
    submission, which is the record of it."""
    times = [d.created_at for d in rec.decisions if d.action in {"submitted", "resubmitted"}]
    return max(times) if times else None


def _disputed_since(rec: IssueRecord, verdict_at: datetime) -> bool:
    return any(d.action == "dispute_opened" and d.created_at >= verdict_at for d in rec.decisions)


def _cost_text(cost: Usdc) -> str:
    return f"{cost.decimal:.6f}"


# ------------------------------------------------------------------ GitHub text


def _summary(body: str) -> str:
    """The issue's first paragraph, short enough for a listing."""
    first = body.strip().split("\n\n", 1)[0].strip()
    return first if len(first) <= 280 else first[:277].rstrip() + "..."


def _criteria_comment(rec: IssueRecord, committed: Usdc) -> str:
    assert rec.deadline is not None
    criteria = "\n".join(f"- [ ] {c}" for c in rec.acceptance_criteria)
    return (
        f"**Funded on Misthos: {committed} USDC**, held in escrow until "
        f"{rec.deadline:%d %B %Y}.\n\n"
        "The first contributor to claim it has 72 hours to open a pull request that "
        "closes this issue. The pull request is accepted when it meets these "
        f"criteria:\n\n{criteria}\n\n"
        "Merging it is acceptance, and acceptance releases the payment."
    )


def _review_comment(verdict: str, findings: list[str]) -> str:
    heading = {
        "accept": "Accepted: the acceptance criteria are met.",
        "rework": "Rework needed before this can be accepted.",
        "reject": "Rejected: this does not meet the acceptance criteria.",
    }[verdict]
    listed = "\n".join(f"- {f}" for f in findings)
    return f"**Misthos review.** {heading}\n\n{listed}" if listed else heading


class _StoreHandle:
    """The process's store, built on first use.

    A test installs its own Store, with in-memory gateways at the boundaries, before
    anything reads this handle. The application has no branch for that: the handle is
    simply where the store lives.
    """

    def __init__(self) -> None:
        self._store: Store | None = None

    def install(self, replacement: Store) -> None:
        self._store = replacement

    def current(self) -> Store:
        if self._store is None:
            self._store = Store(build_repository(settings.database_url))
        return self._store

    def __getattr__(self, name: str):
        return getattr(self.current(), name)


store = _StoreHandle()
