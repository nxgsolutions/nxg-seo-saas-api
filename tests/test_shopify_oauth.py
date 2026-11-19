"""Shopify OAuth, HMAC, crypto, domain-match and GraphQL client — pure logic (no DB)."""

import base64
import hashlib
import hmac
import time
import urllib.parse

import httpx
import pytest
from cryptography.fernet import Fernet

from app.core import crypto
from app.core.config import settings
from app.services.shopify import client, domain_match, oauth, webhooks
from app.services.shopify.domain_match import ShopIdentity
from app.services.shopify.errors import (
    InvalidShopError,
    ShopifyApiError,
    ShopifyAuthError,
    ShopifyUserError,
)

SECRET = "test-shopify-secret"


@pytest.fixture(autouse=True)
def _shopify_settings(monkeypatch):
    monkeypatch.setattr(settings, "SHOPIFY_API_KEY", "test-key")
    monkeypatch.setattr(settings, "SHOPIFY_API_SECRET", SECRET)
    monkeypatch.setattr(settings, "TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(client, "_sleep", lambda s: None)


def _signed(params: dict) -> dict:
    msg = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return {**params, "hmac": hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()}


# --- shop normalisation -----------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("my-store", "my-store.myshopify.com"),
        ("My-Store.myshopify.com", "my-store.myshopify.com"),
        ("https://my-store.myshopify.com/admin", "my-store.myshopify.com"),
        ("https://admin.shopify.com/store/my-store/products", "my-store.myshopify.com"),
    ],
)
def test_normalize_shop_accepts(raw, expected):
    assert oauth.normalize_shop(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "evil.com", "my-store.myshopify.com.evil.com", "-bad.myshopify.com", "a b.myshopify.com", "127.0.0.1", "store.myshopify.com@evil.com"],
)
def test_normalize_shop_rejects(raw):
    with pytest.raises(InvalidShopError):
        oauth.normalize_shop(raw)


@pytest.mark.parametrize(
    "path,expected",
    [("/dashboard/settings", "/dashboard/settings"), ("//evil.com", "/dashboard/settings"),
     ("https://evil.com", "/dashboard/settings"), ("/\\evil.com", "/dashboard/settings"), (None, "/dashboard/settings")],
)
def test_safe_return_path(path, expected):
    assert oauth.safe_return_path(path) == expected


# --- state ------------------------------------------------------------------
def test_state_roundtrip_and_sanitises_return_path():
    token = oauth.sign_state(workspace_id="w", domain_id="d", user_id="u", shop="s.myshopify.com", return_path="//evil")
    payload = oauth.decode_state(token)
    assert payload and payload["wid"] == "w" and payload["did"] == "d" and payload["shop"] == "s.myshopify.com"
    assert payload["ret"] == "/dashboard/settings"


def test_state_rejects_garbage_and_other_types():
    assert oauth.decode_state("nope") is None
    import jwt
    other = jwt.encode({"typ": "gsc_state"}, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    assert oauth.decode_state(other) is None


def test_install_url_has_expected_params():
    url = oauth.build_install_url("s.myshopify.com", "STATE")
    parsed = urllib.parse.urlparse(url)
    q = urllib.parse.parse_qs(parsed.query)
    assert parsed.netloc == "s.myshopify.com" and parsed.path == "/admin/oauth/authorize"
    assert q["client_id"] == ["test-key"] and q["state"] == ["STATE"]
    assert q["redirect_uri"] == [settings.SHOPIFY_REDIRECT_URI]


# --- callback HMAC ----------------------------------------------------------
def test_query_hmac_valid():
    params = _signed({"code": "c", "shop": "s.myshopify.com", "state": "x", "timestamp": str(int(time.time()))})
    assert oauth.verify_query_hmac(params)


def test_query_hmac_tampered():
    params = _signed({"code": "c", "shop": "s.myshopify.com", "state": "x", "timestamp": str(int(time.time()))})
    params["shop"] = "other.myshopify.com"
    assert not oauth.verify_query_hmac(params)


def test_query_hmac_stale_timestamp():
    params = _signed({"code": "c", "shop": "s.myshopify.com", "timestamp": str(int(time.time()) - 3600)})
    assert not oauth.verify_query_hmac(params)


def test_query_hmac_missing():
    assert not oauth.verify_query_hmac({"code": "c", "timestamp": str(int(time.time()))})


# --- webhook HMAC -----------------------------------------------------------
def test_webhook_hmac():
    body = b'{"id": 1}'
    sig = base64.b64encode(hmac.new(SECRET.encode(), body, hashlib.sha256).digest()).decode()
    assert webhooks.verify_hmac(body, sig)
    assert not webhooks.verify_hmac(body + b" ", sig)
    assert not webhooks.verify_hmac(body, None)


# --- crypto -----------------------------------------------------------------
def test_crypto_roundtrip_and_ciphertext_differs():
    enc = crypto.encrypt("shpat_secret")
    assert enc != "shpat_secret" and "shpat" not in enc
    assert crypto.decrypt(enc) == "shpat_secret"


def test_crypto_wrong_key(monkeypatch):
    enc = crypto.encrypt("x")
    monkeypatch.setattr(settings, "TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(enc)


def test_crypto_missing_key(monkeypatch):
    monkeypatch.setattr(settings, "TOKEN_ENCRYPTION_KEY", "")
    with pytest.raises(crypto.EncryptionNotConfiguredError):
        crypto.encrypt("x")


# --- domain match -----------------------------------------------------------
def _ident(*hosts, primary=None):
    return ShopIdentity(name="S", myshopify_domain="s.myshopify.com", primary_host=primary, hosts=list(hosts))


@pytest.mark.parametrize(
    "workspace_domain,identity,expected",
    [
        ("example.com", _ident("example.com", "s.myshopify.com", primary="example.com"), True),
        ("example.com", _ident("www.example.com", "s.myshopify.com", primary="www.example.com"), True),
        ("www.example.com", _ident("example.com", primary="example.com"), True),
        ("shop.example.com", _ident("example.com", "shop.example.com", "s.myshopify.com"), True),
        ("s.myshopify.com", _ident("s.myshopify.com"), True),
        ("other.com", _ident("example.com", "s.myshopify.com", primary="example.com"), False),
        ("example.com.evil.com", _ident("example.com"), False),
        ("myshopify.com", _ident("s.myshopify.com"), False),
    ],
)
def test_domain_match(workspace_domain, identity, expected):
    assert domain_match.matches(workspace_domain, identity) is expected


def test_fetch_identity_falls_back_without_domains(monkeypatch):
    calls = []

    def fake(shop, token, query, variables=None, **kw):
        calls.append(query)
        if "domains" in query:
            raise ShopifyApiError("Field 'domains' doesn't exist on type 'Shop'")
        return {"shop": {"name": "S", "myshopifyDomain": "s.myshopify.com", "primaryDomain": {"host": "www.example.com"}}}

    monkeypatch.setattr(client, "graphql", fake)
    ident = domain_match.fetch_identity("s.myshopify.com", "t")
    assert len(calls) == 2
    assert ident.primary_host == "www.example.com"
    assert ident.hosts == ["www.example.com", "s.myshopify.com"]


# --- GraphQL client ---------------------------------------------------------
class _Seq:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, url, **kw):
        self.calls += 1
        return self.responses.pop(0)


def _resp(status, body=None, headers=None):
    return httpx.Response(status, json=body or {}, headers=headers or {}, request=httpx.Request("POST", "https://x"))


def test_graphql_retries_throttle_then_succeeds(monkeypatch):
    throttled = {"errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
                 "extensions": {"cost": {"requestedQueryCost": 100, "throttleStatus": {"currentlyAvailable": 10, "restoreRate": 50}}}}
    seq = _Seq([_resp(200, throttled), _resp(503), _resp(200, {"data": {"shop": {"name": "S"}}})])
    monkeypatch.setattr(httpx, "post", seq)
    assert client.graphql("s.myshopify.com", "t", "{ shop { name } }") == {"shop": {"name": "S"}}
    assert seq.calls == 3


def test_graphql_auth_error(monkeypatch):
    monkeypatch.setattr(httpx, "post", _Seq([_resp(401)]))
    with pytest.raises(ShopifyAuthError):
        client.graphql("s.myshopify.com", "t", "{}")


def test_graphql_rejects_bad_shop():
    with pytest.raises(InvalidShopError):
        client.graphql("evil.com", "t", "{}")


def test_raise_user_errors():
    client.raise_user_errors({"userErrors": []})
    with pytest.raises(ShopifyUserError):
        client.raise_user_errors({"userErrors": [{"field": ["handle"], "message": "Handle taken"}]})
