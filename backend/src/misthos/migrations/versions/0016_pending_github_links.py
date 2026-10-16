"""Pending GitHub links (#181).

Hold a GitHub login against an address before its account is created,
enforcing one-login-one-wallet for pending links as well as full accounts.

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
    op.create_table(
        "pending_github_links",
        sa.Column("address", sa.String(length=42), primary_key=True),
        sa.Column("github_login", sa.String(length=64), nullable=False, unique=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("pending_github_links")
