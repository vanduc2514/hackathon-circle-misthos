"""MisthosEscrow on Arc: the `ChainGateway` for a deployed escrow.

It answers what the contract holds, from the contract, so the readback the API shows
and the reconciliation the store runs ask the same source. It moves money only in
the ways the contract lets the platform:

- `set_ceiling` records the approved price, signed with the owner key, and the take
  rate goes with it (`setFee`), because the contract fixes the rate before the money
  is in.
- `release` pays the contributor, signed with the attestor key.
- `refund` returns a lapsed commitment, signed with the attestor key (anyone may).

It never signs a commitment. `commit` moves the publisher's USDC, so the publisher's
own wallet sends it; here `commit` checks that the publisher did, for the amount
approved and from the wallet on file, and returns that transaction. Until the
publisher has committed it refuses with `NotCommitted`, and the store saves nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import datetime, timedelta

import httpx

from misthos.domain.escrow import (
    COMMITTED_TOPIC,
    ceiling_call,
    commitments_call,
    commitments_call_by_key,
    decode_address,
    decode_ceiling,
    decode_commitment,
    fee_call,
    fee_recipient_call,
    issue_key,
    refund_call,
    release_call,
    set_ceiling_call,
    set_fee_call,
)
from misthos.domain.ledger import EscrowStatus, OnChain
from misthos.domain.money import Usdc
from misthos.services.chain.base import ChainRevert
from misthos.services.chain.deployment import EscrowDeployment
from misthos.services.chain.rpc import ChainUnavailable, JsonRpc, Sender

# What an operator is told when the escrow has nowhere to send the take rate.
NO_FEE_RECIPIENT = (
    "the escrow has no fee recipient, so every release at a take rate would revert "
    "FeeRecipientNotSet; the owner must call setFeeRecipient before any issue is funded"
)

# How far the deadline the publisher committed may sit from the one the platform
# expects. The publisher's wallet sends the commitment minutes after approval, so
# they cannot match to the second; a deadline days early would cut the escrow term.
DEADLINE_TOLERANCE = timedelta(hours=1)

__all__ = ["ArcEscrow", "ChainUnavailable"]


class ArcEscrow:
    name = "arc"

    # A node caps how wide one `eth_getLogs` may be, and asking once from the deploy
    # block to the head worked only while the chain was young: past the cap every
    # reconcile failed for good, so the sweeper retried every minute and never
    # succeeded -- no claim expired, no deadline refunded and no silent publisher was
    # ever released.
    #
    # Arc testnet's own cap was measured rather than guessed. Against
    # https://rpc.testnet.arc.io, a query ending at the head was accepted at 9,999
    # blocks and refused at 10,000 with `-32012 "requested range too large"`; a wider
    # query is refused earlier with `-32602 "query exceeds max block range 100000"`.
    # So the real cap here is 10,000, and this walks half of it: margin for a node
    # whose limit differs, and the windows still tile the range exactly.
    LOG_RANGE_BLOCKS = 5_000

    def __init__(
        self,
        rpc_url: str,
        deployment: EscrowDeployment,
        *,
        issue_ids: Callable[[], Iterable[str]] = list,
        owner: Sender | None = None,
        attestor: Sender | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.rpc_url = rpc_url
        self.deployment = deployment
        self.address = deployment.address
        self._rpc = JsonRpc(rpc_url, transport=transport, timeout=timeout)
        self._issue_ids = issue_ids
        self._owner = owner
        self._attestor = attestor
        self._verified = False

    @property
    def rpc(self) -> JsonRpc:
        return self._rpc

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
        held = self._read(commitments_call(issue_id))
        if held is None:
            return None
        # The rate is read back with the money, so the fee the store records on
        # release is the one this escrow will actually carve out.
        return replace(held, fee_bps=self.fee_bps(issue_id))

    def fee_bps(self, issue_id: str) -> int:
        self.verify()
        return int(self._call(fee_call(issue_id)), 16)

    def escrow_ceiling(self, issue_id: str) -> Usdc | None:
        self.verify()
        return decode_ceiling(self._call(ceiling_call(issue_id)))

    def fee_recipient(self) -> str | None:
        """Where releases pay the take rate, or None when the owner never named one."""
        self.verify()
        return decode_address(self._call(fee_recipient_call()))

    def settlement_problems(self) -> list[str]:
        """What would stop this escrow paying a funded issue out. Read at startup, so an
        operator hears of it before a publisher commits money, not at the first release."""
        try:
            return [] if self.fee_recipient() is not None else [NO_FEE_RECIPIENT]
        except ChainUnavailable as exc:
            return [str(exc)]

    def commitments(self) -> dict[str, OnChain]:
        """Every commitment the escrow has emitted, keyed by issue id.

        The contract stores keccak256(issue id), so keys are matched back to the
        issues the platform knows. A key nobody knows is reported under the key
        itself: money on chain for an issue with no record is exactly what
        reconciliation exists to surface.
        """
        self.verify()
        known = {issue_key(i): i for i in self._issue_ids()}
        found: dict[str, OnChain] = {}
        for log in self._committed_logs():
            key = str(log["topics"][1]).lower()
            on_chain = self._read(commitments_call_by_key(key))
            if on_chain is not None:
                found[known.get(key, key)] = on_chain
        return found

    # ------------------------------------------------------------- writing

    def set_ceiling(self, issue_id: str, ceiling: Usdc, at: datetime) -> str:
        """Record the approved price on chain. Returns "" when it is already there,
        so approving again after the publisher commits costs no second transaction."""
        if self.escrow_ceiling(issue_id) == ceiling:
            return ""
        return self._signer(self._owner, "owner").send(
            self.address, set_ceiling_call(issue_id, ceiling)
        )

    def commit(
        self,
        issue_id: str,
        publisher: str,
        amount: Usdc,
        deadline: datetime,
        at: datetime,
        fee_bps: int = 0,
    ) -> str:
        """Confirm the publisher's own commitment, and return its transaction.

        Before the publisher has committed, this fixes the take rate on the escrow (the
        contract refuses to move it once the money is in) and refuses with
        `NotCommitted`, so the store saves nothing and the publisher's wallet sends the
        commitment at the rate they saw.
        """
        held = self.commitment(issue_id)
        if held is None:
            # The rate goes on with the money, and the contract refuses to move it once
            # it is in. On an escrow with no fee recipient every release at that rate
            # would revert FeeRecipientNotSet, after the publisher's money was already
            # held, so it is refused here instead (#127).
            if fee_bps and self.fee_recipient() is None:
                raise ChainRevert(f"FeeRecipientNotSet: {NO_FEE_RECIPIENT}")
            if self.fee_bps(issue_id) != fee_bps:
                self._signer(self._owner, "owner").send(
                    self.address, set_fee_call(issue_id, fee_bps)
                )
            raise ChainRevert(
                "NotCommitted: the publisher has not committed this issue from their wallet yet"
            )
        if held.fee_bps != fee_bps:
            raise ChainRevert(
                f"FeeMismatch: the escrow holds {held.fee_bps} bps, but {fee_bps} was approved"
            )
        if held.status is not EscrowStatus.HELD:
            raise ChainRevert(f"NotHeld: the escrow reports {held.status}")
        if (held.publisher or "").lower() != publisher.lower():
            raise ChainRevert(
                f"WrongPublisher: committed from {held.publisher}, not the wallet on file"
            )
        if held.amount != amount:
            raise ChainRevert(
                f"AmountMismatch: committed {held.amount}, but {amount} was approved"
            )
        if held.deadline is None or held.deadline < deadline - DEADLINE_TOLERANCE:
            raise ChainRevert(f"DeadlineTooEarly: committed until {held.deadline}, not {deadline}")
        return self._commit_tx(issue_id)

    def release(self, issue_id: str, contributor: str, amount: Usdc, at: datetime) -> str:
        return self._signer(self._attestor, "attestor").send(
            self.address, release_call(issue_id, contributor, amount)
        )

    def refund(self, issue_id: str, at: datetime) -> str:
        return self._signer(self._attestor, "attestor").send(self.address, refund_call(issue_id))

    def reset(self) -> None:
        raise ChainUnavailable("a real chain cannot be reset")

    # ------------------------------------------------------------- internals

    @staticmethod
    def _signer(sender: Sender | None, role: str) -> Sender:
        if sender is None:
            raise ChainUnavailable(f"no {role} key is configured, so this cannot be signed")
        return sender

    def _committed_logs(self, key: str | None = None) -> list[dict]:
        topics: list[str] = [COMMITTED_TOPIC]
        if key is not None:
            topics.append(key)
        first = self.deployment.deployed_at_block or 0
        head = int(self._rpc("eth_blockNumber", []), 16)
        found: list[dict] = []
        start = first
        while start <= head:
            end = min(start + self.LOG_RANGE_BLOCKS - 1, head)
            logs = self._rpc(
                "eth_getLogs",
                [
                    {
                        "address": self.address,
                        "fromBlock": hex(start),
                        "toBlock": hex(end),
                        "topics": topics,
                    }
                ],
            )
            if isinstance(logs, list):
                found.extend(logs)
            start = end + 1
        return found

    def _commit_tx(self, issue_id: str) -> str:
        logs = self._committed_logs(issue_key(issue_id))
        if not logs:
            raise ChainUnavailable(f"the escrow holds {issue_id}, but no Committed log was found")
        return str(logs[-1]["transactionHash"])

    def _read(self, data: str) -> OnChain | None:
        return decode_commitment(self._call(data))

    def _call(self, data: str) -> str:
        return str(self._rpc("eth_call", [{"to": self.address, "data": data}, "latest"]))
