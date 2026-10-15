"""Backlinks routes (domain-scoped). GETs read stored data only; the POST
refresh routes are the only ones that call DataForSEO (paid when live)."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import DomainContext, get_domain_context
from app.api.errors import upstream_error
from app.db.session import get_db
from app.schemas.backlinks import (
    BacklinkDetailsResponse, BacklinkOptionsResponse, BacklinkProfileResponse, BacklinkRowsResponse, LinkGapResponse,
)
from app.services.pagination import PageQuery, page_query
from app.services import backlinks_service, data_env
from app.services.dataforseo_client import DataForSeoError
from app.services.ranked_keywords_service import allowed_for_domain

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
    list_: str = Query("domains", alias="list", pattern="^(domains|links|anchors|pages)$"),
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
