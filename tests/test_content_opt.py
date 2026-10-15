"""Content Optimization: rule-based scoring (pure) + the run/session API.

No network: page fetches and the OpenAI call are stubbed.
"""

from uuid import uuid4

from fastapi.testclient import TestClient

from app.services import content_opt_service, content_score, openai_ai

REGISTER = "/api/auth/register"
LOGIN = "/api/auth/login"
WS = "/api/workspaces"
PW = "SecurePass123"

FILLER = " ".join(["Learn how a study plan helps you prepare with daily practice and revision."] * 40)


def _html(title: str, h1: str, body: str, h2s: list[str], meta: str = "") -> str:
    heads = "".join(f"<h2>{h}</h2><p>{body}</p>" for h in h2s)
    return (f"<html><head><title>{title}</title><meta name='description' content='{meta}'></head>"
            f"<body><nav><a href='/a'>Menu</a></nav><article><h1>{h1}</h1><p>{body}</p>{heads}"
            f"<ul><li>one</li></ul><p>See <a href='/x'>x</a>, <a href='/y'>y</a> and <a href='/z'>z</a>, "
            f"per <a href='https://gov.example.org/'>source</a>.</p><div><a href='/menu'>menu</a></div>"
            f"<img src='a.png' alt='chart'></article>"
            f"<footer>Copyright</footer></body></html>")


# ── Pure scoring ────────────────────────────────────────────────────────────

def test_match_full_partial_none() -> None:
    assert content_score.match("Best UPSC study plan 2026", "upsc study plan") == "full"
    assert content_score.match("A plan to study for UPSC", "upsc study plan") == "partial"
    assert content_score.match("Nothing here", "upsc study plan") == "none"


def test_extract_ignores_page_chrome() -> None:
    f = content_score.extract(_html("T", "UPSC study plan", FILLER, ["Daily schedule"]), "https://site.com/upsc-study-plan")
    assert f["h1"] == ["UPSC study plan"]
    assert f["internal_links"] == 3  # nav and non-paragraph (menu) links not counted
    assert f["external_links"] == 1
    assert f["images"] == 1 and f["images_missing_alt"] == 0
    assert f["word_count"] > 500
    assert f["reading_ease"] is not None


def test_score_is_deterministic_and_keyword_aware() -> None:
    good = content_score.extract(_html(
        "UPSC study plan: a 12-month schedule", "UPSC study plan", "UPSC study plan tips. " + FILLER,
        ["Why a UPSC study plan works", "Daily schedule", "Revision"],
        meta="A practical UPSC study plan with a daily schedule, revision cycles and mock tests so you can prepare with confidence."),
        "https://site.com/upsc-study-plan")
    bad = content_score.extract("<html><head><title>Home</title></head><body><p>Hello there friends.</p></body></html>",
                                "https://site.com/")
    checks, s1 = content_score.score(good, "upsc study plan")
    _, s1b = content_score.score(good, "upsc study plan")
    _, s2 = content_score.score(bad, "upsc study plan")
    assert s1 == s1b
    assert s1 > s2
    by_id = {c["id"]: c for c in checks}
    assert by_id["kw_title"]["status"] == "pass"
    assert by_id["kw_h1"]["status"] == "pass"
    assert by_id["kw_title"]["tip"] == ""  # passing checks carry no tip


def test_benchmark_terms_and_coverage() -> None:
    comps = [
        content_score.extract(_html(f"Plan {i}", "Plan", FILLER + " mock tests answer writing", ["Syllabus", "Mock tests"]),
                              f"https://c{i}.com/p")
        for i in range(3)
    ]
    bench = content_score.benchmark(comps, "upsc study plan")
    assert bench and bench["pages"] == 3 and bench["word_count"]
    terms = {t["term"] for t in bench["terms"]}
    assert "mock tests" in terms
    mine = content_score.extract(_html("Plan", "Plan", "Short text about revision only. " * 20, []), "https://me.com/p")
    cov = {t["term"]: t["status"] for t in content_score.term_coverage(mine, bench)}
    assert cov["mock tests"] == "missing"


# ── API ─────────────────────────────────────────────────────────────────────

def _base(client: TestClient) -> str:
    email = f"copt-{uuid4().hex}@example.com"
    client.post(REGISTER, json={"first_name": "C", "last_name": "O", "email": email, "password": PW})
    assert client.post(LOGIN, json={"email": email, "password": PW}).status_code == 200
    wid = client.get(WS).json()[0]["id"]
    return f"{WS}/{wid}/content-optimization"


def _no_ai(monkeypatch) -> None:
    def fail(*a, **k):
        raise openai_ai.OpenAiError("AI writing is not configured.")
    monkeypatch.setattr(openai_ai, "write_json", fail)


def _stub_fetch(monkeypatch) -> None:
    pages = {
        "https://me.com/upsc-study-plan": _html("Study plan", "UPSC study plan", FILLER, ["Schedule"]),
        "https://rival.com/plan": _html("UPSC study plan guide", "UPSC study plan", FILLER + " mock tests", ["Mock tests", "Syllabus"]),
    }
    monkeypatch.setattr(content_opt_service, "_fetch_html", lambda url: (pages[url], url))


def test_run_without_ai_is_rule_based_and_saved(client: TestClient, monkeypatch) -> None:
    base = _base(client)
    _stub_fetch(monkeypatch)
    _no_ai(monkeypatch)

    resp = client.post(f"{base}/run", json={
        "url": "https://me.com/upsc-study-plan", "keyword": "UPSC study plan",
        "competitor_urls": ["https://rival.com/plan"],
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    r = body["result"]
    assert body["model_used"] == "rules"
    assert r["version"] == 2 and r["keyword"] == "UPSC study plan"
    assert r["ai"] is None and r["ai_error"]
    assert r["is_sample"] is False
    assert r["checks"] and 0 <= r["content_score"] <= 100
    assert r["competitor_source"] == "manual"
    assert r["competitors"][0]["host"] == "rival.com"
    assert r["benchmark"]["pages"] == 1
    assert len(body["history"]) == 1

    listed = client.get(f"{base}/sessions", params={"page_size": 10}).json()
    assert listed["pagination"]["total"] == 1
    assert listed["rows"][0]["keyword"] == "UPSC study plan"
    one = client.get(f"{base}/sessions/{body['id']}")
    assert one.status_code == 200


def test_run_with_ai_and_number_guard(client: TestClient, monkeypatch) -> None:
    base = _base(client)
    _stub_fetch(monkeypatch)
    monkeypatch.setattr(openai_ai, "write_json", lambda *a, **k: ({
        "summary": "Your page is 97% better than 4,000 rivals.",  # invented numbers → rejected
        "title_options": ["UPSC Study Plan: Daily Schedule & Revision"],
        "meta_options": ["A clear UPSC study plan."],
        "outline": [{"heading": "Mock tests", "level": 2, "is_new": True, "note": "Top pages cover it"}],
        "intro": "A UPSC study plan…",
        "recommendations": [{"area": "content", "recommendation": "Add a mock test section.", "priority": "HIGH"}],
    }, "gpt-4o-mini"))
    r = client.post(f"{base}/run", json={"url": "https://me.com/upsc-study-plan", "keyword": "UPSC study plan"}).json()
    res = r["result"]
    assert r["model_used"] == "gpt-4o-mini"
    assert res["ai"]["title_options"] and res["ai"]["outline"][0]["heading"] == "Mock tests"
    assert res["ai"]["recommendations"][0]["priority"] == "high"
    assert res["summary_source"] == "rules"  # the guard kept the rule summary
    assert "97%" not in res["summary"]


def test_sessions_are_private_per_workspace(client: TestClient, monkeypatch) -> None:
    base = _base(client)
    _stub_fetch(monkeypatch)
    _no_ai(monkeypatch)
    sid = client.post(f"{base}/run", json={"url": "https://me.com/upsc-study-plan"}).json()["id"]

    other = _base(client)  # logs in as a new user
    assert client.get(f"{other}/sessions/{sid}").status_code == 404
    assert client.get(f"{other}/sessions").json()["pagination"]["total"] == 0


def test_get_missing_session_404(client: TestClient) -> None:
    base = _base(client)
    assert client.get(f"{base}/sessions/{uuid4()}").status_code == 404
    assert client.get(f"{base}/sessions/not-a-uuid").status_code == 404
