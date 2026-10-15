"""DataForSEO sandbox routing + isolation (no network calls)."""

import pytest

from app.core.config import settings
from app.models.domain import Domain
from app.models.keyword_analysis import SerpSnapshot, SerpSnapshotItem
from app.services import data_env, dataforseo_client, serp_scrape_service
from app.services import keyword_analysis_service as kas
from app.services.dataforseo_client import DataForSeoError


@pytest.fixture(autouse=True)
def dev_env(monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "development")
    monkeypatch.setattr(settings, "DATAFORSEO_SANDBOX_ENABLED", None)
    monkeypatch.setattr(settings, "DATAFORSEO_FORCE_SANDBOX", False)
    monkeypatch.setattr(settings, "DATAFORSEO_LIVE_ENABLED", True)


def test_base_url_by_env():
    assert dataforseo_client._base_url("live") == "https://api.dataforseo.com"
    assert dataforseo_client._base_url("sandbox") == "https://sandbox.dataforseo.com"


def test_sandbox_blocked_in_production(monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "production")
    with pytest.raises(DataForSeoError):
        dataforseo_client._base_url("sandbox")
    assert dataforseo_client._base_url("live") == "https://api.dataforseo.com"
    monkeypatch.setattr(settings, "DATAFORSEO_SANDBOX_ENABLED", True)
    assert dataforseo_client._base_url("sandbox") == "https://sandbox.dataforseo.com"


def test_force_sandbox_routes_everything(monkeypatch):
    monkeypatch.setattr(settings, "DATAFORSEO_FORCE_SANDBOX", True)
    assert dataforseo_client._base_url("live") == "https://sandbox.dataforseo.com"
    assert data_env.default_env() == data_env.SANDBOX
    assert data_env.env_for_workspace(None) == data_env.SANDBOX


def _snap(db, kw, domain, env):
    db.add(SerpSnapshot(
        keyword_id=kw.id, search_engine="google", source="dataforseo",
        environment=env, country_code="zy", language_code="zy", items_count=1,
        items=[SerpSnapshotItem(type="organic", rank_absolute=1,
                                url=f"https://{domain}/", domain=domain)],
    ))


def test_serp_reads_never_cross_environments(db_session):
    kw = kas._upsert_keyword(db_session, "zzz sandbox isolation kw")
    db_session.flush()
    _snap(db_session, kw, "real-rival.com", "live")
    db_session.flush()
    _snap(db_session, kw, "dummy-rival.com", "sandbox")  # newer, but sandbox
    db_session.flush()

    live = serp_scrape_service.latest_snapshot(
        db_session, kw.keyword, country="zy", language="zy", env="live")
    sandbox = serp_scrape_service.latest_snapshot(
        db_session, kw.keyword, country="zy", language="zy", env="sandbox")
    assert [r["domain"] for r in live["rows"]] == ["real-rival.com"]
    assert [r["domain"] for r in sandbox["rows"]] == ["dummy-rival.com"]

    # Competitor derivation for a live domain ignores the sandbox rows.
    dom = Domain(domain="mysite.com", name="mysite")  # no workspace -> live
    hosts = [r["competitor"] for r in
             kas.competitor_domains(db_session, dom, country="zy", language="zy")["rows"]]
    assert "real-rival.com" in hosts and "dummy-rival.com" not in hosts


def test_live_kill_switch_blocks_live_but_not_sandbox(monkeypatch):
    monkeypatch.setattr(settings, "DATAFORSEO_LIVE_ENABLED", False)
    with pytest.raises(DataForSeoError):
        dataforseo_client._base_url("live")
    assert dataforseo_client._base_url("sandbox") == "https://sandbox.dataforseo.com"
