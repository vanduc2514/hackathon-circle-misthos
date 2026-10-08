"""What MisthosEscrow holds, encoded and decoded without a chain client.

The API reads commitments back from the contract so a contributor can trust the
money before writing code, and reconciliation compares the ledger with the same
read. The encoding is fixed by the contract's ABI, and a mistake here would show
money that is not there, so the call data and the decoding live in the pure domain
where they are tested without an RPC.
"""

from __future__ import annotations

from datetime import UTC, datetime

from Crypto.Hash import keccak

from misthos.domain.ledger import EscrowStatus, OnChain
from misthos.domain.money import Usdc

WORD_HEX = 64

# `MisthosEscrow.Status` in declaration order. 0 is `None`: never committed.
_STATUS = {1: EscrowStatus.HELD, 2: EscrowStatus.RELEASED, 3: EscrowStatus.REFUNDED}


def keccak256(data: bytes) -> bytes:
    return keccak.new(digest_bits=256, data=data).digest()


def issue_key(issue_id: str) -> str:
    """The bytes32 the contract stores an issue under: keccak256 of its platform id."""
    return "0x" + keccak256(issue_id.encode()).hex()


def selector(signature: str) -> str:
    return "0x" + keccak256(signature.encode())[:4].hex()


def topic(signature: str) -> str:
    return "0x" + keccak256(signature.encode()).hex()


COMMITTED_TOPIC = topic("Committed(bytes32,address,uint256,uint64)")


def commitments_call_by_key(key: str) -> str:
    """Call data for `commitments(bytes32)` on a stored key."""
    return selector("commitments(bytes32)") + key.removeprefix("0x")


def commitments_call(issue_id: str) -> str:
    """Call data for `commitments(bytes32)` on the given issue."""
    return commitments_call_by_key(issue_key(issue_id))


def _words(result: str, count: int) -> list[int]:
    body = result.removeprefix("0x")
    if len(body) != count * WORD_HEX:
        raise ValueError(f"expected {count} words, got {len(body) / WORD_HEX:g}")
    return [int(body[i : i + WORD_HEX], 16) for i in range(0, len(body), WORD_HEX)]


def decode_commitment(result: str) -> OnChain | None:
    """Decode the four static words `commitments(bytes32)` returns.

    An issue the contract has never seen comes back as status 0 with a zero
    publisher, which is exactly what the contract means by "not committed": None.
    """
    publisher, amount, deadline, status = _words(result, 4)
    if status == 0:
        return None
    if status not in _STATUS:
        raise ValueError(f"unknown escrow status {status}")
    return OnChain(
        status=_STATUS[status],
        amount=Usdc(amount),
        publisher=f"0x{publisher:040x}",
        deadline=datetime.fromtimestamp(deadline, UTC),
    )
