"""Search by domain: results are stored per run and reopened without Google Ads.

Keyword Planner and the homepage crawl are mocked (no network calls).
"""

import uuid
from datetime import date

import pytest

from app.models.keyword_analysis import KeywordMetric
from app.models.user import User
from app.services import keyword_analysis_service as kas
from app.services import keyword_planner_service, workspace_service

HOST = "pytest-rival-domain.example"


@pytest.fixture
def workspace(db_session):
    user = User(email=f"ds-{uuid.uuid4().hex[:8]}@example.com", password_hash="x",
                first_name="Ds", last_name="Test")
    db_session.add(user)
    db_session.flush()
    return workspace_service.create_workspace(db_session, user, "Domain search test")


@pytest.fixture
def planner(monkeypatch):
    calls = []

    def generate_ideas(term, *, mode="keyword", country="us", language="en"):
        calls.append(term)
        n = len(calls)
        return {"configured": True, "mode": mode, "input": term, "country": country,
                "language": language, "count": 2, "rows": [
                    {"keyword": "zzq pytest low kw", "avg_monthly_searches": 10 * n,
                     "competition": "LOW", "competition_index": 5,
                     "cpc_low": 0.1, "cpc_high": 0.5,
                     "monthly": [{"year": 2026, "month": "AUGUST", "searches": 10}]},
                    {"keyword": "zzq pytest high kw", "avg_monthly_searches": 900,
                     "competition": "HIGH", "competition_index": 80,
                     "cpc_low": 1.0, "cpc_high": 2.5, "monthly": []},
                ]}

    monkeypatch.setattr(kas, "_site_topic_seeds", lambda inp: ["pytest topic"])
    monkeypatch.setattr(keyword_planner_service, "generate_ideas", generate_ideas)
    return calls


def test_first_search_stores_rows_then_reuses_them(db_session, workspace, planner):
    first = kas.run_website_search(db_session, workspace, f"https://www.{HOST}/", country="zz")
    assert first["cached"] is False and first["target"] == HOST
    assert [r["keyword"] for r in first["rows"]] == ["zzq pytest high kw", "zzq pytest low kw"]

    again = kas.run_website_search(db_session, workspace, HOST, country="zz")
    assert again["cached"] is True and again["id"] == first["id"]
    assert len(planner) == 1  # no second Google call
    stored = {r["keyword"]: r for r in again["rows"]}
    assert stored["zzq pytest high kw"]["cpc_high"] == 2.5
    assert stored["zzq pytest low kw"]["monthly"] == [{"year": 2026, "month": "AUGUST", "searches": 10}]

    reopened = kas.get_domain_search(db_session, workspace, first["id"])
    assert reopened["rows"] == again["rows"]


def test_refresh_records_new_run_and_recent_list_dedupes(db_session, workspace, planner):
    first = kas.run_website_search(db_session, workspace, HOST, country="zz")
    fresh = kas.run_website_search(db_session, workspace, HOST, country="zz", refresh=True)
    assert fresh["cached"] is False and fresh["id"] != first["id"] and len(planner) == 2
    low = next(r for r in fresh["rows"] if r["keyword"] == "zzq pytest low kw")
    assert low["avg_monthly_searches"] == 20  # the new numbers

    recent = kas.list_researches(db_session, workspace, kind="all")
    domain_runs = [r for r in recent if r.kind == "domain" and r.target == HOST]
    assert [str(r.id) for r in domain_runs] == [fresh["id"]]  # newest only


def test_domain_search_keeps_existing_gsc_numbers(db_session, workspace, planner):
    kw = kas._upsert_keyword(db_session, "zzq pytest high kw")
    db_session.flush()
    db_session.add(KeywordMetric(keyword_id=kw.id, country_code="zz", language_code="en",
                                 search_engine="google", device="desktop",
                                 metric_date=date.today(), gsc_impressions=321,
                                 gsc_clicks=12, gsc_position=7.5))
    db_session.flush()

    kas.run_website_search(db_session, workspace, HOST, country="zz")
    metric = db_session.query(KeywordMetric).filter_by(
        keyword_id=kw.id, country_code="zz", metric_date=date.today()).one()
    assert (metric.gsc_impressions, metric.gsc_clicks) == (321, 12)
    assert metric.search_volume == 900  # Ads data still written
