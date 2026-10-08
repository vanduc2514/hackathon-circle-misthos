"""Sign-In with Ethereum: every claim in the message is checked, then the signer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from siwe_wallet import Wallet

from misthos.auth.siwe import SignInRefused, checksum, parse, recover, verify

NOW = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
NONCE = "a1b2c3d4e5f6a7b8c9d0e1f2"


def check(message: str, signature: str, *, nonce: str = NONCE) -> object:
    return verify(message, signature, domain="localhost:5173", nonce=nonce, now=NOW)


def test_eip55_checksum_matches_the_spec() -> None:
    assert checksum("0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed") == (
        "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
    )


def test_a_genuine_sign_in_is_accepted() -> None:
    wallet = Wallet()
    message = wallet.message(NONCE, issued_at=NOW)
    signed = check(message, wallet.sign(message))
    assert signed.address == wallet.address  # type: ignore[attr-defined]
    assert signed.chain_id == 5042002  # type: ignore[attr-defined]


@pytest.mark.parametrize("v_offset", [0, 27])
def test_either_recovery_id_convention_recovers_the_signer(v_offset: int) -> None:
    wallet = Wallet(7)
    assert recover("hello", wallet.sign("hello", v_offset=v_offset)) == wallet.address.lower()


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"domain": "evil.example"}, "not localhost:5173"),
        ({"chain_id": 1}, "not Arc"),
        ({"issued_at": NOW - timedelta(minutes=30)}, "not issued just now"),
        ({"issued_at": NOW + timedelta(minutes=30)}, "not issued just now"),
        ({"expiration": NOW - timedelta(seconds=1)}, "expired"),
    ],
)
def test_a_message_for_somewhere_or_somewhen_else_is_refused(change: dict, reason: str) -> None:
    wallet = Wallet()
    message = wallet.message(NONCE, **{"issued_at": NOW, **change})
    with pytest.raises(SignInRefused, match=reason):
        check(message, wallet.sign(message))


def test_a_nonce_we_did_not_issue_is_refused() -> None:
    wallet = Wallet()
    message = wallet.message("ffffffffffffffffffffffff", issued_at=NOW)
    with pytest.raises(SignInRefused, match="nonce"):
        check(message, wallet.sign(message))


def test_signing_for_someone_elses_address_proves_nothing() -> None:
    victim, attacker = Wallet(1), Wallet(2)
    message = attacker.message(NONCE, issued_at=NOW, address=victim.address)
    with pytest.raises(SignInRefused, match="not from the address"):
        check(message, attacker.sign(message))


def test_an_edited_message_no_longer_matches_its_signature() -> None:
    wallet = Wallet()
    message = wallet.message(NONCE, issued_at=NOW)
    signature = wallet.sign(message)
    with pytest.raises(SignInRefused):
        check(message.replace("Chain ID: 5042002", "Chain ID: 5042"), signature)


@pytest.mark.parametrize(
    "garbage", ["", "hello", "x wants you to sign in with your Ethereum account:\nnot-an-address"]
)
def test_garbage_is_not_a_sign_in_message(garbage: str) -> None:
    with pytest.raises(SignInRefused):
        parse(garbage)


def test_a_malformed_signature_is_refused() -> None:
    with pytest.raises(SignInRefused, match="65 bytes"):
        recover("hello", "0x1234")
