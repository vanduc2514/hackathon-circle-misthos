"""The network and its money are named from the chain id, and never understated."""

from __future__ import annotations

import pytest

from misthos.config import Settings
from misthos.domain.network import network_for


class TestNetwork:
    def test_arc_testnet_money_is_test_money(self) -> None:
        net = network_for(5042002, simulated=False)
        assert (net.name, net.money) == ("arc-testnet", "test")
        assert "no real value" in net.description

    def test_live_arc_mainnet_is_real_money(self) -> None:
        net = network_for(5042, simulated=False)
        assert (net.name, net.money) == ("arc-mainnet", "real")
        assert "irreversible" in net.description

    @pytest.mark.parametrize("chain_id", [1, 8453, 137, 31337, 0])
    def test_a_chain_that_is_not_arc_is_never_called_worthless(self, chain_id: int) -> None:
        # Ethereum (1) and Base (8453) carry real USDC. Saying "no real value" for an
        # unknown chain is the mislabelling this module exists to prevent.
        net = network_for(chain_id, simulated=False)
        assert net.money == "unknown"
        assert net.name == f"chain-{chain_id}"
        assert "no real value" not in net.description
        assert "Treat its USDC as real" in net.description

    @pytest.mark.parametrize("chain_id", [5042, 5042002, 8453])
    def test_the_simulation_says_nothing_moves_whatever_the_chain(self, chain_id: int) -> None:
        net = network_for(chain_id, simulated=True)
        assert net.money == "simulated"
        assert "no money moves" in net.description


class TestSettings:
    def test_the_chain_label_follows_the_chain_id_and_cannot_be_set(self) -> None:
        assert Settings(chain_id=5042).chain == "arc-mainnet"
        # The old free-text setting is ignored rather than trusted.
        assert Settings(chain_id=5042, **{"chain": "arc-testnet"}).chain == "arc-mainnet"
