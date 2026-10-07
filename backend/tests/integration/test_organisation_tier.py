"""The organisation tier (#13): the organisation's own spending policy enforced where
money moves, its spend view, and an audit export an auditor can use as it is."""

from __future__ import annotations

import csv
import io
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from misthos.config import settings
from misthos.domain.issue import SILENT_PUBLISHER_GRACE, IssueState
from misthos.domain.ledger import MoneyEventKind
from misthos.domain.policy import PolicyRefusal
from misthos.main import app
from misthos.store import store
from misthos.workers.sweeper import sweep_once

API = "/api/v1"
ISSUE = "ISS-1006"  # PUB-4's, priced about $2,600, labelled compliance and reporting
PUBLISHER = "PUB-4"
APPROVER = "dana@globex.example"


@pytest.fixture(autouse=True)
def fresh_store():
    store.reset()
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def policy(client: TestClient, **body: object) -> object:
    return client.put(f"{API}/publishers/{PUBLISHER}/policy", json=body)


def to_acceptance(issue_id: str) -> None:
    for _ in range(4):  # fund, claim, submit, verdict
        store.advance(issue_id)
    assert store.get(issue_id).state is IssueState.ACCEPTED  # type: ignore[union-attr]


class TestReleaseThreshold:
    def test_a_release_over_the_threshold_waits_for_a_named_approver(
        self, client: TestClient
    ) -> None:
        assert (
            policy(client, approval_threshold_usdc="1000", approvers=[APPROVER]).status_code == 200
        )
        to_acceptance(ISSUE)

        rec = store.advance(ISSUE)  # the publisher merges

        assert rec.state is IssueState.ACCEPTED
        assert rec.payout_hold == "await_approver"
        held = rec.decisions[-1]
        assert (held.action, held.rule) == ("payout_held", "release_threshold")
        assert APPROVER in held.outcome

    def test_the_grace_period_cannot_go_around_it(self, client: TestClient) -> None:
        policy(client, approval_threshold_usdc="1000", approvers=[APPROVER])
        to_acceptance(ISSUE)
        verdict_at = store.get(ISSUE).review.decided_at  # type: ignore[union-attr]
        sweep_once(store, now=verdict_at + SILENT_PUBLISHER_GRACE + timedelta(hours=2))
        assert store.get(ISSUE).state is IssueState.ACCEPTED  # type: ignore[union-attr]

    def test_only_a_named_approver_releases_it(self, client: TestClient) -> None:
        policy(client, approval_threshold_usdc="1000", approvers=[APPROVER])
        to_acceptance(ISSUE)
        store.advance(ISSUE)

        r = client.post(f"{API}/issues/{ISSUE}/approve-release", json={"approver": "mallory"})
        assert r.status_code == 403
        r = client.post(f"{API}/issues/{ISSUE}/approve-release", json={"approver": APPROVER})
        assert r.status_code == 200 and r.json()["state"] == "PAID"

        actions = [d.action for d in store.get(ISSUE).decisions]  # type: ignore[union-attr]
        assert actions.index("release_approved") < actions.index("released")

    def test_nothing_to_approve_is_a_conflict(self, client: TestClient) -> None:
        policy(client, approval_threshold_usdc="1000", approvers=[APPROVER])
        r = client.post(f"{API}/issues/{ISSUE}/approve-release", json={"approver": APPROVER})
        assert r.status_code == 409

    def test_under_the_threshold_nothing_waits(self, client: TestClient) -> None:
        policy(client, approval_threshold_usdc="100000", approvers=[APPROVER])
        assert store.approve_and_accept(ISSUE).state is IssueState.PAID

    @pytest.mark.parametrize(
        "body",
        [
            {"approval_threshold_usdc": "1000", "approvers": []},
            {"approval_threshold_usdc": "-5", "approvers": [APPROVER]},
            {"approval_threshold_usdc": "lots", "approvers": [APPROVER]},
            {"category_limits": {"security": "-1"}},
        ],
    )
    def test_a_policy_that_cannot_work_is_refused(self, client: TestClient, body: dict) -> None:
        assert policy(client, **body).status_code == 422


class TestCategoryLimits:
    def test_a_commitment_over_the_monthly_limit_is_refused(self, client: TestClient) -> None:
        policy(client, category_limits={"compliance": "1000"})

        r = client.post(f"{API}/issues/{ISSUE}/advance")

        assert r.status_code == 403
        assert "compliance is limited to 1000.00 USDC a month" in r.json()["detail"]
        rec = store.get(ISSUE)
        assert rec is not None and rec.state is IssueState.AWAITING_APPROVAL
        assert (rec.decisions[-1].action, rec.decisions[-1].rule) == (
            "funding_refused",
            "category_limit",
        )
        assert rec.money_events == []

    def test_within_the_limit_it_funds(self, client: TestClient) -> None:
        policy(client, category_limits={"compliance": "10000"})
        assert store.advance(ISSUE).state is IssueState.FUNDED

    def test_the_limit_is_the_organisations_own(self) -> None:
        with pytest.raises(PolicyRefusal):
            store.set_policy(
                PUBLISHER,
                approval_threshold_usdc="5",
                approvers=[],
                category_limits={},
            )


class TestSpendView:
    def test_budgeted_committed_released_and_fileable(self, client: TestClient) -> None:
        policy(client, category_limits={"compliance": "10000"})
        paid = store.approve_and_accept(ISSUE)
        amount = paid.money_events[-1].amount

        spend = client.get(f"{API}/publishers/{PUBLISHER}/spend").json()

        released = sum(
            e.amount.base_units
            for r in store.list_issues()
            if r.publisher_id == PUBLISHER
            for e in r.money_events
            if e.kind is MoneyEventKind.RELEASED and e.occurred_at.year == spend["year"]
        )
        assert spend["released"]["base_units"] == released
        compliance = next(c for c in spend["by_category"] if c["label"] == "compliance")
        assert compliance["released"]["base_units"] == amount.base_units
        assert compliance["limit"]["usdc"] == "10000.00"
        filed = next(f for f in spend["fileable"] if f["issue_id"] == ISSUE)
        assert filed["acceptance_criteria"] == paid.acceptance_criteria

    def test_an_unknown_publisher_is_404(self, client: TestClient) -> None:
        assert client.get(f"{API}/publishers/PUB-999/spend").status_code == 404


class TestAuditExport:
    def test_the_export_is_the_record_unedited(self, client: TestClient) -> None:
        store.approve_and_accept(ISSUE)
        body = client.get(f"{API}/publishers/{PUBLISHER}/audit").json()

        mine = [r for r in store.list_issues() if r.publisher_id == PUBLISHER]
        assert {i["id"] for i in body["issues"]} == {r.id for r in mine}
        exported = next(i for i in body["issues"] if i["id"] == ISSUE)
        rec = store.get(ISSUE)
        assert rec is not None
        assert [d["id"] for d in exported["decisions"]] == [d.id for d in rec.decisions]
        assert [e["tx_hash"] for e in exported["money_events"]] == [
            None if e.kind is MoneyEventKind.RELEASED else e.tx_hash for e in rec.money_events
        ]
        # A release's reference would link the contributor's wallet to their handle.
        assert rec.payout_tx_hash is not None
        assert rec.payout_tx_hash not in client.get(
            f"{API}/publishers/{PUBLISHER}/audit", params={"format": "csv"}
        ).text

    def test_the_csv_is_one_sortable_table(self, client: TestClient) -> None:
        store.approve_and_accept(ISSUE)
        r = client.get(f"{API}/publishers/{PUBLISHER}/audit", params={"format": "csv"})
        assert r.headers["content-type"].startswith("text/csv")
        lines = r.text.splitlines()
        assert lines[0].startswith("# SIMULATED")
        rows = list(csv.DictReader(io.StringIO("\n".join(lines[1:]))))
        ours = [row for row in rows if row["issue_id"] == ISSUE]
        assert {row["record"] for row in ours} == {"decision", "money"}
        assert [row["at_utc"] for row in ours] == sorted(row["at_utc"] for row in ours)

    def test_it_is_the_organisations_alone(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "simulated", False)
        assert client.get(f"{API}/publishers/{PUBLISHER}/audit").status_code == 403
        assert client.get(f"{API}/publishers/{PUBLISHER}/spend").status_code == 403
        assert policy(client, approvers=[APPROVER]).status_code == 403
