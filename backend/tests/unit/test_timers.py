"""Timers, judged at a chosen instant.

No clock, no worker and no database: which timed action an issue is owed is pure
arithmetic over its state and timestamps, so every boundary can be pinned exactly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from misthos.domain.compliance import PAYOUT_RETRY_INTERVAL
from misthos.domain.issue import (
    CLAIM_WINDOW,
    ESCROW_TERM,
    RELEASE_MARGIN,
    SILENT_PUBLISHER_GRACE,
    IssueState,
)
from misthos.domain.timers import TIMED_STATES, Clocks, TimedAction, due

T0 = datetime(2026, 10, 1, 12, tzinfo=UTC)
A_MOMENT = timedelta(seconds=1)


class TestClaims:
    def claimed(self, deadline: datetime = T0 + ESCROW_TERM) -> Clocks:
        return Clocks(IssueState.CLAIMED, deadline=deadline, claim_expires_at=T0 + CLAIM_WINDOW)

    def test_a_claim_holds_for_its_whole_window(self) -> None:
        assert due(self.claimed(), T0 + CLAIM_WINDOW) is None

    def test_a_lapsed_claim_returns_the_issue_to_the_pool(self) -> None:
        assert due(self.claimed(), T0 + CLAIM_WINDOW + A_MOMENT) is TimedAction.EXPIRE_CLAIM

    def test_a_claim_ends_at_the_deadline_even_inside_its_window(self) -> None:
        """With no pull request there is no work to wait for past the deadline."""
        clocks = self.claimed(deadline=T0 + timedelta(hours=1))
        assert due(clocks, T0 + timedelta(hours=1) + A_MOMENT) is TimedAction.EXPIRE_CLAIM


class TestDeadline:
    def test_committed_money_waits_until_the_deadline(self) -> None:
        assert due(Clocks(IssueState.FUNDED, deadline=T0), T0) is None

    def test_an_ignored_issue_is_refunded_after_the_deadline(self) -> None:
        assert due(Clocks(IssueState.FUNDED, deadline=T0), T0 + A_MOMENT) is TimedAction.REFUND


class TestSilentPublisher:
    def accepted(self) -> Clocks:
        return Clocks(IssueState.ACCEPTED, deadline=T0 + ESCROW_TERM, verdict_passed_at=T0)

    def test_a_passing_verdict_waits_out_the_grace_period(self) -> None:
        assert due(self.accepted(), T0 + SILENT_PUBLISHER_GRACE) is None

    def test_a_silent_publisher_does_not_strand_finished_work(self) -> None:
        moment = T0 + SILENT_PUBLISHER_GRACE + A_MOMENT
        assert due(self.accepted(), moment) is TimedAction.RELEASE_AFTER_GRACE

    def test_only_a_passing_verdict_starts_the_grace_period(self) -> None:
        clocks = Clocks(IssueState.ACCEPTED, deadline=T0 + ESCROW_TERM, verdict_passed_at=None)
        assert due(clocks, T0 + SILENT_PUBLISHER_GRACE + A_MOMENT) is None

    def test_the_grace_ends_a_margin_before_the_escrow_deadline(self) -> None:
        """A verdict that leaves less than a full grace on the clock releases a margin
        before the deadline. At the deadline itself the release fired only once
        `now > deadline`, which is when `release` reverts (#121)."""
        deadline = T0 + timedelta(days=4)
        clocks = Clocks(IssueState.ACCEPTED, deadline=deadline, verdict_passed_at=T0)
        closes = deadline - RELEASE_MARGIN

        assert due(clocks, closes) is None
        assert due(clocks, closes + A_MOMENT) is TimedAction.RELEASE_AFTER_GRACE
        assert due(clocks, deadline) is TimedAction.RELEASE_AFTER_GRACE
        # Not a day later, which is where the uncapped grace would have put it.
        assert T0 + SILENT_PUBLISHER_GRACE > deadline

    def test_past_the_deadline_accepted_work_is_refunded_not_released(self) -> None:
        """No release can land after the deadline and anyone may refund, so the timer
        follows the chain instead of trying a release on every pass for ever."""
        deadline = T0 + timedelta(days=4)
        clocks = Clocks(IssueState.ACCEPTED, deadline=deadline, verdict_passed_at=T0)
        assert due(clocks, deadline + A_MOMENT) is TimedAction.REFUND

    def test_a_held_payout_past_the_deadline_is_refunded_too(self) -> None:
        clocks = Clocks(
            IssueState.ACCEPTED,
            deadline=T0,
            payout_held_since=T0 - timedelta(days=1),
            payout_hold="await_identity",
        )
        assert due(clocks, T0 + A_MOMENT) is TimedAction.REFUND


class TestHeldPayouts:
    def held(self, hold: str, since: datetime, deadline: datetime = T0 + ESCROW_TERM) -> Clocks:
        return Clocks(
            IssueState.ACCEPTED, deadline=deadline, payout_held_since=since, payout_hold=hold
        )

    def test_a_compliance_hold_is_checked_again_after_the_interval(self) -> None:
        clocks = self.held("blocked_sanctions", T0)
        assert due(clocks, T0 + PAYOUT_RETRY_INTERVAL) is None
        assert due(clocks, T0 + PAYOUT_RETRY_INTERVAL + A_MOMENT) is TimedAction.RETRY_PAYOUT

    def test_a_release_the_chain_refused_is_tried_on_the_next_pass(self) -> None:
        """A revert is caught at gas estimation and costs nothing, so a held release
        waits for the next pass, not the hour a compliance hold waits (#125)."""
        clocks = self.held("release_failed", T0)
        assert due(clocks, T0) is None
        assert due(clocks, T0 + A_MOMENT) is TimedAction.RETRY_PAYOUT

    def test_the_last_retry_comes_while_the_escrow_still_pays(self) -> None:
        """An hourly retry would otherwise first fall after the deadline."""
        deadline = T0 + timedelta(minutes=30)
        clocks = self.held("await_identity", T0, deadline=deadline)
        assert due(clocks, deadline - RELEASE_MARGIN + A_MOMENT) is TimedAction.RETRY_PAYOUT

    def test_a_payout_held_inside_the_margin_is_not_retried_in_the_same_instant(self) -> None:
        deadline = T0 + timedelta(minutes=5)
        clocks = self.held("await_identity", T0, deadline=deadline)
        assert due(clocks, T0) is None
        assert due(clocks, T0 + A_MOMENT) is TimedAction.RETRY_PAYOUT


class TestApprovals:
    def test_approved_terms_wait_for_the_wallet_until_their_deadline(self) -> None:
        clocks = Clocks(IssueState.AWAITING_APPROVAL, approved_until=T0 + ESCROW_TERM)
        assert due(clocks, T0 + ESCROW_TERM) is None

    def test_then_they_lapse_and_anything_unbooked_goes_back(self) -> None:
        """The escrow bounds a commitment's deadline by the approved one (#122), so by
        now a commitment the platform could not book is refundable (#126)."""
        clocks = Clocks(IssueState.AWAITING_APPROVAL, approved_until=T0 + ESCROW_TERM)
        assert due(clocks, T0 + ESCROW_TERM + A_MOMENT) is TimedAction.LAPSE_APPROVAL

    def test_an_issue_nobody_approved_has_no_timer(self) -> None:
        clocks = Clocks(IssueState.AWAITING_APPROVAL)
        assert due(clocks, T0 + timedelta(days=365)) is None


@pytest.mark.parametrize("state", sorted(set(IssueState) - TIMED_STATES))
def test_no_timer_moves_an_issue_out_of_a_state_it_does_not_own(state: IssueState) -> None:
    """Work under review is never timed out: that is the contract's call. See #33."""
    clocks = Clocks(state, deadline=T0, claim_expires_at=T0, verdict_passed_at=T0)
    assert due(clocks, T0 + timedelta(days=365)) is None
