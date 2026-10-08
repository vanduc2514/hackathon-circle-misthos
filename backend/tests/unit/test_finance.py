"""#43: a publisher's books, read-only, for the affordability ceiling."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from misthos.domain.money import Usdc
from misthos.services.finance import Beancount, FinanceError, FireflyIII, build_finance
from misthos.services.finance.beancount import parse, period_start

NOW = datetime(2026, 10, 7, 12, tzinfo=UTC)


def usd(value: str) -> Usdc:
    return Usdc.from_decimal(value)


class TestFireflyIII:
    def _firefly(self, seen: list[httpx.Request], *, status: int = 200) -> FireflyIII:
        def serve(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if status != 200:
                return httpx.Response(status)
            if request.url.path == "/api/v1/budgets/3/limits":
                return httpx.Response(200, json={"data": [
                    {"id": "1", "attributes": {
                        "amount": "2000.00", "currency_code": "USD", "start": "2026-10-01",
                        "spent": [{"sum": "-750.25", "currency_code": "USD"}],
                    }},
                    {"id": "2", "attributes": {"amount": "900", "currency_code": "EUR"}},
                ], "meta": {"pagination": {"total_pages": 1}}})  # fmt: skip
            assert request.url.path == "/api/v1/accounts"
            page = request.url.params["page"]
            rows = {
                "1": [
                    {"attributes": {"current_balance": "10000.50", "currency_code": "USD"}},
                    {"attributes": {"current_balance": "99", "currency_code": "USD",
                                    "active": False}},
                ],
                "2": [{"attributes": {"current_balance": "500", "currency_code": "EUR"}}],
            }[page]  # fmt: skip
            return httpx.Response(
                200, json={"data": rows, "meta": {"pagination": {"total_pages": 2}}}
            )

        return FireflyIII(
            "https://firefly.acme.example/", "pat-1", "3", transport=httpx.MockTransport(serve)
        )

    def test_reads_what_remains_of_todays_budget_and_the_cash(self) -> None:
        seen: list[httpx.Request] = []
        read = self._firefly(seen).read(NOW)
        assert read.source == "Firefly III"
        assert read.budget_remaining == usd("1249.75")
        assert read.cash == usd("10000.50")
        assert "EUR" in read.note

    def test_only_reads_and_only_for_today(self) -> None:
        seen: list[httpx.Request] = []
        self._firefly(seen).read(NOW)
        assert {r.method for r in seen} == {"GET"}
        limits = seen[0]
        assert limits.url.params["start"] == limits.url.params["end"] == "2026-10-07"
        assert limits.headers["Authorization"] == "Bearer pat-1"

    def test_a_refused_token_is_an_error_not_a_zero_budget(self) -> None:
        with pytest.raises(FinanceError, match="refused the token"):
            self._firefly([], status=401).read(NOW)


class TestBooksThatAnswerWithSomethingElse:
    """The declared budget stands in for books that cannot be read only if a failed read
    is a FinanceError: anything else escapes the store's guard and breaks publishing,
    repricing and relisting for that publisher."""

    REPLIES: dict[str, Callable[[], httpx.Response]] = {
        "a login page": lambda: httpx.Response(
            200, text="<html><body>Sign in</body></html>", headers={"content-type": "text/html"}
        ),
        "an empty body": lambda: httpx.Response(200, content=b""),
        "a page count that is not a number": lambda: httpx.Response(
            200, json={"data": [], "meta": {"pagination": {"total_pages": "many"}}}
        ),
        "a list for a document": lambda: httpx.Response(200, json=["data"]),
        "rows that are not objects": lambda: httpx.Response(200, json={"data": ["1", "2"]}),
        "attributes that are null": lambda: httpx.Response(
            200, json={"data": [{"id": "1", "attributes": None}]}
        ),
        "spending that is not a list": lambda: httpx.Response(
            200,
            json={
                "data": [{"attributes": {"amount": "10", "currency_code": "USD", "spent": "750"}}]
            },
        ),
        "an amount that is not a number": lambda: httpx.Response(
            200, json={"data": [{"attributes": {"amount": "NaN", "currency_code": "USD"}}]}
        ),
        "an amount without end": lambda: httpx.Response(
            200, json={"data": [{"attributes": {"amount": "Infinity", "currency_code": "USD"}}]}
        ),
    }

    @pytest.mark.parametrize("reply", REPLIES.values(), ids=REPLIES.keys())
    def test_firefly_is_a_finance_error(self, reply: Callable[[], httpx.Response]) -> None:
        transport = httpx.MockTransport(lambda _request: reply())
        books = FireflyIII("https://firefly.acme.example", "pat", "3", transport=transport)
        with pytest.raises(FinanceError):
            books.read(NOW)

    def test_a_ledger_that_is_not_utf8_is_a_finance_error(self, tmp_path: Path) -> None:
        ledger = tmp_path / "latin1.beancount"
        text = '2026-10-02 * "Caf\u00e9"\n  Expenses:OpenSource  5.00 USD\n  Assets:Bank\n'
        ledger.write_bytes(text.encode("latin-1"))
        with pytest.raises(FinanceError):
            Beancount(ledger, "Expenses:OpenSource", ["Assets:Bank"]).read(NOW)

    def test_a_ledger_with_an_impossible_date_is_a_finance_error(self, tmp_path: Path) -> None:
        ledger = tmp_path / "typo.beancount"
        typo = '2026-02-30 * "Typo"\n  Expenses:OpenSource  5.00 USD\n  Assets:Bank\n'
        ledger.write_text(LEDGER + "\n" + typo)
        with pytest.raises(FinanceError, match="2026-02-30"):
            Beancount(ledger, "Expenses:OpenSource", ["Assets:Bank"]).read(NOW)

    def test_a_budget_past_what_a_decimal_holds_is_a_finance_error(self, tmp_path: Path) -> None:
        ledger = tmp_path / "huge.beancount"
        huge = "99999999999999999999999.00"
        ledger.write_text(f'2026-10-01 custom "budget" Expenses:OpenSource "monthly" {huge} USD\n')
        with pytest.raises(FinanceError):
            Beancount(ledger, "Expenses:OpenSource", ["Assets:Bank"]).read(NOW)


LEDGER = """
option "operating_currency" "USD"
include "prices.beancount"

2026-01-01 custom "budget" Expenses:OpenSource "monthly" 1500.00 USD
2026-10-01 custom "budget" Expenses:OpenSource "monthly" 2000.00 USD

2026-01-01 * "Opening balance"
  Assets:Bank:Checking   25,000.00 USD
  Equity:Opening

2026-09-28 * "Last month's fix"
  Expenses:OpenSource     300.00 USD
  Assets:Bank:Checking

2026-10-02 * "Misthos" "ISS-1004"
  invoice: "inv-1"
  Expenses:OpenSource            500.00 USD ; the fix
  Expenses:OpenSource:Reviews     60.00 USD
  Assets:Bank:Checking          -560.00 USD

2026-10-05 ! "Euro contractor"
  Expenses:OpenSource     100.00 EUR
  Assets:Bank:Euro       -100.00 EUR

2026-10-20 * "Next week"
  Expenses:OpenSource     999.00 USD
  Assets:Bank:Checking
"""


class TestBeancount:
    def test_reads_the_fava_budget_in_effect_less_this_periods_spending(self) -> None:
        read = Beancount("unused", "Expenses:OpenSource", ["Assets:Bank"]).context(LEDGER, NOW)
        # 2,000 this month, less 500 and the 60 under it; September and the future do
        # not count, and euros are not dollars.
        assert read.budget_remaining == usd("1440.00")
        assert read.cash == usd("24140.00")
        assert "EUR" in read.note
        assert "include directives are not followed" in read.note

    def test_an_elided_amount_balances_the_transaction(self) -> None:
        postings, _, _ = parse(LEDGER)
        opening = [p for p in postings if p.day == date(2026, 1, 1)]
        assert sum(p.amount for p in opening) == 0

    def test_without_a_budget_nothing_is_capped(self) -> None:
        read = Beancount("unused", "Expenses:Other", ["Assets:Bank"]).context(LEDGER, NOW)
        assert read.budget_remaining is None
        assert "no budget is set" in read.note

    def test_periods_start_where_fava_starts_them(self) -> None:
        day = date(2026, 8, 13)  # a Thursday
        assert period_start(day, "weekly") == date(2026, 8, 10)
        assert period_start(day, "quarterly") == date(2026, 7, 1)
        assert period_start(day, "yearly") == date(2026, 1, 1)

    def test_an_unreadable_ledger_is_an_error(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(FinanceError):
            Beancount(tmp_path / "missing.beancount", "Expenses:OpenSource", []).read(NOW)


class TestConnections:
    def test_the_operator_connects_publishers(self) -> None:
        finance = build_finance(
            '{"PUB-1": {"kind": "firefly", "url": "https://f.example", "token": "t",'
            ' "budget_id": "3"}, "PUB-2": {"kind": "beancount", "path": "/books/a.beancount",'
            ' "budget_account": "Expenses:OpenSource"}}'
        )
        assert isinstance(finance("PUB-1"), FireflyIII)
        assert isinstance(finance("PUB-2"), Beancount)
        assert finance("PUB-3") is None

    def test_nothing_configured_connects_nobody(self) -> None:
        assert build_finance("")("PUB-1") is None

    @pytest.mark.parametrize(
        "raw", ["not json", "[1]", '{"PUB-1": {"kind": "odoo"}}', '{"PUB-1": {"kind": "firefly"}}']
    )
    def test_a_bad_configuration_fails_loudly(self, raw: str) -> None:
        with pytest.raises(FinanceError):
            build_finance(raw)
