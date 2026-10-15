"""Stripe billing adapter.

Entirely optional: every function raises StripeNotConfiguredError unless
STRIPE_SECRET_KEY is set, so the app runs and the subscription lifecycle works
locally without it. When configured, paid-plan selection routes through Stripe
Checkout, management through the Billing Portal, and a webhook syncs status.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.billing import (
    STATUS_ACTIVE,
    STATUS_CANCELED,
    STATUS_PAST_DUE,
    STATUS_TRIALING,
    Plan,
    Subscription,
)
from app.models.workspace import Workspace
from app.services import billing_service


class StripeNotConfiguredError(Exception):
    pass


class StripeError(Exception):
    pass


def _stripe():
    if not settings.stripe_configured:
        raise StripeNotConfiguredError("Stripe is not configured on the server.")
    import stripe

    stripe.api_key = settings.STRIPE_SECRET_KEY
    return stripe


def create_checkout_session(
    db: Session,
    workspace: Workspace,
    plan: Plan,
    success_url: str,
    cancel_url: str,
    customer_email: str | None = None,
) -> str:
    """Create a Stripe Checkout session for a paid plan; returns its URL."""
    stripe = _stripe()
    if not plan.stripe_price_id:
        raise StripeError(
            f"Plan '{plan.code}' has no Stripe price id. Set stripe_price_id first."
        )
    sub = billing_service.ensure_subscription(db, workspace)
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": plan.stripe_price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            customer=sub.stripe_customer_id or None,
            customer_email=None if sub.stripe_customer_id else customer_email,
            subscription_data={
                "trial_period_days": settings.TRIAL_DAYS,
                "metadata": {"workspace_id": str(workspace.id), "plan_code": plan.code},
            },
            metadata={"workspace_id": str(workspace.id), "plan_code": plan.code},
        )
    except Exception as exc:  # noqa: BLE001
        raise StripeError(f"Could not start checkout: {exc}") from exc
    return session.url


def create_portal_session(db: Session, workspace: Workspace, return_url: str) -> str:
    """Create a Billing Portal session so the customer can manage their plan."""
    stripe = _stripe()
    sub = billing_service.ensure_subscription(db, workspace)
    if not sub.stripe_customer_id:
        raise StripeError("This workspace has no Stripe customer yet.")
    try:
        session = stripe.billing_portal.Session.create(
            customer=sub.stripe_customer_id, return_url=return_url
        )
    except Exception as exc:  # noqa: BLE001
        raise StripeError(f"Could not open the billing portal: {exc}") from exc
    return session.url


# --- Webhook -----------------------------------------------------------------
_STATUS_MAP = {
    "trialing": STATUS_TRIALING,
    "active": STATUS_ACTIVE,
    "past_due": STATUS_PAST_DUE,
    "canceled": STATUS_CANCELED,
    "unpaid": STATUS_PAST_DUE,
}


def _find_subscription(db: Session, workspace_id: str | None, customer_id: str | None) -> Subscription | None:
    if workspace_id:
        import uuid as _uuid

        try:
            wid = _uuid.UUID(workspace_id)
        except (ValueError, TypeError):
            wid = None
        if wid is not None:
            sub = db.execute(
                select(Subscription).where(Subscription.workspace_id == wid)
            ).scalar_one_or_none()
            if sub is not None:
                return sub
    if customer_id:
        return db.execute(
            select(Subscription).where(Subscription.stripe_customer_id == customer_id)
        ).scalar_one_or_none()
    return None


def handle_webhook(db: Session, payload: bytes, sig_header: str | None) -> str:
    """Verify and apply a Stripe webhook event. Returns the event type handled."""
    stripe = _stripe()
    if not settings.STRIPE_WEBHOOK_SECRET:
        raise StripeError("STRIPE_WEBHOOK_SECRET is not set.")
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
        )
    except Exception as exc:  # noqa: BLE001 - signature or parse failure
        raise StripeError(f"Invalid webhook signature: {exc}") from exc

    etype = event["type"]
    obj = event["data"]["object"]

    if etype == "checkout.session.completed":
        meta = obj.get("metadata") or {}
        sub = _find_subscription(db, meta.get("workspace_id"), obj.get("customer"))
        if sub is not None:
            sub.stripe_customer_id = obj.get("customer") or sub.stripe_customer_id
            sub.stripe_subscription_id = obj.get("subscription") or sub.stripe_subscription_id
            plan = billing_service.get_plan_by_code(db, meta.get("plan_code", ""))
            if plan is not None:
                sub.plan_id = plan.id
            sub.status = STATUS_TRIALING
            db.commit()

    elif etype in ("customer.subscription.updated", "customer.subscription.created"):
        meta = obj.get("metadata") or {}
        sub = _find_subscription(db, meta.get("workspace_id"), obj.get("customer"))
        if sub is not None:
            sub.status = _STATUS_MAP.get(obj.get("status", ""), sub.status)
            sub.cancel_at_period_end = bool(obj.get("cancel_at_period_end"))
            cpe = obj.get("current_period_end")
            if cpe:
                sub.current_period_end = datetime.fromtimestamp(cpe, tz=timezone.utc)
            sub.stripe_subscription_id = obj.get("id") or sub.stripe_subscription_id
            db.commit()

    elif etype == "customer.subscription.deleted":
        sub = _find_subscription(db, (obj.get("metadata") or {}).get("workspace_id"), obj.get("customer"))
        if sub is not None:
            # Downgrade to free.
            workspace = db.get(Workspace, sub.workspace_id)
            if workspace is not None:
                free = billing_service.get_plan_by_code(db, "free")
                if free is not None:
                    billing_service.select_plan_local(db, workspace, free)

    return etype
