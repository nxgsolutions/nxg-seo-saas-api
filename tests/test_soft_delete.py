"""Soft-delete behavior for the user-facing data entities.

Each delete stamps deleted_at (row preserved), hides the row from active reads,
is restorable, and re-adding the same value reactivates the same row instead of
raising a uniqueness conflict. Domain soft-delete/restore lives in
test_onboarding_domains.py.
"""

from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text

REGISTER = "/api/auth/register"
LOGIN = "/api/auth/login"
WS = "/api/workspaces"
PW = "SecurePass123"


def _domain(client: TestClient) -> tuple[str, str]:
    """Sign up, return (workspace_id, domain_base_url)."""
    email = f"sd-{uuid4().hex}@example.com"
    client.post(REGISTER, json={"first_name": "S", "last_name": "D", "email": email, "password": PW})
    assert client.post(LOGIN, json={"email": email, "password": PW}).status_code == 200
    wid = client.get(WS).json()[0]["id"]
    did = client.post(
        f"{WS}/{wid}/domains", json={"name": "Site", "url": f"https://{uuid4().hex[:8]}.com"}
    ).json()["id"]
    return wid, f"{WS}/{wid}/domains/{did}"


def test_saved_keyword_delete_restore_reactivate(client: TestClient) -> None:
    _, base = _domain(client)
    mk = f"{base}/my-keywords"
    created = client.post(mk, json={"keyword": "upsc syllabus"})
    kid = created.json()["id"]
    assert client.get(f"{mk}/keys").json() == ["upsc syllabus"]

    # Soft-delete → hidden from the list.
    assert client.delete(f"{mk}/{kid}").status_code == 204
    assert client.get(f"{mk}/keys").json() == []

    # Restore → back in the list.
    assert client.post(f"{mk}/{kid}/restore").status_code == 204
    assert client.get(f"{mk}/keys").json() == ["upsc syllabus"]

    # Re-saving after delete reactivates the SAME row (no duplicate).
    assert client.delete(f"{mk}/{kid}").status_code == 204
    again = client.post(mk, json={"keyword": "upsc syllabus"})
    assert again.json()["id"] == kid


def test_tracked_keyword_delete_restore(client: TestClient) -> None:
    _, base = _domain(client)
    tk = f"{base}/tracked-keywords"
    kid = client.post(tk, json={"term": "ssc cgl"}).json()["id"]
    assert "ssc cgl" in [r["term"] for r in client.get(tk).json()]

    assert client.delete(f"{tk}/{kid}").status_code == 204
    assert "ssc cgl" not in [r["term"] for r in client.get(tk).json()]

    assert client.post(f"{tk}/{kid}/restore").status_code == 204
    assert "ssc cgl" in [r["term"] for r in client.get(tk).json()]

    # Re-adding after delete reactivates the same row (no 409).
    assert client.delete(f"{tk}/{kid}").status_code == 204
    assert client.post(tk, json={"term": "ssc cgl"}).json()["id"] == kid


def test_competitor_delete_preserves_snapshots_and_restores(client: TestClient, db_session) -> None:
    wid, base = _domain(client)
    did = base.split("/domains/")[1]
    assert client.post(f"{base}/competitors/list", json={"domain": "rival.com"}).status_code == 201
    cid = next(r["id"] for r in client.get(f"{base}/competitors/list").json()["rows"])

    # Seed a rank snapshot that must survive the delete (history preservation).
    db_session.execute(
        text(
            "INSERT INTO competitor_rank_snapshots "
            "(domain_id, competitor, captured_on, avg_position, best_position) "
            "VALUES (:d, 'rival.com', CURRENT_DATE, 5.0, 3)"
        ),
        {"d": did},
    )
    db_session.commit()

    assert client.delete(f"{base}/competitors/list/{cid}").status_code == 204
    assert "rival.com" not in [r["competitor"] for r in client.get(f"{base}/competitors/list").json()["rows"]]
    # Snapshot history preserved.
    assert db_session.execute(
        text("select count(*) from competitor_rank_snapshots where domain_id = :d"), {"d": did}
    ).scalar_one() == 1

    assert client.post(f"{base}/competitors/list/{cid}/restore").status_code == 204
    assert "rival.com" in [r["competitor"] for r in client.get(f"{base}/competitors/list").json()["rows"]]


def test_category_delete_restore_reactivate(client: TestClient) -> None:
    _domain(client)  # authenticate
    name = f"Niche {uuid4().hex[:6]}"
    cid = client.post("/api/categories", json={"name": name}).json()["id"]
    assert name in [c["name"] for c in client.get("/api/categories").json()]

    assert client.delete(f"/api/categories/{cid}").status_code == 204
    assert name not in [c["name"] for c in client.get("/api/categories").json()]

    assert client.post(f"/api/categories/{cid}/restore").status_code == 200
    assert name in [c["name"] for c in client.get("/api/categories").json()]

    # Re-creating a deleted category by name reactivates the same row.
    assert client.delete(f"/api/categories/{cid}").status_code == 204
    assert client.post("/api/categories", json={"name": name}).json()["id"] == cid
