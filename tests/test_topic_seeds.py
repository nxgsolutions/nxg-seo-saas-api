"""Topic-term derivation for a site (keyword_analysis_service).

A new domain gets its `topic_terms` auto-derived from its homepage the moment it
is created (workspaces.create_domain → domain_service.autoderive_topics_task →
suggest_topic_terms → _site_topic_seeds). If that derivation keeps promo/price/UI
boilerplate ("best seller sale", "999 00", "start free trial"), every downstream
topic filter (competitor gaps, competitor suggestions, keyword-analysis
relevance) silently mis-fires and the user has to hand-fix the terms.

These tests pin the selection so a new user gets clean, on-topic terms with no
manual correction.
"""

from types import SimpleNamespace

from app.services import keyword_analysis_service as kas
from app.services import domain_service


def _ngram(phrase: str) -> dict:
    return {"phrase": phrase, "count": 1, "density": 0.0}


# ── The pure selector (no crawl) ─────────────────────────────────────────────

def test_keeps_real_nouns_drops_promo_price_and_numbers():
    """Peetal-Kraft-style homepage: product nouns survive; shop/promo/price words
    and anything with a digit are dropped."""
    unigrams = [_ngram(w) for w in
                ["brass", "idol", "diwali", "best", "sale", "999", "piece", "com"]]
    bigrams = [_ngram(p) for p in
               ["antique brass", "lakshmi ganesh", "best seller", "999 00"]]
    trigrams = [_ngram(p) for p in
                ["brass lakshmi ganesh", "off best seller"]]

    out = kas.topic_seeds_from_ngrams(unigrams, bigrams, trigrams)

    # Real topic nouns kept.
    assert {"brass", "idol", "diwali"} <= set(out)
    # Clean multi-word concepts kept.
    assert {"antique brass", "lakshmi ganesh", "brass lakshmi ganesh"} <= set(out)
    # Promo / price / UI / numbers / TLD fragments dropped.
    for junk in ["best", "sale", "999", "piece", "com",
                 "best seller", "999 00", "off best seller"]:
        assert junk not in out


def test_drops_the_sites_own_brand_token():
    unigrams = [_ngram(w) for w in ["peetalkraft", "brass", "idol"]]
    out = kas.topic_seeds_from_ngrams(unigrams, [], [], brand="peetalkraft")
    assert "peetalkraft" not in out
    assert {"brass", "idol"} <= set(out)


def test_single_nouns_come_before_phrases_and_list_is_capped_deduped():
    unigrams = [_ngram(w) for w in ["alpha", "beta", "alpha"]]  # dup collapses
    bigrams = [_ngram("alpha beta")]
    out = kas.topic_seeds_from_ngrams(unigrams, bigrams, [])
    assert out[0] == "alpha" and out[1] == "beta"      # unigrams first, order kept
    assert out.count("alpha") == 1                      # deduped
    assert "alpha beta" in out

    # Cap at 15 (use alphabetic-only words — digits are filtered by design).
    many = [_ngram(f"noun{chr(97 + i // 26)}{chr(97 + i % 26)}") for i in range(40)]
    assert len(kas.topic_seeds_from_ngrams(many, [], [])) == 15


def test_empty_input_yields_no_seeds():
    assert kas.topic_seeds_from_ngrams([], [], []) == []
    assert kas.topic_seeds_from_ngrams(None, None, None) == []


def test_meaningful_seed_word_rules():
    assert kas._meaningful_seed_word("brass")
    assert not kas._meaningful_seed_word("ab")          # too short
    assert not kas._meaningful_seed_word("999")         # not alphabetic
    assert not kas._meaningful_seed_word("sale")        # stopword


# ── The crawl path (content_service monkeypatched — no network) ──────────────

def test_site_topic_seeds_uses_host_as_brand_and_is_clean(monkeypatch):
    """End-to-end of the derivation a new domain runs: a junky homepage still
    yields clean, brand-free topic terms."""
    fake = SimpleNamespace(
        word_count=100,
        unigrams=[_ngram(w) for w in
                  ["peetalkraft", "brass", "idol", "sale", "999"]],
        bigrams=[_ngram(p) for p in ["best seller", "antique brass"]],
        trigrams=[_ngram("brass lakshmi ganesh")],
    )
    monkeypatch.setattr(kas.content_service, "run_analysis", lambda url: fake)

    out = kas._site_topic_seeds("peetalkraft.com")
    assert {"brass", "idol", "antique brass", "brass lakshmi ganesh"} <= set(out)
    assert "peetalkraft" not in out            # brand from the host is dropped
    assert "sale" not in out and "999" not in out and "best seller" not in out


def test_site_topic_seeds_never_raises_on_crawl_failure(monkeypatch):
    def boom(url):
        raise RuntimeError("crawl failed")

    monkeypatch.setattr(kas.content_service, "run_analysis", boom)
    assert kas._site_topic_seeds("unreachable.example") == []


def test_suggest_topic_terms_swallows_errors(monkeypatch):
    """domain_service.suggest_topic_terms is the new-domain entry point; it must
    never raise (a crawl failure just means no auto terms)."""
    monkeypatch.setattr(kas.content_service, "run_analysis",
                        lambda url: (_ for _ in ()).throw(RuntimeError("x")))
    assert domain_service.suggest_topic_terms("unreachable.example") == []
