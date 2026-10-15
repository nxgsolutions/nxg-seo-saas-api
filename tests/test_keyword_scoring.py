"""Tests for the pure keyword scoring rule engine.

No DB, no network — these run anywhere pytest runs (unlike the full app, which
can't be pip-installed on this Python 3.14 box).
"""

from app.services import keyword_scoring as ks


def test_normalize_collapses_case_and_whitespace():
    assert ks.normalize_keyword("  Best   SEO  Tools ") == "best seo tools"


def test_intent_classification():
    assert ks.classify_intent("buy cheap running shoes") == "transactional"
    assert ks.classify_intent("best seo tools 2026") == "commercial"
    assert ks.classify_intent("how to rank on google") == "informational"
    assert ks.classify_intent("facebook login") == "navigational"
    assert ks.classify_intent("blue widget") == "unknown"


def test_intent_priority_transactional_over_informational():
    # "how much does X cost" carries a transactional cue ("cost") — that wins.
    assert ks.classify_intent("how much does it cost") == "transactional"


def test_relevance_is_bounded_and_higher_for_more_overlap():
    low = ks.relevance_score("dog food", "best seo tools")
    high = ks.relevance_score("seo tools", "best seo tools")
    assert 0.0 <= low <= high <= 100.0
    assert low == 0.0


def test_difficulty_is_none_without_signals():
    assert ks.estimate_difficulty() is None


def test_difficulty_scales_with_competition():
    easy = ks.estimate_difficulty(results_count=5_000, exact_title_matches=0, distinct_domains=1)
    hard = ks.estimate_difficulty(
        results_count=20_000_000, exact_title_matches=10, distinct_domains=10
    )
    assert 0 <= easy < hard <= 100


def test_opportunity_zero_without_demand():
    assert ks.opportunity_score(0, 5.0, 10) == 0.0
    assert ks.opportunity_score(None, 5.0, 10) == 0.0


def test_opportunity_favours_striking_distance():
    # Same demand & difficulty: position 12 (striking distance) beats #1.
    winning = ks.opportunity_score(5_000, 1.5, 10)
    striking = ks.opportunity_score(5_000, 12.0, 10)
    assert striking > winning


def test_opportunity_penalises_difficulty():
    easy = ks.opportunity_score(5_000, 12.0, 10)
    hard = ks.opportunity_score(5_000, 12.0, 90)
    assert easy > hard


def test_confidence_rewards_sources_and_gsc():
    assert ks.confidence_score(1, has_gsc=False) < ks.confidence_score(4, has_gsc=True)
    assert 0.0 <= ks.confidence_score(10, has_gsc=True) <= 100.0
