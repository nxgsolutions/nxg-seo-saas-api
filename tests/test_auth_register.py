"""Tests for POST /api/auth/register.

Each test uses a unique email (UUID-based) so the suite is independent of any
data already present in the dev database. The transactional fixture also rolls
back everything these tests write.
"""

from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.user import User

REGISTER_URL = "/api/auth/register"


def _unique_email() -> str:
    return f"reg-{uuid4().hex}@example.com"


def _payload(**overrides) -> dict:
    base = {
        "first_name": "Raj",
        "last_name": "Developer",
        "email": _unique_email(),
        "password": "SecurePass123",
    }
    base.update(overrides)
    return base


def test_register_valid_returns_201_and_safe_body(client: TestClient) -> None:
    payload = _payload()
    resp = client.post(REGISTER_URL, json=payload)
    assert resp.status_code == 201

    body = resp.json()
    assert body["email"] == payload["email"]
    assert body["first_name"] == "Raj"
    assert body["last_name"] == "Developer"
    assert body["is_active"] is True
    assert body["is_verified"] is False
    assert "id" in body and body["id"]
    assert "created_at" in body


def test_register_response_never_exposes_secrets(client: TestClient) -> None:
    resp = client.post(REGISTER_URL, json=_payload())
    assert resp.status_code == 201
    body = resp.json()
    assert "password" not in body
    assert "password_hash" not in body


def test_register_persists_hashed_password(
    client: TestClient, db_session: Session
) -> None:
    payload = _payload()
    resp = client.post(REGISTER_URL, json=payload)
    assert resp.status_code == 201

    user = db_session.execute(
        select(User).where(User.email == payload["email"])
    ).scalar_one()

    # Password is stored as an Argon2 hash, never as plaintext.
    assert user.password_hash != payload["password"]
    assert user.password_hash.startswith("$argon2")


def test_register_normalizes_email_case(client: TestClient) -> None:
    local = f"Mixed-{uuid4().hex}"
    resp = client.post(
        REGISTER_URL, json=_payload(email=f"{local}@Example.com")
    )
    assert resp.status_code == 201
    assert resp.json()["email"] == f"{local.lower()}@example.com"


def test_register_duplicate_email_returns_409(client: TestClient) -> None:
    email = _unique_email()
    first = client.post(REGISTER_URL, json=_payload(email=email))
    assert first.status_code == 201

    second = client.post(REGISTER_URL, json=_payload(email=email))
    assert second.status_code == 409
    assert "already exists" in second.json()["detail"].lower()


def test_register_duplicate_email_is_case_insensitive(client: TestClient) -> None:
    email = _unique_email()
    first = client.post(REGISTER_URL, json=_payload(email=email))
    assert first.status_code == 201

    second = client.post(REGISTER_URL, json=_payload(email=email.upper()))
    assert second.status_code == 409


def test_register_invalid_email_returns_422(client: TestClient) -> None:
    resp = client.post(REGISTER_URL, json=_payload(email="not-an-email"))
    assert resp.status_code == 422


def test_register_short_password_returns_422(client: TestClient) -> None:
    resp = client.post(REGISTER_URL, json=_payload(password="Ab1"))
    assert resp.status_code == 422


def test_register_password_without_number_returns_422(client: TestClient) -> None:
    resp = client.post(REGISTER_URL, json=_payload(password="OnlyLetters"))
    assert resp.status_code == 422


def test_register_missing_fields_returns_422(client: TestClient) -> None:
    resp = client.post(REGISTER_URL, json={"email": _unique_email()})
    assert resp.status_code == 422


def test_register_blank_first_name_returns_422(client: TestClient) -> None:
    resp = client.post(REGISTER_URL, json=_payload(first_name="   "))
    assert resp.status_code == 422
