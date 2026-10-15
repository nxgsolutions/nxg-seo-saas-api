"""Unit tests for the manual SERP scraper's pure helpers (no network)."""

from app.services.serp_scrape_service import (
    _kl,
    _looks_blocked,
    _proxy_urls,
    _unwrap,
    parse_serp_html,
)


def _ddg(*results: tuple[str, str]) -> str:
    """Build a minimal DuckDuckGo-style results page from (href, title) pairs."""
    return "".join(
        f'<a class="result__a" href="{href}">{title}</a>' for href, title in results
    )


def test_parse_extracts_and_unwraps_ddg_redirect():
    html = _ddg(
        ("//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.drishtiias.com%2Fupsc&rut=x", "UPSC Prep"),
        ("https://iaspirants.com/notes", "IAS Notes"),
    )
    rows = parse_serp_html(html)
    assert [r.domain for r in rows] == ["drishtiias.com", "iaspirants.com"]
    assert [r.position for r in rows] == [1, 2]
    assert rows[0].url == "https://www.drishtiias.com/upsc"
    assert rows[0].title == "UPSC Prep"


def test_parse_skips_ddg_and_dedupes_domains():
    html = _ddg(
        ("https://duckduckgo.com/y.js?ad=1", "Ad"),
        ("https://example.com/a", "First"),
        ("https://example.com/b", "Same domain"),
    )
    rows = parse_serp_html(html)
    assert [r.domain for r in rows] == ["example.com"]


def test_parse_respects_limit():
    html = _ddg(*[(f"https://d{i}.com/x", f"t{i}") for i in range(30)])
    assert len(parse_serp_html(html, limit=10)) == 10


def test_unwrap_handles_plain_and_wrapped():
    assert _unwrap("https://x.com/p") == "https://x.com/p"
    assert _unwrap("//duckduckgo.com/l/?uddg=https%3A%2F%2Fx.com%2Fp") == "https://x.com/p"


def test_kl_region_code_override():
    assert _kl("in", "en") == "in-en"
    assert _kl("gb", "en") == "uk-en"  # DDG uses 'uk', not ISO 'gb'


def test_proxy_urls_parse(monkeypatch):
    monkeypatch.setattr(
        "app.services.serp_scrape_service.settings.SERP_PROXIES",
        "1.2.3.4:8000:user:pass, 5.6.7.8:9000:u2:p2, garbage-entry",
    )
    # Well-formed entries parsed; malformed one skipped.
    assert _proxy_urls() == [
        "http://user:pass@1.2.3.4:8000",
        "http://u2:p2@5.6.7.8:9000",
    ]


def test_proxy_urls_empty(monkeypatch):
    monkeypatch.setattr(
        "app.services.serp_scrape_service.settings.SERP_PROXIES", ""
    )
    assert _proxy_urls() == []


def test_block_detection():
    assert _looks_blocked(200, "If this error persists, please let us know") is True
    assert _looks_blocked(200, "we detected an anomaly in your requests") is True
    assert _looks_blocked(403, "whatever") is True
    assert _looks_blocked(200, "normal organic results here") is False
