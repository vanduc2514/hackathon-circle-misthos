"""Correlation ids and request latency for every HTTP request."""

from __future__ import annotations

import time
from typing import Any

from misthos.observability import context
from misthos.observability.metrics import HTTP_SECONDS

HEADER = b"x-request-id"


class RequestContext:
    """ASGI middleware: binds a correlation id and the actor for the request, returns
    the id as `X-Request-ID`, and times the request by its route template."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        sent = dict(scope.get("headers") or []).get(HEADER)
        correlation = context.accept_or_new(sent.decode("latin-1") if sent else None)
        client = scope.get("client")
        who = client[0] if client else None
        status = {"code": 500}
        started = time.perf_counter()

        async def send_with_id(message: dict) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                message.setdefault("headers", [])
                message["headers"] = [*message["headers"], (HEADER, correlation.encode())]
            await send(message)

        with context.bound(correlation, who):
            try:
                await self.app(scope, receive, send_with_id)
            finally:
                HTTP_SECONDS.labels(
                    method=scope.get("method", ""),
                    route=route_template(scope),
                    status=str(status["code"]),
                ).observe(time.perf_counter() - started)


def route_template(scope: dict) -> str:
    """The route a request matched, with its parameters named rather than filled in,
    so ISS-1001 and ISS-1002 are one series. Unmatched paths share one series too, so
    a scanner cannot create a series per path it tries."""
    if scope.get("route") is None:
        return "unmatched"
    names = {str(v): k for k, v in (scope.get("path_params") or {}).items()}
    return "/".join(
        "{" + names[part] + "}" if part in names else part for part in scope["path"].split("/")
    )
