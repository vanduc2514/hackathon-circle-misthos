"""Where MisthosEscrow lives.

The address used to be a constant, which meant the API could show an address that
held nothing. It now comes from the record the deploy script writes when it
broadcasts, so the address shown is the one that was deployed. The record is still
only a claim: `ArcEscrow.verify` checks the chain holds code there before anything
trusts it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from misthos.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[5]
DEPLOYMENTS_DIR = REPO_ROOT / "contracts" / "deployments"

# Shown only while simulated and nothing has been deployed. It is labelled as
# simulated wherever it appears, and refused outside the simulation.
SIMULATED_ESCROW = "0x7A3f19bE5c2D80416aB9e0C7d3F5a12B6c8E4d90"

DeploymentSource = Literal["override", "record", "simulation"]


class EscrowNotDeployed(RuntimeError):
    """Real settlement was asked for, but there is no deployment to settle against."""


@dataclass(frozen=True)
class EscrowDeployment:
    address: str
    chain_id: int
    source: DeploymentSource
    deployed_at_block: int | None = None


def record_path(cfg: Settings) -> Path:
    if cfg.escrow_deployment_file:
        return Path(cfg.escrow_deployment_file)
    return DEPLOYMENTS_DIR / f"{cfg.chain_id}.json"


def load_deployment(cfg: Settings) -> EscrowDeployment:
    """Resolve the escrow address: explicit override, then the deploy record.

    Outside the simulation there is no fallback. Settling against an address
    nobody deployed is the failure this exists to prevent.
    """
    if cfg.escrow_contract:
        return EscrowDeployment(cfg.escrow_contract, cfg.chain_id, "override")

    path = record_path(cfg)
    if path.is_file():
        record = json.loads(path.read_text())
        if int(record["chainId"]) != cfg.chain_id:
            raise EscrowNotDeployed(
                f"{path} records chain {record['chainId']}, but the API is on {cfg.chain_id}"
            )
        return EscrowDeployment(
            address=record["escrow"],
            chain_id=cfg.chain_id,
            source="record",
            deployed_at_block=record.get("deployedAtBlock"),
        )

    if cfg.simulated:
        return EscrowDeployment(SIMULATED_ESCROW, cfg.chain_id, "simulation")
    raise EscrowNotDeployed(
        f"no MisthosEscrow deployment for chain {cfg.chain_id}: run `mise run contracts:deploy` "
        f"or set MISTHOS_ESCROW_CONTRACT"
    )
