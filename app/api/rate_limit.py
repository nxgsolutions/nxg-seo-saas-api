"""In-process, per-client-IP rate limiting (no external dependency).

A fixed-window counter keyed by ``scope:ip``. Two consumers share one store:

* ``auth_rate_limit`` — a FastAPI dependency applied to the auth routes
  (/auth/login, /auth/register, /auth/refresh) to blunt brute-force attempts.
* ``RateLimitMiddleware`` — a coarse global flood guard across every route.

Both raise/return HTTP 429 with a ``Retry-After`` header when the window is
exceeded. All limits are configurable (see app/core/config.py) and the whole
feature can be turned off with RATE_LIMIT_ENABLED=false.

Scope & caveats: the store is in-memory and per-process, so with multiple
workers each worker counts independently (fine as a safety net; use a shared
store like Redis if you need exact global limits). Client IP is the socket peer
unless TRUST_PROXY_HEADERS is set, in which case the first X-Forwarded-For hop
is used — only enable that behind a trusted proxy, or the header can be spoofed.
"""

from __future__ import annotations

import threading
import time

from fastapi import HTTPException, Request, status

from app.core.config import settings


class FixedWindowRateLimiter:
    """Thread-safe fixed-window request counter keyed by an arbitrary string."""

    def __init__(self) -> None:
        # key -> (window_start_epoch_seconds, count)
        self._hits: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    def check(self, key: str, *, limit: int, window: int) -> tuple[bool, int]:
        """Record a hit for ``key``. Return (allowed, retry_after_seconds).

        ``limit`` <= 0 means "no limit" (always allowed). ``retry_after`` is only
        meaningful when the request is blocked.
        """
        if limit <= 0:
            return True, 0

        now = time.monotonic()
        with self._lock:
            start, count = self._hits.get(key, (now, 0))
            # Window elapsed -> start a fresh one.
            if now - start >= window:
                start, count = now, 0
            count += 1
            self._hits[key] = (start, count)
            if count > limit:
                retry_after = max(1, int(window - (now - start)) + 1)
                return False, retry_after

            # Opportunistic cleanup so the dict can't grow without bound.
            if len(self._hits) > 10_000:
                self._prune(now, window)
        return True, 0

    def _prune(self, now: float, window: int) -> None:
        """Drop entries whose window has fully elapsed (caller holds the lock)."""
        stale = [k for k, (start, _) in self._hits.items() if now - start >= window]
        for k in stale:
            del self._hits[k]

    def reset(self) -> None:
        """Clear all counters (used by tests for isolation)."""
        with self._lock:
            self._hits.clear()


# Process-wide singleton shared by the auth dependency and the global middleware.
rate_limiter = FixedWindowRateLimiter()


def client_ip(request_or_scope) -> str:
    """Best-effort client IP from a Request or a raw ASGI scope.

    Honours X-Forwarded-For only when TRUST_PROXY_HEADERS is set; otherwise uses
    the socket peer so the limiter can't be bypassed with a forged header.
    """
    if isinstance(request_or_scope, Request):
        headers = request_or_scope.headers
        peer = request_or_scope.client.host if request_or_scope.client else None
        xff = headers.get("x-forwarded-for")
    else:  # raw ASGI scope (middleware)
        raw = {k.decode("latin-1").lower(): v.decode("latin-1")
               for k, v in request_or_scope.get("headers", [])}
        xff = raw.get("x-forwarded-for")
        peer_t = request_or_scope.get("client")
        peer = peer_t[0] if peer_t else None

    if settings.TRUST_PROXY_HEADERS and xff:
        return xff.split(",")[0].strip()
    return peer or "unknown"


def auth_rate_limit(request: Request) -> None:
    """FastAPI dependency: throttle auth endpoints per client IP."""
    if not settings.RATE_LIMIT_ENABLED:
        return
    key = f"auth:{client_ip(request)}"
    allowed, retry_after = rate_limiter.check(
        key,
        limit=settings.RATE_LIMIT_AUTH_MAX,
        window=settings.RATE_LIMIT_AUTH_WINDOW_SECONDS,
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many attempts. Please wait a moment and try again.",
            headers={"Retry-After": str(retry_after)},
        )


class RateLimitMiddleware:
    """ASGI middleware: coarse per-IP flood guard across every HTTP route."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not settings.RATE_LIMIT_ENABLED:
            await self.app(scope, receive, send)
            return

        key = f"global:{client_ip(scope)}"
        allowed, retry_after = rate_limiter.check(
            key,
            limit=settings.RATE_LIMIT_GLOBAL_MAX,
            window=settings.RATE_LIMIT_GLOBAL_WINDOW_SECONDS,
        )
        if not allowed:
            await self._too_many(send, retry_after)
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _too_many(send, retry_after: int) -> None:
        body = b'{"detail":"Too many requests. Please slow down."}'
        await send({
            "type": "http.response.start",
            "status": status.HTTP_429_TOO_MANY_REQUESTS,
            "headers": [
                (b"content-type", b"application/json"),
                (b"retry-after", str(retry_after).encode("latin-1")),
            ],
        })
        await send({"type": "http.response.body", "body": body})
