"""Review provenance: the commit a verdict is about, who issued it, its time and cost.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-07 20:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("reviews") as batch:
        batch.add_column(sa.Column("head_sha", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("reviewer", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("seconds", sa.Float(), nullable=True))
        batch.add_column(sa.Column("cost_base_units", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("reviews") as batch:
        batch.drop_column("cost_base_units")
        batch.drop_column("seconds")
        batch.drop_column("reviewer")
        batch.drop_column("head_sha")
