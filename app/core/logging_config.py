"""Global logging configuration (one place, applied once at startup).

`configure_logging()` installs a single dictConfig that owns ALL formatting for
the app, so every logger (including uvicorn's) emits the same timestamped line:

    2026-10-23 12:00:00,123 INFO  app.ai: <message>

It also sets up a dedicated "app.ai" logger (used by services/ai_debug.py) that
prints full AI exchanges — system prompt, user prompt, params, raw output. When
settings.AI_LOG_FILE is set, those blocks are ALSO appended to a rotating file so
you keep a persistent, scrollable debug record instead of only terminal
scrollback.

Call configure_logging() ONCE, as early as possible (app.main does this at
import time). Idempotent — safe to call more than once.
"""

from __future__ import annotations

import logging
import logging.config
from pathlib import Path

from app.core.config import settings

_configured = False

_FORMAT = "%(asctime)s %(levelname)-5s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def configure_logging() -> None:
    global _configured
    if _configured:
        return

    root_level = (settings.LOG_LEVEL or "INFO").upper()
    # These debug streams are verbose only when their flag is on; else stay quiet.
    ai_level = "DEBUG" if settings.effective_ai_debug else "WARNING"
    api_level = "DEBUG" if settings.effective_http_debug else "WARNING"

    handlers: dict[str, dict] = {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
            "stream": "ext://sys.stdout",
        },
    }
    ai_handlers = ["console"]

    # Optional rotating file for the AI debug stream.
    if settings.AI_LOG_FILE:
        path = Path(settings.AI_LOG_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers["ai_file"] = {
            "class": "logging.handlers.RotatingFileHandler",
            "formatter": "standard",
            "filename": str(path),
            "maxBytes": 5_000_000,  # ~5 MB per file
            "backupCount": 3,
            "encoding": "utf-8",
        }
        ai_handlers.append("ai_file")

    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {"format": _FORMAT, "datefmt": _DATEFMT},
        },
        "handlers": handlers,
        "root": {"level": root_level, "handlers": ["console"]},
        "loggers": {
            # Full AI exchanges. propagate=False so they don't double-print
            # through the root handler.
            "app.ai": {"level": ai_level, "handlers": ai_handlers, "propagate": False},
            # Per-request API flow (method/path/params in, status/body/ms out).
            "app.api": {"level": api_level, "handlers": ai_handlers, "propagate": False},
            # Keep uvicorn/access on our formatter instead of its own.
            "uvicorn": {"level": root_level, "handlers": ["console"], "propagate": False},
            "uvicorn.error": {"level": root_level, "handlers": ["console"], "propagate": False},
            # Our ApiLoggingMiddleware logs a richer request/response line, so
            # silence uvicorn's duplicate access line when HTTP_DEBUG is on; keep
            # it (INFO) when off so there's still basic request visibility.
            "uvicorn.access": {
                "level": "WARNING" if settings.effective_http_debug else root_level,
                "handlers": ["console"], "propagate": False,
            },
        },
    })
    _configured = True
