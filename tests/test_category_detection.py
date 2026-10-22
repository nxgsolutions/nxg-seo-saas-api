"""Auto-detection of a domain's industry category from its website.

Covers the get-or-create helper (slug matching + create) and the detection
service (maps the model's label to an existing category or creates a new one,
and stays silent/best-effort on any failure). The LLM call and the homepage
scrape are monkeypatched so the test is deterministic and offline.
"""

import uuid

from app.models.user import User
from app.services import (
    category_detection_service,
    category_service,
    competitor_discovery_service,
    domain_service,
    openai_ai,
    workspace_service,
)


def _domain(db):
    user = User(email=f"det-{uuid.uuid4().hex[:8]}@example.com", password_hash="x",
                first_name="Det", last_name="Test")
    db.add(user)
    db.flush()
    ws = workspace_service.create_workspace(db, user, f"Detect {uuid.uuid4().hex[:6]}")
    dom = domain_service.create_domain(db, ws, "mydetect.com", None)
    return ws, dom


def _patch_ai(monkeypatch, *, corpus: str, result):
    monkeypatch.setattr(openai_ai, "any_available", lambda: True)
    monkeypatch.setattr(competitor_discovery_service, "scrape_context", lambda host: corpus)
    monkeypatch.setattr(openai_ai, "write_json",
                        lambda system, user, **kw: (result, "gpt-4o-mini"))


# ── get_or_create_category ────────────────────────────────────────────────

def test_get_or_create_matches_existing_by_slug(db_session):
    created = category_service.create_category(db_session, "Real Estate")
    # Different casing/spacing collapses to the same slug → same row, no dupe.
    got = category_service.get_or_create_category(db_session, "  real   estate ")
    assert got.id == created.id


def test_get_or_create_creates_when_absent(db_session):
    before = {c.slug for c in category_service.list_categories(db_session)}
    assert "veterinary-care" not in before
    got = category_service.get_or_create_category(db_session, "Veterinary Care")
    assert got.slug == "veterinary-care"
    assert got.name == "Veterinary Care"


# ── detect_category ───────────────────────────────────────────────────────

def test_detect_assigns_existing_category(db_session, monkeypatch):
    _ws, dom = _domain(db_session)
    # "Education" is one of the seeded defaults — reuse it rather than duplicate.
    existing = category_service.get_or_create_category(db_session, "Education")
    _patch_ai(monkeypatch, corpus="TITLE: Learn\nTEXT: online courses and exam prep",
              result={"category": "Education"})
    cat = category_detection_service.detect_category(db_session, dom)
    assert cat is not None and cat.id == existing.id


def test_detect_creates_new_category_when_no_match(db_session, monkeypatch):
    _ws, dom = _domain(db_session)
    _patch_ai(monkeypatch, corpus="TITLE: Law firm\nTEXT: attorneys and legal services",
              result={"category": "Legal Services"})
    cat = category_detection_service.detect_category(db_session, dom)
    assert cat is not None and cat.slug == "legal-services"


def test_detect_returns_none_when_ai_unavailable(db_session, monkeypatch):
    _ws, dom = _domain(db_session)
    monkeypatch.setattr(openai_ai, "any_available", lambda: False)
    assert category_detection_service.detect_category(db_session, dom) is None


def test_detect_returns_none_on_ai_error(db_session, monkeypatch):
    _ws, dom = _domain(db_session)
    monkeypatch.setattr(openai_ai, "any_available", lambda: True)
    monkeypatch.setattr(competitor_discovery_service, "scrape_context",
                        lambda host: "TITLE: anything\nTEXT: content")

    def _boom(system, user, **kw):
        raise openai_ai.OpenAiError("down")

    monkeypatch.setattr(openai_ai, "write_json", _boom)
    assert category_detection_service.detect_category(db_session, dom) is None


def test_detect_returns_none_on_blank_label(db_session, monkeypatch):
    _ws, dom = _domain(db_session)
    _patch_ai(monkeypatch, corpus="TITLE: x\nTEXT: y", result={"category": "   "})
    assert category_detection_service.detect_category(db_session, dom) is None
