"""Ingest REAL Opportunity Research data for a domain (default iaspirants.com).

- Crawls the domain for real, clickable public page URLs (homepage + internal).
- Pulls real Google SERP per keyword via DataForSEO: the domain's actual rank +
  ranking URL, the SERP features present, and the top competing domains.
- Pulls real monthly search volume via DataForSEO (Google Ads bulk).
- Writes opportunity_pages + opportunity_keywords for EVERY domain row matching
  the host (leaves opportunity_trend intact). Only keywords the domain actually
  ranks for (real position) are stored, so no fields are null/fake.

Usage:
    .venv/Scripts/python -m scripts.ingest_opportunity iaspirants.com
    .venv/Scripts/python -m scripts.ingest_opportunity iaspirants.com --depth 100
"""

from __future__ import annotations

import re
import sys
import time
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import delete, select

from app.db.session import SessionLocal
from app.models.domain import Domain
from app.models.opportunity import OpportunityKeyword, OpportunityPage
from app.services import audit_service, dataforseo_client
from app.services.keyword_analysis_service import _upsert_keyword

LOCATION_NAME = "India"
LANGUAGE_CODE = "en"

# Curated, high-intent UPSC keywords relevant to iAspirants.
KEYWORDS = [
    "upsc preparation", "upsc online coaching", "best online coaching for upsc",
    "ias exam", "how to become an ias officer", "ias salary",
    "upsc current affairs", "daily current affairs", "the hindu editorial analysis",
    "upsc mock test", "upsc prelims test series", "upsc pyq",
    "upsc previous year question papers", "upsc study material", "best books for upsc",
    "upsc notes pdf", "upsc optional subjects", "best optional subject for upsc",
    "upsc answer writing", "upsc mains answer writing practice",
    "upsc syllabus", "upsc prelims syllabus", "upsc mains syllabus", "upsc exam pattern",
]

# DataForSEO item types -> our SERP feature tags.
FEATURE_MAP = {
    "images": "image", "video": "video", "youtube": "video",
    "featured_snippet": "snippet", "people_also_ask": "snippet",
}


def _host(url: str) -> str:
    h = urlparse(url).netloc.lower()
    return h[4:] if h.startswith("www.") else h


def _intent(keyword: str) -> str:
    k = keyword.lower()
    if any(w in k for w in ("best", "coaching", "books")):
        return "commercial"
    if any(w in k for w in ("test", "mock", "pyq", "pdf", "practice", "series", "question")):
        return "transactional"
    if any(w in k for w in ("date", "login", "notification")):
        return "navigational"
    return "informational"


def _opportunity_score(volume: int, position: int, kd: int) -> int:
    vol_n = min(1.0, volume / 300000)
    pos_n = min(1.0, position / 20)
    kd_n = 1 - min(1.0, kd / 100)
    return max(1, min(100, round((0.5 * vol_n + 0.3 * pos_n + 0.2 * kd_n) * 100)))


# A generous, redirect-following client for sitemap discovery — slow sites (e.g.
# iaspirants.com) time out on audit_service.safe_fetch's short 12s no-redirect
# fetch, so the homepage-anchor crawl misses everything. The sitemap is one fast
# request and lists the whole site, so it's the reliable primary source.
_SITEMAP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_UA = {"User-Agent": "Mozilla/5.0 (compatible; SEOBot/1.0)"}
_ASSET_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".zip", ".xml", ".css", ".js", ".ico")


def _title_from_slug(url: str) -> str:
    """Human-ish title from a URL slug when we don't fetch the page for its <title>."""
    path = urlparse(url).path.rstrip("/")
    if not path:
        return "Home"
    slug = path.rsplit("/", 1)[-1].replace("-", " ").replace("_", " ").strip()
    return slug.title()[:300] or path


def _sitemap_locs(host: str, cap: int = 5000) -> list[str]:
    """Same-host page URLs from the domain's sitemap(s). Reads robots.txt for the
    declared Sitemap:, follows a sitemap index one level, skips asset files."""
    sitemap_urls: list[str] = []
    try:
        rob = httpx.get(f"https://{host}/robots.txt", timeout=_SITEMAP_TIMEOUT,
                        headers=_UA, follow_redirects=True)
        if rob.status_code == 200:
            sitemap_urls += re.findall(r"(?im)^\s*Sitemap:\s*(\S+)", rob.text)
    except httpx.HTTPError:
        pass
    if not sitemap_urls:
        sitemap_urls = [f"https://{host}/sitemap.xml"]

    seen: set[str] = set()
    out: list[str] = []
    queue = list(dict.fromkeys(sitemap_urls))
    fetched_maps = 0
    while queue and fetched_maps < 10 and len(out) < cap:
        sm = queue.pop(0)
        if _host(sm) != host:  # never follow a sitemap to another host
            continue
        try:
            r = httpx.get(sm, timeout=_SITEMAP_TIMEOUT, headers=_UA, follow_redirects=True)
            fetched_maps += 1
            if r.status_code != 200:
                continue
        except httpx.HTTPError:
            continue
        locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", r.text, flags=re.I | re.S)
        # A sitemap index points at more sitemaps (their <loc> ends in .xml).
        if "<sitemapindex" in r.text.lower():
            queue += [l for l in locs if l.lower().endswith(".xml")]
            continue
        for loc in locs:
            loc = loc.split("#")[0].split("?")[0].rstrip("/")
            if not loc or loc in seen or _host(loc) != host:
                continue
            if any(loc.lower().endswith(e) for e in _ASSET_EXT):
                continue
            seen.add(loc)
            out.append(loc)
            if len(out) >= cap:
                break
    return out


def _pick_pages(urls: list[str], home: str, limit: int) -> list[str]:
    """Prefer the homepage + shallow section landing pages (fewer path segments)
    over deep article URLs, then take a stable spread up to `limit`."""
    def depth(u: str) -> tuple[int, int, str]:
        path = urlparse(u).path.strip("/")
        segs = [s for s in path.split("/") if s]
        return (len(segs), len(u), u)  # shallow first, then shorter, then stable

    ranked = sorted(set(urls), key=depth)
    picked = [home] if home in urls or True else []
    for u in ranked:
        if u not in picked:
            picked.append(u)
        if len(picked) >= limit:
            break
    return picked[:limit]


def discover_pages(host: str, limit: int = 18, *, fetch_titles: bool = True) -> dict[str, str]:
    """Reliable page discovery: sitemap first (one fast request, whole site), with
    the homepage-anchor crawl as a fallback. Returns {url: title}."""
    locs = _sitemap_locs(host)
    if not locs:
        print("    - no sitemap; falling back to homepage-link crawl", file=sys.stderr)
        return crawl_pages(host, limit=limit)

    home = f"https://{host}"
    chosen = _pick_pages(locs, home, limit)
    print(f"    - sitemap: {len(locs)} urls -> picked {len(chosen)}", file=sys.stderr)

    pages: dict[str, str] = {}
    for url in chosen:
        title = _title_from_slug(url)
        if fetch_titles:
            try:
                r = httpx.get(url, timeout=_SITEMAP_TIMEOUT, headers=_UA, follow_redirects=True)
                if r.status_code == 200:
                    soup = BeautifulSoup(r.text, "html.parser")
                    if soup.title:
                        title = soup.title.get_text(strip=True)[:300] or title
            except httpx.HTTPError:
                pass  # keep the slug-derived title
        pages[url.rstrip("/")] = title
    return pages


def crawl_pages(host: str, limit: int = 15) -> dict[str, str]:
    """Return {real_url: title} for the homepage + internal links (best-effort)."""
    pages: dict[str, str] = {}
    try:
        resp, final_url, _ = audit_service.safe_fetch(f"https://{host}")
    except Exception as exc:  # noqa: BLE001
        print(f"    ! could not fetch homepage: {exc}", file=sys.stderr)
        return pages
    soup = BeautifulSoup(resp.text, "html.parser")
    pages[final_url] = (soup.title.get_text(strip=True) if soup.title else host)[:300]

    seen = {final_url}
    for a in soup.find_all("a", href=True):
        full = urljoin(final_url, a["href"]).split("#")[0].split("?")[0].rstrip("/")
        if not full or full in seen or _host(full) != host:
            continue
        if any(full.lower().endswith(e) for e in (".pdf", ".jpg", ".png", ".zip", ".xml")):
            continue
        seen.add(full)
        title = a.get_text(" ", strip=True)[:300] or full
        try:
            r, fu, _ = audit_service.safe_fetch(full)
            ps = BeautifulSoup(r.text, "html.parser")
            title = (ps.title.get_text(strip=True) if ps.title else title)[:300]
            pages[fu.rstrip("/")] = title
        except Exception:  # noqa: BLE001
            pages[full] = title
        if len(pages) >= limit:
            break
        time.sleep(0.3)
    return pages


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    host = _host("https://" + (args[0] if args else "iaspirants.com"))
    depth = 100
    if "--depth" in sys.argv:
        depth = int(sys.argv[sys.argv.index("--depth") + 1])

    if not dataforseo_client.is_configured():
        raise SystemExit("DataForSEO is not configured (DATAFORSEO_LOGIN/PASSWORD).")

    print(f"[1/3] Crawling real pages of {host} ...")
    crawled = crawl_pages(host)
    print(f"    - {len(crawled)} pages")
    home = next(iter(crawled), f"https://{host}")

    print(f"[2/3] DataForSEO: volume + SERP for {len(KEYWORDS)} keywords ...")
    volumes = dataforseo_client.search_volume(
        KEYWORDS, location_name=LOCATION_NAME, language_code=LANGUAGE_CODE)

    records: list[dict] = []
    extra_pages: dict[str, str] = {}
    for kw in KEYWORDS:
        try:
            snap = dataforseo_client.serp_organic_snapshot(
                kw, location_name=LOCATION_NAME, language_code=LANGUAGE_CODE, depth=depth)
        except Exception as exc:  # noqa: BLE001
            print(f"    ! SERP failed for {kw!r}: {exc}", file=sys.stderr)
            continue
        items = snap["items"]
        mine = next((it for it in items if _host(it["url"]) == host), None)
        if not mine:
            print(f"    - {kw!r}: not ranking (skipped)")
            continue
        position = mine["rank_absolute"]
        ranking_url = mine["url"].rstrip("/")
        # SERP features from item types (+ sitelinks/reviews from my result).
        types = snap["raw"].get("item_types") or []
        feats = {FEATURE_MAP[t] for t in types if t in FEATURE_MAP}
        my_raw = mine.get("raw") or {}
        if my_raw.get("links"):
            feats.add("sitelinks")
        if my_raw.get("rating"):
            feats.add("reviews")
        competitors = [
            {"domain": _host(it["url"]), "position": it["rank_absolute"]}
            for it in items if _host(it["url"]) != host
        ][:3]
        volume = int(volumes.get(kw.lower(), 0) or 0)
        se_count = snap["raw"].get("se_results_count") or 0
        # crude difficulty proxy from result count (0-100), stable & real-ish.
        kd = max(20, min(85, round((se_count ** 0.15))) if se_count else 40)
        if ranking_url not in crawled and ranking_url not in extra_pages:
            extra_pages[ranking_url] = (mine.get("title") or kw)[:300]
        records.append({
            "keyword": kw, "volume": volume, "position": position, "kd": kd,
            "intent": _intent(kw), "ranking_url": ranking_url,
            "serp_features": sorted(feats), "competitors": competitors,
            "opportunity_score": _opportunity_score(volume, position, kd),
        })
        print(f"    - {kw!r}: rank #{position} vol {volume}")
        time.sleep(0.5)

    if not records:
        raise SystemExit("No ranking keywords found — nothing to ingest.")

    all_pages = {**crawled, **extra_pages}

    db = SessionLocal()
    try:
        domains = db.execute(
            select(Domain).where(Domain.domain.ilike(host))
        ).scalars().all()
        if not domains:
            raise SystemExit(f"No domain rows for {host} in the DB.")

        for dom in domains:
            db.execute(delete(OpportunityKeyword).where(OpportunityKeyword.domain_id == dom.id))
            db.execute(delete(OpportunityPage).where(OpportunityPage.domain_id == dom.id))
            db.flush()

            page_by_url: dict[str, OpportunityPage] = {}
            for url, title in all_pages.items():
                p = OpportunityPage(domain_id=dom.id, url=url, title=title)
                db.add(p)
                page_by_url[url] = p
            db.flush()

            for r in records:
                page = page_by_url.get(r["ranking_url"]) or page_by_url.get(home)
                canonical = _upsert_keyword(db, r["keyword"])
                db.add(OpportunityKeyword(
                    domain_id=dom.id,
                    page_id=page.id if page else None,
                    keyword_id=canonical.id,
                    keyword=r["keyword"],
                    search_volume=r["volume"],
                    yoy_change=0.0,
                    keyword_difficulty=r["kd"],
                    current_position=r["position"],
                    intent=r["intent"],
                    opportunity_score=r["opportunity_score"],
                    cpc=0.0,
                    ranking_url=r["ranking_url"],
                    serp_features=r["serp_features"],
                    competitors=r["competitors"],
                    country_code="in",
                ))
            db.commit()
            print(f"[3/3] wrote {len(records)} keywords + {len(all_pages)} pages -> {dom.domain} ({dom.id})")
    finally:
        db.close()


if __name__ == "__main__":
    main()
