"""The backend serves ANY market/language, not just India/English.

Guards the locale fixes:
  * one unified country->(location_code, location_name) table, with an unknown
    country falling back to a neutral US default (not India);
  * ranked-keyword fetch resolves the DOMAIN's language (not a hardcoded "en"),
    so a run is stored under the same language the read path looks it up by;
  * Keyword Analysis research runs in the domain's own market/language when the
    request omits them;
  * page-insight "our page" lookup uses the domain's language.
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.routes import opportunity as opp
from app.models.domain import Domain
from app.models.keyword_analysis import KeywordResearch
from app.models.ranked_keyword import DomainRankedKeyword, DomainRankedKeywordRun
from app.models.user import User
from app.services import domain_analytics_service as das
from app.services import keyword_analysis_service, page_insight_service, workspace_service
from app.services.ranked_keywords_service import norm_host

REGISTER = "/api/auth/register"
LOGIN = "/api/auth/login"
WS = "/api/workspaces"
PW = "SecurePass123"


# --- finding #4: one map, consistent code<->name, neutral fallback ----------

@pytest.mark.parametrize("cc", ["in", "us", "gb", "ca", "au", "de", "fr", "ae",
                                "sg", "es", "it", "nl", "br", "jp"])
def test_location_code_and_name_agree(cc):
    code, name = das.GEO_TARGETS[cc]
    assert das.location_code_for(cc) == code
    assert das.location_name_for(cc) == name


def test_unknown_country_falls_back_to_us_not_india():
    assert das.DEFAULT_COUNTRY == "us"
    assert das.location_code_for("zz") == das.GEO_TARGETS["us"][0]
    assert das.location_name_for("zz") == "United States"
    # Regression: the old behaviour forced unknowns to India.
    assert das.location_code_for("zz") != das.GEO_TARGETS["in"][0]


# --- finding #1: ranked-keyword language resolves from the domain ------------

def _ctx(country=None, language=None):
    return SimpleNamespace(domain=Domain(name="x", domain="x.example",
                                         country=country, language=language))


def test_language_and_market_resolve_from_domain():
    ctx = _ctx(country="de", language="de")
    assert opp._language(ctx) == "de"
    assert opp._market(ctx, None) == "de"


def test_language_falls_back_to_global_default():
    ctx = _ctx(country=None, language=None)
    assert opp._language(ctx) == "en"       # global language default
    assert opp._market(ctx, None) == "in"   # global market default


def test_explicit_language_overrides_domain():
    ctx = _ctx(country="de", language="de")
    assert opp._language(ctx, "fr") == "fr"


# --- finding #3: page-insight "our page" lookup honours the language ---------

def test_find_our_page_uses_domain_language(db_session):
    user = User(email=f"loc-{uuid.uuid4().hex[:8]}@example.com", password_hash="x",
                first_name="L", last_name="C")
    db_session.add(user)
    db_session.flush()
    ws = workspace_service.create_workspace(db_session, user, "Locale test")
    host = f"zzloc-{uuid.uuid4().hex[:6]}.example"
    d = Domain(workspace_id=ws.id, name="ours", domain=host, country="de", language="de")
    db_session.add(d)
    db_session.flush()
    loc = das.location_code_for("de")
    run = DomainRankedKeywordRun(environment="live", target=norm_host(host),
                                 location_code=loc, language_code="de",
                                 requested_limit=10, items_count=1, meta={})
    db_session.add(run)
    db_session.flush()
    db_session.add(DomainRankedKeyword(
        run_id=run.id, keyword="blue widget guide", rank_absolute=3,
        search_volume=500, keyword_difficulty=20,
        url=f"https://{norm_host(host)}/blue-widget-guide"))
    db_session.flush()

    # Correct language finds our stored ranking page...
    found = page_insight_service._find_our_page(
        db_session, d, "blue widget guide", "live", loc, "de")
    assert found == f"https://{norm_host(host)}/blue-widget-guide"
    # ...the old hardcoded "en" would have missed it entirely.
    assert page_insight_service._find_our_page(
        db_session, d, "blue widget guide", "live", loc, "en") is None


# --- finding #2: research runs in the domain's market when omitted -----------

def _domain_with_locale(client: TestClient, db_session, *, country, language):
    email = f"loc-{uuid.uuid4().hex}@example.com"
    client.post(REGISTER, json={"first_name": "L", "last_name": "C",
                                "email": email, "password": PW})
    assert client.post(LOGIN, json={"email": email, "password": PW}).status_code == 200
    wid = client.get(WS).json()[0]["id"]
    did = client.post(f"{WS}/{wid}/domains",
                      json={"name": "Site", "url": f"https://{uuid.uuid4().hex[:8]}.com"}).json()["id"]
    dom = db_session.get(Domain, uuid.UUID(did))
    dom.country, dom.language = country, language
    db_session.flush()
    return wid, did


def test_research_uses_domain_market_when_request_omits_it(client, db_session, monkeypatch):
    captured = {}

    def fake_run_research(db, workspace, domain, *, seed, country, language):
        captured.update(country=country, language=language)
        kw = keyword_analysis_service._upsert_keyword(db, seed)
        r = KeywordResearch(workspace_id=workspace.id, domain_id=domain.id,
                            seed_keyword_id=kw.id, country_code=country,
                            language_code=language, status="completed",
                            kind="keyword", result_count=0)
        db.add(r)
        db.flush()
        return r

    monkeypatch.setattr(keyword_analysis_service, "run_research", fake_run_research)
    wid, did = _domain_with_locale(client, db_session, country="de", language="de")
    base = f"{WS}/{wid}/domains/{did}/keyword-analysis/research"

    # No country/language in the request -> the domain's own market/language.
    assert client.post(base, json={"seed": "kaffeemaschine"}).status_code == 200
    assert captured == {"country": "de", "language": "de"}

    # An explicit request still wins.
    captured.clear()
    assert client.post(base, json={"seed": "kaffeemaschine", "country": "us",
                                   "language": "es"}).status_code == 200
    assert captured == {"country": "us", "language": "es"}
