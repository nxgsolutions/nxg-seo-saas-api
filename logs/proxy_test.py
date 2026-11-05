"""Standalone proxy fetch + SEO report. No app imports — httpx + bs4 + stdlib.

Reads the proxy from the PROXY constant / $PROXY / backend/.env SERP_PROXIES.
Fetches the target via the PROXY and DIRECT, then writes a READABLE Markdown SEO
report (scraped_report.md) that:
  - shows each important field and WHERE it came from (HTML tag / meta / Open
    Graph / JSON-LD) — key for Shopify-style storefronts that render via JS, so
    the static HTML body is thin and the real product data lives in JSON-LD;
  - lists which SEO elements are present, weak, or missing.
Also saves the raw HTML (scraped_full.html) for reference. Never prints credentials.

Run:  D:\seo\backend\.venv\Scripts\python.exe proxy_test.py
"""
import json
import os
import re
import sys
from datetime import datetime
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # print ₹ etc. safely

# ─────────────────────────── CONFIG ───────────────────────────
# Leave PROXY blank to use SERP_PROXIES from backend/.env (first entry).
# Or hardcode one:  "host:port:user:pass"  /  "http://user:pass@host:port"
PROXY = "31.59.20.176:6754:qdqwdoqh:0zcowc72mt6i"
ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
TARGET_URL = ("https://www.peetalkraft.com/products/"
              "peetalkraft-wooden-handle-brass-table-diya-for-puja-aarti-and-shop-size-21x5x9-cm-weight-195-grams")
HTML_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scraped_full.html")
REPORT_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scraped_report.md")
ROTATION_CHECKS = 3     # exit-IP checks (Rotating Residential shows a new IP each time)
TIMEOUT = 30
# ───────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}
OK, WARN, BAD = "✅", "⚠️", "❌"


# ── proxy / fetch ────────────────────────────────────────────────────────────

def read_proxy() -> str:
    if PROXY.strip():
        return PROXY.strip()
    if os.environ.get("PROXY"):
        return os.environ["PROXY"].strip()
    try:
        for line in open(ENV_PATH, encoding="utf-8"):
            if line.strip().startswith("SERP_PROXIES="):
                raw = line.split("=", 1)[1].strip().strip('"').strip("'")
                return (raw.split(",")[0] or "").strip()
    except FileNotFoundError:
        pass
    return ""


def to_proxy_url(raw: str):
    raw = raw.strip()
    if not raw:
        return None
    if raw.startswith(("http://", "https://")):
        return raw
    p = raw.split(":")
    if len(p) != 4:
        raise SystemExit(f"proxy must be host:port:user:pass or a full URL — got {len(p)} parts")
    return f"http://{p[2]}:{p[3]}@{p[0]}:{p[1]}"


def exit_ip(proxy=None):
    try:
        with httpx.Client(timeout=TIMEOUT, proxy=proxy) as c:
            return c.get("https://api.ipify.org").text.strip()
    except Exception as e:
        return f"ERR {type(e).__name__}: {str(e)[:80]}"


def fetch(url, proxy=None):
    """Returns (status, final_url, html, headers) or (None, None, None, {})."""
    try:
        with httpx.Client(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True, proxy=proxy) as c:
            r = c.get(url)
            print(f"  status {r.status_code} | bytes {len(r.content)} | cf-ray {r.headers.get('cf-ray','-')}")
            return r.status_code, str(r.url), r.text, dict(r.headers)
    except Exception as e:
        print(f"  ERROR {type(e).__name__}: {str(e)[:120]}")
        return None, None, None, {}


# ── analysis helpers ─────────────────────────────────────────────────────────

def meta_name(soup, name):
    t = soup.find("meta", attrs={"name": re.compile(f"^{name}$", re.I)})
    return (t.get("content") or "").strip() if t else ""


def meta_prop(soup, prop):
    t = soup.find("meta", attrs={"property": re.compile(f"^{re.escape(prop)}$", re.I)})
    return (t.get("content") or "").strip() if t else ""


def all_jsonld_nodes(soup):
    nodes = []
    for tag in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        stack = [data]
        while stack:
            n = stack.pop()
            if isinstance(n, dict):
                nodes.append(n)
                stack.extend(v for v in n.values() if isinstance(v, (dict, list)))
            elif isinstance(n, list):
                stack.extend(n)
    return nodes


def node_types(node):
    t = node.get("@type")
    return [t] if isinstance(t, str) else (t or [])


def first_of_type(nodes, wanted):
    for n in nodes:
        if wanted in node_types(n):
            return n
    return None


def plain(text, limit=None):
    txt = BeautifulSoup(text or "", "html.parser").get_text(" ", strip=True)
    return txt[:limit] if limit else txt


def visible_words(soup, strip_chrome=False):
    s = BeautifulSoup(str(soup), "html.parser")
    for t in s(["script", "style", "noscript", "template"]):
        t.decompose()
    if strip_chrome:
        for t in s(["nav", "header", "footer", "aside", "form"]):
            t.decompose()
    return len(s.get_text(" ", strip=True).split())


def host_of(url):
    net = urlparse(url).netloc.lower()
    return net[4:] if net.startswith("www.") else net


# ── report builder ───────────────────────────────────────────────────────────

def build_report(html, url, fetched_via, status, headers):
    soup = BeautifulSoup(html, "html.parser")
    L = []  # markdown lines
    rows = []  # SEO checklist rows: (element, status_icon, value, source, why)

    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    meta_desc = meta_name(soup, "description")
    robots = meta_name(soup, "robots")
    viewport = meta_name(soup, "viewport")
    canonical_tag = soup.find("link", attrs={"rel": re.compile("canonical", re.I)})
    canonical = canonical_tag.get("href", "").strip() if canonical_tag else ""
    lang = (soup.find("html") or {}).get("lang", "") if soup.find("html") else ""
    og = {k: meta_prop(soup, f"og:{k}") for k in ("title", "description", "image", "type", "url")}
    tw_card = meta_name(soup, "twitter:card")

    nodes = all_jsonld_nodes(soup)
    types_found = sorted({t for n in nodes for t in node_types(n)})
    product = first_of_type(nodes, "Product")
    breadcrumb = first_of_type(nodes, "BreadcrumbList")

    # headings
    h1s = [h.get_text(" ", strip=True) for h in soup.find_all("h1") if h.get_text(strip=True)]
    h2s = [h.get_text(" ", strip=True) for h in soup.find_all("h2") if h.get_text(strip=True)]
    h3s = [h.get_text(" ", strip=True) for h in soup.find_all("h3") if h.get_text(strip=True)]

    imgs = soup.find_all("img")
    missing_alt = sum(1 for i in imgs if not (i.get("alt") or "").strip())

    host = host_of(url)
    internal = external = 0
    for a in soup.find_all("a", href=True):
        h = a["href"].strip()
        if h.startswith(("#", "mailto:", "tel:", "javascript:")) or not h:
            continue
        hh = host_of(h) if h.startswith(("http", "//")) else host
        if hh == host or hh.endswith("." + host):
            internal += 1
        else:
            external += 1

    raw_words = visible_words(soup)
    main_words = visible_words(soup, strip_chrome=True)
    jsonld_desc = plain(product.get("description")) if product and product.get("description") else ""
    jsonld_desc_words = len(jsonld_desc.split())
    is_shopify = bool(re.search(r"cdn\.shopify|myshopify|Shopify\.|window\.Shopify", html))
    js_rendered = main_words < 120  # thin static body => content comes from JS / JSON-LD

    # ── header ──
    L += [f"# SEO Scrape Report", ""]
    L += [f"- **URL:** {url}"]
    L += [f"- **Fetched via:** {fetched_via}  ·  **HTTP {status}**  ·  Cloudflare `cf-ray={headers.get('cf-ray','-')}`"]
    L += [f"- **Generated:** {datetime.now():%Y-%m-%d %H:%M}"]
    L += [""]

    # ── content source verdict ──
    L += ["## 1. Where does the content come from?", ""]
    if js_rendered:
        L += [f"{WARN} **This is a JS-rendered storefront** (static HTML body is thin: "
              f"**{main_words} words** of real content outside nav/footer). "
              + ("Shopify detected. " if is_shopify else "")
              + "The actual product copy is **not in the static HTML** — it lives in **JSON-LD structured data**."]
    else:
        L += [f"{OK} The static HTML carries real content (**{main_words} words** in the main body)."]
    L += ["",
          f"| Signal | Words | Meaning |",
          f"|---|---|---|",
          f"| Raw visible text (incl. nav/footer) | {raw_words} | everything the HTML renders statically |",
          f"| Main body only (nav/header/footer removed) | {main_words} | the actual page content in static HTML |",
          f"| JSON-LD product description | {jsonld_desc_words} | the copy search engines read for this product |",
          ""]
    L += [f"**Takeaway:** Google renders JS and also reads JSON-LD, so the product data *is* available to search — "
          f"but the raw HTML is thin, which weaker crawlers / some AI bots may not fully execute. "
          f"The JSON-LD is doing the heavy lifting here.", ""]

    # ── SEO essentials table ──
    def row(name, value, source, why, good):
        icon = OK if good is True else (BAD if good is False else WARN)
        v = (value or "—").replace("|", "\\|")
        rows.append(f"| {icon} {name} | {v[:90]} | {source} | {why} |")

    tl = len(title)
    row("Title tag", f"{title}  ({tl} chars)", "HTML `<title>`",
        "primary ranking + SERP headline", True if 15 <= tl <= 65 else (False if tl == 0 else None))
    dl = len(meta_desc)
    row("Meta description", f"{meta_desc}  ({dl} chars)", "HTML `<meta name=description>`",
        "SERP snippet / CTR", True if 70 <= dl <= 165 else (False if dl == 0 else None))
    row("Canonical URL", canonical, "HTML `<link rel=canonical>`",
        "prevents duplicate-content splits", bool(canonical))
    row("Robots meta", robots or "(default index,follow)", "HTML `<meta name=robots>`",
        "controls indexing", "noindex" not in robots.lower())
    row("H1 heading", f"{len(h1s)} found: " + (h1s[0] if h1s else ""), "HTML `<h1>`",
        "main topic signal (want exactly 1)", True if len(h1s) == 1 else False)
    row("Lang attribute", lang, "HTML `<html lang>`", "language targeting", bool(lang))
    row("Viewport", viewport, "HTML `<meta name=viewport>`", "mobile-friendliness", bool(viewport))
    row("Open Graph", ", ".join(k for k, v in og.items() if v) or "none",
        "HTML `<meta property=og:*>`", "social / link previews",
        True if (og["title"] and og["description"] and og["image"]) else (None if any(og.values()) else False))
    row("Twitter card", tw_card, "HTML `<meta name=twitter:card>`", "X/Twitter previews", bool(tw_card))
    row("Structured data", ", ".join(types_found) or "none", "JSON-LD `<script ld+json>`",
        "rich results / AI answers", bool(product))
    alt_cov = f"{len(imgs)-missing_alt}/{len(imgs)} have alt"
    row("Image alt text", alt_cov, "HTML `<img alt>`", "image SEO + a11y",
        True if imgs and missing_alt == 0 else (None if imgs else False))
    row("Internal links", str(internal), "HTML `<a href>`", "crawl depth / link equity", internal >= 3)

    L += ["## 2. SEO essentials", "",
          "| Element | Value | Source | Why it matters |", "|---|---|---|---|"]
    L += rows
    L += [""]

    # ── product structured data ──
    L += ["## 3. Product data (from JSON-LD)", ""]
    if product:
        offers = product.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        offers = offers or {}
        brand = product.get("brand")
        brand = brand.get("name") if isinstance(brand, dict) else brand
        rating = product.get("aggregateRating") or {}
        avail = str(offers.get("availability", "")).rsplit("/", 1)[-1]
        prod_rows = [
            ("Name", product.get("name")),
            ("Brand", brand),
            ("SKU / GTIN", product.get("sku") or product.get("gtin13") or product.get("mpn")),
            ("Price", f"{offers.get('price','')} {offers.get('priceCurrency','')}".strip()),
            ("Availability", avail),
            ("Rating", f"{rating.get('ratingValue','')} ({rating.get('reviewCount') or rating.get('ratingCount','')} reviews)" if rating else ""),
            ("Image", product.get("image") if isinstance(product.get("image"), str) else "(present)" if product.get("image") else ""),
        ]
        L += ["| Field | Value | Source |", "|---|---|---|"]
        for k, v in prod_rows:
            val = str(v or "—")[:80].replace("|", "\\|")
            L += [f"| {k} | {val} | JSON-LD Product |"]
        L += ["", "**Full product description (JSON-LD):**", "", "> " + (jsonld_desc[:900] or "—"), ""]
    else:
        L += [f"{BAD} No `Product` JSON-LD found — product rich results won't show.", ""]

    # ── headings outline ──
    L += ["## 4. Heading outline (static HTML)", ""]
    if h1s or h2s or h3s:
        for h in h1s:
            L += [f"- **H1** — {h}"]
        for h in h2s[:15]:
            L += [f"  - H2 — {h}"]
        for h in h3s[:15]:
            L += [f"    - H3 — {h}"]
    else:
        L += [f"{WARN} No headings in static HTML (rendered by JS)."]
    L += [""]

    # ── missing / weak ──
    L += ["## 5. What's missing or weak for SEO", ""]
    issues = []
    if js_rendered:
        issues.append(f"{WARN} **Thin static HTML** — main content ({main_words} words) renders via JS; relies on JSON-LD + Google JS rendering. Consider server-side rendering key copy so all crawlers/AI bots see it.")
    if not canonical:
        issues.append(f"{BAD} **No canonical URL** — risks duplicate-content dilution across variant/param URLs.")
    if not meta_desc:
        issues.append(f"{BAD} **No meta description** — Google auto-generates the snippet (lower CTR control).")
    elif not (70 <= len(meta_desc) <= 165):
        issues.append(f"{WARN} **Meta description length** off ({len(meta_desc)} chars; aim 70–165).")
    if len(h1s) != 1:
        issues.append(f"{BAD} **H1 count = {len(h1s)}** (want exactly one in the rendered page).")
    if not (15 <= len(title) <= 65):
        issues.append(f"{WARN} **Title length** = {len(title)} (aim ~15–65 chars).")
    if product:
        offers = product.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        if not offers.get("price"):
            issues.append(f"{WARN} **No price in Product JSON-LD** — needed for product rich results.")
        if not (offers.get("availability")):
            issues.append(f"{WARN} **No availability in offers** — add schema.org/InStock etc.")
        if not product.get("aggregateRating"):
            issues.append(f"{WARN} **No aggregateRating** — star ratings won't show in SERP.")
    else:
        issues.append(f"{BAD} **No Product structured data.**")
    if imgs and missing_alt:
        issues.append(f"{WARN} **{missing_alt}/{len(imgs)} images missing alt text.**")
    if not og.get("image"):
        issues.append(f"{WARN} **No og:image** — poor social link previews.")
    if not issues:
        issues = [f"{OK} No major gaps detected."]
    L += issues + [""]

    return "\n".join(L), {"title": title, "status": status, "js_rendered": js_rendered,
                          "main_words": main_words, "jsonld_desc_words": jsonld_desc_words,
                          "has_product": bool(product), "issues": issues}


# ── main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    proxy_url = to_proxy_url(read_proxy())
    print(f"Proxy endpoint: {proxy_url.split('@')[-1] if proxy_url else '(none — direct only)'}")
    print(f"Target        : {TARGET_URL}\n")

    print("== Exit IP (direct vs proxy; residential should differ & may rotate) ==")
    print("  direct  :", exit_ip())
    if proxy_url:
        for i in range(ROTATION_CHECKS):
            print(f"  proxy #{i+1}:", exit_ip(proxy_url))
    print()

    html = final_url = used = None
    resp_headers = {}
    if proxy_url:
        print("== Target via PROXY ==")
        st, fu, body, hd = fetch(TARGET_URL, proxy_url)
        if st == 200:
            html, final_url, used, resp_headers = body, fu, "residential/proxy", hd

    print("\n== Target DIRECT ==")
    st, fu, body, hd = fetch(TARGET_URL)
    if html is None and st == 200:
        html, final_url, used, resp_headers, st_used = body, fu, "direct (no proxy)", hd, st

    if not html:
        print("\nNo 200 response — nothing to report.")
        sys.exit(0)

    with open(HTML_OUT, "w", encoding="utf-8") as f:
        f.write(html)
    report, summary = build_report(html, final_url or TARGET_URL, used, 200, resp_headers)
    with open(REPORT_OUT, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"\nRaw HTML   -> {HTML_OUT}")
    print(f"SEO REPORT -> {REPORT_OUT}  (open this — it's the readable Markdown)\n")
    print("─── quick summary ───")
    print(f"  title        : {summary['title']}")
    print(f"  content src  : {'JSON-LD (JS-rendered storefront)' if summary['js_rendered'] else 'static HTML'}"
          f"  (main body {summary['main_words']} words / JSON-LD desc {summary['jsonld_desc_words']} words)")
    print(f"  product data : {'yes' if summary['has_product'] else 'NO'}")
    print(f"  issues found : {len([i for i in summary['issues'] if not i.startswith(OK)])}")
    print("\nDONE")
