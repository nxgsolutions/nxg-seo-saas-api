"""Shopify publish: change detection + mutation inputs (pure, no DB)."""

from datetime import datetime, timezone

from app.services.shopify import publish
from app.services.shopify.publish import PublishInput

LIVE_PRODUCT = {
    "external_type": "product", "external_id": "gid://shopify/Product/1", "handle": "red-shoe",
    "title": "Red Shoe", "body_html": "<p>Great shoe</p>", "seo_title": "Buy Red Shoe",
    "seo_description": None, "published": True, "url": "https://example.com/products/red-shoe",
    "updated_at": datetime(2026, 11, 1, tzinfo=timezone.utc),
    "meta": {"images": [{"id": "gid://shopify/MediaImage/9", "alt": "", "url": "https://cdn/x.jpg"}]},
}
LIVE_PAGE = {**LIVE_PRODUCT, "external_type": "page", "external_id": "gid://shopify/Page/5",
             "handle": "about", "meta": {}}


def test_changes_only_include_edited_fields():
    data = PublishInput(title="Red Shoe", body_html="<p>Great shoe</p>", seo_title="Buy Red Shoe",
                        seo_description="Comfy running shoe", handle="red-shoe")
    assert publish.changes_for(LIVE_PRODUCT, data) == {"seo_description": "Comfy running shoe"}


def test_changes_never_blank_title_or_handle_and_normalise():
    data = PublishInput(title="  ", handle="", seo_title="  New\n  title ", body_html=None)
    assert publish.changes_for(LIVE_PRODUCT, data) == {"seo_title": "New title"}


def test_changes_sanitise_body_and_clean_handle():
    data = PublishInput(body_html="<p>New</p><script>x()</script>", handle="Red Shoe / 2026!")
    out = publish.changes_for(LIVE_PRODUCT, data)
    assert out["body_html"] == "<p>New</p>"
    assert out["handle"] == "red-shoe-2026"


def test_clearing_seo_title_is_a_change():
    assert publish.changes_for(LIVE_PRODUCT, PublishInput(seo_title="")) == {"seo_title": ""}


def test_alt_changes_only_known_media():
    alts = {"gid://shopify/MediaImage/9": "Red running shoe", "gid://shopify/MediaImage/404": "x"}
    assert publish.alt_changes(LIVE_PRODUCT, alts) == {"gid://shopify/MediaImage/9": "Red running shoe"}
    assert publish.alt_changes(LIVE_PRODUCT, {"gid://shopify/MediaImage/9": ""}) == {}


def test_product_input_uses_seo_and_redirect_on_handle():
    payload = publish.build_product_input("gid://shopify/Product/1", {
        "seo_title": "T", "seo_description": "D", "handle": "new-shoe", "body_html": "<p>x</p>"})
    assert payload == {
        "id": "gid://shopify/Product/1", "descriptionHtml": "<p>x</p>", "handle": "new-shoe",
        "redirectNewHandle": True, "seo": {"title": "T", "description": "D"},
    }


def test_page_input_uses_metafields_and_deletes_cleared():
    page_input, deletes = publish.build_page_input({"seo_title": "About Us", "seo_description": "", "body_html": "<p>b</p>"})
    assert page_input["body"] == "<p>b</p>"
    assert page_input["metafields"] == [
        {"namespace": "global", "key": "title_tag", "type": "single_line_text_field", "value": "About Us"}]
    assert deletes == [{"namespace": "global", "key": "description_tag"}]


def test_apply_page_handle_change_creates_redirect(monkeypatch):
    calls = []

    def fake_graphql(db, conn, query, variables=None):
        calls.append((query.split("(")[0].split()[-1], variables))
        if "pageUpdate" in query:
            return {"pageUpdate": {"page": {"id": "p"}, "userErrors": []}}
        if "urlRedirectCreate" in query:
            return {"urlRedirectCreate": {"urlRedirect": {"id": "r"}, "userErrors": []}}
        return {}

    monkeypatch.setattr(publish.connection_service, "graphql", fake_graphql)
    redirect, warnings = publish._apply(None, None, LIVE_PAGE, {"handle": "about-us"}, {})
    assert redirect is True and warnings == []
    redirect_vars = next(v for name, v in calls if name == "RedirectCreate")
    assert redirect_vars == {"urlRedirect": {"path": "/pages/about", "target": "/pages/about-us"}}


def test_apply_alt_failure_is_a_warning_not_an_error(monkeypatch):
    def fake_graphql(db, conn, query, variables=None):
        if "fileUpdate" in query:
            return {"fileUpdate": {"files": [], "userErrors": [{"field": ["files"], "message": "Access denied"}]}}
        return {"productUpdate": {"product": {"id": "x"}, "userErrors": []}}

    monkeypatch.setattr(publish.connection_service, "graphql", fake_graphql)
    redirect, warnings = publish._apply(None, None, LIVE_PRODUCT, {"seo_title": "T"},
                                        {"gid://shopify/MediaImage/9": "Alt"})
    assert redirect is False
    assert warnings and "alt text" in warnings[0]


def test_editor_fields_shape():
    f = publish.editor_fields(LIVE_PRODUCT)
    assert f["seo_description"] == "" and f["images"][0]["id"] == "gid://shopify/MediaImage/9"
