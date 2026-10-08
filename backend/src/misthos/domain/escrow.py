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


def ceiling_call(issue_id: str) -> str:
    """Call data for `ceiling(bytes32)`: the most the escrow will take for the issue."""
    return selector("ceiling(bytes32)") + issue_key(issue_id)[2:]


def decode_ceiling(result: str) -> Usdc | None:
    """The escrow's ceiling for an issue. Zero means none: the issue cannot be funded."""
    (value,) = _words(result, 1)
    return Usdc(value) if value else None


def approved_publisher_call(issue_id: str) -> str:
    """Call data for `approvedPublisher(bytes32)`: the only wallet that may commit."""
    return selector("approvedPublisher(bytes32)") + issue_key(issue_id)[2:]


def latest_deadline_call(issue_id: str) -> str:
    """Call data for `latestDeadline(bytes32)`: the latest deadline a commitment may carry."""
    return selector("latestDeadline(bytes32)") + issue_key(issue_id)[2:]


def fee_recipient_call() -> str:
    """Call data for `feeRecipient()`: where every release's take rate is paid."""
    return selector("feeRecipient()")


def decode_address(result: str) -> str | None:
    """An address word. The zero address reads as None: nobody."""
    (value,) = _words(result, 1)
    return f"0x{value:040x}" if value else None


def decode_moment(result: str) -> datetime | None:
    """A Unix-seconds word. Zero reads as None: never."""
    (value,) = _words(result, 1)
    return datetime.fromtimestamp(value, UTC) if value else None


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


# ------------------------------------------------------------- writing

# Every custom error `MisthosEscrow` declares, so a revert reads as its name rather
# than as four bytes. Kept in step with the contract by a test against the ABI.
ERRORS = (
    "NotAToken(address)",
    "WrongDecimals(address,uint256)",
    "CommitmentStarted()",
    "NotOwner()",
    "NotAttestor()",
    "AlreadyExists()",
    "NotHeld()",
    "DeadlineNotReached()",
    "DeadlinePassed()",
    "ZeroAddress()",
    "ZeroAmount()",
    "AmountMismatch()",
    "NoCeiling()",
    "ExceedsCeiling(uint256,uint256)",
    "NotApprovedPublisher(address)",
    "DeadlineTooLate(uint64,uint64)",
    "FeeTooHigh(uint256,uint256)",
    "FeeRecipientNotSet()",
    "TransferFailed()",
)
_ERROR_NAMES = {selector(e): e.split("(")[0] for e in ERRORS}
_PANIC = selector("Panic(uint256)")
_ERROR_STRING = selector("Error(string)")


def _word(value: int | str) -> str:
    if isinstance(value, str):
        body = value.removeprefix("0x").lower()
        if len(body) > WORD_HEX or any(c not in "0123456789abcdef" for c in body):
            raise ValueError(f"{value!r} is not a word")
        return body.rjust(WORD_HEX, "0")
    if value < 0 or value >= 1 << 256:
        raise ValueError(f"{value} does not fit a uint256")
    return f"{value:064x}"


def set_ceiling_call(issue_id: str, ceiling: Usdc, publisher: str, latest_deadline: int) -> str:
    """Call data for `setCeiling(bytes32,uint256,address,uint64)`. Owner only.

    The approval names the price, the only wallet that may commit it and the latest
    deadline it may be committed to (#122).
    """
    return (
        selector("setCeiling(bytes32,uint256,address,uint64)")
        + _word(issue_key(issue_id))
        + _word(ceiling.base_units)
        + _word(publisher)
        + _word(latest_deadline)
    )


def set_fee_call(issue_id: str, bps: int) -> str:
    """Call data for `setFee(bytes32,uint256)`. Owner only, and only before the money is in."""
    return selector("setFee(bytes32,uint256)") + _word(issue_key(issue_id)) + _word(bps)


def fee_call(issue_id: str) -> str:
    """Call data for `feeBps(bytes32)`: the take rate the escrow holds for the issue."""
    return selector("feeBps(bytes32)") + issue_key(issue_id)[2:]


def release_call(issue_id: str, contributor: str, amount: Usdc) -> str:
    """Call data for `release(bytes32,address,uint256)`. Attestor only."""
    return (
        selector("release(bytes32,address,uint256)")
        + _word(issue_key(issue_id))
        + _word(contributor)
        + _word(amount.base_units)
    )


def refund_call(issue_id: str) -> str:
    """Call data for `refund(bytes32)`. Anyone may send it once the deadline passed."""
    return selector("refund(bytes32)") + _word(issue_key(issue_id))


def commit_call(issue_id: str, amount: Usdc, deadline: int) -> str:
    """Call data for `commit(bytes32,uint256,uint64)`, which only the publisher sends."""
    return (
        selector("commit(bytes32,uint256,uint64)")
        + _word(issue_key(issue_id))
        + _word(amount.base_units)
        + _word(deadline)
    )


def approve_call(spender: str, amount: Usdc) -> str:
    """Call data for the USDC `approve(address,uint256)` a commitment needs first."""
    return selector("approve(address,uint256)") + _word(spender) + _word(amount.base_units)


def revert_name(data: str | None) -> str:
    """Name the custom error in revert data, or say it could not be named."""
    body = (data or "").removeprefix("0x")
    if len(body) < 8:
        return "reverted"
    head = "0x" + body[:8].lower()
    if head == _PANIC and len(body) >= 8 + WORD_HEX:
        # Solidity's own checks: 0x11 is arithmetic over- or underflow, which is how a
        # token without enough balance or allowance fails a transfer.
        return f"Panic(0x{int(body[8 : 8 + WORD_HEX], 16):x})"
    if head == _ERROR_STRING and len(body) >= 8 + 2 * WORD_HEX:
        length = int(body[8 + WORD_HEX : 8 + 2 * WORD_HEX], 16)
        start = 8 + 2 * WORD_HEX
        return bytes.fromhex(body[start : start + 2 * length]).decode(errors="replace")
    name = _ERROR_NAMES.get(head)
    if name is None:
        return f"reverted with 0x{body[:8]}"
    if name in {"ExceedsCeiling", "DeadlineTooLate"} and len(body) >= 8 + 2 * WORD_HEX:
        first, second = _words("0x" + body[8 : 8 + 2 * WORD_HEX], 2)
        return f"{name}({first}, {second})"
    if name == "NotApprovedPublisher" and len(body) >= 8 + WORD_HEX:
        (sender,) = _words("0x" + body[8 : 8 + WORD_HEX], 1)
        return f"NotApprovedPublisher(0x{sender:040x})"
    return name
