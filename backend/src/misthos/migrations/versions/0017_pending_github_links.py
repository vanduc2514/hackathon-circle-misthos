"""A GitHub login held for a wallet that has no account yet (#181).

The sign-in gate links GitHub before the side is chosen, and the account is created by
choosing the side. The login has to wait somewhere in between, and it has to take the
one-login-one-wallet slot while it waits, or a second wallet could link the same login
before the first one finished. The account adopts the row when it is created.

Revision ID: 0017
Revises: 0015
Create Date: 2026-10-16 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pending_github_links",
        sa.Column("address", sa.String(length=42), nullable=False),
        sa.Column("github_login", sa.String(length=64), nullable=False),
        sa.Column("login_key", sa.String(length=64), nullable=False),
        sa.Column("held_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("address", name=op.f("pk_pending_github_links")),
        sa.UniqueConstraint("login_key", name=op.f("uq_pending_github_links_login_key")),
    )


def downgrade() -> None:
    op.drop_table("pending_github_links")
