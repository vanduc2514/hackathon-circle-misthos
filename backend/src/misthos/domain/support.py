"""The support commitment the Enterprise plan makes (#53): how soon a request is answered.

A commitment nobody measures is a sentence on a pricing page. So a request records when
it was opened and the moment its first response is due, the sweeper raises the alarm
when that moment passes unanswered, and the organisation sees both. Only the first
response is measured: that is what the commitment promises, and what a person stuck
on a held payout is waiting for.

Two severities. Urgent is money that cannot move, a payout held or a commitment refused,
and is answered around the clock. Anything else is answered by the same time on the
next working day, Monday to Friday in UTC. The figures live here and nowhere else, so
the plan's wording and the deadlines cannot disagree.

Pure arithmetic.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

Severity = Literal["urgent", "normal"]

URGENT_RESPONSE = timedelta(hours=4)
SATURDAY = 5


def respond_by(opened_at: datetime, severity: Severity) -> datetime:
    """When the first response is due."""
    if severity == "urgent":
        return opened_at + URGENT_RESPONSE
    due = opened_at + timedelta(days=1)
    while due.weekday() >= SATURDAY:
        due += timedelta(days=1)
    return due


def overdue(due: datetime, answered_at: datetime | None, now: datetime) -> bool:
    """Unanswered past its deadline, or answered after it."""
    return (answered_at or now) > due


COMMITMENT = (
    f"A first response within {int(URGENT_RESPONSE.total_seconds() // 3600)} hours, around "
    "the clock, when money cannot move; by the same time the next working day otherwise"
)
