"""Backlinks routes (domain-scoped). GETs read stored data only; the POST
refresh routes are the only ones that call DataForSEO (paid when live)."""

import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.api.deps import DomainContext, get_domain_context
from app.api.errors import upstream_error
from app.db.session import get_db
from app.models.backlink import KIND_PAGE, KIND_SITE
from app.schemas.backlinks import (
    BacklinkAnalyticsResponse, BacklinkDetailsResponse, BacklinkOptionsResponse, BacklinkProfileResponse,
    BacklinkRowsResponse, LinkedPageDetailResponse, LinkGapResponse, ReferringSiteDetailResponse,
)
from app.services.pagination import MAX_PAGE_SIZE, PageQuery, page_query
from app.services import backlinks_service, data_env
from app.services.dataforseo_client import DataForSeoError
from app.services.ranked_keywords_service import allowed_for_domain, norm_host

router = APIRouter(
    prefix="/workspaces/{workspace_id}/domains/{domain_id}/backlinks",
    tags=["backlinks"],
)

_TARGET = Query(None, min_length=3, max_length=255, description="Domain to profile (default: yours)")
_ERRORS = {403: {"description": "Live fetching off / domain not allowed"},
           409: {"description": "No tracked competitors (link gap)"},
           502: {"description": "DataForSEO error / not configured"}}


@router.get("/options", response_model=BacklinkOptionsResponse,
            summary="Your domain + competitors, with whether backlink data is saved")
def options(ctx: DomainContext = Depends(get_domain_context),
            db: Session = Depends(get_db)) -> BacklinkOptionsResponse:
    return BacklinkOptionsResponse(**backlinks_service.options(db, ctx.domain))


@router.get("/profile", response_model=BacklinkProfileResponse,
            summary="Stored backlink profile of a domain (no fetch)")
def profile(target: str | None = _TARGET, ctx: DomainContext = Depends(get_domain_context),
            db: Session = Depends(get_db)) -> BacklinkProfileResponse:
    return BacklinkProfileResponse(**backlinks_service.profile_payload(db, ctx.domain, target))


@router.get("/profile/details", response_model=BacklinkDetailsResponse,
            summary="Breakdowns, fetch history and changes since the previous fetch (stored data only)")
def profile_details(target: str | None = _TARGET, ctx: DomainContext = Depends(get_domain_context),
                    db: Session = Depends(get_db)) -> BacklinkDetailsResponse:
    return BacklinkDetailsResponse(**backlinks_service.details(db, ctx.domain, target))


@router.get("/profile/rows", response_model=BacklinkRowsResponse,
            summary="One page of a stored list: domains | links | anchors | pages (search, filter, sort)")
def profile_rows(
    list_: str = Query("domains", alias="list", pattern="^(domains|links|anchors|pages|reclaim)$"),
    filter_: str = Query("all", alias="filter", max_length=20),
    target: str | None = _TARGET,
    listing: PageQuery = Depends(page_query),
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> BacklinkRowsResponse:
    rows, info = backlinks_service.list_rows(
        db, ctx.domain, target, list_, q=listing.search(), filter_=filter_, sort=listing.sort,
        descending=listing.descending, page=listing.page, page_size=listing.page_size)
    return BacklinkRowsResponse(rows=rows, pagination=info)


@router.post("/profile/refresh", response_model=BacklinkProfileResponse, responses=_ERRORS,
             summary="Fetch a backlink profile from DataForSEO (4 requests) and store it")
def refresh_profile(
    target: str | None = _TARGET,
    force: bool = Query(False, description="Fetch again even if data is saved (paid when live)"),
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> BacklinkProfileResponse:
    try:
        _, fetched = backlinks_service.fetch_profile(
            db, target or ctx.domain.domain, env=data_env.env_for_domain(db, ctx.domain),
            allowed=allowed_for_domain(db, ctx.domain), force=force)
    except backlinks_service.BacklinksFetchNotAllowed as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except DataForSeoError as exc:
        raise upstream_error(exc) from exc
    payload = backlinks_service.profile_payload(db, ctx.domain, target)
    return BacklinkProfileResponse(**payload, fetched_now=fetched)


@router.get("/gap", response_model=LinkGapResponse,
            summary="Stored link gap: sites linking to your competitors, not you (no fetch)")
def gap(ctx: DomainContext = Depends(get_domain_context),
        db: Session = Depends(get_db)) -> LinkGapResponse:
    return LinkGapResponse(**backlinks_service.gap_payload(db, ctx.domain))


@router.post("/gap/refresh", response_model=LinkGapResponse, responses=_ERRORS,
             summary="Fetch the link gap from DataForSEO (1 request) and store it")
def refresh_gap(
    force: bool = Query(False, description="Fetch again even if data is saved (paid when live)"),
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> LinkGapResponse:
    try:
        _, fetched = backlinks_service.fetch_gap(
            db, ctx.domain, env=data_env.env_for_domain(db, ctx.domain), force=force)
    except backlinks_service.NoCompetitors as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except backlinks_service.BacklinksFetchNotAllowed as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except DataForSeoError as exc:
        raise upstream_error(exc) from exc
    return LinkGapResponse(**backlinks_service.gap_payload(db, ctx.domain), fetched_now=fetched)


# ── Analytics (Overview dashboard) ──────────────────────────────────────────

@router.get("/analytics", response_model=BacklinkAnalyticsResponse,
            summary="Trend, new/lost, anchor classification and quality signals (stored data only)")
def analytics(target: str | None = _TARGET, ctx: DomainContext = Depends(get_domain_context),
              db: Session = Depends(get_db)) -> BacklinkAnalyticsResponse:
    return BacklinkAnalyticsResponse(**backlinks_service.analytics(db, ctx.domain, target))


# ── Entity drill-down (site / page) ─────────────────────────────────────────

_HOST = Query(..., min_length=3, max_length=255, description="Referring host to drill into")
_URL = Query(..., min_length=1, max_length=2048, description="Target page URL to drill into")


@router.get("/profile/site", response_model=ReferringSiteDetailResponse,
            summary="One referring site: its links to you (stored; complete after Load all links)")
def site(host: str = _HOST, target: str | None = _TARGET, listing: PageQuery = Depends(page_query),
         ctx: DomainContext = Depends(get_domain_context), db: Session = Depends(get_db)) -> ReferringSiteDetailResponse:
    return ReferringSiteDetailResponse(**backlinks_service.site_detail(
        db, ctx.domain, target, host, q=listing.search(), sort=listing.sort,
        descending=listing.descending, page=listing.page, page_size=listing.page_size))


@router.get("/profile/page", response_model=LinkedPageDetailResponse,
            summary="One page: every site linking to it (stored; complete after Load all links)")
def page(url: str = _URL, target: str | None = _TARGET, listing: PageQuery = Depends(page_query),
         ctx: DomainContext = Depends(get_domain_context), db: Session = Depends(get_db)) -> LinkedPageDetailResponse:
    return LinkedPageDetailResponse(**backlinks_service.page_detail(
        db, ctx.domain, target, url, q=listing.search(), sort=listing.sort,
        descending=listing.descending, page=listing.page, page_size=listing.page_size))


@router.post("/profile/site/refresh", response_model=ReferringSiteDetailResponse, responses=_ERRORS,
             summary="Fetch every link from one referring site (1 request) and store it")
def refresh_site(host: str = _HOST, target: str | None = _TARGET, force: bool = Query(False),
                 ctx: DomainContext = Depends(get_domain_context), db: Session = Depends(get_db)) -> ReferringSiteDetailResponse:
    host = norm_host(host)
    try:
        _, fetched = backlinks_service.fetch_links_detail(
            db, target or ctx.domain.domain, kind=KIND_SITE, value=host,
            filters=[["domain_from", "=", host]], env=data_env.env_for_domain(db, ctx.domain),
            allowed=allowed_for_domain(db, ctx.domain), force=force)
    except backlinks_service.BacklinksFetchNotAllowed as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except DataForSeoError as exc:
        raise upstream_error(exc) from exc
    payload = backlinks_service.site_detail(db, ctx.domain, target, host)
    payload["fetched_now"] = fetched
    return ReferringSiteDetailResponse(**payload)


@router.post("/profile/page/refresh", response_model=LinkedPageDetailResponse, responses=_ERRORS,
             summary="Fetch every link pointing at one page (1 request) and store it")
def refresh_page(url: str = _URL, target: str | None = _TARGET, force: bool = Query(False),
                 ctx: DomainContext = Depends(get_domain_context), db: Session = Depends(get_db)) -> LinkedPageDetailResponse:
    try:
        _, fetched = backlinks_service.fetch_links_detail(
            db, target or ctx.domain.domain, kind=KIND_PAGE, value=url,
            filters=[["url_to", "=", url]], env=data_env.env_for_domain(db, ctx.domain),
            allowed=allowed_for_domain(db, ctx.domain), force=force)
    except backlinks_service.BacklinksFetchNotAllowed as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except DataForSeoError as exc:
        raise upstream_error(exc) from exc
    payload = backlinks_service.page_detail(db, ctx.domain, target, url)
    payload["fetched_now"] = fetched
    return LinkedPageDetailResponse(**payload)


# ── Export ──────────────────────────────────────────────────────────────────

@router.get("/export", summary="Download a stored list as CSV (no fetch)")
def export(
    list_: str = Query("links", alias="list", pattern="^(domains|links|anchors|pages|reclaim)$"),
    filter_: str = Query("all", alias="filter", max_length=20),
    target: str | None = _TARGET,
    sort: str | None = Query(None, max_length=40),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    q: str | None = Query(None, max_length=200),
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> Response:
    rows, info = backlinks_service.list_rows(
        db, ctx.domain, target, list_, q=(q or "").strip().lower(), filter_=filter_,
        sort=sort, descending=order == "desc", page=1, page_size=MAX_PAGE_SIZE)
    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()), extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({k: (",".join(v) if isinstance(v, list) else v) for k, v in r.items()})
    host = norm_host(target or ctx.domain.domain)
    headers = {"Content-Disposition": f'attachment; filename="backlinks-{list_}-{host}.csv"'}
    if info["total"] > MAX_PAGE_SIZE:
        headers["X-Truncated"] = "true"
    return Response(content=buf.getvalue(), media_type="text/csv", headers=headers)
