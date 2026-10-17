"""Prices the paid API quoted, with what it was asked (#203).

An agent that pays for a price is a sample of the market the engine prices, so its
question and the answer are kept, for audit and for calibration. Joined to the payment
by the edge's request digest. Appended, never edited.

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

Json = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "price_quotes",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("route", sa.String(length=64), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=True),
        sa.Column("payer", sa.String(length=64), nullable=True),
        sa.Column("free_caller", sa.String(length=64), nullable=True),
        sa.Column("inputs", Json, nullable=False),
        sa.Column("output", Json, nullable=False),
        sa.Column("quoted_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_price_quotes")),
    )
    op.create_index(op.f("ix_price_quotes_request_digest"), "price_quotes", ["request_digest"])


def downgrade() -> None:
    op.drop_index(op.f("ix_price_quotes_request_digest"), table_name="price_quotes")
    op.drop_table("price_quotes")
