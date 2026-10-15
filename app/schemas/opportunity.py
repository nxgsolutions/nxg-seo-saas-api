"""Schemas for the Opportunity Research feature."""

from __future__ import annotations

from datetime import date, datetime

from app.schemas.pagination import PageInfo
from pydantic import BaseModel


class Kpi(BaseModel):
    label: str
    value: str
    delta: float
    up: bool


class RankBucket(BaseModel):
    label: str  # 1-3 | 4-10 | 11-20 | 21+
    count: int


class OpportunityOverviewResponse(BaseModel):
    domain: str
    kpis: list[Kpi]
    rank_distribution: list[RankBucket] = []


class TrendPoint(BaseModel):
    label: str            # e.g. "Nov '23"
    captured_on: date
    organic_traffic: int
    ranking_keywords: int
    serp_opportunities: int


class OpportunityTrendResponse(BaseModel):
    domain: str
    points: list[TrendPoint]


class CompetitorRef(BaseModel):
    domain: str
    position: int


class OpportunityKeywordRow(BaseModel):
    id: str
    keyword: str
    search_volume: int
    yoy_change: float
    keyword_difficulty: int
    current_position: int | None
    intent: str
    opportunity_score: int
    ranking_url: str | None
    page_url: str | None
    serp_features: list[str]
    competitors: list[CompetitorRef]


class OpportunityKeywordsResponse(BaseModel):
    domain: str
    total: int
    page: int
    page_size: int
    rows: list[OpportunityKeywordRow]


class PageKeywordBrief(BaseModel):
    keyword: str
    search_volume: int
    current_position: int | None
    intent: str
    opportunity_score: int
    serp_features: list[str]


class OpportunityPageRow(BaseModel):
    id: str
    url: str
    title: str | None
    keyword_count: int
    total_volume: int
    avg_position: float | None
    keywords: list[PageKeywordBrief]


class OpportunityPagesResponse(BaseModel):
    domain: str
    total: int
    rows: list[OpportunityPageRow]


class VolumePoint(BaseModel):
    month: str
    volume: int


class VolumeMover(BaseModel):
    keyword: str
    prev: int
    curr: int
    delta: int


class OpportunityChangeResponse(BaseModel):
    """Competitive Diagnosis — real month-over-month search-volume change."""
    domain: str
    series: list[VolumePoint]
    prev_label: str
    curr_label: str
    prev_volume: int
    curr_volume: int
    delta: int
    delta_pct: float
    gainers: list[VolumeMover]
    losers: list[VolumeMover]


class CompetitorFootprint(BaseModel):
    domain: str
    keywords_shared: int
    avg_position: float | None = None   # None = not seen in our SERPs yet
    best_position: int | None = None
    curated: bool = False               # in the user's own competitor list
    common_keywords: int = 0   # tracked keywords we BOTH rank for
    gap_keywords: int = 0      # tracked keywords they rank for and we don't


class OpportunityStrategyResponse(BaseModel):
    """Competitive Strategy — real competitor footprint on our keyword universe."""
    domain: str
    total_keywords: int
    our_ranked: int
    scope: str = "serp"          # "curated" (your competitor list) | "serp" (discovery)
    curated_count: int = 0       # size of your competitor list
    competitors: list[CompetitorFootprint]


class CompetitorKeywordRow(BaseModel):
    keyword: str
    their_position: int
    our_position: int | None = None  # null = we don't rank (a gap)
    search_volume: int | None = None
    is_gap: bool  # True when we don't rank for a keyword they do
    their_url: str | None = None       # their ranking page
    our_url: str | None = None
    our_position_source: str | None = None  # "serp" | "gsc"
    est_visits: float | None = None    # volume x CTR at their position
    cpc: float | None = None           # Google Ads (USD)
    paid_difficulty: int | None = None  # Ads competition index 0-100
    seo_difficulty: int | None = None   # 0-100
    intent: str | None = None


class CompetitorKeywordsResponse(BaseModel):
    """Point 3 — the keywords a competitor ranks for, among our tracked keywords,
    with their position vs ours. Covers only keywords we've fetched a SERP for."""
    domain: str
    competitor: str
    total: int
    gaps: int  # how many of these we don't rank for
    common: int = 0  # how many we both rank for
    kind: str = "all"  # filter applied to rows: all | common | gap
    rows: list[CompetitorKeywordRow]   # ONE page (see pagination)
    pagination: PageInfo | None = None


class CompetitorAnalyticsRow(BaseModel):
    domain: str
    is_you: bool = False             # our own domain (shown for comparison)
    organic_traffic: int             # estimated monthly organic visits (Labs etv)
    ranking_keywords: int            # whole-site count of keywords they rank for
    top3_keywords: int = 0
    top10_keywords: int = 0
    top20_keywords: int = 0
    new_keywords: int = 0            # newly ranking since the previous Labs update
    lost_keywords: int = 0
    traffic_value: int = 0           # est. monthly cost of that traffic as ads (USD)
    traffic_share: float             # % of organic traffic across the listed domains
    domain_rank: int | None = None   # 0-100 (DataForSEO Backlinks rank)
    backlinks: int | None = None
    referring_domains: int | None = None
    common_keywords: int | None = None  # tracked keywords we both rank for
    gap_keywords: int | None = None     # tracked keywords they rank for, we don't
    keywords_shared: int | None = None  # overlap with our tracked-keyword SERPs
    fetched_at: datetime | None = None


class CompetitorAnalyticsRefresh(BaseModel):
    fetched: int
    cached: int
    failed: list[str]
    error: str | None = None


class CompetitorAnalyticsResponse(BaseModel):
    """Whole-site competitor analytics from DataForSEO, served from Postgres."""
    domain: str
    is_sample: bool = False          # True in a sandbox workspace (dummy data)
    environment: str = "live"        # "live" | "sandbox"
    configured: bool = True
    country: str = "in"
    source: str = ""
    note: str
    fetched_at: datetime | None = None
    missing: list[str] = []          # domains with no stored metrics yet
    rows: list[CompetitorAnalyticsRow]
    refresh: CompetitorAnalyticsRefresh | None = None


class RankedKeywordRow(BaseModel):
    keyword: str
    position: int | None = None
    prev_position: int | None = None
    change: int | None = None            # + = moved up since the previous Labs update
    is_new: bool = False
    url: str | None = None
    title: str | None = None
    etv: float | None = None             # est. monthly traffic from this keyword
    search_volume: int | None = None
    cpc: float | None = None
    paid_difficulty: int | None = None   # Ads competition 0-100
    keyword_difficulty: int | None = None
    intent: str | None = None
    serp_features: list[str] = []
    our_position: int | None = None
    our_url: str | None = None
    our_position_source: str | None = None  # "ranked" | "gsc"
    is_gap: bool = False
    tracked: bool = False                # already in our tracked keyword list


class RankedKeywordsSummary(BaseModel):
    top3: int = 0
    top10: int = 0
    etv: int = 0
    common: int = 0
    gap: int = 0
    tracked: int = 0


class RankedPage(BaseModel):
    """A competitor page (ranking URL) and the keywords it ranks for."""
    url: str
    title: str | None = None
    keywords: int
    best_position: int | None = None
    etv: float = 0.0
    top_keyword: str | None = None


class RankedStoredScope(BaseModel):
    scope: str
    fetched_at: datetime
    items_count: int
    total_count: int | None = None
    cost: float | None = None


class RankedRelevantRule(BaseModel):
    regex: str
    max_position: int
    min_volume: int


class RankedKeywordsRefresh(BaseModel):
    fetched: bool                        # False = fresh stored run reused (no charge)
    cost: float | None = None


class RankedKeywordsResponse(BaseModel):
    """Keywords a domain actually ranks for (DataForSEO Labs), served from the DB."""
    domain: str
    target: str
    is_you: bool
    environment: str
    configured: bool
    can_fetch: bool
    live_allowlist: list[str]
    live_enabled: bool = True            # False = paid API switched off (kill switch)
    default_limit: int
    max_limit: int
    estimated_cost: float
    fetched_at: datetime | None = None
    total_count: int | None = None       # ALL keywords the domain ranks for (Labs)
    items_count: int = 0                 # how many of those are stored (top-N by traffic)
    cost: float | None = None            # USD charged for the stored run
    has_our_run: bool = False
    scope: str | None = None             # top | relevant | all (of the run shown)
    stored_scopes: list[RankedStoredScope] = []
    pages_fetched: int | None = None
    stop_reason: str | None = None       # complete | cap_reached | no_new_keywords
    relevant_rule: RankedRelevantRule | None = None
    max_keywords: int | None = None      # per multi-page fetch
    max_cost: float | None = None        # USD per multi-page fetch
    pages: list[RankedPage] = []         # top competitor pages by traffic
    summary: RankedKeywordsSummary
    rows: list[RankedKeywordRow]          # ONE page (see pagination)
    pagination: PageInfo | None = None
    refresh: RankedKeywordsRefresh | None = None


class RankedKeywordsEstimate(BaseModel):
    """Size + cost of a scope before buying (one 1-keyword request)."""
    target: str
    scope: str
    environment: str
    matching_count: int
    to_fetch: int
    requests: int
    estimated_cost: float
    full_cost: float
    capped: bool
    max_keywords: int
    max_cost: float
    check_cost: float
    filters: list | None = None


class RankedKeywordsStored(BaseModel):
    target: str
    fetched_at: datetime
    items_count: int
    total_count: int | None = None
    cost: float | None = None


class RankedKeywordsOptions(BaseModel):
    environment: str
    your_domain: str
    live_allowlist: list[str]
    live_enabled: bool = True
    stored: list[RankedKeywordsStored]
    suggestions: list[str]


class ActionItem(BaseModel):
    """One recommended action in the Opportunity Research action plan."""
    type: str                              # improve | gap | target
    keyword: str
    volume: int | None = None
    difficulty: int | None = None
    position: int | None = None            # your position (improve)
    url: str | None = None                 # your ranking page (improve)
    competitor: str | None = None          # best-ranking competitor (gap)
    competitor_position: int | None = None
    competitor_url: str | None = None
    competitors_count: int | None = None
    gain: int = 0                          # est. extra visits / month at the target position
    action: str | None = None
    reason: str | None = None
    source: str | None = None
    tracked: bool = False


class ActionPlanSources(BaseModel):
    search_console_keywords: int
    our_ranked_keywords: int
    competitors: list[str]
    competitors_with_rankings: list[str]


class ActionPlanResponse(BaseModel):
    domain: str
    environment: str
    generated_at: datetime
    target_position: int
    counts: dict[str, int]
    total_gain: int                        # of the top actions, excluding KD >= hard_kd
    hard_kd: int = 70
    sources: ActionPlanSources
    top: list[ActionItem]
    # Full lists are paged via /action-plan/items; empty in this response.
    improve: list[ActionItem] = []
    gaps: list[ActionItem] = []
    targets: list[ActionItem] = []


class ActionItemsResponse(BaseModel):
    """One page of an action-plan list."""
    type: str                              # improve | gap | target
    rows: list[ActionItem]
    pagination: PageInfo


class TrackKeywordResponse(BaseModel):
    keyword: str
    tracked: bool
    created: bool


class PageSnapshot(BaseModel):
    url: str
    ok: bool
    error: str | None = None
    title: str | None = None
    meta_description: str | None = None
    h1: list[str] = []
    headings: list[str] = []
    word_count: int | None = None


class PageCheck(BaseModel):
    label: str
    ok: bool
    value: str


class ContentSuggestion(BaseModel):
    priority: str        # high | medium | low
    area: str            # Title | Heading | Meta | Content | Length | Subtopic | FAQ | Page
    text: str


class PageInsightResponse(BaseModel):
    """Your page vs the keyword + competitor pages, with content suggestions."""
    keyword: str
    your_page: PageSnapshot | None = None
    page_found: bool
    checks: list[PageCheck]
    competitor_pages: list[PageSnapshot]
    target_word_count: int | None = None
    questions: list[str]
    suggestions: list[ContentSuggestion]
