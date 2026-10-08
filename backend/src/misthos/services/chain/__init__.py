"""The settlement chain, behind the one interface the store moves money through.

`SimulatedChain` keeps the escrow's books itself and contacts nothing; it is what
runs while `MISTHOS_SIMULATED` is true. The Arc client implements the same protocol
against the deployed `MisthosEscrow` (#69). Either way the store never fabricates a
transfer: the transaction reference on a commitment, a release or a refund is the
one the chain returned.
"""

from __future__ import annotations

from misthos.services.chain.arc import ArcEscrow, ChainUnavailable
from misthos.services.chain.base import ChainGateway, ChainRevert, NotCommitted
from misthos.services.chain.deployment import (
    EscrowDeployment,
    EscrowNotDeployed,
    load_deployment,
)
from misthos.services.chain.simulated import SimulatedChain

__all__ = [
    "ArcEscrow",
    "ChainGateway",
    "ChainRevert",
    "ChainUnavailable",
    "EscrowDeployment",
    "EscrowNotDeployed",
    "NotCommitted",
    "SimulatedChain",
    "load_deployment",
]
