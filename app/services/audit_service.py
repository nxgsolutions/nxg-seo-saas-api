"""Page audit tool: fetch a URL and run SEO / performance heuristics.

Security: the URL is user-supplied, so this is an SSRF-sensitive surface. We
validate every redirect hop's host against private/loopback/reserved ranges and
only allow http(s).
"""

import ipaddress
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

MAX_REDIRECTS = 5
FETCH_TIMEOUT_SECONDS = 12.0
MAX_HTML_BYTES = 3_000_000  # cap parsing work
_USER_AGENT = "BrightEdgeAuditBot/1.0 (+page-audit)"

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 BrightEdgeAuditBot/1.0"
)
MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36 BrightEdgeAuditBot/1.0"
)

# Page speed: bound the PSI call lower than the standalone tool so an audit
# stays responsive and falls back to the raw fetch time when PSI is slow.
PSI_AUDIT_TIMEOUT_SECONDS = 30.0

# Real-user (CrUX) verdict → audit check status.
_PSI_FIELD_STATUS = {"FAST": "pass", "AVERAGE": "warn", "SLOW": "fail"}
_PSI_FIELD_PHRASE = {"FAST": "are good", "AVERAGE": "need improvement", "SLOW": "are poor"}


class InvalidUrlError(Exception):
    pass


class BlockedUrlError(Exception):
    """Host resolves to a private/loopback/reserved address, or can't resolve."""


class FetchError(Exception):
    """The page could not be fetched (timeout, connection error, too large)."""


@dataclass
class Check:
    id: str
    label: str
    status: str  # "pass" | "warn" | "fail"
    detail: str
    weight: int = 1


@dataclass
class AuditResult:
    url: str
    final_url: str
    status_code: int
    load_time_ms: int
    page_size_bytes: int
    score: int
    checks: list[Check]
    metrics: dict[str, Any] = field(default_factory=dict)
    # Fetched HTML (not saved with the audit) — page history extracts its snapshot from it.
    html: str = field(default="", repr=False)


def normalize_url(raw: str) -> str:
    """Ensure a scheme and validate the URL shape. Defaults to https."""
    value = raw.strip()
    if not value:
        raise InvalidUrlError("A URL is required.")
    if "://" in value:
        scheme = value.split("://", 1)[0].lower()
        if scheme not in ("http", "https"):
            raise InvalidUrlError("Only http(s) URLs are supported.")
    else:
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise InvalidUrlError("Enter a valid http(s) URL.")
    return value


def _assert_public_host(host: str) -> None:
    """Raise BlockedUrlError unless every resolved IP for host is public."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise BlockedUrlError("Could not resolve the domain.") from exc

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise BlockedUrlError("That address is not allowed.")


def _friendly_fetch_error(exc: httpx.HTTPError) -> str:
    """A short, user-facing reason for a failed page fetch.

    The raw httpx/ssl text (e.g. ``_ssl.c:1015: The handshake operation timed
    out``) is kept only as the exception cause for logs — never shown to users.
    """
    if isinstance(exc, httpx.TimeoutException):
        return "Could not fetch the page: the site took too long to respond (timed out). Please try again."
    blob = f"{type(exc).__name__}: {exc}".lower()
    if "ssl" in blob or "certificate" in blob or "handshake" in blob:
        return "Could not fetch the page: there was a secure-connection (SSL) problem with the site."
    if isinstance(exc, httpx.ConnectError) or "name or service not known" in blob or "getaddrinfo" in blob:
        return "Could not fetch the page: the site could not be reached. Check the URL and that the site is online."
    return "Could not fetch the page. Check the URL and try again."


def _fetch(url: str, user_agent: str = _USER_AGENT) -> tuple[httpx.Response, str, int]:
    """Fetch with manual, host-validated redirects. Returns (response, final_url, elapsed_ms)."""
    current = url
    start = time.perf_counter()
    headers = {"User-Agent": user_agent, "Accept": "text/html,*/*"}

    with httpx.Client(
        follow_redirects=False, timeout=FETCH_TIMEOUT_SECONDS, headers=headers
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            parsed = urlparse(current)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise BlockedUrlError("Redirected to a non-http(s) URL.")
            _assert_public_host(parsed.hostname)

            try:
                response = client.get(current)
            except httpx.HTTPError as exc:
                raise FetchError(_friendly_fetch_error(exc)) from exc

            if response.is_redirect and "location" in response.headers:
                current = urljoin(current, response.headers["location"])
                continue

            elapsed_ms = int((time.perf_counter() - start) * 1000)
            return response, current, elapsed_ms

    raise FetchError("Too many redirects.")


def safe_fetch(url: str) -> tuple[httpx.Response, str, int]:
    """Public SSRF-guarded fetch (validates every redirect hop). Reused by
    other tools. Returns (response, final_url, elapsed_ms). May raise
    InvalidUrlError / BlockedUrlError / FetchError."""
    return _fetch(normalize_url(url))


def _word_count(soup: BeautifulSoup) -> int:
    for tag in soup(["script", "style", "noscript"]):
        tag.extract()
    text = soup.get_text(" ", strip=True)
    return len([w for w in re.split(r"\s+", text) if w])


def _psi_speed_check(psi: dict[str, Any]) -> Check | None:
    """Build the scored speed check from real PageSpeed Insights data.

    Prefers field data (Core Web Vitals from real Chrome users via CrUX); falls
    back to the lab performance score. Returns None when PSI carries no usable
    signal, so the caller keeps the raw fetch-time heuristic.
    """
    lab = psi.get("lab") or {}
    field = psi.get("field") or None
    score = psi.get("performance_score")

    def _disp(key: str) -> str | None:
        return (lab.get(key) or {}).get("display")

    parts = [f"{name} {val}" for name, val in
             (("LCP", _disp("lcp")), ("CLS", _disp("cls")), ("TBT", _disp("tbt"))) if val]
    metric_tail = f" {', '.join(parts)}." if parts else ""

    overall = (field or {}).get("overall")
    if overall in _PSI_FIELD_STATUS:
        status = _PSI_FIELD_STATUS[overall]
        detail = f"Real-user Core Web Vitals {_PSI_FIELD_PHRASE[overall]}.{metric_tail}"
    elif isinstance(score, int):
        status = "pass" if score >= 90 else "warn" if score >= 50 else "fail"
        detail = f"PageSpeed performance score {score}/100 (lab-tested).{metric_tail}"
    else:
        return None
    return Check("speed", "Page speed (Core Web Vitals)", status, detail, 2)


def analyze_html(
    html: str,
    final_url: str,
    status_code: int,
    load_time_ms: int,
    page_size_bytes: int,
    device: str = "desktop",
    psi: dict[str, Any] | None = None,
) -> AuditResult:
    """Pure analysis of a fetched page. Separated so it is unit-testable.

    When ``psi`` (a PageSpeed Insights result) is supplied, the speed check is
    scored from real Core Web Vitals; otherwise it uses the raw fetch time.
    """
    soup = BeautifulSoup(html, "html.parser")
    final_host = urlparse(final_url).hostname or ""
    is_https = urlparse(final_url).scheme == "https"

    def _meta_prop(prop: str) -> str:
        tag = soup.find("meta", attrs={"property": prop}) or soup.find(
            "meta", attrs={"name": prop}
        )
        return (tag.get("content", "").strip() if tag else "") or ""

    canonical_tag = soup.find("link", attrs={"rel": re.compile("canonical", re.I)})
    canonical_url = (canonical_tag.get("href", "").strip() if canonical_tag else "") or ""
    og_title = _meta_prop("og:title")
    og_image = _meta_prop("og:image")
    og_description = _meta_prop("og:description")
    twitter_card = _meta_prop("twitter:card")
    has_structured_data = bool(
        soup.find("script", attrs={"type": re.compile("ld\\+json", re.I)})
    )
    charset_tag = soup.find("meta", attrs={"charset": True}) or soup.find(
        "meta", attrs={"http-equiv": re.compile("^content-type$", re.I)}
    )
    charset = ""
    if charset_tag is not None:
        charset = (charset_tag.get("charset") or charset_tag.get("content", "")).strip()
    has_favicon = bool(soup.find("link", attrs={"rel": re.compile("icon", re.I)}))
    hreflang_count = len(
        soup.find_all("link", attrs={"rel": re.compile("alternate", re.I), "hreflang": True})
    )
    h2_count = len(soup.find_all("h2"))

    title_tag = soup.title
    title = (title_tag.get_text(strip=True) if title_tag else "") or ""

    desc_tag = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    description = (desc_tag.get("content", "").strip() if desc_tag else "") or ""

    h1s = soup.find_all("h1")
    headings = soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
    images = soup.find_all("img")
    images_missing_alt = [i for i in images if not i.has_attr("alt")]

    internal_links = 0
    external_links = 0
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith("#") or href.startswith("mailto:") or href.startswith("tel:"):
            continue
        host = urlparse(urljoin(final_url, href)).hostname
        if host and host != final_host:
            external_links += 1
        else:
            internal_links += 1

    has_canonical = bool(canonical_url)
    has_viewport = bool(soup.find("meta", attrs={"name": re.compile("^viewport$", re.I)}))
    robots_tag = soup.find("meta", attrs={"name": re.compile("^robots$", re.I)})
    robots = (robots_tag.get("content", "").strip().lower() if robots_tag else "")
    html_tag = soup.find("html")
    lang = (html_tag.get("lang", "").strip() if html_tag else "") or ""
    word_count = _word_count(BeautifulSoup(str(soup.body or soup), "html.parser"))

    checks: list[Check] = []

    # HTTP status
    if status_code == 200:
        checks.append(Check("status", "Returns HTTP 200", "pass", "The page loaded successfully.", 2))
    elif 300 <= status_code < 400:
        checks.append(Check("status", "HTTP status", "warn", f"Redirect status {status_code}.", 2))
    else:
        checks.append(Check("status", "HTTP status", "fail", f"Returned status {status_code}.", 2))

    # HTTPS
    checks.append(
        Check("https", "Served over HTTPS", "pass" if is_https else "fail",
              "Secure connection." if is_https else "The page is not served over HTTPS.", 2)
    )

    # Title
    tl = len(title)
    if not title:
        checks.append(Check("title", "Title tag", "fail", "No <title> tag found.", 2))
    elif tl < 10 or tl > 60:
        checks.append(Check("title", "Title length", "warn", f"Title is {tl} characters (aim for 10–60).", 2))
    else:
        checks.append(Check("title", "Title tag", "pass", f"Good title ({tl} characters).", 2))

    # Meta description
    dl = len(description)
    if not description:
        checks.append(Check("description", "Meta description", "fail", "No meta description found.", 2))
    elif dl < 50 or dl > 160:
        checks.append(Check("description", "Meta description length", "warn", f"Description is {dl} characters (aim for 50–160).", 2))
    else:
        checks.append(Check("description", "Meta description", "pass", f"Good description ({dl} characters).", 2))

    # H1
    if len(h1s) == 0:
        checks.append(Check("h1", "H1 heading", "fail", "No H1 heading found.", 1))
    elif len(h1s) > 1:
        checks.append(Check("h1", "Single H1", "warn", f"Found {len(h1s)} H1 headings (prefer one).", 1))
    else:
        checks.append(Check("h1", "H1 heading", "pass", "Exactly one H1 heading.", 1))

    # Viewport
    checks.append(
        Check("viewport", "Mobile viewport", "pass" if has_viewport else "fail",
              "Responsive viewport is set." if has_viewport else "Missing responsive viewport meta tag.", 1)
    )

    # Image alt coverage
    if not images:
        checks.append(Check("img_alt", "Image alt text", "pass", "No images to check.", 1))
    elif images_missing_alt:
        pct = round(100 * len(images_missing_alt) / len(images))
        status = "fail" if pct > 50 else "warn"
        checks.append(Check("img_alt", "Image alt text", status, f"{len(images_missing_alt)} of {len(images)} images ({pct}%) lack alt text.", 1))
    else:
        checks.append(Check("img_alt", "Image alt text", "pass", f"All {len(images)} images have alt text.", 1))

    # Canonical
    checks.append(
        Check("canonical", "Canonical URL", "pass" if has_canonical else "warn",
              "Canonical link present." if has_canonical else "No canonical link — may risk duplicate content.", 1)
    )

    # Lang
    checks.append(
        Check("lang", "Language attribute", "pass" if lang else "warn",
              f"Declared language: {lang}." if lang else "No lang attribute on <html>.", 1)
    )

    # Indexability
    if "noindex" in robots:
        checks.append(Check("indexable", "Indexable", "fail", "Meta robots contains noindex — search engines are told not to index.", 2))
    else:
        checks.append(Check("indexable", "HTML index directive", "pass", "No noindex directive found in HTML; robots.txt and search-engine indexing are not verified.", 2))

    # Content depth
    if word_count < 300:
        checks.append(Check("content", "Content depth", "warn", f"Only ~{word_count} words — review whether this page answers its intended purpose. Word count is not a ranking requirement.", 1))
    else:
        checks.append(Check("content", "Content depth", "pass", f"~{word_count} words of content.", 1))

    # Page speed: prefer real Core Web Vitals (PSI) when available, else fall
    # back to the raw server-fetch time (rough — not a real browser).
    psi_speed = _psi_speed_check(psi) if psi else None
    if psi_speed is not None:
        checks.append(psi_speed)
    elif load_time_ms <= 1000:
        checks.append(Check("speed", "Response time", "pass", f"Fetched in {load_time_ms} ms.", 2))
    elif load_time_ms <= 3000:
        checks.append(Check("speed", "Response time", "warn", f"Fetched in {load_time_ms} ms — could be faster.", 2))
    else:
        checks.append(Check("speed", "Response time", "fail", f"Fetched in {load_time_ms} ms — slow.", 2))

    # Page weight (HTML only)
    if page_size_bytes > 1_000_000:
        checks.append(Check("size", "HTML size", "warn", f"HTML is {round(page_size_bytes / 1024)} KB — consider trimming.", 1))
    else:
        checks.append(Check("size", "HTML size", "pass", f"HTML is {round(page_size_bytes / 1024)} KB.", 1))

    # Open Graph (social sharing)
    if og_title and og_image:
        checks.append(Check("og", "Open Graph tags", "pass", "og:title and og:image are set for rich social sharing.", 1))
    elif og_title or og_image:
        checks.append(Check("og", "Open Graph tags", "warn", "Some Open Graph tags are missing (need og:title and og:image).", 1))
    else:
        checks.append(Check("og", "Open Graph tags", "fail", "No Open Graph tags — links won't preview well when shared.", 1))

    # Structured data
    if has_structured_data:
        checks.append(Check("schema", "Structured data", "pass", "JSON-LD structured data found — helps rich results.", 1))
    else:
        checks.append(Check("schema", "Structured data", "warn", "No schema.org structured data (JSON-LD) found.", 1))

    # Charset
    if charset:
        checks.append(Check("charset", "Character encoding", "pass", f"Charset declared ({charset}).", 1))
    else:
        checks.append(Check("charset", "Character encoding", "warn", "No <meta charset> declared.", 1))

    # Store detailed evidence with history without changing the legacy check contract.
    from app.services.audit_details import audit_details
    details, extra_checks = audit_details(soup, final_url, checks)
    checks.extend(Check(**check) for check in extra_checks)
    score = _score(checks)
    metrics = {
        **details,
        "title": title,
        "title_length": tl,
        "description": description,
        "description_length": dl,
        "h1_count": len(h1s),
        "h1_text": h1s[0].get_text(strip=True) if h1s else "",
        "heading_count": len(headings),
        "images_total": len(images),
        "images_missing_alt": len(images_missing_alt),
        "internal_links": internal_links,
        "external_links": external_links,
        "word_count": word_count,
        "has_canonical": has_canonical,
        "has_viewport": has_viewport,
        "lang": lang,
        "is_https": is_https,
        # Richer tag/metadata analysis
        "device": device,
        "canonical_url": canonical_url,
        "og_title": og_title,
        "og_image": og_image,
        "og_description": og_description,
        "has_twitter_card": bool(twitter_card),
        "has_structured_data": has_structured_data,
        "charset": charset,
        "has_favicon": has_favicon,
        "hreflang_count": hreflang_count,
        "h2_count": h2_count,
        # Page speed: where the speed check's verdict came from, plus the real
        # Core Web Vitals when PSI was used (None on the fetch-time fallback).
        "speed_source": "psi" if psi_speed else "fetch",
        "core_web_vitals": psi if psi_speed else None,
    }

    return AuditResult(
        url=final_url,
        final_url=final_url,
        status_code=status_code,
        load_time_ms=load_time_ms,
        page_size_bytes=page_size_bytes,
        score=score,
        checks=checks,
        metrics=metrics,
    )


def _score(checks: list[Check]) -> int:
    factor = {"pass": 1.0, "warn": 0.5, "fail": 0.0}
    total = sum(c.weight for c in checks)
    if total == 0:
        return 0
    got = sum(c.weight * factor[c.status] for c in checks)
    return round(100 * got / total)


def _fetch_psi(url: str, device: str) -> dict[str, Any] | None:
    """Best-effort real Core Web Vitals for the audit's speed score.

    Returns None on any error, timeout or rate limit, so the audit falls back
    to the raw fetch-time heuristic instead of failing. A local import avoids a
    circular dependency (psi_service imports from this module).
    """
    try:
        from app.services import psi_service

        return psi_service.run_psi(
            url,
            "mobile" if device == "mobile" else "desktop",
            timeout_seconds=PSI_AUDIT_TIMEOUT_SECONDS,
        )
    except Exception:
        return None


def run_audit(raw_url: str, device: str = "desktop", *, with_psi: bool = True) -> AuditResult:
    """Full audit: normalize, SSRF-guard, fetch (per device UA), analyze.

    When ``with_psi`` is set, real Core Web Vitals (PageSpeed Insights) drive
    the speed check; it degrades to the raw fetch time if PSI is unavailable.
    """
    url = normalize_url(raw_url)
    user_agent = MOBILE_UA if device == "mobile" else DESKTOP_UA
    response, final_url, elapsed_ms = _fetch(url, user_agent)

    content = response.content or b""
    page_size = len(content)
    html = content[:MAX_HTML_BYTES].decode(response.encoding or "utf-8", errors="replace")

    psi = _fetch_psi(final_url, device) if with_psi else None

    result = analyze_html(
        html=html,
        final_url=final_url,
        status_code=response.status_code,
        load_time_ms=elapsed_ms,
        page_size_bytes=page_size,
        device=device,
        psi=psi,
    )
    result.html = html
    return result
