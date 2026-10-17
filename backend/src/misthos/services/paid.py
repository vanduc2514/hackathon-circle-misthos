"""Who the paid API is free to (#201).

Every paid route an agent buys is also something a signed-in person may already be
entitled to see: a publisher pricing their own issue, a contributor reading their own
receipt. Those callers should not pay. The edge cannot tell, because it holds no
sessions, so it asks the core, which decides per route with the rules below.

A rule takes the signed-in account and the route's parameters and says whether this
call is free. A route without a rule is never free to a session, and no rule makes a
call free to an account without a linked GitHub login; the edge's API keys are the
only other way past the price.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from misthos.schemas import Account

FreeRule = Callable[[Account, Mapping[str, str]], bool]

FREE_TIER: dict[str, FreeRule] = {}
"""Route id to the rule that makes it free. Each paid route that has a free tier adds
its own rule where the route is defined."""


def free_tier(route: str) -> Callable[[FreeRule], FreeRule]:
    """Register the rule that makes `route` free to a signed-in caller."""

    def register(rule: FreeRule) -> FreeRule:
        FREE_TIER[route] = rule
        return rule

    return register


def is_free(route: str, account: Account | None, params: Mapping[str, str]) -> bool:
    """Free only to an account with a linked GitHub login, and only where the route's
    rule says so. A wallet alone is not enough: every x402 buyer already has one, and
    signing in with it costs nothing, so a free tier keyed on a wallet would be free to
    every buyer (#201)."""
    rule = FREE_TIER.get(route)
    return (
        account is not None
        and bool(account.github_login)
        and rule is not None
        and rule(account, params)
    )


def who(account: Account) -> str:
    return f"{account.role}:{account.party_id}"


@dataclass(frozen=True)
class PaidCaller:
    """Who a paid call is for, as the edge reports it."""

    route: str
    payer: str | None
    """The wallet that paid, when someone paid."""
    free_caller: str | None
    """`role:party_id` when the call was free to a session, or `api-key`."""

    @property
    def party(self) -> tuple[str, str] | None:
        """The free caller's role and party, when the call was free to a session."""
        if not self.free_caller or ":" not in self.free_caller:
            return None
        role, _, party_id = self.free_caller.partition(":")
        return role, party_id
