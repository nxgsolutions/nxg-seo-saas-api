"""Tests for saved page-audit history (workspace-scoped)."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.services import audit_service
from app.services.audit_service import AuditResult, Check

REGISTER_URL = "/api/auth/register"
LOGIN_URL = "/api/auth/login"
WORKSPACES_URL = "/api/workspaces"
PASSWORD = "SecurePass123"


def _signup(client: TestClient) -> None:
    email = f"audit-{uuid4().hex}@example.com"
    client.post(
        REGISTER_URL,
        json={
            "first_name": "Aud",
            "last_name": "It",
            "email": email,
            "password": PASSWORD,
        },
    )
    assert client.post(LOGIN_URL, json={"email": email, "password": PASSWORD}).status_code == 200


def _workspace_id(client: TestClient) -> str:
    return client.get(WORKSPACES_URL).json()[0]["id"]


def _fake_result(score: int = 88) -> AuditResult:
    return AuditResult(
        url="https://example.com/",
        final_url="https://example.com/",
        status_code=200,
        load_time_ms=321,
        page_size_bytes=1234,
        score=score,
        checks=[Check("https", "Served over HTTPS", "pass", "Secure.", 2)],
        metrics={"title": "Example", "word_count": 42},
    )


@pytest.fixture
def stub_audit(monkeypatch):
    """Replace the network fetch with a deterministic result."""
    monkeypatch.setattr(
        audit_service, "run_audit", lambda url, device="desktop": _fake_result()
    )


def test_run_audit_saves_and_returns_detail(client: TestClient, stub_audit) -> None:
    _signup(client)
    wid = _workspace_id(client)

    resp = client.post(f"{WORKSPACES_URL}/{wid}/page-audits", json={"url": "example.com"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["score"] == 88
    assert body["url"] == "https://example.com/"
    assert body["checks"][0]["id"] == "https"
    assert body["metrics"]["title"] == "Example"
    assert "id" in body and body["id"]


def test_audit_appears_in_history(client: TestClient, stub_audit) -> None:
    _signup(client)
    wid = _workspace_id(client)
    client.post(f"{WORKSPACES_URL}/{wid}/page-audits", json={"url": "example.com"})
    client.post(f"{WORKSPACES_URL}/{wid}/page-audits", json={"url": "example.org"})

    history = client.get(f"{WORKSPACES_URL}/{wid}/page-audits")
    assert history.status_code == 200
    rows = history.json()
    assert len(rows) == 2
    # Summary rows omit heavy fields.
    assert "checks" not in rows[0]
    assert rows[0]["score"] == 88


def test_get_saved_audit_detail(client: TestClient, stub_audit) -> None:
    _signup(client)
    wid = _workspace_id(client)
    created = client.post(
        f"{WORKSPACES_URL}/{wid}/page-audits", json={"url": "example.com"}
    ).json()

    resp = client.get(f"{WORKSPACES_URL}/{wid}/page-audits/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["checks"][0]["label"] == "Served over HTTPS"


def test_unknown_audit_returns_404(client: TestClient, stub_audit) -> None:
    _signup(client)
    wid = _workspace_id(client)
    assert client.get(f"{WORKSPACES_URL}/{wid}/page-audits/{uuid4()}").status_code == 404


def test_history_requires_membership(client: TestClient, stub_audit) -> None:
    _signup(client)
    wid = _workspace_id(client)
    client.post(f"{WORKSPACES_URL}/{wid}/page-audits", json={"url": "example.com"})

    _signup(client)  # different user
    assert client.get(f"{WORKSPACES_URL}/{wid}/page-audits").status_code == 404
    assert client.post(f"{WORKSPACES_URL}/{wid}/page-audits", json={"url": "x.com"}).status_code == 404


def test_run_audit_requires_auth(client: TestClient) -> None:
    resp = client.post(f"{WORKSPACES_URL}/{uuid4()}/page-audits", json={"url": "example.com"})
    assert resp.status_code == 401
