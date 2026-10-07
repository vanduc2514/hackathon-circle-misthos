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

Pure arithmetic, like the rest of domain/.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from misthos.domain.money import Usdc


class PolicyRefusal(Exception):
    """The organisation's own policy does not allow this."""


@dataclass(frozen=True)
class SpendingPolicy:
    approval_threshold: Usdc | None = None
    """Releases above this amount wait for a named approver. None: no threshold."""
    approvers: tuple[str, ...] = ()
    category_limits: Mapping[str, Usdc] = field(default_factory=dict)
    """Issue label to the most that may be committed under it in one calendar month."""

    def validate(self) -> None:
        if self.approval_threshold is not None and not self.approvers:
            raise PolicyRefusal("a release threshold needs at least one named approver")
        if self.approval_threshold is not None and self.approval_threshold.base_units < 0:
            raise PolicyRefusal("a release threshold cannot be negative")
        for label, limit in self.category_limits.items():
            if limit.base_units < 0:
                raise PolicyRefusal(f"the limit for {label} cannot be negative")


def needs_approval(policy: SpendingPolicy, amount: Usdc) -> bool:
    return policy.approval_threshold is not None and amount > policy.approval_threshold


def may_approve(policy: SpendingPolicy, approver: str) -> bool:
    return approver.strip().lower() in {a.strip().lower() for a in policy.approvers}


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
