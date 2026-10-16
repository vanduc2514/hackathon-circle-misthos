"""The publisher names how long the escrow holds the money (#172).

Named at publish time in whole days, within bounds, shown to contributors before they
claim, and fixed with the money: the deadline the wallet commits is the approval plus
the term, and the term cannot move once the escrow holds it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from misthos.domain.issue import RELEASE_MARGIN, IssueState
from misthos.domain.signals import IssueFacts
from misthos.main import app
from misthos.repositories.sql import SqlRepository
from misthos.services.github import SimulatedGitHub
from misthos.store import Store, store
from misthos.workers.sweeper import sweep_once

API = "/api/v1"
REPO = "acme/widgets"
CRITERIA = ["A test reproduces the 503 from the settlement API."]


@pytest.fixture(autouse=True)
def fresh_store():
    store.reset()
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def publish(client: TestClient, number: int = 5, **extra: object):  # type: ignore[no-untyped-def]
    github = store.github
    assert isinstance(github, SimulatedGitHub)
    github.put_issue(IssueFacts(repo=REPO, number=number, title="Retry on 503", body="x"))
    return client.post(
        f"{API}/issues",
        json={"repo": REPO, "number": number, "title": "x", "publisher_id": "PUB-1",
              "price": "500", **extra},
    )  # fmt: skip


def fund(client: TestClient, iid: str) -> dict:
    approved = client.post(f"{API}/issues/{iid}/criteria", json={"criteria": CRITERIA})
    assert approved.status_code == 200, approved.text
    funded = client.post(f"{API}/issues/{iid}/fund")
    assert funded.status_code == 200, funded.text
    return funded.json()


class TestNamingIt:
    def test_a_publisher_names_the_term_and_contributors_see_it_before_claiming(
        self, client: TestClient
    ) -> None:
        r = publish(client, escrow_term_days=30)
        assert r.status_code == 201, r.text
        issue = r.json()
        assert issue["escrow_term_days"] == 30 and issue["deadline"] is None
        listed = next(i for i in client.get(f"{API}/issues").json() if i["id"] == issue["id"])
        assert listed["escrow_term_days"] == 30

    def test_no_term_named_is_fourteen_days_as_before(self, client: TestClient) -> None:
        issue = publish(client).json()
        assert issue["escrow_term_days"] == 14
        assert all(i["escrow_term_days"] == 14 for i in client.get(f"{API}/issues").json())

    @pytest.mark.parametrize(
        ("days", "said"),
        [
            (2, "at least 3 days"),
            (91, "at most 90 days"),
            (0, "3 to 90 days"),
            (-1, "3 to 90 days"),
            (7.5, "whole days"),
        ],
    )
    def test_a_term_outside_the_bounds_is_refused_with_the_bound_named(
        self, client: TestClient, days: float, said: str
    ) -> None:
        r = publish(client, escrow_term_days=days)
        assert r.status_code == 422 and said in r.json()["detail"]
        assert store.find_open(REPO, 5) is None  # nothing was listed

    def test_a_term_that_is_not_a_number_is_refused(self, client: TestClient) -> None:
        r = publish(client, escrow_term_days="a week")
        assert r.status_code == 422


class TestFixedWithTheMoney:
    def test_the_deadline_is_the_approval_plus_the_term_to_the_second(
        self, client: TestClient
    ) -> None:
        iid = publish(client, escrow_term_days=5).json()["id"]
        funded = fund(client, iid)
        rec = store.get(iid)
        assert rec is not None and rec.funding is not None and rec.escrow is not None
        assert rec.funding.deadline == (rec.funding.approved_at + timedelta(days=5)).replace(
            microsecond=0
        )
        assert rec.escrow.deadline == rec.funding.deadline
        assert datetime.fromisoformat(funded["deadline"]) == rec.funding.deadline
        assert rec.funding.deadline > rec.funding.approved_at  # never DeadlinePassed

    def test_the_term_can_move_until_the_money_is_committed(self, client: TestClient) -> None:
        iid = publish(client).json()["id"]
        moved = client.patch(f"{API}/issues/{iid}/term", json={"days": 21})
        assert moved.status_code == 200 and moved.json()["escrow_term_days"] == 21
        assert client.patch(f"{API}/issues/{iid}/term", json={"days": 1}).status_code == 422
        fund(client, iid)
        rec = store.get(iid)
        assert rec is not None and rec.funding is not None
        assert rec.funding.deadline - rec.funding.approved_at > timedelta(days=21, seconds=-1)

    def test_once_the_escrow_holds_the_money_the_term_cannot_move(
        self, client: TestClient
    ) -> None:
        iid = publish(client, escrow_term_days=10).json()["id"]
        fund(client, iid)
        before = store.get(iid)

        r = client.patch(f"{API}/issues/{iid}/term", json={"days": 60})
        assert r.status_code == 409 and "already committed" in r.json()["detail"]
        after = store.get(iid)
        assert after is not None and before is not None
        assert after.escrow_term == timedelta(days=10)
        assert after.escrow == before.escrow

    def test_the_term_survives_a_restart(self, tmp_path: Path) -> None:
        url = f"sqlite:///{tmp_path / 'term.db'}"
        first = Store(SqlRepository(url))
        first.reset()
        first.set_escrow_term("ISS-1006", 45, by="the publisher")
        assert Store(SqlRepository(url)).get("ISS-1006").escrow_term == timedelta(days=45)  # type: ignore[union-attr]


class TestAShortTerm:
    def test_a_passing_verdict_on_a_short_term_settles_before_the_deadline(
        self, client: TestClient
    ) -> None:
        """Three days is shorter than the seven-day grace, so the grace is capped; the
        release still lands inside the escrow's window, never after it."""
        iid = publish(client, escrow_term_days=3).json()["id"]
        fund(client, iid)
        store.advance(iid)  # claim
        while store.get(iid).state is not IssueState.ACCEPTED:  # type: ignore[union-attr]
            store.advance(iid)
        rec = store.get(iid)
        assert rec is not None and rec.deadline is not None

        sweep_once(store, now=rec.deadline - RELEASE_MARGIN - timedelta(minutes=1))
        assert store.get(iid).state is IssueState.ACCEPTED  # type: ignore[union-attr]
        landed = rec.deadline - RELEASE_MARGIN + timedelta(minutes=1)
        sweep_once(store, now=landed)
        assert store.get(iid).state is IssueState.PAID  # type: ignore[union-attr]
        assert landed < rec.deadline

    def test_the_money_goes_back_only_once_the_term_has_passed(self, client: TestClient) -> None:
        iid = publish(client, escrow_term_days=3).json()["id"]
        fund(client, iid)
        deadline = store.get(iid).deadline  # type: ignore[union-attr]
        assert deadline is not None
        sweep_once(store, now=deadline - timedelta(minutes=1))
        assert store.get(iid).state is IssueState.FUNDED  # type: ignore[union-attr]
        sweep_once(store, now=deadline + timedelta(minutes=1))
        assert store.get(iid).state is IssueState.REFUNDED  # type: ignore[union-attr]
