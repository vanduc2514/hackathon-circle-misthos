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
    chain: str = Field(description="The network the address was recorded on, from its chain id")
    circle_user_id: str | None = Field(
        default=None, description="Set once the address was read from the party's Circle wallet"
    )


class WalletSessionOut(BaseModel):
    """What the browser needs to run Circle's PIN challenge for its own wallet."""

    party: Literal["publisher", "contributor"]
    party_id: str
    circle_user_id: str
    app_id: str
    user_token: str
    encryption_key: str
    challenge_id: str | None = Field(description="None when the wallet already exists")
    wallet: Wallet | None
    simulated: bool


class Publisher(BaseModel):
    id: str
    name: str
    kind: Literal["company", "maintainer"]
    tier: Tier
    wallet: Wallet
    budget_remaining_usdc: str
    # The organisation's own spending policy (domain/policy.py).
    approval_threshold_usdc: str | None = None
    """Releases above this wait for one of the approvers."""
    approvers: list[str] = Field(default_factory=list)
    category_limits: dict[str, str] = Field(default_factory=dict)
    """Issue label to the most that may be committed under it in a calendar month."""


class PublisherListing(BaseModel):
    """A publisher as anyone may see it. Its budget and spending policy are
    commercially sensitive, so outside the simulation they are filled in only for
    the signed-in publisher itself ([PRIVACY.md](../../docs/PRIVACY.md))."""

    id: str
    name: str
    kind: Literal["company", "maintainer"]
    tier: Tier
    wallet: Wallet
    budget_remaining_usdc: str | None = None
    approval_threshold_usdc: str | None = None
    approvers: list[str] = Field(default_factory=list)
    category_limits: dict[str, str] = Field(default_factory=dict)


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


class ComparableOut(BaseModel):
    """A settled issue of similar shape the price was compared with (#42)."""

    issue_id: str
    repo: str
    title: str
    price_usdc: str
    settled_at: datetime


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
    comparables: list[ComparableOut] = Field(default_factory=list)
    """Closest first. Confidence follows these, and nothing else."""


class EscrowCommitment(BaseModel):
    issue_id: str
    contract: str
    chain: str
    tx_hash: str
    amount: dict[str, str | int]
    deadline: datetime
    fee_bps: int = 0
    """The platform's take rate for this issue, in basis points, fixed when the money
    was committed. The escrow enforces this rate, not the publisher's tier at the
    moment of release, so a plan that lapses mid-flight cannot move it."""
    released: bool = False
    refunded: bool = False


class WalletCall(BaseModel):
    """One transaction for the user's own wallet to send. The platform does not sign it."""

    label: str
    to: str
    data: str


class CommitmentPlan(BaseModel):
    """How a publisher funds an issue from their own wallet, in the order to send."""

    issue_id: str
    chain_id: int
    escrow: str
    usdc: str
    amount: dict[str, str | int]
    deadline: int = Field(
        description="Unix seconds. Commit within the hour: the escrow term starts at approval"
    )
    calls: list[WalletCall]


class EscrowReadback(BaseModel):
    """A commitment as the escrow reports it, not as the platform remembers it."""

    issue_id: str
    issue_key: str = Field(description="bytes32 the contract stores the issue under")
    contract: str
    chain_id: int
    source: Literal["chain", "simulation"] = Field(
        description="chain: read from MisthosEscrow; simulation: the simulated escrow's books"
    )
    status: Literal["none", "held", "released", "refunded"]
    publisher: str | None
    amount: dict[str, str | int]
    escrow_ceiling: dict[str, str | int] | None = Field(
        description=(
            "The most the escrow will take for this issue: the price a human approved, "
            "as the escrow holds it. None means the issue cannot be funded. Not the "
            "affordability ceiling, which is the platform's estimate from the budget."
        )
    )
    deadline: datetime | None
    explorer_url: str


class Claim(BaseModel):
    contributor_id: str
    issued_at: datetime
    expires_at: datetime
    active: bool = True


class Submission(BaseModel):
    pr_number: int
    head_sha: str
    checks_passed: bool | None
    """None until the project's checks report on this commit, and for good in a
    repository with no CI. Not reported is not failing."""
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
    """What the contributor received: the commitment less the platform's take rate."""
    platform_fee_usdc: str | None = None
    """The platform's commission on this settlement, at the publisher's tier rate."""
    github_url: str
    criteria_approved_at: datetime | None = None
    """When the publisher approved the acceptance criteria; funding waits for it (#21)."""
    awaiting_approver: bool = False
    """The payout is held over the organisation's release threshold until one of its
    named approvers approves. Compliance holds are not shown here."""


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
    platform_fees_usdc: str
    """The take-rate revenue actually collected across settled issues."""
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


Role = Literal["publisher", "contributor"]


class Account(BaseModel):
    """A signed-in wallet and the one role it chose (#70)."""

    address: str
    """Lowercase. The wallet is the identity that matches the chain."""
    role: Role
    party_id: str
    """The Publisher or Contributor this wallet acts as."""
    github_login: str | None = None
    """Linked through GitHub OAuth (#80). Required before pricing or contributing."""
    created_at: datetime


class NonceOut(BaseModel):
    nonce: str
    domain: str
    uri: str
    chain_id: int
    statement: str


class SignInRequest(BaseModel):
    message: str = Field(max_length=4000)
    signature: str = Field(max_length=200)


class SessionOut(BaseModel):
    address: str
    token: str
    account: Account | None


class MeOut(BaseModel):
    address: str
    account: Account | None


class RoleRequest(BaseModel):
    role: Role
    name: str = Field(min_length=1, max_length=200)
    """The organisation's name for a publisher; a working handle for a contributor
    until their GitHub account is linked."""
    budget_usdc: str = "5000"
    """A publisher's declared budget, which caps every price it is offered."""


class GitHubLinkStart(BaseModel):
    authorize_url: str


class AlreadyPublished(BaseModel):
    """The answer to publishing a GitHub issue that already has an open listing (#120)."""

    detail: str
    issue_id: str
    """The open listing of that GitHub issue."""


class SimulatedLink(BaseModel):
    login: str = Field(min_length=1, max_length=39, pattern=r"^[A-Za-z0-9-]+$")


class CriteriaRequest(BaseModel):
    criteria: list[str] = Field(min_length=1, max_length=12)


class ClaimRequest(BaseModel):
    contributor_id: str | None = None
    """The simulation's only: whom the demo claims as. A signed-in contributor claims
    as themselves."""


class SubmitRequest(BaseModel):
    pr_number: int = Field(gt=0)


class FinanceOut(BaseModel):
    """A publisher's money as the pricing engine sees it, for that publisher alone (#43)."""

    publisher_id: str
    declared_budget_usdc: str
    connected: bool
    """Whether the operator connected this publisher's books."""
    source: str | None = None
    budget_remaining_usdc: str | None = None
    """What the books say remains of the budget this work comes out of."""
    cash_usdc: str | None = None
    as_of: datetime | None = None
    caps_prices_at_usdc: str
    """The lower of the declared budget and what the books say remains."""
    note: str = ""


class DemoPullRequestOut(BaseModel):
    """A pull request the simulated GitHub opened for the claimant (simulation only)."""

    pr_number: int
    author: str
    head_sha: str


class PolicyRequest(BaseModel):
    approval_threshold_usdc: str | None = None
    approvers: list[str] = Field(default_factory=list, max_length=20)
    """GitHub logins. A release over the threshold waits for one of them to approve
    it, signed in with an account that login is linked to."""
    category_limits: dict[str, str] = Field(default_factory=dict)


class ApproveReleaseRequest(BaseModel):
    approver: str | None = Field(default=None, min_length=1, max_length=200)
    """The simulation's only, for a visitor who is not signed in: the approver the demo
    acts as. Signed in, the approver is the account's linked GitHub login, and this is
    ignored."""


class SpendCategory(BaseModel):
    label: str
    committed: dict[str, str | int]
    """Committed during the year."""
    released: dict[str, str | int]
    """Released during the year."""
    committed_this_month: dict[str, str | int]
    """Committed since this calendar month began: what the monthly limit is checked
    against when the next commitment is funded."""
    limit: dict[str, str | int] | None = None
    """The most that may be committed under the label in one calendar month."""


class FileableItem(BaseModel):
    """One settled fix, with what a security review needs to file it."""

    issue_id: str
    repo: str
    number: int
    title: str
    labels: list[str]
    compliance_driven: bool
    acceptance_criteria: list[str]
    amount: dict[str, str | int]
    settled_at: datetime
    github_url: str


class SpendOut(BaseModel):
    """What an organisation budgeted, committed, released and can file (#50)."""

    publisher_id: str
    name: str
    year: int
    month: str
    """The calendar month the `committed_this_month` figures count, as YYYY-MM (UTC)."""
    budget_remaining: dict[str, str | int]
    committed_held: dict[str, str | int]
    """In escrow now, across every open issue."""
    committed: dict[str, str | int]
    """Committed during the year."""
    released: dict[str, str | int]
    refunded: dict[str, str | int]
    by_category: list[SpendCategory]
    fileable: list[FileableItem]
    """Settled fixes that were compliance-driven or security-labelled."""


class AuditMoneyEvent(BaseModel):
    occurred_at: datetime
    kind: str
    amount: dict[str, str | int]
    counterparty_id: str
    tx_hash: str | None
    """The organisation's own transfers only. A release's reference would link the
    contributor's wallet to their handle, so it stays on their statement (PRIVACY.md)."""


class AuditIssue(BaseModel):
    id: str
    repo: str
    number: int
    title: str
    state: str
    labels: list[str]
    compliance_driven: bool
    acceptance_criteria: list[str]
    created_at: datetime
    price: dict[str, str | int] | None
    decisions: list[Decision]
    money_events: list[AuditMoneyEvent]


class AuditExport(BaseModel):
    """Everything an auditor needs about one organisation's issues, from the decision
    log and the money ledger, unedited (#52)."""

    publisher_id: str
    name: str
    generated_at: datetime
    simulated: bool
    issues: list[AuditIssue]


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
    chain: str = Field(description="Derived from chain_id: arc-mainnet, arc-testnet or chain-<id>")
    chain_id: int
    network_label: str
    money: Literal["simulated", "test", "real", "unknown"] = Field(
        description=(
            "simulated: nothing moves; test: Arc testnet faucet USDC; real: Arc mainnet; "
            "unknown: not an Arc network, so treat its USDC as real"
        )
    )
    money_note: str
    seeded_issues: int
    simulated: bool
    github_oauth: bool = Field(
        description=(
            "Whether a real GitHub account can be linked here: both "
            "MISTHOS_GITHUB_OAUTH_CLIENT_ID and MISTHOS_GITHUB_OAUTH_CLIENT_SECRET are set. "
            "Without it, the simulation links a typed login and a deployment links none."
        )
    )
    github_oauth_callback_url: str = Field(
        description="The callback URL to register on the GitHub OAuth App, from MISTHOS_PUBLIC_URL"
    )


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


# ------------------------------------------------------------------ plans (#53)


class PlanOut(BaseModel):
    id: Tier
    name: str
    audience: str
    monthly_usdc: str
    price_from: bool
    """True when the price is where a negotiated one starts (Enterprise)."""
    self_serve: bool
    """Bought from the web app without a conversation."""
    take_rate_percent: float
    minimum_fix_usdc: str
    features: list[str]
    support: str


class PaymentRequest(BaseModel):
    """What to send to start or renew a plan: USDC on Arc, from the publisher's wallet."""

    plan: Tier
    amount_usdc: str
    pay_to: str
    payer: str
    chain: str
    chain_id: int
    usdc_address: str
    expires_at: datetime


class Subscription(BaseModel):
    publisher_id: str
    plan: Tier
    status: Literal["pending", "active", "past_due", "lapsed", "cancelled"]
    """`pending` is chosen and not yet paid for."""
    period_start: datetime | None = None
    period_end: datetime | None = None
    cancel_at_period_end: bool = False
    pending: PaymentRequest | None = None
    updated_at: datetime


class SubscriptionPayment(BaseModel):
    publisher_id: str
    plan: Tier
    amount_usdc: str
    tx_hash: str
    period_start: datetime
    period_end: datetime
    paid_at: datetime


class SubscriptionOut(BaseModel):
    publisher_id: str
    plan: Tier
    """The plan in force now, which is the publisher's tier."""
    status: Literal["none", "pending", "active", "past_due", "lapsed", "cancelled", "contract"]
    """`none` is Open with nothing bought; `contract` is a plan agreed with us."""
    period_end: datetime | None = None
    grace_ends: datetime | None = None
    cancel_at_period_end: bool = False
    pending: PaymentRequest | None = None
    payments: list[SubscriptionPayment] = Field(default_factory=list)
    features: list[str] = Field(default_factory=list)


class SubscribeRequest(BaseModel):
    plan: Tier


class PaymentConfirmation(BaseModel):
    tx_hash: str = Field(pattern=r"^0x[0-9a-fA-F]{64}$")


# ------------------------------------------------------- GitHub connections (#6)


class RepoConnection(BaseModel):
    """A repository the GitHub App is installed on, and the publisher it belongs to."""

    repo: str
    """owner/name, lowercase."""
    installation_id: int
    installed_by: str
    """The GitHub login that installed the App: the publisher, once that login is linked."""
    publisher_id: str | None = None
    connected_at: datetime


class RepositoriesOut(BaseModel):
    """A publisher's connected repositories, and how to connect more."""

    repositories: list[RepoConnection]
    label: str
    """The label that has an issue priced."""
    install_url: str | None = None
    """Where to install the App on another repository, when its slug is configured."""
