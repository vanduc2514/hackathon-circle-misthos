"""Alembic environment.

Run in-process by `upgrade_to_head`, which hands over its own connection, or from
the command line, which reads the database from MISTHOS_DATABASE_URL like the app.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, create_engine, pool

from misthos.config import settings
from misthos.models.tables import metadata
from misthos.repositories.sql import normalise_url

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _url() -> str:
    url = settings.database_url or config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError("set MISTHOS_DATABASE_URL to the database to migrate")
    return normalise_url(url)


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=metadata,
        # SQLite cannot ALTER most things in place; batch mode rebuilds the table.
        render_as_batch=connection.dialect.name == "sqlite",
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    shared = config.attributes.get("connection")
    if shared is not None:
        _run(shared)
        return
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.begin() as connection:
        _run(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
