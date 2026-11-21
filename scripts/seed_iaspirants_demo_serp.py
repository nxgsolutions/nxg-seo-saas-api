"""Seed demo SERP history for the zero-data iaspirants.com workspace so the
competitor rank-trend chart renders a real multi-week / multi-month line.

What it creates (all tagged `raw->>'seed' = SEED_TAG`, so it is fully
idempotent — a re-run deletes its previous rows first and never duplicates):

  * a handful of global `keywords` (reused by normalized text if they exist);
  * `domain_keywords` linking them to the target domain, which is what makes the
    workspace "own" them (`_workspace_keyword_ids`), so the timeline is scoped in;
  * weekly `serp_snapshots` + organic `serp_snapshot_items` across ~8 weeks, with
    each competitor's rank drifting over time so the lines actually move.

Target = the iaspirants.com domain whose workspace has NO owned keywords (the
"zero data" one). Pass --domain-id to override. Nothing here calls any external
API; it only writes local demo rows.

Run:  backend/.venv/Scripts/python.exe backend/scripts/seed_iaspirants_demo_serp.py
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db.session import SessionLocal  # noqa: E402
from app.models.domain import Domain  # noqa: E402
from app.models.keyword_analysis import (  # noqa: E402
    DomainKeyword,
    Keyword,
    SerpSnapshot,
    SerpSnapshotItem,
)
from app.services import keyword_analysis_service as kas  # noqa: E402

SEED_TAG = "iaspirants_demo"
COUNTRY = "in"
LANGUAGE = "en"
WEEKS = 8  # distinct weekly capture days (spans >= 2 months → both filters light up)

KEYWORDS = [
    "upsc coaching",
    "ias preparation",
    "current affairs for upsc",
    "upsc mains answer writing",
    "best civil services coaching",
    "upsc prelims test series",
]

# competitor host -> (base rank, weekly drift). Positive drift = slipping (rank
# climbs), negative = improving. Jitter is added per keyword for realism.
COMPETITORS = {
    "superkalam.com": (6, -0.4),
    "civilsdaily.com": (9, 0.3),
    "insightsias.com": (12, -0.2),
    "clearias.com": (15, 0.5),
    "forumias.com": (18, -0.6),
    "visionias.in": (4, 0.2),
    "drishtiias.com": (8, 0.1),
    "testbook.com": (22, -0.8),
    "unacademy.com": (26, 0.4),
}


def _normalize(kw: str) -> str:
    return " ".join(kw.lower().split())


def _get_or_create_keywords(db: Session) -> list[Keyword]:
    out: list[Keyword] = []
    for text in KEYWORDS:
        norm = _normalize(text)
        kw = db.execute(
            select(Keyword).where(Keyword.normalized_keyword == norm)
        ).scalars().first()
        if kw is None:
            kw = Keyword(keyword=text, normalized_keyword=norm)
            db.add(kw)
            db.flush()
        out.append(kw)
    return out


def _link_domain_keywords(db: Session, domain: Domain, keywords: list[Keyword]) -> int:
    linked = 0
    for kw in keywords:
        exists = db.execute(
            select(DomainKeyword.id).where(
                DomainKeyword.domain_id == domain.id,
                DomainKeyword.keyword_id == kw.id,
                DomainKeyword.country_code == COUNTRY,
                DomainKeyword.language_code == LANGUAGE,
            )
        ).first()
        if exists:
            continue
        db.add(DomainKeyword(
            domain_id=domain.id, keyword_id=kw.id,
            country_code=COUNTRY, language_code=LANGUAGE,
        ))
        linked += 1
    return linked


def _weekly_mondays(n: int) -> list[date]:
    """n weekly Mondays ending with the most recent past Monday, ascending."""
    today = date.today()
    last_monday = today - timedelta(days=today.weekday())
    return sorted(last_monday - timedelta(weeks=i) for i in range(n))


def _rank_for(base: float, drift: float, week_idx: int, kw_idx: int) -> int:
    # Deterministic small per-keyword jitter so lines aren't identical.
    jitter = ((kw_idx * 7) % 5) - 2
    r = round(base + drift * week_idx + jitter)
    return max(1, min(90, r))


def seed(db: Session, domain: Domain) -> dict:
    keywords = _get_or_create_keywords(db)
    linked = _link_domain_keywords(db, domain, keywords)
    kw_ids = [kw.id for kw in keywords]

    # Idempotency: drop this seed's previous snapshots for these keywords.
    old = db.execute(
        select(SerpSnapshot.id).where(
            SerpSnapshot.keyword_id.in_(kw_ids),
            SerpSnapshot.raw["seed"].astext == SEED_TAG,
        )
    ).scalars().all()
    if old:
        db.execute(delete(SerpSnapshot).where(SerpSnapshot.id.in_(old)))

    mondays = _weekly_mondays(WEEKS)
    snaps = 0
    items = 0
    for week_idx, day in enumerate(mondays):
        captured = datetime(day.year, day.month, day.day, 9, 0, tzinfo=timezone.utc)
        for kw_idx, kw in enumerate(keywords):
            ranked = sorted(
                (
                    (_rank_for(base, drift, week_idx, kw_idx), host)
                    for host, (base, drift) in COMPETITORS.items()
                ),
            )
            snap = SerpSnapshot(
                keyword_id=kw.id, search_engine="google", source="import",
                environment="live", device="desktop",
                country_code=COUNTRY, language_code=LANGUAGE,
                items_count=len(ranked), status="ok", captured_at=captured,
                raw={"seed": SEED_TAG},
            )
            db.add(snap)
            db.flush()
            snaps += 1
            for pos, (rank, host) in enumerate(ranked, start=1):
                db.add(SerpSnapshotItem(
                    snapshot_id=snap.id, type="organic",
                    rank_absolute=rank, rank_group=rank, domain=host,
                    url=f"https://{host}/", title=f"{kw.keyword} — {host}",
                ))
                items += 1
    return {"linked": linked, "snapshots": snaps, "items": items,
            "weeks": [str(m) for m in mondays]}


def _pick_domain(db: Session, domain_id: str | None) -> Domain:
    if domain_id:
        dom = db.get(Domain, domain_id)
        if dom is None:
            raise SystemExit(f"No domain with id {domain_id}")
        return dom
    # The iaspirants.com domain whose workspace owns no keywords (zero-data one).
    candidates = db.execute(
        select(Domain).where(Domain.domain.ilike("%iaspirants%"))
    ).scalars().all()
    zero = [
        d for d in candidates
        if d.workspace_id and not kas._workspace_keyword_ids(db, d.workspace_id)
    ]
    if not zero:
        raise SystemExit(
            "No zero-data iaspirants domain found — pass --domain-id explicitly."
        )
    return zero[0]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain-id", default=None, help="Target domain id (optional)")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        dom = _pick_domain(db, args.domain_id)
        print(f"Seeding domain {dom.domain} id={dom.id} workspace={dom.workspace_id}")
        result = seed(db, dom)
        db.commit()
        print("Done:", result)
    finally:
        db.close()


if __name__ == "__main__":
    main()
