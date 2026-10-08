"""Initial schema: the issue aggregate, its append-only history and the parties.

Revision ID: 0001
Revises:
Create Date: 2026-10-07 08:12:04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

Json = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
Timestamp = sa.DateTime(timezone=True)
Id = sa.String(length=32)


def upgrade() -> None:
    op.create_table(
        "publishers",
        sa.Column("id", Id, nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("tier", sa.String(length=16), nullable=False),
        sa.Column("wallet_address", sa.String(length=64), nullable=False),
        sa.Column("chain", sa.String(length=32), nullable=False),
        sa.Column("budget_remaining_base_units", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_publishers")),
    )
    op.create_table(
        "contributors",
        sa.Column("id", Id, nullable=False),
        sa.Column("handle", sa.String(length=100), nullable=False),
        sa.Column("wallet_address", sa.String(length=64), nullable=False),
        sa.Column("chain", sa.String(length=32), nullable=False),
        sa.Column("reputation", sa.Integer(), nullable=False),
        sa.Column("settled_issues", sa.Integer(), nullable=False),
        sa.Column("earned_base_units", sa.BigInteger(), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contributors")),
    )
    op.create_table(
        "counters",
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("value", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_counters")),
    )
    op.create_table(
        "issues",
        sa.Column("id", Id, nullable=False),
        sa.Column("repo", sa.String(length=200), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("labels", Json, nullable=False),
        sa.Column("compliance_driven", sa.Boolean(), nullable=False),
        sa.Column("acceptance_criteria", Json, nullable=False),
        sa.Column("publisher_id", Id, nullable=False),
        sa.Column("contributor_id", Id, nullable=True),
        sa.Column("created_at", Timestamp, nullable=False),
        sa.Column("deadline", Timestamp, nullable=True),
        sa.Column("paid_base_units", sa.BigInteger(), nullable=True),
        sa.Column("relisted_from", Id, nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["contributor_id"],
            ["contributors.id"],
            name=op.f("fk_issues_contributor_id_contributors"),
        ),
        sa.ForeignKeyConstraint(
            ["publisher_id"], ["publishers.id"], name=op.f("fk_issues_publisher_id_publishers")
        ),
        sa.ForeignKeyConstraint(
            ["relisted_from"], ["issues.id"], name=op.f("fk_issues_relisted_from_issues")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_issues")),
    )
    op.create_index(op.f("ix_issues_state"), "issues", ["state"], unique=False)
    op.create_table(
        "price_proposals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("issue_id", Id, nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("band_low_base_units", sa.BigInteger(), nullable=False),
        sa.Column("band_high_base_units", sa.BigInteger(), nullable=False),
        sa.Column("recommended_base_units", sa.BigInteger(), nullable=False),
        sa.Column("estimated_hours", sa.Float(), nullable=False),
        sa.Column("complexity_score", sa.Float(), nullable=False),
        sa.Column("confidence", sa.String(length=16), nullable=False),
        sa.Column("signals", Json, nullable=False),
        sa.Column("justification", sa.Text(), nullable=False),
        sa.Column("fundable", sa.Boolean(), nullable=False),
        sa.Column("created_at", Timestamp, nullable=False),
        sa.ForeignKeyConstraint(
            ["issue_id"], ["issues.id"], name=op.f("fk_price_proposals_issue_id_issues")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_price_proposals")),
        sa.UniqueConstraint("issue_id", "seq", name=op.f("uq_price_proposals_issue_id_seq")),
    )
    op.create_table(
        "escrow_commitments",
        sa.Column("issue_id", Id, nullable=False),
        sa.Column("contract", sa.String(length=64), nullable=False),
        sa.Column("chain", sa.String(length=32), nullable=False),
        sa.Column("tx_hash", sa.String(length=80), nullable=False),
        sa.Column("amount_base_units", sa.BigInteger(), nullable=False),
        sa.Column("deadline", Timestamp, nullable=False),
        sa.Column("released", sa.Boolean(), nullable=False),
        sa.Column("refunded", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["issue_id"], ["issues.id"], name=op.f("fk_escrow_commitments_issue_id_issues")
        ),
        sa.PrimaryKeyConstraint("issue_id", name=op.f("pk_escrow_commitments")),
        sa.UniqueConstraint("tx_hash", name=op.f("uq_escrow_commitments_tx_hash")),
    )
    op.create_table(
        "claims",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("issue_id", Id, nullable=False),
        sa.Column("contributor_id", Id, nullable=False),
        sa.Column("issued_at", Timestamp, nullable=False),
        sa.Column("expires_at", Timestamp, nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["contributor_id"],
            ["contributors.id"],
            name=op.f("fk_claims_contributor_id_contributors"),
        ),
        sa.ForeignKeyConstraint(
            ["issue_id"], ["issues.id"], name=op.f("fk_claims_issue_id_issues")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claims")),
        sa.UniqueConstraint(
            "issue_id",
            "contributor_id",
            "issued_at",
            name=op.f("uq_claims_issue_id_contributor_id_issued_at"),
        ),
    )
    # At most one active claim per issue, enforced where two processes cannot race it.
    op.create_index(
        "uq_claims_one_active_per_issue",
        "claims",
        ["issue_id"],
        unique=True,
        postgresql_where=sa.text("active"),
        sqlite_where=sa.text("active = 1"),
    )
    op.create_table(
        "submissions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("issue_id", Id, nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("head_sha", sa.String(length=64), nullable=False),
        sa.Column("checks_passed", sa.Boolean(), nullable=False),
        sa.Column("files_changed", sa.Integer(), nullable=False),
        sa.Column("additions", sa.Integer(), nullable=False),
        sa.Column("deletions", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["issue_id"], ["issues.id"], name=op.f("fk_submissions_issue_id_issues")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_submissions")),
        sa.UniqueConstraint(
            "issue_id", "head_sha", name=op.f("uq_submissions_issue_id_head_sha")
        ),
    )
    op.create_table(
        "reviews",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("issue_id", Id, nullable=False),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("findings", Json, nullable=False),
        sa.Column("decided_at", Timestamp, nullable=False),
        sa.ForeignKeyConstraint(
            ["issue_id"], ["issues.id"], name=op.f("fk_reviews_issue_id_issues")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reviews")),
        sa.UniqueConstraint(
            "issue_id", "decided_at", name=op.f("uq_reviews_issue_id_decided_at")
        ),
    )
    op.create_table(
        "decisions",
        sa.Column("id", Id, nullable=False),
        sa.Column("issue_id", Id, nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("actor", sa.String(length=16), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("rule", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("cost_usdc", sa.String(length=32), nullable=True),
        sa.Column("created_at", Timestamp, nullable=False),
        sa.ForeignKeyConstraint(
            ["issue_id"], ["issues.id"], name=op.f("fk_decisions_issue_id_issues")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_decisions")),
        sa.UniqueConstraint("issue_id", "position", name=op.f("uq_decisions_issue_id_position")),
    )
    op.create_index(
        op.f("ix_decisions_created_at"), "decisions", ["created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_decisions_created_at"), table_name="decisions")
    op.drop_table("decisions")
    op.drop_table("reviews")
    op.drop_table("submissions")
    op.drop_index("uq_claims_one_active_per_issue", table_name="claims")
    op.drop_table("claims")
    op.drop_table("escrow_commitments")
    op.drop_table("price_proposals")
    op.drop_index(op.f("ix_issues_state"), table_name="issues")
    op.drop_table("issues")
    op.drop_table("counters")
    op.drop_table("contributors")
    op.drop_table("publishers")
