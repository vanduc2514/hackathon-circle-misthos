"""Compliance providers.

Identity is Stripe Identity and screening is the official OFAC SDN list. There is no
fake fallback: a missing Stripe key leaves a first payout pending, and an unreadable
SDN list blocks funding and payouts.
"""

from __future__ import annotations

from misthos.config import Settings
from misthos.services.compliance.identity import IdentityProvider
from misthos.services.compliance.ofac import OfacSdnScreening
from misthos.services.compliance.screening import ScreeningProvider
from misthos.services.compliance.stripe_identity import (
    IdentityUnavailable,
    StripeIdentity,
    UnconfiguredIdentity,
)

__all__ = [
    "IdentityProvider",
    "IdentityUnavailable",
    "ScreeningProvider",
    "build_identity",
    "build_screening",
]


def build_screening(cfg: Settings) -> ScreeningProvider:
    return OfacSdnScreening(
        cfg.ofac_sdn_url,
        cfg.ofac_refresh_seconds,
        cfg.ofac_cache_path or None,
    )


def build_identity(cfg: Settings) -> IdentityProvider:
    if cfg.stripe_secret_key:
        return StripeIdentity(cfg.stripe_secret_key, cfg.stripe_api_url)
    return UnconfiguredIdentity()
