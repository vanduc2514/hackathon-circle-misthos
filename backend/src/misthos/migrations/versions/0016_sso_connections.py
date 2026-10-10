"""Single sign-on: an Enterprise organisation's identity provider, and its domains (#53).

Two new tables and nothing else changes. A connection's domains are rows of their own
so the primary key, not a check in the store, is what keeps one domain from routing
sign-ins to two organisations.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-10 09:00:00
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
    op.create_table(
        "sso_connections",
        sa.Column("publisher_id", sa.String(length=32), nullable=False),
        sa.Column("issuer", sa.String(length=300), nullable=False),
        sa.Column("client_id", sa.String(length=256), nullable=False),
        sa.Column("client_secret_ref", sa.String(length=128), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("configured_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["publisher_id"],
            ["publishers.id"],
            name=op.f("fk_sso_connections_publisher_id_publishers"),
        ),
        sa.PrimaryKeyConstraint("publisher_id", name=op.f("pk_sso_connections")),
    )
    op.create_table(
        "sso_domains",
        sa.Column("domain", sa.String(length=253), nullable=False),
        sa.Column("publisher_id", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["publisher_id"],
            ["sso_connections.publisher_id"],
            name=op.f("fk_sso_domains_publisher_id_sso_connections"),
        ),
        sa.PrimaryKeyConstraint("domain", name=op.f("pk_sso_domains")),
    )


def downgrade() -> None:
    op.drop_table("sso_domains")
    op.drop_table("sso_connections")
