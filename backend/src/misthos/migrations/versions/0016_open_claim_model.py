"""Open the claim model: multi-claimant roster, submissions per claim (#171).

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-16 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Alter claims: expires_at nullable, add state
    with op.batch_alter_table("claims") as batch_op:
        batch_op.alter_column("expires_at", existing_type=sa.TIMESTAMP(timezone=True), nullable=True)
        batch_op.add_column(sa.Column("state", sa.String(length=32), nullable=False, server_default="CLAIMED"))
        try:
            batch_op.drop_index("uq_claims_one_active_per_issue")
        except Exception:
            pass
        batch_op.create_index(
            "uq_claims_one_active_per_contributor",
            ["issue_id", "contributor_id"],
            unique=True,
            postgresql_where=sa.text("active"),
            sqlite_where=sa.text("active = 1"),
        )

    # 2. Add columns to submissions
    with op.batch_alter_table("submissions") as batch_op:
        batch_op.add_column(sa.Column("contributor_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("review_count", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("deposit_state", sa.String(length=32), nullable=False, server_default="none"))
        batch_op.add_column(sa.Column("deposit_amount", sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column("draft", sa.Boolean(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("submissions") as batch_op:
        batch_op.drop_column("draft")
        batch_op.drop_column("deposit_amount")
        batch_op.drop_column("deposit_state")
        batch_op.drop_column("review_count")
        batch_op.drop_column("contributor_id")

    with op.batch_alter_table("claims") as batch_op:
        try:
            batch_op.drop_index("uq_claims_one_active_per_contributor")
        except Exception:
            pass
        batch_op.create_index(
            "uq_claims_one_active_per_issue",
            ["issue_id"],
            unique=True,
            postgresql_where=sa.text("active"),
            sqlite_where=sa.text("active = 1"),
        )
