"""Test fakes and fixtures for testing without external services."""

from tests.fakes.billing import SimulatedRail
from tests.fakes.chain import SimulatedChain
from tests.fakes.github import SimulatedGitHub
from tests.fakes.seed import (
    ISSUE_SPECS,
    _ISSUE_SPECS,
    seed_contributors,
    seed_publishers,
    seed_store_for_tests,
)
from tests.fakes.wallets import simulated_address

__all__ = [
    "SimulatedChain",
    "SimulatedGitHub",
    "SimulatedRail",
    "simulated_address",
    "ISSUE_SPECS",
    "_ISSUE_SPECS",
    "seed_publishers",
    "seed_contributors",
    "seed_store_for_tests",
]
