"""Tests for the overall page-score aggregator (pure logic; run_audit stubbed).

The scorer must only *combine* the existing engines, never re-implement them, so
the key assertions reconcile each category against its source and check that the
overall number is the weighted mean of the categories.
"""

import pytest

from app.services import audit_service, content_score, page_score_service
from app.services.page_score_service import CATEGORY_WEIGHTS

GOOD_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Best Running Shoes Reviewed for 2026</title>
  <meta name="description" content="A nicely sized meta description about the best running shoes, comfortably within the recommended fifty to one hundred and sixty characters for search.">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="canonical" href="https://example.com/running-shoes">
  <meta property="og:title" content="Best Running Shoes">
  <meta property="og:image" content="https://example.com/og.png">
  <script type="application/ld+json">{"@context":"https://schema.org","@type":"WebPage"}</script>
</head>
<body>
  <h1>Best Running Shoes</h1>
  <h2>How we tested running shoes</h2>
  <p>%s</p>
  <ul><li>First running shoe pick for daily training and long runs</li></ul>
  <img src="a.png" alt="a running shoe">
  <a href="/internal">internal</a>
  <a href="https://other.com/x">external</a>
</body>
</html>
""" % ("running shoes are great for comfort and speed " * 60)


@pytest.fixture
def stub_audit(monkeypatch):
    """Make run_page_score use GOOD_HTML without any network fetch or PSI."""
    def _fake_run_audit(raw_url, device="desktop", *, with_psi=True):
        result = audit_service.analyze_html(
            html=GOOD_HTML, final_url="https://example.com/running-shoes",
            status_code=200, load_time_ms=400, page_size_bytes=len(GOOD_HTML.encode()),
            device=device,
        )
        result.html = GOOD_HTML
        return result
    monkeypatch.setattr(page_score_service.audit_service, "run_audit", _fake_run_audit)


def test_all_categories_present_and_weights_sum_100(stub_audit) -> None:
    result = page_score_service.run_page_score("https://example.com/running-shoes")
    ids = [c.id for c in result.categories]
    assert ids == list(CATEGORY_WEIGHTS)  # all six, in order
    assert sum(c.weight for c in result.categories) == 100
    assert 0 <= result.overall <= 100


def test_overall_is_weighted_mean_of_categories(stub_audit) -> None:
    result = page_score_service.run_page_score("https://example.com/running-shoes")
    scored = [c for c in result.categories if c.score is not None]
    wsum = sum(c.weight for c in scored)
    expected = round(sum(c.weight * c.score for c in scored) / wsum)
    assert result.overall == expected


def test_content_category_equals_content_engine_score(stub_audit) -> None:
    """Content quality must be the content engine's own number, not a re-score."""
    result = page_score_service.run_page_score(
        "https://example.com/running-shoes", keyword="running shoes"
    )
    facts = content_score.extract(GOOD_HTML, "https://example.com/running-shoes")
    _, expected = content_score.score(facts, "running shoes")
    content_cat = next(c for c in result.categories if c.id == "content")
    assert content_cat.score == expected


def test_audit_checks_map_to_expected_categories(stub_audit) -> None:
    result = page_score_service.run_page_score("https://example.com/running-shoes")
    where = {c.id: cat.id for cat in result.categories for c in cat.checks}
    assert where["https"] == "technical"
    assert where["canonical"] == "technical"
    assert where["title"] == "onpage"
    assert where["h1"] == "onpage"
    assert where["speed"] == "performance"
    assert where["viewport"] == "performance"
    assert where["img_alt"] == "links_images"
    assert where["schema"] == "structured"


def test_content_depth_check_is_not_double_counted(stub_audit) -> None:
    """The audit's 'content' depth check is informational — content quality owns
    depth, so 'content' must not appear as a scored category check."""
    result = page_score_service.run_page_score("https://example.com/running-shoes")
    all_ids = {c.id for cat in result.categories for c in cat.checks}
    assert "content" not in all_ids


def test_keyword_optional(stub_audit) -> None:
    """Without a keyword, no keyword-placement checks; with one, they appear and
    the content category still scores."""
    no_kw = page_score_service.run_page_score("https://example.com/running-shoes")
    nk_ids = {c.id for cat in no_kw.categories for c in cat.checks}
    assert not any(i.startswith("content_kw_") for i in nk_ids)
    assert no_kw.keyword == ""

    with_kw = page_score_service.run_page_score(
        "https://example.com/running-shoes", keyword="running shoes"
    )
    wk_ids = {c.id for cat in with_kw.categories for c in cat.checks}
    assert any(i.startswith("content_kw_") for i in wk_ids)
    assert with_kw.keyword == "running shoes"
    content_cat = next(c for c in with_kw.categories if c.id == "content")
    assert content_cat.score is not None


def test_links_images_includes_content_link_checks(stub_audit) -> None:
    """Links & images folds in the content engine's internal/external link checks."""
    result = page_score_service.run_page_score("https://example.com/running-shoes")
    li = next(c for c in result.categories if c.id == "links_images")
    ids = {c.id for c in li.checks}
    assert "img_alt" in ids
    assert "content_internal_links" in ids
    assert "content_external_links" in ids
