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

# What every LogRecord has, so anything else on a record came from `extra=`. uvicorn
# adds `color_message`, the same message with terminal colour codes, which is
# decoration rather than a field.
_STANDARD = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "color_message"}

# uvicorn gives these loggers handlers of their own and stops them propagating, so its
# startup lines and its access log would bypass the handler below: plain `INFO: ...
# "GET /api/v1/health" 200 OK` lines between the JSON ones, and no correlation id.
# Routed through the root handler, the access line is written while the request's
# context is still bound (uvicorn logs it from inside the middleware's `send`), so it
# carries the request's id.
_UVICORN = ("uvicorn", "uvicorn.error", "uvicorn.access")


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


def uvicorn_configured() -> bool:
    """Whether uvicorn has given its loggers handlers in this process, which it does
    just before it imports the app it is about to serve."""
    return any(logging.getLogger(name).handlers for name in _UVICORN)


def configure(*, json_lines: bool, level: str = "INFO") -> None:
    """Replace the root handlers with one that writes structured or plain lines, and
    send uvicorn's loggers through it too, so one process writes one kind of line."""
    handler = logging.StreamHandler()
    handler.addFilter(ContextFilter())
    handler.setFormatter(JsonFormatter() if json_lines else logging.Formatter(TEXT_FORMAT))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    for name in _UVICORN:
        logger = logging.getLogger(name)
        # One uvicorn left without a handler is one it switched off, such as the
        # access log under --no-access-log; it stays off.
        if logger.handlers:
            logger.handlers = []
            logger.propagate = True
    # Migrations run once at startup; their plugin chatter is not news.
    logging.getLogger("alembic").setLevel(logging.WARNING)
