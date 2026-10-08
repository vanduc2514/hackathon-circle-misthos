"""Money ledger: commitments, releases and refunds as append-only events.

Also the simulated chain's own books, which the ledger is reconciled against while
no real chain is contacted.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-07 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "money_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("issue_id", sa.String(length=32), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("amount_base_units", sa.BigInteger(), nullable=False),
        sa.Column("counterparty_id", sa.String(length=32), nullable=False),
        sa.Column("tx_hash", sa.String(length=80), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["issue_id"], ["issues.id"], name=op.f("fk_money_events_issue_id_issues")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_money_events")),
        sa.UniqueConstraint("issue_id", "position", name=op.f("uq_money_events_issue_id_position")),
        sa.UniqueConstraint("tx_hash", name=op.f("uq_money_events_tx_hash")),
    )
    op.create_table(
        "simulated_escrow",
        sa.Column("issue_id", sa.String(length=32), nullable=False),
        sa.Column("publisher_wallet", sa.String(length=64), nullable=False),
        sa.Column("amount_base_units", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("last_tx_hash", sa.String(length=80), nullable=False),
        sa.PrimaryKeyConstraint("issue_id", name=op.f("pk_simulated_escrow")),
    )


def downgrade() -> None:
    op.drop_table("simulated_escrow")
    op.drop_table("money_events")
