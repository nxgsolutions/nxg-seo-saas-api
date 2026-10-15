"""Page Discovery — find a domain's public pages and enrich them.

    run_discovery(workspace_id, domain_id)   # background: discover → enrich → store

Discovery is sitemap-first: read robots.txt for declared sitemaps (or the usual
candidates), parse them (recursing sitemap indexes), and collect page URLs. If no
sitemap yields anything, fall back to a bounded same-site crawl from the homepage.
Each discovered URL is then fetched once (SSRF-guarded, in parallel) for its HTTP
status, title, type and indexability, and upserted into the shared `site_pages`
table. One scan is recorded as a `page_discovery_runs` row.

All free (plain HTTP, no paid API). Pure-ish helpers (find/collect/classify) are
network-light and unit-tested; `run_discovery` opens its own DB session and never
raises (it's a background worker).
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import trafilatura
from bs4 import BeautifulSoup
from sqlalchemy import case, func, select, update

from app.db.session import SessionLocal
from app.models.domain import Domain
from app.models.page_discovery import PageDiscoveryRun
from app.models.site_page import SitePage
from app.models.workspace import Workspace
from app.services import audit_service, content_score, crawl_service, page_history_service

logger = logging.getLogger(__name__)

# Caps that keep a scan bounded and cheap (all free HTTP fetches, no paid API).
MAX_PAGES = 5000
SITEMAP_DEPTH = 3          # how deep a sitemap-index tree is followed
MAX_SITEMAPS = 50          # total sitemap documents fetched per scan
CRAWL_DEPTH = 3            # link-following depth for the no-sitemap fallback
CRAWL_TIME_BUDGET_S = 25.0
ENRICH_WORKERS = 6

SITEMAP_CANDIDATES = ("sitemap.xml", "sitemap_index.xml", "sitemap-index.xml", "wp-sitemap.xml")
_ASSET_RE = re.compile(r"\.(?:jpe?g|png|gif|webp|svg|ico|css|js|pdf|zip|gz|mp4|mp3|woff2?|ttf|xml)(?:\?|$)", re.I)
_UTILITY_SEGS = {
    "about", "about-us", "contact", "contact-us", "privacy", "privacy-policy", "terms",
    "terms-and-conditions", "terms-of-service", "disclaimer", "login", "signin", "sign-in",
    "register", "cart", "checkout", "sitemap", "faq", "search",
}


# ── Host helpers ─────────────────────────────────────────────────────────────

def _norm_host(value: str) -> str:
    host = value.strip().lower()
    if "://" in host:
        host = urlparse(host).hostname or ""
    else:
        host = host.split("/")[0]
    return host[4:] if host.startswith("www.") else host


def _same_site(url: str, root_host: str) -> bool:
    h = _norm_host(url)
    return bool(h) and (h == root_host or h.endswith("." + root_host))


# ── Sitemap discovery ────────────────────────────────────────────────────────

def find_sitemaps(origin: str) -> list[str]:
    """Sitemap URLs declared in robots.txt, else the conventional candidates."""
    status, text = crawl_service._fetch_text(urljoin(origin + "/", "robots.txt"))
    if status == 200 and text.strip():
        declared = crawl_service.parse_robots(text).get("sitemaps") or []
        if declared:
            return declared
    return [urljoin(origin + "/", c) for c in SITEMAP_CANDIDATES]


def collect_from_sitemaps(origin: str, root_host: str) -> tuple[str | None, list[dict], bool]:
    """Walk the sitemap tree. Returns (first_sitemap_that_yielded, pages, truncated).

    `pages` is a de-duplicated list of {"loc", "lastmod"} page entries on the same
    site. Sitemap indexes are recursed (bounded by depth / count / MAX_PAGES)."""
    queue: deque[tuple[str, int]] = deque((sm, 0) for sm in find_sitemaps(origin))
    visited: set[str] = set()
    seen_pages: set[str] = set()
    pages: list[dict] = []
    source_sitemap: str | None = None
    truncated = False

    while queue:
        if len(visited) >= MAX_SITEMAPS or len(pages) >= MAX_PAGES:
            truncated = len(pages) >= MAX_PAGES
            break
        sm_url, depth = queue.popleft()
        if sm_url in visited:
            continue
        visited.add(sm_url)

        status, text = crawl_service._fetch_text(sm_url)
        if status != 200 or not text.strip():
            continue
        parsed = crawl_service.parse_sitemap_urls(text)
        if parsed["type"] == "index":
            if depth >= SITEMAP_DEPTH:
                continue
            for e in parsed["entries"]:
                if e["loc"] not in visited:
                    queue.append((e["loc"], depth + 1))
        elif parsed["type"] == "urlset":
            added_here = False
            for e in parsed["entries"]:
                loc = e["loc"]
                key = page_history_service.url_key(loc)
                if key in seen_pages or not _same_site(loc, root_host) or _ASSET_RE.search(loc):
                    continue
                seen_pages.add(key)
                pages.append(e)
                added_here = True
                if len(pages) >= MAX_PAGES:
                    truncated = True
                    break
            if added_here and source_sitemap is None:
                source_sitemap = sm_url
    return source_sitemap, pages, truncated


# ── Crawl fallback (no sitemap) ──────────────────────────────────────────────

def _star_disallows(origin: str) -> list[str]:
    """Disallow path-prefixes for user-agent * (best-effort, for the crawl)."""
    status, text = crawl_service._fetch_text(urljoin(origin + "/", "robots.txt"))
    if status != 200:
        return []
    out: list[str] = []
    applies = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        field, _, value = line.partition(":")
        field, value = field.strip().lower(), value.strip()
        if field == "user-agent":
            applies = value == "*"
        elif field == "disallow" and applies and value:
            out.append(value)
    return out


def crawl_fallback(origin: str, root_host: str) -> tuple[list[dict], bool]:
    """Bounded BFS from the homepage following same-site links. Returns
    ([{"loc", "lastmod": None}], truncated). Used only when sitemaps are empty."""
    disallows = _star_disallows(origin)
    start = origin + "/"
    seen: set[str] = {page_history_service.url_key(start)}
    pages: list[dict] = [{"loc": start, "lastmod": None}]
    queue: deque[tuple[str, int]] = deque([(start, 0)])
    deadline = time.monotonic() + CRAWL_TIME_BUDGET_S
    truncated = False

    while queue and time.monotonic() < deadline and len(pages) < MAX_PAGES:
        url, depth = queue.popleft()
        if depth >= CRAWL_DEPTH:
            continue
        try:
            response, final_url, _ms = audit_service.safe_fetch(url)
        except (audit_service.InvalidUrlError, audit_service.BlockedUrlError, audit_service.FetchError):
            continue
        if response.status_code != 200 or "html" not in (response.headers.get("content-type", "").lower()):
            continue
        soup = BeautifulSoup(response.content or b"", "html.parser")
        for a in soup.find_all("a", href=True):
            href = urljoin(final_url, a["href"].strip())
            href = href.split("#", 1)[0]
            if not href.startswith(("http://", "https://")) or _ASSET_RE.search(href):
                continue
            if not _same_site(href, root_host):
                continue
            path = urlparse(href).path or "/"
            if any(path.startswith(d) for d in disallows):
                continue
            key = page_history_service.url_key(href)
            if key in seen:
                continue
            seen.add(key)
            pages.append({"loc": href, "lastmod": None})
            if len(pages) >= MAX_PAGES:
                truncated = True
                break
            queue.append((href, depth + 1))
    return pages, truncated


# ── Enrichment (fetch each page once) ────────────────────────────────────────

def classify_page_type(url: str, soup: BeautifulSoup | None = None) -> str:
    """home | blog | category | tag | content | other — from URL + light signals."""
    path = (urlparse(url).path or "/").lower().rstrip("/")
    if path in ("", "/"):
        return "home"
    segs = [s for s in path.split("/") if s]
    if any(s in ("tag", "tags") for s in segs):
        return "tag"
    if any(s in ("category", "categories", "cat", "topics", "topic") for s in segs):
        return "category"
    og_type = ""
    has_article = False
    if soup is not None:
        tag = soup.find("meta", attrs={"property": "og:type"})
        og_type = (tag.get("content", "") if tag else "").lower()
        has_article = soup.find("article") is not None
    if og_type == "article" or has_article or re.search(r"/(blog|post|posts|articles?|news)/", path + "/"):
        return "blog"
    if segs and segs[-1] in _UTILITY_SEGS:
        return "other"
    return "content"


def _is_indexable(response, soup: BeautifulSoup) -> bool:
    """A page is indexable when it returns 200 and nothing says noindex (meta
    robots / googlebot, or the X-Robots-Tag HTTP header)."""
    if response.status_code != 200:
        return False
    if "noindex" in (response.headers.get("x-robots-tag", "") or "").lower():
        return False
    for name in ("robots", "googlebot"):
        tag = soup.find("meta", attrs={"name": re.compile(f"^{name}$", re.I)})
        if tag and "noindex" in (tag.get("content", "") or "").lower():
            return False
    return True


def _main_content_word_count(html: str) -> int | None:
    """Clean main-content word count via trafilatura (nav/footer/ads stripped).
    Runs locally — no network, no cost. None when nothing meaningful was found."""
    if not html:
        return None
    try:
        text = trafilatura.extract(html, include_comments=False, include_tables=False, favor_precision=True)
    except Exception:  # noqa: BLE001 — extraction must never break enrichment
        return None
    return len(text.split()) if text else None


def _enrich_one(entry: dict) -> dict:
    """Fetch one discovered URL and read status / title / type / indexability /
    word count. `loc` is the original (stored) URL used to match the row; `url`
    is the final URL after redirects."""
    loc = entry["loc"]
    out = {"loc": loc, "url": loc, "status": None, "title": "",
           "page_type": classify_page_type(loc), "indexable": None, "word_count": None}
    try:
        response, final_url, _ms = audit_service.safe_fetch(loc)
    except (audit_service.InvalidUrlError, audit_service.BlockedUrlError, audit_service.FetchError):
        return out
    out["url"] = final_url
    out["status"] = response.status_code
    ctype = (response.headers.get("content-type", "") or "").lower()
    if response.status_code != 200 or "html" not in ctype:
        out["indexable"] = False
        return out
    raw = (response.content or b"")[:3_000_000]
    soup = BeautifulSoup(raw, "html.parser")
    out["title"] = (soup.title.get_text(" ", strip=True) if soup.title else "")[:512]
    out["page_type"] = classify_page_type(final_url, soup)
    out["indexable"] = _is_indexable(response, soup)
    out["word_count"] = _main_content_word_count(raw.decode("utf-8", "ignore"))
    return out


def enrich_batch(entries: list[dict]) -> list[dict]:
    """Fetch a batch of discovered URLs in parallel (SSRF-guarded)."""
    if not entries:
        return []
    with ThreadPoolExecutor(max_workers=ENRICH_WORKERS) as pool:
        return list(pool.map(_enrich_one, entries))


# ── Storage ──────────────────────────────────────────────────────────────────

def _parse_lastmod(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


ENRICH_BATCH = 50


def _store_urls(db, workspace: Workspace, entries: list[dict], source: str) -> None:
    """Upsert the discovered URL rows FIRST (cheap, no fetch) so the table fills
    in immediately; enrichment updates them in place afterwards."""
    now = datetime.now(timezone.utc)
    for i, e in enumerate(entries):
        page = page_history_service.get_or_create_page(db, workspace, e["loc"])
        page.discovery_source = source
        page.sitemap_lastmod = _parse_lastmod(e.get("lastmod"))
        page.discovered_at = now
        if page.page_type is None:
            page.page_type = classify_page_type(e["loc"])  # refined on enrich
        if i % 200 == 199:
            db.commit()
    db.commit()


def _apply_enrichment(db, workspace: Workspace, results: list[dict]) -> None:
    """Write fetched facts onto the already-stored rows (matched by the original
    URL), then commit the batch."""
    for r in results:
        page = page_history_service.get_or_create_page(db, workspace, r["loc"], title=r.get("title") or "")
        page.http_status = r.get("status")
        page.indexable = r.get("indexable")
        page.page_type = r.get("page_type")
        page.word_count = r.get("word_count")
        if r.get("url") and r["url"] != page.url:
            page.url = r["url"]
        if r.get("title"):
            page.title = r["title"][:512]
    db.commit()


# ── Orchestrator (background worker) ─────────────────────────────────────────

def run_discovery(workspace_id, domain_id, *, allow_crawl: bool = False, session_factory=None) -> None:
    """Discover + enrich + store a domain's pages. Own DB session; never raises
    (background worker — the user's request already returned).

    Sitemap-first. When no sitemap is found the site is crawled ONLY if
    `allow_crawl` is set — otherwise the run finishes with source='no_sitemap'
    and no pages, so the UI can warn the user and ask before crawling (crawling
    is slower and heavier on their server).

    Stores the URL list up front (table populates instantly) then enriches in
    batches, updating pages_enriched as it goes so progress is visible."""
    db = (session_factory or SessionLocal)()
    run = None
    try:
        workspace = db.get(Workspace, workspace_id)
        domain = db.get(Domain, domain_id)
        if workspace is None or domain is None or domain.workspace_id != workspace.id:
            return

        # Supersede any earlier run left 'running' (e.g. a server restart).
        db.execute(update(PageDiscoveryRun).where(
            PageDiscoveryRun.domain_id == domain.id, PageDiscoveryRun.status == "running"
        ).values(status="failed", error="superseded", finished_at=datetime.now(timezone.utc)))
        db.commit()

        prev = db.execute(
            select(PageDiscoveryRun.pages_found)
            .where(PageDiscoveryRun.domain_id == domain.id, PageDiscoveryRun.status == "completed")
            .order_by(PageDiscoveryRun.started_at.desc()).limit(1)
        ).scalar_one_or_none()

        run = PageDiscoveryRun(workspace_id=workspace.id, domain_id=domain.id,
                               status="running", prev_total=prev)
        db.add(run)
        db.commit()

        origin = "https://" + _norm_host(domain.domain)
        root_host = _norm_host(domain.domain)

        source_sitemap, entries, truncated = collect_from_sitemaps(origin, root_host)
        source = "sitemap"
        if not entries:
            if allow_crawl:
                entries, truncated = crawl_fallback(origin, root_host)
                source = "crawl" if entries else "none"
            else:
                # No sitemap and no permission to crawl yet — stop and report
                # back so the UI can suggest adding a sitemap and confirm first.
                source = "no_sitemap"

        run.source = source
        run.sitemap_url = source_sitemap
        run.pages_found = len(entries)
        run.truncated = truncated
        db.commit()

        if entries and source not in ("none", "no_sitemap"):
            _store_urls(db, workspace, entries, source)
            enriched = 0
            for i in range(0, len(entries), ENRICH_BATCH):
                # Honor a user Stop: if the run was cancelled, keep what we have
                # (pages are already stored) and finish early as 'cancelled'.
                db.refresh(run, ["status"])
                if run.status == "cancelled":
                    run.pages_enriched = enriched
                    run.finished_at = datetime.now(timezone.utc)
                    db.commit()
                    return
                batch = entries[i:i + ENRICH_BATCH]
                _apply_enrichment(db, workspace, enrich_batch(batch))
                enriched += len(batch)
                run.pages_enriched = enriched
                db.commit()

        # Don't overwrite a cancellation that landed during the last batch.
        db.refresh(run, ["status"])
        if run.status != "cancelled":
            run.status = "completed"
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
    except Exception as exc:  # noqa: BLE001 — background worker must not crash
        logger.exception("Page discovery failed for domain %s", domain_id)
        try:
            db.rollback()
            if run is not None:
                run = db.get(PageDiscoveryRun, run.id)
                if run is not None:
                    run.status = "failed"
                    run.error = str(exc)[:500]
                    run.finished_at = datetime.now(timezone.utc)
                    db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
    finally:
        db.close()


# ── Reads (for the API) ──────────────────────────────────────────────────────

def latest_run(db, domain: Domain) -> PageDiscoveryRun | None:
    return db.execute(
        select(PageDiscoveryRun).where(PageDiscoveryRun.domain_id == domain.id)
        .order_by(PageDiscoveryRun.started_at.desc()).limit(1)
    ).scalar_one_or_none()


def cancel_running(db, domain: Domain) -> PageDiscoveryRun | None:
    """Flag any in-flight scan for this domain as cancelled. The background
    worker checks this flag between batches and stops, keeping pages found so
    far. Returns the latest run so the API can report current state."""
    db.execute(update(PageDiscoveryRun).where(
        PageDiscoveryRun.domain_id == domain.id, PageDiscoveryRun.status == "running"
    ).values(status="cancelled", error="stopped by user", finished_at=datetime.now(timezone.utc)))
    db.commit()
    return latest_run(db, domain)


def summary(db, domain: Domain) -> dict:
    """KPI totals + several breakdowns for the discovered pages of a domain:
    page type, indexability, HTTP-status class, discovery source, and content
    freshness (by sitemap lastmod). All free — plain aggregates over site_pages."""
    scope = (SitePage.domain_id == domain.id, SitePage.discovery_source.isnot(None))
    base = select(func.count()).select_from(SitePage).where(*scope)
    total = db.execute(base).scalar_one()
    indexable = db.execute(base.where(SitePage.indexable.is_(True))).scalar_one()
    noindex = db.execute(base.where(SitePage.indexable.is_(False))).scalar_one()

    by_type = dict(db.execute(
        select(SitePage.page_type, func.count()).where(*scope).group_by(SitePage.page_type)
    ).all())

    # HTTP status grouped into classes (ok / redirect / client/server error / unknown).
    status_class = case(
        (SitePage.http_status.is_(None), "unknown"),
        (SitePage.http_status.between(200, 299), "ok"),
        (SitePage.http_status.between(300, 399), "redirect"),
        (SitePage.http_status.between(400, 499), "client_error"),
        (SitePage.http_status >= 500, "server_error"),
        else_="unknown",
    )
    by_status = dict(db.execute(
        select(status_class, func.count()).where(*scope).group_by(status_class)
    ).all())

    # How each page was found (sitemap vs crawl).
    by_source = dict(db.execute(
        select(SitePage.discovery_source, func.count()).where(*scope)
        .group_by(SitePage.discovery_source)
    ).all())

    # Content freshness from sitemap <lastmod> — recency buckets.
    now = datetime.now(timezone.utc)
    age_days = func.extract("epoch", now - SitePage.sitemap_lastmod) / 86400.0
    freshness = case(
        (SitePage.sitemap_lastmod.is_(None), "unknown"),
        (age_days <= 30, "last_30"),
        (age_days <= 90, "last_90"),
        (age_days <= 365, "last_year"),
        else_="older",
    )
    by_freshness = dict(db.execute(
        select(freshness, func.count()).where(*scope).group_by(freshness)
    ).all())

    run = latest_run(db, domain)
    prev = run.prev_total if run else None
    delta_pct = round((total - prev) / prev * 100) if prev else None
    return {
        "total": total, "indexable": indexable, "noindex": noindex,
        "unknown_index": total - indexable - noindex,
        "by_type": {(k or "other"): v for k, v in by_type.items()},
        "by_status": {k: v for k, v in by_status.items()},
        "by_source": {(k or "other"): v for k, v in by_source.items()},
        "by_freshness": {k: v for k, v in by_freshness.items()},
        "delta_pct": delta_pct,
    }


def list_pages(db, domain: Domain, *, q: str, page_type: str, status: str, indexable: str,
               page: int, page_size: int, sort: str | None, descending: bool) -> tuple[list[dict], int]:
    """One page of a domain's discovered pages, filtered + sorted in SQL."""
    where = [SitePage.domain_id == domain.id, SitePage.discovery_source.isnot(None)]
    if q:
        like = f"%{q.lower()}%"
        where.append(func.lower(SitePage.url_key).like(like) | func.lower(SitePage.title).like(like))
    if page_type and page_type != "all":
        where.append(SitePage.page_type == page_type)
    if status == "ok":
        where.append(SitePage.http_status == 200)
    elif status == "error":
        where.append(SitePage.http_status.isnot(None) & (SitePage.http_status >= 400))
    if indexable == "yes":
        where.append(SitePage.indexable.is_(True))
    elif indexable == "no":
        where.append(SitePage.indexable.is_(False))

    # "priority" surfaces the evergreen, one-time pages first — home, then the
    # cornerstone static pages (about / contact / pyq / syllabus / pricing …) —
    # and sinks the big feeds (current-affairs & blog articles, paginated lists)
    # and dynamic query-string pages to the bottom. Within a tier, higher-scoring
    # / richer pages rank first.
    if (sort or "priority") == "priority":
        luk = func.lower(SitePage.url_key)
        # Path depth = number of "/" in "host/path" (home has none).
        depth = func.length(SitePage.url_key) - func.length(func.replace(SitePage.url_key, "/", ""))
        cornerstone = (
            luk.like("%/about%") | luk.like("%/contact%") | luk.like("%pyq%")
            | luk.like("%syllabus%") | luk.like("%/pricing%") | luk.like("%/faq%")
            | luk.like("%/terms%") | luk.like("%/privacy%")
        )
        prio = case(
            (SitePage.page_type == "home", 0),           # home
            (depth == 0, 0),                             # root URL (no path) = home
            (SitePage.url_key.like("%?%"), 95),          # dynamic query-string pages — last
            (luk.like("%current-affairs%"), 85),         # the current-affairs feed — near the end
            (cornerstone & (depth <= 1), 3),             # the single evergreen pages (about, contact, syllabus, pyq landing)
            (cornerstone, 7),                            # their deeper hubs (pyq by subject / year …)
            (luk.like("%/page/%"), 85),                  # paginated list pages
            (SitePage.page_type == "tag", 75),
            (SitePage.page_type == "blog", 65),          # blog / article pages
            (SitePage.page_type == "category", 55),
            (depth <= 1, 10),                            # other top-level static pages
            (depth <= 2, 20),
            else_=40,
        )
        order_by = [prio.asc(), SitePage.latest_audit_score.desc().nulls_last(),
                    SitePage.word_count.desc().nulls_last(), SitePage.discovered_at.desc().nulls_last()]
    else:
        cols = {"url": SitePage.url_key, "title": SitePage.title, "type": SitePage.page_type,
                "status": SitePage.http_status, "lastmod": SitePage.sitemap_lastmod,
                "discovered": SitePage.discovered_at, "words": SitePage.word_count,
                "score": SitePage.latest_audit_score}
        col = cols.get(sort or "", SitePage.discovered_at)
        order_by = [col.desc().nulls_last() if descending else col.asc().nulls_last()]
    total = db.execute(select(func.count()).select_from(SitePage).where(*where)).scalar_one()
    rows = db.execute(select(SitePage).where(*where).order_by(*order_by, SitePage.id)
                      .offset((page - 1) * page_size).limit(page_size)).scalars().all()
    return [_page_out(r) for r in rows], total


# ── Content editor (fetch on demand, cache, re-score) ────────────────────────

CONTENT_HTML_CAP = 200_000  # keep cached/editor HTML bounded


def _readable_html(html: str) -> str:
    """Clean, editable main-content HTML via trafilatura (headings, paragraphs,
    lists, tables and links kept; nav/footer/cards stripped). Works on JS-light
    server-rendered apps like Next.js where there's no <article>/<main> wrapper —
    our simple extractor would return raw card markup there. Falls back to that
    simple extractor only if trafilatura finds nothing."""
    try:
        out = trafilatura.extract(
            html, output_format="html", include_formatting=True,
            include_links=True, include_tables=True, favor_precision=False)
    except Exception:  # noqa: BLE001 — extraction must never break the request
        out = None
    if not out:
        return content_score.article_html(html)
    soup = BeautifulSoup(out, "html.parser")
    body = soup.body or soup
    inner = body.decode_contents() if hasattr(body, "decode_contents") else str(body)
    return inner.strip() or content_score.article_html(html)


def get_page(db, domain: Domain, page_id: str) -> SitePage | None:
    page = db.get(SitePage, page_id)
    if page is None or page.domain_id != domain.id:
        return None
    return page


def fetch_page_content(db, domain: Domain, page_id: str, *, refresh: bool = False) -> dict | None:
    """Return the editable content for one discovered page. Served from the cache
    unless `refresh`; otherwise fetched live (SSRF-guarded), cleaned to the main
    article, cached, and returned. None = page not found for this domain."""
    page = get_page(db, domain, page_id)
    if page is None:
        return None
    if page.content_html and not refresh:
        try:
            data = json.loads(page.content_html)
            data["cached"] = True
            data["fetched_at"] = page.content_fetched_at
            return data
        except (ValueError, TypeError):
            pass  # bad cache — fall through and refetch

    try:
        response, final_url, _ms = audit_service.safe_fetch(page.url)
    except (audit_service.InvalidUrlError, audit_service.BlockedUrlError, audit_service.FetchError) as exc:
        return {"error": str(exc) or "Could not fetch this page.", "http_status": None,
                "url": page.url, "cached": False}
    ctype = (response.headers.get("content-type", "") or "").lower()
    if response.status_code != 200 or "html" not in ctype:
        return {"error": f"The page returned HTTP {response.status_code} or isn't an HTML page.",
                "http_status": response.status_code, "url": final_url, "cached": False}

    raw = (response.content or b"")[:3_000_000].decode("utf-8", "ignore")
    facts = content_score.extract(raw, final_url)  # title + meta are reliable from <title>/<meta>
    body_html = _readable_html(raw)[:CONTENT_HTML_CAP]
    # Word count from the content we actually show, so the editor and table agree.
    word_count = len(BeautifulSoup(body_html, "html.parser").get_text(" ", strip=True).split()) or None
    payload = {
        "title": facts["title"], "meta": facts["meta"], "body_html": body_html,
        "word_count": word_count, "http_status": response.status_code, "url": final_url,
    }
    page.content_html = json.dumps(payload)
    page.content_fetched_at = datetime.now(timezone.utc)
    if facts["title"]:
        page.title = facts["title"][:512]
    page.word_count = word_count
    db.commit()
    return {**payload, "cached": False, "fetched_at": page.content_fetched_at}


def score_page_content(db, domain: Domain, page_id: str, *, title: str, meta: str,
                       body_html: str, keyword: str = "") -> dict | None:
    """Re-score edited editor fields for one page (deterministic, no AI, no fetch)."""
    page = get_page(db, domain, page_id)
    if page is None:
        return None
    facts, checks, sc = content_score.score_fields(title, meta, body_html, page.url, keyword)
    return {
        "score": sc, "checks": checks, "word_count": facts["word_count"],
        "reading_ease": facts["reading_ease"], "internal_links": facts["internal_links"],
        "external_links": facts["external_links"], "images": facts["images"],
        "headings": len(facts["headings"]),
    }


def _page_out(p: SitePage) -> dict:
    return {
        "id": str(p.id), "url": p.url, "title": p.title or "",
        "page_type": p.page_type or "other", "http_status": p.http_status,
        "indexable": p.indexable, "discovery_source": p.discovery_source,
        "sitemap_lastmod": p.sitemap_lastmod, "latest_audit_score": p.latest_audit_score,
        "word_count": p.word_count, "has_content": p.content_html is not None,
    }
