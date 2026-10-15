"""Tests for the PageSpeed Insights integration (parsing + endpoint wiring)."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.services import psi_service
from app.services.psi_service import parse_psi

SAMPLE_PSI = {
    "id": "https://example.com/",
    "lighthouseResult": {
        "categories": {"performance": {"score": 0.92}},
        "audits": {
            "largest-contentful-paint": {"displayValue": "1.2 s", "numericValue": 1200, "score": 0.95},
            "cumulative-layout-shift": {"displayValue": "0.02", "numericValue": 0.02, "score": 1.0},
            "total-blocking-time": {"displayValue": "80 ms", "numericValue": 80, "score": 0.9},
            "first-contentful-paint": {"displayValue": "0.9 s", "numericValue": 900, "score": 0.98},
            "speed-index": {"displayValue": "1.5 s"},
            "interactive": {"displayValue": "1.8 s"},
        },
    },
    "loadingExperience": {
        "overall_category": "FAST",
        "metrics": {
            "LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 1800, "category": "FAST"},
            "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 5, "category": "FAST"},
            "INTERACTION_TO_NEXT_PAINT": {"percentile": 150, "category": "AVERAGE"},
            "FIRST_CONTENTFUL_PAINT_MS": {"percentile": 1200, "category": "FAST"},
        },
    },
}


def test_parse_psi_extracts_score_and_metrics() -> None:
    r = parse_psi(SAMPLE_PSI)
    assert r["performance_score"] == 92
    assert r["lab"]["lcp"]["display"] == "1.2 s"
    assert r["lab"]["cls"]["value"] == 0.02
    assert r["field"]["overall"] == "FAST"
    assert r["field"]["lcp"]["category"] == "FAST"
    assert r["field"]["inp"]["percentile"] == 150


def test_parse_psi_handles_missing_field_data() -> None:
    data = {"lighthouseResult": {"categories": {"performance": {"score": 0.5}}, "audits": {}}}
    r = parse_psi(data)
    assert r["performance_score"] == 50
    assert r["field"] is None
    assert r["lab"]["lcp"]["display"] is None


def test_parse_psi_handles_null_score() -> None:
    r = parse_psi({"lighthouseResult": {"categories": {}, "audits": {}}})
    assert r["performance_score"] is None


@pytest.fixture
def stub_psi(monkeypatch):
    def fake_run(url, strategy="mobile"):
        out = parse_psi(SAMPLE_PSI)
        out["url"] = "https://example.com/"
        out["strategy"] = strategy
        return out

    monkeypatch.setattr(psi_service, "run_psi", fake_run)


def test_pagespeed_endpoint(client: TestClient, stub_psi) -> None:
    email = f"psi-{uuid4().hex}@example.com"
    client.post("/api/auth/register", json={"first_name": "P", "last_name": "S", "email": email, "password": "SecurePass123"})
    assert client.post("/api/auth/login", json={"email": email, "password": "SecurePass123"}).status_code == 200

    resp = client.post("/api/tools/pagespeed", json={"url": "example.com", "strategy": "desktop"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["performance_score"] == 92
    assert body["strategy"] == "desktop"
    assert body["field"]["overall"] == "FAST"


def test_pagespeed_requires_auth(client: TestClient) -> None:
    assert client.post("/api/tools/pagespeed", json={"url": "example.com"}).status_code == 401
