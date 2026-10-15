"""Schemas for Google Ads Keyword Planner — RAW data, no scoring."""

from datetime import datetime

from pydantic import BaseModel, Field


class KeywordPlannerRequest(BaseModel):
    # A seed keyword (or comma-separated list) for mode='keyword', or a URL/domain
    # for mode='url'/'site'.
    input: str = Field(min_length=1, max_length=500)
    mode: str = Field(default="keyword", pattern="^(keyword|url|site)$")
    country: str = Field(default="us", min_length=2, max_length=2)
    language: str = Field(default="en", min_length=2, max_length=5)


class MonthlyVolume(BaseModel):
    year: int | None = None
    month: str | None = None  # e.g. "JANUARY"
    searches: int = 0


class PlannerRow(BaseModel):
    keyword: str
    avg_monthly_searches: int = 0
    competition: str | None = None  # LOW | MEDIUM | HIGH | UNSPECIFIED
    competition_index: int | None = None  # 0-100 (paid difficulty / PD)
    cpc_low: float | None = None
    cpc_high: float | None = None
    intent: str | None = None  # computed
    seo_difficulty: int | None = None  # computed 0-100
    monthly: list[MonthlyVolume] = []


class KeywordPlannerResponse(BaseModel):
    configured: bool
    # When configured is False, `message` explains how to enable it and rows is empty.
    message: str | None = None
    mode: str = "keyword"
    input: str = ""
    country: str = "us"
    language: str = "en"
    count: int = 0
    rows: list[PlannerRow] = []
    # Stored domain searches: the run id, the host, whether this came from the
    # DB (cached=True, no Google call) and when it was fetched.
    id: str | None = None
    target: str | None = None
    cached: bool = False
    captured_at: datetime | None = None
