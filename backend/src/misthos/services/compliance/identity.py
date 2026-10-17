"""Identity verification at first payout.

The provider holds the documents; the platform keeps a reference and an outcome.
No provider is configured by default: a first payout stays pending until Stripe
Identity can open a session.
"""

from __future__ import annotations

from typing import Protocol

from misthos.domain.compliance import IdentityStatus


class IdentityProvider(Protocol):
    name: str

    def start(self, contributor_id: str) -> str:
        """Open a verification for this contributor and return the provider's reference.

        The contributor hands their documents to the provider, never to us.
        """

    def status(self, reference: str) -> IdentityStatus: ...
