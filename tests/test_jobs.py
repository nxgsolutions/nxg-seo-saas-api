"""Tests for the scheduled GSC sync job (app/jobs)."""

import uuid
from datetime import date
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.jobs import gsc_sync
from app.jobs.runner import run_job
from app.models.gsc_connection import GscConnection
from app.models.job_run import JobRun
from app.models.keyword_analysis import DomainKeyword, Keyword, KeywordMetric, KeywordSource
from app.models.ranking import RankEntry, TrackedKeyword
from app.services import gsc_service

WS = "/api/workspaces"
PW = "SecurePass123"


def _connected_domain(client: TestClient, db: Session, host: str) -> tuple[str, str]:
    """Sign up, add a domain and attach a (fake) GSC connection. Returns (wid, domain_id)."""
    email = f"job-{uuid4().hex}@example.com"
    client.post("/api/auth/register", json={"first_name": "J", "last_name": "B", "email": email, "password": PW})
    assert client.post("/api/auth/login", json={"email": email, "password": PW}).status_code == 200
    wid = client.get(WS).json()[0]["id"]
    resp = client.post(f"{WS}/{wid}/domains", json={"url": host})
    assert resp.status_code == 201
    db.add(GscConnection(workspace_id=uuid.UUID(wid), google_email=email, refresh_token="x"))
    db.commit()
    return wid, resp.json()["id"]


GSC_ROWS = [
    {"keyword": "UPSC Mock Test", "clicks": 12, "impressions": 900, "ctr": 0.01, "position": 8.4},
    {"keyword": "site:example.com", "clicks": 0, "impressions": 5, "ctr": 0.0, "position": 1.0},
]


@pytest.fixture
def fake_gsc(monkeypatch):
    def _perf(db, ws, domain, days=28):
        if domain.domain.startswith("broken"):
            raise gsc_service.GscPropertyError("no property")
        return {"site_url": f"sc-domain:{domain.domain}", "keywords": GSC_ROWS}

    monkeypatch.setattr(gsc_service, "keyword_performance", _perf)


def test_gsc_sync_grows_cube_and_tracks_ranks(client: TestClient, db_session: Session, fake_gsc) -> None:
    host = f"jobsync-{uuid4().hex[:8]}.com"
    wid, did = _connected_domain(client, db_session, host)
    base = f"{WS}/{wid}/domains/{did}"
    client.post(f"{base}/tracked-keywords", json={"term": "upsc mock test"})

    stats = gsc_sync.run(db_session, country="in", language="en")
    mine = next(d for d in stats["domains"] if d["domain"] == host)
    assert mine["gsc_queries"] == 2
    assert mine["keywords_upserted"] == 1  # the site: operator query is dropped
    assert mine["tracked_synced"] == 1

    kw = db_session.execute(
        select(Keyword).where(Keyword.normalized_keyword == "upsc mock test")
    ).scalar_one()
    # Operator query is skipped.
    assert db_session.execute(
        select(Keyword).where(Keyword.normalized_keyword == "site:example.com")
    ).scalar_one_or_none() is None

    metric = db_session.execute(
        select(KeywordMetric).where(
            KeywordMetric.keyword_id == kw.id,
            KeywordMetric.country_code == "in",
            KeywordMetric.metric_date == date.today(),
        )
    ).scalar_one()
    assert metric.gsc_impressions == 900
    assert metric.gsc_clicks == 12
    assert float(metric.gsc_position) == 8.4
    assert float(metric.opportunity_score) > 0

    assert db_session.execute(
        select(DomainKeyword).where(
            DomainKeyword.domain_id == uuid.UUID(did), DomainKeyword.keyword_id == kw.id
        )
    ).scalar_one_or_none() is not None
    assert db_session.execute(
        select(KeywordSource).where(KeywordSource.keyword_id == kw.id, KeywordSource.source_type == "gsc")
    ).scalars().first() is not None

    tracked = db_session.execute(
        select(TrackedKeyword).where(TrackedKeyword.domain_id == uuid.UUID(did))
    ).scalar_one()
    entry = db_session.execute(select(RankEntry).where(RankEntry.keyword_id == tracked.id)).scalar_one()
    assert entry.position == 8
    assert entry.recorded_at == date.today()


def test_gsc_sync_is_idempotent_same_day(client: TestClient, db_session: Session, fake_gsc) -> None:
    _connected_domain(client, db_session, f"jobidem-{uuid4().hex[:8]}.com")
    gsc_sync.run(db_session)
    gsc_sync.run(db_session)  # second run the same day must not raise on unique keys
    kw = db_session.execute(
        select(Keyword).where(Keyword.normalized_keyword == "upsc mock test")
    ).scalar_one()
    rows = db_session.execute(
        select(KeywordMetric).where(
            KeywordMetric.keyword_id == kw.id,
            KeywordMetric.country_code == "in",
            KeywordMetric.metric_date == date.today(),
        )
    ).scalars().all()
    assert len(rows) == 1


def test_run_job_records_partial_when_a_domain_fails(client: TestClient, db_session: Session, fake_gsc) -> None:
    _connected_domain(client, db_session, f"jobok-{uuid4().hex[:8]}.com")
    _connected_domain(client, db_session, f"broken-{uuid4().hex[:8]}.com")

    run = run_job(db_session, "gsc_sync", gsc_sync.run, triggered_by="scheduler")

    assert run.status == "partial"
    assert run.triggered_by == "scheduler"
    assert run.finished_at is not None
    errors = [d for d in run.stats["domains"] if d["domain"].startswith("broken-")]
    assert errors and "GscPropertyError" in errors[0]["error"]
    assert db_session.get(JobRun, run.id).status == "partial"


def test_run_job_records_failure(db_session: Session) -> None:
    def boom(db):
        raise RuntimeError("kaput")

    with pytest.raises(RuntimeError):
        run_job(db_session, "boom", boom)
    run = db_session.execute(
        select(JobRun).where(JobRun.job_name == "boom").order_by(JobRun.started_at.desc())
    ).scalars().first()
    assert run.status == "failed"
    assert "kaput" in run.error


def test_first_sync_auto_tracks_top_queries_once(client: TestClient, db_session: Session, fake_gsc, monkeypatch) -> None:
    host = f"jobfirst-{uuid4().hex[:8]}.com"
    wid, did = _connected_domain(client, db_session, host)
    monkeypatch.setattr(db_session, "close", lambda: None)  # first_sync closes its session

    gsc_sync.first_sync(uuid.UUID(wid), session_factory=lambda: db_session)

    tracked = db_session.execute(
        select(TrackedKeyword).where(TrackedKeyword.domain_id == uuid.UUID(did))).scalars().all()
    assert [t.term for t in tracked] == ["UPSC Mock Test"]  # operator query skipped
    entry = db_session.execute(select(RankEntry).where(RankEntry.keyword_id == tracked[0].id)).scalar_one()
    assert entry.position == 8  # today's position recorded right away
    run = db_session.execute(
        select(JobRun).where(JobRun.triggered_by == "connect").order_by(JobRun.started_at.desc())).scalars().first()
    assert run.status in ("success", "partial") and run.stats["auto_tracked"] >= 1

    # The user removes it; the nightly run must NOT re-add it.
    db_session.delete(tracked[0])
    db_session.commit()
    gsc_sync.run(db_session)
    assert db_session.execute(
        select(TrackedKeyword).where(TrackedKeyword.domain_id == uuid.UUID(did))).scalars().all() == []


def test_first_sync_keeps_existing_tracked_keywords(client: TestClient, db_session: Session, fake_gsc, monkeypatch) -> None:
    wid, did = _connected_domain(client, db_session, f"jobkeep-{uuid4().hex[:8]}.com")
    client.post(f"{WS}/{wid}/domains/{did}/tracked-keywords", json={"term": "my own pick"})
    monkeypatch.setattr(db_session, "close", lambda: None)
    gsc_sync.first_sync(uuid.UUID(wid), uuid.UUID(did), session_factory=lambda: db_session)
    terms = db_session.execute(
        select(TrackedKeyword.term).where(TrackedKeyword.domain_id == uuid.UUID(did))).scalars().all()
    assert terms == ["my own pick"]


def test_adding_domain_to_connected_workspace_starts_first_sync(client: TestClient, db_session: Session, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(gsc_sync, "first_sync", lambda wid, did=None, **kw: calls.append((str(wid), str(did))))
    wid, first = _connected_domain(client, db_session, f"jobadd-{uuid4().hex[:8]}.com")
    assert calls == []  # added before Search Console was connected
    second = client.post(f"{WS}/{wid}/domains", json={"url": f"jobadd2-{uuid4().hex[:8]}.com"}).json()["id"]
    assert calls == [(wid, second)]
