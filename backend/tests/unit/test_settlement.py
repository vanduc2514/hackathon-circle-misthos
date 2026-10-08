"""Settling against MisthosEscrow: what the platform signs, and what it only checks."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from eth_account import Account

from misthos.domain.escrow import (
    ERRORS,
    ceiling_call,
    commit_call,
    commitments_call,
    fee_call,
    issue_key,
    refund_call,
    release_call,
    revert_name,
    selector,
    set_ceiling_call,
    set_fee_call,
)
from misthos.domain.money import Usdc
from misthos.services.chain import ArcEscrow, ChainRevert, ChainUnavailable, EscrowDeployment
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
        assert set_ceiling_call("ISS-1", Usdc(5)).startswith("0x3c48db7a")
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
        assert revert_name("0xdeadbeef") == "reverted with 0xdeadbeef"
        # What anvil returned when a publisher committed more USDC than they held.
        assert revert_name(selector("Panic(uint256)") + f"{0x11:064x}") == "Panic(0x11)"
        message = b"insufficient".hex()
        error = selector("Error(string)") + f"{32:064x}" + f"{12:064x}" + message.ljust(64, "0")
        assert revert_name(error) == "insufficient"
        assert revert_name(None) == "reverted"


class TestCommit:
    """The platform never signs a commitment; it checks the publisher's."""

    def escrow(self, answers: dict) -> ArcEscrow:
        return ArcEscrow(
            "http://rpc", DEPLOYED, transport=_rpc({"eth_getCode": "0x6080", **answers})
        )

    def test_refused_until_the_publisher_has_committed(self) -> None:
        with pytest.raises(ChainRevert, match="NotCommitted"):
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

    def test_the_take_rate_is_fixed_on_chain_before_the_publisher_commits(self) -> None:
        # The contract refuses to move the rate once the money is in, so the owner
        # sets it on the first approval, while the escrow still waits for the wallet.
        owner = FakeSender()
        escrow = ArcEscrow(
            "http://rpc",
            DEPLOYED,
            owner=owner,  # type: ignore[arg-type]
            transport=_rpc({"eth_getCode": "0x6080"}),
        )
        with pytest.raises(ChainRevert, match="NotCommitted"):
            escrow.commit("ISS-1", PUBLISHER, Usdc(180_000_000), DEADLINE, DEADLINE, 1000)
        assert owner.sent == [(DEPLOYED.address, set_fee_call("ISS-1", 1000))]

    def test_a_rate_already_in_force_is_not_sent_again(self) -> None:
        owner = FakeSender()
        escrow = ArcEscrow(
            "http://rpc",
            DEPLOYED,
            owner=owner,  # type: ignore[arg-type]
            transport=_rpc({"eth_getCode": "0x6080", fee_call("ISS-1"): f"0x{1000:064x}"}),
        )
        with pytest.raises(ChainRevert, match="NotCommitted"):
            escrow.commit("ISS-1", PUBLISHER, Usdc(180_000_000), DEADLINE, DEADLINE, 1000)
        assert owner.sent == []

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
        # The wallet sent it minutes after approval, which is within the tolerance;
        # and an address compares the same whatever its case.
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


def _escrow(**signers: FakeSender) -> ArcEscrow:
    answers = {"eth_getCode": "0x6080"}
    return ArcEscrow("http://rpc", DEPLOYED, transport=_rpc(answers), **signers)  # type: ignore[arg-type]


class TestSignedCalls:
    def test_release_is_signed_by_the_attestor(self) -> None:
        attestor = FakeSender()
        _escrow(attestor=attestor).release("ISS-1", PUBLISHER, Usdc(5), DEADLINE)
        assert attestor.sent == [(DEPLOYED.address, release_call("ISS-1", PUBLISHER, Usdc(5)))]

    def test_refund_is_signed_by_the_attestor(self) -> None:
        attestor = FakeSender()
        _escrow(attestor=attestor).refund("ISS-1", DEADLINE)
        assert attestor.sent == [(DEPLOYED.address, refund_call("ISS-1"))]

    def test_a_ceiling_is_recorded_by_the_owner_once(self) -> None:
        owner = FakeSender()
        _escrow(owner=owner).set_ceiling("ISS-1", Usdc(55), DEADLINE)
        assert owner.sent == [(DEPLOYED.address, set_ceiling_call("ISS-1", Usdc(55)))]

        already = ArcEscrow(
            "http://rpc",
            DEPLOYED,
            owner=owner,  # type: ignore[arg-type]
            transport=_rpc({"eth_getCode": "0x6080", ceiling_call("ISS-1"): f"0x{55:064x}"}),
        )
        assert already.set_ceiling("ISS-1", Usdc(55), DEADLINE) == ""
        assert len(owner.sent) == 1

    def test_without_an_owner_key_no_price_can_be_recorded(self) -> None:
        with pytest.raises(ChainUnavailable, match="no owner key"):
            _escrow().set_ceiling("ISS-1", Usdc(55), DEADLINE)


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
