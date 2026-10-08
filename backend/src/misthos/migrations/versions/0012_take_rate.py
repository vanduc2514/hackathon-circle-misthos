"""The take rate is fixed with the money, not read from the publisher's plan.

A release computes the platform's fee from the rate the escrow holds for that issue, so
the recorded fee is the transfer. Reading the publisher's tier at release time instead
would let a plan that lapses mid-flight move the fee after the price was approved.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-08 21:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "escrow_commitments",
        sa.Column("fee_bps", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "simulated_escrow",
        sa.Column("fee_bps", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("issues", sa.Column("platform_fee_base_units", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("issues", "platform_fee_base_units")
    op.drop_column("simulated_escrow", "fee_bps")
    op.drop_column("escrow_commitments", "fee_bps")
