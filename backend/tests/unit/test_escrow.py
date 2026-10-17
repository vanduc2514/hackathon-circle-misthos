"""Reading MisthosEscrow back, and finding where it was deployed."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from misthos.config import Settings
from misthos.domain.escrow import (
    COMMITTED_TOPIC,
    ceiling_call,
    commitments_call,
    decode_ceiling,
    decode_commitment,
    issue_key,
)
from misthos.domain.ledger import EscrowStatus
from misthos.domain.money import Usdc
from misthos.services.chain import (
    ArcEscrow,
    ChainRevert,
    ChainUnavailable,
    EscrowDeployment,
    EscrowNotDeployed,
    load_deployment,
)
from misthos.services.chain.deployment import DEPLOYMENTS_DIR
from tests.support.fake_chain import FakeChain

# `cast abi-encode "f(address,uint96,uint64,uint8)" 0x...0B0B 180000000 1800000000 1`
HELD_180 = (
    "0x"
    "0000000000000000000000000000000000000000000000000000000000000b0b"
    "000000000000000000000000000000000000000000000000000000000aba9500"
    "000000000000000000000000000000000000000000000000000000006b49d200"
    "0000000000000000000000000000000000000000000000000000000000000001"
)
NEVER_COMMITTED = "0x" + "0" * 256


class TestEncoding:
    def test_the_issue_key_is_the_keccak_the_contract_tests_use(self) -> None:
        # `cast keccak ISS-1001`
        assert issue_key("ISS-1001") == (
            "0x0f55bcbdf7e7e2bb93126334d2f9f5c35484309a5bc7daa92b17b125bb21d4ff"
        )

    def test_the_call_targets_the_commitments_getter(self) -> None:
        # `forge inspect MisthosEscrow methodIdentifiers` lists commitments as 839df945.
        data = commitments_call("ISS-1001")
        assert data.startswith("0x839df945")
        assert data.endswith(issue_key("ISS-1001")[2:])

    def test_the_log_scan_filters_on_the_committed_event(self) -> None:
        # `cast sig-event "Committed(bytes32,address,uint256,uint64)"`
        assert COMMITTED_TOPIC == (
            "0x23bf5c5663115c03f1a8794e5cd8dacbd42538692d2d3e56723138be92050aae"
        )

    def test_a_held_commitment_decodes_in_six_decimal_usdc(self) -> None:
        c = decode_commitment(HELD_180)
        assert c is not None
        assert c.status is EscrowStatus.HELD
        assert c.amount == Usdc.from_decimal("180")
        assert c.publisher == "0x" + "0" * 36 + "0b0b"
        assert c.deadline == datetime.fromtimestamp(1_800_000_000, UTC)

    def test_an_issue_never_committed_decodes_as_nothing(self) -> None:
        assert decode_commitment(NEVER_COMMITTED) is None

    def test_a_short_answer_is_refused_rather_than_padded(self) -> None:
        with pytest.raises(ValueError):
            decode_commitment(HELD_180[:-64])

    def test_a_status_the_contract_cannot_hold_is_refused(self) -> None:
        with pytest.raises(ValueError):
            decode_commitment(HELD_180[:-1] + "9")


def _record(tmp_path: Path, chain_id: int) -> Path:
    path = tmp_path / f"{chain_id}.json"
    path.write_text(json.dumps({"chainId": chain_id, "escrow": "0xE5C", "deployedAtBlock": 42}))
    return path


class TestDeployment:
    def test_the_deploy_record_supplies_the_address(self, tmp_path: Path) -> None:
        cfg = Settings(
            simulated=False,
            chain_id=5042002,
            escrow_deployment_file=str(_record(tmp_path, 5042002)),
        )
        d = load_deployment(cfg)
        assert (d.address, d.source, d.deployed_at_block) == ("0xE5C", "record", 42)

    def test_an_explicit_address_overrides_the_record(self, tmp_path: Path) -> None:
        cfg = Settings(
            escrow_contract="0xABC",
            escrow_deployment_file=str(_record(tmp_path, 5042002)),
        )
        assert load_deployment(cfg).source == "override"

    def test_a_record_for_another_chain_is_refused(self, tmp_path: Path) -> None:
        cfg = Settings(chain_id=5042, escrow_deployment_file=str(_record(tmp_path, 5042002)))
        with pytest.raises(EscrowNotDeployed):
            load_deployment(cfg)

    def test_real_settlement_without_a_deployment_is_refused(self, tmp_path: Path) -> None:
        cfg = Settings(escrow_deployment_file=str(tmp_path / "none.json"))
        with pytest.raises(EscrowNotDeployed):
            load_deployment(cfg)

    def test_the_default_record_lives_with_the_contracts(self) -> None:
        assert DEPLOYMENTS_DIR.parts[-2:] == ("contracts", "deployments")
        assert (DEPLOYMENTS_DIR / "MisthosEscrow.abi.json").is_file()


DEPLOYED = EscrowDeployment("0xE5C", 5042002, "record", deployed_at_block=42)


def _rpc(answers: dict[str, object], seen: list[dict] | None = None) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if seen is not None:
            seen.append(body)
        method = body["method"]
        if method == "eth_call":
            data = body["params"][0]["data"]
            result = answers.get(data, NEVER_COMMITTED)
        else:
            result = answers[method]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    return httpx.MockTransport(handle)


class TestArcEscrow:
    def test_reads_a_commitment_with_eth_call(self) -> None:
        seen: list[dict] = []
        transport = _rpc({"eth_getCode": "0x6080", commitments_call("ISS-1001"): HELD_180}, seen)
        c = ArcEscrow("http://rpc", DEPLOYED, transport=transport).commitment("ISS-1001")
        assert c is not None and c.status is EscrowStatus.HELD
        calls = [b["params"][0] for b in seen if b["method"] == "eth_call"]
        assert {"to": "0xE5C", "data": commitments_call("ISS-1001")} in calls

    def test_a_record_of_a_deploy_that_never_landed_is_refused(self) -> None:
        # The address in the record holds no code: nothing was ever deployed there.
        transport = _rpc({"eth_getCode": "0x"})
        with pytest.raises(ChainUnavailable, match="holds no code"):
            ArcEscrow("http://rpc", DEPLOYED, transport=transport).commitment("ISS-1001")

    def test_an_rpc_error_is_raised_not_read_as_empty(self) -> None:
        def answer(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -1}})

        escrow = ArcEscrow("http://rpc", DEPLOYED, transport=httpx.MockTransport(answer))
        with pytest.raises(ChainUnavailable):
            escrow.commitment("ISS-1")

    def test_commitments_match_logs_to_known_issues_and_surface_strangers(self) -> None:
        stranger = "0x" + "ab" * 32
        logs = [
            {"topics": [COMMITTED_TOPIC, issue_key("ISS-1001")]},
            {"topics": [COMMITTED_TOPIC, stranger]},
        ]
        answers = {
            "eth_getCode": "0x6080",
            "eth_blockNumber": hex(100),
            "eth_getLogs": logs,
            commitments_call("ISS-1001"): HELD_180,
            "0x839df945" + stranger[2:]: HELD_180,
        }
        escrow = ArcEscrow(
            "http://rpc", DEPLOYED, issue_ids=lambda: ["ISS-1001"], transport=_rpc(answers)
        )
        found = escrow.commitments()
        assert set(found) == {"ISS-1001", stranger}

    def test_a_known_issue_is_read_with_one_call_and_not_from_the_log(self) -> None:
        """Sixty-five log windows a minute is the whole of what the node allows, so
        reconciliation must not pay it for issues it can simply ask about. The read is
        `commitment(issue_id)` -- `eth_call`, which is not the rate-limited method."""
        seen: list[dict] = []
        answers = {
            "eth_getCode": "0x6080",
            "eth_blockNumber": hex(100),
            "eth_getLogs": [],
            commitments_call("ISS-1001"): HELD_180,
        }
        escrow = ArcEscrow(
            "http://rpc",
            DEPLOYED,
            issue_ids=lambda: ["ISS-1001"],
            transport=_rpc(answers, seen),
        )
        assert set(escrow.commitments()) == {"ISS-1001"}

        calls = [b["params"][0]["data"] for b in seen if b["method"] == "eth_call"]
        assert commitments_call("ISS-1001") in calls

    def test_the_log_is_only_walked_for_keys_nobody_knows(self) -> None:
        """And even then only recently: a stranger's commitment is still found, but the
        walk is bounded rather than the whole history."""
        stranger = "0x" + "cd" * 32
        seen: list[dict] = []
        answers = {
            "eth_getCode": "0x6080",
            "eth_blockNumber": hex(1_000_000),
            "eth_getLogs": [{"topics": [COMMITTED_TOPIC, stranger]}],
            "0x839df945" + stranger[2:]: HELD_180,
        }
        escrow = ArcEscrow(
            "http://rpc",
            EscrowDeployment("0xE5C", 5042002, "record", deployed_at_block=1),
            issue_ids=lambda: ["ISS-1001"],
            transport=_rpc(answers, seen),
        )
        assert set(escrow.commitments()) == {stranger}

        windows = [b["params"][0] for b in seen if b["method"] == "eth_getLogs"]
        first_window = 1_000_000 - ArcEscrow.LOG_RECENT_BLOCKS + 1
        assert windows[0]["fromBlock"] == hex(first_window)

    def test_a_log_scan_is_walked_in_windows_the_node_accepts(self) -> None:
        """Arc refuses an `eth_getLogs` wider than 100,000 blocks.

        Asking once from the deploy block to the head worked only while the chain was
        young; past the cap the RPC refused it, which failed the whole reconcile and
        with it every sweep, so no claim expired and no refund was ever made.
        """
        first = 1_000_000
        head = first + 3 * ArcEscrow.LOG_RANGE_BLOCKS + 7
        seen: list[dict] = []
        transport = _rpc(
            {"eth_getCode": "0x6080", "eth_getLogs": [], "eth_blockNumber": hex(head)}, seen
        )
        deployed = EscrowDeployment("0xE5C", 5042002, "record", deployed_at_block=first)

        ArcEscrow("http://rpc", deployed, transport=transport).commitments()

        ranges = [b["params"][0] for b in seen if b["method"] == "eth_getLogs"]
        assert len(ranges) == 4  # the range tiled exactly, so no window was widened
        # Under the cap Arc enforces. Measured on Arc testnet: 9,999 blocks accepted,
        # 10,000 refused with "requested range too large". Nothing here may exceed it,
        # because a refused window fails the whole reconcile and with it every sweep.
        assert ArcEscrow.LOG_RANGE_BLOCKS <= 9_999
        for window in ranges:
            width = int(window["toBlock"], 16) - int(window["fromBlock"], 16) + 1
            assert width <= ArcEscrow.LOG_RANGE_BLOCKS
        # The windows tile the range exactly: no gap to miss a commitment in, no
        # overlap to read one twice.
        assert int(ranges[0]["fromBlock"], 16) == first
        assert int(ranges[-1]["toBlock"], 16) == head
        for before, after in zip(ranges, ranges[1:], strict=False):
            assert int(after["fromBlock"], 16) == int(before["toBlock"], 16) + 1

    def test_nothing_is_signed_without_a_configured_key(self) -> None:
        escrow = ArcEscrow("http://rpc", DEPLOYED, transport=_rpc({}))
        with pytest.raises(ChainUnavailable, match="no attestor key"):
            escrow.refund("ISS-1", datetime.now(UTC))


class TestEscrowCeiling:
    def test_the_ceiling_call_targets_the_ceiling_getter(self) -> None:
        # `cast sig "ceiling(bytes32)"`
        assert ceiling_call("ISS-1001").startswith("0x5230bbf7")

    def test_zero_decodes_as_no_ceiling(self) -> None:
        assert decode_ceiling("0x" + "0" * 64) is None
        assert decode_ceiling("0x" + f"{180_000_000:064x}") == Usdc.from_decimal("180")

    def test_arc_reads_the_ceiling_from_the_contract(self) -> None:
        answers = {
            "eth_getCode": "0x6080",
            ceiling_call("ISS-1001"): "0x" + f"{55_000_000:064x}",
        }
        escrow = ArcEscrow("http://rpc", DEPLOYED, transport=_rpc(answers))
        assert escrow.escrow_ceiling("ISS-1001") == Usdc.from_decimal("55")


class TestFakeChain:
    NOW = datetime(2026, 10, 8, tzinfo=UTC)

    def test_a_commitment_without_a_ceiling_is_refused(self) -> None:
        chain = FakeChain()
        with pytest.raises(ChainRevert, match="NoCeiling"):
            chain.commit("ISS-1", "0xB0B", Usdc(1), self.NOW, self.NOW)
        assert chain.commitment("ISS-1") is None

    def test_a_commitment_above_the_ceiling_is_refused(self) -> None:
        chain = FakeChain()
        chain.set_ceiling("ISS-1", Usdc(100), self.NOW)
        with pytest.raises(ChainRevert, match="ExceedsCeiling"):
            chain.commit("ISS-1", "0xB0B", Usdc(101), self.NOW, self.NOW)

    def test_the_ceiling_itself_is_committable(self) -> None:
        chain = FakeChain()
        chain.set_ceiling("ISS-1", Usdc(100), self.NOW)
        chain.commit("ISS-1", "0xB0B", Usdc(100), self.NOW, self.NOW)
        assert chain.commitment("ISS-1") is not None

    def test_clearing_the_ceiling_makes_the_issue_unfundable_again(self) -> None:
        chain = FakeChain()
        chain.set_ceiling("ISS-1", Usdc(100), self.NOW)
        chain.set_ceiling("ISS-1", Usdc(0), self.NOW)
        assert chain.escrow_ceiling("ISS-1") is None
        with pytest.raises(ChainRevert, match="NoCeiling"):
            chain.commit("ISS-1", "0xB0B", Usdc(1), self.NOW, self.NOW)

    def test_a_reset_forgets_ceilings_with_the_books(self) -> None:
        chain = FakeChain()
        chain.set_ceiling("ISS-1", Usdc(100), self.NOW)
        chain.reset()
        assert chain.escrow_ceiling("ISS-1") is None

    def test_one_commitment_reads_back_with_its_publisher(self) -> None:
        chain = FakeChain()
        now = datetime.now(UTC)
        chain.set_ceiling("ISS-1", Usdc.from_decimal("55"), now)
        chain.commit("ISS-1", "0xB0B", Usdc.from_decimal("55"), now, now)
        held = chain.commitment("ISS-1")
        assert held is not None
        assert (held.status, held.publisher) == (EscrowStatus.HELD, "0xB0B")
        assert chain.commitment("ISS-2") is None
