"""Keep the approved funding terms, the escrow deadlines, and the Circle wallet apart.

Three things the Arc settlement path needs to remember rather than recompute:

- The terms a publisher approved (amount, take rate, wallet, deadline), so booking the
  commitment checks it against what they were shown and not against "now" (#126).
- The simulated escrow's deadline per commitment, and who may commit and until when
  per ceiling, so the simulation refuses what the contract refuses (#121, #122).
- A publisher's Circle wallet in a column of its own, beside the wallet they fund
  from, which it used to overwrite (#123).

Every column is nullable: a row written before this revision has none of them.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-09 12:00:00
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
    op.add_column("publishers", sa.Column("circle_wallet_address", sa.String(64)))
    op.add_column("publishers", sa.Column("circle_wallet_chain", sa.String(32)))

    op.add_column("issues", sa.Column("funding_amount_base_units", sa.BigInteger()))
    op.add_column("issues", sa.Column("funding_fee_bps", sa.Integer()))
    op.add_column("issues", sa.Column("funding_wallet", sa.String(64)))
    op.add_column("issues", sa.Column("funding_deadline", sa.DateTime(timezone=True)))
    op.add_column("issues", sa.Column("funding_approved_at", sa.DateTime(timezone=True)))

    op.add_column("simulated_escrow", sa.Column("deadline", sa.DateTime(timezone=True)))
    op.add_column("simulated_escrow", sa.Column("commit_tx_hash", sa.String(80)))

    op.add_column("simulated_escrow_ceilings", sa.Column("publisher_wallet", sa.String(64)))
    op.add_column(
        "simulated_escrow_ceilings", sa.Column("latest_deadline", sa.DateTime(timezone=True))
    )


def downgrade() -> None:
    op.drop_column("simulated_escrow_ceilings", "latest_deadline")
    op.drop_column("simulated_escrow_ceilings", "publisher_wallet")
    op.drop_column("simulated_escrow", "commit_tx_hash")
    op.drop_column("simulated_escrow", "deadline")
    for column in (
        "funding_approved_at",
        "funding_deadline",
        "funding_wallet",
        "funding_fee_bps",
        "funding_amount_base_units",
    ):
        op.drop_column("issues", column)
    op.drop_column("publishers", "circle_wallet_chain")
    op.drop_column("publishers", "circle_wallet_address")
