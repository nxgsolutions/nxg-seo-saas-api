"""Tests for the AI Search (GEO/AEO) analysis feature (AI stubbed — no network)."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.services import ai_service, openai_ai

REGISTER = "/api/auth/register"
LOGIN = "/api/auth/login"
PW = "SecurePass123"

REPLY = {
    "visibility_score": 140,  # out of range → clamped
    "summary": "Moderate potential.",
    "target_queries": [{"query": "best seo audit tool", "opportunity": "high"}],
    "recommendations": [{"title": "Add FAQ", "detail": "Answer common questions.", "priority": "high"}],
    "content_gaps": ["No comparison page"],
    "competitor_notes": ["Competitors use FAQ schema."],
}


def _ai(monkeypatch, reply: dict | None) -> None:
    def fake(*a, **k):
        if reply is None:
            raise openai_ai.OpenAiError("not configured")
        return reply, "gpt-4o-mini"

    monkeypatch.setattr(openai_ai, "write_json", fake)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "")


def test_analysis_from_ai(monkeypatch) -> None:
    _ai(monkeypatch, REPLY)
    data = ai_service.analyze_ai_search("seo audit tool", "example.com")
    assert data["is_sample"] is False and data["model"] == "gpt-4o-mini"
    assert data["visibility_score"] == 100
    assert data["query"] == "seo audit tool" and data["domain"] == "example.com"
    assert data["recommendations"][0]["title"] == "Add FAQ"


def test_no_ai_raises_instead_of_sample(monkeypatch) -> None:
    _ai(monkeypatch, None)
    with pytest.raises(ai_service.AiUnavailableError):
        ai_service.analyze_ai_search("seo audit tool")


def _auth(client: TestClient) -> None:
    email = f"ai-{uuid4().hex}@example.com"
    client.post(REGISTER, json={"first_name": "A", "last_name": "I", "email": email, "password": PW})
    assert client.post(LOGIN, json={"email": email, "password": PW}).status_code == 200


def test_ai_search_endpoint(client: TestClient, monkeypatch) -> None:
    _auth(client)
    _ai(monkeypatch, REPLY)
    resp = client.post("/api/tools/ai-search", json={"query": "keyword research", "domain": "example.com"})
    assert resp.status_code == 200
    assert resp.json()["model"] == "gpt-4o-mini"
    _ai(monkeypatch, None)
    assert client.post("/api/tools/ai-search", json={"query": "keyword research"}).status_code == 503


def test_ai_search_requires_auth(client: TestClient) -> None:
    resp = client.post("/api/tools/ai-search", json={"query": "seo"})
    assert resp.status_code == 401
