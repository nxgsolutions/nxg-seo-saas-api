"""Tests for the response security headers middleware."""

from fastapi.testclient import TestClient


def test_security_headers_present_on_health(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert resp.headers["x-xss-protection"] == "0"
    assert "camera=()" in resp.headers["permissions-policy"]
    # /health is a JSON route, so the strict CSP applies.
    assert "default-src 'none'" in resp.headers["content-security-policy"]


def test_security_headers_present_on_error_response(client: TestClient) -> None:
    # Headers must ride on error responses too (unauthenticated -> 401).
    resp = client.get("/api/auth/me")
    assert resp.status_code == 401
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"


def test_csp_skipped_on_docs_but_other_headers_present(client: TestClient) -> None:
    resp = client.get("/docs")
    assert resp.status_code == 200
    # Swagger UI needs CDN assets + inline scripts, so CSP is intentionally omitted.
    assert "content-security-policy" not in resp.headers
    # The rest still apply.
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_hsts_absent_in_development(client: TestClient) -> None:
    # APP_ENV defaults to development -> no HSTS (it would pin HTTPS on http dev).
    resp = client.get("/health")
    assert "strict-transport-security" not in resp.headers
