"""Compliance rules at payout, judged without a provider, a database or a chain."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from misthos.domain.compliance import (
    PAYOUT_RETRY_INTERVAL,
    RESCREEN_INTERVAL,
    IdentityStatus,
    PayoutGate,
    ScreeningOutcome,
    payout_gate,
    rescreen_due,
)
from misthos.domain.issue import IssueState
from misthos.domain.timers import Clocks, TimedAction, due

T0 = datetime(2026, 10, 1, 12, tzinfo=UTC)


class TestPayoutGate:
    def test_a_clear_verified_contributor_is_paid(self) -> None:
        assert payout_gate(ScreeningOutcome.CLEAR, IdentityStatus.VERIFIED) is PayoutGate.RELEASE

    @pytest.mark.parametrize("identity", list(IdentityStatus))
    def test_a_listed_party_is_never_paid_whatever_else_is_true(
        self, identity: IdentityStatus
    ) -> None:
        assert payout_gate(ScreeningOutcome.HIT, identity) is PayoutGate.BLOCKED_SANCTIONS

    @pytest.mark.parametrize("identity", [IdentityStatus.UNVERIFIED, IdentityStatus.PENDING])
    def test_a_first_payout_waits_for_identity(self, identity: IdentityStatus) -> None:
        assert payout_gate(ScreeningOutcome.CLEAR, identity) is PayoutGate.AWAIT_IDENTITY

    def test_a_failed_verification_waits_for_a_person(self) -> None:
        gate = payout_gate(ScreeningOutcome.CLEAR, IdentityStatus.FAILED)
        assert gate is PayoutGate.BLOCKED_IDENTITY


class TestRescreening:
    def test_a_party_never_screened_is_due(self) -> None:
        assert rescreen_due(None, T0)

    def test_a_fresh_check_is_not_repeated(self) -> None:
        assert not rescreen_due(T0, T0 + RESCREEN_INTERVAL - timedelta(seconds=1))

    def test_a_day_old_check_is_repeated(self) -> None:
        assert rescreen_due(T0, T0 + RESCREEN_INTERVAL)


class TestHeldPayouts:
    def held(self) -> Clocks:
        return Clocks(IssueState.ACCEPTED, deadline=T0 + timedelta(days=14), payout_held_since=T0)

    def test_a_held_payout_is_not_rechecked_every_sweep(self) -> None:
        assert due(self.held(), T0 + PAYOUT_RETRY_INTERVAL) is None

    def test_a_held_payout_is_checked_again_after_the_interval(self) -> None:
        moment = T0 + PAYOUT_RETRY_INTERVAL + timedelta(seconds=1)
        assert due(self.held(), moment) is TimedAction.RETRY_PAYOUT
