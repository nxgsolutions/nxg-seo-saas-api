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


# ── Every endpoint honors `env` (guards against a new endpoint forgetting it) ──
# One entry per public client function. A new DataForSEO endpoint must be added
# here; if it ignores `env` and hits the live host, the sandbox case below fails.

class _FakeResp:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


def _ok_envelope():
    return {"tasks": [{"status_code": 20000, "cost": 0.0, "result": [{"items": []}]}]}


ENDPOINT_CALLS = [
    ("keyword_ideas", lambda env: dataforseo_client.keyword_ideas(
        "seed", location_name="United States", language_code="en", limit=1, env=env)),
    ("serp_organic", lambda env: dataforseo_client.serp_organic(
        "kw", location_name="United States", language_code="en", env=env)),
    ("serp_organic_snapshot", lambda env: dataforseo_client.serp_organic_snapshot(
        "kw", location_name="United States", language_code="en", env=env)),
    ("search_volume", lambda env: dataforseo_client.search_volume(
        ["kw"], location_name="United States", language_code="en", env=env)),
    ("domain_rank_overview", lambda env: dataforseo_client.domain_rank_overview(
        "example.com", location_code=2840, language_code="en", env=env)),
    ("historical_bulk_traffic_estimation", lambda env: dataforseo_client.historical_bulk_traffic_estimation(
        ["example.com"], location_code=2840, language_code="en", env=env)),
    ("historical_rank_overview", lambda env: dataforseo_client.historical_rank_overview(
        "example.com", location_code=2840, language_code="en", env=env)),
    ("bulk_domain_ranks", lambda env: dataforseo_client.bulk_domain_ranks(["example.com"], env=env)),
    ("bulk_backlinks", lambda env: dataforseo_client.bulk_backlinks(["example.com"], env=env)),
    ("ranked_keywords", lambda env: dataforseo_client.ranked_keywords(
        "example.com", location_code=2840, language_code="en", limit=1, env=env)),
    ("backlinks_summary", lambda env: dataforseo_client.backlinks_summary("example.com", env=env)),
    ("backlinks_referring_domains", lambda env: dataforseo_client.backlinks_referring_domains(
        "example.com", limit=1, env=env)),
    ("backlinks_list", lambda env: dataforseo_client.backlinks_list("example.com", limit=1, env=env)),
    ("backlinks_links_all", lambda env: dataforseo_client.backlinks_links_all("example.com", limit=1, env=env)),
    ("backlinks_anchors", lambda env: dataforseo_client.backlinks_anchors("example.com", limit=1, env=env)),
    ("backlinks_gap", lambda env: dataforseo_client.backlinks_gap(["rival.com"], "example.com", limit=1, env=env)),
    ("account_balance", lambda env: dataforseo_client.account_balance(env=env)),
]


@pytest.mark.parametrize("env,host", [
    ("sandbox", "https://sandbox.dataforseo.com"),
    ("live", "https://api.dataforseo.com"),
])
@pytest.mark.parametrize("name,call", ENDPOINT_CALLS, ids=[n for n, _ in ENDPOINT_CALLS])
def test_every_endpoint_routes_by_env(monkeypatch, name, call, env, host):
    """Each client fn must send its request to the host chosen by `env`."""
    monkeypatch.setattr(settings, "DATAFORSEO_LOGIN", "x")
    monkeypatch.setattr(settings, "DATAFORSEO_PASSWORD", "y")
    urls: list[str] = []

    def _fake_post(url, **_kw):
        urls.append(url)
        return _FakeResp(_ok_envelope())

    def _fake_get(url, **_kw):
        urls.append(url)
        return _FakeResp({"tasks": [{"result": [{"money": {"balance": 1.0}}]}]})

    monkeypatch.setattr(dataforseo_client.httpx, "post", _fake_post)
    monkeypatch.setattr(dataforseo_client.httpx, "get", _fake_get)

    call(env)

    assert urls, f"{name} made no HTTP request"
    assert all(u.startswith(host) for u in urls), f"{name} routed to {urls}, expected {host}"
