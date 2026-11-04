"""Standalone residential-proxy fetch test. No app imports — just httpx + stdlib.
Logs the response so you can see exactly what the proxy returns.

Run:  D:\seo\backend\.venv\Scripts\python.exe proxy_test.py
"""
import httpx

# ─────────────────────────── CONFIG ───────────────────────────
# Paste ONE residential proxy here, in either form:
#   "host:port:username:password"            (Webshare Static Residential list row)
#   "http://username:password@host:port"     (full URL)
# Webshare Rotating Residential is usually a single endpoint like:
#   "p.webshare.io:80:USERNAME:PASSWORD"     (rotates IP per request automatically)
PROXY = "31.59.20.176:6754:qdqwdoqh:0zcowc72mt6i"   # <-- replace this

TARGET_URL = "https://www.peetalkraft.com/products/peetalkraft-brass-shri-yantra-tortoise-vastu-feng-shui-lucky-turtle-with-sac"
ROTATION_CHECKS = 3     # how many times to check the exit IP (shows rotation)
TIMEOUT = 30            # residential proxies are slower — give them room
# ───────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}


def to_proxy_url(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith(("http://", "https://")):
        return raw
    parts = raw.split(":")
    if len(parts) != 4:
        raise SystemExit(f"PROXY must be host:port:user:pass or a full URL — got {len(parts)} parts")
    host, port, user, pw = parts
    return f"http://{user}:{pw}@{host}:{port}"


def exit_ip(proxy=None):
    try:
        with httpx.Client(timeout=TIMEOUT, proxy=proxy) as c:
            return c.get("https://api.ipify.org").text.strip()
    except Exception as e:
        return f"ERR {type(e).__name__}: {str(e)[:80]}"


def fetch(url, proxy=None):
    try:
        with httpx.Client(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True, proxy=proxy) as c:
            r = c.get(url)
            print(f"  status      : {r.status_code}")
            print(f"  final url   : {r.url}")
            print(f"  server/cf   : {r.headers.get('server','?')} | cf-ray={r.headers.get('cf-ray','-')}")
            print(f"  content-type: {r.headers.get('content-type','?')}")
            print(f"  bytes       : {len(r.content)}")
            print(f"  has JSON-LD : {'application/ld+json' in r.text}")
            print(f"  <title>     : {_title(r.text)}")
            print(f"  body snippet: {r.text[:200].strip()!r}")
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {str(e)[:120]}")


def _title(html: str) -> str:
    import re
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    return (m.group(1).strip()[:80] if m else "(none)")


if __name__ == "__main__":
    proxy_url = to_proxy_url(PROXY)
    masked = proxy_url.split("@")[-1]  # show host:port only, never creds
    print(f"Proxy endpoint: {masked}\n")

    print("== Exit IP (direct vs proxy; residential should differ & may rotate) ==")
    print("  direct:", exit_ip())
    for i in range(ROTATION_CHECKS):
        print(f"  proxy #{i+1}:", exit_ip(proxy_url))
    print()

    print("== Target page DIRECT ==")
    fetch(TARGET_URL)
    print()

    print("== Target page via RESIDENTIAL PROXY ==")
    fetch(TARGET_URL, proxy_url)
    print("\nDONE")