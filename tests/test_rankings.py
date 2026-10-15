"""Tests for rank tracking (tracked keywords + position entries)."""

from uuid import uuid4

from fastapi.testclient import TestClient

REGISTER = "/api/auth/register"
LOGIN = "/api/auth/login"
WS = "/api/workspaces"
PW = "SecurePass123"


def _signup(client: TestClient) -> None:
    email = f"rank-{uuid4().hex}@example.com"
    client.post(REGISTER, json={"first_name": "R", "last_name": "K", "email": email, "password": PW})
    assert client.post(LOGIN, json={"email": email, "password": PW}).status_code == 200


def _workspace_id(client: TestClient) -> str:
    return client.get(WS).json()[0]["id"]


def _domain(client: TestClient, wid: str, domain: str = "ranktest.com") -> str:
    resp = client.post(f"{WS}/{wid}/domains", json={"url": domain})
    assert resp.status_code == 201
    return resp.json()["id"]


def _base(client: TestClient) -> str:
    _signup(client)
    wid = _workspace_id(client)
    site = _domain(client, wid)
    return f"{WS}/{wid}/domains/{site}"


def test_add_and_list_keyword(client: TestClient) -> None:
    base = _base(client)
    resp = client.post(f"{base}/tracked-keywords", json={"term": "seo tools"})
    assert resp.status_code == 201
    assert resp.json()["term"] == "seo tools"
    assert resp.json()["entry_count"] == 0

    listed = client.get(f"{base}/tracked-keywords").json()
    assert len(listed) == 1
    assert listed[0]["latest_position"] is None


def test_duplicate_keyword_409(client: TestClient) -> None:
    base = _base(client)
    client.post(f"{base}/tracked-keywords", json={"term": "dupe"})
    assert client.post(f"{base}/tracked-keywords", json={"term": "dupe"}).status_code == 409


def test_sync_gsc_records_matching_positions(client: TestClient, monkeypatch) -> None:
    from app.services import gsc_service

    base = _base(client)
    client.post(f"{base}/tracked-keywords", json={"term": "seo tools"})
    client.post(f"{base}/tracked-keywords", json={"term": "not in gsc"})

    monkeypatch.setattr(
        gsc_service,
        "keyword_performance",
        lambda db, ws, site, days=28: {"keywords": [{"keyword": "SEO Tools", "position": 4.4}]},
    )
    resp = client.post(f"{base}/tracked-keywords/sync-gsc")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert body["synced"] == 1  # only "seo tools" matched (case-insensitive)

    # The matched keyword now has a recorded position of 4 (rounded).
    listed = client.get(f"{base}/tracked-keywords").json()
    matched = next(k for k in listed if k["term"] == "seo tools")
    assert matched["latest_position"] == 4


def test_sync_gsc_not_connected_409(client: TestClient, monkeypatch) -> None:
    from app.services import gsc_service

    base = _base(client)

    def _raise(*_a, **_k):
        raise gsc_service.GscNotConnectedError()

    monkeypatch.setattr(gsc_service, "keyword_performance", _raise)
    assert client.post(f"{base}/tracked-keywords/sync-gsc").status_code == 409


def test_import_gsc_creates_tracked_keywords_with_positions(client: TestClient, monkeypatch) -> None:
    from app.services import gsc_service

    base = _base(client)
    # One already tracked (so it's skipped on import, but still gets a position).
    client.post(f"{base}/tracked-keywords", json={"term": "seo tools"})

    monkeypatch.setattr(
        gsc_service,
        "keyword_performance",
        lambda db, ws, site, days=28: {"keywords": [
            {"keyword": "SEO Tools", "clicks": 10, "impressions": 100, "position": 3.2},
            {"keyword": "rank tracker", "clicks": 5, "impressions": 80, "position": 8.0},
            {"keyword": 'site:example.com', "clicks": 4, "impressions": 4, "position": 1.0},  # operator, skipped
        ]},
    )

    resp = client.post(f"{base}/tracked-keywords/import-gsc")
    assert resp.status_code == 200
    body = resp.json()
    assert body["imported"] == 1  # only "rank tracker" is new (operator filtered, "seo tools" existed)
    assert body["total"] == 2
    assert body["synced"] == 2  # both real queries got a position

    listed = {k["term"]: k for k in client.get(f"{base}/tracked-keywords").json()}
    assert set(listed) == {"seo tools", "rank tracker"}
    assert listed["seo tools"]["latest_position"] == 3
    assert listed["rank tracker"]["latest_position"] == 8


def test_import_gsc_not_connected_409(client: TestClient, monkeypatch) -> None:
    from app.services import gsc_service

    base = _base(client)
    monkeypatch.setattr(
        gsc_service, "keyword_performance",
        lambda *_a, **_k: (_ for _ in ()).throw(gsc_service.GscNotConnectedError()),
    )
    assert client.post(f"{base}/tracked-keywords/import-gsc").status_code == 409


def test_record_positions_and_summary(client: TestClient) -> None:
    base = _base(client)
    kid = client.post(f"{base}/tracked-keywords", json={"term": "ranking"}).json()["id"]

    client.post(f"{base}/tracked-keywords/{kid}/entries", json={"position": 20, "recorded_at": "2026-01-01"})
    client.post(f"{base}/tracked-keywords/{kid}/entries", json={"position": 12, "recorded_at": "2026-01-08"})
    client.post(f"{base}/tracked-keywords/{kid}/entries", json={"position": 15, "recorded_at": "2026-01-15"})

    summary = next(k for k in client.get(f"{base}/tracked-keywords").json() if k["id"] == kid)
    assert summary["latest_position"] == 15
    assert summary["previous_position"] == 12
    assert summary["best_position"] == 12
    assert summary["entry_count"] == 3

    entries = client.get(f"{base}/tracked-keywords/{kid}/entries").json()
    assert [e["position"] for e in entries] == [20, 12, 15]  # chronological


def test_record_position_upserts_by_date(client: TestClient) -> None:
    base = _base(client)
    kid = client.post(f"{base}/tracked-keywords", json={"term": "upsert"}).json()["id"]
    client.post(f"{base}/tracked-keywords/{kid}/entries", json={"position": 30, "recorded_at": "2026-02-01"})
    client.post(f"{base}/tracked-keywords/{kid}/entries", json={"position": 8, "recorded_at": "2026-02-01"})

    entries = client.get(f"{base}/tracked-keywords/{kid}/entries").json()
    assert len(entries) == 1
    assert entries[0]["position"] == 8


def test_invalid_position_422(client: TestClient) -> None:
    base = _base(client)
    kid = client.post(f"{base}/tracked-keywords", json={"term": "bad"}).json()["id"]
    assert client.post(f"{base}/tracked-keywords/{kid}/entries", json={"position": 0}).status_code == 422


def test_delete_keyword(client: TestClient) -> None:
    base = _base(client)
    kid = client.post(f"{base}/tracked-keywords", json={"term": "temp"}).json()["id"]
    assert client.delete(f"{base}/tracked-keywords/{kid}").status_code == 204
    assert client.get(f"{base}/tracked-keywords").json() == []


def test_domain_not_in_workspace_404(client: TestClient) -> None:
    _signup(client)
    wid = _workspace_id(client)
    resp = client.get(f"{WS}/{wid}/domains/{uuid4()}/tracked-keywords")
    assert resp.status_code == 404


def test_non_member_cannot_access(client: TestClient) -> None:
    base = _base(client)  # user A
    _signup(client)  # user B (different session)
    assert client.get(f"{base}/tracked-keywords").status_code == 404


def test_requires_auth(client: TestClient) -> None:
    resp = client.get(f"{WS}/{uuid4()}/domains/{uuid4()}/tracked-keywords")
    assert resp.status_code == 401
