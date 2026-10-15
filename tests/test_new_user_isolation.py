"""A brand-new user + workspaces never see another tenant's data.

End-to-end over the API (rolled back after each test): register, log in, get
the auto-created workspace, create a second one, add a domain, then open every
keyword-research surface and check it's empty / scoped — and that another
workspace's ids are refused.
"""

from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

PASSWORD = "SecurePass123"


def _new_user(client: TestClient) -> None:
    email = f"iso-{uuid4().hex}@example.com"
    r = client.post("/api/auth/register",
                    json={"first_name": "New", "last_name": "User", "email": email, "password": PASSWORD})
    assert r.status_code == 201, r.text
    assert client.post("/api/auth/login", json={"email": email, "password": PASSWORD}).status_code == 200


def _setup(client: TestClient) -> tuple[str, str, str]:
    _new_user(client)
    workspaces = client.get("/api/workspaces").json()
    assert len(workspaces) == 1 and workspaces[0]["is_sandbox"] is False
    ws = workspaces[0]["id"]

    second = client.post("/api/workspaces", json={"name": "Second workspace"})
    assert second.status_code == 201, second.text
    assert {w["id"] for w in client.get("/api/workspaces").json()} == {ws, second.json()["id"]}

    d = client.post(f"/api/workspaces/{ws}/domains", json={"url": f"https://newsite-{uuid4().hex[:8]}.example"})
    assert d.status_code == 201, d.text
    return ws, second.json()["id"], d.json()["id"]


def test_new_user_keyword_research_is_empty_and_scoped(client: TestClient) -> None:
    ws, ws2, did = _setup(client)
    base = f"/api/workspaces/{ws}"

    for w in (ws, ws2):
        assert client.get(f"/api/workspaces/{w}/keyword-search/researches").json() == []
        assert client.get(f"/api/workspaces/{w}/keyword-search/domain-searches").json() == []
    assert client.get(f"/api/workspaces/{ws2}/domains").json() == []

    dom = f"{base}/domains/{did}"
    assert client.get(f"{dom}/competitors/list").json()["rows"] == []
    assert client.get(f"{dom}/tracked-keywords").json() == []

    plan = client.get(f"{dom}/opportunity/action-plan")
    assert plan.status_code == 200
    assert plan.json()["counts"] == {"improve": 0, "gaps": 0, "targets": 0}

    assert client.get(f"{dom}/opportunity/strategy").status_code == 200

    overview = client.post(f"{dom}/ai-overview/action-plan")
    assert overview.status_code == 200 and overview.json()["source"] == "rules"
    assert overview.json()["summary"].startswith("No actions yet")

    # Shared SERP data is readable, but nothing is marked as "you".
    bd = client.get(f"{base}/serp/breakdown", params={"keyword": "upsc pyq"})
    assert bd.status_code == 200
    assert not any(r["is_you"] for r in bd.json()["rows"])


def test_new_user_cannot_open_other_workspaces_data(client: TestClient, db_session: Session) -> None:
    other = db_session.execute(text(
        "SELECT r.id, r.workspace_id FROM keyword_researches r ORDER BY r.created_at DESC LIMIT 1")).first()
    other_domain = db_session.execute(text("SELECT id, workspace_id FROM domains LIMIT 1")).first()
    ws, _, did = _setup(client)

    if other is not None:
        # Their workspace, and their search via my workspace — both refused.
        assert client.get(f"/api/workspaces/{other.workspace_id}/keyword-search/researches").status_code == 404
        assert client.get(f"/api/workspaces/{ws}/keyword-search/researches/{other.id}").status_code == 404
        assert client.post(f"/api/workspaces/{ws}/ai-overview/keyword-search/{other.id}").status_code == 404
    if other_domain is not None:
        assert client.get(
            f"/api/workspaces/{ws}/domains/{other_domain.id}/opportunity/action-plan").status_code == 404
        assert client.get(
            f"/api/workspaces/{other_domain.workspace_id}/domains/{other_domain.id}/competitors/list").status_code == 404


def test_production_refuses_sandbox_workspace(client: TestClient, monkeypatch) -> None:
    from app.core.config import settings
    monkeypatch.setattr(settings, "DATAFORSEO_SANDBOX_ENABLED", False)
    _new_user(client)
    r = client.post("/api/workspaces", json={"name": "Sneaky", "is_sandbox": True})
    assert r.status_code >= 400
