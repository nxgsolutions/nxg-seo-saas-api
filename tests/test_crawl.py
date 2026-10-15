"""Tests for the robots.txt & sitemap checker."""

from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.services import crawl_service
from app.services.crawl_service import parse_robots, parse_sitemap

ROBOTS_URL = "/api/tools/robots-check"


def test_parse_robots_basic() -> None:
    text = """
    User-agent: *
    Disallow: /admin/
    Allow: /admin/public
    Disallow: /tmp/

    Sitemap: https://example.com/sitemap.xml
    """
    info = parse_robots(text)
    assert info["disallow_count"] == 2
    assert info["allow_count"] == 1
    assert info["sitemaps"] == ["https://example.com/sitemap.xml"]
    assert info["blocks_all"] is False
    assert info["restricts_star"] is True


def test_parse_robots_blocks_all() -> None:
    info = parse_robots("User-agent: *\nDisallow: /")
    assert info["blocks_all"] is True


def test_parse_robots_ignores_comments() -> None:
    info = parse_robots("# comment\nUser-agent: *\nDisallow: /x  # inline")
    assert info["disallow_count"] == 1


def test_parse_sitemap_urlset() -> None:
    xml = """<?xml version="1.0"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://example.com/</loc></url>
      <url><loc>https://example.com/about</loc></url>
      <url><loc>https://example.com/pricing</loc></url>
    </urlset>"""
    result = parse_sitemap(xml)
    assert result["valid"] is True
    assert result["type"] == "urlset"
    assert result["url_count"] == 3


def test_parse_sitemap_index() -> None:
    xml = """<?xml version="1.0"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://example.com/sitemap1.xml</loc></sitemap>
      <sitemap><loc>https://example.com/sitemap2.xml</loc></sitemap>
    </sitemapindex>"""
    result = parse_sitemap(xml)
    assert result["type"] == "index"
    assert result["url_count"] == 2


def test_parse_sitemap_invalid() -> None:
    assert parse_sitemap("not xml at all")["valid"] is False
    assert parse_sitemap("")["valid"] is False


def _make_response(status: int, text: str) -> httpx.Response:
    return httpx.Response(status_code=status, text=text)


@pytest.fixture
def stub_fetch(monkeypatch):
    """Serve canned robots.txt + sitemap based on the requested URL."""
    robots = "User-agent: *\nDisallow: /admin/\nSitemap: https://example.com/sitemap.xml"
    sitemap = (
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://example.com/</loc></url>"
        "<url><loc>https://example.com/a</loc></url>"
        "</urlset>"
    )

    def fake_safe_fetch(url: str):
        if url.endswith("robots.txt"):
            return _make_response(200, robots), url, 5
        if "sitemap" in url:
            return _make_response(200, sitemap), url, 5
        return _make_response(404, ""), url, 5

    # crawl_service calls audit_service.safe_fetch through its own reference.
    monkeypatch.setattr(
        crawl_service.audit_service, "safe_fetch", fake_safe_fetch
    )


def test_robots_check_endpoint(client: TestClient, stub_fetch) -> None:
    email = f"crawl-{uuid4().hex}@example.com"
    client.post(
        "/api/auth/register",
        json={"first_name": "C", "last_name": "R", "email": email, "password": "SecurePass123"},
    )
    assert client.post("/api/auth/login", json={"email": email, "password": "SecurePass123"}).status_code == 200

    resp = client.post(ROBOTS_URL, json={"url": "example.com"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["robots"]["found"] is True
    assert body["robots"]["blocks_all"] is False
    assert body["sitemap"]["found"] is True
    assert body["sitemap"]["url_count"] == 2
    ids = {c["id"] for c in body["checks"]}
    assert {"robots_found", "not_blocked", "sitemap_found"} <= ids


def test_robots_check_requires_auth(client: TestClient) -> None:
    assert client.post(ROBOTS_URL, json={"url": "example.com"}).status_code == 401
