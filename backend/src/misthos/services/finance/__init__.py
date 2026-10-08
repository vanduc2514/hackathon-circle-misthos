"""A publisher's books, read-only, for the pricing engine's affordability ceiling.

The engine caps every price at what the publisher can afford (06, part two). The
budget a publisher declares is always there and is the honest fallback. When the
operator connects a publisher's books, the figure the books give caps the price as
well, and the lower of the two wins. Nothing is ever written to anyone's ledger.

Connections are the operator's, in `MISTHOS_FINANCE_CONNECTIONS`, and never the
API's: a connection holds a credential or a path on this server, and letting a
caller choose either would let them point our credentials, or our file reads,
wherever they liked.

    {"PUB-1": {"kind": "firefly", "url": "https://firefly.acme.example",
               "token": "...", "budget_id": "3"},
     "PUB-2": {"kind": "beancount", "path": "/books/acme.beancount",
               "budget_account": "Expenses:OpenSource", "cash_accounts": ["Assets:Bank"]}}
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

from misthos.config import settings
from misthos.services.finance.base import (
    DOLLARS,
    FinanceContext,
    FinanceError,
    FinanceSource,
    dollars,
)
from misthos.services.finance.beancount import Beancount
from misthos.services.finance.firefly import FireflyIII

__all__ = [
    "DOLLARS",
    "Beancount",
    "FinanceContext",
    "FinanceError",
    "FinanceSource",
    "FireflyIII",
    "build_finance",
    "dollars",
]

log = logging.getLogger("misthos.finance")

Finance = Callable[[str], FinanceSource | None]


def _source(config: dict) -> FinanceSource:
    kind = config.get("kind")
    if kind == "firefly":
        return FireflyIII(
            str(config.get("url", "")),
            str(config.get("token", "")),
            str(config.get("budget_id", "")),
        )
    if kind == "beancount":
        return Beancount(
            str(config.get("path", "")),
            str(config.get("budget_account", "")),
            list(config.get("cash_accounts") or []),
        )
    raise FinanceError(f"unknown finance connection kind {kind!r}")


def build_finance(raw: str | None = None) -> Finance:
    """The publisher's connected books, if the operator connected any."""
    text = settings.finance_connections if raw is None else raw
    sources: dict[str, FinanceSource] = {}
    if text.strip():
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise FinanceError(f"MISTHOS_FINANCE_CONNECTIONS is not JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise FinanceError("MISTHOS_FINANCE_CONNECTIONS maps publisher ids to connections")
        for publisher_id, config in parsed.items():
            sources[str(publisher_id)] = _source(dict(config))
        log.info("finance connected for %d publisher(s)", len(sources))
    return sources.get
