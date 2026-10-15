"""Seed realistic Opportunity Research demo data.

Idempotent per domain: wipes the domain's opportunity_* rows and reinserts
pages, target keywords (with volume/rank/intent/SERP features/opportunity score/
competitors) and 13 months of trend history. Seeds ALL domains by default, or
one via --domain <substring>.

Run:  .venv/Scripts/python -m scripts.seed_opportunity
"""

from __future__ import annotations

import random
import sys
from datetime import date

from sqlalchemy import delete, select

from app.db.session import SessionLocal
from app.models.domain import Domain
from app.models.opportunity import OpportunityKeyword, OpportunityPage, OpportunityTrend
from app.services.keyword_analysis_service import _upsert_keyword

random.seed(42)

PAGES = [
    ("/upsc-preparation", "UPSC Preparation Guide"),
    ("/ias-exam", "IAS Exam Overview"),
    ("/current-affairs", "Daily Current Affairs"),
    ("/mock-tests", "UPSC Mock Tests & Test Series"),
    ("/study-material", "UPSC Study Material & Notes"),
    ("/optional-subjects", "UPSC Optional Subjects"),
    ("/answer-writing", "UPSC Mains Answer Writing"),
    ("/upsc-syllabus", "UPSC Syllabus & Exam Pattern"),
]

# keyword -> (page index, base volume, intent)
KEYWORDS = [
    ("upsc preparation", 0, 368000, "informational"),
    ("how to prepare for upsc", 0, 90000, "informational"),
    ("upsc preparation strategy", 0, 60000, "informational"),
    ("upsc preparation for beginners", 0, 40000, "informational"),
    ("upsc preparation without coaching", 0, 33000, "informational"),
    ("upsc online coaching", 0, 110000, "commercial"),
    ("best online coaching for upsc", 0, 74000, "commercial"),
    ("ias exam", 1, 301000, "informational"),
    ("ias exam pattern", 1, 74000, "informational"),
    ("ias eligibility", 1, 55000, "informational"),
    ("ias exam date", 1, 49000, "navigational"),
    ("ias salary", 1, 61000, "informational"),
    ("ias officer", 1, 90000, "informational"),
    ("how to become an ias officer", 1, 82000, "informational"),
    ("upsc current affairs", 2, 138000, "informational"),
    ("daily current affairs", 2, 88000, "informational"),
    ("monthly current affairs pdf", 2, 42000, "transactional"),
    ("current affairs quiz", 2, 39000, "transactional"),
    ("upsc editorial analysis", 2, 21000, "informational"),
    ("the hindu editorial analysis", 2, 60000, "informational"),
    ("weekly current affairs", 2, 28000, "informational"),
    ("upsc mock test", 3, 142000, "transactional"),
    ("upsc prelims test series", 3, 69000, "transactional"),
    ("upsc mains test series", 3, 33000, "transactional"),
    ("upsc daily quiz", 3, 39000, "transactional"),
    ("upsc pyq", 3, 121000, "transactional"),
    ("upsc prelims mock test", 3, 88000, "transactional"),
    ("free upsc mock test", 3, 54000, "transactional"),
    ("upsc study material", 4, 64000, "transactional"),
    ("best books for upsc", 4, 52000, "commercial"),
    ("upsc notes pdf", 4, 46000, "transactional"),
    ("ncert notes for upsc", 4, 31000, "transactional"),
    ("upsc booklist", 4, 36000, "informational"),
    ("upsc study material free pdf", 4, 40000, "transactional"),
    ("upsc optional subjects", 5, 88000, "informational"),
    ("best optional subject for upsc", 5, 66000, "informational"),
    ("upsc optional subject list", 5, 44000, "informational"),
    ("sociology optional for upsc", 5, 30000, "informational"),
    ("psir optional for upsc", 5, 27000, "informational"),
    ("geography optional for upsc", 5, 25000, "informational"),
    ("upsc answer writing", 6, 74000, "informational"),
    ("upsc mains answer writing practice", 6, 41000, "transactional"),
    ("how to write answers in upsc mains", 6, 29000, "informational"),
    ("upsc answer writing practice online", 6, 23000, "transactional"),
    ("ai mains answer evaluation", 6, 12000, "transactional"),
    ("upsc syllabus", 7, 246000, "informational"),
    ("upsc prelims syllabus", 7, 96000, "informational"),
    ("upsc mains syllabus", 7, 71000, "informational"),
    ("upsc exam pattern", 7, 58000, "informational"),
    ("upsc syllabus pdf", 7, 64000, "transactional"),
]

SERP_FEATURES = ["sitelinks", "reviews", "image", "snippet", "video"]
COMPETITOR_POOL = [
    "drishtiias.com", "visionias.in", "testbook.com", "forumias.com",
    "byjus.com", "unacademy.com", "superkalam.com", "clearias.com",
    "insightsonindia.com", "nextias.com",
]


def _opportunity_score(volume: int, position: int, kd: int) -> int:
    """Higher volume + worse (higher) current position + lower difficulty => more
    opportunity. Normalized 0-100."""
    vol_n = min(1.0, volume / 300000)
    pos_n = min(1.0, position / 20)          # more room to climb
    kd_n = 1 - min(1.0, kd / 100)
    return max(1, min(100, round((0.5 * vol_n + 0.3 * pos_n + 0.2 * kd_n) * 100)))


def seed_domain(db, domain: Domain) -> None:
    # Wipe existing opportunity data for this domain (idempotent).
    db.execute(delete(OpportunityKeyword).where(OpportunityKeyword.domain_id == domain.id))
    db.execute(delete(OpportunityTrend).where(OpportunityTrend.domain_id == domain.id))
    db.execute(delete(OpportunityPage).where(OpportunityPage.domain_id == domain.id))
    db.flush()

    base = f"https://{domain.domain}"
    pages: list[OpportunityPage] = []
    for slug, title in PAGES:
        p = OpportunityPage(domain_id=domain.id, url=base + slug, title=title)
        db.add(p)
        pages.append(p)
    db.flush()

    for term, page_idx, volume, intent in KEYWORDS:
        position = random.randint(3, 18)
        kd = random.randint(28, 68)
        yoy = round(random.uniform(-8, 35), 1)
        feats = random.sample(SERP_FEATURES, k=random.randint(1, 4))
        comps = [
            {"domain": d, "position": random.randint(1, 10)}
            for d in random.sample(COMPETITOR_POOL, k=3)
        ]
        canonical = _upsert_keyword(db, term)  # link the canonical keyword row
        db.add(OpportunityKeyword(
            domain_id=domain.id,
            page_id=pages[page_idx].id,
            keyword_id=canonical.id,
            keyword=term,
            search_volume=volume,
            yoy_change=yoy,
            keyword_difficulty=kd,
            current_position=position,
            intent=intent,
            opportunity_score=_opportunity_score(volume, position, kd),
            cpc=round(random.uniform(0.2, 4.0), 2),
            ranking_url=pages[page_idx].url,
            serp_features=feats,
            competitors=comps,
            country_code="in",
        ))

    # 13 months of trend history ending this month.
    today = date.today()
    organic = random.randint(30000, 40000)
    ranking = 18000
    serp = 285000
    for k in range(12, -1, -1):
        # step back k months (approx 30 days)
        d = date.fromordinal(today.toordinal() - k * 30)
        organic = max(15000, int(organic + random.uniform(-6000, 6000)))
        ranking = int(ranking + random.uniform(200, 800))
        serp = int(serp + random.uniform(-4000, 8000))
        db.add(OpportunityTrend(
            domain_id=domain.id, captured_on=d,
            organic_traffic=organic, ranking_keywords=ranking, serp_opportunities=serp,
        ))

    db.commit()


def main() -> None:
    substr = None
    if "--domain" in sys.argv:
        substr = sys.argv[sys.argv.index("--domain") + 1].lower()
    db = SessionLocal()
    try:
        q = select(Domain)
        domains = db.execute(q).scalars().all()
        if substr:
            domains = [d for d in domains if substr in d.domain.lower()]
        if not domains:
            print("No domains found to seed.")
            return
        for d in domains:
            seed_domain(db, d)
            print(f"  seeded {d.domain} ({d.id})")
        print(f"Done — seeded {len(domains)} domain(s).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
