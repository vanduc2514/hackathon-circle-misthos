"""Timers, judged at a chosen instant.

No clock, no worker and no database: which timed action an issue is owed is pure
arithmetic over its state and timestamps, so every boundary can be pinned exactly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from misthos.domain.issue import ESCROW_TERM, SILENT_PUBLISHER_GRACE, IssueState
from misthos.domain.timers import TIMED_STATES, Clocks, TimedAction, due

T0 = datetime(2026, 10, 1, 12, tzinfo=UTC)
A_MOMENT = timedelta(seconds=1)


class TestClaims:
    def test_no_timer_expires_a_claim(self) -> None:
        """In the open claim model, claims never expire and no timer expires a claim."""
        clocks = Clocks(IssueState.FUNDED, deadline=T0 + ESCROW_TERM)
        # 3 days later (the old 72h window): no timer fires
        assert due(clocks, T0 + timedelta(days=3)) is None
        assert not hasattr(TimedAction, "EXPIRE_CLAIM")
        assert not hasattr(clocks, "claim_expires_at")


class TestDeadline:
    def test_committed_money_waits_until_the_deadline(self) -> None:
        assert due(Clocks(IssueState.FUNDED, deadline=T0), T0) is None

    def test_an_ignored_issue_is_refunded_after_the_deadline(self) -> None:
        assert due(Clocks(IssueState.FUNDED, deadline=T0), T0 + A_MOMENT) is TimedAction.REFUND


class TestSilentPublisher:
    def accepted(self) -> Clocks:
        return Clocks(IssueState.IN_REVIEW, deadline=T0 + ESCROW_TERM, verdict_passed_at=T0)

    def test_a_passing_verdict_waits_out_the_grace_period(self) -> None:
        assert due(self.accepted(), T0 + SILENT_PUBLISHER_GRACE) is None

    def test_a_silent_publisher_does_not_strand_finished_work(self) -> None:
        moment = T0 + SILENT_PUBLISHER_GRACE + A_MOMENT
        assert due(self.accepted(), moment) is TimedAction.RELEASE_AFTER_GRACE

    def test_only_a_passing_verdict_starts_the_grace_period(self) -> None:
        clocks = Clocks(IssueState.IN_REVIEW, deadline=T0, verdict_passed_at=None)
        assert due(clocks, T0 + ESCROW_TERM) is None

    def test_the_grace_is_capped_at_the_escrow_deadline(self) -> None:
        """A verdict that leaves less than a full grace on the clock releases at the
        deadline. Uncapped it would release after `release` has started reverting, and
        the work the verdict passed would be refunded instead. See #33."""
        clocks = Clocks(
            IssueState.IN_REVIEW,
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
    clocks = Clocks(state, deadline=T0, verdict_passed_at=T0)
    assert due(clocks, T0 + timedelta(days=365)) is None
