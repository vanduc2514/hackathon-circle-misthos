"""API tests against the seeded simulation."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from misthos.domain.issue import IssueState
from misthos.main import app
from misthos.store import store

API = "/api/v1"


@pytest.fixture(autouse=True)
def fresh_store():
    store.reset()
    yield

@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


class TestHealth:
    def test_health_reports_the_simulation(self, client: TestClient) -> None:
        body = client.get(f"{API}/health").json()
        assert body["status"] == "ok"
        assert body["simulated"] is True
        assert body["seeded_issues"] == 8
        assert body["chain"] == "arc-testnet"


class TestIssues:
    def test_lists_every_seeded_issue(self, client: TestClient) -> None:
        assert len(client.get(f"{API}/issues").json()) == 8

    def test_filters_by_state(self, client: TestClient) -> None:
        rows = client.get(f"{API}/issues", params={"state": "funded"}).json()
        assert rows and all(r["state"] == "FUNDED" for r in rows)

    def test_unknown_state_is_rejected(self, client: TestClient) -> None:
        assert client.get(f"{API}/issues", params={"state": "nonsense"}).status_code == 400

    def test_filters_compliance_driven(self, client: TestClient) -> None:
        rows = client.get(f"{API}/issues", params={"compliance_only": True}).json()
        assert rows and all(r["compliance_driven"] for r in rows)

    def test_detail_carries_the_price_and_its_reasoning(self, client: TestClient) -> None:
        body = client.get(f"{API}/issues/ISS-1001").json()
        assert body["proposal"]["recommended"]["usdc"]
        assert body["proposal"]["justification"]
        assert body["proposal"]["confidence"] in {"low", "medium", "high"}

    def test_missing_issue_is_404(self, client: TestClient) -> None:
        assert client.get(f"{API}/issues/NOPE").status_code == 404

    def test_timeline_is_a_decision_log(self, client: TestClient) -> None:
        rows = client.get(f"{API}/issues/ISS-1002/timeline").json()
        assert rows
        assert {"actor", "action", "outcome"} <= rows[0].keys()


class TestLifecycleThroughTheApi:
    def test_advance_funds_an_awaiting_approval_issue(self, client: TestClient) -> None:
        assert client.get(f"{API}/issues/ISS-1006").json()["state"] == "AWAITING_APPROVAL"
        body = client.post(f"{API}/issues/ISS-1006/advance").json()
        assert body["state"] == "FUNDED"
        assert body["escrow"]["tx_hash"].startswith("0x")

    def test_complete_runs_the_rest_of_the_happy_path(self, client: TestClient) -> None:
        client.post(f"{API}/issues/ISS-1006/advance")  # fund
        client.post(f"{API}/issues/ISS-1006/advance")  # claim
        client.post(f"{API}/issues/ISS-1006/advance")  # submit
        body = client.post(f"{API}/issues/ISS-1006/complete").json()
        assert body["state"] == "PAID"
        assert float(body["paid_usdc"]) > 0

    def test_settlement_pays_one_party_and_never_a_reviewer(self, client: TestClient) -> None:
        """The platform reviews, so there is no reviewer to pay."""
        client.post(f"{API}/issues/ISS-1002/complete")
        body = client.get(f"{API}/issues/ISS-1002").json()
        assert body["state"] == "PAID"
        assert "reviewer_id" not in body
        assert "review_fee_paid_usdc" not in body

    def test_advancing_a_finished_issue_is_conflict(self, client: TestClient) -> None:
        assert client.post(f"{API}/issues/ISS-1005/advance").status_code == 409

    def test_a_silent_publisher_does_not_strand_finished_work(self) -> None:
        """The one release path with no human signature. See `08`."""
        from datetime import timedelta

        from misthos.domain.issue import SILENT_PUBLISHER_GRACE

        rec = store.get("ISS-1002")  # seeded IN_REVIEW
        assert rec is not None
        store.advance("ISS-1002")
        assert rec.state is IssueState.ACCEPTED
        assert rec.review is not None

        # Backdate the verdict past the grace window and advance again.
        rec.review.decided_at -= SILENT_PUBLISHER_GRACE + timedelta(hours=1)
        store.advance("ISS-1002")

        assert rec.state is IssueState.PAID
        assert rec.decisions[-1].rule == "silent_publisher_grace_period"

    def test_every_transition_leaves_a_decision(self, client: TestClient) -> None:
        before = len(client.get(f"{API}/issues/ISS-1006/timeline").json())
        client.post(f"{API}/issues/ISS-1006/advance")
        after = client.get(f"{API}/issues/ISS-1006/timeline").json()
        assert len(after) == before + 1
        assert after[-1]["actor"] == "publisher"


class TestPublish:
    def test_publishing_produces_a_priced_issue(self, client: TestClient) -> None:
        body = client.post(
            f"{API}/issues",
            json={
                "repo": "acme/ledger-core",
                "title": "Add a health endpoint",
                "publisher_id": "PUB-1",
                "labels": ["feature"],
            },
        ).json()
        assert body["state"] == "AWAITING_APPROVAL"
        assert float(body["proposal"]["recommended"]["usdc"]) > 0

    def test_unknown_publisher_is_rejected(self, client: TestClient) -> None:
        r = client.post(
            f"{API}/issues",
            json={"repo": "a/b", "title": "x", "publisher_id": "PUB-999"},
        )
        assert r.status_code == 400


class TestMetrics:
    def test_metrics_match_the_seeded_state(self, client: TestClient) -> None:
        m = client.get(f"{API}/metrics").json()
        assert m["settled_issues"] == 2
        assert float(m["matched_volume_usdc"]) > 0
        assert 0 <= m["publisher_overturn_rate"] <= 1
        assert "total_review_fees_usdc" not in m
        assert sum(m["by_state"].values()) == 8

    def test_metrics_move_after_a_settlement(self, client: TestClient) -> None:
        before = client.get(f"{API}/metrics").json()["settled_issues"]
        for _ in range(3):
            client.post(f"{API}/issues/ISS-1006/advance")
        client.post(f"{API}/issues/ISS-1006/complete")
        assert client.get(f"{API}/metrics").json()["settled_issues"] == before + 1


class TestDecisions:
    def test_the_log_is_newest_first(self, client: TestClient) -> None:
        rows = client.get(f"{API}/decisions").json()
        assert len(rows) > 10
        stamps = [r["created_at"] for r in rows]
        assert stamps == sorted(stamps, reverse=True)


class TestWebhooks:
    def test_describes_what_it_handles(self, client: TestClient) -> None:
        body = client.get(f"{API}/webhooks/github").json()
        assert "pull_request" in body["handled_events"]

    def test_accepts_an_unsigned_payload_in_simulation(self, client: TestClient) -> None:
        r = client.post(
            f"{API}/webhooks/github",
            json={"action": "opened"},
            headers={"X-GitHub-Event": "pull_request"},
        )
        assert r.status_code == 202
        assert r.json()["signature_verified"] is False

    def test_rejects_a_bad_signature(self, client: TestClient) -> None:
        r = client.post(
            f"{API}/webhooks/github",
            json={"action": "opened"},
            headers={"X-Hub-Signature-256": "sha256=deadbeef"},
        )
        assert r.status_code == 401

    def test_accepts_a_valid_signature(self, client: TestClient) -> None:
        import hashlib
        import hmac
        import json

        from misthos.config import settings

        body = json.dumps({"action": "opened"}).encode()
        sig = "sha256=" + hmac.new(
            settings.github_webhook_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        r = client.post(
            f"{API}/webhooks/github",
            content=body,
            headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "pull_request"},
        )
        assert r.status_code == 202
        assert r.json()["signature_verified"] is True


class TestReset:
    def test_reset_restores_the_seed(self, client: TestClient) -> None:
        client.post(f"{API}/issues/ISS-1006/advance")
        assert client.post(f"{API}/demo/reset").json()["issues"] == 8
        assert client.get(f"{API}/issues/ISS-1006").json()["state"] == "AWAITING_APPROVAL"
