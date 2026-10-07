"""The sweeper: claim expiry, refunds and the silent-publisher release, with nobody
calling the API. Time is passed in rather than waited for."""

from __future__ import annotations

from datetime import timedelta

import pytest

from misthos.domain.issue import SILENT_PUBLISHER_GRACE, IssueState
from misthos.models.records import IssueRecord
from misthos.store import store
from misthos.workers.sweeper import sweep_once

A_MINUTE = timedelta(minutes=1)


@pytest.fixture(autouse=True)
def fresh_store():
    store.reset()
    yield


def get(issue_id: str) -> IssueRecord:
    rec = store.get(issue_id)
    assert rec is not None
    return rec


class TestDeadlines:
    def test_an_issue_funded_and_ignored_reaches_refunded_on_its_own(self) -> None:
        funded = get("ISS-1001")  # seeded FUNDED, never claimed
        assert funded.state is IssueState.FUNDED and funded.deadline is not None

        report = sweep_once(store, now=funded.deadline + A_MINUTE)

        assert report.applied["ISS-1001"] == ["refund"]
        after = get("ISS-1001")
        assert after.state is IssueState.REFUNDED
        assert after.escrow is not None and after.escrow.refunded
        assert [d.action for d in after.decisions[-2:]] == ["refunded", "relisted"]

    def test_an_unclaimed_issue_is_relisted_higher_for_the_publisher_to_approve(self) -> None:
        """A higher price is a new obligation, so it waits for the publisher's approval."""
        funded = get("ISS-1001")
        assert funded.deadline is not None and funded.proposal is not None
        sweep_once(store, now=funded.deadline + A_MINUTE)

        relisted = [r for r in store.list_issues() if r.relisted_from == "ISS-1001"]
        assert len(relisted) == 1
        again = relisted[0]
        assert again.state is IssueState.AWAITING_APPROVAL
        assert again.escrow is None
        assert again.proposal is not None
        assert again.proposal.recommended > funded.proposal.recommended

    def test_a_claimed_issue_at_its_deadline_is_refunded_but_not_relisted(self) -> None:
        """Someone took it, so the price drew a claim and is not the problem."""
        claimed = get("ISS-1003")  # seeded CLAIMED
        assert claimed.deadline is not None

        report = sweep_once(store, now=claimed.deadline + A_MINUTE)

        assert report.applied["ISS-1003"] == ["expire_claim", "refund"]
        after = get("ISS-1003")
        assert after.state is IssueState.REFUNDED
        assert after.decisions[-2].rule == "deadline_passed"
        assert not any(r.relisted_from == "ISS-1003" for r in store.list_issues())


class TestClaims:
    def test_a_lapsed_claim_returns_the_issue_to_the_pool(self) -> None:
        claimed = get("ISS-1003")
        assert claimed.claim is not None

        sweep_once(store, now=claimed.claim.expires_at + A_MINUTE)

        after = get("ISS-1003")
        assert after.state is IssueState.FUNDED
        assert after.claim is None and after.contributor_id is None
        assert after.decisions[-1].rule == "claim_window_elapsed"

    def test_a_live_claim_is_left_alone(self) -> None:
        claimed = get("ISS-1003")
        assert claimed.claim is not None
        report = sweep_once(store, now=claimed.claim.expires_at - A_MINUTE)
        assert "ISS-1003" not in report.applied
        assert get("ISS-1003").state is IssueState.CLAIMED


class TestSilentPublisher:
    def test_a_backdated_passing_verdict_releases_without_any_api_call(self) -> None:
        store.advance("ISS-1002")  # seeded IN_REVIEW, the verdict passes
        accepted = get("ISS-1002")
        assert accepted.state is IssueState.ACCEPTED and accepted.review is not None

        later = accepted.review.decided_at + SILENT_PUBLISHER_GRACE + timedelta(hours=1)
        sweep_once(store, now=later)

        after = get("ISS-1002")
        assert after.state is IssueState.PAID
        assert after.paid is not None
        assert after.decisions[-1].rule == "silent_publisher_grace_period"

    def test_the_grace_period_is_not_cut_short(self) -> None:
        store.advance("ISS-1002")
        accepted = get("ISS-1002")
        assert accepted.review is not None
        sweep_once(store, now=accepted.review.decided_at + SILENT_PUBLISHER_GRACE - A_MINUTE)
        assert get("ISS-1002").state is IssueState.ACCEPTED


class TestSweeping:
    def test_a_sweep_with_nothing_due_changes_nothing(self) -> None:
        before = {r.id: (r.state, r.version) for r in store.list_issues()}
        report = sweep_once(store)
        assert report.ran and report.applied == {}
        assert {r.id: (r.state, r.version) for r in store.list_issues()} == before

    def test_a_second_sweeper_waits_its_turn(self) -> None:
        """Two processes sweeping at once could refund the same commitment twice."""
        with store.repo.try_lock("sweeper") as held:
            assert held
            report = sweep_once(store, now=get("ISS-1001").created_at + timedelta(days=365))
        assert report.ran is False
        assert get("ISS-1001").state is IssueState.FUNDED

    def test_a_timer_acts_on_what_was_saved_not_on_the_copy_it_listed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The contributor opened a pull request after the sweeper listed the issue:
        the claim it would have expired no longer exists, and the submission stands."""
        stale = get("ISS-1003")  # seeded CLAIMED
        assert stale.claim is not None
        store.advance("ISS-1003")  # submitted while the sweeper holds the old copy
        monkeypatch.setattr(store, "list_issues", lambda states=None: [stale])

        report = sweep_once(store, now=stale.claim.expires_at + A_MINUTE)

        assert "ISS-1003" not in report.applied
        after = get("ISS-1003")
        assert after.state is IssueState.IN_REVIEW
        assert "claim_expired" not in [d.action for d in after.decisions]

    def test_an_issue_someone_is_acting_on_is_skipped_until_the_next_pass(self) -> None:
        funded = get("ISS-1001")
        assert funded.deadline is not None
        due = funded.deadline + A_MINUTE

        with store.coordinator.lock("issue:ISS-1001", timedelta(seconds=30)) as held:
            assert held
            report = sweep_once(store, now=due)
        assert report.skipped == ["ISS-1001"]
        assert get("ISS-1001").state is IssueState.FUNDED

        assert sweep_once(store, now=due).applied["ISS-1001"] == ["refund"]

    def test_one_failing_issue_does_not_stop_the_others(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from misthos.services.chain import ChainRevert

        funded = get("ISS-1001")
        assert funded.deadline is not None
        refund = store.chain.refund

        def refuse_one(issue_id: str, at: object) -> str:
            if issue_id == "ISS-1001":
                raise ChainRevert("NotHeld")
            return refund(issue_id, at)

        monkeypatch.setattr(store.chain, "refund", refuse_one)
        report = sweep_once(store, now=funded.deadline + timedelta(days=30))

        assert report.failed == ["ISS-1001"]
        assert get("ISS-1001").state is IssueState.FUNDED  # nothing half-saved
        assert report.applied  # every other due issue still moved
