"""Requests under the Enterprise support commitment, and when each is due (#53).

One new table and nothing else changes.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-10 17:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "support_requests",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("publisher_id", sa.String(length=32), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("issue_id", sa.String(length=32), nullable=True),
        sa.Column("opened_by", sa.String(length=254), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("respond_by", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_response", sa.Text(), nullable=True),
        sa.Column("responder", sa.String(length=200), nullable=True),
        sa.Column("first_response_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("alerted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["publisher_id"],
            ["publishers.id"],
            name=op.f("fk_support_requests_publisher_id_publishers"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_support_requests")),
    )


def downgrade() -> None:
    op.drop_table("support_requests")
