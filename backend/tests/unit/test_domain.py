"""Domain tests.

No database, no chain, no HTTP. The domain layer is IO-free, so these run in
milliseconds and cover the two places where a bug costs real money: money units
and lifecycle transitions.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from misthos.domain.issue import (
    IllegalTransition,
    IssueState,
    can_transition,
    is_open,
    transition,
)
from misthos.domain.money import NativeUsdc, Usdc, format_usdc
from misthos.domain.pricing import ComplexitySignals, confidence_for, propose


class TestUsdc:
    def test_from_decimal_round_trips(self) -> None:
        assert Usdc.from_decimal("12.34").decimal == Decimal("12.34")

    def test_base_units_use_six_decimals(self) -> None:
        assert Usdc.from_decimal("1").base_units == 1_000_000

    def test_rejects_more_precision_than_usdc_supports(self) -> None:
        with pytest.raises(ValueError, match="precision"):
            Usdc.from_decimal("0.0000001")

    def test_addition_and_subtraction(self) -> None:
        a = Usdc.from_decimal("10.00")
        b = Usdc.from_decimal("2.50")
        assert (a + b).decimal == Decimal("12.50")
        assert (a - b).decimal == Decimal("7.50")

    def test_ordering(self) -> None:
        assert Usdc.from_decimal("1") < Usdc.from_decimal("2")
        assert Usdc.from_decimal("2") <= Usdc.from_decimal("2")

    def test_native_view_is_a_separate_type(self) -> None:
        """The 18-decimal gas view must not be substitutable for the 6-decimal one."""
        native = NativeUsdc(wei=10**18)
        assert native.decimal == Decimal("1")
        assert not isinstance(native, Usdc)

    def test_formatting(self) -> None:
        assert format_usdc(Usdc.from_decimal("1234.5")) == "1,234.50"


class TestLifecycle:
    def test_happy_path(self) -> None:
        state = IssueState.DRAFT
        for nxt in (
            IssueState.PRICED,
            IssueState.AWAITING_APPROVAL,
            IssueState.FUNDED,
            IssueState.CLAIMED,
            IssueState.IN_REVIEW,
            IssueState.ACCEPTED,
            IssueState.PAID,
            IssueState.REVIEWED,
        ):
            state = transition(state, nxt)
        assert state is IssueState.REVIEWED

    def test_cannot_skip_the_human_price_checkpoint(self) -> None:
        with pytest.raises(IllegalTransition):
            transition(IssueState.PRICED, IssueState.FUNDED)

    def test_cannot_accept_work_that_was_never_submitted(self) -> None:
        with pytest.raises(IllegalTransition):
            transition(IssueState.CLAIMED, IssueState.ACCEPTED)

    def test_cannot_pay_before_acceptance(self) -> None:
        with pytest.raises(IllegalTransition):
            transition(IssueState.IN_REVIEW, IssueState.PAID)

    def test_expired_claim_returns_the_issue_to_the_pool(self) -> None:
        assert can_transition(IssueState.CLAIMED, IssueState.FUNDED)

    def test_funded_issue_can_refund(self) -> None:
        assert can_transition(IssueState.FUNDED, IssueState.REFUNDED)

    def test_terminal_states_are_terminal(self) -> None:
        for state in (IssueState.REVIEWED, IssueState.REFUNDED):
            assert not is_open(state)

    def test_priced_issue_can_be_sent_back_for_repricing(self) -> None:
        assert can_transition(IssueState.AWAITING_APPROVAL, IssueState.DRAFT)


class TestPricing:
    def test_effort_scales_with_complexity(self) -> None:
        small = ComplexitySignals(1, 1, 1, 1, 1, 1)
        large = ComplexitySignals(5, 5, 5, 5, 5, 5)
        assert propose(large).estimated_hours > propose(small).estimated_hours

    def test_band_brackets_the_recommendation(self) -> None:
        p = propose(ComplexitySignals(3, 3, 3, 3, 3, 3))
        assert p.band_low < p.recommended < p.band_high

    def test_review_fee_has_a_floor(self) -> None:
        p = propose(ComplexitySignals(1, 1, 1, 1, 1, 1))
        assert p.review_fee.decimal >= Decimal("25")

    def test_publisher_total_is_price_plus_review_fee(self) -> None:
        p = propose(ComplexitySignals(3, 2, 3, 2, 2, 2))
        assert p.publisher_total.base_units == p.recommended.base_units + p.review_fee.base_units

    def test_compliance_obligation_raises_the_price(self) -> None:
        signals = ComplexitySignals(3, 3, 3, 3, 3, 3)
        assert propose(signals, compliance_driven=True).recommended > propose(
            signals, compliance_driven=False
        ).recommended

    def test_affordability_ceiling_caps_but_never_raises(self) -> None:
        signals = ComplexitySignals(4, 4, 4, 4, 4, 4)
        uncapped = propose(signals)
        ceiling = Usdc.from_decimal("3200")
        capped = propose(signals, affordability_ceiling=ceiling)
        assert capped.fundable
        assert capped.recommended + capped.review_fee <= ceiling
        assert capped.recommended < uncapped.recommended
        assert "Capped" in capped.justification

    def test_a_budget_below_the_review_fee_is_declined_not_capped(self) -> None:
        """A ceiling smaller than the review fee cannot buy anything, so say so."""
        signals = ComplexitySignals(5, 5, 5, 5, 5, 5)
        p = propose(signals, affordability_ceiling=Usdc.from_decimal("10"))
        assert p.fundable is False
        assert "cannot be funded" in p.justification

    def test_a_generous_ceiling_changes_nothing(self) -> None:
        signals = ComplexitySignals(2, 2, 2, 2, 2, 2)
        assert propose(signals, affordability_ceiling=Usdc.from_decimal("999999")).recommended == (
            propose(signals).recommended
        )

    def test_signals_outside_the_scale_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="between 1 and 5"):
            propose(ComplexitySignals(9, 1, 1, 1, 1, 1))

    def test_confidence_tracks_comparable_history(self) -> None:
        assert confidence_for(0) == "low"
        assert confidence_for(3) == "medium"
        assert confidence_for(10) == "high"

    def test_justification_names_the_driving_signals(self) -> None:
        p = propose(ComplexitySignals(5, 5, 1, 1, 1, 1))
        assert "code surface" in p.justification or "requirement clarity" in p.justification
