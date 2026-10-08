"""#53: the plans as 06 publishes them, and reading a payment from Arc."""

from __future__ import annotations

import httpx
import pytest

from misthos.domain import plans
from misthos.domain.money import Usdc
from misthos.services.billing import TRANSFER_TOPIC, ArcRail, PaymentError

USDC = "0x3600000000000000000000000000000000000000"
PAYER = "0x00000000000000000000000000000000000000a1"
PLATFORM = "0x00000000000000000000000000000000000000b2"
TX = "0x" + "ab" * 32


class TestTheCatalogue:
    def test_prices_take_rates_and_floors_match_the_published_tiers(self) -> None:
        open_, team, enterprise = (plans.PLANS[k] for k in ("open", "team", "enterprise"))
        assert (open_.monthly, team.monthly) == (Usdc(0), Usdc.from_decimal("249"))
        assert enterprise.monthly == Usdc.from_decimal("2000") and not enterprise.self_serve
        assert [float(p.take_rate) for p in (open_, team, enterprise)] == [0.12, 0.10, 0.08]
        floors = [p.minimum_fix for p in (open_, team, enterprise)]
        assert floors == [Usdc.from_decimal(v) for v in ("55", "65", "80")]

    def test_each_tier_adds_to_the_one_below(self) -> None:
        assert plans.PLANS["open"].features < plans.PLANS["team"].features
        assert plans.PLANS["team"].features < plans.PLANS["enterprise"].features

    def test_a_refusal_names_the_plan_to_upgrade_to(self) -> None:
        assert "Team plan" in plans.refusal(plans.Feature.SPEND)
        assert "Enterprise plan" in plans.refusal(plans.Feature.AUDIT)
        assert not plans.allows("open", plans.Feature.POLICY)
        assert plans.allows("team", plans.Feature.POLICY)


def topic(address: str) -> str:
    return "0x" + "0" * 24 + address[2:].lower()


def rail(receipt: dict | None, *, error: dict | None = None, seen: list | None = None) -> ArcRail:
    def answer(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        body = {"jsonrpc": "2.0", "id": 1, "result": receipt}
        if error:
            body = {"jsonrpc": "2.0", "id": 1, "error": error}
        return httpx.Response(200, json=body)

    return ArcRail("https://rpc.example", USDC, transport=httpx.MockTransport(answer))


def transfer(frm: str, to: str, amount: int, token: str = USDC) -> dict:
    return {"address": token, "topics": [TRANSFER_TOPIC, topic(frm), topic(to)],
            "data": hex(amount)}  # fmt: skip


class TestReadingAPaymentFromArc:
    def test_a_usdc_transfer_from_the_payer_to_the_platform_is_seen(self) -> None:
        seen: list[httpx.Request] = []
        receipt = {"status": "0x1", "logs": [transfer(PAYER, PLATFORM, 249_000_000)]}
        got = rail(receipt, seen=seen).received(TX, payer=PAYER, payee=PLATFORM)
        assert got is not None and got.amount == Usdc.from_decimal("249")
        assert b"eth_getTransactionReceipt" in seen[0].content

    def test_other_tokens_and_other_parties_do_not_count(self) -> None:
        other = "0x" + "99" * 20
        logs = [
            transfer(PAYER, PLATFORM, 249_000_000, token=other),
            transfer(other, PLATFORM, 249_000_000),
            transfer(PAYER, other, 249_000_000),
        ]
        assert (
            rail({"status": "0x1", "logs": logs}).received(TX, payer=PAYER, payee=PLATFORM) is None
        )

    def test_a_failed_or_unknown_transaction_pays_nothing(self) -> None:
        failed = {"status": "0x0", "logs": [transfer(PAYER, PLATFORM, 249_000_000)]}
        assert rail(failed).received(TX, payer=PAYER, payee=PLATFORM) is None
        assert rail(None).received(TX, payer=PAYER, payee=PLATFORM) is None

    def test_an_rpc_that_refuses_is_an_error_not_a_missing_payment(self) -> None:
        with pytest.raises(PaymentError):
            rail(None, error={"code": -32000, "message": "down"}).received(
                TX, payer=PAYER, payee=PLATFORM
            )
        with pytest.raises(PaymentError, match="not a transaction hash"):
            rail(None).received("0x1234", payer=PAYER, payee=PLATFORM)
