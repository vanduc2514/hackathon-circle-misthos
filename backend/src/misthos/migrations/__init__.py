"""Schema migrations.

Alembic owns the schema, so a deployment and a laptop reach the same tables by the
same steps. The app upgrades to head before its first query; `mise run db:migrate`
does the same from the command line, and `alembic revision --autogenerate` writes
the next step after a change to `models/tables.py`.
"""

from __future__ import annotations

import zlib

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Engine

# Held for the length of the upgrade, so an API process and a worker process that
# start together against an empty database do not both try to create it.
_MIGRATION_LOCK = zlib.crc32(b"misthos:migrations")


def upgrade_to_head(engine: Engine) -> None:
    config = Config()
    config.set_main_option("script_location", "misthos:migrations")
    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _MIGRATION_LOCK})
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
