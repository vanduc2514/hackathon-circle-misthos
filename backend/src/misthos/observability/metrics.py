"""Prometheus metrics for the paths the non-functional requirements name.

Price in under 60 seconds, a verdict in under five minutes for about six dollars,
and a ledger that never silently drifts from the chain: each is a series here, so
each is observable rather than asserted. The API serves them at
`/internal/metrics`; a separate worker serves them on
`MISTHOS_WORKER_METRICS_PORT`. Neither belongs on the public internet.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

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
DIVERGENCES = Gauge(
    "misthos_ledger_divergences",
    "Issues whose ledger and on-chain escrow disagreed at the last reconciliation",
)
DIVERGENCE_ALERTS = Counter(
    "misthos_ledger_divergence_alerts_total", "Divergences raised as alerts"
)
SWEEP_SECONDS = Histogram("misthos_sweep_seconds", "Duration of one sweeper pass")
SWEEP_FAILURES = Counter(
    "misthos_sweep_failures_total", "Issues whose timed action or review failed in a pass"
)
