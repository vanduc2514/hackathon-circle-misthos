"""Configurable escrow term (#172).

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-15 00:00:00
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
    op.add_column("issues", sa.Column("escrow_term_days", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("issues", "escrow_term_days")
