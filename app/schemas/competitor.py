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


class CompetitorBulkAddItem(BaseModel):
    domain: str = Field(..., min_length=3, max_length=255)
    label: str | None = Field(None, max_length=255)
    reason: str | None = Field(None, max_length=600)
    confidence: str | None = Field(None, max_length=20)
    match_score: int | None = Field(None, ge=0, le=100)


class CompetitorBulkAddRequest(BaseModel):
    """Add several competitors at once — used when the user confirms the newly
    found competitors from a "find competitors again" preview."""
    competitors: list[CompetitorBulkAddItem] = Field(..., max_length=50)


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
    # Preview mode: discover candidates WITHOUT changing the tracked list, so the
    # user can confirm which newly found competitors to merge in. Default False =
    # first-run discovery that seeds the list directly (nothing to overwrite).
    preview: bool = False


class ProposedCompetitor(BaseModel):
    """A candidate from a preview ("find competitors again") run, shown to the
    user for confirmation before it is merged into the tracked list."""
    domain: str
    name: str | None = None
    reason: str | None = None
    confidence: str | None = None
    match_score: int | None = None
    # True when this candidate is already on the user's list (shown greyed out).
    already_tracked: bool = False


class DiscoveryJobOut(BaseModel):
    """Live status of an async discovery run, polled by the UI for step progress."""
    job_id: str
    status: str                      # 'running' | 'done' | 'error'
    step: int                        # 0..total_steps
    step_label: str | None = None
    total_steps: int
    count: int | None = None         # competitors found (when done)
    error: str | None = None
    # Preview runs only: the candidates to confirm before merging (null otherwise).
    proposed: list[ProposedCompetitor] | None = None


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
    # Whole-site analytics for this period's latest fetch (from the competitor
    # analytics snapshots). A metric is null where that fetch lacked it.
    est_traffic: int | None = None
    ranking_keywords: int | None = None
    domain_rank: int | None = None
    traffic_share: float | None = None


class CompetitorHistorySeries(BaseModel):
    competitor: str
    is_you: bool = False
    points: list[CompetitorHistoryPoint]


class CompetitorHistoryResponse(BaseModel):
    domain: str
    country: str
    # Frequency the points are bucketed into ("weekly" | "monthly").
    freq: str = "weekly"
    # Selected date range the series covers.
    start: date
    end: date
    # Period-start dates (ascending) that actually have data (no zero-fill).
    dates: list[date]
    competitors: list[CompetitorHistorySeries]
    # Whether any competitor has a record in the range (else show empty state).
    has_data: bool = False


class CompetitorRefreshResponse(BaseModel):
    domain: str
    captured_on: date
    competitors_tracked: int
    message: str
