"""Tests for rate limiting: the limiter primitive and the auth-route guard."""

from fastapi.testclient import TestClient

from app.api.rate_limit import FixedWindowRateLimiter
from app.core.config import settings

LOGIN_URL = "/api/auth/login"


# ── Limiter primitive ──────────────────────────────────────────────────────

def test_limiter_allows_up_to_limit_then_blocks() -> None:
    limiter = FixedWindowRateLimiter()
    for _ in range(3):
        allowed, retry = limiter.check("k", limit=3, window=60)
        assert allowed and retry == 0
    allowed, retry = limiter.check("k", limit=3, window=60)
    assert not allowed
    assert retry >= 1


def test_limiter_keys_are_independent() -> None:
    limiter = FixedWindowRateLimiter()
    assert limiter.check("a", limit=1, window=60)[0] is True
    assert limiter.check("a", limit=1, window=60)[0] is False
    # A different key has its own fresh window.
    assert limiter.check("b", limit=1, window=60)[0] is True


def test_limiter_zero_limit_means_unlimited() -> None:
    limiter = FixedWindowRateLimiter()
    for _ in range(50):
        assert limiter.check("k", limit=0, window=60)[0] is True


def test_limiter_reset_clears_counts() -> None:
    limiter = FixedWindowRateLimiter()
    limiter.check("k", limit=1, window=60)
    assert limiter.check("k", limit=1, window=60)[0] is False
    limiter.reset()
    assert limiter.check("k", limit=1, window=60)[0] is True


# ── Auth-route guard ─────────────────────────────────────────────────────────

def test_login_is_throttled_after_too_many_attempts(client: TestClient) -> None:
    body = {"email": "nobody@example.com", "password": "whatever123"}
    allowed = settings.RATE_LIMIT_AUTH_MAX  # window reset by autouse fixture
    # The first `allowed` attempts fail auth (401) but are not throttled.
    for _ in range(allowed):
        assert client.post(LOGIN_URL, json=body).status_code == 401
    # The next one trips the limiter.
    blocked = client.post(LOGIN_URL, json=body)
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) >= 1


def test_rate_limit_can_be_disabled(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)
    body = {"email": "nobody2@example.com", "password": "whatever123"}
    # Well past the auth cap — every one should reach auth and return 401.
    for _ in range(settings.RATE_LIMIT_AUTH_MAX + 5):
        assert client.post(LOGIN_URL, json=body).status_code == 401
