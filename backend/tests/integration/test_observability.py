"""Observability (#77): a correlation id on every request and every line it causes,
money events and verdicts logged as structured fields, latency and cost as metrics,
and a ledger divergence raised as an alert rather than drifting quietly."""

from __future__ import annotations

import io
import json
import logging

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from misthos.domain.ledger import EscrowStatus
from misthos.main import app
from misthos.observability.logs import ContextFilter, JsonFormatter
from misthos.store import store
from misthos.workers.sweeper import sweep_once

API = "/api/v1"


@pytest.fixture(autouse=True)
def fresh_store():
    store.reset()
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


class TestCorrelation:
    def test_every_response_carries_a_request_id(self, client: TestClient) -> None:
        r = client.get(f"{API}/health")
        assert len(r.headers["X-Request-ID"]) == 32

    def test_a_callers_plain_id_is_kept_and_a_crafted_one_replaced(
        self, client: TestClient
    ) -> None:
        assert (
            client.get(f"{API}/health", headers={"X-Request-ID": "req-42"}).headers["X-Request-ID"]
            == "req-42"
        )
        crafted = client.get(f"{API}/health", headers={"X-Request-ID": 'x" "level":"ERROR'})
        assert crafted.headers["X-Request-ID"] != 'x" "level":"ERROR'

    def test_a_money_event_is_logged_with_the_id_of_the_request_that_caused_it(
        self, client: TestClient
    ) -> None:
        out = io.StringIO()
        handler = logging.StreamHandler(out)
        handler.addFilter(ContextFilter())
        handler.setFormatter(JsonFormatter())
        logger = logging.getLogger("misthos")
        logger.addHandler(handler)
        previous = logger.level
        logger.setLevel(logging.INFO)
        try:
            client.post(f"{API}/issues/ISS-1006/advance", headers={"X-Request-ID": "fund-1006"})
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)

        records = [json.loads(line) for line in out.getvalue().splitlines()]
        money = [r for r in records if r["message"] == "money event"]
        assert len(money) == 1
        event = money[0]
        assert event["correlation_id"] == "fund-1006"
        assert (event["issue_id"], event["kind"]) == ("ISS-1006", "committed")
        assert event["amount_usdc"] and event["tx_hash"].startswith("0x")
        assert event["level"] == "INFO" and event["logger"] == "misthos.store"


class TestMetrics:
    def test_the_paths_the_requirements_name_are_measured(self, client: TestClient) -> None:
        released = sample("misthos_money_events_total", kind="released")
        reviews = sample("misthos_review_seconds_count", reviewer="rules_v1", verdict="accept")
        prices = sample("misthos_price_seconds_count", source="form")

        client.post(f"{API}/issues/ISS-1006/complete")
        client.post(
            f"{API}/issues",
            json={"repo": "acme/ledger-core", "title": "Add a probe", "publisher_id": "PUB-1"},
        )

        assert sample("misthos_money_events_total", kind="released") == released + 1
        assert (
            sample("misthos_review_seconds_count", reviewer="rules_v1", verdict="accept")
            == reviews + 1
        )
        assert sample("misthos_price_seconds_count", source="form") == prices + 1

    def test_requests_are_timed_by_route_not_by_path(self, client: TestClient) -> None:
        route = "/api/v1/issues/{issue_id}"
        before = sample(
            "misthos_http_request_seconds_count", method="GET", route=route, status="200"
        )
        client.get(f"{API}/issues/ISS-1001")
        client.get(f"{API}/issues/ISS-1002")
        after = sample(
            "misthos_http_request_seconds_count", method="GET", route=route, status="200"
        )
        assert after == before + 2

    def test_the_scraper_endpoint_serves_them(self, client: TestClient) -> None:
        client.get(f"{API}/health")
        body = client.get("/internal/metrics").text
        assert "misthos_http_request_seconds_bucket" in body
        assert "misthos_ledger_divergences" in body
        assert "/internal/metrics" not in client.get("/openapi.json").text


class TestDivergence:
    def test_a_divergence_is_an_alert_with_a_gauge_and_a_counter(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        alerts = sample("misthos_ledger_divergence_alerts_total")
        store.chain.tamper("ISS-1001", EscrowStatus.RELEASED)  # type: ignore[attr-defined]

        with caplog.at_level(logging.ERROR, logger="misthos.store"):
            sweep_once(store)

        assert sample("misthos_ledger_divergences") == 1.0
        assert sample("misthos_ledger_divergence_alerts_total") == alerts + 1
        alert = next(r for r in caplog.records if getattr(r, "alert", None))
        assert alert.alert == "ledger_divergence"  # type: ignore[attr-defined]
        assert alert.issue_id == "ISS-1001"  # type: ignore[attr-defined]

    def test_a_clean_pass_clears_the_gauge(self) -> None:
        sweep_once(store)
        assert sample("misthos_ledger_divergences") == 0.0


def test_a_json_line_is_one_parseable_object_even_with_an_exception() -> None:
    record = logging.makeLogRecord(
        {"name": "misthos.x", "levelname": "ERROR", "levelno": 40, "msg": "boom %s",
         "args": ("now",), "issue_id": "ISS-1"}
    )  # fmt: skip
    try:
        raise ValueError("bad")
    except ValueError:
        import sys

        record.exc_info = sys.exc_info()
    ContextFilter().filter(record)
    line = json.loads(JsonFormatter().format(record))
    assert line["message"] == "boom now" and line["issue_id"] == "ISS-1"
    assert "ValueError: bad" in line["exc"]
    assert line["correlation_id"] == "-"
