"""Rank tracking business logic (tracked keywords + position entries)."""

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models.keyword_analysis import Keyword
from app.models.ranking import RankEntry, TrackedKeyword
from app.models.domain import Domain
from app.services import keyword_scoring


class KeywordExistsError(Exception):
    """The keyword is already tracked for this domain."""


@dataclass
class KeywordSummary:
    keyword: TrackedKeyword
    latest_position: int | None
    previous_position: int | None
    best_position: int | None
    entry_count: int
    # Market signals joined from the shared keyword_metrics (None when unknown).
    search_volume: int | None = None
    difficulty: float | None = None
    intent: str | None = None


def _link_keyword_id(db: Session, term: str) -> uuid.UUID:
    """Find-or-create the global canonical keyword for a term and return its id.

    Links the Keyword List into the shared keyword library so metrics can join.
    INSERT ... ON CONFLICT is race-safe and RETURNING yields the row either way.
    """
    normalized = keyword_scoring.normalize_keyword(term)
    return db.execute(
        pg_insert(Keyword)
        .values(keyword=term.strip(), normalized_keyword=normalized)
        .on_conflict_do_update(
            index_elements=["normalized_keyword"], set_={"updated_at": func.now()}
        )
        .returning(Keyword.id)
    ).scalar_one()


def add_keyword(db: Session, domain: Domain, term: str) -> TrackedKeyword:
    term = term.strip()
    kw = TrackedKeyword(
        domain_id=domain.id, term=term, keyword_id=_link_keyword_id(db, term)
    )
    db.add(kw)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise KeywordExistsError(term) from exc
    db.refresh(kw)
    return kw


def get_keyword(
    db: Session, domain: Domain, keyword_id: str
) -> TrackedKeyword | None:
    try:
        parsed = uuid.UUID(keyword_id)
    except (ValueError, TypeError):
        return None
    return db.execute(
        select(TrackedKeyword).where(
            TrackedKeyword.id == parsed,
            TrackedKeyword.domain_id == domain.id,
        )
    ).scalar_one_or_none()


def delete_keyword(db: Session, domain: Domain, keyword_id: str) -> bool:
    kw = get_keyword(db, domain, keyword_id)
    if kw is None:
        return False
    db.delete(kw)
    db.commit()
    return True


def _metrics_by_tracked(db: Session, domain: Domain) -> dict[uuid.UUID, dict]:
    """Best available keyword_metrics row per tracked keyword, in one query.

    Prefers the canonical link (keyword_id); falls back to matching the
    normalized term. Picks the newest row that actually has a search volume."""
    rows = db.execute(
        text(
            """
            SELECT t.id AS tracked_id,
                   m.search_volume, m.difficulty, m.intent
            FROM tracked_keywords t
            LEFT JOIN LATERAL (
                SELECT km.search_volume, km.difficulty, km.intent
                FROM keyword_metrics km
                JOIN keywords k ON k.id = km.keyword_id
                WHERE (t.keyword_id IS NOT NULL AND k.id = t.keyword_id)
                   OR (t.keyword_id IS NULL AND k.normalized_keyword = lower(btrim(t.term)))
                ORDER BY (km.search_volume IS NULL), km.metric_date DESC
                LIMIT 1
            ) m ON true
            WHERE t.domain_id = :d
            """
        ),
        {"d": str(domain.id)},
    ).mappings().all()
    return {
        r["tracked_id"]: {
            "search_volume": r["search_volume"],
            "difficulty": float(r["difficulty"]) if r["difficulty"] is not None else None,
            "intent": r["intent"],
        }
        for r in rows
    }


def list_keyword_summaries(
    db: Session, domain: Domain
) -> list[KeywordSummary]:
    keywords = list(
        db.execute(
            select(TrackedKeyword)
            .where(TrackedKeyword.domain_id == domain.id)
            .options(selectinload(TrackedKeyword.entries))
            .order_by(TrackedKeyword.created_at.asc())
        ).scalars()
    )
    metrics = _metrics_by_tracked(db, domain)
    summaries: list[KeywordSummary] = []
    for kw in keywords:
        # entries are ordered by recorded_at asc (relationship order_by).
        entries = kw.entries
        positions = [e.position for e in entries if e.position is not None]
        latest = entries[-1].position if entries else None
        previous = entries[-2].position if len(entries) >= 2 else None
        best = min(positions) if positions else None
        m = metrics.get(kw.id) or {}
        summaries.append(
            KeywordSummary(
                keyword=kw,
                latest_position=latest,
                previous_position=previous,
                best_position=best,
                entry_count=len(entries),
                search_volume=m.get("search_volume"),
                difficulty=m.get("difficulty"),
                intent=m.get("intent"),
            )
        )
    return summaries


def list_entries(db: Session, keyword: TrackedKeyword) -> list[RankEntry]:
    return list(
        db.execute(
            select(RankEntry)
            .where(RankEntry.keyword_id == keyword.id)
            .order_by(RankEntry.recorded_at.asc())
        ).scalars()
    )


# How many top Search Console queries to import when the user asks to pull
# tracked keywords from GSC (mirrors the first-sync auto-track, but user-triggered).
IMPORT_GSC_LIMIT = 25


def import_from_gsc(
    db: Session,
    domain: Domain,
    gsc_keywords: list[dict],
    recorded_at: date,
) -> dict:
    """Create tracked keywords from the domain's top Search Console queries
    (most clicks, then impressions) and record today's position for each.

    Already-tracked terms are skipped (never duplicated) but still get a position
    recorded by the ``sync_from_gsc`` pass below. Operator/scraper queries and
    absurdly long terms are filtered out.

    Returns {"imported": n_new, "synced": n_positions, "total": tracked_count}.
    """
    from app.services import keyword_analysis_service as kas

    existing = {
        (t or "").strip().lower()
        for t in db.execute(
            select(TrackedKeyword.term).where(TrackedKeyword.domain_id == domain.id)
        ).scalars()
    }

    imported = 0
    seen: set[str] = set()
    for row in sorted(
        gsc_keywords,
        key=lambda r: (-(r.get("clicks") or 0), -(r.get("impressions") or 0)),
    ):
        if imported >= IMPORT_GSC_LIMIT:
            break
        term = (row.get("keyword") or "").strip()
        low = term.lower()
        if not term or len(term) > 200 or low in seen or low in existing:
            continue
        if kas._is_operator_query(term):
            continue
        seen.add(low)
        try:
            add_keyword(db, domain, term)  # commits per keyword
            imported += 1
        except KeywordExistsError:
            # Raced with another add; treat as already tracked.
            continue

    result = sync_from_gsc(db, domain, gsc_keywords, recorded_at)
    return {"imported": imported, "synced": result["synced"], "total": result["total"]}


def sync_from_gsc(
    db: Session,
    domain: Domain,
    gsc_keywords: list[dict],
    recorded_at: date,
) -> dict:
    """Record today's real position for each tracked keyword that appears in the
    Search Console query data. Matching is case-insensitive on the exact term.

    Returns {"synced": n, "total": m} where m is the tracked-keyword count.
    """
    pos_map: dict[str, int] = {}
    for row in gsc_keywords:
        term = (row.get("keyword") or "").strip().lower()
        position = row.get("position")
        if not term or not position:
            continue
        rounded = round(position)
        # Keep the best (lowest) position if a term appears more than once.
        if term not in pos_map or rounded < pos_map[term]:
            pos_map[term] = rounded

    keywords = list(
        db.execute(
            select(TrackedKeyword).where(TrackedKeyword.domain_id == domain.id)
        ).scalars()
    )
    synced = 0
    for kw in keywords:
        p = pos_map.get(kw.term.strip().lower())
        if p is not None:
            record_position(db, kw, p, recorded_at)
            synced += 1
    return {"synced": synced, "total": len(keywords)}


def sync_from_serp_snapshot(db: Session, domain: Domain) -> dict:
    """Record positions for tracked keywords from the domain's latest SERP
    snapshot (domain_ranked_keywords — DataForSEO "ranked keywords" already
    pulled). Matching is case-insensitive on the exact term; the entry is dated
    to the snapshot's fetch date so the trend reflects when it was measured.

    Returns {"synced": n, "total": m, "recorded_at": date|None}. recorded_at is
    None when the domain has no SERP snapshot yet (nothing to sync).
    """
    host = domain.domain.strip().lower().removeprefix("www.")
    run = db.execute(
        text(
            """
            SELECT id, fetched_at FROM domain_ranked_keyword_runs
            WHERE lower(btrim(target)) IN (:h, :wh)
            ORDER BY fetched_at DESC
            LIMIT 1
            """
        ),
        {"h": host, "wh": "www." + host},
    ).mappings().first()

    keywords = list(
        db.execute(
            select(TrackedKeyword).where(TrackedKeyword.domain_id == domain.id)
        ).scalars()
    )
    if run is None:
        return {"synced": 0, "total": len(keywords), "recorded_at": None}

    rows = db.execute(
        text(
            """
            SELECT lower(btrim(keyword)) AS kw, min(rank_absolute) AS pos
            FROM domain_ranked_keywords
            WHERE run_id = :rid AND rank_absolute IS NOT NULL
            GROUP BY lower(btrim(keyword))
            """
        ),
        {"rid": run["id"]},
    ).mappings().all()
    pos_map = {r["kw"]: int(r["pos"]) for r in rows}

    recorded_at = run["fetched_at"].date()
    synced = 0
    for kw in keywords:
        p = pos_map.get(kw.term.strip().lower())
        if p is not None:
            record_position(db, kw, p, recorded_at)
            synced += 1
    return {"synced": synced, "total": len(keywords), "recorded_at": recorded_at}


def record_position(
    db: Session,
    keyword: TrackedKeyword,
    position: int | None,
    recorded_at: date,
) -> RankEntry:
    """Upsert a position for a keyword on a given date."""
    existing = db.execute(
        select(RankEntry).where(
            RankEntry.keyword_id == keyword.id,
            RankEntry.recorded_at == recorded_at,
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.position = position
        db.commit()
        db.refresh(existing)
        return existing

    entry = RankEntry(
        keyword_id=keyword.id, position=position, recorded_at=recorded_at
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry
