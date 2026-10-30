"""Local AI via Ollama (free, private).

Thin client over Ollama's HTTP API. Used as the FALLBACK for AI writing when
OpenAI isn't configured or fails (see openai_ai.write_json).
"""

from __future__ import annotations

import json

import httpx

from app.core.config import settings


class LocalAiError(Exception):
    pass


def available() -> bool:
    """Quick reachability check (short timeout) — is Ollama serving?"""
    if not settings.OLLAMA_ENABLED:
        return False
    try:
        with httpx.Client(timeout=3.0) as client:
            resp = client.get(f"{settings.OLLAMA_BASE_URL}/api/tags")
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


def chat_json(system: str, user: str, timeout: float = 240.0, *,
              think: bool | None = None, options: dict | None = None) -> dict:
    """Call the local model and parse a JSON object from its reply.

    Uses Ollama's `format: "json"` so the model is constrained to valid JSON.
    `think=False` turns off a reasoning model's hidden thinking (short answers
    otherwise spend their whole token budget thinking); `options` overrides
    generation options (temperature, num_predict, ...).
    """
    body = {
        "model": settings.OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "format": "json",
        "stream": False,
        "options": {"temperature": 0.4, **(options or {})},
    }
    if think is not None:
        body["think"] = think
    from app.services import ai_debug

    ai_debug.log_call(
        "local_ai.chat_json", system=system, user=user,
        params={"model": settings.OLLAMA_MODEL, "think": think,
                "options": body["options"]},
    )
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(f"{settings.OLLAMA_BASE_URL}/api/chat", json=body)
    except httpx.HTTPError as exc:
        raise LocalAiError(f"Ollama request failed: {exc}") from exc
    if resp.status_code != 200:
        raise LocalAiError(f"Ollama error ({resp.status_code}): {resp.text[:200]}")

    content = (resp.json().get("message") or {}).get("content", "").strip()
    ai_debug.log_call("local_ai.chat_json", output=content)
    if content.startswith("```"):
        content = content.strip("`")
        content = content.removeprefix("json").strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise LocalAiError("Local model returned unparseable JSON.") from exc
