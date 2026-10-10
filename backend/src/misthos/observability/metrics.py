"""Prometheus metrics for the paths the non-functional requirements name.

Price in under 60 seconds, a verdict in under five minutes for about six dollars,
and a ledger that never silently drifts from the chain: each is a series here, so
each is observable rather than asserted. The API serves them at
`/internal/metrics`; a separate worker serves them on
`MISTHOS_WORKER_METRICS_PORT`. Neither belongs on the public internet.

A process exports only what it writes. Requests, prices, reviews and money events
happen in either process and are labelled, so a process that never writes one shows
no sample for it. The sweeper's series have no labels and read 0 from the moment
they exist, so an API that leaves the sweeping to a worker, as compose runs it,
would report "no divergence" forever while the worker held the real figure. They
live in a registry of their own, served only by the process that runs the sweeper.
"""

from __future__ import annotations

from collections.abc import Iterator
from wsgiref.simple_server import WSGIServer

from prometheus_client import (
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    start_http_server,
)
from prometheus_client.metrics_core import Metric

HTTP_SECONDS = Histogram(
    "misthos_http_request_seconds",
    "HTTP request latency",
    ["method", "route", "status"],
)
PRICE_SECONDS = Histogram(
    "misthos_price_seconds",
    "Time to read an issue and propose its price",
    ["source"],
    buckets=(0.01, 0.05, 0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
)
REVIEW_SECONDS = Histogram(
    "misthos_review_seconds",
    "Time for the review agent to judge one commit",
    ["reviewer", "verdict"],
    buckets=(0.1, 1, 5, 15, 30, 60, 120, 300, 600),
)
REVIEW_COST_USDC = Histogram(
    "misthos_review_cost_usdc",
    "Inference cost of one review",
    ["reviewer"],
    buckets=(0, 0.01, 0.05, 0.1, 0.5, 1, 2, 4, 6, 10, 20),
)
MONEY_EVENTS = Counter("misthos_money_events_total", "Money events booked in the ledger", ["kind"])

# Written only by the sweeper's passes.
SWEEPER = CollectorRegistry()
DIVERGENCES = Gauge(
    "misthos_ledger_divergences",
    "Issues whose ledger and on-chain escrow disagreed at the last reconciliation",
    registry=SWEEPER,
)
DIVERGENCE_ALERTS = Counter(
    "misthos_ledger_divergence_alerts_total", "Divergences raised as alerts", registry=SWEEPER
)
SWEEP_SECONDS = Histogram("misthos_sweep_seconds", "Duration of one sweeper pass", registry=SWEEPER)
SWEEP_FAILURES = Counter(
    "misthos_sweep_failures_total",
    "Issues whose timed action or review failed in a pass",
    registry=SWEEPER,
)
SUPPORT_OVERDUE = Gauge(
    "misthos_support_overdue",
    "Support requests past their first-response deadline and still unanswered (#53)",
    registry=SWEEPER,
)
SUPPORT_OVERDUE_ALERTS = Counter(
    "misthos_support_overdue_alerts_total",
    "Support requests raised as overdue, once each",
    registry=SWEEPER,
)


class _Joined:
    """Several registries read as one, for a scrape endpoint that serves one."""

    def __init__(self, *registries: CollectorRegistry) -> None:
        self._registries = registries

    def collect(self) -> Iterator[Metric]:
        for registry in self._registries:
            yield from registry.collect()


_SWEEPING = CollectorRegistry(auto_describe=False)
_SWEEPING.register(_Joined(REGISTRY, SWEEPER))


def exported(*, sweeper: bool) -> CollectorRegistry:
    """What a process serves its scraper: what every process writes, and the sweeper's
    series only when this process runs the sweeper."""
    return _SWEEPING if sweeper else REGISTRY


def serve(port: int) -> WSGIServer:
    """The worker's scrape endpoint, on every interface so a scraper on the compose
    network reaches it. The worker always runs the sweeper."""
    server, _ = start_http_server(port, registry=exported(sweeper=True))
    return server
