"""Key an account by its party, so it can sign in with GitHub before it has a wallet.

Until #131 an account was a wallet: the address was the primary key, and every
publisher and contributor had one. Signing in with GitHub makes an account that has
no wallet yet, so:

- `accounts` is keyed by `party_id`, which was already unique and never null. The
  address becomes nullable and stays unique, and `github_id`, GitHub's numeric user
  id, is added beside the login, unique too.
- `publishers` and `contributors` may have no wallet until one is connected, so their
  `wallet_address` and `chain` become nullable.

Every row is carried over as it is. An account linked to GitHub before this keeps its
login, and its `github_id` stays null until it first signs in with GitHub, which
fills it in. A wallet-only account keeps signing in with its wallet.

SQLite cannot change a primary key in place, so the accounts table goes through
alembic's batch mode, which rebuilds it there and alters it in place on Postgres.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-09 18:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("accounts") as batch:
        # The party is the key now, so its own unique constraint would only repeat it.
        batch.drop_constraint("uq_accounts_party_id", type_="unique")
        batch.drop_constraint("pk_accounts", type_="primary")
        batch.create_primary_key("pk_accounts", ["party_id"])
        batch.alter_column("address", existing_type=sa.String(length=42), nullable=True)
        batch.add_column(sa.Column("github_id", sa.BigInteger(), nullable=True))
        batch.create_unique_constraint("uq_accounts_address", ["address"])
        batch.create_unique_constraint("uq_accounts_github_id", ["github_id"])

    for table in ("publishers", "contributors"):
        with op.batch_alter_table(table) as batch:
            batch.alter_column("wallet_address", existing_type=sa.String(length=64), nullable=True)
            batch.alter_column("chain", existing_type=sa.String(length=32), nullable=True)


def downgrade() -> None:
    # 0014 cannot hold an account or a party without a wallet. Dropping them would
    # lose people and their history, so the downgrade stops and says so instead.
    connection = op.get_bind()
    walletless = {
        table: connection.execute(
            sa.text(f"SELECT count(*) FROM {table} WHERE {column} IS NULL")
        ).scalar_one()
        for table, column in (
            ("accounts", "address"),
            ("publishers", "wallet_address"),
            ("contributors", "wallet_address"),
        )
    }
    if any(walletless.values()):
        found = ", ".join(f"{n} in {table}" for table, n in walletless.items() if n)
        raise RuntimeError(
            f"cannot downgrade below 0015: rows without a wallet ({found}) have no place "
            "in the earlier schema"
        )

    for table in ("contributors", "publishers"):
        with op.batch_alter_table(table) as batch:
            batch.alter_column("chain", existing_type=sa.String(length=32), nullable=False)
            batch.alter_column(
                "wallet_address", existing_type=sa.String(length=64), nullable=False
            )

    with op.batch_alter_table("accounts") as batch:
        batch.drop_constraint("uq_accounts_github_id", type_="unique")
        batch.drop_constraint("uq_accounts_address", type_="unique")
        batch.drop_column("github_id")
        batch.alter_column("address", existing_type=sa.String(length=42), nullable=False)
        batch.drop_constraint("pk_accounts", type_="primary")
        batch.create_primary_key("pk_accounts", ["address"])
        batch.create_unique_constraint("uq_accounts_party_id", ["party_id"])
