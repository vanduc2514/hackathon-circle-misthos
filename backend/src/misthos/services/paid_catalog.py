"""The edge's catalog, for the web app's API page (#208).

The page shows the x402 routes beside the free ones, and their prices must be the ones
the edge charges, so they are read from the edge (`GET /paid`) rather than written
down twice. The core reads it over the internal network at `edge_url`, keeps it a
minute, and answers `available: false` when the edge cannot be read, so the page still
shows the free routes. Nothing from the edge is passed on that is not a route, a price
or how to pay.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx

from misthos.config import settings
from misthos.schemas import PaidCatalogOut, PaidRouteOut

TTL_SECONDS = 60
GROUPS = {"Core", "Marketplace", "Analytics", "Trust"}

_lock = threading.Lock()
_kept: tuple[float, PaidCatalogOut] | None = None


def _routes(raw: Any) -> list[PaidRouteOut]:
    routes: list[PaidRouteOut] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or item.get("available") is False:
            continue
        price = item.get("price") or {}
        if not isinstance(price, dict) or not price.get("usdc"):
            continue
        group = item.get("group") if item.get("group") in GROUPS else "Analytics"
        try:
            routes.append(
                PaidRouteOut(
                    id=str(item["id"]),
                    method=item["method"],
                    path=str(item["path"]),
                    price={
                        "usdc": str(price["usdc"]),
                        "base_units": str(price.get("base_units", "")),
                    },
                    name=str(item.get("name") or item["id"]),
                    summary=str(item.get("summary") or item.get("description") or ""),
                    group=group,
                    returns=item.get("returns"),
                    free_tier=bool(item.get("free_tier")),
                    is_async=bool(item.get("async")),
                    capped=bool(item.get("capped")),
                    note=item.get("note"),
                )
            )
        except (KeyError, ValueError):
            continue
    return routes


def read(client: httpx.Client | None = None) -> PaidCatalogOut:
    """Ask the edge for its catalog."""
    base = settings.paid_public_url.rstrip("/") or None
    try:
        http = client or httpx.Client(timeout=3.0)
        try:
            res = http.get(
                settings.edge_url.rstrip("/") + "/paid", headers={"Accept": "application/json"}
            )
        finally:
            if client is None:
                http.close()
        body = res.json() if res.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        body = None
    if not isinstance(body, dict):
        return PaidCatalogOut(available=False, base_url=base)
    accepts = body.get("accepts") if isinstance(body.get("accepts"), list) else []
    first = accepts[0] if accepts and isinstance(accepts[0], dict) else {}
    return PaidCatalogOut(
        available=True,
        base_url=base,
        networks=[str(n) for n in body.get("networks") or []],
        pay_to=body.get("payTo"),
        asset=first.get("asset"),
        certificate_signer=body.get("certificate_signer"),
        routes=_routes(body.get("routes")),
    )


def catalog(now: float | None = None) -> PaidCatalogOut:
    """The catalog, kept a minute."""
    global _kept
    at = time.monotonic() if now is None else now
    with _lock:
        if _kept is not None and at - _kept[0] < TTL_SECONDS:
            return _kept[1]
    found = read()
    with _lock:
        _kept = (at, found)
    return found


def forget() -> None:
    """Drop the kept catalog. For the tests."""
    global _kept
    with _lock:
        _kept = None
