"""Firefly III, read-only: one budget's remaining amount and the asset accounts' cash.

Firefly III is personal finance software with no chart of accounts (06, part two),
so the publisher names the one budget open-source work comes out of. What remains is
that budget's limits for the period containing today, less what was spent against
them. Its own documentation refuses programmatic writes, and we do not attempt any:
a personal access token is used for GET requests and nothing else.

API: `GET /api/v1/budgets/{id}/limits?start&end` returns limits with `amount` and a
`spent` list of `{sum, currency_code}`; `GET /api/v1/accounts?type=asset` returns
accounts with `current_balance` and `currency_code`. Both are JSON:API documents,
paginated under `meta.pagination`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from misthos.domain.money import Usdc
from misthos.services.finance.base import DOLLARS, FinanceContext, FinanceError, dollars

MAX_PAGES = 20


class FireflyIII:
    name = "Firefly III"

    def __init__(
        self,
        base_url: str,
        token: str,
        budget_id: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 5.0,
    ) -> None:
        if not (base_url and token and budget_id):
            raise FinanceError("a Firefly III connection needs a URL, a token and a budget")
        self.base_url = base_url.rstrip("/")
        self.budget_id = str(budget_id)
        self._token = token
        self._transport = transport
        self._timeout = timeout

    def read(self, now: datetime) -> FinanceContext:
        today = now.date().isoformat()
        left_out: set[str] = set()
        try:
            with httpx.Client(
                base_url=self.base_url,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/vnd.api+json",
                },
                transport=self._transport,
                timeout=self._timeout,
            ) as http:
                limits = self._all(
                    http,
                    f"/api/v1/budgets/{self.budget_id}/limits",
                    {"start": today, "end": today},
                )
                accounts = self._all(http, "/api/v1/accounts", {"type": "asset"})
        except httpx.HTTPError as exc:
            raise FinanceError(f"Firefly III could not be read: {exc}") from exc

        budget: Usdc | None = None
        for limit in limits:
            a = limit.get("attributes", {})
            if a.get("currency_code") not in DOLLARS:
                left_out.add(str(a.get("currency_code")))
                continue
            amount = dollars(a.get("amount", "0"))
            spent = sum(
                (abs(dollars(s.get("sum", "0")).base_units) for s in a.get("spent") or []
                 if s.get("currency_code") in DOLLARS),
                0,
            )  # fmt: skip
            budget = (budget or Usdc(0)) + Usdc(amount.base_units - spent)

        cash: Usdc | None = None
        for account in accounts:
            a = account.get("attributes", {})
            if a.get("active") is False:
                continue
            if a.get("currency_code") not in DOLLARS:
                left_out.add(str(a.get("currency_code")))
                continue
            cash = (cash or Usdc(0)) + dollars(a.get("current_balance", "0"))

        notes = []
        if budget is None:
            notes.append("the budget has no dollar limit for today, so it caps nothing")
        if left_out - {"None"}:
            notes.append(f"amounts in {', '.join(sorted(left_out - {'None'}))} are not counted")
        return FinanceContext(
            source=self.name,
            budget_remaining=budget,
            cash=cash,
            as_of=now,
            note="; ".join(notes),
        )

    def _all(self, http: httpx.Client, path: str, params: dict[str, str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        page = 1
        while page <= MAX_PAGES:
            response = http.get(path, params={**params, "page": str(page)})
            if response.status_code in (401, 403):
                raise FinanceError("Firefly III refused the token")
            if response.status_code == 404:
                raise FinanceError(f"Firefly III has no {path}")
            response.raise_for_status()
            body = response.json()
            rows.extend(body.get("data") or [])
            pagination = (body.get("meta") or {}).get("pagination") or {}
            if page >= int(pagination.get("total_pages") or 1):
                break
            page += 1
        return rows
