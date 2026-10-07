"""Persistence boundary.

The store makes every lifecycle decision and a repository makes none: it loads an
issue aggregate, saves it whole, and refuses a save made from a copy someone else
has saved since. Two implementations sit behind one protocol. The memory repository
keeps `mise run dev` free of infrastructure and doubles as the test double; the SQL
repository is Postgres in a deployment and a SQLite file for durable local runs.
"""

from __future__ import annotations

from misthos.repositories.base import AppendOnlyViolation, Repository, StaleIssue
from misthos.repositories.memory import MemoryRepository


def build_repository(database_url: str) -> Repository:
    """Memory when no database is configured, so the demo still needs nothing."""
    if not database_url:
        return MemoryRepository()
    # Imported here so the zero-config path never loads a database driver.
    from misthos.repositories.sql import SqlRepository

    return SqlRepository(database_url)


__all__ = [
    "AppendOnlyViolation",
    "MemoryRepository",
    "Repository",
    "StaleIssue",
    "build_repository",
]
