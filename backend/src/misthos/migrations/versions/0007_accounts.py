"""Accounts: a signed-in wallet and its role; and when an issue's criteria were approved.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-08 09:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("address", sa.String(length=42), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("party_id", sa.String(length=32), nullable=False),
        sa.Column("github_login", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("address", name=op.f("pk_accounts")),
        sa.UniqueConstraint("github_login", name=op.f("uq_accounts_github_login")),
        sa.UniqueConstraint("party_id", name=op.f("uq_accounts_party_id")),
    )
    with op.batch_alter_table("issues") as batch:
        batch.add_column(sa.Column("criteria_approved_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    with op.batch_alter_table("issues") as batch:
        batch.drop_column("criteria_approved_at")
    op.drop_table("accounts")
