"""Issue lifecycle.

A funded issue is the unit of sale. The state machine is the product, so it lives
here with no IO and no imports from services, which makes every transition
testable without a database, a chain or a GitHub token.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from misthos.domain.money import Usdc


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
    # REFUNDED from here is a commitment the platform could not book (a wallet that
    # sent another amount or deadline than the approval named) going back to the
    # publisher at its deadline. The escrow takes one commitment per issue, so that
    # listing is over (#126).
    IssueState.AWAITING_APPROVAL: frozenset(
        {IssueState.DRAFT, IssueState.FUNDED, IssueState.REFUNDED}
    ),
    IssueState.FUNDED: frozenset({IssueState.CLAIMED, IssueState.REFUNDED}),
    IssueState.CLAIMED: frozenset({IssueState.IN_REVIEW, IssueState.FUNDED}),
    IssueState.IN_REVIEW: frozenset({IssueState.REWORK, IssueState.ACCEPTED, IssueState.REJECTED}),
    IssueState.REWORK: frozenset({IssueState.IN_REVIEW}),
    # ACCEPTED means the platform's verdict passed and the publisher has not merged
    # yet. That is the grace window, not a resting state.
    # A publisher may decline a passing verdict, with a reason, once (#54); the work
    # goes back for rework rather than the money back to them.
    # REFUNDED is the escrow deadline passing before any release landed: the contract
    # pays nothing after it and refunds anyone who asks, so the money goes back to
    # the publisher and the record follows the chain (#121, the hole #84 left).
    IssueState.ACCEPTED: frozenset({IssueState.PAID, IssueState.REWORK, IssueState.REFUNDED}),
    IssueState.PAID: frozenset(),
    # A rejection sends the issue back to the pool, or, when the contributor's
    # dispute is upheld on a second review, on to acceptance (#39).
    IssueState.REJECTED: frozenset({IssueState.FUNDED, IssueState.ACCEPTED}),
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

# How long the first claim holds an issue exclusively. With no pull request by then
# the issue returns to the pool, which is what stops squatting on good work. See 05.
CLAIM_WINDOW = timedelta(hours=72)

# How long committed funds wait for acceptable work before they go back to the
# publisher. The escrow contract holds the same deadline on chain.
ESCROW_TERM = timedelta(days=14)

# How long before the escrow deadline the silent-publisher release fires. `release`
# reverts once the deadline has passed, so a release timed for the deadline itself
# lands exactly when the contract refuses it. Ten sweeper passes at the default
# minute leave room for a failed pass, an RPC that is slow to answer and a block
# clock a little ahead of ours (#121).
RELEASE_MARGIN = timedelta(minutes=10)

# How long the terms a publisher approved stand for their wallet to commit them: the
# amount, the take rate, the wallet and the escrow deadline. Booking checks the
# commitment against those terms, never against ones recomputed later, so a booking
# an hour or a plan change after the approval still succeeds (#126). Past this, with
# nothing committed, approving again sets new terms; once money is committed the
# terms it was committed under are the only ones it is booked against.
COMMIT_WINDOW = timedelta(hours=24)

# How far before the approved deadline a commitment's own deadline may sit and still
# be booked. The approval fixes the deadline to the second and the plan hands it to
# the wallet, so the slack only covers a wallet that rounds; any earlier would cut
# the contributor's window. The contract refuses one later than the approved deadline.
DEADLINE_TOLERANCE = timedelta(hours=1)


@dataclass(frozen=True)
class FundingTerms:
    """What the publisher approved at the price checkpoint, as the escrow is told it.

    Fixed when the price is approved and kept with the issue, so the commitment the
    publisher's wallet sends is booked against what they were shown: a deadline or a
    take rate recomputed at booking would refuse it for good once the clock or the
    plan had moved, with the money already in the escrow (#126).
    """

    amount: Usdc
    fee_bps: int
    """The take rate, fixed on the escrow before the money is in."""
    wallet: str
    """The only wallet the escrow lets commit: the one the publisher funds from (#122)."""
    deadline: datetime
    """The escrow deadline, to the second: the latest the escrow accepts."""
    approved_at: datetime

    def stand(self, now: datetime) -> bool:
        """Whether a new approval keeps these terms rather than setting new ones."""
        return now < self.approved_at + COMMIT_WINDOW


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


def release_window_closes(deadline: datetime) -> datetime:
    """When every timed release is due at the latest: a margin before the deadline, so
    it lands while the escrow still pays."""
    return deadline - RELEASE_MARGIN


def silent_release_at(decided_at: datetime, deadline: datetime) -> datetime:
    """When a passing verdict releases without the publisher's signature.

    The escrow only pays inside its own window, so the grace can never outlive it. A
    verdict that leaves less than the full grace on the clock releases a margin before
    the deadline rather than later: at the deadline itself the release would reach the
    contract just as it starts refusing, and the only path left would be the refund to
    the publisher that the grace was written to avoid. A verdict inside the margin is
    due at once.
    """
    return min(decided_at + SILENT_PUBLISHER_GRACE, release_window_closes(deadline))
