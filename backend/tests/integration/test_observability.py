"""Observability (#77): a correlation id on every request and every line it causes,
money events and verdicts logged as structured fields, latency and cost as metrics,
and a ledger divergence raised as an alert rather than drifting quietly."""

from __future__ import annotations

import io
import json
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from misthos.config import settings
from misthos.domain.ledger import EscrowStatus
from misthos.main import app
from misthos.observability import logs
from misthos.observability.logs import ContextFilter, JsonFormatter
from misthos.observability.metrics import exported, serve
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
    return exported(sweeper=True).get_sample_value(name, labels) or 0.0


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


class TestEachProcessExportsWhatItWrites:
    """The sweeper's series have no labels, so they read 0 from the moment they exist.
    Exported by an API that leaves the sweeping to the worker, as compose runs it,
    `misthos_ledger_divergences` said 0.0 forever while the worker held the figure."""

    SWEEPER_SERIES = (
        "misthos_ledger_divergences",
        "misthos_ledger_divergence_alerts_total",
        "misthos_sweep_seconds",
        "misthos_sweep_failures_total",
    )

    def test_an_api_that_does_not_sweep_does_not_export_the_sweepers_series(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "sweeper_in_process", False)
        body = client.get("/internal/metrics").text
        assert "misthos_http_request_seconds_bucket" in body
        for series in self.SWEEPER_SERIES:
            assert series not in body, series

    def test_an_api_that_sweeps_exports_them(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "sweeper_in_process", True)
        body = client.get("/internal/metrics").text
        for series in self.SWEEPER_SERIES:
            assert series in body, series

    def test_the_worker_serves_the_divergence_its_sweep_found(self) -> None:
        store.chain.tamper("ISS-1001", EscrowStatus.RELEASED)  # type: ignore[attr-defined]
        sweep_once(store)
        server = serve(0)
        try:
            body = httpx.get(f"http://127.0.0.1:{server.server_port}/metrics").text
        finally:
            server.shutdown()
            server.server_close()
        assert "misthos_ledger_divergences 1.0" in body
        assert "misthos_sweep_seconds_count" in body
        # And what every process writes, such as the reviews its passes make.
        assert "misthos_review_seconds" in body


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


ROOT = Path(__file__).resolve().parents[3]


def _image_command(port: int) -> list[str]:
    """The API's command from the Dockerfile, on loopback and the given port."""
    dockerfile = (ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
    raw = dockerfile.split("\nCMD ", 1)[1]
    command = json.loads(raw[: raw.index("]") + 1].replace("\\\n", " "))
    swap = {"0.0.0.0": "127.0.0.1", "8000": str(port)}
    return [swap.get(part, part) for part in command]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _parses(line: str) -> bool:
    try:
        return isinstance(json.loads(line), dict)
    except ValueError:
        return False


class TestJsonLines:
    def test_every_line_the_api_process_writes_is_one_json_object(self) -> None:
        """Started as the image starts it. uvicorn's loggers kept handlers of their own,
        so container logs mixed `INFO: ... "GET /api/v1/health" 200` lines, with no
        correlation id, into the JSON a log pipeline reads."""
        port = _free_port()
        command = _image_command(port)
        assert command[0] == "uvicorn"
        # In memory, whatever this suite runs against: the server is its own process.
        shared = ("MISTHOS_DATABASE_URL", "MISTHOS_REDIS_URL")
        env = {k: v for k, v in os.environ.items() if k not in shared}
        env |= {"MISTHOS_LOG_JSON": "true", "MISTHOS_SWEEPER_IN_PROCESS": "false"}
        server = subprocess.Popen(
            [sys.executable, "-m", *command],
            cwd=ROOT / "backend",
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    httpx.get(f"http://127.0.0.1:{port}/api/v1/health", timeout=1)
                    break
                except httpx.TransportError:
                    assert server.poll() is None, server.communicate()
                    assert time.monotonic() < deadline, "the API never came up"
                    time.sleep(0.2)
            r = httpx.get(
                f"http://127.0.0.1:{port}/api/v1/health", headers={"X-Request-ID": "json-1"}
            )
            assert r.status_code == 200
        finally:
            server.terminate()
            out, err = server.communicate(timeout=30)

        lines = [line for line in (out + err).splitlines() if line.strip()]
        assert lines, "the server wrote nothing"
        assert [line for line in lines if not _parses(line)] == []
        records = [json.loads(line) for line in lines]
        loggers = {r["logger"] for r in records}
        assert {"uvicorn.error", "uvicorn.access"} <= loggers
        access = [r for r in records if r["logger"] == "uvicorn.access"]
        assert any(
            r["correlation_id"] == "json-1" and "GET /api/v1/health" in r["message"] for r in access
        )
        # uvicorn's coloured copy of a message is terminal decoration, not a field.
        assert not any("color_message" in r for r in records)

    def test_an_access_log_switched_off_stays_off(self) -> None:
        """--no-access-log leaves uvicorn's access logger with no handler and not
        propagating. Sending uvicorn's loggers through ours must not turn it back on."""
        root, access = logging.getLogger(), logging.getLogger("uvicorn.access")
        saved = (root.handlers[:], root.level, access.handlers[:], access.propagate)
        access.handlers, access.propagate = [], False
        try:
            logs.configure(json_lines=True)
            assert access.propagate is False and not access.hasHandlers()
        finally:
            root.handlers, access.handlers, access.propagate = saved[0], saved[2], saved[3]
            root.setLevel(saved[1])
