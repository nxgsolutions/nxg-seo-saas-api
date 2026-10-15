"""Seed a persistent DEMO sandbox workspace spanning several sectors + markets.

    python -m app.scripts.seed_demo_sectors --owner info@iaspirants.com

Creates (or reuses) one `is_sandbox=True` workspace with example domains —
healthcare, technology, business, education, and a Spanish-market retail domain
(retail-es) — each carrying the new per-domain settings (country / language /
topic_terms / brand_terms / is_local) and enough stored data (tagged
environment='sandbox') for the Action Plan to render. The retail-es domain is
stored under language 'es' to exercise the non-English path end to end:

  * tracked keywords, some ranked #4-20  -> "Improve"
  * a curated competitor + a sandbox ranked run mixing ON-topic and OFF-topic
    keywords                              -> "Competitor gaps" (on-topic only)
  * keyword-research ideas (vol>=100, KD<=30) scoped to the domain -> "New targets"
  * two months of keyword_metrics         -> the month-over-month volume chart

Because the workspace is sandbox, no paid API is ever called, and the in-app
"Refresh" buttons also hit the free sandbox. Idempotent and NON-destructive:
re-running reuses existing rows and never deletes anything.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.competitor import TrackedCompetitor
from app.models.domain import Domain
from app.models.keyword_analysis import (
    KeywordMetric, KeywordResearch, KeywordResearchResult,
)
from app.models.opportunity import OpportunityKeyword, OpportunityTrend
from app.models.ranked_keyword import DomainRankedKeyword, DomainRankedKeywordRun
from app.models.user import User
from app.models.workspace import Workspace
from app.services import domain_analytics_service, workspace_service
from app.services import keyword_analysis_service as kas
from app.services.ranked_keywords_service import norm_host

WORKSPACE_NAME = "Global Demo (Sandbox)"
ENV = "sandbox"

# Each sector: a domain with its niche, a rival, our tracked keywords (some in
# striking distance), the rival's ranked keywords (on- and off-topic), and
# research ideas for "New targets".
SECTORS = [
    {
        "key": "healthcare",
        "domain": "demo-health.example",
        "name": "Demo Health",
        "country": None,  # inherits the workspace default (India/English)
        "topics": "clinic, treatment, symptoms, diagnosis, therapy",
        "rival": "rival-health.example",
        # (keyword, volume, kd, our_position)  -> improve when 4..50
        "tracked": [
            ("diabetes treatment options", 2400, 38, 7),
            ("physiotherapy at home", 1300, 30, 12),
            ("book doctor appointment", 900, 42, None),
        ],
        # (keyword, rank, volume, kd)  on-topic become gaps; off-topic dropped
        "rival_on": [
            ("knee replacement recovery", 3, 1600, 44),
            ("thyroid symptoms in women", 5, 2100, 36),
        ],
        "rival_off": [
            ("latest bollywood movies", 2, 90000, 70),
            ("ipl match schedule", 4, 120000, 75),
        ],
        # (keyword, volume, kd) vol>=100, kd<=30 -> targets
        "ideas": [("home remedies for cold", 3200, 18), ("healthy diet plan", 2600, 24)],
    },
    {
        "key": "technology",
        "domain": "demo-tech.example",
        "name": "Demo Tech",
        "country": "us",  # exercise the per-domain market override
        "topics": "software, api, cloud, saas, developer",
        "rival": "rival-tech.example",
        "tracked": [
            ("best api monitoring tools", 1800, 40, 6),
            ("kubernetes tutorial", 5400, 48, 15),
            ("cloud cost optimization", 800, 33, None),
        ],
        "rival_on": [
            ("serverless architecture guide", 4, 1200, 41),
            ("saas onboarding checklist", 6, 700, 28),
        ],
        "rival_off": [
            ("nba playoffs scores", 2, 150000, 72),
            ("best pizza near times square", 3, 40000, 60),
        ],
        "ideas": [("rest api best practices", 2900, 27), ("ci cd pipeline example", 1500, 22)],
    },
    {
        "key": "business",
        "domain": "demo-biz.example",
        "name": "Demo Business",
        "country": None,
        "topics": "invoice, payroll, crm, accounting, bookkeeping",
        "rival": "rival-biz.example",
        "tracked": [
            ("free invoice template", 4100, 35, 9),
            ("payroll software for small business", 1200, 46, 18),
            ("crm for startups", 700, 31, None),
        ],
        "rival_on": [
            ("bookkeeping for freelancers", 5, 900, 29),
            ("accounting software comparison", 7, 1600, 43),
        ],
        "rival_off": [
            ("weather forecast tomorrow", 1, 300000, 80),
            ("celebrity gossip today", 3, 80000, 68),
        ],
        "ideas": [("small business tax deductions", 2200, 26), ("expense tracking tips", 600, 19)],
    },
    {
        "key": "education",
        "domain": "demo-edu.example",
        "name": "Demo Education",
        "country": None,
        "topics": "course, syllabus, exam, tutorial, study guide",
        "rival": "rival-edu.example",
        "tracked": [
            ("online python course", 3300, 39, 8),
            ("ielts exam preparation", 2700, 45, 14),
            ("free study planner", 500, 25, None),
        ],
        "rival_on": [
            ("calculus study guide", 6, 1400, 34),
            ("spanish course for beginners", 4, 1900, 37),
        ],
        "rival_off": [
            ("stock market news", 2, 200000, 78),
            ("football transfer rumours", 3, 110000, 74),
        ],
        "ideas": [("how to study effectively", 4800, 21), ("time management for students", 900, 23)],
    },
    {
        # Non-English market: exercises the per-domain LANGUAGE override end to
        # end (data is stored + read back under 'es'). Guards the bugs where
        # ranked-keyword refresh / page-insight hardcoded language="en".
        "key": "retail-es",
        "domain": "demo-retail-es.example",
        "name": "Demo Retail (ES)",
        "country": "es",
        "language": "es",
        "topics": "zapatillas, ropa, ofertas, tienda, moda",
        "rival": "rival-retail-es.example",
        "tracked": [
            ("comprar zapatillas online", 2200, 34, 7),
            ("ofertas ropa mujer", 1500, 30, 13),
            ("tienda de moda online", 600, 40, None),
        ],
        "rival_on": [
            ("zapatillas running baratas", 4, 1800, 36),
            ("ropa de invierno rebajas", 6, 2400, 42),
        ],
        "rival_off": [
            ("resultados de futbol hoy", 2, 250000, 76),
            ("noticias de famosos", 3, 90000, 69),
        ],
        "ideas": [("ofertas de ropa barata", 3100, 20), ("moda sostenible guia", 700, 22)],
    },
]


def _upsert_domain(db, ws, spec) -> Domain:
    dom = db.execute(select(Domain).where(
        Domain.workspace_id == ws.id, Domain.domain == spec["domain"])).scalar_one_or_none()
    if dom is None:
        dom = Domain(workspace_id=ws.id, name=spec["name"], domain=spec["domain"])
        db.add(dom)
    # Always (re)apply the niche settings so re-runs pick up spec changes.
    dom.name = spec["name"]
    dom.country = spec["country"]
    dom.language = spec.get("language", "en")
    dom.topic_terms = spec["topics"]
    dom.is_local = False
    db.flush()
    return dom


def _ensure_opp_kw(db, dom, keyword, *, volume, kd, position, country) -> None:
    kw = kas._upsert_keyword(db, keyword)
    row = db.execute(select(OpportunityKeyword).where(
        OpportunityKeyword.domain_id == dom.id, OpportunityKeyword.keyword == keyword
    )).scalar_one_or_none()
    if row is None:
        db.add(OpportunityKeyword(
            domain_id=dom.id, keyword_id=kw.id, keyword=keyword, search_volume=volume,
            keyword_difficulty=kd, current_position=position, intent="informational",
            opportunity_score=0, country_code=country))
    else:
        row.keyword_id = kw.id
        row.search_volume, row.keyword_difficulty, row.current_position = volume, kd, position


def _ensure_metric(db, kw_id, *, volume, kd, country, language, metric_date) -> None:
    exists = db.execute(select(KeywordMetric.id).where(
        KeywordMetric.keyword_id == kw_id, KeywordMetric.country_code == country,
        KeywordMetric.language_code == language, KeywordMetric.search_engine == "google",
        KeywordMetric.device == "desktop", KeywordMetric.metric_date == metric_date)).first()
    if exists:
        return
    db.add(KeywordMetric(
        keyword_id=kw_id, country_code=country, language_code=language, search_engine="google",
        device="desktop", search_volume=volume, difficulty=kd, metric_date=metric_date))


def _ensure_run(db, rival, loc, lang, rows) -> None:
    target = norm_host(rival)
    run = db.execute(select(DomainRankedKeywordRun).where(
        DomainRankedKeywordRun.environment == ENV, DomainRankedKeywordRun.target == target,
        DomainRankedKeywordRun.location_code == loc, DomainRankedKeywordRun.language_code == lang,
        DomainRankedKeywordRun.source == "demo_seed")).scalar_one_or_none()
    if run is not None:
        return  # already seeded for this (target, market)
    run = DomainRankedKeywordRun(
        environment=ENV, target=target, location_code=loc, language_code=lang,
        source="demo_seed", requested_limit=len(rows), total_count=len(rows),
        items_count=len(rows), cost=0, meta={"scope": "top", "demo": True})
    db.add(run)
    db.flush()
    for keyword, rank, volume, kd in rows:
        db.add(DomainRankedKeyword(
            run_id=run.id, keyword=keyword, rank_absolute=rank, rank_group=rank,
            url=f"https://{target}/{keyword.replace(' ', '-')}", search_volume=volume,
            keyword_difficulty=kd))


def _ensure_research(db, ws, dom, spec, country, lang) -> None:
    marker = f"demo:{spec['key']}"
    research = db.execute(select(KeywordResearch).where(
        KeywordResearch.workspace_id == ws.id, KeywordResearch.domain_id == dom.id,
        KeywordResearch.target == marker)).scalar_one_or_none()
    seed_kw = kas._upsert_keyword(db, spec["ideas"][0][0])
    if research is None:
        research = KeywordResearch(
            workspace_id=ws.id, domain_id=dom.id, seed_keyword_id=seed_kw.id,
            country_code=country, language_code=lang, status="completed", kind="keyword",
            target=marker, result_count=len(spec["ideas"]),
            completed_at=datetime.now(timezone.utc))
        db.add(research)
        db.flush()
    today = date.today()
    for rank, (keyword, volume, kd) in enumerate(spec["ideas"], start=1):
        kw = kas._upsert_keyword(db, keyword)
        exists = db.execute(select(KeywordResearchResult.id).where(
            KeywordResearchResult.research_id == research.id,
            KeywordResearchResult.keyword_id == kw.id)).first()
        if not exists:
            db.add(KeywordResearchResult(research_id=research.id, keyword_id=kw.id, rank=rank))
        _ensure_metric(db, kw.id, volume=volume, kd=kd, country=country, language=lang,
                       metric_date=today)


def _ensure_trend(db, dom) -> None:
    today = date.today()
    for n, base in ((60, 1200), (30, 1500), (0, 1800)):
        d = today - timedelta(days=n)
        if db.execute(select(OpportunityTrend.id).where(
            OpportunityTrend.domain_id == dom.id, OpportunityTrend.captured_on == d)).first():
            continue
        db.add(OpportunityTrend(domain_id=dom.id, captured_on=d, organic_traffic=base,
                                ranking_keywords=3, serp_opportunities=2))


def seed_sector(db, ws, spec) -> None:
    dom = _upsert_domain(db, ws, spec)
    country = (spec["country"] or ws.default_country or "in").lower()
    lang = spec.get("language", "en")
    loc = domain_analytics_service.location_code_for(country)

    # Months for the volume-change chart (first of this + previous month).
    curr_m = date.today().replace(day=1)
    prev_m = (curr_m - timedelta(days=1)).replace(day=1)

    for keyword, volume, kd, pos in spec["tracked"]:
        _ensure_opp_kw(db, dom, keyword, volume=volume, kd=kd, position=pos, country=country)
        kw = kas._upsert_keyword(db, keyword)
        _ensure_metric(db, kw.id, volume=volume, kd=kd, country=country, language=lang,
                       metric_date=prev_m)
        _ensure_metric(db, kw.id, volume=volume + 150, kd=kd, country=country, language=lang,
                       metric_date=curr_m)

    rival = norm_host(spec["rival"])
    if db.execute(select(TrackedCompetitor.id).where(
        TrackedCompetitor.domain_id == dom.id, TrackedCompetitor.competitor == rival)).first() is None:
        db.add(TrackedCompetitor(domain_id=dom.id, competitor=rival, source="manual"))

    _ensure_run(db, rival, loc, lang, spec["rival_on"] + spec["rival_off"])
    _ensure_research(db, ws, dom, spec, country, lang)
    _ensure_trend(db, dom)
    db.flush()
    print(f"  {spec['key']:11s} {dom.domain}  market={country}/{lang}  topics={spec['topics']!r}")


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m app.scripts.seed_demo_sectors")
    ap.add_argument("--owner", required=True, help="owner's login email")
    ap.add_argument("--name", default=WORKSPACE_NAME, help="workspace name")
    args = ap.parse_args()

    if not settings.dataforseo_sandbox_allowed:
        print("Sandbox is disabled in this environment (APP_ENV=production).", file=sys.stderr)
        return 1

    db = SessionLocal()
    try:
        owner = db.execute(select(User).where(User.email == args.owner.lower())).scalar_one_or_none()
        if owner is None:
            print(f"No user with email {args.owner}", file=sys.stderr)
            return 1

        ws = db.execute(select(Workspace).where(
            Workspace.owner_id == owner.id, Workspace.name == args.name,
            Workspace.is_sandbox.is_(True))).scalar_one_or_none()
        if ws is None:
            ws = workspace_service.create_workspace(db, owner, args.name, is_sandbox=True)
            print(f"Created sandbox workspace {ws.name!r} ({ws.id})")
        else:
            print(f"Reusing sandbox workspace {ws.name!r} ({ws.id})")

        for spec in SECTORS:
            seed_sector(db, ws, spec)
        db.commit()
        print("Done. Open the dashboard, switch to this workspace, and pick each domain.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
