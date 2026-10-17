"""Paid pre-flight reviews (#204): the job while it runs, the record afterwards.

A review an agent pays for runs as a job: the edge holds the verified payment, the
core reviews the diff, and the edge settles when it collects a finished review. The
row is the job and then the calibration record: latency, cost and verdict. It keeps
the diff's size, never the diff.

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

Json = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "paid_reviews",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=True),
        sa.Column("payer", sa.String(length=64), nullable=True),
        sa.Column("free_caller", sa.String(length=64), nullable=True),
        sa.Column("repo", sa.String(length=200), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("criteria", sa.Integer(), nullable=False),
        sa.Column("files", sa.Integer(), nullable=False),
        sa.Column("additions", sa.Integer(), nullable=False),
        sa.Column("deletions", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verdict", sa.String(length=16), nullable=True),
        sa.Column("rule", sa.String(length=64), nullable=True),
        sa.Column("reviewer", sa.String(length=100), nullable=True),
        sa.Column("seconds", sa.Float(), nullable=True),
        sa.Column("cost_base_units", sa.BigInteger(), nullable=True),
        sa.Column("result", Json, nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_paid_reviews")),
    )
    op.create_index(op.f("ix_paid_reviews_request_digest"), "paid_reviews", ["request_digest"])


def downgrade() -> None:
    op.drop_index(op.f("ix_paid_reviews_request_digest"), table_name="paid_reviews")
    op.drop_table("paid_reviews")
