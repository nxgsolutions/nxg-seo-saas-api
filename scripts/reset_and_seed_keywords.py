"""Clear keyword-research + SERP data and seed a fresh sample set.

Scope: ONLY the keyword/SERP tables are cleared. Users, tenants, workspaces,
domains, billing and GSC connections are left untouched.

Seeds, for the newest workspace:
  * a handful of UPSC keywords, each with a today-dated keyword_metrics row
    (Ads-style volume/cpc/competition + computed intent/difficulty/score),
  * a KeywordResearch run (kind='keyword') + its ranked results,
  * one DataForSEO-style SerpSnapshot for the seed with organic items.

Run:  .venv/Scripts/python.exe scripts/reset_and_seed_keywords.py
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

# Allow running directly (python scripts/reset_and_seed_keywords.py) — put the
# backend root (this file's parent's parent) on the path so `app` imports.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, text

from app.db.session import SessionLocal
from app.models.keyword_analysis import (
    Keyword,
    KeywordMetric,
    KeywordResearch,
    KeywordResearchResult,
    KeywordSource,
    SerpSnapshot,
    SerpSnapshotItem,
)
from app.models.workspace import Workspace
from app.services import keyword_scoring

# Tables cleared, child-before-parent so CASCADE order is safe either way.
_CLEAR = [
    "serp_snapshot_items",
    "serp_snapshots",
    "competitor_rank_snapshots",
    "keyword_research_results",
    "keyword_researches",
    "keyword_cluster_members",
    "keyword_clusters",
    "keyword_metrics",
    "keyword_sources",
    "keyword_relations",
    "domain_keywords",
    "domain_hosts",
    "keywords",
]

# (keyword, avg_monthly_searches, cpc_usd, competition_0_1)
_SEED = "upsc preparation"
_KEYWORDS = [
    (_SEED, 40500, 0.12, 0.08),
    ("upsc syllabus", 33100, 0.09, 0.05),
    ("best upsc coaching", 12100, 0.85, 0.62),
    ("upsc optional subjects", 8100, 0.21, 0.18),
    ("upsc previous year papers", 6600, 0.07, 0.04),
    ("upsc eligibility criteria", 5400, 0.10, 0.06),
]

# SERP for the seed: (domain, url, title) in rank order.
_SERP = [
    ("upsc.gov.in", "https://upsc.gov.in/", "Union Public Service Commission"),
    ("byjus.com", "https://byjus.com/free-ias-prep/", "UPSC Preparation - Free IAS Prep"),
    ("drishtiias.com", "https://www.drishtiias.com/", "Drishti IAS: UPSC Preparation"),
    ("iaspirants.com", "https://iaspirants.com/", "IAspirants - UPSC Study Material"),
    ("clearias.com", "https://www.clearias.com/", "ClearIAS: Self-Study for UPSC"),
]

_COUNTRY, _LANGUAGE = "in", "en"


def clear(db) -> None:
    db.execute(text("TRUNCATE " + ", ".join(_CLEAR) + " RESTART IDENTITY CASCADE;"))
    db.commit()
    print("cleared:", ", ".join(_CLEAR))


def seed(db) -> None:
    workspace = db.execute(
        select(Workspace).order_by(Workspace.created_at.desc()).limit(1)
    ).scalar_one_or_none()
    if workspace is None:
        raise SystemExit("No workspace found — create one before seeding.")
    print(f"seeding into workspace {workspace.slug!r} (tenant {workspace.tenant_id})")

    today = date.today()
    keyword_rows: dict[str, Keyword] = {}
    scored: list[tuple[float, Keyword]] = []

    for term, volume, cpc, competition in _KEYWORDS:
        kw = Keyword(keyword=term, normalized_keyword=keyword_scoring.normalize_keyword(term))
        db.add(kw)
        db.flush()
        keyword_rows[term] = kw

        db.add(KeywordSource(keyword_id=kw.id, source_type="seed" if term == _SEED else "related",
                             source_reference=_SEED))

        difficulty = keyword_scoring.heuristic_difficulty(term)
        score = keyword_scoring.opportunity_score(volume, None, difficulty)
        db.add(KeywordMetric(
            keyword_id=kw.id, country_code=_COUNTRY, language_code=_LANGUAGE,
            search_engine="google", device="desktop", metric_date=today,
            search_volume=volume, cpc=cpc, competition=competition, difficulty=difficulty,
            intent=keyword_scoring.classify_intent(term),
            relevance_score=keyword_scoring.relevance_score(term, _SEED),
            opportunity_score=score, confidence_score=70.0,
        ))
        scored.append((score, kw))

    seed_kw = keyword_rows[_SEED]
    research = KeywordResearch(
        workspace_id=workspace.id, domain_id=None, seed_keyword_id=seed_kw.id,
        country_code=_COUNTRY, language_code=_LANGUAGE, status="completed",
        kind="keyword", completed_at=datetime.now(timezone.utc),
    )
    db.add(research)
    db.flush()

    scored.sort(key=lambda pair: pair[0], reverse=True)
    for rank, (_s, kw) in enumerate(scored, start=1):
        db.add(KeywordResearchResult(research_id=research.id, keyword_id=kw.id, rank=rank))

    snapshot = SerpSnapshot(
        keyword_id=seed_kw.id, search_engine="google", source="dataforseo",
        device="desktop", country_code=_COUNTRY, language_code=_LANGUAGE,
        items_count=len(_SERP), status="ok",
    )
    snapshot.items = [
        SerpSnapshotItem(type="organic", rank_absolute=i, rank_group=i,
                         domain=dom, url=url, title=title)
        for i, (dom, url, title) in enumerate(_SERP, start=1)
    ]
    db.add(snapshot)

    db.commit()
    print(f"seeded: {len(_KEYWORDS)} keywords, 1 research run, "
          f"1 SERP snapshot ({len(_SERP)} items)")


def main() -> None:
    db = SessionLocal()
    try:
        clear(db)
        seed(db)
    finally:
        db.close()


if __name__ == "__main__":
    main()
