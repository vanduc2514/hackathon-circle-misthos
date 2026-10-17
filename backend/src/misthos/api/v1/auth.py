"""Sign-in with a wallet (#70), a Circle wallet as a second way in, and linking a
GitHub account (#80) -- the three steps of the sign-in gate (#181).

The gate asks for a wallet, then a GitHub login, then a side. The side is what creates
the account, so a login linked before it is held against the wallet until then, and
the account adopts it. Verification is the same for both wallets: a Circle wallet
signs the message `/auth/nonce` describes, and `/auth/verify` recovers its address
exactly as it recovers MetaMask's.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse

from misthos.api.guards import limit_signin
from misthos.api.session import signed_in_address
from misthos.auth import sessions, siwe
from misthos.config import settings
from misthos.repositories import AccountConflict
from misthos.schemas import (
    Account,
    CircleSignature,
    CircleSignIn,
    CircleSignInSession,
    CircleSignInWallet,
    CircleSignRequest,
    GitHubLinkStart,
    MeOut,
    NonceOut,
    RoleRequest,
    SessionOut,
    SignInRequest,
)
from misthos.services.coordination import Busy
from misthos.services.github import GitHubError
from misthos.services.github.oauth import GitHubOAuth
from misthos.services.wallets import Wallets, WalletUnavailable
from misthos.store import AccountExists, store

router = APIRouter(prefix="/auth", tags=["auth"])

NONCE_TTL = timedelta(minutes=10)
STATEMENT = "Sign in to Misthos. This costs nothing and moves no money."


def domain() -> str:
    return urlsplit(settings.public_url).netloc


def callback_url() -> str:
    return f"{settings.public_url.rstrip('/')}/api/v1/auth/github/callback"


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def oauth_configured() -> bool:
    """Whether a real GitHub account can be linked: the OAuth App's id and secret are
    both set. Read again on every call, so a test can configure it."""
    return bool(settings.github_oauth_client_id and settings.github_oauth_client_secret)


def oauth() -> GitHubOAuth | None:
    if not oauth_configured():
        return None
    return GitHubOAuth(settings.github_oauth_client_id, settings.github_oauth_client_secret)


def not_configured() -> HTTPException:
    """What an operator sets for linking to work, said where linking was asked for."""
    return HTTPException(
        status_code=503,
        detail="GitHub linking is not set up on this server: create a GitHub OAuth App "
        f"with the callback {callback_url()}, set MISTHOS_GITHUB_OAUTH_CLIENT_ID and "
        "MISTHOS_GITHUB_OAUTH_CLIENT_SECRET, and restart the API",
    )


@router.post("/nonce", response_model=NonceOut, dependencies=[limit_signin])
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


@router.post("/verify", response_model=SessionOut, dependencies=[limit_signin])
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
    return await _me(address)


async def _me(address: str) -> MeOut:
    account = await run_in_threadpool(store.repo.get_account, address)
    pending = (
        None
        if account is not None
        else await run_in_threadpool(store.pending_github_login, address)
    )
    return MeOut(address=address, account=account, pending_github_login=pending)


@router.post(
    "/role",
    response_model=Account,
    status_code=201,
    responses={409: {"description": "This wallet already has a side; a side is chosen once"}},
)
async def choose_role(payload: RoleRequest, request: Request) -> Account:
    """The gate's last step: publisher or contributor, chosen once.

    This creates the account. A GitHub login linked in the step before is adopted onto
    it, so the account answers with `github_login` set.
    """
    address = await signed_in_address(request)
    if address is None:
        raise HTTPException(status_code=401, detail="sign in first")
    try:
        return await run_in_threadpool(
            lambda: store.create_account(
                address, payload.role, payload.name, budget_usdc=payload.budget_usdc
            )
        )
    except (AccountExists, AccountConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Busy as exc:
        raise HTTPException(status_code=409, detail=str(exc), headers={"Retry-After": "1"}) from exc


LINK_SLOT = "gh-link"
"""Where a link's state is kept between starting it and GitHub returning it."""


async def mint_link_state(address: str) -> str:
    """A single-use state for a GitHub link.

    The state proves the person finishing the link is the one who started it: without
    it a caller could start a link with their own wallet and have someone else's GitHub
    login bound to it, and `github_login` is what gates publishing and claiming.
    """
    state = secrets.token_urlsafe(24)
    await run_in_threadpool(store.coordinator.put, f"{LINK_SLOT}:{state}", address, NONCE_TTL)
    return state


@router.post(
    "/github/start",
    response_model=GitHubLinkStart,
    responses={
        401: {"description": "No wallet is signed in"},
        409: {"description": "The browser is on another address than GitHub returns to"},
        503: {"description": "No GitHub OAuth App is configured; the detail says what to set"},
    },
)
async def start_github_link(request: Request) -> GitHubLinkStart:
    """Where to send the user to approve linking their GitHub account.

    A signed-in wallet is enough: the gate links GitHub before the side is chosen
    (#181), and the login is held for the wallet until choosing the side adopts it.
    """
    address = await signed_in_address(request)
    if address is None:
        raise HTTPException(status_code=401, detail="sign in with a wallet first")
    client = oauth()
    if client is None:
        raise not_configured()
    # GitHub returns to the public URL, and the session cookie belongs to the address
    # the browser is on. Opened at 127.0.0.1:5173 with the public URL on localhost, the
    # callback would arrive without the session and be refused, after the user had
    # approved on GitHub. Said now instead, while it can still be put right.
    public = _origin(settings.public_url)
    origin = request.headers.get("origin")
    if origin and _origin(origin) != public:
        raise HTTPException(
            status_code=409,
            detail=f"GitHub sends you back to {public}, and this browser is on {origin}; "
            f"open the app at {public} and link from there",
        )
    state = await mint_link_state(address)
    return GitHubLinkStart(authorize_url=client.authorize_url(state, callback_url()))


@router.get("/github/callback", include_in_schema=False)
async def github_callback(
    request: Request, state: str, code: str | None = None, error: str | None = None
) -> RedirectResponse:
    """Finish a link the *same* signed-in wallet started.

    The state is server-side, so it proves the flow began here; it does not prove who
    is finishing it. Without the session check any caller could start a link with
    their own wallet, send the authorize URL to someone else, and have that person's
    GitHub login bound to the attacker's account -- and `github_login` is what gates
    publishing, claiming and submitting, so the attacker would then be paid for work
    the victim opened. The account is therefore taken from the session and must match
    the address the state was issued to.    """
    if code is None or error is not None:
        # The account holder said no on GitHub, which sends `error` and no code. Back
        # to the gate, which offers the step again; nothing was linked or held.
        return RedirectResponse(back_to_the_gate(github="cancelled"), 303)
    address = await signed_in_address(request)
    if address is None:
        raise HTTPException(status_code=401, detail="sign in to finish linking GitHub")
    slot = f"{LINK_SLOT}:{state}"
    started_by = await run_in_threadpool(store.coordinator.get, slot)
    if started_by is None:
        raise HTTPException(status_code=400, detail="the link request is unknown or expired")
    if started_by != address:
        # Spend the state either way: it is single-use, and a mismatch is not a
        # request to leave it lying around for a second attempt.
        await run_in_threadpool(store.coordinator.delete, slot)
        raise HTTPException(status_code=403, detail="this link was started by another wallet")
    await run_in_threadpool(store.coordinator.delete, slot)
    client = oauth()
    if client is None:
        raise not_configured()
    try:
        login = await run_in_threadpool(client.login_for, code, callback_url())
    except GitHubError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    try:
        await run_in_threadpool(store.hold_github_login, address, login)
    except AccountConflict:
        # Back to the gate, which says the login is another wallet's. Nothing was held.
        return RedirectResponse(back_to_the_gate(github="taken"), 303)
    except Busy:
        # Someone was linking the same login that instant; the gate asks to try again.
        return RedirectResponse(back_to_the_gate(github="busy"), 303)
    return RedirectResponse(back_to_the_gate(), 303)


def back_to_the_gate(**reason: str) -> str:
    """Where GitHub's return lands: the landing page, with the gate open where it was."""
    query = urlencode({"signin": "continue", **reason})
    return f"{settings.public_url.rstrip('/')}/?{query}"


# ---------------------------------------------------------------- Circle, the second door


def circle_sign_in_configured() -> bool:
    """Whether a Circle wallet can be the way in here: the edge that holds Circle's SDK
    must be configured."""
    return bool(settings.edge_core_token)


def _circle_or_refuse() -> Wallets:
    if not circle_sign_in_configured():
        raise HTTPException(
            status_code=503,
            detail="signing in with a Circle wallet is not set up on this server: run the "
            "edge service with CIRCLE_API_KEY and CIRCLE_APP_ID, set MISTHOS_EDGE_URL and "
            "MISTHOS_EDGE_CORE_TOKEN, and restart the API",
        )
    return Wallets(settings)


CIRCLE_RESPONSES: dict[int | str, dict[str, str]] = {
    502: {"description": "The edge or Circle could not answer"},
    503: {"description": "No edge is configured for Circle wallets; the detail says what to set"},
}


@router.post(
    "/circle/session",
    response_model=CircleSignInSession,
    dependencies=[limit_signin],
    responses=CIRCLE_RESPONSES,
)
async def circle_session(payload: CircleSignIn) -> CircleSignInSession:
    """Start or continue signing in with a Circle wallet, before any account exists.

    Not keyed by a party, because there is none yet: the browser's own sign-in id names
    the Circle user. While the wallet does not exist the answer carries the challenge
    that sets its PIN and creates it in Circle's frame; once it does, its address, which
    is the address the sign-in message names. Nothing is written here.
    """
    wallets = _circle_or_refuse()
    try:
        session = await wallets.sign_in_session(payload.sign_in_id)
    except WalletUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return CircleSignInSession(
        app_id=session.app_id,
        user_token=session.user_token,
        encryption_key=session.encryption_key,
        challenge_id=session.challenge_id,
        address=session.address,
    )


@router.post(
    "/circle/wallet",
    response_model=CircleSignInWallet,
    dependencies=[limit_signin],
    responses=CIRCLE_RESPONSES,
)
async def circle_wallet(payload: CircleSignIn) -> CircleSignInWallet:
    """The Circle wallet's address, once it exists; null while Circle is creating it.

    Only reads. The browser asks this after Circle's frame closes, rather than starting
    the session again, which would ask Circle for another PIN and wallet challenge.
    """
    wallets = _circle_or_refuse()
    try:
        found = await wallets.sign_in_wallet(payload.sign_in_id)
    except WalletUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return CircleSignInWallet(address=found.address)


@router.post(
    "/circle/sign",
    response_model=CircleSignature,
    dependencies=[limit_signin],
    responses={
        **CIRCLE_RESPONSES,
        400: {"description": "Not a sign-in message for this site, this wallet and a live nonce"},
        409: {"description": "The Circle wallet does not exist yet"},
    },
)
async def circle_sign(payload: CircleSignRequest) -> CircleSignature:
    """Have a Circle wallet sign the sign-in message: a `SIGN_MESSAGE` challenge.

    Only a sign-in message is signed: one for this site, with a nonce `/auth/nonce`
    issued and nobody has spent, naming this Circle wallet's own address. The nonce is
    not spent here; `/auth/verify` spends it and checks the signature exactly as it
    checks MetaMask's, so this route opens no way around it.
    """
    wallets = _circle_or_refuse()
    try:
        claimed = siwe.parse(payload.message)
    except siwe.SignInRefused as exc:
        raise HTTPException(status_code=400, detail=f"not a sign-in message: {exc}") from exc
    if claimed.domain != domain():
        raise HTTPException(status_code=400, detail=f"the message is for {claimed.domain}")
    if await run_in_threadpool(store.coordinator.get, f"siwe:{claimed.nonce}") is None:
        raise HTTPException(status_code=400, detail="the nonce is unknown, used or expired")
    try:
        session = await wallets.sign_in_wallet(payload.sign_in_id)
        if session.address is None:
            raise HTTPException(
                status_code=409, detail="the Circle wallet has not been created yet"
            )
        if claimed.address.lower() != session.address.lower():
            raise HTTPException(
                status_code=400, detail="the message names another wallet than this Circle wallet"
            )
        signed = await wallets.sign_in_signature(payload.sign_in_id, payload.message)
    except WalletUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return CircleSignature(
        app_id=signed.app_id,
        user_token=signed.user_token,
        encryption_key=signed.encryption_key,
        challenge_id=signed.challenge_id,
        signature=signed.signature,
    )
