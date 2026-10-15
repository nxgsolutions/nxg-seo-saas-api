"""Tests for onboarding state, workspace rename, and domains."""

from uuid import uuid4

from fastapi.testclient import TestClient

REGISTER_URL = "/api/auth/register"
LOGIN_URL = "/api/auth/login"
ME_URL = "/api/auth/me"
WORKSPACES_URL = "/api/workspaces"
ONBOARD_URL = "/api/onboarding/complete"

PASSWORD = "SecurePass123"


def _signup(client: TestClient) -> None:
    email = f"onb-{uuid4().hex}@example.com"
    client.post(
        REGISTER_URL,
        json={
            "first_name": "Ona",
            "last_name": "Board",
            "email": email,
            "password": PASSWORD,
        },
    )
    assert (
        client.post(LOGIN_URL, json={"email": email, "password": PASSWORD}).status_code
        == 200
    )


def _default_workspace_id(client: TestClient) -> str:
    return client.get(WORKSPACES_URL).json()[0]["id"]


def test_new_user_is_not_onboarded(client: TestClient) -> None:
    _signup(client)
    me = client.get(ME_URL).json()
    assert me["is_onboarded"] is False


def test_complete_onboarding_is_idempotent(client: TestClient) -> None:
    _signup(client)

    first = client.post(ONBOARD_URL)
    assert first.status_code == 200
    assert first.json()["is_onboarded"] is True

    # /me now reflects onboarded state.
    assert client.get(ME_URL).json()["is_onboarded"] is True

    # Second call is still fine (idempotent).
    assert client.post(ONBOARD_URL).status_code == 200


def test_onboarding_requires_auth(client: TestClient) -> None:
    assert client.post(ONBOARD_URL).status_code == 401


def test_rename_workspace(client: TestClient) -> None:
    _signup(client)
    wid = _default_workspace_id(client)
    before = client.get(f"{WORKSPACES_URL}/{wid}").json()

    resp = client.patch(f"{WORKSPACES_URL}/{wid}", json={"name": "Renamed Co"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Renamed Co"
    assert body["slug"] == before["slug"]  # slug stays stable


def test_rename_non_member_returns_404(client: TestClient) -> None:
    _signup(client)
    wid = _default_workspace_id(client)
    _signup(client)  # switch to a different user
    assert (
        client.patch(f"{WORKSPACES_URL}/{wid}", json={"name": "Nope"}).status_code
        == 404
    )


def test_add_domain_normalizes_full_url(client: TestClient) -> None:
    _signup(client)
    wid = _default_workspace_id(client)

    resp = client.post(
        f"{WORKSPACES_URL}/{wid}/domains",
        json={"url": "https://www.Example.com/some/path?q=1"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["domain"] == "www.example.com"
    assert body["name"] == "www.example.com"  # defaults to domain


def test_add_domain_bare_domain_with_name(client: TestClient) -> None:
    _signup(client)
    wid = _default_workspace_id(client)
    resp = client.post(
        f"{WORKSPACES_URL}/{wid}/domains",
        json={"url": "example.org", "name": "Main Site"},
    )
    assert resp.status_code == 201
    assert resp.json()["domain"] == "example.org"
    assert resp.json()["name"] == "Main Site"


def test_add_domain_invalid_domain_422(client: TestClient) -> None:
    _signup(client)
    wid = _default_workspace_id(client)
    resp = client.post(
        f"{WORKSPACES_URL}/{wid}/domains", json={"url": "not a domain!!"}
    )
    assert resp.status_code == 422


def test_duplicate_domain_returns_409(client: TestClient) -> None:
    _signup(client)
    wid = _default_workspace_id(client)
    assert (
        client.post(
            f"{WORKSPACES_URL}/{wid}/domains", json={"url": "dupe.com"}
        ).status_code
        == 201
    )
    assert (
        client.post(
            f"{WORKSPACES_URL}/{wid}/domains", json={"url": "https://dupe.com"}
        ).status_code
        == 409
    )


def test_list_domains(client: TestClient) -> None:
    _signup(client)
    wid = _default_workspace_id(client)
    client.post(f"{WORKSPACES_URL}/{wid}/domains", json={"url": "a-site.com"})
    client.post(f"{WORKSPACES_URL}/{wid}/domains", json={"url": "b-site.com"})

    resp = client.get(f"{WORKSPACES_URL}/{wid}/domains")
    assert resp.status_code == 200
    domains = {w["domain"] for w in resp.json()}
    assert domains == {"a-site.com", "b-site.com"}


def test_non_member_cannot_add_domain(client: TestClient) -> None:
    _signup(client)
    wid = _default_workspace_id(client)
    _signup(client)  # different user
    assert (
        client.post(
            f"{WORKSPACES_URL}/{wid}/domains", json={"url": "intruder.com"}
        ).status_code
        == 404
    )


def _add_domain(client: TestClient, wid: str, url: str) -> str:
    resp = client.post(f"{WORKSPACES_URL}/{wid}/domains", json={"url": url})
    assert resp.status_code == 201
    return resp.json()["id"]


def test_delete_domain_removes_it_and_its_data(client: TestClient, db_session) -> None:
    from sqlalchemy import text

    _signup(client)
    wid = _default_workspace_id(client)
    keep = _add_domain(client, wid, "keep-site.com")
    gone = _add_domain(client, wid, "gone-site.com")
    # A child row that must cascade away with the domain.
    assert client.post(
        f"{WORKSPACES_URL}/{wid}/domains/{gone}/competitors/list", json={"domain": "rival.com"}
    ).status_code == 201

    assert client.delete(f"{WORKSPACES_URL}/{wid}/domains/{gone}").status_code == 204

    remaining = [d["id"] for d in client.get(f"{WORKSPACES_URL}/{wid}/domains").json()]
    assert remaining == [keep]
    left = db_session.execute(
        text("select count(*) from tracked_competitors where domain_id = :d"), {"d": gone}
    ).scalar_one()
    assert left == 0


def test_delete_unknown_domain_404(client: TestClient) -> None:
    _signup(client)
    wid = _default_workspace_id(client)
    assert client.delete(f"{WORKSPACES_URL}/{wid}/domains/{uuid4()}").status_code == 404
    assert client.delete(f"{WORKSPACES_URL}/{wid}/domains/not-a-uuid").status_code == 404


def test_non_member_cannot_delete_domain(client: TestClient) -> None:
    _signup(client)
    wid = _default_workspace_id(client)
    did = _add_domain(client, wid, "mine.com")
    _signup(client)  # different user
    assert client.delete(f"{WORKSPACES_URL}/{wid}/domains/{did}").status_code == 404


def test_plain_member_cannot_delete_domain(client: TestClient) -> None:
    member = f"onb-{uuid4().hex}@example.com"
    client.post(REGISTER_URL, json={"first_name": "Mem", "last_name": "Ber", "email": member, "password": PASSWORD})
    _signup(client)  # owner
    wid = _default_workspace_id(client)
    did = _add_domain(client, wid, "owned.com")
    assert client.post(f"{WORKSPACES_URL}/{wid}/members", json={"email": member}).status_code == 201

    assert client.post(LOGIN_URL, json={"email": member, "password": PASSWORD}).status_code == 200
    assert client.delete(f"{WORKSPACES_URL}/{wid}/domains/{did}").status_code == 403
