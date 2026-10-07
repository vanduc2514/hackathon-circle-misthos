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
    id: str
    handle: str
    wallet: Wallet
    reputation: int
    settled_issues: int
    earned_usdc: str
    verified: bool


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
    settled_issues: int
    funded_issues_published: int
    claim_rate_72h: float
    acceptance_rate_first_review: float
    repeat_publisher_rate: float
    matched_volume_usdc: str
    median_hours_to_payout: float | None
    dispute_rate: float
    publisher_overturn_rate: float
    open_issues: int
    by_state: dict[str, int]


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


class HealthOut(BaseModel):
    status: str
    service: str
    chain: str
    seeded_issues: int
    simulated: bool
