"""Schemas for rank tracking."""

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator


class TrackedKeywordCreate(BaseModel):
    term: str = Field(min_length=1, max_length=200)

    @field_validator("term")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Keyword is required")
        return value


class RankEntryCreate(BaseModel):
    # 1..1000; null means "not ranking in the tracked range".
    position: int | None = Field(default=None, ge=1, le=1000)
    recorded_at: date | None = None


class RankEntryResponse(BaseModel):
    id: uuid.UUID
    position: int | None
    recorded_at: date


class TrackedKeywordResponse(BaseModel):
    id: uuid.UUID
    term: str
    latest_position: int | None
    previous_position: int | None
    best_position: int | None
    entry_count: int
    created_at: datetime
    # Market signals from the shared keyword library (None when not known yet).
    search_volume: int | None = None
    difficulty: float | None = None
    intent: str | None = None
    # Automated-tracking status.
    tracking_status: str = "idle"
    tracking_enabled: bool = True
    tracking_frequency_days: int = 15
    last_fetched_at: datetime | None = None
    next_fetch_at: datetime | None = None


class TrackedKeywordUpdate(BaseModel):
    """Change a keyword's tracking cadence / on-off."""

    tracking_frequency_days: int | None = Field(default=None)
    tracking_enabled: bool | None = None

    @field_validator("tracking_frequency_days")
    @classmethod
    def _freq(cls, value: int | None) -> int | None:
        # 7 / 14 / 30 are the UI options (Every 1 week / 2 weeks / 1 month). 15 is
        # tolerated as a legacy value (older rows default to it) but not offered.
        if value is not None and value not in (7, 14, 15, 30):
            raise ValueError("tracking_frequency_days must be 7, 14 or 30")
        return value


class RankHistoryPoint(BaseModel):
    date: str
    # float so the aggregated timeline can carry averaged positions (e.g. 4.5);
    # per-keyword history uses whole numbers.
    position: float | None
    url: str | None = None


class RankSeries(BaseModel):
    host: str
    points: list[RankHistoryPoint]


class RankHistoryMeta(BaseModel):
    current: int | None
    previous: int | None
    change: int | None
    best: int | None
    last_tracked: str | None
    last_fetched_at: str | None
    next_fetch_at: str | None
    tracking_frequency_days: int
    tracking_status: str
    tracking_enabled: bool


class RankHistoryResponse(BaseModel):
    keyword: str
    you_host: str
    dates: list[str]
    you: RankSeries
    competitors: list[RankSeries]
    meta: RankHistoryMeta


class RankTimelineResponse(BaseModel):
    """Aggregated competitor rank over time across tracked keywords."""

    label: str
    you_host: str
    freq: str
    dates: list[str]
    you: RankSeries
    competitors: list[RankSeries]
    all_competitors: list[str]


class KeywordReportingPeriod(BaseModel):
    date: str
    page1: int  # ranked 1-10
    page2: int  # ranked 11-20
    page3: int  # ranked 21-30
    page4plus: int  # ranked 31-100 (present in the top 100, beyond page 3)
    total: int  # page1+page2+page3+page4plus (ranked anywhere in the top 100)
    not_ranked: int  # fetched but our host not in the top 100


class KeywordReportingResponse(BaseModel):
    freq: str
    dates: list[str]
    periods: list[KeywordReportingPeriod]


class ReportingLocation(BaseModel):
    code: int
    name: str


class ReportingFacetsResponse(BaseModel):
    """Distinct device + location values present in this domain's stored SERP
    snapshots — drives the Keyword Reporting filters (a selector is only shown
    when more than one value exists)."""

    devices: list[str]
    locations: list[ReportingLocation]


class TrackNowResponse(BaseModel):
    term: str
    our_position: int | None = None
    recorded_at: str | None = None
    results: int = 0
    competitors: int = 0
    reused: bool = False
    skipped: str | None = None


class RunDueResponse(BaseModel):
    due: int
    fetched: int
    failed: int
    skipped_live: int
    aborted: str | None = None


class RankSyncResponse(BaseModel):
    synced: int
    total: int
    recorded_at: date


class RankImportResponse(BaseModel):
    """Result of importing top Search Console queries as tracked keywords."""

    imported: int  # new keywords added to tracking
    synced: int  # tracked keywords given a position from the GSC data
    total: int  # tracked-keyword count after the import
    recorded_at: date
