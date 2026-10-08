"""A test wallet that signs Sign-In with Ethereum messages the way MetaMask does."""

from __future__ import annotations

from datetime import UTC, datetime

from coincurve import PrivateKey

from misthos.auth.siwe import address_of, checksum, personal_digest


class Wallet:
    def __init__(self, seed: int = 1) -> None:
        self.key = PrivateKey((seed).to_bytes(32, "big"))
        self.address = checksum(address_of(self.key.public_key))

    def sign(self, message: str, *, v_offset: int = 27) -> str:
        raw = self.key.sign_recoverable(personal_digest(message), hasher=None)
        return "0x" + (raw[:64] + bytes([raw[64] + v_offset])).hex()

    def message(
        self,
        nonce: str,
        *,
        domain: str = "localhost:5173",
        chain_id: int = 5042002,
        issued_at: datetime | None = None,
        expiration: datetime | None = None,
        address: str | None = None,
    ) -> str:
        issued = (issued_at or datetime.now(UTC)).isoformat().replace("+00:00", "Z")
        lines = [
            f"{domain} wants you to sign in with your Ethereum account:",
            address or self.address,
            "",
            "Sign in to Misthos. This costs nothing and moves no money.",
            "",
            f"URI: http://{domain}",
            "Version: 1",
            f"Chain ID: {chain_id}",
            f"Nonce: {nonce}",
            f"Issued At: {issued}",
        ]
        if expiration is not None:
            lines.append(f"Expiration Time: {expiration.isoformat().replace('+00:00', 'Z')}")
        return "\n".join(lines)
