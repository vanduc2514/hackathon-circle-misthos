"""Unit tests for the single sign-in gate (#181).

- Step 1: Prove a wallet (MetaMask or Circle wallet)
- Step 2: Link GitHub account (before choosing role/side)
- Step 3: Choose role and name party, creating the account carrying the linked GitHub login
"""

from __future__ import annotations

import pytest
from datetime import UTC, datetime
from fastapi.testclient import TestClient

from misthos.config import settings
from misthos.main import app
from misthos.repositories import AccountConflict
from misthos.repositories.memory import MemoryRepository
from misthos.schemas import Account, NonceOut
from misthos.services.wallets import simulated_identity_address
from misthos.store import AccountExists, Store
from tests.siwe_wallet import Wallet

API = "/api/v1"


def test_repository_pending_github_links():
    repo = MemoryRepository()
    repo.reset()

    # Link pending login
    repo.set_pending_github_link("0x1111", "alice")
    assert repo.get_pending_github_link("0x1111") == "alice"
    assert repo.get_wallet_by_pending_github_link("alice") == "0x1111"

    # Same wallet updating pending login is allowed
    repo.set_pending_github_link("0x1111", "alice_new")
    assert repo.get_pending_github_link("0x1111") == "alice_new"

    # Another wallet cannot take a pending login
    with pytest.raises(AccountConflict, match="alice_new is linked to another wallet"):
        repo.set_pending_github_link("0x2222", "alice_new")

    # Another wallet cannot take a login from an existing account
    repo.save_account(
        Account(
            address="0x3333",
            role="publisher",
            party_id="PUB-1",
            github_login="bob",
            created_at=datetime.now(UTC),
        )
    )
    with pytest.raises(AccountConflict, match="bob is linked to another wallet"):
        repo.set_pending_github_link("0x4444", "bob")

    # Saving account with a login already pending on another wallet is refused
    with pytest.raises(AccountConflict, match="alice_new is linked to another wallet"):
        repo.save_account(
            Account(
                address="0x5555",
                role="contributor",
                party_id="CON-1",
                github_login="alice_new",
                created_at=datetime.now(UTC),
            )
        )


def test_sql_repository_pending_github_links(tmp_path):
    from misthos.repositories.sql import SqlRepository
    db_path = tmp_path / "test.db"
    repo = SqlRepository(f"sqlite:///{db_path}")
    repo.migrate()

    repo.set_pending_github_link("0x1111", "alice")
    assert repo.get_pending_github_link("0x1111") == "alice"
    assert repo.get_wallet_by_pending_github_link("alice") == "0x1111"

    repo.set_pending_github_link("0x1111", "alice_new")
    assert repo.get_pending_github_link("0x1111") == "alice_new"

    with pytest.raises(AccountConflict, match="alice_new is linked to another wallet"):
        repo.set_pending_github_link("0x2222", "alice_new")

    repo.save_account(
        Account(
            address="0x3333",
            role="publisher",
            party_id="PUB-1",
            github_login="bob",
            created_at=datetime.now(UTC),
        )
    )
    with pytest.raises(AccountConflict, match="bob is linked to another wallet"):
        repo.set_pending_github_link("0x4444", "bob")

    with pytest.raises(AccountConflict, match="alice_new is linked to another wallet"):
        repo.save_account(
            Account(
                address="0x5555",
                role="contributor",
                party_id="CON-1",
                github_login="alice_new",
                created_at=datetime.now(UTC),
            )
        )


def test_sign_in_gate_flow():
    client = TestClient(app)
    wallet = Wallet(101)

    # Step 1: Wallet sign in
    nonce = client.post(f"{API}/auth/nonce").json()["nonce"]
    message = wallet.message(nonce)
    sig = wallet.sign(message)
    res = client.post(f"{API}/auth/verify", json={"message": message, "signature": sig})
    assert res.status_code == 200

    me = client.get(f"{API}/auth/me").json()
    assert me["address"].lower() == wallet.address.lower()
    assert me["account"] is None
    assert me["pending_github_login"] is None

    # Step 2: Link GitHub BEFORE choosing a role
    # First verify start_github_link does not return 401 "sign in and choose a role first"
    start_res = client.post(f"{API}/auth/github/start")
    # OAuth may not be configured in test env, but it must not refuse with 401 "sign in and choose a role first"
    assert start_res.status_code in (200, 503)

    # Simulate linking GitHub before role
    sim_res = client.post(f"{API}/auth/github/simulate", json={"login": "charlie-dev"})
    assert sim_res.status_code == 200, sim_res.text
    assert sim_res.json()["github_login"] == "charlie-dev"

    me = client.get(f"{API}/auth/me").json()
    assert me["pending_github_login"] == "charlie-dev"
    assert me["account"] is None

    # Another wallet cannot link the same login (conflict)
    client2 = TestClient(app)
    wallet2 = Wallet(102)
    n2 = client2.post(f"{API}/auth/nonce").json()["nonce"]
    m2 = wallet2.message(n2)
    client2.post(f"{API}/auth/verify", json={"message": m2, "signature": wallet2.sign(m2)})
    conflict = client2.post(f"{API}/auth/github/simulate", json={"login": "charlie-dev"})
    assert conflict.status_code == 409
    assert "linked to another wallet" in conflict.text

    # Step 3: Choose role and name party
    role_res = client.post(f"{API}/auth/role", json={"role": "contributor", "name": "Charlie"})
    assert role_res.status_code == 201, role_res.text
    account = role_res.json()
    assert account["role"] == "contributor"
    assert account["github_login"] == "charlie-dev"

    me = client.get(f"{API}/auth/me").json()
    assert me["account"]["github_login"] == "charlie-dev"
    assert me["pending_github_login"] is None

    # Second attempt on same wallet is refused with AccountExists (409)
    again = client.post(f"{API}/auth/role", json={"role": "publisher", "name": "Charlie Org"})
    assert again.status_code == 409


def test_circle_wallet_sign_in_door():
    client = TestClient(app)
    user_id = "test-circle-visitor-1"

    # Step 1 with Circle wallet: get session
    session_res = client.post(f"{API}/wallets/circle/session", json={"user_id": user_id})
    assert session_res.status_code == 200, session_res.text
    session_data = session_res.json()
    circle_address = session_data["wallet"]["address"]
    assert circle_address == simulated_identity_address(user_id)

    # Get SIWE nonce
    nonce = client.post(f"{API}/auth/nonce").json()["nonce"]

    domain = settings.public_url.split("://")[-1].rstrip("/")
    issued_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    message = (
        f"{domain} wants you to sign in with your Ethereum account:\n"
        f"{circle_address}\n\n"
        f"Sign in to Misthos. This costs nothing and moves no money.\n\n"
        f"URI: {settings.public_url}\n"
        f"Version: 1\n"
        f"Chain ID: 5042002\n"
        f"Nonce: {nonce}\n"
        f"Issued At: {issued_at}"
    )

    # Sign through Circle identity sign endpoint
    sign_res = client.post(f"{API}/wallets/circle/sign", json={"user_id": user_id, "message": message})
    assert sign_res.status_code == 200, sign_res.text
    sig = sign_res.json()["signature"]
    assert sig is not None

    # Verify signature with SIWE verify
    verified = client.post(f"{API}/auth/verify", json={"message": message, "signature": sig})
    assert verified.status_code == 200, verified.text
    assert verified.json()["address"].lower() == circle_address.lower()

    # Verify session cookie was set and /me works
    me = client.get(f"{API}/auth/me").json()
    assert me["address"].lower() == circle_address.lower()

    # Refuse invalid signature
    bad_sig = "0x" + "00" * 65
    bad_verify = client.post(f"{API}/auth/verify", json={"message": message, "signature": bad_sig})
    assert bad_verify.status_code in (400, 401)
