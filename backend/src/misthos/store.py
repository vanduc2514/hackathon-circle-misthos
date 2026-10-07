"""In-memory store with seeded demo data.

This is a simulation, not persistence. It exists so the web application has
something real to render: a lifecycle that can actually be advanced, prices that
were actually computed by the pricing engine, and a decision log that grows as
you drive it. Swap this for Postgres plus repositories when the schema settles.
"""

from __future__ import annotations

import itertools
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from misthos.domain import issue as lifecycle
from misthos.domain.issue import IssueState
from misthos.domain.money import Usdc
from misthos.domain.pricing import (
    ComplexitySignals,
    PriceProposal,
    UnfundableIssue,
    platform_fee,
    propose,
    take_rate_bps,
)
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

ESCROW_CONTRACT = "0x7A3f19bE5c2D80416aB9e0C7d3F5a12B6c8E4d90"
CHAIN = "arc-testnet"

REPO_POOL = [
    "acme/ledger-core",
    "northwind/httpx-retry",
    "contoso/schema-cli",
    "acme/ledger-adapters",
    "globex/parse-locale",
]

_ISSUE_SEQ = itertools.count(1001)
_DECISION_SEQ = itertools.count(1)
_TX_SEQ = itertools.count(0x4A1)


def _tx_hash() -> str:
    return f"0x{next(_TX_SEQ):08x}{random.getrandbits(180):045x}"


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class IssueRecord:
    id: str
    repo: str
    number: int
    title: str
    summary: str
    state: IssueState
    labels: list[str]
    compliance_driven: bool
    acceptance_criteria: list[str]
    publisher_id: str
    created_at: datetime
    deadline: datetime | None = None
    proposal: PriceProposal | None = None
    escrow: EscrowCommitment | None = None
    claim: Claim | None = None
    submission: Submission | None = None
    review: Review | None = None
    contributor_id: str | None = None
    paid: Usdc | None = None
    platform_fee: Usdc | None = None
    decisions: list[Decision] = field(default_factory=list)

    @property
    def github_url(self) -> str:
        return f"https://github.com/{self.repo}/issues/{self.number}"


class Store:
    def __init__(self) -> None:
        self.publishers: dict[str, Publisher] = {}
        self.contributors: dict[str, Contributor] = {}
        self.issues: dict[str, IssueRecord] = {}
        self.reset()

    # ---------------------------------------------------------------- seeding

    def reset(self) -> None:
        global _ISSUE_SEQ, _DECISION_SEQ, _TX_SEQ
        _ISSUE_SEQ = itertools.count(1001)
        _DECISION_SEQ = itertools.count(1)
        _TX_SEQ = itertools.count(0x4A1)
        random.seed(7)
        self.publishers = _seed_publishers()
        self.contributors = _seed_contributors()
        self.issues = {}
        for spec in _ISSUE_SPECS:
            rec = self._build(spec)
            self.issues[rec.id] = rec

    def _build(self, spec: dict) -> IssueRecord:
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
            tier=self.publishers[spec["publisher_id"]].tier,
        )
        now = _now()
        rec = IssueRecord(
            id=f"ISS-{next(_ISSUE_SEQ)}",
            repo=spec["repo"],
            number=spec["number"],
            title=spec["title"],
            summary=spec["summary"],
            state=IssueState(spec["state"]),
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

        if spec["state"] in {
            IssueState.AWAITING_APPROVAL,
            IssueState.DRAFT,
            IssueState.PRICED,
        }:
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

        if spec["state"] == IssueState.FUNDED:
            return rec

        if spec["state"] == IssueState.REFUNDED:
            rec.escrow.refunded = True  # type: ignore[union-attr]
            rec.state = IssueState.REFUNDED
            self._log(
                rec,
                actor="system",
                action="refunded",
                rule="deadline_passed",
                outcome="no acceptable work arrived, funds returned",
            )
            return rec

        # Claim onward.
        contributor_id = spec["contributor_id"]
        self._claim(rec, contributor_id, when=rec.created_at + timedelta(hours=20))
        self._log(
            rec,
            actor="contributor",
            action="claimed",
            rule="first_claim_wins",
            outcome=f"{self.contributors[contributor_id].handle} took an exclusive 72h claim",
        )

        if spec["state"] == IssueState.CLAIMED:
            return rec

        self._submit(rec, spec["pr"], when=rec.created_at + timedelta(hours=40))

        if spec["state"] == IssueState.IN_REVIEW:
            return rec

        verdict = spec["verdict"]
        findings = spec["findings"]
        self._review(
            rec,
            verdict=verdict,
            findings=findings,
            when=rec.created_at + timedelta(hours=44),
        )

        if spec["state"] == IssueState.REWORK:
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

        if spec["state"] == IssueState.ACCEPTED:
            return rec

        self._release(rec, when=rec.created_at + timedelta(hours=45, minutes=2))
        return rec

    # ------------------------------------------------------------- primitives

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
            id=f"DEC-{next(_DECISION_SEQ):04d}",
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

    def _fund(self, rec: IssueRecord, when: datetime | None = None) -> None:
        assert rec.proposal is not None
        committed = rec.proposal.recommended
        rec.escrow = EscrowCommitment(
            issue_id=rec.id,
            contract=ESCROW_CONTRACT,
            chain=CHAIN,
            tx_hash=_tx_hash(),
            amount=money(committed),
            deadline=(when or _now()) + timedelta(days=14),
        )
        rec.deadline = rec.escrow.deadline
        rec.state = IssueState.FUNDED

    def _claim(self, rec: IssueRecord, contributor_id: str, when: datetime | None = None) -> None:
        at = when or _now()
        rec.claim = Claim(contributor_id=contributor_id, issued_at=at, expires_at=at + timedelta(days=3))
        rec.contributor_id = contributor_id
        rec.state = IssueState.CLAIMED

    def _submit(self, rec: IssueRecord, pr: dict, when: datetime | None = None) -> None:
        rec.submission = Submission(**pr)
        rec.state = IssueState.IN_REVIEW

    def _review(
        self,
        rec: IssueRecord,
        *,
        verdict: str,
        findings: list[str],
        when: datetime | None = None,
    ) -> None:
        """Record the platform's verdict. It is the decision, not a draft for one."""
        rec.review = Review(
            verdict=verdict,  # type: ignore[arg-type]
            findings=findings,
            decided_at=when or _now(),
        )
        rec.state = {
            "accept": IssueState.ACCEPTED,
            "rework": IssueState.REWORK,
            "reject": IssueState.REJECTED,
        }[verdict]

    def _release(
        self,
        rec: IssueRecord,
        *,
        rule: str = "escrow_acceptance_attestation",
        when: datetime | None = None,
    ) -> None:
        assert rec.escrow is not None and rec.proposal is not None
        gross = rec.proposal.recommended
        tier = self.publishers[rec.publisher_id].tier
        # The publisher paid the fix price and nothing else, so the take rate comes
        # out of the commitment. The basis points are what the escrow would enforce
        # for this issue, and the division floors the same way.
        fee = platform_fee(gross, tier)
        rec.escrow.released = True
        rec.platform_fee = fee
        rec.paid = gross - fee
        rec.state = IssueState.PAID
        self._log(
            rec,
            actor="system",
            action="released",
            rule=rule,
            outcome=(
                f"released {rec.paid} to contributor and {fee} platform fee "
                f"at {take_rate_bps(tier)} bps"
            ),
            cost="0.01",
            when=when,
        )

    # ---------------------------------------------------------------- actions

    def get(self, issue_id: str) -> IssueRecord | None:
        return self.issues.get(issue_id)

    def advance(self, issue_id: str) -> IssueRecord:
        """Move an issue one step forward along the demo path."""
        rec = self.issues[issue_id]
        if rec.state in lifecycle.TERMINAL_STATES:
            raise lifecycle.IllegalTransition(rec.state, IssueState.PAID)
        match rec.state:
            case IssueState.AWAITING_APPROVAL:
                self._fund(rec)
                self._log(
                    rec,
                    actor="publisher",
                    action="price_approved",
                    rule="human_checkpoint",
                    outcome="approved the price and committed funds",
                )
            case IssueState.FUNDED if rec.deadline is not None and rec.deadline < _now():
                rec.escrow.refunded = True  # type: ignore[union-attr]
                rec.state = IssueState.REFUNDED
                self._log(
                    rec,
                    actor="system",
                    action="refunded",
                    rule="deadline_passed",
                    outcome="no acceptable work arrived, funds returned",
                )
            case IssueState.FUNDED:
                available = [c for c in self.contributors if c not in {rec.contributor_id}]
                cid = available[0] if available else next(iter(self.contributors))
                self._claim(rec, cid)
                self._log(
                    rec,
                    actor="contributor",
                    action="claimed",
                    rule="first_claim_wins",
                    outcome=f"{self.contributors[cid].handle} claimed the issue",
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
            case IssueState.ACCEPTED if (
                rec.review is not None
                and _now() > rec.review.decided_at + lifecycle.SILENT_PUBLISHER_GRACE
            ):
                # The verdict passed and the publisher went quiet past the grace
                # window. Release rather than strand finished work.
                self._release(rec, rule="silent_publisher_grace_period")
            case IssueState.ACCEPTED:
                self._release(rec)
                self._log(
                    rec,
                    actor="publisher",
                    action="merged",
                    rule="merge_is_acceptance",
                    outcome="publisher merged the pull request, which is acceptance",
                )
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
        return rec

    def approve_and_accept(self, issue_id: str) -> IssueRecord:
        """Run the remainder of the happy path in one call, for demos."""
        rec = self.issues[issue_id]
        for _ in range(12):
            if rec.state in lifecycle.TERMINAL_STATES:
                break
            self.advance(issue_id)
        return rec

    def publish(self, payload) -> IssueRecord:
        publisher = self.publishers[payload.publisher_id]
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
            "affordability_ceiling": Usdc.from_decimal(
                publisher.budget_remaining_usdc.replace(",", "")
            ),
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
        rec.state = IssueState.AWAITING_APPROVAL
        self._log(
            rec,
            actor="system",
            action="published",
            rule="publisher_created_issue",
            outcome="issue published and price proposed",
        )
        self.issues[rec.id] = rec
        return rec

    # ------------------------------------------------------------ projections

    def to_out(self, rec: IssueRecord) -> IssueOut:
        publisher = self.publishers[rec.publisher_id]
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
            platform_fee_usdc=str(rec.platform_fee.decimal) if rec.platform_fee else None,
            github_url=rec.github_url,
        )

    def summary(self, rec: IssueRecord) -> IssueSummaryOut:
        return IssueSummaryOut(
            id=rec.id,
            repo=rec.repo,
            number=rec.number,
            title=rec.title,
            state=rec.state.value,
            labels=rec.labels,
            compliance_driven=rec.compliance_driven,
            publisher_name=self.publishers[rec.publisher_id].name,
            price_usdc=f"{rec.proposal.recommended.decimal:.2f}" if rec.proposal else None,
            confidence=rec.proposal.confidence if rec.proposal else None,
            deadline=rec.deadline,
            github_url=rec.github_url,
        )

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
        records = list(self.issues.values())
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

        # Matched volume is what the publishers committed. The take rate is a slice
        # of it rather than an addition to it, so the revenue line is separate.
        matched = sum(
            (
                r.paid.base_units + (r.platform_fee.base_units if r.platform_fee else 0)
                for r in settled
                if r.paid
            ),
            start=0,
        )
        fee_revenue = sum(
            (r.platform_fee.base_units for r in settled if r.platform_fee), start=0
        )

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
            platform_fees_usdc=f"{Usdc(fee_revenue).decimal:.2f}",
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
            verified=r[5],
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
        "age_days": 5,
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


store = Store()
