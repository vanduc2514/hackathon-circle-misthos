"""Compliance rules for money that leaves the escrow.

Three obligations from docs/misthos/08 decide whether a payout may happen, and all
three are judged here as plain rules over facts a provider has already established,
so they can be tested without a provider, a database or a chain:

- Screening is continuous, not a gate at onboarding. A counterparty is screened at
  every payout and again on a schedule while the relationship is live, so a
  contributor who is listed after being verified is caught before the next payout.
- Identity is verified when a contributor first earns, never at signup. Claiming and
  submitting work need no identity step; being paid does.
- A listed party is never paid, whatever else is true. Sanctions are checked before
  identity. A payout held for a listing is screened again every
  `PAYOUT_RETRY_INTERVAL` and released on its own once the wallet is no longer
  listed; nobody has to clear it. A failed identity check is the hold that waits for a
  person.

The platform never holds identity documents. A provider does, and the platform keeps
only the provider's reference and the outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


class ComplianceRefusal(Exception):
    """Money would move to or from a party the rules say it must not."""


class IdentityStatus(StrEnum):
    UNVERIFIED = "unverified"
    PENDING = "pending"
    VERIFIED = "verified"
    FAILED = "failed"


class ScreeningOutcome(StrEnum):
    CLEAR = "clear"
    HIT = "hit"


class ScreeningReason(StrEnum):
    FUNDING = "funding"
    PAYOUT = "payout"
    SCHEDULED = "scheduled"


class PartyKind(StrEnum):
    PUBLISHER = "publisher"
    CONTRIBUTOR = "contributor"


class PayoutGate(StrEnum):
    RELEASE = "release"
    AWAIT_IDENTITY = "await_identity"
    BLOCKED_SANCTIONS = "blocked_sanctions"
    BLOCKED_IDENTITY = "blocked_identity"
    AWAIT_APPROVER = "await_approver"
    """Over the organisation's release threshold, waiting for a named approver.
    Not a compliance gate: the organisation's own policy (domain/policy.py)."""


@dataclass(frozen=True)
class Screening:
    """One screening of one counterparty's wallet. Kept as an append-only audit trail."""

    party_kind: PartyKind
    party_id: str
    wallet_address: str
    outcome: ScreeningOutcome
    provider: str
    reason: ScreeningReason
    checked_at: datetime
    list_name: str | None = None


# How often a live counterparty is screened again with no payout prompting it.
RESCREEN_INTERVAL = timedelta(hours=24)

# How long a held payout waits before it is checked again. Long enough not to
# re-screen every sweep, short enough that a verification that finished overnight
# pays the contributor the same morning.
PAYOUT_RETRY_INTERVAL = timedelta(hours=1)

# Retention, published in docs/PRIVACY.md. Screening results are kept five years
# after the check, the usual record-keeping period for sanctions controls.
SCREENING_RETENTION = timedelta(days=5 * 365)


def payout_gate(screening: ScreeningOutcome, identity: IdentityStatus) -> PayoutGate:
    """Whether a payout may leave the escrow now, and if not, why."""
    if screening is ScreeningOutcome.HIT:
        return PayoutGate.BLOCKED_SANCTIONS
    match identity:
        case IdentityStatus.VERIFIED:
            return PayoutGate.RELEASE
        case IdentityStatus.FAILED:
            return PayoutGate.BLOCKED_IDENTITY
        case _:
            return PayoutGate.AWAIT_IDENTITY


def rescreen_due(last_checked_at: datetime | None, now: datetime) -> bool:
    return last_checked_at is None or now - last_checked_at >= RESCREEN_INTERVAL
