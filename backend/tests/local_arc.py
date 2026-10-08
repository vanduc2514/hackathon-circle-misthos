"""A local Arc for the tests that must meet the real escrow, not the simulation.

The simulation refuses what MisthosEscrow refuses as far as anyone wrote it down, and
#121 showed what that misses: a release the simulation paid that the contract reverts.
These tests run the store against the compiled contract on anvil instead, with a
6-decimal USDC stand-in and the keys the platform signs with.

Nothing here reaches a network: anvil is a process on this machine with no fork. They
run where Foundry's `anvil` and `forge` are on the PATH (or in `MISTHOS_FOUNDRY_BIN`)
and forge-std is cloned under contracts/lib (`mise run setup:contracts`), and are
skipped otherwise, as in the backend's CI job.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from eth_account import Account

from misthos.domain.escrow import approve_call, commit_call, selector
from misthos.domain.money import Usdc
from misthos.services.chain import ArcEscrow, EscrowDeployment
from misthos.services.chain.rpc import JsonRpc, Sender

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
CHAIN_ID = 31337

# anvil's default accounts, by the part each plays.
KEYS = {
    "owner": "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",
    "attestor": "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",
    "publisher": "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a",
    "treasury": "0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6",
    "stranger": "0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a",
    "contributor": "0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba",
}
ADDRESS = {who: Account.from_key(key).address for who, key in KEYS.items()}
GRANT = Usdc.from_decimal("100000")


def _tool(name: str) -> str | None:
    return shutil.which(name, path=os.environ.get("MISTHOS_FOUNDRY_BIN")) or shutil.which(name)


def unavailable() -> str | None:
    """Why the local Arc cannot run here, or None when it can."""
    for name in ("anvil", "forge"):
        if _tool(name) is None:
            return f"{name} is not installed (Foundry)"
    if not (CONTRACTS / "lib" / "forge-std").is_dir():
        return "forge-std is not cloned: mise run setup:contracts"
    return None


_built: dict[str, str] = {}


def _bytecode(source: str, name: str) -> str:
    """The compiled creation code, built once per test session."""
    if not _built:
        forge = _tool("forge")
        assert forge is not None
        subprocess.run([forge, "build", "--root", str(CONTRACTS)], check=True, capture_output=True)
    key = f"{source}:{name}"
    if key not in _built:
        artifact = json.loads((CONTRACTS / "out" / source / f"{name}.json").read_text())
        _built[key] = artifact["bytecode"]["object"]
    return _built[key]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _word(address: str) -> str:
    return address.lower().removeprefix("0x").rjust(64, "0")


@dataclass
class LocalArc:
    """One anvil, with the escrow and the token deployed and the parties funded."""

    url: str
    rpc: JsonRpc
    escrow: str
    usdc: str
    process: subprocess.Popen = field(repr=False)

    @classmethod
    def start(cls, *, fee_recipient: bool = True) -> LocalArc:
        anvil = _tool("anvil")
        assert anvil is not None
        port = _free_port()
        process = subprocess.Popen(
            [anvil, "--port", str(port), "--chain-id", str(CHAIN_ID), "--silent"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        url = f"http://127.0.0.1:{port}"
        rpc = JsonRpc(url, timeout=5)
        for _ in range(100):
            try:
                rpc("eth_chainId", [])
                break
            except Exception:
                time.sleep(0.05)
        arc = cls(url=url, rpc=rpc, escrow="", usdc="", process=process)
        arc.usdc = arc._deploy(_bytecode("MockUsdcToken.sol", "MockUsdcToken"))
        arc.escrow = arc._deploy(
            _bytecode("MisthosEscrow.sol", "MisthosEscrow")
            + _word(ADDRESS["attestor"])
            + _word(arc.usdc)
        )
        if fee_recipient:
            treasury = selector("setFeeRecipient(address)") + _word(ADDRESS["treasury"])
            arc.send("owner", arc.escrow, treasury)
        for who in ("publisher", "stranger"):
            mint = selector("mint(address,uint256)") + _word(ADDRESS[who])
            arc.send("owner", arc.usdc, mint + f"{GRANT.base_units:064x}")
        return arc

    def _deploy(self, code: str) -> str:
        created = {"from": ADDRESS["owner"], "data": "0x" + code.removeprefix("0x")}
        tx = self.rpc("eth_sendTransaction", [{**created, "gas": hex(8_000_000)}])
        receipt = self.rpc("eth_getTransactionReceipt", [tx])
        assert isinstance(receipt, dict) and receipt.get("contractAddress"), receipt
        return str(receipt["contractAddress"])

    def stop(self) -> None:
        self.process.terminate()
        self.process.wait(timeout=10)

    # ---------------------------------------------------------------- reading

    def now(self) -> datetime:
        """The latest block's clock: what the contract compares deadlines with."""
        block = self.rpc("eth_getBlockByNumber", ["latest", False])
        assert isinstance(block, dict)
        return datetime.fromtimestamp(int(block["timestamp"], 16), UTC)

    def balance(self, who: str) -> Usdc:
        address = ADDRESS.get(who, who)
        data = selector("balanceOf(address)") + _word(address)
        return Usdc(int(str(self.rpc("eth_call", [{"to": self.usdc, "data": data}, "latest"])), 16))

    # ---------------------------------------------------------------- acting

    def sender(self, who: str) -> Sender:
        return Sender(self.rpc, CHAIN_ID, lambda: KEYS[who], poll=0.02)

    def send(self, who: str, to: str, data: str) -> str:
        return self.sender(who).send(to, data)

    def wallet_commits(self, who: str, issue_id: str, amount: Usdc, deadline: int) -> None:
        """What the web app has the publisher's own wallet send from the plan."""
        self.send(who, self.usdc, approve_call(self.escrow, amount))
        self.send(who, self.escrow, commit_call(issue_id, amount, deadline))

    def warp_to(self, moment: datetime) -> None:
        """Move the chain's clock to `moment` and mine a block there."""
        ahead = int((moment - self.now()).total_seconds())
        if ahead > 0:
            self.rpc("evm_increaseTime", [ahead])
        self.rpc("evm_mine", [])

    def gateway(self, issue_ids: object) -> ArcEscrow:
        """The escrow as the store settles through it, signing as the platform does."""
        return ArcEscrow(
            self.url,
            EscrowDeployment(self.escrow, CHAIN_ID, "override", deployed_at_block=0),
            issue_ids=issue_ids,  # type: ignore[arg-type]
            owner=self.sender("owner"),
            attestor=self.sender("attestor"),
        )
