"""Sessions: a signed token naming the wallet, in a cookie or a bearer header.

The token is an HMAC-signed JWT with the address as its subject and a twelve-hour
life. The browser carries it in an HttpOnly, SameSite=Lax cookie, so a script on the
page cannot read it and another site cannot send it with a form; an API client sends
it as `Authorization: Bearer`.
"""

from __future__ import annotations

import logging
import secrets
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
    try:
        claims = jwt.decode(token, _SECRET, algorithms=["HS256"], issuer=ISSUER)
    except jwt.PyJWTError:
        return None
    subject = claims.get("sub")
    return subject if isinstance(subject, str) else None
