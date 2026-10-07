"""API schemas.

Money crosses the boundary twice: as a decimal string for display and as integer
base units for precision. Both are always present so a client never has to guess
which view it is looking at.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from misthos.domain.money import Usdc

Tier = Literal["open", "team", "enterprise"]
Actor = Literal["agent", "publisher", "contributor", "system"]


def money(amount: Usdc) -> dict[str, str | int]:
    return {"usdc": f"{amount.decimal:.2f}", "base_units": amount.base_units}


class Wallet(BaseModel):
    address: str
    chain: str = "arc-testnet"


class Publisher(BaseModel):
    id: str
    name: str
    kind: Literal["company", "maintainer"]
    tier: Tier
    wallet: Wallet
    budget_remaining_usdc: str


class Contributor(BaseModel):
    """A contributor as the platform holds them. Never served as is: it carries the
    wallet, and a wallet next to a GitHub handle is exactly the link 08 says we must
    never publish. `ContributorProfile` is the public view."""

    id: str
    handle: str
    wallet: Wallet
    reputation: int
    settled_issues: int
    earned_usdc: str
    identity_status: Literal["unverified", "pending", "verified", "failed"] = "unverified"
    """Verified at first payout, never at signup."""
    identity_reference: str | None = None
    """The identity provider's reference. The documents stay with the provider."""
    identity_verified_at: datetime | None = None

    @property
    def verified(self) -> bool:
        return self.identity_status == "verified"


class ContributorProfile(BaseModel):
    """What anyone may see about a contributor: no wallet, no provider reference."""

    id: str
    handle: str
    reputation: int
    settled_issues: int
    earned_usdc: str
    verified: bool

    @classmethod
    def of(cls, contributor: Contributor) -> ContributorProfile:
        return cls(
            id=contributor.id,
            handle=contributor.handle,
            reputation=contributor.reputation,
            settled_issues=contributor.settled_issues,
            earned_usdc=contributor.earned_usdc,
            verified=contributor.verified,
        )


class Decision(BaseModel):
    id: str
    issue_id: str
    actor: Actor
    action: str
    rule: str
    outcome: str
    cost_usdc: str | None = None
    created_at: datetime


class PriceProposalOut(BaseModel):
    band_low: dict[str, str | int]
    band_high: dict[str, str | int]
    recommended: dict[str, str | int]
    estimated_hours: float
    fundable: bool = True
    """False when the engine will not put a price on this issue. The justification
    says why, and publishing is refused rather than quietly subsidised."""
    complexity_score: float
    confidence: Literal["low", "medium", "high"]
    signals: dict[str, float]
    justification: str
    comparables_note: str


class EscrowCommitment(BaseModel):
    issue_id: str
    contract: str
    chain: str
    tx_hash: str
    amount: dict[str, str | int]
    deadline: datetime
    released: bool = False
    refunded: bool = False


class Claim(BaseModel):
    contributor_id: str
    issued_at: datetime
    expires_at: datetime
    active: bool = True


class Submission(BaseModel):
    pr_number: int
    head_sha: str
    checks_passed: bool
    files_changed: int
    additions: int
    deletions: int


class Review(BaseModel):
    """The platform's verdict on a submission.

    The agent owns this record: there is no separate human verdict, and no draft
    to confirm. The publisher's remaining decision is whether to merge.
    """

    verdict: Literal["accept", "rework", "reject"]
    findings: list[str]
    decided_at: datetime
    head_sha: str | None = None
    """The commit the verdict is about. A push after it needs a new review."""
    reviewer: str | None = None
    """The rule reviewer, or the model that read the diff."""
    seconds: float | None = None
    """How long the review took."""
    cost_usdc: str | None = None
    """What the review cost in inference."""


class IssueOut(BaseModel):
    id: str
    repo: str
    number: int
    title: str
    summary: str
    state: str
    labels: list[str]
    compliance_driven: bool
    acceptance_criteria: list[str]
    publisher_id: str
    publisher_name: str
    created_at: datetime
    deadline: datetime | None = None
    proposal: PriceProposalOut | None = None
    escrow: EscrowCommitment | None = None
    claim: Claim | None = None
    submission: Submission | None = None
    review: Review | None = None
    contributor_id: str | None = None
    paid_usdc: str | None = None
    github_url: str


class IssueSummaryOut(BaseModel):
    id: str
    repo: str
    number: int
    title: str
    state: str
    labels: list[str]
    compliance_driven: bool
    publisher_name: str
    price_usdc: str | None = None
    confidence: str | None = None
    deadline: datetime | None = None
    github_url: str


class MetricsOut(BaseModel):
    """Every number is computed from the ledger and the lifecycle records (09)."""

    settled_issues: int
    """Releases booked in the ledger, all time."""
    settled_issues_7d: int = 0
    """The North Star: releases booked in the last seven days."""
    funded_issues_published: int
    """Commitments booked in the ledger, all time."""
    funded_issues_7d: int = 0
    claim_rate_72h: float
    """Of the issues funded at least 72 hours ago or already claimed, the share
    claimed within 72 hours of the money arriving."""
    acceptance_rate_first_review: float
    """The share of reviewed issues whose first verdict was accept."""
    repeat_publisher_rate: float
    """Publishers who funded a second issue within 60 days of an earlier one."""
    matched_volume_usdc: str
    median_hours_to_payout: float | None
    refund_rate: float = 0.0
    """Of the commitments that closed, the share refunded rather than paid."""
    dispute_rate: float
    publisher_overturn_rate: float
    """Of the issues that passed review, the share whose publisher declined it."""
    earners_over_500_share: float | None = None
    """Of the contributors paid in the last 30 days, the share paid $500 or more."""
    top10_payout_share: float | None = None
    """The ten best-paid contributors' share of everything paid out."""
    open_issues: int
    by_state: dict[str, int]
    reviews_issued: int = 0
    """Verdicts the review agent has issued, every round counted."""
    median_review_seconds: float | None = None
    """How long a verdict takes, on the latest review of each issue."""
    median_review_cost_usdc: str | None = None
    """Inference spent on review per issue, across all its rounds."""


class TimelineEntry(BaseModel):
    id: str
    actor: Actor
    action: str
    outcome: str
    created_at: datetime
    cost_usdc: str | None = None


class PublishRequest(BaseModel):
    repo: str = Field(examples=["acme/ledger"])
    number: int | None = None
    title: str
    summary: str = ""
    labels: list[str] = Field(default_factory=list)
    publisher_id: str
    compliance_driven: bool = False
    signals: dict[str, float] = Field(default_factory=dict)


class DeclineRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)
    """What the work still lacks, in the publisher's words."""


class ReputationEventOut(BaseModel):
    """One settled issue's contribution to a contributor's standing."""

    issue_id: str
    repo: str
    amount: dict[str, str | int]
    points: int
    settled_at: datetime


class LoopSettlement(BaseModel):
    issue_id: str
    repo: str
    number: int
    title: str
    amount: dict[str, str | int]
    contributor: str
    """The contributor's GitHub handle, which the settlement comment already shows."""
    settled_at: datetime
    github_url: str


class LoopOut(BaseModel):
    """The loop in public: what was funded, settled and paid, for one repository or
    all of them. No wallet and no transfer appears here."""

    repo: str | None
    funded_open: int
    settled_issues: int
    settled_issues_7d: int
    matched_volume_usdc: str
    recent: list[LoopSettlement]


class DisputeRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)
    """Which criteria the contributor believes are met, and where the work shows it."""


class HealthOut(BaseModel):
    status: str
    service: str
    chain: str
    seeded_issues: int
    simulated: bool


class StatementLine(BaseModel):
    paid_at: datetime
    issue_id: str
    repo: str
    issue_number: int
    issue_title: str
    counterparty_id: str
    """The publisher who funded the work."""
    counterparty_name: str
    amount: dict[str, str | int]
    currency: Literal["USDC"] = "USDC"
    chain: str
    tx_hash: str | None


class AnnualStatement(BaseModel):
    """Every payout one contributor received in one calendar year, in UTC.

    Built for a finance team to file from without asking anything: each line carries
    its date, issue, counterparty, amount and settlement reference.
    """

    contributor_id: str
    handle: str
    year: int
    identity_status: str
    lines: list[StatementLine]
    total: dict[str, str | int]
    payouts: int
    generated_at: datetime
    simulated: bool
    """True when no chain was contacted. A simulated statement is not a tax record."""
