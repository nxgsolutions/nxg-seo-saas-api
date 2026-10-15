"""Google Ads Keyword Planner — real search volume, competition and CPC.

Free (a Google API, not a paid third party), but requires a Google Ads account +
developer token + an OAuth refresh token with the `adwords` scope. Called over
the Ads REST API with httpx (no google-ads SDK dependency).

Two modes, both via GenerateKeywordIdeas:
  * mode="keyword" -> keyword seed(s)         ("Search by keywords")
  * mode="url"     -> a specific page URL     ("Search by website" — one page)
  * mode="site"    -> a whole domain          ("Search by website" — whole site)

Returns RAW Google fields only — no scoring, no rules. When credentials are not
set, raises NotConfiguredError so the endpoint can fall back / prompt to connect.
"""

from __future__ import annotations

import httpx

from app.core.config import settings

_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
_TIMEOUT = httpx.Timeout(30.0)

# country code -> Google Ads geoTargetConstant id
_GEO = {
    "us": 2840, "in": 2356, "gb": 2826, "ca": 2124, "au": 2036,
    "de": 2276, "fr": 2250, "sg": 2702, "ae": 2784, "za": 2710,
}
# language code -> Google Ads languageConstant id
_LANG = {
    "en": 1000, "hi": 1023, "es": 1003, "fr": 1002, "de": 1001,
    "pt": 1014, "ar": 1019, "ta": 1098, "te": 1130, "bn": 1056,
}


class PlannerError(Exception):
    """The planner is configured but the request failed."""


class NotConfiguredError(PlannerError):
    """Google Ads Keyword Planner credentials are not set."""


def _access_token() -> str:
    """Exchange the stored refresh token for a short-lived access token."""
    payload = {
        "client_id": settings.ads_client_id,
        "client_secret": settings.ads_client_secret,
        "refresh_token": settings.GOOGLE_ADS_REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.post(_OAUTH_TOKEN_URL, data=payload)
    except httpx.HTTPError as exc:
        raise PlannerError(f"Google token request failed: {exc}") from exc
    if resp.status_code != 200:
        raise PlannerError(f"Google token error ({resp.status_code}): {resp.text[:200]}")
    return resp.json()["access_token"]


def _micros(value: str | int | None) -> float | None:
    """Ads money is in micros (1,000,000 micros = 1 unit of currency)."""
    if value in (None, ""):
        return None
    return round(int(value) / 1_000_000, 2)


def _raise_friendly(resp: httpx.Response) -> None:
    """Turn a Google Ads error response into a clear, actionable message."""
    blob = resp.text.lower()
    try:
        err = resp.json().get("error", {})
        parts = [err.get("message", "")]
        for d in err.get("details", []):
            for e in d.get("errors", []):
                parts.append(str(e.get("errorCode", "")))
        blob = " ".join(parts).lower()
    except ValueError:
        pass

    if "customer_not_enabled" in blob:
        raise PlannerError(
            "Your Google Ads account isn't enabled yet. Finish account setup and "
            "add a billing profile in Google Ads, then try again."
        )
    if resp.status_code == 403 or "permission_denied" in blob or "not have permission" in blob:
        raise PlannerError(
            "Google Ads access level too low for Keyword Planner. Your Cloud project "
            "has Test/Explorer access, but Keyword Planner needs BASIC access — "
            "complete brand verification and apply for Basic access in Google Cloud "
            "Console. Ideas, intent and SEO difficulty still work meanwhile."
        )
    raise PlannerError(f"Keyword Planner error ({resp.status_code}): {resp.text[:300]}")


def _seed_body(term: str, mode: str) -> dict:
    if mode == "url":
        return {"urlSeed": {"url": term}}
    if mode == "site":
        return {"siteSeed": {"site": term}}
    # default: keyword seed (accepts a comma-separated list too)
    keywords = [k.strip() for k in term.split(",") if k.strip()]
    return {"keywordSeed": {"keywords": keywords or [term]}}


def _ads_url(method: str) -> str:
    version = settings.GOOGLE_ADS_API_VERSION
    customer = settings.GOOGLE_ADS_CUSTOMER_ID.replace("-", "").strip()
    return f"https://googleads.googleapis.com/{version}/customers/{customer}:{method}"


def _ads_headers() -> dict:
    """Auth headers. Developer token is optional (sunset 2026-09-09)."""
    headers = {"Authorization": f"Bearer {_access_token()}", "Content-Type": "application/json"}
    if settings.GOOGLE_ADS_DEVELOPER_TOKEN:
        headers["developer-token"] = settings.GOOGLE_ADS_DEVELOPER_TOKEN
    if settings.GOOGLE_ADS_LOGIN_CUSTOMER_ID:
        headers["login-customer-id"] = settings.GOOGLE_ADS_LOGIN_CUSTOMER_ID.replace("-", "").strip()
    return headers


def _metrics_row(text: str, m: dict) -> dict:
    """Normalize a Google metrics object (keywordIdeaMetrics or keywordMetrics)."""
    trend = [
        {
            "year": int(mv["year"]) if mv.get("year") else None,
            "month": mv.get("month"),
            "searches": int(mv["monthlySearches"]) if mv.get("monthlySearches") else 0,
        }
        for mv in (m.get("monthlySearchVolumes") or [])
    ]
    return {
        "keyword": text,
        "avg_monthly_searches": int(m["avgMonthlySearches"]) if m.get("avgMonthlySearches") else 0,
        "competition": m.get("competition"),  # LOW | MEDIUM | HIGH | UNSPECIFIED
        "competition_index": int(m["competitionIndex"]) if m.get("competitionIndex") else None,
        "cpc_low": _micros(m.get("lowTopOfPageBidMicros")),
        "cpc_high": _micros(m.get("highTopOfPageBidMicros")),
        "monthly": trend,
    }


def generate_ideas(
    term: str,
    *,
    mode: str = "keyword",
    country: str = "us",
    language: str = "en",
    limit: int = 200,
) -> dict:
    """Raw keyword ideas from Google Ads Keyword Planner.

    `mode`: 'keyword' | 'url' | 'site'. Returns {"configured": True, "rows": [...]}
    where each row is Google's raw metrics for one keyword idea."""
    if not settings.google_ads_configured:
        raise NotConfiguredError(
            "Google Ads Keyword Planner is not configured. Set "
            "GOOGLE_ADS_REFRESH_TOKEN and GOOGLE_ADS_CUSTOMER_ID in backend/.env "
            "to get real volume & CPC (a developer token is no longer required)."
        )

    url = _ads_url("generateKeywordIdeas")
    geo = _GEO.get(country.lower(), _GEO["us"])
    lang = _LANG.get(language.lower(), _LANG["en"])
    body = {
        "geoTargetConstants": [f"geoTargetConstants/{geo}"],
        "language": f"languageConstants/{lang}",
        "keywordPlanNetwork": "GOOGLE_SEARCH",
        "pageSize": min(limit, 1000),
        **_seed_body(term, mode),
    }

    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.post(url, headers=_ads_headers(), json=body)
    except httpx.HTTPError as exc:
        raise PlannerError(f"Keyword Planner request failed: {exc}") from exc
    if resp.status_code != 200:
        _raise_friendly(resp)

    rows = [
        _metrics_row(item.get("text", ""), item.get("keywordIdeaMetrics") or {})
        for item in resp.json().get("results", [])[:limit]
    ]
    return {
        "configured": True,
        "mode": mode,
        "input": term,
        "country": country,
        "language": language,
        "count": len(rows),
        "rows": rows,
    }


def historical_metrics(
    keywords: list[str], *, country: str = "us", language: str = "en"
) -> list[dict]:
    """Exact metrics (volume/CPC/competition) for a specific list of keywords, via
    GenerateKeywordHistoricalMetrics. Used to decorate keyword-search results so
    every keyword we show gets real numbers (not just seed-expansion overlaps)."""
    if not settings.google_ads_configured:
        raise NotConfiguredError("Google Ads Keyword Planner is not configured.")
    kws = [k for k in dict.fromkeys(k.strip() for k in keywords) if k][:1000]
    if not kws:
        return []

    url = _ads_url("generateKeywordHistoricalMetrics")
    geo = _GEO.get(country.lower(), _GEO["us"])
    lang = _LANG.get(language.lower(), _LANG["en"])
    body = {
        "keywords": kws,
        "geoTargetConstants": [f"geoTargetConstants/{geo}"],
        "language": f"languageConstants/{lang}",
        "keywordPlanNetwork": "GOOGLE_SEARCH",
    }
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.post(url, headers=_ads_headers(), json=body)
    except httpx.HTTPError as exc:
        raise PlannerError(f"Keyword metrics request failed: {exc}") from exc
    if resp.status_code != 200:
        _raise_friendly(resp)

    return [
        _metrics_row(item.get("text", ""), item.get("keywordMetrics") or {})
        for item in resp.json().get("results", [])
    ]
