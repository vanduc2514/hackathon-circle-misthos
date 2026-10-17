"""Guards on the API: one action per issue at a time, idempotent requests and rate
limits. Run against whatever coordinator the app is configured with, so the
backend-postgres CI job runs them against Redis."""

from __future__ import annotations

import json
import threading
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from misthos.api import guards
from misthos.config import settings
from misthos.domain.issue import IssueState
from misthos.main import app
from misthos.services.coordination import Busy
from misthos.store import store

API = "/api/v1"
CLAIM = f"{API}/issues/ISS-1001/claim"  # seeded FUNDED
PUBLISH = {
    "repo": "acme/ledger-core",
    "title": "Add a health endpoint",
    "publisher_id": "PUB-1",
    "price": "500",
}


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def key(value: str) -> dict[str, str]:
    return {"Idempotency-Key": value}


class TestOneActionAtATime:
    def test_a_second_claim_while_the_first_is_in_flight_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inside, release = threading.Event(), threading.Event()
        claim = store.current()._claim

        def slow_claim(*args: object, **kwargs: object) -> None:
            inside.set()
            assert release.wait(5)
            return claim(*args, **kwargs)

        monkeypatch.setattr(store.current(), "_claim", slow_claim)
        first = threading.Thread(target=store.advance, args=("ISS-1001",))
        first.start()
        try:
            assert inside.wait(5)
            with pytest.raises(Busy):
                store.advance("ISS-1001")
        finally:
            release.set()
            first.join(5)

        rec = store.get("ISS-1001")
        assert rec is not None and rec.state is IssueState.CLAIMED
        assert [d.action for d in rec.decisions].count("claimed") == 1

    def test_the_api_says_busy_and_when_to_retry(self, client: TestClient) -> None:
        with store.coordinator.lock("issue:ISS-1001", timedelta(seconds=30)) as held:
            assert held
            r = client.post(CLAIM)
        assert r.status_code == 409
        assert r.headers["Retry-After"] == "1"
        assert client.post(CLAIM).json()["state"] == "CLAIMED"


class TestIdempotency:
    def test_a_retried_step_runs_once(self, client: TestClient) -> None:
        first = client.post(CLAIM, headers=key("claim-1001"))
        retry = client.post(CLAIM, headers=key("claim-1001"))

        assert first.status_code == retry.status_code == 200
        assert retry.json() == first.json()
        assert first.headers[guards.REPLAYED] == "false"
        assert retry.headers[guards.REPLAYED] == "true"
        rec = store.get("ISS-1001")
        assert rec is not None and rec.state is IssueState.CLAIMED

    def test_publishing_twice_with_one_key_creates_one_issue(self, client: TestClient) -> None:
        before = len(store.list_issues())
        first = client.post(f"{API}/issues", json=PUBLISH, headers=key("publish-1"))
        retry = client.post(f"{API}/issues", json=PUBLISH, headers=key("publish-1"))
        assert first.status_code == retry.status_code == 201
        assert retry.json()["id"] == first.json()["id"]
        assert len(store.list_issues()) == before + 1

    def test_a_key_reused_for_a_different_request_is_refused(self, client: TestClient) -> None:
        client.post(CLAIM, headers=key("k"))
        r = client.post(f"{API}/issues/ISS-1001/submit", json={"pr_number": 1}, headers=key("k"))
        assert r.status_code == 422
        rec = store.get("ISS-1001")
        assert rec is not None and rec.state is IssueState.CLAIMED

    def test_a_retry_while_the_first_is_still_running_waits(self, client: TestClient) -> None:
        running = {"state": "pending", "fingerprint": guards.fingerprint("POST", CLAIM, b"")}
        store.coordinator.put("idem:k", json.dumps(running), guards.PENDING_TTL)

        r = client.post(CLAIM, headers=key("k"))
        assert r.status_code == 409
        assert r.headers["Retry-After"] == "1"
        rec = store.get("ISS-1001")
        assert rec is not None and rec.state is IssueState.FUNDED

    def test_a_failed_request_keeps_no_answer(self, client: TestClient) -> None:
        r = client.post(f"{API}/issues/ISS-1005/claim", headers=key("k"))  # already paid
        assert r.status_code == 409
        assert store.coordinator.get("idem:k") is None

    def test_a_malformed_key_is_refused(self, client: TestClient) -> None:
        r = client.post(CLAIM, headers=key("x" * 300))
        assert r.status_code == 400


class TestRateLimits:
    @pytest.fixture(autouse=True)
    def frozen_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(guards, "_now", lambda: 1_800_000_030.0)

    def test_publishing_past_the_budget_is_refused(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "rate_limit_publish_per_minute", 2)
        codes = [client.post(f"{API}/issues", json=PUBLISH).status_code for _ in range(3)]
        assert codes == [201, 201, 429]

    def test_a_refusal_says_when_to_come_back(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "rate_limit_actions_per_minute", 1)
        client.post(CLAIM)
        r = client.post(CLAIM)
        assert r.status_code == 429
        assert r.headers["Retry-After"] == "30"

    def test_publishing_and_actions_have_separate_budgets(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "rate_limit_publish_per_minute", 1)
        assert client.post(f"{API}/issues", json=PUBLISH).status_code == 201
        assert client.post(CLAIM).status_code == 200

    def test_zero_turns_the_limit_off(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "rate_limit_publish_per_minute", 0)
        codes = {client.post(f"{API}/issues", json=PUBLISH).status_code for _ in range(5)}
        assert codes == {201}

    def test_reads_are_not_limited(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "rate_limit_actions_per_minute", 1)
        assert {client.get(f"{API}/issues/ISS-1006").status_code for _ in range(5)} == {200}
