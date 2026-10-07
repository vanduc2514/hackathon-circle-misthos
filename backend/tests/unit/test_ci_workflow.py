"""The CI contract.

Three guardrails in the backlog — money-path invariants (#35), verdict agreement
(#37) and the price floor — are only as real as the workflow that enforces them.
Nothing else in the repository notices when a job is dropped or a command is
renamed into something that no longer fails, so this test reads the workflow and
checks that every runtime is still covered and that no step swallows a failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "ci.yml"

# yaml.safe_load parses the unquoted `on:` key as the boolean True (YAML 1.1).
TRIGGERS_KEY = True

EXPECTED_COMMANDS = {
    "backend": ["mise run lint:backend", "mise run test:backend"],
    "web": ["mise run lint:web", "mise run test:web"],
    "edge": ["mise run lint:edge"],
    "contracts": ["mise run test:contracts"],
}


@pytest.fixture(scope="module")
def workflow() -> dict:
    assert WORKFLOW.is_file(), f"{WORKFLOW} is missing: a pull request would run nothing"
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_it_runs_on_every_pull_request(workflow: dict) -> None:
    assert "pull_request" in workflow[TRIGGERS_KEY]


def test_every_runtime_has_its_own_job(workflow: dict) -> None:
    assert set(EXPECTED_COMMANDS) <= set(workflow["jobs"])


@pytest.mark.parametrize("runtime", sorted(EXPECTED_COMMANDS))
def test_each_job_installs_its_dependencies_and_runs_its_suites(
    workflow: dict, runtime: str
) -> None:
    commands = [
        step["run"] for step in workflow["jobs"][runtime]["steps"] if "run" in step
    ]

    assert commands[0] == f"mise run setup:{runtime}", commands
    assert commands[1:] == EXPECTED_COMMANDS[runtime], commands


def test_no_step_swallows_a_failure(workflow: dict) -> None:
    for name, job in workflow["jobs"].items():
        for step in job["steps"]:
            assert not step.get("continue-on-error"), f"{name} ignores a failure"


def test_the_backend_suite_also_runs_against_postgres(workflow: dict) -> None:
    """Memory is the default, so without this job nothing would exercise the database."""
    job = workflow["jobs"]["backend-postgres"]
    commands = [step["run"] for step in job["steps"] if "run" in step]

    assert commands == ["mise run setup:backend", "mise run test:backend"]
    assert job["env"]["MISTHOS_DATABASE_URL"].startswith("postgresql")
    assert "postgres" in job["services"]


def test_the_guards_also_run_against_redis(workflow: dict) -> None:
    """Without Redis the lock, idempotency keys and rate limits are per process, so this
    job is the only thing that proves them where a deployment would keep them."""
    job = workflow["jobs"]["backend-postgres"]

    assert job["env"]["MISTHOS_REDIS_URL"].startswith("redis://")
    assert "redis" in job["services"]


def test_the_whole_system_boots_from_compose(workflow: dict) -> None:
    """A Dockerfile or compose change that breaks the shipped system fails the build."""
    commands = [step["run"] for step in workflow["jobs"]["compose"]["steps"] if "run" in step]
    up = next(c for c in commands if c.startswith("docker compose up"))
    assert "--wait" in up and "--build" in up
    assert any("/api/v1/health" in c and "--fail" in c for c in commands)
