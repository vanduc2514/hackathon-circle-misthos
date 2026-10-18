"""Timers.

Four things in the lifecycle happen because time passed rather than because someone
acted: a claim lapses, a commitment reaches its deadline, a passing verdict outlives
the publisher's silence, and a payout that compliance held is checked again. Which of
them is due is arithmetic over an issue's state and its timestamps, so it lives here,
where it can be judged at any instant without a clock, a database or a worker. The
sweeper asks this module what is due and the store carries it out; nothing here
moves state.

Work under review is deliberately never timed out. An issue in IN_REVIEW past its
deadline is the conflict #33 exists to settle in the contract, and refunding it from
here would take money from a contributor whose work the platform is judging.

The silent-publisher release is the one timer bounded by another: the grace is capped at
the escrow deadline, because `release` reverts once that has passed. Uncapped, a verdict
that leaves less than a full grace on the clock would release after the contract had
closed, and the work the verdict passed would be refunded instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from misthos.domain.compliance import PAYOUT_RETRY_INTERVAL
from misthos.domain.issue import SILENT_PUBLISHER_GRACE, IssueState, silent_release_at


class TimedAction(StrEnum):
    REFUND = "refund"
    RELEASE_AFTER_GRACE = "release_after_grace"
    RETRY_PAYOUT = "retry_payout"


# The only states a timer can move an issue out of.
TIMED_STATES = frozenset({IssueState.FUNDED, IssueState.CLAIMED, IssueState.IN_REVIEW})


@dataclass(frozen=True)
class Clocks:
    """The timestamps a timer can fire on. None means that timer is not running."""

    state: IssueState
    deadline: datetime | None = None
    verdict_passed_at: datetime | None = None
    """When the verdict passed, while nothing has accepted the work yet."""
    payout_held_since: datetime | None = None
    """When compliance last held an owed payout. The retry waits an interval."""


def due(clocks: Clocks, now: datetime) -> TimedAction | None:
    """The timed action this issue is owed at `now`, if any."""
    match clocks.state:
        case IssueState.IN_REVIEW if _release_due(clocks, now):
            return TimedAction.RELEASE_AFTER_GRACE
        case IssueState.IN_REVIEW if _elapsed(clocks.payout_held_since, now, PAYOUT_RETRY_INTERVAL):
            return TimedAction.RETRY_PAYOUT
        case IssueState.FUNDED | IssueState.CLAIMED if _elapsed(clocks.deadline, now):
            return TimedAction.REFUND
    return None


def deadline_passed(clocks: Clocks, now: datetime) -> bool:
    return _elapsed(clocks.deadline, now)


def _release_due(clocks: Clocks, now: datetime) -> bool:
    """Whether the silent-publisher release is owed at `now`.

    The grace runs from the verdict and stops at the escrow deadline: the contract will
    not pay after it, so releasing later is not a release. A commitment always carries a
    deadline; the uncapped case is kept for a record that has none, where the grace
    stands on its own rather than the timer disappearing.
    """
    passed = clocks.verdict_passed_at
    if passed is None:
        return False
    if clocks.deadline is None:
        return now > passed + SILENT_PUBLISHER_GRACE
    return now > silent_release_at(passed, clocks.deadline)


def _elapsed(moment: datetime | None, now: datetime, wait: timedelta = timedelta(0)) -> bool:
    return moment is not None and now > moment + wait
