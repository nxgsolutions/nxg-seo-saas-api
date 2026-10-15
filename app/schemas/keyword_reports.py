"""Schemas for keyword reports (read-only analyses over GSC + Ads data)."""

from app.schemas.pagination import PageInfo
from pydantic import BaseModel


class StrikingDistanceRow(BaseModel):
    keyword: str
    position: float  # current GSC average position
    impressions: int
    clicks: int
    ctr: float
    volume: int | None = None  # Ads avg monthly searches (real demand)
    intent: str
    opportunity: float  # demand × how close to page 1


class StrikingDistancePoint(BaseModel):
    """Slim per-keyword values for the quick-wins chart (every matching keyword)."""
    keyword: str
    position: float
    impressions: int
    volume: int | None = None
    opportunity: float


class StrikingDistanceResponse(BaseModel):
    connected: bool  # False when GSC isn't connected / has no data
    message: str | None = None
    site_url: str = ""
    days: int = 0
    count: int = 0
    rows: list[StrikingDistanceRow] = []          # ONE page (see pagination)
    pagination: PageInfo | None = None
    points: list[StrikingDistancePoint] = []      # all matching keywords, slim (chart)


class CompetitorGapRow(BaseModel):
    keyword: str
    volume: int | None = None  # Ads monthly searches
    intent: str
    difficulty: int  # estimated SEO difficulty 0-100
    your_position: float | None = None  # your GSC rank, or null = you don't rank (a gap)


class CompetitorGapResponse(BaseModel):
    configured: bool  # False when Ads isn't configured
    message: str | None = None
    competitor: str = ""
    gsc_connected: bool = False  # whether we could read your own rankings
    count: int = 0
    rows: list[CompetitorGapRow] = []


class CompetitorDomainRow(BaseModel):
    competitor: str  # normalized host, e.g. "drishtiias.com"
    keywords_shared: int  # how many of your scraped keywords they rank for
    avg_position: float  # their average SERP position across those keywords
    best_position: int  # their best (lowest) position
    keywords: list[str] = []  # sample keywords they appear for


class CompetitorDomainsResponse(BaseModel):
    domain: str = ""  # your own normalized host
    country: str = "in"
    count: int = 0
    rows: list[CompetitorDomainRow] = []
