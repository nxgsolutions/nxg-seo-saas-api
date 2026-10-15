"""My Keywords routes — a website's saved keyword list (private per website)."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import DomainContext, get_domain_context
from app.db.session import get_db
from app.schemas.pagination import PageInfo
from app.services import saved_keywords_service
from app.services.pagination import PageQuery, page_query

router = APIRouter(prefix="/workspaces/{workspace_id}/domains/{domain_id}/my-keywords", tags=["my-keywords"])


class SaveKeywordRequest(BaseModel):
    keyword: str = Field(min_length=1, max_length=200)
    country: str = Field(default="in", min_length=2, max_length=8)
    language: str = Field(default="en", min_length=2, max_length=10)
    source: str | None = Field(default=None, max_length=40)


class SavedKeywordOut(BaseModel):
    id: str
    keyword: str
    country: str
    language: str
    source: str | None = None
    created_at: datetime
    search_volume: int | None = None
    cpc: float | None = None
    difficulty: float | None = None
    competition: float | None = None
    intent: str | None = None
    metrics_date: str | None = None
    rank_tracked: bool = False
    position: int | None = None
    previous_position: int | None = None
    rank_date: str | None = None


class SaveKeywordResponse(BaseModel):
    id: str
    keyword: str
    created: bool


class SavedKeywordPage(BaseModel):
    rows: list[SavedKeywordOut]
    pagination: PageInfo


@router.get("", response_model=SavedKeywordPage, summary="My Keywords (paged; sort: added|keyword|volume|cpc|difficulty|rank)")
def list_my_keywords(
    listing: PageQuery = Depends(page_query),
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> SavedKeywordPage:
    rows, total = saved_keywords_service.list_saved(
        db, ctx.domain, q=listing.search(), page=listing.page, page_size=listing.page_size,
        sort=listing.sort, descending=listing.descending)
    total_pages = max(1, -(-total // listing.page_size))
    return SavedKeywordPage(rows=[SavedKeywordOut(**r) for r in rows], pagination={
        "page": min(listing.page, total_pages), "page_size": listing.page_size, "total": total,
        "total_pages": total_pages})


@router.get("/keys", response_model=list[str], summary="Saved keywords (normalized) — to mark rows as saved")
def saved_keys(ctx: DomainContext = Depends(get_domain_context), db: Session = Depends(get_db)) -> list[str]:
    return saved_keywords_service.saved_set(db, ctx.domain)


@router.post("", response_model=SaveKeywordResponse, summary="Save a keyword (idempotent)")
def save_keyword(
    payload: SaveKeywordRequest,
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> SaveKeywordResponse:
    try:
        row, created = saved_keywords_service.save(
            db, ctx.domain, payload.keyword, country=payload.country, language=payload.language, source=payload.source)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return SaveKeywordResponse(id=str(row.id), keyword=row.keyword, created=created)


@router.delete("/{saved_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Remove a saved keyword")
def remove_keyword(
    saved_id: str,
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> None:
    if not saved_keywords_service.remove(db, ctx.domain, saved_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Saved keyword not found")
