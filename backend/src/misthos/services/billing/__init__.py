"""Subscription payments (#53): USDC from the publisher's wallet to the platform's.

Payments are verified directly on Arc.
"""

from __future__ import annotations

from misthos.config import settings
from misthos.services.billing.arc import TRANSFER_TOPIC, ArcRail
from misthos.services.billing.base import PaymentError, PaymentRail, Received

__all__ = [
    "TRANSFER_TOPIC",
    "ArcRail",
    "PaymentError",
    "PaymentRail",
    "Received",
    "build_rail",
]


def build_rail() -> PaymentRail:
    return ArcRail(settings.rpc_url, settings.usdc_address)
