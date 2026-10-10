"""Signing in, with GitHub or with a wallet (#70, #131); connecting a wallet later (#131);
and linking a GitHub account to a wallet that signed in first (#80).

**Sign in with GitHub** is the main way in. The browser is sent to GitHub with a
state, and the state is kept twice: in the coordinator, which proves the flow began
here, and in a short-lived HttpOnly cookie, which proves it began in *this* browser.
The callback refuses a state whose cookie does not match, and issues no session.
Without that, anyone could start a sign-in, approve it on GitHub as themselves, and
send the callback URL to a victim, whose browser would then be signed in to the
attacker's account and do its work for it (login CSRF).

**Sign in with a wallet** stays as the second way: a Sign-In with Ethereum message
over a one-time nonce. That account links GitHub afterwards, before it publishes or
claims, through the same OAuth App and the same callback; the callback tells the two
apart by which slot the state is kept in.

Either way the account connects a wallet when money is about to move, by signing one
message over a nonce, which proves the wallet is theirs.

**Sign in with single sign-on** is for the staff of an Enterprise organisation (#53).
A work e-mail's domain finds the organisation's identity provider, and the flow is
OpenID Connect's authorization code with PKCE and a nonce. The state is kept twice, as
for GitHub, and for the same reason. The session acts as the organisation's account
and names the person, by the verified address the provider asserted.

Nothing here logs an OAuth code, a token or the client secret.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse

from misthos.api.guards import limit_signin
from misthos.api.session import (
    SESSION,
    SIGNED_IN,
    account_of,
    acting_account,
    signed_in,
    sso_unmet,
)
from misthos.auth import sessions, siwe
from misthos.auth.sessions import Identity
from misthos.config import settings
from misthos.domain.sso import SsoRefusal, admitted, domain_of
from misthos.repositories import AccountConflict
from misthos.schemas import (
    Account,
    GitHubLinkStart,
    GitHubSignInStart,
    MeOut,
    NonceOut,
    RoleRequest,
    SessionOut,
    SignInRequest,
    SimulatedLink,
    SimulatedSignIn,
    SsoConnection,
    SsoSignInRequest,
    SsoSignInStart,
    WalletConnectRequest,
)
from misthos.services.attestor import AttestorKeyError, secret_store
from misthos.services.github import GitHubError
from misthos.services.github.oauth import GitHubOAuth, simulated_user
from misthos.services.sso import OidcClient, SsoError, pkce
from misthos.store import AccountExists, NoAccountYet, store

router = APIRouter(prefix="/auth", tags=["auth"])

NONCE_TTL = timedelta(minutes=10)
STATEMENT = "Sign in to Misthos. This costs nothing and moves no money."

# The state of a GitHub sign-in this browser started. Sent back only to the two routes
# that need it, and gone once the sign-in finishes or the state expires.
STATE_COOKIE = "misthos_github_state"
STATE_COOKIE_PATH = "/api/v1/auth/github"

_SIGNIN = "gh-signin:"
_LINK = "gh-link:"

SSO_STATE_COOKIE = "misthos_sso_state"
SSO_STATE_COOKIE_PATH = "/api/v1/auth/sso"
_SSO = "sso:"
# What an identity provider may put in `error` that is worth repeating to the user.
_PROVIDER_ERROR = re.compile(r"^[a-z_]{1,64}$")

R401 = {"description": "Not signed in, or the signature does not prove the wallet"}
R409_ORIGIN = {
    "description": "The browser is on another address than GitHub returns to; the detail "
    "says which address to open"
}
R503 = {"description": "No GitHub OAuth App is configured; the detail says what to set"}
R429 = {"description": "Too many sign-in attempts from this client in a minute"}


def domain() -> str:
    return urlsplit(settings.public_url).netloc


def callback_url() -> str:
    return f"{settings.public_url.rstrip('/')}/api/v1/auth/github/callback"


def sso_callback_url() -> str:
    """The redirect URI an organisation registers with its identity provider."""
    return f"{settings.public_url.rstrip('/')}/api/v1/auth/sso/callback"


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def oauth_configured() -> bool:
    """Whether a real GitHub account can sign in or be linked: the OAuth App's id and
    secret are both set. Read again on every call, so a test can configure it."""
    return bool(settings.github_oauth_client_id and settings.github_oauth_client_secret)


def oauth() -> GitHubOAuth | None:
    if not oauth_configured():
        return None
    return GitHubOAuth(settings.github_oauth_client_id, settings.github_oauth_client_secret)


def not_configured() -> HTTPException:
    """What an operator sets for GitHub to work, said where it was asked for."""
    return HTTPException(
        status_code=503,
        detail="GitHub sign-in and linking are not set up on this server: create a GitHub "
        f"OAuth App with the callback {callback_url()}, set MISTHOS_GITHUB_OAUTH_CLIENT_ID "
        "and MISTHOS_GITHUB_OAUTH_CLIENT_SECRET, and restart the API",
    )


def _secure() -> bool:
    # Cookies are sent only over HTTPS once the app is served there; a Secure cookie on
    # http://localhost would be dropped.
    return settings.public_url.startswith("https://")


def _refuse_another_origin(request: Request, what: str) -> None:
    """GitHub returns to the public URL, and the cookies belong to the address the
    browser is on. Opened at 127.0.0.1:5173 with the public URL on localhost, the
    callback would arrive without them and be refused, after the user had approved on
    GitHub (#128). Said now instead, while it can still be put right."""
    public = _origin(settings.public_url)
    origin = request.headers.get("origin")
    if origin and _origin(origin) != public:
        raise HTTPException(
            status_code=409,
            detail=f"GitHub sends you back to {public}, and this browser is on {origin}; "
            f"open the app at {public} and {what} from there",
        )


def _start_session(response: Response, identity: Identity) -> str:
    token = sessions.issue(identity)
    response.set_cookie(
        sessions.COOKIE,
        token,
        max_age=int(sessions.LIFETIME.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=_secure(),
        path="/",
    )
    return token


def _me(identity: Identity) -> MeOut:
    """The session and its account, from the store. Runs in the threadpool."""
    account = account_of(identity)
    wallet = identity.method == "wallet"
    github = identity.method == "github"
    return MeOut(
        method=identity.method,
        address=identity.address if wallet else (account.address if account else None),
        github_id=identity.github_id if github else (account.github_id if account else None),
        github_login=(account.github_login if account else None) or identity.github_login,
        account=account,
        wallet=store.wallet_of(account) if account else None,
        sso_email=identity.email,
        sso_required=sso_unmet(identity, account),
    )


async def _session_out(response: Response, identity: Identity) -> SessionOut:
    token = _start_session(response, identity)
    me = await run_in_threadpool(_me, identity)
    return SessionOut(**me.model_dump(), token=token)


def _take(slot: str) -> str | None:
    """What a one-time slot holds, for the first caller only, and the slot spent.

    Reading and then deleting lets two requests that arrive together both read it, so
    the first to mark it spent wins; the mark is atomic in Redis and in this process,
    and outlives the slot. Runs in the threadpool.
    """
    found = store.coordinator.get(slot)
    if found is None or not store.coordinator.reserve(f"spent:{slot}", "spent", NONCE_TTL):
        return None
    store.coordinator.delete(slot)
    return found


async def _proven_address(message: str, signature: str) -> str:
    """The wallet a signed Sign-In with Ethereum message proves, lowercase.

    The nonce is spent before the signature is checked, so a message is good for one
    attempt whatever its outcome, and the same nonce cannot both sign in and connect.
    """
    try:
        claimed = siwe.parse(message)
    except siwe.SignInRefused as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if await run_in_threadpool(_take, f"siwe:{claimed.nonce}") is None:
        raise HTTPException(status_code=401, detail="the nonce is unknown, used or expired")
    try:
        signed = siwe.verify(
            message, signature, domain=domain(), nonce=claimed.nonce, now=datetime.now(UTC)
        )
    except siwe.SignInRefused as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return signed.address.lower()


# ------------------------------------------------------------- with a wallet


@router.post(
    "/nonce",
    summary="A nonce for a wallet to sign",
    response_model=NonceOut,
    dependencies=[limit_signin],
    responses={429: R429},
)
async def nonce() -> NonceOut:
    """A one-time nonce for a wallet to sign.

    What the Sign-In with Ethereum message must say around it: this site's domain, the
    Arc chain and the statement. Good for one sign-in, or one wallet connection, within
    ten minutes.
    """
    value = secrets.token_hex(12)
    await run_in_threadpool(store.coordinator.put, f"siwe:{value}", "issued", NONCE_TTL)
    return NonceOut(
        nonce=value,
        domain=domain(),
        uri=settings.public_url,
        chain_id=settings.chain_id,
        statement=STATEMENT,
    )


@router.post(
    "/verify",
    summary="Sign in with a wallet",
    response_model=SessionOut,
    dependencies=[limit_signin],
    responses={401: R401, 429: R429},
)
async def verify(payload: SignInRequest, response: Response) -> SessionOut:
    """Sign in with a wallet.

    Checks the signed message, spends its nonce, and starts a wallet session, in the
    cookie and as a bearer token. The account is the one this wallet is connected to,
    whichever way it signed in first; null until a side is chosen.
    """
    address = await _proven_address(payload.message, payload.signature)
    return await _session_out(response, Identity.wallet(address))


# ------------------------------------------------------------- with GitHub


@router.post(
    "/github/signin",
    summary="Sign in with GitHub",
    response_model=GitHubSignInStart,
    dependencies=[limit_signin],
    responses={409: R409_ORIGIN, 429: R429, 503: R503},
)
async def start_github_signin(request: Request, response: Response) -> GitHubSignInStart:
    """Start signing in with GitHub.

    Answers with the URL to send the browser to, and sets a short-lived HttpOnly cookie
    holding the same state; GitHub sends the user back to `/auth/github/callback`, which
    signs them in only if the two match. Needs no session.
    """
    client = oauth()
    if client is None:
        raise not_configured()
    _refuse_another_origin(request, "sign in")
    state = secrets.token_urlsafe(24)
    await run_in_threadpool(store.coordinator.put, f"{_SIGNIN}{state}", "signin", NONCE_TTL)
    response.set_cookie(
        STATE_COOKIE,
        state,
        max_age=int(NONCE_TTL.total_seconds()),
        httponly=True,
        # Lax, so the browser sends it on the top-level return from github.com.
        samesite="lax",
        secure=_secure(),
        path=STATE_COOKIE_PATH,
    )
    return GitHubSignInStart(authorize_url=client.authorize_url(state, callback_url()))


@router.post(
    "/github/simulate-signin",
    summary="Sign in with a typed GitHub login (simulation only)",
    response_model=SessionOut,
    dependencies=[limit_signin],
    responses={
        403: {"description": "Outside the simulation, where GitHub sign-in goes through GitHub"},
        409: {"description": "The login is linked to an account of another GitHub user"},
        429: R429,
    },
)
async def simulate_github_signin(payload: SimulatedSignIn, response: Response) -> SessionOut:
    """Sign in as a GitHub login without OAuth: the simulation's demo only.

    The login stands for a made-up GitHub user whose id follows from it, so the same
    login signs in to the same account every time. It finds only accounts made that
    way, never one linked to a real GitHub user or linked before ids were kept.
    """
    if not settings.simulated:
        raise HTTPException(status_code=403, detail="sign in through GitHub")
    user = simulated_user(payload.login)
    try:
        await run_in_threadpool(
            lambda: store.sign_in_with_github(user.id, user.login, by_login=False)
        )
    except AccountConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await _session_out(response, Identity.github(user.id, user.login))


@router.get(
    "/github/callback",
    summary="Where GitHub returns, for a sign-in or a link",
    status_code=303,
    response_class=RedirectResponse,
    responses={
        303: {"description": "Signed in or linked; on to the Account page"},
        400: {"description": "The state is unknown, used or expired"},
        401: {"description": "Linking, and the browser is not signed in"},
        403: {
            "description": "Signing in, and this browser did not start it; or linking, and "
            "another account started it. No session is issued."
        },
        409: {"description": "That GitHub account or login belongs to another account"},
        502: {"description": "GitHub refused the code or could not be reached"},
        503: R503,
    },
)
async def github_callback(request: Request, code: str, state: str) -> RedirectResponse:
    """Where GitHub sends the user back, for a sign-in and for a link.

    The state's slot says which. A sign-in must come back to the browser that started
    it, by the state cookie; a link must be finished by the account that started it,
    by the session. Either way the state is spent on first use, and the redirect goes
    only to this app's own Account page.
    """
    # Spent on the way in: a state is single-use, whatever happens next.
    slot = f"{_SIGNIN}{state}"
    if await run_in_threadpool(_take, slot) is not None:
        return await _finish_signin(request, code, state)
    if await run_in_threadpool(store.coordinator.get, f"spent:{slot}") is not None:
        raise HTTPException(
            status_code=400,
            detail="this GitHub sign-in was already used; start it again from the Account page",
        )
    return await _finish_link(request, code, state)


async def _finish_signin(request: Request, code: str, state: str) -> RedirectResponse:
    kept = request.cookies.get(STATE_COOKIE, "")
    if not kept or not secrets.compare_digest(kept.encode(), state.encode()):
        raise HTTPException(
            status_code=403,
            detail="this GitHub sign-in was not started in this browser; start it again "
            "from the Account page",
        )
    client = oauth()
    if client is None:
        raise not_configured()
    try:
        user = await run_in_threadpool(client.user_for, code, callback_url())
        await run_in_threadpool(store.sign_in_with_github, user.id, user.login)
    except GitHubError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except AccountConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    back = RedirectResponse(f"{settings.public_url.rstrip('/')}/account?signed_in=github", 303)
    _start_session(back, Identity.github(user.id, user.login))
    back.delete_cookie(STATE_COOKIE, path=STATE_COOKIE_PATH)
    return back


async def _finish_link(request: Request, code: str, state: str) -> RedirectResponse:
    """Finish a link the *same* signed-in account started.

    The state is server-side, so it proves the flow began here; it does not prove who
    is finishing it. Without the session check any caller could start a link with
    their own wallet, send the authorize URL to someone else, and have that person's
    GitHub account bound to the attacker's -- and `github_login` is what gates
    publishing, claiming and submitting, so the attacker would then be paid for work
    the victim opened. The account is therefore taken from the session and must be
    the one the state was issued to.
    """
    identity = await signed_in(request)
    if identity is None:
        raise HTTPException(status_code=401, detail="sign in to finish linking GitHub")
    # Spent either way: it is single-use, and a mismatch is not a request to leave it
    # lying around for a second attempt.
    started_by = await run_in_threadpool(_take, f"{_LINK}{state}")
    if started_by is None:
        raise HTTPException(status_code=400, detail="the request is unknown, used or expired")
    account = await run_in_threadpool(account_of, identity)
    if account is None or started_by != account.party_id:
        raise HTTPException(status_code=403, detail="this link was started by another account")
    client = oauth()
    if client is None:
        raise not_configured()
    try:
        user = await run_in_threadpool(client.user_for, code, callback_url())
        await run_in_threadpool(store.link_github, account.party_id, user.login, user.id)
    except GitHubError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except AccountConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(f"{settings.public_url.rstrip('/')}/account?linked=github", 303)


# ------------------------------------------------------- with single sign-on


def oidc_client(connection: SsoConnection) -> OidcClient:
    """A client for the organisation's provider, with the secret read from the secret
    store now, by reference, and held no longer than the request. Runs in the
    threadpool."""
    try:
        secret = secret_store(settings.secret_store_dir).read(connection.client_secret_ref)
    except AttestorKeyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if secret is None:
        raise HTTPException(
            status_code=503,
            detail=f"single sign-on for {connection.publisher_id} is not finished: the secret "
            f"store holds nothing at {connection.client_secret_ref}",
        )
    return OidcClient(connection.issuer, connection.client_id, secret)


def _connection_for(email: str) -> SsoConnection:
    connection = store.sso_for_email(email)
    if connection is None:
        raise HTTPException(
            status_code=404,
            detail=f"no organisation signs in with single sign-on at {domain_of(email) or email}",
        )
    return connection


def _organisation_name(publisher_id: str) -> str:
    publisher = store.get_publisher(publisher_id)
    return publisher.name if publisher else publisher_id


def _no_account(publisher_id: str) -> str:
    return f"{publisher_id} has no account to sign in to; its owner signs in and chooses a side"


@router.post(
    "/sso/signin",
    summary="Sign in through your organisation's single sign-on",
    response_model=SsoSignInStart,
    dependencies=[limit_signin],
    responses={
        404: {"description": "No organisation signs in with single sign-on at that domain"},
        409: R409_ORIGIN,
        429: R429,
        502: {"description": "The identity provider could not be reached or answered wrong"},
        503: {"description": "The organisation's client secret is not in the secret store"},
    },
)
async def start_sso_signin(
    payload: SsoSignInRequest, request: Request, response: Response
) -> SsoSignInStart:
    """Start signing in through an organisation's identity provider.

    The work e-mail's domain says which. Answers with the URL to send the browser to,
    and sets a short-lived HttpOnly cookie holding the same state; the provider sends
    the user back to `/auth/sso/callback`, which signs them in only if the two match.
    """
    connection = await run_in_threadpool(_connection_for, payload.email)
    _refuse_another_origin(request, "sign in")
    client = await run_in_threadpool(oidc_client, connection)
    try:
        discovery = await run_in_threadpool(client.discover)
    except SsoError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    state, nonce, proof = secrets.token_urlsafe(24), secrets.token_urlsafe(24), pkce()
    pending = json.dumps(
        {
            "publisher_id": connection.publisher_id,
            "issuer": connection.issuer,
            "nonce": nonce,
            "verifier": proof.verifier,
        }
    )
    await run_in_threadpool(store.coordinator.put, f"{_SSO}{state}", pending, NONCE_TTL)
    response.set_cookie(
        SSO_STATE_COOKIE,
        state,
        max_age=int(NONCE_TTL.total_seconds()),
        httponly=True,
        # Lax, so the browser sends it on the top-level return from the provider.
        samesite="lax",
        secure=_secure(),
        path=SSO_STATE_COOKIE_PATH,
    )
    return SsoSignInStart(
        authorize_url=client.authorize_url(
            discovery,
            state=state,
            nonce=nonce,
            challenge=proof.challenge,
            redirect_uri=sso_callback_url(),
            login_hint=payload.email,
        ),
        organisation=await run_in_threadpool(_organisation_name, connection.publisher_id),
    )


@router.post(
    "/sso/simulate-signin",
    summary="Sign in with a typed work e-mail (simulation only)",
    response_model=SessionOut,
    dependencies=[limit_signin],
    responses={
        403: {"description": "Outside the simulation, where the identity provider signs in"},
        404: {"description": "No organisation signs in with single sign-on at that domain"},
        409: {"description": "The organisation has no account to act as"},
        429: R429,
    },
)
async def simulate_sso_signin(payload: SsoSignInRequest, response: Response) -> SessionOut:
    """Sign in as a work e-mail without a provider: the simulation's demo only. The
    address stands for one the organisation's provider verified."""
    if not settings.simulated:
        raise HTTPException(status_code=403, detail="sign in through your identity provider")
    connection = await run_in_threadpool(_connection_for, payload.email)
    try:
        email = admitted(connection.domains, payload.email, True)
    except SsoRefusal as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    identity = Identity.sso(connection.publisher_id, email)
    if await run_in_threadpool(account_of, identity) is None:
        raise HTTPException(status_code=409, detail=_no_account(connection.publisher_id))
    return await _session_out(response, identity)


@router.get(
    "/sso/callback",
    summary="Where an organisation's identity provider returns",
    status_code=303,
    response_class=RedirectResponse,
    responses={
        303: {"description": "Signed in; on to the Account page"},
        400: {"description": "The state is unknown, used or expired, or no code came back"},
        403: {
            "description": "This browser did not start the sign-in, the provider refused it, "
            "or the address it asserted is not admitted. No session is issued."
        },
        409: {"description": "The organisation's sign-on changed mid-flight, or it has no account"},
        502: {"description": "The provider refused the code, or its ID token does not check out"},
        503: {"description": "The organisation's client secret is not in the secret store"},
    },
)
async def sso_callback(
    request: Request, state: str, code: str | None = None, error: str | None = None
) -> RedirectResponse:
    """Where the provider sends the user back. The state is spent on first use, must
    come back to the browser that started it, and the redirect goes only to this app's
    own Account page."""
    pending = await run_in_threadpool(_take, f"{_SSO}{state}")
    if pending is None:
        raise HTTPException(
            status_code=400,
            detail="this sign-in is unknown, used or expired; start it again from the Account page",
        )
    kept = request.cookies.get(SSO_STATE_COOKIE, "")
    if not kept or not secrets.compare_digest(kept.encode(), state.encode()):
        raise HTTPException(
            status_code=403,
            detail="this sign-in was not started in this browser; start it again from the "
            "Account page",
        )
    if error is not None:
        said = f": {error}" if _PROVIDER_ERROR.match(error) else ""
        raise HTTPException(status_code=403, detail=f"your identity provider refused{said}")
    if not code:
        raise HTTPException(status_code=400, detail="the identity provider sent no code back")
    started = json.loads(pending)
    publisher_id = started["publisher_id"]
    connection = await run_in_threadpool(store.sso, publisher_id)
    if connection is None or connection.issuer != started["issuer"]:
        raise HTTPException(
            status_code=409,
            detail="the organisation's single sign-on changed while you were signing in; "
            "start again",
        )
    client = await run_in_threadpool(oidc_client, connection)
    try:
        user = await run_in_threadpool(
            lambda: client.user_for(
                code=code,
                verifier=started["verifier"],
                nonce=started["nonce"],
                redirect_uri=sso_callback_url(),
            )
        )
        email = admitted(connection.domains, user.email, user.email_verified)
    except SsoError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except SsoRefusal as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    identity = Identity.sso(publisher_id, email)
    if await run_in_threadpool(account_of, identity) is None:
        raise HTTPException(status_code=409, detail=_no_account(publisher_id))
    back = RedirectResponse(f"{settings.public_url.rstrip('/')}/account?signed_in=sso", 303)
    _start_session(back, identity)
    back.delete_cookie(SSO_STATE_COOKIE, path=SSO_STATE_COOKIE_PATH)
    return back


# ------------------------------------------------------------- the session


@router.post(
    "/logout",
    summary="Sign out",
    status_code=204,
    responses={204: {"description": "Signed out"}},
)
async def logout(response: Response) -> Response:
    """Sign out: the session cookie is cleared. A bearer token lapses on its own."""
    response.delete_cookie(sessions.COOKIE, path="/")
    response.status_code = 204
    return response


@router.get(
    "/me",
    summary="Who is signed in, and how",
    response_model=MeOut,
    responses={401: {"description": "Not signed in"}},
)
async def me(identity: Identity | None = SESSION) -> MeOut:
    """Who is signed in.

    How the session signed in (`github` or `wallet`), the GitHub login and id, the
    wallet, the account once a side is chosen, and the wallet its money moves through.
    Any of them may be null.
    """
    if identity is None:
        raise HTTPException(status_code=401, detail="not signed in")
    return await run_in_threadpool(_me, identity)


@router.post(
    "/role",
    summary="Choose a side, once",
    response_model=Account,
    status_code=201,
    responses={
        401: {"description": "Not signed in"},
        409: {
            "description": "This wallet or GitHub account already has a side, the budget is "
            "not a number, or the GitHub login belongs to another account"
        },
    },
)
async def choose_role(payload: RoleRequest, identity: Identity | None = SESSION) -> Account:
    """Choose a side, once: publisher or contributor.

    Signed in with GitHub, the account starts with that GitHub account and no wallet,
    and a contributor is known by their login. Signed in with a wallet, the account
    starts with that wallet and links GitHub next.
    """
    if identity is None:
        raise HTTPException(status_code=401, detail="sign in first")
    if identity.method == "sso":
        raise HTTPException(
            status_code=409,
            detail="single sign-on acts as the organisation's account, which already has a side",
        )
    try:
        return await run_in_threadpool(
            lambda: store.create_account(
                identity.address,
                payload.role,
                payload.name,
                github_id=identity.github_id,
                github_login=identity.github_login,
                budget_usdc=payload.budget_usdc,
            )
        )
    except (AccountExists, AccountConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/wallet/connect",
    summary="Connect a wallet to the signed-in account",
    response_model=Account,
    dependencies=[limit_signin],
    responses={
        401: R401,
        409: {
            "description": "The wallet belongs to another account, this account already has "
            "another wallet, or no side has been chosen yet"
        },
        429: R429,
    },
)
async def connect_wallet(
    payload: WalletConnectRequest, identity: Identity | None = SESSION
) -> Account:
    """Connect a wallet to the signed-in account.

    Sign a message over a nonce from `/auth/nonce` with the wallet, as for signing in;
    that proves it is yours. It becomes the account's wallet: where a publisher funds
    from, or where a contributor is paid. A wallet belongs to one account, and an
    account keeps the wallet it connected.

    The exception: signed in with GitHub, before choosing a side, connecting the wallet
    of an account that signed in with a wallet before GitHub sign-in existed, and was
    never linked to GitHub, joins that account to you. You are then that account.
    """
    if identity is None:
        raise HTTPException(status_code=401, detail="sign in first, then connect a wallet")
    address = await _proven_address(payload.message, payload.signature)
    account = await run_in_threadpool(acting_account, identity)
    try:
        return await run_in_threadpool(
            lambda: store.connect_wallet(
                address,
                party_id=account.party_id if account else None,
                github_id=identity.github_id,
                github_login=identity.github_login,
            )
        )
    except (AccountConflict, NoAccountYet) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# ------------------------------------------------- linking GitHub to a wallet


def _refuse_relink(account: Account) -> None:
    if account.github_id is not None:
        raise HTTPException(
            status_code=409,
            detail=f"this account is already linked to GitHub as {account.github_login}",
        )


@router.post(
    "/github/start",
    summary="Link a GitHub account to a wallet account",
    response_model=GitHubLinkStart,
    responses={
        401: {"description": "Not signed in, or no side chosen yet"},
        409: {
            "description": "The browser is on another address than GitHub returns to, or "
            "the account is already linked to a GitHub account"
        },
        503: R503,
    },
)
async def start_github_link(
    request: Request, account: Account | None = SIGNED_IN
) -> GitHubLinkStart:
    """Start linking a GitHub account to an account that signed in with a wallet.

    Answers with the URL to send the browser to. GitHub sends the user back to
    `/auth/github/callback`, which links the account only for the session that
    started it.
    """
    if account is None:
        raise HTTPException(status_code=401, detail="sign in and choose a role first")
    client = oauth()
    if client is None:
        raise not_configured()
    _refuse_another_origin(request, "link")
    _refuse_relink(account)
    state = secrets.token_urlsafe(24)
    await run_in_threadpool(store.coordinator.put, f"{_LINK}{state}", account.party_id, NONCE_TTL)
    return GitHubLinkStart(authorize_url=client.authorize_url(state, callback_url()))


@router.post(
    "/github/simulate",
    summary="Link a typed GitHub login (simulation only)",
    response_model=Account,
    responses={
        401: {"description": "Not signed in, or no side chosen yet"},
        403: {"description": "Outside the simulation, where linking goes through GitHub"},
        409: {
            "description": "The login belongs to another account, or this account is "
            "already linked to another GitHub account"
        },
    },
)
async def simulate_github_link(
    payload: SimulatedLink, account: Account | None = SIGNED_IN
) -> Account:
    """Link a GitHub login without OAuth: the simulation's demo only.

    The login stands for the same made-up GitHub user a demo GitHub sign-in with it
    does, so the account can sign in with it afterwards.
    """
    if not settings.simulated:
        raise HTTPException(status_code=403, detail="link through GitHub")
    if account is None:
        raise HTTPException(status_code=401, detail="sign in and choose a role first")
    user = simulated_user(payload.login)
    try:
        return await run_in_threadpool(store.link_github, account.party_id, user.login, user.id)
    except AccountConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
