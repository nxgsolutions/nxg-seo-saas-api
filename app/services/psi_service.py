"""Google PageSpeed Insights (Lighthouse + CrUX) integration.

Complements the static BeautifulSoup audit with REAL performance data: lab
metrics from Lighthouse (rendered in headless Chrome by Google) and field data
(Core Web Vitals from real users, via CrUX) when available.

`parse_psi` is pure and unit-tested; `run_psi` performs the API call.
"""

from typing import Any

import httpx

from app.core.config import settings
from app.services.audit_service import InvalidUrlError, normalize_url

PSI_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
PSI_TIMEOUT_SECONDS = 60.0


class PsiUnavailableError(Exception):
    """PSI could not be reached, was rate-limited, or returned an error."""


def _lab_metric(audits: dict[str, Any], audit_id: str) -> dict[str, Any]:
    a = audits.get(audit_id, {})
    return {
        "display": a.get("displayValue"),
        "score": a.get("score"),
        "value": a.get("numericValue"),
    }


def _field_metric(metrics: dict[str, Any], key: str) -> dict[str, Any] | None:
    m = metrics.get(key)
    if not m:
        return None
    return {"percentile": m.get("percentile"), "category": m.get("category")}


def parse_psi(data: dict[str, Any]) -> dict[str, Any]:
    """Extract a compact result (lab + field) from a PSI v5 response."""
    lh = data.get("lighthouseResult", {})
    perf = lh.get("categories", {}).get("performance", {}).get("score")
    score = round(perf * 100) if isinstance(perf, (int, float)) else None
    audits = lh.get("audits", {})

    lab = {
        "lcp": _lab_metric(audits, "largest-contentful-paint"),
        "cls": _lab_metric(audits, "cumulative-layout-shift"),
        "tbt": _lab_metric(audits, "total-blocking-time"),
        "fcp": _lab_metric(audits, "first-contentful-paint"),
        "si": _lab_metric(audits, "speed-index"),
        "tti": _lab_metric(audits, "interactive"),
    }

    le = data.get("loadingExperience", {})
    metrics = le.get("metrics") or {}
    field: dict[str, Any] | None = None
    if metrics:
        field = {
            "overall": le.get("overall_category"),
            "lcp": _field_metric(metrics, "LARGEST_CONTENTFUL_PAINT_MS"),
            "cls": _field_metric(metrics, "CUMULATIVE_LAYOUT_SHIFT_SCORE"),
            "inp": _field_metric(metrics, "INTERACTION_TO_NEXT_PAINT")
            or _field_metric(metrics, "EXPERIMENTAL_INTERACTION_TO_NEXT_PAINT"),
            "fcp": _field_metric(metrics, "FIRST_CONTENTFUL_PAINT_MS"),
        }

    return {"performance_score": score, "lab": lab, "field": field}


def run_psi(url: str, strategy: str = "mobile") -> dict[str, Any]:
    """Call PSI for a URL. Raises InvalidUrlError / PsiUnavailableError."""
    normalized = normalize_url(url)  # may raise InvalidUrlError

    params: dict[str, str] = {
        "url": normalized,
        "strategy": "desktop" if strategy == "desktop" else "mobile",
        "category": "performance",
    }
    if settings.PSI_API_KEY:
        params["key"] = settings.PSI_API_KEY

    try:
        response = httpx.get(PSI_ENDPOINT, params=params, timeout=PSI_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        raise PsiUnavailableError(f"Could not reach PageSpeed Insights: {exc}") from exc

    if response.status_code in (403, 429):
        raise PsiUnavailableError(
            "PageSpeed Insights rate limit reached. Add a PSI_API_KEY to raise limits."
        )
    if response.status_code != 200:
        detail = ""
        try:
            detail = response.json().get("error", {}).get("message", "")
        except Exception:  # pragma: no cover
            pass
        raise PsiUnavailableError(
            f"PageSpeed Insights error ({response.status_code}). {detail}".strip()
        )

    data = response.json()
    result = parse_psi(data)
    result["url"] = data.get("id", normalized)
    result["strategy"] = params["strategy"]
    return result
