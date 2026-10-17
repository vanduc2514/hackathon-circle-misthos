"""The marker that puts a route on the public API page (#208).

The web app's API page is built from `openapi.json`: it lists the operations that
carry `x-misthos-public`, with the group, the name and the one-line summary here, and
nothing else. A route a stranger cannot call without signing in is never marked, so
the page cannot advertise one. The x402 routes the edge sells come from the edge's own
catalog, at its prices (`GET /api/v1/catalog/paid`).
"""

from __future__ import annotations

from typing import Any, Literal

Group = Literal["Core", "Marketplace", "Analytics", "Trust"]
GROUPS: tuple[Group, ...] = ("Core", "Marketplace", "Analytics", "Trust")


def public(group: Group, name: str, summary: str) -> dict[str, Any]:
    """`openapi_extra` for a route anyone may call, as the API page shows it."""
    return {"x-misthos-public": {"group": group, "name": name, "summary": summary}}
