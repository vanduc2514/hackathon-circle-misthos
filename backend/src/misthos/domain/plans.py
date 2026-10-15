"""The plans an organisation can be on, and what each one includes (#53).

The tiers come from 06 ("How the platform charges"): Open is per issue with nothing
to buy, Team adds the organisation's own controls for $249 a month, and Enterprise
adds the compliance record, SSO and a support commitment from $2,000 a month. Team
is bought without a conversation; Enterprise is agreed with us, because "from" is a
price that is negotiated.

A plan is paid a period at a time, in USDC on Arc, like everything else here. A
period that ends unpaid has a grace period, then the organisation is back on Open.
Its spending policy keeps being enforced when it lapses: a limit that protects the
organisation's money is not a feature to switch off for non-payment.

Pure values; the store keeps who is on which plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum

from misthos.domain.money import Usdc
from misthos.domain.pricing import MINIMUM_PRICE, PLATFORM_FEE_BPS
from misthos.domain.support import COMMITMENT

PERIOD = timedelta(days=30)
GRACE = timedelta(days=7)
"""How long an unpaid period keeps its plan before the organisation is back on Open."""
PAYMENT_WINDOW = timedelta(hours=24)
"""How long a payment request stays open."""


class Feature(StrEnum):
    POLICY = "policy"
    SPEND = "spend"
    AUDIT = "audit"
    SSO = "sso"
    SUPPORT = "support"


FEATURE_NAMES: dict[Feature, str] = {
    Feature.POLICY: "Budget rules, approval thresholds and named approvers",
    Feature.SPEND: "Spend reporting",
    Feature.AUDIT: "The audit export an auditor accepts",
    Feature.SSO: "Single sign-on",
    Feature.SUPPORT: "A support commitment",
}


@dataclass(frozen=True)
class Plan:
    id: str
    name: str
    audience: str
    monthly: Usdc
    """What a period costs; for Enterprise, where the price starts."""
    self_serve: bool
    features: frozenset[Feature]
    support: str

    @property
    def take_rate(self) -> Decimal:
        """The platform's share of a settled issue.

        The same on every plan. The fee is what the platform charges for pricing and
        verifying a piece of work, which does not get cheaper or dearer because the
        publisher bought a subscription; a rate that moved with the plan is a second
        price for the same service (#142).
        """
        return Decimal(PLATFORM_FEE_BPS) / Decimal(10_000)

    @property
    def minimum_fix(self) -> Usdc:
        """The lowest publishable price, the same on every plan."""
        return MINIMUM_PRICE


PLANS: dict[str, Plan] = {
    "open": Plan(
        id="open",
        name="Open",
        audience="Maintainers, and anyone funding a fix",
        monthly=Usdc(0),
        self_serve=True,
        features=frozenset(),
        support="Community support through the issue tracker",
    ),
    "team": Plan(
        id="team",
        name="Team",
        audience="Companies of 10 to 200 engineers",
        monthly=Usdc.from_decimal("249"),
        self_serve=True,
        features=frozenset({Feature.POLICY, Feature.SPEND}),
        support="Community support through the issue tracker",
    ),
    "enterprise": Plan(
        id="enterprise",
        name="Enterprise",
        audience="Companies with a compliance obligation",
        monthly=Usdc.from_decimal("2000"),
        self_serve=False,
        features=frozenset(Feature),
        support=COMMITMENT,
    ),
}


def allows(plan_id: str, feature: Feature) -> bool:
    return feature in PLANS[plan_id].features


def cheapest_with(feature: Feature) -> Plan:
    """The plan to upgrade to for a feature."""
    return min(
        (p for p in PLANS.values() if feature in p.features),
        key=lambda p: p.monthly.base_units,
    )


def refusal(feature: Feature) -> str:
    plan = cheapest_with(feature)
    return f"{FEATURE_NAMES[feature]} is in the {plan.name} plan. See /plans to upgrade."
