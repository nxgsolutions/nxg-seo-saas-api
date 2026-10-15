"""Schemas for plans and subscriptions."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class PlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    code: str
    name: str
    description: str
    price_cents: int
    currency: str
    interval: str
    features: list[str]
    sort_order: int


class SubscriptionOut(BaseModel):
    status: str
    plan: PlanOut
    trial_end: datetime | None = None
    current_period_end: datetime | None = None
    cancel_at_period_end: bool = False
    stripe_configured: bool = False


class SelectPlanRequest(BaseModel):
    plan_code: str
    # Where Stripe Checkout should return to (only used when Stripe is configured).
    success_url: str | None = None
    cancel_url: str | None = None


class SelectPlanResponse(BaseModel):
    # "local" = applied immediately (free or trial); "checkout" = redirect to Stripe.
    mode: str
    checkout_url: str | None = None
    subscription: SubscriptionOut | None = None


class PortalRequest(BaseModel):
    return_url: str


class PortalResponse(BaseModel):
    url: str
