"""Subscription payments (#53): USDC from the publisher's wallet to the platform's.

The simulation has its own rail; anywhere else the payment is read from Arc.
"""

from __future__ import annotations

from misthos.config import settings
from misthos.services.billing.arc import TRANSFER_TOPIC, ArcRail
from misthos.services.billing.base import PaymentError, PaymentRail, Received
from misthos.services.billing.simulated import SimulatedRail

__all__ = [
    "TRANSFER_TOPIC",
    "ArcRail",
    "PaymentError",
    "PaymentRail",
    "Received",
    "SimulatedRail",
    "build_rail",
]


def build_rail() -> PaymentRail:
    if settings.simulated:
        return SimulatedRail()
    return ArcRail(settings.rpc_url, settings.usdc_address)
