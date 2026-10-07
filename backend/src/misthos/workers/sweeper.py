"""The sweeper.

Claim expiry, deadline refunds, the silent-publisher release and the retry of a
payout that compliance held are owed to people whether or not anyone calls the API.
Before this module they happened only when the demo stepper was pushed, which is to
say they did not happen. The sweeper wakes on an interval, asks the store which open
issues have a timer due, and has the store carry each one out, so the lifecycle keeps
its single writer.

It holds a named lock while it works, an advisory lock under Postgres, so a second
API process or a dedicated worker never applies the same refund twice. Each issue is
handled under that issue's own lock and read afresh inside it, so a person's action is
never overwritten by a timer; an issue someone is acting on is skipped and picked up
on the next pass. An issue whose action fails is logged and skipped too, because one
bad issue must not stop every other refund.

Each pass also has the review agent judge every submitted commit that has no
verdict yet, so a pull request is reviewed without anyone asking. It re-screens live
counterparties whose last check is a day old, because screening once at onboarding
is the mistake 08 is written against; deletes screening records past their published
retention period; and reconciles the money ledger against the chain, raising an
alert for any divergence.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from misthos.domain.timers import TIMED_STATES
from misthos.repositories import StaleIssue
from misthos.services.coordination import Busy
from misthos.store import Store

log = logging.getLogger("misthos.sweeper")

# A model review takes up to a few minutes, and refunds wait behind a pass, so a pass
# reviews a few submissions and leaves the rest for the next one.
MAX_REVIEWS_PER_PASS = 5

# A submission the reviewer cannot judge must not hold one of those slots forever, or
# every submission queued behind it is never looked at while a billed model call is
# made on the same broken one every pass. After this many consecutive failures the
# sweeper stops retrying and logs an alert instead, because the answer is a person
# rather than another attempt. Counts are keyed by the commit under review, so a
# resubmission starts with a fresh budget.
MAX_REVIEW_ATTEMPTS = 3

# Issue id to (head sha, consecutive failures). Process-local on purpose: a restart
# gives a poisoned submission another few attempts rather than forgetting it.
_review_failures: dict[str, tuple[str, int]] = {}


def reset_review_failures() -> None:
    """Forget every failure count, as a full store reset does."""
    _review_failures.clear()


def _failures_for(store: Store, issue_id: str) -> int:
    rec = store.get(issue_id)
    sha = rec.submission.head_sha if rec is not None and rec.submission is not None else ""
    recorded = _review_failures.get(issue_id)
    if recorded is None or recorded[0] != sha:
        return 0
    return recorded[1]


def _record_failure(store: Store, issue_id: str) -> int:
    rec = store.get(issue_id)
    sha = rec.submission.head_sha if rec is not None and rec.submission is not None else ""
    count = _failures_for(store, issue_id) + 1
    _review_failures[issue_id] = (sha, count)
    return count


@dataclass(frozen=True)
class SweepReport:
    ran: bool
    """False when another process held the sweeper lock, so this pass did nothing."""
    applied: dict[str, list[str]] = field(default_factory=dict)
    """Issue id to the timed actions carried out on it."""
    skipped: list[str] = field(default_factory=list)
    """Issues someone was acting on mid-sweep. They are retried on the next pass."""
    failed: list[str] = field(default_factory=list)
    """Issues whose timed action or review raised, such as a chain revert. Logged and
    retried."""
    reviewed: dict[str, str] = field(default_factory=dict)
    """Issue id to the verdict the review agent issued on it this pass."""
    screened: int = 0
    """Counterparties screened again on schedule."""
    purged: int = 0
    """Screening records deleted at the end of their retention period."""
    divergences: int = 0
    """Issues whose ledger and on-chain escrow disagree. Each one is an alert."""


def sweep_once(store: Store, now: datetime | None = None) -> SweepReport:
    now = now or datetime.now(UTC)
    with store.repo.try_lock("sweeper") as held:
        if not held:
            return SweepReport(ran=False)
        applied: dict[str, list[str]] = {}
        skipped: list[str] = []
        failed: list[str] = []
        for rec in store.list_issues(TIMED_STATES):
            try:
                actions = store.run_timers(rec.id, now)
            except (Busy, StaleIssue):
                skipped.append(rec.id)
                continue
            except Exception:
                log.exception("sweeper: timers on %s failed, retrying next pass", rec.id)
                failed.append(rec.id)
                continue
            if actions:
                applied[rec.id] = [a.value for a in actions]
        reviewed: dict[str, str] = {}
        # Least-failed first, so a submission that keeps failing cannot hold a slot
        # while the ones behind it starve; anything past the cap is left alone.
        queue = sorted(store.reviews_due(), key=lambda i: (_failures_for(store, i), i))
        for issue_id in queue[:MAX_REVIEWS_PER_PASS]:
            if _failures_for(store, issue_id) >= MAX_REVIEW_ATTEMPTS:
                continue
            try:
                rec = store.review(issue_id, now)
            except (Busy, StaleIssue):
                skipped.append(issue_id)
                continue
            except Exception:
                failed.append(issue_id)
                count = _record_failure(store, issue_id)
                if count >= MAX_REVIEW_ATTEMPTS:
                    log.error(
                        "sweeper: %s could not be reviewed in %d attempts; it needs a "
                        "person, not another retry",
                        issue_id,
                        count,
                    )
                else:
                    log.exception(
                        "sweeper: review of %s failed (%d/%d), retrying next pass",
                        issue_id,
                        count,
                        MAX_REVIEW_ATTEMPTS,
                    )
                continue
            if rec is not None and rec.review is not None:
                reviewed[issue_id] = rec.review.verdict
            _review_failures.pop(issue_id, None)
        screened = store.rescreen(now)
        purged = store.purge_expired(now)
        divergences = store.reconcile(now)
        return SweepReport(
            ran=True,
            applied=applied,
            skipped=skipped,
            failed=failed,
            reviewed=reviewed,
            screened=len(screened),
            purged=purged,
            divergences=len(divergences),
        )


async def run_forever(store: Store, interval_seconds: float) -> None:
    """Sweep now and then every interval, until cancelled. A failed pass is logged and
    the next one still runs: one bad issue must not stop every other refund."""
    while True:
        try:
            report = await asyncio.to_thread(sweep_once, store)
        except Exception:
            log.exception("sweep failed, retrying in %ss", interval_seconds)
        else:
            for issue_id, actions in report.applied.items():
                log.info("sweeper: %s %s", issue_id, ", ".join(actions))
        await asyncio.sleep(interval_seconds)
