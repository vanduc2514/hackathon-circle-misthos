"""Settling against MisthosEscrow: what the platform signs, and what it only checks."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from eth_account import Account

from misthos.domain.escrow import (
    ERRORS,
    approved_publisher_call,
    ceiling_call,
    commit_call,
    commitments_call,
    fee_call,
    fee_recipient_call,
    issue_key,
    latest_deadline_call,
    refund_call,
    release_call,
    revert_name,
    selector,
    set_ceiling_call,
    set_fee_call,
)
from misthos.domain.money import Usdc
from misthos.services.chain import (
    ArcEscrow,
    ChainRevert,
    ChainUnavailable,
    EscrowDeployment,
    NotCommitted,
)
from misthos.services.chain.arc import NO_FEE_RECIPIENT
from misthos.services.chain.deployment import DEPLOYMENTS_DIR
from misthos.services.chain.rpc import JsonRpc, Sender

DEPLOYED = EscrowDeployment("0x" + "e5" * 20, 5042002, "record", deployed_at_block=7)
PUBLISHER = "0x" + "0" * 36 + "0b0b"
DEADLINE = datetime.fromtimestamp(1_800_000_000, UTC)
# commitments(bytes32) for a 180 USDC commitment by 0x...0B0B, held until DEADLINE.
HELD_180 = (
    "0x" + f"{0xB0B:064x}" + f"{180_000_000:064x}" + f"{1_800_000_000:064x}" + f"{1:064x}"
)
NONE = "0x" + "0" * 256


def _rpc(answers: dict, seen: list[dict] | None = None) -> httpx.MockTransport:
    """An RPC that answers by method, or for eth_call by its call data."""

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if seen is not None:
            seen.append(body)
        method = body["method"]
        key = body["params"][0]["data"] if method == "eth_call" else method
        # An unknown commitment reads as four zero words; any other getter as one.
        unset = NONE if key.startswith(commitments_call("")[:10]) else "0x" + "0" * 64
        answer = answers.get(key, unset if method == "eth_call" else None)
        if isinstance(answer, dict) and "error" in answer:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, **answer})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": answer})

    return httpx.MockTransport(handle)


class TestEncoding:
    def test_calls_are_encoded_as_the_contract_declares_them(self) -> None:
        # Selectors from `forge inspect MisthosEscrow methodIdentifiers`.
        assert set_ceiling_call("ISS-1", Usdc(5), PUBLISHER, 1).startswith("0x42fae350")
        assert approved_publisher_call("ISS-1").startswith("0xcdac873f")
        assert latest_deadline_call("ISS-1").startswith("0xdd1cb20e")
        assert fee_recipient_call() == "0x46904840"
        # The approval names the price, the publisher and the latest deadline (#122).
        assert set_ceiling_call("ISS-1", Usdc(5), PUBLISHER, 1_800_000_000) == (
            "0x42fae350"
            + issue_key("ISS-1")[2:]
            + f"{5:064x}"
            + PUBLISHER[2:].rjust(64, "0")
            + f"{1_800_000_000:064x}"
        )
        assert release_call("ISS-1", PUBLISHER, Usdc(5)).startswith("0xf5b16b84")
        assert refund_call("ISS-1").startswith("0x7249fbb6")
        assert commit_call("ISS-1", Usdc(5), 1).startswith("0x71ad6fd3")
        assert release_call("ISS-1", PUBLISHER, Usdc(5)) == (
            "0xf5b16b84" + issue_key("ISS-1")[2:] + PUBLISHER[2:].rjust(64, "0") + f"{5:064x}"
        )

    def test_every_error_the_contract_declares_has_a_name(self) -> None:
        abi = json.loads((DEPLOYMENTS_DIR / "MisthosEscrow.abi.json").read_text())
        declared = {
            f"{e['name']}({','.join(i['type'] for i in e['inputs'])})"
            for e in abi
            if e["type"] == "error"
        }
        assert declared == set(ERRORS)

    def test_a_revert_reads_as_the_contracts_error(self) -> None:
        assert revert_name(selector("NoCeiling()")) == "NoCeiling"
        data = selector("ExceedsCeiling(uint256,uint256)") + f"{150:064x}" + f"{100:064x}"
        assert revert_name(data) == "ExceedsCeiling(150, 100)"
        squat = selector("NotApprovedPublisher(address)") + "0" * 24 + "bad".rjust(40, "0")
        assert revert_name(squat) == "NotApprovedPublisher(0x" + "bad".rjust(40, "0") + ")"
        late = selector("DeadlineTooLate(uint64,uint64)") + f"{9:064x}" + f"{8:064x}"
        assert revert_name(late) == "DeadlineTooLate(9, 8)"
        assert revert_name("0xdeadbeef") == "reverted with 0xdeadbeef"
        # What anvil returned when a publisher committed more USDC than they held.
        assert revert_name(selector("Panic(uint256)") + f"{0x11:064x}") == "Panic(0x11)"
        message = b"insufficient".hex()
        error = selector("Error(string)") + f"{32:064x}" + f"{12:064x}" + message.ljust(64, "0")
        assert revert_name(error) == "insufficient"
        assert revert_name(None) == "reverted"


class TestCommit:
    """The platform never signs a commitment; it checks the publisher's against the terms
    the approval fixed (#126)."""

    def escrow(self, answers: dict) -> ArcEscrow:
        return ArcEscrow(
            "http://rpc", DEPLOYED, transport=_rpc({"eth_getCode": "0x6080", **answers})
        )

    def test_refused_until_the_publisher_has_committed(self) -> None:
        with pytest.raises(NotCommitted, match="NotCommitted"):
            self.escrow({}).commit("ISS-1", PUBLISHER, Usdc(180_000_000), DEADLINE, DEADLINE)

    def test_a_commitment_from_another_wallet_is_refused(self) -> None:
        escrow = self.escrow({commitments_call("ISS-1"): HELD_180})
        with pytest.raises(ChainRevert, match="WrongPublisher"):
            escrow.commit("ISS-1", "0x" + "1" * 40, Usdc(180_000_000), DEADLINE, DEADLINE)

    def test_a_commitment_for_another_amount_is_refused(self) -> None:
        escrow = self.escrow({commitments_call("ISS-1"): HELD_180})
        with pytest.raises(ChainRevert, match="AmountMismatch"):
            escrow.commit("ISS-1", PUBLISHER, Usdc(170_000_000), DEADLINE, DEADLINE)

    def test_a_deadline_that_cuts_the_escrow_term_is_refused(self) -> None:
        escrow = self.escrow({commitments_call("ISS-1"): HELD_180})
        later = DEADLINE + timedelta(days=2)
        with pytest.raises(ChainRevert, match="DeadlineTooEarly"):
            escrow.commit("ISS-1", PUBLISHER, Usdc(180_000_000), later, DEADLINE)

    def test_a_deadline_past_the_approved_one_is_refused(self) -> None:
        """The contract refuses it too; this is the booking's own bound (#122)."""
        escrow = self.escrow({commitments_call("ISS-1"): HELD_180})
        earlier = DEADLINE - timedelta(seconds=1)
        with pytest.raises(ChainRevert, match="DeadlineTooLate"):
            escrow.commit("ISS-1", PUBLISHER, Usdc(180_000_000), earlier, DEADLINE)

    def test_a_booking_long_after_the_approval_books_the_approved_deadline(self) -> None:
        """The booking used to compare the committed deadline with one it computed from
        its own now, so booking more than an hour after the plan refused for good with
        the money already committed (#126). It now checks the approved deadline."""
        escrow = self.escrow(
            {
                commitments_call("ISS-1"): HELD_180,
                "eth_getLogs": [{"topics": ["0x", issue_key("ISS-1")], "transactionHash": "0xC0"}],
            }
        )
        three_days_on = DEADLINE - timedelta(days=11)
        tx = escrow.commit("ISS-1", PUBLISHER, Usdc(180_000_000), DEADLINE, three_days_on)
        assert tx == "0xC0"

    def test_a_commitment_at_another_rate_is_refused(self) -> None:
        escrow = self.escrow(
            {commitments_call("ISS-1"): HELD_180, fee_call("ISS-1"): f"0x{800:064x}"}
        )
        with pytest.raises(ChainRevert, match="FeeMismatch"):
            escrow.commit("ISS-1", PUBLISHER, Usdc(180_000_000), DEADLINE, DEADLINE, 1000)

    def test_the_publishers_own_transaction_is_returned(self) -> None:
        escrow = self.escrow(
            {
                commitments_call("ISS-1"): HELD_180,
                "eth_getLogs": [{"topics": ["0x", issue_key("ISS-1")], "transactionHash": "0xC0"}],
            }
        )
        # A wallet that rounded the approved deadline down is within the tolerance; and
        # an address compares the same whatever its case.
        expected = DEADLINE + timedelta(minutes=20)
        shouting = "0x" + PUBLISHER[2:].upper()
        tx = escrow.commit("ISS-1", shouting, Usdc(180_000_000), expected, DEADLINE)
        assert tx == "0xC0"


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, to: str, data: str) -> str:
        self.sent.append((to, data))
        return "0x" + "ab" * 32


def _escrow(answers: dict | None = None, **signers: FakeSender) -> ArcEscrow:
    answers = {"eth_getCode": "0x6080", **(answers or {})}
    return ArcEscrow("http://rpc", DEPLOYED, transport=_rpc(answers), **signers)  # type: ignore[arg-type]


FEES = "0x" + "0" * 36 + "fee5"
WITH_A_RECIPIENT = {fee_recipient_call(): "0x" + FEES[2:].rjust(64, "0")}


class TestApproving:
    """The owner records what a human approved: the price, who may commit it, until
    when, and the rate it settles at (#122)."""

    def approve(self, escrow: ArcEscrow, fee_bps: int = 0) -> str:
        return escrow.set_ceiling(
            "ISS-1",
            Usdc(55),
            DEADLINE,
            publisher=PUBLISHER,
            latest_deadline=DEADLINE,
            fee_bps=fee_bps,
        )

    def test_the_approval_names_the_publisher_and_the_latest_deadline(self) -> None:
        owner = FakeSender()
        self.approve(_escrow(owner=owner))
        assert owner.sent == [
            (DEPLOYED.address, set_ceiling_call("ISS-1", Usdc(55), PUBLISHER, 1_800_000_000))
        ]

    def test_an_approval_already_in_force_is_not_sent_again(self) -> None:
        owner = FakeSender()
        held = {
            ceiling_call("ISS-1"): f"0x{55:064x}",
            approved_publisher_call("ISS-1"): "0x" + PUBLISHER[2:].rjust(64, "0"),
            latest_deadline_call("ISS-1"): f"0x{1_800_000_000:064x}",
        }
        assert self.approve(_escrow(held, owner=owner)) == ""
        assert owner.sent == []

    def test_the_rate_is_set_before_the_ceiling_lets_money_in(self) -> None:
        """A ceiling in force before the rate would let the wallet commit at a rate
        nobody approved, and the contract refuses to move it once the money is in."""
        owner = FakeSender()
        self.approve(_escrow(WITH_A_RECIPIENT, owner=owner), fee_bps=1000)
        assert [data[:10] for _, data in owner.sent] == [
            set_fee_call("ISS-1", 1000)[:10],
            set_ceiling_call("ISS-1", Usdc(55), PUBLISHER, 1)[:10],
        ]

    def test_a_rate_already_in_force_is_not_sent_again(self) -> None:
        owner = FakeSender()
        answers = {**WITH_A_RECIPIENT, fee_call("ISS-1"): f"0x{1000:064x}"}
        self.approve(_escrow(answers, owner=owner), fee_bps=1000)
        assert [data[:10] for _, data in owner.sent] == [
            set_ceiling_call("ISS-1", Usdc(55), PUBLISHER, 1)[:10]
        ]

    def test_an_escrow_with_no_fee_recipient_is_refused_before_anything_is_sent(
        self,
    ) -> None:
        """Every release at a rate would revert FeeRecipientNotSet, after the publisher's
        money was in. Refused at the approval instead, before the wallet commits (#127)."""
        owner = FakeSender()
        with pytest.raises(ChainRevert, match="FeeRecipientNotSet"):
            self.approve(_escrow(owner=owner), fee_bps=1200)
        assert owner.sent == []

    def test_an_operator_hears_of_a_missing_fee_recipient(self) -> None:
        assert _escrow().settlement_problems() == [NO_FEE_RECIPIENT]
        assert _escrow(WITH_A_RECIPIENT).settlement_problems() == []

    def test_without_an_owner_key_no_price_can_be_recorded(self) -> None:
        with pytest.raises(ChainUnavailable, match="no owner key"):
            self.approve(_escrow())


class TestSignedCalls:
    def test_release_is_signed_by_the_attestor(self) -> None:
        attestor = FakeSender()
        _escrow(attestor=attestor).release("ISS-1", PUBLISHER, Usdc(5), DEADLINE)
        assert attestor.sent == [(DEPLOYED.address, release_call("ISS-1", PUBLISHER, Usdc(5)))]

    def test_refund_is_signed_by_the_attestor(self) -> None:
        attestor = FakeSender()
        _escrow(attestor=attestor).refund("ISS-1", DEADLINE)
        assert attestor.sent == [(DEPLOYED.address, refund_call("ISS-1"))]


KEY = "0x" + bytes(Account.create().key).hex()


class TestSender:
    def answers(self, **overrides: object) -> dict:
        return {
            "eth_estimateGas": "0x5208",
            "eth_getTransactionCount": "0x3",
            "eth_maxPriorityFeePerGas": "0x1",
            "eth_gasPrice": "0x10",
            "eth_sendRawTransaction": "0xfeed",
            "eth_getTransactionReceipt": {"status": "0x1"},
            **overrides,
        }

    def test_a_refused_call_is_raised_as_the_contracts_error_before_signing(self) -> None:
        seen: list[dict] = []
        refused = {"error": {"code": 3, "message": "reverted", "data": selector("NoCeiling()")}}
        rpc = JsonRpc("http://rpc", transport=_rpc(self.answers(eth_estimateGas=refused), seen))
        with pytest.raises(ChainRevert, match="NoCeiling"):
            Sender(rpc, 5042002, lambda: KEY).send("0x" + "e5" * 20, "0x00")
        assert "eth_sendRawTransaction" not in [b["method"] for b in seen]

    def test_signs_with_the_loaded_key_and_waits_for_the_receipt(self) -> None:
        seen: list[dict] = []
        rpc = JsonRpc("http://rpc", transport=_rpc(self.answers(), seen))
        assert Sender(rpc, 5042002, lambda: KEY).send("0x" + "e5" * 20, "0x1234") == "0xfeed"

        raw = next(b for b in seen if b["method"] == "eth_sendRawTransaction")["params"][0]
        assert Account.recover_transaction(raw) == Account.from_key(KEY).address

    def test_a_transaction_that_reverts_on_chain_is_raised(self) -> None:
        reverted = self.answers(eth_getTransactionReceipt={"status": "0x0"})
        rpc = JsonRpc("http://rpc", transport=_rpc(reverted))
        with pytest.raises(ChainRevert, match="reverted on chain"):
            Sender(rpc, 5042002, lambda: KEY).send("0x" + "e5" * 20, "0x1234")

    def test_a_missing_key_fails_before_anything_is_sent(self) -> None:
        seen: list[dict] = []
        rpc = JsonRpc("http://rpc", transport=_rpc(self.answers(), seen))

        def no_key() -> str:
            raise RuntimeError("the secret store holds nothing at attestor-1")

        with pytest.raises(RuntimeError, match="holds nothing"):
            Sender(rpc, 5042002, no_key).send("0x" + "e5" * 20, "0x1234")
        assert seen == []


class TestStartup:
    async def test_the_api_reports_an_escrow_with_no_fee_recipient_when_it_starts(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An operator hears of it before a publisher commits money, not at the first
        release (#127)."""
        from misthos import main

        monkeypatch.setattr(main.settings, "simulated", False)
        monkeypatch.setattr(main.store, "chain", _escrow())
        with caplog.at_level(logging.ERROR, logger="misthos.main"):
            problems = await main.report_settlement_problems()

        assert problems == [NO_FEE_RECIPIENT]
        assert NO_FEE_RECIPIENT in caplog.text

    async def test_the_simulation_has_nothing_to_report(self) -> None:
        from misthos import main

        assert await main.report_settlement_problems() == []
