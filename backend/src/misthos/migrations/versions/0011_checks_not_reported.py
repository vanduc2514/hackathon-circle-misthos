"""A submission's checks can be not reported yet, which is not the same as failing.

NULL means the project's checks have not reported on the commit: still running, or a
repository with no CI at all. Stored as false, a fresh submission read as failing
and a repository without CI could never be accepted.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-08 20:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("submissions") as batch:
        batch.alter_column("checks_passed", existing_type=sa.Boolean(), nullable=True)


def downgrade() -> None:
    # The old schema has no "not reported", and false is what it stored for one.
    op.execute("UPDATE submissions SET checks_passed = false WHERE checks_passed IS NULL")
    with op.batch_alter_table("submissions") as batch:
        batch.alter_column("checks_passed", existing_type=sa.Boolean(), nullable=False)
