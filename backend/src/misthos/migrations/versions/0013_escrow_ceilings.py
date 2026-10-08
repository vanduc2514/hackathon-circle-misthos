"""The simulated escrow keeps per-issue ceilings, as MisthosEscrow does.

The contract refuses a commitment with no ceiling or above it (#34), and the
simulation must refuse the same calls. A ceiling exists before any commitment, so
it has its own table rather than a column on the books.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-08 22:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "simulated_escrow_ceilings",
        sa.Column("issue_id", sa.String(length=32), nullable=False),
        sa.Column("ceiling_base_units", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("issue_id", name=op.f("pk_simulated_escrow_ceilings")),
    )


def downgrade() -> None:
    op.drop_table("simulated_escrow_ceilings")
