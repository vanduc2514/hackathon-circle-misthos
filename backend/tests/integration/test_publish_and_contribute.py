"""One wallet, both sides: it publishes issues and works on other people's (#164).

Until now a wallet chose one side for good, so someone who funds fixes and also fixes
things kept two profiles with two wallets. A wallet now takes the other side too. What
does not change: nobody is paid for their own issue, because the money would go from
their funding wallet back to them through the escrow, with the review as the only
witness.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from siwe_wallet import Wallet
from test_github_native_loop import commented, comments

from misthos.domain.issue import IssueState
from misthos.domain.signals import IssueFacts
from misthos.main import app
from misthos.repositories.sql import SqlRepository
from misthos.services.github import SimulatedGitHub
from misthos.store import Store, store

API = "/api/v1"
REPO = "acme/widgets"
CRITERIA = ["A test reproduces the 503 from the settlement API."]


@pytest.fixture(autouse=True)
def fresh_store():
    store.reset()
    yield


@pytest.fixture
def github() -> SimulatedGitHub:
    assert isinstance(store.github, SimulatedGitHub)
    return store.github


def sign_in(client: TestClient, wallet: Wallet) -> None:
    nonce = client.post(f"{API}/auth/nonce").json()["nonce"]
    message = wallet.message(nonce)
    r = client.post(
        f"{API}/auth/verify", json={"message": message, "signature": wallet.sign(message)}
    )
    assert r.status_code == 200, r.text


def both_sides(client: TestClient, wallet: Wallet, login: str = "ada-dev") -> dict:
    """Sign in, work on issues first, link GitHub, then publish too."""
    sign_in(client, wallet)
    r = client.post(f"{API}/auth/role", json={"role": "contributor", "name": "ada"})
    assert r.status_code == 201, r.text
    assert client.post(f"{API}/auth/github/simulate", json={"login": login}).status_code == 200
    r = client.post(f"{API}/auth/role", json={"role": "publisher", "name": "Ada Labs"})
    assert r.status_code == 201, r.text
    return r.json()


def publish_and_fund(client: TestClient, github: SimulatedGitHub, number: int = 5) -> str:
    github.put_issue(IssueFacts(repo=REPO, number=number, title="Retry on 503", body="x"))
    r = client.post(
        f"{API}/issues",
        json={"repo": REPO, "number": number, "title": "x", "publisher_id": "PUB-1",
              "price": "500"},
    )  # fmt: skip
    assert r.status_code == 201, r.text
    iid = r.json()["id"]
    approved = client.post(f"{API}/issues/{iid}/criteria", json={"criteria": CRITERIA})
    assert approved.status_code == 200, approved.text
    assert client.post(f"{API}/issues/{iid}/fund").json()["state"] == "FUNDED"
    return iid


class TestBothSides:
    def test_a_wallet_that_works_on_issues_can_publish_one_too(self) -> None:
        client = TestClient(app)
        account = both_sides(client, Wallet(41))
        assert account["role"] == "contributor"  # the side chosen first
        assert account["contributor_id"].startswith("CON-")
        assert account["publisher_id"].startswith("PUB-")
        assert client.get(f"{API}/auth/me").json()["account"] == account

    def test_it_publishes_its_own_issue_and_claims_another_publishers(
        self, github: SimulatedGitHub
    ) -> None:
        client = TestClient(app)
        account = both_sides(client, Wallet(42))
        mine = publish_and_fund(client, github)
        assert store.get(mine).publisher_id == account["publisher_id"]  # type: ignore[union-attr]

        theirs = client.post(f"{API}/issues/ISS-1001/claim")
        assert theirs.status_code == 200, theirs.text
        assert theirs.json()["contributor_id"] == account["contributor_id"]

    def test_it_is_never_paid_for_its_own_issue(self, github: SimulatedGitHub) -> None:
        client = TestClient(app)
        both_sides(client, Wallet(43))
        mine = publish_and_fund(client, github)

        r = client.post(f"{API}/issues/{mine}/claim")
        assert r.status_code == 403 and "your own issue" in r.json()["detail"]
        assert store.get(mine).state is IssueState.FUNDED  # type: ignore[union-attr]

    def test_nor_by_claiming_it_from_github(self, github: SimulatedGitHub) -> None:
        client = TestClient(app)
        both_sides(client, Wallet(44), login="ada-dev")
        mine = publish_and_fund(client, github, number=12)

        commented(12, "ada-dev", "/misthos claim")
        assert "your own issue" in comments(github, 12)[-1]
        assert store.get(mine).state is IssueState.FUNDED  # type: ignore[union-attr]

    def test_a_contributor_still_cannot_publish_until_it_takes_that_side(self) -> None:
        client = TestClient(app)
        sign_in(client, Wallet(45))
        client.post(f"{API}/auth/role", json={"role": "contributor", "name": "bo"})
        client.post(f"{API}/auth/github/simulate", json={"login": "bo-dev"})
        r = client.post(
            f"{API}/issues",
            json={"repo": REPO, "number": 9, "title": "x", "publisher_id": "PUB-1", "price": "500"},
        )
        assert r.status_code == 403 and "only a publisher" in r.json()["detail"]


class TestKeepingBothSides:
    def test_both_sides_come_back_from_the_database(self, tmp_path: Path) -> None:
        url = f"sqlite:///{tmp_path / 'm.db'}"
        first = Store(SqlRepository(url))
        first.reset()
        first.create_account("0x" + "ab" * 20, "publisher", "Ada Labs")
        both = first.create_account("0x" + "ab" * 20, "contributor", "ada")

        restarted = Store(SqlRepository(url))
        kept = restarted.repo.get_account("0x" + "ab" * 20)
        assert kept == both and kept is not None
        assert kept.role == "publisher" and kept.publisher_id and kept.contributor_id
        for party in (kept.publisher_id, kept.contributor_id):
            assert restarted.repo.get_account_by_party(party) == kept

    def test_an_account_from_before_keeps_its_one_side(self, tmp_path: Path) -> None:
        """0016 fills in the side every existing account chose, from its role."""
        from alembic import command
        from alembic.config import Config
        from sqlalchemy import text

        repo = SqlRepository(f"sqlite:///{tmp_path / 'old.db'}")
        config = Config()
        config.set_main_option("script_location", "misthos:migrations")

        def run(step: str, revision: str) -> None:
            with repo.engine.begin() as conn:
                config.attributes["connection"] = conn
                getattr(command, step)(config, revision)

        run("upgrade", "0015")
        with repo.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO accounts (address, role, party_id, created_at) VALUES "
                    "('0x" + "cd" * 20 + "', 'contributor', 'CON-7', '2026-10-01 00:00:00')"
                )
            )
        run("upgrade", "head")
        kept = repo.get_account("0x" + "cd" * 20)
        assert kept is not None
        assert (kept.contributor_id, kept.publisher_id) == ("CON-7", None)
