"""AI Search (Generative Engine Optimization) analysis.

Given a topic/query and optionally a domain, returns an AI-written assessment of
how likely the brand is to surface in AI answers (ChatGPT, Perplexity, Google
AI Overviews) and how to improve that — a "GEO/AEO" report.

Written by OpenAI gpt-4o-mini (openai_ai.write_json — the local model is the
fallback), then Claude if an Anthropic key is set. The visibility score is the
model's ESTIMATE, not a measurement of live AI answers. When no AI is
available it raises AiUnavailableError — there is no sample output.
"""

from __future__ import annotations

import json

from app.core.config import settings
from app.services import openai_ai

SYSTEM_PROMPT = (
    "You are an expert SEO and Generative Engine Optimization (GEO/AEO) analyst. "
    "You assess how likely a brand or domain is to be cited in AI-generated "
    "answers (ChatGPT, Perplexity, Google AI Overviews, Claude) for a topic, and "
    "give concrete, prioritized guidance to improve that visibility. "
    "Respond with ONLY a single JSON object — no markdown, no prose outside the "
    "JSON. Use this exact shape:\n"
    "{\n"
    '  "visibility_score": <int 0-100>,\n'
    '  "summary": "<2-3 sentence assessment>",\n'
    '  "target_queries": [{"query": "<AI prompt a user might ask>", "opportunity": "high|medium|low"}],\n'
    '  "recommendations": [{"title": "<short>", "detail": "<1-2 sentences>", "priority": "high|medium|low"}],\n'
    '  "content_gaps": ["<missing content/topic>"],\n'
    '  "competitor_notes": ["<how competitors win in AI answers>"]\n'
    "}"
)


class AiUnavailableError(Exception):
    """No AI provider could produce the analysis."""


def _user_prompt(query: str, domain: str | None) -> str:
    user = f"Topic / target query: {query}"
    if domain:
        user += f"\nBrand domain: {domain}"
    return user + "\n\nProduce the JSON analysis."


def _normalize(data: dict, query: str, domain: str | None, model: str) -> dict:
    """Coerce an AI reply into the response shape (guards missing/odd fields)."""
    def _tq(items) -> list[dict]:
        out = []
        for it in items or []:
            if isinstance(it, dict):
                out.append({"query": str(it.get("query", "")), "opportunity": str(it.get("opportunity", "medium"))})
        return out

    def _recs(items) -> list[dict]:
        out = []
        for it in items or []:
            if isinstance(it, dict):
                out.append({
                    "title": str(it.get("title", "")),
                    "detail": str(it.get("detail", "")),
                    "priority": str(it.get("priority", "medium")),
                })
        return out

    def _strs(items) -> list[str]:
        return [str(x) for x in (items or []) if isinstance(x, (str, int, float))]

    return {
        "is_sample": False,
        "model": model,
        "query": query,
        "domain": domain,
        "visibility_score": max(0, min(100, int(data.get("visibility_score") or 0))),
        "summary": str(data.get("summary") or ""),
        "target_queries": _tq(data.get("target_queries")),
        "recommendations": _recs(data.get("recommendations")),
        "content_gaps": _strs(data.get("content_gaps")),
        "competitor_notes": _strs(data.get("competitor_notes")),
    }


def _call_claude(query: str, domain: str | None) -> dict:
    import anthropic

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    try:
        resp = client.messages.create(
            model=settings.AI_MODEL,
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _user_prompt(query, domain)}],
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as 502 to the caller
        raise AiUnavailableError(f"AI request failed: {exc}") from exc

    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if "```" in text[3:] else text.strip("`")
        text = text.removeprefix("json").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AiUnavailableError("AI returned an unparseable response.") from exc

    return _normalize(data, query, domain, settings.AI_MODEL)


def analyze_ai_search(query: str, domain: str | None = None) -> dict:
    """Return an AI-search visibility analysis.

    Provider order: OpenAI gpt-4o-mini (local model fallback) → Claude (if a key
    is set) → AiUnavailableError.
    """
    try:
        data, model = openai_ai.write_json(SYSTEM_PROMPT, _user_prompt(query, domain),
                                           max_tokens=1500, temperature=0.4, timeout=90.0)
        return _normalize(data, query, domain, model)
    except openai_ai.OpenAiError as exc:
        if settings.ai_configured:
            return _call_claude(query, domain)
        raise AiUnavailableError("AI analysis is unavailable right now. Please try again in a minute.") from exc
