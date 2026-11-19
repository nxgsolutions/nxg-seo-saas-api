"""Shopify import: normalisation, sanitising, pagination (pure, no DB)."""

from types import SimpleNamespace

from app.services.shopify import connection_service, sync
from app.services.shopify.html import sanitize, word_count

CONN = SimpleNamespace(shop="s.myshopify.com", primary_host="www.example.com")


def test_sanitize_strips_scripts_and_handlers_keeps_content():
    dirty = (
        '<p onclick="steal()">Hi <a href="javascript:alert(1)">x</a></p>'
        '<script>alert(1)</script><iframe src="https://www.youtube.com/embed/abc"></iframe>'
        '<img src="https://cdn.shopify.com/a.jpg" alt="Shoe" onerror="x()"><h2 style="color:red">Spec</h2>'
    )
    clean = sanitize(dirty)
    assert "script" not in clean and "onclick" not in clean and "onerror" not in clean
    assert "javascript:" not in clean
    assert 'src="https://www.youtube.com/embed/abc"' in clean
    assert 'alt="Shoe"' in clean and "<h2" in clean and "color:red" in clean


def test_word_count():
    assert word_count("<p>one two</p><p>three</p>") == 3
    assert word_count("") == 0


def test_normalize_product():
    node = {
        "id": "gid://shopify/Product/1", "handle": "red-shoe", "title": "Red Shoe",
        "descriptionHtml": "<p>Great shoe</p><script>x</script>", "status": "ACTIVE",
        "onlineStoreUrl": "https://www.example.com/products/red-shoe", "updatedAt": "2026-11-01T10:00:00Z",
        "seo": {"title": "Buy Red Shoe", "description": "Comfy"},
        "media": {"nodes": [{"id": "gid://shopify/MediaImage/9", "alt": "", "image": {"url": "https://cdn/x.jpg"}}, {}]},
    }
    item = sync.normalize_product(CONN, node)
    assert item["external_type"] == "product" and item["external_id"] == "gid://shopify/Product/1"
    assert item["url"] == "https://www.example.com/products/red-shoe"
    assert item["published"] is True
    assert item["seo_title"] == "Buy Red Shoe" and item["seo_description"] == "Comfy"
    assert "script" not in item["body_html"]
    assert item["meta"]["images"] == [{"id": "gid://shopify/MediaImage/9", "alt": "", "url": "https://cdn/x.jpg"}]
    assert item["updated_at"].year == 2026


def test_normalize_product_draft_is_unpublished_and_url_derived():
    node = {"id": "gid://shopify/Product/2", "handle": "draft", "title": "D", "status": "DRAFT",
            "onlineStoreUrl": None, "seo": {}, "media": {"nodes": []}}
    item = sync.normalize_product(CONN, node)
    assert item["published"] is False
    assert item["url"] == "https://www.example.com/products/draft"


def test_normalize_page_reads_seo_metafields():
    node = {"id": "gid://shopify/Page/5", "handle": "about-us", "title": "About", "body": "<p>We</p>",
            "isPublished": True, "updatedAt": "2026-10-01T00:00:00Z",
            "titleTag": {"value": "About Example"}, "descriptionTag": None}
    item = sync.normalize_page(CONN, node)
    assert item["url"] == "https://www.example.com/pages/about-us"
    assert item["seo_title"] == "About Example" and item["seo_description"] is None
    assert item["published"] is True


def test_editor_payload_falls_back_to_title():
    item = sync.normalize_page(CONN, {"id": "g", "handle": "h", "title": "Plain", "body": "<p>a b</p>",
                                      "isPublished": False, "titleTag": None, "descriptionTag": None})
    payload = sync.editor_payload(item)
    assert payload["title"] == "Plain" and payload["meta"] == "" and payload["word_count"] == 2
    assert payload["http_status"] is None


def test_paginate_follows_cursor(monkeypatch):
    pages = [
        {"products": {"nodes": [{"id": "1"}, {"id": "2"}], "pageInfo": {"hasNextPage": True, "endCursor": "c1"}}},
        {"products": {"nodes": [{"id": "3"}], "pageInfo": {"hasNextPage": False, "endCursor": None}}},
    ]
    seen = []

    def fake(db, conn, query, variables=None):
        seen.append(variables["after"])
        return pages.pop(0)

    monkeypatch.setattr(connection_service, "graphql", fake)
    nodes = sync._paginate(None, CONN, sync.PRODUCTS_QUERY, "products", 100)
    assert [n["id"] for n in nodes] == ["1", "2", "3"]
    assert seen == [None, "c1"]


def test_paginate_respects_limit(monkeypatch):
    monkeypatch.setattr(
        connection_service, "graphql",
        lambda db, conn, q, v=None: {"pages": {"nodes": [{"id": str(i)} for i in range(5)],
                                               "pageInfo": {"hasNextPage": True, "endCursor": "x"}}},
    )
    assert len(sync._paginate(None, CONN, sync.PAGES_QUERY, "pages", 7)) == 7
