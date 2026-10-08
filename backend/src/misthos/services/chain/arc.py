"""MisthosEscrow on Arc: the `ChainGateway` for a deployed escrow.

It answers what the contract holds, from the contract, so the readback the API shows
and the reconciliation the store runs ask the same source. It moves money only in
the ways the contract lets the platform:

- `set_ceiling` records the approved terms, signed with the owner key: the price as
  the ceiling, the only wallet that may commit it and the latest deadline (#122). The
  take rate goes first (`setFee`), because the contract fixes it with the money, and an
  escrow with no fee recipient is refused here, before any publisher commits money it
  could never release (#127).
- `release` pays the contributor, signed with the attestor key.
- `refund` returns a lapsed commitment, signed with the attestor key (anyone may).

It never signs a commitment. `commit` moves the publisher's USDC, so the publisher's
own wallet sends it; here `commit` checks that the publisher did, against the terms
they approved (the amount, the rate, the wallet and the deadline), and returns that
transaction. Until the publisher has committed it refuses with `NotCommitted`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import UTC, datetime

import httpx

from misthos.domain.escrow import (
    COMMITTED_TOPIC,
    approved_publisher_call,
    ceiling_call,
    commitments_call,
    commitments_call_by_key,
    decode_address,
    decode_ceiling,
    decode_commitment,
    decode_moment,
    fee_call,
    fee_recipient_call,
    issue_key,
    latest_deadline_call,
    refund_call,
    release_call,
    set_ceiling_call,
    set_fee_call,
)
from misthos.domain.issue import DEADLINE_TOLERANCE
from misthos.domain.ledger import EscrowStatus, OnChain
from misthos.domain.money import Usdc
from misthos.services.chain.base import ChainRevert, NotCommitted
from misthos.services.chain.deployment import EscrowDeployment
from misthos.services.chain.rpc import ChainUnavailable, JsonRpc, Sender

# What an operator is told when the escrow has nowhere to send the take rate.
NO_FEE_RECIPIENT = (
    "the escrow has no fee recipient, so every release at a take rate would revert "
    "FeeRecipientNotSet; the owner must call setFeeRecipient before any issue is funded"
)

__all__ = ["ArcEscrow", "ChainUnavailable"]


class ArcEscrow:
    name = "arc"

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

    def approval(self, issue_id: str) -> tuple[Usdc | None, str | None, datetime | None]:
        """The approval the escrow holds: the ceiling, who may commit, and until when."""
        self.verify()
        return (
            decode_ceiling(self._call(ceiling_call(issue_id))),
            decode_address(self._call(approved_publisher_call(issue_id))),
            decode_moment(self._call(latest_deadline_call(issue_id))),
        )

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

    def set_ceiling(
        self,
        issue_id: str,
        ceiling: Usdc,
        at: datetime,
        *,
        publisher: str,
        latest_deadline: datetime,
        fee_bps: int = 0,
    ) -> str:
        """Record the approved terms on chain. Returns the last transaction sent, or ""
        when the escrow already holds them, so approving again after the publisher
        commits costs no second transaction.

        While nothing is committed the rate is set first: a ceiling in force before the
        rate would let the wallet commit at a rate nobody approved, and the escrow
        refuses to move it once the money is in. A rate with no fee recipient on the
        escrow is refused before anything is sent, since every release at that rate
        would revert after the publisher's money was already held.
        """
        signer = self._signer(self._owner, "owner")
        sent = ""
        if self.commitment(issue_id) is None:
            if fee_bps and self.fee_recipient() is None:
                raise ChainRevert(f"FeeRecipientNotSet: {NO_FEE_RECIPIENT}")
            if self.fee_bps(issue_id) != fee_bps:
                sent = signer.send(self.address, set_fee_call(issue_id, fee_bps))
        latest = int(latest_deadline.timestamp())
        wanted = (ceiling, publisher.lower(), datetime.fromtimestamp(latest, UTC))
        if self.approval(issue_id) != wanted:
            sent = signer.send(self.address, set_ceiling_call(issue_id, ceiling, publisher, latest))
        return sent

    def commit(
        self,
        issue_id: str,
        publisher: str,
        amount: Usdc,
        deadline: datetime,
        at: datetime,
        fee_bps: int = 0,
    ) -> str:
        """Confirm the publisher's own commitment against the approved terms, and return
        its transaction.

        Refuses with `NotCommitted` until the publisher's wallet has committed, so the
        store books nothing and the wallet sends the commitment the plan spelled out.
        `deadline` is the one the approval fixed: the escrow refuses a later one, and a
        commitment more than `DEADLINE_TOLERANCE` earlier would cut the contributor's
        window, so it is not booked.
        """
        held = self.commitment(issue_id)
        if held is None:
            raise NotCommitted(
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
                f"WrongPublisher: committed from {held.publisher}, not the wallet approved"
            )
        if held.amount != amount:
            raise ChainRevert(
                f"AmountMismatch: committed {held.amount}, but {amount} was approved"
            )
        if held.deadline is None or held.deadline < deadline - DEADLINE_TOLERANCE:
            raise ChainRevert(f"DeadlineTooEarly: committed until {held.deadline}, not {deadline}")
        if held.deadline > deadline:
            raise ChainRevert(f"DeadlineTooLate: committed until {held.deadline}, not {deadline}")
        return self._commit_tx(issue_id)

    def commit_tx(self, issue_id: str) -> str:
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
        logs = self._rpc(
            "eth_getLogs",
            [
                {
                    "address": self.address,
                    "fromBlock": hex(self.deployment.deployed_at_block or 0),
                    "toBlock": "latest",
                    "topics": topics,
                }
            ],
        )
        return list(logs) if isinstance(logs, list) else []

    def _commit_tx(self, issue_id: str) -> str:
        logs = self._committed_logs(issue_key(issue_id))
        if not logs:
            raise ChainUnavailable(f"the escrow holds {issue_id}, but no Committed log was found")
        return str(logs[-1]["transactionHash"])

    def _read(self, data: str) -> OnChain | None:
        return decode_commitment(self._call(data))

    def _call(self, data: str) -> str:
        return str(self._rpc("eth_call", [{"to": self.address, "data": data}, "latest"]))
