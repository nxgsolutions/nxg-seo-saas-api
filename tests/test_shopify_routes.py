"""Shopify connect / callback / status / disconnect / webhook routes (needs the DB)."""

import base64
import hashlib
import hmac
import json
import time
import urllib.parse
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core import crypto
from app.core.config import settings
from app.models.shopify import ShopifyConnection
from app.services.shopify import connection_service, domain_match, oauth, sync
from app.services.shopify.domain_match import ShopIdentity

REGISTER = "/api/auth/register"
LOGIN = "/api/auth/login"
WS = "/api/workspaces"
PW = "SecurePass123"
SECRET = "test-shopify-secret"
SHOP = "teststore.myshopify.com"


@pytest.fixture(autouse=True)
def _shopify_settings(monkeypatch):
    monkeypatch.setattr(settings, "SHOPIFY_API_KEY", "test-key")
    monkeypatch.setattr(settings, "SHOPIFY_API_SECRET", SECRET)
    monkeypatch.setattr(settings, "TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "SHOPIFY_WEBHOOK_BASE_URL", "")
    # Never call Shopify for real.
    monkeypatch.setattr(connection_service, "_revoke_remote", lambda shop, token: None)
    monkeypatch.setattr(sync, "run_shopify_sync", lambda *a, **k: None)


def _login(client: TestClient) -> str:
    email = f"shop-{uuid4().hex}@example.com"
    client.post(REGISTER, json={"first_name": "S", "last_name": "H", "email": email, "password": PW})
    assert client.post(LOGIN, json={"email": email, "password": PW}).status_code == 200
    return client.get(WS).json()[0]["id"]


def _domain(client: TestClient, wid: str, host: str = "example-store.com") -> str:
    resp = client.post(f"{WS}/{wid}/domains", json={"url": host})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _base(wid: str, did: str) -> str:
    return f"{WS}/{wid}/domains/{did}/shopify"


def _state_from(install_url: str) -> str:
    return urllib.parse.parse_qs(urllib.parse.urlparse(install_url).query)["state"][0]


def _signed_callback(state: str, shop: str = SHOP) -> dict:
    params = {"code": "auth-code", "shop": shop, "state": state, "timestamp": str(int(time.time()))}
    msg = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    params["hmac"] = hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return params


def _fake_shopify(monkeypatch, hosts: list[str], primary: str | None):
    monkeypatch.setattr(oauth, "exchange_code", lambda shop, code: oauth.ShopToken("shpat_raw_token", "read_products"))
    monkeypatch.setattr(
        domain_match,
        "fetch_identity",
        lambda shop, token: ShopIdentity(name="Test Store", myshopify_domain=SHOP, primary_host=primary, hosts=hosts),
    )


def _connect(client, wid, did) -> str:
    resp = client.post(f"{_base(wid, did)}/connect", json={"shop": "teststore"})
    assert resp.status_code == 200, resp.text
    return _state_from(resp.json()["install_url"])


def _row(db_session, did) -> ShopifyConnection | None:
    return db_session.execute(
        select(ShopifyConnection).where(ShopifyConnection.domain_id == did)
    ).scalar_one_or_none()


# --- connect / status -------------------------------------------------------
def test_status_unconnected(client):
    wid = _login(client)
    did = _domain(client, wid)
    body = client.get(_base(wid, did)).json()
    assert body["configured"] is True and body["connected"] is False


def test_connect_unconfigured_returns_503(client, monkeypatch):
    wid = _login(client)
    did = _domain(client, wid)
    monkeypatch.setattr(settings, "SHOPIFY_API_KEY", "")
    assert client.post(f"{_base(wid, did)}/connect", json={"shop": "teststore"}).status_code == 503


def test_connect_bad_shop_returns_400(client):
    wid = _login(client)
    did = _domain(client, wid)
    assert client.post(f"{_base(wid, did)}/connect", json={"shop": "evil.com"}).status_code == 400


def test_connect_other_workspace_domain_is_404(client):
    wid = _login(client)
    did = _domain(client, wid)
    _login(client)  # now logged in as a different user
    other_wid = client.get(WS).json()[0]["id"]
    assert client.post(f"{_base(other_wid, did)}/connect", json={"shop": "teststore"}).status_code == 404
    assert client.get(_base(wid, did)).status_code == 404


# --- callback ---------------------------------------------------------------
def test_callback_domain_mismatch_saves_nothing(client, db_session, monkeypatch):
    wid = _login(client)
    did = _domain(client, wid, "example-store.com")
    state = _connect(client, wid, did)
    _fake_shopify(monkeypatch, ["another-site.com", SHOP], "another-site.com")
    resp = client.get("/api/integrations/shopify/callback", params=_signed_callback(state), follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert "shopify=domain_mismatch" in resp.headers["location"]
    assert "found=another-site.com" in resp.headers["location"]
    assert _row(db_session, did) is None


def test_callback_bad_hmac_saves_nothing(client, db_session, monkeypatch):
    wid = _login(client)
    did = _domain(client, wid)
    state = _connect(client, wid, did)
    _fake_shopify(monkeypatch, ["example-store.com"], "example-store.com")
    params = _signed_callback(state)
    params["hmac"] = "0" * 64
    resp = client.get("/api/integrations/shopify/callback", params=params, follow_redirects=False)
    assert "shopify=error" in resp.headers["location"]
    assert _row(db_session, did) is None


def test_callback_shop_must_match_state(client, db_session, monkeypatch):
    wid = _login(client)
    did = _domain(client, wid)
    state = _connect(client, wid, did)
    _fake_shopify(monkeypatch, ["example-store.com"], "example-store.com")
    params = _signed_callback(state, shop="someone-else.myshopify.com")
    resp = client.get("/api/integrations/shopify/callback", params=params, follow_redirects=False)
    assert "shopify=error" in resp.headers["location"]
    assert _row(db_session, did) is None


def test_callback_success_stores_encrypted_token_and_status_hides_it(client, db_session, monkeypatch):
    wid = _login(client)
    did = _domain(client, wid, "example-store.com")
    state = _connect(client, wid, did)
    _fake_shopify(monkeypatch, ["www.example-store.com", SHOP], "www.example-store.com")
    resp = client.get("/api/integrations/shopify/callback", params=_signed_callback(state), follow_redirects=False)
    assert "shopify=connected" in resp.headers["location"]
    assert resp.headers["location"].startswith(settings.FRONTEND_URL + "/dashboard/settings")

    row = _row(db_session, did)
    assert row is not None and row.status == "active" and row.shop == SHOP
    assert row.access_token_enc != "shpat_raw_token"
    assert crypto.decrypt(row.access_token_enc) == "shpat_raw_token"

    status_resp = client.get(_base(wid, did))
    body = status_resp.json()
    assert body["connected"] is True and body["shop"] == SHOP and body["primary_host"] == "www.example-store.com"
    assert "shpat" not in status_resp.text and "token" not in json.dumps(list(body.keys()))


def test_disconnect_removes_row(client, db_session, monkeypatch):
    wid = _login(client)
    did = _domain(client, wid, "example-store.com")
    state = _connect(client, wid, did)
    _fake_shopify(monkeypatch, ["example-store.com"], "example-store.com")
    client.get("/api/integrations/shopify/callback", params=_signed_callback(state), follow_redirects=False)
    assert _row(db_session, did) is not None
    assert client.delete(_base(wid, did)).status_code == 204
    db_session.expire_all()
    assert _row(db_session, did) is None


# --- webhooks ---------------------------------------------------------------
def _post_webhook(client, topic: str, body: dict, webhook_id: str, *, bad_hmac: bool = False):
    raw = json.dumps(body).encode()
    sig = base64.b64encode(hmac.new(SECRET.encode(), raw, hashlib.sha256).digest()).decode()
    return client.post(
        "/api/integrations/shopify/webhooks",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Hmac-Sha256": "bad" if bad_hmac else sig,
            "X-Shopify-Topic": topic,
            "X-Shopify-Shop-Domain": SHOP,
            "X-Shopify-Webhook-Id": webhook_id,
        },
    )


def _connected(client, db_session, monkeypatch):
    wid = _login(client)
    did = _domain(client, wid, "example-store.com")
    state = _connect(client, wid, did)
    _fake_shopify(monkeypatch, ["example-store.com"], "example-store.com")
    client.get("/api/integrations/shopify/callback", params=_signed_callback(state), follow_redirects=False)
    return wid, did


def test_webhook_bad_hmac_is_401(client):
    assert _post_webhook(client, "app/uninstalled", {}, uuid4().hex, bad_hmac=True).status_code == 401


def test_webhook_uninstalled_revokes_and_dedupes(client, db_session, monkeypatch):
    wid, did = _connected(client, db_session, monkeypatch)
    wh_id = uuid4().hex
    first = _post_webhook(client, "app/uninstalled", {"id": 1}, wh_id)
    assert first.status_code == 200 and first.json()["result"] == "revoked"
    db_session.expire_all()
    row = _row(db_session, did)
    assert row.status == "revoked" and row.access_token_enc is None
    assert client.get(_base(wid, did)).json()["connected"] is False

    again = _post_webhook(client, "app/uninstalled", {"id": 1}, wh_id)
    assert again.json()["result"] == "duplicate"


def test_webhook_shop_redact_deletes(client, db_session, monkeypatch):
    _, did = _connected(client, db_session, monkeypatch)
    resp = _post_webhook(client, "shop/redact", {"shop_domain": SHOP}, uuid4().hex)
    assert resp.json()["result"] == "redacted"
    db_session.expire_all()
    assert _row(db_session, did) is None


def test_webhook_customer_topics_ack(client):
    resp = _post_webhook(client, "customers/data_request", {"customer": {"id": 1}}, uuid4().hex)
    assert resp.status_code == 200 and resp.json()["result"] == "no_customer_data"
