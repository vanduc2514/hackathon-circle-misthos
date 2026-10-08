"""Which network this is, and whether its money is real.

The label used to be a free-text setting that could say "arc-testnet" while the
chain id pointed at mainnet. Judges and users weigh real usage differently from a
demo, so the label is derived from the chain id and nothing else, and the money is
described by the one fact that matters: can it be lost.

Only the two Arc networks are known. Any other chain id is named by its id and its
money is "unknown": this module will not call USDC on a chain it does not know
worthless, because on most chains it is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ARC_MAINNET = 5042
ARC_TESTNET = 5042002

MoneyKind = Literal["simulated", "test", "real", "unknown"]


@dataclass(frozen=True)
class Network:
    name: str
    """`arc-mainnet`, `arc-testnet`, or `chain-<id>` for anything else."""
    label: str
    chain_id: int
    money: MoneyKind

    @property
    def description(self) -> str:
        return {
            "simulated": "Simulation: no chain is contacted and no money moves.",
            "test": f"{self.label}: test USDC from a faucet, with no real value.",
            "real": f"{self.label}: real USDC. Settlement is irreversible.",
            "unknown": (
                f"{self.label} is not an Arc network. Treat its USDC as real: "
                "settlement there may be irreversible."
            ),
        }[self.money]


def network_for(chain_id: int, *, simulated: bool) -> Network:
    """Describe the network from its chain id alone."""
    if chain_id == ARC_MAINNET:
        name, label, live = "arc-mainnet", "Arc mainnet", "real"
    elif chain_id == ARC_TESTNET:
        name, label, live = "arc-testnet", "Arc testnet", "test"
    else:
        name, label, live = f"chain-{chain_id}", f"Chain {chain_id}", "unknown"
    money: MoneyKind = "simulated" if simulated else live  # type: ignore[assignment]
    return Network(name=name, label=label, chain_id=chain_id, money=money)
