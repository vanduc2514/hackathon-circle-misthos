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


def test_the_proxy_replaces_the_forwarded_for_header_rather_than_extending_it() -> None:
    """The rate limiter and every log line's `actor` key on the client address.

    `$proxy_add_x_forwarded_for` keeps whatever the caller sent and appends the real
    address, and uvicorn reads the leftmost entry, so a caller could pick its own
    rate-limit key and forge the `actor` on each log line it causes.
    """
    conf = (ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")
    directives = [
        line.strip()
        for line in conf.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in directives
    assert not any("proxy_add_x_forwarded_for" in line for line in directives)


def test_the_api_does_not_trust_forwarded_headers_from_everywhere() -> None:
    """`*` would believe the header whoever sent it, including a direct connection.

    But the trusted set still has to cover where the proxy comes from: a compose
    network on Docker Desktop lands in 192.168/16, outside 172.16/12. Trusting only
    the Linux range left the header unread there, so every client shared one
    rate-limit bucket and the identity in the logs was the proxy's.
    """
    dockerfile = (ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
    assert '"--forwarded-allow-ips"' in dockerfile
    trusted = dockerfile.split("--forwarded-allow-ips")[1]
    assert '"*"' not in trusted
    # Every range a Docker bridge can be allocated from, on Linux and on Desktop.
    for reachable in ("127.0.0.1", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"):
        assert reachable in trusted, reachable


def _trusted_ranges() -> list:
    """The IPv4 networks `--forwarded-allow-ips` names, across the line continuation."""
    import ipaddress
    import re

    dockerfile = (ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
    raw = dockerfile.split("--forwarded-allow-ips")[1].split("]")[0].replace('"', "")
    raw = raw.replace("\\", " ").replace("\n", " ")
    networks = []
    for candidate in (c for c in re.split(r"[,\s]+", raw) if c):
        try:
            networks.append(ipaddress.ip_network(candidate, strict=False))
        except ValueError:
            continue  # the IPv6 loopback, which ip_address handles below
    return networks


def test_the_trusted_range_covers_a_docker_network() -> None:
    """Guards the mistake above at the level it happened: is a real bridge covered?"""
    import ipaddress

    networks = _trusted_ranges()
    assert networks, "no IPv4 range is trusted at all"
    # The default bridge on this project's Docker Desktop, the Linux bridge range, and
    # loopback. Trusting only 172.16/12 left 192.168 unread, so the proxy's header was
    # ignored and every client shared one rate-limit bucket.
    for address in ("192.168.107.5", "172.17.0.2", "10.0.0.5", "127.0.0.1"):
        ip = ipaddress.ip_address(address)
        assert any(ip in net for net in networks), f"{address} is not trusted"
