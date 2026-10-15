"""Billing routes: public plan list, workspace subscription, Stripe webhook."""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.deps import WorkspaceContext, get_current_user, get_workspace_context
from app.api.errors import upstream_error
from app.core.config import settings
from app.models.billing import Plan, Subscription
from app.models.user import User
from app.db.session import get_db
from app.schemas.billing import (
    PlanOut,
    PortalRequest,
    PortalResponse,
    SelectPlanRequest,
    SelectPlanResponse,
    SubscriptionOut,
)
from app.services import billing_service, stripe_service
from app.services.stripe_service import StripeError, StripeNotConfiguredError

# Public: plan list (used by the signup pricing table). No auth.
plans_router = APIRouter(prefix="/plans", tags=["billing"])
# Workspace-scoped subscription management.
billing_router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["billing"])
# Stripe webhook (no auth; verified by signature).
webhook_router = APIRouter(prefix="/stripe", tags=["billing"])


def _plan_out(plan: Plan) -> PlanOut:
    return PlanOut(
        id=str(plan.id),
        code=plan.code,
        name=plan.name,
        description=plan.description,
        price_cents=plan.price_cents,
        currency=plan.currency,
        interval=plan.interval,
        features=list(plan.features or []),
        sort_order=plan.sort_order,
    )


def _sub_out(sub: Subscription) -> SubscriptionOut:
    return SubscriptionOut(
        status=sub.status,
        plan=_plan_out(sub.plan),
        trial_end=sub.trial_end,
        current_period_end=sub.current_period_end,
        cancel_at_period_end=sub.cancel_at_period_end,
        stripe_configured=settings.stripe_configured,
    )


@plans_router.get("", response_model=list[PlanOut], summary="List available plans")
def list_plans(db: Session = Depends(get_db)) -> list[PlanOut]:
    return [_plan_out(p) for p in billing_service.list_plans(db)]


@billing_router.get(
    "/subscription", response_model=SubscriptionOut, summary="Get the workspace subscription"
)
def get_subscription(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> SubscriptionOut:
    sub = billing_service.ensure_subscription(db, ctx.workspace)
    return _sub_out(sub)


@billing_router.post(
    "/subscription/select",
    response_model=SelectPlanResponse,
    summary="Select or upgrade to a plan",
)
def select_plan(
    payload: SelectPlanRequest,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SelectPlanResponse:
    if ctx.role not in ("owner", "admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only owners/admins can change the plan.")

    plan = billing_service.get_plan_by_code(db, payload.plan_code)
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found.")

    # Free plan (or unpriced enterprise) always applies locally.
    paid_via_stripe = (
        plan.code != "free"
        and plan.price_cents > 0
        and settings.stripe_configured
        and bool(plan.stripe_price_id)
    )
    if paid_via_stripe and payload.success_url and payload.cancel_url:
        try:
            url = stripe_service.create_checkout_session(
                db, ctx.workspace, plan, payload.success_url, payload.cancel_url, current_user.email
            )
        except (StripeError, StripeNotConfiguredError) as exc:
            raise upstream_error(exc, "The payment service couldn't complete this. Please try again or contact support.", status.HTTP_502_BAD_GATEWAY) from exc
        return SelectPlanResponse(mode="checkout", checkout_url=url)

    sub = billing_service.select_plan_local(db, ctx.workspace, plan)
    return SelectPlanResponse(mode="local", subscription=_sub_out(sub))


@billing_router.post(
    "/subscription/cancel", response_model=SubscriptionOut, summary="Cancel / downgrade to free"
)
def cancel_subscription(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> SubscriptionOut:
    if ctx.role not in ("owner", "admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only owners/admins can change the plan.")
    sub = billing_service.cancel(db, ctx.workspace)
    return _sub_out(sub)


@billing_router.post(
    "/billing/portal", response_model=PortalResponse, summary="Open the Stripe billing portal"
)
def billing_portal(
    payload: PortalRequest,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> PortalResponse:
    if ctx.role not in ("owner", "admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only owners/admins can manage billing.")
    try:
        url = stripe_service.create_portal_session(db, ctx.workspace, payload.return_url)
    except StripeNotConfiguredError as exc:
        raise upstream_error(exc, "Payments aren't available right now. Please contact support.", status.HTTP_409_CONFLICT) from exc
    except StripeError as exc:
        raise upstream_error(exc, "The payment service couldn't complete this. Please try again or contact support.", status.HTTP_502_BAD_GATEWAY) from exc
    return PortalResponse(url=url)


@webhook_router.post("/webhook", include_in_schema=False)
async def stripe_webhook(request: Request, db: Session = Depends(get_db)) -> dict:
    payload = await request.body()
    sig = request.headers.get("stripe-signature")
    try:
        etype = stripe_service.handle_webhook(db, payload, sig)
    except StripeNotConfiguredError as exc:
        raise upstream_error(exc, "Payments aren't available right now. Please contact support.", status.HTTP_503_SERVICE_UNAVAILABLE) from exc
    except StripeError as exc:
        raise upstream_error(exc, "The payment service couldn't complete this. Please try again or contact support.", status.HTTP_400_BAD_REQUEST) from exc
    return {"received": True, "type": etype}
