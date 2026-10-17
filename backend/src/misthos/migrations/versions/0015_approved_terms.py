"""Approved funding terms (#126).

Booking compared the commitment against a deadline and a take rate worked out again at
booking time, so a booking more than an hour after the plan -- or after a plan change --
was refused for good, with the publisher's USDC already in the escrow. The terms a price
approval fixes now have to survive until the commitment is booked, so they get columns of
their own on the issue.

Nullable: this only applies to issues approved after this revision. An approval already
in flight when it deploys has no terms row, and booking falls back to today's behaviour
for it rather than refusing the commitment it is holding.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-14 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("issues", sa.Column("terms_amount_base_units", sa.BigInteger(), nullable=True))
    op.add_column("issues", sa.Column("terms_fee_bps", sa.Integer(), nullable=True))
    op.add_column("issues", sa.Column("terms_wallet", sa.String(length=64), nullable=True))
    op.add_column("issues", sa.Column("terms_deadline", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "issues", sa.Column("terms_approved_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    for column in (
        "terms_amount_base_units",
        "terms_fee_bps",
        "terms_wallet",
        "terms_deadline",
        "terms_approved_at",
    ):
        op.drop_column("issues", column)
