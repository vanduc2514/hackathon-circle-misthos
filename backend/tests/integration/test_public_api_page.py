"""What the public API page is built from (#208): the routes the schema marks public,
and the edge's catalog of x402 routes read through the API."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from misthos.api.public import GROUPS
from misthos.config import settings
from misthos.main import app
from misthos.services import paid_catalog

SAMPLE = {"issue_id": "ISS-1001", "contributor_id": "CON-1"}


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def fresh_catalog():
    paid_catalog.forget()
    yield
    paid_catalog.forget()


def marked(client: TestClient) -> list[tuple[str, str, dict]]:
    schema = client.get("/openapi.json").json()
    return [
        (method, path, op["x-misthos-public"])
        for path, ops in schema["paths"].items()
        for method, op in ops.items()
        if "x-misthos-public" in op
    ]


def test_the_page_lists_the_routes_a_stranger_can_call(client):
    found = {path for _, path, _ in marked(client)}
    assert {
        "/api/v1/health",
        "/api/v1/issues",
        "/api/v1/issues/{issue_id}",
        "/api/v1/issues/{issue_id}/timeline",
        "/api/v1/issues/{issue_id}/escrow",
        "/api/v1/publishers",
        "/api/v1/metrics",
        "/api/v1/decisions",
        "/api/v1/contributors",
        "/api/v1/contributors/{contributor_id}/reputation",
        "/api/v1/catalog/paid",
    } == found
    for method, path, marker in marked(client):
        assert method == "get", path
        assert marker["group"] in GROUPS, path
        assert marker["name"] and 0 < len(marker["summary"]) <= 100, path


@pytest.mark.real_auth
def test_every_route_it_lists_answers_without_signing_in(client):
    for _, path, _ in marked(client):
        url = path.format(**SAMPLE)
        res = client.get(url)
        assert res.status_code == 200, f"{url} answered {res.status_code} to a stranger"


def test_no_signed_in_or_internal_route_is_listed(client):
    found = {path for _, path, _ in marked(client)}
    for path in found:
        assert not any(
            word in path
            for word in ("/auth", "/wallets", "/statements", "/audit", "/policy", "/internal")
        ), path
    assert "/api/v1/issues/{issue_id}/commitment" not in found, "it needs the publisher"


EDGE_CATALOG = {
    "networks": ["eip155:5042002"],
    "payTo": "0x1111111111111111111111111111111111111111",
    "accepts": [
        {
            "scheme": "exact",
            "network": "eip155:5042002",
            "asset": "0x3600000000000000000000000000000000000000",
        }
    ],
    "certificate_signer": "0x4444444444444444444444444444444444444444",
    "routes": [
        {
            "id": "price",
            "method": "POST",
            "path": "/paid/price",
            "name": "Price an issue",
            "summary": "The engine's suggested price.",
            "group": "Analytics",
            "returns": "PriceReport",
            "price": {"usdc": "0.25", "base_units": "250000"},
            "free_tier": True,
            "async": False,
            "capped": False,
            "available": True,
        },
        {
            "id": "review",
            "method": "POST",
            "path": "/paid/review",
            "name": "Pre-flight review",
            "summary": "A verdict on a diff.",
            "group": "Analytics",
            "price": {"usdc": "2.00", "base_units": "2000000"},
            "async": True,
            "capped": True,
            "note": "the cap is the price",
            "available": True,
        },
        {
            "id": "screen",
            "method": "POST",
            "path": "/paid/screen",
            "name": "Sanctions screen",
            "summary": "Off.",
            "group": "Trust",
            "price": {"usdc": "0.25", "base_units": "250000"},
            "available": False,
        },
    ],
}


def edge(monkeypatch: pytest.MonkeyPatch, answer) -> list[str]:
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return answer(request)

    real = httpx.Client
    monkeypatch.setattr(
        paid_catalog.httpx,
        "Client",
        lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs),
    )
    return asked


def test_the_paid_routes_are_the_edges_own_at_its_prices(client, monkeypatch):
    monkeypatch.setattr(settings, "edge_url", "http://edge.internal:8080")
    monkeypatch.setattr(settings, "paid_public_url", "https://paid.example.com/")
    asked = edge(monkeypatch, lambda _r: httpx.Response(200, json=EDGE_CATALOG))
    body = client.get("/api/v1/catalog/paid").json()
    assert asked == ["http://edge.internal:8080/paid"]
    assert body["available"] is True
    assert body["base_url"] == "https://paid.example.com"
    assert body["pay_to"] == EDGE_CATALOG["payTo"]
    assert body["asset"] == "0x3600000000000000000000000000000000000000"
    assert body["certificate_signer"] == EDGE_CATALOG["certificate_signer"]
    assert [r["id"] for r in body["routes"]] == ["price", "review"], (
        "a route that is off is not listed"
    )
    assert body["routes"][0]["price"] == {"usdc": "0.25", "base_units": "250000"}
    assert body["routes"][1]["async"] is True and body["routes"][1]["capped"] is True
    assert "edge.internal" not in client.get("/api/v1/catalog/paid").text


def test_the_catalog_is_kept_a_minute(client, monkeypatch):
    asked = edge(monkeypatch, lambda _r: httpx.Response(200, json=EDGE_CATALOG))
    client.get("/api/v1/catalog/paid")
    client.get("/api/v1/catalog/paid")
    assert len(asked) == 1


@pytest.mark.parametrize(
    "answer",
    [
        lambda _r: httpx.Response(503, json={"error": "down"}),
        lambda _r: httpx.Response(200, text="<html>maintenance</html>"),
        lambda _r: (_ for _ in ()).throw(httpx.ConnectError("refused")),
    ],
)
def test_an_edge_it_cannot_read_leaves_the_free_routes(client, monkeypatch, answer):
    edge(monkeypatch, answer)
    body = client.get("/api/v1/catalog/paid").json()
    assert body["available"] is False and body["routes"] == []
