"""Page history routes — a workspace's tracked pages, their timeline and search impact."""

import re

from bs4 import BeautifulSoup
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import WorkspaceContext, get_workspace_context
from app.db.session import get_db
from app.schemas.page_history import (
    InspectRequest,
    InspectResult,
    PagePerformance,
    PageTimeline,
    SitePageList,
    SitePageOut,
    SnapshotOut,
)
from app.services import content_opt_service, content_score, page_history_service
from app.services.pagination import PageQuery, page_query

router = APIRouter(prefix="/workspaces/{workspace_id}/pages", tags=["page-history"])


def _page_or_404(db: Session, ctx: WorkspaceContext, page_id: str):
    page = page_history_service.get_page(db, ctx.workspace, page_id)
    if page is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Page not found")
    return page


@router.get("", response_model=SitePageList, summary="Tracked pages (paged)")
def list_pages(
    domain_id: str | None = Query(None, description="A website id, or 'other' for pages on other sites"),
    listing: PageQuery = Depends(page_query),
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> SitePageList:
    rows, total = page_history_service.list_pages(
        db, ctx.workspace, domain_id=domain_id, q=listing.search(), page=listing.page,
        page_size=listing.page_size, sort=listing.sort, descending=listing.descending)
    total_pages = max(1, -(-total // listing.page_size))
    return SitePageList(rows=[SitePageOut(**r) for r in rows], pagination={
        "page": min(listing.page, total_pages), "page_size": listing.page_size, "total": total,
        "total_pages": total_pages})


@router.get("/{page_id}", response_model=PageTimeline, summary="A page's snapshots and changes (newest first)")
def page_timeline(
    page_id: str,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> PageTimeline:
    page = _page_or_404(db, ctx, page_id)
    return PageTimeline(page=SitePageOut(**page_history_service.page_out(page)),
                        snapshots=[SnapshotOut(**s) for s in page_history_service.timeline(db, ctx.workspace, page)])


@router.get("/{page_id}/performance", response_model=PagePerformance,
            summary="Daily Search Console numbers + before/after each change")
def page_performance(
    page_id: str,
    days: int = Query(180, ge=28, le=480),
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> PagePerformance:
    page = _page_or_404(db, ctx, page_id)
    return PagePerformance(**page_history_service.performance(db, ctx.workspace, page, days=days))


def _meta(soup: BeautifulSoup, *names: str) -> str:
    for n in names:
        tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return ""


@router.post("/inspect", response_model=InspectResult, summary="Read a page's live title, meta and social tags")
def inspect_page(
    payload: InspectRequest,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> InspectResult:
    try:
        html, final_url = content_opt_service._fetch_html(payload.url)
    except content_opt_service.ContentFetchError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    soup = BeautifulSoup(html, "html.parser")
    canonical = soup.find("link", attrs={"rel": re.compile("canonical", re.I)})
    extracted = content_score.extract(html, final_url)
    snap = None
    try:
        snap = page_history_service.record(db, ctx.workspace, final_url, extracted=extracted, source="inspect")
    except Exception:  # noqa: BLE001 — history is a side effect; reading tags must still work
        db.rollback()
    return InspectResult(
        page_id=str(snap.page_id) if snap else None,
        url=final_url,
        title=extracted["title"],
        meta=extracted["meta"],
        h1=extracted["h1"],
        canonical=(canonical.get("href", "").strip() if canonical else ""),
        og_title=_meta(soup, "og:title"),
        og_description=_meta(soup, "og:description"),
        og_image=_meta(soup, "og:image"),
        og_site_name=_meta(soup, "og:site_name"),
        twitter_site=_meta(soup, "twitter:site"),
        word_count=extracted["word_count"],
        changes=snap.changes if snap else [],
    )
