"""Schemas for the competitors feature: current list, rank history, refresh."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel


from pydantic import Field


class CompetitorRow(BaseModel):
    competitor: str
    keywords_shared: int
    avg_position: float
    best_position: int
    keywords: list[str] = []


# ── User-editable curated competitor list ───────────────────────────────────

class CuratedCompetitor(BaseModel):
    id: str
    competitor: str
    label: str | None = None
    source: str                      # 'auto' | 'manual' | 'ai'
    reason: str | None = None        # LLM rationale (source='ai')
    confidence: str | None = None    # 'Very High'/'High'/'Medium'/'Low'
    # Latest known SERP standing (null if this competitor has no snapshot yet).
    avg_position: float | None = None
    best_position: int | None = None
    keywords_shared: int | None = None
    last_captured_on: date | None = None


class CuratedCompetitorsResponse(BaseModel):
    domain: str
    count: int
    rows: list[CuratedCompetitor]


class CompetitorCreate(BaseModel):
    domain: str = Field(..., min_length=3, max_length=255)
    label: str | None = Field(None, max_length=255)


class CompetitorDiscoverRequest(BaseModel):
    # Domain to analyze; defaults to the path domain when omitted.
    target_domain: str | None = Field(None, max_length=255)
    limit: int = Field(10, ge=1, le=20)
    # Extra services/features to add to the scraped ones (features not yet on the
    # site), to steer the competitor match. E.g. ["AI-based Mains answer evaluation"].
    extra_services: list[str] | None = Field(None, max_length=20)


class CompetitorDiscoverResponse(BaseModel):
    domain: str
    target: str
    market: str
    count: int
    competitors: list[CuratedCompetitor]


class CompetitorSuggestion(BaseModel):
    competitor: str
    keywords_shared: int
    avg_position: float
    best_position: int


class CompetitorSuggestionsResponse(BaseModel):
    domain: str
    count: int
    rows: list[CompetitorSuggestion]


class CompetitorListResponse(BaseModel):
    domain: str
    country: str
    language: str
    count: int
    rows: list[CompetitorRow]


class CompetitorHistoryPoint(BaseModel):
    captured_on: date
    avg_position: float
    best_position: int
    keywords_shared: int


class CompetitorHistorySeries(BaseModel):
    competitor: str
    points: list[CompetitorHistoryPoint]


class CompetitorHistoryResponse(BaseModel):
    domain: str
    country: str
    language: str
    # Distinct captured_on dates (ascending) across the returned series.
    dates: list[date]
    competitors: list[CompetitorHistorySeries]


class CompetitorRefreshResponse(BaseModel):
    domain: str
    captured_on: date
    competitors_tracked: int
    message: str
