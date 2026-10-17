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

import time
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

    # And a limit on how fast that walk may go. Against the same node, roughly ten
    # back-to-back `eth_getLogs` are accepted and the eleventh answers 429, and the same
    # node allows about sixty in a rolling minute -- which one walk of the range needs
    # sixty-five of, so a walk of the whole history cannot fit inside the budget however
    # it is paced (#154, #161). What a caller gets instead is a page that is retried when
    # the node refuses it for being too fast, waiting long enough for the minute to roll.
    LOG_PAUSE_SECONDS = 0.2
    LOG_THROTTLE_SECONDS = 5.0
    LOG_THROTTLE_MAX_SECONDS = 65.0
    LOG_WINDOW_ATTEMPTS = 8

    # So the common case does not walk the range at all. A confirmation is about a
    # commitment the publisher's wallet just made, and the booking has to happen within
    # an hour of it (#126), so its log is within the last few hours of blocks. Arc
    # testnet ran at about 2 blocks a second when this was measured, so this is roughly
    # three hours: four windows, comfortably clear of both limits, and a few hundred
    # blocks of a fresh deployment.
    LOG_RECENT_BLOCKS = 20_000

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

        The contract stores one commitment per `keccak256(issue id)`, so an issue the
        platform knows is read with a single `eth_call` -- `commitment(issue_id)`, which
        is not the method the node rate-limits. Walking the log instead cost about
        sixty-five requests a pass against a node that allows roughly sixty a minute, so
        reconciliation failed on every pass and the divergence it exists to report went
        unreported (#154).

        The log walk is kept for the one question it is needed for: keys nobody knows.
        Money on chain for an issue the platform has no record of is exactly what
        reconciliation exists to surface, and no per-issue read can find it. That walk is
        bounded to the recent blocks, because a key nobody knows is also not something to
        read the whole history for.
        """
        self.verify()
        known = {issue_key(i): i for i in self._issue_ids()}
        found: dict[str, OnChain] = {}
        # The issues we know, one call each.
        for key, issue_id in known.items():
            on_chain = self._read(commitments_call_by_key(key))
            if on_chain is not None:
                found[issue_id] = on_chain
        # Then anybody else's, which only the log can name.
        for log in self._committed_logs(recent=self.LOG_RECENT_BLOCKS):
            key = str(log["topics"][1]).lower()
            if key in known:
                continue
            on_chain = self._read(commitments_call_by_key(key))
            if on_chain is not None:
                found[key] = on_chain
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
        # `deadline` is the one the approval fixed, not one recomputed from the clock:
        # the escrow's term starts when the wallet commits, so a deadline worked out at
        # booking time is later than the one on chain, and comparing the two could never
        # pass again once an hour had gone by -- with the money already in (#126). The
        # tolerance below is therefore slack for a wallet that rounds, and nothing more.
        if held.deadline is None or held.deadline < deadline - DEADLINE_TOLERANCE:
            raise ChainRevert(f"DeadlineTooEarly: committed until {held.deadline}, not {deadline}")
        if held.deadline > deadline:
            # The fix widening on the other side: a far-future deadline would hold the
            # platform's own refund off, so the escrow must not accept one beyond what
            # the publisher approved.
            raise ChainRevert(f"DeadlineTooLate: committed until {held.deadline}, not {deadline}")
        return self._commit_tx(issue_id, publisher)

    def commitment_tx(self, issue_id: str, publisher: str) -> str:
        """The transaction that carried this issue's commitment, from the log.

        A commitment the platform booked already has its reference recorded. This is for
        one it did not: reconciliation reads it back so the ledger can be told what the
        chain already knows (#161).
        """
        return self._commit_tx(issue_id, publisher)

    def release(self, issue_id: str, contributor: str, amount: Usdc, at: datetime) -> str:
        return self._signer(self._attestor, "attestor").send(
            self.address, release_call(issue_id, contributor, amount)
        )

    def refund(self, issue_id: str, at: datetime) -> str:
        return self._signer(self._attestor, "attestor").send(self.address, refund_call(issue_id))

    # ------------------------------------------------------------- internals

    @staticmethod
    def _signer(sender: Sender | None, role: str) -> Sender:
        if sender is None:
            raise ChainUnavailable(f"no {role} key is configured, so this cannot be signed")
        return sender

    def _committed_logs(
        self,
        key: str | None = None,
        publisher: str | None = None,
        recent: int | None = None,
    ) -> list[dict]:
        """Every Committed log, narrowed to one issue and one publisher when given.

        The narrowing is the node's work, not ours: both are indexed topics, so a whole
        history of commitments for other issues costs one page's worth of data instead of
        a page per commitment.

        `recent` bounds the walk to the last that many blocks. A confirmation wants this:
        the commitment it is looking for was just made, and walking the range from the
        deployment would spend sixty-five requests to find it, past what the node allows
        in a minute (#154). Without `recent`, the walk is the whole history, which is what
        reconciliation needs for a commitment that is not recent.

        The pages are asked for one at a time, and a page the node refuses for being too
        fast is waited out and retried: a walk of the whole history cannot fit inside a
        minute's budget at this chain's length, so the only thing that makes it complete
        is being willing to take longer than a minute (#161).
        """
        topics: list[str] = [COMMITTED_TOPIC]
        if key is not None:
            topics.append(key)
        if publisher is not None:
            # A topic is a full word, so an address is padded to the right.
            topics.append("0x" + publisher.lower().replace("0x", "").rjust(64, "0"))
        head = int(self._rpc("eth_blockNumber", []), 16)
        first = self.deployment.deployed_at_block or 0
        start = max(first, head - recent + 1) if recent is not None else first
        found: list[dict] = []
        while start <= head:
            end = min(start + self.LOG_RANGE_BLOCKS - 1, head)
            logs = self._window(start, end, topics)
            if isinstance(logs, list):
                found.extend(logs)
            start = end + 1
        return found

    def _window(self, start: int, end: int, topics: list[str]) -> object:
        """One page of the log, retried when the node refuses it for being too fast.

        `JsonRpc` retries too, but with a short backoff meant for a transient blip. A
        page-rate refusal is not transient: it clears when the minute rolls over, so this
        waits longer and, on the way, grows. Without it a walk that needs more pages than
        the budget allows fails partway every time, and reconciliation can never read a
        commitment that is not recent (#161).
        """
        delay = self.LOG_THROTTLE_SECONDS
        for attempt in range(self.LOG_WINDOW_ATTEMPTS):
            try:
                return self._rpc(
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
            except ChainUnavailable as exc:
                if "429" not in str(exc) or attempt == self.LOG_WINDOW_ATTEMPTS - 1:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, self.LOG_THROTTLE_MAX_SECONDS)
        raise ChainUnavailable(f"the log window {start}-{end} was never accepted")

    def _commit_tx(self, issue_id: str, publisher: str) -> str:
        """The publisher's own commitment transaction, as the chain reports it.

        Asked of the escrow's log rather than remembered, because the platform does not
        send this transaction: the publisher's wallet does. `publisher` is the same
        address the commitment was just checked against, so the log found is this
        issue's commitment and not an earlier one from another wallet (#154).

        The recent blocks are tried first, because a booking confirms a commitment made
        moments ago. An older one is found by paging through the history a window at a
        time -- reconciliation cannot assume its orphan is recent -- with the query
        narrowed by issue and publisher, so each page carries one commitment at most.
        """
        logs = self._committed_logs(issue_key(issue_id), publisher, recent=self.LOG_RECENT_BLOCKS)
        if not logs:
            logs = self._committed_logs(issue_key(issue_id), publisher)
        if not logs:
            raise ChainUnavailable(f"the escrow holds {issue_id}, but no Committed log was found")
        return str(logs[-1]["transactionHash"])

    def _read(self, data: str) -> OnChain | None:
        return decode_commitment(self._call(data))

    def _call(self, data: str) -> str:
        return str(self._rpc("eth_call", [{"to": self.address, "data": data}, "latest"]))
