"""The compose file: the four processes against Postgres and Redis, wired the way
the architecture says. CI boots it for real; this catches the wiring mistakes that
would only show up as a service that never becomes healthy."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))


def test_runs_the_four_processes_and_their_stores(compose: dict) -> None:
    assert set(compose["services"]) == {"postgres", "redis", "api", "worker", "web", "edge"}


def test_every_long_running_service_says_when_it_is_healthy(compose: dict) -> None:
    for name, service in compose["services"].items():
        assert "healthcheck" in service, name


def test_the_api_and_the_worker_share_the_stores(compose: dict) -> None:
    api, worker = compose["services"]["api"], compose["services"]["worker"]
    for service in (api, worker):
        env = service["environment"]
        assert env["MISTHOS_DATABASE_URL"].startswith("postgresql+psycopg://")
        assert env["MISTHOS_REDIS_URL"].startswith("redis://")
        assert set(service["depends_on"]) == {"postgres", "redis"}


def test_the_worker_owns_the_sweeper(compose: dict) -> None:
    assert compose["services"]["api"]["environment"]["MISTHOS_SWEEPER_IN_PROCESS"] == "false"
    assert compose["services"]["worker"]["command"] == ["python", "-m", "misthos.workers"]


def test_each_built_service_has_a_dockerfile(compose: dict) -> None:
    for name, service in compose["services"].items():
        if "build" in service:
            assert (ROOT / service["build"] / "Dockerfile").is_file(), name


def test_the_web_app_proxies_the_api_but_not_the_internal_metrics() -> None:
    conf = (ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")
    assert "location /api/" in conf and "proxy_pass http://api:8000" in conf
    assert "location /internal" not in conf
    assert "X-Request-ID $request_id" in conf
