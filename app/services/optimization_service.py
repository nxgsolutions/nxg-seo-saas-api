"""Page optimization: turn a page audit into a prioritized, actionable fix plan.

Reuses the audit engine, then maps each failing/warning check to a concrete
recommendation with an impact level, an effort estimate, and specific guidance
on how to fix it. Deterministic and rule-based (no external calls beyond the
audit fetch).
"""

from __future__ import annotations

from app.services import audit_service
from app.services.audit_service import Check

# Per-check metadata: category, how-to-fix guidance, and effort to implement.
_CHECK_META: dict[str, dict[str, str]] = {
    "status": {"category": "Technical", "effort": "moderate",
               "fix": "Make sure the page returns HTTP 200. Fix server errors or unintended redirects."},
    "https": {"category": "Technical", "effort": "moderate",
              "fix": "Serve the page over HTTPS with a valid TLS certificate and redirect all http:// traffic to https://."},
    "title": {"category": "Meta & Tags", "effort": "quick",
              "fix": "Write a unique 10–60 character <title> with your primary keyword near the start."},
    "description": {"category": "Meta & Tags", "effort": "quick",
                    "fix": "Add a compelling 50–160 character meta description that summarizes the page and invites the click."},
    "h1": {"category": "Content", "effort": "quick",
           "fix": "Use exactly one <h1> that clearly states the page's topic, then structure the rest with H2/H3."},
    "viewport": {"category": "Technical", "effort": "quick",
                 "fix": "Add <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"> so the page is mobile-friendly."},
    "img_alt": {"category": "Accessibility", "effort": "moderate",
                "fix": "Add descriptive alt text to every meaningful image; use empty alt=\"\" for decorative ones."},
    "canonical": {"category": "Technical", "effort": "quick",
                  "fix": "Add a self-referencing <link rel=\"canonical\"> to consolidate duplicate URLs."},
    "lang": {"category": "Technical", "effort": "quick",
             "fix": "Set the page language on the root element, e.g. <html lang=\"en\">."},
    "indexable": {"category": "Technical", "effort": "quick",
                  "fix": "Remove the noindex directive if this page should appear in search results."},
    "content": {"category": "Content", "effort": "high",
                "fix": "Expand the page past ~300 words with genuinely useful, on-topic content and clear subheadings."},
    "speed": {"category": "Performance", "effort": "high",
              "fix": "Reduce server response time (caching, CDN, lighter backend). Run PageSpeed Insights for lab metrics."},
    "size": {"category": "Performance", "effort": "moderate",
             "fix": "Trim and minify the HTML, remove unused markup and inline data, and lazy-load below-the-fold content."},
    "og": {"category": "Social", "effort": "quick",
           "fix": "Add og:title, og:description and og:image so shared links preview richly on social and chat apps."},
    "schema": {"category": "Technical", "effort": "moderate",
               "fix": "Add JSON-LD structured data (schema.org) appropriate to the page type to enable rich results."},
    "charset": {"category": "Technical", "effort": "quick",
                "fix": "Declare <meta charset=\"utf-8\"> as the first tag inside <head>."},
}

_DEFAULT_META = {"category": "Other", "effort": "moderate", "fix": "Review and address this issue."}


def _impact(check: Check) -> str:
    """High/medium/low impact from the check's weight and severity."""
    if check.status == "fail":
        return "high" if check.weight >= 2 else "medium"
    # warn
    return "medium" if check.weight >= 2 else "low"


_IMPACT_ORDER = {"high": 0, "medium": 1, "low": 2}


def optimize(raw_url: str, device: str = "desktop") -> dict:
    """Run an audit and return a prioritized optimization plan."""
    audit = audit_service.run_audit(raw_url, device)

    recommendations: list[dict] = []
    wins: list[dict] = []
    for c in audit.checks:
        if c.status == "pass":
            meta = _CHECK_META.get(c.id, _DEFAULT_META)
            wins.append({"id": c.id, "title": c.label, "category": meta["category"]})
            continue
        meta = _CHECK_META.get(c.id, _DEFAULT_META)
        recommendations.append({
            "id": c.id,
            "category": meta["category"],
            "title": c.label,
            "impact": _impact(c),
            "effort": meta["effort"],
            "current": c.detail,
            "fix": meta["fix"],
            "severity": c.status,  # warn | fail
        })

    recommendations.sort(key=lambda r: (_IMPACT_ORDER[r["impact"]], r["title"]))

    summary = {
        "high": sum(1 for r in recommendations if r["impact"] == "high"),
        "medium": sum(1 for r in recommendations if r["impact"] == "medium"),
        "low": sum(1 for r in recommendations if r["impact"] == "low"),
        "total_issues": len(recommendations),
        "passed": len(wins),
    }

    return {
        "url": audit.url,
        "final_url": audit.final_url,
        "score": audit.score,
        "device": device,
        "summary": summary,
        "recommendations": recommendations,
        "wins": wins,
    }
