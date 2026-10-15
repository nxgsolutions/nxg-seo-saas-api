"""Tests for AI meta-tag suggestions (OpenAI stubbed — no network)."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.services import meta_service, openai_ai

REGISTER = "/api/auth/register"
LOGIN = "/api/auth/login"
PW = "SecurePass123"


def _ai(monkeypatch, reply: dict | None) -> list[str]:
    prompts: list[str] = []

    def fake(system, user, **kw):
        prompts.append(user)
        if reply is None:
            raise openai_ai.OpenAiError("not configured")
        return reply, "gpt-4o-mini"

    monkeypatch.setattr(openai_ai, "write_json", fake)
    return prompts


def test_meta_suggest_topic(monkeypatch) -> None:
    prompts = _ai(monkeypatch, {"title": "Best SEO Audit Tool", "description": "Find and fix SEO issues fast."})
    data = meta_service.suggest("best seo audit tool")
    assert data == {"is_sample": False, "title": "Best SEO Audit Tool", "description": "Find and fix SEO issues fast."}
    assert "Topic: best seo audit tool" in prompts[0]


def test_meta_suggest_reads_the_url(monkeypatch) -> None:
    html = "<html><head><title>Old</title></head><body><h1>Pricing</h1><p>Plans for every team size and budget.</p></body></html>"
    monkeypatch.setattr(meta_service.content_opt_service, "_fetch_html", lambda url: (html, url))
    prompts = _ai(monkeypatch, {"title": "Pricing", "description": "Plans."})
    meta_service.suggest("https://example.com/pricing")
    assert "Existing title: Old" in prompts[0] and "H1: Pricing" in prompts[0]


def test_meta_unavailable_raises(monkeypatch) -> None:
    _ai(monkeypatch, None)
    with pytest.raises(meta_service.MetaUnavailableError):
        meta_service.suggest("seo audit")


def test_meta_endpoint(client: TestClient, monkeypatch) -> None:
    email = f"meta-{uuid4().hex}@example.com"
    client.post(REGISTER, json={"first_name": "M", "last_name": "T", "email": email, "password": PW})
    client.post(LOGIN, json={"email": email, "password": PW})
    _ai(monkeypatch, {"title": "SEO Audit", "description": "Audit your site."})
    resp = client.post("/api/tools/meta-suggest", json={"input": "seo audit"})
    assert resp.status_code == 200 and resp.json()["title"] == "SEO Audit"
    _ai(monkeypatch, None)
    assert client.post("/api/tools/meta-suggest", json={"input": "seo audit"}).status_code == 503


def test_meta_requires_auth(client: TestClient) -> None:
    assert client.post("/api/tools/meta-suggest", json={"input": "seo"}).status_code == 401
