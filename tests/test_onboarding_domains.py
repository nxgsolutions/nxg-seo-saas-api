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


def test_add_domain_with_category_links_it(client: TestClient) -> None:
    _signup(client)
    wid = _default_workspace_id(client)
    cat = client.post("/api/categories", json={"name": "Real Estate"})
    assert cat.status_code in (200, 201)
    cat_id = cat.json()["id"]

    resp = client.post(
        f"{WORKSPACES_URL}/{wid}/domains",
        json={"url": "realty.example", "category_id": cat_id},
    )
    assert resp.status_code == 201
    assert resp.json()["category_id"] == cat_id


def test_add_domain_without_category_is_uncategorized(client: TestClient) -> None:
    _signup(client)
    wid = _default_workspace_id(client)
    resp = client.post(f"{WORKSPACES_URL}/{wid}/domains", json={"url": "nocat.example"})
    assert resp.status_code == 201
    assert resp.json()["category_id"] is None


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


def test_delete_domain_soft_deletes_and_preserves_data(client: TestClient, db_session) -> None:
    from sqlalchemy import text

    _signup(client)
    wid = _default_workspace_id(client)
    keep = _add_domain(client, wid, "keep-site.com")
    gone = _add_domain(client, wid, "gone-site.com")
    # A child row whose history must be PRESERVED (not cascaded away) on delete.
    assert client.post(
        f"{WORKSPACES_URL}/{wid}/domains/{gone}/competitors/list", json={"domain": "rival.com"}
    ).status_code == 201

    assert client.delete(f"{WORKSPACES_URL}/{wid}/domains/{gone}").status_code == 204

    # Hidden from the active list…
    remaining = [d["id"] for d in client.get(f"{WORKSPACES_URL}/{wid}/domains").json()]
    assert remaining == [keep]
    # …and domain-scoped access now 404s (the resolution gate skips deleted rows).
    assert client.delete(f"{WORKSPACES_URL}/{wid}/domains/{gone}").status_code == 404

    # The domain row is soft-deleted (deleted_at set), NOT physically removed…
    row = db_session.execute(
        text("select deleted_at from domains where id = :d"), {"d": gone}
    ).first()
    assert row is not None and row[0] is not None
    # …and its child data is kept for history / restore.
    left = db_session.execute(
        text("select count(*) from tracked_competitors where domain_id = :d"), {"d": gone}
    ).scalar_one()
    assert left == 1


def test_restore_domain_brings_it_back(client: TestClient) -> None:
    _signup(client)
    wid = _default_workspace_id(client)
    did = _add_domain(client, wid, "restore-me.com")
    assert client.delete(f"{WORKSPACES_URL}/{wid}/domains/{did}").status_code == 204
    assert did not in [d["id"] for d in client.get(f"{WORKSPACES_URL}/{wid}/domains").json()]

    assert client.post(f"{WORKSPACES_URL}/{wid}/domains/{did}/restore").status_code == 204
    assert did in [d["id"] for d in client.get(f"{WORKSPACES_URL}/{wid}/domains").json()]
    # Restoring a live (non-deleted) domain 404s.
    assert client.post(f"{WORKSPACES_URL}/{wid}/domains/{did}/restore").status_code == 404


def test_re_add_deleted_domain_reactivates_same_row(client: TestClient, db_session) -> None:
    from sqlalchemy import text

    _signup(client)
    wid = _default_workspace_id(client)
    did = _add_domain(client, wid, "reuse.com")
    assert client.post(
        f"{WORKSPACES_URL}/{wid}/domains/{did}/competitors/list", json={"domain": "rival.com"}
    ).status_code == 201
    assert client.delete(f"{WORKSPACES_URL}/{wid}/domains/{did}").status_code == 204

    # Re-adding the same host reactivates the SAME row (no duplicate / no 409)…
    re_id = _add_domain(client, wid, "reuse.com")
    assert re_id == did
    # …with its preserved competitor still attached.
    left = db_session.execute(
        text("select count(*) from tracked_competitors where domain_id = :d"), {"d": did}
    ).scalar_one()
    assert left == 1


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
