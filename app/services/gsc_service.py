"""Google Search Console integration.

Handles the OAuth 2.0 authorization-code flow, token storage/refresh, and
Search Console API queries (verified sites + Search Analytics). All network
calls target fixed Google hosts, so there is no SSRF surface here.

The features stay dormant unless GOOGLE_OAUTH_CLIENT_ID/SECRET are configured;
callers should check ``settings.gsc_configured`` and surface a connect prompt.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.gsc_connection import GscConnection
from app.models.domain import Domain
from app.models.workspace import Workspace

# --- Google endpoints -------------------------------------------------------
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
SITES_ENDPOINT = "https://searchconsole.googleapis.com/webmasters/v3/sites"
SEARCH_ANALYTICS_TMPL = (
    "https://searchconsole.googleapis.com/webmasters/v3/sites/{site}/searchAnalytics/query"
)

SCOPES = [
    "openid",
    "email",
    "https://www.googleapis.com/auth/webmasters.readonly",
]

HTTP_TIMEOUT = 30.0
# Search Console data lags real time by ~2-3 days; end queries a bit back so we
# don't request an empty tail.
DATA_LATENCY_DAYS = 2


class GscNotConfiguredError(Exception):
    """OAuth client credentials are not set on the server."""


class GscNotConnectedError(Exception):
    """The workspace has not connected a Google Search Console account."""


class GscPropertyError(Exception):
    """No verified Search Console property matches the domain domain."""


class GscApiError(Exception):
    """A Google API call failed."""


# --- OAuth state (signed, short-lived) --------------------------------------
def sign_state(workspace_id: str, user_id: str, return_path: str) -> str:
    """Sign a short-lived state token binding the callback to a workspace/user."""
    now = datetime.now(timezone.utc)
    payload = {
        "typ": "gsc_state",
        "wid": workspace_id,
        "uid": user_id,
        "ret": return_path,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=10)).timestamp()),
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_state(token: str) -> dict | None:
    try:
        payload = jwt.decode(
            token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
        )
    except jwt.PyJWTError:
        return None
    if payload.get("typ") != "gsc_state":
        return None
    return payload


def build_auth_url(state: str) -> str:
    """Build the Google consent URL for the authorization-code flow."""
    if not settings.gsc_configured:
        raise GscNotConfiguredError()
    params = {
        "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"


# --- Token exchange / refresh ----------------------------------------------
@dataclass
class TokenBundle:
    access_token: str
    refresh_token: str | None
    expires_in: int
    scope: str | None
    email: str | None


def _email_from_id_token(id_token: str | None) -> str | None:
    if not id_token:
        return None
    try:
        # The id_token comes straight from Google's token endpoint over TLS, so
        # we read the email claim without re-verifying the signature.
        claims = jwt.decode(id_token, options={"verify_signature": False})
    except jwt.PyJWTError:
        return None
    email = claims.get("email")
    return email if isinstance(email, str) else None


def exchange_code(code: str) -> TokenBundle:
    if not settings.gsc_configured:
        raise GscNotConfiguredError()
    data = {
        "code": code,
        "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
        "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
        "redirect_uri": settings.GOOGLE_OAUTH_REDIRECT_URI,
        "grant_type": "authorization_code",
    }
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            resp = client.post(TOKEN_ENDPOINT, data=data)
    except httpx.HTTPError as exc:
        raise GscApiError(f"Token exchange failed: {exc}") from exc
    if resp.status_code != 200:
        raise GscApiError(f"Token exchange failed ({resp.status_code}): {resp.text[:200]}")
    body = resp.json()
    return TokenBundle(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token"),
        expires_in=int(body.get("expires_in", 3600)),
        scope=body.get("scope"),
        email=_email_from_id_token(body.get("id_token")),
    )


def _refresh(conn: GscConnection) -> TokenBundle:
    data = {
        "refresh_token": conn.refresh_token,
        "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
        "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
        "grant_type": "refresh_token",
    }
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            resp = client.post(TOKEN_ENDPOINT, data=data)
    except httpx.HTTPError as exc:
        raise GscApiError(f"Token refresh failed: {exc}") from exc
    if resp.status_code != 200:
        raise GscApiError(f"Token refresh failed ({resp.status_code}): {resp.text[:200]}")
    body = resp.json()
    return TokenBundle(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token") or conn.refresh_token,
        expires_in=int(body.get("expires_in", 3600)),
        scope=body.get("scope") or conn.scope,
        email=conn.google_email,
    )


# --- Connection persistence -------------------------------------------------
def get_connection(db: Session, workspace: Workspace) -> GscConnection | None:
    return db.execute(
        select(GscConnection).where(GscConnection.workspace_id == workspace.id)
    ).scalar_one_or_none()


def save_connection(
    db: Session, workspace: Workspace, tokens: TokenBundle
) -> GscConnection:
    """Create or update the workspace's GSC connection."""
    if not tokens.refresh_token:
        raise GscApiError(
            "Google did not return a refresh token. Disconnect the app in your "
            "Google account and reconnect to grant offline access."
        )
    conn = get_connection(db, workspace)
    expiry = datetime.now(timezone.utc) + timedelta(seconds=tokens.expires_in - 60)
    if conn is None:
        conn = GscConnection(
            workspace_id=workspace.id,
            google_email=tokens.email or "unknown",
            refresh_token=tokens.refresh_token,
            access_token=tokens.access_token,
            token_expiry=expiry,
            scope=tokens.scope,
        )
        db.add(conn)
    else:
        conn.google_email = tokens.email or conn.google_email
        conn.refresh_token = tokens.refresh_token
        conn.access_token = tokens.access_token
        conn.token_expiry = expiry
        conn.scope = tokens.scope
    db.commit()
    db.refresh(conn)
    return conn


def disconnect(db: Session, workspace: Workspace) -> bool:
    conn = get_connection(db, workspace)
    if conn is None:
        return False
    db.delete(conn)
    db.commit()
    return True


def _valid_access_token(db: Session, conn: GscConnection) -> str:
    """Return a non-expired access token, refreshing via Google if needed."""
    now = datetime.now(timezone.utc)
    expiry = conn.token_expiry
    if expiry is not None and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if conn.access_token and expiry and expiry > now:
        return conn.access_token
    tokens = _refresh(conn)
    conn.access_token = tokens.access_token
    conn.refresh_token = tokens.refresh_token
    conn.token_expiry = now + timedelta(seconds=tokens.expires_in - 60)
    conn.scope = tokens.scope
    db.commit()
    db.refresh(conn)
    return conn.access_token


# --- Search Console API -----------------------------------------------------
def list_sites(access_token: str) -> list[dict]:
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            resp = client.get(
                SITES_ENDPOINT,
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except httpx.HTTPError as exc:
        raise GscApiError(f"Could not list sites: {exc}") from exc
    if resp.status_code != 200:
        raise GscApiError(f"Could not list sites ({resp.status_code}): {resp.text[:200]}")
    return resp.json().get("siteEntry", [])


def _host_of(site_url: str) -> str:
    if site_url.startswith("sc-domain:"):
        host = site_url[len("sc-domain:") :]
    else:
        host = urllib.parse.urlparse(site_url).netloc
    return host.lower().removeprefix("www.")


def match_property(sites: list[dict], domain: str) -> str | None:
    """Pick the verified property that best covers ``domain``.

    Prefers a domain property (``sc-domain:``) over a URL-prefix property.
    """
    target = domain.strip().lower().removeprefix("www.")
    domain_match: str | None = None
    prefix_match: str | None = None
    for entry in sites:
        site_url = entry.get("siteUrl", "")
        if not site_url:
            continue
        if _host_of(site_url) != target:
            continue
        if site_url.startswith("sc-domain:"):
            domain_match = domain_match or site_url
        else:
            prefix_match = prefix_match or site_url
    return domain_match or prefix_match


def search_analytics(access_token: str, site_url: str, body: dict) -> list[dict]:
    encoded = urllib.parse.quote(site_url, safe="")
    url = SEARCH_ANALYTICS_TMPL.format(site=encoded)
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            resp = client.post(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
                json=body,
            )
    except httpx.HTTPError as exc:
        raise GscApiError(f"Search Analytics query failed: {exc}") from exc
    if resp.status_code != 200:
        raise GscApiError(
            f"Search Analytics query failed ({resp.status_code}): {resp.text[:200]}"
        )
    return resp.json().get("rows", [])


# --- Date helpers -----------------------------------------------------------
def _date_range(days: int) -> tuple[str, str]:
    from datetime import date

    end = date.today() - timedelta(days=DATA_LATENCY_DAYS)
    start = end - timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()


def _resolve_property(db: Session, workspace: Workspace, domain: Domain) -> tuple[str, str]:
    """Return (access_token, site_url) for the domain, or raise."""
    conn = get_connection(db, workspace)
    if conn is None:
        raise GscNotConnectedError()
    token = _valid_access_token(db, conn)
    sites = list_sites(token)
    site_url = match_property(sites, domain.domain)
    if site_url is None:
        raise GscPropertyError(
            f"No verified Search Console property matches '{domain.domain}'. "
            "Add and verify the property in Search Console."
        )
    return token, site_url


# --- High-level queries -----------------------------------------------------
def page_performance(
    db: Session, workspace: Workspace, domain: Domain, page_url: str, days: int = 28
) -> dict:
    """Totals + top queries for a single page over the last ``days`` days."""
    token, site_url = _resolve_property(db, workspace, domain)
    start, end = _date_range(days)
    page_filter = {
        "dimensionFilterGroups": [
            {"filters": [{"dimension": "page", "operator": "equals", "expression": page_url}]}
        ]
    }

    totals_rows = search_analytics(
        token, site_url, {"startDate": start, "endDate": end, **page_filter}
    )
    totals = totals_rows[0] if totals_rows else None

    query_rows = search_analytics(
        token,
        site_url,
        {
            "startDate": start,
            "endDate": end,
            "dimensions": ["query"],
            "rowLimit": 25,
            **page_filter,
        },
    )
    queries = [
        {
            "query": r["keys"][0],
            "clicks": r.get("clicks", 0),
            "impressions": r.get("impressions", 0),
            "ctr": r.get("ctr", 0.0),
            "position": r.get("position", 0.0),
        }
        for r in query_rows
    ]
    return {
        "site_url": site_url,
        "page_url": page_url,
        "start_date": start,
        "end_date": end,
        "days": days,
        "has_data": totals is not None,
        "clicks": (totals or {}).get("clicks", 0),
        "impressions": (totals or {}).get("impressions", 0),
        "ctr": (totals or {}).get("ctr", 0.0),
        "position": (totals or {}).get("position", 0.0),
        "queries": queries,
    }


def _rank_bucket(position: float) -> str:
    if position <= 3:
        return "top3"
    if position <= 10:
        return "top10"
    if position <= 20:
        return "top20"
    return "beyond"


def keyword_performance(
    db: Session, workspace: Workspace, domain: Domain, days: int = 28
) -> dict:
    """Ranking keywords, daily traffic trend, and rank distribution for a site."""
    token, site_url = _resolve_property(db, workspace, domain)
    start, end = _date_range(days)

    # Ranking keywords (top by clicks).
    kw_rows = search_analytics(
        token,
        site_url,
        {
            "startDate": start,
            "endDate": end,
            "dimensions": ["query"],
            "rowLimit": 250,
        },
    )
    keywords = [
        {
            "keyword": r["keys"][0],
            "clicks": r.get("clicks", 0),
            "impressions": r.get("impressions", 0),
            "ctr": r.get("ctr", 0.0),
            "position": r.get("position", 0.0),
        }
        for r in kw_rows
    ]

    # Rank distribution buckets.
    distribution = {"top3": 0, "top10": 0, "top20": 0, "beyond": 0}
    for k in keywords:
        distribution[_rank_bucket(k["position"])] += 1

    # Daily traffic trend.
    trend_rows = search_analytics(
        token,
        site_url,
        {
            "startDate": start,
            "endDate": end,
            "dimensions": ["date"],
            "rowLimit": days + 5,
        },
    )
    trend = [
        {
            "date": r["keys"][0],
            "clicks": r.get("clicks", 0),
            "impressions": r.get("impressions", 0),
            "position": r.get("position", 0.0),
        }
        for r in trend_rows
    ]

    total_clicks = sum(k["clicks"] for k in keywords)
    total_impressions = sum(k["impressions"] for k in keywords)
    avg_position = (
        sum(k["position"] for k in keywords) / len(keywords) if keywords else 0.0
    )

    return {
        "site_url": site_url,
        "start_date": start,
        "end_date": end,
        "days": days,
        "keyword_count": len(keywords),
        "total_clicks": total_clicks,
        "total_impressions": total_impressions,
        "avg_position": avg_position,
        "distribution": distribution,
        "keywords": keywords,
        "trend": trend,
    }
