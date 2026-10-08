"""The settlement chain, behind the one interface the store moves money through.

`SimulatedChain` keeps the escrow's books itself and contacts nothing; it is what
runs while `MISTHOS_SIMULATED` is true. The Arc client implements the same protocol
against the deployed `MisthosEscrow` (#69). Either way the store never fabricates a
transfer: the transaction reference on a commitment, a release or a refund is the
one the chain returned.
"""

from __future__ import annotations

from misthos.services.chain.base import ChainGateway, ChainRevert
from misthos.services.chain.simulated import SimulatedChain

__all__ = ["ChainGateway", "ChainRevert", "SimulatedChain"]
