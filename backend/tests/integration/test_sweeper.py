"""The sweeper: claim expiry, refunds and the silent-publisher release, with nobody
calling the API. Time is passed in rather than waited for."""

from __future__ import annotations

from datetime import timedelta

import pytest

from misthos.domain.compliance import PAYOUT_RETRY_INTERVAL, PayoutGate
from misthos.domain.issue import ESCROW_TERM, SILENT_PUBLISHER_GRACE, IssueState
from misthos.models.records import IssueRecord
from misthos.services.chain import ChainRevert
from misthos.services.review import ReviewFailed
from misthos.store import store
from misthos.workers.sweeper import (
    MAX_REVIEW_ATTEMPTS,
    sweep_once,
)

A_MINUTE = timedelta(minutes=1)
IN_REVIEW = "ISS-1002"  # seeded, PR #903, no verdict yet




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
        assert after.decisions[-1].action == "refunded"
        assert not any(r.relisted_from == "ISS-1001" for r in store.list_issues())

    def test_an_unclaimed_issue_at_its_deadline_is_refunded_and_not_relisted(self) -> None:
        """The money goes back and no new listing appears: the publisher can name a
        different price themselves while the issue is still unclaimed (#142)."""
        funded = get("ISS-1001")
        assert funded.deadline is not None
        sweep_once(store, now=funded.deadline + A_MINUTE)

        after = get("ISS-1001")
        assert after.state is IssueState.REFUNDED
        assert not any(r.relisted_from == "ISS-1001" for r in store.list_issues())

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

    def test_the_grace_runs_in_full_while_the_escrow_has_room(self) -> None:
        """A verdict with a whole grace on the clock is not paid early. The seeded
        issue decides inside the last week of its term, so give it room first: that is
        the case this property is about."""
        store.advance("ISS-1002")
        rec = get("ISS-1002")
        assert rec.review is not None and rec.escrow is not None
        decided = rec.review.decided_at
        rec.deadline = decided + ESCROW_TERM
        rec.escrow.deadline = rec.deadline
        store.save(rec)

        sweep_once(store, now=decided + SILENT_PUBLISHER_GRACE - A_MINUTE)
        assert get("ISS-1002").state is IssueState.ACCEPTED

        sweep_once(store, now=decided + SILENT_PUBLISHER_GRACE + A_MINUTE)
        assert get("ISS-1002").state is IssueState.PAID

    def test_a_verdict_inside_the_last_week_releases_at_the_deadline(self) -> None:
        """Five days of grace left on a fourteen-day term: the release window ends at
        the deadline, because `release` reverts after it. Uncapped it would fire two days
        later, and the work the verdict passed would be stranded instead of paid (#33)."""
        store.advance("ISS-1002")
        rec = get("ISS-1002")
        assert rec.review is not None and rec.deadline is not None
        decided = rec.review.decided_at
        assert decided + SILENT_PUBLISHER_GRACE > rec.deadline

        sweep_once(store, now=rec.deadline - A_MINUTE)
        assert get("ISS-1002").state is IssueState.ACCEPTED

        sweep_once(store, now=rec.deadline + A_MINUTE)
        assert get("ISS-1002").state is IssueState.PAID

    def test_a_release_the_chain_refused_is_held_and_paid_on_a_later_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A revert must not throw the acceptance away (#125). The pass is not a failure,
        the issue keeps the grace's acceptance, and the release is tried again."""
        store.advance("ISS-1002")
        accepted = get("ISS-1002")
        assert accepted.review is not None
        release = store.chain.release

        def refused(*_: object) -> str:
            raise ChainRevert("NotAttestor")

        monkeypatch.setattr(store.chain, "release", refused)
        later = accepted.review.decided_at + SILENT_PUBLISHER_GRACE + timedelta(hours=1)
        report = sweep_once(store, now=later)

        assert report.failed == []
        held = get("ISS-1002")
        assert held.state is IssueState.ACCEPTED and held.accepted_by == "grace"
        assert held.payout_hold == PayoutGate.RELEASE_FAILED.value
        assert held.decisions[-1].action == "payout_held"
        assert "NotAttestor" in held.decisions[-1].outcome

        # The chain recovers, and the next pass pays what was already accepted.
        monkeypatch.setattr(store.chain, "release", release)
        assert held.payout_checked_at is not None
        sweep_once(store, now=held.payout_checked_at + PAYOUT_RETRY_INTERVAL + A_MINUTE)

        paid = get("ISS-1002")
        assert paid.state is IssueState.PAID and paid.accepted_by == "grace"
        assert paid.payout_hold is None and paid.paid is not None


class TestSweeping:
    def test_a_sweep_with_nothing_due_changes_nothing(self) -> None:
        store.advance("ISS-1002")  # the one submission awaiting a verdict gets it
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
        monkeypatch.setattr(store.current(), "list_issues", lambda states=None: [stale])

        report = sweep_once(store, now=stale.claim.expires_at + A_MINUTE)

        assert "ISS-1003" not in report.applied
        after = get("ISS-1003")
        assert after.state in {IssueState.IN_REVIEW, IssueState.ACCEPTED}
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


class TestAFailingReview:
    """A submission the reviewer cannot judge must not be retried forever.

    Every attempt is a billed model call, and the pass only reviews a fixed number of
    submissions, so one that always fails would hold a slot while the submissions
    queued behind it are never looked at.
    """

    def test_a_submission_that_keeps_failing_is_left_for_a_person(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        judged: list[str] = []

        class AlwaysFails:
            name = "broken"

            def judge(self, submitted: object) -> None:
                judged.append("attempt")
                raise ReviewFailed("the model refused")

        monkeypatch.setattr(store.current(), "reviewer", AlwaysFails())

        for _ in range(MAX_REVIEW_ATTEMPTS):
            report = sweep_once(store)
            assert IN_REVIEW in report.failed

        assert len(judged) == MAX_REVIEW_ATTEMPTS
        assert get(IN_REVIEW).review is None

        # Past the cap the sweeper stops paying for the same failure.
        sweep_once(store)
        assert len(judged) == MAX_REVIEW_ATTEMPTS

