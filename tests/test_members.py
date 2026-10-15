"""Tests for adding/removing workspace members."""

from uuid import uuid4

from fastapi.testclient import TestClient

REGISTER_URL = "/api/auth/register"
LOGIN_URL = "/api/auth/login"
WORKSPACES_URL = "/api/workspaces"
PASSWORD = "SecurePass123"


def _register(client: TestClient, email: str) -> None:
    client.post(
        REGISTER_URL,
        json={"first_name": "M", "last_name": "X", "email": email, "password": PASSWORD},
    )


def _login(client: TestClient, email: str) -> None:
    assert client.post(LOGIN_URL, json={"email": email, "password": PASSWORD}).status_code == 200


def _new_user(client: TestClient) -> str:
    email = f"mem-{uuid4().hex}@example.com"
    _register(client, email)
    _login(client, email)
    return email


def _workspace_id(client: TestClient) -> str:
    return client.get(WORKSPACES_URL).json()[0]["id"]


def test_owner_adds_existing_user_as_member(client: TestClient) -> None:
    other = f"mem-{uuid4().hex}@example.com"
    _register(client, other)  # exists but not logged in

    _new_user(client)  # owner, logged in
    wid = _workspace_id(client)

    resp = client.post(f"{WORKSPACES_URL}/{wid}/members", json={"email": other})
    assert resp.status_code == 201
    assert resp.json()["email"] == other
    assert resp.json()["role"] == "member"

    members = client.get(f"{WORKSPACES_URL}/{wid}/members").json()
    assert len(members) == 2


def test_add_member_unknown_email_404(client: TestClient) -> None:
    _new_user(client)
    wid = _workspace_id(client)
    resp = client.post(
        f"{WORKSPACES_URL}/{wid}/members",
        json={"email": f"ghost-{uuid4().hex}@example.com"},
    )
    assert resp.status_code == 404


def test_add_member_duplicate_409(client: TestClient) -> None:
    other = f"mem-{uuid4().hex}@example.com"
    _register(client, other)
    _new_user(client)
    wid = _workspace_id(client)
    assert client.post(f"{WORKSPACES_URL}/{wid}/members", json={"email": other}).status_code == 201
    assert client.post(f"{WORKSPACES_URL}/{wid}/members", json={"email": other}).status_code == 409


def test_add_member_invalid_role_422(client: TestClient) -> None:
    other = f"mem-{uuid4().hex}@example.com"
    _register(client, other)
    _new_user(client)
    wid = _workspace_id(client)
    resp = client.post(
        f"{WORKSPACES_URL}/{wid}/members", json={"email": other, "role": "owner"}
    )
    assert resp.status_code == 422


def test_member_role_cannot_manage_members(client: TestClient) -> None:
    member_email = f"mem-{uuid4().hex}@example.com"
    _register(client, member_email)

    owner_email = _new_user(client)  # owner
    wid = _workspace_id(client)
    client.post(f"{WORKSPACES_URL}/{wid}/members", json={"email": member_email})

    # Log in as the plain member and try to add someone.
    _login(client, member_email)
    third = f"mem-{uuid4().hex}@example.com"
    _register(client, third)
    _login(client, member_email)  # re-login (register logged nobody in, but be safe)
    resp = client.post(f"{WORKSPACES_URL}/{wid}/members", json={"email": third})
    assert resp.status_code == 403
    _ = owner_email


def test_remove_member(client: TestClient) -> None:
    other = f"mem-{uuid4().hex}@example.com"
    _register(client, other)
    _new_user(client)
    wid = _workspace_id(client)
    added = client.post(f"{WORKSPACES_URL}/{wid}/members", json={"email": other}).json()

    resp = client.delete(f"{WORKSPACES_URL}/{wid}/members/{added['user_id']}")
    assert resp.status_code == 204
    assert len(client.get(f"{WORKSPACES_URL}/{wid}/members").json()) == 1


def test_cannot_remove_owner(client: TestClient) -> None:
    _new_user(client)
    wid = _workspace_id(client)
    owner_id = client.get(f"{WORKSPACES_URL}/{wid}/members").json()[0]["user_id"]
    resp = client.delete(f"{WORKSPACES_URL}/{wid}/members/{owner_id}")
    assert resp.status_code == 409


def test_remove_unknown_member_404(client: TestClient) -> None:
    _new_user(client)
    wid = _workspace_id(client)
    assert client.delete(f"{WORKSPACES_URL}/{wid}/members/{uuid4()}").status_code == 404


def test_change_member_role(client: TestClient) -> None:
    other = f"mem-{uuid4().hex}@example.com"
    _register(client, other)
    _new_user(client)  # owner
    wid = _workspace_id(client)
    added = client.post(f"{WORKSPACES_URL}/{wid}/members", json={"email": other}).json()
    assert added["role"] == "member"

    resp = client.patch(
        f"{WORKSPACES_URL}/{wid}/members/{added['user_id']}", json={"role": "admin"}
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "admin"


def test_cannot_change_owner_role(client: TestClient) -> None:
    _new_user(client)
    wid = _workspace_id(client)
    owner_id = client.get(f"{WORKSPACES_URL}/{wid}/members").json()[0]["user_id"]
    resp = client.patch(f"{WORKSPACES_URL}/{wid}/members/{owner_id}", json={"role": "member"})
    assert resp.status_code == 409


def test_change_role_invalid_422(client: TestClient) -> None:
    other = f"mem-{uuid4().hex}@example.com"
    _register(client, other)
    _new_user(client)
    wid = _workspace_id(client)
    added = client.post(f"{WORKSPACES_URL}/{wid}/members", json={"email": other}).json()
    resp = client.patch(f"{WORKSPACES_URL}/{wid}/members/{added['user_id']}", json={"role": "owner"})
    assert resp.status_code == 422
