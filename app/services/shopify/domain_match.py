"""Verify a Shopify store belongs to the workspace domain.

A store matches when the workspace domain equals the store's primary domain or
one of its connected custom domains (``www.`` ignored). The permanent
``*.myshopify.com`` host only matches if the workspace domain *is* that host.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services.shopify import client
from app.services.shopify.errors import ShopifyApiError

SHOP_QUERY_WITH_DOMAINS = """
query ShopIdentity {
  shop {
    name
    myshopifyDomain
    primaryDomain { host }
    domains { host }
  }
}
"""

SHOP_QUERY = """
query ShopIdentity {
  shop {
    name
    myshopifyDomain
    primaryDomain { host }
  }
}
"""


@dataclass
class ShopIdentity:
    name: str | None
    myshopify_domain: str
    primary_host: str | None
    hosts: list[str] = field(default_factory=list)


def _bare(host: str | None) -> str:
    value = (host or "").strip().lower().rstrip(".")
    return value[4:] if value.startswith("www.") else value


def fetch_identity(shop: str, token: str) -> ShopIdentity:
    try:
        data = client.graphql(shop, token, SHOP_QUERY_WITH_DOMAINS)
    except ShopifyApiError as exc:
        # `shop.domains` may not exist in every API version; fall back.
        if "domains" not in str(exc):
            raise
        data = client.graphql(shop, token, SHOP_QUERY)
    s = data.get("shop") or {}
    myshopify = (s.get("myshopifyDomain") or shop).lower()
    primary = ((s.get("primaryDomain") or {}).get("host") or "").lower() or None
    hosts: list[str] = []
    for h in [primary, *[(d or {}).get("host") for d in s.get("domains") or []], myshopify]:
        h = (h or "").strip().lower()
        if h and h not in hosts:
            hosts.append(h)
    return ShopIdentity(name=s.get("name"), myshopify_domain=myshopify, primary_host=primary, hosts=hosts)


def matches(workspace_domain: str, identity: ShopIdentity) -> bool:
    target = _bare(workspace_domain)
    if not target:
        return False
    for host in identity.hosts:
        bare = _bare(host)
        if bare.endswith(".myshopify.com"):
            if bare == target:
                return True
            continue
        if bare == target:
            return True
    return False
