"""Price proposals keep the settled issues their price was compared with (#42).

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-08 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    with op.batch_alter_table("price_proposals") as batch:
        batch.add_column(sa.Column("comparables", json, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("price_proposals") as batch:
        batch.drop_column("comparables")
