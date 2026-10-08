"""Webhook deliveries: every GitHub delivery handled, so a redelivery is ignored.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-07 18:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "webhook_deliveries",
        sa.Column("delivery_id", sa.String(length=64), nullable=False),
        sa.Column("event", sa.String(length=32), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("delivery_id", name=op.f("pk_webhook_deliveries")),
    )


def downgrade() -> None:
    op.drop_table("webhook_deliveries")
