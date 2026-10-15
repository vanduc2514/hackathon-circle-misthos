"""Speaking OpenID Connect to an organisation's identity provider (#53).

The authorization code flow, with PKCE and a nonce, against whatever the provider's
discovery document names. The code is exchanged for an ID token, which is checked
against the provider's published keys, its issuer, our client id and the nonce this
sign-in sent; then it is dropped. Nothing else the provider returns is kept, and no
access token is used: who the person is, by verified e-mail, is all a session needs.

Neither the code, a token nor the client secret is ever logged or put in an error.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
import jwt

# Asymmetric only. HS256 would make the client secret, which we hold, a signing key.
ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384"]
LEEWAY_SECONDS = 60


class SsoError(Exception):
    """The provider could not be reached, or answered with something unusable."""


@dataclass(frozen=True)
class Discovery:
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str


@dataclass(frozen=True)
class SsoUser:
    subject: str
    email: object
    """As the provider asserted it: checked by domain/sso.py, not here."""
    email_verified: object


@dataclass(frozen=True)
class Pkce:
    verifier: str
    challenge: str


def pkce() -> Pkce:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return Pkce(verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode())


class OidcClient:
    def __init__(
        self,
        issuer: str,
        client_id: str,
        client_secret: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.issuer, self._id, self._secret = issuer, client_id, client_secret
        self._http = httpx.Client(timeout=10.0, transport=transport)

    def _json(self, response: httpx.Response, what: str) -> dict:
        if response.status_code >= 400:
            raise SsoError(f"the identity provider refused {what}: {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise SsoError(f"the identity provider's {what} is not JSON") from exc
        if not isinstance(body, dict):
            raise SsoError(f"the identity provider's {what} is not an object")
        return body

    def discover(self) -> Discovery:
        try:
            body = self._json(
                self._http.get(f"{self.issuer}/.well-known/openid-configuration"),
                "discovery document",
            )
        except httpx.HTTPError as exc:
            raise SsoError(f"the identity provider could not be reached: {exc}") from exc
        if body.get("issuer") != self.issuer:
            # A document naming another issuer would have us accept that issuer's tokens.
            raise SsoError(f"the discovery document names another issuer: {body.get('issuer')}")
        endpoints = [body.get(k) for k in ("authorization_endpoint", "token_endpoint", "jwks_uri")]
        # Plain HTTP only from a provider whose issuer is itself local (domain/sso.py).
        schemes = ("https://", "http://") if self.issuer.startswith("http://") else ("https://",)
        if not all(isinstance(e, str) and e.startswith(schemes) for e in endpoints):
            raise SsoError("the discovery document does not name the endpoints sign-in needs")
        return Discovery(*endpoints)  # type: ignore[arg-type]

    def authorize_url(
        self,
        discovery: Discovery,
        *,
        state: str,
        nonce: str,
        challenge: str,
        redirect_uri: str,
        login_hint: str,
    ) -> str:
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._id,
                "redirect_uri": redirect_uri,
                "scope": "openid email",
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "login_hint": login_hint,
            }
        )
        separator = "&" if "?" in discovery.authorization_endpoint else "?"
        return f"{discovery.authorization_endpoint}{separator}{query}"

    def user_for(self, *, code: str, verifier: str, nonce: str, redirect_uri: str) -> SsoUser:
        """Exchange the code, check the ID token, and drop every token."""
        discovery = self.discover()
        try:
            tokens = self._json(
                self._http.post(
                    discovery.token_endpoint,
                    headers={"Accept": "application/json"},
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": redirect_uri,
                        "client_id": self._id,
                        "client_secret": self._secret,
                        "code_verifier": verifier,
                    },
                ),
                "code",
            )
            id_token = tokens.get("id_token")
            if not isinstance(id_token, str):
                raise SsoError("the identity provider answered without an ID token")
            keys = self._json(self._http.get(discovery.jwks_uri), "signing keys")
        except httpx.HTTPError as exc:
            raise SsoError(f"the identity provider could not be reached: {exc}") from exc
        claims = self._verified(id_token, keys)
        if not isinstance(claims.get("nonce"), str) or not secrets.compare_digest(
            claims["nonce"].encode(), nonce.encode()
        ):
            # A token minted for another sign-in, replayed into this one.
            raise SsoError("the ID token was not issued for this sign-in")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise SsoError("the ID token does not name its subject")
        return SsoUser(subject, claims.get("email"), claims.get("email_verified"))

    def _verified(self, id_token: str, keys: dict) -> dict:
        try:
            header = jwt.get_unverified_header(id_token)
            signing = _key_for(header, jwt.PyJWKSet.from_dict(keys))
            claims = jwt.decode(
                id_token,
                signing.key,
                algorithms=ALGORITHMS,
                audience=self._id,
                issuer=self.issuer,
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except (jwt.PyJWTError, ValueError, KeyError) as exc:
            raise SsoError(f"the ID token does not check out: {exc}") from exc
        if not isinstance(claims, dict):
            raise SsoError("the ID token's claims are not an object")
        return claims


def _key_for(header: dict, keys: jwt.PyJWKSet) -> jwt.PyJWK:
    kid = header.get("kid")
    if kid is None:
        if len(keys.keys) != 1:
            raise KeyError("the token names no key, and the provider publishes several")
        return keys.keys[0]
    return keys[kid]
