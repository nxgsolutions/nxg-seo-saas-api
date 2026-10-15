"""Keyword Breakdown — stored SERP enriched with stored domain metrics (DB only)."""

import uuid
from datetime import date

from app.models.domain import Domain
from app.models.domain_analytics import DomainAnalyticsSnapshot
from app.models.keyword_analysis import KeywordMetric, SerpSnapshot, SerpSnapshotItem
from app.models.ranked_keyword import DomainRankedKeyword, DomainRankedKeywordRun
from app.models.user import User
from app.services import keyword_analysis_service as kas
from app.services import serp_breakdown_service, workspace_service

OURS = "pytest-ours-bd.example"
RIVAL = "pytest-rival-bd.example"
OTHER = "pytest-other-bd.example"
KW = "zzbd upsc pyq booklet"


def _setup(db):
    user = User(email=f"bd-{uuid.uuid4().hex[:8]}@example.com", password_hash="x",
                first_name="Bd", last_name="Test")
    db.add(user)
    db.flush()
    ws = workspace_service.create_workspace(db, user, "Breakdown test")
    db.add(Domain(workspace_id=ws.id, name="ours", domain=OURS))
    kw = kas._upsert_keyword(db, KW)
    db.add(KeywordMetric(keyword_id=kw.id, country_code="in", language_code="en",
                         search_volume=1000, metric_date=date.today()))
    snap = SerpSnapshot(keyword_id=kw.id, source="dataforseo", environment="live",
                        country_code="in", language_code="en", items_count=3)
    db.add(snap)
    db.flush()
    db.add_all([
        SerpSnapshotItem(snapshot_id=snap.id, rank_absolute=1, domain=f"www.{RIVAL}",
                         url=f"https://www.{RIVAL}/pyq", title="Rival PYQ"),
        SerpSnapshotItem(snapshot_id=snap.id, rank_absolute=2, domain=OTHER, url=f"https://{OTHER}/a"),
        SerpSnapshotItem(snapshot_id=snap.id, rank_absolute=3, domain=OURS, url=f"https://{OURS}/pyq"),
    ])
    db.add(DomainAnalyticsSnapshot(environment="live", target=RIVAL, location_code=2356, language_code="en",
                                   domain_rank=58, backlinks=76000, referring_domains=900,
                                   organic_etv=120000, organic_count=5000, raw={}))
    run = DomainRankedKeywordRun(environment="live", target=RIVAL, location_code=2356,
                                 language_code="en", requested_limit=100, items_count=1, meta={})
    db.add(run)
    db.flush()
    db.add(DomainRankedKeyword(run_id=run.id, keyword=KW, rank_absolute=1, etv=321.4))
    db.flush()
    return ws


def test_breakdown_enriches_rows_from_stored_data(db_session):
    ws = _setup(db_session)
    d = serp_breakdown_service.build(db_session, ws, KW)

    assert d["captured_at"] and d["metrics"]["search_volume"] == 1000
    rival, other, ours = d["rows"]
    # www. is normalised so the stored metrics match.
    assert rival["host"] == RIVAL and rival["authority"] == 58 and rival["backlinks"] == 76000
    # Labs' per-keyword estimate wins when that domain's rankings are stored...
    assert rival["visits"] == 321 and rival["visits_source"] == "dataforseo"
    # ...otherwise volume x CTR(position): 1000 x 30/2^0.8 % = 172.
    assert other["visits"] == 172 and other["visits_source"] == "estimate"
    assert other["authority"] is None
    assert ours["is_you"] and d["summary"]["your_position"] == 3
    assert d["summary"]["avg_authority"] == 58 and d["summary"]["top10_with_metrics"] == 1


def test_breakdown_without_stored_serp_is_empty(db_session):
    ws = _setup(db_session)
    d = serp_breakdown_service.build(db_session, ws, "zzbd never fetched keyword")
    assert d["captured_at"] is None and d["rows"] == []
    assert d["summary"]["your_position"] is None
