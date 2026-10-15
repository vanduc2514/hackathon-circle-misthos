"""Signing in through an Enterprise organisation's own identity provider (#53).

A work e-mail's domain finds the organisation's connection, and the flow is OpenID
Connect's authorization code with PKCE and a nonce. The state is kept twice: in the
coordinator, which proves the flow began here, and in a short-lived HttpOnly cookie,
which proves it began in *this* browser. The callback refuses a state whose cookie does
not match and issues no session; without that, anyone could start a sign-in, approve it
as themselves, and send the callback URL to a victim, whose browser would then act for
the attacker (login CSRF).

The session acts as the organisation's account and names the person, by the verified
address the provider asserted. Nothing here logs a code, a token or the client secret.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse

from misthos.api.guards import limit_signin
from misthos.auth import sessions
from misthos.config import settings
from misthos.domain.sso import SsoRefusal, admitted, domain_of
from misthos.schemas import SessionOut, SsoConnection, SsoSignInRequest, SsoSignInStart
from misthos.services.attestor import AttestorKeyError, secret_store
from misthos.services.sso import OidcClient, SsoError, pkce
from misthos.store import store

router = APIRouter(prefix="/auth/sso", tags=["auth"])

STATE_TTL = timedelta(minutes=10)
STATE_COOKIE = "misthos_sso_state"
STATE_COOKIE_PATH = "/api/v1/auth/sso"
_SLOT = "sso:"
# What an identity provider may put in `error` that is worth repeating to the user.
_PROVIDER_ERROR = re.compile(r"^[a-z_]{1,64}$")


def callback_url() -> str:
    """The redirect URI an organisation registers with its identity provider."""
    return f"{settings.public_url.rstrip('/')}/api/v1/auth/sso/callback"


def _secure() -> bool:
    return settings.public_url.startswith("https://")


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def _start_session(response: Response, publisher_id: str, email: str) -> str:
    token = sessions.issue_sso(publisher_id, email)
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


def _take(slot: str) -> str | None:
    """What a one-time slot holds, for the first caller only, and the slot spent.
    Runs in the threadpool."""
    found = store.coordinator.get(slot)
    if found is None or not store.coordinator.reserve(f"spent:{slot}", "spent", STATE_TTL):
        return None
    store.coordinator.delete(slot)
    return found


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


async def _session_out(response: Response, publisher_id: str, email: str) -> SessionOut:
    account = await run_in_threadpool(store.account_for_sso, publisher_id, email)
    if account is None:
        raise HTTPException(status_code=409, detail=_no_account(publisher_id))
    token = _start_session(response, publisher_id, email)
    return SessionOut(address=account.address, token=token, account=account, sso_email=email)


@router.post(
    "/signin",
    summary="Sign in through your organisation's single sign-on",
    response_model=SsoSignInStart,
    dependencies=[limit_signin],
    responses={
        404: {"description": "No organisation signs in with single sign-on at that domain"},
        409: {"description": "The browser is on another address than the provider returns to"},
        502: {"description": "The identity provider could not be reached or answered wrong"},
        503: {"description": "The organisation's client secret is not in the secret store"},
    },
)
async def start(payload: SsoSignInRequest, request: Request, response: Response) -> SsoSignInStart:
    """Start signing in through an organisation's identity provider.

    The work e-mail's domain says which. Answers with the URL to send the browser to,
    and sets a short-lived HttpOnly cookie holding the same state; the provider sends
    the user back to `/auth/sso/callback`, which signs them in only if the two match.
    """
    connection = await run_in_threadpool(_connection_for, payload.email)
    public, origin = _origin(settings.public_url), request.headers.get("origin")
    if origin and _origin(origin) != public:
        raise HTTPException(
            status_code=409,
            detail=f"your identity provider sends you back to {public}, and this browser is "
            f"on {origin}; open the app at {public} and sign in from there",
        )
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
    await run_in_threadpool(store.coordinator.put, f"{_SLOT}{state}", pending, STATE_TTL)
    response.set_cookie(
        STATE_COOKIE,
        state,
        max_age=int(STATE_TTL.total_seconds()),
        httponly=True,
        # Lax, so the browser sends it on the top-level return from the provider.
        samesite="lax",
        secure=_secure(),
        path=STATE_COOKIE_PATH,
    )
    return SsoSignInStart(
        authorize_url=client.authorize_url(
            discovery,
            state=state,
            nonce=nonce,
            challenge=proof.challenge,
            redirect_uri=callback_url(),
            login_hint=payload.email,
        ),
        organisation=await run_in_threadpool(_organisation_name, connection.publisher_id),
    )


@router.post(
    "/simulate-signin",
    summary="Sign in with a typed work e-mail (simulation only)",
    response_model=SessionOut,
    dependencies=[limit_signin],
    responses={
        403: {"description": "Outside the simulation, where the identity provider signs in"},
        404: {"description": "No organisation signs in with single sign-on at that domain"},
        409: {"description": "The organisation has no account to act as"},
    },
)
async def simulate(payload: SsoSignInRequest, response: Response) -> SessionOut:
    """Sign in as a work e-mail without a provider: the simulation's demo only. The
    address stands for one the organisation's provider verified."""
    if not settings.simulated:
        raise HTTPException(status_code=403, detail="sign in through your identity provider")
    connection = await run_in_threadpool(_connection_for, payload.email)
    try:
        email = admitted(connection.domains, payload.email, True)
    except SsoRefusal as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return await _session_out(response, connection.publisher_id, email)


@router.get(
    "/callback",
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
async def callback(
    request: Request, state: str, code: str | None = None, error: str | None = None
) -> RedirectResponse:
    """Where the provider sends the user back. The state is spent on first use, must
    come back to the browser that started it, and the redirect goes only to this app's
    own Account page."""
    pending = await run_in_threadpool(_take, f"{_SLOT}{state}")
    if pending is None:
        raise HTTPException(
            status_code=400,
            detail="this sign-in is unknown, used or expired; start it again from the Account page",
        )
    kept = request.cookies.get(STATE_COOKIE, "")
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
                redirect_uri=callback_url(),
            )
        )
        email = admitted(connection.domains, user.email, user.email_verified)
    except SsoError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except SsoRefusal as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if await run_in_threadpool(store.account_for_sso, publisher_id, email) is None:
        raise HTTPException(status_code=409, detail=_no_account(publisher_id))
    back = RedirectResponse(f"{settings.public_url.rstrip('/')}/account?signed_in=sso", 303)
    _start_session(back, publisher_id, email)
    back.delete_cookie(STATE_COOKIE, path=STATE_COOKIE_PATH)
    return back
