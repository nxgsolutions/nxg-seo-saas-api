"""Persisted Shopify connections: save, read, token access, disconnect."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core import crypto
from app.core.config import settings
from app.models.domain import Domain
from app.models.shopify import ShopifyConnection
from app.services.shopify import client, oauth
from app.services.shopify.domain_match import ShopIdentity
from app.services.shopify.errors import ShopifyApiError, ShopifyNotConnectedError

log = logging.getLogger(__name__)

# Refresh expiring tokens this long before they expire.
REFRESH_MARGIN = timedelta(minutes=5)


def get(db: Session, domain: Domain) -> ShopifyConnection | None:
    return db.execute(
        select(ShopifyConnection).where(ShopifyConnection.domain_id == domain.id)
    ).scalar_one_or_none()


def get_active(db: Session, domain: Domain) -> ShopifyConnection:
    conn = get(db, domain)
    if conn is None or conn.status != "active" or not conn.access_token_enc:
        raise ShopifyNotConnectedError("No Shopify store is connected for this domain.")
    return conn


def save(
    db: Session,
    *,
    domain: Domain,
    user_id,
    identity: ShopIdentity,
    token: oauth.ShopToken,
) -> ShopifyConnection:
    """Create or replace the domain's connection (token stored encrypted)."""
    conn = get(db, domain)
    if conn is None:
        conn = ShopifyConnection(workspace_id=domain.workspace_id, domain_id=domain.id)
        db.add(conn)
    conn.shop = identity.myshopify_domain
    conn.shop_name = identity.name
    conn.primary_host = identity.primary_host
    conn.shop_hosts = identity.hosts
    conn.access_token_enc = crypto.encrypt(token.access_token)
    conn.refresh_token_enc = crypto.encrypt(token.refresh_token) if token.refresh_token else None
    conn.token_expires_at = token.expires_at
    conn.scopes = token.scope
    conn.api_version = settings.SHOPIFY_API_VERSION
    conn.status = "active"
    conn.installed_by = user_id
    conn.installed_at = datetime.now(timezone.utc)
    conn.uninstalled_at = None
    conn.last_sync_error = None
    db.commit()
    db.refresh(conn)
    return conn


def access_token(db: Session, conn: ShopifyConnection) -> str:
    """Decrypted access token, refreshing an expiring token when close to expiry."""
    if conn.status != "active" or not conn.access_token_enc:
        raise ShopifyNotConnectedError("No Shopify store is connected for this domain.")
    if (
        conn.token_expires_at is not None
        and conn.refresh_token_enc
        and conn.token_expires_at - REFRESH_MARGIN <= datetime.now(timezone.utc)
    ):
        fresh = oauth.refresh_token(conn.shop, crypto.decrypt(conn.refresh_token_enc))
        conn.access_token_enc = crypto.encrypt(fresh.access_token)
        if fresh.refresh_token:
            conn.refresh_token_enc = crypto.encrypt(fresh.refresh_token)
        conn.token_expires_at = fresh.expires_at
        db.commit()
        return fresh.access_token
    return crypto.decrypt(conn.access_token_enc)


def graphql(db: Session, conn: ShopifyConnection, query: str, variables: dict | None = None) -> dict:
    return client.graphql(conn.shop, access_token(db, conn), query, variables, api_version=conn.api_version)


def _revoke_remote(shop: str, token: str) -> None:
    """Best-effort uninstall so the token stops working on Shopify's side."""
    try:
        httpx.delete(
            f"https://{shop}/admin/api_permissions/current.json",
            headers={"X-Shopify-Access-Token": token},
            timeout=10.0,
        )
    except httpx.HTTPError:
        log.info("shopify revoke failed for %s (ignored)", shop)


def disconnect(db: Session, domain: Domain) -> bool:
    conn = get(db, domain)
    if conn is None:
        return False
    if conn.status == "active" and conn.access_token_enc and oauth.SHOP_RE.match(conn.shop):
        try:
            _revoke_remote(conn.shop, crypto.decrypt(conn.access_token_enc))
        except crypto.DecryptionError:
            pass
    db.delete(conn)
    db.commit()
    return True


def mark_revoked(db: Session, shop: str) -> int:
    """App uninstalled on Shopify: keep the row for history, wipe the token."""
    rows = list(db.execute(select(ShopifyConnection).where(ShopifyConnection.shop == shop)).scalars())
    now = datetime.now(timezone.utc)
    for conn in rows:
        conn.status = "revoked"
        conn.access_token_enc = None
        conn.refresh_token_enc = None
        conn.token_expires_at = None
        conn.uninstalled_at = now
    db.commit()
    return len(rows)


def delete_shop_data(db: Session, shop: str) -> int:
    """shop/redact: remove everything we hold for the shop."""
    result = db.execute(delete(ShopifyConnection).where(ShopifyConnection.shop == shop))
    db.commit()
    return result.rowcount or 0


WEBHOOK_CREATE = """
mutation WebhookCreate($topic: WebhookSubscriptionTopic!, $sub: WebhookSubscriptionInput!) {
  webhookSubscriptionCreate(topic: $topic, webhookSubscription: $sub) {
    webhookSubscription { id }
    userErrors { field message }
  }
}
"""

WEBHOOK_TOPICS = ("APP_UNINSTALLED",)


def subscribe_webhooks(db: Session, conn: ShopifyConnection) -> list[str]:
    """Subscribe the per-shop webhooks. Skipped unless an HTTPS base URL is set
    (Shopify cannot deliver to localhost). Compliance webhooks are configured in
    the app settings on Shopify, not here."""
    if not settings.shopify_webhooks_enabled:
        log.info("shopify webhooks skipped: SHOPIFY_WEBHOOK_BASE_URL is not https")
        return []
    uri = settings.SHOPIFY_WEBHOOK_BASE_URL.rstrip("/") + f"{settings.API_V1_PREFIX}/integrations/shopify/webhooks"
    created: list[str] = []
    for topic in WEBHOOK_TOPICS:
        try:
            data = graphql(db, conn, WEBHOOK_CREATE, {"topic": topic, "sub": {"uri": uri, "format": "JSON"}})
            result = data.get("webhookSubscriptionCreate") or {}
            errors = result.get("userErrors") or []
            if errors and not any("taken" in str(e.get("message", "")).lower() for e in errors):
                log.warning("shopify webhook %s not created: %s", topic, errors)
                continue
            created.append(topic)
        except ShopifyApiError as exc:
            log.warning("shopify webhook %s failed: %s", topic, exc)
    return created
