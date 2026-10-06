"""Pricing engine.

Scores an issue on six signals, turns that into an effort estimate, then applies
market context and the publisher's affordability ceiling to produce a band rather
than a single number. A band invites the publisher to pick a position; a single
figure invites an argument about whether it is exactly right.

Weights are a starting guess and should be tuned against real settlements.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from misthos.domain.money import Usdc

# Signal weights, summing to 1.0.
WEIGHTS: dict[str, float] = {
    "code_surface": 0.25,
    "requirement_clarity": 0.20,
    "test_coverage": 0.15,
    "dependency_depth": 0.15,
    "prior_attempts": 0.15,
    "blast_radius": 0.10,
}

# Reference rate per hour by rough seniority band.
RATE_PER_HOUR = Usdc.from_decimal("90")

# The floor is derived, not chosen: review costs roughly 6.31 per issue, so at the
# Open tier's 12% take rate the break-even is about 53. Below it the platform
# declines the issue rather than subsidising it. Tiers with a thinner take rate
# need a higher floor (see docs/misthos/06-pricing-engine.md).
MIN_FIX_PRICE = Usdc.from_decimal("55")

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

    @property
    def comparables_note(self) -> str:
        if self.confidence == "high":
            return "Backed by several recently settled issues of similar shape."
        if self.confidence == "medium":
            return "A few comparable issues settled recently. Treat the band as indicative."
        return "Little comparable history. The band rests on the signals above."


def estimate_hours(signals: ComplexitySignals) -> float:
    signals.validate()
    score = sum(signals.as_dict()[name] * weight for name, weight in WEIGHTS.items())
    # A 1.0 score is a trivial change; a 5.0 is a substantial one. Map to hours.
    return round(2 + (score - 1) * 6.5, 1)


def complexity_multiplier(signals: ComplexitySignals) -> Decimal:
    score = sum(signals.as_dict()[name] * weight for name, weight in WEIGHTS.items())
    return Decimal("1.0") + (Decimal(str(score)) - Decimal("1.0")) * Decimal("0.15")


def confidence_for(comparables: int) -> str:
    if comparables >= 6:
        return "high"
    if comparables >= 2:
        return "medium"
    return "low"


def propose(
    signals: ComplexitySignals,
    *,
    urgency: Usdc | None = None,
    risk_premium: Usdc | None = None,
    rate_per_hour: Usdc = RATE_PER_HOUR,
    compliance_driven: bool = False,
    comparables: int = 0,
    affordability_ceiling: Usdc | None = None,
) -> PriceProposal:
    """Produce a price band with a written justification."""
    hours = estimate_hours(signals)
    multiplier = complexity_multiplier(signals)

    fix_price = Usdc(int(rate_per_hour.base_units * hours * float(multiplier)))
    if risk_premium is not None:
        fix_price = fix_price + risk_premium
    if urgency is not None:
        fix_price = fix_price + urgency

    # A compliance obligation changes willingness to pay more than complexity does.
    if compliance_driven:
        fix_price = fix_price * Decimal("1.15")

    low = Usdc(int(fix_price.base_units * float(BAND_LOW)))
    high = Usdc(int(fix_price.base_units * float(BAND_HIGH)))

    confidence = confidence_for(comparables)
    recommended = Usdc(int((low.base_units + high.base_units) / 2))

    # The ceiling never raises a price. It caps one, and it says so.
    fundable = True
    notes: list[str] = []
    if affordability_ceiling is not None and recommended > affordability_ceiling:
        if affordability_ceiling.base_units > 0:
            recommended = affordability_ceiling
            high = affordability_ceiling
            low = Usdc(int(affordability_ceiling.base_units * 0.6))
            notes.append(
                f"Capped at the publisher's remaining budget of {affordability_ceiling}."
            )
        else:
            # The budget covers nothing. Say so rather than producing a number
            # nobody should accept.
            fundable = False
            notes.append(
                f"The remaining budget of {affordability_ceiling} does not cover the "
                "work, so this issue cannot be funded as scoped. Consider splitting it."
            )

    if fundable and recommended < MIN_FIX_PRICE:
        # Below the floor review costs more than the take it earns, so decline the
        # issue rather than publish a price the platform loses money on.
        fundable = False
        notes.append(
            f"At {recommended} this issue is below the {MIN_FIX_PRICE} minimum, which "
            "is where review pays for itself. Consider bundling it with related work."
        )

    return PriceProposal(
        band_low=low,
        band_high=high,
        recommended=recommended,
        estimated_hours=hours,
        complexity_score=round(
            sum(signals.as_dict()[n] * w for n, w in WEIGHTS.items()), 2
        ),
        confidence=confidence,
        signals=signals.as_dict(),
        justification=_justify(signals, hours, confidence, compliance_driven, notes),
        fundable=fundable,
    )


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
        parts.append("A compliance obligation is attached, which raises the band.")
    parts.append(f"Confidence is {confidence}.")
    if extra:
        parts.extend(extra)
    return " ".join(parts)
