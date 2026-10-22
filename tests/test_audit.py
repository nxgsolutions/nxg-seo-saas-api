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
    # Missing title, description, h1, viewport, not https, slow.
    assert {"title", "description", "h1", "viewport", "https", "speed"} <= failed
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


def test_speed_check_uses_real_field_cwv() -> None:
    result = analyze_html(GOOD_HTML, "https://example.com/", 200, 9000, len(GOOD_HTML),
                          psi=_psi(overall="FAST"))
    speed = next(c for c in result.checks if c.id == "speed")
    # Real users say the page is fast, so it passes despite the slow fetch time.
    assert speed.status == "pass"
    assert "Core Web Vitals" in speed.detail and "LCP 2.1 s" in speed.detail
    assert result.metrics["speed_source"] == "psi"
    assert result.metrics["core_web_vitals"]["field"]["overall"] == "FAST"


def test_speed_check_poor_field_fails() -> None:
    result = analyze_html(GOOD_HTML, "https://example.com/", 200, 100, len(GOOD_HTML),
                          psi=_psi(overall="SLOW"))
    assert next(c for c in result.checks if c.id == "speed").status == "fail"


def test_speed_check_falls_back_to_lab_score() -> None:
    # No field data → use the lab performance score bucket.
    result = analyze_html(GOOD_HTML, "https://example.com/", 200, 100, len(GOOD_HTML),
                          psi=_psi(score=40))
    speed = next(c for c in result.checks if c.id == "speed")
    assert speed.status == "fail"
    assert "40/100" in speed.detail


def test_speed_check_falls_back_to_fetch_time_without_psi() -> None:
    result = analyze_html(GOOD_HTML, "https://example.com/", 200, 5000, len(GOOD_HTML))
    speed = next(c for c in result.checks if c.id == "speed")
    assert speed.status == "fail"  # 5000 ms fetch
    assert "Fetched in" in speed.detail
    assert result.metrics["speed_source"] == "fetch"
    assert result.metrics["core_web_vitals"] is None


def test_image_detail_limit_is_explicit():
    html = GOOD_HTML + '<img src="x.png" alt="test">' * 205
    result = analyze_html(html, 'https://example.com/', 200, 100, len(html))
    assert len(result.metrics['image_details']) == 200
    assert result.metrics['image_details_truncated'] is True
