"""Every number on the dashboard, from the records alone.

Nothing here counts states in memory or trusts a field a fixture set. Money figures
come from the ledger's events, timings from those events and the claim records, and
review figures from the decision log, so every number can be reproduced from the
database with no process running (#56). The windows and thresholds are the ones 09
sets for each metric.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from decimal import Decimal

from misthos.domain import issue as lifecycle
from misthos.domain.ledger import MoneyEvent, MoneyEventKind
from misthos.domain.money import Usdc
from misthos.models.records import IssueRecord
from misthos.schemas import Decision, MetricsOut

WEEK = timedelta(days=7)
MONTH = timedelta(days=30)
CLAIM_WINDOW = timedelta(hours=72)
REPEAT_WINDOW = timedelta(days=60)
EARNER_THRESHOLD = Usdc.from_decimal("500")
TOP_EARNERS = 10

PASSING = "accept"


def _events(records: Iterable[IssueRecord], kind: MoneyEventKind):  # noqa: ANN202
    return [(r, e) for r in records for e in r.money_events if e.kind is kind]


def _first_claim(rec: IssueRecord) -> datetime | None:
    """When the issue was first claimed, whether or not that claim lasted."""
    times = [d.created_at for d in rec.decisions if d.action == "claimed"]
    if rec.claim is not None:
        times.append(rec.claim.issued_at)
    return min(times) if times else None


def verdicts(rec: IssueRecord) -> list[str]:
    """Every verdict the issue received, in order."""
    found: list[str] = []
    for d in rec.decisions:
        if d.action == "verdict_passed":
            found.append(PASSING)
        elif d.action == "verdict_issued":
            found.append(d.outcome.split(" ", 1)[0].rstrip(":"))
        elif d.action == "dispute_overturned":
            found.append(PASSING)
    return found


def _passed(rec: IssueRecord) -> bool:
    return PASSING in verdicts(rec)


def _any(decisions: Sequence[Decision], action: str) -> bool:
    return any(d.action == action for d in decisions)


def _rate(part: int, whole: int) -> float:
    return round(part / whole, 2) if whole else 0.0


def _median(values: Sequence[float | Decimal]) -> float | Decimal | None:
    if not values:
        return None
    ordered = sorted(values)  # type: ignore[type-var]
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _repeat_publishers(committed: list[tuple[IssueRecord, MoneyEvent]]) -> tuple[int, int]:
    """Publishers who funded a second issue within 60 days of one before it."""
    times: dict[str, list[datetime]] = defaultdict(list)
    for _, event in committed:
        times[event.counterparty_id].append(event.occurred_at)
    repeat = 0
    for stamps in times.values():
        stamps.sort()
        if any(b - a <= REPEAT_WINDOW for a, b in zip(stamps, stamps[1:], strict=False)):
            repeat += 1
    return repeat, len(times)


def earnings(
    released: list[tuple[IssueRecord, MoneyEvent]], *, since: datetime | None = None
) -> dict[str, int]:
    """Base units paid to each contributor, optionally only since a moment."""
    paid: dict[str, int] = defaultdict(int)
    for rec, event in released:
        if since is None or event.occurred_at >= since:
            paid[event.counterparty_id] += rec.received.base_units
    return dict(paid)


def compute(records: list[IssueRecord], now: datetime) -> MetricsOut:
    committed = _events(records, MoneyEventKind.COMMITTED)
    released = _events(records, MoneyEventKind.RELEASED)
    refunded = _events(records, MoneyEventKind.REFUNDED)

    # Claim rate: of the issues funded long enough ago to judge, or already claimed,
    # the share claimed within 72 hours of the money arriving.
    judgeable = claimed_in_time = 0
    for rec, event in committed:
        first = _first_claim(rec)
        if first is None and now - event.occurred_at < CLAIM_WINDOW:
            continue
        judgeable += 1
        if first is not None and first - event.occurred_at <= CLAIM_WINDOW:
            claimed_in_time += 1

    first_verdicts = [v[0] for r in records if (v := verdicts(r))]
    repeat, publishers = _repeat_publishers(committed)

    hours_to_payout = [
        (event.occurred_at - claimed).total_seconds() / 3600
        for rec, event in released
        if (claimed := _first_claim(rec)) is not None
    ]

    passed = [r for r in records if _passed(r)]
    submitted = [r for r in records if r.submission is not None]

    recent = earnings(released, since=now - MONTH)
    lifetime = earnings(released)
    total_paid = sum(lifetime.values())
    top = sum(sorted(lifetime.values(), reverse=True)[:TOP_EARNERS])

    review_costs = [
        sum((Decimal(d.cost_usdc) for d in spent), start=Decimal(0))
        for r in records
        if (spent := [d for d in r.decisions if d.action == "verdict_issued" and d.cost_usdc])
        or _any(r.decisions, "verdict_issued")
    ]
    review_seconds = [
        r.review.seconds for r in records if r.review and r.review.seconds is not None
    ]
    median_cost = _median(review_costs)
    median_hours = _median(hours_to_payout)
    median_seconds = _median(review_seconds)

    by_state: dict[str, int] = {}
    for r in records:
        by_state[r.state.value] = by_state.get(r.state.value, 0) + 1

    fees = sum(r.platform_fee.base_units for r in records if r.platform_fee)
    return MetricsOut(
        settled_issues=len(released),
        settled_issues_7d=sum(1 for _, e in released if e.occurred_at >= now - WEEK),
        funded_issues_published=len(committed),
        funded_issues_7d=sum(1 for _, e in committed if e.occurred_at >= now - WEEK),
        claim_rate_72h=_rate(claimed_in_time, judgeable),
        acceptance_rate_first_review=_rate(
            sum(1 for v in first_verdicts if v == PASSING), len(first_verdicts)
        ),
        repeat_publisher_rate=_rate(repeat, publishers),
        matched_volume_usdc=f"{Usdc(sum(e.amount.base_units for _, e in released)).decimal:.2f}",
        platform_fees_usdc=f"{Usdc(fees).decimal:.2f}",
        median_hours_to_payout=round(float(median_hours), 1) if median_hours is not None else None,
        refund_rate=_rate(len(refunded), len(released) + len(refunded)),
        dispute_rate=_rate(
            sum(1 for r in submitted if _any(r.decisions, "dispute_opened")), len(submitted)
        ),
        publisher_overturn_rate=_rate(
            sum(1 for r in passed if _any(r.decisions, "publisher_declined")), len(passed)
        ),
        earners_over_500_share=(
            _rate(sum(1 for v in recent.values() if v >= EARNER_THRESHOLD.base_units), len(recent))
            if recent
            else None
        ),
        top10_payout_share=round(top / total_paid, 2) if total_paid else None,
        open_issues=sum(1 for r in records if lifecycle.is_open(r.state)),
        reviews_issued=sum(1 for r in records for d in r.decisions if d.action == "verdict_issued"),
        median_review_seconds=float(median_seconds) if median_seconds is not None else None,
        median_review_cost_usdc=f"{median_cost:.2f}" if median_cost is not None else None,
        by_state=by_state,
    )
