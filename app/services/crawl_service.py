"""Robots.txt & sitemap crawlability checker.

Reuses the SSRF-guarded fetch from audit_service. Parsing functions are pure so
they can be unit-tested without network.
"""

import gzip
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

from app.services import audit_service
from app.services.audit_service import (  # noqa: F401 (re-exported for callers)
    BlockedUrlError,
    FetchError,
    InvalidUrlError,
)

MAX_SITEMAP_BYTES = 5_000_000


@dataclass
class Check:
    id: str
    label: str
    status: str  # pass | warn | fail
    detail: str


@dataclass
class CrawlResult:
    url: str
    origin: str
    robots: dict[str, Any]
    sitemap: dict[str, Any]
    checks: list[Check] = field(default_factory=list)


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def parse_robots(text: str) -> dict[str, Any]:
    """Parse robots.txt into a summary. Pure function."""
    sitemaps: list[str] = []
    disallow_count = 0
    allow_count = 0
    star_disallow_root = False
    star_disallow_any = False

    current_agents: set[str] = set()
    seen_rule = False

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field_name, _, value = line.partition(":")
        field_name = field_name.strip().lower()
        value = value.strip()

        if field_name == "user-agent":
            if seen_rule:
                current_agents = set()
                seen_rule = False
            current_agents.add(value.lower())
        elif field_name == "sitemap":
            if value:
                sitemaps.append(value)
        elif field_name == "disallow":
            seen_rule = True
            disallow_count += 1
            if "*" in current_agents and value.strip():
                star_disallow_any = True
                if value.strip() == "/":
                    star_disallow_root = True
        elif field_name == "allow":
            seen_rule = True
            allow_count += 1

    return {
        "sitemaps": sitemaps,
        "disallow_count": disallow_count,
        "allow_count": allow_count,
        "blocks_all": star_disallow_root,
        "restricts_star": star_disallow_any,
    }


def parse_sitemap(content: str) -> dict[str, Any]:
    """Parse a sitemap or sitemap index. Pure function."""
    text = content.strip().lstrip("﻿")
    if not text:
        return {"valid": False, "type": None, "url_count": 0}
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return {"valid": False, "type": None, "url_count": 0}

    root_tag = root.tag.split("}")[-1].lower()

    def _count(child_local: str) -> int:
        return sum(
            1 for c in root if c.tag.split("}")[-1].lower() == child_local
        )

    if root_tag == "sitemapindex":
        return {"valid": True, "type": "index", "url_count": _count("sitemap")}
    if root_tag == "urlset":
        return {"valid": True, "type": "urlset", "url_count": _count("url")}
    return {"valid": True, "type": "unknown", "url_count": 0}


def parse_sitemap_urls(content: str) -> dict[str, Any]:
    """Return a sitemap's entries (not just a count). Pure function.

    {"type": "index"|"urlset"|"unknown"|None,
     "entries": [{"loc": str, "lastmod": str|None}, ...]}
    For an index, each entry's `loc` is a CHILD SITEMAP url (recurse on it);
    for a urlset, each `loc` is a PAGE url. Namespace-agnostic (tags vary)."""
    text = content.strip().lstrip("﻿")
    if not text:
        return {"type": None, "entries": []}
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return {"type": None, "entries": []}

    def _local(tag: str) -> str:
        return tag.split("}")[-1].lower()

    root_tag = _local(root.tag)
    child_tag = "sitemap" if root_tag == "sitemapindex" else "url" if root_tag == "urlset" else None
    if child_tag is None:
        return {"type": "unknown", "entries": []}

    entries: list[dict] = []
    for node in root:
        if _local(node.tag) != child_tag:
            continue
        loc = lastmod = None
        for leaf in node:
            name = _local(leaf.tag)
            if name == "loc" and leaf.text:
                loc = leaf.text.strip()
            elif name == "lastmod" and leaf.text:
                lastmod = leaf.text.strip()
        if loc:
            entries.append({"loc": loc, "lastmod": lastmod})
    return {"type": "index" if root_tag == "sitemapindex" else "urlset", "entries": entries}


def _maybe_gunzip(url: str, raw: bytes) -> bytes:
    """Decompress a gzipped sitemap (.xml.gz or gzip magic bytes)."""
    if url.lower().endswith(".gz") or raw[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(raw)
        except (OSError, EOFError):
            return raw
    return raw


def _fetch_text(url: str) -> tuple[int, str]:
    """Fetch a URL and return (status_code, text). Returns (0, '') on failure.
    Transparently decompresses gzipped (.xml.gz) sitemaps."""
    try:
        response, _final, _ms = audit_service.safe_fetch(url)
    except FetchError:
        return 0, ""
    content = _maybe_gunzip(url, response.content or b"")[:MAX_SITEMAP_BYTES]
    text = content.decode(response.encoding or "utf-8", errors="replace")
    return response.status_code, text


def run_check(url: str) -> CrawlResult:
    """Fetch robots.txt + first sitemap for a URL's origin and analyze."""
    normalized = audit_service.normalize_url(url)
    origin = _origin(normalized)

    # robots.txt
    robots_status, robots_text = _fetch_text(urljoin(origin + "/", "robots.txt"))
    robots_found = robots_status == 200 and bool(robots_text.strip())
    robots_info = (
        parse_robots(robots_text)
        if robots_found
        else {
            "sitemaps": [],
            "disallow_count": 0,
            "allow_count": 0,
            "blocks_all": False,
            "restricts_star": False,
        }
    )
    robots_info["found"] = robots_found
    robots_info["status_code"] = robots_status

    # sitemap: declared in robots, else /sitemap.xml
    candidates = list(robots_info["sitemaps"]) or [urljoin(origin + "/", "sitemap.xml")]
    sitemap_url = candidates[0]
    sm_status, sm_text = _fetch_text(sitemap_url)
    sm_parsed = parse_sitemap(sm_text) if sm_status == 200 else {"valid": False, "type": None, "url_count": 0}
    sitemap_info = {
        "url": sitemap_url,
        "status_code": sm_status,
        "found": sm_status == 200 and sm_parsed["valid"],
        "type": sm_parsed["type"],
        "url_count": sm_parsed["url_count"],
        "declared_in_robots": bool(robots_info["sitemaps"]),
    }

    checks: list[Check] = []

    if robots_found:
        checks.append(Check("robots_found", "robots.txt found", "pass", "A robots.txt file is present."))
    else:
        checks.append(Check("robots_found", "robots.txt", "warn", "No robots.txt found — everything is crawlable by default."))

    if robots_info["blocks_all"]:
        checks.append(Check("not_blocked", "Site crawlable", "fail", "robots.txt disallows all crawlers from the entire site (Disallow: /)."))
    else:
        checks.append(Check("not_blocked", "Site crawlable", "pass", "No site-wide crawl block for search engines."))

    if sitemap_info["declared_in_robots"]:
        checks.append(Check("sitemap_declared", "Sitemap declared", "pass", "A sitemap is referenced in robots.txt."))
    else:
        checks.append(Check("sitemap_declared", "Sitemap declared", "warn", "No Sitemap directive in robots.txt (checked /sitemap.xml)."))

    if sitemap_info["found"]:
        checks.append(Check("sitemap_found", "Sitemap found", "pass", f"A valid {sitemap_info['type']} sitemap was found."))
    else:
        checks.append(Check("sitemap_found", "Sitemap found", "fail", "No valid XML sitemap was found."))

    if sitemap_info["found"]:
        if sitemap_info["url_count"] > 0:
            noun = "sitemaps" if sitemap_info["type"] == "index" else "URLs"
            checks.append(Check("sitemap_urls", "Sitemap entries", "pass", f"Sitemap lists {sitemap_info['url_count']} {noun}."))
        else:
            checks.append(Check("sitemap_urls", "Sitemap entries", "warn", "The sitemap is empty."))

    return CrawlResult(
        url=normalized, origin=origin, robots=robots_info, sitemap=sitemap_info, checks=checks
    )
