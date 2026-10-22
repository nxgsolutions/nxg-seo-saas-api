"""Keyword Analysis orchestration — first-party pipeline, no paid provider.

Ties the free signal sources together into a persisted research run:

    seed ─┬─► Google Autocomplete (breadth)      ─┐
          ├─► GSC queries (real demand / position) ├─► upsert keywords + sources
          └─► seed itself                          ─┘
                                                    │
                          keyword_scoring (pure)  ◄─┘  intent / difficulty /
                                                       opportunity / relevance
                                                    │
                            ranked ─► keyword_research_results + daily metrics

Every source that surfaces a keyword leaves a `keyword_sources` row, so each
keyword's provenance is auditable. GSC is best-effort: if the workspace hasn't
connected it, the run still completes on autocomplete + seed.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone

from sqlalchemy import and_, exists, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.keyword_analysis import (
    Keyword,
    KeywordMetric,
    KeywordRelation,
    KeywordResearch,
    KeywordResearchResult,
    KeywordSource,
    DomainKeyword,
    SerpSnapshot,
    SerpSnapshotItem,
)
from app.models.category import Category
from app.models.domain import Domain
from app.models.saved_keyword import SavedKeyword
from app.models.workspace import Workspace
from app.core.config import settings
from app.db.session import SessionLocal
from app.services import (
    content_service,
    data_env,
    dataforseo_client,
    domain_analytics_service,
    domain_settings,
    gsc_service,
    keyword_planner_service,
    keyword_scoring,
    keyword_suggest,
    keyword_trends,
)
from app.services.gsc_service import (
    GscApiError,
    GscNotConfiguredError,
    GscNotConnectedError,
    GscPropertyError,
)

# How many autocomplete candidates to keep per run (keeps runs bounded/cheap).
_MAX_CANDIDATES = 100

logger = logging.getLogger(__name__)


def _upsert_keyword(db: Session, keyword: str) -> Keyword:
    """Find-or-create the GLOBAL canonical keyword via INSERT ... ON CONFLICT.

    RETURNING yields the row even on conflict (do_update), so two concurrent
    inserts of the same normalized keyword can never race or raise."""
    normalized = keyword_scoring.normalize_keyword(keyword)
    stmt = (
        pg_insert(Keyword)
        .values(keyword=keyword.strip(), normalized_keyword=normalized)
        .on_conflict_do_update(
            index_elements=["normalized_keyword"],
            set_={"updated_at": func.now()},
        )
        .returning(Keyword.id)
    )
    keyword_id = db.execute(stmt).scalar_one()
    return db.get(Keyword, keyword_id)


def _upsert_domain_keyword(
    db: Session, domain: Domain, keyword: Keyword, country: str, language: str
) -> DomainKeyword:
    """Link a global keyword to a domain at a locale (per-domain targeting).

    The link inherits the domain's category so SERP-derived competitors can be
    scoped to one industry; kept in sync on conflict if the domain's category
    changed since the link was first written."""
    stmt = (
        pg_insert(DomainKeyword)
        .values(
            domain_id=domain.id,
            keyword_id=keyword.id,
            country_code=country,
            language_code=language,
            category_id=domain.category_id,
        )
        .on_conflict_do_update(
            constraint="uq_domain_keyword",
            set_={"category_id": domain.category_id},
        )
        .returning(DomainKeyword.id)
    )
    dk_id = db.execute(stmt).scalar_one()
    return db.get(DomainKeyword, dk_id)


def _upsert_relation(
    db: Session, keyword: Keyword, related: Keyword, relation_type: str
) -> None:
    """Create a keyword→keyword link if it doesn't already exist."""
    exists = db.execute(
        select(KeywordRelation.id).where(
            KeywordRelation.keyword_id == keyword.id,
            KeywordRelation.related_keyword_id == related.id,
            KeywordRelation.relation_type == relation_type,
        )
    ).scalar_one_or_none()
    if exists is None:
        db.add(
            KeywordRelation(
                keyword_id=keyword.id,
                related_keyword_id=related.id,
                relation_type=relation_type,
            )
        )


def _add_source(db: Session, keyword: Keyword, source_type: str, reference: str | None) -> None:
    db.add(
        KeywordSource(
            keyword_id=keyword.id, source_type=source_type, source_reference=reference
        )
    )


def _upsert_metric_snapshot(
    db: Session,
    keyword: Keyword,
    *,
    seed: str,
    country: str,
    language: str,
    gsc: dict | None,
    source_count: int,
    trend: keyword_trends.TrendResult | None = None,
    planner: dict | None = None,
) -> KeywordMetric:
    """Write (or update) today's metric snapshot for a keyword.

    Keyed by (keyword, locale, engine, device, day). GSC fills impressions/clicks/
    position (owned domain); Google Ads Keyword Planner fills search_volume/cpc/
    competition (market data). Both are optional and independent."""
    today = date.today()
    # Race-safe: reserve the (keyword, locale, engine, device, day) row if absent,
    # then load the managed instance and assign the computed columns below.
    db.execute(
        pg_insert(KeywordMetric)
        .values(
            keyword_id=keyword.id,
            country_code=country,
            language_code=language,
            search_engine="google",
            device="desktop",
            metric_date=today,
        )
        .on_conflict_do_nothing(constraint="uq_keyword_metric")
    )
    metric = db.execute(
        select(KeywordMetric).where(
            KeywordMetric.keyword_id == keyword.id,
            KeywordMetric.country_code == country,
            KeywordMetric.language_code == language,
            KeywordMetric.search_engine == "google",
            KeywordMetric.device == "desktop",
            KeywordMetric.metric_date == today,
        )
    ).scalar_one()

    impressions = gsc.get("impressions") if gsc else None
    clicks = gsc.get("clicks") if gsc else None
    position = gsc.get("position") if gsc else None

    # Difficulty: a word-count heuristic for now (no SERP crawl yet), so the KD
    # column is populated but honestly low-confidence.
    difficulty = keyword_scoring.heuristic_difficulty(keyword.keyword)

    metric.intent = keyword_scoring.classify_intent(keyword.keyword)
    # Only a caller WITH GSC data may set these — a domain/Ads-only search must
    # not erase the GSC numbers another run stored for today.
    if gsc is not None:
        metric.gsc_impressions = impressions
        metric.gsc_clicks = clicks
        metric.gsc_position = position
    else:
        impressions = metric.gsc_impressions
        position = metric.gsc_position
    metric.trend_index = trend.index if trend else None
    metric.trend_change_pct = trend.change_pct if trend else None
    metric.difficulty = difficulty
    metric.relevance_score = keyword_scoring.relevance_score(keyword.keyword, seed)

    # Real market data from Google Ads Keyword Planner (when configured).
    if planner is not None:
        metric.search_volume = planner.get("avg_monthly_searches")
        # Store a single representative CPC (top-of-page bid, high end).
        metric.cpc = planner.get("cpc_high") or planner.get("cpc_low")
        # competition_index is Google's 0-100 numeric; keep it in `competition`.
        metric.competition = planner.get("competition_index")

    # Opportunity blends real demand (GSC impressions or Ads volume) with position
    # and difficulty.
    demand = impressions if impressions is not None else metric.search_volume
    metric.opportunity_score = keyword_scoring.opportunity_score(
        demand, position, difficulty
    )
    metric.confidence_score = keyword_scoring.confidence_score(
        source_count, has_gsc=gsc is not None
    )
    return metric


def _planner_keywords(
    keywords: list[str], country: str, language: str
) -> dict[str, dict]:
    """EXACT Ads metrics (volume/cpc/competition) for the given keyword list, keyed
    by normalized keyword. Uses GenerateKeywordHistoricalMetrics so every keyword we
    show gets real numbers. Empty when Ads isn't configured or the call fails."""
    if not settings.google_ads_configured:
        return {}
    kws = [k for k in dict.fromkeys(k.strip() for k in keywords) if k]
    if not kws:
        return {}
    try:
        rows = keyword_planner_service.historical_metrics(
            kws, country=country, language=language
        )
    except keyword_planner_service.PlannerError as exc:
        # Surface the real cause (e.g. deprecated API version, access level) in the
        # logs — the UI only gets a neutral "couldn't load" state, never this text.
        logger.warning("Keyword Planner metrics unavailable: %s", exc)
        return {}
    except Exception:  # noqa: BLE001 — market data is best-effort; never fail the run
        logger.exception("Keyword Planner metrics failed; continuing without them")
        return {}
    return {keyword_scoring.normalize_keyword(r["keyword"]): r for r in rows}


def _gsc_keywords(
    db: Session, workspace: Workspace, domain: Domain
) -> dict[str, dict]:
    """GSC ranking keywords keyed by normalized query. Empty if GSC unavailable."""
    try:
        data = gsc_service.keyword_performance(db, workspace, domain)
    except (GscNotConfiguredError, GscNotConnectedError, GscPropertyError, GscApiError):
        return {}
    out: dict[str, dict] = {}
    for row in data.get("keywords", []):
        out[keyword_scoring.normalize_keyword(row["keyword"])] = row
    return out


def run_research(
    db: Session,
    workspace: Workspace,
    domain: Domain | None = None,
    *,
    seed: str,
    country: str = "us",
    language: str = "en",
) -> KeywordResearch:
    """Run a keyword search from a seed and persist it.

    `domain` is optional: with a domain the run adds GSC (owned-domain rankings)
    and a domain_keywords association; without one it's a workspace-scoped
    keyword-only search that still grows the global keywords + keyword_metrics."""
    # The seed is a global keyword; the run points at it by FK.
    seed_keyword = _upsert_keyword(db, seed)
    research = KeywordResearch(
        workspace_id=workspace.id,
        domain_id=domain.id if domain else None,
        seed_keyword_id=seed_keyword.id,
        country_code=country,
        language_code=language,
        status="running",
    )
    db.add(research)
    db.flush()

    seed_norm = keyword_scoring.normalize_keyword(seed)

    # 1. Gather candidates from all sources.
    gsc_map = _gsc_keywords(db, workspace, domain) if domain else {}  # owned-domain rank
    # Categorized Autocomplete expansion (the Ubersuggest idea tabs).
    idea_buckets = keyword_suggest.ideas(seed, country, language)
    question_norms = {
        keyword_scoring.normalize_keyword(q) for q in idea_buckets.get("questions", [])
    }
    # Google Trends for the seed only (one call — keeps us well under rate limits).
    seed_trend = keyword_trends.interest(seed, country, language)

    # candidate -> set of source_types that surfaced it (the category is the source).
    candidates: dict[str, set[str]] = {}

    def _note(term: str, source: str) -> None:
        norm = keyword_scoring.normalize_keyword(term)
        if not norm:
            return
        candidates.setdefault(norm, set()).add(source)

    _note(seed, "seed")
    # Each idea category becomes a source_type so the UI can rebuild the tabs.
    for category in ("autocomplete", "related", "questions", "prepositions", "comparisons"):
        for term in idea_buckets.get(category, []):
            _note(term, category)
    for norm in gsc_map:
        _note(norm, "gsc")

    # Bound the run; seed and GSC keywords are always kept, then autocomplete.
    if len(candidates) > _MAX_CANDIDATES:
        prioritized = {
            k: v for k, v in candidates.items() if "seed" in v or "gsc" in v
        }
        for k, v in candidates.items():
            if len(prioritized) >= _MAX_CANDIDATES:
                break
            prioritized.setdefault(k, v)
        candidates = prioritized

    # Fetch EXACT Ads metrics (volume/cpc/competition) for exactly the keywords we
    # will show — so every row gets real numbers, not just seed-expansion overlaps.
    planner_map = _planner_keywords(list(candidates.keys()), country, language)

    # 2. Persist keywords, provenance and today's metric snapshot.
    scored: list[tuple[float, Keyword]] = []
    keywords_by_norm: dict[str, Keyword] = {}
    for norm, sources in candidates.items():
        # Prefer real (GSC / Ads) keyword casing when available, else seed casing.
        display = (
            gsc_map[norm]["keyword"] if norm in gsc_map
            else planner_map[norm]["keyword"] if norm in planner_map
            else norm
        )
        keyword = _upsert_keyword(db, display)
        if domain is not None:
            _upsert_domain_keyword(db, domain, keyword, country, language)
        keywords_by_norm[norm] = keyword
        for source in sorted(sources):
            _add_source(db, keyword, source, seed if source == "seed" else None)
        metric = _upsert_metric_snapshot(
            db,
            keyword,
            seed=seed,
            country=country,
            language=language,
            gsc=gsc_map.get(norm),
            source_count=len(sources),
            trend=seed_trend if norm == seed_norm else None,
            planner=planner_map.get(norm),
        )
        scored.append((float(metric.opportunity_score or 0.0), keyword))

    # Link mined questions to the seed keyword (relation_type='question') so the
    # Questions panel can be rebuilt from stored data.
    seed_keyword = keywords_by_norm.get(seed_norm)
    if seed_keyword is not None:
        for qnorm in question_norms:
            qkw = keywords_by_norm.get(qnorm)
            if qkw is None or qkw.id == seed_keyword.id:
                continue
            _upsert_relation(db, seed_keyword, qkw, "question")

    # 3. Rank by opportunity and persist the ordered result set.
    scored.sort(key=lambda pair: pair[0], reverse=True)
    for rank, (_score, keyword) in enumerate(scored, start=1):
        db.add(
            KeywordResearchResult(
                research_id=research.id, keyword_id=keyword.id, rank=rank
            )
        )

    research.status = "completed"
    research.completed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(research)
    return research


def snapshot_seed_serp(
    research_id: str, *, seed: str, country: str, language: str
) -> None:
    """Background job: fetch the seed's organic SERP from DataForSEO (the ONLY
    DataForSEO use) and store it as a SerpSnapshot(source='dataforseo').

    Runs after run_research returns, so the interactive response stays fast and
    the slow provider call doesn't block the request. Best-effort: a failure is
    logged, never surfaced. Opens its own DB session."""
    if not dataforseo_client.is_configured():
        return
    location_name = domain_analytics_service.location_name_for(country)
    db = SessionLocal()
    try:
        research = db.get(KeywordResearch, research_id)
        if research is None:
            return
        env = data_env.env_for_workspace_id(db, research.workspace_id)
        serp = dataforseo_client.serp_organic(
            seed, location_name=location_name, language_code=language, depth=20, env=env
        )
        if not serp:
            return
        snapshot = SerpSnapshot(
            keyword_id=research.seed_keyword_id, search_engine="google",
            source="dataforseo", environment=env, device="desktop", country_code=country,
            language_code=language, items_count=len(serp), status="ok",
        )
        snapshot.items = [
            SerpSnapshotItem(
                type="organic", rank_absolute=hit["position"],
                rank_group=hit["position"], url=hit["url"],
                domain=hit["domain"] or "", title=hit.get("title"),
            )
            for hit in serp
        ]
        db.add(snapshot)
        # Upgrade the seed's difficulty from this real SERP (replaces the
        # word-count heuristic with an actual contested-SERP measure).
        _apply_serp_difficulty(
            db, research.seed_keyword_id, seed, serp,
            country=country, language=language,
        )
        db.commit()
    except Exception:  # noqa: BLE001 - background job must not crash
        db.rollback()
        logger.exception("SERP snapshot for research %s failed", research_id)
    finally:
        db.close()


def _apply_serp_difficulty(
    db: Session, keyword_id, seed: str, serp: list[dict], *, country: str, language: str
) -> None:
    """Compute a SERP-based difficulty for the seed from the top results and write
    it onto today's metric (with a recomputed opportunity_score). No-op when no
    metric row exists for the seed+locale. Keeps the heuristic as a fallback for
    every other keyword that has no SERP yet."""
    top = serp[:10]
    if not top:
        return
    seed_norm = keyword_scoring.normalize_keyword(seed)
    distinct_domains = len({(h.get("domain") or "").lower() for h in top if h.get("domain")})
    exact_title_matches = sum(
        1 for h in top if seed_norm and seed_norm in (h.get("title") or "").lower()
    )
    difficulty = keyword_scoring.estimate_difficulty(
        results_count=None,
        exact_title_matches=exact_title_matches,
        distinct_domains=distinct_domains,
    )
    if difficulty is None:
        return
    metric = db.execute(
        select(KeywordMetric)
        .where(
            KeywordMetric.keyword_id == keyword_id,
            KeywordMetric.country_code == country,
            KeywordMetric.language_code == language,
            KeywordMetric.search_engine == "google",
            KeywordMetric.device == "desktop",
        )
        .order_by(KeywordMetric.metric_date.desc())
        .limit(1)
    ).scalar_one_or_none()
    if metric is None:
        return
    metric.difficulty = difficulty
    demand = metric.gsc_impressions if metric.gsc_impressions is not None else metric.search_volume
    position = float(metric.gsc_position) if metric.gsc_position is not None else None
    metric.opportunity_score = keyword_scoring.opportunity_score(demand, position, difficulty)


def _norm_host(value: str) -> str:
    host = re.sub(r"^https?://", "", value.strip(), flags=re.I).split("/")[0].lower()
    return host[4:] if host.startswith("www.") else host


def _add_source_once(db: Session, keyword: Keyword, source_type: str, reference: str) -> None:
    exists = db.execute(
        select(KeywordSource.id).where(
            KeywordSource.keyword_id == keyword.id,
            KeywordSource.source_type == source_type,
            KeywordSource.source_reference == reference,
        )
    ).scalar_one_or_none()
    if exists is None:
        db.add(
            KeywordSource(keyword_id=keyword.id, source_type=source_type, source_reference=reference)
        )


# Generic app/social/nav words that pollute keyword seeds (they come from a site's
# footer/download links, not its topic) — a phrase containing any is dropped.
_SEED_STOPWORDS = {
    "app", "apps", "download", "downloads", "telegram", "whatsapp", "facebook",
    "instagram", "youtube", "twitter", "gmail", "google", "play", "store",
    "playstore", "appstore", "login", "signin", "signup", "register", "subscribe",
    "home", "page", "click", "menu", "share", "follow", "email", "contact", "chat",
}


def _site_topic_seeds(website_input: str) -> list[str]:
    """Crawl the site's homepage and return its top TOPICAL phrases — the reliable
    signal of what the site is about (Google's site/url seed guesses wrong for
    lesser-known domains). Uses multi-word phrases only and filters generic app/nav
    words, so footer 'download app / telegram' links don't leak in. Best-effort."""
    url = website_input if website_input.lower().startswith("http") else f"https://{website_input}"
    try:
        analysis = content_service.run_analysis(url)
    except Exception:  # noqa: BLE001 — any crawl/parse failure just skips this path
        return []
    phrases: list[str] = []
    # Bigrams + trigrams only (single words are too broad and pull in junk).
    for p in analysis.trigrams[:6] + analysis.bigrams[:8]:
        text = str(p.get("phrase", "")).strip().lower()
        words = text.split()
        if not text or any(w in _SEED_STOPWORDS for w in words):
            continue
        phrases.append(text)
    return list(dict.fromkeys(phrases))[:12]


def _domain_payload(research: KeywordResearch, rows: list[dict], *, cached: bool) -> dict:
    """KeywordPlannerResponse-shaped dict for a stored/fresh domain search."""
    return {
        "configured": True,
        "id": str(research.id),
        "mode": research.mode or "site",
        "input": research.target or "",
        "target": research.target,
        "country": research.country_code,
        "language": research.language_code,
        "count": len(rows),
        "rows": rows,
        "cached": cached,
        "captured_at": research.completed_at or research.created_at,
    }


def domain_search_rows(db: Session, research: KeywordResearch) -> list[dict]:
    """The stored rows of a domain search, exactly as Google Ads returned them."""
    results = db.execute(
        select(KeywordResearchResult, Keyword.keyword)
        .join(Keyword, Keyword.id == KeywordResearchResult.keyword_id)
        .where(KeywordResearchResult.research_id == research.id)
        .order_by(KeywordResearchResult.rank)
    ).all()
    return [
        {
            "keyword": kw,
            "avg_monthly_searches": int(r.search_volume or 0),
            "competition": r.competition,
            "competition_index": r.competition_index,
            "cpc_low": float(r.cpc_low) if r.cpc_low is not None else None,
            "cpc_high": float(r.cpc_high) if r.cpc_high is not None else None,
            "intent": r.intent,
            "seo_difficulty": r.seo_difficulty,
            "monthly": r.monthly or [],
        }
        for r, kw in results
    ]


def latest_domain_search(
    db: Session, workspace: Workspace, target: str, *, country: str, language: str
) -> KeywordResearch | None:
    """Newest completed, non-empty domain search for this host + locale."""
    return db.execute(
        select(KeywordResearch)
        .where(
            KeywordResearch.workspace_id == workspace.id,
            KeywordResearch.kind == "domain",
            KeywordResearch.target == target,
            KeywordResearch.country_code == country,
            KeywordResearch.language_code == language,
            KeywordResearch.status == "completed",
            KeywordResearch.result_count > 0,
        )
        .order_by(KeywordResearch.created_at.desc(),
                  KeywordResearch.completed_at.desc().nulls_last())
        .limit(1)
    ).scalar_one_or_none()


def get_domain_search(db: Session, workspace: Workspace, research_id: str) -> dict | None:
    """Reopen a stored domain search — DB only, no Google Ads call."""
    research = get_research(db, workspace, research_id)
    if research is None or research.kind != "domain":
        return None
    return _domain_payload(research, domain_search_rows(db, research), cached=True)


def run_website_search(
    db: Session,
    workspace: Workspace,
    website_input: str,
    *,
    country: str = "us",
    language: str = "en",
    refresh: bool = False,
) -> dict:
    """'Search by website': the keywords a domain is about, from Google Ads
    Keyword Planner (free), stored so the search can be reopened.

    Without `refresh`, the newest stored search for the same host + locale is
    returned from the DB (no Google call). Otherwise Google is queried and a NEW
    run is recorded (history kept): the research row (kind='domain', target=host,
    linked to a managed domain when the host matches one), one result row per
    keyword with the exact numbers returned, plus the global keyword tables
    (keywords, keyword_metrics, keyword_sources, domain_keywords).

    Relevance strategy: crawl the homepage for its real topic phrases and seed the
    Keyword Planner with those; fall back to Google's site/url seed if the crawl
    fails."""
    inp = website_input.strip()
    host = _norm_host(inp)

    if not refresh:
        stored = latest_domain_search(db, workspace, host, country=country, language=language)
        if stored is not None:
            return _domain_payload(stored, domain_search_rows(db, stored), cached=True)

    seeds = _site_topic_seeds(inp)
    if seeds:
        # Content-driven: relevant keywords from what the site is actually about.
        data = keyword_planner_service.generate_ideas(
            ", ".join(seeds), mode="keyword", country=country, language=language
        )
        mode = "site"
    else:
        # Fallback: Google's own site/url association.
        stripped = re.sub(r"^https?://", "", inp, flags=re.I)
        mode = "url" if re.search(r"/[^/]", stripped) else "site"
        data = keyword_planner_service.generate_ideas(
            inp, mode=mode, country=country, language=language
        )

    # Link to a managed domain in this workspace if the host matches one.
    match: Domain | None = None
    for d in db.execute(
        select(Domain).where(Domain.workspace_id == workspace.id)
    ).scalars():
        if _norm_host(d.domain) == host:
            match = d
            break

    research = KeywordResearch(
        workspace_id=workspace.id,
        domain_id=match.id if match else None,
        seed_keyword_id=_upsert_keyword(db, host).id,
        country_code=country,
        language_code=language,
        status="completed",
        kind="domain",
        target=host,
        mode=mode,
        source="google_ads",
        completed_at=datetime.now(timezone.utc),
    )
    db.add(research)
    db.flush()

    # Highest demand first; one row per distinct keyword.
    planner_rows = sorted(
        (r for r in data.get("rows", []) if r.get("keyword")),
        key=lambda r: r.get("avg_monthly_searches") or 0, reverse=True,
    )
    rows: list[dict] = []
    seen: set = set()
    for row in planner_rows:
        keyword = _upsert_keyword(db, row["keyword"])
        if keyword.id in seen:
            continue
        seen.add(keyword.id)
        # Enrich each row with computed intent + SEO difficulty (for the UI columns).
        row["intent"] = keyword_scoring.classify_intent(row["keyword"])
        row["seo_difficulty"] = keyword_scoring.heuristic_difficulty(row["keyword"])
        if match is not None:
            _upsert_domain_keyword(db, match, keyword, country, language)
        _add_source_once(db, keyword, mode, host)
        _upsert_metric_snapshot(
            db, keyword, seed=inp, country=country, language=language,
            gsc=None, source_count=1, planner=row,
        )
        db.add(KeywordResearchResult(
            research_id=research.id, keyword_id=keyword.id, rank=len(rows) + 1,
            search_volume=row.get("avg_monthly_searches"),
            competition=row.get("competition"),
            competition_index=row.get("competition_index"),
            cpc_low=row.get("cpc_low"), cpc_high=row.get("cpc_high"),
            intent=row["intent"], seo_difficulty=row["seo_difficulty"],
            monthly=row.get("monthly") or [],
        ))
        rows.append(row)
    research.result_count = len(rows)
    db.commit()
    db.refresh(research)
    return _domain_payload(research, rows, cached=False)


_OPERATOR_RE = re.compile(r"""[()"'|]|(?:^|\s)-?(?:site|intitle|inurl|intext|filetype|source):""", re.I)


def _is_operator_query(keyword: str) -> bool:
    """GSC surfaces scraper/operator queries (site:, -site:, quotes, boolean AND/OR
    with parens). These aren't real keyword targets."""
    kw = keyword.lower()
    if _OPERATOR_RE.search(kw):
        return True
    words = kw.split()
    if len(words) > 10:  # real search queries are rarely this long
        return True
    if " or " in f" {kw} " and " and " in f" {kw} ":
        return True
    return False


def striking_distance(
    db: Session,
    workspace: Workspace,
    domain: Domain,
    *,
    days: int = 90,
    country: str = "in",
    language: str = "en",
    min_impressions: int = 1,
    min_volume: int = 0,
    min_position: float = 4.0,
    max_position: float = 20.0,
) -> dict:
    """Keywords the domain ALREADY ranks #4-20 for (GSC), enriched with real Ads
    search volume — the 'almost page one' quick wins. Read-only; persists nothing.

    Opportunity = real demand (Ads volume, else GSC impressions) weighted by how
    close the keyword is to page one (positions 11-20 have the biggest upside)."""
    try:
        gsc = gsc_service.keyword_performance(db, workspace, domain, days=days)
    except (GscNotConfiguredError, GscNotConnectedError, GscPropertyError, GscApiError) as exc:
        return {"connected": False, "message": str(exc), "rows": []}

    cands = [
        k for k in gsc.get("keywords", [])
        if min_position <= k["position"] <= max_position
        and k["impressions"] >= min_impressions
        and not _is_operator_query(k["keyword"])  # drop scraper/operator queries
    ]
    planner = _planner_keywords([k["keyword"] for k in cands], country, language)

    rows = []
    for k in cands:
        metric = planner.get(keyword_scoring.normalize_keyword(k["keyword"]))
        volume = metric.get("avg_monthly_searches") if metric else None
        # Require real Ads demand when a min_volume is set — cuts the no-demand noise.
        if min_volume and (volume is None or volume < min_volume):
            continue
        demand = volume if volume else k["impressions"]
        # Positions 11-20 (page 2) have the most upside; 4-10 are already close.
        pos_factor = 1.0 if k["position"] > 10 else 0.7
        rows.append({
            "keyword": k["keyword"],
            "position": round(k["position"], 1),
            "impressions": int(k["impressions"]),
            "clicks": int(k["clicks"]),
            "ctr": round(k["ctr"], 4),
            "volume": volume,
            "intent": keyword_scoring.classify_intent(k["keyword"]),
            "opportunity": round((demand or 0) * pos_factor, 1),
        })
    rows.sort(key=lambda r: r["opportunity"], reverse=True)
    return {
        "connected": True,
        "site_url": gsc.get("site_url", ""),
        "days": days,
        "count": len(rows),
        "rows": rows,
    }


def competitor_gap(
    db: Session,
    workspace: Workspace,
    domain: Domain,
    competitor: str,
    *,
    country: str = "in",
    language: str = "en",
    min_volume: int = 10,
    gaps_only: bool = False,
) -> dict:
    """Keywords a COMPETITOR is associated with (Ads, seeded from their homepage
    topics) cross-referenced with YOUR rankings (GSC). `your_position` is null when
    you don't rank for it = a content gap to target. Read-only."""
    comp = competitor.strip()

    # Competitor brand tokens to exclude (you can't rank for their brand).
    sld = _norm_host(comp).split(".")[0]
    brand = {sld}
    for suf in ("ias", "upsc", "academy", "institute", "classes", "online", "edu", "app"):
        if sld.endswith(suf) and len(sld) > len(suf) + 2:
            brand.add(sld[: -len(suf)])

    # 1. Competitor's keywords (relevant — seeded from their homepage content).
    seeds = _site_topic_seeds(comp)
    if seeds:
        data = keyword_planner_service.generate_ideas(
            ", ".join(seeds), mode="keyword", country=country, language=language
        )
    else:
        stripped = re.sub(r"^https?://", "", comp, flags=re.I)
        mode = "url" if re.search(r"/[^/]", stripped) else "site"
        data = keyword_planner_service.generate_ideas(comp, mode=mode, country=country, language=language)

    # 2. Your own rankings (GSC), keyed by normalized keyword.
    gsc_map: dict[str, float] = {}
    gsc_connected = False
    try:
        g = gsc_service.keyword_performance(db, workspace, domain, days=90)
        gsc_map = {
            keyword_scoring.normalize_keyword(k["keyword"]): k["position"]
            for k in g.get("keywords", [])
        }
        gsc_connected = True
    except (GscNotConfiguredError, GscNotConnectedError, GscPropertyError, GscApiError):
        gsc_connected = False

    # 3. Build gap rows.
    rows = []
    seen: set[str] = set()
    for r in data.get("rows", []):
        kw = r.get("keyword", "")
        norm = keyword_scoring.normalize_keyword(kw)
        if not kw or norm in seen or _is_operator_query(kw):
            continue
        # Skip the competitor's own brand terms.
        if brand & set(norm.split()):
            continue
        seen.add(norm)
        volume = r.get("avg_monthly_searches")
        if min_volume and (volume is None or volume < min_volume):
            continue
        pos = gsc_map.get(norm)
        if gaps_only and pos is not None:
            continue
        rows.append({
            "keyword": kw,
            "volume": volume,
            "intent": keyword_scoring.classify_intent(kw),
            "difficulty": keyword_scoring.heuristic_difficulty(kw),
            "your_position": round(pos, 1) if pos is not None else None,
        })
    rows.sort(key=lambda x: (x["your_position"] is not None, -(x["volume"] or 0)))
    return {
        "configured": True,
        "competitor": comp,
        "gsc_connected": gsc_connected,
        "count": len(rows),
        "rows": rows,
    }


# Hosts that appear in SERPs but aren't SEO competitors: search engines, social,
# video, the official exam body, aggregators/CDNs. Substring match on the host.
_NON_COMPETITOR_HOSTS = (
    "google.", "youtube.", "facebook.", "instagram.", "twitter.", "x.com",
    "linkedin.", "wikipedia.", "reddit.", "pinterest.", "t.me", "whatsapp.",
    "amazon.", "play.google", "apps.apple", "gov.in", ".gov", "cdn-", "cdn.",
)

# Streaming / movie / music hosts that surface for ambiguous keywords (e.g. a
# course brand that is also a show title) but are never SEO competitors — UNLESS
# the domain is itself an entertainment niche (see _is_entertainment_domain).
# Substring match on the host.
_ENTERTAINMENT_HOSTS = (
    "imdb.", "primevideo.", "tv.apple", "justwatch.", "netflix.", "hotstar.",
    "jiocinema.", "disneyplus.", "hulu.", "zee5.", "sonyliv.", "mxplayer.",
    "spotify.", "rottentomatoes.", "letterboxd.", "themoviedb.", "tvguide.",
    "bookmyshow.", "paramountplus.", "max.com",
)

# Words in a domain's category or topic terms that mark it as an entertainment
# niche, for which the hosts above ARE legitimate competitors.
_ENTERTAINMENT_SIGNALS = (
    "movie", "film", "stream", "cinema", "tv show", "web series", "series",
    "music", "entertainment", "ott", "watch online", "box office", "showtime",
)


def _is_entertainment_domain(db: Session, domain: Domain) -> bool:
    """True when the domain's category or topic terms indicate an entertainment
    niche, so streaming/movie hosts should be kept as real competitors."""
    hay: list[str] = []
    cat_id = getattr(domain, "category_id", None)
    if cat_id:
        cat = db.get(Category, cat_id)
        if cat:
            hay.append(f"{cat.name} {cat.slug}".lower())
    hay.append((getattr(domain, "topic_terms", None) or "").lower())
    text = " ".join(hay)
    return any(sig in text for sig in _ENTERTAINMENT_SIGNALS)


def _on_topic(keyword: str, topic_regex: str | None) -> bool:
    """Whether a keyword's text matches the domain's topic terms. No topic
    configured (topic_regex is None) ⇒ everything is on-topic. Also checks the
    compact form so "u.p.s.c mock test" matches a "upsc" topic term."""
    if not topic_regex:
        return True
    compact = re.sub(r"[^a-z0-9]", "", keyword.lower())
    return (re.search(topic_regex, keyword, re.I) is not None
            or re.search(topic_regex, compact) is not None)


def _workspace_keyword_ids(db: Session, workspace_id) -> set:
    """Every keyword this workspace has worked on — the isolation boundary for
    competitor suggestions. SERP snapshots are keyed to global keywords, not to a
    workspace, so a keyword "belongs" to a workspace when the workspace has
    targeted it (domain_keywords), saved it (saved_keywords), or researched it
    (a keyword_research seed or result). domain_keywords alone is too narrow —
    many researched keywords with SERP data are never written there."""
    ids: set = set()
    ids.update(db.execute(
        select(DomainKeyword.keyword_id)
        .join(Domain, Domain.id == DomainKeyword.domain_id)
        .where(Domain.workspace_id == workspace_id)
    ).scalars())
    ids.update(
        kid for kid in db.execute(
            select(SavedKeyword.keyword_id)
            .join(Domain, Domain.id == SavedKeyword.domain_id)
            .where(Domain.workspace_id == workspace_id)
        ).scalars() if kid
    )
    ids.update(
        kid for kid in db.execute(
            select(KeywordResearch.seed_keyword_id)
            .where(KeywordResearch.workspace_id == workspace_id)
        ).scalars() if kid
    )
    ids.update(db.execute(
        select(KeywordResearchResult.keyword_id)
        .join(KeywordResearch, KeywordResearch.id == KeywordResearchResult.research_id)
        .where(KeywordResearch.workspace_id == workspace_id)
    ).scalars())
    return ids


def _category_keyword_ids(db: Session, workspace_id, category_id) -> set:
    """Keywords tagged with a category (via domain_keywords) in this workspace.
    Category only lives on domain_keywords, so this can be empty even when the
    workspace has plenty of researched keywords — callers fall back to the full
    workspace set when it is."""
    return set(db.execute(
        select(DomainKeyword.keyword_id)
        .join(Domain, Domain.id == DomainKeyword.domain_id)
        .where(Domain.workspace_id == workspace_id,
               DomainKeyword.category_id == category_id)
    ).scalars())


def competitor_domains(
    db: Session,
    domain: Domain,
    *,
    country: str = "in",
    language: str = "en",
    limit: int = 50,
    min_shared: int = 1,
) -> dict:
    """Competitor domains derived from SERP snapshots: the sites that show up in
    the search results for the keywords you've scraped, ranked by how many of
    those keywords they appear for (share of voice) and how well they rank.

    Read-only. Your own domain, search engines, social/video, the official exam
    body and CDNs are excluded. Empty until serp_snapshots has data.

    Scoped to this domain's WORKSPACE: a SERP keyword only counts when the
    workspace has worked on it — targeted (domain_keywords), saved
    (saved_keywords) or researched (a keyword_research seed/result). SERP
    snapshots aren't workspace-bound, so without this an uncategorized domain
    would pool every workspace's SERP data and inherit an unrelated niche's
    competitors. Crucially this is NOT domain_keywords alone: most researched
    keywords with SERP data are never written there, so keying only on them hid
    a workspace's own competitors (see _workspace_keyword_ids).

    When the domain has a CATEGORY and the workspace has keywords tagged with it,
    results narrow to that category so an Education domain never sees Technology
    competitors. Category lives only on domain_keywords, so a workspace whose
    keywords aren't tagged (common) falls back to its full owned set rather than
    showing nothing. A workspace with no keywords yet gets no suggestions.

    On top of that, when the domain has topic_terms set, keywords whose text is
    off-topic (a topic_regex miss) are dropped before aggregation. No topic_terms
    (topic_regex is None) ⇒ no topic filtering, mirroring domain_settings."""
    own = _norm_host(domain.domain)
    topic_regex = domain_settings.resolve(
        domain, getattr(domain, "workspace", None)
    ).topic_regex
    stmt = (
        select(
            SerpSnapshotItem.domain,
            SerpSnapshotItem.rank_absolute,
            SerpSnapshot.keyword_id,
            Keyword.keyword,
        )
        .join(SerpSnapshot, SerpSnapshot.id == SerpSnapshotItem.snapshot_id)
        .join(Keyword, Keyword.id == SerpSnapshot.keyword_id)
        .where(
            SerpSnapshot.environment == data_env.env_for_domain(db, domain),
            SerpSnapshot.country_code == country,
            SerpSnapshot.language_code == language,
            SerpSnapshotItem.type == "organic",
        )
    )
    # Isolate to the workspace's own keywords. A domain with no workspace_id (not
    # persisted, e.g. a unit-test stub) can't be scoped and keeps the locale pool.
    category_ids: set = set()
    if domain.workspace_id is not None:
        owned_ids = _workspace_keyword_ids(db, domain.workspace_id)
        if not owned_ids:
            return {"domain": own, "country": country, "count": 0, "rows": []}
        stmt = stmt.where(SerpSnapshot.keyword_id.in_(owned_ids))
        if domain.category_id is not None:
            category_ids = _category_keyword_ids(db, domain.workspace_id, domain.category_id)
    rows_db = db.execute(stmt).all()

    # Drop streaming/movie hosts unless this is an entertainment-niche domain.
    drop_entertainment = not _is_entertainment_domain(db, domain)

    def _aggregate(apply_topic: bool, category_only: bool) -> list[dict]:
        agg: dict[str, dict] = {}
        for host, position, keyword_id, keyword in rows_db:
            h = _norm_host(host)
            if not h or h == own:
                continue
            if any(bad in h for bad in _NON_COMPETITOR_HOSTS):
                continue
            if drop_entertainment and any(bad in h for bad in _ENTERTAINMENT_HOSTS):
                continue
            if category_only and keyword_id not in category_ids:
                continue
            if apply_topic and not _on_topic(keyword, topic_regex):
                continue
            a = agg.setdefault(h, {"kids": set(), "positions": [], "keywords": set()})
            a["kids"].add(keyword_id)
            a["positions"].append(int(position))
            a["keywords"].add(keyword)

        out = []
        for host, a in agg.items():
            shared = len(a["kids"])
            if shared < min_shared:
                continue
            out.append({
                "competitor": host,
                "keywords_shared": shared,
                "avg_position": round(sum(a["positions"]) / len(a["positions"]), 1),
                "best_position": min(a["positions"]),
                "keywords": sorted(a["keywords"])[:10],
            })
        # Most keyword overlap first; break ties by better average rank.
        out.sort(key=lambda r: (-r["keywords_shared"], r["avg_position"]))
        return out

    # Prefer the category-scoped, topic-filtered set; progressively relax so a
    # narrow category (no tagged keywords) or narrow topic_terms never wrongly
    # empties the suggestions.
    use_category = bool(category_ids)
    rows = _aggregate(apply_topic=bool(topic_regex), category_only=use_category)
    if not rows and use_category:
        rows = _aggregate(apply_topic=bool(topic_regex), category_only=False)
    if not rows and topic_regex:
        rows = _aggregate(apply_topic=False, category_only=False)
    rows = rows[:limit]
    return {"domain": own, "country": country, "count": len(rows), "rows": rows}


def list_researches(
    db: Session, workspace: Workspace, domain: Domain | None = None, *,
    kind: str = "keyword", limit: int | None = None,
) -> list[KeywordResearch]:
    """Searches for a workspace, newest first, filtered by kind ('keyword' |
    'domain' | 'all'). An optional domain further scopes runs to that domain.

    Domain searches are de-duplicated to the newest non-empty run per host +
    locale (a refresh records a new run; older runs stay in the DB as history)."""
    query = select(KeywordResearch).where(KeywordResearch.workspace_id == workspace.id)
    if kind != "all":
        query = query.where(KeywordResearch.kind == kind)
    if domain is not None:
        query = query.where(KeywordResearch.domain_id == domain.id)
    runs = db.execute(query.order_by(
        KeywordResearch.created_at.desc(),
        KeywordResearch.completed_at.desc().nulls_last(),
    )).scalars()

    out: list[KeywordResearch] = []
    seen: set = set()
    for r in runs:
        if r.kind == "domain":
            if not r.result_count:
                continue  # legacy / failed runs that stored no rows
            key = (r.target, r.country_code, r.language_code)
            if key in seen:
                continue
            seen.add(key)
        out.append(r)
        if limit and len(out) >= limit:
            break
    return out


def get_research(
    db: Session, workspace: Workspace, research_id: str
) -> KeywordResearch | None:
    return db.execute(
        select(KeywordResearch).where(
            KeywordResearch.id == research_id,
            KeywordResearch.workspace_id == workspace.id,
        )
    ).scalar_one_or_none()


def research_questions(db: Session, research: KeywordResearch) -> list[str]:
    """Question-form keywords mined for this run's seed (the Questions panel)."""
    rows = db.execute(
        select(Keyword.keyword)
        .join(KeywordRelation, KeywordRelation.related_keyword_id == Keyword.id)
        .where(
            KeywordRelation.keyword_id == research.seed_keyword_id,
            KeywordRelation.relation_type == "question",
        )
        .order_by(Keyword.keyword)
    ).scalars()
    return list(rows)


def research_results(
    db: Session, research: KeywordResearch
) -> list[tuple[KeywordResearchResult, Keyword, KeywordMetric | None, list[str]]]:
    """Ranked results joined to their keyword, latest metric and source categories
    (autocomplete / related / questions / prepositions / comparisons / gsc / …)."""
    rows = db.execute(
        select(KeywordResearchResult, Keyword)
        .join(Keyword, Keyword.id == KeywordResearchResult.keyword_id)
        .where(KeywordResearchResult.research_id == research.id)
        .order_by(KeywordResearchResult.rank)
    ).all()
    keyword_ids = [kw.id for _r, kw in rows]

    # All source categories for these keywords, in one query.
    sources_by_kw: dict[str, set[str]] = {}
    if keyword_ids:
        for kid, stype in db.execute(
            select(KeywordSource.keyword_id, KeywordSource.source_type).where(
                KeywordSource.keyword_id.in_(keyword_ids)
            )
        ).all():
            sources_by_kw.setdefault(str(kid), set()).add(stype)

    # Latest metric per keyword, batched into one query (no per-row N+1).
    latest_metric: dict = {}
    if keyword_ids:
        metric_rows = db.execute(
            select(KeywordMetric)
            .where(KeywordMetric.keyword_id.in_(keyword_ids))
            .order_by(KeywordMetric.keyword_id, KeywordMetric.metric_date.desc())
        ).scalars()
        for m in metric_rows:
            latest_metric.setdefault(m.keyword_id, m)  # first seen per keyword = newest

    out: list[tuple[KeywordResearchResult, Keyword, KeywordMetric | None, list[str]]] = []
    for result, keyword in rows:
        out.append((
            result,
            keyword,
            latest_metric.get(keyword.id),
            sorted(sources_by_kw.get(str(keyword.id), set())),
        ))
    return out
