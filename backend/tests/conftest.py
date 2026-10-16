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
os.environ["MISTHOS_ATTESTOR_SECRET_REF"] = "test-attestor-ref"
os.environ["MISTHOS_OWNER_SECRET_REF"] = "test-owner-ref"

from misthos.config import settings

settings.attestor_secret_ref = "test-attestor-ref"
settings.owner_secret_ref = "test-owner-ref"
settings.platform_wallet = "0x" + "1" * 40

# Test seam: inject test doubles into services modules for test suites
import misthos.services.billing
import misthos.services.chain
import misthos.services.github
import misthos.services.wallets
import misthos.store
from tests.fakes.billing import SimulatedRail
from tests.fakes.chain import SimulatedChain
from tests.fakes.github import SimulatedGitHub
from tests.fakes.wallets import simulated_address
from tests.fakes.seed import reset_store_for_tests

setattr(misthos.services.chain, "SimulatedChain", SimulatedChain)
setattr(misthos.services.github, "SimulatedGitHub", SimulatedGitHub)
setattr(misthos.services.billing, "SimulatedRail", SimulatedRail)
setattr(misthos.services.wallets, "simulated_address", simulated_address)
setattr(misthos.store, "NotSimulated", type("NotSimulated", (Exception,), {}))
setattr(misthos.store, "SimulatedChain", SimulatedChain)
setattr(misthos.store, "SimulatedGitHub", SimulatedGitHub)
setattr(misthos.store, "SimulatedRail", SimulatedRail)
setattr(misthos.store.Store, "reset", reset_store_for_tests)

from tests.fakes.lifecycle import advance, approve_and_accept, demo_pull_request, demo_merge
setattr(misthos.store.Store, "advance", advance)
setattr(misthos.store.Store, "approve_and_accept", approve_and_accept)
setattr(misthos.store.Store, "demo_pull_request", demo_pull_request)
setattr(misthos.store.Store, "demo_merge", demo_merge)
from tests.fakes.seed import _fabricate_files
setattr(misthos.store.Store, "_fabricate_files", lambda self, rec: _fabricate_files(self, rec))

# Wire test seam into store
def _make_chain(*args, **kwargs):
    engine = kwargs.get("engine") or (args[1] if len(args) > 1 else None)
    return SimulatedChain(engine=engine)

misthos.store.build_chain = _make_chain
misthos.store.build_github = lambda *args, **kwargs: SimulatedGitHub()
misthos.store.build_rail = lambda: SimulatedRail()

misthos.store.store.chain = SimulatedChain()
misthos.store.store.github = SimulatedGitHub()
misthos.store.store.rail = SimulatedRail()

# Test seam: wire test-only action endpoints into FastAPI app for integration tests
from fastapi import Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from misthos.api.guards import idempotent, limit_actions
from misthos.domain.compliance import ComplianceRefusal
from misthos.domain.policy import PolicyRefusal
from misthos.main import app
from misthos.repositories import StaleIssue
from misthos.services.chain import ChainRevert
from misthos.services.coordination import Busy
from misthos.api.session import SIGNED_IN
import misthos.api.v1.wallets
from tests.fakes.wallets import SimulatedWallets

setattr(misthos.api.v1.wallets, "Wallets", SimulatedWallets)

from misthos.services.review import ReviewFailed

@app.post("/api/v1/issues/{issue_id}/advance", dependencies=[limit_actions])
async def _test_advance(
    issue_id: str,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    account: Account | None = SIGNED_IN,
):
    if not account:
        raise HTTPException(status_code=401, detail="sign in first")
    def run():
        try:
            rec = misthos.store.store.advance(issue_id)
            return misthos.store.store.to_out(rec)
        except (Busy, StaleIssue) as exc:
            raise HTTPException(status_code=409, detail=str(exc), headers={"Retry-After": "1"}) from exc
        except ReviewFailed as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except ChainRevert as exc:
            raise HTTPException(status_code=409, detail=f"escrow refused: {exc}") from exc
        except (PolicyRefusal, ComplianceRefusal) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except Exception as exc:
            if "already paid" in str(exc) or "IllegalTransition" in type(exc).__name__:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            raise

    return await idempotent(request, idempotency_key, lambda: run_in_threadpool(run))

@app.post("/api/v1/issues/{issue_id}/complete")
async def _test_complete(issue_id: str):
    def run():
        rec = misthos.store.store.approve_and_accept(issue_id)
        return misthos.store.store.to_out(rec)
    return await run_in_threadpool(run)

from misthos.api.session import SIGNED_IN
from misthos.domain.money import Usdc
from misthos.schemas import Account
from pydantic import BaseModel

class SimulatedLink(BaseModel):
    login: str

@app.post("/api/v1/auth/github/simulate", response_model=Account)
async def _test_simulate_github_link(
    payload: SimulatedLink, account: Account | None = SIGNED_IN
) -> Account:
    if account is None:
        raise HTTPException(status_code=401, detail="sign in and choose a role first")
    try:
        return await run_in_threadpool(misthos.store.store.link_github, account.address, payload.login)
    except Exception as exc:
        if "already linked" in str(exc) or "AccountConflict" in type(exc).__name__:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        raise

@app.post("/api/v1/demo/issues/{issue_id}/pull-request")
async def _test_demo_pr(issue_id: str, account: Account | None = SIGNED_IN):
    if not account:
        raise HTTPException(status_code=401, detail="sign in first")
    rec = misthos.store.store.get(issue_id)
    if not rec:
        raise HTTPException(status_code=404, detail="not found")
    if account.party_id != rec.contributor_id:
        raise HTTPException(status_code=403, detail="only claimant can open PR")
    pr = misthos.store.store.demo_pull_request(issue_id)
    return {"pr_number": pr.number, "author": pr.author}

@app.post("/api/v1/demo/issues/{issue_id}/merge")
async def _test_demo_merge(issue_id: str, account: Account | None = SIGNED_IN):
    if not account:
        raise HTTPException(status_code=401, detail="sign in first")
    rec = misthos.store.store.get(issue_id)
    if not rec:
        raise HTTPException(status_code=404, detail="not found")
    if account.party_id != rec.publisher_id:
        raise HTTPException(status_code=403, detail="only publisher can merge")
    merged = misthos.store.store.demo_merge(issue_id)
    return misthos.store.store.to_out(merged)

@app.post("/api/v1/demo/publishers/{publisher_id}/subscription/pay")
async def _test_demo_sub_pay(publisher_id: str, account: Account | None = SIGNED_IN):
    sub = misthos.store.store.subscription(publisher_id)
    if not sub.pending:
        raise HTTPException(status_code=409, detail="no pending subscription")
    tx = misthos.store.store.rail.send(
        payer=sub.pending.payer,
        payee=sub.pending.pay_to,
        amount=Usdc.from_decimal(sub.pending.amount_usdc),
    )
    return misthos.store.store.confirm_payment(publisher_id, tx)






