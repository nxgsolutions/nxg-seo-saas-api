"""Shopify import into site_pages + Page Discovery filters (needs the DB)."""

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.domain import Domain
from app.models.site_page import SitePage
from app.models.workspace import Workspace
from app.services.shopify import sync

REGISTER = "/api/auth/register"
LOGIN = "/api/auth/login"
WS = "/api/workspaces"
PW = "SecurePass123"
CONN = SimpleNamespace(shop="s.myshopify.com", primary_host="example-store.com")


def _setup(client: TestClient, db_session):
    email = f"shopimp-{uuid4().hex}@example.com"
    client.post(REGISTER, json={"first_name": "S", "last_name": "I", "email": email, "password": PW})
    assert client.post(LOGIN, json={"email": email, "password": PW}).status_code == 200
    wid = client.get(WS).json()[0]["id"]
    did = client.post(f"{WS}/{wid}/domains", json={"url": "example-store.com"}).json()["id"]
    return wid, did, db_session.get(Workspace, wid), db_session.get(Domain, did)


def _product(pid="1", handle="red-shoe", published=True):
    return sync.normalize_product(CONN, {
        "id": f"gid://shopify/Product/{pid}", "handle": handle, "title": "Red Shoe",
        "descriptionHtml": "<p>Great red shoe for running</p>", "status": "ACTIVE",
        "onlineStoreUrl": f"https://example-store.com/products/{handle}" if published else None,
        "updatedAt": "2026-11-01T10:00:00Z", "seo": {"title": "Buy Red Shoe", "description": "Comfy"},
        "media": {"nodes": []},
    })


def _page(pid="5"):
    return sync.normalize_page(CONN, {
        "id": f"gid://shopify/Page/{pid}", "handle": "about-us", "title": "About", "body": "<p>About us</p>",
        "isPublished": True, "updatedAt": "2026-10-01T00:00:00Z", "titleTag": None, "descriptionTag": None,
    })


def test_import_lists_and_filters(client, db_session):
    wid, did, ws, dom = _setup(client, db_session)
    now = datetime.now(timezone.utc)
    sync.apply_item(db_session, ws, dom, _product(), now)
    sync.apply_item(db_session, ws, dom, _page(), now)
    db_session.commit()

    base = f"{WS}/{wid}/domains/{did}/page-discovery"
    rows = client.get(base, params={"source": "shopify"}).json()["rows"]
    assert {r["external_type"] for r in rows} == {"product", "page"}
    product = next(r for r in rows if r["external_type"] == "product")
    assert product["page_type"] == "product" and product["seo_title"] == "Buy Red Shoe"
    assert product["discovery_source"] == "shopify" and product["published"] is True

    only_pages = client.get(base, params={"source": "shopify", "external_type": "page"}).json()["rows"]
    assert [r["external_type"] for r in only_pages] == ["page"]

    content = client.get(f"{base}/pages/{product['id']}/content").json()
    assert content["cached"] is True
    assert content["title"] == "Buy Red Shoe" and content["meta"] == "Comfy"
    assert "Great red shoe" in content["body_html"]


def test_import_is_idempotent_and_follows_handle_change(client, db_session):
    _, _, ws, dom = _setup(client, db_session)
    now = datetime.now(timezone.utc)
    first = sync.apply_item(db_session, ws, dom, _product(), now)
    db_session.commit()
    again = sync.apply_item(db_session, ws, dom, _product(), now)
    db_session.commit()
    assert first.id == again.id

    moved = sync.apply_item(db_session, ws, dom, _product(handle="crimson-shoe"), now)
    db_session.commit()
    assert moved.id == first.id
    assert moved.url_key.endswith("/products/crimson-shoe")
    count = db_session.execute(
        select(SitePage).where(SitePage.domain_id == dom.id, SitePage.external_id == "gid://shopify/Product/1")
    ).scalars().all()
    assert len(count) == 1


def test_import_merges_existing_sitemap_row(client, db_session):
    _, _, ws, dom = _setup(client, db_session)
    from app.services import page_history_service

    existing = page_history_service.get_or_create_page(db_session, ws, "https://example-store.com/products/red-shoe")
    existing.discovery_source = "sitemap"
    db_session.commit()
    page = sync.apply_item(db_session, ws, dom, _product(), datetime.now(timezone.utc))
    db_session.commit()
    assert page.id == existing.id and page.external_source == "shopify"


def test_mark_removed(client, db_session):
    _, _, ws, dom = _setup(client, db_session)
    now = datetime.now(timezone.utc)
    sync.apply_item(db_session, ws, dom, _product("1"), now)
    sync.apply_item(db_session, ws, dom, _product("2", handle="blue-shoe"), now)
    db_session.commit()
    assert sync._mark_removed(db_session, dom, {"gid://shopify/Product/1"}) == 1
    db_session.commit()
    gone = db_session.execute(
        select(SitePage).where(SitePage.external_id == "gid://shopify/Product/2", SitePage.domain_id == dom.id)
    ).scalar_one()
    assert gone.http_status == 410 and gone.published is False


# ── publish / conflict / undo (Shopify mocked) ──────────────────────────────

class _FakeShopify:
    """Stands in for the store: holds one product and applies mutations."""

    def __init__(self):
        self.node = {
            "id": "gid://shopify/Product/1", "handle": "red-shoe", "title": "Red Shoe",
            "descriptionHtml": "<p>Great red shoe for running</p>", "status": "ACTIVE",
            "onlineStoreUrl": "https://example-store.com/products/red-shoe",
            "updatedAt": "2026-11-01T10:00:00Z", "seo": {"title": "Buy Red Shoe", "description": "Comfy"},
            "media": {"nodes": []},
        }
        self.mutations = []

    def fetch_one(self, db, conn, external_type, external_id):
        return sync.normalize_product(CONN, self.node)

    def graphql(self, db, conn, query, variables=None):
        self.mutations.append(variables)
        p = (variables or {}).get("product") or {}
        if "seo" in p:
            self.node["seo"] = {**self.node["seo"], **p["seo"]}
        if "descriptionHtml" in p:
            self.node["descriptionHtml"] = p["descriptionHtml"]
        self.node["updatedAt"] = "2026-11-02T10:00:00Z"
        return {"productUpdate": {"product": {"id": p.get("id")}, "userErrors": []}}


def _publish_setup(client, db_session, monkeypatch):
    from app.services.shopify import connection_service, publish

    wid, did, ws, dom = _setup(client, db_session)
    page = sync.apply_item(db_session, ws, dom, _product(), datetime.now(timezone.utc))
    db_session.commit()
    fake = _FakeShopify()
    monkeypatch.setattr(connection_service, "get_active", lambda db, domain: CONN)
    monkeypatch.setattr(connection_service, "get", lambda db, domain: CONN)
    monkeypatch.setattr(connection_service, "graphql", fake.graphql)
    monkeypatch.setattr(sync, "fetch_one", fake.fetch_one)
    return wid, did, dom, page, fake, publish


def test_publish_sends_only_changes_logs_and_updates_row(client, db_session, monkeypatch):
    wid, did, dom, page, fake, publish = _publish_setup(client, db_session, monkeypatch)
    url = f"{WS}/{wid}/domains/{did}/page-discovery/pages/{page.id}/shopify"
    loaded = client.get(url).json()
    assert loaded["seo_title"] == "Buy Red Shoe" and loaded["admin_url"].endswith("/admin/products/1")

    resp = client.put(url, json={"seo_description": "Lightweight red running shoe", "seo_title": "Buy Red Shoe",
                                 "expected_updated_at": loaded["updated_at"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "published" and body["changed"] == ["seo_description"]
    assert fake.mutations[-1]["product"] == {"id": "gid://shopify/Product/1",
                                             "seo": {"description": "Lightweight red running shoe"}}
    db_session.expire_all()
    assert db_session.get(SitePage, page.id).seo_description == "Lightweight red running shoe"

    history = client.get(f"{url}/history").json()
    assert history[0]["status"] == "ok" and history[0]["changed"] == ["seo_description"]


def test_publish_conflict_returns_409_with_current(client, db_session, monkeypatch):
    wid, did, dom, page, fake, publish = _publish_setup(client, db_session, monkeypatch)
    url = f"{WS}/{wid}/domains/{did}/page-discovery/pages/{page.id}/shopify"
    resp = client.put(url, json={"seo_title": "New", "expected_updated_at": "2026-10-01T00:00:00Z"})
    assert resp.status_code == 409
    assert resp.json()["code"] == "shopify_conflict" and resp.json()["current"]["seo_title"] == "Buy Red Shoe"
    assert fake.mutations == []
    forced = client.put(url, json={"seo_title": "New", "expected_updated_at": "2026-10-01T00:00:00Z", "force": True})
    assert forced.status_code == 200 and forced.json()["changed"] == ["seo_title"]


def test_unchanged_publish_sends_nothing(client, db_session, monkeypatch):
    wid, did, dom, page, fake, publish = _publish_setup(client, db_session, monkeypatch)
    url = f"{WS}/{wid}/domains/{did}/page-discovery/pages/{page.id}/shopify"
    resp = client.put(url, json={"seo_title": "Buy Red Shoe"})
    assert resp.json()["status"] == "unchanged" and fake.mutations == []


def test_undo_restores_previous_values(client, db_session, monkeypatch):
    wid, did, dom, page, fake, publish = _publish_setup(client, db_session, monkeypatch)
    url = f"{WS}/{wid}/domains/{did}/page-discovery/pages/{page.id}/shopify"
    log_id = client.put(url, json={"seo_title": "Changed title"}).json()["log_id"]
    assert fake.node["seo"]["title"] == "Changed title"
    undo = client.post(f"{url}/revert/{log_id}")
    assert undo.status_code == 200, undo.text
    assert fake.node["seo"]["title"] == "Buy Red Shoe"


def test_publish_on_non_shopify_page_is_400(client, db_session, monkeypatch):
    from app.services import page_history_service

    wid, did, dom, page, fake, publish = _publish_setup(client, db_session, monkeypatch)
    ws = db_session.get(Workspace, wid)
    plain = page_history_service.get_or_create_page(db_session, ws, "https://example-store.com/blog/post")
    plain.domain_id = dom.id
    plain.discovery_source = "sitemap"
    db_session.commit()
    url = f"{WS}/{wid}/domains/{did}/page-discovery/pages/{plain.id}/shopify"
    assert client.put(url, json={"seo_title": "x"}).status_code == 400
