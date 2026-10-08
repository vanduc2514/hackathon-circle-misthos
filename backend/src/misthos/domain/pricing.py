"""Pricing engine.

Scores an issue on six signals, turns that into an effort estimate, then applies
market context and the publisher's affordability ceiling to produce a band rather
than a single number. A band invites the publisher to pick a position; a single
figure invites an argument about whether it is exactly right.

Weights are a starting guess and should be tuned against real settlements
(`python -m misthos.services.calibration`, #41). Comparables come from the
platform's own settled issues (`domain/comparables.py`, #42): they move the price
toward what similar work actually settled at, and they alone set the confidence.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from statistics import median

from misthos.domain import comparables as history
from misthos.domain.comparables import Comparable, ComparableRef
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

# What one issue costs us to review: agent inference, settlement and amortised
# screening. It is not a line on the publisher's invoice, so it comes out of the
# take rate, and that is what sets the price floor.
REVIEW_COST_PER_ISSUE = Usdc.from_decimal("6.31")

# Take rate by publisher tier. The tier table in docs/misthos/06 is the source of
# truth for these, and the floor is derived from them rather than picked.
TAKE_RATE_BY_TIER: dict[str, Decimal] = {
    "open": Decimal("0.12"),
    "team": Decimal("0.10"),
    "enterprise": Decimal("0.08"),
}

# Published floors are the break-even rounded up to the next whole $5, so every
# issue clears its own review cost with a little room. Deriving it per tier is what
# stops the Enterprise rate, which is the thinnest, from subsidising small issues.
FLOOR_ROUNDING = 5


def min_fix_price(tier: str = "open") -> Usdc:
    """The lowest fix price we will publish for a tier, derived from break-even."""
    rate = TAKE_RATE_BY_TIER[tier]
    break_even = REVIEW_COST_PER_ISSUE.decimal / rate
    rounded = (break_even / FLOOR_ROUNDING).to_integral_value(rounding="ROUND_CEILING")
    return Usdc.from_decimal(rounded * FLOOR_ROUNDING)


def take_rate_bps(tier: str = "open") -> int:
    """The tier's take rate in basis points, which is the unit the escrow enforces."""
    return int(TAKE_RATE_BY_TIER[tier] * 10_000)


def platform_fee(amount: Usdc, tier: str = "open") -> Usdc:
    """The platform's cut of a settlement.

    Charged on the fix price rather than added to it, so it comes out of the
    commitment the publisher already approved. Integer basis points and floor
    division mirror `MisthosEscrow.release` exactly, which is what keeps the
    recorded fee equal to the on-chain transfer.
    """
    return Usdc(amount.base_units * take_rate_bps(tier) // 10_000)


BAND_LOW = Decimal("0.7")
BAND_HIGH = Decimal("1.4")

# How far a re-listed band rises after the first listing drew no claim. A starting
# guess like the weights: an issue nobody took at its price is evidence the price was
# low, and a quarter is enough to be seen without doubling what the publisher spends.
RELIST_UPLIFT = Decimal("1.25")


class UnfundableIssue(Exception):
    """The engine will not put a price on this issue, and says why."""

    def __init__(self, justification: str) -> None:
        super().__init__(justification)
        self.justification = justification


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
    affordability_ceiling: Usdc | None = None,
    ceiling_source: str | None = None,
    tier: str = "open",
    weights: dict[str, float] | None = None,
) -> PriceProposal:
    """Produce a price band with a written justification.

    `comparables` are settled issues of similar shape (`comparables.find`); they
    move the price toward what that work settled at and set the confidence. The
    ceiling is the publisher's remaining budget, from `ceiling_source` when it was
    read from their books rather than declared.
    """
    hours = estimate_hours(signals, weights)
    multiplier = complexity_multiplier(signals, weights)

    fix_price = Usdc(int(rate_per_hour.base_units * hours * float(multiplier)))
    if risk_premium is not None:
        fix_price = fix_price + risk_premium
    if urgency is not None:
        fix_price = fix_price + urgency

    # A compliance obligation changes willingness to pay more than complexity does.
    if compliance_driven:
        fix_price = fix_price * Decimal("1.15")

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

    # The ceiling never raises a price. It caps one, and it says so, without saying
    # how much budget is left: the justification is public, the budget is not.
    fundable = True
    where = f", as {ceiling_source} reports it" if ceiling_source else ""
    if affordability_ceiling is not None and recommended > affordability_ceiling:
        if affordability_ceiling.base_units > 0:
            recommended = affordability_ceiling
            high = affordability_ceiling
            low = Usdc(int(affordability_ceiling.base_units * 0.6))
            notes.append(f"Capped at what remains of the publisher's budget{where}.")
        else:
            # The budget covers nothing. Say so rather than producing a number
            # nobody should accept.
            fundable = False
            notes.append(
                f"What remains of the publisher's budget{where} does not cover the "
                "work, so this issue cannot be funded as scoped. Consider splitting it."
            )

    floor = min_fix_price(tier)
    if fundable and recommended < floor:
        # Below the floor review costs more than the take it earns, so decline the
        # issue rather than publish a price the platform loses money on.
        fundable = False
        notes.append(
            f"At {recommended} this issue is below the {floor} minimum for this "
            "publisher's tier, which is where review pays for itself. Consider "
            "bundling it with related work."
        )

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
        parts.append("A compliance obligation is attached, which raises the band.")
    parts.append(f"Confidence is {confidence}.")
    if extra:
        parts.extend(extra)
    return " ".join(parts)


def relist(proposal: PriceProposal, *, ceiling: Usdc | None = None) -> PriceProposal | None:
    """A higher band for an issue nobody claimed, or None when the budget cannot rise.

    The ceiling caps a re-listed price exactly as it caps a first one. A re-list the
    budget would hold at or below the old price is no re-list at all, so it is declined
    rather than published again at the number that already drew nobody.
    """
    low = proposal.band_low * RELIST_UPLIFT
    high = proposal.band_high * RELIST_UPLIFT
    recommended = proposal.recommended * RELIST_UPLIFT
    notes = [f"Re-listed after no contributor claimed it at {proposal.recommended}."]

    if ceiling is not None and recommended > ceiling:
        if not proposal.recommended < ceiling:
            return None
        recommended = high = ceiling
        low = low if low < ceiling else ceiling
        notes.append("Capped at what remains of the publisher's budget.")

    return replace(
        proposal,
        band_low=low,
        band_high=high,
        recommended=recommended,
        justification=" ".join([proposal.justification, *notes]),
    )
