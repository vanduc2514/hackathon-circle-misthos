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

import copy
import dataclasses
import logging
import random
import threading
import time
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from misthos.config import settings
from misthos.domain import comparables, compliance, ledger, plans, pricing, timers
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
from misthos.domain.issue import IssueState
from misthos.domain.ledger import Divergence, MoneyEvent, MoneyEventKind
from misthos.domain.money import Usdc
from misthos.domain.policy import (
    PolicyRefusal,
    SpendingPolicy,
    approves_own_payout,
    category_breaches,
    committed_this_month,
    login,
    may_approve,
    month_start,
    needs_approval,
)
from misthos.domain.pricing import (
    WEIGHTS,
    ComplexitySignals,
    UnfundableIssue,
    effort,
    propose,
    take_rate_bps,
)
from misthos.domain.review import (
    ChangedFile,
    Judgement,
    Submitted,
    Verdict,
    decide,
    ready_for_review,
)
from misthos.domain.signals import read as read_signals
from misthos.domain.timers import TimedAction
from misthos.models.records import IssueRecord
from misthos.observability.metrics import (
    DIVERGENCE_ALERTS,
    DIVERGENCES,
    MONEY_EVENTS,
    PRICE_SECONDS,
    REVIEW_COST_USDC,
    REVIEW_SECONDS,
)
from misthos.repositories import (
    MemoryRepository,
    PaymentAlreadyUsed,
    Repository,
    Seed,
    StaleIssue,
    build_repository,
)
from misthos.schemas import (
    Account,
    Claim,
    Contributor,
    Decision,
    EscrowCommitment,
    FileableItem,
    IssueOut,
    IssueSummaryOut,
    LoopOut,
    LoopSettlement,
    MetricsOut,
    PaymentRequest,
    Publisher,
    RepoConnection,
    Review,
    SpendCategory,
    SpendOut,
    Submission,
    Subscription,
    SubscriptionOut,
    SubscriptionPayment,
    TimelineEntry,
    Wallet,
    money,
)
from misthos.services import metrics, reputation
from misthos.services.billing import PaymentRail, SimulatedRail, build_rail
from misthos.services.chain import ChainGateway, SimulatedChain, load_deployment
from misthos.services.chain.factory import build_chain
from misthos.services.compliance import (
    IdentityProvider,
    ScreeningProvider,
    SimulatedIdentity,
    SimulatedScreening,
)
from misthos.services.coordination import ISSUE_LOCK_TTL, Busy, Coordinator, build_coordinator
from misthos.services.finance import Finance, FinanceContext, FinanceError, build_finance
from misthos.services.github import (
    GitHubError,
    GitHubGateway,
    PullRequest,
    ReviewEvent,
    SimulatedGitHub,
    StatusState,
    build_github,
)
from misthos.services.review import Reviewer, build_reviewer

__all__ = ["IssueRecord", "Store", "store"]


def _demo_files(repo: str) -> list[ChangedFile]:
    """The file list of a pull request the simulation makes up."""
    slug = repo.split("/")[-1].replace("-", "_")
    return [
        ChangedFile(f"src/{slug}/fix.py", additions=40, deletions=6),
        ChangedFile(f"tests/test_{slug}.py", additions=22),
        ChangedFile(f"docs/{slug}.md", additions=12),
        ChangedFile("CHANGELOG.md", additions=2),
    ]


# Recorded on every commitment: the address `mise run contracts:deploy` recorded under
# contracts/deployments/, or MISTHOS_ESCROW_CONTRACT. Only the simulation may run
# without one, under a placeholder that is labelled as simulated wherever it shows.
ESCROW = load_deployment(settings)
ESCROW_CONTRACT = ESCROW.address
CHAIN = settings.chain

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


class DisputeRefused(Exception):
    """A dispute the verdict cannot take: one dispute per verdict."""


class AccountExists(Exception):
    """A wallet chooses its role once."""


class CriteriaNotApproved(Exception):
    """No funding before the publisher approves the acceptance criteria (#21)."""


class PlanNotSelfServe(Exception):
    """A plan that is agreed with us rather than bought from the web app."""


class NoPaymentDue(Exception):
    """Nothing is waiting to be paid for: choose a plan first, or the request expired."""


class PaymentNotFound(Exception):
    """The transaction does not pay what is due, from the publisher, to the platform."""


class BillingUnavailable(Exception):
    """Plans cannot be paid for here: no platform wallet is configured."""


# Where the simulation's subscription payments go when no platform wallet is set.
SIMULATED_PLATFORM_WALLET = "0x000000000000000000000000000000000000c0de"


class UntestableCriteria(Exception):
    """Criteria a reviewer could not judge are not approved (#38)."""

    def __init__(self, problems: list[acceptance.Problem]) -> None:
        super().__init__("; ".join(f"criterion {p.index} {p.reason}" for p in problems))
        self.problems = problems


class NotSimulated(Exception):
    """A step only the simulated GitHub can take, asked of the real one."""


class DeclineRefused(Exception):
    """A publisher declines a passing verdict once, and only inside the grace period;
    after that the merge or the grace period settles it."""


# How long a process that lost the race to seed an empty database waits for the one
# that won. Seeding takes well under a second, so a holder still busy after this is
# stuck or gone, and the loser carries on unready and asks again on its next call.
SEED_WAIT = timedelta(seconds=10)
_SEED_POLL_SECONDS = 0.05


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
        finance: Finance | None = None,
        rail: PaymentRail | None = None,
    ) -> None:
        self.repo: Repository = repository or MemoryRepository()
        # A publisher's books, read-only, where the operator connected them (#43).
        self.finance: Finance = finance or build_finance()
        # Where subscription payments are seen: Arc, or the simulation's own rail.
        self.rail: PaymentRail = rail or build_rail()
        # The simulated escrow keeps its books beside the repository's tables when
        # there is a database, so a restart does not make every issue look divergent.
        # Outside the simulation it is the deployed MisthosEscrow (#69).
        self.chain: ChainGateway = chain or build_chain(
            settings,
            getattr(self.repo, "engine", None),
            lambda: [rec.id for rec in self.repo.list_issues()],
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

        An API process and a worker starting together against an empty database race
        for the seed lock. The one that loses waits until it can take the lock itself,
        which is once the winner has seeded or died, and then looks again: marking
        itself ready on losing would have it serve the database as it found it, empty,
        for as long as it runs. If the wait runs out it stays unready, so its next call
        looks again.
        """
        if self._ready:
            return
        with self._ready_guard:
            if self._ready:
                return
            give_up = time.monotonic() + SEED_WAIT.total_seconds()
            while True:
                with self.repo.try_lock("seed") as held:
                    if held:
                        # The demo seed funds made-up issues from made-up wallets, so
                        # it is only ever written against the simulated escrow.
                        if self.repo.is_empty() and isinstance(self.chain, SimulatedChain):
                            self._seed()
                        self._ready = True
                        return
                if time.monotonic() >= give_up:
                    log.warning("another process still holds the seed lock; looking again later")
                    return
                time.sleep(_SEED_POLL_SECONDS)

    def reset(self) -> None:
        """Put the simulation back to its seed, as one step for anyone reading it.

        The seed is built first, against a repository of its own, and the repository
        then swaps its contents for it whole. Someone stepping the demo meanwhile sees
        the old issues or the new ones, never an empty table, and a copy they loaded
        before is refused as stale after.
        """
        with self._ready_guard:
            migrate = getattr(self.repo, "migrate", None)
            if migrate is not None:
                migrate()  # the simulated chain keeps its books in the same database
            self.chain.reset()
            self.coordinator.reset()
            if isinstance(self.rail, SimulatedRail):
                self.rail.reset()
            reset_github = getattr(self.github, "reset", None)
            if reset_github is not None:
                reset_github()  # the simulation's fixtures and sent posts
            self.repo.reset(self._built_seed())
            self._ready = True

    def _built_seed(self) -> Seed:
        """The seed, built by this store pointed at an empty repository of its own.

        The chain and the simulated GitHub are this store's own, so the seed's
        commitments and pull requests are the ones the reset store will see.
        """
        staging = MemoryRepository()
        builder = copy.copy(self)
        builder.repo = staging
        builder._ready = True  # it writes the seed; it must never go looking for one
        builder._seed()
        return staging.as_seed()

    def _seed(self) -> None:
        random.seed(7)
        for publisher in _seed_publishers().values():
            self.repo.save_publisher(publisher)
        for contributor in _seed_contributors().values():
            self.repo.save_contributor(contributor)
        for spec in _ISSUE_SPECS:
            self.repo.save_issues(self._build(spec))
        # Standing comes from settled issues alone, never from the fixture's numbers.
        self.rebuild_standing()

    def _build(self, spec: dict) -> IssueRecord:
        publisher = self.repo.get_publisher(spec["publisher_id"])
        assert publisher is not None, spec["publisher_id"]
        signals = ComplexitySignals(**spec["signals"])
        proposal = propose(
            signals,
            compliance_driven=spec.get("compliance_driven", False),
            # Settled issues of the same shape, from the platform's own history (#42).
            comparables=self._comparables_for(signals, spec["repo"]),
            urgency=Usdc.from_decimal(spec["urgency"]) if spec.get("urgency") else None,
            risk_premium=Usdc.from_decimal(spec["risk"]) if spec.get("risk") else None,
            # The publisher's remaining budget actually constrains the price, which
            # is the whole point of reading it. Seeded demo issues declare none.
            affordability_ceiling=spec.get("affordability_ceiling"),
            ceiling_source=spec.get("ceiling_source"),
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
            action="price_proposed",
            rule="complexity_score_v1",
            outcome=f"proposed {proposal.recommended} for {proposal.estimated_hours}h",
            cost="0.00",
        )

        if target in _PRE_FUNDING:
            return rec

        # Everything from FUNDED onward has approved criteria and money behind it.
        rec.criteria_approved_at = rec.created_at + timedelta(hours=5)
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
        self._fabricate_files(rec)

        if target is IssueState.IN_REVIEW:
            return rec

        self._review(
            rec,
            verdict=spec["verdict"],
            findings=spec["findings"],
            when=rec.created_at + timedelta(hours=44),
        )

        if target is IssueState.REWORK:
            self._log(
                rec,
                actor="agent",
                action="verdict_issued",
                rule="criteria_unmet",
                outcome="rework by review_agent_v1: " + " ".join(spec["findings"]),
                when=rec.created_at + timedelta(hours=44),
            )
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
        committed = rec.proposal.recommended
        deadline = at + lifecycle.ESCROW_TERM
        # The approved price goes to the escrow as its ceiling first: the escrow
        # refuses any commitment without one or above it, so what an agent can commit
        # is bounded by what a person approved, on chain and not in this code.
        self.chain.set_ceiling(rec.id, committed, at)
        # The rate is fixed here, with the money. The escrow enforces the rate it was
        # given on release, so reading the publisher's plan at settlement time instead
        # would let a plan that lapses mid-flight move the fee after the price was
        # approved, and the record would stop matching the transfer (#32).
        fee_bps = take_rate_bps(publisher.tier)
        tx = self.chain.commit(rec.id, publisher.wallet.address, committed, deadline, at, fee_bps)
        self._book(rec, MoneyEventKind.COMMITTED, committed, publisher.id, tx, at)
        rec.escrow = EscrowCommitment(
            issue_id=rec.id,
            contract=ESCROW_CONTRACT,
            chain=CHAIN,
            tx_hash=tx,
            amount=money(committed),
            deadline=deadline,
            fee_bps=fee_bps,
        )
        rec.deadline = rec.escrow.deadline
        criteria = _criteria_comment(rec)
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
        self._move(rec, IssueState.PAID)
        at = when or _now()
        amount = self._committed(rec)
        tx = self.chain.release(rec.id, contributor.wallet.address, amount, at)
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
            f"from escrow on {CHAIN} when the work was accepted."
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
        proposal = pricing.relist(rec.proposal, ceiling=self._ceiling(publisher)[0])
        if proposal is None:
            self._log(
                rec,
                actor="agent",
                action="relist_declined",
                rule="affordability_ceiling",
                # The log is public and the budget is not, so the figure stays out.
                outcome="what remains of the publisher's budget cannot carry a higher "
                "price, so the issue was not re-listed",
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
            self._release(rec, rule=rule, when=now)
            return True

        if rec.payout_hold != gate.value:
            if gate is PayoutGate.AWAIT_APPROVER:
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
        assert publisher is not None and rec.proposal is not None
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
        breaches = category_breaches(
            policy, rec.labels, rec.proposal.recommended, committed
        )
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
        self.ensure_ready()
        return self.repo.purge_screenings((now or _now()) - compliance.SCREENING_RETENTION)

    # ------------------------------------------------------------- GitHub posts

    def _post(self, what: str, send: Callable[[GitHubGateway], None]) -> None:
        """Queue a post for after the save. Outside an action (seeding) it is dropped:
        the seed is history, and history was posted when it happened."""
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

    def reconcile(self, now: datetime | None = None) -> list[Divergence]:
        """Compare every issue's ledger with what the chain holds, and raise the alarm.

        A divergence found in the first pass is checked again under the issue's lock,
        because an action caught between its chain call and its save looks divergent
        for an instant and is not. What survives is logged as an alert and written to
        the issue's decision log once.
        """
        self.ensure_ready()
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

    def run_timers(self, issue_id: str, now: datetime | None = None) -> list[TimedAction]:
        """Apply what is due on one issue and save it.

        The sweeper's entry point. It holds the issue's lock and reads the issue
        afresh inside it, so a timer acts on what a person last saved rather than on
        the copy the sweeper listed. Raises Busy when someone else holds the issue.
        """
        self.ensure_ready()
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
    def advance(self, issue_id: str) -> IssueRecord:
        """Move an issue one step forward along the demo path.

        A timer that is already due is the step: the demo cannot claim an issue
        whose deadline has passed or merge past a silent publisher's grace window,
        any more than real time would let it.
        """
        self.ensure_ready()
        current = self.repo.get_issue(issue_id)
        if (
            current is not None
            and current.state is IssueState.IN_REVIEW
            and timers.due(self._clocks(current), _now()) is None
        ):
            # The step is the review agent's verdict, which may take minutes and so
            # runs outside the issue's lock.
            return self.review(issue_id) or self.get(issue_id) or current
        with self._exclusive(issue_id), self._posting():
            return self._advance(issue_id)

    def _advance(self, issue_id: str) -> IssueRecord:
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
                # The demo's publisher approves the drafted criteria as they stand.
                rec.criteria_approved_at = rec.criteria_approved_at or now
                self._approve_and_fund(
                    rec, now, "approved the criteria and the price, and committed funds"
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
                self._fabricate_files(rec)
                self._log(
                    rec,
                    actor="contributor",
                    action="submitted",
                    rule="pull_request_opened",
                    outcome=f"opened PR #{rec.submission.pr_number} with checks passing",
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
                        "pr_number": (
                            rec.submission.pr_number if rec.submission else rec.number + 400
                        ),
                        "head_sha": f"{random.getrandbits(160):040x}",
                        "checks_passed": True,
                        "files_changed": 4,
                        "additions": 88,
                        "deletions": 21,
                    },
                )
                self._fabricate_files(rec)
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
        started = time.perf_counter()
        facts = None
        if payload.number and not payload.signals:
            try:
                facts = self.github.read_issue(payload.repo, payload.number)
            except GitHubError as exc:
                raise UnreadableIssue(
                    f"could not read {payload.repo}#{payload.number} from GitHub: {exc}"
                ) from exc
        ceiling, ceiling_source = self._ceiling(publisher)
        spec = {
            "affordability_ceiling": ceiling,
            "ceiling_source": ceiling_source,
            "repo": payload.repo,
            "number": payload.number or random.randint(100, 999),
            "title": payload.title,
            "summary": payload.summary or "Published through the web application.",
            "state": IssueState.PRICED,
            "labels": payload.labels or ["needs-price"],
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
        _observe_price(rec, "github" if facts is not None else "form", started)
        return rec

    # ------------------------------------------------------------- plans (#53)

    def entitled(self, publisher_id: str, feature: plans.Feature) -> bool:
        """Whether the publisher's plan includes the feature."""
        self.ensure_ready()
        publisher = self.repo.get_publisher(publisher_id)
        if publisher is None:
            raise KeyError(publisher_id)
        return plans.allows(publisher.tier, feature)

    def _pay_to(self) -> str:
        if settings.platform_wallet:
            return settings.platform_wallet
        if settings.simulated:
            return SIMULATED_PLATFORM_WALLET
        raise BillingUnavailable("no platform wallet is configured, so plans cannot be paid for")

    def subscription(self, publisher_id: str, now: datetime | None = None) -> SubscriptionOut:
        """The plan in force, its period, what is waiting to be paid, and every payment."""
        self.ensure_ready()
        now = now or _now()
        publisher = self.repo.get_publisher(publisher_id)
        if publisher is None:
            raise KeyError(publisher_id)
        sub = self.repo.get_subscription(publisher_id)
        pending = sub.pending if sub and sub.pending and sub.pending.expires_at > now else None
        if sub is None:
            status = "none" if publisher.tier == "open" else "contract"
        else:
            status = sub.status
        grace_ends = None
        if sub is not None and sub.status == "past_due" and sub.period_end is not None:
            grace_ends = sub.period_end + plans.GRACE
        return SubscriptionOut(
            publisher_id=publisher_id,
            plan=publisher.tier,
            status=status,  # type: ignore[arg-type]
            period_end=sub.period_end if sub else None,
            grace_ends=grace_ends,
            cancel_at_period_end=bool(sub and sub.cancel_at_period_end),
            pending=pending,
            payments=self.repo.list_subscription_payments(publisher_id),
            features=sorted(plans.FEATURE_NAMES[f] for f in plans.PLANS[publisher.tier].features),
        )

    def _unfinished_payment(
        self,
        publisher_id: str,
        attempted: SubscriptionPayment,
        sub: Subscription,
        request: PaymentRequest,
    ) -> SubscriptionPayment | None:
        """The recorded row that is *this* attempt's, when activation never ran.

        The payment is written before the subscription is, so a crash in between leaves
        a paid period that was never activated. Finishing that row is recovery. A hash
        from an *earlier* period is a replay, and the difference is whether the
        subscription has already moved past the row: if it has, the row was activated
        once and the hash is spent.

        Four things have to agree for it to be this attempt's own, and any of them
        failing refuses rather than guesses: the hash, the plan and the amount, that the
        subscription never reached the row's period start, and that the row was written
        inside the window the caller is currently answering. Without the last one a
        subscription that later lost its period end could be talked into re-activating a
        long-spent hash.
        """
        for recorded in self.repo.list_subscription_payments(publisher_id):
            if recorded.tx_hash != attempted.tx_hash:
                continue
            if recorded.plan != attempted.plan:
                return None
            # Numeric, not the formatted string: the row is written from the rail's
            # amount and the attempt from the request, and the two spell 249.00 and
            # 249.000000. Comparing the text would refuse a genuine recovery.
            if Usdc.from_decimal(recorded.amount_usdc) != Usdc.from_decimal(
                attempted.amount_usdc
            ):
                return None
            if sub.period_end is not None and sub.period_end > recorded.period_start:
                return None  # the subscription is already past it: spent
            window_opened = request.expires_at - plans.PAYMENT_WINDOW
            if recorded.paid_at < window_opened:
                return None  # written for an earlier request, not this one
            return recorded
        return None

    @staticmethod
    def _refuse_self_serve_enterprise(publisher: Publisher, sub: Subscription | None) -> None:
        """A contract plan is changed with us, not from the plan picker.

        Applies to Open as well: ending an Enterprise plan is a change like any other,
        and the plan picker is not where it happens.
        """
        if publisher.tier == "enterprise" and (sub is None or sub.plan == "enterprise"):
            raise PlanNotSelfServe(
                "this organisation's Enterprise plan is agreed with us, so it is changed "
                "with us too"
            )

    def subscribe(
        self, publisher_id: str, plan_id: str, now: datetime | None = None
    ) -> SubscriptionOut:
        """Choose a plan. A paid plan returns what to send; Open cancels at period end."""
        self.ensure_ready()
        now = now or _now()
        plan = plans.PLANS[plan_id]
        with self._exclusive(f"subscription:{publisher_id}"):
            publisher = self.repo.get_publisher(publisher_id)
            if publisher is None:
                raise KeyError(publisher_id)
            sub = self.repo.get_subscription(publisher_id)
            if plan_id == "open":
                # Open is self-serve too, and it used to return before this guard: a
                # contract customer could end their own Enterprise plan by choosing
                # Open, which is the downgrade the guard exists to refuse.
                self._refuse_self_serve_enterprise(publisher, sub)
                if sub is not None:
                    sub.pending = None
                    if sub.status in {"active", "past_due"}:
                        # Paid for to the end of the period; Open from then on.
                        sub.cancel_at_period_end = True
                    sub.updated_at = now
                    self.repo.save_subscription(sub)
                log.info("%s chose Open", publisher_id)
                return self.subscription(publisher_id, now)
            if not plan.self_serve:
                raise PlanNotSelfServe(
                    f"{plan.name} starts at {plan.monthly} a month and is agreed with us, "
                    "not bought here"
                )
            self._refuse_self_serve_enterprise(publisher, sub)
            request = PaymentRequest(
                plan=plan_id,  # type: ignore[arg-type]
                amount_usdc=f"{plan.monthly.decimal:.2f}",
                pay_to=self._pay_to(),
                payer=publisher.wallet.address,
                chain=CHAIN,
                chain_id=settings.chain_id,
                usdc_address=settings.usdc_address,
                expires_at=now + plans.PAYMENT_WINDOW,
            )
            if sub is None:
                sub = Subscription(
                    publisher_id=publisher_id,
                    plan=plan_id,  # type: ignore[arg-type]
                    status="pending",
                    updated_at=now,
                )
            sub.pending = request
            sub.updated_at = now
            self.repo.save_subscription(sub)
            return self.subscription(publisher_id, now)

    def confirm_payment(
        self, publisher_id: str, tx_hash: str, now: datetime | None = None
    ) -> SubscriptionOut:
        """The publisher says it paid. The rail says whether it did; then the plan is on."""
        self.ensure_ready()
        now = now or _now()
        with self._exclusive(f"subscription:{publisher_id}"):
            publisher = self.repo.get_publisher(publisher_id)
            if publisher is None:
                raise KeyError(publisher_id)
            sub = self.repo.get_subscription(publisher_id)
            if sub is None or sub.pending is None or sub.pending.expires_at <= now:
                raise NoPaymentDue("nothing is waiting to be paid for; choose a plan first")
            request = sub.pending
            seen = self.rail.received(tx_hash, payer=request.payer, payee=request.pay_to)
            due = Usdc.from_decimal(request.amount_usdc)
            if seen is None:
                raise PaymentNotFound(
                    f"{tx_hash} moved no USDC from {request.payer} to {request.pay_to}"
                )
            if seen.amount < due:
                raise PaymentNotFound(f"{tx_hash} moved {seen.amount}, and {due} is due")
            renewing = (
                sub.status == "active"
                and sub.plan == request.plan
                and sub.period_end is not None
                and sub.period_end > now
            )
            start = sub.period_end if renewing and sub.period_end else now
            payment = SubscriptionPayment(
                publisher_id=publisher_id,
                plan=request.plan,
                amount_usdc=str(seen.amount.decimal),
                tx_hash=tx_hash.lower(),
                period_start=start,
                period_end=start + plans.PERIOD,
                paid_at=now,
            )
            try:
                # Claiming the transaction comes first, so it pays for one period only.
                self.repo.add_subscription_payment(payment)
            except PaymentAlreadyUsed:
                recovery = self._unfinished_payment(publisher_id, payment, sub, request)
                if recovery is None:
                    # A hash that already paid for a period. Refusing is what the
                    # constraint is for: the old code finished these either way, so a
                    # stale hash copied out of wallet history overwrote an activated
                    # period with an older one and reported success while doing it.
                    raise PaymentAlreadyUsed(
                        f"{tx_hash} has already paid for a period; "
                        "one transaction pays for one period"
                    ) from None
                # The row is this attempt's own: it was recorded and the process died
                # before the subscription was saved. Finish what it started.
                payment = recovery
            sub.plan = payment.plan
            sub.period_start = sub.period_start if renewing else payment.period_start
            sub.status = "active"
            sub.period_end = payment.period_end
            sub.pending = None
            sub.cancel_at_period_end = False
            sub.updated_at = now
            self.repo.save_subscription(sub)
            publisher.tier = payment.plan
            self.repo.save_publisher(publisher)
            log.info("%s is on %s until %s", publisher_id, payment.plan, payment.period_end)
            return self.subscription(publisher_id, now)

    def demo_pay(self, publisher_id: str, now: datetime | None = None) -> SubscriptionOut:
        """Pay what is due from the publisher's wallet, on the simulation's rail."""
        if not isinstance(self.rail, SimulatedRail):
            raise NotSimulated("pay from your wallet on Arc, then confirm the transaction")
        self.ensure_ready()
        sub = self.repo.get_subscription(publisher_id)
        if sub is None or sub.pending is None:
            raise NoPaymentDue("nothing is waiting to be paid for; choose a plan first")
        request = sub.pending
        tx = self.rail.send(request.payer, request.pay_to, Usdc.from_decimal(request.amount_usdc))
        return self.confirm_payment(publisher_id, tx, now)

    def set_contract_plan(
        self, publisher_id: str, plan_id: str, until: datetime, now: datetime | None = None
    ) -> SubscriptionOut:
        """A plan agreed with us, such as Enterprise, in force until the contract ends."""
        self.ensure_ready()
        now = now or _now()
        with self._exclusive(f"subscription:{publisher_id}"):
            publisher = self.repo.get_publisher(publisher_id)
            if publisher is None:
                raise KeyError(publisher_id)
            sub = self.repo.get_subscription(publisher_id) or Subscription(
                publisher_id=publisher_id,
                plan=plan_id,  # type: ignore[arg-type]
                status="active",
                updated_at=now,
            )
            sub.plan = plan_id  # type: ignore[assignment]
            sub.status = "active"
            sub.period_start, sub.period_end = now, until
            sub.pending, sub.cancel_at_period_end, sub.updated_at = None, False, now
            self.repo.save_subscription(sub)
            publisher.tier = plan_id  # type: ignore[assignment]
            self.repo.save_publisher(publisher)
        return self.subscription(publisher_id, now)

    def sweep_subscriptions(self, now: datetime | None = None) -> list[str]:
        """Periods that ended: past due with a grace period, then back on Open.

        A cancelled plan ends at its period's end with no grace, because nothing is
        owed. The organisation's spending policy stays in force either way.
        """
        self.ensure_ready()
        now = now or _now()
        changed: list[str] = []
        for listed in self.repo.list_subscriptions():
            if listed.period_end is None or listed.period_end > now:
                continue
            try:
                with self._exclusive(f"subscription:{listed.publisher_id}"):
                    # Read afresh under the lock: a payment may have just renewed it.
                    sub = self.repo.get_subscription(listed.publisher_id)
                    if sub is None or sub.period_end is None or sub.period_end > now:
                        continue
                    graced = sub.period_end + plans.GRACE <= now
                    if sub.status == "active" and sub.cancel_at_period_end:
                        sub.status = "cancelled"
                    elif sub.status in {"active", "past_due"} and graced:
                        sub.status = "lapsed"
                    elif sub.status == "active":
                        sub.status = "past_due"
                    else:
                        continue
                    sub.updated_at = now
                    self.repo.save_subscription(sub)
                    if sub.status in {"cancelled", "lapsed"}:
                        publisher = self.repo.get_publisher(sub.publisher_id)
                        if publisher is not None and publisher.tier != "open":
                            publisher.tier = "open"
                            self.repo.save_publisher(publisher)
            except Busy:
                continue
            changed.append(sub.publisher_id)
            log.info("%s's %s plan is %s", sub.publisher_id, sub.plan, sub.status)
        return changed

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

    def _ceiling(self, publisher: Publisher) -> tuple[Usdc, str | None]:
        """What a price may not exceed: the declared budget, or less where the
        publisher's connected books say less is left (#43). The books failing to answer
        never blocks a price; the declared budget still holds."""
        declared = _budget(publisher)
        source = self.finance(publisher.id)
        if source is None:
            return declared, None
        try:
            read = source.read(_now())
        except FinanceError as exc:
            log.warning("finance for %s could not be read: %s", publisher.id, exc)
            return declared, None
        if read.budget_remaining is None or declared < read.budget_remaining:
            return declared, None
        return read.budget_remaining, read.source

    def finance_context(self, publisher_id: str) -> tuple[Publisher, FinanceContext | None, str]:
        """The publisher's declared budget and what its books say, for its own eyes."""
        self.ensure_ready()
        publisher = self.repo.get_publisher(publisher_id)
        if publisher is None:
            raise KeyError(publisher_id)
        source = self.finance(publisher_id)
        if source is None:
            return publisher, None, ""
        try:
            return publisher, source.read(_now()), ""
        except FinanceError as exc:
            return publisher, None, str(exc)

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
        self.ensure_ready()
        now = now or _now()
        return [
            r.id for r in self.list_issues({IssueState.IN_REVIEW}) if self._awaiting_review(r, now)
        ]

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
        self.ensure_ready()
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
        self.ensure_ready()
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
        self.ensure_ready()
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
        self.ensure_ready()
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
        self.ensure_ready()
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

    def spend(
        self, publisher_id: str, year: int | None = None, now: datetime | None = None
    ) -> SpendOut:
        """What a publisher budgeted, committed, released and refunded in a year, by
        category, and the settled fixes a security review can file (#50).

        Each category also carries what was committed this calendar month, counted by
        the function funding checks the monthly limit with, so the panel answers the
        question the limit asks.
        """
        self.ensure_ready()
        publisher = self.repo.get_publisher(publisher_id)
        if publisher is None:
            raise KeyError(publisher_id)
        now = now or _now()
        year = year or now.year
        policy = _policy(publisher)
        mine = [r for r in self.repo.list_issues() if r.publisher_id == publisher_id]
        this_month = committed_this_month(((r.labels, r.money_events) for r in mine), now)

        def total(kind: MoneyEventKind) -> Usdc:
            return Usdc(
                sum(
                    e.amount.base_units
                    for r in mine
                    for e in r.money_events
                    if e.kind is kind and e.occurred_at.year == year
                )
            )

        held = Usdc(
            sum(
                p.held.base_units
                for r in mine
                if (p := ledger.position(r.money_events)) is not None
            )
        )
        labels = sorted({label for r in mine for label in r.labels} | set(policy.category_limits))
        by_category = []
        for label in labels:
            tagged = [r for r in mine if label in r.labels]

            def tagged_total(kind: MoneyEventKind, issues: list[IssueRecord] = tagged) -> int:
                return sum(
                    e.amount.base_units
                    for r in issues
                    for e in r.money_events
                    if e.kind is kind and e.occurred_at.year == year
                )

            limit = policy.category_limits.get(label)
            by_category.append(
                SpendCategory(
                    label=label,
                    committed=money(Usdc(tagged_total(MoneyEventKind.COMMITTED))),
                    released=money(Usdc(tagged_total(MoneyEventKind.RELEASED))),
                    committed_this_month=money(this_month.get(label, Usdc(0))),
                    limit=money(limit) if limit is not None else None,
                )
            )

        fileable = [
            FileableItem(
                issue_id=r.id,
                repo=r.repo,
                number=r.number,
                title=r.title,
                labels=r.labels,
                compliance_driven=r.compliance_driven,
                acceptance_criteria=r.acceptance_criteria,
                amount=money(e.amount),
                settled_at=e.occurred_at,
                github_url=r.github_url,
            )
            for r in mine
            for e in r.money_events
            if e.kind is MoneyEventKind.RELEASED
            and e.occurred_at.year == year
            and (r.compliance_driven or any(_security(label) for label in r.labels))
        ]
        return SpendOut(
            publisher_id=publisher.id,
            name=publisher.name,
            year=year,
            month=f"{month_start(now):%Y-%m}",
            budget_remaining=money(_budget(publisher)),
            committed_held=money(held),
            committed=money(total(MoneyEventKind.COMMITTED)),
            released=money(total(MoneyEventKind.RELEASED)),
            refunded=money(total(MoneyEventKind.REFUNDED)),
            by_category=by_category,
            fileable=sorted(fileable, key=lambda f: f.settled_at),
        )

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
        self.ensure_ready()
        return [
            e
            for e in reputation.events(self.repo.list_issues())
            if e.contributor_id == contributor_id
        ]

    # ------------------------------------------------------------- the loop

    def loop(self, repo: str | None = None, now: datetime | None = None) -> LoopOut:
        """The loop in public: funded, settled and paid, for one repository or all."""
        self.ensure_ready()
        now = now or _now()
        records = [
            r for r in self.repo.list_issues() if repo is None or r.repo.lower() == repo.lower()
        ]
        handles = {c.id: c.handle for c in self.repo.list_contributors()}
        settled = sorted(
            (
                (e, r)
                for r in records
                for e in r.money_events
                if e.kind is MoneyEventKind.RELEASED
            ),
            key=lambda pair: pair[0].occurred_at,
            reverse=True,
        )
        return LoopOut(
            repo=repo,
            funded_open=sum(
                1 for r in records if r.escrow is not None and lifecycle.is_open(r.state)
            ),
            settled_issues=len(settled),
            settled_issues_7d=sum(
                1 for e, _ in settled if e.occurred_at >= now - metrics.WEEK
            ),
            matched_volume_usdc=f"{Usdc(sum(e.amount.base_units for e, _ in settled)).decimal:.2f}",
            recent=[
                LoopSettlement(
                    issue_id=r.id,
                    repo=r.repo,
                    number=r.number,
                    title=r.title,
                    amount=money(e.amount),
                    contributor=handles.get(e.counterparty_id, e.counterparty_id),
                    settled_at=e.occurred_at,
                    github_url=r.github_url,
                )
                for e, r in settled[:20]
            ],
        )

    def _fabricate_files(self, rec: IssueRecord) -> None:
        """Give a pull request the simulation made up a file list the review agent can
        read: a fix, its test and a changelog line. Only the simulated GitHub has
        pull requests that do not exist."""
        if rec.submission is None or not isinstance(self.github, SimulatedGitHub):
            return
        self.github.put_files(rec.repo, rec.submission.pr_number, _demo_files(rec.repo))

    # ------------------------------------------------- the simulated GitHub's side

    def demo_pull_request(self, issue_id: str) -> PullRequest:
        """Open, on the simulated GitHub, the pull request the claimant would open on
        the real one: a fix, its test, docs and a changelog line, checks passing.
        After rework it is a new commit on the same pull request. Submitting it is
        still the claimant's explicit action."""
        self.ensure_ready()
        if not isinstance(self.github, SimulatedGitHub):
            raise NotSimulated("only the simulated GitHub opens pull requests for you")
        rec = self.repo.get_issue(issue_id)
        if rec is None:
            raise KeyError(issue_id)
        if rec.contributor_id is None or rec.state not in {
            IssueState.CLAIMED,
            IssueState.REWORK,
        }:
            raise lifecycle.IllegalTransition(rec.state, IssueState.IN_REVIEW)
        files = _demo_files(rec.repo)
        pr = PullRequest(
            repo=rec.repo,
            number=rec.submission.pr_number if rec.submission else rec.number + 400,
            author=self._handle(rec.contributor_id),
            head_sha=f"{random.getrandbits(160):040x}",
            body=f"Fixes #{rec.number}",
            merged=False,
            files_changed=len(files),
            additions=sum(f.additions for f in files),
            deletions=sum(f.deletions for f in files),
        )
        self.github.put_pull_request(pr)
        self.github.put_files(rec.repo, pr.number, files)
        self.github.put_checks(rec.repo, pr.head_sha, True)
        return pr

    def demo_merge(self, issue_id: str) -> IssueRecord:
        """Merge the submitted pull request on the simulated GitHub, as the publisher
        would on the real one, and handle it as that merge's webhook is handled."""
        self.ensure_ready()
        if not isinstance(self.github, SimulatedGitHub):
            raise NotSimulated("merge the pull request on GitHub")
        rec = self.repo.get_issue(issue_id)
        if rec is None:
            raise KeyError(issue_id)
        if rec.submission is None:
            raise NotTheSubmission(f"{issue_id} has no pull request to merge")
        number = rec.submission.pr_number
        try:
            opened = self.github.read_pull_request(rec.repo, number)
        except GitHubError:
            opened = None
        if opened is not None:
            self.github.put_pull_request(dataclasses.replace(opened, merged=True))
        return self.pull_request_closed(issue_id, number, merged=True)

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
        """A wallet's first sign-in: it becomes a publisher or a contributor, once."""
        self.ensure_ready()
        address = address.lower()
        if self.repo.get_account(address) is not None:
            raise AccountExists(f"{address} already has a role")
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
                    tier="open",
                    wallet=Wallet(address=address, chain=CHAIN),
                    budget_remaining_usdc=_display(budget),
                )
            )
        else:
            party_id = f"CON-{self.repo.next_value('contributor', 100)}"
            self.repo.save_contributor(
                Contributor(
                    id=party_id,
                    handle=name,
                    wallet=Wallet(address=address, chain=CHAIN),
                    reputation=0,
                    settled_issues=0,
                    earned_usdc="0.00",
                )
            )
        account = Account(
            address=address, role=role, party_id=party_id, created_at=now or _now()  # type: ignore[arg-type]
        )
        self.repo.save_account(account)
        return account

    def link_github(self, address: str, login: str) -> Account:
        """Link the wallet's GitHub account (#80). A contributor's handle is their login."""
        self.ensure_ready()
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

    # ------------------------------------------------------- explicit actions

    def approve_criteria(
        self, issue_id: str, criteria: list[str], by: str, now: datetime | None = None
    ) -> IssueRecord:
        """The publisher edits the drafted acceptance criteria and approves them (#21).
        Until the money is committed they can be revised and approved again."""
        self.ensure_ready()
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

    def approve_price(self, issue_id: str, by: str, now: datetime | None = None) -> IssueRecord:
        """The publisher approves the price, and the money is committed."""
        self.ensure_ready()
        with self._exclusive(issue_id), self._posting():
            rec = self.repo.get_issue(issue_id)
            if rec is None:
                raise KeyError(issue_id)
            if rec.state is not IssueState.AWAITING_APPROVAL:
                raise lifecycle.IllegalTransition(rec.state, IssueState.FUNDED)
            self._approve_and_fund(
                rec, now or _now(), f"{by} approved the price and committed the funds"
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
        self.ensure_ready()
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
        self.ensure_ready()
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
        self.ensure_ready()
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
        self.ensure_ready()
        found = self.repo.get_connection(repo)
        if found is not None and found.publisher_id is None:
            publisher_id = self._publisher_with_login(found.installed_by)
            if publisher_id is not None:
                found.publisher_id = publisher_id
                self.repo.save_connection(found)
        return found

    def connections_of(self, publisher_id: str) -> list[RepoConnection]:
        self.ensure_ready()
        return sorted(
            (
                c
                for c in (self.connection(r.repo) for r in self.repo.list_connections())
                if c is not None and c.publisher_id == publisher_id
            ),
            key=lambda c: c.repo,
        )

    def account_with_login(self, login: str) -> Account | None:
        self.ensure_ready()
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
        """Price an issue again from what GitHub says now, while it is still unfunded.

        A changed issue is a changed job: the new price is a new proposal, and it
        waits for the publisher's approval like the first one did. Once money is
        committed the price is a promise and an edit does not move it.
        """
        self.ensure_ready()
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
            publisher = self.repo.get_publisher(rec.publisher_id)
            assert publisher is not None
            reading = read_signals(facts)
            ceiling, ceiling_source = self._ceiling(publisher)
            proposal = propose(
                reading.signals,
                compliance_driven=rec.compliance_driven,
                comparables=self._comparables_for(reading.signals, rec.repo, exclude=rec.id),
                affordability_ceiling=ceiling,
                ceiling_source=ceiling_source,
                tier=publisher.tier,
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
                action="price_proposed",
                rule="issue_changed",
                outcome=f"the issue changed, so it is re-priced at {proposal.recommended}, "
                f"from {previous}, for the publisher to approve",
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
        request that mentions an issue, and only one of them is owed money.
        """
        self.ensure_ready()
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
            self._log(
                rec,
                actor="contributor",
                action="resubmitted" if resubmitted else "submitted",
                rule="pull_request_synchronized" if resubmitted else "pull_request_opened",
                outcome=f"{claimant} {'pushed rework to' if resubmitted else 'opened'} "
                f"PR #{pr.number}; {history} earlier pull request(s) of theirs merged in "
                f"{rec.repo}",
                when=now,
            )
            self.repo.save_issues(rec)
            return rec

    def record_checks(self, issue_id: str, head_sha: str, now: datetime | None = None) -> bool:
        """Read the project's own checks on the submitted commit. True if that changed
        what the submission records."""
        self.ensure_ready()
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
        self.ensure_ready()
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
        "price proposed",
        extra={
            "issue_id": rec.id,
            "source": source,
            "seconds": round(seconds, 3),
            "price_usdc": f"{rec.proposal.recommended.decimal:.2f}" if rec.proposal else None,
        },
    )


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


def _criteria_comment(rec: IssueRecord) -> str:
    assert rec.proposal is not None and rec.deadline is not None
    criteria = "\n".join(f"- [ ] {c}" for c in rec.acceptance_criteria)
    return (
        f"**Funded on Misthos: {rec.proposal.recommended} USDC**, held in escrow until "
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
        # Paid four days before the seed, so a fresh demo has a settlement this week:
        # the dashboard leads with that number.
        "age_days": 6,
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
        "criteria": [
            "A second config format is added by registering a loader, without editing core.",
            "Existing TOML configs keep working.",
        ],
        "publisher_id": "PUB-5",
        "age_days": 40,
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
