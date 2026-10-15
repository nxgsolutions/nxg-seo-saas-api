"""AI meta tag suggestions (title + description) for a topic or a URL.

Written by OpenAI gpt-4o-mini (openai_ai.write_json — the local model is the
fallback). For a URL the page is read first (SSRF-guarded) so the suggestion
fits the real content. When no AI is available it raises MetaUnavailableError —
there is no sample/placeholder output.
"""

from __future__ import annotations

from app.services import content_opt_service, content_score, openai_ai

SYSTEM_PROMPT = (
    "You are an expert SEO copywriter. Given the page context, write ONE "
    "SEO-optimized title tag (50-60 characters, primary keyword near the front) "
    "and ONE meta description (140-155 characters, compelling with a call to "
    "action). Write in the page's language. Do not invent prices, dates or statistics. "
    "Return ONLY a JSON object: {\"title\": \"...\", \"description\": \"...\"}"
)


class MetaUnavailableError(Exception):
    pass


def _context(input_text: str) -> str:
    context = input_text.strip()
    if not context.lower().startswith("http"):
        return f"Topic: {context}"
    try:
        html, final_url = content_opt_service._fetch_html(context)
    except content_opt_service.ContentFetchError:
        return f"URL (could not be read): {context}"
    f = content_score.extract(html, final_url)
    heads = ", ".join(h["text"] for h in f["headings"][:8])
    return (
        f"URL: {final_url}\n"
        f"Existing title: {f['title'] or '(none)'}\n"
        f"Existing meta description: {f['meta'] or '(none)'}\n"
        f"H1: {', '.join(f['h1']) or '(none)'}\n"
        f"Headings: {heads or '(none)'}\n"
        f"Opening: {f['first_paragraph'][:600]}"
    )


def suggest(input_text: str) -> dict:
    """Suggest a title + meta description for a topic or URL."""
    context = _context(input_text)
    try:
        data, _model = openai_ai.write_json(
            SYSTEM_PROMPT, f"Page context:\n{context}\n\nProduce the JSON.", max_tokens=300, temperature=0.5)
    except openai_ai.OpenAiError as exc:
        raise MetaUnavailableError("AI writing is unavailable right now. Please try again in a minute.") from exc
    title = str(data.get("title", "")).strip()[:120]
    description = str(data.get("description", "")).strip()[:300]
    if not title and not description:
        raise MetaUnavailableError("The AI returned no suggestion. Please try again.")
    return {"is_sample": False, "title": title, "description": description}
