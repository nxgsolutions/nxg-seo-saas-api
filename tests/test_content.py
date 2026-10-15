"""Tests for the keyword / content analysis tool."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.services import content_service
from app.services.content_service import analyze_html, tokenize

SAMPLE = """
<html>
<head>
  <title>Best Running Shoes for Marathon Training</title>
  <meta name="description" content="Find the best running shoes for marathon training and long distance runs.">
</head>
<body>
  <h1>Best Running Shoes</h1>
  <p>Running shoes matter for marathon training. The best running shoes give
  comfort. Marathon runners need durable running shoes for long training runs.
  Training with the right running shoes prevents injury.</p>
  <script>var x = 'ignore this script text running running running';</script>
</body>
</html>
"""


def test_tokenize_lowercases_and_splits() -> None:
    assert tokenize("Hello, World! It's SEO-friendly") == [
        "hello",
        "world",
        "it's",
        "seo-friendly",
    ]


def test_top_unigrams_excludes_stopwords_and_ranks() -> None:
    result = analyze_html(SAMPLE, "https://example.com/")
    words = [u["phrase"] for u in result.unigrams]
    assert "running" in words
    assert "shoes" in words
    # Stopwords excluded.
    assert "the" not in words and "for" not in words
    top = result.unigrams[0]
    assert top["count"] >= 3
    assert top["density"] > 0


def test_bigrams_capture_phrases() -> None:
    result = analyze_html(SAMPLE, "https://example.com/")
    phrases = [b["phrase"] for b in result.bigrams]
    assert "running shoes" in phrases


def test_script_text_ignored() -> None:
    result = analyze_html(SAMPLE, "https://example.com/")
    # "ignore" only appears in <script>, so it must not be counted.
    assert all(u["phrase"] != "ignore" for u in result.unigrams)


def test_target_keyword_presence() -> None:
    result = analyze_html(SAMPLE, "https://example.com/", term="running shoes")
    t = result.target
    assert t is not None
    assert t["in_title"] is True
    assert t["in_h1"] is True
    assert t["in_meta_description"] is True
    assert t["occurrences"] >= 3
    assert t["score"] >= 80


def test_target_absent_keyword_low_score() -> None:
    result = analyze_html(SAMPLE, "https://example.com/", term="blue widgets")
    assert result.target["score"] == 0
    assert result.target["occurrences"] == 0


def _make_result():
    return analyze_html(SAMPLE, "https://example.com/", term="running shoes")


@pytest.fixture
def stub(monkeypatch):
    class _Resp:
        status_code = 200
        content = SAMPLE.encode()
        encoding = "utf-8"

    monkeypatch.setattr(
        content_service.audit_service,
        "safe_fetch",
        lambda url: (_Resp(), "https://example.com/", 5),
    )


def test_keyword_analysis_endpoint(client: TestClient, stub) -> None:
    email = f"kw-{uuid4().hex}@example.com"
    client.post(
        "/api/auth/register",
        json={"first_name": "K", "last_name": "W", "email": email, "password": "SecurePass123"},
    )
    assert client.post("/api/auth/login", json={"email": email, "password": "SecurePass123"}).status_code == 200

    resp = client.post(
        "/api/tools/keyword-analysis",
        json={"url": "example.com", "term": "running shoes"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["word_count"] > 0
    assert any(u["phrase"] == "running" for u in body["unigrams"])
    assert body["target"]["score"] >= 80


def test_keyword_analysis_requires_auth(client: TestClient) -> None:
    assert client.post("/api/tools/keyword-analysis", json={"url": "example.com"}).status_code == 401
