"""Remove the subscription model (#183).

The platform has one price discipline now: the publisher names the price and the
escrow carves out a flat 10 percent on settlement. There is no plan to buy, so the
subscription rows and the column that held a publisher's tier go with the pages that
sold them.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-17 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("subscription_payments")
    op.drop_table("subscriptions")
    with op.batch_alter_table("publishers") as batch:
        batch.drop_column("tier")


def downgrade() -> None:
    with op.batch_alter_table("publishers") as batch:
        # Existing publishers come back on Open: no subscription rows survive to say
        # otherwise.
        batch.add_column(
            sa.Column("tier", sa.String(length=16), nullable=False, server_default="open")
        )
    json = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "subscriptions",
        sa.Column("publisher_id", sa.String(length=32), nullable=False),
        sa.Column("plan", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False),
        sa.Column("pending", json, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["publisher_id"],
            ["publishers.id"],
            name=op.f("fk_subscriptions_publisher_id_publishers"),
        ),
        sa.PrimaryKeyConstraint("publisher_id", name=op.f("pk_subscriptions")),
    )
    op.create_table(
        "subscription_payments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("publisher_id", sa.String(length=32), nullable=False),
        sa.Column("plan", sa.String(length=16), nullable=False),
        sa.Column("amount_base_units", sa.BigInteger(), nullable=False),
        sa.Column("tx_hash", sa.String(length=66), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["publisher_id"],
            ["publishers.id"],
            name=op.f("fk_subscription_payments_publisher_id_publishers"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_subscription_payments")),
        sa.UniqueConstraint("tx_hash", name=op.f("uq_subscription_payments_tx_hash")),
    )
