"""Manual SERP scraper (DuckDuckGo HTML endpoint) -> serp_snapshots.

Why DuckDuckGo, not Google: Google now hard-requires JavaScript for its results
page (plain HTTP returns an "enable JavaScript" interstitial with zero result
links — not an IP block, a JS wall), so it can't be scraped without a headless
browser. DuckDuckGo's HTML endpoint (html.duckduckgo.com/html) serves real,
no-JS, keyless results and tolerates light scraping.

CAUTION: this is still low-volume manual scraping — DDG rate-limits by IP too
(from a local machine as well). `fetch_serp_html` raises ScrapeBlockedError the
moment we get a rate-limit/anomaly page or a non-200, so we never persist
garbage. Rankings are DuckDuckGo's, NOT Google's — use for "who competes on this
term", not exact Google positions (GSC gives your real Google rank).

`parse_serp_html` is pure so it can be unit-tested without network.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.keyword_analysis import Keyword, SerpSnapshot, SerpSnapshotItem
from app.services import dataforseo_client, keyword_analysis_service as kas
from app.services import data_env, keyword_scoring
from app.services.dataforseo_client import DataForSeoError

_ENDPOINT = "https://html.duckduckgo.com/html/"

# ISO country code -> DataForSEO location_name (Google organic geo target).
_LOCATION_NAMES = {
    "in": "India",
    "us": "United States",
    "gb": "United Kingdom",
    "ca": "Canada",
    "au": "Australia",
}


def _location_name(country: str) -> str:
    return _LOCATION_NAMES.get(country.lower(), "India")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    # No 'br': httpx can't decode Brotli without the brotli package, which would
    # give us a garbled body and zero parsed results.
    "Accept-Encoding": "gzip, deflate",
}

_TIMEOUT = 15.0
# Be gentle: a fixed pause before every request so a loop over keywords cannot
# hammer the endpoint. Callers doing multiple keywords should still add a gap.
_THROTTLE_SECONDS = 2.0

# DuckDuckGo region codes ("kl") differ from ISO country codes in places.
_KL_OVERRIDE = {"gb": "uk"}

# Non-organic hosts we never want as "results".
_SKIP_HOSTS = ("duckduckgo.com", "google.", "youtube.com", "gstatic.")


class ScrapeBlockedError(RuntimeError):
    """The endpoint rate-limited us / returned an anomaly or non-200 page."""


class ScrapeError(RuntimeError):
    """Network/transport failure talking to the SERP endpoint."""


@dataclass
class SerpRow:
    position: int
    url: str
    domain: str
    title: str | None


def _proxy_urls() -> list[str]:
    """Parse settings.SERP_PROXIES ('host:port:user:pass,...') into httpx proxy
    URLs. Malformed entries are skipped rather than crashing a scrape."""
    urls: list[str] = []
    for line in (settings.SERP_PROXIES or "").split(","):
        line = line.strip()
        if not line:
            continue
        parts = line.split(":")
        if len(parts) != 4:
            continue  # skip malformed entry
        host, port, user, pw = parts
        urls.append(f"http://{user}:{pw}@{host}:{port}")
    return urls

def _pick_proxy() -> str | None:
    urls = _proxy_urls()
    return random.choice(urls) if urls else None   # None = go direct


def _host(url: str) -> str:
    net = urlparse(url).netloc.lower()
    return net[4:] if net.startswith("www.") else net


def _unwrap(href: str) -> str:
    """DDG wraps results as /l/?uddg=<encoded real url>. Unwrap to the target."""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.path.startswith("/l/") or "uddg" in (parsed.query or ""):
        target = parse_qs(parsed.query).get("uddg")
        if target:
            return unquote(target[0])
    return href


def _kl(country: str, language: str) -> str:
    c = _KL_OVERRIDE.get(country.lower(), country.lower())
    return f"{c}-{language.lower()}"


def _looks_blocked(status: int, html: str) -> bool:
    if status != 200:
        return True
    low = html.lower()
    markers = (
        "if this error persists",
        "anomaly",
        "unusual traffic",
        "please try again in a few",
        "rate limit",
    )
    return any(m in low for m in markers)


def parse_serp_html(html: str, limit: int = 20) -> list[SerpRow]:
    """Extract organic results (position, url, domain, title) from a DuckDuckGo
    HTML results page. Pure — no network. One row per domain (first/best)."""
    soup = BeautifulSoup(html, "html.parser")
    rows: list[SerpRow] = []
    seen: set[str] = set()
    for a in soup.select("a.result__a"):
        href = a.get("href")
        if not href:
            continue
        real = _unwrap(href)
        if not real.startswith("http"):
            continue
        host = _host(real)
        if not host or any(s in host for s in _SKIP_HOSTS):
            continue
        if host in seen:  # one row per domain, first (best) position
            continue
        seen.add(host)
        rows.append(
            SerpRow(
                position=len(rows) + 1,
                url=real,
                domain=host,
                title=a.get_text(strip=True) or None,
            )
        )
        if len(rows) >= limit:
            break
    return rows


def fetch_serp_html(keyword: str, *, country: str = "in", language: str = "en") -> str:
    """Fetch a DuckDuckGo results page. Throttled. Routes through the proxies in
    SERP_PROXIES (rotated, one attempt each) when set, else goes direct.

    Raises ScrapeBlockedError if every attempt is rate-limited/blocked, or
    ScrapeError if every attempt fails at the transport level. Note: datacenter
    proxies are usually blocked/decoyed by search engines — use residential ones.
    """
    data = {"q": keyword, "kl": _kl(country, language)}
    proxies: list[str | None] = list(_proxy_urls())
    random.shuffle(proxies)
    if not proxies:
        proxies = [None]  # no proxy configured -> go direct

    last_blocked: ScrapeBlockedError | None = None
    last_error: ScrapeError | None = None
    for proxy in proxies:
        time.sleep(_THROTTLE_SECONDS)
        try:
            with httpx.Client(
                follow_redirects=True, timeout=_TIMEOUT, headers=_HEADERS, proxy=proxy
            ) as c:
                resp = c.post(_ENDPOINT, data=data)
        except httpx.HTTPError as exc:
            last_error = ScrapeError(f"Could not reach the SERP endpoint: {exc}")
            continue  # try the next proxy
        if _looks_blocked(resp.status_code, resp.text):
            last_blocked = ScrapeBlockedError(
                "DuckDuckGo rate-limited the request. Wait a few minutes, reduce "
                "volume, rotate to a residential proxy, or use a paid SERP API."
            )
            continue  # try the next proxy
        return resp.text  # success

    # Every attempt failed: prefer the "blocked" signal (actionable) over transport.
    raise last_blocked or last_error or ScrapeError("SERP fetch failed")


def scrape_keyword(db: Session, keyword: str, *, country: str = "in",
                   language: str = "en", num: int = 20, persist: bool = True,
                   env: str = data_env.LIVE) -> dict:
    """Scrape one keyword's SERP and (optionally) persist to serp_snapshots,
    tagged with the calling workspace's environment (see data_env). Returns {keyword, country, language, count, rows}. Propagates
    ScrapeBlockedError / ScrapeError to the caller."""
    html = fetch_serp_html(keyword, country=country, language=language)
    rows = parse_serp_html(html, limit=num)

    if persist and rows:
        kw = kas._upsert_keyword(db, keyword)
        snapshot = SerpSnapshot(
            keyword_id=kw.id, search_engine="duckduckgo", source="scrape",
            environment=env,
            device="desktop", country_code=country, language_code=language,
            depth=num, items_count=len(rows), status="ok",
        )
        snapshot.items = [
            SerpSnapshotItem(
                type="organic", rank_absolute=r.position, rank_group=r.position,
                url=r.url, domain=r.domain, title=r.title,
            )
            for r in rows
        ]
        db.add(snapshot)
        db.commit()

    return {
        "keyword": keyword,
        "country": country,
        "language": language,
        "count": len(rows),
        "rows": [
            {"position": r.position, "url": r.url, "domain": r.domain, "title": r.title}
            for r in rows
        ],
    }


def latest_snapshot(
    db: Session, keyword: str, *, country: str = "in", language: str = "en",
    env: str = data_env.LIVE,
) -> dict | None:
    """Return the most recent STORED SERP snapshot for a keyword — DB only, no
    provider call (zero cost). Used so viewing a keyword shows the cached SERP
    instead of re-charging DataForSEO. Returns None if nothing is stored yet."""
    normalized = keyword_scoring.normalize_keyword(keyword)
    kw = db.execute(
        select(Keyword).where(Keyword.normalized_keyword == normalized)
    ).scalar_one_or_none()
    if kw is None:
        return None
    snap = db.execute(
        select(SerpSnapshot)
        .where(
            SerpSnapshot.keyword_id == kw.id,
            SerpSnapshot.environment == env,
            SerpSnapshot.country_code == country,
            SerpSnapshot.language_code == language,
        )
        .order_by(SerpSnapshot.captured_at.desc())
        .limit(1)
    ).scalars().first()
    if snap is None:
        return None
    items = db.execute(
        select(SerpSnapshotItem)
        .where(SerpSnapshotItem.snapshot_id == snap.id)
        .order_by(SerpSnapshotItem.rank_absolute.asc())
    ).scalars().all()
    return {
        "keyword": keyword,
        "country": country,
        "language": language,
        "engine": snap.search_engine,
        "source": snap.source,
        "se_results_count": snap.se_results_count,
        "check_url": snap.check_url,
        "captured_at": snap.captured_at.isoformat() if snap.captured_at else None,
        "count": len(items),
        "rows": [
            {
                "position": it.rank_absolute,
                "rank_group": it.rank_group,
                "type": it.type,
                "url": it.url,
                "domain": it.domain,
                "title": it.title,
                "description": it.description,
                "breadcrumb": it.breadcrumb,
            }
            for it in items
        ],
    }


def analyze_keyword(
    db: Session,
    keyword: str,
    *,
    country: str = "in",
    language: str = "en",
    depth: int = 100,
    persist: bool = True,
    provider: str = "dataforseo",
    env: str = data_env.LIVE,
) -> dict:
    """SERP Analyzer: fetch a keyword's Google SERP via DataForSEO (default) and
    persist a rich snapshot to serp_snapshots / serp_snapshot_items. Falls back
    to the DuckDuckGo scrape when provider == 'scrape'.

    Returns {keyword, country, language, engine, source, se_results_count,
    check_url, count, rows[]} where each row has position, rank_group, url,
    domain, title, description, breadcrumb.
    """
    if provider == "dataforseo":
        if not dataforseo_client.is_configured():
            raise ScrapeError(
                "DataForSEO is not configured. Add DATAFORSEO_LOGIN / "
                "DATAFORSEO_PASSWORD, or use the scrape provider."
            )
        try:
            snap = dataforseo_client.serp_organic_snapshot(
                keyword, location_name=_location_name(country),
                language_code=language, depth=depth, env=env,
            )
        except DataForSeoError as exc:
            raise ScrapeError(str(exc)) from exc
        items = snap["items"]
        engine, source = "google", "dataforseo"
        meta = snap
    else:
        html = fetch_serp_html(keyword, country=country, language=language)
        scraped = parse_serp_html(html, limit=depth)
        items = [
            {
                "type": "organic", "rank_absolute": r.position,
                "rank_group": r.position, "domain": r.domain, "url": r.url,
                "title": r.title, "description": None, "breadcrumb": None,
                "raw": None,
            }
            for r in scraped
        ]
        engine, source = "duckduckgo", "scrape"
        meta = {"location_code": None, "se_results_count": None,
                "check_url": None, "raw": None}

    if persist and items:
        kw = kas._upsert_keyword(db, keyword)
        snapshot = SerpSnapshot(
            keyword_id=kw.id, search_engine=engine, source=source,
            environment=env, device="desktop", country_code=country, language_code=language,
            location_code=meta.get("location_code"), depth=depth,
            items_count=len(items), se_results_count=meta.get("se_results_count"),
            check_url=meta.get("check_url"), status="ok", raw=meta.get("raw"),
        )
        snapshot.items = [
            SerpSnapshotItem(
                type=it["type"], rank_absolute=it["rank_absolute"],
                rank_group=it.get("rank_group"), url=it["url"],
                domain=it["domain"], title=it.get("title"),
                description=it.get("description"), breadcrumb=it.get("breadcrumb"),
                raw=it.get("raw"),
            )
            for it in items
        ]
        db.add(snapshot)
        db.commit()
        db.refresh(snapshot)
        captured_at = snapshot.captured_at.isoformat() if snapshot.captured_at else None
    else:
        captured_at = None

    return {
        "keyword": keyword,
        "country": country,
        "language": language,
        "engine": engine,
        "source": source,
        "se_results_count": meta.get("se_results_count"),
        "check_url": meta.get("check_url"),
        "captured_at": captured_at,
        "count": len(items),
        "rows": [
            {
                "position": it["rank_absolute"],
                "rank_group": it.get("rank_group"),
                "url": it["url"],
                "domain": it["domain"],
                "title": it.get("title"),
                "description": it.get("description"),
                "breadcrumb": it.get("breadcrumb"),
            }
            for it in items
        ],
    }
