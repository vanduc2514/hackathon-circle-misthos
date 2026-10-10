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
from misthos.schemas import Account, RepoConnection, Wallet
from misthos.services.chain import ChainRevert
from misthos.store import Store


def postgres_url() -> str:
    url = os.environ.get("MISTHOS_DATABASE_URL", "")
    if not url.startswith(("postgres://", "postgresql")):
        pytest.skip("set MISTHOS_DATABASE_URL to a Postgres database to run against it")
    return url


@pytest.fixture(params=["sqlite", "postgres"])
def database_url(request: pytest.FixtureRequest, tmp_path: Path) -> str:
    if request.param == "sqlite":
        return f"sqlite:///{tmp_path / 'misthos.db'}"
    return postgres_url()


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


@pytest.fixture(params=["memory", "sqlite", "postgres"])
def every_store(request: pytest.FixtureRequest, tmp_path: Path) -> Store:
    """The test double and both databases, for behaviour the store relies on from all three."""
    repo = (
        MemoryRepository()
        if request.param == "memory"
        else SqlRepository(
            f"sqlite:///{tmp_path / 'misthos.db'}" if request.param == "sqlite" else postgres_url()
        )
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

    def test_the_approved_terms_the_circle_wallet_and_the_escrow_deadline_survive(
        self, database_url: str
    ) -> None:
        """Booking checks the approved terms (#126), the simulated escrow judges release
        and refund by its deadline (#121), and a publisher's Circle wallet sits beside
        the wallet they fund from (#123). A restart must lose none of the three."""
        first = fresh(database_url)
        funded = first.advance("ISS-1006")
        circle = Wallet(address="0x" + "c1" * 20, chain="arc-testnet")
        first.link_wallet("publisher", funded.publisher_id, circle)

        second = Store(SqlRepository(database_url))
        again = second.get("ISS-1006")
        assert again is not None and funded.funding is not None
        assert again.funding == funded.funding
        held = second.chain.commitment("ISS-1006")
        assert held is not None and held.deadline == funded.deadline
        publisher = second.get_publisher(funded.publisher_id)
        assert publisher is not None and publisher.circle_wallet == circle
        assert publisher.wallet.address == funded.funding.wallet

        assert funded.deadline is not None and funded.contributor_id is None
        with pytest.raises(ChainRevert, match="DeadlinePassed"):
            second.chain.release(
                "ISS-1006", "0xC0FE", funded.funding.amount, funded.deadline + timedelta(seconds=1)
            )

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
        # Saved as new rows. The version is the repository's to number: it carries on
        # from every version this database handed out before, so compare the copies
        # it numbered.
        saved = [replace(rec, version=0) for rec in built.list_issues()]
        for rec in saved:
            repo.save_issues(rec)

        for rec in saved:
            assert rec.version > 0
            assert repo.get_issue(rec.id) == rec, rec.id
        assert repo.list_publishers() == sorted(built.list_publishers(), key=lambda p: p.id)
        assert repo.list_contributors() == sorted(built.list_contributors(), key=lambda c: c.id)

    def test_checks_that_have_not_reported_stay_unknown_through_a_restart(
        self, database_url: str
    ) -> None:
        """Read back as False, they would make the review send good work back."""
        first = fresh(database_url)
        rec = first.get("ISS-1002")  # seeded in review
        assert rec is not None and rec.submission is not None
        rec.submission = rec.submission.model_copy(
            update={"head_sha": "c" * 40, "checks_passed": None}
        )
        first.save(rec)

        again = Store(SqlRepository(database_url)).get("ISS-1002")
        assert again is not None and again.submission is not None
        assert again.submission.head_sha == "c" * 40
        assert again.submission.checks_passed is None

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

    def test_a_spending_policy_round_trips(self, any_store: Store) -> None:
        saved = any_store.set_policy(
            "PUB-1",
            approval_threshold_usdc="1500",
            approvers=["dana-acme"],
            category_limits={"security": "12000.5"},
        )
        again = any_store.get_publisher("PUB-1")
        assert again == saved
        assert again is not None and again.category_limits == {"security": "12,000.50"}

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


AT = datetime(2026, 10, 8, 9, 30, tzinfo=UTC)


def connection(
    repo: str, installation: int = 42, by: str = "acme-maint", publisher: str | None = "PUB-1"
) -> RepoConnection:
    return RepoConnection(
        repo=repo,
        installation_id=installation,
        installed_by=by,
        publisher_id=publisher,
        connected_at=AT,
    )


class TestGitHubConnections:
    """The repositories the GitHub App is installed on, and whose they are (#6). GitHub
    spells a repository and a login in whatever case its owner chose, and a webhook
    and a person may spell them differently, so neither lookup depends on it."""

    @pytest.fixture
    def three(self, every_store: Store) -> Store:
        for c in (
            connection("acme/widgets"),
            connection("acme/gears"),
            connection("other/thing", installation=7, by="other-maint", publisher="PUB-2"),
        ):
            every_store.repo.save_connection(c)
        return every_store

    @staticmethod
    def repos(store: Store) -> list[str]:
        return sorted(c.repo for c in store.repo.list_connections())

    def test_a_connection_is_found_whatever_the_case_of_its_name(self, every_store: Store) -> None:
        every_store.repo.save_connection(connection("acme/widgets"))
        assert every_store.repo.get_connection("Acme/Widgets") == connection("acme/widgets")
        assert every_store.repo.get_connection("acme/gears") is None

    def test_saving_a_repository_again_moves_it_to_the_new_installation_and_publisher(
        self, every_store: Store
    ) -> None:
        every_store.repo.save_connection(connection("acme/widgets"))
        moved = connection("Acme/Widgets", installation=43, by="other-maint", publisher="PUB-2")
        every_store.repo.save_connection(moved)
        expected = connection("acme/widgets", installation=43, by="other-maint", publisher="PUB-2")
        assert every_store.repo.list_connections() == [expected]
        assert every_store.repo.get_connection("acme/widgets") == expected

    def test_an_account_is_found_by_its_github_login_whatever_the_case(
        self, every_store: Store
    ) -> None:
        every_store.repo.save_account(
            Account(
                address="0x" + "ab" * 20,
                role="publisher",
                party_id="PUB-1",
                github_login="Acme-Maint",
                created_at=AT,
            )
        )
        found = every_store.repo.get_account_by_github_login("acme-MAINT")
        assert found is not None and found.party_id == "PUB-1"
        assert found.github_login == "Acme-Maint"
        assert every_store.repo.get_account_by_github_login("someone-else") is None

    def test_a_publisher_lists_only_its_own_repositories(self, three: Store) -> None:
        assert [c.repo for c in three.connections_of("PUB-1")] == ["acme/gears", "acme/widgets"]
        assert [c.repo for c in three.connections_of("PUB-2")] == ["other/thing"]
        assert three.connections_of("PUB-3") == []

    def test_uninstalling_disconnects_only_that_installations_repositories(
        self, three: Store
    ) -> None:
        assert sorted(three.disconnect_repositories(installation_id=42)) == [
            "acme/gears",
            "acme/widgets",
        ]
        assert self.repos(three) == ["other/thing"]

    def test_removed_repositories_are_disconnected_whatever_the_case_of_their_names(
        self, three: Store
    ) -> None:
        three.disconnect_repositories(["Acme/Widgets", "OTHER/thing"])
        assert self.repos(three) == ["acme/gears"]

    def test_an_empty_list_or_no_installation_disconnects_nothing(self, three: Store) -> None:
        three.repo.delete_connections([])
        assert three.disconnect_repositories([]) == []
        assert three.disconnect_repositories(installation_id=None) == []
        # An empty list of removed repositories is not an uninstall.
        assert three.disconnect_repositories([], installation_id=42) == []
        assert self.repos(three) == ["acme/gears", "acme/widgets", "other/thing"]


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

    def test_every_account_is_kept_when_accounts_stop_being_wallets(
        self, database_url: str
    ) -> None:
        """0015 (#131) keys an account by its party. An account linked to GitHub and a
        wallet-only one, as 0014 held them, both come through and sign in as before;
        the linked one gets its GitHub id at its first GitHub sign-in."""
        from alembic import command
        from alembic.config import Config
        from sqlalchemy import DateTime, column, table

        repo = SqlRepository(database_url)
        config = Config()
        config.set_main_option("script_location", "misthos:migrations")

        def run(step: str, revision: str) -> None:
            with repo.engine.begin() as conn:
                config.attributes["connection"] = conn
                getattr(command, step)(config, revision)

        run("upgrade", "head")
        repo.reset()
        run("downgrade", "0014")
        linked, wallet_only = "0x" + "a1" * 20, "0x" + "b2" * 20
        with repo.engine.begin() as conn:
            conn.execute(
                insert(tables.publishers).values(
                    id="PUB-L", name="Linked Co", kind="company", tier="open",
                    wallet_address=linked, chain="arc-testnet",
                    budget_remaining_base_units=5_000_000_000, approvers=[],
                    category_limits={},
                )
            )  # fmt: skip
            conn.execute(
                insert(tables.contributors).values(
                    id="CON-W", handle="wallet-only", wallet_address=wallet_only,
                    chain="arc-testnet", reputation=0, settled_issues=0, earned_base_units=0,
                    identity_status="unverified",
                )
            )  # fmt: skip
            # The accounts table as 0014 has it, keyed by address and with no GitHub id.
            accounts_0014 = table(
                "accounts",
                column("address"), column("role"), column("party_id"), column("github_login"),
                column("created_at", DateTime(timezone=True)),
            )  # fmt: skip
            for address, role, party, login in (
                (linked, "publisher", "PUB-L", "linked-maint"),
                (wallet_only, "contributor", "CON-W", None),
            ):
                conn.execute(
                    insert(accounts_0014).values(
                        address=address, role=role, party_id=party, github_login=login,
                        created_at=AT,
                    )
                )  # fmt: skip
        run("upgrade", "head")

        assert repo.get_account("PUB-L") == Account(
            address=linked, role="publisher", party_id="PUB-L", github_login="linked-maint",
            created_at=AT,
        )  # fmt: skip
        assert repo.get_account_by_address(wallet_only) == Account(
            address=wallet_only, role="contributor", party_id="CON-W", created_at=AT
        )
        assert repo.get_account_by_github_login("Linked-Maint") == repo.get_account("PUB-L")
        publisher = repo.get_publisher("PUB-L")
        assert publisher is not None and publisher.wallet == Wallet(
            address=linked, chain="arc-testnet"
        )

        store = Store(repo)
        found = store.sign_in_with_github(4242, "linked-maint")
        assert found is not None and found.party_id == "PUB-L" and found.github_id == 4242
        assert store.account_by_github_id(4242) == found
        # The wallet-only account still signs in with its wallet.
        assert store.account_by_address(wallet_only.upper().replace("0X", "0x")) is not None
        # And the new schema holds what the old one could not: an account with no wallet.
        new = store.create_account(None, "contributor", "x", github_id=43, github_login="new-dev")
        assert new.address is None
        contributor = repo.get_contributor(new.party_id)
        assert contributor is not None and contributor.wallet is None

        # Which 0014 has no place for, so going back stops rather than drop it.
        # The revisions above 0015 go first, in a step of their own: SQLite commits DDL
        # as it runs, so undoing them inside the refused step would leave their tables
        # dropped under a version that still names them.
        run("downgrade", "0015")
        with pytest.raises(RuntimeError, match="rows without a wallet"):
            run("downgrade", "0014")
        run("upgrade", "head")
        repo.reset()


class TestEscrowCeilings:
    def test_an_approved_ceiling_survives_a_restart(self, database_url: str) -> None:
        # The simulated escrow refuses a commitment without a ceiling, so a restart
        # that forgot the ceilings would make every approved issue unfundable.
        from misthos.domain.money import Usdc

        first = fresh(database_url)
        funded = next(r for r in first.repo.list_issues() if r.state is IssueState.FUNDED)
        ceiling = first.chain.escrow_ceiling(funded.id)
        assert ceiling is not None and ceiling == funded.proposal.recommended

        again = Store(SqlRepository(database_url))
        assert again.chain.escrow_ceiling(funded.id) == ceiling
        assert again.chain.escrow_ceiling("ISS-NEVER") is None
        assert isinstance(ceiling, Usdc)
