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
    Column("wallet_address", String(64), nullable=False),
    Column("chain", String(32), nullable=False),
    Column("budget_remaining_base_units", BigInteger, nullable=False),
)

contributors = Table(
    "contributors",
    metadata,
    Column("id", Id, primary_key=True),
    Column("handle", String(100), nullable=False),
    Column("wallet_address", String(64), nullable=False),
    Column("chain", String(32), nullable=False),
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
    Column("paid_at", Timestamp),
    Column("payout_tx_hash", String(80)),
    Column("accepted_by", String(16)),
    Column("payout_hold", String(32)),
    Column("payout_checked_at", Timestamp),
    Column("relisted_from", Id, ForeignKey("issues.id")),
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
    Column("tx_hash", String(80), nullable=False, unique=True),
    Column("amount_base_units", BigInteger, nullable=False),
    Column("deadline", Timestamp, nullable=False),
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
    Column("checks_passed", Boolean, nullable=False),
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
