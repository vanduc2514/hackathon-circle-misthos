"""Paid calls: what an agent bought over x402, and what the books say it paid (#201).

The edge sells read-only views of what Misthos already knows. It verifies a buyer's
payment, has the core do the work, and settles the payment only when the work
succeeded, so every row here is a call that was answered and paid for. The edge writes
one row per settlement; nothing else writes them, and nothing edits one.

Circle Gateway is the authority on the money, as the chain is for the escrow: a paid
call the books hold that Gateway did not settle, or the reverse, is a divergence. The
totals here are what an operator compares with Gateway's balance for the seller.

Nothing here reads a database or the network.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from misthos.domain.money import Usdc


@dataclass(frozen=True)
class PaidCall:
    id: str
    route: str
    """The catalog id of the route that was paid for, as the edge names it."""
    request_digest: str
    """The edge's SHA-256 of the request, so a reused payment can be recognised."""
    payer: str
    """The buyer's wallet, lowercased. An agent's address, not a person's identity."""
    amount: Usdc
    network: str
    """CAIP-2, such as `eip155:5042002` for Arc testnet."""
    asset: str
    """The USDC contract the payment was in."""
    transaction: str
    """Gateway's reference for the settlement."""
    ref: str | None
    """What the call was about or produced, when it is worth naming: an issue, a
    contributor, an async job."""
    settled_at: datetime


@dataclass(frozen=True)
class PriceQuote:
    """One price the paid API quoted, with what it was asked (#203).

    Kept for audit and for calibrating the engine: an agent's question is a sample of
    the market the engine prices. `request_digest` is the edge's digest of the request,
    the same one its settlement carries, so a quote joins the payment that bought it.
    A quote whose payment was never settled has no paid call beside it.
    """

    id: str
    route: str
    request_digest: str | None
    payer: str | None
    free_caller: str | None
    inputs: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    quoted_at: datetime | None = None


@dataclass(frozen=True)
class PaidReview:
    """One pre-flight review an agent paid for, and the job that ran it (#204).

    The row is the job: queued when the edge has verified the payment, running while
    the reviewer reads the diff, then done or failed. The edge settles the payment
    when it collects a done review and never for a failed one. Kept afterwards for
    calibration: how long the review took, what it cost and what it decided. The
    diff itself is not kept, only its size.
    """

    id: str
    status: str
    """queued, running, done or failed."""
    request_digest: str | None
    payer: str | None
    free_caller: str | None
    repo: str | None
    title: str
    criteria: int
    files: int
    additions: int
    deletions: int
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    verdict: str | None = None
    rule: str | None = None
    reviewer: str | None = None
    seconds: float | None = None
    cost: Usdc | None = None
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True)
class RouteTotal:
    route: str
    network: str
    calls: int
    amount: Usdc


def totals(calls: Iterable[PaidCall]) -> list[RouteTotal]:
    """What each route earned on each network, in route order."""
    counted: dict[tuple[str, str], list[PaidCall]] = defaultdict(list)
    for call in calls:
        counted[(call.route, call.network)].append(call)
    return [
        RouteTotal(
            route=route,
            network=network,
            calls=len(group),
            amount=Usdc(sum(c.amount.base_units for c in group)),
        )
        for (route, network), group in sorted(counted.items())
    ]
