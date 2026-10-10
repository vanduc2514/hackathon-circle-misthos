"""The database schema.

The tables mirror the issue aggregate in `records.py` and the entity list in
docs/ARCHITECTURE.md. Three rules the architecture calls load-bearing are enforced
by the schema rather than trusted to code:

- price proposals and decisions are append-only: a change is a new row, never an
  edit, because overrides are training signal and the log is the audit trail;
- an issue has at most one active claim, by a partial unique index, so two
  contributors cannot hold the same issue even if two processes race;
- money is integer base units of the 6-decimal USDC view, never a decimal or a
  float, the same unit `domain/money.py` uses.

Postgres is the target. SQLite runs the same schema for a local file database, which
is why the partial index carries each dialect's own WHERE clause.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)

Json = JSON().with_variant(JSONB(), "postgresql")
Timestamp = DateTime(timezone=True)
Id = String(32)

publishers = Table(
    "publishers",
    metadata,
    Column("id", Id, primary_key=True),
    Column("name", String(200), nullable=False),
    Column("kind", String(16), nullable=False),
    Column("tier", String(16), nullable=False),
    # The wallet the publisher funds from: the escrow takes a commitment from it alone,
    # and refunds go back to it. Null for one who signed in with GitHub and has not
    # connected a wallet yet (#131); approving a price waits for it.
    Column("wallet_address", String(64)),
    Column("chain", String(32)),
    # The publisher's own Circle wallet, kept beside the funding wallet and never in its
    # place: a commitment from the browser wallet would otherwise be refused (#123).
    Column("circle_wallet_address", String(64)),
    Column("circle_wallet_chain", String(32)),
    Column("budget_remaining_base_units", BigInteger, nullable=False),
    Column("approval_threshold_base_units", BigInteger),
    Column("approvers", Json, nullable=False),
    Column("category_limits", Json, nullable=False),
)

contributors = Table(
    "contributors",
    metadata,
    Column("id", Id, primary_key=True),
    Column("handle", String(100), nullable=False),
    # Where payouts go. Null until a contributor who signed in with GitHub connects a
    # wallet or sets up a Circle one (#131); claiming waits for it.
    Column("wallet_address", String(64)),
    Column("chain", String(32)),
    Column("reputation", Integer, nullable=False),
    Column("settled_issues", Integer, nullable=False),
    Column("earned_base_units", BigInteger, nullable=False),
    # Only the outcome and the provider's reference: the documents stay with the provider.
    Column("identity_status", String(16), nullable=False),
    Column("identity_reference", String(128)),
    Column("identity_verified_at", Timestamp),
)

issues = Table(
    "issues",
    metadata,
    Column("id", Id, primary_key=True),
    Column("repo", String(200), nullable=False),
    Column("number", Integer, nullable=False),
    Column("title", Text, nullable=False),
    Column("summary", Text, nullable=False),
    # Only the lifecycle, through the store, writes this column.
    Column("state", String(32), nullable=False, index=True),
    Column("labels", Json, nullable=False),
    Column("compliance_driven", Boolean, nullable=False),
    Column("acceptance_criteria", Json, nullable=False),
    Column("publisher_id", Id, ForeignKey("publishers.id"), nullable=False),
    Column("contributor_id", Id, ForeignKey("contributors.id")),
    Column("created_at", Timestamp, nullable=False),
    Column("deadline", Timestamp),
    Column("paid_base_units", BigInteger),
    Column("platform_fee_base_units", BigInteger),
    Column("paid_at", Timestamp),
    Column("payout_tx_hash", String(80)),
    Column("accepted_by", String(16)),
    Column("payout_hold", String(32)),
    Column("payout_checked_at", Timestamp),
    Column("criteria_approved_at", Timestamp),
    Column("relisted_from", Id, ForeignKey("issues.id")),
    # The terms the publisher approved at the price checkpoint, which booking checks
    # the commitment against (#126). Null until the price is approved.
    Column("funding_amount_base_units", BigInteger),
    Column("funding_fee_bps", Integer),
    Column("funding_wallet", String(64)),
    Column("funding_deadline", Timestamp),
    Column("funding_approved_at", Timestamp),
    Column("version", Integer, nullable=False),
)

price_proposals = Table(
    "price_proposals",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("issue_id", Id, ForeignKey("issues.id"), nullable=False),
    Column("seq", Integer, nullable=False),
    Column("band_low_base_units", BigInteger, nullable=False),
    Column("band_high_base_units", BigInteger, nullable=False),
    Column("recommended_base_units", BigInteger, nullable=False),
    Column("estimated_hours", Float, nullable=False),
    Column("complexity_score", Float, nullable=False),
    Column("confidence", String(16), nullable=False),
    Column("signals", Json, nullable=False),
    Column("justification", Text, nullable=False),
    Column("fundable", Boolean, nullable=False),
    # The settled issues the price was compared with (#42); null before they were kept.
    Column("comparables", Json),
    Column("created_at", Timestamp, nullable=False),
    UniqueConstraint("issue_id", "seq"),
)

# A mirror of the on-chain commitment, never the authority on it. See ARCHITECTURE.
escrow_commitments = Table(
    "escrow_commitments",
    metadata,
    Column("issue_id", Id, ForeignKey("issues.id"), primary_key=True),
    Column("contract", String(64), nullable=False),
    Column("chain", String(32), nullable=False),
    # What the money was (#31): the simulation and Arc testnet share a chain id, so the
    # chain alone cannot tell test money from none. Null before it was kept.
    Column("money", String(16)),
    Column("tx_hash", String(80), nullable=False, unique=True),
    Column("amount_base_units", BigInteger, nullable=False),
    Column("deadline", Timestamp, nullable=False),
    # The take rate the escrow holds for this issue, in basis points (#32).
    Column("fee_bps", Integer, nullable=False, server_default="0"),
    Column("released", Boolean, nullable=False),
    Column("refunded", Boolean, nullable=False),
)

claims = Table(
    "claims",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("issue_id", Id, ForeignKey("issues.id"), nullable=False),
    Column("contributor_id", Id, ForeignKey("contributors.id"), nullable=False),
    Column("issued_at", Timestamp, nullable=False),
    Column("expires_at", Timestamp, nullable=False),
    Column("active", Boolean, nullable=False),
    UniqueConstraint("issue_id", "contributor_id", "issued_at"),
    Index(
        "uq_claims_one_active_per_issue",
        "issue_id",
        unique=True,
        postgresql_where=text("active"),
        sqlite_where=text("active = 1"),
    ),
)

submissions = Table(
    "submissions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("issue_id", Id, ForeignKey("issues.id"), nullable=False),
    Column("pr_number", Integer, nullable=False),
    Column("head_sha", String(64), nullable=False),
    Column("checks_passed", Boolean),  # NULL: the checks have not reported
    Column("files_changed", Integer, nullable=False),
    Column("additions", Integer, nullable=False),
    Column("deletions", Integer, nullable=False),
    UniqueConstraint("issue_id", "head_sha"),
)

reviews = Table(
    "reviews",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("issue_id", Id, ForeignKey("issues.id"), nullable=False),
    Column("verdict", String(16), nullable=False),
    Column("findings", Json, nullable=False),
    Column("decided_at", Timestamp, nullable=False),
    Column("head_sha", String(64)),
    Column("reviewer", String(64)),
    Column("seconds", Float),
    Column("cost_base_units", BigInteger),
    UniqueConstraint("issue_id", "decided_at"),
)

# Append-only. `position` keeps each issue's log in the order it was written, which
# is not always timestamp order: seeded history is backdated around a live entry.
decisions = Table(
    "decisions",
    metadata,
    Column("id", Id, primary_key=True),
    Column("issue_id", Id, ForeignKey("issues.id"), nullable=False),
    Column("position", Integer, nullable=False),
    Column("actor", String(16), nullable=False),
    Column("action", String(64), nullable=False),
    Column("rule", String(64), nullable=False),
    Column("outcome", Text, nullable=False),
    Column("cost_usdc", String(32)),
    Column("created_at", Timestamp, nullable=False, index=True),
    UniqueConstraint("issue_id", "position"),
)

# Append-only audit trail of every sanctions check, kept for SCREENING_RETENTION.
screenings = Table(
    "screenings",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("party_kind", String(16), nullable=False),
    Column("party_id", Id, nullable=False),
    Column("wallet_address", String(64), nullable=False),
    Column("outcome", String(8), nullable=False),
    Column("list_name", String(64)),
    Column("provider", String(32), nullable=False),
    Column("reason", String(16), nullable=False),
    Column("checked_at", Timestamp, nullable=False),
    Index("ix_screenings_party", "party_kind", "party_id", "checked_at"),
)

# The money ledger: every commitment, release and refund, appended and never edited.
# Reconciled against the chain, which is the authority; see domain/ledger.py.
money_events = Table(
    "money_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("issue_id", Id, ForeignKey("issues.id"), nullable=False),
    Column("position", Integer, nullable=False),
    Column("kind", String(16), nullable=False),
    Column("amount_base_units", BigInteger, nullable=False),
    Column("counterparty_id", Id, nullable=False),
    Column("tx_hash", String(80), nullable=False, unique=True),
    Column("occurred_at", Timestamp, nullable=False),
    UniqueConstraint("issue_id", "position"),
)

# The simulated chain's own books. Not the ledger: the thing the ledger is reconciled
# against while no real chain is contacted. Unused once the Arc client runs (#69).
simulated_escrow = Table(
    "simulated_escrow",
    metadata,
    Column("issue_id", Id, primary_key=True),
    Column("publisher_wallet", String(64), nullable=False),
    Column("amount_base_units", BigInteger, nullable=False),
    Column("status", String(16), nullable=False),
    Column("last_tx_hash", String(80), nullable=False),
    # The take rate the simulated escrow holds, so the fee a settlement records is the
    # rate the escrow enforced (#32).
    Column("fee_bps", Integer, nullable=False, server_default="0"),
    # The commitment's deadline, which the simulation enforces as the contract does:
    # no release after it, no refund before it (#121). Null on books kept before it was.
    Column("deadline", Timestamp),
    Column("commit_tx_hash", String(80)),
)

# The simulated escrow's per-issue ceilings: the price a human approved, which the
# escrow refuses to exceed (#34). Separate from the books because a ceiling exists
# before any commitment does.
simulated_escrow_ceilings = Table(
    "simulated_escrow_ceilings",
    metadata,
    Column("issue_id", Id, primary_key=True),
    Column("ceiling_base_units", BigInteger, nullable=False),
    # Who may commit and until when, named with the ceiling (#122).
    Column("publisher_wallet", String(64)),
    Column("latest_deadline", Timestamp),
)

# A signed-in party and the role it chose (#70), one row per party. It signs in with a
# wallet, a GitHub account, or either (#131), and each of those belongs to one account:
# the GitHub identity is the numeric id, because a login can be renamed and reused.
accounts = Table(
    "accounts",
    metadata,
    Column("party_id", Id, primary_key=True),
    Column("role", String(16), nullable=False),
    Column("address", String(42), unique=True),
    Column("github_id", BigInteger, unique=True),
    Column("github_login", String(64), unique=True),
    Column("created_at", Timestamp, nullable=False),
)

# Every GitHub webhook delivery handled, so a redelivery is recognised and ignored.
webhook_deliveries = Table(
    "webhook_deliveries",
    metadata,
    Column("delivery_id", String(64), primary_key=True),
    Column("event", String(32), nullable=False),
    Column("received_at", Timestamp, nullable=False),
)

# Sequences for the human-readable ids (ISS-1001, DEC-0001), shared by every process.
counters = Table(
    "counters",
    metadata,
    Column("name", String(32), primary_key=True),
    Column("value", BigInteger, nullable=False),
)


# A publisher's plan, one row per publisher (#53). The pending payment request, if
# any, is kept with it.
subscriptions = Table(
    "subscriptions",
    metadata,
    Column("publisher_id", Id, ForeignKey("publishers.id"), primary_key=True),
    Column("plan", String(16), nullable=False),
    Column("status", String(16), nullable=False),
    Column("period_start", Timestamp),
    Column("period_end", Timestamp),
    Column("cancel_at_period_end", Boolean, nullable=False),
    Column("pending", Json),
    Column("updated_at", Timestamp, nullable=False),
)

# Every subscription payment, append-only. A transaction pays for one period, once.
subscription_payments = Table(
    "subscription_payments",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("publisher_id", Id, ForeignKey("publishers.id"), nullable=False),
    Column("plan", String(16), nullable=False),
    Column("amount_base_units", BigInteger, nullable=False),
    Column("tx_hash", String(66), nullable=False, unique=True),
    Column("period_start", Timestamp, nullable=False),
    Column("period_end", Timestamp, nullable=False),
    Column("paid_at", Timestamp, nullable=False),
)


# Repositories the GitHub App is installed on (#6). The publisher is whoever installed
# it, once that GitHub login is linked to a publisher's wallet.
repo_connections = Table(
    "repo_connections",
    metadata,
    Column("repo", String(140), primary_key=True),
    Column("installation_id", BigInteger, nullable=False),
    Column("installed_by", String(64), nullable=False),
    Column("publisher_id", Id, ForeignKey("publishers.id")),
    Column("connected_at", Timestamp, nullable=False),
)


# An Enterprise organisation's connection to its own identity provider (#53). The
# client secret is named by reference in the secret store, never kept here.
sso_connections = Table(
    "sso_connections",
    metadata,
    Column("publisher_id", Id, ForeignKey("publishers.id"), primary_key=True),
    Column("issuer", String(300), nullable=False),
    Column("client_id", String(256), nullable=False),
    Column("client_secret_ref", String(128), nullable=False),
    Column("required", Boolean, nullable=False),
    Column("configured_at", Timestamp, nullable=False),
)

# The e-mail domains each connection speaks for. A domain routes sign-ins to one
# organisation only, and the primary key is what makes that hold across processes.
sso_domains = Table(
    "sso_domains",
    metadata,
    Column("domain", String(253), primary_key=True),
    Column(
        "publisher_id", Id, ForeignKey("sso_connections.publisher_id"), nullable=False
    ),
)
