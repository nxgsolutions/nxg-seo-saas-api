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
from datetime import date, datetime, timedelta, timezone

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
    intent_service,
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


def business_seeds(domain: Domain) -> tuple[list[str], str | None]:
    """Business/category seed terms + a representative URL for Google Keyword
    Planner, so a domain's keyword research is topic-specific instead of a generic
    expansion of one typed word.

    Built from first-party context we already store: the domain's cached LLM
    `business_profile` (services) and its auto-derived `topic_terms`.

    ONLY specific MULTI-WORD phrases are used as Planner seeds. The category name
    ("Education"), the industry label, the brand name, and bare single words
    ("services", "notes", "civil") are deliberately excluded: Google expands a
    single generic word into its huge head term (e.g. "notes" -> 5.5M searches)
    which is irrelevant to the typed query. Multi-word phrases ("upsc civil
    services", "daily current affairs") stay on-topic. The site URL (returned
    separately, used as keywordAndUrlSeed) carries the broad business grounding
    instead. Returns ([], url) when no multi-word context exists — callers then
    seed Google with just the user's keyword + the URL."""
    raw: list[str] = []
    profile = domain.business_profile or {}
    for svc in (profile.get("services") or [])[:8]:
        if isinstance(svc, str) and svc.strip():
            raw.append(svc.strip().lower())
    for term in (domain.topic_terms or "").split(","):
        t = term.strip().lower()
        if t:
            raw.append(t)

    def _specific(phrase: str) -> bool:
        words = phrase.split()
        # Multi-word only, and at least one word must be a real content word
        # (drops "best sellers", "online store" and other all-chrome phrases).
        return len(words) >= 2 and any(_meaningful_seed_word(w) for w in words)

    seeds = [s for s in dict.fromkeys(raw) if len(s) >= 3 and _specific(s)][:8]
    url = f"https://{domain.domain}" if getattr(domain, "domain", None) else None
    return seeds, url


def _planner_idea_terms(
    seed: str, domain: Domain | None, country: str, language: str
) -> dict[str, str | None]:
    """Expand the candidate set with Google Keyword Planner IDEAS seeded from the
    business/category context (keyword_and_url when a site URL is available), so a
    domain search surfaces on-topic keywords Google itself suggests — not only the
    Autocomplete expansion of the seed. Returns {normalized_keyword: concept}.
    Empty (and silent) when Ads isn't configured, no domain context exists, or the
    call fails — the run always continues on the free sources."""
    if domain is None:
        logger.info(
            "[kw-research] 3/5 business/category ideas: SKIPPED — no active website, "
            "so this is a GENERIC keyword search (Google Autocomplete only)"
        )
        return {}
    if not settings.google_ads_configured:
        logger.info(
            "[kw-research] 3/5 business/category ideas: SKIPPED — Google Ads not "
            "configured (Autocomplete only; no Google Ads idea/metric data)"
        )
        return {}
    seeds, url = business_seeds(domain)
    mode = "keyword_and_url" if url else "keyword"
    terms = list(dict.fromkeys([seed, *seeds]))
    logger.info(
        "[kw-research] 3/5 business/category-AWARE for domain=%s: Google Ads idea seeds=%s "
        "url=%s mode=%s  (from category + business_profile.services/industry + topic_terms)",
        domain.domain, terms, url, mode,
    )
    if not terms:
        return {}
    try:
        data = keyword_planner_service.generate_ideas(
            ", ".join(terms),
            mode=mode,
            seed_url=url,
            country=country,
            language=language,
            limit=_MAX_CANDIDATES,
        )
    except keyword_planner_service.PlannerError as exc:
        logger.warning("[kw-research] 3/5 Google Ads ideas unavailable: %s", exc)
        return {}
    except Exception:  # noqa: BLE001 — ideas are best-effort; never fail the run
        logger.exception("[kw-research] 3/5 Google Ads ideas failed; continuing without them")
        return {}
    raw = {
        keyword_scoring.normalize_keyword(r["keyword"]): r.get("concept")
        for r in data.get("rows", []) if r.get("keyword")
    }
    # Relevance gate: Google (prompted by the site URL) still volunteers broad
    # single-word head terms ("notes", "services", "education") that don't match
    # the typed query. Keep multi-word ideas (specific) and single words that share
    # a token with the seed; drop lone generic head terms.
    seed_tokens = set(re.findall(r"[a-z0-9]+", seed.lower()))

    def _relevant(norm_kw: str) -> bool:
        words = norm_kw.split()
        return len(words) >= 2 or bool(seed_tokens & set(words))

    out = {k: v for k, v in raw.items() if _relevant(k)}
    logger.info(
        "[kw-research] 3/5 Google Ads Keyword Planner IDEAS -> %d relevant keywords "
        "(dropped %d off-topic head terms)", len(out), len(raw) - len(out),
    )
    return out


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
    logger.info(
        "[kw-research] 1/5 START seed=%r country=%s language=%s active_website=%s business_aware=%s",
        seed, country, language, (domain.domain if domain else None), domain is not None,
    )

    # 1. Gather candidates from all sources.
    gsc_map = _gsc_keywords(db, workspace, domain) if domain else {}  # owned-domain rank
    # Categorized Autocomplete expansion (the Ubersuggest idea tabs).
    idea_buckets = keyword_suggest.ideas(seed, country, language)
    _ac_total = sum(
        len(idea_buckets.get(c, []))
        for c in ("autocomplete", "related", "questions", "prepositions", "comparisons")
    )
    logger.info(
        "[kw-research] 2/5 Google Autocomplete (free, keyless) -> %d ideas; GSC rank rows -> %d",
        _ac_total, len(gsc_map),
    )
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
    # Business/category-specific ideas from Google Keyword Planner (domain searches
    # only): seeded from the domain's category, services and topic terms (+ its URL)
    # so results are on-topic, not a generic expansion of the typed seed.
    planner_concepts = _planner_idea_terms(seed, domain, country, language)
    for norm in planner_concepts:
        _note(norm, "keyword_planner")

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
    logger.info(
        "[kw-research] 4/5 Google Ads metrics (volume/CPC/competition) for %d candidate keywords",
        len(candidates),
    )
    planner_map = _planner_keywords(list(candidates.keys()), country, language)

    # 2. Persist keywords, provenance and today's metric snapshot.
    scored: list[tuple[float, Keyword]] = []
    keywords_by_norm: dict[str, Keyword] = {}
    metrics_by_norm: dict[str, KeywordMetric] = {}
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
        metrics_by_norm[norm] = metric
        # Rank by opportunity BLENDED with relevance to the typed seed, so on-topic
        # keywords lead and a zero-overlap head term can never top the list (volume
        # alone used to float generic head terms up). Seed itself is pinned on top.
        opp = float(metric.opportunity_score or 0.0)
        rel = float(metric.relevance_score or 0.0) / 100.0  # 0-1 token overlap
        rank_key = 1e9 if norm == seed_norm else opp * (0.4 + 0.6 * rel)
        scored.append((rank_key, keyword))

    # 2b. AI batch intent (business/category aware) for the keywords the deterministic
    # rules left as 'unknown' — one LLM call for the whole residual, so bare product
    # nouns ("buddha statue for home") get a real intent instead of 'unknown'.
    _classify_unknown_intents(metrics_by_norm, keywords_by_norm, domain)

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
    logger.info(
        "[kw-research] 5/5 DONE: %d keywords stored (%d with Google Ads metrics). "
        "business_aware=%s", len(scored), sum(1 for n in candidates if n in planner_map),
        domain is not None,
    )
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
        # Refine the seed's intent from the same SERP (+ an AI fallback for the
        # genuinely ambiguous) — the one keyword that HAS a SERP, off the hot path.
        _apply_serp_intent(
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


def _apply_serp_intent(
    db: Session, keyword_id, seed: str, serp: list[dict], *, country: str, language: str,
    category: str | None = None,
) -> None:
    """Refine the SEED's intent with the hybrid classifier (rules + this SERP + an
    AI fallback) and write it onto the latest metric row. Best-effort: only
    overwrites when the hybrid resolver returns a concrete (non-'unknown') intent,
    so a weak SERP never erases a good rule-based label. No-op without a metric
    row. Bulk keywords keep their fast rule-based intent from `_upsert_metric_snapshot`."""
    try:
        result = intent_service.resolve(
            seed, serp_items=serp, category=category, allow_ai=True,
        )
    except Exception:  # noqa: BLE001 - intent refinement must never crash the job
        logger.exception("SERP/AI intent for seed %r failed", seed)
        return
    if result.intent == "unknown":
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
    metric.intent = result.intent


def _classify_unknown_intents(
    metrics_by_norm: dict[str, KeywordMetric],
    keywords_by_norm: dict[str, Keyword],
    domain: Domain | None,
) -> None:
    """Fill in the intent for every keyword the rules left as 'unknown' with ONE
    business/category-aware AI batch call. In-place on the metric rows; best-effort
    (no AI provider, or a failure, just leaves them 'unknown'). This is what turns
    bare product-noun searches from mostly-'unknown' into real intents."""
    unknown = {
        norm: keywords_by_norm[norm].keyword
        for norm, metric in metrics_by_norm.items()
        if (metric.intent or "unknown") == "unknown" and norm in keywords_by_norm
    }
    if not unknown:
        return
    category = getattr(getattr(domain, "category", None), "name", None) if domain else None
    topic = (domain.topic_terms if domain else None) or None
    logger.info(
        "[kw-research] 4.5/5 AI intent: classifying %d 'unknown' keywords "
        "(category=%s, topic=%s)", len(unknown), category, bool(topic),
    )
    try:
        resolved = intent_service.classify_batch(
            list(unknown.values()), category=category, topic=topic,
        )
    except Exception:  # noqa: BLE001 — intent is best-effort; never fail the run
        logger.exception("[kw-research] 4.5/5 AI intent batch failed; keeping 'unknown'")
        return
    for norm, result in resolved.items():
        metric = metrics_by_norm.get(norm)
        if metric is not None:
            metric.intent = result.intent
    logger.info(
        "[kw-research] 4.5/5 AI intent resolved %d of %d unknowns",
        len(resolved), len(unknown),
    )


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


# Words that pollute topic seeds — they come from a site's chrome (footer/download
# links), its shop UI, or promo/price copy, not from its actual niche. A seed
# word that is one of these, or a phrase containing one, is dropped.
_SEED_STOPWORDS = {
    # app / social / nav chrome
    "app", "apps", "download", "downloads", "telegram", "whatsapp", "facebook",
    "instagram", "youtube", "twitter", "gmail", "google", "play", "store",
    "playstore", "appstore", "login", "signin", "signup", "register", "subscribe",
    "home", "page", "click", "menu", "share", "follow", "email", "contact", "chat",
    "about", "faq", "faqs", "blog", "account", "search", "help", "support",
    # shop UI / checkout
    "shop", "shopping", "buy", "cart", "add", "wishlist", "checkout", "order",
    "orders", "quantity", "qty", "view", "more", "product", "products", "category",
    "categories", "collection", "collections", "item", "items", "shipping",
    "delivery", "returns", "return", "track", "payment",
    # promo / price / commerce adjectives (dominate e-commerce homepages, say
    # nothing about the niche)
    "best", "seller", "sellers", "sale", "sales", "off", "deal", "deals", "offer",
    "offers", "discount", "discounts", "free", "price", "prices", "pricing", "save",
    "new", "latest", "trending", "popular", "featured", "exclusive", "luxury",
    "premium", "quality", "authentic", "original", "genuine", "top", "online",
    "rs", "inr", "usd", "rupees", "amount", "total", "review", "reviews", "rating",
    "ratings", "star", "stars", "india", "indian",
    # generic filler nouns
    "piece", "pieces", "every", "day", "days", "set", "sets", "size", "sizes",
    "color", "colour", "gift", "gifts", "range", "world", "time", "way",
    # TLD / URL fragments that survive tokenisation
    "com", "www", "net", "org", "co", "https", "http",
}


def _meaningful_seed_word(w: str) -> bool:
    """A content word worth keeping as a topic seed: alphabetic, not too short,
    and not a chrome/shop/promo stopword (so digits, prices and 'best seller'
    never become a topic)."""
    return len(w) >= 3 and w.isalpha() and w not in _SEED_STOPWORDS


def topic_seeds_from_ngrams(
    unigrams: list[dict], bigrams: list[dict], trigrams: list[dict], *, brand: str = "",
) -> list[str]:
    """Pure topic-seed selection from a page's ranked n-grams (no I/O — so it's
    unit-testable and shared by the crawl path below).

    Prefers salient single nouns (what the site actually sells / covers —
    'brass', 'diya', 'statue'), then adds clean multi-word phrases for precision.
    Chrome, shop-UI, promo/price words, anything with a digit, and the site's own
    brand token are all dropped, so 'best seller sale' / '999 00' / the brand
    name never become a topic."""
    brand = (brand or "").lower()

    def ok(w: str) -> bool:
        if not _meaningful_seed_word(w):
            return False
        # Drop the brand token itself (e.g. "peetalkraft") and words built from it.
        return not (brand and (w == brand or brand in w or w in brand))

    seeds: list[str] = []
    # 1) Salient single nouns — the core topic signal, and what competitor-gap
    #    keywords actually match against.
    for p in (unigrams or [])[:20]:
        w = str(p.get("phrase", "")).strip().lower()
        if ok(w):
            seeds.append(w)
    # 2) Clean multi-word phrases (every word meaningful, no digits) for extra
    #    precision on multi-word concepts.
    for p in (trigrams or [])[:8] + (bigrams or [])[:10]:
        text = str(p.get("phrase", "")).strip().lower()
        words = text.split()
        if words and all(ok(w) for w in words):
            seeds.append(text)
    return list(dict.fromkeys(seeds))[:15]


def _site_topic_seeds(website_input: str) -> list[str]:
    """Crawl the site's homepage and return its top TOPICAL terms — the reliable
    signal of what the site is about (Google's site/url seed guesses wrong for
    lesser-known domains). Best-effort; the selection itself lives in the pure
    topic_seeds_from_ngrams so it can be tested without a crawl."""
    url = website_input if website_input.lower().startswith("http") else f"https://{website_input}"
    try:
        analysis = content_service.run_analysis(url)
    except Exception:  # noqa: BLE001 — any crawl/parse failure just skips this path
        return []
    # The site's own brand token (host without TLD) is not a topic.
    brand = _norm_host(website_input).split(".")[0]
    return topic_seeds_from_ngrams(
        analysis.unigrams, analysis.bigrams, analysis.trigrams, brand=brand)


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


# ── Competitor rank TIMELINE (per-period, from dated SERP snapshots) ──────────

_TL_FREQS = ("daily", "weekly", "monthly")


def _period_start(d: date, freq: str) -> date:
    """Period a capture date falls into: daily = the day, weekly = that week's
    Monday, monthly = the 1st. SERP fetches land on irregular days; bucketing
    groups them into even periods for a readable trend."""
    if freq == "monthly":
        return d.replace(day=1)
    if freq == "daily":
        return d
    return d - timedelta(days=d.weekday())  # weekly → Monday


def _next_period(d: date, freq: str) -> date:
    """The start of the period after `d` (which must already be a period start)."""
    if freq == "daily":
        return d + timedelta(days=1)
    if freq == "weekly":
        return d + timedelta(days=7)
    # monthly → first of next month
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1)


def _period_seq(start: date, end: date, freq: str) -> list[date]:
    """Continuous list of period-start dates spanning [start, end] inclusive, so
    the chart has a gapless x-axis even where no snapshot exists for a period."""
    cur = _period_start(start, freq)
    last = _period_start(end, freq)
    out: list[date] = []
    while cur <= last:
        out.append(cur)
        cur = _next_period(cur, freq)
    return out


def competitor_rank_timeline(
    db: Session,
    domain: Domain,
    *,
    freq: str = "weekly",
    start: date | None = None,
    end: date | None = None,
    country: str = "in",
    language: str = "en",
    top: int = 8,
) -> dict:
    """Per-competitor average SERP rank over time, for a date range + frequency.

    Point-in-time: each period's average uses only that period's SERP snapshots
    (unlike `competitor_domains`, which blends every date into one number). Built
    straight from `serp_snapshots.captured_at` + `serp_snapshot_items`, reusing
    the same competitor filters (own-domain/non-competitor/entertainment
    exclusions, workspace keyword scope, category + topic narrowing with the same
    progressive relaxation). Historical snapshot data only — never a live lookup.

    The series is CONTINUOUS across [start, end] at the chosen `freq`
    ('daily'|'weekly'|'monthly'): every period appears, and a period with no
    snapshot gets `avg_position = 0` with `has_data = False` (a display zero, not
    a real rank — never interpolated or carried forward). Each competitor also
    carries `average_position` computed over the range EXCLUDING missing periods.
    Competitors = the top `top` by keyword overlap within the range.
    """
    freq = freq if freq in _TL_FREQS else "weekly"
    today = date.today()
    end_d = end or today
    start_d = start or (end_d - timedelta(days=27))
    if start_d > end_d:
        start_d, end_d = end_d, start_d
    periods = _period_seq(start_d, end_d, freq)
    period_set = set(periods)
    own = _norm_host(domain.domain)
    topic_regex = domain_settings.resolve(
        domain, getattr(domain, "workspace", None)
    ).topic_regex

    def empty() -> dict:
        # Still return the continuous axis so the UI can draw a flat line at 0.
        return {
            "domain": own, "country": country, "language": language,
            "freq": freq, "start": start_d, "end": end_d,
            "dates": list(periods), "competitors": [], "has_data": False,
        }

    base = (
        select(
            SerpSnapshotItem.domain,
            SerpSnapshotItem.rank_absolute,
            SerpSnapshot.keyword_id,
            SerpSnapshot.captured_at,
        )
        .join(SerpSnapshot, SerpSnapshot.id == SerpSnapshotItem.snapshot_id)
        .join(Keyword, Keyword.id == SerpSnapshot.keyword_id)
        .where(
            SerpSnapshot.environment == data_env.env_for_domain(db, domain),
            SerpSnapshotItem.type == "organic",
        )
    )
    category_ids: set = set()
    if domain.workspace_id is not None:
        owned_ids = _workspace_keyword_ids(db, domain.workspace_id)
        if not owned_ids:
            return empty()
        base = base.where(SerpSnapshot.keyword_id.in_(owned_ids))
        if domain.category_id is not None:
            category_ids = _category_keyword_ids(db, domain.workspace_id, domain.category_id)

    # Prefer the domain's resolved locale, but fall back to all locales if it has
    # no snapshots there — SERP captures aren't always tagged with the exact
    # market, and an empty trend is worse than a locale-mixed one.
    locale = (SerpSnapshot.country_code == country, SerpSnapshot.language_code == language)

    def _fetch(with_text: bool) -> list:
        stmt = base.add_columns(Keyword.keyword) if with_text else base
        rows = db.execute(stmt.where(*locale)).all()
        return rows if rows else db.execute(stmt).all()

    rows_db = _fetch(with_text=False)
    if not rows_db:
        return empty()

    drop_entertainment = not _is_entertainment_domain(db, domain)

    def _passes(host: str, keyword_id, keyword, *, apply_topic: bool, category_only: bool) -> bool:
        if not host or host == own:
            return False
        if any(bad in host for bad in _NON_COMPETITOR_HOSTS):
            return False
        if drop_entertainment and any(bad in host for bad in _ENTERTAINMENT_HOSTS):
            return False
        if category_only and keyword_id not in category_ids:
            return False
        if apply_topic and not _on_topic(keyword, topic_regex):
            return False
        return True

    # Topic filtering needs the keyword text, so fetch it alongside only when
    # topic_terms are set (most domains have none — avoids a wider row).
    rows_t = _fetch(with_text=True) if topic_regex is not None else None

    def _collect(apply_topic: bool, category_only: bool) -> dict[str, list[tuple]]:
        # comp host -> list of (capture_date, position, keyword_id), raw (not yet
        # bucketed) so the same collection serves any granularity.
        recs: dict[str, list[tuple]] = {}
        source = rows_t if (apply_topic and rows_t is not None) else rows_db
        for row in source:
            if apply_topic and rows_t is not None:
                host, position, keyword_id, captured_at, kw = row
            else:
                host, position, keyword_id, captured_at = row
                kw = None
            cday = captured_at.date()
            if cday < start_d or cday > end_d:  # outside the selected range
                continue
            h = _norm_host(host)
            if not _passes(h, keyword_id, kw, apply_topic=apply_topic, category_only=category_only):
                continue
            recs.setdefault(h, []).append((cday, int(position), keyword_id))
        return recs

    use_category = bool(category_ids)
    recs = _collect(apply_topic=bool(topic_regex), category_only=use_category)
    if not recs and use_category:
        recs = _collect(apply_topic=bool(topic_regex), category_only=False)
    if not recs and topic_regex is not None:
        recs = _collect(apply_topic=False, category_only=False)
    if not recs:
        return empty()

    # Keep the top competitors by TOTAL keyword overlap across all periods
    # (tie-break by average rank). Stable regardless of the chosen bucket, and it
    # favours broadly-present competitors, which read best as a trend line.
    def _overlap(records: list[tuple]) -> int:
        return len({kid for _, _, kid in records})

    def _avg(records: list[tuple]) -> float:
        return sum(p for _, p, _ in records) / len(records)

    keep = sorted(recs.items(), key=lambda kv: (-_overlap(kv[1]), _avg(kv[1])))[:top]

    # Build a continuous series per competitor: one point per period in the range.
    # Periods with no snapshot get avg_position 0 + has_data False (a display
    # zero, never interpolated); the range average excludes those.
    competitors = []
    any_data = False
    for host, records in keep:
        by_period: dict[date, list[int]] = {}
        for cday, pos, _ in records:
            p = _period_start(cday, freq)
            if p in period_set:
                by_period.setdefault(p, []).append(pos)
        points = []
        measured: list[float] = []
        for p in periods:
            positions = by_period.get(p)
            if positions:
                avg = round(sum(positions) / len(positions), 1)
                measured.append(avg)
                points.append({"captured_on": p, "avg_position": avg, "has_data": True})
            else:
                points.append({"captured_on": p, "avg_position": 0, "has_data": False})
        if measured:
            any_data = True
        competitors.append({
            "competitor": host,
            # Average over the range, EXCLUDING missing periods (0 if none).
            "average_position": round(sum(measured) / len(measured), 1) if measured else 0,
            "points": points,
        })

    # Order competitors by best (lowest) range average; no-data ones sink last.
    competitors.sort(key=lambda c: c["average_position"] if c["average_position"] else 1e9)

    return {
        "domain": own, "country": country, "language": language,
        "freq": freq, "start": start_d, "end": end_d,
        "dates": list(periods), "competitors": competitors, "has_data": any_data,
    }


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
