"""The paid pricing APIs (#203): a price for an issue the caller describes, and the
settled history behind it, as the core answers them to the edge.

The price must be the engine's own answer for the same inputs, honest about thin
history, and free of anything a publisher keeps private. Every refusal is uncharged.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from misthos.auth import sessions
from misthos.config import settings
from misthos.domain.pricing import propose, suggested_range
from misthos.domain.signals import IssueFacts
from misthos.domain.signals import read as read_signals
from misthos.main import app
from misthos.services import paid_pricing
from misthos.store import Store, store
from tests.support.accounts import account_for

TOKEN = "edge-core-token-for-tests"
EDGE = {"X-Misthos-Core-Token": TOKEN}
PAID = "/api/v1/internal/paid"
PAYER = "0x2222222222222222222222222222222222222222"
DIGEST = "d" * 64

ASKED = {
    "title": "Retry webhook deliveries that time out",
    "body": (
        "Deliveries that time out in `src/hooks/deliver.py` are dropped.\n\n"
        "Steps to reproduce: point a hook at a slow endpoint. Expected: the delivery is "
        "retried with backoff. Actual: it is dropped and nothing is logged."
    ),
    "repo": "northwind/httpx-retry",
    "labels": ["bug", "reliability"],
}


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def edge_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "edge_core_token", TOKEN)


def ask(client: TestClient, body: object, **headers: str):
    return client.post(
        f"{PAID}/price",
        json=body,
        headers={
            **EDGE,
            "X-Misthos-Paid-Route": "price",
            "X-Misthos-Payer": PAYER,
            "X-Misthos-Request-Digest": DIGEST,
            **headers,
        },
    )


def keys_of(value: object) -> list[str]:
    """Every key in a JSON document, however deep."""
    if isinstance(value, dict):
        return [k for k, v in value.items() for k in [k, *keys_of(v)]]
    if isinstance(value, list):
        return [k for item in value for k in keys_of(item)]
    return []


def settled_signals(issue_id: str) -> dict[str, float]:
    rec = store.get(issue_id)
    assert rec is not None and rec.proposal is not None
    return dict(rec.proposal.signals)


class TestAPrice:
    def test_is_the_engines_own_answer_for_the_same_inputs(self, client):
        body = ask(client, ASKED).json()
        facts = IssueFacts(
            repo=ASKED["repo"],
            number=0,
            title=ASKED["title"],
            body=ASKED["body"],
            labels=tuple(ASKED["labels"]),
        )
        reading = read_signals(facts)
        engine = propose(
            reading.signals, comparables=store._comparables_for(reading.signals, facts.repo)
        )
        low, high = suggested_range(engine)
        assert body["recommended"]["base_units"] == engine.recommended.base_units
        assert body["band"]["low"]["base_units"] == low.base_units
        assert body["band"]["high"]["base_units"] == high.base_units
        assert body["confidence"] == engine.confidence
        assert body["justification"] == engine.justification
        assert body["complexity_score"] == engine.complexity_score
        assert {k: v["score"] for k, v in body["signals"].items()} == engine.signals
        assert all(v["source"] == "read" and v["reason"] for v in body["signals"].values())

    def test_names_every_comparable_that_moved_it(self, client):
        signals = settled_signals("ISS-1004")
        body = ask(client, {**ASKED, "signals": signals}).json()
        assert body["comparables_used"] >= 1
        first = body["comparables"][0]
        assert first["issue_id"] == "ISS-1004"
        assert first["distance"] == 0
        assert first["same_repo"] is True
        assert first["implied"]["base_units"] == first["price"]["base_units"]
        assert body["confidence"] == "medium"
        assert "evidence, not yet a market" in body["confidence_reason"]
        assert all(v["source"] == "given" for v in body["signals"].values())

    def test_says_so_when_there_is_no_history(self, client):
        lonely = {name: 5.0 for name in paid_pricing.SIGNALS}
        body = ask(client, {**ASKED, "repo": "someone/new-repo", "signals": lonely}).json()
        assert body["comparables_used"] == 0 and body["comparables"] == []
        assert body["confidence"] == "low"
        assert "signals alone" in body["confidence_reason"]
        assert body["repo"] == "someone/new-repo"

    def test_takes_a_github_link_for_the_repo(self, client):
        body = ask(client, {**ASKED, "repo": "https://github.com/Northwind/httpx-retry/issues/9"})
        assert body.status_code == 200
        assert body.json()["repo"] == "Northwind/httpx-retry"

    def test_sets_given_signals_over_the_ones_it_reads(self, client):
        body = ask(client, {**ASKED, "signals": {"blast_radius": 5}}).json()
        assert body["signals"]["blast_radius"] == {"score": 5.0, "source": "given", "reason": None}
        assert body["signals"]["code_surface"]["source"] == "read"

    @pytest.mark.parametrize(
        ("change", "error"),
        [
            ({"signals": {"vibes": 3}}, "bad_signals"),
            ({"signals": {"code_surface": 6}}, "bad_signals"),
            ({"signals": {"code_surface": 0.5}}, "bad_signals"),
            ({"signals": {"code_surface": "lots"}}, "bad_request"),
            ({"repo": "not a repo"}, "bad_repo"),
            ({"repo": "https://gitlab.com/a/b"}, "bad_repo"),
            ({"title": ""}, "bad_request"),
            ({"budget": "48,500.00"}, "bad_request"),
            ({"labels": ["x" * 51]}, "bad_request"),
        ],
    )
    def test_refuses_what_it_cannot_price(self, client, change, error):
        res = ask(client, {**ASKED, **change})
        assert res.status_code == 400
        assert res.json()["error"] == error
        assert "pydantic" not in res.text

    def test_refuses_a_body_that_is_not_json(self, client):
        res = client.post(
            f"{PAID}/price",
            content=b"title=x",
            headers={**EDGE, "Content-Type": "application/json"},
        )
        assert (res.status_code, res.json()["error"]) == (400, "bad_request")

    def test_carries_no_budget_and_no_policy(self, client):
        res = ask(client, {**ASKED, "signals": settled_signals("ISS-1004")})
        text = res.text.lower()
        for publisher in store.repo.list_publishers():
            assert publisher.budget_remaining_usdc.lower() not in text
            assert publisher.wallet.address.lower() not in text
        for key in keys_of(res.json()):
            for word in ("budget", "policy", "approver", "threshold", "wallet", "limit"):
                assert word not in key.lower(), key

    def test_lists_nothing_and_changes_no_issue(self, client):
        before = {r.id: r.version for r in store.repo.list_issues()}
        ask(client, ASKED)
        assert {r.id: r.version for r in store.repo.list_issues()} == before

    def test_keeps_the_quote_against_the_payment(self, client):
        body = ask(client, ASKED).json()
        [quote] = store.repo.list_price_quotes()
        assert quote.request_digest == DIGEST
        assert quote.payer == PAYER
        assert quote.free_caller is None
        assert quote.inputs["title"] == ASKED["title"]
        assert quote.output["recommended"] == body["recommended"]
        assert quote.id.startswith("PQ-")


def test_every_repository_keeps_price_quotes(every_store: Store):
    from datetime import UTC, datetime, timedelta

    from misthos.domain.paid import PriceQuote

    at = datetime(2026, 10, 1, tzinfo=UTC)
    for i in range(3):
        every_store.repo.record_price_quote(
            PriceQuote(
                id=f"PQ-{i:06d}",
                route="price",
                request_digest=DIGEST if i else None,
                payer=PAYER if i else None,
                free_caller=None if i else "publisher:PUB-1",
                inputs={"title": f"t{i}", "labels": ["a"]},
                output={"recommended": {"base_units": i}},
                quoted_at=at + timedelta(minutes=i),
            )
        )
    quotes = every_store.repo.list_price_quotes()
    assert [q.id for q in quotes] == ["PQ-000002", "PQ-000001", "PQ-000000"]
    assert quotes[0].inputs == {"title": "t2", "labels": ["a"]}
    assert quotes[-1].free_caller == "publisher:PUB-1" and quotes[-1].payer is None


class TestTheComparables:
    def get(self, client: TestClient, **query: str):
        return client.get(f"{PAID}/comparables", params=query, headers=EDGE)

    def test_without_signals_is_every_settlement_newest_first(self, client):
        body = self.get(client).json()
        settled = sorted(store.settled_history(), key=lambda w: w.settled_at, reverse=True)
        assert [c["issue_id"] for c in body["comparables"]] == [w.issue_id for w in settled]
        assert body["total"] == len(settled) and body["next"] is None
        assert body["order"] == "newest first"
        first = body["comparables"][0]
        assert {"price", "settled_at", "repo", "distance", "implied"} <= set(first)

    def test_filters_by_repo_and_labels(self, client):
        assert [
            c["issue_id"] for c in self.get(client, repo="contoso/schema-cli").json()["comparables"]
        ] == ["ISS-1005"]
        assert [
            c["issue_id"] for c in self.get(client, labels="bug, Reliability").json()["comparables"]
        ] == ["ISS-1004"]
        assert self.get(client, labels="bug,dx").json()["comparables"] == []

    def test_with_signals_is_the_engines_comparables_closest_first(self, client):
        signals = {k: str(v) for k, v in settled_signals("ISS-1004").items()}
        body = self.get(client, **signals).json()
        assert body["order"] == "closest first" and body["window_days"] == 365
        first = body["comparables"][0]
        assert first["issue_id"] == "ISS-1004" and first["distance"] == 0
        assert first["implied"]["base_units"] == first["price"]["base_units"]

    def test_pages_with_a_cursor(self, client):
        first = self.get(client, limit="1").json()
        assert len(first["comparables"]) == 1 and first["next"]
        second = self.get(client, limit="1", cursor=first["next"]).json()
        assert second["comparables"][0]["issue_id"] != first["comparables"][0]["issue_id"]
        assert second["next"] is None

    def test_an_unknown_repo_has_no_history(self, client):
        body = self.get(client, repo="someone/new-repo").json()
        assert (body["comparables"], body["total"]) == ([], 0)

    @pytest.mark.parametrize(
        ("query", "error"),
        [
            ({"budget": "1"}, "unknown_filter"),
            ({"publisher_id": "PUB-1"}, "unknown_filter"),
            ({"code_surface": "2"}, "bad_signals"),
            ({**{name: "2" for name in paid_pricing.SIGNALS}, "blast_radius": "9"}, "bad_signals"),
            ({**{name: "x" for name in paid_pricing.SIGNALS}}, "bad_signals"),
            ({"limit": "0"}, "bad_limit"),
            ({"limit": "101"}, "bad_limit"),
            ({"limit": "many"}, "bad_limit"),
            ({"cursor": "nope"}, "bad_cursor"),
            ({"repo": "not a repo"}, "bad_repo"),
        ],
    )
    def test_refuses_what_it_does_not_know(self, client, query, error):
        res = self.get(client, **query)
        assert res.status_code == 400
        assert res.json()["error"] == error


class TestThePreview:
    def free(self, client: TestClient, route: str, party_id: str | None) -> bool:
        headers = dict(EDGE)
        if party_id:
            account = account_for(party_id)
            store.repo.save_account(account)
            headers["X-Misthos-Caller-Authorization"] = f"Bearer {sessions.issue(account.address)}"
        res = client.post(f"{PAID}/access", json={"route": route}, headers=headers)
        return res.json()["free"]

    @pytest.mark.parametrize("route", ["price", "comparables"])
    def test_is_free_to_a_signed_in_publisher_only(self, client, route):
        assert self.free(client, route, "PUB-1") is True
        assert self.free(client, route, "CON-1") is False
        assert self.free(client, route, None) is False

    def test_a_free_quote_is_kept_against_the_publisher(self, client):
        ask(client, ASKED, **{"X-Misthos-Payer": "", "X-Misthos-Free-Caller": "publisher:PUB-1"})
        [quote] = store.repo.list_price_quotes()
        assert (quote.payer, quote.free_caller) == (None, "publisher:PUB-1")
