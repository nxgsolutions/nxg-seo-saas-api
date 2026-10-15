"""AI overviews — rule drafts from stored data, AI number guard, cache."""

import datetime
import uuid

import pytest

from app.models.user import User
from app.services import ai_overview_service as ov
from app.services import openai_ai, workspace_service


def _row(kw, vol, kd, intent="informational"):
    return {"keyword": kw, "metric": {"search_volume": vol, "difficulty": kd, "intent": intent}}


def test_keyword_search_draft_picks_demand_and_easy_wins():
    facts, draft = ov.keyword_search({"seed_keyword": "upsc mcq", "country_code": "in", "results": [
        _row("upsc mcq", 900, 60), _row("upsc mcq pdf", 500, 50),
        _row("daily upsc mcq", 120, 20), _row("upsc mcq app", 0, 10, "commercial"),
    ]})
    assert facts["ideas"] == 4 and facts["with_volume"] == 3 and facts["total_volume"] == 1520
    assert "“upsc mcq” (900 searches/mo, difficulty 60)" in draft
    assert "Easier wins: “daily upsc mcq”" in draft
    assert "mostly informational (75%)" in draft


def test_keyword_search_without_volume_says_connect_ads():
    _, draft = ov.keyword_search({"seed_keyword": "x", "results": [_row("x a", None, 30, "unknown")]})
    assert "connect Google Ads Keyword Planner" in draft


def test_action_plan_uses_plan_priority_and_skips_hard_keywords():
    plan = {"counts": {"improve": 1, "gaps": 2, "targets": 0}, "total_gain": 900, "hard_kd": 70,
            "top": [{"type": "gap", "keyword": "upsc", "difficulty": 90, "gain": 99999, "competitor": "d.com",
                     "competitor_position": 1},
                    {"type": "gap", "keyword": "ncert pdf", "difficulty": 20, "gain": 500, "competitor": "d.com",
                     "competitor_position": 4},
                    {"type": "improve", "keyword": "upsc notes", "difficulty": 10, "gain": 400, "position": 8}],
            "improve": [], "gaps": [], "targets": []}
    _, draft = ov.action_plan(plan)
    assert "“ncert pdf”" in draft and "“upsc”" not in draft
    assert "you rank #8" in draft


def test_number_guard_rejects_invented_numbers(monkeypatch):
    facts, draft = {"volume": 170}, "It gets about 170 searches a month and is worth a page."
    monkeypatch.setattr(openai_ai, "write_json",
                        lambda *a, **k: ({"bullets": ["About 170 searches a month.", "Worth a dedicated page."]}, "gpt-4o-mini"))
    assert ov._polish(facts, draft) == ("About 170 searches a month.\nWorth a dedicated page.", "gpt-4o-mini")
    # A number not in the facts/draft is rejected, whichever bullet it's in.
    monkeypatch.setattr(openai_ai, "write_json",
                        lambda *a, **k: ({"bullets": ["About 1,700 searches a month.", "Worth a dedicated page."]}, "gpt-4o-mini"))
    assert ov._polish(facts, draft) is None


def test_polish_returns_none_when_ai_unavailable(monkeypatch):
    def fail(*a, **k):
        raise openai_ai.OpenAiError("not configured")
    monkeypatch.setattr(openai_ai, "write_json", fail)
    assert ov._polish({"v": 1}, "A rule-based draft that is long enough.") is None


@pytest.fixture
def ws(db_session):
    user = User(email=f"ov-{uuid.uuid4().hex[:8]}@example.com", password_hash="x", first_name="O", last_name="V")
    db_session.add(user)
    db_session.flush()
    return workspace_service.create_workspace(db_session, user, "Overview test")


def test_cache_reuses_until_facts_change(db_session, ws, monkeypatch):
    calls = []
    monkeypatch.setattr(ov, "_polish", lambda f, d: calls.append(1) or (f"AI version of: {d}", "gpt-4o-mini"))
    first = ov.get_or_create(db_session, ws, "keyword_search", "k1", {"v": 1}, "Draft one here.")
    assert first["source"] == "ai" and not first["cached"] and first["model"] == "gpt-4o-mini"
    again = ov.get_or_create(db_session, ws, "keyword_search", "k1", {"v": 1}, "Draft one here.")
    assert again["cached"] and len(calls) == 1
    changed = ov.get_or_create(db_session, ws, "keyword_search", "k1", {"v": 2}, "Draft two here.")
    assert not changed["cached"] and changed["summary"] == "AI version of: Draft two here."


def test_falls_back_to_rules_and_retries_ai_later(db_session, ws, monkeypatch):
    monkeypatch.setattr(ov, "_polish", lambda f, d: None)
    r = ov.get_or_create(db_session, ws, "action_plan", "k2", {"v": 1}, "Rule draft text.")
    assert r["source"] == "rules" and r["summary"] == "Rule draft text."
    # A fresh rules fallback is reused (no AI call on every view)…
    monkeypatch.setattr(ov, "_polish", lambda f, d: ("Now polished.", "gpt-4o-mini"))
    assert ov.get_or_create(db_session, ws, "action_plan", "k2", {"v": 1}, "Rule draft text.")["source"] == "rules"
    # …and retried once the retry window has passed.
    monkeypatch.setattr(ov, "RULES_RETRY", datetime.timedelta(0))
    r2 = ov.get_or_create(db_session, ws, "action_plan", "k2", {"v": 1}, "Rule draft text.")
    assert r2["source"] == "ai" and r2["summary"] == "Now polished."
