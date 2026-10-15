"""Domain analytics refresh: DataForSEO results are stored, cached and re-read.

The provider is mocked, so no paid calls are made.
"""

import pytest

from app.core.config import settings
from app.services import dataforseo_client, domain_analytics_service as svc
from app.services.dataforseo_client import DataForSeoError

LOC = svc.LOCATION_CODES["in"]
T1, T2 = "pytest-rival-one.example", "pytest-rival-two.example"


def _metrics(etv: float, count: int) -> dict:
    return {"organic": {"etv": etv, "count": count, "pos_1": 3, "pos_2_3": 4,
                        "pos_4_10": 5, "is_new": 2, "is_lost": 1,
                        "estimated_paid_traffic_cost": 99.5},
            "paid": {"etv": 0, "count": 0}}


@pytest.fixture
def provider(monkeypatch):
    calls = {"overview": [], "ranks": 0}

    def overview(target, *, location_code, language_code, env=None):
        calls.setdefault("envs", []).append(env)
        calls["overview"].append(target)
        if target == "pytest-broken.example":
            raise DataForSeoError("boom")
        return _metrics(1000.4 if target == T1 else 250.0, 40)

    def ranks(targets, *, env=None):
        calls["ranks"] += 1
        return {t: 55 for t in targets}

    monkeypatch.setattr(settings, "DATAFORSEO_LOGIN", "x")
    monkeypatch.setattr(settings, "DATAFORSEO_PASSWORD", "y")
    monkeypatch.setattr(dataforseo_client, "domain_rank_overview", overview)
    monkeypatch.setattr(dataforseo_client, "bulk_domain_ranks", ranks)

    def backlinks(targets, *, env=None):
        calls["backlinks"] = calls.get("backlinks", 0) + 1
        return {t: {"backlinks": 1234, "referring_domains": 56} for t in targets}

    monkeypatch.setattr(dataforseo_client, "bulk_backlinks", backlinks)
    return calls


def test_refresh_stores_and_latest_reads_back(db_session, provider):
    out = svc.refresh(db_session, [T1, T2], location_code=LOC, language="en")
    assert out == {"fetched": 2, "cached": 0, "failed": [], "error": None}

    stored = svc.latest(db_session, [T1, T2], location_code=LOC, language="en")
    snap = stored[T1]
    assert snap.organic_etv == pytest.approx(1000.4)
    assert snap.organic_count == 40
    assert (snap.pos_1, snap.pos_2_3, snap.pos_4_10) == (3, 4, 5)
    assert snap.domain_rank == 55
    assert (snap.backlinks, snap.referring_domains) == (1234, 56)
    assert snap.raw["metrics"]["organic"]["count"] == 40


def test_refresh_uses_cache_unless_forced(db_session, provider):
    svc.refresh(db_session, [T1], location_code=LOC, language="en")
    again = svc.refresh(db_session, [T1], location_code=LOC, language="en")
    assert again["fetched"] == 0 and again["cached"] == 1
    assert provider["overview"] == [T1]

    forced = svc.refresh(db_session, [T1], location_code=LOC, language="en", force=True)
    assert forced["fetched"] == 1
    # Same day -> upserted in place, not duplicated.
    assert len(svc.latest(db_session, [T1], location_code=LOC, language="en")) == 1


def test_partial_failure_keeps_successes(db_session, provider):
    out = svc.refresh(db_session, [T1, "pytest-broken.example"],
                      location_code=LOC, language="en")
    assert out["fetched"] == 1 and out["failed"] == ["pytest-broken.example"]


def test_all_failed_raises(db_session, provider):
    with pytest.raises(DataForSeoError):
        svc.refresh(db_session, ["pytest-broken.example"], location_code=LOC, language="en")


def test_unconfigured_raises(db_session, monkeypatch):
    monkeypatch.setattr(settings, "DATAFORSEO_LOGIN", "")
    with pytest.raises(DataForSeoError):
        svc.refresh(db_session, [T1], location_code=LOC, language="en")


def test_sandbox_rows_are_isolated_from_live(db_session, provider):
    svc.refresh(db_session, [T1], location_code=LOC, language="en", env="live")
    svc.refresh(db_session, [T1], location_code=LOC, language="en", env="sandbox")
    assert provider["envs"] == ["live", "sandbox"]  # sandbox is its own cache

    live = svc.latest(db_session, [T1], location_code=LOC, language="en", env="live")[T1]
    sandbox = svc.latest(db_session, [T1], location_code=LOC, language="en", env="sandbox")[T1]
    assert live.id != sandbox.id
    assert (live.environment, live.source) == ("live", "dataforseo_labs")
    assert (sandbox.environment, sandbox.source) == ("sandbox", "dataforseo_sandbox")


def test_fresh_rows_without_backlinks_get_backfilled(db_session, provider):
    svc.refresh(db_session, [T1], location_code=LOC, language="en")
    snap = svc.latest(db_session, [T1], location_code=LOC, language="en")[T1]
    snap.backlinks = snap.referring_domains = None  # row stored before backlinks existed
    db_session.flush()

    out = svc.refresh(db_session, [T1], location_code=LOC, language="en")
    assert out["fetched"] == 0 and out["cached"] == 1  # no paid overview re-buy
    assert provider["overview"] == [T1]
    db_session.refresh(snap)
    assert (snap.backlinks, snap.referring_domains) == (1234, 56)
