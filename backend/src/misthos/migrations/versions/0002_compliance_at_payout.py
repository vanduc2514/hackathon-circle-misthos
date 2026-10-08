"""Compliance at payout: identity status, screenings, and the payout's own record.

Identity moves from a boolean to the provider's outcome and reference, verified at
first payout. Screenings get an append-only table. The release itself (when, which
transfer, what accepted the work, why it is held) gets columns on the issue, kept off
every public view.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-07 10:30:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

Timestamp = sa.DateTime(timezone=True)


def upgrade() -> None:
    with op.batch_alter_table("contributors") as batch:
        batch.add_column(
            sa.Column(
                "identity_status",
                sa.String(length=16),
                nullable=False,
                server_default="unverified",
            )
        )
        batch.add_column(sa.Column("identity_reference", sa.String(length=128), nullable=True))
        batch.add_column(sa.Column("identity_verified_at", Timestamp, nullable=True))
    op.execute("UPDATE contributors SET identity_status = 'verified' WHERE verified")
    with op.batch_alter_table("contributors") as batch:
        batch.drop_column("verified")
        batch.alter_column("identity_status", server_default=None)

    with op.batch_alter_table("issues") as batch:
        batch.add_column(sa.Column("paid_at", Timestamp, nullable=True))
        batch.add_column(sa.Column("payout_tx_hash", sa.String(length=80), nullable=True))
        batch.add_column(sa.Column("accepted_by", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("payout_hold", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("payout_checked_at", Timestamp, nullable=True))

    op.create_table(
        "screenings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("party_kind", sa.String(length=16), nullable=False),
        sa.Column("party_id", sa.String(length=32), nullable=False),
        sa.Column("wallet_address", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=8), nullable=False),
        sa.Column("list_name", sa.String(length=64), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=16), nullable=False),
        sa.Column("checked_at", Timestamp, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_screenings")),
    )
    op.create_index(
        "ix_screenings_party",
        "screenings",
        ["party_kind", "party_id", "checked_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_screenings_party", table_name="screenings")
    op.drop_table("screenings")

    with op.batch_alter_table("issues") as batch:
        batch.drop_column("payout_checked_at")
        batch.drop_column("payout_hold")
        batch.drop_column("accepted_by")
        batch.drop_column("payout_tx_hash")
        batch.drop_column("paid_at")

    with op.batch_alter_table("contributors") as batch:
        batch.add_column(
            sa.Column("verified", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    op.execute("UPDATE contributors SET verified = (identity_status = 'verified')")
    with op.batch_alter_table("contributors") as batch:
        batch.alter_column("verified", server_default=None)
        batch.drop_column("identity_reference")
        batch.drop_column("identity_verified_at")
        batch.drop_column("identity_status")
