"""Issue lifecycle.

A funded issue is the unit of sale. The state machine is the product, so it lives
here with no IO and no imports from services, which makes every transition
testable without a database, a chain or a GitHub token.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum


class IssueState(StrEnum):
    DRAFT = "DRAFT"
    PRICED = "PRICED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    FUNDED = "FUNDED"
    CLAIMED = "CLAIMED"
    IN_REVIEW = "IN_REVIEW"
    REWORK = "REWORK"
    ACCEPTED = "ACCEPTED"
    PAID = "PAID"
    REJECTED = "REJECTED"
    REFUNDED = "REFUNDED"


# Every legal transition. Anything not listed is a bug, not an edge case.
TRANSITIONS: dict[IssueState, frozenset[IssueState]] = {
    IssueState.DRAFT: frozenset({IssueState.PRICED}),
    IssueState.PRICED: frozenset({IssueState.AWAITING_APPROVAL}),
    IssueState.AWAITING_APPROVAL: frozenset({IssueState.DRAFT, IssueState.FUNDED}),
    IssueState.FUNDED: frozenset({IssueState.CLAIMED, IssueState.REFUNDED}),
    IssueState.CLAIMED: frozenset({IssueState.IN_REVIEW, IssueState.FUNDED}),
    IssueState.IN_REVIEW: frozenset({IssueState.REWORK, IssueState.ACCEPTED, IssueState.REJECTED}),
    IssueState.REWORK: frozenset({IssueState.IN_REVIEW}),
    # ACCEPTED means the platform's verdict passed and the publisher has not merged
    # yet. That is the grace window, not a resting state.
    IssueState.ACCEPTED: frozenset({IssueState.PAID}),
    IssueState.PAID: frozenset(),
    IssueState.REJECTED: frozenset({IssueState.FUNDED}),
    IssueState.REFUNDED: frozenset(),
}

# States where money is committed and the issue is still live. ACCEPTED belongs
# here: a passing verdict with an unmerged pull request is exactly that, for up to
# the length of the silent-publisher grace period.
OPEN_STATES = frozenset(
    {
        IssueState.FUNDED,
        IssueState.CLAIMED,
        IssueState.IN_REVIEW,
        IssueState.REWORK,
        IssueState.ACCEPTED,
    }
)

TERMINAL_STATES = frozenset({IssueState.PAID, IssueState.REFUNDED})

HUMAN_CHECKPOINTS = frozenset(
    {
        "approve_price",
        "accept_work",
    }
)

# A passing verdict releases without a signature once the publisher has been silent
# this long. The only release path that does not carry one. See 08.
SILENT_PUBLISHER_GRACE = timedelta(days=7)

# How long committed funds stay in escrow before the publisher can reclaim them. The
# contract refuses a release once it has passed, which is what makes this window and
# the grace a pair rather than two independent timers. See `silent_release_at`.
FUNDING_WINDOW = timedelta(days=14)


class IllegalTransition(Exception):
    def __init__(self, current: IssueState, requested: IssueState) -> None:
        super().__init__(f"cannot move an issue from {current} to {requested}")
        self.current = current
        self.requested = requested


def can_transition(current: IssueState, requested: IssueState) -> bool:
    return requested in TRANSITIONS[current]


def transition(current: IssueState, requested: IssueState) -> IssueState:
    if not can_transition(current, requested):
        raise IllegalTransition(current, requested)
    return requested


def is_open(state: IssueState) -> bool:
    return state in OPEN_STATES


def silent_release_at(decided_at: datetime, deadline: datetime) -> datetime:
    """When a passing verdict releases without the publisher's signature.

    The escrow only pays inside its own window, so the grace can never outlive the
    deadline. A verdict that leaves less than the full grace on the clock releases at
    the deadline instead of later: uncapped, the release the grace exists for would
    land after the contract has closed and the only path left would be the refund to
    the publisher that the grace was written to avoid.
    """
    return min(decided_at + SILENT_PUBLISHER_GRACE, deadline)
