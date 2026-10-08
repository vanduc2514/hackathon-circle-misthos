"""Every dashboard number from the ledger and the lifecycle records (#14): the North
Star and its inputs, the guardrails that could not move before, contributor
standing, and the public view of the loop."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from misthos.config import settings
from misthos.domain.issue import SILENT_PUBLISHER_GRACE, IssueState
from misthos.domain.ledger import MoneyEventKind
from misthos.domain.money import Usdc
from misthos.main import app
from misthos.repositories.sql import SqlRepository
from misthos.services import reputation
from misthos.services.github import SimulatedGitHub
from misthos.store import DeclineRefused, Store, store
from misthos.workers.sweeper import sweep_once

API = "/api/v1"


@pytest.fixture(autouse=True)
def fresh_store():
    store.reset()
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def released() -> list:  # type: ignore[type-arg]
    return [
        e for r in store.list_issues() for e in r.money_events if e.kind is MoneyEventKind.RELEASED
    ]


class TestTheNorthStar:
    def test_a_fresh_demo_has_settled_work_this_week(self) -> None:
        """The dashboard leads with settlements this week. Both seeded payouts used to
        land 19 and 28 days before the seed, so a fresh demo always showed 0."""
        store.reset()
        m = store.metrics()
        assert m.settled_issues == 2
        assert m.settled_issues_7d == 1

    def test_settled_issues_per_week_counts_releases_in_the_last_seven_days(self) -> None:
        before = store.metrics().settled_issues_7d
        paid = store.approve_and_accept("ISS-1006")
        assert paid.state is IssueState.PAID
        assert store.metrics().settled_issues_7d == before + 1
        a_week_on = paid.money_events[-1].occurred_at + timedelta(days=8)
        assert store.metrics(now=a_week_on).settled_issues_7d == 0

    def test_the_numbers_come_back_the_same_from_the_database_alone(self, tmp_path: Path) -> None:
        url = f"sqlite:///{tmp_path / 'm.db'}"
        first = Store(SqlRepository(url))
        first.reset()
        first.approve_and_accept("ISS-1006")
        moment = first.get("ISS-1006").money_events[-1].occurred_at  # type: ignore[union-attr]

        restarted = Store(SqlRepository(url))
        assert restarted.metrics(now=moment) == first.metrics(now=moment)

    def test_a_new_commitment_waits_72_hours_before_it_counts_against_the_claim_rate(
        self,
    ) -> None:
        before = store.metrics()
        funded = store.advance("ISS-1006")
        assert store.metrics().claim_rate_72h == before.claim_rate_72h
        later = funded.money_events[0].occurred_at + timedelta(hours=73)
        assert store.metrics(now=later).claim_rate_72h < before.claim_rate_72h

    def test_the_refund_rate_moves_when_money_goes_back(self) -> None:
        before = store.metrics().refund_rate
        funded = store.get("ISS-1001")
        assert funded is not None and funded.deadline is not None
        sweep_once(store, now=funded.deadline + timedelta(minutes=1))
        assert store.metrics().refund_rate > before


class TestPublisherDecline:
    def accepted(self) -> str:
        rec = store.advance("ISS-1002")  # the review passes: ACCEPTED, awaiting the merge
        assert rec.state is IssueState.ACCEPTED
        return rec.id

    def test_declining_sends_the_work_back_and_moves_the_overturn_rate(
        self, github_sent: list
    ) -> None:  # type: ignore[type-arg]
        issue_id = self.accepted()
        assert store.metrics().publisher_overturn_rate == 0.0

        rec = store.decline(issue_id, "The example in the docs does not run.")

        assert rec.state is IssueState.REWORK
        assert rec.decisions[-1].action == "publisher_declined"
        assert store.metrics().publisher_overturn_rate > 0.0
        assert github_sent[-1].state == "REQUEST_CHANGES"
        assert "does not run" in github_sent[-1].body

    def test_a_publisher_declines_once(self) -> None:
        issue_id = self.accepted()
        store.decline(issue_id, "Not yet.")
        store.advance(issue_id)  # rework pushed
        assert store.advance(issue_id).state is IssueState.ACCEPTED  # passes again
        with pytest.raises(DeclineRefused):
            store.decline(issue_id, "Still no.")

    def test_a_retried_decline_says_it_was_already_declined(self, client: TestClient) -> None:
        """The state check used to run first, so the retry was told it could not move
        the issue from REWORK to REWORK."""
        issue_id = self.accepted()
        store.decline(issue_id, "Not yet.")
        with pytest.raises(DeclineRefused, match="already declined"):
            store.decline(issue_id, "Not yet.")
        r = client.post(f"{API}/issues/{issue_id}/decline", json={"reason": "Not yet."})
        assert r.status_code == 409 and "already declined" in r.json()["detail"]

    def test_a_decline_after_the_grace_period_is_refused_and_the_payout_releases(
        self,
    ) -> None:
        """Past the grace window the sweeper owes the release. A decline in that window
        used to send the work back and drop the payout that was due."""
        issue_id = self.accepted()
        verdict_at = store.get(issue_id).review.decided_at  # type: ignore[union-attr]
        late = verdict_at + SILENT_PUBLISHER_GRACE + timedelta(minutes=1)

        with pytest.raises(DeclineRefused, match="grace period"):
            store.decline(issue_id, "Too late.", now=late)
        assert store.get(issue_id).state is IssueState.ACCEPTED  # type: ignore[union-attr]

        sweep_once(store, now=late)
        assert store.get(issue_id).state is IssueState.PAID  # type: ignore[union-attr]

    def test_a_decline_inside_the_grace_period_still_counts(self) -> None:
        issue_id = self.accepted()
        verdict_at = store.get(issue_id).review.decided_at  # type: ignore[union-attr]
        in_time = verdict_at + SILENT_PUBLISHER_GRACE - timedelta(minutes=1)
        assert store.decline(issue_id, "Not yet.", now=in_time).state is IssueState.REWORK

    def test_after_a_decline_the_grace_period_does_not_pay(self) -> None:
        issue_id = self.accepted()
        verdict_at = store.get(issue_id).review.decided_at  # type: ignore[union-attr]
        store.decline(issue_id, "Not yet.")
        sweep_once(store, now=verdict_at + SILENT_PUBLISHER_GRACE + timedelta(hours=1))
        assert store.get(issue_id).state is IssueState.REWORK  # type: ignore[union-attr]

    def test_only_work_awaiting_the_merge_can_be_declined(self, client: TestClient) -> None:
        r = client.post(f"{API}/issues/ISS-1001/decline", json={"reason": "no"})
        assert r.status_code == 409
        issue_id = self.accepted()
        r = client.post(f"{API}/issues/{issue_id}/decline", json={"reason": "Missing example."})
        assert r.status_code == 200 and r.json()["state"] == "REWORK"

    def test_declining_needs_the_publisher_signed_in_outside_the_simulation(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        issue_id = self.accepted()
        monkeypatch.setattr(settings, "simulated", False)
        r = client.post(f"{API}/issues/{issue_id}/decline", json={"reason": "no"})
        assert r.status_code == 401


@pytest.fixture
def github_sent() -> list:  # type: ignore[type-arg]
    assert isinstance(store.github, SimulatedGitHub)
    return store.github.sent


class TestEarnings:
    def test_the_500_dollar_share_and_concentration_come_from_payouts(self) -> None:
        store.approve_and_accept("ISS-1006")
        paid: dict[str, int] = {}
        for e in released():
            paid[e.counterparty_id] = paid.get(e.counterparty_id, 0) + e.amount.base_units
        over = sum(1 for v in paid.values() if v >= Usdc.from_decimal("500").base_units)

        m = store.metrics()
        assert m.earners_over_500_share == round(over / len(paid), 2)
        assert m.top10_payout_share == 1.0  # fewer than ten contributors have been paid

    def test_with_no_payouts_there_is_no_share_to_report(self) -> None:
        later = max(e.occurred_at for e in released()) + timedelta(days=31)
        assert store.metrics(now=later).earners_over_500_share is None


class TestStanding:
    def test_standing_comes_from_settled_issues_alone(self) -> None:
        by_id = {c.id: c for c in store.list_contributors()}
        totals = reputation.standing(store.list_issues())
        for contributor_id, contributor in by_id.items():
            expected = reputation.as_fields(totals.get(contributor_id, reputation.Standing()))
            assert {k: getattr(contributor, k) for k in expected} == expected
        # Claiming is activity, not an outcome: ISS-1003's claimant has earned nothing.
        claimant = store.get("ISS-1003").contributor_id  # type: ignore[union-attr]
        assert by_id[claimant].reputation == 0  # type: ignore[index]

    def test_a_settlement_raises_the_contributors_standing(self) -> None:
        paid = store.approve_and_accept("ISS-1006")
        contributor = store.get_contributor(paid.contributor_id)  # type: ignore[arg-type]
        assert contributor is not None
        assert contributor.settled_issues >= 1
        assert contributor.reputation >= reputation.points_for(paid.paid)  # type: ignore[arg-type]
        events = store.reputation_events(contributor.id)
        assert events[-1].issue_id == "ISS-1006"

    def test_drift_from_the_ledger_is_found_and_repaired(self) -> None:
        contributor = store.list_contributors()[0]
        store.save_contributor(contributor.model_copy(update={"reputation": 999}))

        drift = store.rebuild_standing(write=False)
        assert set(drift) == {contributor.id}
        assert reputation.main(["--check"]) == 1
        store.rebuild_standing()
        assert store.rebuild_standing(write=False) == {}

    def test_the_reputation_events_are_public(self, client: TestClient) -> None:
        store.approve_and_accept("ISS-1006")
        cid = store.get("ISS-1006").contributor_id  # type: ignore[union-attr]
        rows = client.get(f"{API}/contributors/{cid}/reputation").json()
        assert rows[-1]["issue_id"] == "ISS-1006"
        assert rows[-1]["points"] >= reputation.POINTS_PER_SETTLED_ISSUE
        assert client.get(f"{API}/contributors/NOPE/reputation").status_code == 404


class TestTheLoopInPublic:
    def test_the_loop_reports_what_settled_without_wallets_or_transfers(
        self, client: TestClient
    ) -> None:
        paid = store.approve_and_accept("ISS-1006")
        body = client.get(f"{API}/loop").json()

        assert body["settled_issues"] == len(released())
        assert body["recent"][0]["issue_id"] == "ISS-1006"
        text = str(body).lower()
        assert paid.payout_tx_hash is not None and paid.payout_tx_hash.lower() not in text
        assert all(c.wallet.address.lower() not in text for c in store.list_contributors())

    def test_one_repositorys_watchers_see_their_repository(self, client: TestClient) -> None:
        rec = store.approve_and_accept("ISS-1006")
        body = client.get(f"{API}/loop", params={"repo": rec.repo}).json()
        assert body["repo"] == rec.repo
        assert {s["repo"] for s in body["recent"]} == {rec.repo}
        assert client.get(f"{API}/loop", params={"repo": "nobody/here"}).json()["recent"] == []
