"""Schemas for the manual SERP scraper (separate from keyword research)."""

from pydantic import BaseModel


class SerpRow(BaseModel):
    position: int
    url: str
    domain: str
    title: str | None = None


class SerpScrapeResponse(BaseModel):
    keyword: str
    country: str
    language: str
    count: int
    rows: list[SerpRow] = []


class SerpAnalyzeRow(BaseModel):
    position: int
    rank_group: int | None = None
    url: str
    domain: str
    title: str | None = None
    description: str | None = None
    breadcrumb: str | None = None
    type: str = "organic"


class SerpAnalyzeResponse(BaseModel):
    keyword: str
    country: str
    language: str
    engine: str          # 'google' (dataforseo) | 'duckduckgo' (scrape)
    source: str          # 'dataforseo' | 'scrape'
    se_results_count: int | None = None
    check_url: str | None = None
    # ISO timestamp of the stored snapshot (last updated). Null = never fetched.
    captured_at: str | None = None
    count: int
    rows: list[SerpAnalyzeRow] = []



class BreakdownMetrics(BaseModel):
    search_volume: int | None = None
    cpc: float | None = None
    difficulty: float | None = None
    competition: float | None = None
    intent: str | None = None
    metric_date: str | None = None


class BreakdownRow(SerpAnalyzeRow):
    host: str
    is_you: bool = False
    visits: int | None = None
    visits_source: str | None = None   # 'dataforseo' | 'estimate'
    authority: int | None = None       # DataForSEO domain rank (0-100 scale)
    backlinks: int | None = None
    referring_domains: int | None = None
    site_traffic: int | None = None
    site_keywords: int | None = None


class BreakdownSummary(BaseModel):
    top10_with_metrics: int = 0
    avg_authority: int | None = None
    avg_backlinks: int | None = None
    your_position: int | None = None


class KeywordBreakdownResponse(BaseModel):
    keyword: str
    country: str
    language: str
    environment: str
    metrics: BreakdownMetrics
    captured_at: str | None = None
    source: str | None = None
    se_results_count: int | None = None
    check_url: str | None = None
    summary: BreakdownSummary
    rows: list[BreakdownRow] = []
