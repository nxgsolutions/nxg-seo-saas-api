"""Tests for competitor-domain derivation from serp_snapshots."""

from app.models.domain import Domain
from app.models.keyword_analysis import SerpSnapshot, SerpSnapshotItem
from app.services import keyword_analysis_service as kas


# Use an isolated locale ('zz'/'zz') so the assertions don't pick up the real
# serp_snapshots rows visible through the rollback fixture's shared connection.
def _snap(db, kw, domain, pos):
    db.add(SerpSnapshot(
        keyword_id=kw.id, search_engine="bing", source="test",
        country_code="zz", language_code="zz", items_count=1,
        items=[SerpSnapshotItem(
            type="organic", rank_absolute=pos, url=f"https://{domain}/p",
            domain=domain,
        )],
    ))


def test_competitor_domains_aggregates_and_excludes(db_session):
    kw1 = kas._upsert_keyword(db_session, "zzz competitor test kw one")
    kw2 = kas._upsert_keyword(db_session, "zzz competitor test kw two")
    db_session.flush()

    _snap(db_session, kw1, "www.rivala.com", 2)   # same host, two keywords ->
    _snap(db_session, kw2, "rivala.com", 6)        #   shared=2, avg (2+6)/2=4.0
    _snap(db_session, kw1, "rivalb.com", 3)        # shared=1
    _snap(db_session, kw1, "mysite.com", 1)        # own domain -> excluded
    _snap(db_session, kw1, "www.youtube.com", 4)   # non-competitor -> excluded
    _snap(db_session, kw1, "upsc.gov.in", 5)       # gov -> excluded
    db_session.flush()

    dom = Domain(domain="mysite.com", name="mysite")  # unsaved; only .domain read
    out = kas.competitor_domains(db_session, dom, country="zz", language="zz")

    hosts = [r["competitor"] for r in out["rows"]]
    assert hosts == ["rivala.com", "rivalb.com"]  # own/youtube/gov excluded, ranked

    by_host = {r["competitor"]: r for r in out["rows"]}
    assert by_host["rivala.com"]["keywords_shared"] == 2
    assert by_host["rivala.com"]["avg_position"] == 4.0
    assert by_host["rivala.com"]["best_position"] == 2
    assert by_host["rivalb.com"]["keywords_shared"] == 1


def test_competitor_domains_min_shared_filter(db_session):
    kw = kas._upsert_keyword(db_session, "zzz single keyword only")
    db_session.flush()
    _snap(db_session, kw, "onlyonce.com", 5)
    db_session.flush()

    dom = Domain(domain="mysite.com", name="mysite")
    out = kas.competitor_domains(db_session, dom, country="zz", language="zz", min_shared=2)
    assert out["rows"] == []
