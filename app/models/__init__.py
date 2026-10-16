"""ORM models package.

Import all models here so Alembic's autogenerate and metadata see them.
"""

from app.models.ai_overview import AiOverview
from app.models.backlink import (
    BacklinkAnchor,
    BacklinkGapDomain,
    BacklinkLink,
    BacklinkReferringDomain,
    BacklinkRun,
)
from app.models.billing import Plan, Subscription
from app.models.category import Category
from app.models.competitor import CompetitorRankSnapshot, TrackedCompetitor
from app.models.opportunity import (
    OpportunityKeyword,
    OpportunityPage,
    OpportunityTrend,
)
from app.models.content_optimization import ContentOptimization
from app.models.domain_analytics import DomainAnalyticsSnapshot
from app.models.ranked_keyword import DomainRankedKeyword, DomainRankedKeywordRun
from app.models.gsc_connection import GscConnection
from app.models.job_run import JobRun
from app.models.keyword_analysis import (
    Keyword,
    KeywordCluster,
    KeywordClusterMember,
    KeywordMetric,
    KeywordRelation,
    KeywordResearch,
    KeywordResearchResult,
    KeywordSource,
    DomainHost,
    DomainKeyword,
    SerpSnapshot,
)
from app.models.page_audit import PageAudit
from app.models.site_page import PageSearchDaily, PageSnapshot, SitePage
from app.models.saved_keyword import SavedKeyword
from app.models.ranking import RankEntry, TrackedKeyword
from app.models.refresh_token import RefreshToken
from app.models.tenant import Tenant, TenantMember
from app.models.user import User
from app.models.domain import Domain
from app.models.workspace import Workspace, WorkspaceMember

__all__ = [
    "BacklinkRun",
    "BacklinkReferringDomain",
    "BacklinkLink",
    "BacklinkAnchor",
    "BacklinkGapDomain",
    "User",
    "RefreshToken",
    "Tenant",
    "TenantMember",
    "Workspace",
    "WorkspaceMember",
    "Domain",
    "Category",
    "PageAudit",
    "SitePage",
    "PageSnapshot",
    "PageSearchDaily",
    "SavedKeyword",
    "TrackedKeyword",
    "RankEntry",
    "GscConnection",
    "JobRun",
    "Plan",
    "Subscription",
    "ContentOptimization",
    "DomainAnalyticsSnapshot",
    "DomainRankedKeywordRun",
    "DomainRankedKeyword",
    "CompetitorRankSnapshot",
    "TrackedCompetitor",
    "OpportunityPage",
    "OpportunityKeyword",
    "OpportunityTrend",
    "Keyword",
    "DomainKeyword",
    "DomainHost",
    "KeywordCluster",
    "KeywordClusterMember",
    "KeywordMetric",
    "SerpSnapshot",
    "KeywordSource",
    "KeywordRelation",
    "KeywordResearch",
    "KeywordResearchResult",
]
