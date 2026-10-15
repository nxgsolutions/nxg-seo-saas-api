"""Tests for refresh-token rotation, reuse detection, and revocation."""

from uuid import uuid4

from fastapi.testclient import TestClient

from app.core.config import settings

REGISTER_URL = "/api/auth/register"
LOGIN_URL = "/api/auth/login"
LOGOUT_URL = "/api/auth/logout"
REFRESH_URL = "/api/auth/refresh"
ME_URL = "/api/auth/me"

PASSWORD = "SecurePass123"
ACCESS = settings.AUTH_COOKIE_NAME
REFRESH = settings.AUTH_REFRESH_COOKIE_NAME


def _login(client: TestClient) -> str:
    email = f"refresh-{uuid4().hex}@example.com"
    client.post(
        REGISTER_URL,
        json={
            "first_name": "Re",
            "last_name": "Fresh",
            "email": email,
            "password": PASSWORD,
        },
    )
    resp = client.post(LOGIN_URL, json={"email": email, "password": PASSWORD})
    assert resp.status_code == 200
    return email


def test_login_sets_both_cookies(client: TestClient) -> None:
    _login(client)
    assert client.cookies.get(ACCESS)
    assert client.cookies.get(REFRESH)


def test_refresh_rotates_tokens(client: TestClient) -> None:
    _login(client)
    old_refresh = client.cookies.get(REFRESH)
    old_access = client.cookies.get(ACCESS)

    resp = client.post(REFRESH_URL)
    assert resp.status_code == 200

    new_refresh = client.cookies.get(REFRESH)
    new_access = client.cookies.get(ACCESS)
    assert new_refresh and new_refresh != old_refresh  # rotated
    assert new_access and new_access != old_access  # new access token

    # New session still works.
    assert client.get(ME_URL).status_code == 200


def test_refresh_without_cookie_returns_401(client: TestClient) -> None:
    resp = client.post(REFRESH_URL)
    assert resp.status_code == 401


def test_refresh_with_garbage_cookie_returns_401(client: TestClient) -> None:
    client.cookies.set(REFRESH, "not-a-real-token")
    resp = client.post(REFRESH_URL)
    assert resp.status_code == 401


def test_reused_refresh_token_is_rejected_and_revokes_family(
    client: TestClient,
) -> None:
    _login(client)
    t1 = client.cookies.get(REFRESH)

    # Rotate: t1 -> t2
    assert client.post(REFRESH_URL).status_code == 200
    t2 = client.cookies.get(REFRESH)
    assert t2 and t2 != t1

    # Replay the OLD token t1 -> reuse detected -> 401.
    client.cookies.clear()
    client.cookies.set(REFRESH, t1)
    assert client.post(REFRESH_URL).status_code == 401

    # Reuse revokes the whole family: t2 is now dead too.
    client.cookies.clear()
    client.cookies.set(REFRESH, t2)
    assert client.post(REFRESH_URL).status_code == 401


def test_logout_revokes_refresh_token(client: TestClient) -> None:
    _login(client)
    refresh_token = client.cookies.get(REFRESH)

    assert client.post(LOGOUT_URL).status_code == 204

    # Even replaying the captured refresh token fails after logout.
    client.cookies.clear()
    client.cookies.set(REFRESH, refresh_token)
    assert client.post(REFRESH_URL).status_code == 401


def test_invalid_refresh_clears_both_session_cookies(client: TestClient) -> None:
    response = client.post(
        REFRESH_URL,
        headers={"Cookie": f"{ACCESS}=expired; {REFRESH}=invalid"},
    )
    assert response.status_code == 401
    cookies = response.headers.get_list("set-cookie")
    for name in (ACCESS, REFRESH):
        assert any(cookie.startswith(f'{name}=') and 'Max-Age=0' in cookie for cookie in cookies)
