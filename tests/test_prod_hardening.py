"""Production-forced safety overrides: Secure cookies, debug off, docs off."""

from uuid import uuid4

from fastapi.testclient import TestClient

from app.core.config import settings

LOGIN_URL = "/api/auth/login"
REGISTER_URL = "/api/auth/register"


# ── Property logic ───────────────────────────────────────────────────────────

def test_effective_flags_follow_raw_settings_in_development(monkeypatch) -> None:
    monkeypatch.setattr(settings, "APP_ENV", "development")
    monkeypatch.setattr(settings, "COOKIE_SECURE", False)
    monkeypatch.setattr(settings, "HTTP_DEBUG", True)
    monkeypatch.setattr(settings, "AI_DEBUG", False)
    assert settings.effective_cookie_secure is False
    assert settings.effective_http_debug is True
    assert settings.effective_ai_debug is False
    assert settings.docs_enabled is True


def test_production_forces_safe_values_regardless_of_raw_settings(monkeypatch) -> None:
    # Even with the insecure/noisy raw values, production locks them down.
    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.setattr(settings, "COOKIE_SECURE", False)
    monkeypatch.setattr(settings, "HTTP_DEBUG", True)
    monkeypatch.setattr(settings, "AI_DEBUG", True)
    assert settings.effective_cookie_secure is True
    assert settings.effective_http_debug is False
    assert settings.effective_ai_debug is False
    assert settings.docs_enabled is False


# ── Behaviour ────────────────────────────────────────────────────────────────

def test_session_cookie_is_secure_in_production(client: TestClient, monkeypatch) -> None:
    email = f"prod-{uuid4().hex}@example.com"
    assert client.post(
        REGISTER_URL,
        json={"first_name": "P", "last_name": "R", "email": email,
              "password": "SecurePass123"},
    ).status_code == 201

    monkeypatch.setattr(settings, "APP_ENV", "production")
    resp = client.post(LOGIN_URL, json={"email": email, "password": "SecurePass123"})
    assert resp.status_code == 200
    set_cookies = "; ".join(resp.headers.get_list("set-cookie")).lower()
    assert "secure" in set_cookies
