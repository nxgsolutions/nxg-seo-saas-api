"""FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.logging_middleware import ApiLoggingMiddleware
from app.api.rate_limit import RateLimitMiddleware
from app.api.router import api_router
from app.api.security_headers import SecurityHeadersMiddleware
from app.core.config import settings
from app.core.logging_config import configure_logging
from app.db.session import SessionLocal
from app.services import billing_service, category_service

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup tasks. Ensure the default billing plans always exist so a data
    wipe can never leave the plans table empty (which would break signup)."""
    try:
        with SessionLocal() as db:
            inserted = billing_service.seed_default_plans(db)
            if inserted:
                logger.info("Seeded %d default billing plan(s) on startup.", inserted)
            seeded_cats = category_service.seed_default_categories(db)
            if seeded_cats:
                logger.info("Seeded %d default categor(ies) on startup.", seeded_cats)
    except Exception:  # pragma: no cover - never block startup on the seed
        logger.exception("Failed to seed default billing plans/categories on startup.")
    yield


# Interactive API docs + the OpenAPI schema are served only outside production
# (in prod they would expose the full API surface to anyone). None disables them.
app = FastAPI(
    title=settings.PROJECT_NAME,
    docs_url="/docs" if settings.docs_enabled else None,
    redoc_url="/redoc" if settings.docs_enabled else None,
    openapi_url="/openapi.json" if settings.docs_enabled else None,
    lifespan=lifespan,
)

# Middleware order note: Starlette applies the LAST-added middleware outermost,
# so the registration order below is inner -> outer. The effective wrapping is:
#   CORS -> SecurityHeaders -> RateLimit -> ApiLogging -> routes
# CORS stays outermost so even a 429 / error response carries CORS headers;
# SecurityHeaders sits just inside it so its headers survive onto every response.

# Per-request flow logging (gated by settings.HTTP_DEBUG) — innermost; wraps the
# actual route handling.
app.add_middleware(ApiLoggingMiddleware)

# Coarse per-IP flood guard across every route (gated by RATE_LIMIT_ENABLED).
app.add_middleware(RateLimitMiddleware)

# Defensive response headers (gated by SECURITY_HEADERS_ENABLED).
app.add_middleware(SecurityHeadersMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# All application routes live under the configured API prefix (e.g. /api).
app.include_router(api_router, prefix=settings.API_V1_PREFIX)


@app.get("/", tags=["health"])
@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    """Lightweight liveness probe (served at both `/` and `/health`)."""
    return {"status": "ok"}
