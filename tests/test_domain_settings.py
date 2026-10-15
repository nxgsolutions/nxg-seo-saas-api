"""Per-domain settings resolver: the domain → workspace → global fallback."""

import re
from types import SimpleNamespace

from app.services import domain_settings as ds


def _dom(**kw):
    base = dict(country=None, language=None, topic_terms=None, brand_terms=None, is_local=False)
    base.update(kw)
    return SimpleNamespace(**base)


def _ws(**kw):
    base = dict(default_country=None, default_language=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_split_terms_dedupes_and_normalizes():
    assert ds.split_terms("Clinic, Treatment ,  symptoms") == ["clinic", "treatment", "symptoms"]
    assert ds.split_terms("a\nb, a") == ["a", "b"]           # newline + comma, de-duped
    assert ds.split_terms("") == [] and ds.split_terms(None) == []


def test_compile_topic_regex_matches_terms_and_escapes():
    assert ds.compile_topic_regex([]) is None
    rx = ds.compile_topic_regex(["treatment", "c++"])
    assert re.search(rx, "diabetes treatment options", re.I)
    assert not re.search(rx, "celebrity news today", re.I)
    assert re.search(rx, "learn c++ fast", re.I)             # special chars escaped


def test_fallback_chain_country_language():
    # domain value wins
    s = ds.resolve(_dom(country="US", language="de"), _ws(default_country="gb", default_language="fr"))
    assert s.country == "us" and s.language == "de"
    # else the workspace default
    s = ds.resolve(_dom(), _ws(default_country="gb", default_language="fr"))
    assert s.country == "gb" and s.language == "fr"
    # else the global default
    assert ds.resolve(_dom(), _ws()).country == "in"
    assert ds.resolve(_dom(), _ws()).language == "en"
    # tolerates a missing workspace entirely
    s = ds.resolve(_dom(), None)
    assert s.country == "in" and s.language == "en"


def test_topic_brand_and_local():
    s = ds.resolve(_dom(topic_terms="Clinic, Treatment", brand_terms="acme, globex", is_local=True), _ws())
    assert s.topic_terms == ["clinic", "treatment"]
    assert s.brand_terms == ["acme", "globex"]
    assert s.is_local is True and s.topic_regex is not None
    # no topic terms => regex is None (callers treat everything as on-topic)
    assert ds.resolve(_dom(), _ws()).topic_regex is None
