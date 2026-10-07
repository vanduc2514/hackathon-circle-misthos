"""Plans and paying for them (#53).

`GET /plans` is public. A publisher's subscription is its own: it chooses a plan, is
told what to send, sends USDC from its wallet on Arc, and confirms the transaction,
which the server reads from the chain before switching the plan on. Team is bought
this way without a conversation; Enterprise is agreed with us.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool

from misthos.api.session import SIGNED_IN, require_owner_or_simulation
from misthos.config import settings
from misthos.domain import plans
from misthos.repositories import PaymentAlreadyUsed
from misthos.schemas import (
    Account,
    PaymentConfirmation,
    PlanOut,
    SubscribeRequest,
    SubscriptionOut,
)
from misthos.services.billing import PaymentError
from misthos.services.coordination import Busy
from misthos.store import (
    BillingUnavailable,
    NoPaymentDue,
    NotSimulated,
    PaymentNotFound,
    PlanNotSelfServe,
    store,
)

router = APIRouter(tags=["plans"])


def _plan(plan: plans.Plan) -> PlanOut:
    return PlanOut(
        id=plan.id,  # type: ignore[arg-type]
        name=plan.name,
        audience=plan.audience,
        monthly_usdc=f"{plan.monthly.decimal:.2f}",
        price_from=not plan.self_serve,
        self_serve=plan.self_serve,
        take_rate_percent=float(plan.take_rate * 100),
        minimum_fix_usdc=f"{plan.minimum_fix.decimal:.2f}",
        features=[plans.FEATURE_NAMES[f] for f in plans.Feature if f in plan.features],
        support=plan.support,
    )


@router.get("/plans", response_model=list[PlanOut])
async def list_plans() -> list[PlanOut]:
    """What each plan costs and includes, with the take rate and the smallest fix
    it will price."""
    return [_plan(p) for p in plans.PLANS.values()]


async def _call(step, *args):  # type: ignore[no-untyped-def]
    try:
        return await run_in_threadpool(step, *args)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"no publisher {args[0]}") from exc
    except (PlanNotSelfServe, NoPaymentDue, PaymentAlreadyUsed, Busy) as exc:
        detail = (
            "that transaction already paid for a period"
            if isinstance(exc, PaymentAlreadyUsed)
            else str(exc)
        )
        raise HTTPException(status_code=409, detail=detail) from exc
    except PaymentNotFound as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PaymentError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except BillingUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except NotSimulated as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.get("/publishers/{publisher_id}/subscription", response_model=SubscriptionOut)
async def subscription(publisher_id: str, account: Account | None = SIGNED_IN) -> SubscriptionOut:
    """The plan in force, its period, what is waiting to be paid, and every payment."""
    require_owner_or_simulation(account, publisher_id, "see this subscription")
    return await _call(store.subscription, publisher_id)


@router.post("/publishers/{publisher_id}/subscription", response_model=SubscriptionOut)
async def subscribe(
    publisher_id: str, payload: SubscribeRequest, account: Account | None = SIGNED_IN
) -> SubscriptionOut:
    """Choose a plan. A paid plan answers with what to send and where; choosing Open
    cancels at the end of the paid period."""
    require_owner_or_simulation(account, publisher_id, "change this plan")
    return await _call(store.subscribe, publisher_id, payload.plan)


@router.post("/publishers/{publisher_id}/subscription/payment", response_model=SubscriptionOut)
async def confirm_payment(
    publisher_id: str, payload: PaymentConfirmation, account: Account | None = SIGNED_IN
) -> SubscriptionOut:
    """Confirm the transaction that paid. The server reads it from the chain, and the
    plan switches on only if it moved what is due from the publisher's wallet to ours."""
    require_owner_or_simulation(account, publisher_id, "pay for this plan")
    return await _call(store.confirm_payment, publisher_id, payload.tx_hash)


@router.post("/demo/publishers/{publisher_id}/subscription/pay", response_model=SubscriptionOut)
async def demo_pay(publisher_id: str, account: Account | None = SIGNED_IN) -> SubscriptionOut:
    """Send what is due on the simulation's rail and confirm it. The simulation's only."""
    if not settings.simulated:
        raise HTTPException(status_code=403, detail="pay from your wallet on Arc")
    require_owner_or_simulation(account, publisher_id, "pay for this plan")
    return await _call(store.demo_pay, publisher_id)
