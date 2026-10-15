"""Competitor tracking service.

Competitors and their SERP standing come from `serp_snapshots` (reusing
`keyword_analysis_service.competitor_domains`). A refresh computes the current
standing and writes one dated row per competitor into
`competitor_rank_snapshots`; the accumulated rows form the rank-change time
series. Only real captured snapshots are stored — the trend fills in as the
user refreshes over time (no demo / simulated data).
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.competitor import CompetitorRankSnapshot, TrackedCompetitor
from app.models.domain import Domain
from app.services import keyword_analysis_service

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

_MARKET_TO_CC = {
    "india": "in", "united states": "us", "usa": "us", "united kingdom": "gb",
    "uk": "gb", "canada": "ca", "australia": "au",
}


def discover_and_store(
    db: Session, domain: Domain, *, target_domain: str | None = None,
    limit: int = 10, extra_services: list[str] | None = None,
) -> dict:
    """Run the LLM competitor finder for `target_domain` (defaults to the domain
    itself), replace the auto/AI-sourced curated entries with the results (keeping
    the user's manual ones), then snapshot ranks for any with SERP data.
    `extra_services` steers the match toward features not yet on the site."""
    from app.services import competitor_discovery_service as disc

    target = (target_domain or domain.domain).strip()
    result = disc.discover(target, limit=limit, extra_services=extra_services)
    comps = result["competitors"]
    market = result.get("market") or "India"
    country = _MARKET_TO_CC.get(market.strip().lower(), "in")

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
        "competitors": list_competitors(db, domain)["rows"],
    }
