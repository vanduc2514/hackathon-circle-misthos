"""Spending policy: a release threshold, its named approvers, and category limits.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-07 22:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

Json = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    with op.batch_alter_table("publishers") as batch:
        batch.add_column(sa.Column("approval_threshold_base_units", sa.BigInteger(), nullable=True))
        # Existing publishers start with no policy: nobody to approve, nothing limited.
        batch.add_column(
            sa.Column("approvers", Json, nullable=False, server_default=sa.text("'[]'"))
        )
        batch.add_column(
            sa.Column("category_limits", Json, nullable=False, server_default=sa.text("'{}'"))
        )


def downgrade() -> None:
    with op.batch_alter_table("publishers") as batch:
        batch.drop_column("category_limits")
        batch.drop_column("approvers")
        batch.drop_column("approval_threshold_base_units")
