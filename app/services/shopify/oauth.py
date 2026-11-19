"""Shopify OAuth (authorization-code grant) for a non-embedded app.

Flow: the frontend asks ``/connect`` for an install URL, the merchant approves
on Shopify, Shopify redirects to our callback with ``code``, ``shop``,
``state``, ``timestamp`` and ``hmac``. The callback verifies the HMAC and the
signed state, then exchanges the code for an offline access token.

The only hosts contacted are ``<shop>.myshopify.com`` addresses that passed
``SHOP_RE``, so a user-supplied shop can never point requests elsewhere.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
import jwt

from app.core.config import settings
from app.services.shopify.errors import (
    InvalidShopError,
    ShopifyApiError,
    ShopifyAuthError,
    ShopifyNotConfiguredError,
)

SHOP_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$")
STATE_TYP = "shopify_state"
STATE_TTL_MINUTES = 10
# Reject callbacks whose Shopify timestamp is older than this (replay guard).
CALLBACK_MAX_AGE_SECONDS = 10 * 60
HTTP_TIMEOUT = 20.0


def normalize_shop(raw: str) -> str:
    """Accept "store", "store.myshopify.com" or an admin/store URL; return the
    canonical "<store>.myshopify.com" or raise InvalidShopError."""
    value = (raw or "").strip().lower()
    if "://" in value:
        value = urllib.parse.urlparse(value).netloc
    value = value.split("/")[0].split("?")[0].split(":")[0].strip(".")
    # admin.shopify.com/store/<name> style input
    if value == "admin.shopify.com":
        parts = urllib.parse.urlparse(raw.strip().lower()).path.strip("/").split("/")
        value = parts[1] if len(parts) >= 2 and parts[0] == "store" else ""
    if value and "." not in value:
        value = f"{value}.myshopify.com"
    if not SHOP_RE.match(value):
        raise InvalidShopError("Enter your store's .myshopify.com address, e.g. my-store.myshopify.com")
    return value


def safe_return_path(path: str | None, default: str = "/dashboard/settings") -> str:
    """Only allow same-site relative paths (blocks //evil.com and @host tricks)."""
    if not path or not path.startswith("/") or path.startswith("//") or "\\" in path:
        return default
    return path


# --- signed state -----------------------------------------------------------
def sign_state(*, workspace_id: str, domain_id: str, user_id: str, shop: str, return_path: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "typ": STATE_TYP,
        "wid": workspace_id,
        "did": domain_id,
        "uid": user_id,
        "shop": shop,
        "ret": safe_return_path(return_path),
        "nonce": secrets.token_urlsafe(12),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=STATE_TTL_MINUTES)).timestamp()),
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_state(token: str) -> dict | None:
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None
    if payload.get("typ") != STATE_TYP:
        return None
    return payload


def _require_configured() -> None:
    if not settings.shopify_configured:
        raise ShopifyNotConfiguredError("Shopify is not configured on the server.")


def build_install_url(shop: str, state: str) -> str:
    _require_configured()
    if not SHOP_RE.match(shop):
        raise InvalidShopError("Invalid shop")
    params = {
        "client_id": settings.SHOPIFY_API_KEY,
        "scope": settings.SHOPIFY_SCOPES,
        "redirect_uri": settings.SHOPIFY_REDIRECT_URI,
        "state": state,
    }
    return f"https://{shop}/admin/oauth/authorize?{urllib.parse.urlencode(params)}"


# --- HMAC -------------------------------------------------------------------
def verify_query_hmac(params: dict[str, str], *, now: float | None = None) -> bool:
    """Verify Shopify's callback HMAC: hex HMAC-SHA256 (app secret) over the
    remaining params sorted by key and joined as ``k=v&k=v``. Also rejects a
    stale ``timestamp``."""
    if not settings.SHOPIFY_API_SECRET:
        return False
    received = params.get("hmac") or ""
    if not received:
        return False
    message = "&".join(
        f"{k}={v}" for k, v in sorted(params.items()) if k not in ("hmac", "signature")
    )
    expected = hmac.new(
        settings.SHOPIFY_API_SECRET.encode(), message.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, received):
        return False
    try:
        ts = int(params.get("timestamp", "0"))
    except ValueError:
        return False
    current = time.time() if now is None else now
    return abs(current - ts) <= CALLBACK_MAX_AGE_SECONDS


# --- token exchange / refresh ----------------------------------------------
@dataclass
class ShopToken:
    access_token: str
    scope: str | None
    refresh_token: str | None = None
    expires_at: datetime | None = None


def _token_from(body: dict) -> ShopToken:
    token = body.get("access_token")
    if not token:
        raise ShopifyApiError("Shopify did not return an access token")
    expires_at = None
    if body.get("expires_in"):
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(body["expires_in"]))
    return ShopToken(
        access_token=token,
        scope=body.get("scope"),
        refresh_token=body.get("refresh_token"),
        expires_at=expires_at,
    )


def _post_token(shop: str, data: dict) -> dict:
    if not SHOP_RE.match(shop):
        raise InvalidShopError("Invalid shop")
    try:
        resp = httpx.post(
            f"https://{shop}/admin/oauth/access_token",
            data={"client_id": settings.SHOPIFY_API_KEY, "client_secret": settings.SHOPIFY_API_SECRET, **data},
            headers={"Accept": "application/json"},
            timeout=HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise ShopifyApiError("Could not reach Shopify") from exc
    if resp.status_code in (400, 401, 403):
        raise ShopifyAuthError("Shopify rejected the authorization")
    if resp.status_code >= 400:
        raise ShopifyApiError(f"Shopify token endpoint returned {resp.status_code}")
    return resp.json()


def exchange_code(shop: str, code: str) -> ShopToken:
    _require_configured()
    return _token_from(_post_token(shop, {"code": code}))


def refresh_token(shop: str, refresh: str) -> ShopToken:
    _require_configured()
    return _token_from(_post_token(shop, {"grant_type": "refresh_token", "refresh_token": refresh}))
