"""Pydantic schemas for the Google Search Console integration."""

from app.schemas.pagination import PageInfo
from pydantic import BaseModel


class GscSite(BaseModel):
    site_url: str
    permission_level: str | None = None


class GscStatus(BaseModel):
    configured: bool  # server has OAuth client credentials
    connected: bool  # this workspace has an active grant
    google_email: str | None = None
    sites: list[GscSite] = []


class GscConnectRequest(BaseModel):
    # Where to send the browser after the OAuth callback completes.
    return_path: str = "/dashboard"


class GscConnectResponse(BaseModel):
    auth_url: str


class GscQueryRow(BaseModel):
    query: str
    clicks: float
    impressions: float
    ctr: float
    position: float


class GscPagePerformance(BaseModel):
    site_url: str
    page_url: str
    start_date: str
    end_date: str
    days: int
    has_data: bool
    clicks: float
    impressions: float
    ctr: float
    position: float
    queries: list[GscQueryRow]


class GscKeywordRow(BaseModel):
    keyword: str
    clicks: float
    impressions: float
    ctr: float
    position: float


class GscTrendPoint(BaseModel):
    date: str
    clicks: float
    impressions: float
    position: float


class GscDistribution(BaseModel):
    top3: int
    top10: int
    top20: int
    beyond: int


class GscKeywordPerformance(BaseModel):
    site_url: str
    start_date: str
    end_date: str
    days: int
    keyword_count: int
    total_clicks: float
    total_impressions: float
    avg_position: float
    distribution: GscDistribution
    keywords: list[GscKeywordRow]          # ONE page (see pagination)
    pagination: PageInfo | None = None
    trend: list[GscTrendPoint]
