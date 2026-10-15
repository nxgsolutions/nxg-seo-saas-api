"""Tests for workspace listing, creation, membership, and authorization."""

from uuid import uuid4

from fastapi.testclient import TestClient

REGISTER_URL = "/api/auth/register"
LOGIN_URL = "/api/auth/login"
WORKSPACES_URL = "/api/workspaces"

PASSWORD = "SecurePass123"


def _signup(client: TestClient, first_name: str = "Wsp") -> str:
    email = f"ws-{uuid4().hex}@example.com"
    client.post(
        REGISTER_URL,
        json={
            "first_name": first_name,
            "last_name": "User",
            "email": email,
            "password": PASSWORD,
        },
    )
    resp = client.post(LOGIN_URL, json={"email": email, "password": PASSWORD})
    assert resp.status_code == 200
    return email


def test_new_user_has_default_workspace(client: TestClient) -> None:
    _signup(client, first_name="Nadia")
    resp = client.get(WORKSPACES_URL)
    assert resp.status_code == 200

    workspaces = resp.json()
    assert len(workspaces) == 1
    ws = workspaces[0]
    assert ws["name"] == "Nadia's Workspace"
    assert ws["role"] == "owner"
    assert ws["member_count"] == 1
    assert ws["slug"]


def test_workspaces_require_auth(client: TestClient) -> None:
    assert client.get(WORKSPACES_URL).status_code == 401


def test_create_workspace(client: TestClient) -> None:
    _signup(client)
    resp = client.post(WORKSPACES_URL, json={"name": "Acme Marketing"})
    assert resp.status_code == 201

    ws = resp.json()
    assert ws["name"] == "Acme Marketing"
    assert ws["slug"] == "acme-marketing"
    assert ws["role"] == "owner"
    assert ws["member_count"] == 1

    # It now appears in the list (default + created = 2).
    listed = client.get(WORKSPACES_URL).json()
    assert len(listed) == 2


def test_create_workspace_blank_name_returns_422(client: TestClient) -> None:
    _signup(client)
    assert client.post(WORKSPACES_URL, json={"name": "   "}).status_code == 422


def test_duplicate_names_get_unique_slugs(client: TestClient) -> None:
    _signup(client)
    a = client.post(WORKSPACES_URL, json={"name": "Growth Team"}).json()
    b = client.post(WORKSPACES_URL, json={"name": "Growth Team"}).json()
    assert a["slug"] == "growth-team"
    assert b["slug"] != a["slug"]
    assert b["slug"].startswith("growth-team-")


def test_get_workspace_detail_and_members(client: TestClient) -> None:
    _signup(client, first_name="Owner")
    ws = client.get(WORKSPACES_URL).json()[0]
    wid = ws["id"]

    detail = client.get(f"{WORKSPACES_URL}/{wid}")
    assert detail.status_code == 200
    assert detail.json()["id"] == wid

    members = client.get(f"{WORKSPACES_URL}/{wid}/members")
    assert members.status_code == 200
    body = members.json()
    assert len(body) == 1
    assert body[0]["role"] == "owner"
    assert "password" not in body[0]


def test_non_member_cannot_access_workspace(client: TestClient) -> None:
    # User A creates/owns a workspace.
    _signup(client, first_name="Alice")
    a_ws = client.get(WORKSPACES_URL).json()[0]["id"]

    # Switch to a different user (new cookies replace the old session).
    _signup(client, first_name="Bob")

    # Bob is not a member of Alice's workspace -> 404 (existence not leaked).
    assert client.get(f"{WORKSPACES_URL}/{a_ws}").status_code == 404
    assert client.get(f"{WORKSPACES_URL}/{a_ws}/members").status_code == 404


def test_unknown_workspace_returns_404(client: TestClient) -> None:
    _signup(client)
    assert client.get(f"{WORKSPACES_URL}/{uuid4()}").status_code == 404
    assert client.get(f"{WORKSPACES_URL}/not-a-uuid").status_code == 404


def test_create_sandbox_workspace(client: TestClient, monkeypatch) -> None:
    from app.core.config import settings

    _signup(client, "Sbx")
    monkeypatch.setattr(settings, "APP_ENV", "development")
    monkeypatch.setattr(settings, "DATAFORSEO_SANDBOX_ENABLED", None)
    resp = client.post(WORKSPACES_URL, json={"name": "Sandbox", "is_sandbox": True})
    assert resp.status_code == 201 and resp.json()["is_sandbox"] is True
    listed = {w["name"]: w["is_sandbox"] for w in client.get(WORKSPACES_URL).json()}
    assert listed["Sandbox"] is True

    monkeypatch.setattr(settings, "APP_ENV", "production")
    resp = client.post(WORKSPACES_URL, json={"name": "Sandbox 2", "is_sandbox": True})
    assert resp.status_code == 403
