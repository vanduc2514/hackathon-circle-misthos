"""beancount, read-only: a Fava budget's remainder and the cash accounts' balance.

A plain-text ledger has no budgets of its own, so we read the convention Fava uses:

    2026-01-01 custom "budget" Expenses:OpenSource "monthly" 2000.00 USD

The budget in effect today is the latest such directive for the account, and what
remains is its amount for the period containing today (the day, week, month, quarter
or year it names) less what was posted to that account, or below it, in that period.
Cash is the balance of the cash accounts named in the connection.

This reads the subset of the format those two figures need: transactions with their
postings, including one elided amount, and the budget directive. Prices, costs,
arithmetic and `include` are not followed; amounts not in dollars are left out. The
ledger is never written to.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from misthos.domain.money import Usdc
from misthos.services.finance.base import DOLLARS, FinanceContext, FinanceError

_ACCOUNT = r"[A-Z][A-Za-z0-9-]*(?::[A-Z0-9][A-Za-z0-9-]*)+"
_TXN = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+(?:\*|!|txn)(?:\s|$)")
_POSTING = re.compile(
    rf"^\s+(?:[*!]\s+)?(?P<account>{_ACCOUNT})"
    r"(?:\s+(?P<amount>-?[\d,]*\.?\d+)\s+(?P<currency>[A-Z][A-Z0-9'._-]*))?\s*(?:[{@;].*)?$"
)
_BUDGET = re.compile(
    rf'^(\d{{4}}-\d{{2}}-\d{{2}})\s+custom\s+"budget"\s+(?P<account>{_ACCOUNT})\s+'
    r'"(?P<period>daily|weekly|monthly|quarterly|yearly)"\s+'
    r"(?P<amount>[\d,]*\.?\d+)\s+(?P<currency>[A-Z][A-Z0-9'._-]*)"
)
_INCLUDE = re.compile(r'^include\s+"')


@dataclass(frozen=True)
class Posting:
    day: date
    account: str
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class Budget:
    day: date
    account: str
    period: str
    amount: Decimal
    currency: str


def _amount(text: str) -> Decimal:
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation as exc:
        raise FinanceError(f"not an amount: {text}") from exc


def _day(text: str) -> date:
    # The pattern admits 2026-02-30, which only the calendar refuses.
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise FinanceError(f"not a date: {text}") from exc


def parse(text: str) -> tuple[list[Posting], list[Budget], list[str]]:
    """Postings and budget directives, and what was not followed."""
    postings: list[Posting] = []
    budgets: list[Budget] = []
    skipped: set[str] = set()
    current: date | None = None
    pending: list[tuple[str, Decimal | None, str | None]] = []

    def close() -> None:
        nonlocal pending
        if current is not None and pending:
            elided = [p for p in pending if p[1] is None]
            stated = [p for p in pending if p[1] is not None]
            if len(elided) == 1:
                totals: dict[str, Decimal] = defaultdict(Decimal)
                for _, amount, currency in stated:
                    totals[currency or ""] += amount or Decimal(0)
                if len(totals) == 1:
                    ((currency, total),) = totals.items()
                    stated.append((elided[0][0], -total, currency))
                else:
                    skipped.add(
                        "a transaction balancing several currencies by elision is not counted"
                    )
            elif len(elided) > 1:
                skipped.add("a transaction with more than one elided amount is not counted")
            for account, amount, currency in stated:
                postings.append(Posting(current, account, amount or Decimal(0), currency or ""))
        pending = []

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith(";"):
            continue
        if not line[0].isspace():
            close()
            current = None
            if found := _TXN.match(line):
                current = _day(found[1])
            elif found := _BUDGET.match(line):
                budgets.append(
                    Budget(
                        day=_day(found[1]),
                        account=found["account"],
                        period=found["period"],
                        amount=_amount(found["amount"]),
                        currency=found["currency"],
                    )
                )
            elif _INCLUDE.match(line):
                skipped.add("include directives are not followed")
            continue
        if current is None:
            continue
        if re.match(r"^\s+[a-z][\w-]*:\s", line):
            continue  # transaction or posting metadata
        found = _POSTING.match(line)
        if found is None:
            skipped.add("some postings use syntax this reader does not follow")
            continue
        amount = _amount(found["amount"]) if found["amount"] else None
        pending.append((found["account"], amount, found["currency"]))
    close()
    return postings, budgets, sorted(skipped)


def period_start(today: date, period: str) -> date:
    if period == "daily":
        return today
    if period == "weekly":
        return today - timedelta(days=today.weekday())
    if period == "monthly":
        return today.replace(day=1)
    if period == "quarterly":
        return today.replace(month=3 * ((today.month - 1) // 3) + 1, day=1)
    return today.replace(month=1, day=1)


def _usdc(amount: Decimal) -> Usdc:
    return Usdc.from_decimal(amount.quantize(Decimal("0.000001")))


def _under(account: str, root: str) -> bool:
    return account == root or account.startswith(root + ":")


class Beancount:
    name = "beancount"

    def __init__(self, path: str | Path, budget_account: str, cash_accounts: list[str]) -> None:
        if not (path and budget_account):
            raise FinanceError("a beancount connection needs a ledger file and a budget account")
        self.path = Path(path)
        self.budget_account = budget_account
        self.cash_accounts = list(cash_accounts) or ["Assets"]

    def read(self, now: datetime) -> FinanceContext:
        try:
            text = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # A ledger saved in another encoding fails as a ValueError, not an OSError.
            raise FinanceError(f"the ledger could not be read: {exc}") from exc
        try:
            return self.context(text, now)
        except ArithmeticError as exc:
            # An amount past what a decimal holds at USDC's precision.
            raise FinanceError(f"the ledger could not be understood: {exc}") from exc

    def context(self, text: str, now: datetime) -> FinanceContext:
        postings, budgets, skipped = parse(text)
        today = now.date()
        notes = list(skipped)

        in_effect = [
            b for b in budgets if b.account == self.budget_account and b.day <= today
        ]  # fmt: skip
        budget: Usdc | None = None
        if not in_effect:
            notes.append(f"no budget is set for {self.budget_account}")
        else:
            current = max(in_effect, key=lambda b: b.day)
            if current.currency not in DOLLARS:
                notes.append(f"the budget is in {current.currency}, which is not counted")
            else:
                start = period_start(today, current.period)
                spent = sum(
                    (p.amount for p in postings
                     if _under(p.account, self.budget_account) and start <= p.day <= today
                     and p.currency in DOLLARS),
                    Decimal(0),
                )  # fmt: skip
                budget = _usdc(current.amount - spent)

        cash_postings = [
            p for p in postings
            if any(_under(p.account, root) for root in self.cash_accounts) and p.day <= today
        ]  # fmt: skip
        other = sorted({p.currency for p in cash_postings if p.currency not in DOLLARS})
        if other:
            notes.append(f"amounts in {', '.join(other)} are not counted")
        dollar_postings = [p for p in cash_postings if p.currency in DOLLARS]
        cash = (
            _usdc(sum((p.amount for p in dollar_postings), Decimal(0))) if dollar_postings else None
        )
        return FinanceContext(
            source=self.name, budget_remaining=budget, cash=cash, as_of=now, note="; ".join(notes)
        )
