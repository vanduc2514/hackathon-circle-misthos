"""Drop the simulated escrow's books.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-17
"""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS simulated_escrow_ceilings")
    op.execute("DROP TABLE IF EXISTS simulated_escrow")


def downgrade() -> None:
    """The books are not rebuilt: nothing writes them any more."""
