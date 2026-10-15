"""When the Enterprise support commitment says a first response is due (#53)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from misthos.domain.plans import PLANS
from misthos.domain.support import COMMITMENT, URGENT_RESPONSE, overdue, respond_by

FRIDAY_3PM = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)


class TestTheDeadline:
    def test_money_that_cannot_move_is_answered_within_hours_even_at_the_weekend(self) -> None:
        saturday_night = datetime(2026, 10, 10, 23, 0, tzinfo=UTC)
        assert respond_by(saturday_night, "urgent") == saturday_night + URGENT_RESPONSE

    def test_anything_else_is_answered_by_the_same_time_the_next_working_day(self) -> None:
        tuesday = datetime(2026, 10, 6, 9, 30, tzinfo=UTC)
        assert respond_by(tuesday, "normal") == tuesday + timedelta(days=1)

    def test_the_weekend_does_not_count_as_a_working_day(self) -> None:
        monday_3pm = FRIDAY_3PM + timedelta(days=3)
        assert respond_by(FRIDAY_3PM, "normal") == monday_3pm
        sunday_noon = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
        assert respond_by(sunday_noon, "normal") == datetime(2026, 10, 12, 12, 0, tzinfo=UTC)

    def test_late_is_unanswered_past_the_deadline_or_answered_after_it(self) -> None:
        due = FRIDAY_3PM
        assert not overdue(due, None, due)
        assert overdue(due, None, due + timedelta(seconds=1))
        assert not overdue(due, due - timedelta(hours=1), due + timedelta(days=9))
        assert overdue(due, due + timedelta(minutes=1), due + timedelta(days=9))


def test_the_plan_promises_what_the_deadlines_enforce() -> None:
    assert PLANS["enterprise"].support == COMMITMENT
    assert "4 hours" in COMMITMENT and "next working day" in COMMITMENT
    assert PLANS["team"].support == PLANS["open"].support != COMMITMENT
