"""Auto-detect a domain's industry category from its website (best-effort).

Mirrors the competitor-discovery flow: scrape the site for business context, then
ask the shared AI helper (OpenAI → local model fallback) to classify it into ONE
of the existing editable categories, or propose a new concise industry label when
none fit. The caller stores the result on ``domains.category_id``.

Every failure (AI off, site unreachable, bad response) returns ``None`` so the
domain simply stays uncategorized — competitor suggestions then fall back to the
workspace-wide behavior, exactly as before categories were assigned.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.category import Category
from app.models.domain import Domain


def _build_prompt(domain: str, category_names: list[str], corpus: str) -> tuple[str, str]:
    allowed = ", ".join(category_names) if category_names else "(none yet)"
    system = (
        "You are an SEO analyst that classifies a website into a single industry "
        "category. Reply with one valid JSON object only."
    )
    user = f"""Classify the website "{domain}" into ONE industry category.

Prefer an EXISTING category from this list when one clearly fits the site:
{allowed}

If none of them fit, propose a NEW concise industry category name (1-3 words,
Title Case, e.g. "Real Estate", "Legal Services", "Automotive"). Pick the single
best category — do not explain.

Return ONLY JSON in this shape:
{{"category": "<category name>"}}

Website content:
{corpus}
"""
    return system, user


def detect_category(db: Session, domain: Domain) -> Category | None:
    """Detect the domain's category and return the matching (or newly created)
    Category, or ``None`` when detection isn't possible. Never raises."""
    # Local imports keep this module light and avoid import cycles.
    from app.services import (
        category_service,
        competitor_discovery_service,
        openai_ai,
    )

    if not openai_ai.any_available():
        return None

    try:
        corpus = competitor_discovery_service.scrape_context(domain.domain)
    except Exception:  # noqa: BLE001 — unreachable/blocked site ⇒ no detection
        return None
    if not corpus.strip():
        return None

    category_names = [c.name for c in category_service.list_categories(db)]
    system, user = _build_prompt(domain.domain, category_names, corpus)
    try:
        data, _model = openai_ai.write_json(
            system, user, max_tokens=120, temperature=0.1
        )
    except openai_ai.OpenAiError:
        return None

    name = str(data.get("category") or "").strip()
    if not name:
        return None
    try:
        return category_service.get_or_create_category(db, name)
    except Exception:  # noqa: BLE001 — never let detection break domain creation
        return None
