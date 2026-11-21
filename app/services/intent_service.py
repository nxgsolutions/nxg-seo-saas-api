"""Hybrid search-intent resolution: rules -> SERP signal -> AI fallback.

Layer 1 (keyword_scoring, pure & free) classifies the bulk of keywords. Layer 2
reads an ALREADY-FETCHED organic SERP (no new network call) to confirm or break
ties on ambiguous head terms. Layer 3 (openai_ai.write_json, local model
fallback) resolves the genuinely ambiguous residual, and ONLY when a caller opts
in (`allow_ai=True`) and a provider is available — it is never run per-keyword in
the 100-keyword research loop, to keep that path fast and free.

The four intents mirror `keyword_scoring.INTENTS` minus 'unknown', which stays a
valid result only when every layer abstains.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.services import keyword_scoring, openai_ai

logger = logging.getLogger(__name__)

_CLASSES = ("informational", "navigational", "commercial", "transactional")


@dataclass(frozen=True)
class IntentResult:
    intent: str          # one of _CLASSES, or 'unknown'
    confidence: float    # 0.0 - 1.0
    source: str          # 'rule' | 'serp' | 'ai'


def _get(hit, key: str):
    """Read `key` from a SERP hit that may be a dict or an ORM object."""
    if isinstance(hit, dict):
        return hit.get(key)
    return getattr(hit, key, None)


# SERP fingerprints -> an intent vote (domain substrings + title/url keywords).
_SHOP_DOMAINS = ("amazon.", "flipkart.", "ebay.", "etsy.", "walmart.", "myntra.",
                 "alibaba.", "bestbuy.", "target.", "shopify.")
_REVIEW_DOMAINS = ("g2.com", "capterra.", "trustpilot.", "tripadvisor.", "yelp.",
                   "producthunt.", "softwareadvice.", "trustradius.")
_INFO_DOMAINS = ("wikipedia.org", "quora.com", ".edu", "medium.com", "stackoverflow.",
                 "stackexchange.", "reddit.com", "healthline.", "webmd.", ".gov")

_TXN_WORDS = ("price", "buy", "shop", "sale", "order", "deal", "discount", "pricing",
              "for sale", "add to cart", "checkout")
_COM_WORDS = ("best", "top ", "review", " vs ", "compare", "comparison", "alternative")
_INFO_WORDS = ("how to", "what is", "guide", "tutorial", "meaning", "definition",
               "examples", "ways to", "tips", "explained")


def from_rules(keyword: str) -> IntentResult:
    """Layer 1: deterministic rule classifier."""
    intent, conf = keyword_scoring.classify_intent_scored(keyword)
    return IntentResult(intent, conf, "rule")


def from_serp(items) -> IntentResult | None:
    """Layer 2: vote an intent from the page types in an organic SERP (top 10).

    `items` is a list of dicts/ORM rows with `domain`, `title`, `url`. Returns
    None when the SERP offers no usable signal."""
    top = list(items or [])[:10]
    if not top:
        return None
    votes = {"transactional": 0.0, "commercial": 0.0, "informational": 0.0, "navigational": 0.0}
    domains: list[str] = []
    for hit in top:
        dom = (_get(hit, "domain") or "").lower()
        title = (_get(hit, "title") or "").lower()
        url = (_get(hit, "url") or "").lower()
        hay = f"{title} {url}"
        if dom:
            domains.append(dom)
        if any(d in dom for d in _SHOP_DOMAINS) or any(w in hay for w in _TXN_WORDS):
            votes["transactional"] += 1
        if any(d in dom for d in _REVIEW_DOMAINS) or any(w in hay for w in _COM_WORDS):
            votes["commercial"] += 1
        if any(d in dom for d in _INFO_DOMAINS) or any(w in hay for w in _INFO_WORDS):
            votes["informational"] += 1
    # One domain dominating the SERP => navigational (a brand/entity query).
    if domains:
        top_dom = max(set(domains), key=domains.count)
        if domains.count(top_dom) >= 4:
            votes["navigational"] += 3.0
    intent = max(votes, key=votes.get)
    score = votes[intent]
    if score <= 0:
        return None
    # 1 vote -> 0.55, scaling to a 0.85 ceiling.
    confidence = min(0.85, 0.45 + 0.10 * score)
    return IntentResult(intent, round(confidence, 2), "serp")


_AI_SYSTEM = (
    "You are an SEO search-intent classifier. Classify the user's search keyword "
    "into EXACTLY one of: informational, navigational, commercial, transactional. "
    "Definitions: informational = wants to learn or understand something; "
    "navigational = wants a specific site, brand, or page; commercial = comparing "
    "options before buying (best, review, vs, alternatives); transactional = ready "
    "to act, buy, sign up, download, or visit. Respond with ONLY a JSON object: "
    '{"intent": "<one of the four>", "confidence": <float 0-1>}.'
)


def from_ai(keyword: str, *, category: str | None = None,
            serp_titles: list[str] | None = None) -> IntentResult | None:
    """Layer 3: LLM classification (OpenAI, local model fallback). Returns None
    when no provider is available or the reply is unusable. Cheap: ~60 tokens."""
    if not openai_ai.any_available():
        return None
    user = f"Keyword: {keyword}"
    if category:
        user += f"\nBusiness category: {category}"
    if serp_titles:
        joined = "; ".join(t for t in serp_titles[:5] if t)
        if joined:
            user += f"\nTop search result titles: {joined}"
    user += "\n\nReturn the JSON."
    try:
        data, _model = openai_ai.write_json(
            _AI_SYSTEM, user, max_tokens=60, temperature=0.0, timeout=30.0
        )
    except openai_ai.OpenAiError as exc:
        logger.info("AI intent unavailable for %r: %s", keyword, exc)
        return None
    intent = str(data.get("intent", "")).strip().lower()
    if intent not in _CLASSES:
        return None
    try:
        conf = float(data.get("confidence", 0.7))
    except (TypeError, ValueError):
        conf = 0.7
    return IntentResult(intent, max(0.0, min(1.0, conf)), "ai")


_BATCH_SYSTEM = (
    "You are an SEO search-intent classifier. For EACH keyword in the list, assign "
    "EXACTLY one intent: informational (wants to learn), navigational (wants a "
    "specific site/brand), commercial (comparing options before buying — best, "
    "review, vs, for <use>), or transactional (ready to buy/order/visit, incl. "
    "product and 'near me'/local queries). Use the business context to judge "
    "ambiguous product nouns. Respond with ONLY a JSON object mapping each keyword "
    'EXACTLY as given to its intent, e.g. {"buddha statue for home": "transactional"}.'
)


def classify_batch(
    keywords: list[str], *, category: str | None = None, topic: str | None = None,
    max_items: int = 80, use_local: bool = False,
) -> dict[str, "IntentResult"]:
    """Classify MANY keywords in ONE LLM call, business-category aware. Returns
    {normalized_keyword: IntentResult} for the ones the model classified; empty
    when no provider is available. Used for the residual keywords the
    deterministic rules leave as 'unknown' — one cheap call per batch.

    Provider: default is the app order (OpenAI gpt-4o-mini → local model) — used in
    production. `use_local=True` forces the local Ollama model only (free), e.g. for
    bulk backfills."""
    items = [k for k in dict.fromkeys(k.strip() for k in keywords) if k][:max_items]
    if not items:
        return {}
    ctx = ""
    if category:
        ctx += f"Business category: {category}\n"
    if topic:
        ctx += f"Business topics/products: {topic}\n"
    user = (
        ctx
        + "Classify each of these search keywords. Return a JSON object mapping the "
        "EXACT keyword text to one intent.\nKeywords:\n"
        + "\n".join(f"- {k}" for k in items)
    )
    try:
        if use_local:
            from app.services import local_ai
            if not local_ai.available():
                return {}
            data = local_ai.chat_json(
                _BATCH_SYSTEM, user, timeout=180.0, think=False,
                options={"temperature": 0.0, "num_predict": 2000},
            )
        else:
            if not openai_ai.any_available():
                return {}
            data, _model = openai_ai.write_json(
                _BATCH_SYSTEM, user, max_tokens=1500, temperature=0.0, timeout=90.0
            )
    except Exception as exc:  # noqa: BLE001 — OpenAiError / LocalAiError / transport
        logger.info("Batch AI intent unavailable (%d keywords): %s", len(items), exc)
        return {}
    norm_map = {keyword_scoring.normalize_keyword(k): k for k in items}
    out: dict[str, IntentResult] = {}
    for key, val in (data or {}).items():
        intent = (
            str(val.get("intent", "")) if isinstance(val, dict) else str(val)
        ).strip().lower()
        if intent not in _CLASSES:
            continue
        nk = keyword_scoring.normalize_keyword(str(key))
        if nk in norm_map:
            out[nk] = IntentResult(intent, 0.7, "ai")
    return out


def resolve(
    keyword: str,
    *,
    serp_items=None,
    category: str | None = None,
    serp_titles: list[str] | None = None,
    allow_ai: bool = False,
) -> IntentResult:
    """Hybrid resolution. Rules first; SERP confirms or overrides; AI resolves the
    ambiguous residual only when `allow_ai` is set and a provider is available.

    Pass `serp_items` (an organic SERP already fetched for this keyword) to enable
    Layer 2. `allow_ai` should be True only off the hot path (e.g. the seed in the
    background SERP job), never inside the bulk keyword loop."""
    rule = from_rules(keyword)
    serp = from_serp(serp_items) if serp_items else None

    # Confident rule: trust it when the SERP agrees or is silent (boost on agree).
    if rule.intent != "unknown" and rule.confidence >= 0.8:
        if serp and serp.intent == rule.intent:
            return IntentResult(rule.intent, round(min(0.98, rule.confidence + 0.10), 2), "rule")
        if serp is None or serp.confidence < 0.65:
            return rule

    # A clear SERP opinion wins (also overrides a confident-but-contradicted rule).
    if serp and serp.confidence >= 0.60:
        return serp

    # Still ambiguous -> AI (opt-in) for the residual.
    if allow_ai and rule.confidence < 0.60:
        titles = serp_titles or [str(_get(h, "title") or "") for h in (serp_items or [])]
        ai = from_ai(keyword, category=category, serp_titles=titles)
        if ai is not None:
            return ai

    return rule
