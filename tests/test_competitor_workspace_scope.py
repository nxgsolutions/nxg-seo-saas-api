"""Competitor suggestions are scoped to the domain's own workspace.

SERP snapshots are keyed to global keywords, not to a workspace, so without
scoping a domain in an empty workspace would inherit another workspace's SERP
competitors. Suggestions must only surface sites ranking for keywords THIS
workspace targets (via its domains' domain_keywords).
"""

import uuid

from app.models.domain import Domain
from app.models.keyword_analysis import KeywordResearch, SerpSnapshot, SerpSnapshotItem
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


def _ws(db, label):
    user = User(email=f"{label}-{uuid.uuid4().hex[:8]}@example.com", password_hash="x",
                first_name=label, last_name="Test")
    db.add(user)
    db.flush()
    return workspace_service.create_workspace(db, user, f"{label} ws")


def test_suggestions_do_not_leak_across_workspaces(db_session):
    # Workspace A owns a keyword with a SERP competitor.
    ws_a = _ws(db_session, "owner")
    dom_a = Domain(workspace_id=ws_a.id, name="a", domain="aowner.com")
    db_session.add(dom_a)
    db_session.flush()
    kw = kas._upsert_keyword(db_session, "zzws scoped keyword one")
    db_session.flush()
    _snap(db_session, kw, "rivalfroma.com", 2)
    kas._upsert_domain_keyword(db_session, dom_a, kw, "zz", "zz")
    db_session.flush()

    # Workspace A sees its competitor.
    out_a = kas.competitor_domains(db_session, dom_a, country="zz", language="zz")
    assert [r["competitor"] for r in out_a["rows"]] == ["rivalfroma.com"]

    # Workspace B is empty (no targeted keywords) — it must NOT inherit A's data,
    # even though the SERP snapshot shares the same locale.
    ws_b = _ws(db_session, "newbiz")
    dom_b = Domain(workspace_id=ws_b.id, name="b", domain="bnewbiz.com")
    db_session.add(dom_b)
    db_session.flush()
    out_b = kas.competitor_domains(db_session, dom_b, country="zz", language="zz")
    assert out_b["rows"] == []


def test_keyword_owned_via_research_surfaces(db_session):
    """A keyword the workspace only RESEARCHED (no domain_keywords row) still
    surfaces its SERP competitors — domain_keywords is too narrow a scope."""
    ws = _ws(db_session, "researcher")
    dom = Domain(workspace_id=ws.id, name="r", domain="researchco.com")
    db_session.add(dom)
    db_session.flush()

    kw = kas._upsert_keyword(db_session, "zzrs researched only keyword")
    db_session.flush()
    _snap(db_session, kw, "rivalfromresearch.com", 3)
    # Owned via a research run's seed — NOT via domain_keywords.
    db_session.add(KeywordResearch(
        workspace_id=ws.id, seed_keyword_id=kw.id,
        country_code="zz", language_code="zz", status="completed",
    ))
    db_session.flush()

    out = kas.competitor_domains(db_session, dom, country="zz", language="zz")
    assert [r["competitor"] for r in out["rows"]] == ["rivalfromresearch.com"]
