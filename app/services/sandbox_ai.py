"""Static, token-free LLM stand-ins for sandbox workspaces.

Sandbox workspaces exist to exercise the app's plumbing without spending money.
DataForSEO calls already route to its free sandbox; these helpers do the same
for the LLM-backed features so a sandbox domain never burns OpenAI/Ollama tokens:

  * competitor discovery  (competitor_service.discover_and_store)
  * business profile       (competitor_service.refresh_business_profile)
  * manual-add match score (competitor_service._verify_manual_add)
  * category auto-detect   (category_detection_service.detect_category)

The data is deterministic and obviously a placeholder, shaped exactly like the
real responses so downstream code, storage and the UI behave identically — the
only difference is that no model is called.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.domain import Domain
from app.services import data_env

# A stable, obviously-dummy competitor pool. Hosts use the reserved `.example`
# TLD so they can never collide with a real site.
_SANDBOX_COMPETITORS: list[tuple[str, str]] = [
    ("Sandbox Rival One", "sandbox-rival-one.example"),
    ("Sandbox Rival Two", "sandbox-rival-two.example"),
    ("Sandbox Rival Three", "sandbox-rival-three.example"),
    ("Sandbox Rival Four", "sandbox-rival-four.example"),
    ("Sandbox Rival Five", "sandbox-rival-five.example"),
    ("Sandbox Rival Six", "sandbox-rival-six.example"),
    ("Sandbox Rival Seven", "sandbox-rival-seven.example"),
    ("Sandbox Rival Eight", "sandbox-rival-eight.example"),
    ("Sandbox Rival Nine", "sandbox-rival-nine.example"),
    ("Sandbox Rival Ten", "sandbox-rival-ten.example"),
]

_SANDBOX_CATEGORY = "Sandbox Industry"


def is_sandbox(db: Session, domain: Domain) -> bool:
    """True when `domain`'s workspace routes to the sandbox (so LLM calls should
    be stubbed instead of billed)."""
    return data_env.env_for_domain(db, domain) == data_env.SANDBOX


def business_profile(target: str) -> dict:
    """Static stand-in for competitor_discovery_service.extract_business_profile."""
    return {
        "business_description": f"Sandbox placeholder business profile for {target}.",
        "industry": _SANDBOX_CATEGORY,
        "services": ["Sandbox Service A", "Sandbox Service B", "Sandbox Service C"],
        "target_audience": "Sandbox test audience",
        "market": "India",
        "primary_products": ["Sandbox Product"],
        "secondary_products": [],
        "combined_products": ["Sandbox Product"],
    }


def competitors(limit: int = 10, exclude_domains: list[str] | None = None) -> list[dict]:
    """Static competitor rows, shaped like the discovery output
    ({name, domain, reason, confidence_level, match_score})."""
    excluded = {(d or "").strip().lower().lstrip("www.") for d in (exclude_domains or [])}
    rows: list[dict] = []
    for name, host in _SANDBOX_COMPETITORS:
        if host in excluded:
            continue
        rows.append({
            "name": name,
            "domain": host,
            "reason": "Sandbox placeholder competitor (no model call).",
            "confidence_level": "Medium",
            "match_score": 60,
        })
        if len(rows) >= max(1, int(limit)):
            break
    return rows


def discover_result(
    target: str, *, limit: int = 10, market: str | None = None,
    exclude_domains: list[str] | None = None, business: dict | None = None,
) -> dict:
    """Static stand-in for competitor_discovery_service.discover."""
    return {
        "business": business or business_profile(target),
        "market": (market or "").strip() or "India",
        "competitors": competitors(limit, exclude_domains),
    }


def verify_score() -> dict:
    """Static stand-in for competitor_discovery_service.verify_single."""
    return {
        "match_score": 60,
        "confidence_level": "Medium",
        "reason": "Sandbox placeholder match (no model call).",
    }


def category_name() -> str:
    """Static stand-in for category_detection_service's LLM classification."""
    return _SANDBOX_CATEGORY
