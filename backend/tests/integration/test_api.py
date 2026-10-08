"""API tests against the seeded simulation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from siwe_wallet import Wallet

from misthos.config import settings
from misthos.domain.issue import IssueState
from misthos.domain.money import Usdc
from misthos.domain.pricing import take_rate_bps
from misthos.main import app
from misthos.repositories import StaleIssue
from misthos.services.wallets import simulated_address
from misthos.store import store

API = "/api/v1"


def sign_in(client: TestClient, seed: int, role: str, name: str, login: str) -> dict:
    """A signed-in account with a role and a linked GitHub login, for the guards."""
    wallet = Wallet(seed)
    nonce = client.post(f"{API}/auth/nonce").json()["nonce"]
    message = wallet.message(nonce)
    verified = client.post(
        f"{API}/auth/verify", json={"message": message, "signature": wallet.sign(message)}
    )
    assert verified.status_code == 200, verified.text
    account = client.post(f"{API}/auth/role", json={"role": role, "name": name})
    assert account.status_code == 201, account.text
    linked = client.post(f"{API}/auth/github/simulate", json={"login": login})
    assert linked.status_code == 200, linked.text
    return linked.json()


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


class TestWallets:
    def test_a_session_links_the_contributor_wallet(self, client: TestClient) -> None:
        body = client.post(f"{API}/wallets/contributor/CON-1/session").json()
        assert body["simulated"] is True
        assert body["challenge_id"] is None
        linked = body["wallet"]
        assert body["circle_user_id"] == "misthos-contributor-CON-1"
        assert linked["address"] == simulated_address("contributor", "CON-1")

        # The party's own record. The public listing carries no wallet on purpose: a
        # wallet next to a GitHub handle is the link 08 says must never be published.
        held = store.get_contributor("CON-1")
        assert held is not None and held.wallet.address == linked["address"]
        profiles = {c["id"]: c for c in client.get(f"{API}/contributors").json()}
        assert "wallet" not in profiles["CON-1"]

    def test_link_reads_the_address_back_for_a_publisher(self, client: TestClient) -> None:
        wallet = client.post(f"{API}/wallets/publisher/PUB-1/link").json()
        publishers = {p["id"]: p for p in client.get(f"{API}/publishers").json()}
        assert publishers["PUB-1"]["wallet"] == wallet

    def test_an_unknown_party_or_id_is_refused(self, client: TestClient) -> None:
        assert client.post(f"{API}/wallets/contributor/CON-999/session").status_code == 404
        assert client.post(f"{API}/wallets/admin/1/session").status_code == 422

    def test_only_the_party_itself_may_link_its_wallet(self, client: TestClient) -> None:
        account = sign_in(client, 61, "contributor", "erin", "erin-dev")
        own = account["party_id"]
        assert client.post(f"{API}/wallets/contributor/{own}/session").status_code == 200
        # Someone else's wallet is not this account's to link, because what the route
        # records is where that party's payouts go.
        assert client.post(f"{API}/wallets/contributor/CON-1/session").status_code == 403

    def test_the_wrong_kind_of_party_is_refused(self, client: TestClient) -> None:
        account = sign_in(client, 62, "publisher", "Acme", "acme-co")
        own = account["party_id"]
        assert client.post(f"{API}/wallets/contributor/{own}/session").status_code == 403
        assert client.post(f"{API}/wallets/publisher/{own}/session").status_code == 200

    def test_a_wallet_needs_a_signed_in_party_outside_the_simulation(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "simulated", False)
        assert client.post(f"{API}/wallets/contributor/CON-1/session").status_code == 401
        assert client.post(f"{API}/wallets/publisher/PUB-1/link").status_code == 401

    def test_a_release_pays_the_linked_wallet(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        accepted = client.get(f"{API}/issues", params={"state": "in_review"}).json()[0]
        issue = client.get(f"{API}/issues/{accepted['id']}").json()
        cid = issue["contributor_id"]
        wallet = client.post(f"{API}/wallets/contributor/{cid}/session").json()["wallet"]

        # Watch where the escrow is told to send the money, rather than reading it back
        # out of the public record: the decision log names the contributor, and the
        # wallet stays private.
        paid_to: list[str] = []
        release = store.chain.release
        monkeypatch.setattr(
            store.chain,
            "release",
            lambda *args, **kwargs: (paid_to.append(args[1]), release(*args, **kwargs))[1],
        )

        client.post(f"{API}/issues/{accepted['id']}/complete")
        timeline = client.get(f"{API}/issues/{accepted['id']}/timeline").json()
        released = [d for d in timeline if d["action"] == "released"]
        assert released
        assert paid_to == [wallet["address"]]
        assert wallet["address"] not in released[0]["outcome"]


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

    def test_a_settlement_records_the_platform_fee_at_the_tier_rate(
        self, client: TestClient
    ) -> None:
        """The take rate is carved out of the commitment, not added to it."""
        client.post(f"{API}/issues/ISS-1002/complete")
        body = client.get(f"{API}/issues/ISS-1002").json()
        assert body["state"] == "PAID"

        tier = next(
            p["tier"]
            for p in client.get(f"{API}/publishers").json()
            if p["id"] == body["publisher_id"]
        )
        gross = body["escrow"]["amount"]["base_units"]
        fee = Usdc.from_decimal(body["platform_fee_usdc"])
        payout = Usdc.from_decimal(body["paid_usdc"])

        assert fee.base_units == gross * take_rate_bps(tier) // 10_000
        assert payout.base_units + fee.base_units == gross

    def test_advancing_a_finished_issue_is_conflict(self, client: TestClient) -> None:
        assert client.post(f"{API}/issues/ISS-1005/advance").status_code == 409

    def test_a_silent_publisher_does_not_strand_finished_work(self) -> None:
        """The one release path with no human signature. See `08`."""
        from datetime import timedelta

        from misthos.domain.issue import SILENT_PUBLISHER_GRACE

        store.advance("ISS-1002")  # seeded IN_REVIEW, the verdict passes
        rec = store.get("ISS-1002")
        assert rec is not None
        assert rec.state is IssueState.ACCEPTED
        assert rec.review is not None

        # Backdate the verdict past the grace window and advance again.
        rec.review.decided_at -= SILENT_PUBLISHER_GRACE + timedelta(hours=1)
        store.save(rec)
        rec = store.advance("ISS-1002")

        assert rec.state is IssueState.PAID
        assert rec.decisions[-1].rule == "silent_publisher_grace_period"

    def test_a_late_verdict_releases_at_the_deadline_not_after_it(self) -> None:
        """The grace is capped at the escrow deadline, so a late verdict is not stranded
        waiting on a window the contract has already closed (#33)."""
        store.advance("ISS-1002")
        rec = store.get("ISS-1002")
        assert rec is not None and rec.escrow is not None and rec.review is not None
        assert rec.state is IssueState.ACCEPTED

        # A verdict six days ago, with the deadline already behind us: a full seven-day
        # grace would end a day after the contract stopped paying, so the window ends
        # at the deadline instead.
        now = datetime.now(UTC)
        rec.review.decided_at = now - timedelta(days=6)
        rec.deadline = now - timedelta(hours=1)
        rec.escrow.deadline = rec.deadline
        store.save(rec)

        store.run_timers("ISS-1002", now=now)

        paid = store.get("ISS-1002")
        assert paid is not None
        assert paid.state is IssueState.PAID
        assert paid.decisions[-1].rule == "silent_publisher_grace_period"

    def test_an_issue_awaiting_merge_still_counts_as_open(self, client: TestClient) -> None:
        """ACCEPTED holds committed money until the publisher merges."""
        before = client.get(f"{API}/metrics").json()["open_issues"]
        client.post(f"{API}/issues/ISS-1002/advance")  # IN_REVIEW -> ACCEPTED
        assert client.get(f"{API}/issues/ISS-1002").json()["state"] == "ACCEPTED"
        assert client.get(f"{API}/metrics").json()["open_issues"] == before

    def test_advancing_a_state_with_no_demo_leg_is_conflict(
        self, client: TestClient
    ) -> None:
        """Refuse rather than return an unchanged record behind a 200."""
        rec = store.get("ISS-1006")
        assert rec is not None
        rec.state = IssueState.PRICED
        store.save(rec)
        assert client.post(f"{API}/issues/ISS-1006/advance").status_code == 409

    def test_acting_on_an_issue_that_just_changed_is_conflict(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stale copy is refused, never saved over a refund or a release."""

        def changed_underneath(issue_id: str) -> None:
            raise StaleIssue(issue_id)

        monkeypatch.setattr(store, "advance", changed_underneath)
        r = client.post(f"{API}/issues/ISS-1006/advance")
        assert r.status_code == 409
        assert "changed since it was loaded" in r.json()["detail"]

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

    def test_publishing_beyond_the_budget_is_refused_with_a_reason(
        self, client: TestClient
    ) -> None:
        """A budget under the price floor cannot carry its own review cost."""
        publisher = store.get_publisher("PUB-3")
        assert publisher is not None
        publisher.budget_remaining_usdc = "40.00"
        store.save_publisher(publisher)
        r = client.post(
            f"{API}/issues",
            json={
                "repo": "acme/ledger-core",
                "title": "Rewrite the rounding path",
                "publisher_id": "PUB-3",
                "signals": {
                    "code_surface": 5.0,
                    "requirement_clarity": 5.0,
                    "test_coverage": 5.0,
                    "dependency_depth": 5.0,
                    "prior_attempts": 5.0,
                    "blast_radius": 5.0,
                },
            },
        )
        assert r.status_code == 422
        assert "minimum" in r.json()["detail"]

    def test_a_published_issue_reports_whether_it_is_fundable(
        self, client: TestClient
    ) -> None:
        body = client.post(
            f"{API}/issues",
            json={"repo": "a/b", "title": "Add a health endpoint", "publisher_id": "PUB-1"},
        ).json()
        assert body["proposal"]["fundable"] is True


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

    def test_metrics_report_the_take_rate_revenue(self, client: TestClient) -> None:
        """The revenue line moves with each settlement, and never exceeds it."""
        before = client.get(f"{API}/metrics").json()
        for _ in range(3):
            client.post(f"{API}/issues/ISS-1006/advance")
        client.post(f"{API}/issues/ISS-1006/complete")
        after = client.get(f"{API}/metrics").json()

        fees = Usdc.from_decimal(after["platform_fees_usdc"])
        assert fees.base_units > Usdc.from_decimal(before["platform_fees_usdc"]).base_units
        assert fees.base_units < Usdc.from_decimal(after["matched_volume_usdc"]).base_units


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
    def test_reset_restores_the_seed(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from misthos.config import settings

        monkeypatch.setattr(settings, "database_url", "")  # the zero-config demo
        client.post(f"{API}/issues/ISS-1006/advance")
        assert client.post(f"{API}/demo/reset").json()["issues"] == 8
        assert client.get(f"{API}/issues/ISS-1006").json()["state"] == "AWAITING_APPROVAL"

    def test_reset_is_refused_when_a_database_is_configured(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The README's durable setup sets only MISTHOS_DATABASE_URL, and there an
        unauthenticated reset would delete every row of every table."""
        from misthos.config import settings

        monkeypatch.setattr(settings, "database_url", "postgresql://db.example/misthos")
        monkeypatch.setattr(settings, "allow_demo_reset", False)
        client.post(f"{API}/issues/ISS-1006/advance")

        r = client.post(f"{API}/demo/reset")
        assert r.status_code == 403
        assert "MISTHOS_ALLOW_DEMO_RESET" in r.json()["detail"]
        assert client.get(f"{API}/issues/ISS-1006").json()["state"] == "FUNDED"

    def test_an_operator_can_allow_reset_on_a_throwaway_database(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from misthos.config import settings

        monkeypatch.setattr(settings, "database_url", "postgresql://db.example/misthos")
        monkeypatch.setattr(settings, "allow_demo_reset", True)
        client.post(f"{API}/issues/ISS-1006/advance")
        assert client.post(f"{API}/demo/reset").json()["issues"] == 8
        assert client.get(f"{API}/issues/ISS-1006").json()["state"] == "AWAITING_APPROVAL"

    def test_reset_is_refused_outside_the_simulation(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Against a real database, reset would delete every table."""
        from misthos.config import settings

        monkeypatch.setattr(settings, "simulated", False)
        monkeypatch.setattr(settings, "allow_demo_reset", True)  # not a way round it
        assert client.post(f"{API}/demo/reset").status_code == 403
        assert client.get(f"{API}/health").json()["seeded_issues"] == 8
