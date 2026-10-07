"""Logging: one JSON object per line in deployments, readable text in development.

Fields passed with `extra=` become top-level keys, so a money event is logged as
`{"message": "money event", "kind": "released", "issue_id": "ISS-1006", ...}` and a
log pipeline can filter on any of them without parsing prose.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from misthos.observability.context import actor, correlation_id

# What every LogRecord has, so anything else on a record came from `extra=`.
_STANDARD = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class ContextFilter(logging.Filter):
    """Put the correlation id and the actor on every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = correlation_id.get() or "-"
        record.actor = actor.get() or "-"
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD and key not in payload:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(correlation_id)s] %(message)s"


def configure(*, json_lines: bool, level: str = "INFO") -> None:
    """Replace the root handlers with one that writes structured or plain lines."""
    handler = logging.StreamHandler()
    handler.addFilter(ContextFilter())
    handler.setFormatter(JsonFormatter() if json_lines else logging.Formatter(TEXT_FORMAT))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # Migrations run once at startup; their plugin chatter is not news.
    logging.getLogger("alembic").setLevel(logging.WARNING)
