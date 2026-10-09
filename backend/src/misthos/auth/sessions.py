"""Sessions: a signed token naming who signed in, in a cookie or a bearer header.

Someone signs in with a wallet or with GitHub (#131), and the token says which. It
is an HMAC-signed JWT with a twelve-hour life whose subject is `wallet:<address>` or
`github:<numeric GitHub user id>`; a GitHub session also carries the login it had
then, for display. The numeric id is the identity because a login can be renamed,
and the next person to take it is somebody else.

A token issued before this change has a bare `0x…` address as its subject. It is
read as the wallet session it was, so nobody is signed out by the upgrade.

The browser carries the token in an HttpOnly, SameSite=Lax cookie, so a script on
the page cannot read it and another site cannot send it with a form; an API client
sends it as `Authorization: Bearer`.
"""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import jwt

from misthos.config import settings

COOKIE = "misthos_session"
LIFETIME = timedelta(hours=12)
ISSUER = "misthos"

log = logging.getLogger("misthos.auth")

_SECRET = settings.session_secret or secrets.token_hex(32)
_ADDRESS = re.compile(r"^0x[0-9a-f]{40}$")
_NUMBER = re.compile(r"^[0-9]{1,20}$")

Method = Literal["wallet", "github"]


@dataclass(frozen=True)
class Identity:
    """Who a session proves: a wallet, or a GitHub account.

    Only what the signature proved is here. Whether that wallet or GitHub account has
    an account, and which, is the store's to answer, so a session issued before the
    account existed, or before a wallet was joined to it, still finds it.
    """

    method: Method
    address: str | None = None
    """Lowercase. Set for a wallet session only."""
    github_id: int | None = None
    """GitHub's numeric user id. Set for a GitHub session only."""
    github_login: str | None = None
    """The login when the session was issued. Display only: the id is the identity."""

    @classmethod
    def wallet(cls, address: str) -> Identity:
        return cls(method="wallet", address=address.lower())

    @classmethod
    def github(cls, github_id: int, login: str) -> Identity:
        return cls(method="github", github_id=github_id, github_login=login)

    @property
    def subject(self) -> str:
        if self.method == "github":
            return f"github:{self.github_id}"
        return f"wallet:{self.address}"


def warn_if_unshared() -> None:
    """Said at startup, once logging is configured. Said at import, it was written
    before any handler existed, as the one bare line in a JSON log."""
    if not settings.session_secret:
        # Fine for one process; sessions end at a restart and do not cross processes.
        log.warning("MISTHOS_SESSION_SECRET is not set; sessions last only this process")


def issue(identity: Identity, now: datetime | None = None) -> str:
    at = now or datetime.now(UTC)
    claims: dict[str, object] = {
        "sub": identity.subject,
        "iss": ISSUER,
        "iat": at,
        "exp": at + LIFETIME,
    }
    if identity.method == "github":
        claims["login"] = identity.github_login
    return jwt.encode(claims, _SECRET, algorithm="HS256")


def identity_in(token: str) -> Identity | None:
    """The identity a valid token names, or None for anything else.

    A subject this code does not recognise is no session at all, rather than a
    guess: a token is only ever read as what it was issued as.
    """
    try:
        claims = jwt.decode(token, _SECRET, algorithms=["HS256"], issuer=ISSUER)
    except jwt.PyJWTError:
        return None
    subject = claims.get("sub")
    if not isinstance(subject, str):
        return None
    if subject.startswith("github:"):
        number = subject.removeprefix("github:")
        login = claims.get("login")
        if not _NUMBER.match(number) or not isinstance(login, str):
            return None
        return Identity.github(int(number), login)
    address = subject.removeprefix("wallet:").lower()
    # A bare address is a session issued before GitHub sign-in existed.
    return Identity.wallet(address) if _ADDRESS.match(address) else None
