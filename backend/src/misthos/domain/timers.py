"""Timers.

Several things in the lifecycle happen because time passed rather than because someone
acted: a claim lapses, a commitment reaches its deadline, a passing verdict outlives
the publisher's silence, a payout that compliance or the chain held is tried again,
and an approval nobody booked runs out. Which of them is due is arithmetic over an
issue's state and its timestamps, so it lives here, where it can be judged at any
instant without a clock, a database or a worker. The sweeper asks this module what is
due and the store carries it out; nothing here moves state.

Work under review is deliberately never timed out. An issue in IN_REVIEW or REWORK
past its deadline is the conflict #33 exists to settle in the contract, and refunding
it from here would take money from a contributor whose work the platform is judging.

Every release is bounded by the escrow deadline, because `release` reverts once it has
passed. The silent-publisher release and any retry of a held payout are therefore due
by `RELEASE_MARGIN` before the deadline at the latest, so they land while the contract
still pays (#121). Accepted work whose release had still not landed by the deadline
cannot be paid at all: the contract refunds anyone who asks, so the timer refunds it,
and the record follows the chain rather than waiting in ACCEPTED for a release that can
no longer happen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from misthos.domain.compliance import PAYOUT_RETRY_INTERVAL, PayoutGate
from misthos.domain.issue import (
    SILENT_PUBLISHER_GRACE,
    IssueState,
    release_window_closes,
    silent_release_at,
)


class TimedAction(StrEnum):
    EXPIRE_CLAIM = "expire_claim"
    REFUND = "refund"
    RELEASE_AFTER_GRACE = "release_after_grace"
    RETRY_PAYOUT = "retry_payout"
    LAPSE_APPROVAL = "lapse_approval"
    """The approved terms ran out unbooked. A commitment the wallet sent under them
    that could never be booked goes back to the publisher; with none, the terms go."""


# The only states a timer can move an issue out of.
TIMED_STATES = frozenset(
    {
        IssueState.AWAITING_APPROVAL,
        IssueState.FUNDED,
        IssueState.CLAIMED,
        IssueState.ACCEPTED,
    }
)

# How long a payout the chain refused waits before it is tried again: nothing, so the
# sweeper's next pass tries it. A revert is caught at gas estimation, before anything
# is signed or spent, so trying often costs nothing.
RELEASE_RETRY_INTERVAL = timedelta(0)


@dataclass(frozen=True)
class Clocks:
    """The timestamps a timer can fire on. None means that timer is not running."""

    state: IssueState
    deadline: datetime | None = None
    claim_expires_at: datetime | None = None
    verdict_passed_at: datetime | None = None
    """When the verdict passed, while nothing has accepted the work yet."""
    payout_held_since: datetime | None = None
    """When an owed payout was last held. The retry waits an interval."""
    payout_hold: str | None = None
    """Why it was held, which decides how long the retry waits."""
    approved_until: datetime | None = None
    """While approved and not yet booked: the escrow deadline the approval named."""


def due(clocks: Clocks, now: datetime) -> TimedAction | None:
    """The timed action this issue is owed at `now`, if any.

    One at a time, because each changes the state the next is judged on: a lapsed
    claim puts the issue back in FUNDED, and only from there can a passed deadline
    refund it.
    """
    match clocks.state:
        case IssueState.ACCEPTED if deadline_passed(clocks, now):
            # Checked first: past the deadline no release can land, so neither the
            # grace nor a retry is owed any more, only the refund the contract allows.
            return TimedAction.REFUND
        case IssueState.ACCEPTED if _release_due(clocks, now):
            return TimedAction.RELEASE_AFTER_GRACE
        case IssueState.ACCEPTED if _retry_due(clocks, now):
            return TimedAction.RETRY_PAYOUT
        case IssueState.CLAIMED if _elapsed(clocks.claim_expires_at, now) or _elapsed(
            clocks.deadline, now
        ):
            # A claim also ends at the deadline: with no pull request there is no
            # work to wait for, and the commitment it was holding is refundable.
            return TimedAction.EXPIRE_CLAIM
        case IssueState.FUNDED if _elapsed(clocks.deadline, now):
            return TimedAction.REFUND
        case IssueState.AWAITING_APPROVAL if _elapsed(clocks.approved_until, now):
            # The escrow refuses a commitment past the approved deadline, so by now any
            # commitment sent under these terms is refundable.
            return TimedAction.LAPSE_APPROVAL
    return None


def deadline_passed(clocks: Clocks, now: datetime) -> bool:
    return _elapsed(clocks.deadline, now)


def _release_due(clocks: Clocks, now: datetime) -> bool:
    """Whether the silent-publisher release is owed at `now`.

    The grace runs from the verdict and stops a margin before the escrow deadline: the
    contract will not pay after the deadline, so a release has to be under way before
    it. A commitment always carries a deadline; the uncapped case is kept for a record
    that has none, where the grace stands on its own rather than the timer disappearing.
    """
    passed = clocks.verdict_passed_at
    if passed is None:
        return False
    if clocks.deadline is None:
        return now > passed + SILENT_PUBLISHER_GRACE
    return now > silent_release_at(passed, clocks.deadline)


def _retry_due(clocks: Clocks, now: datetime) -> bool:
    """Whether a held payout is tried again at `now`.

    A hold the chain caused waits for the next pass, one compliance or the
    organisation's policy caused waits `PAYOUT_RETRY_INTERVAL`. Either way the retry
    comes no later than the margin before the deadline, so the last attempt can still
    land, and never at the instant it was held, so one pass tries once.
    """
    held = clocks.payout_held_since
    if held is None:
        return False
    wait = (
        RELEASE_RETRY_INTERVAL
        if clocks.payout_hold == PayoutGate.RELEASE_FAILED
        else PAYOUT_RETRY_INTERVAL
    )
    retry_at = held + wait
    if clocks.deadline is not None:
        retry_at = max(held, min(retry_at, release_window_closes(clocks.deadline)))
    return now > retry_at


def _elapsed(moment: datetime | None, now: datetime, wait: timedelta = timedelta(0)) -> bool:
    return moment is not None and now > moment + wait
