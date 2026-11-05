"""Shared proxy-pool parsing for outbound website fetches.

Both the SERP scraper (`serp_scrape_service`) and the page fetcher
(`audit_service`) read a Webshare-style `host:port:user:pass,...` setting and
rotate through the resulting httpx proxy URLs. This is the one parser they share
so the format never drifts. Residential proxies are the point — datacenter IPs
are blocked by the same bot protection we're trying to get past.
"""

from __future__ import annotations

import random


def proxy_urls(raw: str | None) -> list[str]:
    """Parse a `host:port:user:pass,...` CSV (or ready-made `http(s)://…` URLs)
    into httpx proxy URLs. Malformed entries are skipped, never fatal, so one bad
    line can't break a whole fetch."""
    urls: list[str] = []
    for line in (raw or "").split(","):
        line = line.strip()
        if not line:
            continue
        if line.startswith(("http://", "https://")):
            urls.append(line)
            continue
        parts = line.split(":")
        if len(parts) != 4:
            continue  # skip malformed entry
        host, port, user, pw = parts
        urls.append(f"http://{user}:{pw}@{host}:{port}")
    return urls


def shuffled(raw: str | None) -> list[str]:
    """`proxy_urls` in random order, so repeated fetches don't always hit the
    same endpoint first. Empty list when nothing is configured (fetch direct)."""
    urls = proxy_urls(raw)
    random.shuffle(urls)
    return urls
