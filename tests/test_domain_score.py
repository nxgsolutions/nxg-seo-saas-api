"""Tests for the domain (whole-site) score aggregation — pure logic, no DB."""

from app.services import domain_score_service as dss
from app.services.crawl_service import Check


def _page(score, page_type="other", categories=None):
    return {"score": score, "page_type": page_type, "categories": categories or {}}


def test_aggregate_empty() -> None:
    out = dss.aggregate([])
    assert out == {"page_quality": 0, "weak_share": 0.0, "category_averages": {}, "scored": 0}


def test_aggregate_weighting_and_weak_penalty() -> None:
    # home (weight 3, score 90) + other (weight 1, score 40 → weak).
    out = dss.aggregate([_page(90, "home"), _page(40, "other")])
    # weighted mean = (3*90 + 1*40) / 4 = 77.5; weak_share = 1/2 = 0.5
    # adjusted = 77.5 * (1 - 0.3*0.5) = 77.5 * 0.85 = 65.875 → 66
    assert out["weak_share"] == 0.5
    assert out["page_quality"] == 66
    assert out["scored"] == 2


def test_aggregate_no_weak_pages_has_no_penalty() -> None:
    out = dss.aggregate([_page(80, "home"), _page(70, "content")])
    # weighted mean = (3*80 + 2*70)/5 = 76; no weak pages → no penalty
    assert out["weak_share"] == 0.0
    assert out["page_quality"] == 76


def test_aggregate_category_averages_skip_nulls() -> None:
    out = dss.aggregate([
        _page(80, "home", {"technical": 100, "content": 60}),
        _page(60, "content", {"technical": 80, "content": None}),
    ])
    assert out["category_averages"]["technical"] == 90   # (100+80)/2
    assert out["category_averages"]["content"] == 60      # only the non-null one


def test_site_technical_all_pass_is_100() -> None:
    checks = [
        Check("robots_found", "robots.txt found", "pass", ""),
        Check("not_blocked", "Site crawlable", "pass", ""),
        Check("sitemap_found", "Sitemap found", "pass", ""),
        Check("sitemap_urls", "Sitemap entries", "pass", ""),
    ]
    score, out = dss.site_technical_score(checks, {"total": 10, "indexable": 9}, https_ok=True)
    assert score == 100
    ids = {c["id"] for c in out}
    assert {"https", "coverage"} <= ids  # our own checks are appended


def test_site_technical_penalizes_blocked_and_no_https() -> None:
    checks = [
        Check("robots_found", "robots.txt found", "pass", ""),
        Check("not_blocked", "Site crawlable", "fail", ""),      # weight 3
        Check("sitemap_found", "Sitemap found", "fail", ""),      # weight 2
    ]
    score, _ = dss.site_technical_score(checks, {"total": 10, "indexable": 3}, https_ok=False)
    assert score < 50  # heavy failures (crawl block + no https + poor coverage) tank it


def test_site_technical_no_pages_fails_coverage() -> None:
    _, out = dss.site_technical_score([], {"total": 0, "indexable": 0}, https_ok=True)
    coverage = next(c for c in out if c["id"] == "coverage")
    assert coverage["status"] == "fail"


def test_thin_domain_scores_low_rich_domain_scores_high() -> None:
    """With the genuine (stricter) per-page scores, a thin-page domain must land
    low and a rich-page domain high — the ordering the lenient scores missed."""
    thin = dss.aggregate([_page(30, "content") for _ in range(5)])
    rich = dss.aggregate([_page(85, "content") for _ in range(5)])
    assert thin["page_quality"] < 40
    assert rich["page_quality"] >= 80
    assert rich["page_quality"] > thin["page_quality"]
    assert thin["weak_share"] == 1.0  # every page below the weak threshold


def test_aggregate_handles_category_dict_without_performance() -> None:
    """Category averages work with the performance-less category dict (no KeyError)."""
    out = dss.aggregate([_page(70, "home", {"content": 70, "technical": 80})])
    assert out["scored"] == 1
    assert "performance" not in out["category_averages"]
    assert out["category_averages"]["content"] == 70
