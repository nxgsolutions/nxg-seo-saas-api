"""Backlinks API schemas (stored DataForSEO Backlinks data)."""

from datetime import datetime

from pydantic import BaseModel


class BacklinkSummary(BaseModel):
    rank: int | None = None
    backlinks: int | None = None
    referring_domains: int | None = None
    dofollow_referring_domains: int | None = None
    referring_main_domains: int | None = None
    referring_ips: int | None = None
    broken_backlinks: int | None = None
    spam_score: int | None = None
    first_seen: datetime | None = None
    link_types: dict = {}
    link_attributes: dict = {}
    # Share (0-1) of the stored linking domains with spam score >= 30.
    spammy_share: float | None = None


class ReferringDomainRow(BaseModel):
    domain: str
    rank: int | None = None
    backlinks: int | None = None
    nofollow_backlinks: int | None = None
    spam_score: int | None = None
    first_seen: datetime | None = None
    lost_date: datetime | None = None


class BacklinkRow(BaseModel):
    url_from: str
    domain_from: str | None = None
    url_to: str | None = None
    anchor: str | None = None
    item_type: str | None = None
    dofollow: bool | None = None
    is_new: bool = False
    is_lost: bool = False
    is_broken: bool = False
    domain_from_rank: int | None = None
    page_from_title: str | None = None
    spam_score: int | None = None
    first_seen: datetime | None = None
    last_seen: datetime | None = None


class AnchorRow(BaseModel):
    anchor: str
    backlinks: int | None = None
    referring_domains: int | None = None
    referring_domains_nofollow: int | None = None
    spam_score: int | None = None
    first_seen: datetime | None = None


class BacklinkProfileResponse(BaseModel):
    target: str
    is_you: bool
    environment: str
    live_enabled: bool
    can_fetch: bool
    estimated_cost: float
    fetched_at: datetime | None = None
    cost: float | None = None
    requests: int | None = None
    # True when this response came from a new (possibly paid) fetch.
    fetched_now: bool = False
    summary: BacklinkSummary | None = None
    referring_domains: list[ReferringDomainRow] = []
    backlinks: list[BacklinkRow] = []
    anchors: list[AnchorRow] = []


class GapDomainRow(BaseModel):
    domain: str
    rank: int | None = None
    competitors_linking: int
    links: dict[str, int | None] = {}
    spam_score: int | None = None


class LinkGapResponse(BaseModel):
    your_domain: str
    environment: str
    live_enabled: bool
    competitors: list[str]
    run_competitors: list[str]
    competitors_changed: bool
    can_fetch: bool
    estimated_cost: float
    total_count: int | None = None
    fetched_at: datetime | None = None
    cost: float | None = None
    requests: int | None = None
    fetched_now: bool = False
    domains: list[GapDomainRow] = []


class BacklinkDomainOption(BaseModel):
    domain: str
    is_you: bool
    fetched_at: datetime | None = None


class BacklinkOptionsResponse(BaseModel):
    your_domain: str
    environment: str
    live_enabled: bool
    domains: list[BacklinkDomainOption]


class BreakdownItem(BaseModel):
    label: str
    count: int


class BacklinkBreakdowns(BaseModel):
    follow: list[BreakdownItem] = []
    link_types: list[BreakdownItem] = []
    tlds: list[BreakdownItem] = []
    countries: list[BreakdownItem] = []
    platforms: list[BreakdownItem] = []
    referring_pages: int | None = None
    crawled_pages: int | None = None


class LinkStatus(BaseModel):
    """Counts over the SAVED backlinks (one per linking domain)."""
    saved: int
    dofollow: int
    nofollow: int
    new: int
    lost: int
    broken: int


class ProfileHistoryPoint(BaseModel):
    fetched_at: datetime
    rank: int | None = None
    backlinks: int | None = None
    referring_domains: int | None = None
    spam_score: int | None = None
    cost: float | None = None


class ProfileChanges(BaseModel):
    previous_fetched_at: datetime
    backlinks: int | None = None
    referring_domains: int | None = None
    rank: int | None = None
    # Among the saved (strongest) linking domains of each fetch.
    gained_domains: list[str] = []
    lost_domains: list[str] = []


class BacklinkDetailsResponse(BaseModel):
    target: str
    breakdowns: BacklinkBreakdowns | None = None
    link_status: LinkStatus | None = None
    history: list[ProfileHistoryPoint] = []
    changes: ProfileChanges | None = None


class BacklinkRowsResponse(BaseModel):
    rows: list[dict]
    pagination: dict


# ── Analytics (Overview dashboard) ──────────────────────────────────────────

class TrendPoint(BaseModel):
    fetched_at: datetime
    rank: int | None = None
    backlinks: int | None = None
    referring_domains: int | None = None
    spam_score: int | None = None


class NewLostSummary(BaseModel):
    since: datetime | None = None
    backlinks_delta: int | None = None
    referring_domains_delta: int | None = None
    rank_delta: int | None = None
    new_links: int = 0
    lost_links: int = 0
    broken_links: int = 0
    gained_domains: list[str] = []
    lost_domains: list[str] = []


class AnchorClass(BaseModel):
    label: str
    count: int
    share: float


class AnchorMix(BaseModel):
    total: int
    classes: list[AnchorClass] = []
    # True when keyword-rich anchors dominate (advisory, not a verdict).
    over_optimized: bool = False


class DiversitySignals(BaseModel):
    referring_domains: int | None = None
    referring_ips: int | None = None
    dofollow_share: float | None = None
    spammy_share: float | None = None
    links_per_domain: float | None = None
    concentrated: bool = False
    shared_ips: bool = False
    top_tlds: list[BreakdownItem] = []
    top_countries: list[BreakdownItem] = []


class BacklinkAnalyticsResponse(BaseModel):
    target: str
    trend: list[TrendPoint] = []
    new_lost: NewLostSummary | None = None
    anchors: AnchorMix | None = None
    diversity: DiversitySignals | None = None


# ── Entity drill-down (site / page) ─────────────────────────────────────────

class _DetailMeta(BaseModel):
    environment: str
    live_enabled: bool
    can_fetch: bool
    estimated_cost: float
    fetched_at: datetime | None = None
    cost: float | None = None
    fetched_now: bool = False


class SiteDetailSummary(BaseModel):
    host: str
    authority: int | None = None
    spam_score: int | None = None
    links_to_you: int
    dofollow_links: int
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    complete: bool = False


class PresencePoint(BaseModel):
    fetched_at: datetime
    present: bool
    links: int


class ReferringSiteDetailResponse(_DetailMeta):
    target: str
    host: str
    found: bool
    summary: SiteDetailSummary | None = None
    links: list[BacklinkRow] = []
    pagination: dict = {}
    presence: list[PresencePoint] = []
    complete: bool = False


class PageDetailSummary(BaseModel):
    url: str
    status: str          # "live" | "broken"
    links: int
    linking_sites: int
    dofollow: int
    best_authority: int | None = None
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    suggested_fix: str | None = None
    complete: bool = False


class LinkedPageDetailResponse(_DetailMeta):
    target: str
    url: str
    found: bool
    summary: PageDetailSummary | None = None
    links: list[BacklinkRow] = []
    pagination: dict = {}
    complete: bool = False
