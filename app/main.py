"""FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import settings
from app.db.session import SessionLocal
from app.services import billing_service, category_service

logging.basicConfig(level=logging.INFO)
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


app = FastAPI(
    title=settings.PROJECT_NAME,
    docs_url="/docs",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

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
