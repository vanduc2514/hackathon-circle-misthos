"""Domain tests.

No database, no chain, no HTTP. The domain layer is IO-free, so these run in
milliseconds and cover the two places where a bug costs real money: money units
and lifecycle transitions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from misthos.domain import comparables
from misthos.domain.comparables import SettledWork
from misthos.domain.issue import (
    ESCROW_TERM,
    SILENT_PUBLISHER_GRACE,
    IllegalTransition,
    IssueState,
    can_transition,
    is_open,
    silent_release_at,
    transition,
)
from misthos.domain.money import NativeUsdc, Usdc, format_usdc
from misthos.domain.pricing import (
    RELIST_UPLIFT,
    REVIEW_COST_PER_ISSUE,
    TAKE_RATE_BY_TIER,
    WEIGHTS,
    ComplexitySignals,
    effort,
    min_fix_price,
    platform_fee,
    propose,
    relist,
    take_rate_bps,
)


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
        ):
            state = transition(state, nxt)
        assert state is IssueState.PAID

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
        for state in (IssueState.PAID, IssueState.REFUNDED):
            assert not is_open(state)

    def test_nothing_follows_payment(self) -> None:
        """Payment is the end of the line, now that the review fee is gone."""
        for nxt in IssueState:
            assert not can_transition(IssueState.PAID, nxt)

    def test_a_silent_publisher_has_a_bounded_grace_window(self) -> None:
        assert SILENT_PUBLISHER_GRACE == timedelta(days=7)

    def test_priced_issue_can_be_sent_back_for_repricing(self) -> None:
        assert can_transition(IssueState.AWAITING_APPROVAL, IssueState.DRAFT)


class TestGraceAgainstTheEscrowDeadline:
    """The grace and the escrow deadline are one window, not two timers.

    `release` reverts once the escrow deadline has passed, so a grace that ran past
    it would be a release path that exists on paper only.
    """

    def test_committed_funds_live_for_a_fortnight(self) -> None:
        assert ESCROW_TERM == timedelta(days=14)

    def test_an_early_verdict_still_gets_the_whole_grace(self) -> None:
        decided_at = datetime(2026, 10, 1, tzinfo=UTC)
        deadline = decided_at + ESCROW_TERM
        assert silent_release_at(decided_at, deadline) == decided_at + SILENT_PUBLISHER_GRACE

    def test_a_verdict_inside_the_last_week_releases_at_the_deadline(self) -> None:
        """Day ten of a fourteen-day funding window: six days of grace remain, not
        seven, so the release is the deadline rather than a day the contract has
        already closed."""
        decided_at = datetime(2026, 10, 1, tzinfo=UTC)
        deadline = decided_at + timedelta(days=4)
        assert silent_release_at(decided_at, deadline) == deadline

    def test_every_passing_verdict_before_the_deadline_has_a_reachable_release(self) -> None:
        """Whatever the verdict time, the release lands inside the escrow window."""
        funded_at = datetime(2026, 10, 1, tzinfo=UTC)
        deadline = funded_at + ESCROW_TERM
        for hours in range(0, 14 * 24, 7):
            decided_at = funded_at + timedelta(hours=hours)
            release_at = silent_release_at(decided_at, deadline)
            assert decided_at <= release_at <= deadline


class TestPricing:
    def test_effort_scales_with_complexity(self) -> None:
        small = ComplexitySignals(1, 1, 1, 1, 1, 1)
        large = ComplexitySignals(5, 5, 5, 5, 5, 5)
        assert propose(large).estimated_hours > propose(small).estimated_hours

    def test_band_brackets_the_recommendation(self) -> None:
        p = propose(ComplexitySignals(3, 3, 3, 3, 3, 3))
        assert p.band_low < p.recommended < p.band_high

    def test_the_cheapest_scoped_issue_clears_the_floor(self) -> None:
        """The engine's own minimum output must not fall below break-even."""
        p = propose(ComplexitySignals(1, 1, 1, 1, 1, 1))
        assert p.fundable
        assert p.recommended >= min_fix_price("open")

    def test_every_tier_floor_clears_its_own_review_cost(self) -> None:
        """This is the point of deriving the floor per tier instead of picking one."""
        for tier, rate in TAKE_RATE_BY_TIER.items():
            floor = min_fix_price(tier)
            assert floor * rate >= REVIEW_COST_PER_ISSUE, tier

    def test_the_floor_rises_as_the_take_rate_thins(self) -> None:
        assert min_fix_price("open") < min_fix_price("team") < min_fix_price("enterprise")

    def test_the_platform_fee_is_the_tier_rate_of_the_fix_price(self) -> None:
        """The commission comes out of the price the publisher already approved."""
        price = Usdc.from_decimal("500")
        assert platform_fee(price, "open") == Usdc.from_decimal("60")
        assert platform_fee(price, "team") == Usdc.from_decimal("50")
        assert platform_fee(price, "enterprise") == Usdc.from_decimal("40")

    def test_the_platform_fee_floors_like_the_escrow_does(self) -> None:
        """Basis points and integer division, so the recorded fee is the transfer."""
        odd = Usdc(55_555_555)
        assert take_rate_bps("open") == 1200
        assert platform_fee(odd, "open").base_units == odd.base_units * 1200 // 10_000

    def test_every_tier_rate_fits_the_escrows_ceiling(self) -> None:
        """`MisthosEscrow.MAX_FEE_BPS` is 1500; a higher tier could never be set."""
        assert all(0 < take_rate_bps(tier) <= 1500 for tier in TAKE_RATE_BY_TIER)

    def test_the_fee_never_takes_the_whole_commitment(self) -> None:
        commitment = Usdc.from_decimal("55")
        for tier in TAKE_RATE_BY_TIER:
            assert platform_fee(commitment, tier) < commitment

    def test_the_floor_is_applied_at_the_publisher_tier(self) -> None:
        """A price that clears the Open floor can still fail the Enterprise one."""
        signals = ComplexitySignals(4, 4, 4, 4, 4, 4)
        ceiling = Usdc.from_decimal("60")
        assert propose(signals, affordability_ceiling=ceiling, tier="open").fundable is True
        assert propose(signals, affordability_ceiling=ceiling, tier="enterprise").fundable is False

    def test_a_ceiling_below_the_floor_reports_both_reasons(self) -> None:
        """The cap fires first, then the floor declines what the cap produced."""
        p = propose(
            ComplexitySignals(4, 4, 4, 4, 4, 4),
            affordability_ceiling=Usdc.from_decimal("50"),
        )
        assert p.fundable is False
        assert "Capped" in p.justification
        assert "minimum" in p.justification

    def test_a_price_below_the_floor_is_declined_not_published(self) -> None:
        """Below the floor, review costs more than the take it earns."""
        p = propose(ComplexitySignals(1, 1, 1, 1, 1, 1), rate_per_hour=Usdc.from_decimal("10"))
        assert p.recommended < min_fix_price("open")
        assert p.fundable is False
        assert "minimum" in p.justification

    def test_compliance_obligation_raises_the_price(self) -> None:
        signals = ComplexitySignals(3, 3, 3, 3, 3, 3)
        assert propose(signals, compliance_driven=True).recommended > propose(
            signals, compliance_driven=False
        ).recommended

    def test_affordability_ceiling_caps_but_never_raises(self) -> None:
        signals = ComplexitySignals(4, 4, 4, 4, 4, 4)
        uncapped = propose(signals)
        ceiling = Usdc.from_decimal("2000")
        capped = propose(signals, affordability_ceiling=ceiling)
        assert capped.fundable
        assert capped.recommended <= ceiling
        assert capped.recommended < uncapped.recommended
        assert "Capped" in capped.justification

    def test_a_budget_that_buys_nothing_is_declined_not_capped(self) -> None:
        """A ceiling of zero cannot buy anything, so say so."""
        signals = ComplexitySignals(5, 5, 5, 5, 5, 5)
        p = propose(signals, affordability_ceiling=Usdc.from_decimal("0"))
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

    def test_without_history_confidence_is_low_and_says_why(self) -> None:
        p = propose(ComplexitySignals(3, 3, 3, 3, 3, 3))
        assert p.confidence == "low"
        assert p.comparables == ()
        assert "No settled issue of similar shape" in p.comparables_note

    def test_the_cap_does_not_publish_the_budget(self) -> None:
        """The justification is public; what remains of a budget is not."""
        ceiling = Usdc.from_decimal("1234.56")
        p = propose(ComplexitySignals(4, 4, 4, 4, 4, 4), affordability_ceiling=ceiling)
        assert "Capped" in p.justification and "1,234.56" not in p.justification
        read = propose(
            ComplexitySignals(4, 4, 4, 4, 4, 4),
            affordability_ceiling=ceiling,
            ceiling_source="Firefly III",
        )
        assert "as Firefly III reports it" in read.justification

    def test_justification_names_the_driving_signals(self) -> None:
        p = propose(ComplexitySignals(5, 5, 1, 1, 1, 1))
        assert "code surface" in p.justification or "requirement clarity" in p.justification


class TestRelist:
    """An issue nobody claimed comes back priced to draw a claim, within the budget."""

    def test_an_unclaimed_issue_comes_back_a_quarter_higher(self) -> None:
        first = propose(ComplexitySignals(3, 3, 3, 3, 3, 3))
        again = relist(first)
        assert again is not None
        assert again.recommended == first.recommended * RELIST_UPLIFT
        assert again.band_low < again.recommended < again.band_high
        assert "Re-listed after no contributor claimed it" in again.justification

    def test_the_budget_caps_a_relisted_price(self) -> None:
        first = propose(ComplexitySignals(3, 3, 3, 3, 3, 3))
        ceiling = first.recommended * Decimal("1.1")
        again = relist(first, ceiling=ceiling)
        assert again is not None
        assert again.recommended == ceiling
        assert again.band_high == ceiling
        assert again.band_low <= ceiling

    def test_a_budget_that_cannot_rise_declines_the_relist(self) -> None:
        """Re-listing at the price that already drew nobody would be no re-list at all."""
        first = propose(ComplexitySignals(3, 3, 3, 3, 3, 3))
        assert relist(first, ceiling=first.recommended) is None


NOW = datetime(2026, 10, 7, tzinfo=UTC)
MID = ComplexitySignals(3, 3, 3, 3, 3, 3)


def settled(issue_id: str, price: str, signals: ComplexitySignals = MID, *, days_ago: int = 10,
            repo: str = "acme/ledger") -> SettledWork:  # fmt: skip
    return SettledWork(
        issue_id=issue_id,
        repo=repo,
        title=f"settled {issue_id}",
        signals=signals.as_dict(),
        effort=effort(signals),
        price=Usdc.from_decimal(price),
        settled_at=NOW - timedelta(days=days_ago),
    )


def found(history: list[SettledWork], signals: ComplexitySignals = MID, **kw: object) -> list:
    return comparables.find(
        signals.as_dict(), effort(signals), history, weights=WEIGHTS, now=NOW, **kw  # type: ignore[arg-type]
    )


class TestComparables:
    """#42: comparables come from settled history, and confidence follows them."""

    def test_only_work_of_the_same_shape_counts(self) -> None:
        history = [
            settled("ISS-1", "1000", ComplexitySignals(3, 3, 3, 3, 3, 3.5)),
            settled("ISS-2", "1000", ComplexitySignals(5, 5, 5, 5, 5, 5)),
        ]
        assert [c.work.issue_id for c in found(history)] == ["ISS-1"]

    def test_old_settlements_do_not_count(self) -> None:
        assert found([settled("ISS-1", "1000", days_ago=400)]) == []

    def test_the_same_repository_ranks_first(self) -> None:
        history = [
            settled("ISS-1", "1000", ComplexitySignals(3, 3, 3, 3, 3, 3.2), repo="other/x"),
            settled("ISS-2", "1000", ComplexitySignals(3, 3, 3, 3, 3, 3.6)),
        ]
        ranked = found(history, repo="acme/ledger")
        assert [c.work.issue_id for c in ranked] == ["ISS-2", "ISS-1"]

    def test_a_settled_price_is_scaled_to_this_issues_effort(self) -> None:
        small = ComplexitySignals(2.5, 2.5, 2.5, 2.5, 2.5, 2.5)
        (c,) = found([settled("ISS-1", "1000", small)])
        assert c.implied > Usdc.from_decimal("1000")

    def test_history_moves_the_price_toward_what_it_implies(self) -> None:
        formula = propose(MID)
        cheaper = found([settled("ISS-1", "400"), settled("ISS-2", "420"), settled("ISS-3", "410")])
        p = propose(MID, comparables=cheaper)
        assert p.recommended < formula.recommended
        # Three comparables move it half of the way, never all of it.
        assert p.recommended > Usdc.from_decimal("420")
        assert "moved 50% of the way" in p.justification
        assert p.band_low < p.recommended < p.band_high

    def test_one_comparable_moves_it_a_quarter_of_the_way(self) -> None:
        assert comparables.pull(1) == Decimal("0.25")
        assert comparables.pull(10) == Decimal("0.5")

    def test_confidence_is_high_only_when_enough_history_agrees(self) -> None:
        agreeing = found([settled(f"ISS-{i}", p) for i, p in enumerate(["900", "1000", "1100"])])
        assert propose(MID, comparables=agreeing).confidence == "high"
        split = found([settled(f"ISS-{i}", p) for i, p in enumerate(["300", "1000", "2500"])])
        assert propose(MID, comparables=split).confidence == "medium"
        assert propose(MID, comparables=agreeing[:1]).confidence == "medium"

    def test_the_publisher_is_shown_the_closest_three(self) -> None:
        history = [settled(f"ISS-{i}", "1000") for i in range(5)]
        p = propose(MID, comparables=found(history))
        assert len(p.comparables) == 3
        assert all(ref.issue_id in p.comparables_note for ref in p.comparables)
