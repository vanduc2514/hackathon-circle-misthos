"""Pricing: the fee the platform charges, and the advice it offers.

**The publisher names the price.** Nothing here refuses it, binds it into a band or
caps it against a budget. What the work is worth is the one input the buyer holds and
the platform cannot supply, and telling a publisher their own number is wrong is the
fastest way to lose them.

What still lives here is the *suggestion*: an effort estimate from six signals, moved
toward what comparable settled work actually paid, and the range that implies. It is
advice on the page, never a condition of publishing. It survives because it is the one
part of the engine that gets better with every settlement, and because it costs the
publisher nothing to ignore (docs/misthos/06-pricing-engine.md).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from statistics import median

from misthos.domain import comparables as history
from misthos.domain.comparables import Comparable, ComparableRef
from misthos.domain.money import Usdc

# What the platform keeps of every settlement, in the basis points the escrow takes
# and enforces (`MisthosEscrow.release`, bounded there at MAX_FEE_BPS = 1500).
# One rate for everyone: a rate that moves with what a publisher buys is a second
# price for the same service, and it made what a publisher could spend depend on
# which plan they happened to be on.
PLATFORM_FEE_BPS = 1_000


def fee_bps() -> int:
    """The platform's share of a settlement, in basis points."""
    return PLATFORM_FEE_BPS


def platform_fee(amount: Usdc) -> Usdc:
    """The platform's cut of a settlement.

    Integer basis points and floor division mirror `MisthosEscrow.release` exactly,
    which is what keeps the recorded fee equal to the on-chain transfer.
    """
    return Usdc(amount.base_units * PLATFORM_FEE_BPS // 10_000)


def contributor_share(amount: Usdc) -> Usdc:
    """What the contributor receives: the committed amount less the platform's fee."""
    return amount - platform_fee(amount)


# The bounds on a price. The floor catches a typo and damps spam; it is not derived
# from the platform's costs, because review is the platform's own expense to carry
# on the small work it most wants a publisher to start with.
MINIMUM_PRICE = Usdc.from_decimal("5")
MAXIMUM_PRICE = Usdc.from_decimal("100000")


class PriceRefused(Exception):
    """A price outside the bounds, with the bounds that would work."""

    def __init__(self, justification: str) -> None:
        super().__init__(justification)
        self.justification = justification


def check_price(amount: Usdc) -> Usdc:
    """The price as given, or a refusal naming the bounds. The only refusal left.

    Nothing about the work, the publisher's budget or what the engine would have
    suggested is consulted: a price within the bounds is the publisher's call.
    """
    if amount < MINIMUM_PRICE or amount > MAXIMUM_PRICE:
        raise PriceRefused(
            f"{amount} is outside the {MINIMUM_PRICE} to {MAXIMUM_PRICE} a price may be"
        )
    return amount


# Signal weights for the suggestion, summing to 1.0. A starting guess: they order
# which signals matter when the engine reads an issue, and nothing is refused on them.
WEIGHTS: dict[str, float] = {
    "code_surface": 0.25,
    "requirement_clarity": 0.20,
    "test_coverage": 0.15,
    "dependency_depth": 0.15,
    "prior_attempts": 0.15,
    "blast_radius": 0.10,
}

# Reference rate per hour for the suggestion. Advisory: the publisher's price is the
# one that binds, and a buyer knows their own market rate better than a constant.
# The suggestion is optimistic at the top of its range, which is why nothing rests on it.
RATE_PER_HOUR = Usdc.from_decimal("90")

# What one issue costs the platform to review: agent inference, settlement and
# amortised screening. Kept here because it explains the platform's own economics.
# It no longer sets a floor under anyone's price.
REVIEW_COST_PER_ISSUE = Usdc.from_decimal("6.31")


# The precision a price is shown at.
CENT = Decimal("0.01")

# How wide the suggestion's range is around its point. Wide on purpose: a range that
# admits its own uncertainty is harder to mistake for an instruction.
BAND_LOW = Decimal("0.7")
BAND_HIGH = Decimal("1.4")


@dataclass(frozen=True)
class ComplexitySignals:
    """Each signal is scored 1 to 5. Higher means more work."""

    code_surface: float = 1.0
    requirement_clarity: float = 1.0
    test_coverage: float = 1.0
    dependency_depth: float = 1.0
    prior_attempts: float = 1.0
    blast_radius: float = 1.0

    def validate(self) -> None:
        for name, value in self.as_dict().items():
            if not 1.0 <= value <= 5.0:
                raise ValueError(f"signal {name} must be between 1 and 5, got {value}")

    def as_dict(self) -> dict[str, float]:
        return {
            "code_surface": self.code_surface,
            "requirement_clarity": self.requirement_clarity,
            "test_coverage": self.test_coverage,
            "dependency_depth": self.dependency_depth,
            "prior_attempts": self.prior_attempts,
            "blast_radius": self.blast_radius,
        }


@dataclass(frozen=True)
class PriceProposal:
    band_low: Usdc
    band_high: Usdc
    recommended: Usdc
    estimated_hours: float
    complexity_score: float
    confidence: str
    signals: dict[str, float] = field(default_factory=dict)
    justification: str = ""
    fundable: bool = True
    comparables: tuple[ComparableRef, ...] = ()
    """The settled issues of similar shape the engine found, closest first."""

    @property
    def comparables_note(self) -> str:
        if not self.comparables:
            return "No settled issue of similar shape yet. The band rests on the signals above."
        prices = ", ".join(f"{c.issue_id} at {c.price}" for c in self.comparables)
        if self.confidence == "high":
            return f"Backed by settled issues of similar shape that agree: {prices}."
        return f"Comparable settled issues: {prices}. Treat the band as indicative."


def score(signals: ComplexitySignals, weights: dict[str, float] | None = None) -> float:
    """The weighted complexity score, 1 to 5."""
    w = WEIGHTS if weights is None else weights
    return sum(signals.as_dict()[name] * weight for name, weight in w.items())


def estimate_hours(signals: ComplexitySignals, weights: dict[str, float] | None = None) -> float:
    signals.validate()
    score_ = score(signals, weights)
    # A 1.0 score is a trivial change; a 5.0 is a substantial one. Map to hours.
    return round(2 + (score_ - 1) * 6.5, 1)


def complexity_multiplier(
    signals: ComplexitySignals, weights: dict[str, float] | None = None
) -> Decimal:
    score_ = score(signals, weights)
    return Decimal("1.0") + (Decimal(str(score_)) - Decimal("1.0")) * Decimal("0.15")


def effort(signals: ComplexitySignals, weights: dict[str, float] | None = None) -> float:
    """Hours times the complexity multiplier: what the formula charges the rate for,
    and what scales one settled price to another issue."""
    return estimate_hours(signals, weights) * float(complexity_multiplier(signals, weights))


def propose(
    signals: ComplexitySignals,
    *,
    urgency: Usdc | None = None,
    risk_premium: Usdc | None = None,
    rate_per_hour: Usdc = RATE_PER_HOUR,
    compliance_driven: bool = False,
    comparables: Sequence[Comparable] = (),
    weights: dict[str, float] | None = None,
) -> PriceProposal:
    """A suggested price and the reasoning for it, for the page and the record.

    Advice only. The publisher names the price and this is never consulted to bound
    it: `check_price` is the only refusal in the price path. The proposal keeps its
    shape and its name because the reasoning and the comparables are worth keeping;
    what is gone is its authority.

    `comparables` are settled issues of similar shape (`comparables.find`); they
    move the suggestion toward what that work settled at and set the confidence.
    """
    hours = estimate_hours(signals, weights)
    multiplier = complexity_multiplier(signals, weights)

    fix_price = Usdc(int(rate_per_hour.base_units * hours * float(multiplier)))
    if risk_premium is not None:
        fix_price = fix_price + risk_premium
    if urgency is not None:
        fix_price = fix_price + urgency

    notes: list[str] = []
    if comparables:
        # History moves the formula's recommendation toward what it implies; the band
        # keeps its shape around wherever the recommendation lands.
        midpoint = (BAND_LOW + BAND_HIGH) / 2
        formula = Usdc(int(fix_price.base_units * float(midpoint)))
        moved = history.anchored(formula, comparables)
        fix_price = Usdc(int(Decimal(moved.base_units) / midpoint))
        share = int(history.pull(len(comparables)) * 100)
        noun = "issue" if len(comparables) == 1 else "issues"
        notes.append(
            f"{len(comparables)} settled {noun} of similar shape put it nearer "
            f"{_implied(comparables)}, so the price moved {share}% of the way there."
        )

    low = Usdc(int(fix_price.base_units * float(BAND_LOW)))
    high = Usdc(int(fix_price.base_units * float(BAND_HIGH)))

    confidence = history.confidence(comparables)
    recommended = Usdc(int((low.base_units + high.base_units) / 2))

    # Nothing here refuses an issue. The flag is kept because the stored proposal and
    # the API carry it, and it is always true: a price the publisher is willing to pay
    # is publishable whatever this engine thinks of it.
    fundable = True

    return PriceProposal(
        band_low=low,
        band_high=high,
        recommended=recommended,
        estimated_hours=hours,
        complexity_score=round(score(signals, weights), 2),
        confidence=confidence,
        signals=signals.as_dict(),
        justification=_justify(signals, hours, confidence, compliance_driven, notes),
        fundable=fundable,
        comparables=history.refs(comparables),
    )


def _implied(comparables: Sequence[Comparable]) -> Usdc:
    return Usdc(int(median(c.implied.base_units for c in comparables)))


def _justify(
    signals: ComplexitySignals,
    hours: float,
    confidence: str,
    compliance_driven: bool,
    extra: list[str],
) -> str:
    drivers = sorted(signals.as_dict().items(), key=lambda kv: kv[1], reverse=True)
    top = [name.replace("_", " ") for name, value in drivers[:2] if value >= 3]
    parts = [f"Estimated at {hours} hours of work."]
    if top:
        if len(top) > 1:
            parts.append(f"Driven mainly by {top[0]} and {top[1]}.")
        else:
            parts.append(f"Driven mainly by {top[0]}.")
    else:
        parts.append("All signals are low, so this looks like a contained change.")
    if compliance_driven:
        parts.append(
            "A compliance obligation is attached, which buyers usually pay more for."
        )
    parts.append(f"Confidence is {confidence}.")
    if extra:
        parts.extend(extra)
    return " ".join(parts)


def suggested_range(proposal: PriceProposal) -> tuple[Usdc, Usdc]:
    """The range to show next to the suggestion. Advisory, and nothing more.

    It is not a window the publisher must price inside: `check_price` holds the only
    bounds that bind. Rounded outwards to the cent, because a cent is the precision a
    price is shown at and a figure displayed as 126.00 should not read as 125.9968.
    """
    return (
        Usdc.from_decimal(proposal.band_low.decimal.quantize(CENT, rounding="ROUND_FLOOR")),
        Usdc.from_decimal(proposal.band_high.decimal.quantize(CENT, rounding="ROUND_CEILING")),
    )
