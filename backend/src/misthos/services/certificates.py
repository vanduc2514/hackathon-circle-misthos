"""Signed certificates: an escrow attestation, a settlement receipt (#202, #206).

A buyer pays for a certificate because it can be checked without trusting us twice.
The payload is serialised one way only (`canonical`: sorted keys, no whitespace), and
that text is signed with EIP-191 `personal_sign` by the certificate key. Anyone
recovers the signer from the text and the signature with any Ethereum library
(`eth_account.Account.recover_message(encode_defunct(text=canonical), signature=...)`)
and compares it with the signer we publish; the facts in the payload are then checked
against the chain, which is the authority.

The key is read from the secret store by reference at signing time, through the same
guards as the escrow's keys, and is a different key from both: the escrow does not
know it, so nothing it signs can move money. Without it the signed routes answer 503,
and a call that cannot be answered is not charged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from eth_account import Account
from eth_account.messages import encode_defunct

from misthos.config import settings
from misthos.services.attestor import AttestorKeyError, load_certificate_key, secret_store

SCHEME = "eip191-personal-sign"


class CertificatesUnavailable(RuntimeError):
    """No certificate key is configured, or it cannot be read."""


@dataclass(frozen=True)
class Signed:
    canonical: str
    signature: str
    signer: str


def canonical(payload: dict[str, Any]) -> str:
    """The one text a certificate is signed as."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _key() -> str:
    try:
        return load_certificate_key(
            secret_store(settings.secret_store_dir), settings.certificate_secret_ref
        ).material
    except AttestorKeyError as exc:
        raise CertificatesUnavailable(str(exc)) from exc


def signer() -> str:
    """The address certificates are signed by, for a buyer to check against."""
    return Account.from_key(_key()).address


def sign(payload: dict[str, Any]) -> Signed:
    text = canonical(payload)
    key = _key()
    signed = Account.sign_message(encode_defunct(text=text), private_key=key)
    return Signed(
        canonical=text,
        signature="0x" + signed.signature.hex().removeprefix("0x"),
        signer=Account.from_key(key).address,
    )


def recover(text: str, signature: str) -> str:
    """Who signed `text`. What a buyer does with a certificate, here for the tests."""
    return Account.recover_message(encode_defunct(text=text), signature=signature)
