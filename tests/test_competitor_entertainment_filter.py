"""Niche-aware entertainment-host exclusion for competitor suggestions.

Streaming/movie hosts (surfaced by ambiguous keywords like a brand that is also
a show title) are dropped for a normal domain but kept for an entertainment one.
"""

from app.models.domain import Domain
from app.models.keyword_analysis import SerpSnapshot, SerpSnapshotItem
from app.services import keyword_analysis_service as kas


def _snap(db, kw, host, pos):
    db.add(SerpSnapshot(
        keyword_id=kw.id, search_engine="bing", source="test",
        country_code="zz", language_code="zz", items_count=1,
        items=[SerpSnapshotItem(type="organic", rank_absolute=pos,
                                url=f"https://{host}/p", domain=host)],
    ))


def test_entertainment_hosts_dropped_for_non_entertainment_domain(db_session):
    kw = kas._upsert_keyword(db_session, "zzent aspirants show")
    db_session.flush()
    _snap(db_session, kw, "imdb.com", 1)        # streaming -> dropped
    _snap(db_session, kw, "primevideo.com", 2)  # streaming -> dropped
    _snap(db_session, kw, "realrival.com", 3)   # real competitor -> kept
    db_session.flush()

    dom = Domain(domain="mysite.com", name="mysite")  # no entertainment signal
    out = kas.competitor_domains(db_session, dom, country="zz", language="zz")
    assert [r["competitor"] for r in out["rows"]] == ["realrival.com"]


def test_entertainment_hosts_kept_for_entertainment_domain(db_session):
    kw = kas._upsert_keyword(db_session, "zzent top movies twenty")
    db_session.flush()
    _snap(db_session, kw, "imdb.com", 1)
    _snap(db_session, kw, "realrival.com", 2)
    db_session.flush()

    # topic_terms marks the niche as entertainment AND keeps the keyword on-topic.
    dom = Domain(domain="mysite.com", name="mysite", topic_terms="movies, streaming")
    out = kas.competitor_domains(db_session, dom, country="zz", language="zz")
    hosts = {r["competitor"] for r in out["rows"]}
    assert "imdb.com" in hosts and "realrival.com" in hosts
