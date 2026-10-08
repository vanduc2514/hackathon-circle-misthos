"""Comparables: the platform's own settled issues that look like the one being priced.

The pricing engine's formula is a starting guess. What makes a price believable is
that work of the same shape settled at about that price, so the engine looks for it
in the settled history and lets what it finds move the price and set the confidence
(#42). Nothing here is declared by a caller: an issue with no history behind it is
priced on its signals alone and says so.

Pure functions over plain values, like the rest of the domain.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import median

from misthos.domain.money import Usdc

# Two issues are comparable when their signals differ by at most this much, as a
# weighted root mean square on the 1 to 5 scale. 0.75 is three quarters of a point
# across the board, or two points on one heavily weighted signal: the same kind of
# job, not merely the same size.
MAX_DISTANCE = 0.75

# Settled work older than this says little about today's price.
WINDOW = timedelta(days=365)

# Work in the same repository is the best evidence there is, so it ranks ahead of
# work elsewhere that is this much closer in signals.
SAME_REPO_BONUS = 0.25

# How far the comparables may pull the formula's price, and how fast. One comparable
# moves it a quarter of the way to what history implies; it never moves more than
# half, because a handful of settlements is evidence, not a market.
PULL_PER_COMPARABLE = 3
MAX_PULL = Decimal("0.5")

# Comparables agree when the prices they imply for this issue are within this factor
# of each other. Disagreeing history is not confidence, however much of it there is.
AGREEMENT = Decimal("1.5")
HIGH_CONFIDENCE_AT = 3

# How many the publisher is shown.
SHOWN = 3


@dataclass(frozen=True)
class SettledWork:
    """One issue the platform settled: what it looked like and what it paid."""

    issue_id: str
    repo: str
    title: str
    signals: dict[str, float]
    effort: float
    """Hours times the complexity multiplier, as the engine scored it when priced."""
    price: Usdc
    settled_at: datetime


@dataclass(frozen=True)
class Comparable:
    work: SettledWork
    distance: float
    implied: Usdc
    """What this settlement implies the issue being priced is worth: its price,
    scaled by how much more or less effort this issue is."""


@dataclass(frozen=True)
class ComparableRef:
    """What a proposal keeps of a comparable, for the publisher and the record."""

    issue_id: str
    repo: str
    title: str
    price: Usdc
    settled_at: datetime
    distance: float


def distance(a: dict[str, float], b: dict[str, float], weights: dict[str, float]) -> float:
    """Weighted root mean square difference between two sets of signals."""
    return math.sqrt(sum(w * (a[name] - b[name]) ** 2 for name, w in weights.items()))


def find(
    signals: dict[str, float],
    effort: float,
    history: Iterable[SettledWork],
    *,
    weights: dict[str, float],
    now: datetime,
    repo: str | None = None,
    exclude: str | None = None,
) -> list[Comparable]:
    """Every settled issue close enough to count, best first."""
    found: list[tuple[float, Comparable]] = []
    for work in history:
        if work.issue_id == exclude or now - work.settled_at > WINDOW or work.effort <= 0:
            continue
        d = distance(signals, work.signals, weights)
        if d > MAX_DISTANCE:
            continue
        scale = Decimal(str(effort)) / Decimal(str(work.effort))
        implied = Usdc(int(work.price.base_units * scale))
        rank = d - (SAME_REPO_BONUS if repo and work.repo.lower() == repo.lower() else 0.0)
        found.append((rank, Comparable(work=work, distance=round(d, 3), implied=implied)))
    found.sort(key=lambda pair: (pair[0], -pair[1].work.settled_at.timestamp()))
    return [c for _, c in found]


def pull(count: int) -> Decimal:
    """How far history moves the formula's price, from how much of it there is."""
    if count <= 0:
        return Decimal(0)
    return min(MAX_PULL, Decimal(count) / Decimal(count + PULL_PER_COMPARABLE))


def anchored(formula: Usdc, comparables: Sequence[Comparable]) -> Usdc:
    """The formula's price, moved toward the median of what history implies."""
    if not comparables:
        return formula
    target = Decimal(int(median(c.implied.base_units for c in comparables)))
    w = pull(len(comparables))
    blended = Decimal(formula.base_units) * (1 - w) + target * w
    return Usdc(int(blended))


def agree(comparables: Sequence[Comparable]) -> bool:
    implied = [c.implied.base_units for c in comparables if c.implied.base_units > 0]
    if len(implied) < 2:
        return True
    return Decimal(max(implied)) <= Decimal(min(implied)) * AGREEMENT


def confidence(comparables: Sequence[Comparable]) -> str:
    """High only when enough comparables exist and they agree; low with none."""
    if not comparables:
        return "low"
    if len(comparables) >= HIGH_CONFIDENCE_AT and agree(comparables):
        return "high"
    return "medium"


def refs(comparables: Sequence[Comparable]) -> tuple[ComparableRef, ...]:
    return tuple(
        ComparableRef(
            issue_id=c.work.issue_id,
            repo=c.work.repo,
            title=c.work.title,
            price=c.work.price,
            settled_at=c.work.settled_at,
            distance=c.distance,
        )
        for c in comparables[:SHOWN]
    )
