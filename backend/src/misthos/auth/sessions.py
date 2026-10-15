"""Sessions: a signed token naming the wallet, in a cookie or a bearer header.

A session that came through an Enterprise organisation's single sign-on (#53) names
the organisation instead, as `sso:<publisher id>`, and carries the verified e-mail its
identity provider asserted. It is never read as a wallet.

The token is an HMAC-signed JWT with the address as its subject and a twelve-hour
life. The browser carries it in an HttpOnly, SameSite=Lax cookie, so a script on the
page cannot read it and another site cannot send it with a form; an API client sends
it as `Authorization: Bearer`.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from misthos.config import settings

COOKIE = "misthos_session"
LIFETIME = timedelta(hours=12)
ISSUER = "misthos"

log = logging.getLogger("misthos.auth")

_SECRET = settings.session_secret or secrets.token_hex(32)


def warn_if_unshared() -> None:
    """Said at startup, once logging is configured. Said at import, it was written
    before any handler existed, as the one bare line in a JSON log."""
    if not settings.session_secret:
        # Fine for one process; sessions end at a restart and do not cross processes.
        log.warning("MISTHOS_SESSION_SECRET is not set; sessions last only this process")


def issue(address: str, now: datetime | None = None) -> str:
    at = now or datetime.now(UTC)
    claims = {"sub": address.lower(), "iss": ISSUER, "iat": at, "exp": at + LIFETIME}
    return jwt.encode(claims, _SECRET, algorithm="HS256")


def address_in(token: str) -> str | None:
    claims = _claims(token)
    subject = claims.get("sub") if claims else None
    if not isinstance(subject, str) or subject.startswith(_SSO):
        return None
    return subject


_SSO = "sso:"


@dataclass(frozen=True)
class SsoSession:
    """Who a single sign-on session proves: a person, by the verified address their
    organisation's identity provider asserted, acting for that organisation."""

    publisher_id: str
    email: str


def issue_sso(publisher_id: str, email: str, now: datetime | None = None) -> str:
    at = now or datetime.now(UTC)
    claims = {
        "sub": f"{_SSO}{publisher_id}",
        "email": email.lower(),
        "iss": ISSUER,
        "iat": at,
        "exp": at + LIFETIME,
    }
    return jwt.encode(claims, _SECRET, algorithm="HS256")


def sso_in(token: str) -> SsoSession | None:
    """The single sign-on session a valid token names, or None for anything else."""
    claims = _claims(token)
    subject = claims.get("sub") if claims else None
    email = claims.get("email") if claims else None
    if not isinstance(subject, str) or not subject.startswith(_SSO):
        return None
    publisher_id = subject.removeprefix(_SSO)
    if not publisher_id or not isinstance(email, str) or "@" not in email:
        return None
    return SsoSession(publisher_id, email.lower())


def _claims(token: str) -> dict | None:
    try:
        return jwt.decode(token, _SECRET, algorithms=["HS256"], issuer=ISSUER)
    except jwt.PyJWTError:
        return None
