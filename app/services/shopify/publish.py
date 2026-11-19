"""Publish SEO/content edits from the editor back to Shopify, with undo.

Rules:
  * Conflict check: if the item changed in Shopify after the editor loaded it
    (``updatedAt`` newer than ``expected_updated_at``) the publish is refused
    with both versions, unless ``force``.
  * Only fields that actually differ from Shopify's current values are sent,
    so an untouched body is never rewritten.
  * Body HTML is sanitised server-side (``html.sanitize``).
  * Changing the URL handle always leaves a 301 from the old URL.
  * Every attempt is written to ``shopify_push_log`` (before/after) and a
    successful publish is recorded in Page History.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.domain import Domain
from app.models.shopify import ShopifyConnection, ShopifyPushLog
from app.models.site_page import SitePage
from app.models.workspace import Workspace
from app.services import content_score, page_history_service
from app.services.shopify import client, connection_service, sync
from app.services.shopify.errors import ShopifyApiError, ShopifyError, ShopifyUserError
from app.services.shopify.html import sanitize

log = logging.getLogger(__name__)

FIELDS = ("title", "body_html", "seo_title", "seo_description", "handle")


class NotShopifyPageError(ShopifyError):
    """The page is not linked to a Shopify product/page."""


class ShopifyItemGoneError(ShopifyError):
    """The product/page no longer exists in Shopify."""


class ShopifyConflictError(ShopifyError):
    """The item changed in Shopify after the editor loaded it."""

    def __init__(self, current: dict) -> None:
        self.current = current
        super().__init__("This item was changed in Shopify after you opened it.")


@dataclass
class PublishInput:
    title: str | None = None
    body_html: str | None = None
    seo_title: str | None = None
    seo_description: str | None = None
    handle: str | None = None
    image_alts: dict[str, str] = field(default_factory=dict)
    expected_updated_at: datetime | None = None
    force: bool = False


# ── reading ─────────────────────────────────────────────────────────────────

def _linked(page: SitePage) -> None:
    if page.external_source != "shopify" or not page.external_id:
        raise NotShopifyPageError("This page isn't linked to a Shopify product or page.")


def editor_fields(item: dict) -> dict:
    """The editable values as the editor shows them (raw Shopify values: an
    empty SEO title means 'falls back to the title' on the storefront)."""
    return {
        "external_type": item["external_type"],
        "external_id": item["external_id"],
        "title": item["title"],
        "body_html": item["body_html"],
        "seo_title": item["seo_title"] or "",
        "seo_description": item["seo_description"] or "",
        "handle": item["handle"],
        "url": item["url"],
        "published": item["published"],
        "updated_at": item["updated_at"],
        "images": (item.get("meta") or {}).get("images") or [],
    }


def load_live(db: Session, domain: Domain, page: SitePage) -> dict:
    """Current values straight from Shopify (also refreshes the stored row)."""
    _linked(page)
    conn = connection_service.get_active(db, domain)
    item = sync.fetch_one(db, conn, page.external_type or "product", page.external_id)
    if item is None:
        page.http_status = 410
        page.published = False
        db.commit()
        raise ShopifyItemGoneError("This item no longer exists in Shopify.")
    workspace = db.get(Workspace, domain.workspace_id)
    sync.apply_item(db, workspace, domain, item, datetime.now(timezone.utc))
    db.commit()
    return item


# ── mutations ───────────────────────────────────────────────────────────────

PRODUCT_UPDATE = """
mutation ProductUpdate($product: ProductUpdateInput!) {
  productUpdate(product: $product) {
    product { id handle updatedAt }
    userErrors { field message }
  }
}
"""

PAGE_UPDATE = """
mutation PageUpdate($id: ID!, $page: PageUpdateInput!) {
  pageUpdate(id: $id, page: $page) {
    page { id handle updatedAt }
    userErrors { field message }
  }
}
"""

METAFIELDS_DELETE = """
mutation MetafieldsDelete($metafields: [MetafieldIdentifierInput!]!) {
  metafieldsDelete(metafields: $metafields) {
    deletedMetafields { key }
    userErrors { field message }
  }
}
"""

REDIRECT_CREATE = """
mutation RedirectCreate($urlRedirect: UrlRedirectInput!) {
  urlRedirectCreate(urlRedirect: $urlRedirect) {
    urlRedirect { id }
    userErrors { field message }
  }
}
"""

FILE_UPDATE = """
mutation FileUpdate($files: [FileUpdateInput!]!) {
  fileUpdate(files: $files) {
    files { id alt }
    userErrors { field message }
  }
}
"""


def _clean_handle(value: str) -> str:
    import re

    h = re.sub(r"[^a-z0-9-]+", "-", (value or "").strip().lower()).strip("-")
    return re.sub(r"-{2,}", "-", h)


def changes_for(live: dict, data: PublishInput) -> dict:
    """Fields whose new value differs from Shopify's current value."""
    proposed = {
        "title": data.title,
        "body_html": sanitize(data.body_html) if data.body_html is not None else None,
        "seo_title": data.seo_title,
        "seo_description": data.seo_description,
        "handle": _clean_handle(data.handle) if data.handle is not None else None,
    }
    current = editor_fields(live)
    out = {}
    for key, value in proposed.items():
        if value is None:
            continue
        if key in ("seo_title", "seo_description"):
            value = " ".join(value.split())  # SEO fields are single-line
        elif key != "body_html":
            value = value.strip()
        if key in ("title", "handle") and not value:
            continue  # never blank a title / handle
        if value != (current.get(key) or ""):
            out[key] = value
    return out


def alt_changes(live: dict, image_alts: dict[str, str]) -> dict[str, str]:
    current = {img["id"]: img.get("alt") or "" for img in editor_fields(live)["images"]}
    return {mid: alt.strip() for mid, alt in (image_alts or {}).items()
            if mid in current and alt.strip() != current[mid]}


def build_product_input(external_id: str, changes: dict) -> dict:
    payload: dict = {"id": external_id}
    if "title" in changes:
        payload["title"] = changes["title"]
    if "body_html" in changes:
        payload["descriptionHtml"] = changes["body_html"]
    if "handle" in changes:
        payload["handle"] = changes["handle"]
        payload["redirectNewHandle"] = True
    seo = {}
    if "seo_title" in changes:
        seo["title"] = changes["seo_title"]
    if "seo_description" in changes:
        seo["description"] = changes["seo_description"]
    if seo:
        payload["seo"] = seo
    return payload


def build_page_input(changes: dict) -> tuple[dict, list[dict]]:
    """(PageUpdateInput, metafields to delete). Shopify can't store an empty
    metafield, so clearing an SEO field deletes its metafield instead."""
    payload: dict = {}
    deletes: list[dict] = []
    if "title" in changes:
        payload["title"] = changes["title"]
    if "body_html" in changes:
        payload["body"] = changes["body_html"]
    if "handle" in changes:
        payload["handle"] = changes["handle"]
    metafields = []
    for key, tag in (("seo_title", "title_tag"), ("seo_description", "description_tag")):
        if key not in changes:
            continue
        if changes[key]:
            metafields.append({"namespace": "global", "key": tag, "type": "single_line_text_field",
                               "value": " ".join(changes[key].split())})
        else:
            deletes.append({"namespace": "global", "key": tag})
    if metafields:
        payload["metafields"] = metafields
    return payload, deletes


def _ensure_redirect(db: Session, conn: ShopifyConnection, kind: str, old: str, new: str) -> bool:
    prefix = "/products/" if kind == "product" else "/pages/"
    data = connection_service.graphql(db, conn, REDIRECT_CREATE,
                                      {"urlRedirect": {"path": prefix + old, "target": prefix + new}})
    errors = (data.get("urlRedirectCreate") or {}).get("userErrors") or []
    if errors and not any("already" in str(e.get("message", "")).lower() or "taken" in str(e.get("message", "")).lower()
                          for e in errors):
        raise ShopifyUserError(errors)
    return True


def _apply(db: Session, conn: ShopifyConnection, live: dict, changes: dict, alts: dict) -> tuple[bool, list[str]]:
    """Send the mutations. Returns (redirect_created, warnings)."""
    warnings: list[str] = []
    kind = live["external_type"]
    redirect = False
    if changes:
        if kind == "product":
            data = connection_service.graphql(db, conn, PRODUCT_UPDATE,
                                              {"product": build_product_input(live["external_id"], changes)})
            client.raise_user_errors(data.get("productUpdate"))
            redirect = "handle" in changes  # redirectNewHandle
        else:
            page_input, deletes = build_page_input(changes)
            if page_input:
                data = connection_service.graphql(db, conn, PAGE_UPDATE, {"id": live["external_id"], "page": page_input})
                client.raise_user_errors(data.get("pageUpdate"))
            if deletes:
                data = connection_service.graphql(db, conn, METAFIELDS_DELETE, {
                    "metafields": [{"ownerId": live["external_id"], **d} for d in deletes]})
                client.raise_user_errors(data.get("metafieldsDelete"))
            if "handle" in changes:
                redirect = _ensure_redirect(db, conn, kind, live["handle"], changes["handle"])
    if alts:
        try:
            data = connection_service.graphql(db, conn, FILE_UPDATE,
                                              {"files": [{"id": mid, "alt": alt} for mid, alt in alts.items()]})
            client.raise_user_errors(data.get("fileUpdate"))
        except ShopifyApiError as exc:
            warnings.append(f"Image alt text wasn't saved: {exc}")
    return redirect, warnings


def _snapshot(live: dict) -> dict:
    f = editor_fields(live)
    return {
        "title": f["title"], "body_html": f["body_html"], "seo_title": f["seo_title"],
        "seo_description": f["seo_description"], "handle": f["handle"],
        "image_alts": {img["id"]: img.get("alt") or "" for img in f["images"]},
        "updated_at": f["updated_at"].isoformat() if f["updated_at"] else None,
    }


def _log(db: Session, page: SitePage, user_id, action: str, before: dict, after: dict, status: str,
         error: str | None = None, redirect: bool = False) -> ShopifyPushLog:
    entry = ShopifyPushLog(workspace_id=page.workspace_id, page_id=page.id, user_id=user_id,
                           external_id=page.external_id, action=action, before=before, after=after,
                           status=status, error=error, redirect_created=redirect)
    db.add(entry)
    db.commit()
    return entry


def push(db: Session, domain: Domain, page: SitePage, data: PublishInput, *, user_id, action: str = "publish") -> dict:
    """Publish edits. Returns {status, changed, redirect_created, warnings, item, log_id}."""
    _linked(page)
    conn = connection_service.get_active(db, domain)
    live = sync.fetch_one(db, conn, page.external_type or "product", page.external_id)
    if live is None:
        raise ShopifyItemGoneError("This item no longer exists in Shopify.")

    if (not data.force and data.expected_updated_at is not None and live["updated_at"] is not None
            and live["updated_at"] > data.expected_updated_at):
        _log(db, page, user_id, action, _snapshot(live), {}, "conflict", "changed in Shopify")
        raise ShopifyConflictError(editor_fields(live))

    changes = changes_for(live, data)
    alts = alt_changes(live, data.image_alts)
    if not changes and not alts:
        return {"status": "unchanged", "changed": [], "redirect_created": False, "warnings": [],
                "item": editor_fields(live), "log_id": None}

    before = _snapshot(live)
    try:
        redirect, warnings = _apply(db, conn, live, changes, alts)
    except ShopifyApiError as exc:
        _log(db, page, user_id, action, before, {**changes, "image_alts": alts}, "error", str(exc)[:1000])
        raise

    fresh = sync.fetch_one(db, conn, page.external_type or "product", page.external_id) or live
    workspace = db.get(Workspace, domain.workspace_id)
    sync.apply_item(db, workspace, domain, fresh, datetime.now(timezone.utc))
    # The saved editor draft is now live in Shopify, so Shopify is canonical
    # again; drop the draft so the editor reopens the published version.
    page.edited_content_html = None
    page.content_edited_at = None
    db.commit()
    entry = _log(db, page, user_id, action, before, _snapshot(fresh), "ok", redirect=redirect)

    # Page History: what changed and when, so later traffic can be compared.
    try:
        f = editor_fields(fresh)
        facts, _checks, sc = content_score.score_fields(
            f["seo_title"] or f["title"], f["seo_description"], f["body_html"], f["url"], "",
            page_type=page.page_type or "")
        page_history_service.safe_record(db, workspace, f["url"], extracted=facts,
                                         source="shopify", source_id=entry.id, content_score_value=sc)
    except Exception:  # noqa: BLE001 — history is a side effect
        db.rollback()

    return {"status": "published", "changed": sorted([*changes.keys(), *(["image_alts"] if alts else [])]),
            "redirect_created": redirect, "warnings": warnings, "item": editor_fields(fresh),
            "log_id": entry.id}


def history(db: Session, page: SitePage, limit: int = 50) -> list[ShopifyPushLog]:
    return list(db.execute(
        select(ShopifyPushLog).where(ShopifyPushLog.page_id == page.id)
        .order_by(ShopifyPushLog.created_at.desc()).limit(limit)
    ).scalars())


def revert(db: Session, domain: Domain, page: SitePage, log_id, *, user_id) -> dict:
    """Undo a successful publish by re-publishing its 'before' values."""
    entry = db.get(ShopifyPushLog, log_id)
    if entry is None or entry.page_id != page.id or entry.status != "ok":
        raise LookupError("Nothing to undo for this entry.")
    b = entry.before or {}
    data = PublishInput(
        title=b.get("title"), body_html=b.get("body_html"), seo_title=b.get("seo_title"),
        seo_description=b.get("seo_description"), handle=b.get("handle"),
        image_alts=b.get("image_alts") or {}, force=True,
    )
    return push(db, domain, page, data, user_id=user_id, action="revert")
