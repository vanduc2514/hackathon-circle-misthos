"""The publisher names the price (#142).

Until now an issue's amount was the engine's recommendation, approved by default and
never stored before funding. The price a publisher sets at publish time now has to
survive until the money moves, so it gets a column of its own on the issue.

Nullable: issues published and funded before this revision have no publisher price,
and their committed amount is the escrow's record to keep.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-13 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("issues", sa.Column("price_base_units", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("issues", "price_base_units")
