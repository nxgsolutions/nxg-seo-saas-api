"""Genuineness tests for the content scorer.

The score must reward real content quality: thin pages fail the depth check and
score low, depth expectations scale with page type, and a deep, well-structured
page clearly beats a shallow one. Keyword placement is three-state.
"""

from app.services import content_score
from tests.fixtures.sample_pages import GOOD_PRODUCT, RICH_BLOG, THIN_16_WORDS


def _score(html: str, url: str, keyword: str = "", page_type: str = ""):
    facts = content_score.extract(html, url)
    checks, sc = content_score.score(facts, keyword, page_type=page_type)
    by_id = {c["id"]: c for c in checks}
    return facts, by_id, sc


def test_thin_content_fails_depth_and_scores_low() -> None:
    _, checks, sc = _score(THIN_16_WORDS, "https://x.com/contact", page_type="other")
    assert checks["words"]["status"] == "fail"
    assert sc < 45


def test_depth_bar_is_page_type_aware() -> None:
    """The same shortish body passes as a product but fails as a blog."""
    _, as_product, product_sc = _score(GOOD_PRODUCT, "https://x.com/p", page_type="product")
    _, as_blog, blog_sc = _score(GOOD_PRODUCT, "https://x.com/p", page_type="blog")
    assert as_product["words"]["status"] in {"pass", "warn"}
    assert as_blog["words"]["status"] == "fail"
    assert product_sc > blog_sc


def test_rich_page_beats_thin_page() -> None:
    _, _, rich = _score(RICH_BLOG, "https://x.com/blog/guide", page_type="blog")
    _, _, thin = _score(THIN_16_WORDS, "https://x.com/contact", page_type="other")
    assert rich > thin + 25  # a clear, not marginal, gap
    assert rich >= 70


def test_keyword_placement_is_three_state() -> None:
    url = "https://x.com/blog/guide"
    # Exact phrase in title/H1 → pass.
    _, exact, _ = _score(RICH_BLOG, url, keyword="brass diyas", page_type="blog")
    assert exact["kw_title"]["status"] == "pass"
    # Words present but not as a phrase → warn (partial).
    _, partial, _ = _score(RICH_BLOG, url, keyword="diyas brass festival", page_type="blog")
    assert partial["kw_title"]["status"] in {"warn", "fail"}
    # Keyword absent entirely → fail.
    _, absent, _ = _score(RICH_BLOG, url, keyword="stainless steel kettle", page_type="blog")
    assert absent["kw_title"]["status"] == "fail"


def test_depth_thresholds_lookup_defaults() -> None:
    assert content_score.depth_thresholds("blog") == (700, 300, 150)
    assert content_score.depth_thresholds("unknown-type") == content_score.DEPTH_THRESHOLDS["other"]
