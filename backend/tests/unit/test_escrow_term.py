"""The escrow term a publisher names, and the bounds that keep it honest (#172)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from misthos.domain.issue import (
    ESCROW_TERM,
    MAX_ESCROW_TERM,
    MIN_ESCROW_TERM,
    RELEASE_MARGIN,
    SILENT_PUBLISHER_GRACE,
    EscrowTermRefused,
    escrow_term,
    silent_release_at,
)


class TestTheBounds:
    def test_no_term_named_is_the_fourteen_days_every_issue_had(self) -> None:
        assert escrow_term(None) == ESCROW_TERM == timedelta(days=14)

    def test_the_floor_refuses_a_term_short_enough_to_reclaim_before_anyone_works(self) -> None:
        assert escrow_term(3) == MIN_ESCROW_TERM
        with pytest.raises(EscrowTermRefused, match="at least 3 days"):
            escrow_term(2)

    def test_the_ceiling_refuses_a_term_long_enough_to_strand_the_money(self) -> None:
        assert escrow_term(90) == MAX_ESCROW_TERM
        with pytest.raises(EscrowTermRefused, match="at most 90 days"):
            escrow_term(91)

    @pytest.mark.parametrize("days", [0, -5, Decimal("-0")])
    def test_zero_or_a_negative_term_is_refused_with_the_bounds(self, days: object) -> None:
        with pytest.raises(EscrowTermRefused, match="3 to 90 days"):
            escrow_term(days)  # type: ignore[arg-type]

    @pytest.mark.parametrize("days", [Decimal("7.5"), "10.25", Decimal("3.000001")])
    def test_a_term_that_is_not_a_whole_number_of_days_is_refused(self, days: object) -> None:
        with pytest.raises(EscrowTermRefused, match="whole days"):
            escrow_term(days)  # type: ignore[arg-type]

    @pytest.mark.parametrize("days", ["seven", "", "NaN", "Infinity"])
    def test_a_term_that_is_not_a_number_is_refused(self, days: str) -> None:
        with pytest.raises(EscrowTermRefused, match="not a number of days"):
            escrow_term(days)

    def test_a_whole_number_written_with_decimals_is_still_whole(self) -> None:
        assert escrow_term(Decimal("21.0")) == timedelta(days=21)

    def test_the_bounds_can_be_configured(self) -> None:
        tight = {"shortest": timedelta(days=5), "longest": timedelta(days=30)}
        assert escrow_term(30, **tight) == timedelta(days=30)
        with pytest.raises(EscrowTermRefused, match="at least 5 days"):
            escrow_term(4, **tight)


class TestAShortTermAndTheGrace:
    def test_a_short_term_caps_the_grace_and_releases_before_the_deadline(self) -> None:
        """Three days is shorter than the seven-day grace, so every passing verdict on it
        is capped; the release must still land inside the escrow's window."""
        funded = datetime(2026, 10, 1, tzinfo=UTC)
        deadline = funded + MIN_ESCROW_TERM
        for hours in range(0, 72, 3):
            decided = funded + timedelta(hours=hours)
            assert decided + SILENT_PUBLISHER_GRACE > deadline
            assert silent_release_at(decided, deadline) == deadline - RELEASE_MARGIN < deadline
