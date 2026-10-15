"""Is a keyword a sensible target for OUR domain?

Two cheap, explainable rules (no API calls):

* competitor brand / navigational terms (e.g. a rival's host label or a curated
  brand term) — searchers want that brand; we can't win them;
* local "near me" intent — the SERP is local businesses, not an online site —
  UNLESS this domain is itself a local business (`is_local_ok`).

Brand detection = the keyword (spaces removed) contains a competitor's host label
or one of the per-domain `brand_terms` passed in. Our own brand is never treated
as a competitor. The brand list is NOT global/hardcoded any more — it comes from
the domain's own settings (see services/domain_settings.py), so this works for
any niche. `KNOWN_BRAND_TERMS` remains only as an opt-in seed for the UPSC space.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Opt-in seed of brand terms for the UPSC space — NO LONGER applied by default.
# A domain's own brand_terms (domain_settings) drive brand filtering instead.
KNOWN_BRAND_TERMS = frozenset({
    "drishti", "drishtiias", "visionias", "vajiram", "vajirao", "byju", "unacademy",
    "testbook", "prepp", "physicswallah", "pwlive", "nextias", "chahalacademy",
    "sleepyclasses", "insightsias", "insightsonindia", "superkalam", "studyiq",
    "adda247", "forumias", "shankarias", "khanglobal", "iasbaba",
})

_LOCAL_RE = re.compile(r"\bnear me\b|\bnearby\b", re.I)


def _label(host: str) -> str:
    h = re.sub(r"^https?://", "", (host or "").strip().lower()).split("/")[0]
    h = h[4:] if h.startswith("www.") else h
    return h.split(".")[0]


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def competitor_brand(
    keyword: str, *, own_host: str, competitor_hosts: Iterable[str] = (),
    brand_terms: Iterable[str] = (),
) -> str | None:
    """The competitor brand a keyword targets, or None.

    Brands come from the competitor hosts and the per-domain `brand_terms`; no
    global list is applied, so an off-niche term never trips the filter.
    """
    own = _compact(_label(own_host))
    kw = _compact(keyword)
    # Host-derived labels need >= 5 chars (short labels like "next" are common
    # words); curated per-domain terms are hand-picked, so they're trusted as-is.
    terms = {(_label(h), 5) for h in competitor_hosts if h}
    terms |= {(t, 1) for t in brand_terms if t}
    for term, min_len in sorted(terms, key=lambda x: len(x[0]), reverse=True):
        t = _compact(term)
        if len(t) >= min_len and t != own and t not in own and t in kw:
            return term
    return None


def irrelevance_reason(
    keyword: str, *, own_host: str, competitor_hosts: Iterable[str] = (),
    brand_terms: Iterable[str] = (), is_local_ok: bool = False,
) -> str | None:
    """Why a keyword isn't a sensible target for us (None = keep it)."""
    brand = competitor_brand(
        keyword, own_host=own_host, competitor_hosts=competitor_hosts, brand_terms=brand_terms)
    if brand:
        return f"competitor brand ({brand})"
    if not is_local_ok and _LOCAL_RE.search(keyword):
        return "local 'near me' intent"
    return None
