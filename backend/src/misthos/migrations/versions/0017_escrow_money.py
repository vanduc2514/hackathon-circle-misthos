"""Record what an escrow's money was: simulated, test, real or unknown (#31).

The simulation and Arc testnet share a chain id, so `chain` could not tell test USDC
from no money at all, and the dashboard added both into one figure. One nullable
column. A commitment recorded before it stays null and is reported as unrecorded,
never guessed.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-10 15:00:00
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
    with op.batch_alter_table("escrow_commitments") as batch:
        batch.add_column(sa.Column("money", sa.String(length=16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("escrow_commitments") as batch:
        batch.drop_column("money")
