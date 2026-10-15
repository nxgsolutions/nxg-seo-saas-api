"""Opportunity Research action plan — built from stored data only."""

import uuid
from datetime import date

import pytest

from app.models.competitor import TrackedCompetitor
from app.models.domain import Domain
from app.models.keyword_analysis import KeywordMetric, KeywordResearch, KeywordResearchResult
from app.models.opportunity import OpportunityKeyword
from app.models.ranked_keyword import DomainRankedKeyword, DomainRankedKeywordRun
from app.models.user import User
from app.services import action_plan_service as plan
from app.services import keyword_analysis_service as kas
from app.services import workspace_service

OURS = "pytest-ours-plan.example"
RIVAL = "pytest-rival-plan.example"


def _row(run, keyword, pos, vol, kd=10, url=None):
    return DomainRankedKeyword(run_id=run.id, keyword=keyword, rank_absolute=pos, search_volume=vol,
                               keyword_difficulty=kd, url=url or f"https://{RIVAL}/{pos}", etv=1.0)


@pytest.fixture
def setup(db_session):
    user = User(email=f"ap-{uuid.uuid4().hex[:8]}@example.com", password_hash="x",
                first_name="Ap", last_name="Test")
    db_session.add(user)
    db_session.flush()
    ws = workspace_service.create_workspace(db_session, user, "Plan test")
    # Topic terms now live on the DOMAIN (not a global setting): they drive the
    # on-topic filter for competitor gaps + content suggestions.
    d = Domain(workspace_id=ws.id, name="ours", domain=OURS, topic_terms="upsc, ias, exam")
    db_session.add(d)
    db_session.flush()
    db_session.add(TrackedCompetitor(domain_id=d.id, competitor=RIVAL, source="manual"))

    # We rank #8 (Search Console) for a tracked keyword -> "improve".
    db_session.add(OpportunityKeyword(domain_id=d.id, keyword="zzap upsc notes", search_volume=2000,
                                      keyword_difficulty=20, current_position=8))

    # Competitor's stored whole-site rankings -> "gaps".
    run = DomainRankedKeywordRun(environment="live", target=RIVAL, location_code=2356,
                                 language_code="en", requested_limit=100, items_count=4, meta={})
    db_session.add(run)
    db_session.flush()
    db_session.add_all([
        _row(run, "zzap upsc mock test", 3, 5000),       # relevant gap
        _row(run, "zzap u.p.s.c mock test", 4, 800),     # variant of the same topic
        _row(run, "zzap cricket score", 1, 900000),      # off-topic -> skipped
        _row(run, "zzap upsc notes", 2, 2000),           # we already rank -> not a gap
    ])

    # A keyword-research idea in the sweet spot -> "targets".
    kw = kas._upsert_keyword(db_session, "zzap ias daily quiz")
    research = KeywordResearch(workspace_id=ws.id, seed_keyword_id=kw.id, kind="keyword",
                               status="completed", country_code="in", language_code="en")
    db_session.add(research)
    db_session.flush()
    db_session.add(KeywordResearchResult(research_id=research.id, keyword_id=kw.id, rank=1))
    db_session.add(KeywordMetric(keyword_id=kw.id, country_code="in", language_code="en",
                                 search_engine="google", device="desktop", metric_date=date.today(),
                                 search_volume=1300, difficulty=18))
    db_session.flush()
    return d


def test_action_plan_lists(db_session, setup):
    out = plan.build(db_session, setup, country="in")

    improve = {i["keyword"]: i for i in out["improve"]}
    assert improve["zzap upsc notes"]["position"] == 8 and improve["zzap upsc notes"]["gain"] > 0

    gaps = {i["keyword"]: i for i in out["gaps"]}
    assert "zzap cricket score" not in gaps           # off-topic
    assert "zzap upsc notes" not in gaps              # we already rank for it
    assert "zzap upsc mock test" in gaps and "zzap u.p.s.c mock test" not in gaps  # variants merged
    g = gaps["zzap upsc mock test"]
    # one competitor, two spellings -> still ONE competitor; best position kept
    assert g["competitor"] == RIVAL and g["competitor_position"] == 3 and g["competitors_count"] == 1
    assert g["volume"] == 5000                        # highest-volume variant is the headline

    targets = {i["keyword"] for i in out["targets"]}
    assert "zzap ias daily quiz" in targets

    types = {i["type"] for i in out["top"]}
    assert types == {"improve", "gap", "target"}      # every kind gets a share of the top list


def test_track_keyword_is_idempotent(db_session, setup):
    first = plan.track_keyword(db_session, setup, "zzap ias daily quiz")
    again = plan.track_keyword(db_session, setup, "  ZZAP ias   daily quiz ")
    assert first["created"] and not again["created"]
    row = db_session.query(OpportunityKeyword).filter_by(domain_id=setup.id, keyword="zzap ias daily quiz").one()
    assert row.search_volume == 1300 and row.keyword_difficulty == 18
    # Tracked now -> no longer a "new target".
    assert "zzap ias daily quiz" not in {i["keyword"] for i in plan.build(db_session, setup)["targets"]}


# ── Page insight: rule-based content suggestions (fetching mocked) ───────────

def _fake_page(url):
    if "rival" in url:
        return {"url": url, "ok": True, "title": "UPSC mock test", "meta_description": "", "h1": ["Mock test"],
                "headings": ["Mock test pattern for UPSC", "Achievers Corner", "Pytest-rival-plan Specials"],
                "word_count": 2000, "text": "mock test"}
    return {"url": url, "ok": True, "title": "Home", "meta_description": "", "h1": ["Welcome"],
            "headings": [], "word_count": 400, "text": "welcome to our site"}


def test_page_insight_suggestions(db_session, setup, monkeypatch):
    from app.services import page_insight_service as pi
    monkeypatch.setattr(pi, "_page", _fake_page)
    out = pi.analyze(db_session, setup, "zzap upsc mock test",
                     url=f"https://{OURS}/", competitor_url=f"https://{RIVAL}/mock")
    areas = [s["area"] for s in out["suggestions"]]
    assert {"Title", "Heading", "Meta", "Length"} <= set(areas)
    subtopics = [s["text"] for s in out["suggestions"] if s["area"] == "Subtopic"]
    assert any("Mock test pattern for UPSC" in t for t in subtopics)      # on-topic heading kept
    assert not any("Achievers Corner" in t or "Specials" in t for t in subtopics)  # nav/brand dropped
    assert out["target_word_count"] == 2000


def test_page_insight_unreachable_page_is_not_create(db_session, setup, monkeypatch):
    from app.services import page_insight_service as pi
    monkeypatch.setattr(pi, "_page", lambda url: {"url": url, "ok": False, "error": "timeout"})
    out = pi.analyze(db_session, setup, "zzap upsc mock test", url=f"https://{OURS}/")
    assert out["suggestions"][0]["text"].startswith("Couldn't load your page")
