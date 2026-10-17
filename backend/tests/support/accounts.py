"""Sign-in helpers for the seeded parties, so a test can act as one."""

from __future__ import annotations

from siwe_wallet import Wallet

from misthos.schemas import Account

PARTY_SEEDS = {
    **{f"PUB-{i}": 1000 + i for i in range(1, 6)},
    **{f"CON-{i}": 2000 + i for i in range(1, 7)},
}


def account_for(party_id: str) -> Account:
    seed = PARTY_SEEDS[party_id]
    wallet = Wallet(seed)
    role = "publisher" if party_id.startswith("PUB") else "contributor"
    login = f"{party_id.lower()}-login"
    return Account(
        address=wallet.address.lower(),
        role=role,  # type: ignore[arg-type]
        party_id=party_id,
        github_login=login,
        created_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )


def sign_in_party(client, party_id: str) -> dict:
    seed = PARTY_SEEDS[party_id]
    wallet = Wallet(seed)
    nonce = client.post("/api/v1/auth/nonce").json()["nonce"]
    message = wallet.message(nonce)
    response = client.post(
        "/api/v1/auth/verify",
        json={"message": message, "signature": wallet.sign(message)},
    )
    assert response.status_code == 200, response.text
    return response.json()
