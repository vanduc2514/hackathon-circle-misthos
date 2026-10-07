"""Sign-in with a wallet (#70) and linking a GitHub account (#80)."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse

from misthos.api.session import SIGNED_IN, signed_in_address
from misthos.auth import sessions, siwe
from misthos.config import settings
from misthos.repositories import AccountConflict
from misthos.schemas import (
    Account,
    GitHubLinkStart,
    MeOut,
    NonceOut,
    RoleRequest,
    SessionOut,
    SignInRequest,
    SimulatedLink,
)
from misthos.services.github import GitHubError
from misthos.services.github.oauth import GitHubOAuth
from misthos.store import AccountExists, store

router = APIRouter(prefix="/auth", tags=["auth"])

NONCE_TTL = timedelta(minutes=10)
STATEMENT = "Sign in to Misthos. This costs nothing and moves no money."


def domain() -> str:
    return urlsplit(settings.public_url).netloc


def callback_url() -> str:
    return f"{settings.public_url.rstrip('/')}/api/v1/auth/github/callback"


def oauth() -> GitHubOAuth | None:
    if not (settings.github_oauth_client_id and settings.github_oauth_client_secret):
        return None
    return GitHubOAuth(settings.github_oauth_client_id, settings.github_oauth_client_secret)


@router.post("/nonce", response_model=NonceOut)
async def nonce() -> NonceOut:
    """A one-time nonce, and what the sign-in message must say around it."""
    value = secrets.token_hex(12)
    await run_in_threadpool(store.coordinator.put, f"siwe:{value}", "issued", NONCE_TTL)
    return NonceOut(
        nonce=value,
        domain=domain(),
        uri=settings.public_url,
        chain_id=settings.chain_id,
        statement=STATEMENT,
    )


@router.post("/verify", response_model=SessionOut)
async def verify(payload: SignInRequest, response: Response) -> SessionOut:
    """Check the signed message, spend its nonce, and start a session."""
    try:
        claimed = siwe.parse(payload.message)
    except siwe.SignInRefused as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    slot = f"siwe:{claimed.nonce}"
    if await run_in_threadpool(store.coordinator.get, slot) is None:
        raise HTTPException(status_code=401, detail="the nonce is unknown, used or expired")
    await run_in_threadpool(store.coordinator.delete, slot)  # one use, even if refused
    try:
        signed = siwe.verify(
            payload.message,
            payload.signature,
            domain=domain(),
            nonce=claimed.nonce,
            now=datetime.now(UTC),
        )
    except siwe.SignInRefused as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    address = signed.address.lower()
    token = sessions.issue(address)
    response.set_cookie(
        sessions.COOKIE,
        token,
        max_age=int(sessions.LIFETIME.total_seconds()),
        httponly=True,
        samesite="lax",
        # Sent only over HTTPS once the app is served there.
        secure=settings.public_url.startswith("https://"),
        path="/",
    )
    account = await run_in_threadpool(store.repo.get_account, address)
    return SessionOut(address=address, token=token, account=account)


@router.post("/logout", status_code=204)
async def logout(response: Response) -> Response:
    response.delete_cookie(sessions.COOKIE, path="/")
    response.status_code = 204
    return response


@router.get("/me", response_model=MeOut)
async def me(request: Request) -> MeOut:
    address = await signed_in_address(request)
    if address is None:
        raise HTTPException(status_code=401, detail="not signed in")
    account = await run_in_threadpool(store.repo.get_account, address)
    return MeOut(address=address, account=account)


@router.post("/role", response_model=Account, status_code=201)
async def choose_role(payload: RoleRequest, request: Request) -> Account:
    """The first sign-in's one choice: publisher or contributor."""
    address = await signed_in_address(request)
    if address is None:
        raise HTTPException(status_code=401, detail="sign in first")
    try:
        return await run_in_threadpool(
            lambda: store.create_account(
                address, payload.role, payload.name, budget_usdc=payload.budget_usdc
            )
        )
    except AccountExists as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/github/start", response_model=GitHubLinkStart)
async def start_github_link(account: Account | None = SIGNED_IN) -> GitHubLinkStart:
    """Where to send the user to approve linking their GitHub account."""
    if account is None:
        raise HTTPException(status_code=401, detail="sign in and choose a role first")
    client = oauth()
    if client is None:
        raise HTTPException(status_code=503, detail="GitHub OAuth is not configured")
    state = secrets.token_urlsafe(24)
    await run_in_threadpool(store.coordinator.put, f"gh-link:{state}", account.address, NONCE_TTL)
    return GitHubLinkStart(authorize_url=client.authorize_url(state, callback_url()))


@router.get("/github/callback", include_in_schema=False)
async def github_callback(code: str, state: str) -> RedirectResponse:
    slot = f"gh-link:{state}"
    address = await run_in_threadpool(store.coordinator.get, slot)
    if address is None:
        raise HTTPException(status_code=400, detail="the link request is unknown or expired")
    await run_in_threadpool(store.coordinator.delete, slot)
    client = oauth()
    if client is None:
        raise HTTPException(status_code=503, detail="GitHub OAuth is not configured")
    try:
        login = await run_in_threadpool(client.login_for, code, callback_url())
        await run_in_threadpool(store.link_github, address, login)
    except GitHubError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except AccountConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(f"{settings.public_url.rstrip('/')}/account?linked=github", 303)


@router.post("/github/simulate", response_model=Account)
async def simulate_github_link(
    payload: SimulatedLink, account: Account | None = SIGNED_IN
) -> Account:
    """Link a GitHub login without OAuth, for the simulation's demo only."""
    if not settings.simulated:
        raise HTTPException(status_code=403, detail="link through GitHub")
    if account is None:
        raise HTTPException(status_code=401, detail="sign in and choose a role first")
    try:
        return await run_in_threadpool(store.link_github, account.address, payload.login)
    except AccountConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
