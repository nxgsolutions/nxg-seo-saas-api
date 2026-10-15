"""Nightly GSC sync — grows the DataCube from Search Console without user clicks.

For every domain whose workspace has a connected Search Console account:

  1. Pull the last N days of query data (position / clicks / impressions).
  2. Record today's position for each *tracked* keyword  -> rank_entries
  3. Upsert every real query as a global keyword, link it to the domain and
     record its provenance                               -> keywords,
                                                            domain_keywords,
                                                            keyword_sources('gsc')
  4. Write today's GSC columns on the keyword's metric   -> keyword_metrics

Only the gsc_* columns (and scores derived from them) are touched on the metric
row, so Ads volume / Trends written earlier the same day by a research run are
kept. Each domain is its own unit of work: one domain failing (expired token,
unverified property) is recorded and the job moves on.
"""

from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.domain import Domain
from app.models.gsc_connection import GscConnection
from app.models.keyword_analysis import Keyword, KeywordMetric
from app.models.ranking import TrackedKeyword
from app.models.workspace import Workspace
from app.services import gsc_service, keyword_scoring, rank_service
from app.services import keyword_analysis_service as kas

logger = logging.getLogger(__name__)

# First sync only: how many top Search Console queries to start tracking for a
# domain that tracks nothing yet (so Rank Tracking has history from day one).
AUTO_TRACK_LIMIT = 15

_GSC_ERRORS = (
    gsc_service.GscNotConfiguredError,
    gsc_service.GscNotConnectedError,
    gsc_service.GscPropertyError,
    gsc_service.GscApiError,
)


def _connected_domains(db: Session) -> list[Domain]:
    """Every domain in a workspace that has a Search Console connection."""
    return list(
        db.execute(
            select(Domain)
            .join(GscConnection, GscConnection.workspace_id == Domain.workspace_id)
            .order_by(Domain.created_at)
        ).scalars()
    )


def _record_gsc_metric(
    db: Session,
    keyword: Keyword,
    row: dict,
    *,
    country: str,
    language: str,
    today: date,
) -> None:
    """Set today's GSC columns on the keyword's metric snapshot (create if absent)."""
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

    metric.gsc_impressions = int(row.get("impressions") or 0)
    metric.gsc_clicks = int(row.get("clicks") or 0)
    metric.gsc_position = round(float(row.get("position") or 0.0), 2) or None
    if metric.intent is None:
        metric.intent = keyword_scoring.classify_intent(keyword.keyword)
    if metric.difficulty is None:
        metric.difficulty = keyword_scoring.heuristic_difficulty(keyword.keyword)
    metric.opportunity_score = keyword_scoring.opportunity_score(
        metric.gsc_impressions,
        float(metric.gsc_position) if metric.gsc_position is not None else None,
        int(metric.difficulty) if metric.difficulty is not None else None,
    )
    if metric.confidence_score is None:
        metric.confidence_score = keyword_scoring.confidence_score(1, has_gsc=True)


def _auto_track(db: Session, domain: Domain, rows: list[dict], limit: int = AUTO_TRACK_LIMIT) -> int:
    """Start tracking the domain's top Search Console queries (most clicks, then
    impressions) — only when it tracks nothing yet. Used by the FIRST sync only,
    never nightly, so keywords a user removed are not re-added."""
    tracked = db.execute(
        select(func.count()).select_from(TrackedKeyword).where(TrackedKeyword.domain_id == domain.id)
    ).scalar_one()
    if tracked:
        return 0
    picked: list[str] = []
    seen: set[str] = set()
    for row in sorted(rows, key=lambda r: (-(r.get("clicks") or 0), -(r.get("impressions") or 0))):
        term = (row.get("keyword") or "").strip()
        if not term or len(term) > 200 or term.lower() in seen or kas._is_operator_query(term):
            continue
        seen.add(term.lower())
        picked.append(term)
        if len(picked) >= limit:
            break
    db.add_all([TrackedKeyword(domain_id=domain.id, term=t) for t in picked])
    db.flush()
    return len(picked)


def sync_domain(
    db: Session,
    domain: Domain,
    *,
    days: int,
    country: str,
    language: str,
    today: date,
    auto_track: bool = False,
) -> dict:
    """Sync one domain. Raises the gsc_service errors for the caller to record.
    `auto_track` (first sync only) starts tracking top queries if none are tracked."""
    workspace = db.get(Workspace, domain.workspace_id)
    data = gsc_service.keyword_performance(db, workspace, domain, days)
    rows = data.get("keywords", [])

    auto_tracked = _auto_track(db, domain, rows) if auto_track else 0
    ranks = rank_service.sync_from_gsc(db, domain, rows, today)

    keywords = 0
    for row in rows:
        term = (row.get("keyword") or "").strip()
        if not term or kas._is_operator_query(term):
            continue
        keyword = kas._upsert_keyword(db, term)
        kas._upsert_domain_keyword(db, domain, keyword, country, language)
        kas._add_source_once(db, keyword, "gsc", domain.domain)
        _record_gsc_metric(db, keyword, row, country=country, language=language, today=today)
        keywords += 1
    db.commit()

    return {
        "domain": domain.domain,
        "site_url": data.get("site_url"),
        "gsc_queries": len(rows),
        "keywords_upserted": keywords,
        "tracked_synced": ranks["synced"],
        "tracked_total": ranks["total"],
        "auto_tracked": auto_tracked,
    }


def run(
    db: Session,
    *,
    days: int = 28,
    country: str = "in",
    language: str = "en",
) -> dict:
    """Sync every connected domain. Returns stats; `failed` > 0 means partial."""
    return _sync_all(db, _connected_domains(db), days=days, country=country, language=language)


def _sync_all(
    db: Session,
    domains: list[Domain],
    *,
    days: int,
    country: str,
    language: str,
    auto_track: bool = False,
) -> dict:
    today = date.today()
    results: list[dict] = []
    failed = 0
    for domain in domains:
        try:
            results.append(
                sync_domain(
                    db, domain, days=days, country=country, language=language, today=today,
                    auto_track=auto_track,
                )
            )
        except _GSC_ERRORS as exc:
            db.rollback()
            failed += 1
            results.append({"domain": domain.domain, "error": f"{type(exc).__name__}: {exc}"})
        except Exception as exc:  # noqa: BLE001 - one bad domain must not stop the job
            db.rollback()
            failed += 1
            logger.exception("GSC sync failed for domain %s", domain.domain)
            results.append({"domain": domain.domain, "error": f"{type(exc).__name__}: {exc}"})

    return {
        "date": today.isoformat(),
        "days": days,
        "country": country,
        "language": language,
        "domains_total": len(domains),
        "domains_ok": len(domains) - failed,
        "failed": failed,
        "keywords_upserted": sum(r.get("keywords_upserted", 0) for r in results),
        "tracked_synced": sum(r.get("tracked_synced", 0) for r in results),
        "auto_tracked": sum(r.get("auto_tracked", 0) for r in results),
        "domains": results,
    }


def first_sync(
    workspace_id,
    domain_id=None,
    *,
    session_factory=None,
    days: int = 28,
    country: str = "in",
    language: str = "en",
) -> None:
    """Sync right away instead of waiting for the nightly run — called in the
    background when Search Console is connected (all the workspace's domains) or
    a domain is added to a connected workspace (just that domain). Also starts
    tracking top queries for domains that track nothing yet. Uses its own DB
    session (the request's is closed by then); logged in job_runs as
    triggered_by='connect'. Never raises — the user's request already succeeded."""
    from app.db.session import SessionLocal
    from app.jobs.runner import run_job

    db = (session_factory or SessionLocal)()
    try:
        stmt = select(Domain).where(Domain.workspace_id == workspace_id).order_by(Domain.created_at)
        if domain_id is not None:
            stmt = stmt.where(Domain.id == domain_id)
        domains = list(db.execute(stmt).scalars())
        if not domains:
            return
        run_job(db, "gsc_sync", lambda d, **kw: _sync_all(d, domains, auto_track=True, **kw),
                triggered_by="connect", days=days, country=country, language=language)
    except Exception:  # noqa: BLE001 - logged by run_job; the nightly run retries
        logger.exception("First GSC sync failed for workspace %s", workspace_id)
    finally:
        db.close()
