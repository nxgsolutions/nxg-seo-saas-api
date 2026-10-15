"""Additive seed to guarantee every table has >= 2-3 rows.

Only fills the tables that were empty/short (domain_hosts, domain_keywords,
keyword_relations, keyword_clusters, keyword_cluster_members,
competitor_rank_snapshots) and tops up keyword_researches / serp_snapshots.
All inserts are ON CONFLICT DO NOTHING, so it is safe to re-run and never
touches auth/tenant/billing rows.

Run:  .venv/Scripts/python.exe scripts/seed_all_tables.py
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

from app.db.session import SessionLocal  # noqa: E402
from app.models.competitor import CompetitorRankSnapshot  # noqa: E402
from app.models.domain import Domain  # noqa: E402
from app.models.keyword_analysis import (  # noqa: E402
    DomainHost,
    DomainKeyword,
    Keyword,
    KeywordCluster,
    KeywordClusterMember,
    KeywordRelation,
    KeywordResearch,
    KeywordResearchResult,
    SerpSnapshot,
    SerpSnapshotItem,
)

_COUNTRY, _LANGUAGE = "in", "en"


def _do_nothing(model, values: list[dict], constraint: str | None = None,
                elements: list[str] | None = None):
    """Return a pg_insert...on_conflict_do_nothing statement for a batch."""
    stmt = pg_insert(model).values(values)
    if constraint:
        return stmt.on_conflict_do_nothing(constraint=constraint)
    return stmt.on_conflict_do_nothing(index_elements=elements)


def main() -> None:
    db = SessionLocal()
    try:
        # Anchor on a domain that belongs to a workspace, plus the 6 seed keywords.
        domain = db.execute(select(Domain).limit(1)).scalar_one_or_none()
        if domain is None:
            raise SystemExit("No domain found — seed a domain first.")
        keywords = list(db.execute(select(Keyword).order_by(Keyword.created_at).limit(6)).scalars())
        if len(keywords) < 4:
            raise SystemExit("Run reset_and_seed_keywords.py first (need >=4 keywords).")
        ws_id = domain.workspace_id
        kids = [k.id for k in keywords]
        seed_kw = keywords[0]
        print(f"anchor domain={domain.domain!r} workspace={ws_id}")

        # 1. domain_hosts (root + 2 subdomains).
        db.execute(_do_nothing(DomainHost, [
            {"domain_id": domain.id, "host": domain.domain, "host_type": "root", "is_primary": True},
            {"domain_id": domain.id, "host": f"blog.{domain.domain}", "host_type": "subdomain", "is_primary": False},
            {"domain_id": domain.id, "host": f"help.{domain.domain}", "host_type": "subdomain", "is_primary": False},
        ], constraint="uq_domain_host"))

        # 2. domain_keywords (link first 3 keywords to the domain).
        db.execute(_do_nothing(DomainKeyword, [
            {"domain_id": domain.id, "keyword_id": kid, "country_code": _COUNTRY, "language_code": _LANGUAGE}
            for kid in kids[:3]
        ], constraint="uq_domain_keyword"))

        # 3. keyword_relations (seed -> 3 related, no self-links).
        db.execute(_do_nothing(KeywordRelation, [
            {"keyword_id": seed_kw.id, "related_keyword_id": kid, "relation_type": "related", "score": round(0.9 - i * 0.1, 2)}
            for i, kid in enumerate(kids[1:4], start=1)
        ], constraint="uq_keyword_relation"))

        # 4. keyword_clusters (2) + members (3 in the first).
        db.execute(pg_insert(KeywordCluster).values([
            {"workspace_id": ws_id, "domain_id": domain.id, "label": "UPSC Core Prep",
             "head_keyword_id": seed_kw.id, "size": 3, "total_volume": 81700, "source": "lexical"},
            {"workspace_id": ws_id, "domain_id": domain.id, "label": "UPSC Coaching & Papers",
             "head_keyword_id": None, "size": 2, "total_volume": 18700, "source": "lexical"},
        ]).on_conflict_do_nothing())
        db.commit()

        cluster = db.execute(
            select(KeywordCluster).where(KeywordCluster.label == "UPSC Core Prep").limit(1)
        ).scalar_one()
        db.execute(_do_nothing(KeywordClusterMember, [
            {"cluster_id": cluster.id, "keyword_id": kid, "score": round(1.0 - i * 0.1, 2),
             "is_head": (kid == seed_kw.id)}
            for i, kid in enumerate(kids[:3])
        ], constraint="uq_cluster_member"))

        # 5. competitor_rank_snapshots (3 competitors, today).
        today = date.today()
        db.execute(_do_nothing(CompetitorRankSnapshot, [
            {"domain_id": domain.id, "competitor": "byjus.com", "captured_on": today,
             "avg_position": 6.4, "best_position": 2, "keywords_shared": 4,
             "country_code": _COUNTRY, "language_code": _LANGUAGE, "is_simulated": True},
            {"domain_id": domain.id, "competitor": "drishtiias.com", "captured_on": today,
             "avg_position": 8.1, "best_position": 3, "keywords_shared": 3,
             "country_code": _COUNTRY, "language_code": _LANGUAGE, "is_simulated": True},
            {"domain_id": domain.id, "competitor": "clearias.com", "captured_on": today,
             "avg_position": 11.7, "best_position": 5, "keywords_shared": 2,
             "country_code": _COUNTRY, "language_code": _LANGUAGE, "is_simulated": True},
        ], constraint="uq_competitor_domain_date"))

        # 6. Top up keyword_researches (+2) and serp_snapshots (+2) so both are >=3.
        for i, kw in enumerate(keywords[1:3], start=1):
            research = KeywordResearch(
                workspace_id=ws_id, domain_id=domain.id, seed_keyword_id=kw.id,
                country_code=_COUNTRY, language_code=_LANGUAGE, status="completed",
                kind="keyword", completed_at=datetime.now(timezone.utc),
            )
            db.add(research)
            db.flush()
            for rank, k in enumerate(keywords[:3], start=1):
                db.add(KeywordResearchResult(research_id=research.id, keyword_id=k.id, rank=rank))

            snap = SerpSnapshot(
                keyword_id=kw.id, search_engine="google", source="dataforseo",
                device="desktop", country_code=_COUNTRY, language_code=_LANGUAGE,
                items_count=3, status="ok",
                captured_at=datetime.now(timezone.utc) - timedelta(days=i),
            )
            snap.items = [
                SerpSnapshotItem(type="organic", rank_absolute=r, rank_group=r,
                                 domain=d, url=f"https://{d}/", title=d)
                for r, d in enumerate(["byjus.com", "drishtiias.com", "iaspirants.com"], start=1)
            ]
            db.add(snap)

        db.commit()

        # Report final counts for the filled tables.
        for t in ["domain_hosts", "domain_keywords", "keyword_relations",
                  "keyword_clusters", "keyword_cluster_members",
                  "competitor_rank_snapshots", "keyword_researches", "serp_snapshots"]:
            print(f"{t:28}", db.execute(text(f"SELECT count(*) FROM {t}")).scalar())
    finally:
        db.close()


if __name__ == "__main__":
    main()
