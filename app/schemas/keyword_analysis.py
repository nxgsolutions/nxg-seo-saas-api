"""Schemas for the first-party Keyword Analysis domain."""

from datetime import date, datetime

from app.schemas.pagination import PageInfo
from pydantic import BaseModel, Field


class ResearchRequest(BaseModel):
    seed: str = Field(min_length=1, max_length=200)
    # None => use the domain's resolved market/language (domain_settings), not a
    # static default, so searches run in the right market for any niche/domain.
    country: str | None = Field(default=None, min_length=2, max_length=2)
    language: str | None = Field(default=None, min_length=2, max_length=2)


class KeywordMetricOut(BaseModel):
    intent: str | None = None
    # Google Ads Keyword Planner (real market data)
    search_volume: int | None = None  # avg monthly searches
    cpc: float | None = None  # top-of-page bid
    competition: float | None = None  # competition index 0-100
    # Google Search Console (owned-domain performance)
    gsc_impressions: int | None = None
    gsc_clicks: int | None = None
    gsc_position: float | None = None
    # Computed / Trends
    trend_index: int | None = None
    trend_change_pct: float | None = None
    difficulty: float | None = None
    opportunity_score: float | None = None
    relevance_score: float | None = None
    confidence_score: float | None = None
    captured_on: date | None = None


class ResearchResultOut(BaseModel):
    rank: int
    keyword_id: str
    keyword: str
    country_code: str
    language_code: str
    # Idea categories this keyword surfaced from (autocomplete / related /
    # questions / prepositions / comparisons / gsc / keyword_planner / seed).
    sources: list[str] = []
    metric: KeywordMetricOut | None = None


class CategoryCount(BaseModel):
    name: str
    count: int


class ResearchSummary(BaseModel):
    id: str
    seed_keyword: str
    country_code: str
    language_code: str
    status: str
    created_at: datetime
    kind: str = "keyword"            # 'keyword' | 'domain'
    target: str | None = None        # domain searches: the host searched
    result_count: int = 0


class ResearchDetail(ResearchSummary):
    # The seed keyword's own metric drives the summary header.
    seed_metric: KeywordMetricOut | None = None
    questions: list[str] = []
    # Per-category counts for the Ubersuggest-style idea tabs.
    categories: list[CategoryCount] = []
    results: list[ResearchResultOut] = []


class ResearchResultsPage(BaseModel):
    """One page of a keyword search's results."""
    rows: list[ResearchResultOut]
    pagination: PageInfo


class OpportunityPoint(BaseModel):
    id: str
    keyword: str
    volume: int
    difficulty: float
    cpc: float | None = None


class OpportunityPoints(BaseModel):
    """Every plottable keyword of a search (volume > 0 and a difficulty), slim."""
    points: list[OpportunityPoint]
    skipped: int
