"""LLM competitor discovery (backend port of scraping/seo_competitor_finder.py).

Given a target domain: scrape its homepage (+ an about/services page) for context,
ask the LLM to summarize the business, then ask it for the top N direct
competitors with a reason and confidence level. Uses OpenAI (settings.OPENAI_*),
mirroring the standalone script.

The result feeds competitor_service.discover_and_store(), which persists the
competitors into tracked_competitors (source='ai') and snapshots ranks.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.core.config import settings
from app.services import audit_service

# Internal links worth scraping for extra context (about / services / pricing).
_CONTEXT_LINK = re.compile(
    r"about|who-we-are|company|service|product|feature|solution|course|plan|pricing",
    re.I,
)
_SKIP_EXT = re.compile(r"\.(pdf|jpg|jpeg|png|gif|zip|mp4|css|js)$", re.I)


class DiscoveryError(RuntimeError):
    """Discovery failed (LLM unavailable, unreachable domain, bad reply)."""


# ── Scraping ────────────────────────────────────────────────────────────────

def _parse_page(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer"]):
        tag.decompose()
    title = soup.title.get_text(strip=True) if soup.title else ""
    md = soup.find("meta", attrs={"name": "description"})
    meta = (md.get("content") or "").strip() if md else ""
    headings = [
        h.get_text(" ", strip=True)
        for h in soup.find_all(["h1", "h2", "h3"])
        if h.get_text(strip=True)
    ][:20]
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:4000]
    return {"title": title, "meta": meta, "headings": headings, "text": text}


def _internal_links(base_url: str, html: str, limit: int = 2) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    base_host = urlparse(base_url).netloc
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        full = urljoin(base_url, a["href"]).split("#")[0].split("?")[0]
        if urlparse(full).netloc != base_host or full in seen:
            continue
        if _SKIP_EXT.search(full):
            continue
        hay = f"{full} {a.get_text(' ', strip=True)}"
        if _CONTEXT_LINK.search(hay):
            seen.add(full)
            out.append(full)
        if len(out) >= limit:
            break
    return out


def scrape_context(target_domain: str) -> str:
    """Fetch the homepage (+ up to 2 context pages) and return a text corpus.
    Uses the SSRF-guarded fetch. Raises DiscoveryError if unreachable."""
    host = target_domain.strip().replace("https://", "").replace("http://", "").strip("/")
    try:
        resp, final_url, _ = audit_service.safe_fetch(f"https://{host}")
    except Exception as exc:  # noqa: BLE001
        raise DiscoveryError(f"Could not fetch {host}: {exc}") from exc

    pages = [_parse_page(resp.text)]
    for link in _internal_links(final_url, resp.text, limit=2):
        try:
            r, _, _ = audit_service.safe_fetch(link)
            pages.append(_parse_page(r.text))
        except Exception:  # noqa: BLE001 - a broken subpage shouldn't abort
            continue

    parts = []
    for p in pages:
        parts.append(
            f"TITLE: {p['title']}\nMETA: {p['meta']}\n"
            f"HEADINGS: {' | '.join(p['headings'])}\nTEXT: {p['text']}"
        )
    return "\n\n---\n\n".join(parts)[:12000]


# ── OpenAI ──────────────────────────────────────────────────────────────────

def _openai_json(system: str, user: str, *, max_tokens: int = 4096):
    """Call OpenAI and parse a JSON value (object or array) from the reply.
    Handles the gpt-5 family (max_completion_tokens + reasoning_effort, default
    temperature) vs gpt-4.x (max_tokens + temperature=0)."""
    if not settings.openai_configured:
        raise DiscoveryError(
            "OpenAI is not configured. Set OPENAI_API_KEY in the backend .env."
        )
    from openai import OpenAI

    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    model = settings.OPENAI_MODEL
    kwargs: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if model.startswith("gpt-5"):
        kwargs["max_completion_tokens"] = max_tokens
        kwargs["reasoning_effort"] = "low"
    else:
        kwargs["max_tokens"] = max_tokens
        kwargs["temperature"] = 0

    from app.services import ai_debug

    ai_debug.log_call(
        "competitor_discovery._openai_json", system=system, user=user,
        params={k: v for k, v in kwargs.items() if k != "messages"},
    )

    # Echo the prompts to the terminal BEFORE the request runs, so they can be
    # inspected live in the console (unconditional, independent of AI_DEBUG).
    _sep = "=" * 60
    print(
        f"\n{_sep}\n[competitor_discovery] model={model}\n"
        f"--- SYSTEM PROMPT ---\n{system}\n"
        f"--- USER PROMPT ---\n{user}\n{_sep}",
        flush=True,
    )

    # Try twice: a transient hiccup (network, rate limit, a one-off malformed
    # reply) on the first attempt shouldn't surface to the user as an error when
    # a retry usually succeeds.
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            resp = client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            last_err = DiscoveryError(f"OpenAI request failed: {exc}")
            continue

        text = (resp.choices[0].message.content or "").strip()
        ai_debug.log_call("competitor_discovery._openai_json", output=text)
        if text.startswith("```"):
            text = text.strip("`")
            text = text.removeprefix("json").strip()
        # Extract the first JSON array or object.
        match = re.search(r"(\{.*\}|\[.*\])", text, re.S)
        if not match:
            last_err = DiscoveryError("OpenAI returned no parseable JSON.")
            continue
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            last_err = DiscoveryError("OpenAI returned invalid JSON.")
            last_err.__cause__ = exc

    raise last_err or DiscoveryError("OpenAI request failed.")


# ── Pipeline ────────────────────────────────────────────────────────────────

def _extract_business(corpus: str, domain: str) -> dict:
    system = "You are a SEO analyst. Reply with a single valid JSON object only."
    user = f"""From the scraped website content of "{domain}" below, build an accurate, self-contained business profile that another analyst could use to find this site's DIRECT COMPETITORS without seeing the website.

Only capture what actually defines who the competitors are. Leave out marketing slogans and generic store policies — they add noise and pull in the wrong competitors.

Return ONLY JSON with these keys:

- "business_description": 5-6 factual sentences — WHAT it sells and HOW it sells (online store / D2C brand / marketplace / retailer / agency), and the concrete, matchable distinctives (e.g. handmade, solid brass, bespoke). Do NOT include marketing claims, founding year, or vague slogans (e.g. "slow craft", "circular metallurgy", "established 2026").

- "industry": the COMMERCE MODEL only — a SHORT label of 3-6 words for the KIND of business / how it makes money, e.g. "D2C e-commerce / online retail", "online marketplace", "marketing agency", "SaaS". Do NOT put the product/niche here (that goes in "sector"). The model is what keeps competitors to the SAME KIND of business.

- "sector": the product niche / market sector as a SHORT label of 2-6 words — the SUBJECT the business operates in, e.g. "Handcrafted brass décor & pooja items", "Men's fashion apparel", "B2B logistics software". Keep it concise — no sentences, no marketing words.

- "business_type": exactly one of "products", "services", or "both". Decide by WHAT THE CUSTOMER ACTUALLY BUYS, not by the word "plan" or "shop" on the site:
    - "products" = tangible/physical goods that get shipped, or one-off downloadable goods (e.g. brass idols, apparel, furniture, an ebook or template sold once).
    - "services" = anything delivered as an ongoing service, subscription or account rather than a shipped good — web hosting, domain registration, VPS/cloud, SaaS / software subscriptions, memberships, online courses, bookings, and professional/agency work. A hosting company, SaaS tool or agency is ALWAYS "services" even though it sells "plans" or "packages".
    - "both" = a genuine mix of the two (e.g. a store that also sells installation/consulting, or a SaaS that also ships hardware). Do NOT use "both" just because a product store has shipping/returns/support policies.

- "primary_products": 4-8 CORE product categories that define a PRODUCTS business — what it mainly sells and is known for. CONSOLIDATE duplicates and spelling variants into one entry ("Ganesh"/"Ganesha" → one; don't list a product and its "set" separately). Use the site's own wording. EMPTY [] when business_type is "services" — a pure service/SaaS/hosting business has no physical products, so its offerings go in "services", not here.

- "secondary_products": 0-6 SUPPORTING or peripheral product categories — items the business also sells but that aren't core, including one-off / off-theme lines (e.g. a lone desk-toy line in a brass-idol store). CONSOLIDATE variants here too. These matter less for matching. EMPTY [] if there are none (always [] for a pure services business).

- "services": the service OFFERINGS the business provides.
    - FOR A "services" OR "both" BUSINESS this is the CORE list a competitor is matched on — the main service lines by TYPE, not by price tier (e.g. a web host: "shared hosting", "WordPress hosting", "VPS / cloud hosting", "domain registration", "email hosting", "website builder", "SSL certificates" — NOT "Premium plan ₹149/mo"). List 4-10 so the real competitors can be found.
    - FOR A PURE "products" STORE this is instead ONLY the extra offerings genuine COMPETITORS differ on — bespoke/custom commissions, corporate/bulk/wholesale orders, gift packing, customization.
    - EITHER WAY exclude generic store/site policies: shipping / free delivery, returns / refunds / damage support, order tracking, stock / restock notifications, payment methods, and support channels (WhatsApp, email, 24/7 chat). EMPTY [] only when there are genuinely none.

- "specialties": ONE short phrase (3-8 words) for the distinctive angle / what sets this business apart for matching — e.g. "Handcrafted religious & Vastu brass, antique finishes", "Vegan skincare, cruelty-free". "" if nothing distinctive.

- "price_positioning": one short phrase for the price tier — "budget" / "mid-range" / "premium" / "luxury" — plus a typical price range if visible (e.g. "premium, ~₹1,500-15,000"). "" if unclear.

- "target_audience": ONE concise sentence — who buys and in which market/segment.

- "market": the primary country/market (e.g. "India").

Do not invent anything not on the site. At least one of "primary_products" or "services" must be non-empty.

Website content:
{corpus}
"""
    data = _openai_json(system, user, max_tokens=2048)
    if not isinstance(data, dict):
        raise DiscoveryError("Business extraction returned an unexpected shape.")
    _merge_products(data)
    return data


def _merge_products(biz: dict) -> None:
    """Keep a combined, de-duplicated `products` (primary first, then secondary)
    so matching/verification/storage that read `products` keep working, while the
    UI shows the two groups separately. Backfills from an old `products` list when
    primary/secondary are absent (cached profiles from before the split)."""
    primary = [str(p).strip() for p in (biz.get("primary_products") or []) if str(p).strip()]
    secondary = [str(p).strip() for p in (biz.get("secondary_products") or []) if str(p).strip()]
    if not primary and not secondary and biz.get("products"):
        primary = [str(p).strip() for p in biz["products"] if str(p).strip()]
    biz["primary_products"] = primary
    biz["secondary_products"] = secondary
    # Combined list: primary then secondary, de-duped case-insensitively.
    combined, done = [], set()
    for p in primary + secondary:
        k = p.lower()
        if k not in done:
            done.add(k)
            combined.append(p)
    biz["products"] = combined


def _model_category(biz: dict) -> str:
    """Business model + niche as one "model — sector" line for competitor matching.
    The profile now stores the model (industry) and the niche (sector) separately
    for a cleaner UI, so recombine them here to keep match quality unchanged."""
    industry = str(biz.get("industry") or "").strip()
    sector = str(biz.get("sector") or "").strip()
    if industry and sector:
        return f"{industry} — {sector}"
    return industry or sector or "n/a"


def _competitor_prompt(biz: dict, market: str, count: int) -> str:
    primary = [str(p) for p in (biz.get("primary_products") or [])]
    secondary = [str(p) for p in (biz.get("secondary_products") or [])]
    products = [str(p) for p in (biz.get("products") or [])]
    services = [str(s) for s in (biz.get("services") or [])]
    primary_txt = "\n".join(primary) if primary else (("\n".join(products)) or "n/a")
    secondary_txt = "\n".join(secondary) if secondary else "n/a"
    services_txt = "\n".join(services) if services else "n/a"
    price = str(biz.get("price_positioning") or "n/a")
    specialties = str(biz.get("specialties") or "n/a")
    industry = _model_category(biz)
    btype = biz.get("business_type") or "n/a"
    # What ranking is primarily based on: core products for a store, services for a
    # pure service business (so the same rule serves any niche).
    primary_axis = (
        'the "Primary products" list (which of the same CORE products they also sell)'
        if (primary or products) else
        'the "Services / Features" list (which of the same services/features they also provide)'
    )
    return f"""Find the DIRECT competitors in {market} for the following business. List up to {count} candidates — fewer is expected and correct.

Business model / category:
{industry}

Business type: {btype}

Business Description:
{biz.get('business_description', 'n/a')}

PRIMARY products (core — match on these FIRST):
{primary_txt}

Secondary products (supporting / peripheral — minor weight):
{secondary_txt}

Services / offerings (for a service/SaaS business these are the CORE offerings to match on):
{services_txt}

Price positioning: {price}
Specialty / angle: {specialties}

Target Audience:
{biz.get('target_audience', 'n/a')}

STRICT RULES — read carefully:
1. A competitor must be the SAME KIND OF BUSINESS as the reference (same commerce model shown in "Business type" / "Business model" above) AND overlap its {primary_axis}. If the reference is a service/SaaS/hosting business, competitors are OTHER companies offering the same kind of service (NOT physical-goods stores); if the reference sells goods, competitors are other goods sellers (NOT service businesses). Judge by what the competitor's core business actually IS, not by one feature they also happen to offer.
2. NEVER return marketplaces, directories, social/video platforms or general resellers — exclude Amazon, Flipkart, Meesho, IndiaMART, Etsy, eBay, Snapdeal, Alibaba/AliExpress, Pinterest, YouTube, Facebook/Instagram, JustDial, Google, Wikipedia and the like. They rank for everything but are not direct competitors.
3. EXCLUDE companies from an adjacent or different category even if they overlap on one minor/bundled feature (e.g. for a brass décor store, exclude general fashion/apparel retailers; for a web host, exclude a plain blog or a pure design agency that merely mentions hosting).
4. Prefer competitors that match the PRICE POSITIONING above — a premium provider is not a direct competitor to a cheap mass-market one. Use this to break ties, not to drop a strong offering match.
5. Try hard to find {count} genuine same-category competitors. Include BOTH the best-known, established head-to-head rivals in this category and market AND smaller/newer/niche ones — do NOT omit an obvious major competitor (for a web host, that means names like GoDaddy, Namecheap, Bluehost, SiteGround, etc. when relevant). Only return fewer than {count} if you genuinely cannot find that many real same-category competitors. Never fill the gap with an adjacent or off-category company.
6. RANK BY OFFERING OVERLAP FIRST. A competitor that offers the SAME items as {primary_axis} is the strongest match and MUST rank at the top with the highest match_score — the MORE of those same offerings they share, the higher. The FIRST 2-3 competitors MUST be the strongest, most direct head-to-head rivals (broadest overlap in the same market). Only after overlap do price tier, services and audience act as tie-breakers. Do not rank a weak match above a strong one just because it is famous, but among equally strong matches put the most established direct rival first.

For each competitor provide: name, domain, reason, confidence_level, match_score.
- reason: name the SPECIFIC overlapping products/offerings from the lists above that this competitor also sells/provides (be concrete, e.g. "also offers shared hosting, domain registration and a website builder").
- confidence_level: one of Very High, High, Medium, Low.
- match_score: an INTEGER 0-100, driven PRIMARILY by overlap on {primary_axis}: roughly, the share of those items the competitor also offers. 100 = offers essentially the same set to the same audience; 80-99 = same category and most of the same items; 50-79 = same category, some overlap. Services/features and audience only nudge the score within those bands. A small niche brand selling the same items is still a strong match — score on overlap, not size or fame. Only score below 50 if it is a different/adjacent category (which you should not include at all).

Return a JSON object of this shape:
{{"competitors": [{{"name": "Example", "domain": "example.com", "reason": "...", "confidence_level": "High", "match_score": 88}}]}}
Return only valid JSON. No markdown, explanations or extra text."""


# Below this the model is told the match is a different/adjacent category, so we
# drop it. Kept at 50 (not higher) so genuine same-category NICHE brands — which
# the model tends to score in the 50s-60s — survive and the list can reach
# `limit`, instead of only the few famous names clearing the bar.
MIN_MATCH_SCORE = 50

# Marketplaces, directories and social/video platforms rank for every niche query
# but are never a direct competitor — dropped from the candidate pool outright
# (the LLM and SERP data both surface them). Matched on the registrable host.
_EXCLUDED_HOSTS = frozenset({
    "amazon.com", "amazon.in", "flipkart.com", "meesho.com", "indiamart.com",
    "etsy.com", "ebay.com", "snapdeal.com", "alibaba.com", "aliexpress.com",
    "pinterest.com", "youtube.com", "facebook.com", "instagram.com", "twitter.com",
    "x.com", "justdial.com", "google.com", "wikipedia.org", "reddit.com",
    "quora.com", "tradeindia.com", "exportersindia.com", "udaan.com",
    "shopify.com", "ajio.com", "myntra.com", "nykaa.com", "tatacliq.com",
})


def _is_excluded_host(domain: str) -> bool:
    """True if the domain is a marketplace/directory/social platform (exact host
    or a subdomain of one), which is never a direct competitor."""
    d = _norm_domain(domain)
    return any(d == h or d.endswith("." + h) for h in _EXCLUDED_HOSTS)

# Over-fetch strategy: ask for a bigger candidate pool, prune off-category and
# dead-domain rows, then keep the strongest `limit`. The pool is generous on
# purpose: niche categories plus dead-link attrition (invented domains that
# don't resolve) mean we need headroom to still reach `limit` live matches.
# Pool = max(limit + overfetch, min).
CANDIDATE_OVERFETCH = 25
MIN_CANDIDATE_POOL = 35

# Live-progress step labels (1-indexed), reported via the `progress` callback and
# surfaced in the UI while the async job runs.
DISCOVERY_STEPS = [
    "Creating your business profile",
    "Finding competitors",
    "Verifying your competitors",
]


def _parse_score(raw) -> int | None:
    """Coerce the LLM's match_score into an int in 0..100, or None if absent."""
    if raw is None or raw == "":
        return None
    try:
        n = int(round(float(raw)))
    except (TypeError, ValueError):
        return None
    return max(0, min(100, n))


def _norm_domain(raw: str) -> str:
    d = (raw or "").strip().lower()
    d = re.sub(r"^https?://", "", d).split("/")[0]
    return d[4:] if d.startswith("www.") else d


# How many liveness pings to run at once (each is a short SSRF-guarded fetch).
_LIVE_CHECK_WORKERS = 10

# Registrar / parking hosts a dropped or expired domain commonly lands on. A
# final URL on one of these means the domain itself is not a live business site.
_PARKING_HOSTS = (
    "sedo.com", "sedoparking.com", "hugedomains.com", "afternic.com", "dan.com",
    "bodis.com", "parkingcrew.net", "above.com", "uniregistry.com", "domainmarket.com",
    "buydomains.com", "namecheap.com", "godaddy.com/domainsearch", "voodoo.com",
    "domainnamesales.com", "undeveloped.com", "parklogic.com",
)

# Strong phrases found on parked / expired / for-sale landing pages. Kept specific
# so a real store that merely mentions "sale" isn't dropped.
_PARKING_PHRASES = (
    "this domain is for sale", "the domain name is for sale", "buy this domain",
    "this domain may be for sale", "domain is for sale", "domain for sale",
    "is for sale. make an offer", "interested in this domain", "parked free",
    "domain parking", "the domain has expired", "this domain has expired",
    "domain name has expired", "renew now to keep", "checkout the full domain details",
    "the owner of this domain", "get this domain", "buy now for",
)


def _looks_parked(final_url: str, body: str) -> bool:
    """True if the fetched page is a registrar parking / for-sale / expired page
    rather than a real site (common for expired domains that still resolve)."""
    host = (urlparse(final_url).netloc or "").lower().removeprefix("www.")
    if any(ph in host or host.endswith(ph) for ph in _PARKING_HOSTS):
        return True
    text = (body or "")[:6000].lower()
    # A parking page is tiny and dominated by the for-sale pitch; require a strong
    # phrase and keep the scan to the top of the document.
    return any(p in text for p in _PARKING_PHRASES)


# A nav/menu link that points at a store's catalog — fetched for a richer
# product list when the homepage itself is thin.
_CATALOG_LINK = re.compile(r"shop|store|product|collection|catalog|category|/buy", re.I)


def _profile_page(base_url: str, html: str) -> tuple[str, list[str]]:
    """Extract a competitor-profiling snippet from a page AND the catalog links on
    it. Unlike _parse_page this KEEPS nav/menu link text, because a store's menu
    is where it lists the product categories it sells (e.g. "Brass Idols | Diyas |
    Pooja Items"). Returns (snippet, catalog_urls). snippet is "" when the page has
    no real content (empty/JS-only shell)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    title = soup.title.get_text(strip=True) if soup.title else ""
    md = soup.find("meta", attrs={"name": "description"})
    meta = (md.get("content") or "").strip() if md else ""

    link_texts: list[str] = []
    catalog: list[str] = []
    seen_txt: set[str] = set()
    base_host = urlparse(base_url).netloc
    for a in soup.find_all("a", href=True):
        txt = " ".join(a.get_text(" ", strip=True).split())
        if 2 <= len(txt) <= 40 and txt.lower() not in seen_txt:
            seen_txt.add(txt.lower())
            link_texts.append(txt)
        full = urljoin(base_url, a["href"]).split("#")[0].split("?")[0]
        if (urlparse(full).netloc == base_host and _CATALOG_LINK.search(full)
                and not _SKIP_EXT.search(full) and full not in catalog):
            catalog.append(full)

    headings = [h.get_text(" ", strip=True) for h in soup.find_all(["h1", "h2", "h3"])
                if h.get_text(strip=True)][:14]
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:900]

    if not (title or meta or link_texts or text.strip()):
        return "", catalog  # empty / JS-only shell -> no usable content
    snippet = (
        f"TITLE: {title}\nMETA: {meta}\n"
        f"MENU/CATEGORIES: {' | '.join(link_texts[:45])}\n"
        f"HEADINGS: {' | '.join(headings)}\nTEXT: {text}"
    )
    return snippet, catalog[:4]


def _check_site(domain: str) -> tuple[bool, str]:
    """(is_live, profile_snippet). Live = resolves, responds, not parked, and has
    real readable content. Profiles the competitor from its homepage PLUS one
    catalog/shop page (menu categories + products), so verification judges what it
    ACTUALLY sells. A page with no readable content (dead shell / 404 body) is
    treated as not-live so dead/empty URLs never reach the list."""
    for scheme in ("https", "http"):
        try:
            resp, final_url, _ = audit_service.safe_fetch(f"{scheme}://{domain}")
        except audit_service.FetchError:
            continue  # try http, then treat as dead
        except (audit_service.BlockedUrlError, audit_service.InvalidUrlError):
            return False, ""  # unresolvable / blocked / malformed -> not a live site
        except Exception:  # noqa: BLE001 - unexpected error: don't drop a real site
            return True, ""
        if resp.status_code >= 500:
            continue  # server error -> try http, else dead
        try:
            if _looks_parked(final_url, resp.text):
                return False, ""  # registrar parking / for-sale / expired page
            snippet, catalog = _profile_page(final_url, resp.text)
            if not snippet:
                return False, ""  # empty/JS-only shell -> effectively dead
            # Pull one catalog/shop page for a stronger product signal (homepages
            # are often thin). Best-effort — skip on any error.
            for url in catalog[:1]:
                try:
                    r2, u2, _ = audit_service.safe_fetch(url)
                    extra, _ = _profile_page(u2, r2.text)
                    if extra:
                        snippet += "\n--CATALOG PAGE--\n" + extra
                except Exception:  # noqa: BLE001
                    pass
            return True, snippet
        except Exception:  # noqa: BLE001 - body read failed: trust the live response
            return True, ""
    return False, ""


def _filter_live(competitors: list[dict]) -> list[dict]:
    """Keep only competitors whose domain is reachable, checked concurrently, and
    attach each kept one's homepage snippet (transient key "_snippet") for the
    cross-verification step."""
    if not competitors:
        return competitors
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=_LIVE_CHECK_WORKERS) as ex:
        checks = list(ex.map(lambda c: _check_site(c["domain"]), competitors))
    kept = []
    for c, (ok, snippet) in zip(competitors, checks):
        if ok:
            c["_snippet"] = snippet
            kept.append(c)
    return kept


# Content cross-check thresholds. The verification model scores conservatively
# (homepage snippets are thin), so a genuine same-category seller can score in
# the 30s — keep those, and only hard-drop clear off-category / negligible cases.
# VERIFY_MIN_OVERLAP: same-category candidates at/above this are always kept.
# VERIFY_OFFCAT_KEEP: an off-category-labelled site needs at least this overlap.
# VERIFY_FLOOR: below this (any label) the overlap is negligible -> drop.
VERIFY_MIN_OVERLAP = 40
VERIFY_OFFCAT_KEEP = 55
VERIFY_FLOOR = 30
# Verify the whole live pool (LLM candidates + SERP-ranked ones).
VERIFY_MAX_CANDIDATES = 45


def _verify_competitors(biz: dict, candidates: list[dict]) -> list[dict]:
    """Cross-verify candidates against their REAL homepage content.

    The first LLM pass sometimes claims a site sells our products when it doesn't
    (e.g. a fashion/sarees store tagged as a brass brand). Using each site's
    scraped homepage snippet, ONE LLM call judges genuine same-category product
    overlap: it drops off-category sites, re-scores the rest by ACTUAL overlap,
    and rewrites each reason with what the competitor really sells. Candidates we
    couldn't scrape keep their original score (we never drop a site we couldn't
    read). Also strips the transient "_snippet" key."""
    verifiable = [c for c in candidates if c.get("_snippet")][:VERIFY_MAX_CANDIDATES]
    if not verifiable:
        for c in candidates:
            c.pop("_snippet", None)
        return candidates

    # Build the reference OFFERINGS the candidate must overlap. For a goods store
    # that's its products; for a service/SaaS/hosting business it's its services
    # (products are empty); for "both" it's the two combined. Measuring overlap
    # against PRODUCTS only used to drop every real competitor of a service
    # business (e.g. GoDaddy for a web host), so key off business_type instead.
    btype = (biz.get("business_type") or "").strip().lower()
    is_service = btype in ("service", "services")
    products = [str(p) for p in (biz.get("products") or []) if str(p).strip()]
    services = [str(s) for s in (biz.get("services") or []) if str(s).strip()]
    if is_service:
        offerings = services or products
        kind_desc = ("a company that provides the SAME KIND of service "
                     "(the same hosting / SaaS / software / agency / course type), NOT a physical-goods store")
        offcat_hint = ("a plain physical-goods store, a blog/content/directory site, "
                       "or a company in a clearly different service category")
    elif btype == "both":
        offerings = products + services
        kind_desc = "a business offering the SAME KIND of products and/or services"
        offcat_hint = "a blog/content/directory site or a business in a clearly different category"
    else:  # products (default)
        offerings = products or services
        kind_desc = "an online store / brand / retailer selling the SAME KIND of goods"
        offcat_hint = ("a pure services business (decorator, booking, agency), a blog/content/"
                       "directory site, or a store in a different/adjacent category")
    offerings_txt = "\n".join(offerings) or "n/a"
    industry = _model_category(biz)
    price = str(biz.get("price_positioning") or "n/a")
    # Cap each candidate's profile so a big pool stays within the token budget.
    blocks = [f"[{i}] domain: {c['domain']}\n{c['_snippet'][:1600]}"
              for i, c in enumerate(verifiable, 1)]

    system = "You verify competitor matches from scraped homepages. Reply with only valid JSON."
    user = f"""Reference business — model / category: {industry}
Business type: {btype or 'n/a'}
Reference OFFERINGS (the products and/or services this business is built on):
{offerings_txt}
Reference PRICE POSITIONING: {price}

Below are candidate competitor sites, each with a snippet scraped from its REAL homepage. For EACH candidate, judge FROM ITS OWN SNIPPET (not assumptions) whether it is a genuine DIRECT competitor: it must be the SAME KIND OF BUSINESS as the reference ({kind_desc}) whose PRIMARY business overlaps the reference OFFERINGS above.

Set same_category = FALSE ONLY for a site whose PRIMARY business is {offcat_hint}, or a marketplace / general reseller (Amazon, Flipkart, Meesho, IndiaMART, Etsy, Pinterest, YouTube and the like). Do NOT mark a site off-category merely because it also offers some extra things — judge by its PRIMARY business, and do NOT treat a genuine same-kind competitor as off-category.

Candidates:
{chr(10).join(blocks)}

Return ONLY JSON: {{"results":[{{"domain":"...","their_products":["..."],"their_category":"short label","overlap_score":0-100,"same_category":true|false}}]}}
- their_products: 3-8 concrete products/offerings actually visible in THAT candidate's snippet.
- overlap_score: 0-100 = how much their ACTUAL offerings overlap the reference OFFERINGS — the MORE of the same offerings they provide, the higher (a competitor matching most of them scores highest, so the strongest direct rivals rank at the top). Same price tier is a stronger match.
- same_category: per the rule above.
Judge every candidate. No markdown, no extra text."""

    try:
        data = _openai_json(system, user, max_tokens=4096)
    except DiscoveryError:
        for c in candidates:  # verification unavailable -> keep the unverified list
            c.pop("_snippet", None)
        return candidates

    rows = data.get("results") if isinstance(data, dict) else (data if isinstance(data, list) else [])
    by_domain = {
        _norm_domain(r["domain"]): r
        for r in (rows or []) if isinstance(r, dict) and r.get("domain")
    }

    out = []
    for c in candidates:
        c.pop("_snippet", None)
        v = by_domain.get(_norm_domain(c["domain"]))
        if v is None:
            out.append(c)  # not verified (no snippet / no result) -> keep as-is
            continue
        overlap = _parse_score(v.get("overlap_score"))
        off_category = v.get("same_category") is False
        # Rank/keep by ACTUAL product overlap, not the model's category LABEL: it
        # mislabels niche brass sellers ("puja accessories", "religious
        # marketplace") as a different category even when they sell the same
        # products. Keep same-category sellers down to a low score (thin homepages
        # under-score real matches); require stronger overlap to keep a site the
        # model flags as a different business; drop negligible overlap outright.
        if overlap is not None:
            if overlap < VERIFY_FLOOR:
                continue  # negligible real overlap (dead/empty/off-topic) -> drop
            if off_category and overlap < VERIFY_OFFCAT_KEEP:
                continue  # different business and not a strong overlap -> drop
            # else: same-category >= FLOOR, or off-category with strong overlap -> keep
        elif off_category:
            continue  # no score and flagged a different business -> drop
        if overlap is not None:
            c["match_score"] = overlap  # trust the content-verified score
        their = [str(p).strip() for p in (v.get("their_products") or []) if str(p).strip()]
        if their:
            c["reason"] = ("Offers " + ", ".join(their[:6]))[:600]
        out.append(c)
    return out


def extract_business_profile(target_domain: str) -> dict:
    """Scrape the domain and return its LLM business profile. This is the slow,
    expensive first step of discovery; callers may cache the result and pass it
    back to `discover(..., business=...)` to skip it next time."""
    corpus = scrape_context(target_domain)
    return _extract_business(corpus, target_domain)


def discover(
    target_domain: str, *, limit: int = 10, extra_services: list[str] | None = None,
    business: dict | None = None, market: str | None = None,
    extra_candidates: list[str] | None = None, exclude_domains: list[str] | None = None,
    progress=None,
) -> dict:
    """Summarize the business (or reuse a cached `business`) and return top
    competitors.
    `extra_services` are appended to the scraped services (for features not yet on
    the site, e.g. "AI-based Mains answer evaluation") to steer the match.
    `business` : a previously extracted profile — when given, scraping and
                 extraction are skipped (fast path).
    `market`   : force the competitor market (e.g. the domain's configured
                 country); when omitted, the LLM-guessed market is used.
    `extra_candidates` : real competitor domains from SERP data (who actually
                 ranks for the site's keywords). These are merged into the LLM
                 candidate pool and go through the SAME liveness + content
                 verification, so genuine competitors fill the list even when the
                 model can't name enough real ones.
    `exclude_domains` : domains that must never appear as competitors (e.g. the
                 workspace's own OTHER tracked domains), so a newly-added sibling
                 property is never surfaced as a competitor of this one.
    `progress` : optional callback(step:int, label:str) reporting pipeline stage
                 (1..3) for the live progress UI. Errors in it are ignored.
    Returns {"business": {...}, "market": str, "competitors": [{name, domain,
    reason, confidence_level}]}."""
    def _report(step: int, label: str) -> None:
        if progress:
            try:
                progress(step, label)
            except Exception:  # noqa: BLE001 - progress must never break discovery
                pass

    _report(1, DISCOVERY_STEPS[0])  # step 1: build the business profile
    biz = dict(business) if business else extract_business_profile(target_domain)
    _merge_products(biz)  # ensure primary/secondary/combined products (also for cached)

    if extra_services:
        scraped = biz.get("services") or []
        existing = {str(s).lower() for s in scraped}
        biz["services"] = scraped + [
            s.strip() for s in extra_services
            if s.strip() and s.strip().lower() not in existing
        ]

    market = (market or "").strip() or str(biz.get("market") or "India").strip() or "India"

    system = (
        "You are an SEO competitor-research analyst. Reply with only valid JSON, "
        "no markdown."
    )
    # Over-fetch a candidate pool, then prune low-score / off-category rows and
    # keep the strongest `limit`. A bigger pool means the final list is more
    # likely to reach `limit` with genuine matches (instead of padding).
    _report(2, DISCOVERY_STEPS[1])  # step 2: find competitor candidates
    pool = max(limit + CANDIDATE_OVERFETCH, MIN_CANDIDATE_POOL)
    data = _openai_json(system, _competitor_prompt(biz, market, pool), max_tokens=8192)

    # Accept a bare array or an object wrapping the array.
    rows = data if isinstance(data, list) else (
        data.get("competitors") or data.get("results") or []
    )
    self_host = _norm_domain(target_domain)
    # Never surface the target itself or any of the workspace's own other domains
    # (a freshly-added sibling property must not come back as a "competitor").
    excluded = {self_host} | {
        _norm_domain(d) for d in (exclude_domains or []) if _norm_domain(d)
    }
    out, seen = [], set()
    for c in rows:
        if not isinstance(c, dict):
            continue
        dom = _norm_domain(c.get("domain") or "")
        if not dom or "." not in dom or dom in excluded or dom in seen:
            continue
        if _is_excluded_host(dom):
            continue  # marketplace / directory / social platform — never a competitor
        confidence = (c.get("confidence_level") or c.get("confidence") or "").strip()
        score = _parse_score(c.get("match_score"))
        # Prune the pool: below 60 the model is told the match is too weak /
        # off-category, and "Low" confidence means not a real direct competitor.
        if score is not None and score < MIN_MATCH_SCORE:
            continue
        if confidence.lower() == "low":
            continue
        seen.add(dom)
        out.append({
            "name": (c.get("name") or dom).strip()[:255],
            "domain": dom,
            "reason": (c.get("reason") or "").strip()[:600],
            "confidence_level": confidence[:20],
            "match_score": score,
        })

    # Merge in real SERP-ranked competitor domains (who actually rank for this
    # site's keywords). They get a neutral starting score; content verification
    # then scores them by real product overlap, so genuine ones rise and SERP
    # noise (a grocery app that ranked for one keyword) is dropped.
    for dom in (extra_candidates or []):
        d = _norm_domain(dom)
        if not d or "." not in d or d in excluded or d in seen:
            continue
        if _is_excluded_host(d):
            continue  # SERP often surfaces marketplaces/directories — drop them
        seen.add(d)
        out.append({
            "name": d, "domain": d, "reason": "",
            "confidence_level": "", "match_score": 60,
        })

    # Strongest match first (unscored rows last) so verification checks the best
    # candidates first within its cap.
    out.sort(key=lambda c: (c["match_score"] is None, -(c["match_score"] or 0)))
    # Drop dead/unreachable/parked domains (the LLM sometimes invents ones that
    # don't resolve) and capture each live site's homepage snippet.
    # Step 3 "Verifying your competitors" covers visiting live sites, matching
    # products and final ranking — one phase to the user.
    _report(3, DISCOVERY_STEPS[2])  # visiting each competitor's live site
    out = _filter_live(out)
    # Cross-verify against the competitors' REAL homepages: drop off-category
    # sites (e.g. a fashion store mislabeled as a brass brand) and re-score by
    # actual product overlap, so only genuinely similar competitors remain.
    _report(3, DISCOVERY_STEPS[2])  # matching products (still "Verifying")
    out = _verify_competitors(biz, out)
    # Re-rank by the verified overlap score, then cap.
    _report(3, DISCOVERY_STEPS[2])  # ranking the best matches (still "Verifying")
    out.sort(key=lambda c: (c["match_score"] is None, -(c["match_score"] or 0)))
    out = out[:limit]
    return {"business": biz, "market": market, "competitors": out}
