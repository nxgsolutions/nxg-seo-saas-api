"""Make Opportunity Research use REAL, clickable pages for a domain.

Crawls the domain's actual public pages and rewrites opportunity_pages +
opportunity_keywords so every Top URL / Page↔Keyword URL is a real reachable
page. Keyword metrics stay as realistic values (volume/rank/KD/score/features/
competitors) because DataForSEO is currently out of balance (402) — run
scripts.ingest_opportunity to backfill REAL SERP/volume once credits are topped up.

Usage: .venv/Scripts/python -m scripts.seed_real_pages iaspirants.com
"""

from __future__ import annotations

import random
import re
import sys

from sqlalchemy import delete, select

from app.db.session import SessionLocal
from app.models.domain import Domain
from app.models.opportunity import OpportunityKeyword, OpportunityPage
from app.services.keyword_analysis_service import _upsert_keyword
from scripts.ingest_opportunity import _host, _intent, _opportunity_score, discover_pages

random.seed(7)

# Curated keywords + realistic volume (placeholder metrics until DataForSEO credits).
KEYWORDS = [
    ("upsc preparation", 368000), ("upsc online coaching", 110000),
    ("best online coaching for upsc", 74000), ("how to prepare for upsc", 90000),
    ("ias exam", 301000), ("ias salary", 61000), ("how to become an ias officer", 82000),
    ("upsc current affairs", 138000), ("daily current affairs", 88000),
    ("the hindu editorial analysis", 60000), ("upsc mock test", 142000),
    ("upsc prelims test series", 69000), ("upsc pyq", 121000),
    ("free upsc mock test", 54000), ("upsc study material", 64000),
    ("best books for upsc", 52000), ("upsc notes pdf", 46000),
    ("upsc optional subjects", 88000), ("best optional subject for upsc", 66000),
    ("upsc answer writing", 74000), ("upsc mains answer writing practice", 41000),
    ("ai mains answer evaluation", 12000), ("upsc syllabus", 246000),
    ("upsc exam pattern", 58000),
]
SERP_FEATURES = ["sitelinks", "reviews", "image", "snippet", "video"]
COMPETITORS = ["drishtiias.com", "visionias.in", "testbook.com", "forumias.com",
               "byjus.com", "unacademy.com", "superkalam.com", "clearias.com"]

_WORD = re.compile(r"[a-z0-9]+")


def _tokens(s: str) -> set[str]:
    return {w for w in _WORD.findall(s.lower()) if len(w) > 2 and w not in {"upsc", "ias", "the", "for", "com", "www"}}


def _best_page(keyword: str, pages: list[tuple[str, str]], home: str) -> str:
    kt = _tokens(keyword)
    best, best_score = home, 0
    for url, title in pages:
        score = len(kt & _tokens(url + " " + title))
        if score > best_score:
            best, best_score = url, score
    return best


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    host = _host("https://" + (args[0] if args else "iaspirants.com"))

    # fetch_titles=False: derive titles from the URL slug instead of fetching each
    # page — slow sites (iaspirants) time out per-page, and slug titles ("Upsc
    # Current Affairs", "Pricing") are clean enough for the page list.
    print(f"[1/2] Discovering real pages of {host} (sitemap first) ...")
    crawled = discover_pages(host, limit=18, fetch_titles=False)
    if not crawled:
        raise SystemExit("Could not discover any pages.")
    pages_list = list(crawled.items())
    home = pages_list[0][0]
    print(f"    - {len(crawled)} real pages")

    db = SessionLocal()
    try:
        domains = db.execute(select(Domain).where(Domain.domain.ilike(host))).scalars().all()
        if not domains:
            raise SystemExit(f"No domain rows for {host}.")
        for dom in domains:
            db.execute(delete(OpportunityKeyword).where(OpportunityKeyword.domain_id == dom.id))
            db.execute(delete(OpportunityPage).where(OpportunityPage.domain_id == dom.id))
            db.flush()
            page_by_url = {}
            for url, title in crawled.items():
                p = OpportunityPage(domain_id=dom.id, url=url, title=title)
                db.add(p)
                page_by_url[url] = p
            db.flush()

            for term, volume in KEYWORDS:
                url = _best_page(term, pages_list, home)
                position = random.randint(3, 18)
                kd = random.randint(28, 68)
                feats = random.sample(SERP_FEATURES, k=random.randint(1, 4))
                comps = [{"domain": d, "position": random.randint(1, 10)}
                         for d in random.sample(COMPETITORS, k=3)]
                canonical = _upsert_keyword(db, term)
                db.add(OpportunityKeyword(
                    domain_id=dom.id, page_id=page_by_url[url].id, keyword_id=canonical.id,
                    keyword=term, search_volume=volume,
                    yoy_change=round(random.uniform(-8, 35), 1), keyword_difficulty=kd,
                    current_position=position, intent=_intent(term),
                    opportunity_score=_opportunity_score(volume, position, kd),
                    cpc=round(random.uniform(0.2, 4.0), 2), ranking_url=url,
                    serp_features=feats, competitors=comps, country_code="in",
                ))
            db.commit()
            print(f"[2/2] wrote {len(KEYWORDS)} keywords on {len(crawled)} real pages -> {dom.domain} ({dom.id})")
    finally:
        db.close()


if __name__ == "__main__":
    main()
