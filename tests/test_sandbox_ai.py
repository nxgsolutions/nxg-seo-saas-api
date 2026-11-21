"""Sandbox workspaces use static LLM stand-ins — no model tokens are spent.

Competitor discovery, business-profile extraction, manual-add scoring and
category auto-detect all route to app.services.sandbox_ai for an is_sandbox
workspace. These tests make the real LLM/scrape paths raise if touched, so any
regression that lets a sandbox domain call the model fails loudly.
"""

import uuid

import pytest

from app.models.domain import Domain
from app.models.user import User
from app.services import category_detection_service
from app.services import competitor_discovery_service as disc
from app.services import competitor_service, openai_ai, workspace_service


@pytest.fixture(autouse=True)
def _blow_up_on_model_calls(monkeypatch):
    """Any real LLM/scrape call is a bug in a sandbox workspace."""
    def _boom(*_a, **_k):
        raise AssertionError("sandbox workspace must not call the model/scrape")

    monkeypatch.setattr(disc, "discover", _boom)
    monkeypatch.setattr(disc, "extract_business_profile", _boom)
    monkeypatch.setattr(disc, "verify_single", _boom)
    monkeypatch.setattr(disc, "scrape_context", _boom)
    monkeypatch.setattr(openai_ai, "write_json", _boom)
    monkeypatch.setattr(openai_ai, "chat_json", _boom)


def _sandbox_domain(db, *, business_profile=None):
    user = User(email=f"sbx-{uuid.uuid4().hex[:8]}@example.com", password_hash="x",
                first_name="Sbx", last_name="Test")
    db.add(user)
    db.flush()
    ws = workspace_service.create_workspace(db, user, "sbx ws", is_sandbox=True)
    domain = Domain(workspace_id=ws.id, name="mine", domain="mysite.com",
                    business_profile=business_profile)
    db.add(domain)
    db.flush()
    return domain


def test_discover_uses_static_competitors(db_session):
    domain = _sandbox_domain(db_session)
    result = competitor_service.discover_and_store(db_session, domain)

    assert result["count"] >= 1
    hosts = {r["competitor"] for r in result["competitors"]}
    assert any(h.endswith(".example") for h in hosts)
    # The cached profile is the static one.
    assert result["profile"]["industry"] == "Sandbox Industry"


def test_refresh_business_profile_is_static(db_session):
    domain = _sandbox_domain(db_session)
    out = competitor_service.refresh_business_profile(db_session, domain)
    assert out["profile"]["industry"] == "Sandbox Industry"
    assert domain.business_profile["industry"] == "Sandbox Industry"


def test_manual_add_scored_from_stub(db_session):
    domain = _sandbox_domain(db_session, business_profile={"industry": "x"})
    row = competitor_service.add_competitor(db_session, domain, "rival.com")
    assert row["source"] == "manual"
    assert row["match_score"] == 60
    assert row["confidence"] == "Medium"


def test_detect_category_is_static(db_session):
    domain = _sandbox_domain(db_session)
    category = category_detection_service.detect_category(db_session, domain)
    assert category is not None
    assert category.name == "Sandbox Industry"
