"""Niche (topic_terms) filtering of SERP-derived competitor suggestions.

On top of the category scope, a domain with topic_terms set drops competitor
hosts whose only SERP keywords are off-topic (a topic_regex miss) — e.g. an
ambiguous show-title keyword that pulls streaming sites into an exam-prep
domain. A domain with no topic_terms (topic_regex is None) keeps every host,
matching how domain_settings describes the empty case.
"""

import uuid

from app.models.category import Category
from app.models.domain import Domain
from app.models.keyword_analysis import SerpSnapshot, SerpSnapshotItem
from app.models.user import User
from app.services import keyword_analysis_service as kas
from app.services import workspace_service


def _snap(db, kw, host, pos):
    db.add(SerpSnapshot(
        keyword_id=kw.id, search_engine="bing", source="test",
        country_code="zz", language_code="zz", items_count=1,
        items=[SerpSnapshotItem(type="organic", rank_absolute=pos,
                                url=f"https://{host}/p", domain=host)],
    ))


def _setup(db, *, topic_terms):
    """A domain in one category with two scoped keywords:

      - an on-topic keyword ("...upsc syllabus...") a real coaching rival ranks for;
      - an off-topic keyword ("...aspirants web series...", the ambiguous show
        title) that streaming sites rank for.

    The coaching rival also appears for the off-topic keyword, so the test proves
    a host is kept when ANY of its keywords is on-topic, while a host seen only on
    off-topic keywords is dropped.
    """
    user = User(email=f"topic-{uuid.uuid4().hex[:8]}@example.com", password_hash="x",
                first_name="Topic", last_name="Test")
    db.add(user)
    db.flush()
    ws = workspace_service.create_workspace(db, user, "Topic filter test")

    edu = Category(name=f"Edu-{uuid.uuid4().hex[:6]}", slug=f"edu-{uuid.uuid4().hex[:6]}")
    db.add(edu)
    db.flush()

    dom = Domain(workspace_id=ws.id, name="edu", domain="myedu.com",
                 category_id=edu.id, topic_terms=topic_terms)
    db.add(dom)
    db.flush()

    on_kw = kas._upsert_keyword(db, "zztopic upsc syllabus guide")
    off_kw = kas._upsert_keyword(db, "zztopic aspirants web series cast")
    db.flush()

    _snap(db, on_kw, "goodcoach.com", 2)        # on-topic -> coaching rival
    _snap(db, off_kw, "bingewatch.tv", 1)       # off-topic -> streaming only
    _snap(db, off_kw, "goodcoach.com", 9)        # coaching rival also on off-topic SERP

    kas._upsert_domain_keyword(db, dom, on_kw, "zz", "zz")
    kas._upsert_domain_keyword(db, dom, off_kw, "zz", "zz")
    db.flush()
    return dom


def test_topic_terms_drop_off_topic_streaming_noise(db_session):
    dom = _setup(db_session, topic_terms="upsc syllabus, civil services")

    out = kas.competitor_domains(db_session, dom, country="zz", language="zz")
    hosts = {r["competitor"] for r in out["rows"]}

    # Streaming site seen only on the off-topic keyword is dropped.
    assert "bingewatch.tv" not in hosts
    # Coaching rival survives via the on-topic keyword...
    assert "goodcoach.com" in hosts
    # ...but only its on-topic keyword counts after filtering.
    row = next(r for r in out["rows"] if r["competitor"] == "goodcoach.com")
    assert row["keywords"] == ["zztopic upsc syllabus guide"]


def test_empty_topic_terms_keeps_every_host(db_session):
    dom = _setup(db_session, topic_terms=None)  # topic_regex is None -> no filtering

    out = kas.competitor_domains(db_session, dom, country="zz", language="zz")
    hosts = {r["competitor"] for r in out["rows"]}

    # Both hosts visible when no niche is configured (fallback behavior).
    assert {"goodcoach.com", "bingewatch.tv"} <= hosts
