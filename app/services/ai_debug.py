"""Debug logging for every AI call (prompts in, raw output out).

One place that formats and logs an AI exchange, gated by settings.AI_DEBUG.
Call `log_call(...)` at each AI chokepoint (openai_ai, local_ai,
competitor_discovery_service). Uses the "app.ai" logger at INFO so you can route
it with normal logging config; a no-op when AI_DEBUG is off, so it is safe to
leave the calls in place.

Enable with AI_DEBUG=true in the backend .env.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.core.config import settings

logger = logging.getLogger("app.ai")

_SEP = "=" * 60


def _ensure_handler() -> None:
    """Make sure the global logging config is applied so AI debug lines use the
    same timestamped formatter as the rest of the app (idempotent). Falls back to
    a matching stdout handler only if config somehow left this logger bare."""
    if logger.handlers:
        return
    from app.core.logging_config import configure_logging

    configure_logging()
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-5s %(name)s: %(message)s",
                          "%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def enabled() -> bool:
    return bool(settings.effective_ai_debug)


def _fmt(value: Any) -> str:
    """Pretty-print dict/list as JSON; everything else as str."""
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, indent=2, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def log_call(
    label: str,
    *,
    system: str | None = None,
    user: str | None = None,
    params: dict | None = None,
    output: Any = None,
) -> None:
    """Log one AI exchange. No-op unless AI_DEBUG is on.

    `label`   : which call site (e.g. "openai_ai.chat_json").
    `system`  : the system prompt.
    `user`    : the user prompt.
    `params`  : request parameters (model, max_tokens, temperature, ...).
    `output`  : the model's reply (parsed object or raw text).
    """
    if not enabled():
        return
    _ensure_handler()
    lines = [f"\n{_SEP}\n[AI] {label}"]
    if params:
        lines.append(f"PARAMS: {_fmt(params)}")
    if system is not None:
        lines.append(f"SYSTEM:\n{system}")
    if user is not None:
        lines.append(f"USER:\n{user}")
    if output is not None:
        lines.append(f"OUTPUT:\n{_fmt(output)}")
    lines.append(_SEP)
    logger.info("\n".join(lines))
