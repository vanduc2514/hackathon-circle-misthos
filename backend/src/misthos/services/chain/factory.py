"""Which escrow the store settles through.

The store settles through the deployed `MisthosEscrow`, with the keys it may sign
with read from the secret store by reference at signing time. A run without a
deployment refuses to start, rather than discover at the first approval that there
is nowhere to put the money.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from misthos.config import Settings
from misthos.services.attestor import load_attestor_key, load_owner_key, secret_store
from misthos.services.chain.arc import ArcEscrow
from misthos.services.chain.base import ChainGateway
from misthos.services.chain.deployment import load_deployment
from misthos.services.chain.rpc import JsonRpc, Sender


def build_chain(
    cfg: Settings,
    issue_ids: Callable[[], Iterable[str]],
) -> ChainGateway:
    store = secret_store(cfg.secret_store_dir)
    rpc = JsonRpc(cfg.rpc_url)

    def owner_key() -> str:
        return load_owner_key(store, cfg.owner_secret_ref).material

    def attestor_key() -> str:
        return load_attestor_key(store, cfg.attestor_secret_ref).material

    return ArcEscrow(
        cfg.rpc_url,
        load_deployment(cfg),
        issue_ids=issue_ids,
        owner=Sender(rpc, cfg.chain_id, owner_key),
        attestor=Sender(rpc, cfg.chain_id, attestor_key),
    )
