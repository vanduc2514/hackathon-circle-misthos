"""API schemas.

Money crosses the boundary twice: as a decimal string for display and as integer
base units for precision. Both are always present so a client never has to guess
which view it is looking at.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from misthos.domain.money import Usdc
from misthos.domain.repo import parse_repo_link

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


class Publisher(BaseModel):
    id: str
    name: str
    kind: Literal["company", "maintainer"]
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
    commercially sensitive, so they are filled in only for
    the signed-in publisher itself ([PRIVACY.md](../../docs/PRIVACY.md))."""

    id: str
    name: str
    kind: Literal["company", "maintainer"]
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
    was committed. The escrow enforces this rate, so nothing after the commitment
    can move it."""
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
    source: Literal["chain"] = Field(description="read from MisthosEscrow")
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
    price_usdc: str | None = None
    """What the publisher said the work is worth, and what the escrow commits (#142).
    Null on an issue published before #142, whose amount came from the recommendation."""
    proposal: PriceProposalOut | None = None
    escrow: EscrowCommitment | None = None
    claim: Claim | None = None
    submission: Submission | None = None
    review: Review | None = None
    contributor_id: str | None = None
    paid_usdc: str | None = None
    """What the contributor received: the commitment less the platform's fee."""
    platform_fee_usdc: str | None = None
    """The platform's commission on this settlement: 10 percent of the commitment."""
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
    repo: str = Field(examples=["acme/ledger", "https://github.com/acme/ledger/issues/12"])
    """`owner/name`, or a link to the repository or the issue, which is read for both."""
    number: int | None = None
    title: str
    summary: str = ""
    labels: list[str] = Field(default_factory=list)
    publisher_id: str
    price: Decimal | None = None
    """USDC, as the publisher types it. Their number, and the amount the escrow
    commits: the engine's suggestion is advice beside it, never a bound (#142).

    Optional because the GitHub flow has no form: an issue listed by a `/misthos`
    label arrives unpriced, the publisher names the price with `/misthos price`, and
    funding is refused until they do. The web form always sends one."""
    compliance_driven: bool = False
    signals: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _name_the_repository(self) -> PublishRequest:
        """Read a pasted GitHub link as the `owner/name` the rest of the app matches.

        Connections, webhooks and the loop all compare the `owner/name` GitHub
        reports, so a link has to arrive as that or nothing would ever match it. A
        link also carries the issue number, which stands in when none was typed.

        Anything that is not a link is left exactly as the publisher wrote it. The
        repository is their own words, and the CSV export is what makes those safe to
        open in a spreadsheet — not this field (see
        `tests/integration/test_compliance_at_payout.py`).
        """
        repo, number = parse_repo_link(self.repo)
        if repo:
            self.repo = repo
            if self.number is None and number is not None:
                self.number = number
        return self


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
    """Who is signed in, and how far through the sign-in gate they are (#181).

    The gate's three steps read off this: signed in at all, a GitHub login (linked on
    the account, or held while there is none), and an account, which is the side.
    """

    address: str
    account: Account | None
    pending_github_login: str | None = Field(
        default=None,
        description=(
            "A GitHub login linked before the side was chosen. The account adopts it when "
            "it is created, and from then on it is the account's github_login and this is "
            "null."
        ),
    )


class RoleRequest(BaseModel):
    role: Role
    name: str = Field(min_length=1, max_length=200)
    """The organisation's name for a publisher. A contributor's handle is their GitHub
    login once one is linked, which the sign-in gate does before this step."""
    budget_usdc: str = "5000"
    """A publisher's declared budget, which caps every price it is offered."""


class CircleSignIn(BaseModel):
    """Which Circle sign-in this browser is continuing (#181).

    The id is the browser's own, random, and names the Circle user behind its wallet. It
    is not a credential: Circle signs nothing for it without the PIN its owner set in
    Circle's own frame.
    """

    sign_in_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,64}$")


class CircleSignRequest(CircleSignIn):
    message: str = Field(max_length=4000)
    """The sign-in message `POST /auth/nonce` described, naming this wallet's address."""


class CircleSignInSession(BaseModel):
    """What Circle's frame needs to continue a sign-in with a Circle wallet."""

    app_id: str
    user_token: str
    encryption_key: str
    challenge_id: str | None
    """Set when the wallet does not exist yet: the PIN and wallet challenge to run."""
    address: str | None
    """The wallet's address, once it exists. This is the address the sign-in message
    names and the address `POST /auth/verify` recovers."""


class CircleSignInWallet(BaseModel):
    """A Circle sign-in wallet as it stands: its address once Circle has created it."""

    address: str | None


class CircleSignature(BaseModel):
    """The answer to signing a sign-in message with a Circle wallet.

    Circle signs once its owner approves with their PIN, so this carries the challenge
    for the frame to run and the signature comes back from the frame.
    """

    app_id: str
    user_token: str
    encryption_key: str
    challenge_id: str | None
    signature: str | None


class FundRequest(BaseModel):
    """Approving the price the publisher already set.

    Empty on purpose. An amount here would be a second place to change the number
    after it was published and audited, so the price is set at publish time and moved
    with `PATCH /issues/{issue_id}/price` until someone claims the issue (#142).
    """


class PriceRequest(BaseModel):
    """A new price for an issue nobody has claimed yet."""

    amount: str
    """USDC, as decimal text. Checked against the platform's bounds and nothing else."""


class GitHubLinkStart(BaseModel):
    authorize_url: str


class AlreadyPublished(BaseModel):
    """The answer to publishing a GitHub issue that already has an open listing (#120)."""

    detail: str
    issue_id: str
    """The open listing of that GitHub issue."""


class CriteriaRequest(BaseModel):
    criteria: list[str] = Field(min_length=1, max_length=12)


class ClaimRequest(BaseModel):
    """Empty: a signed-in contributor claims as themselves."""


class SubmitRequest(BaseModel):
    pr_number: int = Field(gt=0)


class PolicyRequest(BaseModel):
    approval_threshold_usdc: str | None = None
    approvers: list[str] = Field(default_factory=list, max_length=20)
    """GitHub logins. A release over the threshold waits for one of them to approve
    it, signed in with an account that login is linked to."""
    category_limits: dict[str, str] = Field(default_factory=dict)


class ApproveReleaseRequest(BaseModel):
    """Empty: the approver is the signed-in account's linked GitHub login."""


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


class DisputeRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)
    """Which criteria the contributor believes are met, and where the work shows it."""


class HealthOut(BaseModel):
    status: str
    service: str
    chain: str = Field(description="Derived from chain_id: arc-mainnet, arc-testnet or chain-<id>")
    chain_id: int
    network_label: str
    money: Literal["test", "real", "unknown"] = Field(
        description=(
            "test: Arc testnet faucet USDC; real: Arc mainnet; "
            "unknown: not an Arc network, so treat its USDC as real"
        )
    )
    money_note: str
    github_oauth: bool = Field(
        description=(
            "Whether a real GitHub account can be linked here: both "
            "MISTHOS_GITHUB_OAUTH_CLIENT_ID and MISTHOS_GITHUB_OAUTH_CLIENT_SECRET are set."
        )
    )
    github_oauth_callback_url: str = Field(
        description="The callback URL to register on the GitHub OAuth App, from MISTHOS_PUBLIC_URL"
    )
    circle_sign_in: bool = Field(
        default=False,
        description=(
            "Whether a Circle wallet can be the way in (#181): the edge service is "
            "configured, which MISTHOS_EDGE_CORE_TOKEN being set stands for."
        ),
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


class GitHubIssueOut(BaseModel):
    """One open issue in a connected repository, for the publisher's picker (#199)."""

    number: int
    title: str
    labels: list[str]
    html_url: str
    listed_issue_id: str | None = None
    """The Misthos listing that already prices this issue, if one is open."""


class GitHubIssuesOut(BaseModel):
    issues: list[GitHubIssueOut]


class RepositoriesOut(BaseModel):
    """A publisher's connected repositories, and how to connect more."""

    repositories: list[RepoConnection]
    label: str
    """The label that has an issue priced."""
    install_url: str | None = None
    """Where to install the App on another repository, when its slug is configured."""
    app_name: str | None = None
    """The App's name, for labelling the link that manages it."""
    settings_url: str | None = None
    """Where to manage the installation that already covers a repository.

    GitHub's per-installation page is the only place a repository is added to or removed
    from an installation, so once something is connected this is what to offer instead
    of installing again. None when nothing is connected yet."""


# ------------------------------------------------------------- paid API (#200)
# What the edge and the core say to each other about x402 calls. None of this is
# served to a buyer as is: the edge answers buyers, and these routes answer the edge.


class PaidAccessAsked(BaseModel):
    """Is this caller free on this paid route? Asked by the edge for a free-tier route."""

    route: str = Field(max_length=64)
    params: dict[str, str] = Field(default_factory=dict)
    query: dict[str, str | list[str]] = Field(default_factory=dict)


class PaidAccess(BaseModel):
    free: bool
    who: str | None = Field(
        default=None,
        description="The free caller as `role:party_id`, passed back on the call it makes",
    )


class PaidCallIn(BaseModel):
    """One settlement, as the edge reports it once the call it paid for was answered."""

    route: str = Field(min_length=1, max_length=64)
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    payer: str = Field(pattern=r"^0x[0-9a-fA-F]{40}$")
    amount_base_units: int = Field(gt=0)
    network: str = Field(pattern=r"^eip155:\d+$")
    asset: str = Field(pattern=r"^0x[0-9a-fA-F]{40}$")
    transaction: str = Field(min_length=1, max_length=128)
    ref: str | None = Field(default=None, max_length=64)


class PaidCallOut(BaseModel):
    id: str
    route: str
    request_digest: str
    payer: str
    amount: dict[str, str | int]
    network: str
    asset: str
    transaction: str
    ref: str | None
    settled_at: datetime


class PaidRouteTotal(BaseModel):
    route: str
    network: str
    calls: int
    amount: dict[str, str | int]


class PaidCallsOut(BaseModel):
    """The books of paid calls, to reconcile with Circle Gateway's balance for the seller."""

    calls: list[PaidCallOut]
    totals: list[PaidRouteTotal]
    total: dict[str, str | int]


# ------------------------------------------------------- the API page (#208)


class PaidRouteOut(BaseModel):
    """One x402 route the edge sells, as its catalog lists it."""

    id: str
    method: Literal["GET", "POST"]
    path: str
    price: dict[str, str]
    """`usdc` and `base_units`, as the edge charges them."""
    name: str
    summary: str
    group: Literal["Core", "Marketplace", "Analytics", "Trust"]
    returns: str | None = None
    free_tier: bool = False
    is_async: bool = Field(default=False, alias="async")
    capped: bool = False
    note: str | None = None

    model_config = {"populate_by_name": True}


class PaidCatalogOut(BaseModel):
    """What the edge sells over x402, read from the edge itself so its prices are the
    ones charged. `available` is false when the edge could not be read."""

    available: bool
    base_url: str | None = Field(
        description="Where buyers call the paid routes; MISTHOS_PAID_PUBLIC_URL"
    )
    networks: list[str] = Field(default_factory=list)
    pay_to: str | None = None
    asset: str | None = None
    certificate_signer: str | None = None
    routes: list[PaidRouteOut] = Field(default_factory=list)
