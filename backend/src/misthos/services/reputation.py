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
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from misthos.domain.ledger import MoneyEventKind
from misthos.domain.money import Usdc, format_usdc
from misthos.models.records import IssueRecord
from misthos.schemas import Decision

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


# A score of 50 is this many points: about five settled issues of a few hundred USDC.
HALF_SCORE_POINTS = 50
TOP_REPOS = 10
TOP_LABELS = 10


@dataclass(frozen=True)
class RepoShare:
    repo: str
    settled_issues: int
    earned: Usdc


@dataclass(frozen=True)
class Signals:
    """How the contributor's work went, from the decision log of the issues they hold.

    Counted only from their own claim on: an issue that changed hands is not the
    earlier claimant's record and not this one's.
    """

    issues_held: int = 0
    accepted: int = 0
    reworked: int = 0
    rejected: int = 0
    rework_rounds: int = 0
    first_pass_settled: int = 0
    disputes_opened: int = 0
    disputes_overturned: int = 0
    disputes_upheld: int = 0

    @property
    def verdicts(self) -> int:
        return self.accepted + self.reworked + self.rejected


@dataclass(frozen=True)
class Profile:
    """A contributor's standing and what it is made of, as aggregates only (#202).

    No wallet, no handle, no issue that is not already public: the paid reputation is
    a derived view, and a wallet next to an identity is what docs/PRIVACY.md forbids.
    """

    contributor_id: str
    standing: Standing
    score: int
    """0 to 100: points / (points + HALF_SCORE_POINTS). Saturates, so a long record
    counts for more than one big issue, and never quite reaches 100."""
    percentile: int | None
    """The share of contributors with any standing whose points are at or below this
    one's, in percent. None for a contributor with none."""
    ranked: int
    """How many contributors have any standing: what the percentile is out of."""
    signals: Signals
    repos: list[RepoShare] = field(default_factory=list)
    labels: list[tuple[str, int]] = field(default_factory=list)
    first_settled_at: datetime | None = None
    last_settled_at: datetime | None = None


def score_of(points: int) -> int:
    return round(100 * points / (points + HALF_SCORE_POINTS)) if points > 0 else 0


def _since_claim(rec: IssueRecord) -> list[Decision]:
    """The issue's decisions from its current claim on."""
    claimed = [i for i, d in enumerate(rec.decisions) if d.action == "claimed"]
    return rec.decisions[claimed[-1] :] if claimed else list(rec.decisions)


def profile(records: Iterable[IssueRecord], contributor_id: str) -> Profile:
    """Everything the paid reputation says about one contributor, from the ledger and
    the decision log alone. The standing is `standing(records)`'s, exactly."""
    records = list(records)
    standings = standing(records)
    mine = standings.get(contributor_id, Standing())
    mine_events = [e for e in events(records) if e.contributor_id == contributor_id]
    settled_ids = {e.issue_id for e in mine_events}

    accepted = reworked = rejected = rounds = first_pass = 0
    opened = overturned = upheld = held = 0
    labels: Counter[str] = Counter()
    for rec in records:
        if rec.contributor_id != contributor_id:
            continue
        held += 1
        since = _since_claim(rec)
        issue_rounds = 0
        for d in since:
            if d.action == "verdict_issued":
                if d.outcome.startswith("accept"):
                    accepted += 1
                elif d.outcome.startswith("rework"):
                    reworked += 1
                    issue_rounds += 1
                elif d.outcome.startswith("reject"):
                    rejected += 1
            elif d.action == "publisher_declined":
                issue_rounds += 1
            elif d.action == "dispute_opened":
                opened += 1
            elif d.action == "dispute_overturned":
                overturned += 1
            elif d.action == "dispute_upheld":
                upheld += 1
        rounds += issue_rounds
        if rec.id in settled_ids:
            labels.update(rec.labels)
            if issue_rounds == 0:
                first_pass += 1

    by_repo: dict[str, list[ReputationEvent]] = {}
    for e in mine_events:
        by_repo.setdefault(e.repo, []).append(e)
    repos = sorted(
        (
            RepoShare(
                repo=repo,
                settled_issues=len(group),
                earned=Usdc(sum(e.amount.base_units for e in group)),
            )
            for repo, group in by_repo.items()
        ),
        key=lambda r: (-r.settled_issues, -r.earned.base_units, r.repo),
    )[:TOP_REPOS]

    ranked = [st.reputation for st in standings.values() if st.reputation > 0]
    percentile = (
        round(100 * sum(1 for p in ranked if p <= mine.reputation) / len(ranked))
        if mine.reputation > 0
        else None
    )
    return Profile(
        contributor_id=contributor_id,
        standing=mine,
        score=score_of(mine.reputation),
        percentile=percentile,
        ranked=len(ranked),
        signals=Signals(
            issues_held=held,
            accepted=accepted,
            reworked=reworked,
            rejected=rejected,
            rework_rounds=rounds,
            first_pass_settled=first_pass,
            disputes_opened=opened,
            disputes_overturned=overturned,
            disputes_upheld=upheld,
        ),
        repos=repos,
        labels=sorted(labels.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_LABELS],
        first_settled_at=mine_events[0].settled_at if mine_events else None,
        last_settled_at=mine_events[-1].settled_at if mine_events else None,
    )


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
