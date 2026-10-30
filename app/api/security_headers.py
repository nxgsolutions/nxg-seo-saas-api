"""Response security headers (one pure-ASGI middleware for every route).

Adds the standard defensive headers to every HTTP response:

    X-Content-Type-Options: nosniff        — no MIME sniffing
    X-Frame-Options: DENY                   — no framing (clickjacking)
    Referrer-Policy: strict-origin-when-cross-origin
    X-XSS-Protection: 0                     — disable the legacy, buggy auditor
    Permissions-Policy: camera=(), ...      — deny powerful features by default
    Content-Security-Policy: default-src 'none'; ...   — this is a JSON API
    Strict-Transport-Security: ...          — production (HTTPS) only

Pure ASGI (not BaseHTTPMiddleware) to match ApiLoggingMiddleware and to rewrite
the response's header list directly without buffering the body. Gated by
settings.SECURITY_HEADERS_ENABLED — a near-zero-cost pass-through when off.

The strict `default-src 'none'` CSP is correct for JSON responses but would
break the Swagger UI at /docs (it loads CDN assets + inline scripts), so CSP is
omitted for the docs/openapi paths; all the other headers still apply there.
"""

from __future__ import annotations

from app.core.config import settings

# Interactive API docs served HTML + CDN assets — skip the strict CSP for these
# (every other security header is still applied).
_CSP_SKIP_PATHS = {"/docs", "/redoc", "/openapi.json"}

# Static headers applied to every response. HSTS is added separately (prod only).
_BASE_HEADERS: list[tuple[bytes, bytes]] = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"x-xss-protection", b"0"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
]

# Locked-down CSP suitable for a JSON API (no scripts/styles/frames of our own).
_CSP = (
    b"default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)

# Two years, with subdomains + preload — only sent in production (over HTTPS).
_HSTS = b"max-age=63072000; includeSubDomains; preload"


class SecurityHeadersMiddleware:
    """ASGI middleware: attach security headers to every response."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not settings.SECURITY_HEADERS_ENABLED:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        extra = list(_BASE_HEADERS)
        if path not in _CSP_SKIP_PATHS:
            extra.append((b"content-security-policy", _CSP))
        if settings.is_production:
            extra.append((b"strict-transport-security", _HSTS))

        # Names we own: drop any that a handler already set so ours win cleanly.
        owned = {name for name, _ in extra}

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() not in owned
                ]
                headers.extend(extra)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)
