"""Minimal Shopify GraphQL Admin API client.

Handles Shopify's cost-based throttling (``THROTTLED`` errors / HTTP 429),
retries transient 5xx responses, and maps 401/403 to ShopifyAuthError.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any

import httpx

from app.core.config import settings
from app.services.shopify.errors import (
    InvalidShopError,
    ShopifyApiError,
    ShopifyAuthError,
    ShopifyUserError,
)
from app.services.shopify.oauth import SHOP_RE

log = logging.getLogger(__name__)

HTTP_TIMEOUT = 30.0
MAX_ATTEMPTS = 5


def _sleep(seconds: float) -> None:  # patched in tests
    time.sleep(seconds)


def _throttle_wait(body: dict) -> float:
    """Seconds to wait until enough query cost has been restored."""
    cost = (body.get("extensions") or {}).get("cost") or {}
    status = cost.get("throttleStatus") or {}
    requested = float(cost.get("requestedQueryCost") or 50)
    available = float(status.get("currentlyAvailable") or 0)
    restore = float(status.get("restoreRate") or 50) or 50
    return max(1.0, min(30.0, (requested - available) / restore + 0.5))


def _is_throttled(body: dict) -> bool:
    return any(
        (e.get("extensions") or {}).get("code") == "THROTTLED" for e in body.get("errors") or []
    )


def graphql(
    shop: str,
    token: str,
    query: str,
    variables: dict[str, Any] | None = None,
    *,
    api_version: str | None = None,
) -> dict:
    """Run a GraphQL Admin query and return its ``data`` dict."""
    if not SHOP_RE.match(shop):
        raise InvalidShopError("Invalid shop")
    version = api_version or settings.SHOPIFY_API_VERSION
    url = f"https://{shop}/admin/api/{version}/graphql.json"
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    payload = {"query": query, "variables": variables or {}}

    last_error = "Shopify request failed"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = httpx.post(url, json=payload, headers=headers, timeout=HTTP_TIMEOUT)
        except httpx.HTTPError as exc:
            last_error = f"Could not reach Shopify ({exc.__class__.__name__})"
            _sleep(min(8.0, 2 ** attempt) + random.random())
            continue

        if resp.status_code in (401, 403):
            raise ShopifyAuthError("Shopify rejected the access token. Reconnect the store.")
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After") or 2)
            _sleep(min(30.0, retry_after))
            continue
        if resp.status_code >= 500:
            last_error = f"Shopify returned {resp.status_code}"
            _sleep(min(8.0, 2 ** attempt) + random.random())
            continue
        if resp.status_code >= 400:
            raise ShopifyApiError(f"Shopify returned {resp.status_code}")

        deprecated = resp.headers.get("X-Shopify-API-Deprecated-Reason")
        if deprecated:
            log.warning("shopify api deprecation: %s", deprecated)

        body = resp.json()
        if _is_throttled(body):
            _sleep(_throttle_wait(body))
            continue
        if body.get("errors"):
            msg = "; ".join(str(e.get("message", e)) for e in body["errors"])
            raise ShopifyApiError(msg or "Shopify GraphQL error")
        return body.get("data") or {}

    raise ShopifyApiError(last_error)


def raise_user_errors(result: dict | None) -> None:
    """Raise ShopifyUserError if a mutation payload carries userErrors."""
    errors = (result or {}).get("userErrors") or []
    if errors:
        raise ShopifyUserError(errors)
