"""Identity verification through Stripe Identity.

The contributor hands their documents to Stripe, never to us. We keep only the
verification session id and its outcome. When no key is configured, no session can be
opened and a first payout stays unverified: the gate holds it rather than approving it.
"""

from __future__ import annotations

import logging

import httpx

from misthos.domain.compliance import IdentityStatus

log = logging.getLogger("misthos.compliance.identity")


class IdentityUnavailable(RuntimeError):
    """No identity provider is configured, so a verification cannot start."""


class StripeIdentity:
    name = "stripe-identity"

    def __init__(
        self,
        secret_key: str,
        api_url: str = "https://api.stripe.com",
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._key = secret_key
        self._api_url = api_url.rstrip("/")
        self._client = client

    def start(self, contributor_id: str) -> str:
        body = {
            "metadata[contributor_id]": contributor_id,
            "type": "document",
        }
        response = self._request("POST", "/v1/identity/verification_sessions", data=body)
        reference = response.get("id")
        if not reference:
            raise IdentityUnavailable("Stripe returned no verification session id")
        return str(reference)

    def status(self, reference: str) -> IdentityStatus:
        response = self._request("GET", f"/v1/identity/verification_sessions/{reference}")
        status = response.get("status")
        match status:
            case "verified":
                return IdentityStatus.VERIFIED
            case "canceled":
                return IdentityStatus.FAILED
            case "requires_input" | "processing":
                return IdentityStatus.PENDING
            case _:
                # A session Stripe does not know, or a status we do not recognise, is
                # not a verification: leave it unverified rather than guess.
                return IdentityStatus.UNVERIFIED

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self._api_url}{path}"
        if self._client is not None:
            response = self._client.request(method, url, auth=(self._key, ""), **kwargs)
        else:
            with httpx.Client(timeout=30.0) as client:
                response = client.request(method, url, auth=(self._key, ""), **kwargs)
        if response.status_code >= 400:
            raise IdentityUnavailable(
                f"Stripe Identity answered {response.status_code}: {response.text[:200]}"
            )
        return response.json()


class UnconfiguredIdentity:
    """The provider when `MISTHOS_STRIPE_SECRET_KEY` is unset.

    It cannot verify anybody, so a first payout stays pending. It is a guard, not a
    stand-in: there is no path on which it returns `VERIFIED`.
    """

    name = "unconfigured"

    def start(self, contributor_id: str) -> str:
        raise IdentityUnavailable(
            "MISTHOS_STRIPE_SECRET_KEY is unset, so no identity verification can start "
            "and a first payout is held"
        )

    def status(self, reference: str) -> IdentityStatus:
        return IdentityStatus.UNVERIFIED
