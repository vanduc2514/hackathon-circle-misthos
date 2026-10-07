"""The sweeper.

Claim expiry, deadline refunds and the silent-publisher release are owed to people
whether or not anyone calls the API. Before this module they happened only when the
demo stepper was pushed, which is to say they did not happen. The sweeper wakes on an
interval, asks the store which open issues have a timer due, and has the store carry
each one out, so the lifecycle keeps its single writer.

It holds a named lock while it works, an advisory lock under Postgres, so a second
API process or a dedicated worker never applies the same refund twice. An issue a
person changed between the sweeper reading it and saving it is skipped and picked up
on the next pass, never overwritten.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from misthos.domain.timers import TIMED_STATES
from misthos.repositories import StaleIssue
from misthos.store import Store

log = logging.getLogger("misthos.sweeper")


@dataclass(frozen=True)
class SweepReport:
    ran: bool
    """False when another process held the sweeper lock, so this pass did nothing."""
    applied: dict[str, list[str]] = field(default_factory=dict)
    """Issue id to the timed actions carried out on it."""
    skipped: list[str] = field(default_factory=list)
    """Issues that changed mid-sweep. They are retried on the next pass."""


def sweep_once(store: Store, now: datetime | None = None) -> SweepReport:
    now = now or datetime.now(UTC)
    with store.repo.try_lock("sweeper") as held:
        if not held:
            return SweepReport(ran=False)
        applied: dict[str, list[str]] = {}
        skipped: list[str] = []
        for rec in store.list_issues(TIMED_STATES):
            try:
                actions = store.run_timers(rec, now)
            except StaleIssue:
                skipped.append(rec.id)
                continue
            if actions:
                applied[rec.id] = [a.value for a in actions]
        return SweepReport(ran=True, applied=applied, skipped=skipped)


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
