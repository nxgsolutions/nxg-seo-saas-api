"""Unit tests for Page Discovery pure helpers (no network, no DB)."""

import gzip

from app.services import crawl_service
from app.services import page_discovery_service as pd

URLSET = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://ex.com/</loc><lastmod>2026-10-01</lastmod></url>
  <url><loc>https://ex.com/blog/post-1</loc></url>
</urlset>"""

INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://ex.com/sitemap-posts.xml</loc></sitemap>
  <sitemap><loc>https://ex.com/sitemap-pages.xml</loc></sitemap>
</sitemapindex>"""


def test_parse_sitemap_urls_urlset():
    out = crawl_service.parse_sitemap_urls(URLSET)
    assert out["type"] == "urlset"
    locs = [e["loc"] for e in out["entries"]]
    assert locs == ["https://ex.com/", "https://ex.com/blog/post-1"]
    assert out["entries"][0]["lastmod"] == "2026-10-01"
    assert out["entries"][1]["lastmod"] is None


def test_parse_sitemap_urls_index():
    out = crawl_service.parse_sitemap_urls(INDEX)
    assert out["type"] == "index"
    assert [e["loc"] for e in out["entries"]] == [
        "https://ex.com/sitemap-posts.xml", "https://ex.com/sitemap-pages.xml"]


def test_parse_sitemap_urls_invalid():
    assert crawl_service.parse_sitemap_urls("not xml")["entries"] == []
    assert crawl_service.parse_sitemap_urls("")["type"] is None


def test_maybe_gunzip_roundtrip():
    raw = gzip.compress(URLSET.encode())
    assert crawl_service._maybe_gunzip("https://ex.com/sitemap.xml.gz", raw) == URLSET.encode()
    # Plain bytes are returned untouched.
    assert crawl_service._maybe_gunzip("https://ex.com/sitemap.xml", b"<xml/>") == b"<xml/>"


def test_classify_page_type():
    assert pd.classify_page_type("https://ex.com/") == "home"
    assert pd.classify_page_type("https://ex.com/tag/upsc") == "tag"
    assert pd.classify_page_type("https://ex.com/category/notes") == "category"
    assert pd.classify_page_type("https://ex.com/blog/how-to-study") == "blog"
    assert pd.classify_page_type("https://ex.com/about-us") == "other"
    assert pd.classify_page_type("https://ex.com/upsc-syllabus") == "content"


def test_collect_from_sitemaps_recurses_index_and_dedupes(monkeypatch):
    posts = """<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://ex.com/blog/a</loc></url>
      <url><loc>https://ex.com/blog/a/</loc></url>
      <url><loc>https://other.com/x</loc></url>
      <url><loc>https://ex.com/img.png</loc></url>
    </urlset>"""
    pages = """<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://ex.com/about</loc></url></urlset>"""
    responses = {
        "https://ex.com/robots.txt": (200, "Sitemap: https://ex.com/sitemap_index.xml"),
        "https://ex.com/sitemap_index.xml": (200, INDEX),
        "https://ex.com/sitemap-posts.xml": (200, posts),
        "https://ex.com/sitemap-pages.xml": (200, pages),
    }
    monkeypatch.setattr(crawl_service, "_fetch_text", lambda u: responses.get(u, (404, "")))

    source, found, truncated = pd.collect_from_sitemaps("https://ex.com", "ex.com")
    locs = {e["loc"] for e in found}
    # trailing-slash dupe collapsed, off-site + asset dropped, index recursed into both children
    assert locs == {"https://ex.com/blog/a", "https://ex.com/about"}
    assert not truncated
    assert source == "https://ex.com/sitemap-posts.xml"


def test_collect_truncates_at_cap(monkeypatch):
    many = "".join(f"<url><loc>https://ex.com/p/{i}</loc></url>" for i in range(pd.MAX_PAGES + 10))
    big = f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{many}</urlset>'
    responses = {
        "https://ex.com/robots.txt": (404, ""),
        "https://ex.com/sitemap.xml": (200, big),
    }
    monkeypatch.setattr(crawl_service, "_fetch_text", lambda u: responses.get(u, (404, "")))
    _source, found, truncated = pd.collect_from_sitemaps("https://ex.com", "ex.com")
    assert len(found) == pd.MAX_PAGES
    assert truncated is True


# ── Enrichment: block handling + JSON-LD extraction ──────────────────────────

class _FakeResp:
    """Minimal stand-in for an httpx.Response (status/headers/content)."""

    def __init__(self, status_code: int, body: bytes = b"", content_type: str = "text/html"):
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self.content = body


def _patch_fetch(monkeypatch, resp: _FakeResp, final_url: str) -> None:
    monkeypatch.setattr(pd.audit_service, "safe_fetch", lambda url: (resp, final_url, 10))


def test_enrich_one_block_status_is_blocked_not_noindex(monkeypatch):
    """A 503 (bot-protection) is flagged blocked, with no fabricated facts —
    indexability stays unknown (None), not False/"Noindex", and no score/words."""
    _patch_fetch(monkeypatch, _FakeResp(503), "https://ex.com/products/x")
    out = pd._enrich_one({"loc": "https://ex.com/products/x"}, jitter=False)
    assert out["blocked"] is True
    assert out["status"] == 503
    assert out["indexable"] is None
    assert out["word_count"] is None
    assert out["score"] is None


def test_enrich_one_recovers_word_count_from_json_ld(monkeypatch):
    """A JS-rendered page with a near-empty visible body still gets a real word
    count from its JSON-LD description — the same recovery Content Optimization
    uses — so the page is scorable instead of showing a blank/thin count."""
    html = (
        "<html><head><title>Brass Horse Showpiece | PeetalKraft</title></head>"
        "<body><div id=app>Loading…</div>"
        '<script type="application/ld+json">'
        '{"@type":"Product","name":"Brass Horse",'
        '"description":"A handcrafted brass horse showpiece on wheels, seventeen '
        "centimetres tall, cast and polished by skilled artisans. A warm, auspicious "
        'piece for home decor, festive gifting and Diwali celebrations."}'
        "</script></body></html>"
    ).encode()
    url = "https://ex.com/products/brass-horse"
    _patch_fetch(monkeypatch, _FakeResp(200, html), url)
    out = pd._enrich_one({"loc": url}, jitter=False)

    from app.services import content_score
    expected = content_score.extract(html.decode(), url)["word_count"]
    assert out["blocked"] is False
    assert out["indexable"] is True
    assert out["word_count"] == expected
    # The thin DOM body alone ("Loading…") is 1 word; JSON-LD recovery lifts it well past that.
    assert out["word_count"] > 15
    assert "Brass Horse Showpiece" in out["title"]


class _FakePage:
    """A site_pages row stand-in carrying good data from an earlier scan."""

    def __init__(self):
        self.http_status = 200
        self.page_type = "content"
        self.indexable = True
        self.word_count = 150
        self.latest_audit_score = 72  # good value that MUST survive a transient block
        self.url = "https://ex.com/products/x"
        self.title = "Old title"
        self.last_checked_at = None


def test_apply_enrichment_block_is_non_destructive(monkeypatch):
    """A transient block records the block status but must NOT wipe the word
    count / score / indexability an earlier successful scan read — otherwise a
    throttled rescan degrades good data. The block status alone flags the UI."""
    page = _FakePage()
    monkeypatch.setattr(pd.page_history_service, "get_or_create_page",
                        lambda db, ws, loc, title="": page)

    class _DB:
        def commit(self):
            pass

    result = {"loc": "https://ex.com/products/x", "status": 503, "blocked": True,
              "page_type": "content", "indexable": None, "word_count": None, "score": None}
    pd._apply_enrichment(_DB(), object(), [result])

    assert page.http_status == 503          # the block is recorded (UI shows "Blocked")
    assert page.last_checked_at is not None  # we did check it
    assert page.word_count == 150            # kept
    assert page.latest_audit_score == 72     # kept — never wiped by a transient block
    assert page.indexable is True            # kept


def test_prioritize_orders_gap_first():
    """Each run enriches gap-first so a rate-limited site still converges across
    runs: never-fetched → previously-blocked → no word count → already has data."""
    uk = pd.page_history_service.url_key
    entries = [
        {"loc": "https://ex.com/has-data"},
        {"loc": "https://ex.com/blocked"},
        {"loc": "https://ex.com/never"},   # absent from DB state -> http_status None
        {"loc": "https://ex.com/thin"},
    ]
    state_rows = [
        (uk("https://ex.com/has-data"), 200, 300),
        (uk("https://ex.com/blocked"), 503, 300),
        (uk("https://ex.com/thin"), 200, None),
    ]

    class _Res:
        def all(self):
            return state_rows

    class _DB:
        def execute(self, *a, **k):
            return _Res()

    class _Dom:
        id = "d"

    out = [e["loc"] for e in pd._prioritize(_DB(), _Dom(), entries)]
    assert out == [
        "https://ex.com/never",     # tier 0: never fetched
        "https://ex.com/blocked",   # tier 1: retry a cooled block
        "https://ex.com/thin",      # tier 2: fetched but no readable content
        "https://ex.com/has-data",  # tier 3: already has data — refresh last
    ]


def test_run_discovery_pauses_when_site_rate_limits(db_session, monkeypatch):
    """End-to-end: when a site clamps the IP mid-scan, the run PAUSES (status
    'partial') instead of plowing through and blocking everything — and the pages
    read before the clamp are saved. No network (fetch is faked)."""
    import threading

    import pytest
    from sqlalchemy import func, select

    from app.models.domain import Domain
    from app.models.page_discovery import PageDiscoveryRun
    from app.models.site_page import SitePage

    domain = db_session.execute(select(Domain).limit(1)).scalar_one_or_none()
    if domain is None:
        pytest.skip("no domain in the dev DB to exercise run_discovery")
    host = domain.domain

    entries = [{"loc": f"https://{host}/p/{i}", "lastmod": None} for i in range(100)]
    monkeypatch.setattr(pd, "collect_from_sitemaps",
                        lambda origin, root: (f"https://{host}/sitemap.xml", entries, False))
    monkeypatch.setattr(pd, "ENRICH_JITTER_S", (0.0, 0.0))  # no real delays in the test

    # 200 for the first window (20 fetches), then the IP is clamped → 503 for the rest.
    lock, calls = threading.Lock(), {"n": 0}
    html = (b"<html><head><title>P</title></head><body><article>"
            + b"word " * 80 + b"</article></body></html>")

    def fake_fetch(url):
        with lock:
            calls["n"] += 1
            n = calls["n"]
        return (_FakeResp(200, html) if n <= 20 else _FakeResp(503)), url, 5

    monkeypatch.setattr(pd.audit_service, "safe_fetch", fake_fetch)
    monkeypatch.setattr(db_session, "close", lambda: None)  # don't close the fixture's session

    pd.run_discovery(domain.workspace_id, domain.id, session_factory=lambda: db_session)

    run = db_session.execute(
        select(PageDiscoveryRun).where(PageDiscoveryRun.domain_id == domain.id)
        .order_by(PageDiscoveryRun.started_at.desc()).limit(1)).scalar_one()
    assert run.status == "partial"          # paused, not completed
    assert run.pages_enriched == 40         # window 1 (ok) + window 2 (fully blocked → stop)
    ours_200 = db_session.execute(
        select(func.count()).select_from(SitePage).where(
            SitePage.domain_id == domain.id, SitePage.url_key.like("%/p/%"),
            SitePage.http_status == 200)).scalar_one()
    assert ours_200 == 20                   # everything read before the clamp was saved
