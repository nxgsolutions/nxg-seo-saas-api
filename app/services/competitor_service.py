"""Competitor tracking service.

Competitors and their SERP standing come from `serp_snapshots` (reusing
`keyword_analysis_service.competitor_domains`). A refresh computes the current
standing and writes one dated row per competitor into
`competitor_rank_snapshots`; the accumulated rows form the rank-change time
series. Only real captured snapshots are stored — the trend fills in as the
user refreshes over time (no demo / simulated data).
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.competitor import (
    CompetitorDiscoveryJob,
    CompetitorRankSnapshot,
    TrackedCompetitor,
)
from app.models.domain import Domain
from app.services import (
    domain_analytics_service,
    domain_settings,
    keyword_analysis_service,
)

# How many top competitors to persist per refresh / return in the trend.
TRACK_LIMIT = 10
CHART_LIMIT = 8


class CompetitorError(ValueError):
    """Bad input for a competitor edit (surfaced to the route as 400)."""


# ── Curated (user-editable) competitor list ─────────────────────────────────

def _curated(db: Session, domain: Domain) -> list[TrackedCompetitor]:
    return db.execute(
        select(TrackedCompetitor)
        .where(TrackedCompetitor.domain_id == domain.id)
        .order_by(TrackedCompetitor.created_at)
    ).scalars().all()


def _curated_hosts(db: Session, domain: Domain) -> list[str]:
    return [tc.competitor for tc in _curated(db, domain)]


def _ensure_seeded_list(
    db: Session, domain: Domain, *, country: str, language: str
) -> None:
    """If the curated list is empty, seed it from the top serp-derived
    competitors (source='auto') so the user starts with a real list to edit."""
    if _curated_hosts(db, domain):
        return
    live = keyword_analysis_service.competitor_domains(
        db, domain, country=country, language=language, limit=TRACK_LIMIT
    )
    for r in live["rows"]:
        db.add(TrackedCompetitor(
            domain_id=domain.id, competitor=r["competitor"], source="auto",
        ))
    db.commit()


def _latest_snapshot(db: Session, domain: Domain, host: str):
    return db.execute(
        select(CompetitorRankSnapshot)
        .where(
            CompetitorRankSnapshot.domain_id == domain.id,
            CompetitorRankSnapshot.competitor == host,
        )
        .order_by(CompetitorRankSnapshot.captured_on.desc())
        .limit(1)
    ).scalars().first()


def list_competitors(db: Session, domain: Domain) -> dict:
    """The curated (editable) competitor list, each with its latest SERP standing."""
    rows = []
    for tc in _curated(db, domain):
        snap = _latest_snapshot(db, domain, tc.competitor)
        rows.append({
            "id": str(tc.id),
            "competitor": tc.competitor,
            "label": tc.label,
            "source": tc.source,
            "reason": tc.reason,
            "confidence": tc.confidence,
            "match_score": tc.match_score,
            "avg_position": round(snap.avg_position, 1) if snap else None,
            "best_position": snap.best_position if snap else None,
            "keywords_shared": snap.keywords_shared if snap else None,
            "last_captured_on": snap.captured_on if snap else None,
        })
    return {"domain": domain.domain, "count": len(rows), "rows": rows}


def add_competitor(
    db: Session, domain: Domain, raw_domain: str, label: str | None = None
) -> dict:
    """Add a user-supplied competitor domain (idempotent, normalized to a host)."""
    host = keyword_analysis_service._norm_host(raw_domain)
    if not host or "." not in host:
        raise CompetitorError("Enter a valid domain like example.com")
    if host == keyword_analysis_service._norm_host(domain.domain):
        raise CompetitorError("That's your own domain, not a competitor.")

    existing = db.execute(
        select(TrackedCompetitor).where(
            TrackedCompetitor.domain_id == domain.id,
            TrackedCompetitor.competitor == host,
        )
    ).scalars().first()
    if existing is None:
        existing = TrackedCompetitor(
            domain_id=domain.id, competitor=host,
            label=(label or None), source="manual",
        )
        db.add(existing)
        db.commit()
        db.refresh(existing)

    snap = _latest_snapshot(db, domain, host)
    return {
        "id": str(existing.id),
        "competitor": existing.competitor,
        "label": existing.label,
        "source": existing.source,
        "reason": existing.reason,
        "confidence": existing.confidence,
        "match_score": existing.match_score,
        "avg_position": round(snap.avg_position, 1) if snap else None,
        "best_position": snap.best_position if snap else None,
        "keywords_shared": snap.keywords_shared if snap else None,
        "last_captured_on": snap.captured_on if snap else None,
    }


def remove_competitor(db: Session, domain: Domain, competitor_id: str) -> bool:
    """Remove a curated competitor by id (scoped to the domain)."""
    tc = db.execute(
        select(TrackedCompetitor).where(
            TrackedCompetitor.id == competitor_id,
            TrackedCompetitor.domain_id == domain.id,
        )
    ).scalars().first()
    if tc is None:
        return False
    db.delete(tc)
    db.commit()
    return True


def get_suggestions(
    db: Session, domain: Domain, *, country: str = "in", language: str = "en",
    limit: int = 15,
) -> dict:
    """Serp-derived competitor domains NOT already in the curated list — the
    one-click 'add' suggestions."""
    curated = set(_curated_hosts(db, domain))
    live = keyword_analysis_service.competitor_domains(
        db, domain, country=country, language=language, limit=limit + len(curated) + 10
    )
    rows = [
        {
            "competitor": r["competitor"],
            "keywords_shared": r["keywords_shared"],
            "avg_position": r["avg_position"],
            "best_position": r["best_position"],
        }
        for r in live["rows"] if r["competitor"] not in curated
    ][:limit]
    return {"domain": domain.domain, "count": len(rows), "rows": rows}


def current_competitors(
    db: Session,
    domain: Domain,
    *,
    country: str = "in",
    language: str = "en",
    limit: int = 50,
) -> dict:
    """The live competitor list computed from serp_snapshots (no persistence)."""
    result = keyword_analysis_service.competitor_domains(
        db, domain, country=country, language=language, limit=limit
    )
    return {
        "domain": result["domain"],
        "country": result["country"],
        "language": language,
        "count": result["count"],
        "rows": result["rows"],
    }


def get_history(db: Session, domain: Domain, *, top: int = CHART_LIMIT) -> dict:
    """Per-competitor rank time series for the chart.

    Includes the `top` competitors by keyword overlap in the most recent
    snapshot, with all of their historical points (ascending by date).
    """
    rows = db.execute(
        select(CompetitorRankSnapshot)
        .where(CompetitorRankSnapshot.domain_id == domain.id,
               CompetitorRankSnapshot.is_simulated.is_(False))  # defensive: skip any legacy demo rows
        .order_by(
            CompetitorRankSnapshot.competitor,
            CompetitorRankSnapshot.captured_on,
        )
    ).scalars().all()

    country = rows[0].country_code if rows else "in"
    language = rows[0].language_code if rows else "en"

    if not rows:
        return {
            "domain": domain.domain, "country": country, "language": language,
            "dates": [], "competitors": [],
        }

    latest = max(r.captured_on for r in rows)
    # Rank competitors by their standing in the latest snapshot (overlap desc).
    latest_rows = [r for r in rows if r.captured_on == latest]
    latest_rows.sort(key=lambda r: (-r.keywords_shared, r.avg_position))
    keep = [r.competitor for r in latest_rows[:top]]

    by_comp: dict[str, list[CompetitorRankSnapshot]] = {c: [] for c in keep}
    dates: set[date] = set()
    for r in rows:
        if r.competitor not in by_comp:
            continue
        by_comp[r.competitor].append(r)
        dates.add(r.captured_on)

    series = [
        {
            "competitor": comp,
            "points": [
                {
                    "captured_on": p.captured_on,
                    "avg_position": round(p.avg_position, 1),
                    "best_position": p.best_position,
                    "keywords_shared": p.keywords_shared,
                }
                for p in pts
            ],
        }
        for comp, pts in by_comp.items()
    ]
    return {
        "domain": domain.domain, "country": country, "language": language,
        "dates": sorted(dates), "competitors": series,
    }


def _upsert(db: Session, values: list[dict]) -> None:
    """Insert snapshot rows, updating metrics if the (domain, competitor, date)
    already exists (so a same-day re-refresh overwrites rather than errors)."""
    if not values:
        return
    stmt = pg_insert(CompetitorRankSnapshot).values(values)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_competitor_domain_date",
        set_={
            "avg_position": stmt.excluded.avg_position,
            "best_position": stmt.excluded.best_position,
            "keywords_shared": stmt.excluded.keywords_shared,
            "is_simulated": stmt.excluded.is_simulated,
        },
    )
    db.execute(stmt)


def refresh(
    db: Session,
    domain: Domain,
    *,
    country: str = "in",
    language: str = "en",
) -> dict:
    """Snapshot today's real rank for the competitor list. Seeds the
    curated list from serp data on first use. Curated competitors with no SERP
    data yet are listed but skipped in the trend (no position to record)."""
    # Ensure the user has a list to track (seed from serp data if empty).
    _ensure_seeded_list(db, domain, country=country, language=language)
    curated = _curated_hosts(db, domain)

    # Metrics for as many hosts as possible, keyed by host.
    live = keyword_analysis_service.competitor_domains(
        db, domain, country=country, language=language, limit=200
    )
    metrics = {r["competitor"]: r for r in live["rows"]}
    today = date.today()

    values: list[dict] = []
    tracked = 0
    for host in curated:
        m = metrics.get(host)
        if not m:
            continue  # curated but no SERP data yet — listed, not charted
        tracked += 1
        values.append({
            "domain_id": domain.id,
            "competitor": host,
            "captured_on": today,
            "avg_position": float(m["avg_position"]),
            "best_position": int(m["best_position"]),
            "keywords_shared": int(m["keywords_shared"]),
            "country_code": country,
            "language_code": language,
            "is_simulated": False,
        })
    _upsert(db, values)
    db.commit()

    if not curated:
        msg = ("No competitors yet — analyze keywords in SERP Analyzer, then "
               "refresh, or add competitors manually.")
    elif tracked == 0:
        msg = (f"{len(curated)} competitors listed, but none have SERP data yet. "
               "Run the SERP Analyzer for your keywords, then refresh.")
    else:
        msg = f"Refreshed {tracked} competitors for {today.isoformat()}."

    return {
        "domain": domain.domain,
        "captured_on": today,
        "competitors_tracked": tracked,
        "message": msg,
    }


# ── LLM competitor discovery (by domain) ────────────────────────────────────


def discover_and_store(
    db: Session, domain: Domain, *, target_domain: str | None = None,
    limit: int = 10, extra_services: list[str] | None = None,
    refresh: bool = False, progress=None,
) -> dict:
    """Run the LLM competitor finder for `target_domain` (defaults to the domain
    itself), replace the auto/AI-sourced curated entries with the results (keeping
    the user's manual ones), then snapshot ranks for any with SERP data.
    `extra_services` steers the match toward features not yet on the site.

    The competitor market follows the domain's CONFIGURED country (domain →
    workspace → global default), not the LLM's guess, so a domain set to India
    gets Indian competitors. The business profile extracted from the domain's own
    site is cached on the domain and reused on later runs; `refresh=True` forces a
    fresh extraction."""
    from app.services import competitor_discovery_service as disc

    target = (target_domain or domain.domain).strip()

    # Market = the domain's configured country, not whatever the LLM infers.
    cfg = domain_settings.resolve(domain, getattr(domain, "workspace", None))
    country = cfg.country
    market = domain_analytics_service.location_name_for(country)

    # Reuse the cached business profile when discovering for the domain itself
    # (not an arbitrary target) and the caller didn't ask for a refresh.
    own_host = keyword_analysis_service._norm_host(domain.domain)
    target_host = keyword_analysis_service._norm_host(target)
    is_own = bool(own_host) and target_host == own_host
    cached = domain.business_profile if (is_own and not refresh) else None

    # Enrichment: real competitors from SERP data (domains that actually rank for
    # this site's keywords). Blended into the candidate pool so the list reliably
    # fills to `limit`. Empty for a brand-new domain with no SERP data yet, so the
    # flow falls back to pure profiling automatically. Best-effort.
    extra_candidates: list[str] = []
    if is_own:
        try:
            sugg = get_suggestions(db, domain, country=country, language=cfg.language, limit=20)
            extra_candidates = [r["competitor"] for r in sugg["rows"]]
        except Exception:  # noqa: BLE001 - SERP enrichment is a bonus, not required
            extra_candidates = []

    # The workspace's own OTHER tracked domains — excluded from the results so a
    # newly-added sibling property never shows up as a "competitor" of this one.
    sibling_domains = [
        d for d in db.execute(
            select(Domain.domain).where(
                Domain.workspace_id == domain.workspace_id,
                Domain.id != domain.id,
            )
        ).scalars().all()
        if d
    ]

    # Three agents: 1) profile this business, 2) propose candidates (+ SERP),
    # 3) profile each candidate's real site and keep the best `limit` matches.
    result = disc.discover(
        target, limit=limit, extra_services=extra_services,
        business=cached, market=market, extra_candidates=extra_candidates,
        exclude_domains=sibling_domains, progress=progress,
    )
    comps = result["competitors"]

    # Persist the (possibly freshly extracted) profile for reuse next time.
    if is_own and result.get("business"):
        domain.business_profile = result["business"]
        domain.business_profile_at = datetime.now(timezone.utc)
        db.add(domain)

    # Replace previous auto/ai entries; keep manual ones the user curated.
    for tc in db.execute(
        select(TrackedCompetitor).where(
            TrackedCompetitor.domain_id == domain.id,
            TrackedCompetitor.source.in_(["auto", "ai"]),
        )
    ).scalars().all():
        db.delete(tc)
    db.flush()

    kept = set(_curated_hosts(db, domain))  # remaining = manual
    stored = 0
    for c in comps:
        host = keyword_analysis_service._norm_host(c["domain"])
        if not host or "." not in host or host in kept:
            continue
        db.add(TrackedCompetitor(
            domain_id=domain.id,
            competitor=host,
            label=c.get("name") or None,
            source="ai",
            reason=(c.get("reason") or None),
            confidence=(c.get("confidence_level") or None),
            match_score=c.get("match_score"),
        ))
        kept.add(host)
        stored += 1
    db.commit()

    # No simulated history: the trend fills from real refreshes (SERP snapshots).

    return {
        "domain": domain.domain,
        "target": target,
        "market": market,
        "count": stored,
        "profile": result.get("business") or None,
        "competitors": list_competitors(db, domain)["rows"],
    }


# ── Async discovery job (background worker + live step progress) ─────────────

def create_discovery_job(db: Session, domain: Domain) -> CompetitorDiscoveryJob:
    """Create a 'running' job row the UI can poll for live progress."""
    from app.services.competitor_discovery_service import DISCOVERY_STEPS

    job = CompetitorDiscoveryJob(
        domain_id=domain.id, status="running", step=0,
        step_label="Starting…", total_steps=len(DISCOVERY_STEPS),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def get_discovery_job(db: Session, job_id) -> CompetitorDiscoveryJob | None:
    return db.get(CompetitorDiscoveryJob, job_id)


def active_discovery_job(db: Session, domain: Domain) -> CompetitorDiscoveryJob | None:
    """Most recent still-running job for this domain (to avoid duplicate runs)."""
    return db.execute(
        select(CompetitorDiscoveryJob)
        .where(CompetitorDiscoveryJob.domain_id == domain.id,
               CompetitorDiscoveryJob.status == "running")
        .order_by(CompetitorDiscoveryJob.started_at.desc())
    ).scalars().first()


def run_discovery_job(
    workspace_id, domain_id, job_id, *, target_domain: str | None = None,
    limit: int = 10, extra_services: list[str] | None = None, refresh: bool = False,
    session_factory=None,
) -> None:
    """Background worker: runs the 3-agent discovery, updating the job row's step
    as it goes, and stores the result. Own DB session; never raises (the user's
    request already returned)."""
    db = (session_factory or SessionLocal)()
    job = None
    try:
        job = db.get(CompetitorDiscoveryJob, job_id)
        domain = db.get(Domain, domain_id)
        if job is None or domain is None or domain.workspace_id != workspace_id:
            return

        def _cb(step: int, label: str) -> None:
            job.step = step
            job.step_label = label
            db.add(job)
            db.commit()

        result = discover_and_store(
            db, domain, target_domain=target_domain, limit=limit,
            extra_services=extra_services, refresh=refresh, progress=_cb,
        )
        job.status = "done"
        job.step = job.total_steps
        job.step_label = "Done"
        job.count = int(result.get("count") or 0)
        job.finished_at = datetime.now(timezone.utc)
        db.add(job)
        db.commit()
    except Exception as exc:  # noqa: BLE001 - background worker, log & record
        from app.services.competitor_discovery_service import DiscoveryError
        import logging
        logging.getLogger("app.api").exception("discovery job %s failed", job_id)
        msg = ("We couldn't find competitors right now. Please try again."
               if isinstance(exc, DiscoveryError)
               else "Something went wrong while finding competitors.")
        try:
            if job is not None:
                db.rollback()
                job.status = "error"
                job.error = msg
                job.finished_at = datetime.now(timezone.utc)
                db.add(job)
                db.commit()
        except Exception:  # noqa: BLE001
            pass
    finally:
        db.close()


def get_business_profile(db: Session, domain: Domain) -> dict:
    """Return the business profile cached on the domain (the first-pass output of
    discovery), so the UI can show it above the competitor list without re-running
    the LLM. `profile` is null until the first discovery extracts it."""
    return {
        "domain": domain.domain,
        "target": domain.domain,
        "profile": domain.business_profile or None,
        "extracted_at": domain.business_profile_at,
    }


def refresh_business_profile(
    db: Session, domain: Domain, *, target_domain: str | None = None,
) -> dict:
    """Re-run ONLY the first pass of discovery: re-scrape the site and re-extract
    the business profile, then persist it on the domain. The competitor list is
    left untouched (this is the cheap "just fix my profile" path).

    The stored `market` is pinned to the domain's CONFIGURED country so the
    profile headline market matches the rest of the app, regardless of what the
    LLM infers from the page text.
    """
    from app.services import competitor_discovery_service as disc

    target = (target_domain or domain.domain).strip()
    profile = disc.extract_business_profile(target)

    # Pin the market to the domain's configured country (same rule discovery uses).
    cfg = domain_settings.resolve(domain, getattr(domain, "workspace", None))
    profile["market"] = domain_analytics_service.location_name_for(cfg.country)

    # Only cache it when profiling the domain itself (not an arbitrary target).
    own_host = keyword_analysis_service._norm_host(domain.domain)
    target_host = keyword_analysis_service._norm_host(target)
    if own_host and target_host == own_host:
        domain.business_profile = profile
        domain.business_profile_at = datetime.now(timezone.utc)
        db.add(domain)
        db.commit()
        db.refresh(domain)

    return {
        "domain": domain.domain,
        "target": target,
        "profile": profile,
        "extracted_at": domain.business_profile_at,
    }
