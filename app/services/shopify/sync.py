"""Import a Shopify store's products and pages into Page Discovery.

    run_shopify_sync(workspace_id, domain_id)   # background worker

Pulls every product and online-store page through the GraphQL Admin API
(cursor pagination), upserts them into ``site_pages`` linked by Shopify GID,
marks items deleted in Shopify as gone (410), then fetches + scores the live,
published pages with the same enrichment Page Discovery uses. Recorded as a
``page_discovery_runs`` row with source='shopify' so the existing progress,
cancel and banner UI work unchanged.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.domain import Domain
from app.models.page_discovery import PageDiscoveryRun
from app.models.shopify import ShopifyConnection
from app.models.site_page import SitePage
from app.models.workspace import Workspace
from app.services import page_discovery_service, page_history_service
from app.services.shopify import connection_service
from app.services.shopify.html import sanitize, word_count

log = logging.getLogger(__name__)

PAGE_SIZE = 100
MAX_ITEMS = page_discovery_service.MAX_PAGES

PRODUCT_FIELDS = """
  id handle title descriptionHtml status onlineStoreUrl updatedAt
  seo { title description }
  media(first: 20) { nodes { ... on MediaImage { id alt image { url } } } }
"""

PRODUCTS_QUERY = f"""
query Products($first: Int!, $after: String) {{
  products(first: $first, after: $after, sortKey: ID) {{
    pageInfo {{ hasNextPage endCursor }}
    nodes {{ {PRODUCT_FIELDS} }}
  }}
}}
"""

PAGE_FIELDS = """
  id handle title body isPublished updatedAt
  titleTag: metafield(namespace: "global", key: "title_tag") { value }
  descriptionTag: metafield(namespace: "global", key: "description_tag") { value }
"""

PAGES_QUERY = f"""
query Pages($first: Int!, $after: String) {{
  pages(first: $first, after: $after, sortKey: ID) {{
    pageInfo {{ hasNextPage endCursor }}
    nodes {{ {PAGE_FIELDS} }}
  }}
}}
"""

PRODUCT_QUERY = f"query Product($id: ID!) {{ product(id: $id) {{ {PRODUCT_FIELDS} }} }}"
PAGE_QUERY = f"query Page($id: ID!) {{ page(id: $id) {{ {PAGE_FIELDS} }} }}"


# ── normalisation (pure) ─────────────────────────────────────────────────────

def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def public_url(conn: ShopifyConnection, kind: str, handle: str, online_store_url: str | None = None) -> str:
    if online_store_url:
        return online_store_url
    host = conn.primary_host or conn.shop
    path = "products" if kind == "product" else "pages"
    return f"https://{host}/{path}/{handle}"


def normalize_product(conn: ShopifyConnection, node: dict) -> dict:
    images = [
        {"id": m.get("id"), "alt": m.get("alt") or "", "url": (m.get("image") or {}).get("url")}
        for m in ((node.get("media") or {}).get("nodes") or [])
        if m and m.get("id")
    ]
    seo = node.get("seo") or {}
    body = sanitize(node.get("descriptionHtml"))
    return {
        "external_type": "product",
        "external_id": node["id"],
        "handle": node.get("handle") or "",
        "title": node.get("title") or "",
        "body_html": body,
        "seo_title": seo.get("title"),
        "seo_description": seo.get("description"),
        "published": bool(node.get("onlineStoreUrl")) and (node.get("status") or "ACTIVE") == "ACTIVE",
        "updated_at": _parse_dt(node.get("updatedAt")),
        "url": public_url(conn, "product", node.get("handle") or "", node.get("onlineStoreUrl")),
        "meta": {"images": images, "status": node.get("status")},
    }


def normalize_page(conn: ShopifyConnection, node: dict) -> dict:
    body = sanitize(node.get("body"))
    return {
        "external_type": "page",
        "external_id": node["id"],
        "handle": node.get("handle") or "",
        "title": node.get("title") or "",
        "body_html": body,
        "seo_title": (node.get("titleTag") or {}).get("value"),
        "seo_description": (node.get("descriptionTag") or {}).get("value"),
        "published": bool(node.get("isPublished")),
        "updated_at": _parse_dt(node.get("updatedAt")),
        "url": public_url(conn, "page", node.get("handle") or ""),
        "meta": {},
    }


# ── fetch ────────────────────────────────────────────────────────────────────

def _paginate(db: Session, conn: ShopifyConnection, query: str, root: str, limit: int) -> list[dict]:
    nodes: list[dict] = []
    cursor = None
    while len(nodes) < limit:
        data = connection_service.graphql(db, conn, query, {"first": PAGE_SIZE, "after": cursor})
        block = data.get(root) or {}
        nodes.extend(n for n in block.get("nodes") or [] if n)
        info = block.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        cursor = info.get("endCursor")
    return nodes[:limit]


def fetch_all(db: Session, conn: ShopifyConnection) -> tuple[list[dict], bool]:
    products = [normalize_product(conn, n) for n in _paginate(db, conn, PRODUCTS_QUERY, "products", MAX_ITEMS)]
    remaining = max(0, MAX_ITEMS - len(products))
    pages = [normalize_page(conn, n) for n in _paginate(db, conn, PAGES_QUERY, "pages", remaining)] if remaining else []
    items = products + pages
    return items, len(items) >= MAX_ITEMS


def fetch_one(db: Session, conn: ShopifyConnection, external_type: str, external_id: str) -> dict | None:
    """Current values of one product/page straight from Shopify."""
    if external_type == "product":
        node = (connection_service.graphql(db, conn, PRODUCT_QUERY, {"id": external_id}) or {}).get("product")
        return normalize_product(conn, node) if node else None
    node = (connection_service.graphql(db, conn, PAGE_QUERY, {"id": external_id}) or {}).get("page")
    return normalize_page(conn, node) if node else None


# ── storage ──────────────────────────────────────────────────────────────────

def apply_item(db: Session, workspace: Workspace, domain: Domain, item: dict, now: datetime) -> SitePage:
    """Upsert one Shopify item onto site_pages (matched by GID, then by URL)."""
    page = db.execute(
        select(SitePage).where(SitePage.domain_id == domain.id, SitePage.external_id == item["external_id"])
    ).scalar_one_or_none()
    new_key = page_history_service.url_key(item["url"])
    if page is None:
        page = page_history_service.get_or_create_page(db, workspace, item["url"], title=item["title"])
    elif page.url_key != new_key:
        # Handle changed in Shopify: move the row to the new URL. A plain
        # sitemap copy of the new URL (not linked to Shopify) is merged away.
        clash = db.execute(
            select(SitePage).where(SitePage.workspace_id == workspace.id, SitePage.url_key == new_key)
        ).scalar_one_or_none()
        if clash is not None and clash.id != page.id and clash.external_id is None:
            db.delete(clash)
            db.flush()
            clash = None
        if clash is None:
            page.url_key = new_key

    page.domain_id = domain.id
    page.url = item["url"]
    page.title = (item["title"] or "")[:512]
    page.discovery_source = "shopify"
    page.discovered_at = page.discovered_at or now
    page.page_type = "product" if item["external_type"] == "product" else "content"
    page.external_source = "shopify"
    page.external_type = item["external_type"]
    page.external_id = item["external_id"]
    page.external_handle = item["handle"]
    page.external_updated_at = item["updated_at"]
    page.external_meta = item["meta"] or None
    page.seo_title = item["seo_title"]
    page.seo_description = item["seo_description"]
    page.published = item["published"]
    page.content_html = json.dumps(editor_payload(item))
    page.content_fetched_at = now
    page.sitemap_lastmod = item["updated_at"]
    if not item["published"]:
        page.indexable = False
    if page.word_count is None:
        page.word_count = word_count(item["body_html"]) or None
    return page


def editor_payload(item: dict) -> dict:
    """The cached editor content (same JSON shape Page Discovery stores). An
    empty Shopify SEO title/description means the storefront falls back to the
    product/page title, so the editor shows that effective value."""
    body = item["body_html"][: page_discovery_service.CONTENT_HTML_CAP]
    return {
        "title": item["seo_title"] or item["title"],
        "meta": item["seo_description"] or "",
        "body_html": body,
        "word_count": word_count(body) or None,
        "http_status": 200 if item["published"] else None,
        "url": item["url"],
    }


def refresh_page(db: Session, domain: Domain, page: SitePage) -> dict | None:
    """Re-read one linked page from Shopify and update the stored row."""
    conn = connection_service.get_active(db, domain)
    item = fetch_one(db, conn, page.external_type or "product", page.external_id or "")
    if item is None:
        page.http_status = 410
        page.published = False
        db.commit()
        return None
    workspace = db.get(Workspace, domain.workspace_id)
    apply_item(db, workspace, domain, item, datetime.now(timezone.utc))
    db.commit()
    return item


def _mark_removed(db: Session, domain: Domain, seen: set[str]) -> int:
    rows = db.execute(
        select(SitePage).where(
            SitePage.domain_id == domain.id,
            SitePage.external_source == "shopify",
            SitePage.external_id.isnot(None),
        )
    ).scalars().all()
    gone = 0
    for p in rows:
        if p.external_id not in seen and p.http_status != 410:
            p.http_status = 410
            p.published = False
            p.indexable = False
            gone += 1
    return gone


# ── orchestrator ─────────────────────────────────────────────────────────────

def run_shopify_sync(workspace_id, domain_id, *, session_factory=None) -> None:
    """Background worker: never raises."""
    db = (session_factory or SessionLocal)()
    run = None
    conn = None
    try:
        workspace = db.get(Workspace, workspace_id)
        domain = db.get(Domain, domain_id)
        if workspace is None or domain is None or domain.workspace_id != workspace.id:
            return
        conn = connection_service.get_active(db, domain)

        db.execute(update(PageDiscoveryRun).where(
            PageDiscoveryRun.domain_id == domain.id, PageDiscoveryRun.status == "running"
        ).values(status="failed", error="superseded", finished_at=datetime.now(timezone.utc)))
        prev = db.execute(
            select(PageDiscoveryRun.pages_found)
            .where(PageDiscoveryRun.domain_id == domain.id, PageDiscoveryRun.status == "completed",
                   PageDiscoveryRun.source == "shopify")
            .order_by(PageDiscoveryRun.started_at.desc()).limit(1)
        ).scalar_one_or_none()
        run = PageDiscoveryRun(workspace_id=workspace.id, domain_id=domain.id, source="shopify",
                               sitemap_url=f"https://{conn.shop}", status="running", prev_total=prev)
        db.add(run)
        db.commit()

        items, truncated = fetch_all(db, conn)
        now = datetime.now(timezone.utc)
        for i, item in enumerate(items):
            apply_item(db, workspace, domain, item, now)
            if i % 200 == 199:
                db.commit()
        _mark_removed(db, domain, {it["external_id"] for it in items})
        run.pages_found = len(items)
        run.truncated = truncated
        conn.last_sync_at = now
        conn.last_sync_error = None
        db.commit()

        # Fetch + score the live storefront pages (published only).
        types = {it["url"]: ("product" if it["external_type"] == "product" else "content") for it in items}
        worklist = [{"loc": it["url"]} for it in items if it["published"]]
        enriched = 0
        win = page_discovery_service.ENRICH_WINDOW
        status = "completed"
        for i in range(0, len(worklist), win):
            db.refresh(run, ["status"])
            if run.status == "cancelled":
                run.pages_enriched = enriched
                run.finished_at = datetime.now(timezone.utc)
                db.commit()
                return
            window = worklist[i:i + win]
            results = page_discovery_service.enrich_batch(window)
            for r in results:
                r["page_type"] = types.get(r["loc"], r.get("page_type"))
            page_discovery_service._apply_enrichment(db, workspace, results)
            enriched += len(window)
            run.pages_enriched = enriched
            db.commit()
            blocked = sum(1 for r in results if r.get("blocked"))
            if len(window) >= win and blocked / len(window) >= page_discovery_service.BLOCK_STOP_RATE:
                status = "partial"
                run.error = ("Imported everything from Shopify, but the storefront started rate-limiting "
                             "our reader, so some pages aren't scored yet. Run again later to finish.")
                break

        db.refresh(run, ["status"])
        if run.status != "cancelled":
            run.status = status
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
    except Exception as exc:  # noqa: BLE001 — background worker must not crash
        log.exception("Shopify sync failed for domain %s", domain_id)
        try:
            db.rollback()
            if run is not None:
                run = db.get(PageDiscoveryRun, run.id)
                if run is not None:
                    run.status = "failed"
                    run.error = f"Shopify import failed: {str(exc)[:400]}"
                    run.finished_at = datetime.now(timezone.utc)
            if conn is not None:
                c = db.get(ShopifyConnection, conn.id)
                if c is not None:
                    c.last_sync_error = str(exc)[:500]
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
    finally:
        db.close()
