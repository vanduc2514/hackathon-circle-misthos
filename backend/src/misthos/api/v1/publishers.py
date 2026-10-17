"""An organisation's own controls and records: its spending policy and its audit
export (epic #13), and its repositories (#6).

These are the organisation's alone: a signed-in publisher sees and sets its own.
An operator exports the audit record with
`python -m misthos.services.audit`.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.concurrency import run_in_threadpool

from misthos.api.session import SIGNED_IN, require_owner
from misthos.config import settings
from misthos.domain.policy import PolicyRefusal
from misthos.schemas import (
    Account,
    AuditExport,
    PolicyRequest,
    Publisher,
    RepositoriesOut,
)
from misthos.services.audit import export, to_csv
from misthos.store import store

router = APIRouter(prefix="/publishers", tags=["publishers"])


@router.put("/{publisher_id}/policy", response_model=Publisher)
async def set_policy(
    publisher_id: str,
    payload: PolicyRequest,
    account: Account | None = SIGNED_IN,
) -> Publisher:
    """Replace the organisation's spending policy: a release threshold with its named
    approvers, and monthly limits per issue label."""
    require_owner(account, publisher_id, "set this spending policy")
    try:
        return await run_in_threadpool(
            lambda: store.set_policy(
                publisher_id,
                approval_threshold_usdc=payload.approval_threshold_usdc,
                approvers=payload.approvers,
                category_limits=payload.category_limits,
            )
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"no publisher {publisher_id}") from exc
    except PolicyRefusal as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get(
    "/{publisher_id}/audit",
    response_model=AuditExport,
    responses={200: {"content": {"text/csv": {}}}},
)
async def audit(
    publisher_id: str,
    format: Literal["json", "csv"] = Query(default="json"),
    account: Account | None = SIGNED_IN,
) -> AuditExport | Response:
    """The decision record and the money events for every issue the organisation
    funded, unedited, as JSON or one sortable CSV."""
    require_owner(account, publisher_id, "export this audit record")
    found = await run_in_threadpool(export, store, publisher_id)
    if found is None:
        raise HTTPException(status_code=404, detail=f"no publisher {publisher_id}")
    if format == "csv":
        return Response(
            content=to_csv(found),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="misthos-audit-{publisher_id}.csv"'
            },
        )
    return found


@router.get("/{publisher_id}/repositories", response_model=RepositoriesOut)
async def repositories(publisher_id: str, account: Account | None = SIGNED_IN) -> RepositoriesOut:
    """The repositories this publisher installed the GitHub App on (#6). An issue there
    given the label is priced without opening the web app."""
    require_owner(account, publisher_id, "see these repositories")
    if await run_in_threadpool(store.get_publisher, publisher_id) is None:
        raise HTTPException(status_code=404, detail=f"no publisher {publisher_id}")
    slug = settings.github_app_slug
    connected = await run_in_threadpool(store.connections_of, publisher_id)
    return RepositoriesOut(
        repositories=connected,
        label=settings.github_label,
        # Plain: installing connects repositories for label-driven pricing. It does not
        # link anyone, so it carries no state and finishes no link.
        install_url=f"https://github.com/apps/{slug}/installations/new" if slug else None,
        app_name=slug or None,
        # Once something is connected, adding or removing repositories happens on GitHub's
        # own page for that installation -- there is no API for it -- so that is what to
        # point at rather than an install page the App is already on.
        settings_url=(
            f"https://github.com/settings/installations/{connected[0].installation_id}"
            if connected
            else None
        ),
    )
