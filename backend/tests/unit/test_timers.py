"""Timers, judged at a chosen instant.

No clock, no worker and no database: which timed action an issue is owed is pure
arithmetic over its state and timestamps, so every boundary can be pinned exactly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from misthos.domain.issue import CLAIM_WINDOW, ESCROW_TERM, SILENT_PUBLISHER_GRACE, IssueState
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
        clocks = Clocks(IssueState.ACCEPTED, deadline=T0, verdict_passed_at=None)
        assert due(clocks, T0 + ESCROW_TERM) is None

    def test_the_grace_is_capped_at_the_escrow_deadline(self) -> None:
        """A verdict that leaves less than a full grace on the clock releases at the
        deadline. Uncapped it would release after `release` has started reverting, and
        the work the verdict passed would be refunded instead. See #33."""
        clocks = Clocks(
            IssueState.ACCEPTED,
            deadline=T0 + timedelta(days=4),
            verdict_passed_at=T0,
        )

        assert due(clocks, T0 + timedelta(days=4)) is None
        assert due(clocks, T0 + timedelta(days=4) + A_MOMENT) is TimedAction.RELEASE_AFTER_GRACE
        # Not a day later, which is where the uncapped grace would have put it.
        assert T0 + SILENT_PUBLISHER_GRACE > clocks.deadline


@pytest.mark.parametrize("state", sorted(set(IssueState) - TIMED_STATES))
def test_no_timer_moves_an_issue_out_of_a_state_it_does_not_own(state: IssueState) -> None:
    """Work under review is never timed out: that is the contract's call. See #33."""
    clocks = Clocks(state, deadline=T0, claim_expires_at=T0, verdict_passed_at=T0)
    assert due(clocks, T0 + timedelta(days=365)) is None
