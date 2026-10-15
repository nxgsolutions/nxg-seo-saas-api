"""Tests for login / logout / me."""

from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.user import User

REGISTER_URL = "/api/auth/register"
LOGIN_URL = "/api/auth/login"
LOGOUT_URL = "/api/auth/logout"
ME_URL = "/api/auth/me"

PASSWORD = "SecurePass123"


def _register(client: TestClient, email: str | None = None) -> str:
    email = email or f"login-{uuid4().hex}@example.com"
    resp = client.post(
        REGISTER_URL,
        json={
            "first_name": "Log",
            "last_name": "In",
            "email": email,
            "password": PASSWORD,
        },
    )
    assert resp.status_code == 201
    return email


def test_login_success_sets_cookie_and_returns_user(client: TestClient) -> None:
    email = _register(client)
    resp = client.post(LOGIN_URL, json={"email": email, "password": PASSWORD})
    assert resp.status_code == 200

    body = resp.json()
    assert body["user"]["email"] == email
    # Secret token must never appear in the body.
    assert "access_token" not in body
    assert "password" not in body["user"]
    # Session cookie is set.
    assert settings.AUTH_COOKIE_NAME in resp.cookies


def test_login_is_case_insensitive_on_email(client: TestClient) -> None:
    email = _register(client)
    resp = client.post(
        LOGIN_URL, json={"email": email.upper(), "password": PASSWORD}
    )
    assert resp.status_code == 200


def test_login_wrong_password_returns_401(client: TestClient) -> None:
    email = _register(client)
    resp = client.post(LOGIN_URL, json={"email": email, "password": "WrongPass9"})
    assert resp.status_code == 401
    assert "invalid" in resp.json()["detail"].lower()


def test_login_unknown_email_returns_401(client: TestClient) -> None:
    resp = client.post(
        LOGIN_URL,
        json={"email": f"nobody-{uuid4().hex}@example.com", "password": PASSWORD},
    )
    assert resp.status_code == 401


def test_login_updates_last_login_at(
    client: TestClient, db_session: Session
) -> None:
    email = _register(client)
    user = db_session.execute(
        select(User).where(User.email == email)
    ).scalar_one()
    assert user.last_login_at is None

    resp = client.post(LOGIN_URL, json={"email": email, "password": PASSWORD})
    assert resp.status_code == 200

    db_session.refresh(user)
    assert user.last_login_at is not None


def test_login_inactive_user_returns_401(
    client: TestClient, db_session: Session
) -> None:
    email = _register(client)
    user = db_session.execute(
        select(User).where(User.email == email)
    ).scalar_one()
    user.is_active = False
    db_session.commit()

    resp = client.post(LOGIN_URL, json={"email": email, "password": PASSWORD})
    assert resp.status_code == 401


def test_me_requires_authentication(client: TestClient) -> None:
    resp = client.get(ME_URL)
    assert resp.status_code == 401


def test_me_returns_user_after_login(client: TestClient) -> None:
    email = _register(client)
    client.post(LOGIN_URL, json={"email": email, "password": PASSWORD})
    # TestClient keeps the cookie jar, so the session cookie is sent.
    resp = client.get(ME_URL)
    assert resp.status_code == 200
    assert resp.json()["email"] == email


def test_me_with_invalid_token_returns_401(client: TestClient) -> None:
    client.cookies.set(settings.AUTH_COOKIE_NAME, "not-a-valid-jwt")
    resp = client.get(ME_URL)
    assert resp.status_code == 401


def test_logout_clears_session(client: TestClient) -> None:
    email = _register(client)
    client.post(LOGIN_URL, json={"email": email, "password": PASSWORD})
    assert client.get(ME_URL).status_code == 200

    logout = client.post(LOGOUT_URL)
    assert logout.status_code == 204

    # Cookie cleared -> protected route now rejects.
    client.cookies.clear()
    assert client.get(ME_URL).status_code == 401
