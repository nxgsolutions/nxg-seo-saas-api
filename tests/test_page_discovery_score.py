"""Page Discovery now scores every scanned page genuinely, from the HTML it
already fetched — no extra PageSpeed/GSC calls."""

import types
from unittest.mock import patch

from app.services import audit_service, page_discovery_service
from tests.fixtures.sample_pages import RICH_BLOG, THIN_16_WORDS


def test_score_html_is_genuine_and_orders_pages() -> None:
    rich = page_discovery_service._score_html(
        RICH_BLOG, "https://x.com/blog/guide", 200, len(RICH_BLOG.encode()), "blog")
    thin = page_discovery_service._score_html(
        THIN_16_WORDS, "https://x.com/contact", 200, len(THIN_16_WORDS.encode()), "other")
    assert rich is not None and thin is not None
    assert rich > thin
    assert thin < 80  # thin content keeps a page out of the "Good"/green band


def test_scoring_makes_no_pagespeed_call() -> None:
    import app.services.psi_service as psi_service
    with patch.object(psi_service, "run_psi") as run_psi:
        page_discovery_service._score_html(
            RICH_BLOG, "https://x.com/blog/guide", 200, len(RICH_BLOG.encode()), "blog")
    assert run_psi.call_count == 0


def _fake_response(status: int, html: str):
    return types.SimpleNamespace(
        status_code=status,
        headers={"content-type": "text/html; charset=utf-8"},
        content=html.encode(),
    )


def test_enrich_one_populates_a_genuine_score(monkeypatch) -> None:
    html = RICH_BLOG
    url = "https://x.com/blog/guide"
    monkeypatch.setattr(audit_service, "safe_fetch",
                        lambda loc: (_fake_response(200, html), url, 100))
    out = page_discovery_service._enrich_one({"loc": url})
    assert out["status"] == 200
    assert out["score"] is not None and out["score"] >= 70


def test_enrich_one_thin_page_scores_low(monkeypatch) -> None:
    url = "https://x.com/contact"
    monkeypatch.setattr(audit_service, "safe_fetch",
                        lambda loc: (_fake_response(200, THIN_16_WORDS), url, 100))
    out = page_discovery_service._enrich_one({"loc": url})
    # Thin content is a soft cap: the page is kept out of "Good", not branded "Poor".
    assert out["score"] is not None and out["score"] < 80
