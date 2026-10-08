"""MisthosEscrow on Arc, read through JSON-RPC.

This is the `ChainGateway` for a deployed escrow, read side only. It answers what
the contract holds, from the contract, so the readback the API shows and the
reconciliation the store runs ask the same source. The write side (commit, release,
refund) needs the attestor's signer and is the settlement orchestrator's job (#69);
until then those calls fail loudly rather than pretend a transfer happened.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime

import httpx

from misthos.domain.escrow import (
    COMMITTED_TOPIC,
    ceiling_call,
    commitments_call,
    commitments_call_by_key,
    decode_ceiling,
    decode_commitment,
    issue_key,
)
from misthos.domain.ledger import OnChain
from misthos.domain.money import Usdc
from misthos.services.chain.deployment import EscrowDeployment


class ChainUnavailable(RuntimeError):
    """The RPC could not be read, or the deployment could not be trusted."""


class ArcEscrow:
    name = "arc"

    def __init__(
        self,
        rpc_url: str,
        deployment: EscrowDeployment,
        *,
        issue_ids: Callable[[], Iterable[str]] = list,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.rpc_url = rpc_url
        self.deployment = deployment
        self.address = deployment.address
        self._issue_ids = issue_ids
        self._transport = transport
        self._timeout = timeout
        self._verified = False

    # ------------------------------------------------------------- reading

    def verify(self) -> None:
        """Refuse an address with no code: a record of a deploy that never landed."""
        if self._verified:
            return
        code = self._rpc("eth_getCode", [self.address, "latest"])
        if not code or code in ("0x", "0x0"):
            raise ChainUnavailable(
                f"no contract at {self.address} on chain {self.deployment.chain_id}: "
                "the deployment record names an address that holds no code"
            )
        self._verified = True

    def commitment(self, issue_id: str) -> OnChain | None:
        self.verify()
        return self._read(commitments_call(issue_id))

    def escrow_ceiling(self, issue_id: str) -> Usdc | None:
        self.verify()
        return decode_ceiling(self._call(ceiling_call(issue_id)))

    def commitments(self) -> dict[str, OnChain]:
        """Every commitment the escrow has emitted, keyed by issue id.

        The contract stores keccak256(issue id), so keys are matched back to the
        issues the platform knows. A key nobody knows is reported under the key
        itself: money on chain for an issue with no record is exactly what
        reconciliation exists to surface.
        """
        self.verify()
        known = {issue_key(i): i for i in self._issue_ids()}
        logs = self._rpc(
            "eth_getLogs",
            [
                {
                    "address": self.address,
                    "fromBlock": hex(self.deployment.deployed_at_block or 0),
                    "toBlock": "latest",
                    "topics": [COMMITTED_TOPIC],
                }
            ],
        )
        found: dict[str, OnChain] = {}
        for log in logs or []:
            key = str(log["topics"][1]).lower()
            issue_id = known.get(key, key)
            on_chain = self._read(commitments_call_by_key(key))
            if on_chain is not None:
                found[issue_id] = on_chain
        return found

    # ------------------------------------------------------------- writing

    def set_ceiling(self, issue_id: str, ceiling: Usdc, at: datetime) -> str:
        raise NotImplementedError("setting a ceiling on Arc is the settlement orchestrator (#69)")

    def commit(
        self, issue_id: str, publisher: str, amount: Usdc, deadline: datetime, at: datetime
    ) -> str:
        raise NotImplementedError("committing on Arc is the settlement orchestrator (#69)")

    def release(self, issue_id: str, contributor: str, amount: Usdc, at: datetime) -> str:
        raise NotImplementedError("releasing on Arc is the settlement orchestrator (#69)")

    def refund(self, issue_id: str, at: datetime) -> str:
        raise NotImplementedError("refunding on Arc is the settlement orchestrator (#69)")

    def reset(self) -> None:
        raise ChainUnavailable("a real chain cannot be reset")

    # ------------------------------------------------------------- the RPC

    def _read(self, data: str) -> OnChain | None:
        return decode_commitment(self._call(data))

    def _call(self, data: str) -> str:
        return str(self._rpc("eth_call", [{"to": self.address, "data": data}, "latest"]))

    def _rpc(self, method: str, params: list) -> object:
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            with httpx.Client(transport=self._transport, timeout=self._timeout) as http:
                response = http.post(self.rpc_url, json=body)
                response.raise_for_status()
                answer = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ChainUnavailable(f"the Arc RPC could not be read: {exc}") from exc
        if answer.get("error"):
            raise ChainUnavailable(f"the Arc RPC refused {method}: {answer['error']}")
        return answer.get("result")
