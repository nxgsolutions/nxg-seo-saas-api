"""Category-scoped competitor suggestions.

A domain with a category only sees competitors from keywords in that same
category; a domain with no category keeps the old workspace-wide behavior.
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


def _setup(db):
    user = User(email=f"cat-{uuid.uuid4().hex[:8]}@example.com", password_hash="x",
                first_name="Cat", last_name="Test")
    db.add(user)
    db.flush()
    ws = workspace_service.create_workspace(db, user, "Category scope test")

    edu = Category(name=f"Edu-{uuid.uuid4().hex[:6]}", slug=f"edu-{uuid.uuid4().hex[:6]}")
    tech = Category(name=f"Tech-{uuid.uuid4().hex[:6]}", slug=f"tech-{uuid.uuid4().hex[:6]}")
    db.add_all([edu, tech])
    db.flush()

    edu_dom = Domain(workspace_id=ws.id, name="edu", domain="myedu.com", category_id=edu.id)
    tech_dom = Domain(workspace_id=ws.id, name="tech", domain="mytech.com", category_id=tech.id)
    db.add_all([edu_dom, tech_dom])
    db.flush()

    edu_kw = kas._upsert_keyword(db, "zzcat education keyword one")
    tech_kw = kas._upsert_keyword(db, "zzcat technology keyword one")
    db.flush()

    # SERP results: an education rival for the edu keyword, a tech rival for the
    # tech keyword. Without scoping, both pool into the same locale.
    _snap(db, edu_kw, "edurival.com", 2)
    _snap(db, tech_kw, "techrival.com", 3)

    # Link each keyword to its domain so it inherits the domain's category.
    kas._upsert_domain_keyword(db, edu_dom, edu_kw, "zz", "zz")
    kas._upsert_domain_keyword(db, tech_dom, tech_kw, "zz", "zz")
    db.flush()
    return ws, edu_dom, tech_dom


def test_category_scopes_suggestions(db_session):
    _ws, edu_dom, tech_dom = _setup(db_session)

    edu_out = kas.competitor_domains(db_session, edu_dom, country="zz", language="zz")
    assert [r["competitor"] for r in edu_out["rows"]] == ["edurival.com"]

    tech_out = kas.competitor_domains(db_session, tech_dom, country="zz", language="zz")
    assert [r["competitor"] for r in tech_out["rows"]] == ["techrival.com"]


def test_no_category_falls_back_to_workspace_wide(db_session):
    ws, _edu_dom, _tech_dom = _setup(db_session)

    neutral = Domain(workspace_id=ws.id, name="neutral", domain="neutral.com", category_id=None)
    db_session.add(neutral)
    db_session.flush()

    out = kas.competitor_domains(db_session, neutral, country="zz", language="zz")
    hosts = {r["competitor"] for r in out["rows"]}
    assert {"edurival.com", "techrival.com"} <= hosts  # both visible without a category
