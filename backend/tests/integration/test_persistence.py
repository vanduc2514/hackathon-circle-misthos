"""Durability: the lifecycle against a real database, and across a restart.

Every behaviour runs against SQLite in a temporary file. Set MISTHOS_DATABASE_URL to
a Postgres database, as the backend-postgres CI job does, and the same tests run
against Postgres too; the rest of the suite then runs against it as well.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError

from misthos.domain.issue import IssueState
from misthos.domain.money import Usdc
from misthos.models import tables
from misthos.repositories import AppendOnlyViolation, MemoryRepository, StaleIssue
from misthos.repositories.sql import SqlRepository
from misthos.store import Store


@pytest.fixture(params=["sqlite", "postgres"])
def database_url(request: pytest.FixtureRequest, tmp_path: Path) -> str:
    if request.param == "sqlite":
        return f"sqlite:///{tmp_path / 'misthos.db'}"
    url = os.environ.get("MISTHOS_DATABASE_URL", "")
    if not url.startswith(("postgres://", "postgresql")):
        pytest.skip("set MISTHOS_DATABASE_URL to a Postgres database to run against it")
    return url


@pytest.fixture(params=["memory", "sql"])
def any_store(request: pytest.FixtureRequest, tmp_path: Path) -> Store:
    """Behaviour every repository owes the store, the test double included."""
    repo = (
        MemoryRepository()
        if request.param == "memory"
        else SqlRepository(f"sqlite:///{tmp_path / 'misthos.db'}")
    )
    store = Store(repo)
    store.reset()
    return store


def fresh(url: str) -> Store:
    store = Store(SqlRepository(url))
    store.reset()
    return store


class TestRestart:
    def test_a_settlement_survives_a_restart(self, database_url: str) -> None:
        first = fresh(database_url)
        paid = first.approve_and_accept("ISS-1006")
        assert paid.state is IssueState.PAID

        # A new process: a new engine and a new store, sharing only the database.
        second = Store(SqlRepository(database_url))
        again = second.get("ISS-1006")

        assert again is not None
        assert again.state is IssueState.PAID
        assert again.paid == paid.paid
        assert again.escrow is not None and again.escrow.released
        assert [d.id for d in again.decisions] == [d.id for d in paid.decisions]
        assert second.metrics().settled_issues == first.metrics().settled_issues

    def test_the_ledger_and_the_escrow_books_survive_a_restart(self, database_url: str) -> None:
        """Otherwise every settled issue would look divergent after a deploy."""
        first = fresh(database_url)
        paid = first.approve_and_accept("ISS-1006")

        second = Store(SqlRepository(database_url))
        again = second.get("ISS-1006")
        assert again is not None
        assert again.money_events == paid.money_events
        assert second.reconcile() == []

    def test_a_database_that_holds_issues_is_not_reseeded(self, database_url: str) -> None:
        first = fresh(database_url)
        first.advance("ISS-1006")

        second = Store(SqlRepository(database_url))
        assert second.count_issues() == 8
        again = second.get("ISS-1006")
        assert again is not None and again.state is IssueState.FUNDED

    def test_every_seeded_issue_round_trips_through_the_database_intact(
        self, database_url: str
    ) -> None:
        """What the store builds is exactly what a later process reads back."""
        built = Store(MemoryRepository())
        built.reset()
        repo = SqlRepository(database_url)
        repo.reset()
        for publisher in built.list_publishers():
            repo.save_publisher(publisher)
        for contributor in built.list_contributors():
            repo.save_contributor(contributor)
        originals = built.list_issues()
        for rec in originals:
            repo.save_issues(replace(rec, version=0))

        for rec in originals:
            assert repo.get_issue(rec.id) == replace(rec, version=1), rec.id
        assert repo.list_publishers() == sorted(built.list_publishers(), key=lambda p: p.id)
        assert repo.list_contributors() == sorted(built.list_contributors(), key=lambda c: c.id)

    def test_new_ids_carry_on_after_a_restart(self, database_url: str) -> None:
        """The id sequences live in the database, so a second process cannot reuse one."""
        fresh(database_url)
        second = Store(SqlRepository(database_url))
        before = {d.id for d in second.list_decisions(500)}
        rec = second.advance("ISS-1006")
        assert rec.decisions[-1].id not in before


class TestSaving:
    def test_a_save_from_a_stale_copy_is_refused(self, any_store: Store) -> None:
        mine = any_store.get("ISS-1006")
        theirs = any_store.get("ISS-1006")
        assert mine is not None and theirs is not None
        any_store.save(theirs)
        with pytest.raises(StaleIssue):
            any_store.save(mine)

    def test_a_failed_save_writes_nothing(self, any_store: Store) -> None:
        """Saving two issues is all or nothing, so a relist never outlives its refund."""
        fresh_one = any_store.get("ISS-1001")
        stale = any_store.get("ISS-1006")
        assert fresh_one is not None and stale is not None
        any_store.save(any_store.get("ISS-1006"))  # moves ISS-1006 on
        fresh_one.title = "renamed"
        with pytest.raises(StaleIssue):
            any_store.save(fresh_one, stale)
        assert any_store.get("ISS-1001").title != "renamed"  # type: ignore[union-attr]

    def test_a_webhook_delivery_is_recorded_once(self, any_store: Store) -> None:
        at = datetime(2026, 10, 7, tzinfo=UTC)
        assert any_store.repo.record_delivery("d-1", "pull_request", at)
        assert not any_store.repo.record_delivery("d-1", "pull_request", at)
        any_store.repo.forget_delivery("d-1")
        assert any_store.repo.record_delivery("d-1", "pull_request", at)

    def test_the_decision_log_is_append_only(self, any_store: Store) -> None:
        rec = any_store.get("ISS-1002")
        assert rec is not None
        rec.decisions.pop()
        with pytest.raises(AppendOnlyViolation):
            any_store.save(rec)


class TestSchema:
    def test_two_active_claims_on_one_issue_are_refused(self, database_url: str) -> None:
        """Two processes racing a claim cannot both win: the index decides."""
        store = fresh(database_url)
        repo = store.repo
        assert isinstance(repo, SqlRepository)
        now = datetime.now(UTC)
        claim = {"issue_id": "ISS-1001", "issued_at": now, "expires_at": now + timedelta(days=3)}
        with pytest.raises(IntegrityError), repo.engine.begin() as conn:
            conn.execute(insert(tables.claims).values(contributor_id="CON-1", active=True, **claim))
            conn.execute(insert(tables.claims).values(contributor_id="CON-2", active=True, **claim))

    def test_a_changed_price_is_a_new_proposal_not_an_edit(self, database_url: str) -> None:
        """Overrides are the pricing engine's best training signal, so none is lost."""
        store = fresh(database_url)
        rec = store.get("ISS-1006")
        assert rec is not None and rec.proposal is not None
        rec.proposal = replace(rec.proposal, recommended=Usdc.from_decimal("999"))
        store.save(rec)

        repo = store.repo
        assert isinstance(repo, SqlRepository)
        with repo.engine.connect() as conn:
            count = conn.execute(
                select(func.count())
                .select_from(tables.price_proposals)
                .where(tables.price_proposals.c.issue_id == "ISS-1006")
            ).scalar_one()
        assert count == 2
        again = store.get("ISS-1006")
        assert again is not None and again.proposal is not None
        assert again.proposal.recommended == Usdc.from_decimal("999")


class TestMigrations:
    def test_verified_contributors_keep_their_status_through_the_identity_migration(
        self, database_url: str
    ) -> None:
        """0002 turns the old boolean into the provider's outcome without losing anyone."""
        from alembic import command
        from alembic.config import Config
        from sqlalchemy import text

        repo = SqlRepository(database_url)
        config = Config()
        config.set_main_option("script_location", "misthos:migrations")

        def run(step: str, revision: str) -> None:
            with repo.engine.begin() as conn:
                config.attributes["connection"] = conn
                getattr(command, step)(config, revision)

        run("upgrade", "head")
        repo.reset()  # the downgrade below must not trip over rows from other tests
        run("downgrade", "0001")
        with repo.engine.begin() as conn:
            for cid, verified in (("CON-A", True), ("CON-B", False)):
                conn.execute(
                    text(
                        "INSERT INTO contributors (id, handle, wallet_address, chain, "
                        "reputation, settled_issues, earned_base_units, verified) VALUES "
                        "(:id, :id, '0x0', 'arc-testnet', 0, 0, 0, :verified)"
                    ),
                    {"id": cid, "verified": verified},
                )
        run("upgrade", "head")

        statuses = {c.id: c.identity_status for c in repo.list_contributors()}
        assert statuses == {"CON-A": "verified", "CON-B": "unverified"}
        repo.reset()
