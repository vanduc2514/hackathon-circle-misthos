"""Signing in with GitHub (#131) and linking a GitHub account (#80), through the OAuth App.

The user is sent to GitHub to approve, GitHub sends them back with a one-time code,
and the code is exchanged for a token that is used once, to read who they are. The
token is not kept. What the platform needs is the numeric user id, which is the
identity because it survives a renamed login, and the login, so that a publisher's
issues and a contributor's pull requests map to a real GitHub account.

Neither the code, the token nor the client secret is ever logged or put in an error.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from misthos.services.github.base import GitHubError

WEB = "https://github.com"
API = "https://api.github.com"

# Where the simulation's made-up GitHub user ids start. Real ids are far below it, so
# a demo sign-in can never be mistaken for, or collide with, a real GitHub account;
# and every id stays a whole number a browser can hold exactly (under 2**53).
SIMULATED_ID_BASE = 10**15


@dataclass(frozen=True)
class GitHubUser:
    id: int
    login: str


def simulated_user(login: str) -> GitHubUser:
    """The simulation's stand-in for a GitHub account: the same login is always the
    same user, case aside, as GitHub matches logins."""
    digest = hashlib.sha256(login.lower().encode()).digest()
    return GitHubUser(
        id=SIMULATED_ID_BASE + int.from_bytes(digest[:8], "big") % SIMULATED_ID_BASE,
        login=login,
    )


class GitHubOAuth:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        web_url: str = WEB,
        api_url: str = API,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._id, self._secret = client_id, client_secret
        self._web, self._api = web_url.rstrip("/"), api_url.rstrip("/")
        self._http = httpx.Client(timeout=10.0, transport=transport)

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        query = urlencode(
            {
                "client_id": self._id,
                "redirect_uri": redirect_uri,
                "state": state,
                "allow_signup": "false",
            }
        )
        return f"{self._web}/login/oauth/authorize?{query}"

    def user_for(self, code: str, redirect_uri: str) -> GitHubUser:
        """Exchange the code, read who it belongs to, and drop the token."""
        try:
            exchanged = self._http.post(
                f"{self._web}/login/oauth/access_token",
                headers={"Accept": "application/json"},
                data={
                    "client_id": self._id,
                    "client_secret": self._secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
            )
            token = exchanged.json().get("access_token")
            if not token:
                raise GitHubError(f"GitHub refused the code: {exchanged.json().get('error')}")
            user = self._http.get(
                f"{self._api}/user",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
            )
            if user.status_code >= 400:
                raise GitHubError(f"GitHub would not say who this is: {user.status_code}")
            body = user.json()
        except httpx.HTTPError as exc:
            # httpx names what failed and where, never the form or headers it carried.
            raise GitHubError(f"GitHub could not be reached: {exc}") from exc
        except ValueError as exc:
            raise GitHubError("GitHub answered with something other than JSON") from exc
        number = body.get("id") if isinstance(body, dict) else None
        login = body.get("login") if isinstance(body, dict) else None
        # bool is an int to Python, and no GitHub account is user True.
        if not isinstance(number, int) or isinstance(number, bool) or not isinstance(login, str):
            raise GitHubError("GitHub's answer did not name a user")
        return GitHubUser(id=number, login=login)
