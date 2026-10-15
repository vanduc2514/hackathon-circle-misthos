"""Let one wallet publish and contribute (#164).

Until now an account was one side: `party_id` named its Publisher or its Contributor,
and `role` said which. Two nullable, unique columns name every side it has. Every
account is carried over with its one side filled in from `role` and `party_id`, which
stay as the side it chose first.

SQLite cannot add a unique column in place, so the table goes through alembic's batch
mode, which rebuilds it there and alters it in place on Postgres.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-14 12:00:00
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
    with op.batch_alter_table("accounts") as batch:
        batch.add_column(sa.Column("publisher_id", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("contributor_id", sa.String(length=32), nullable=True))
        batch.create_unique_constraint("uq_accounts_publisher_id", ["publisher_id"])
        batch.create_unique_constraint("uq_accounts_contributor_id", ["contributor_id"])
    op.execute("UPDATE accounts SET publisher_id = party_id WHERE role = 'publisher'")
    op.execute("UPDATE accounts SET contributor_id = party_id WHERE role = 'contributor'")


def downgrade() -> None:
    # An account holding both sides has nowhere to keep its second one before #164.
    bind = op.get_bind()
    both = bind.execute(
        sa.text(
            "SELECT count(*) FROM accounts "
            "WHERE publisher_id IS NOT NULL AND contributor_id IS NOT NULL"
        )
    ).scalar()
    if both:
        raise RuntimeError(
            f"{both} accounts publish and contribute; 0015 can hold only one side each"
        )
    with op.batch_alter_table("accounts") as batch:
        batch.drop_constraint("uq_accounts_contributor_id", type_="unique")
        batch.drop_constraint("uq_accounts_publisher_id", type_="unique")
        batch.drop_column("contributor_id")
        batch.drop_column("publisher_id")
