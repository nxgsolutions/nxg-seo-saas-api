"""My Keywords: save (idempotent), list with metrics + tracked rank, search/sort/page, remove, privacy."""

from datetime import date, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text

REGISTER = "/api/auth/register"
LOGIN = "/api/auth/login"
WS = "/api/workspaces"
PW = "SecurePass123"


def _site(client: TestClient) -> str:
    email = f"mk-{uuid4().hex}@example.com"
    client.post(REGISTER, json={"first_name": "M", "last_name": "K", "email": email, "password": PW})
    assert client.post(LOGIN, json={"email": email, "password": PW}).status_code == 200
    wid = client.get(WS).json()[0]["id"]
    did = client.post(f"{WS}/{wid}/domains", json={"name": "Site", "url": f"https://{uuid4().hex[:8]}.com"}).json()["id"]
    return f"{WS}/{wid}/domains/{did}/my-keywords"


def test_save_is_idempotent_and_normalized(client: TestClient) -> None:
    base = _site(client)
    first = client.post(base, json={"keyword": "UPSC  Syllabus", "source": "keyword_analysis"})
    assert first.status_code == 200 and first.json()["created"] is True
    again = client.post(base, json={"keyword": "upsc syllabus"})
    assert again.json()["created"] is False and again.json()["id"] == first.json()["id"]
    assert client.get(f"{base}/keys").json() == ["upsc syllabus"]
    assert client.post(base, json={"keyword": "   "}).status_code == 422


def test_list_joins_metrics_and_rank_and_pages(client: TestClient, db_session) -> None:
    base = _site(client)
    kw = f"zz test keyword {uuid4().hex[:6]}"
    for k in (kw, "alpha one", "beta two"):
        client.post(base, json={"keyword": k})
    did = base.split("/domains/")[1].split("/")[0]

    # Shared metrics + a tracked rank for `kw`. Saving already created the canonical
    # keywords row (saved_keywords_service._canonical_keyword_id), so reuse its id.
    kid = db_session.execute(text("SELECT id FROM keywords WHERE normalized_keyword = :k"),
                             {"k": kw}).scalar_one()
    db_session.execute(text("""INSERT INTO keyword_metrics (keyword_id, country_code, language_code, search_volume, cpc,
                              difficulty, metric_date) VALUES (:id, 'in', 'en', 1900, 12.5, 41, :d)"""),
                       {"id": kid, "d": date.today()})
    tid = db_session.execute(text("INSERT INTO tracked_keywords (domain_id, term) VALUES (:d, :k) RETURNING id"),
                             {"d": did, "k": kw}).scalar_one()
    for days, pos in ((7, 9), (0, 5)):
        db_session.execute(text("INSERT INTO rank_entries (keyword_id, position, recorded_at) VALUES (:t, :p, :r)"),
                           {"t": tid, "p": pos, "r": date.today() - timedelta(days=days)})
    db_session.commit()

    data = client.get(base, params={"sort": "volume", "order": "desc", "page_size": 2}).json()
    assert data["pagination"]["total"] == 3 and data["pagination"]["total_pages"] == 2
    top = data["rows"][0]
    assert top["keyword"] == kw and top["search_volume"] == 1900 and top["difficulty"] == 41.0
    assert top["rank_tracked"] is True and top["position"] == 5 and top["previous_position"] == 9

    found = client.get(base, params={"q": "alpha"}).json()
    assert [r["keyword"] for r in found["rows"]] == ["alpha one"]


def test_remove_and_privacy(client: TestClient) -> None:
    base = _site(client)
    sid = client.post(base, json={"keyword": "remove me"}).json()["id"]
    other = _site(client)  # a different user / website
    assert client.delete(f"{other}/{sid}").status_code == 404
    assert client.get(other).json()["pagination"]["total"] == 0


def test_owner_can_remove(client: TestClient) -> None:
    base = _site(client)
    sid = client.post(base, json={"keyword": "remove me"}).json()["id"]
    assert client.delete(f"{base}/{sid}").status_code == 204
    assert client.get(base).json()["pagination"]["total"] == 0
    assert client.delete(f"{base}/{sid}").status_code == 404
