"""Tests for the page audit service (pure logic + SSRF guard)."""

import pytest

from app.services import audit_service
from app.services.audit_service import (
    BlockedUrlError,
    InvalidUrlError,
    analyze_html,
    normalize_url,
)

GOOD_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>A Reasonable Page Title Here</title>
  <meta name="description" content="This is a nicely sized meta description that sits comfortably within the recommended fifty to one hundred and sixty characters.">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="canonical" href="https://example.com/">
  <meta property="og:title" content="A Reasonable Page Title Here">
  <meta property="og:image" content="https://example.com/og.png">
  <script type="application/ld+json">{"@context":"https://schema.org","@type":"WebPage"}</script>
</head>
<body>
  <h1>Main Heading</h1>
  <p>%s</p>
  <img src="a.png" alt="described">
  <a href="/internal">internal</a>
  <a href="https://other.com/x">external</a>
</body>
</html>
""" % ("word " * 400)


def test_normalize_url_adds_scheme() -> None:
    assert normalize_url("example.com").startswith("https://example.com")
    assert normalize_url("http://example.com") == "http://example.com"


def test_normalize_url_rejects_garbage() -> None:
    with pytest.raises(InvalidUrlError):
        normalize_url("   ")
    with pytest.raises(InvalidUrlError):
        normalize_url("ftp://example.com")


def test_ssrf_guard_blocks_loopback() -> None:
    with pytest.raises(BlockedUrlError):
        audit_service._assert_public_host("localhost")
    with pytest.raises(BlockedUrlError):
        audit_service._assert_public_host("127.0.0.1")
    with pytest.raises(BlockedUrlError):
        audit_service._assert_public_host("169.254.169.254")  # cloud metadata
    with pytest.raises(BlockedUrlError):
        audit_service._assert_public_host("10.0.0.1")


def test_analyze_good_page_scores_high() -> None:
    result = analyze_html(
        html=GOOD_HTML,
        final_url="https://example.com/",
        status_code=200,
        load_time_ms=400,
        page_size_bytes=len(GOOD_HTML.encode()),
    )
    assert result.score >= 90
    assert result.metrics["title"] == "A Reasonable Page Title Here"
    assert result.metrics["h1_count"] == 1
    assert result.metrics["is_https"] is True
    assert result.metrics["internal_links"] == 1
    assert result.metrics["external_links"] == 1
    assert result.metrics["images_missing_alt"] == 0
    # No failing checks on a healthy page.
    assert all(c.status != "fail" for c in result.checks)


def test_analyze_bad_page_flags_problems() -> None:
    bad = "<html><body><p>hi</p><img src='x.png'></body></html>"
    result = analyze_html(
        html=bad,
        final_url="http://example.com/",  # not https
        status_code=200,
        load_time_ms=5000,  # slow
        page_size_bytes=len(bad.encode()),
    )
    failed = {c.id for c in result.checks if c.status == "fail"}
    # Missing title, description, h1, viewport, not https. (Speed is context now.)
    assert {"title", "description", "h1", "viewport", "https"} <= failed
    assert result.score < 50


def test_analyze_extracts_tags_and_new_checks() -> None:
    result = analyze_html(
        GOOD_HTML, "https://example.com/", 200, 400, len(GOOD_HTML.encode()), device="mobile"
    )
    ids = {c.id for c in result.checks}
    assert {"og", "schema", "charset"} <= ids
    assert next(c for c in result.checks if c.id == "og").status == "pass"
    assert result.metrics["device"] == "mobile"
    assert result.metrics["og_image"].endswith("og.png")
    assert result.metrics["has_structured_data"] is True
    assert result.metrics["charset"] == "utf-8"
    assert result.metrics["canonical_url"] == "https://example.com/"


def test_analyze_flags_missing_social_tags() -> None:
    html = "<html lang='en'><head><title>No social tags at all here</title></head><body><h1>x</h1></body></html>"
    result = analyze_html(html, "https://example.com/", 200, 300, len(html))
    assert next(c for c in result.checks if c.id == "og").status == "fail"
    assert next(c for c in result.checks if c.id == "schema").status == "warn"


def test_analyze_detects_noindex() -> None:
    html = (
        "<html lang='en'><head><title>Blocked From Index Page</title>"
        "<meta name='robots' content='noindex,follow'></head>"
        "<body><h1>x</h1></body></html>"
    )
    result = analyze_html(html, "https://example.com/", 200, 300, len(html))
    indexable = next(c for c in result.checks if c.id == "indexable")
    assert indexable.status == "fail"


def test_image_details_and_blog_guidance():
    html = GOOD_HTML.replace('<img src="a.png" alt="described">', '<img src="/hero.jpg"><img src="/decor.svg" alt="" width="20" height="20">')
    result = analyze_html(html, 'https://example.com/blog/post', 200, 100, len(html))
    assert result.metrics['images_missing_alt'] == 1
    rows = result.metrics['image_details']
    assert rows[0]['src'] == 'https://example.com/hero.jpg'
    assert rows[0]['alt'] is None
    assert rows[1]['alt'] == ''
    assert any('decorative' in fix for fix in rows[1]['fixes'])
    assert any(f['id'] == 'article_schema' for f in result.metrics['findings'])


def test_blog_graph_and_invalid_json():
    html = GOOD_HTML.replace('"@type":"WebPage"', '"@graph":[{"@type":["BlogPosting"],"headline":"Test"}]')
    html += '<script type="application/ld+json">{broken}</script>'
    result = analyze_html(html, 'https://example.com/news/item', 200, 100, len(html))
    ids = {f['id'] for f in result.metrics['findings']}
    assert {'schema_invalid', 'article_fields_1'} <= ids
    assert 'article_schema' not in ids
    assert next(c for c in result.checks if c.id == 'schema').status == 'fail'
    assert all(f['fix'] for f in result.metrics['findings'])


def test_duplicate_tags_and_invalid_canonical():
    html = GOOD_HTML.replace('</head>', '<title>Duplicate</title><link rel="canonical" href="javascript:alert(1)"></head>')
    result = analyze_html(html, 'https://example.com/', 200, 100, len(html))
    ids = {f['id'] for f in result.metrics['findings']}
    assert {'duplicate_title', 'duplicate_link', 'canonical_invalid'} <= ids


def _psi(overall=None, score=None):
    return {
        "performance_score": score,
        "lab": {
            "lcp": {"display": "2.1 s", "value": 2100},
            "cls": {"display": "0.05", "value": 0.05},
            "tbt": {"display": "120 ms", "value": 120},
        },
        "field": {"overall": overall} if overall else None,
    }


def test_speed_is_context_metric_not_a_scored_check() -> None:
    """Core Web Vitals is shown as context in metrics, never as a scored check."""
    result = analyze_html(GOOD_HTML, "https://example.com/", 200, 9000, len(GOOD_HTML),
                          psi=_psi(overall="FAST"))
    assert all(c.id != "speed" for c in result.checks)
    # Real users say the page is fast, so the context verdict passes.
    assert result.metrics["speed_status"] == "pass"
    assert "Core Web Vitals" in result.metrics["speed_detail"] and "LCP 2.1 s" in result.metrics["speed_detail"]
    assert result.metrics["speed_source"] == "psi"
    assert result.metrics["core_web_vitals"]["field"]["overall"] == "FAST"


def test_speed_context_poor_field() -> None:
    result = analyze_html(GOOD_HTML, "https://example.com/", 200, 100, len(GOOD_HTML),
                          psi=_psi(overall="SLOW"))
    assert result.metrics["speed_status"] == "fail"


def test_speed_context_falls_back_to_lab_score() -> None:
    # No field data → use the lab performance score bucket.
    result = analyze_html(GOOD_HTML, "https://example.com/", 200, 100, len(GOOD_HTML),
                          psi=_psi(score=40))
    assert result.metrics["speed_status"] == "fail"
    assert "40/100" in result.metrics["speed_detail"]


def test_speed_context_falls_back_to_fetch_time_without_psi() -> None:
    result = analyze_html(GOOD_HTML, "https://example.com/", 200, 5000, len(GOOD_HTML))
    assert result.metrics["speed_status"] == "fail"  # 5000 ms fetch
    assert "Fetched in" in result.metrics["speed_detail"]
    assert result.metrics["speed_source"] == "fetch"
    assert result.metrics["core_web_vitals"] is None


def test_cwv_does_not_change_the_audit_score() -> None:
    """Score invariance: PSI/CWV present vs absent yields the same audit score."""
    base = analyze_html(GOOD_HTML, "https://example.com/", 200, 100, len(GOOD_HTML))
    fast = analyze_html(GOOD_HTML, "https://example.com/", 200, 100, len(GOOD_HTML),
                        psi=_psi(overall="FAST"))
    slow = analyze_html(GOOD_HTML, "https://example.com/", 200, 100, len(GOOD_HTML),
                        psi=_psi(overall="SLOW"))
    assert base.score == fast.score == slow.score


def test_image_detail_limit_is_explicit():
    html = GOOD_HTML + '<img src="x.png" alt="test">' * 205
    result = analyze_html(html, 'https://example.com/', 200, 100, len(html))
    assert len(result.metrics['image_details']) == 200
    assert result.metrics['image_details_truncated'] is True


# ── Residential-proxy fetch backend (mocked, no network) ─────────────────────

from app.services import proxy_pool  # noqa: E402


class _Resp:
    """Minimal httpx.Response stand-in (only what _fetch reads)."""

    def __init__(self, status_code: int):
        self.status_code = status_code


def test_proxy_pool_parsing():
    assert proxy_pool.proxy_urls("") == []
    assert proxy_pool.proxy_urls(None) == []
    assert proxy_pool.proxy_urls("h:80:u:p") == ["http://u:p@h:80"]
    # ready-made URLs pass through; malformed (wrong field count) is skipped.
    assert proxy_pool.proxy_urls("http://u:p@h:80, nope:1, a:8:u:p") == [
        "http://u:p@h:80", "http://u:p@a:8"]


def _patch_fetch(monkeypatch, statuses, proxies):
    """Script _fetch_once to return `statuses` in order, recording the proxy used
    each call. `proxies` is what proxy_pool.shuffled yields. Returns the call log."""
    calls = []

    def fake_once(url, headers, proxy=None):
        calls.append(proxy)
        return _Resp(statuses[min(len(calls) - 1, len(statuses) - 1)]), url, 1

    monkeypatch.setattr(audit_service, "_fetch_once", fake_once)
    monkeypatch.setattr(audit_service.time, "sleep", lambda *_: None)
    monkeypatch.setattr(audit_service.proxy_pool, "shuffled", lambda raw: list(proxies))
    return calls


def test_fetch_first_200_never_touches_proxy(monkeypatch):
    """A page that isn't blocked is fetched DIRECT — no proxy bandwidth spent."""
    calls = _patch_fetch(monkeypatch, [200], ["http://u:p@a:1", "http://u:p@b:2"])
    r, _url, _ms = audit_service._fetch("https://ex.com/")
    assert r.status_code == 200
    assert calls == [None]  # only the direct attempt happened


def test_fetch_retries_through_proxy_after_block(monkeypatch):
    """On a block the FIRST attempt is direct, then retries rotate exit IPs via
    the proxy pool, and the recovered 200 is returned."""
    calls = _patch_fetch(monkeypatch, [503, 200], ["http://u:p@a:1", "http://u:p@b:2"])
    r, _url, _ms = audit_service._fetch("https://ex.com/")
    assert r.status_code == 200
    assert calls[0] is None                  # first attempt DIRECT
    assert calls[1] == "http://u:p@a:1"       # first retry via a proxy exit IP


def test_fetch_rotates_to_a_fresh_exit_ip_each_retry(monkeypatch):
    """Every blocked retry uses a different proxy endpoint (fresh exit IP)."""
    calls = _patch_fetch(monkeypatch, [503, 503, 503, 200],
                         ["http://u:p@a:1", "http://u:p@b:2", "http://u:p@c:3"])
    r, _url, _ms = audit_service._fetch("https://ex.com/")
    assert r.status_code == 200
    assert calls[0] is None
    assert calls[1:4] == ["http://u:p@a:1", "http://u:p@b:2", "http://u:p@c:3"]


def test_fetch_without_proxies_is_all_direct(monkeypatch):
    """No proxies configured → unchanged behaviour: direct + UA-rotating retries,
    all with proxy=None."""
    calls = _patch_fetch(monkeypatch, [503], proxies=[])
    r, _url, _ms = audit_service._fetch("https://ex.com/")
    assert r.status_code == 503
    assert calls == [None, None, None]  # 1 + FETCH_RETRIES attempts, all direct


def test_fetch_falls_back_to_proxy_on_server_error(monkeypatch):
    """A 5xx (site errored) is a failure the proxy fallback also covers."""
    calls = _patch_fetch(monkeypatch, [500, 200], ["http://u:p@a:1"])
    r, _url, _ms = audit_service._fetch("https://ex.com/")
    assert r.status_code == 200
    assert calls[0] is None                 # direct first
    assert calls[1] == "http://u:p@a:1"      # fell back to proxy on the 5xx


def test_fetch_404_is_not_retried_via_proxy(monkeypatch):
    """A definite 404 is a real result, not a block — the proxy is NOT wasted."""
    calls = _patch_fetch(monkeypatch, [404], ["http://u:p@a:1"])
    r, _url, _ms = audit_service._fetch("https://ex.com/")
    assert r.status_code == 404
    assert calls == [None]  # only the direct attempt


def test_fetch_500_without_proxies_returned_directly(monkeypatch):
    """With no proxy pool, a 5xx is returned as-is (original behaviour — only a
    WAF block triggers the direct UA-rotating retry)."""
    calls = _patch_fetch(monkeypatch, [500], proxies=[])
    r, _url, _ms = audit_service._fetch("https://ex.com/")
    assert r.status_code == 500
    assert calls == [None]


def test_fetch_connection_error_falls_back_to_proxy(monkeypatch):
    """'Page didn't come' (connection error) on the direct hit falls back to the
    proxy as the second step."""
    calls = []

    def fake_once(url, headers, proxy=None):
        calls.append(proxy)
        if proxy is None:
            raise audit_service.FetchError("direct did not come")
        return _Resp(200), url, 1

    monkeypatch.setattr(audit_service, "_fetch_once", fake_once)
    monkeypatch.setattr(audit_service.time, "sleep", lambda *_: None)
    monkeypatch.setattr(audit_service.proxy_pool, "shuffled", lambda raw: ["http://u:p@a:1"])
    r, _url, _ms = audit_service._fetch("https://ex.com/")
    assert r.status_code == 200
    assert calls[0] is None                 # direct attempt (errored)
    assert calls[1] == "http://u:p@a:1"      # fell back to proxy
