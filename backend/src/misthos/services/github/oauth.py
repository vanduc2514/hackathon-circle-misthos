"""Linking a user's own GitHub account (#80), through the App's OAuth client.

The user is sent to GitHub to approve, GitHub sends them back with a one-time code,
and the code is exchanged for a token that is used once, to read who they are. The
token is not kept: what the platform needs is the login, so that a publisher's
issues and a contributor's pull requests map to a real GitHub identity.
"""

from __future__ import annotations

from urllib.parse import urlencode

import httpx

from misthos.services.github.base import GitHubError

WEB = "https://github.com"
API = "https://api.github.com"


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

    def login_for(self, code: str, redirect_uri: str) -> str:
        """Exchange the code, read the login, and drop the token."""
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
        except httpx.HTTPError as exc:
            raise GitHubError(f"GitHub could not be reached: {exc}") from exc
        if user.status_code >= 400:
            raise GitHubError(f"GitHub would not say who this is: {user.status_code}")
        return str(user.json()["login"])
