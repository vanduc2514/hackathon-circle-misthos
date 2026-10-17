"""Settling against MisthosEscrow: what the platform signs, and what it only checks."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from eth_account import Account

from misthos.domain.escrow import (
    COMMITTED_TOPIC,
    ERRORS,
    ceiling_call,
    commit_call,
    commitments_call,
    decode_address,
    fee_call,
    fee_recipient_call,
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
from misthos.services.chain.arc import NO_FEE_RECIPIENT
from misthos.services.chain.deployment import DEPLOYMENTS_DIR
from misthos.services.chain.rpc import JsonRpc, Sender

DEPLOYED = EscrowDeployment("0x" + "e5" * 20, 5042002, "record", deployed_at_block=7)
PUBLISHER = "0x" + "0" * 36 + "0b0b"
FEES = "0x" + "fe" * 20
DEADLINE = datetime.fromtimestamp(1_800_000_000, UTC)
# commitments(bytes32) for a 180 USDC commitment by 0x...0B0B, held until DEADLINE.
HELD_180 = (
    "0x" + f"{0xB0B:064x}" + f"{180_000_000:064x}" + f"{1_800_000_000:064x}" + f"{1:064x}"
)
def _held_until(deadline: datetime) -> str:
    """What `commitments(bytes32)` returns for a 180 USDC commitment by 0x...0B0B held
    until `deadline`. The same shape as HELD_180, with the moment moved."""
    return (
        "0x"
        + f"{0xB0B:064x}"
        + f"{180_000_000:064x}"
        + f"{int(deadline.timestamp()):064x}"
        + f"{1:064x}"
    )


NONE = "0x" + "0" * 256
WITH_A_RECIPIENT = {fee_recipient_call(): "0x" + FEES[2:].rjust(64, "0")}


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
        assert fee_recipient_call() == "0x46904840"

    def test_an_address_word_reads_as_an_address_and_zero_as_nobody(self) -> None:
        assert decode_address("0x" + FEES[2:].rjust(64, "0")) == FEES
        assert decode_address("0x" + "0" * 64) is None

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

    def test_a_deadline_worked_out_after_the_approval_is_refused(self) -> None:
        """What `now + 14d` becomes an hour after the approval (#126).

        The escrow holds the deadline the approval fixed, so a commitment checked against
        a deadline recomputed later is refused `DeadlineTooEarly` -- and keeps being
        refused, with the USDC already in the escrow. The approved deadline itself books,
        which is what the fix makes the plan hand the wallet.
        """
        escrow = self.escrow({commitments_call("ISS-1"): HELD_180, **self.log_answers()})
        recomputed = DEADLINE + timedelta(hours=1, minutes=1)
        with pytest.raises(ChainRevert, match="DeadlineTooEarly"):
            escrow.commit("ISS-1", PUBLISHER, Usdc(180_000_000), recomputed, DEADLINE)
        # And the approved one is bookable, an hour after the approval: the check is
        # against the terms, not against the clock.
        assert escrow.commit("ISS-1", PUBLISHER, Usdc(180_000_000), DEADLINE, DEADLINE)

    def test_a_commitment_beyond_the_approved_deadline_is_refused(self) -> None:
        """The other direction, and why the check is bounded on both sides: a commitment
        holding a far-future deadline would keep the platform's own refund off, since a
        refund is refused until the escrow's deadline passes (#126)."""
        far = DEADLINE + timedelta(days=365)
        escrow = self.escrow({commitments_call("ISS-1"): _held_until(far)})
        with pytest.raises(ChainRevert, match="DeadlineTooLate"):
            escrow.commit("ISS-1", PUBLISHER, Usdc(180_000_000), DEADLINE, DEADLINE)

    def log_answers(self) -> dict:
        return {
            "eth_blockNumber": hex(999),
            "eth_getLogs": [{"topics": ["0x", issue_key("ISS-1")], "transactionHash": "0xC0"}],
        }

    def test_the_take_rate_is_fixed_on_chain_before_the_publisher_commits(self) -> None:
        # The contract refuses to move the rate once the money is in, so the owner
        # sets it on the first approval, while the escrow still waits for the wallet.
        # Named on an escrow that has a recipient: a rate with nowhere to pay it is
        # refused before this is sent (#127).
        owner = FakeSender()
        escrow = _escrow_with(WITH_A_RECIPIENT, owner=owner)
        with pytest.raises(ChainRevert, match="NotCommitted"):
            escrow.commit("ISS-1", PUBLISHER, Usdc(180_000_000), DEADLINE, DEADLINE, 1000)
        assert owner.sent == [(DEPLOYED.address, set_fee_call("ISS-1", 1000))]

    def test_a_rate_already_in_force_is_not_sent_again(self) -> None:
        owner = FakeSender()
        escrow = _escrow_with(
            {**WITH_A_RECIPIENT, fee_call("ISS-1"): f"0x{1000:064x}"}, owner=owner
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
                "eth_blockNumber": hex(9),
                "eth_getLogs": [{"topics": ["0x", issue_key("ISS-1")], "transactionHash": "0xC0"}],
            }
        )
        # The wallet sent it minutes after approval, which is within the tolerance;
        # and an address compares the same whatever its case.
        expected = DEADLINE + timedelta(minutes=20)
        shouting = "0x" + PUBLISHER[2:].upper()
        tx = escrow.commit("ISS-1", shouting, Usdc(180_000_000), expected, DEADLINE)
        assert tx == "0xC0"

    def test_the_log_is_asked_for_by_issue_and_publisher_not_filtered_in_python(self) -> None:
        """Both are indexed topics, so the node does the narrowing. It matters when a
        wallet commits twice: the earlier commitment is a different publisher, and the
        transaction returned has to be this one's (#154)."""
        seen: list[dict] = []
        escrow = ArcEscrow(
            "http://rpc",
            DEPLOYED,
            transport=_rpc(
                {
                    "eth_getCode": "0x6080",
                    commitments_call("ISS-1"): HELD_180,
                    "eth_blockNumber": hex(9),
                    "eth_getLogs": [
                        {"topics": ["0x", issue_key("ISS-1")], "transactionHash": "0xC0"}
                    ],
                },
                seen,
            ),
        )
        expected = DEADLINE + timedelta(minutes=20)
        escrow.commit("ISS-1", PUBLISHER, Usdc(180_000_000), expected, DEADLINE)

        asked = next(b for b in seen if b["method"] == "eth_getLogs")["params"][0]
        assert asked["topics"] == [
            COMMITTED_TOPIC,
            issue_key("ISS-1"),
            # A topic is a full word, so an address is padded to the right.
            "0x" + PUBLISHER[2:].lower().rjust(64, "0"),
        ]

    def test_the_commitment_is_looked_for_in_the_recent_blocks_first(self) -> None:
        """The common case: the wallet committed moments ago, so the log is in the last
        few hours. Walking from the deployment instead spends sixty-five requests on a
        node that allows about sixty a minute, which is how a booking was lost (#154)."""
        seen: list[dict] = []
        escrow = ArcEscrow(
            "http://rpc",
            # A deployment far in the past: the range is what makes a full walk expensive.
            EscrowDeployment("0x" + "e5" * 20, 5042002, "record", deployed_at_block=1),
            transport=_rpc(
                {
                    "eth_getCode": "0x6080",
                    commitments_call("ISS-1"): HELD_180,
                    "eth_blockNumber": hex(1_000_000),
                    "eth_getLogs": [
                        {"topics": ["0x", issue_key("ISS-1")], "transactionHash": "0xC0"}
                    ],
                },
                seen,
            ),
        )
        expected = DEADLINE + timedelta(minutes=20)
        assert escrow.commit("ISS-1", PUBLISHER, Usdc(180_000_000), expected, DEADLINE) == "0xC0"

        asked = [b for b in seen if b["method"] == "eth_getLogs"]
        # Four windows of the last 20,000 blocks, not two hundred of the whole range.
        assert len(asked) == 4
        first_window = 1_000_000 - ArcEscrow.LOG_RECENT_BLOCKS + 1
        assert asked[0]["params"][0]["fromBlock"] == hex(first_window)
        assert asked[-1]["params"][0]["toBlock"] == hex(1_000_000)

    def test_a_commitment_older_than_the_recent_window_is_still_found(self) -> None:
        """Widening is the fallback, not the first move, but it is still there for a
        commitment the recent window missed."""
        seen: list[dict] = []

        def handle(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            seen.append(body)
            if body["method"] == "eth_call":
                key = body["params"][0]["data"]
                if key == commitments_call("ISS-1"):
                    result: object = HELD_180
                elif key.startswith(commitments_call("")[:10]):
                    result = NONE
                else:
                    result = "0x" + "0" * 64
            elif body["method"] == "eth_blockNumber":
                result = hex(100)
            elif body["method"] == "eth_getLogs":
                window = body["params"][0]
                # Only the window that reaches back past the deployment has it.
                inside = int(window["fromBlock"], 16) <= 7 <= int(window["toBlock"], 16)
                result = (
                    [{"topics": ["0x", issue_key("ISS-1")], "transactionHash": "0xC0"}]
                    if inside
                    else []
                )
            else:
                result = "0x6080" if body["method"] == "eth_getCode" else None
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

        escrow = ArcEscrow("http://rpc", DEPLOYED, transport=httpx.MockTransport(handle))
        expected = DEADLINE + timedelta(minutes=20)
        assert escrow.commit("ISS-1", PUBLISHER, Usdc(180_000_000), expected, DEADLINE) == "0xC0"

        windows = [b["params"][0] for b in seen if b["method"] == "eth_getLogs"]
        # The recent walk first, then the whole range: both start at the deployment here,
        # because there is less history than one recent window.
        assert windows[0]["fromBlock"] == hex(7)

    def test_a_scan_of_one_window_does_not_pause(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A fresh deployment has one window of history, and the common case must not
        pay for a pause it will not use."""
        clock = _FakeClock.installed(monkeypatch)
        escrow = self.escrow(
            {
                commitments_call("ISS-1"): HELD_180,
                "eth_blockNumber": hex(9),
                "eth_getLogs": [{"topics": ["0x", issue_key("ISS-1")], "transactionHash": "0xC0"}],
            }
        )
        expected = DEADLINE + timedelta(minutes=20)
        escrow.commit("ISS-1", PUBLISHER, Usdc(180_000_000), expected, DEADLINE)
        assert clock.slept == []

    def test_a_page_the_node_throttles_is_waited_out_and_retried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A walk of the whole history needs more pages than the node allows in a minute,
        so at this chain's length it cannot finish on pace alone. What makes it complete
        is retrying the page that was refused, waiting long enough for the budget to roll
        (#161). Without this, reconciliation can never read a commitment that is not
        recent -- which is exactly the orphan it exists to adopt.
        """
        clock = _FakeClock.installed(monkeypatch)
        # More refusals than `JsonRpc` retries on its own: a page-rate refusal is not a
        # transient blip, so it outlasts the short backoff and is left to the paging
        # loop, which waits long enough for the minute to roll.
        refusals = {"left": 4}

        def handle(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            method = body["method"]
            if method == "eth_getLogs":
                if refusals["left"] > 0:
                    refusals["left"] -= 1
                    return httpx.Response(429, headers={"retry-after": "0"})
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": [
                            {"topics": ["0x", issue_key("ISS-1")], "transactionHash": "0xC0"}
                        ],
                    },
                )
            key = body["params"][0].get("data") if method == "eth_call" else None
            if method == "eth_call":
                result: object = HELD_180 if key == commitments_call("ISS-1") else NONE
            elif method == "eth_blockNumber":
                result = hex(9)
            elif method == "eth_getCode":
                result = "0x6080"
            else:
                result = None
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

        escrow = ArcEscrow("http://rpc", DEPLOYED, transport=httpx.MockTransport(handle))
        expected = DEADLINE + timedelta(minutes=20)
        assert escrow.commit("ISS-1", PUBLISHER, Usdc(180_000_000), expected, DEADLINE) == "0xC0"
        # One wait, and it was the throttle pause rather than a burst pause.
        assert clock.slept == [ArcEscrow.LOG_THROTTLE_SECONDS]


class _FakeClock:
    """Stands in for the module's `time`, so a test can see the pacing without waiting."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    @classmethod
    def installed(cls, monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
        import misthos.services.chain.arc as arc_module

        clock = cls()
        monkeypatch.setattr(arc_module, "time", clock)
        return clock


class TestThrottling:
    """A refusal for being too fast is waited out; a refusal for being wrong is not."""

    def test_a_throttled_read_is_retried_and_then_succeeds(self) -> None:
        attempts: list[int] = []

        def handle(_request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) == 1:
                # `retry-after: 0` also proves the header is honoured instead of the
                # backoff, which is what keeps this test from waiting.
                return httpx.Response(429, headers={"retry-after": "0"})
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x1"})

        rpc = JsonRpc("http://rpc", transport=httpx.MockTransport(handle))
        assert rpc("eth_blockNumber", []) == "0x1"
        assert len(attempts) == 2

    def test_a_throttle_that_does_not_clear_is_raised_with_its_reason(self) -> None:
        def handle(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"retry-after": "0"})

        rpc = JsonRpc("http://rpc", transport=httpx.MockTransport(handle))
        with pytest.raises(ChainUnavailable, match="429"):
            rpc("eth_blockNumber", [])

    def test_a_call_the_node_refuses_on_its_merits_is_not_retried(self) -> None:
        attempts: list[int] = []

        def handle(_request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(400, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32602}})

        rpc = JsonRpc("http://rpc", transport=httpx.MockTransport(handle))
        with pytest.raises(ChainUnavailable, match="400"):
            rpc("eth_getLogs", [{}])
        assert len(attempts) == 1


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, to: str, data: str) -> str:
        self.sent.append((to, data))
        return "0x" + "ab" * 32


def _escrow(**signers: FakeSender) -> ArcEscrow:
    answers = {"eth_getCode": "0x6080"}
    return ArcEscrow("http://rpc", DEPLOYED, transport=_rpc(answers), **signers)  # type: ignore[arg-type]


def _escrow_with(answers: dict, **signers: FakeSender) -> ArcEscrow:
    """An escrow whose RPC answers what the test names, plus code at its address."""
    return ArcEscrow(
        "http://rpc",
        DEPLOYED,
        transport=_rpc({"eth_getCode": "0x6080", **answers}),
        **signers,  # type: ignore[arg-type]
    )


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


class TestTheFeeRecipient:
    """How an operator, and a publisher, find out the escrow cannot pay a take rate out.

    The platform sets a take rate on every issue, so an escrow deployed without a
    recipient would take commitments whose every release reverts FeeRecipientNotSet
    (#127).
    """

    def test_an_operator_hears_of_a_missing_fee_recipient(self) -> None:
        assert _escrow().settlement_problems() == [NO_FEE_RECIPIENT]
        assert _escrow_with(WITH_A_RECIPIENT).settlement_problems() == []

    def test_an_unreachable_chain_is_reported_rather_than_raised(self) -> None:
        rpc = ArcEscrow(
            "http://rpc",
            DEPLOYED,
            transport=_rpc({"eth_getCode": "0x", "eth_chainId": hex(5042002)}),
        )
        problems = rpc.settlement_problems()
        assert len(problems) == 1 and "0x" in problems[0]

    def test_the_commitment_is_refused_before_the_rate_goes_on(self) -> None:
        owner = FakeSender()
        escrow = _escrow_with({}, owner=owner)
        with pytest.raises(ChainRevert, match="FeeRecipientNotSet"):
            escrow.commit("ISS-1", PUBLISHER, Usdc(180), DEADLINE, DEADLINE, 1000)
        # Nothing was signed: the setFee that would have made every release revert was
        # never sent, and no money was asked for.
        assert owner.sent == []

    def test_a_named_recipient_lets_the_rate_go_on(self) -> None:
        owner = FakeSender()
        escrow = _escrow_with(WITH_A_RECIPIENT, owner=owner)
        with pytest.raises(ChainRevert, match="NotCommitted"):
            escrow.commit("ISS-1", PUBLISHER, Usdc(180), DEADLINE, DEADLINE, 1000)
        assert owner.sent == [(DEPLOYED.address, set_fee_call("ISS-1", 1000))]

    def test_a_commitment_with_no_rate_goes_on_without_a_recipient(self) -> None:
        # Nothing to pay out means nothing to be missing: a zero-bps commitment is not
        # refused, so an escrow used without a take rate still works.
        owner = FakeSender()
        escrow = _escrow_with({}, owner=owner)
        with pytest.raises(ChainRevert, match="NotCommitted"):
            escrow.commit("ISS-1", PUBLISHER, Usdc(180), DEADLINE, DEADLINE, 0)
        assert owner.sent == []


class TestTheStartupReport:
    async def test_the_api_reports_an_escrow_with_no_fee_recipient_when_it_starts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from misthos import main

        monkeypatch.setattr(main.store.current(), "chain", _escrow())
        assert await main.report_settlement_problems() == [NO_FEE_RECIPIENT]

        monkeypatch.setattr(main.store.current(), "chain", _escrow_with(WITH_A_RECIPIENT))
        assert await main.report_settlement_problems() == []

