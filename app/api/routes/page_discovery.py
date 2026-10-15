"""Page Discovery routes — scan a domain's pages (sitemap-first, crawl fallback)."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import DomainContext, get_domain_context
from app.db.session import get_db
from app.schemas.page_discovery import (
    DiscoveredPageList,
    DiscoveredPageOut,
    DiscoveryRunOut,
    PageContentOut,
    PageDiscoverySummary,
    ScoreContentRequest,
    ScoreContentResult,
)
from app.services import page_discovery_service
from app.services.pagination import PageQuery, page_query

router = APIRouter(
    prefix="/workspaces/{workspace_id}/domains/{domain_id}/page-discovery",
    tags=["page-discovery"],
)


def _run_out(run) -> DiscoveryRunOut:
    return DiscoveryRunOut(
        id=run.id, source=run.source, sitemap_url=run.sitemap_url, status=run.status,
        pages_found=run.pages_found, pages_enriched=run.pages_enriched, truncated=run.truncated,
        started_at=run.started_at, finished_at=run.finished_at)


@router.post("/discover", response_model=DiscoveryRunOut, summary="Scan this domain for pages")
def discover(
    background_tasks: BackgroundTasks,
    allow_crawl: bool = Query(
        False,
        description="Crawl the site when no sitemap is found. Default false: a "
        "no-sitemap scan stops with source='no_sitemap' so the user can confirm first.",
    ),
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> DiscoveryRunOut:
    """Start a page-discovery scan in the background. Sitemap-first; the site is
    crawled only when `allow_crawl=true` (crawling is slower and heavier on the
    site, so the UI asks first). Poll GET /status for progress. Free — no paid API."""
    existing = page_discovery_service.latest_run(db, ctx.domain)
    # A genuinely in-flight scan blocks a duplicate; a 'running' row older than
    # 15 min is stale (e.g. a server restart) and is allowed to be superseded.
    if existing is not None and existing.status == "running" and existing.started_at is not None:
        started = existing.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - started < timedelta(minutes=15):
            return _run_out(existing)
    background_tasks.add_task(
        page_discovery_service.run_discovery, ctx.workspace.id, ctx.domain.id, allow_crawl=allow_crawl)
    return DiscoveryRunOut(source="none", status="running", started_at=datetime.now(timezone.utc))


@router.post("/cancel", response_model=DiscoveryRunOut | None, summary="Stop the running scan")
def cancel(
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> DiscoveryRunOut | None:
    """Stop an in-progress scan. Pages already found are kept; the run finishes
    as 'cancelled' and can be started again later. Safe to call when nothing
    is running (no-op)."""
    run = page_discovery_service.cancel_running(db, ctx.domain)
    return _run_out(run) if run is not None else None


@router.get("/status", response_model=DiscoveryRunOut | None, summary="Latest scan status")
def status(
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> DiscoveryRunOut | None:
    run = page_discovery_service.latest_run(db, ctx.domain)
    return _run_out(run) if run is not None else None


@router.get("/summary", response_model=PageDiscoverySummary, summary="Discovered-pages KPIs + type breakdown")
def summary(
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> PageDiscoverySummary:
    return PageDiscoverySummary(**page_discovery_service.summary(db, ctx.domain))


@router.get("/pages/{page_id}/content", response_model=PageContentOut,
            summary="Fetch a page's editable content (cached; free, no paid API)")
def page_content(
    page_id: str,
    refresh: bool = Query(False, description="Refetch the live page instead of using the cache."),
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> PageContentOut:
    """Get the main-article content for one discovered page, for the content
    editor. Served from cache unless `refresh`; first open fetches it live and
    caches it, so large sites with many dynamic pages stay cheap to scan."""
    data = page_discovery_service.fetch_page_content(db, ctx.domain, page_id, refresh=refresh)
    if data is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Page not found")
    return PageContentOut(**data)


@router.post("/pages/{page_id}/score", response_model=ScoreContentResult,
             summary="Re-score edited content (deterministic, no AI)")
def score_content(
    page_id: str,
    payload: ScoreContentRequest,
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> ScoreContentResult:
    """Score the editor's current title / meta / body for a target keyword, using
    the same rules as Content Optimization. Same input always scores the same."""
    data = page_discovery_service.score_page_content(
        db, ctx.domain, page_id, title=payload.title, meta=payload.meta,
        body_html=payload.body_html, keyword=payload.keyword)
    if data is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Page not found")
    return ScoreContentResult(**data)


@router.get("", response_model=DiscoveredPageList, summary="Discovered pages (paged, filterable)")
def list_pages(
    type: str = Query("all", max_length=20, description="Page type filter"),
    status: str = Query("all", pattern="^(all|ok|error)$"),
    indexable: str = Query("all", pattern="^(all|yes|no)$"),
    listing: PageQuery = Depends(page_query),
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> DiscoveredPageList:
    """sort = url | title | type | status | lastmod | discovered (default: discovered desc)."""
    rows, total = page_discovery_service.list_pages(
        db, ctx.domain, q=listing.search(), page_type=type, status=status, indexable=indexable,
        page=listing.page, page_size=listing.page_size, sort=listing.sort, descending=listing.descending)
    total_pages = max(1, -(-total // listing.page_size))
    return DiscoveredPageList(
        rows=[DiscoveredPageOut(**r) for r in rows],
        pagination={"page": min(listing.page, total_pages), "page_size": listing.page_size,
                    "total": total, "total_pages": total_pages})
