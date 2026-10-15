"""Rule-based content scoring for Content Optimization (pure, no I/O, no AI).

    facts = extract(html, url)                 # what the page contains
    bench = benchmark(competitor_facts, kw)    # what the top-ranking pages do
    checks, score = score(facts, kw, bench)    # pass / warn / fail checklist + 0-100

The score is deterministic: the same page and keyword always get the same
score, so re-running after an edit shows real progress. AI is only used later
to WRITE suggestions (titles, meta, outline, intro) — never to score.
"""

from __future__ import annotations

import math
import re
import statistics
from collections import Counter
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from app.services.content_service import STOPWORDS

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'\-]*")
_SENTENCE_RE = re.compile(r"[.!?]+(?:\s|$)")
# Page chrome that isn't the article (menus, cookie bars, footers).
_CHROME = ["script", "style", "noscript", "template", "svg", "nav", "header", "footer", "aside", "form", "iframe"]
_EXTRA_STOP = {
    "will", "like", "make", "need", "also", "well", "new", "many", "much", "every", "first", "best",
    "within", "without", "while", "since", "still", "even", "often", "one's", "it's", "don't", "can't",
    "click", "read", "share", "menu", "home", "login", "sign", "cookie", "cookies", "privacy", "policy",
    "copyright", "rights", "reserved", "contact", "subscribe", "email", "skip", "content", "page",
    "try", "yourself", "tutorial", "tutorials", "reference", "exercise", "exercises", "quiz", "same",
    "certificate", "next", "previous", "prev", "back", "top", "log", "spaces", "upgrade", "newsletter",
}
STOP = STOPWORDS | _EXTRA_STOP


# ── Text helpers ────────────────────────────────────────────────────────────

def norm(text: str) -> str:
    return " ".join(_WORD_RE.findall((text or "").lower()))


def words(text: str) -> list[str]:
    return _WORD_RE.findall((text or "").lower())


def match(text: str, keyword: str) -> str:
    """'full' = exact phrase, 'partial' = every meaningful word present, else 'none'."""
    t, k = norm(text), norm(keyword)
    if not k or not t:
        return "none"
    if f" {k} " in f" {t} ":
        return "full"
    need = [w for w in k.split() if w not in STOP] or k.split()
    have = set(t.split())
    return "partial" if all(w in have for w in need) else "none"


def _syllables(word: str) -> int:
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return 0
    if len(w) <= 3:
        return 1
    w = re.sub(r"(?:[^laeiouy]es|ed|[^laeiouy]e)$", "", w)
    w = re.sub(r"^y", "", w)
    return max(1, len(re.findall(r"[aeiouy]{1,2}", w)))


def reading_ease(text: str) -> float | None:
    """Flesch reading ease (0-100+, higher = easier). None for very short text."""
    ws = words(text)
    if len(ws) < 50:
        return None
    sentences = max(1, len(_SENTENCE_RE.findall(text)))
    syl = sum(_syllables(w) for w in ws)
    score = 206.835 - 1.015 * (len(ws) / sentences) - 84.6 * (syl / len(ws))
    return round(max(0.0, min(100.0, score)), 1)


def _host(url: str) -> str:
    net = urlparse(url).netloc.lower()
    return net[4:] if net.startswith("www.") else net


# ── Extraction ──────────────────────────────────────────────────────────────

def extract(html: str, url: str) -> dict[str, Any]:
    """Pull the on-page facts the checks and the comparison need."""
    soup = BeautifulSoup(html or "", "html.parser")
    title = (soup.title.get_text(" ", strip=True) if soup.title else "")[:300]
    desc = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    meta = (desc.get("content", "") if desc else "").strip()[:400]
    has_schema = bool(soup.find("script", attrs={"type": "application/ld+json"}))

    # Everything below is measured on the article only — menus, headers and
    # footers are removed so site-wide links and boilerplate don't count.
    host = _host(url)
    for tag in soup(_CHROME):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup

    h1 = [h.get_text(" ", strip=True).replace("¶", "").strip() for h in root.find_all("h1")]
    headings = [
        {"level": int(h.name[1]), "text": h.get_text(" ", strip=True).replace("¶", "").strip()[:200]}
        for h in root.find_all(["h2", "h3"]) if h.get_text(strip=True)
    ][:60]
    paragraphs = [p.get_text(" ", strip=True) for p in root.find_all("p")]
    paragraphs = [p for p in paragraphs if len(p.split()) >= 5]
    body = re.sub(r"\s+", " ", root.get_text(" ", strip=True))
    first = next((p for p in paragraphs if len(p.split()) >= 12), " ".join(body.split()[:100]))

    internal = external = 0
    # In-text links only (inside paragraphs): menus and sidebars that aren't
    # marked up as <nav>/<aside> would otherwise add hundreds of site links.
    for a in (x for p in root.find_all("p") for x in p.find_all("a", href=True)):
        href = a["href"].strip()
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        h = _host(href) if href.startswith(("http://", "https://", "//")) else host
        if h == host or h.endswith("." + host):
            internal += 1
        else:
            external += 1
    imgs = root.find_all("img")
    missing_alt = sum(1 for i in imgs if not (i.get("alt") or "").strip())

    prose = " ".join(paragraphs + [li.get_text(" ", strip=True) for li in root.find_all("li")
                                    if len(li.get_text(" ", strip=True).split()) >= 5])
    word_list = words(body)
    return {
        "url": url,
        "host": host,
        "slug": urlparse(url).path,
        "title": title,
        "meta": meta,
        "h1": h1[:5],
        "headings": headings,
        "first_paragraph": " ".join(first.split()[:120]),
        "body": body[:60_000],
        "word_count": len(word_list),
        "paragraphs": len(paragraphs),
        "avg_paragraph_words": round(statistics.mean(len(p.split()) for p in paragraphs), 1) if paragraphs else None,
        "lists": len(root.find_all(["ul", "ol"])),
        "images": len(imgs),
        "images_missing_alt": missing_alt,
        "internal_links": internal,
        "external_links": external,
        # Prose only (paragraphs + list items) — code blocks and tables would skew it.
        "reading_ease": reading_ease(prose) if len(words(prose)) >= 50 else reading_ease(body),
        "has_schema": has_schema,
    }


def article_html(html: str) -> str:
    """The main article's inner HTML with page chrome removed — a clean starting
    point for the content editor (headings, paragraphs, lists and links kept)."""
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(_CHROME):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    inner = root.decode_contents() if hasattr(root, "decode_contents") else str(root)
    return re.sub(r"\n{3,}", "\n\n", inner).strip()


def score_fields(title: str, meta: str, body_html: str, url: str,
                 keyword: str = "") -> tuple[dict, list[dict], int]:
    """Score edited editor fields: wrap them as a minimal page, then reuse the
    same extract + score path the live analyzer uses. Returns (facts, checks, 0-100)."""
    from html import escape
    html = (
        f"<html><head><title>{escape(title or '')}</title>"
        f"<meta name=\"description\" content=\"{escape(meta or '')}\"></head>"
        f"<body><article>{body_html or ''}</article></body></html>"
    )
    facts = extract(html, url or "https://example.com/")
    checks, sc = score(facts, keyword or "")
    return facts, checks, sc


# ── Competitor benchmark ────────────────────────────────────────────────────

def _terms(text: str) -> Counter:
    ws = [w for w in words(text) if not w.isdigit() and len(w) > 2]
    c: Counter = Counter(w for w in ws if w not in STOP)
    for a, b in zip(ws, ws[1:]):
        if a not in STOP and b not in STOP:
            c[f"{a} {b}"] += 1
    return c


def _median(values: list[float]) -> float | None:
    vals = [v for v in values if v is not None]
    return round(statistics.median(vals), 1) if vals else None


def benchmark(competitors: list[dict], keyword: str = "") -> dict | None:
    """Medians of the top-ranking pages + the terms most of them use."""
    pages = [c for c in competitors if c.get("word_count", 0) >= 100]
    if not pages:
        return None
    n = len(pages)
    brand = {t for c in pages for t in re.split(r"[.\-]", c.get("host", "")) if t}
    df: Counter = Counter()
    totals: Counter = Counter()
    for c in pages:
        counts = _terms(f"{c['title']} {' '.join(h['text'] for h in c['headings'])} {c['body']}")
        for term, k in counts.items():
            df[term] += 1
            totals[term] += k
    need = max(2, math.ceil(n * 0.4)) if n > 1 else 1
    candidates = [
        t for t, d in df.items()
        if d >= need and not (set(t.split()) & brand)
    ]
    candidates.sort(key=lambda t: (df[t], totals[t]), reverse=True)
    bigrams = [t for t in candidates if " " in t][:20]
    unigrams = [t for t in candidates if " " not in t][:30]
    terms = [
        {"term": t, "competitors": df[t], "avg_count": round(totals[t] / n, 1)}
        for t in sorted(bigrams + unigrams, key=lambda t: (df[t], totals[t]), reverse=True)
    ]
    return {
        "pages": n,
        "word_count": _median([c["word_count"] for c in pages]),
        "headings": _median([len(c["headings"]) for c in pages]),
        "reading_ease": _median([c["reading_ease"] for c in pages]),
        "images": _median([c["images"] for c in pages]),
        "internal_links": _median([c["internal_links"] for c in pages]),
        "h2": _median([sum(1 for h in c["headings"] if h["level"] == 2) for c in pages]),
        "keyword_in_title": sum(1 for c in pages if keyword and match(c["title"], keyword) == "full"),
        "terms": terms,
    }


def term_coverage(facts: dict, bench: dict | None) -> list[dict]:
    """Each benchmark term with how often YOUR page uses it."""
    if not bench:
        return []
    mine = _terms(f"{facts['title']} {' '.join(h['text'] for h in facts['headings'])} {facts['body']}")
    out = []
    for t in bench["terms"]:
        have = mine.get(t["term"], 0)
        status = "missing" if have == 0 else ("low" if have < max(1, t["avg_count"] * 0.4) else "ok")
        out.append({**t, "your_count": have, "status": status})
    return out


# ── Checks + score ──────────────────────────────────────────────────────────

def _check(cid: str, group: str, label: str, status: str, weight: int, value: str, tip: str) -> dict:
    return {"id": cid, "group": group, "label": label, "status": status, "weight": weight,
            "value": value, "tip": tip if status != "pass" else ""}


def _band(v: float | None, good: tuple[float, float], ok: tuple[float, float]) -> str:
    if v is None:
        return "fail"
    if good[0] <= v <= good[1]:
        return "pass"
    return "warn" if ok[0] <= v <= ok[1] else "fail"


def score(facts: dict, keyword: str = "", bench: dict | None = None,
          terms: list[dict] | None = None) -> tuple[list[dict], int]:
    """The checklist (pass / warn / fail, weighted) and the 0-100 score."""
    kw = " ".join((keyword or "").split())
    c: list[dict] = []
    KP, TM, ST, CD, ML = "Keyword placement", "Title & meta", "Structure", "Content depth", "Media & links"

    if kw:
        def place(cid: str, label: str, text: str, weight: int, where: str) -> None:
            m = match(text, kw)
            st = {"full": "pass", "partial": "warn"}.get(m, "fail")
            val = {"full": "Exact keyword", "partial": "Keyword words, not the exact phrase"}.get(m, "Not found")
            c.append(_check(cid, KP, label, st, weight, val, f"Use “{kw}” in {where}."))

        place("kw_title", "Keyword in title", facts["title"], 10, "the title, ideally near the start")
        place("kw_h1", "Keyword in H1", " ".join(facts["h1"]), 8, "the main H1 heading")
        place("kw_intro", "Keyword in the first paragraph", facts["first_paragraph"], 6, "the first 100 words")
        place("kw_meta", "Keyword in meta description", facts["meta"], 5, "the meta description")
        place("kw_sub", "Keyword in a subheading", " | ".join(h["text"] for h in facts["headings"]), 5,
              "at least one H2 or H3")
        slug = facts["slug"].replace("-", " ").replace("_", " ").replace("/", " ")
        m = match(slug, kw)
        c.append(_check("kw_url", KP, "Keyword in URL", "pass" if m != "none" else "warn", 4,
                        facts["slug"] or "/", "Use a short URL slug that contains the keyword (for new pages)."))
        total = max(1, facts["word_count"])
        occurrences = f" {norm(facts['body'])} ".count(f" {norm(kw)} ")
        density = round(occurrences * len(kw.split()) / total * 100, 2)
        st = "pass" if 0.5 <= density <= 2.5 else ("fail" if occurrences == 0 else "warn")
        tip = ("Mention the keyword naturally a few more times." if density < 0.5
               else "Use the keyword less often — it reads as stuffing.")
        c.append(_check("kw_density", KP, "Keyword frequency", st, 4,
                        f"{occurrences}× ({density}% of words)", tip))

    tl = len(facts["title"])
    c.append(_check("title_len", TM, "Title length", "fail" if not tl else _band(tl, (30, 60), (15, 70)), 6,
                    f"{tl} characters" if tl else "Missing", "Keep the title between 30 and 60 characters."))
    ml = len(facts["meta"])
    c.append(_check("meta_len", TM, "Meta description length", "fail" if not ml else _band(ml, (120, 160), (70, 180)), 5,
                    f"{ml} characters" if ml else "Missing", "Write a 120–160 character meta description."))
    h1n = len(facts["h1"])
    c.append(_check("h1_single", TM, "One H1 heading", "pass" if h1n == 1 else "warn" if h1n > 1 else "fail", 5,
                    f"{h1n} found", "Use exactly one H1 that states the page topic."))

    hn = len(facts["headings"])
    want_h = max(3, round((bench or {}).get("headings") or 0) * 0.6) if bench else 3
    c.append(_check("subheadings", ST, "Subheadings (H2/H3)", "pass" if hn >= want_h else "warn" if hn >= 1 else "fail", 6,
                    f"{hn} found" + (f" · top pages ≈ {round(bench['headings'])}" if bench and bench.get("headings") else ""),
                    "Break the content into scannable H2/H3 sections."))
    apw = facts["avg_paragraph_words"]
    c.append(_check("paragraphs", ST, "Short paragraphs", "warn" if apw is None else _band(apw, (0, 80), (0, 120)), 4,
                    f"{apw} words on average" if apw else "No paragraphs found",
                    "Keep paragraphs under ~80 words."))
    c.append(_check("lists", ST, "Lists", "pass" if facts["lists"] else "warn", 3,
                    f"{facts['lists']} found", "Add a bulleted or numbered list for skimmers."))

    wc = facts["word_count"]
    if bench and bench.get("word_count"):
        target = bench["word_count"]
        ratio = wc / target if target else 1
        st = "pass" if ratio >= 0.8 else "warn" if ratio >= 0.5 else "fail"
        c.append(_check("words", CD, "Content length", st, 10,
                        f"{wc:,} words · top pages ≈ {round(target):,}",
                        f"Expand toward ~{round(target * 0.8):,}+ words by covering missing subtopics."))
    else:
        st = "pass" if wc >= 800 else "warn" if wc >= 300 else "fail"
        c.append(_check("words", CD, "Content length", st, 10, f"{wc:,} words",
                        "Aim for 800+ words of useful content."))
    re_ = facts["reading_ease"]
    c.append(_check("readability", CD, "Readability", "warn" if re_ is None else _band(re_, (50, 100), (30, 100)), 6,
                    f"Reading ease {re_}" if re_ is not None else "Too little text to measure",
                    "Use shorter sentences and simpler words."))
    if terms:
        used = sum(1 for t in terms if t["status"] != "missing")
        share = used / len(terms)
        st = "pass" if share >= 0.7 else "warn" if share >= 0.4 else "fail"
        c.append(_check("terms", CD, "Topic coverage", st, 8,
                        f"{used} of {len(terms)} common terms",
                        "Cover the missing terms that most top pages use (see Terms)."))

    if facts["images"]:
        miss = facts["images_missing_alt"]
        c.append(_check("img_alt", ML, "Image alt text", "pass" if miss == 0 else "warn", 4,
                        f"{miss} of {facts['images']} missing alt", "Describe every image in its alt text."))
    else:
        c.append(_check("img_alt", ML, "Images", "warn", 4, "No images", "Add at least one relevant image with alt text."))
    il = facts["internal_links"]
    c.append(_check("internal_links", ML, "Internal links", "pass" if il >= 3 else "warn" if il else "fail", 4,
                    f"{il} links", "Link to 3+ related pages on your site."))
    c.append(_check("external_links", ML, "Outbound links", "pass" if facts["external_links"] else "warn", 2,
                    f"{facts['external_links']} links", "Cite at least one trustworthy source."))

    credit = {"pass": 1.0, "warn": 0.5, "fail": 0.0}
    total_w = sum(x["weight"] for x in c)
    got = sum(x["weight"] * credit[x["status"]] for x in c)
    return c, round(got / total_w * 100) if total_w else 0
