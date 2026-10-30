"""Schemas for the competitors feature: current list, rank history, refresh."""

from __future__ import annotations

from datetime import date, datetime

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
    match_score: int | None = None   # 0-100 relevance (source='ai')
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
    # Re-scrape and re-extract the business profile instead of reusing the cached
    # one. Default False = reuse the stored profile (fast, no re-extraction).
    refresh: bool = False


class DiscoveryJobOut(BaseModel):
    """Live status of an async discovery run, polled by the UI for step progress."""
    job_id: str
    status: str                      # 'running' | 'done' | 'error'
    step: int                        # 0..total_steps
    step_label: str | None = None
    total_steps: int
    count: int | None = None         # competitors found (when done)
    error: str | None = None


class BusinessProfile(BaseModel):
    """The business details the LLM extracts from the site in the first pass of
    discovery (shown above the competitor list)."""
    business_description: str | None = None
    # Short commerce-model label only, e.g. "D2C e-commerce / online retail".
    industry: str | None = None
    # Short product niche / market sector, e.g. "Handcrafted brass décor & pooja".
    sector: str | None = None
    # "products" | "services" | "both" — what the site primarily offers.
    business_type: str | None = None
    # Core products that define the business (match on these first).
    primary_products: list[str] = []
    # Supporting / peripheral products (minor weight for matching).
    secondary_products: list[str] = []
    # Combined primary+secondary, de-duped (back-compat for matching/storage).
    products: list[str] = []
    # Services / offerings / features (empty for a pure product catalog).
    services: list[str] = []
    # Short distinctive angle, e.g. "Handcrafted religious & Vastu brass".
    specialties: str | None = None
    # Price tier, e.g. "premium, ~₹1,500-15,000". Separates premium makers from
    # cheap mass sellers during matching.
    price_positioning: str | None = None
    target_audience: str | None = None
    market: str | None = None


class BusinessProfileResponse(BaseModel):
    domain: str
    # The analyzed site the profile was extracted from (defaults to the domain).
    target: str | None = None
    # null when the profile hasn't been extracted yet (run discovery once).
    profile: BusinessProfile | None = None
    extracted_at: datetime | None = None


class CompetitorDiscoverResponse(BaseModel):
    domain: str
    target: str
    market: str
    count: int
    # The business profile extracted from the analyzed site (first pass).
    profile: BusinessProfile | None = None
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
