"""Issue lifecycle.

A funded issue is the unit of sale. The state machine is the product, so it lives
here with no IO and no imports from services, which makes every transition
testable without a database, a chain or a GitHub token.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
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
    IssueState.AWAITING_APPROVAL: frozenset({IssueState.DRAFT, IssueState.FUNDED}),
    IssueState.FUNDED: frozenset({IssueState.CLAIMED, IssueState.REFUNDED}),
    IssueState.CLAIMED: frozenset({IssueState.IN_REVIEW, IssueState.FUNDED}),
    IssueState.IN_REVIEW: frozenset({IssueState.REWORK, IssueState.ACCEPTED, IssueState.REJECTED}),
    IssueState.REWORK: frozenset({IssueState.IN_REVIEW}),
    # ACCEPTED means the platform's verdict passed and the publisher has not merged
    # yet. That is the grace window, not a resting state.
    # A publisher may decline a passing verdict, with a reason, once (#54); the work
    # goes back for rework rather than the money back to them.
    IssueState.ACCEPTED: frozenset({IssueState.PAID, IssueState.REWORK}),
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
# publisher, when the publisher names no term of their own (#172). The escrow
# contract holds the deadline the term implies; it takes any deadline in the future.
ESCROW_TERM = timedelta(days=14)

# The bounds on a term the publisher names (#172). The contract only checks that a
# deadline is in the future, so without a floor a publisher could commit for a second
# and reclaim before anyone could work, and without a ceiling strand the money for
# years. Whole days, both ways.
MIN_ESCROW_TERM = timedelta(days=3)
MAX_ESCROW_TERM = timedelta(days=90)

# How long before the escrow deadline the silent-publisher release fires when the
# deadline caps the grace. The contract refuses a release after its deadline, so a
# release due at the deadline itself would be tried a sweep too late, every time; a
# short term always caps the grace, which made that the common case (#172).
RELEASE_MARGIN = timedelta(minutes=10)

# How long approved terms stand, while nothing is committed to the escrow. Within it,
# approving again keeps the terms the plan already handed the wallet rather than moving
# the deadline under a publisher who is mid-click; after it, the approval sets new ones.
# Once money is in the escrow the terms are not replaced at all.
COMMIT_WINDOW = timedelta(hours=24)

# How far before the approved deadline a commitment's own deadline may sit and still be
# booked. The approval fixes the deadline to the second and the plan hands the wallet
# exactly that, so the slack only covers a wallet that rounds; any earlier would cut the
# contributor's window short. The escrow refuses one later than the approved deadline.
DEADLINE_TOLERANCE = timedelta(hours=1)


@dataclass(frozen=True)
class FundingTerms:
    """What the publisher approved at the price checkpoint, and what booking checks the
    commitment against.

    Fixed when the price is approved and kept with the issue. Recomputing the deadline
    from the clock at booking time instead is what refused a commitment for good once an
    hour had passed between the plan and the booking (#126): the escrow's term starts at
    the wallet's commit, so the deadline it holds is earlier than a later `now + 14d`,
    and the check could never be satisfied again with the money already in.
    """

    amount: Usdc
    """The price the publisher named, which the escrow holds as its ceiling."""
    fee_bps: int
    """The take rate, fixed on the escrow before the money goes on."""
    wallet: str
    """The wallet the publisher funds from: the one the plan hands the commitment to
    (#123). Not the Circle wallet a publisher may also set up."""
    deadline: datetime
    """The escrow deadline, to the second. The latest the escrow accepts and the
    earliest this will book."""
    approved_at: datetime

    def stand(self, now: datetime) -> bool:
        """Whether a new approval keeps these terms rather than setting new ones."""
        return now < self.approved_at + COMMIT_WINDOW


class EscrowTermRefused(ValueError):
    """A term the publisher named that the escrow should not hold, with the reason."""


def escrow_term(
    days: Decimal | int | str | None,
    *,
    shortest: timedelta = MIN_ESCROW_TERM,
    longest: timedelta = MAX_ESCROW_TERM,
) -> timedelta:
    """The term a publisher named, checked; 14 days when they named none (#172).

    Whole days only, so the deadline a contributor reads is a date, not an instant
    someone chose to the second. The refusal names the bound it broke.
    """
    if days is None:
        return ESCROW_TERM
    try:
        value = Decimal(str(days).strip())
    except (InvalidOperation, ValueError) as exc:
        raise EscrowTermRefused(f"{days!r} is not a number of days") from exc
    if not value.is_finite():
        raise EscrowTermRefused(f"{days!r} is not a number of days")
    if value != value.to_integral_value():
        raise EscrowTermRefused(f"name the escrow term in whole days, not {value}")
    low, high = shortest.days, longest.days
    if value <= 0:
        raise EscrowTermRefused(f"the escrow term must be {low} to {high} days, not {value}")
    if value < low:
        raise EscrowTermRefused(
            f"the escrow term must be at least {low} days, so a contributor has time to "
            f"work before the money can go back; {value} is too short"
        )
    if value > high:
        raise EscrowTermRefused(
            f"the escrow term can be at most {high} days, so the money is never stranded; "
            f"{value} is too long"
        )
    return timedelta(days=int(value))


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
    deadline. A verdict that leaves less than the full grace on the clock releases a
    `RELEASE_MARGIN` before the deadline instead of later: uncapped, the release the
    grace exists for would land after the contract has closed and the only path left
    would be the refund to the publisher that the grace was written to avoid. Due at
    the deadline itself, the sweep that found it due would already be past it.
    """
    # Capped, it is due a margin before the deadline: a sweep that finds it due can
    # still land it. A verdict later than that is due at once (#172).
    return min(decided_at + SILENT_PUBLISHER_GRACE, deadline - RELEASE_MARGIN)
