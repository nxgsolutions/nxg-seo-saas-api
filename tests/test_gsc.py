"""Tests for the Google Search Console integration.

Network-touching paths are not exercised here; we cover the pure logic
(property matching, rank buckets, signed state) and the connection lifecycle /
error mapping via the API using an unconfigured, unconnected workspace.
"""

from uuid import uuid4

from fastapi.testclient import TestClient

from app.services import gsc_service

REGISTER = "/api/auth/register"
LOGIN = "/api/auth/login"
WS = "/api/workspaces"
PW = "SecurePass123"


# --- pure logic -------------------------------------------------------------
def test_match_property_prefers_domain_property() -> None:
    sites = [
        {"siteUrl": "https://example.com/"},
        {"siteUrl": "sc-domain:example.com"},
    ]
    assert gsc_service.match_property(sites, "example.com") == "sc-domain:example.com"


def test_match_property_url_prefix_and_www() -> None:
    sites = [{"siteUrl": "https://www.example.com/"}]
    assert gsc_service.match_property(sites, "example.com") == "https://www.example.com/"


def test_match_property_no_match() -> None:
    sites = [{"siteUrl": "sc-domain:other.com"}]
    assert gsc_service.match_property(sites, "example.com") is None


def test_rank_bucket_boundaries() -> None:
    assert gsc_service._rank_bucket(1.0) == "top3"
    assert gsc_service._rank_bucket(3.0) == "top3"
    assert gsc_service._rank_bucket(3.5) == "top10"
    assert gsc_service._rank_bucket(10.0) == "top10"
    assert gsc_service._rank_bucket(15.0) == "top20"
    assert gsc_service._rank_bucket(50.0) == "beyond"


def test_state_roundtrip() -> None:
    token = gsc_service.sign_state("w1", "u1", "/dashboard/page-audit")
    payload = gsc_service.decode_state(token)
    assert payload is not None
    assert payload["wid"] == "w1"
    assert payload["uid"] == "u1"
    assert payload["ret"] == "/dashboard/page-audit"


def test_state_rejects_garbage() -> None:
    assert gsc_service.decode_state("not-a-token") is None


# --- API lifecycle ----------------------------------------------------------
def _authed_workspace(client: TestClient) -> str:
    email = f"gsc-{uuid4().hex}@example.com"
    client.post(REGISTER, json={"first_name": "G", "last_name": "C", "email": email, "password": PW})
    assert client.post(LOGIN, json={"email": email, "password": PW}).status_code == 200
    return client.get(WS).json()[0]["id"]


def test_status_unconnected(client: TestClient) -> None:
    wid = _authed_workspace(client)
    resp = client.get(f"{WS}/{wid}/gsc/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["connected"] is False
    assert body["sites"] == []


def test_connect_unconfigured_returns_503(client: TestClient, monkeypatch) -> None:
    # Force the "not configured" state regardless of the developer's .env.
    from app.core.config import settings

    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "")
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_SECRET", "")

    wid = _authed_workspace(client)
    resp = client.post(f"{WS}/{wid}/gsc/connect", json={"return_path": "/dashboard"})
    assert resp.status_code == 503


def test_page_data_not_connected_returns_409(client: TestClient) -> None:
    wid = _authed_workspace(client)
    site = client.post(f"{WS}/{wid}/domains", json={"url": "gsctest.com"}).json()["id"]
    resp = client.get(
        f"{WS}/{wid}/domains/{site}/gsc/page", params={"url": "https://gsctest.com/"}
    )
    assert resp.status_code == 409
