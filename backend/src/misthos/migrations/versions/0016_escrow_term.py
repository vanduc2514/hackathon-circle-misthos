"""The publisher names the escrow term (#172).

Until now every issue's escrow held the money for 14 days. The term a publisher names
at publish time has to survive until the money is committed, so it gets a column of
its own on the issue, in whole days.

Nullable: an issue published before this revision has no term of its own, and is read
as the 14 days it was funded on.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-15 09:00:00
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
