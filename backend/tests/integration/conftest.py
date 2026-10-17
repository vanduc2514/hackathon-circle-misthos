"""Fixtures more than one module needs."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from misthos.repositories import MemoryRepository
from misthos.repositories.sql import SqlRepository
from misthos.store import Store
from misthos.workers.sweeper import reset_review_failures
from tests.support.fake_chain import FakeChain
from tests.support.fake_compliance import FakeIdentity, FakeScreening
from tests.support.fake_github import FakeGitHub
from tests.support.seeded import SeededStore


@pytest.fixture(params=["memory", "sqlite", "postgres"])
def every_store(request: pytest.FixtureRequest, tmp_path: Path) -> Store:
    """A seeded store on the test double and on both databases.

    Postgres runs where MISTHOS_DATABASE_URL points at one, as CI's backend-postgres
    job does, and is skipped elsewhere.
    """
    if request.param == "memory":
        repo: MemoryRepository | SqlRepository = MemoryRepository()
    elif request.param == "sqlite":
        repo = SqlRepository(f"sqlite:///{tmp_path / 'misthos.db'}")
    else:
        url = os.environ.get("MISTHOS_DATABASE_URL", "")
        if not url.startswith(("postgres://", "postgresql")):
            pytest.skip("set MISTHOS_DATABASE_URL to a Postgres database to run against it")
        repo = SqlRepository(url)
    if isinstance(repo, SqlRepository):
        # The CI Postgres database is shared by the whole run. Start each test from
        # empty tables, the way the memory repository starts empty.
        from misthos.models.tables import metadata

        repo.migrate()
        with repo.engine.begin() as conn:
            for table in reversed(metadata.sorted_tables):
                conn.execute(table.delete())

    store: Store = SeededStore(
        repository=repo,
        chain=FakeChain(),
        github=FakeGitHub(),
        screening=FakeScreening(),
        identity=FakeIdentity(),
    )
    reset_review_failures()
    return store
