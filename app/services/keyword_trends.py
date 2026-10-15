"""Google Trends client — keyless relative demand, no paid provider.

Uses Google Trends' public web endpoints (the same ones trends.google.com calls)
via the standard two-step handshake: an `explore` call returns a widget token,
then `multiline` returns the interest-over-time series. No API key, no quota.
Hosts are fixed, so there is no SSRF surface.

This is an UNOFFICIAL endpoint: it can rate-limit or change shape. Every call is
best-effort and returns None on any failure so the pipeline degrades to GSC-only
signals rather than breaking. Trends gives *relative* demand (0-100), never an
absolute search volume.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

_EXPLORE_URL = "https://trends.google.com/trends/api/explore"
_MULTILINE_URL = "https://trends.google.com/trends/api/widgetdata/multiline"
_TIMEOUT = httpx.Timeout(8.0)
# Trends prefixes its JSON responses with this XSSI guard.
_XSSI_PREFIX = ")]}'"


@dataclass
class TrendResult:
    """Relative demand for a keyword over the requested window."""

    index: int  # latest interest value, 0-100
    change_pct: int  # recent-vs-earlier change, signed percent
    series: list[int]  # the raw interest-over-time points


def _strip_xssi(text: str) -> str:
    text = text.lstrip()
    if text.startswith(_XSSI_PREFIX):
        text = text[len(_XSSI_PREFIX):]
    return text.lstrip()


def interest(
    keyword: str, country: str = "us", language: str = "en", timeframe: str = "today 12-m"
) -> TrendResult | None:
    """Interest-over-time for a keyword. None if Trends is unavailable."""
    keyword = keyword.strip()
    if not keyword:
        return None

    req = {
        "comparisonItem": [
            {"keyword": keyword, "geo": country.upper(), "time": timeframe}
        ],
        "category": 0,
        "property": "",
    }
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=False) as client:
            explore = client.get(
                _EXPLORE_URL,
                params={"hl": language, "tz": "0", "req": json.dumps(req)},
            )
            explore.raise_for_status()
            widgets = json.loads(_strip_xssi(explore.text)).get("widgets", [])
            timeseries = next(
                (w for w in widgets if w.get("id") == "TIMESERIES"), None
            )
            if not timeseries:
                return None

            data = client.get(
                _MULTILINE_URL,
                params={
                    "hl": language,
                    "tz": "0",
                    "req": json.dumps(timeseries["request"]),
                    "token": timeseries["token"],
                },
            )
            data.raise_for_status()
            rows = json.loads(_strip_xssi(data.text)).get("default", {}).get(
                "timelineData", []
            )
    except (httpx.HTTPError, ValueError, KeyError, StopIteration):
        return None

    values = [
        int(r["value"][0])
        for r in rows
        if isinstance(r.get("value"), list) and r["value"]
    ]
    if not values:
        return None

    index = values[-1]
    change_pct = _change_pct(values)
    return TrendResult(index=index, change_pct=change_pct, series=values)


def _change_pct(values: list[int]) -> int:
    """Percent change of the recent quarter vs the preceding quarter."""
    if len(values) < 8:
        return 0
    window = max(1, len(values) // 4)
    recent = sum(values[-window:]) / window
    prior = sum(values[-2 * window:-window]) / window
    if prior <= 0:
        return 0
    return round(100.0 * (recent - prior) / prior)
