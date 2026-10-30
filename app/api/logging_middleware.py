"""Per-request API flow logging (one ASGI middleware for every route).

Logs each request and its response through the "app.api" logger (see
app/core/logging_config.py). An arrival line, then a response block with the
status, duration and pretty-printed (indented) request/response bodies, e.g.

    REQ [a1b2c3d4] POST /api/competitors/discover
    RSP [a1b2c3d4] POST /api/competitors/discover -> 200 (842ms)
      in:
        { "target_domain": "example.com", "limit": 10 }
      out:
        { "market": "India", "count": 10, ... }

Pure ASGI (not BaseHTTPMiddleware) so the request/response bodies can be captured
without consuming the stream: `receive` is wrapped to buffer-and-replay request
chunks (the Stripe webhook's raw body still reaches its signature check), and
`send` is wrapped to capture the status and response body. Gated by
settings.HTTP_DEBUG — a near-zero-cost pass-through when off.
"""

from __future__ import annotations

import json
import logging
import time
import uuid

from app.core.config import settings

logger = logging.getLogger("app.api")

# Paths we never log (health probes / API docs / favicon) — pure noise.
_SKIP_PATHS = {"/", "/health", "/docs", "/redoc", "/openapi.json", "/favicon.ico"}

# Keys whose values are secrets — redacted anywhere they appear in a body.
_SECRET_KEYS = (
    "password", "token", "secret", "api_key", "apikey", "authorization",
    "refresh", "card", "cvv", "client_secret",
)

# Bodies are only decoded/logged for these request/response content types.
_TEXT_CT = ("application/json", "application/x-www-form-urlencoded", "text/")


def _is_secret(key: str) -> bool:
    k = key.lower()
    return any(s in k for s in _SECRET_KEYS)


def _redact(obj):
    """Recursively replace secret-keyed values with '***'."""
    if isinstance(obj, dict):
        return {k: ("***" if _is_secret(str(k)) else _redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def _fmt_body(raw: bytes, content_type: str, limit: int) -> str:
    """Render a body for the log: pretty, redacted JSON when possible, else a
    size note. May return a multi-line string (indented JSON)."""
    if not raw:
        return "-"
    ct = (content_type or "").lower()
    if not any(t in ct for t in _TEXT_CT):
        return f"<{ct or 'binary'}, {len(raw)} bytes>"
    try:
        text = raw.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return f"<{len(raw)} bytes>"
    if "application/json" in ct:
        try:
            text = json.dumps(
                _redact(json.loads(text)), indent=2, ensure_ascii=False, default=str
            )
        except (ValueError, TypeError):
            pass  # not valid JSON after all — log the raw text (truncated)
    else:
        text = text.strip()
    if len(text) > limit:
        text = text[:limit] + f"\n... (+{len(text) - limit} more chars)"
    return text


def _section(label: str, body: str) -> str:
    """One `in:`/`out:` section — inline for short/empty bodies, indented block
    (4 spaces) for multi-line ones so JSON stays readable under the header."""
    if body == "-" or ("\n" not in body and len(body) <= 80):
        return f"  {label}: {body}"
    indented = "\n".join("    " + ln for ln in body.splitlines())
    return f"  {label}:\n{indented}"


def _header(headers: list[tuple[bytes, bytes]], name: bytes) -> str:
    for k, v in headers:
        if k.lower() == name:
            return v.decode("latin-1")
    return ""


class ApiLoggingMiddleware:
    """ASGI middleware: logs method/path/params in and status/body/duration out."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not settings.effective_http_debug:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "")
        if path in _SKIP_PATHS:
            await self.app(scope, receive, send)
            return

        limit = settings.HTTP_LOG_MAX_BODY
        req_id = uuid.uuid4().hex[:8]
        query = scope.get("query_string", b"").decode("latin-1")
        req_ct = _header(scope.get("headers", []), b"content-type")

        # ── Capture the request body while replaying it downstream ──────────
        body_chunks: list[bytes] = []

        async def recv_logging():
            message = await receive()
            if message["type"] == "http.request":
                body_chunks.append(message.get("body", b""))
            return message

        # Arrival marker (the request body isn't read yet — it's logged with the
        # response, once the handler has consumed it, so long requests are still
        # visible the moment they land). Leading blank line separates flows.
        q = f"?{query}" if query else ""
        logger.debug("\nREQ [%s] %s %s%s", req_id, method, path, q)

        start = time.perf_counter()
        status_holder = {"status": 0, "ct": ""}
        resp_chunks: list[bytes] = []

        async def send_logging(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                status_holder["ct"] = _header(message.get("headers", []), b"content-type")
            elif message["type"] == "http.response.body":
                resp_chunks.append(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, recv_logging, send_logging)
        except Exception as exc:  # noqa: BLE001 — log then re-raise unchanged
            ms = (time.perf_counter() - start) * 1000
            req_body = _fmt_body(b"".join(body_chunks), req_ct, limit)
            logger.exception(
                "ERR [%s] %s %s -> %s (%.0fms)\n%s",
                req_id, method, path, exc.__class__.__name__, ms,
                _section("in", req_body),
            )
            raise

        ms = (time.perf_counter() - start) * 1000
        req_body = _fmt_body(b"".join(body_chunks), req_ct, limit)
        resp_body = _fmt_body(b"".join(resp_chunks), status_holder["ct"], limit)
        logger.debug(
            "RSP [%s] %s %s -> %s (%.0fms)\n%s\n%s",
            req_id, method, path, status_holder["status"], ms,
            _section("in", req_body), _section("out", resp_body),
        )
