"""Sign-In with Ethereum (EIP-4361), verified without trusting the client.

The wallet signs a plain-text message naming our domain, its own address, the Arc
chain, a nonce we issued and when it was issued. We check every one of those, then
recover the signer from the signature and require it to be the address the message
names. A signature for another site, another chain, an old nonce or someone else's
address proves nothing here.

Pure functions apart from the curve arithmetic; nothing here touches storage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from coincurve import PublicKey
from Crypto.Hash import keccak

# Arc testnet and mainnet. Nothing else is accepted.
ARC_CHAIN_IDS = frozenset({5042002, 5042})
MAX_AGE = timedelta(minutes=10)
CLOCK_SKEW = timedelta(minutes=2)

_HEADER = re.compile(r"^(?P<domain>\S+) wants you to sign in with your Ethereum account:$")
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_FIELD = re.compile(r"^(?P<key>[A-Za-z ]+): (?P<value>.+)$")


class SignInRefused(Exception):
    """The message or the signature does not prove what it claims."""


@dataclass(frozen=True)
class SiweMessage:
    domain: str
    address: str
    statement: str
    uri: str
    version: str
    chain_id: int
    nonce: str
    issued_at: datetime
    expiration_time: datetime | None = None


def _when(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse(message: str) -> SiweMessage:
    lines = message.replace("\r\n", "\n").split("\n")
    if len(lines) < 7:
        raise SignInRefused("not a sign-in message")
    header = _HEADER.match(lines[0])
    if header is None or not _ADDRESS.match(lines[1]):
        raise SignInRefused("not a sign-in message")
    fields: dict[str, str] = {}
    statement: list[str] = []
    for line in lines[2:]:
        found = _FIELD.match(line)
        if found and found["key"] in {
            "URI", "Version", "Chain ID", "Nonce", "Issued At", "Expiration Time",
            "Not Before", "Request ID", "Resources",
        }:  # fmt: skip
            fields[found["key"]] = found["value"].strip()
        elif line.strip() and not fields:
            statement.append(line.strip())
    try:
        return SiweMessage(
            domain=header["domain"],
            address=lines[1],
            statement=" ".join(statement),
            uri=fields["URI"],
            version=fields["Version"],
            chain_id=int(fields["Chain ID"]),
            nonce=fields["Nonce"],
            issued_at=_when(fields["Issued At"]),
            expiration_time=_when(fields["Expiration Time"])
            if "Expiration Time" in fields
            else None,
        )
    except (KeyError, ValueError) as exc:
        raise SignInRefused(f"the sign-in message is missing or garbles {exc}") from exc


def _keccak(data: bytes) -> bytes:
    return keccak.new(digest_bits=256, data=data).digest()


def personal_digest(message: str) -> bytes:
    """What `personal_sign` signs: the EIP-191 prefix, the length, the message."""
    body = message.encode()
    return _keccak(b"\x19Ethereum Signed Message:\n" + str(len(body)).encode() + body)


def address_of(public_key: PublicKey) -> str:
    return "0x" + _keccak(public_key.format(compressed=False)[1:])[-20:].hex()


def recover(message: str, signature: str) -> str:
    """The address that signed `message`, lowercase."""
    raw = bytes.fromhex(signature.removeprefix("0x"))
    if len(raw) != 65:
        raise SignInRefused("a signature is 65 bytes")
    v = raw[64]
    recovery = v - 27 if v >= 27 else v
    if recovery not in (0, 1):
        raise SignInRefused("the signature's recovery id is not 0 or 1")
    try:
        key = PublicKey.from_signature_and_message(
            raw[:64] + bytes([recovery]), personal_digest(message), hasher=None
        )
    except Exception as exc:
        raise SignInRefused("the signature does not recover a key") from exc
    return address_of(key)


def checksum(address: str) -> str:
    """EIP-55 mixed case, the way wallets display an address."""
    lower = address.lower().removeprefix("0x")
    digest = _keccak(lower.encode()).hex()
    return "0x" + "".join(
        c.upper() if c.isalpha() and int(digest[i], 16) >= 8 else c for i, c in enumerate(lower)
    )


def verify(message: str, signature: str, *, domain: str, nonce: str, now: datetime) -> SiweMessage:
    """Check everything the message claims, then that its address signed it."""
    parsed = parse(message)
    if parsed.domain != domain:
        raise SignInRefused(f"the message is for {parsed.domain}, not {domain}")
    if parsed.version != "1":
        raise SignInRefused("only version 1 sign-in messages are accepted")
    if parsed.chain_id not in ARC_CHAIN_IDS:
        raise SignInRefused(f"chain {parsed.chain_id} is not Arc")
    if parsed.nonce != nonce:
        raise SignInRefused("the nonce is not the one we issued")
    if parsed.issued_at > now + CLOCK_SKEW or now - parsed.issued_at > MAX_AGE:
        raise SignInRefused("the message was not issued just now")
    if parsed.expiration_time is not None and parsed.expiration_time <= now:
        raise SignInRefused("the message has expired")
    if recover(message, signature) != parsed.address.lower():
        raise SignInRefused("the signature is not from the address in the message")
    return parsed
