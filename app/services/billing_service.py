"""Billing business logic: plans and per-workspace subscriptions.

Kept independent of Stripe so the subscription lifecycle works with or without
payment configured. When Stripe is not configured, selecting a paid plan grants
an introductory trial locally (no charge); see stripe_service for the paid flow.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.billing import (
    STATUS_FREE,
    STATUS_TRIALING,
    Plan,
    Subscription,
)
from app.models.workspace import Workspace


class PlanNotFoundError(Exception):
    pass


# Canonical default plans. This is the single source of truth used both by the
# startup seed and as a self-healing fallback in ensure_subscription, so an
# empty `plans` table (e.g. after a data wipe) can never break registration.
# Keep in sync with the seed in alembic migration 0008.
DEFAULT_PLANS: list[dict] = [
    {
        "code": "free",
        "name": "Free",
        "description": "Everything you need to start optimizing one site.",
        "price_cents": 0,
        "currency": "usd",
        "interval": "month",
        "features": [
            "1 website",
            "Page Audit & Optimization",
            "On-page keyword analysis",
            "Google Search Console",
            "Core Web Vitals (PageSpeed)",
            "Community support",
        ],
        "is_active": True,
        "sort_order": 0,
    },
    {
        "code": "pro",
        "name": "Pro",
        "description": "For growing teams that need keyword and competitor data.",
        "price_cents": 4900,
        "currency": "usd",
        "interval": "month",
        "features": [
            "Up to 10 websites",
            "Everything in Free",
            "AI Search analysis",
            "Keyword & competitor data",
            "Rank tracking",
            "Priority email support",
        ],
        "is_active": True,
        "sort_order": 1,
    },
    {
        "code": "enterprise",
        "name": "Enterprise",
        "description": "Custom scale, security and support for large organizations.",
        "price_cents": 0,
        "currency": "usd",
        "interval": "month",
        "features": [
            "Unlimited websites",
            "Everything in Pro",
            "Custom integrations",
            "SSO & audit logs",
            "Dedicated success manager",
            "SLA",
        ],
        "is_active": True,
        "sort_order": 2,
    },
]


def seed_default_plans(db: Session) -> int:
    """Insert any missing default plans. Idempotent — safe to run on startup.

    Returns the number of plans inserted. Plans are matched by `code`, so
    existing plans are left untouched.
    """
    existing = {
        row[0] for row in db.execute(select(Plan.code)).all()
    }
    inserted = 0
    for spec in DEFAULT_PLANS:
        if spec["code"] in existing:
            continue
        db.add(Plan(**spec))
        inserted += 1
    if inserted:
        db.commit()
    return inserted


def list_plans(db: Session) -> list[Plan]:
    return list(
        db.execute(
            select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.sort_order.asc())
        ).scalars()
    )


def get_plan_by_code(db: Session, code: str) -> Plan | None:
    return db.execute(select(Plan).where(Plan.code == code)).scalar_one_or_none()


def _free_plan(db: Session) -> Plan:
    plan = get_plan_by_code(db, "free")
    if plan is None:
        # Self-heal: the plans table was emptied (e.g. after a data wipe).
        # Re-seed the defaults so registration/billing keeps working.
        seed_default_plans(db)
        plan = get_plan_by_code(db, "free")
    if plan is None:
        raise PlanNotFoundError("free")
    return plan


def get_subscription(db: Session, workspace: Workspace) -> Subscription | None:
    return db.execute(
        select(Subscription).where(Subscription.workspace_id == workspace.id)
    ).scalar_one_or_none()


def ensure_subscription(
    db: Session, workspace: Workspace, plan_code: str = "free"
) -> Subscription:
    """Guarantee the workspace has a subscription. Idempotent."""
    existing = get_subscription(db, workspace)
    if existing is not None:
        return existing

    plan = get_plan_by_code(db, plan_code)
    if plan is None:
        # Requested plan missing — ensure defaults exist, then retry the
        # requested code before falling back to free.
        seed_default_plans(db)
        plan = get_plan_by_code(db, plan_code) or _free_plan(db)
    if plan.code == "free":
        sub = Subscription(
            workspace_id=workspace.id, tenant_id=workspace.tenant_id,
            plan_id=plan.id, status=STATUS_FREE,
        )
    else:
        sub = _trial_subscription(workspace, plan)
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _trial_subscription(workspace: Workspace, plan: Plan) -> Subscription:
    now = datetime.now(timezone.utc)
    trial_end = now + timedelta(days=settings.TRIAL_DAYS)
    return Subscription(
        workspace_id=workspace.id,
        tenant_id=workspace.tenant_id,
        plan_id=plan.id,
        status=STATUS_TRIALING,
        trial_end=trial_end,
        current_period_end=trial_end,
    )


def select_plan_local(db: Session, workspace: Workspace, plan: Plan) -> Subscription:
    """Switch the workspace to `plan` without payment.

    Free -> free status; paid -> a local introductory trial. Used when Stripe is
    not configured, and for the free plan always.
    """
    sub = ensure_subscription(db, workspace)
    now = datetime.now(timezone.utc)
    sub.plan_id = plan.id
    sub.cancel_at_period_end = False
    if plan.code == "free":
        sub.status = STATUS_FREE
        sub.trial_end = None
        sub.current_period_end = None
    else:
        sub.status = STATUS_TRIALING
        sub.trial_end = now + timedelta(days=settings.TRIAL_DAYS)
        sub.current_period_end = sub.trial_end
    db.commit()
    db.refresh(sub)
    return sub


def cancel(db: Session, workspace: Workspace) -> Subscription:
    """Cancel a paid/trialing subscription — downgrades to free at period end.

    With no Stripe, we downgrade to free immediately (there is no billing period
    to wait out); with Stripe, the webhook drives the final state.
    """
    sub = ensure_subscription(db, workspace)
    if sub.status == STATUS_FREE:
        return sub
    return select_plan_local(db, workspace, _free_plan(db))
