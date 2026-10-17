"""Keep the suite hermetic: a developer's `.env` must not change what the tests prove.

`Settings` reads `backend/.env`, which is where a local run keeps its API keys. The
integration tests are written against the rule reviewer, whose verdicts the file list
decides deterministically. A key in that file would quietly send every verdict to the
model instead, so a machine that is merely configured — not broken — would see the
suite fail on a network response it never meant to depend on.

The GitHub App is the same shape and worse: `build_github` returns a live `GitHubApp`
as soon as the id and the key are both set, so a configured machine would read a real
repository, and a publisher-shaped test would open a real issue on GitHub.

The environment wins over the `.env` file, and this module is imported before any test
module, so clearing both here keeps the network out of the run. Tests that want the
model, or the App, construct one themselves.
"""

from __future__ import annotations

import os

os.environ["MISTHOS_ANTHROPIC_API_KEY"] = ""
os.environ["MISTHOS_GITHUB_APP_ID"] = ""
os.environ["MISTHOS_GITHUB_APP_PRIVATE_KEY"] = ""
os.environ["MISTHOS_GITHUB_WEBHOOK_SECRET"] = "test-webhook-secret"
os.environ["MISTHOS_ATTESTOR_SECRET_REF"] = "test-attestor"
os.environ["MISTHOS_OWNER_SECRET_REF"] = "test-owner"

import pytest  # noqa: E402
from fastapi import Request  # noqa: E402

from misthos.main import app  # noqa: E402
from misthos.store import store  # noqa: E402
from tests.support.fake_chain import FakeChain  # noqa: E402
from tests.support.fake_compliance import FakeIdentity, FakeScreening  # noqa: E402
from tests.support.fake_github import FakeGitHub  # noqa: E402
from tests.support.fake_wallets import FakeWallets  # noqa: E402
from tests.support.seeded import SeededStore  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_store():
    """Every test starts from the demo fixtures, with the boundaries faked.

    This is the seam: the application always builds its live store; the suite
    installs one built with in-memory gateways instead. No runtime switch is read.
    """
    seeded = SeededStore(
        chain=FakeChain(),
        github=FakeGitHub(),
        screening=FakeScreening(),
        identity=FakeIdentity(),
    )
    # Redis keeps rate-limit counters and one-time nonces across tests; a fresh store
    # in memory does not. Clear them either way so one test cannot throttle the next.
    seeded.coordinator.reset()
    store.install(seeded)
    yield


@pytest.fixture(autouse=True)
def fake_wallets(monkeypatch: pytest.MonkeyPatch):
    """The wallet routes talk to an edge; the suite supplies one in-process."""
    from misthos.api.v1 import wallets as wallet_api

    monkeypatch.setattr(wallet_api, "Wallets", lambda _cfg: FakeWallets())
    yield


@pytest.fixture(autouse=True)
def auto_signed_in(request, fresh_store, monkeypatch):
    """Act as the party a route is about, unless the test is proving the guard.

    The application's dependency is overridden, not bypassed inside it: the real
    `require_owner` and `require_role` still run, against an account that belongs to
    the party named by the path. A test marked `real_auth` keeps the real session.
    """
    if request.node.get_closest_marker("real_auth"):
        yield
        return

    from misthos.api import session as session_api
    from misthos.schemas import Account

    async def acting_account(req: Request):
        params = req.path_params
        party_id = params.get("party_id")
        role = params.get("party")
        if party_id and role:
            return Account(
                address="0x" + "0" * 40,
                role=role,
                party_id=party_id,
                github_login="test-login",
                created_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
            )

        if params.get("publisher_id"):
            party_id = params["publisher_id"]
            role = "publisher"
        elif params.get("contributor_id"):
            party_id = params["contributor_id"]
            role = "contributor"
        elif params.get("issue_id"):
            rec = store.get(params["issue_id"])
            if rec is None:
                return None
            if req.url.path.endswith(("/claim", "/submit", "/review")):
                party_id = rec.contributor_id or "CON-1"
                role = "contributor"
            else:
                party_id = rec.publisher_id
                role = "publisher"
        elif req.method == "POST" and req.url.path == "/api/v1/issues":
            import json

            body = json.loads(await req.body() or b"{}")
            party_id = body.get("publisher_id") or "PUB-1"
            role = "publisher"
        else:
            return None

        from tests.support.accounts import PARTY_SEEDS, account_for

        if party_id in PARTY_SEEDS:
            found = account_for(party_id)
            return found.model_copy(update={"role": role}) if role else found
        return Account(
            address="0x" + "0" * 40,
            role=role,
            party_id=party_id,
            github_login="test-login",
            created_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        )

    app.dependency_overrides[session_api.current_account] = acting_account
    yield
    app.dependency_overrides.pop(session_api.current_account, None)
