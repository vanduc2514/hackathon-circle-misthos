"""Run the sweeper as its own process: `python -m misthos.workers`.

Only meaningful against a database. With in-memory state this process would sweep
a private copy nobody else can see, so it refuses to start and says why.
"""

from __future__ import annotations

import asyncio
import sys

from prometheus_client import start_http_server

from misthos.config import settings
from misthos.observability import logs
from misthos.store import store
from misthos.workers.sweeper import run_forever


def main() -> int:
    logs.configure(json_lines=settings.log_json, level=settings.log_level)
    if not settings.database_url:
        print(
            "misthos.workers needs MISTHOS_DATABASE_URL: with in-memory state the API "
            "runs the sweeper itself (MISTHOS_SWEEPER_IN_PROCESS=true).",
            file=sys.stderr,
        )
        return 1
    if settings.worker_metrics_port:
        start_http_server(settings.worker_metrics_port)
    try:
        asyncio.run(run_forever(store, settings.sweep_interval_seconds))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
