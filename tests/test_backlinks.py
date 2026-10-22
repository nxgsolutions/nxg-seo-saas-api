"""Backlinks (DataForSEO Backlinks API): storage, reuse, budget guards, link gap.

The provider is mocked — no API calls (live or sandbox).
"""

import uuid

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.models.domain import Domain
from app.models.user import User
from app.services import backlinks_service as svc, dataforseo_client, workspace_service

OURS = "pytest-ours-bl.example"
RIVAL = "pytest-rival-bl.example"
RIVAL2 = "pytest-rival2-bl.example"


@pytest.fixture
def provider(monkeypatch):
    calls: list[str] = []

    def summary(target, *, env=None):
        calls.append(f"summary:{target}:{env}")
        return {"cost": 0.024, "summary": {"rank": 42, "backlinks": 900, "referring_domains": 50,
                                           "referring_domains_nofollow": 10, "backlinks_spam_score": 3,
                                           "first_seen": "2020-01-02 03:04:05 +00:00",
                                           "referring_links_types": {"anchor": 800}}}

    def refs(target, *, limit, env=None):
        calls.append(f"refs:{limit}")
        return {"cost": 0.025, "total_count": 50, "items": [
            {"domain": "weak.example", "rank": 5, "backlinks": 2, "nofollow_backlinks": 0, "spam_score": 1,
             "first_seen": "2021-01-01 00:00:00 +00:00", "lost_date": None},
            {"domain": "strong.example", "rank": 80, "backlinks": 40, "nofollow_backlinks": 1, "spam_score": 0,
             "first_seen": None, "lost_date": None}]}

    def links(target, *, limit, env=None):
        calls.append("links")
        return {"cost": 0.025, "total_count": 900, "items": [
            {"url_from": "https://strong.example/a", "domain_from": "strong.example", "url_to": f"https://{target}/",
             "anchor": "best upsc notes", "item_type": "anchor", "dofollow": True, "is_new": True, "is_lost": False,
             "is_broken": False, "domain_from_rank": 80, "page_from_rank": 50, "page_from_title": "A",
             "spam_score": 0, "first_seen": None, "last_seen": None}]}

    def anchors(target, *, limit, env=None):
        calls.append("anchors")
        return {"cost": 0.025, "total_count": 30, "items": [
            {"anchor": "best upsc notes", "backlinks": 12, "referring_domains": 4, "referring_domains_nofollow": 1,
             "rank": 30, "spam_score": 2, "first_seen": None}]}

    def gap(competitors, exclude, *, limit, env=None):
        calls.append(f"gap:{','.join(competitors)}:{exclude}")
        return {"cost": 0.0276, "total_count": 2, "items": [
            {"domain": "one.example", "rank": 20, "competitors_linking": 1, "links": {competitors[0]: 3}, "spam_score": 0},
            {"domain": "both.example", "rank": 10, "competitors_linking": 2,
             "links": {c: 1 for c in competitors}, "spam_score": 0}]}

    monkeypatch.setattr(settings, "DATAFORSEO_FORCE_SANDBOX", False)
    monkeypatch.setattr(settings, "DATAFORSEO_LIVE_ENABLED", True)
    for name, fn in (("backlinks_summary", summary), ("backlinks_referring_domains", refs),
                     ("backlinks_list", links), ("backlinks_anchors", anchors), ("backlinks_gap", gap)):
        monkeypatch.setattr(dataforseo_client, name, fn)
    return calls


@pytest.fixture
def domain(db_session):
    user = User(email=f"bl-{uuid.uuid4().hex[:8]}@example.com", password_hash="x",
                first_name="Bl", last_name="Test")
    db_session.add(user)
    db_session.flush()
    ws = workspace_service.create_workspace(db_session, user, "Backlinks test")
    d = Domain(workspace_id=ws.id, name="ours", domain=OURS)
    db_session.add(d)
    db_session.flush()
    return d


def _add_competitor(db, domain, host):
    db.execute(text("INSERT INTO tracked_competitors (domain_id, competitor) VALUES (:d, :c)"),
               {"d": str(domain.id), "c": host})
    db.flush()


def test_profile_is_stored_and_reused_until_forced(db_session, domain, provider):
    allowed = {OURS}
    run, fetched = svc.fetch_profile(db_session, f"https://www.{OURS}/", env="live", allowed=allowed)
    assert fetched and run.target == OURS and run.requests == 4
    assert float(run.cost) == pytest.approx(0.099)
    assert len(provider) == 4

    again, fetched2 = svc.fetch_profile(db_session, OURS, env="live", allowed=allowed)
    assert not fetched2 and again.id == run.id and len(provider) == 4  # no second charge

    p = svc.profile_payload(db_session, domain, None)
    assert p["is_you"] and p["summary"]["rank"] == 42
    assert p["summary"]["dofollow_referring_domains"] == 40
    assert [r["domain"] for r in p["referring_domains"]] == ["strong.example", "weak.example"]  # strongest first
    assert p["backlinks"][0]["anchor"] == "best upsc notes" and p["anchors"][0]["backlinks"] == 12

    _, forced = svc.fetch_profile(db_session, OURS, env="live", allowed=allowed, force=True)
    assert forced and len(provider) == 8


def test_live_profile_only_for_your_site_and_competitors(db_session, domain, provider, monkeypatch):
    with pytest.raises(svc.BacklinksFetchNotAllowed):
        svc.fetch_profile(db_session, "random-site.example", env="live", allowed={OURS})
    monkeypatch.setattr(settings, "DATAFORSEO_LIVE_ENABLED", False)
    with pytest.raises(svc.BacklinksFetchNotAllowed):
        svc.fetch_profile(db_session, OURS, env="live", allowed={OURS})
    assert provider == []
    # The sandbox is free dummy data: any domain, even with live switched off.
    run, fetched = svc.fetch_profile(db_session, "random-site.example", env="sandbox", allowed=set())
    assert fetched and run.environment == "sandbox"


def test_sandbox_and_live_data_never_mix(db_session, domain, provider):
    svc.fetch_profile(db_session, OURS, env="sandbox", allowed=set())
    assert svc.latest_run(db_session, "profile", OURS, "live") is None
    assert svc.profile_payload(db_session, domain, None)["summary"] is None  # live workspace view


def test_link_gap_needs_competitors_and_refetches_when_list_changes(db_session, domain, provider):
    with pytest.raises(svc.NoCompetitors):
        svc.fetch_gap(db_session, domain, env="live")

    _add_competitor(db_session, domain, RIVAL)
    run, fetched = svc.fetch_gap(db_session, domain, env="live")
    assert fetched and run.competitors == [RIVAL] and provider[-1] == f"gap:{RIVAL}:{OURS}"
    _, again = svc.fetch_gap(db_session, domain, env="live")
    assert not again  # same list -> stored run reused

    _add_competitor(db_session, domain, RIVAL2)
    g = svc.gap_payload(db_session, domain)
    assert g["competitors_changed"] and [d["domain"] for d in g["domains"]] == ["both.example", "one.example"]
    _, changed = svc.fetch_gap(db_session, domain, env="live")
    assert changed and not svc.gap_payload(db_session, domain)["competitors_changed"]


def test_estimated_cost():
    assert svc.estimated_cost("profile", "sandbox") == 0
    assert svc.estimated_cost("profile", "live") == pytest.approx(4 * 0.024 + 301 * 0.000036, abs=1e-4)
    assert svc.estimated_cost("gap", "live") == pytest.approx(0.024 + 100 * 0.000036, abs=1e-4)


def test_routes(client, provider):
    from uuid import uuid4
    email = f"blr-{uuid4().hex}@example.com"
    client.post("/api/auth/register", json={"first_name": "B", "last_name": "L", "email": email,
                                            "password": "SecurePass123"})
    client.post("/api/auth/login", json={"email": email, "password": "SecurePass123"})
    wid = client.get("/api/workspaces").json()[0]["id"]
    did = client.post(f"/api/workspaces/{wid}/domains", json={"url": OURS}).json()["id"]
    base = f"/api/workspaces/{wid}/domains/{did}/backlinks"

    empty = client.get(f"{base}/profile").json()
    assert empty["summary"] is None and empty["can_fetch"] and len(provider) == 0  # GET never fetches

    r = client.post(f"{base}/profile/refresh")
    assert r.status_code == 200 and r.json()["fetched_now"] and r.json()["summary"]["backlinks"] == 900
    r2 = client.post(f"{base}/profile/refresh")
    assert not r2.json()["fetched_now"] and len(provider) == 4

    assert client.post(f"{base}/profile/refresh", params={"target": "stranger.example"}).status_code == 403
    assert client.post(f"{base}/gap/refresh").status_code == 409  # no competitors yet
    opts = client.get(f"{base}/options").json()
    assert opts["domains"][0]["domain"] == OURS and opts["domains"][0]["fetched_at"]


def test_details_breakdowns_history_and_changes(db_session, domain, provider):
    allowed = {OURS}
    assert svc.details(db_session, domain, None)["breakdowns"] is None  # nothing saved yet
    svc.fetch_profile(db_session, OURS, env="live", allowed=allowed)
    d = svc.details(db_session, domain, None)
    assert d["breakdowns"]["link_types"] == [{"label": "Anchor", "count": 800}]
    assert d["link_status"] == {"saved": 1, "dofollow": 1, "nofollow": 0, "new": 1, "lost": 0, "broken": 0}
    assert len(d["history"]) == 1 and d["changes"] is None

    svc.fetch_profile(db_session, OURS, env="live", allowed=allowed, force=True)
    d2 = svc.details(db_session, domain, None)
    assert len(d2["history"]) == 2
    assert d2["changes"]["backlinks"] == 0 and d2["changes"]["gained_domains"] == []


def test_paged_lists_search_filter_sort(db_session, domain, provider):
    svc.fetch_profile(db_session, OURS, env="live", allowed={OURS})
    rows, info = svc.list_rows(db_session, domain, None, "domains", sort="backlinks", descending=False, page_size=1)
    assert info["total"] == 2 and info["total_pages"] == 2 and rows[0]["domain"] == "weak.example"
    rows, _ = svc.list_rows(db_session, domain, None, "domains", q="strong")
    assert [r["domain"] for r in rows] == ["strong.example"] and rows[0]["dofollow_backlinks"] == 39
    rows, info = svc.list_rows(db_session, domain, None, "links", filter_="nofollow")
    assert info["total"] == 0
    rows, _ = svc.list_rows(db_session, domain, None, "pages")
    assert rows[0]["url"] == f"https://{OURS}/" and rows[0]["links"] == 1 and rows[0]["domains"] == 1
    with pytest.raises(ValueError):
        svc.list_rows(db_session, domain, None, "nope")


def test_reclaim_groups_broken_backlinks_by_dead_page(db_session, domain, provider, monkeypatch):
    def broken_links(target, *, limit, env=None):
        dead = f"https://{target}/old-guide"
        return {"cost": 0.025, "total_count": 3, "items": [
            {"url_from": "https://strong.example/x", "domain_from": "strong.example", "url_to": dead,
             "anchor": "old guide", "item_type": "anchor", "dofollow": True, "is_new": False, "is_lost": False,
             "is_broken": True, "domain_from_rank": 70, "page_from_rank": 40, "page_from_title": "X",
             "spam_score": 0, "first_seen": "2021-01-01 00:00:00 +00:00", "last_seen": "2022-01-01 00:00:00 +00:00"},
            {"url_from": "https://mid.example/y", "domain_from": "mid.example", "url_to": dead,
             "anchor": "guide", "item_type": "anchor", "dofollow": False, "is_new": False, "is_lost": False,
             "is_broken": True, "domain_from_rank": 30, "page_from_rank": 20, "page_from_title": "Y",
             "spam_score": 0, "first_seen": None, "last_seen": None},
            {"url_from": "https://ok.example/z", "domain_from": "ok.example", "url_to": f"https://{target}/",
             "anchor": "home", "item_type": "anchor", "dofollow": True, "is_new": False, "is_lost": False,
             "is_broken": False, "domain_from_rank": 10, "page_from_rank": 5, "page_from_title": "Z",
             "spam_score": 0, "first_seen": None, "last_seen": None}]}
    monkeypatch.setattr(dataforseo_client, "backlinks_list", broken_links)
    svc.fetch_profile(db_session, OURS, env="live", allowed={OURS})

    rows, info = svc.list_rows(db_session, domain, None, "reclaim")
    assert info["total"] == 1  # only the broken page, and the two links to it are grouped
    r = rows[0]
    assert r["url"] == f"https://{OURS}/old-guide"
    assert r["links"] == 2 and r["domains"] == 2 and r["dofollow"] == 1
    assert r["best_authority"] == 70
    assert r["priority"] == 1 * (70 + 1)  # dofollow × (strongest linking domain + 1)
    assert "old guide" in r["anchors"]


def test_analytics_trend_anchors_diversity(db_session, domain, provider):
    svc.fetch_profile(db_session, OURS, env="live", allowed={OURS})
    svc.fetch_profile(db_session, OURS, env="live", allowed={OURS}, force=True)
    a = svc.analytics(db_session, domain, None)
    assert len(a["trend"]) == 2 and a["trend"][0]["fetched_at"] <= a["trend"][1]["fetched_at"]
    assert a["anchors"]["total"] == 1 and sum(c["count"] for c in a["anchors"]["classes"]) == 1
    assert a["new_lost"]["since"] is not None and a["new_lost"]["backlinks_delta"] == 0
    assert a["diversity"]["referring_domains"] == 50 and a["diversity"]["dofollow_share"] == 0.8


def test_anchor_classification():
    brands, kw = {"acme"}, {"best running shoes"}
    assert svc.classify_anchor(None, brands, kw) == "Empty/other"
    assert svc.classify_anchor("https://x.com/a", brands, kw) == "URL"
    assert svc.classify_anchor("example.com", brands, kw) == "URL"
    assert svc.classify_anchor("Acme store", brands, kw) == "Branded"
    assert svc.classify_anchor("best running shoes online", brands, kw) == "Exact match"
    assert svc.classify_anchor("click here", brands, kw) == "Generic"


def test_site_detail_prefers_detail_run(db_session, domain, provider, monkeypatch):
    from sqlalchemy import select
    from app.models.backlink import BacklinkRun

    svc.fetch_profile(db_session, OURS, env="live", allowed={OURS})
    d = svc.site_detail(db_session, domain, None, "strong.example")
    assert d["found"] and d["complete"] is False and d["summary"]["links_to_you"] == 1

    def all_links(target, *, limit, filters=None, env=None):
        return {"cost": 0.03, "total_count": 3, "items": [
            {"url_from": f"https://strong.example/{i}", "domain_from": "strong.example",
             "url_to": f"https://{target}/p{i}", "anchor": f"a{i}", "item_type": "anchor",
             "dofollow": True, "is_new": False, "is_lost": False, "is_broken": False,
             "domain_from_rank": 80, "page_from_rank": 50, "page_from_title": "T",
             "spam_score": 0, "first_seen": None, "last_seen": None} for i in range(3)]}
    monkeypatch.setattr(dataforseo_client, "backlinks_links_all", all_links)

    f = dict(kind="site", value="strong.example", filters=[["domain_from", "=", "strong.example"]],
             env="live", allowed={OURS})
    run, fetched = svc.fetch_links_detail(db_session, OURS, **f)
    assert fetched and run.scope == "site:strong.example" and run.kind == "site"
    d2 = svc.site_detail(db_session, domain, None, "strong.example")
    assert d2["complete"] and d2["summary"]["links_to_you"] == 3

    _, again = svc.fetch_links_detail(db_session, OURS, **f)
    assert not again  # reused, no second charge
    _, forced = svc.fetch_links_detail(db_session, OURS, **f, force=True)
    assert forced
    n = len(db_session.execute(select(BacklinkRun).where(
        BacklinkRun.kind == "site", BacklinkRun.scope == "site:strong.example")).scalars().all())
    assert n == 1  # forced refetch replaces, detail runs don't accumulate


def test_page_detail_reports_broken(db_session, domain, provider, monkeypatch):
    dead = f"https://{OURS}/old-guide"

    def broken_links(target, *, limit, env=None):
        return {"cost": 0.025, "total_count": 1, "items": [
            {"url_from": "https://strong.example/x", "domain_from": "strong.example", "url_to": dead,
             "anchor": "old guide", "item_type": "anchor", "dofollow": True, "is_new": False,
             "is_lost": False, "is_broken": True, "domain_from_rank": 70, "page_from_rank": 40,
             "page_from_title": "X", "spam_score": 0, "first_seen": None, "last_seen": None}]}
    monkeypatch.setattr(dataforseo_client, "backlinks_list", broken_links)
    svc.fetch_profile(db_session, OURS, env="live", allowed={OURS})
    d = svc.page_detail(db_session, domain, None, dead)
    assert d["found"] and d["summary"]["status"] == "broken"
    assert d["summary"]["suggested_fix"] and d["summary"]["linking_sites"] == 1


def test_detail_and_analytics_routes(client, provider):
    from uuid import uuid4
    email = f"bld-{uuid4().hex}@example.com"
    client.post("/api/auth/register", json={"first_name": "B", "last_name": "L", "email": email,
                                            "password": "SecurePass123"})
    client.post("/api/auth/login", json={"email": email, "password": "SecurePass123"})
    wid = client.get("/api/workspaces").json()[0]["id"]
    did = client.post(f"/api/workspaces/{wid}/domains", json={"url": OURS}).json()["id"]
    base = f"/api/workspaces/{wid}/domains/{did}/backlinks"

    client.post(f"{base}/profile/refresh")
    assert client.get(f"{base}/analytics").json()["trend"]
    site = client.get(f"{base}/profile/site", params={"host": "strong.example"}).json()
    assert site["found"] and site["summary"]["links_to_you"] == 1
    page = client.get(f"{base}/profile/page", params={"url": f"https://{OURS}/"}).json()
    assert page["found"]
    csv_resp = client.get(f"{base}/export", params={"list": "domains"})
    assert csv_resp.status_code == 200 and "strong.example" in csv_resp.text
