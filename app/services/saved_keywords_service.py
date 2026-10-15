"""My Keywords: save / list / remove a website's keywords.

The list is read in ONE query: each saved keyword joined with
  - its latest metrics from the shared keyword_metrics (same country/language),
  - its rank if it's tracked in Rank Tracking (latest + previous position),
sorted and paged in SQL (server-side pagination).
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.domain import Domain
from app.models.keyword_analysis import Keyword
from app.models.saved_keyword import SavedKeyword

MAX_PER_WEBSITE = 5000

SORTS = {
    "added": "s.created_at",
    "keyword": "s.normalized_keyword",
    "volume": "m.search_volume",
    "cpc": "m.cpc",
    "difficulty": "m.difficulty",
    "rank": "r.position",
}


def normalize(keyword: str) -> str:
    return " ".join((keyword or "").lower().split())


def _canonical_keyword_id(db: Session, kw: str, norm: str) -> uuid.UUID:
    """Find-or-create the master keyword for a term and return its id (race-safe
    via INSERT ... ON CONFLICT). Links saved keywords into the shared library."""
    return db.execute(
        insert(Keyword)
        .values(keyword=kw, normalized_keyword=norm)
        .on_conflict_do_update(index_elements=["normalized_keyword"], set_={"updated_at": func.now()})
        .returning(Keyword.id)
    ).scalar_one()


def save(db: Session, domain: Domain, keyword: str, *, country: str = "in", language: str = "en",
         source: str | None = None) -> tuple[SavedKeyword, bool]:
    """Save a keyword (idempotent). Returns (row, created)."""
    kw = " ".join((keyword or "").split())[:200]
    norm = normalize(kw)
    if not norm:
        raise ValueError("Keyword is empty.")
    country, language = (country or "in").lower()[:8], (language or "en").lower()[:10]
    existing = db.execute(select(SavedKeyword).where(
        SavedKeyword.domain_id == domain.id, SavedKeyword.normalized_keyword == norm,
        SavedKeyword.country_code == country)).scalar_one_or_none()
    if existing is not None:
        return existing, False
    count = db.execute(text("SELECT count(*) FROM saved_keywords WHERE domain_id = :d"), {"d": str(domain.id)}).scalar_one()
    if count >= MAX_PER_WEBSITE:
        raise ValueError(f"My Keywords is full ({MAX_PER_WEBSITE:,} keywords). Remove some first.")
    keyword_id = _canonical_keyword_id(db, kw, norm)
    row = db.execute(insert(SavedKeyword).values(
        domain_id=domain.id, keyword=kw, keyword_id=keyword_id, normalized_keyword=norm,
        country_code=country, language_code=language, source=(source or None) and source[:40],
    ).on_conflict_do_nothing(constraint="uq_saved_keyword").returning(SavedKeyword)).scalar_one_or_none()
    db.commit()
    if row is None:  # saved concurrently
        row = db.execute(select(SavedKeyword).where(
            SavedKeyword.domain_id == domain.id, SavedKeyword.normalized_keyword == norm,
            SavedKeyword.country_code == country)).scalar_one()
        return row, False
    return row, True


def remove(db: Session, domain: Domain, saved_id: str) -> bool:
    try:
        sid = uuid.UUID(saved_id)
    except ValueError:
        return False
    row = db.execute(select(SavedKeyword).where(SavedKeyword.id == sid, SavedKeyword.domain_id == domain.id)
                     ).scalar_one_or_none()
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def saved_set(db: Session, domain: Domain) -> list[str]:
    """Normalized keywords saved for the website (to mark rows as saved)."""
    return list(db.execute(select(SavedKeyword.normalized_keyword).where(SavedKeyword.domain_id == domain.id)).scalars())


def list_saved(db: Session, domain: Domain, *, q: str, page: int, page_size: int,
               sort: str | None, descending: bool) -> tuple[list[dict], int]:
    params = {"d": str(domain.id), "q": f"%{normalize(q)}%" if q else None,
              "limit": page_size, "offset": (page - 1) * page_size}
    where = "s.domain_id = :d" + (" AND s.normalized_keyword LIKE :q" if q else "")
    total = db.execute(text(f"SELECT count(*) FROM saved_keywords s WHERE {where}"), params).scalar_one()
    col = SORTS.get(sort or "", "s.created_at")
    order = f"{col} {'DESC' if descending else 'ASC'} NULLS LAST, s.normalized_keyword"
    rows = db.execute(text(f"""
        SELECT s.id, s.keyword, s.country_code, s.language_code, s.source, s.created_at,
               m.search_volume, m.cpc, m.difficulty, m.competition, m.intent, m.metric_date,
               r.tracked_id, r.position, r.previous_position, r.recorded_at
        FROM saved_keywords s
        LEFT JOIN LATERAL (
            SELECT km.search_volume, km.cpc, km.difficulty, km.competition, km.intent, km.metric_date
            FROM keywords k JOIN keyword_metrics km ON km.keyword_id = k.id
            WHERE k.normalized_keyword = s.normalized_keyword
              AND km.country_code = s.country_code
            ORDER BY (km.search_volume IS NULL), (km.language_code = s.language_code) DESC, km.metric_date DESC
            LIMIT 1
        ) m ON true
        LEFT JOIN LATERAL (
            SELECT t.id AS tracked_id,
                   (SELECT e.position FROM rank_entries e WHERE e.keyword_id = t.id ORDER BY e.recorded_at DESC LIMIT 1) AS position,
                   (SELECT e.position FROM rank_entries e WHERE e.keyword_id = t.id ORDER BY e.recorded_at DESC OFFSET 1 LIMIT 1) AS previous_position,
                   (SELECT e.recorded_at FROM rank_entries e WHERE e.keyword_id = t.id ORDER BY e.recorded_at DESC LIMIT 1) AS recorded_at
            FROM tracked_keywords t
            WHERE t.domain_id = s.domain_id AND lower(t.term) = s.normalized_keyword
            LIMIT 1
        ) r ON true
        WHERE {where}
        ORDER BY {order}
        LIMIT :limit OFFSET :offset
    """), params).mappings().all()
    out = []
    for r in rows:
        out.append({
            "id": str(r["id"]), "keyword": r["keyword"], "country": r["country_code"], "language": r["language_code"],
            "source": r["source"], "created_at": r["created_at"],
            "search_volume": r["search_volume"],
            "cpc": float(r["cpc"]) if r["cpc"] is not None else None,
            "difficulty": float(r["difficulty"]) if r["difficulty"] is not None else None,
            "competition": float(r["competition"]) if r["competition"] is not None else None,
            "intent": r["intent"],
            "metrics_date": r["metric_date"].isoformat() if r["metric_date"] else None,
            "rank_tracked": r["tracked_id"] is not None,
            "position": r["position"], "previous_position": r["previous_position"],
            "rank_date": r["recorded_at"].isoformat() if r["recorded_at"] else None,
        })
    return out, total
