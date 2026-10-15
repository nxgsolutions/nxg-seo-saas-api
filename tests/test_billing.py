"""Tests for plans and subscriptions (local flow; Stripe not configured)."""

from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models.billing import Plan, Subscription
from app.services import billing_service

REGISTER = "/api/auth/register"
LOGIN = "/api/auth/login"
WS = "/api/workspaces"
PW = "SecurePass123"


def _signup(client: TestClient, plan_code: str | None = None) -> str:
    email = f"bill-{uuid4().hex}@example.com"
    body = {"first_name": "B", "last_name": "L", "email": email, "password": PW}
    if plan_code:
        body["plan_code"] = plan_code
    assert client.post(REGISTER, json=body).status_code == 201
    assert client.post(LOGIN, json={"email": email, "password": PW}).status_code == 200
    return client.get(WS).json()[0]["id"]


def test_plans_public_list(client: TestClient) -> None:
    resp = client.get("/api/plans")
    assert resp.status_code == 200
    codes = [p["code"] for p in resp.json()]
    assert codes == ["free", "pro", "enterprise"]


def test_new_user_starts_on_free(client: TestClient) -> None:
    wid = _signup(client)
    sub = client.get(f"{WS}/{wid}/subscription").json()
    assert sub["status"] == "free"
    assert sub["plan"]["code"] == "free"
    assert sub["stripe_configured"] is False


def test_signup_with_pro_grants_trial(client: TestClient) -> None:
    wid = _signup(client, plan_code="pro")
    sub = client.get(f"{WS}/{wid}/subscription").json()
    assert sub["plan"]["code"] == "pro"
    assert sub["status"] == "trialing"
    assert sub["trial_end"] is not None


def test_select_and_cancel_plan(client: TestClient) -> None:
    wid = _signup(client)
    # Upgrade to pro locally (no Stripe) -> trial.
    resp = client.post(f"{WS}/{wid}/subscription/select", json={"plan_code": "pro"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "local"
    assert body["subscription"]["plan"]["code"] == "pro"
    assert body["subscription"]["status"] == "trialing"

    # Cancel -> back to free.
    resp = client.post(f"{WS}/{wid}/subscription/cancel")
    assert resp.status_code == 200
    assert resp.json()["plan"]["code"] == "free"
    assert resp.json()["status"] == "free"


def test_select_unknown_plan_404(client: TestClient) -> None:
    wid = _signup(client)
    resp = client.post(f"{WS}/{wid}/subscription/select", json={"plan_code": "nope"})
    assert resp.status_code == 404


def test_seed_default_plans_is_idempotent(db_session: Session) -> None:
    # Wipe plans (subscriptions first, FK), then re-seed.
    db_session.execute(delete(Subscription))
    db_session.execute(delete(Plan))
    db_session.flush()

    first = billing_service.seed_default_plans(db_session)
    assert first == 3
    codes = [p.code for p in billing_service.list_plans(db_session)]
    assert codes == ["free", "pro", "enterprise"]

    # Running again inserts nothing.
    assert billing_service.seed_default_plans(db_session) == 0


def test_register_self_heals_when_plans_empty(
    client: TestClient, db_session: Session
) -> None:
    """Regression: registration must not 500 when the plans table is empty
    (e.g. after a data wipe). It should re-seed and create a free subscription."""
    db_session.execute(delete(Subscription))
    db_session.execute(delete(Plan))
    db_session.flush()

    email = f"heal-{uuid4().hex}@example.com"
    resp = client.post(
        REGISTER,
        json={"first_name": "H", "last_name": "L", "email": email, "password": PW},
    )
    assert resp.status_code == 201

    # Plans were re-seeded and the new workspace is on free.
    assert client.post(LOGIN, json={"email": email, "password": PW}).status_code == 200
    wid = client.get(WS).json()[0]["id"]
    sub = client.get(f"{WS}/{wid}/subscription").json()
    assert sub["plan"]["code"] == "free"
    assert sub["status"] == "free"


def test_portal_without_stripe_conflicts(client: TestClient) -> None:
    wid = _signup(client)
    resp = client.post(f"{WS}/{wid}/billing/portal", json={"return_url": "http://x/"})
    assert resp.status_code == 409
