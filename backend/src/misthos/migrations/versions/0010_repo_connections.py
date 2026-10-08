"""GitHub: the repositories the App is installed on, and their publisher (#6).

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-08 18:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "repo_connections",
        sa.Column("repo", sa.String(length=140), nullable=False),
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column("installed_by", sa.String(length=64), nullable=False),
        sa.Column("publisher_id", sa.String(length=32), nullable=True),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["publisher_id"],
            ["publishers.id"],
            name=op.f("fk_repo_connections_publisher_id_publishers"),
        ),
        sa.PrimaryKeyConstraint("repo", name=op.f("pk_repo_connections")),
    )


def downgrade() -> None:
    op.drop_table("repo_connections")
