"""Epic #11: prices follow the platform's settled history (#42) and the publisher's
own books (#43), and neither leaks what the publisher would rather keep."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from misthos.config import settings
from misthos.domain.money import Usdc
from misthos.main import app
from misthos.services.finance import Beancount, FinanceContext, FinanceError, FireflyIII
from misthos.store import store

API = "/api/v1"

# The shape of ISS-1004, which the seed settles on northwind/httpx-retry.
SETTLED_SHAPE = {
    "code_surface": 2.0,
    "requirement_clarity": 2.0,
    "test_coverage": 3.0,
    "dependency_depth": 2.0,
    "prior_attempts": 1.0,
    "blast_radius": 3.0,
}


@pytest.fixture(autouse=True)
def fresh_store():
    store.reset()
    yield
    store.finance = lambda _publisher_id: None


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def publish(client: TestClient, signals: dict[str, float], repo: str = "northwind/httpx-retry"):
    r = client.post(
        f"{API}/issues",
        json={"repo": repo, "title": "Retry on 429 too", "publisher_id": "PUB-1",
              "signals": signals},
    )  # fmt: skip
    assert r.status_code == 201, r.text
    return r.json()


class Books:
    def __init__(self, remaining: str | None, *, fail: bool = False) -> None:
        self.name = "Test Books"
        self.remaining = remaining
        self.fail = fail

    def read(self, now: datetime) -> FinanceContext:
        if self.fail:
            raise FinanceError("the books are down")
        return FinanceContext(
            source=self.name,
            budget_remaining=Usdc.from_decimal(self.remaining) if self.remaining else None,
            cash=Usdc.from_decimal("12345.67"),
            as_of=now,
        )


class TestComparablesFromSettledIssues:
    def test_work_of_a_settled_shape_is_compared_with_it(self, client: TestClient) -> None:
        proposal = publish(client, SETTLED_SHAPE)["proposal"]
        assert [c["issue_id"] for c in proposal["comparables"]] == ["ISS-1004"]
        assert proposal["confidence"] == "medium"
        assert "ISS-1004" in proposal["comparables_note"]
        assert "settled issue of similar shape" in proposal["justification"]

    def test_unlike_work_says_it_has_no_history(self, client: TestClient) -> None:
        unlike = {name: 5.0 for name in SETTLED_SHAPE}
        proposal = publish(client, unlike)["proposal"]
        assert proposal["comparables"] == []
        assert proposal["confidence"] == "low"
        assert "No settled issue of similar shape" in proposal["comparables_note"]

    def test_the_comparison_is_kept_with_the_price(self, client: TestClient) -> None:
        issue = publish(client, SETTLED_SHAPE)
        rec = store.get(issue["id"])
        assert rec is not None and rec.proposal is not None
        assert [c.issue_id for c in rec.proposal.comparables] == ["ISS-1004"]
        assert rec.proposal.comparables[0].price == store.get("ISS-1004").paid  # type: ignore[union-attr]


class TestTheBooksCapThePrice:
    def test_less_left_in_the_books_caps_the_price_and_says_where_from(
        self, client: TestClient
    ) -> None:
        store.finance = lambda pid: Books("321.00") if pid == "PUB-1" else None
        proposal = publish(client, {name: 4.0 for name in SETTLED_SHAPE})["proposal"]
        assert proposal["recommended"]["usdc"] == "321.00"
        assert "as Test Books reports it" in proposal["justification"]
        # The justification is public; what remains of the budget is not.
        assert "321" not in proposal["justification"]

    def test_the_lower_of_declared_and_read_wins(self, client: TestClient) -> None:
        store.finance = lambda pid: Books("999999.00")
        capped_by_declared = publish(client, {name: 4.0 for name in SETTLED_SHAPE})
        store.finance = lambda pid: None
        plain = publish(client, {name: 4.0 for name in SETTLED_SHAPE})
        assert capped_by_declared["proposal"]["recommended"] == plain["proposal"]["recommended"]

    def test_books_that_cannot_be_read_never_block_a_price(self, client: TestClient) -> None:
        store.finance = lambda pid: Books(None, fail=True)
        assert publish(client, SETTLED_SHAPE)["proposal"]["fundable"] is True

    @pytest.mark.parametrize(
        "reply",
        [
            lambda: httpx.Response(200, text="<html>Sign in</html>"),
            lambda: httpx.Response(200, content=b""),
            lambda: httpx.Response(200, json={"meta": {"pagination": {"total_pages": "many"}}}),
        ],
        ids=["a login page", "an empty body", "a page count that is not a number"],
    )
    def test_firefly_answering_with_something_else_never_blocks_a_price(
        self, client: TestClient, reply: Callable[[], httpx.Response]
    ) -> None:
        transport = httpx.MockTransport(lambda _request: reply())
        store.finance = lambda pid: FireflyIII("https://f.example", "pat", "3", transport=transport)
        assert publish(client, SETTLED_SHAPE)["proposal"]["fundable"] is True
        body = client.get(f"{API}/publishers/PUB-1/finance").json()
        assert body["connected"] is True and body["note"]
        assert body["caps_prices_at_usdc"] == body["declared_budget_usdc"]

    def test_a_ledger_that_cannot_be_understood_never_blocks_a_price(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        ledger = tmp_path / "latin1.beancount"
        ledger.write_bytes('2026-10-02 * "Caf\u00e9"\n'.encode("latin-1"))
        store.finance = lambda pid: Beancount(ledger, "Expenses:OpenSource", ["Assets:Bank"])
        assert publish(client, SETTLED_SHAPE)["proposal"]["fundable"] is True


class TestTheFinanceView:
    def test_shows_the_books_and_what_caps_prices(self, client: TestClient) -> None:
        store.finance = lambda pid: Books("321.00")
        body = client.get(f"{API}/publishers/PUB-1/finance").json()
        assert body["connected"] is True and body["source"] == "Test Books"
        assert body["budget_remaining_usdc"] == "321.00"
        assert body["cash_usdc"] == "12345.67"
        assert body["caps_prices_at_usdc"] == "321.00"

    def test_unconnected_books_fall_back_to_the_declared_budget(self, client: TestClient) -> None:
        body = client.get(f"{API}/publishers/PUB-1/finance").json()
        assert body["connected"] is False
        assert body["caps_prices_at_usdc"] == body["declared_budget_usdc"]
        # A plain decimal, which the web app can read, not the stored "48,500.00".
        assert "," not in body["declared_budget_usdc"]
        float(body["declared_budget_usdc"])

    def test_a_broken_connection_is_reported_not_hidden(self, client: TestClient) -> None:
        store.finance = lambda pid: Books(None, fail=True)
        body = client.get(f"{API}/publishers/PUB-1/finance").json()
        assert body["connected"] is True and "down" in body["note"]

    def test_outside_the_simulation_the_books_are_the_publishers_alone(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "simulated", False)
        assert client.get(f"{API}/publishers/PUB-1/finance").status_code == 401
