"""The core's side of the paid API (#201): only the edge gets in, and it only reads.

The edge sells x402 calls and forwards each one here. These tests hold the core to its
half of the contract: a caller without the edge's token finds nothing, a session is
free only where a route's rule says so, every settlement is written down once, and
the books add up to what was settled.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from misthos.auth import sessions
from misthos.config import settings
from misthos.domain.money import Usdc
from misthos.domain.paid import PaidCall, RouteTotal, totals
from misthos.main import app
from misthos.services import paid as paid_rules
from misthos.store import Store, store
from tests.support.accounts import account_for

TOKEN = "edge-core-token-for-tests"
EDGE = {"X-Misthos-Core-Token": TOKEN}
PAYER = "0x2222222222222222222222222222222222222222"
USDC = "0x3600000000000000000000000000000000000000"
ARC_TESTNET = "eip155:5042002"
PAID_PREFIX = "/api/v1/internal/paid/"


def routes_of(routes, prefix: str = ""):
    """Every (path, endpoint) the app serves, through routers included in routers."""
    for route in routes:
        context = getattr(route, "include_context", None)
        if context is not None:
            yield from routes_of(context.included_router.routes, prefix + context.prefix)
        elif getattr(route, "endpoint", None) is not None:
            yield prefix + route.path, route.endpoint


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def edge_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "edge_core_token", TOKEN)


@pytest.fixture
def free_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """A route free to the publisher a `publisher_id` parameter names."""
    monkeypatch.setitem(
        paid_rules.FREE_TIER,
        "lookup",
        lambda account, params: (
            account.role == "publisher" and account.party_id == params.get("publisher_id")
        ),
    )


def settlement(**overrides: object) -> dict:
    body = {
        "route": "lookup",
        "request_digest": "a" * 64,
        "payer": PAYER,
        "amount_base_units": 10_000,
        "network": ARC_TESTNET,
        "asset": USDC,
        "transaction": "batch-1",
    }
    return {**body, **overrides}


def session_for(party_id: str) -> str:
    account = account_for(party_id)
    store.repo.save_account(account)
    return f"Bearer {sessions.issue(account.address)}"


class TestOnlyTheEdge:
    @pytest.mark.parametrize(
        ("method", "path"),
        [("post", "access"), ("post", "calls"), ("get", "calls")],
    )
    @pytest.mark.parametrize("token", [None, "", "wrong", TOKEN + "x"])
    def test_anyone_else_finds_nothing(self, client, method, path, token):
        headers = {} if token is None else {"X-Misthos-Core-Token": token}
        res = getattr(client, method)(f"/api/v1/internal/paid/{path}", headers=headers)
        assert res.status_code == 404
        assert res.json() == {"detail": "Not Found"}

    def test_nothing_gets_in_while_the_token_is_unset(self, client, monkeypatch):
        monkeypatch.setattr(settings, "edge_core_token", "")
        res = client.get("/api/v1/internal/paid/calls", headers={"X-Misthos-Core-Token": ""})
        assert res.status_code == 404

    def test_the_edge_gets_in(self, client):
        assert client.get("/api/v1/internal/paid/calls", headers=EDGE).status_code == 200

    def test_the_paid_api_is_not_in_the_schema(self, client):
        paths = client.get("/openapi.json").json()["paths"]
        assert not [p for p in paths if "/internal/" in p]

    def test_every_paid_route_is_the_paid_module_and_none_is_an_auth_route(self):
        """No paid route reaches an auth, wallet, statement or policy route: every route
        under the prefix is defined by the paid module, and nothing else is."""
        mounted = dict(routes_of(app.routes))
        assert "/api/v1/auth/nonce" in mounted, "the walk sees the whole app"
        under = {
            path: endpoint for path, endpoint in mounted.items() if path.startswith(PAID_PREFIX)
        }
        assert under, "the paid API is mounted"
        for path, endpoint in under.items():
            assert endpoint.__module__ == "misthos.api.v1.paid", path
            assert not any(
                word in path for word in ("auth", "wallet", "statement", "policy", "session")
            ), path
        for path, endpoint in mounted.items():
            if endpoint.__module__ == "misthos.api.v1.paid":
                assert path.startswith(PAID_PREFIX), path


class TestTheFreeTier:
    def test_a_route_without_a_rule_is_never_free(self, client):
        res = client.post(
            "/api/v1/internal/paid/access",
            json={"route": "lookup", "params": {"publisher_id": "PUB-1"}},
            headers={**EDGE, "X-Misthos-Caller-Authorization": session_for("PUB-1")},
        )
        assert res.json() == {"free": False, "who": None}

    def test_free_to_the_session_the_rule_names(self, client, free_lookup):
        res = client.post(
            "/api/v1/internal/paid/access",
            json={"route": "lookup", "params": {"publisher_id": "PUB-1"}},
            headers={**EDGE, "X-Misthos-Caller-Authorization": session_for("PUB-1")},
        )
        assert res.json() == {"free": True, "who": "publisher:PUB-1"}

    @pytest.mark.parametrize(
        "caller",
        [None, "Bearer not-a-session", "Basic dXNlcjpwYXNz", "PUB-2", "CON-1"],
    )
    def test_not_free_to_anyone_else(self, client, free_lookup, caller):
        headers = dict(EDGE)
        if caller in ("PUB-2", "CON-1"):
            headers["X-Misthos-Caller-Authorization"] = session_for(caller)
        elif caller is not None:
            headers["X-Misthos-Caller-Authorization"] = caller
        res = client.post(
            "/api/v1/internal/paid/access",
            json={"route": "lookup", "params": {"publisher_id": "PUB-1"}},
            headers=headers,
        )
        assert res.json() == {"free": False, "who": None}

    def test_a_wallet_without_a_linked_github_login_is_not_free(self, client, free_lookup):
        """Every buyer has a wallet, and signing in with one costs nothing, so a free
        tier keyed on a wallet alone would be free to every buyer."""
        account = account_for("PUB-1").model_copy(update={"github_login": None})
        store.repo.save_account(account)
        res = client.post(
            "/api/v1/internal/paid/access",
            json={"route": "lookup", "params": {"publisher_id": "PUB-1"}},
            headers={
                **EDGE,
                "X-Misthos-Caller-Authorization": f"Bearer {sessions.issue(account.address)}",
            },
        )
        assert res.json() == {"free": False, "who": None}

    def test_a_session_for_a_wallet_with_no_account_is_not_free(self, client, free_lookup):
        token = sessions.issue("0x" + "9" * 40)
        res = client.post(
            "/api/v1/internal/paid/access",
            json={"route": "lookup", "params": {"publisher_id": "PUB-1"}},
            headers={**EDGE, "X-Misthos-Caller-Authorization": f"Bearer {token}"},
        )
        assert res.json()["free"] is False


class TestTheBooks:
    def test_a_settlement_is_written_down(self, client):
        res = client.post("/api/v1/internal/paid/calls", json=settlement(ref="job-1"), headers=EDGE)
        assert res.status_code == 201
        body = res.json()
        assert body["id"].startswith("PAY-")
        assert body["payer"] == PAYER
        assert body["amount"] == {"usdc": "0.01", "base_units": 10_000}
        assert body["network"] == ARC_TESTNET
        assert body["transaction"] == "batch-1"
        assert body["ref"] == "job-1"

    def test_the_same_settlement_twice_is_written_once(self, client):
        first = client.post("/api/v1/internal/paid/calls", json=settlement(), headers=EDGE)
        again = client.post("/api/v1/internal/paid/calls", json=settlement(), headers=EDGE)
        assert (first.status_code, again.status_code) == (201, 200)
        assert again.json()["id"] == first.json()["id"]
        books = client.get("/api/v1/internal/paid/calls", headers=EDGE).json()
        assert len(books["calls"]) == 1

    @pytest.mark.parametrize(
        "bad",
        [
            {"request_digest": "not-hex"},
            {"payer": "someone"},
            {"amount_base_units": 0},
            {"network": "arc"},
            {"asset": "USDC"},
            {"transaction": ""},
            {"route": ""},
        ],
    )
    def test_a_settlement_that_is_not_one_is_refused(self, client, bad):
        res = client.post("/api/v1/internal/paid/calls", json=settlement(**bad), headers=EDGE)
        assert res.status_code == 422

    def test_the_books_add_up_by_route_and_network(self, client):
        for i, (route, amount) in enumerate(
            [("lookup", 10_000), ("lookup", 10_000), ("price", 250_000)]
        ):
            client.post(
                "/api/v1/internal/paid/calls",
                json=settlement(route=route, amount_base_units=amount, transaction=f"batch-{i}"),
                headers=EDGE,
            )
        books = client.get("/api/v1/internal/paid/calls", headers=EDGE).json()
        assert [c["transaction"] for c in books["calls"]] == ["batch-2", "batch-1", "batch-0"]
        assert books["totals"] == [
            {
                "route": "lookup",
                "network": ARC_TESTNET,
                "calls": 2,
                "amount": {"usdc": "0.02", "base_units": 20_000},
            },
            {
                "route": "price",
                "network": ARC_TESTNET,
                "calls": 1,
                "amount": {"usdc": "0.25", "base_units": 250_000},
            },
        ]
        assert books["total"] == {"usdc": "0.27", "base_units": 270_000}

        only = client.get("/api/v1/internal/paid/calls?route=price", headers=EDGE).json()
        assert [c["route"] for c in only["calls"]] == ["price"]


def test_every_repository_keeps_paid_calls_once_and_newest_first(every_store: Store):
    at = datetime(2026, 10, 1, tzinfo=UTC)

    def record(transaction: str, minutes: int, route: str = "lookup"):
        return every_store.record_paid_call(
            route=route,
            request_digest="b" * 64,
            payer=PAYER.upper().replace("0X", "0x"),
            amount=Usdc(10_000),
            network=ARC_TESTNET,
            asset=USDC,
            transaction=transaction,
            at=at + timedelta(minutes=minutes),
        )

    first, new = record("t-1", 0)
    assert new and first.payer == PAYER
    again, new_again = record("t-1", 5)
    assert not new_again and again.id == first.id
    record("t-2", 10)
    record("t-3", 20, route="price")

    listed = every_store.list_paid_calls()
    assert [c.transaction for c in listed] == ["t-3", "t-2", "t-1"]
    assert [c.transaction for c in every_store.list_paid_calls(route="lookup")] == ["t-2", "t-1"]
    since = every_store.list_paid_calls(since=at + timedelta(minutes=10))
    assert [c.transaction for c in since] == ["t-3", "t-2"]
    assert every_store.repo.get_paid_call(first.id) == first
    assert every_store.repo.get_paid_call("PAY-999999") is None


def test_totals_count_each_route_on_each_network():
    def call(route: str, network: str, amount: int) -> PaidCall:
        return PaidCall(
            id=f"PAY-{route}-{network}-{amount}",
            route=route,
            request_digest="c" * 64,
            payer=PAYER,
            amount=Usdc(amount),
            network=network,
            asset=USDC,
            transaction="t",
            ref=None,
            settled_at=datetime(2026, 10, 1, tzinfo=UTC),
        )

    calls = [
        call("b", ARC_TESTNET, 1),
        call("a", ARC_TESTNET, 2),
        call("a", ARC_TESTNET, 3),
        call("a", "eip155:1", 4),
    ]
    assert totals(calls) == [
        RouteTotal("a", "eip155:1", 1, Usdc(4)),
        RouteTotal("a", ARC_TESTNET, 2, Usdc(5)),
        RouteTotal("b", ARC_TESTNET, 1, Usdc(1)),
    ]
