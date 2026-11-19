"""Shopify webhook verification, dedupe and dispatch."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.shopify import ShopifyWebhookEvent
from app.services.shopify import connection_service
from app.services.shopify.oauth import SHOP_RE

log = logging.getLogger(__name__)

COMPLIANCE_TOPICS = {"customers/data_request", "customers/redact", "shop/redact"}


def verify_hmac(raw_body: bytes, header_value: str | None) -> bool:
    """Base64 HMAC-SHA256 of the raw request body with the app secret."""
    if not settings.SHOPIFY_API_SECRET or not header_value:
        return False
    digest = hmac.new(settings.SHOPIFY_API_SECRET.encode(), raw_body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, header_value.strip())


def _first_delivery(db: Session, webhook_id: str, topic: str, shop: str) -> bool:
    """Record the webhook id; False if it was already processed."""
    if not webhook_id:
        return True  # cannot dedupe; process it
    stmt = (
        insert(ShopifyWebhookEvent)
        .values(webhook_id=webhook_id[:100], topic=topic[:100], shop=shop[:255])
        .on_conflict_do_nothing(index_elements=["webhook_id"])
        .returning(ShopifyWebhookEvent.webhook_id)
    )
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    return inserted is not None


def handle(db: Session, *, topic: str, shop: str, webhook_id: str, payload: dict) -> str:
    """Process one verified webhook. Returns a short outcome label."""
    topic = (topic or "").strip().lower()
    shop = (shop or "").strip().lower()
    if not SHOP_RE.match(shop):
        return "ignored"
    if not _first_delivery(db, webhook_id, topic, shop):
        return "duplicate"

    if topic == "app/uninstalled":
        connection_service.mark_revoked(db, shop)
        return "revoked"
    if topic == "shop/redact":
        connection_service.delete_shop_data(db, shop)
        return "redacted"
    if topic in ("customers/data_request", "customers/redact"):
        # No customer data is requested or stored by this app.
        return "no_customer_data"
    log.info("shopify webhook %s for %s not handled", topic, shop)
    return "ignored"
