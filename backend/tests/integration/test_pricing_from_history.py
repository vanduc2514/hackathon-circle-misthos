"""Epic #11: prices follow the platform's settled history (#42)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from misthos.main import app
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


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def publish(client: TestClient, signals: dict[str, float], repo: str = "northwind/httpx-retry"):
    r = client.post(
        f"{API}/issues",
        json={"repo": repo, "title": "Retry on 429 too", "publisher_id": "PUB-1",
              "price": "500", "signals": signals},
    )  # fmt: skip
    assert r.status_code == 201, r.text
    return r.json()


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
