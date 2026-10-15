"""Unit tests for Page Discovery pure helpers (no network, no DB)."""

import gzip

from app.services import crawl_service
from app.services import page_discovery_service as pd

URLSET = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://ex.com/</loc><lastmod>2026-10-01</lastmod></url>
  <url><loc>https://ex.com/blog/post-1</loc></url>
</urlset>"""

INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://ex.com/sitemap-posts.xml</loc></sitemap>
  <sitemap><loc>https://ex.com/sitemap-pages.xml</loc></sitemap>
</sitemapindex>"""


def test_parse_sitemap_urls_urlset():
    out = crawl_service.parse_sitemap_urls(URLSET)
    assert out["type"] == "urlset"
    locs = [e["loc"] for e in out["entries"]]
    assert locs == ["https://ex.com/", "https://ex.com/blog/post-1"]
    assert out["entries"][0]["lastmod"] == "2026-10-01"
    assert out["entries"][1]["lastmod"] is None


def test_parse_sitemap_urls_index():
    out = crawl_service.parse_sitemap_urls(INDEX)
    assert out["type"] == "index"
    assert [e["loc"] for e in out["entries"]] == [
        "https://ex.com/sitemap-posts.xml", "https://ex.com/sitemap-pages.xml"]


def test_parse_sitemap_urls_invalid():
    assert crawl_service.parse_sitemap_urls("not xml")["entries"] == []
    assert crawl_service.parse_sitemap_urls("")["type"] is None


def test_maybe_gunzip_roundtrip():
    raw = gzip.compress(URLSET.encode())
    assert crawl_service._maybe_gunzip("https://ex.com/sitemap.xml.gz", raw) == URLSET.encode()
    # Plain bytes are returned untouched.
    assert crawl_service._maybe_gunzip("https://ex.com/sitemap.xml", b"<xml/>") == b"<xml/>"


def test_classify_page_type():
    assert pd.classify_page_type("https://ex.com/") == "home"
    assert pd.classify_page_type("https://ex.com/tag/upsc") == "tag"
    assert pd.classify_page_type("https://ex.com/category/notes") == "category"
    assert pd.classify_page_type("https://ex.com/blog/how-to-study") == "blog"
    assert pd.classify_page_type("https://ex.com/about-us") == "other"
    assert pd.classify_page_type("https://ex.com/upsc-syllabus") == "content"


def test_collect_from_sitemaps_recurses_index_and_dedupes(monkeypatch):
    posts = """<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://ex.com/blog/a</loc></url>
      <url><loc>https://ex.com/blog/a/</loc></url>
      <url><loc>https://other.com/x</loc></url>
      <url><loc>https://ex.com/img.png</loc></url>
    </urlset>"""
    pages = """<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://ex.com/about</loc></url></urlset>"""
    responses = {
        "https://ex.com/robots.txt": (200, "Sitemap: https://ex.com/sitemap_index.xml"),
        "https://ex.com/sitemap_index.xml": (200, INDEX),
        "https://ex.com/sitemap-posts.xml": (200, posts),
        "https://ex.com/sitemap-pages.xml": (200, pages),
    }
    monkeypatch.setattr(crawl_service, "_fetch_text", lambda u: responses.get(u, (404, "")))

    source, found, truncated = pd.collect_from_sitemaps("https://ex.com", "ex.com")
    locs = {e["loc"] for e in found}
    # trailing-slash dupe collapsed, off-site + asset dropped, index recursed into both children
    assert locs == {"https://ex.com/blog/a", "https://ex.com/about"}
    assert not truncated
    assert source == "https://ex.com/sitemap-posts.xml"


def test_collect_truncates_at_cap(monkeypatch):
    many = "".join(f"<url><loc>https://ex.com/p/{i}</loc></url>" for i in range(pd.MAX_PAGES + 10))
    big = f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{many}</urlset>'
    responses = {
        "https://ex.com/robots.txt": (404, ""),
        "https://ex.com/sitemap.xml": (200, big),
    }
    monkeypatch.setattr(crawl_service, "_fetch_text", lambda u: responses.get(u, (404, "")))
    _source, found, truncated = pd.collect_from_sitemaps("https://ex.com", "ex.com")
    assert len(found) == pd.MAX_PAGES
    assert truncated is True
