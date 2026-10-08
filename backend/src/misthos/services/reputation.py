"""Contributor reputation, from settled issues and nothing else (ADR-10).

A contributor earns standing only when their work merged and the payment released:
claims, submissions and reviews are activity, and activity is cheap. Each release
in the money ledger is one reputation event, worth a fixed amount for the settled
issue and a little more for its size, capped so one large issue cannot outweigh a
track record. Because events come from the ledger alone, a contributor's standing
can be rebuilt at any time and must come out the same:

    python -m misthos.services.reputation --check
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from misthos.domain.ledger import MoneyEventKind
from misthos.domain.money import Usdc, format_usdc
from misthos.models.records import IssueRecord

POINTS_PER_SETTLED_ISSUE = 10
POINTS_PER_HUNDRED_USDC = 1
MAX_SIZE_POINTS = 10
_HUNDRED = Usdc.from_decimal("100").base_units


@dataclass(frozen=True)
class ReputationEvent:
    contributor_id: str
    issue_id: str
    repo: str
    amount: Usdc
    points: int
    settled_at: datetime


@dataclass(frozen=True)
class Standing:
    settled_issues: int = 0
    earned: Usdc = Usdc(0)
    reputation: int = 0


def points_for(amount: Usdc) -> int:
    size = min(MAX_SIZE_POINTS, amount.base_units // _HUNDRED * POINTS_PER_HUNDRED_USDC)
    return POINTS_PER_SETTLED_ISSUE + size


def events(records: Iterable[IssueRecord]) -> list[ReputationEvent]:
    """One event per release in the ledger, oldest first."""
    found = [
        ReputationEvent(
            contributor_id=e.counterparty_id,
            issue_id=rec.id,
            repo=rec.repo,
            amount=rec.received,
            points=points_for(rec.received),
            settled_at=e.occurred_at,
        )
        for rec in records
        for e in rec.money_events
        if e.kind is MoneyEventKind.RELEASED
    ]
    return sorted(found, key=lambda ev: ev.settled_at)


def standing(records: Iterable[IssueRecord]) -> dict[str, Standing]:
    totals: dict[str, Standing] = {}
    for ev in events(records):
        now = totals.get(ev.contributor_id, Standing())
        totals[ev.contributor_id] = Standing(
            settled_issues=now.settled_issues + 1,
            earned=now.earned + ev.amount,
            reputation=now.reputation + ev.points,
        )
    return totals


def as_fields(st: Standing) -> dict[str, object]:
    """The contributor record's fields for a standing."""
    return {
        "settled_issues": st.settled_issues,
        "earned_usdc": format_usdc(st.earned),
        "reputation": st.reputation,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rebuild contributor standing from the ledger")
    parser.add_argument(
        "--check", action="store_true", help="report drift without writing, exit 1 on any"
    )
    args = parser.parse_args(argv)

    from misthos.store import store

    drift = store.rebuild_standing(write=not args.check)
    for contributor_id, (before, after) in sorted(drift.items()):
        print(f"{contributor_id}: {before} -> {after}")
    if not drift:
        print("Every contributor's standing matches the ledger.")
    return 1 if drift and args.check else 0


if __name__ == "__main__":
    sys.exit(main())
