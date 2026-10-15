"""The Action Plan is now domain-agnostic: competitor gaps keep only keywords
on-topic for THE DOMAIN's niche, across sectors — healthcare, technology,
business, education — not a single hardcoded (UPSC) niche."""

import uuid

import pytest

from app.models.competitor import TrackedCompetitor
from app.models.domain import Domain
from app.models.opportunity import OpportunityKeyword
from app.models.ranked_keyword import DomainRankedKeyword, DomainRankedKeywordRun
from app.models.user import User
from app.services import action_plan_service as plan
from app.services import workspace_service

LOC = 2356  # India — the default market when a domain sets no country

# (sector, topic_terms, on-topic gap, off-topic noise) — keywords use a unique
# zz* prefix so they never collide with real rows on the shared test DB.
SECTORS = [
    ("healthcare", "clinic, treatment, symptoms",
     ("zzsec knee treatment cost", 3, 1600), ("zzsec celebrity gossip today", 2, 90000)),
    ("technology", "software, api, cloud",
     ("zzsec api monitoring software", 4, 1200), ("zzsec football transfer news", 3, 80000)),
    ("business", "invoice, payroll, crm",
     ("zzsec payroll crm setup", 5, 900), ("zzsec movie tickets online", 2, 70000)),
    ("education", "course, exam, syllabus",
     ("zzsec exam syllabus pdf", 6, 1400), ("zzsec cricket live score", 1, 120000)),
]


@pytest.fixture
def make(db_session):
    """Build a workspace + domain (+ one curated rival with a ranked run) and
    return (domain, rival_host, run) so a test can add ranked keywords."""
    def _make(topic_terms):
        user = User(email=f"sec-{uuid.uuid4().hex[:8]}@example.com", password_hash="x",
                    first_name="S", last_name="T")
        db_session.add(user)
        db_session.flush()
        ws = workspace_service.create_workspace(db_session, user, "Sector test")
        host = f"zzsec-{uuid.uuid4().hex[:6]}.example"
        rival = f"zzsec-rival-{uuid.uuid4().hex[:6]}.example"
        d = Domain(workspace_id=ws.id, name="ours", domain=host, topic_terms=topic_terms)
        db_session.add(d)
        db_session.flush()
        db_session.add(TrackedCompetitor(domain_id=d.id, competitor=rival, source="manual"))
        # One striking-distance keyword of ours -> an "improve" item exists too.
        db_session.add(OpportunityKeyword(domain_id=d.id, keyword="zzsec our ranked term",
                                          search_volume=1500, keyword_difficulty=30, current_position=8))
        run = DomainRankedKeywordRun(environment="live", target=rival, location_code=LOC,
                                     language_code="en", requested_limit=10, items_count=0, meta={})
        db_session.add(run)
        db_session.flush()
        return d, rival, run

    return _make


def _rk(run, rival, keyword, pos, vol):
    return DomainRankedKeyword(run_id=run.id, keyword=keyword, rank_absolute=pos,
                               search_volume=vol, keyword_difficulty=30,
                               url=f"https://{rival}/{pos}")


@pytest.mark.parametrize("name,topics,gap,off", SECTORS)
def test_gaps_respect_each_domains_niche(db_session, make, name, topics, gap, off):
    d, rival, run = make(topics)
    gkw, gpos, gvol = gap
    okw, opos, ovol = off
    db_session.add_all([_rk(run, rival, gkw, gpos, gvol), _rk(run, rival, okw, opos, ovol)])
    db_session.flush()

    gaps = {i["keyword"] for i in plan.build(db_session, d)["gaps"]}
    assert gkw in gaps, f"{name}: on-topic competitor keyword should be a gap"
    assert okw not in gaps, f"{name}: off-topic keyword must be excluded"


def test_no_topic_terms_keeps_every_competitor_keyword(db_session, make):
    # A generic domain with no niche set must not drop anything for topic reasons.
    d, rival, run = make(None)
    db_session.add_all([
        _rk(run, rival, "zzsec random niche phrase", 3, 800),
        _rk(run, rival, "zzsec totally unrelated thing", 4, 600),
    ])
    db_session.flush()

    gaps = {i["keyword"] for i in plan.build(db_session, d)["gaps"]}
    assert {"zzsec random niche phrase", "zzsec totally unrelated thing"} <= gaps
