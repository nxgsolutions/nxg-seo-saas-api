"""OpenAI chat → JSON object (AI writing: Content Optimization, AI overviews, Title & Meta).

`chat_json` = one OpenAI call (JSON mode). `write_json` = what features use:
OpenAI first, the local model as a fallback. Every failure raises OpenAiError
so callers fall back to their rule-based text — never to sample output.
"""

from __future__ import annotations

import json

from app.core.config import settings


class OpenAiError(Exception):
    pass


def available() -> bool:
    return settings.openai_configured


def any_available() -> bool:
    """Can `write_json` write anything (OpenAI key set, or the local model up)?"""
    from app.services import local_ai

    return available() or local_ai.available()


def chat_json(system: str, user: str, *, model: str | None = None,
              max_tokens: int = 1800, temperature: float = 0.4,
              timeout: float = 60.0) -> dict:
    """Ask the model for ONE JSON object and parse it."""
    if not available():
        raise OpenAiError("OpenAI is not configured (set OPENAI_API_KEY in the backend .env).")
    from openai import OpenAI

    client = OpenAI(api_key=settings.OPENAI_API_KEY, timeout=timeout, max_retries=1)
    try:
        resp = client.chat.completions.create(
            model=model or settings.AI_TEXT_MODEL,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_object"},
            max_tokens=max_tokens,
            temperature=temperature,
        )
    except Exception as exc:  # noqa: BLE001 — network, auth, quota, …
        raise OpenAiError(f"OpenAI request failed: {exc.__class__.__name__}") from exc

    text = (resp.choices[0].message.content or "").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OpenAiError("OpenAI returned invalid JSON.") from exc
    if not isinstance(data, dict):
        raise OpenAiError("OpenAI returned no JSON object.")
    return data


def write_json(system: str, user: str, *, max_tokens: int = 1800, temperature: float = 0.4,
               timeout: float = 60.0) -> tuple[dict, str]:
    """AI writing with one provider order for the whole app:
    OpenAI (settings.AI_TEXT_MODEL) → the local model (Ollama) → OpenAiError.
    Returns (json object, model name used)."""
    from app.services import local_ai  # local import: keeps this module light

    errors = []
    if available():
        try:
            return chat_json(system, user, max_tokens=max_tokens, temperature=temperature,
                             timeout=timeout), settings.AI_TEXT_MODEL
        except OpenAiError as exc:
            errors.append(str(exc))
    if local_ai.available():
        try:
            data = local_ai.chat_json(system, user, timeout=max(timeout, 120.0), think=False,
                                      options={"temperature": temperature, "num_predict": max_tokens})
            return data, settings.OLLAMA_MODEL
        except local_ai.LocalAiError as exc:
            errors.append(str(exc))
    raise OpenAiError(errors[0] if errors else "AI writing is not configured.")
