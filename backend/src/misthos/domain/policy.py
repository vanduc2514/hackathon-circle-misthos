"""An organisation's own spending policy: category limits, a release threshold, and
who may approve a release above it (#51).

The limit has to sit where the agent cannot reach it, and an organisation sets it
for itself rather than accepting ours. So the policy is checked by the store at the
two moments money moves, never by the agent that prices or reviews:

- Funding: a commitment that would take a category past its monthly limit is
  refused, with the limit and the month's total in the reason.
- Release: a payout above the threshold waits for one of the named approvers, and
  waits whether it was triggered by a merge or by the grace period. The approval is
  required, not requested.

An approver is named by GitHub login, because that is the one thing about a person a
session proves: the wallet signs in, and the login is linked to it through GitHub's
own OAuth. Nothing proves an e-mail address. So the approval is the signed-in
account's, matched by its linked login, and a name in the request counts for nothing;
otherwise anyone who knew an approver's name could release the money. Holding the
organisation's wallet confers nothing either: its account approves only if its own
login is named. And the contributor being paid never approves their own payout, even
if named, or the second signature would be theirs.

Pure arithmetic, like the rest of domain/.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from misthos.domain.ledger import MoneyEvent, MoneyEventKind
from misthos.domain.money import Usdc


class PolicyRefusal(Exception):
    """The organisation's own policy does not allow this."""


# GitHub's own shape for a login: letters, digits and hyphens, at most 39, not
# starting with a hyphen.
_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")


def login(name: str) -> str:
    """An approver as named, the way GitHub would print it: `@Dana ` is `Dana`."""
    return name.strip().removeprefix("@")


@dataclass(frozen=True)
class SpendingPolicy:
    approval_threshold: Usdc | None = None
    """Releases above this amount wait for a named approver. None: no threshold."""
    approvers: tuple[str, ...] = ()
    """GitHub logins, matched against the signed-in account's linked login."""
    category_limits: Mapping[str, Usdc] = field(default_factory=dict)
    """Issue label to the most that may be committed under it in one calendar month."""

    def validate(self) -> None:
        if self.approval_threshold is not None and not self.approvers:
            raise PolicyRefusal("a release threshold needs at least one named approver")
        if self.approval_threshold is not None and self.approval_threshold.base_units < 0:
            raise PolicyRefusal("a release threshold cannot be negative")
        for name in self.approvers:
            if not _LOGIN.fullmatch(login(name)):
                # It could never match a session, so the payouts it holds would wait
                # for good.
                raise PolicyRefusal(f"name approvers by GitHub login; {name} is not one")
        for label, limit in self.category_limits.items():
            if limit.base_units < 0:
                raise PolicyRefusal(f"the limit for {label} cannot be negative")


def needs_approval(policy: SpendingPolicy, amount: Usdc) -> bool:
    return policy.approval_threshold is not None and amount > policy.approval_threshold


def may_approve(policy: SpendingPolicy, approver: str) -> bool:
    """Whether this GitHub login is one the organisation named. Logins are matched
    the way GitHub matches them, ignoring case."""
    return login(approver).lower() in {login(a).lower() for a in policy.approvers}


def approves_own_payout(approver: str, payee: str) -> bool:
    """Whether the approver is the contributor the release would pay, by login."""
    return login(approver).lower() == login(payee).lower()


def month_start(now: datetime) -> datetime:
    """The start of the calendar month a monthly limit counts from."""
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def committed_this_month(
    issues: Iterable[tuple[Collection[str], Iterable[MoneyEvent]]], now: datetime
) -> dict[str, Usdc]:
    """What is committed under each label since `now`'s month began, from each issue's
    labels and ledger. It is the figure a limit is checked against at funding and the
    one shown beside the limit, so the two cannot disagree about the window."""
    since = month_start(now)
    committed: dict[str, Usdc] = {}
    for labels, events in issues:
        for event in events:
            if event.kind is MoneyEventKind.COMMITTED and event.occurred_at >= since:
                for label in labels:
                    committed[label] = committed.get(label, Usdc(0)) + event.amount
    return committed


def category_breaches(
    policy: SpendingPolicy,
    labels: Iterable[str],
    amount: Usdc,
    committed_this_month: Mapping[str, Usdc],
) -> list[str]:
    """Every category this commitment would take past its monthly limit, said plainly."""
    breaches = []
    for label in labels:
        limit = policy.category_limits.get(label)
        if limit is None:
            continue
        already = committed_this_month.get(label, Usdc(0))
        if already + amount > limit:
            breaches.append(
                f"{label} is limited to {limit} a month, {already} is committed already, "
                f"and this would add {amount}"
            )
    return breaches
