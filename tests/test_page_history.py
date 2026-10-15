"""Page history: URL identity, change detection, snapshots from tools, search impact."""

from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

from app.models.site_page import PageSearchDaily
from app.services import audit_service, content_opt_service, page_history_service as ph

REGISTER = "/api/auth/register"
LOGIN = "/api/auth/login"
WS = "/api/workspaces"
PW = "SecurePass123"

TEXT = " ".join(["Plan your study with daily practice, revision and mock tests."] * 30)


def _html(title: str, h2s: list[str], body: str = TEXT, meta: str = "A study plan.") -> str:
    heads = "".join(f"<h2>{h}</h2><p>{body}</p>" for h in h2s)
    return (f"<html lang='en'><head><title>{title}</title><meta name='description' content='{meta}'>"
            f"<meta name='viewport' content='width=device-width'></head>"
            f"<body><article><h1>Study plan</h1><p>{body}</p>{heads}</article></body></html>")


# ── Pure ────────────────────────────────────────────────────────────────────

def test_url_key_ignores_scheme_www_slash_fragment() -> None:
    assert ph.url_key("https://www.Site.com/Blog/post/#top") == "site.com/Blog/post"
    assert ph.url_key("http://site.com/") == "site.com/"
    assert ph.url_key("site.com/a?x=1") == "site.com/a?x=1"


def test_diff_reports_meta_content_and_checks() -> None:
    prev = {"facts": {"title": "Old", "meta": "m", "h1": ["A"], "headings": [{"level": 2, "text": "Intro"}],
                      "word_count": 500, "images": 1, "images_missing_alt": 1, "internal_links": 2,
                      "external_links": 0, "has_schema": False},
            "tech": {"canonical": "", "indexable": True}, "checks": [{"id": "title", "label": "Title", "status": "warn"},
                                                                      {"id": "speed", "label": "Speed", "status": "pass"}]}
    cur = {"facts": {"title": "New", "meta": "m", "h1": ["A"], "headings": [{"level": 2, "text": "Intro"},
                                                                           {"level": 2, "text": "FAQ"}],
                     "word_count": 900, "images": 1, "images_missing_alt": 0, "internal_links": 6,
                     "external_links": 0, "has_schema": True},
           "tech": {"canonical": "https://s.com/p", "indexable": True},
           "checks": [{"id": "title", "label": "Title", "status": "pass"}, {"id": "speed", "label": "Speed", "status": "fail"}]}
    fields = {c["field"]: c for c in ph.diff(prev, cur)}
    assert fields["title"]["before"] == "Old" and fields["title"]["after"] == "New"
    assert "meta" not in fields
    assert fields["headings_added"]["after"] == ["FAQ"]
    assert fields["word_count"]["after"] == 900
    assert {"images_missing_alt", "internal_links", "has_schema", "canonical", "fixed"} <= set(fields)
    assert fields["fixed"]["after"] == ["Title"]
    assert "regressed" not in fields  # response time is noise, not a page change


def test_diff_skips_fields_older_snapshots_did_not_record() -> None:
    prev = {"facts": {"title": "T", "meta": "m", "h1": []}, "tech": {"canonical": None}}
    cur = {"facts": {"title": "T", "meta": "m", "h1": [], "headings": [{"level": 2, "text": "X"}], "word_count": 900},
           "tech": {"canonical": "https://s.com/"}}
    assert ph.diff(prev, cur) == []


# ── API ─────────────────────────────────────────────────────────────────────

def _login(client: TestClient) -> str:
    email = f"ph-{uuid4().hex}@example.com"
    client.post(REGISTER, json={"first_name": "P", "last_name": "H", "email": email, "password": PW})
    assert client.post(LOGIN, json={"email": email, "password": PW}).status_code == 200
    wid = client.get(WS).json()[0]["id"]
    client.post(f"{WS}/{wid}/domains", json={"name": "Site", "url": "https://site.com"})
    return wid


def _stub_audit(monkeypatch, html: str) -> None:
    def fake(url, device="desktop"):
        r = audit_service.analyze_html(html, "https://site.com/plan", 200, 300, len(html), device)
        r.html = html
        return r
    monkeypatch.setattr(audit_service, "run_audit", fake)


def test_audits_build_a_timeline_with_changes(client: TestClient, monkeypatch) -> None:
    wid = _login(client)
    _stub_audit(monkeypatch, _html("UPSC plan", ["Intro"]))
    assert client.post(f"{WS}/{wid}/page-audits", json={"url": "https://site.com/plan"}).status_code == 201
    _stub_audit(monkeypatch, _html("UPSC study plan 2026", ["Intro", "Mock tests"]))
    second = client.post(f"{WS}/{wid}/page-audits", json={"url": "https://site.com/plan/"})
    assert second.status_code == 201
    assert second.json()["page_id"] and {c["field"] for c in second.json()["changes"]} >= {"title"}

    pages = client.get(f"{WS}/{wid}/pages").json()
    assert pages["pagination"]["total"] == 1  # same page despite the trailing slash
    page = pages["rows"][0]
    assert page["snapshot_count"] == 2 and page["domain_id"] and page["last_changed_at"]

    tl = client.get(f"{WS}/{wid}/pages/{page['id']}").json()
    newest = tl["snapshots"][0]
    fields = {c["field"] for c in newest["changes"]}
    assert {"title", "headings_added"} <= fields
    assert tl["snapshots"][1]["changes"] == []

    # Website filter + "other sites" filter.
    did = page["domain_id"]
    assert client.get(f"{WS}/{wid}/pages", params={"domain_id": did}).json()["pagination"]["total"] == 1
    assert client.get(f"{WS}/{wid}/pages", params={"domain_id": "other"}).json()["pagination"]["total"] == 0


def test_inspect_reads_live_tags_and_records(client: TestClient, monkeypatch) -> None:
    wid = _login(client)
    html = _html("Pricing", ["Plans"]).replace(
        "</head>", "<meta property='og:title' content='OG T'><link rel='canonical' href='https://site.com/p'></head>")
    monkeypatch.setattr(content_opt_service, "_fetch_html", lambda url: (html, "https://site.com/p"))
    r = client.post(f"{WS}/{wid}/pages/inspect", json={"url": "site.com/p"}).json()
    assert r["title"] == "Pricing" and r["og_title"] == "OG T" and r["canonical"] == "https://site.com/p"
    assert r["page_id"]
    assert client.get(f"{WS}/{wid}/pages/{r['page_id']}").json()["snapshots"][0]["source"] == "inspect"


def test_pages_are_private_per_workspace(client: TestClient, monkeypatch) -> None:
    wid = _login(client)
    monkeypatch.setattr(content_opt_service, "_fetch_html", lambda url: (_html("A", []), "https://site.com/a"))
    pid = client.post(f"{WS}/{wid}/pages/inspect", json={"url": "site.com/a"}).json()["page_id"]
    other = _login(client)
    assert client.get(f"{WS}/{other}/pages/{pid}").status_code == 404
    assert client.get(f"{WS}/{other}/pages").json()["pagination"]["total"] == 0


def test_performance_before_after_from_stored_days(client: TestClient, db_session, monkeypatch) -> None:
    wid = _login(client)
    monkeypatch.setattr(content_opt_service, "_fetch_html", lambda url: (_html("Old", []), "https://site.com/x"))
    pid = client.post(f"{WS}/{wid}/pages/inspect", json={"url": "site.com/x"}).json()["page_id"]
    monkeypatch.setattr(content_opt_service, "_fetch_html", lambda url: (_html("New title", []), "https://site.com/x"))
    client.post(f"{WS}/{wid}/pages/inspect", json={"url": "site.com/x"})

    # Move the change 20 days back and store 40 days of search data around it.
    from app.models.site_page import PageSnapshot
    snaps = db_session.query(PageSnapshot).filter(PageSnapshot.page_id == pid).all()
    change_day = date.today() - timedelta(days=20)
    for s in snaps:
        s.captured_at = datetime.combine(change_day, datetime.min.time(), tzinfo=timezone.utc)
    for i in range(40):
        d = change_day - timedelta(days=20) + timedelta(days=i)
        after = d > change_day
        db_session.add(PageSearchDaily(page_id=snaps[0].page_id, day=d, clicks=10 if after else 5,
                                       impressions=100, ctr=0.1 if after else 0.05, position=4.0 if after else 8.0))
    db_session.commit()
    monkeypatch.setattr(ph, "sync_search_daily", lambda *a, **k: "not_connected")

    perf = client.get(f"{WS}/{wid}/pages/{pid}/performance").json()
    assert perf["status"] == "not_connected" and len(perf["series"]) == 40
    imp = perf["impacts"][0]
    assert imp["too_early"] is False
    assert imp["before"]["clicks"] < imp["after"]["clicks"]
    assert imp["after"]["position"] == 4.0 and imp["before"]["position"] == 8.0
