"""Shopify integration routes.

Routers:
  * ``router``         - domain-scoped connect / status / disconnect
  * ``public_router``  - OAuth callback + webhooks (no auth cookie; verified by
                         Shopify HMAC and our signed state)
"""

import json
import logging
from datetime import datetime, timedelta, timezone
import urllib.parse
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import DomainContext, get_current_user, get_domain_context
from app.api.rate_limit import rate_limiter
from app.core.config import settings
from app.db.session import get_db
from app.models.user import User
from app.models.site_page import SitePage
from app.schemas.shopify import (
    ShopifyConnectRequest,
    ShopifyConnectResponse,
    ShopifyEditorFields,
    ShopifyPublishRequest,
    ShopifyPublishResponse,
    ShopifyPushLogOut,
    ShopifyStatus,
)
from app.schemas.page_discovery import DiscoveryRunOut
from app.services import domain_service, page_discovery_service, user_service, workspace_service
from app.services.shopify import connection_service, domain_match, oauth, publish, sync, webhooks
from app.services.shopify.errors import (
    InvalidShopError,
    ShopifyApiError,
    ShopifyNotConfiguredError,
    ShopifyNotConnectedError,
    ShopifyUserError,
)

log = logging.getLogger(__name__)

MANAGER_ROLES = ("owner", "admin")

router = APIRouter(prefix="/workspaces/{workspace_id}/domains/{domain_id}/shopify", tags=["shopify"])
public_router = APIRouter(prefix="/integrations/shopify", tags=["shopify"])
editor_router = APIRouter(
    prefix="/workspaces/{workspace_id}/domains/{domain_id}/page-discovery/pages/{page_id}/shopify",
    tags=["shopify"],
)


# Per-domain caps on calls that hit Shopify (protects the store's API budget).
SYNC_LIMIT = (6, 3600)       # imports per hour
PUBLISH_LIMIT = (60, 60)     # publishes / undos per minute


def _throttle(kind: str, ctx: DomainContext, limit: tuple[int, int]) -> None:
    if not settings.RATE_LIMIT_ENABLED:
        return
    allowed, retry_after = rate_limiter.check(f"shopify:{kind}:{ctx.domain.id}", limit=limit[0], window=limit[1])
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many Shopify requests for this site. Please wait a moment and try again.",
            headers={"Retry-After": str(retry_after)},
        )


def _require_manager(ctx: DomainContext) -> None:
    if ctx.role not in MANAGER_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only workspace owners or admins can manage the Shopify connection.",
        )


@router.get("", response_model=ShopifyStatus, summary="Shopify connection status")
def shopify_status(
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> ShopifyStatus:
    conn = connection_service.get(db, ctx.domain)
    if conn is None:
        return ShopifyStatus(
            configured=settings.shopify_configured,
            connected=False,
            webhooks_enabled=settings.shopify_webhooks_enabled,
        )
    return ShopifyStatus(
        configured=settings.shopify_configured,
        connected=conn.status == "active",
        status=conn.status,
        shop=conn.shop,
        shop_name=conn.shop_name,
        primary_host=conn.primary_host,
        shop_hosts=list(conn.shop_hosts or []),
        scopes=conn.scopes,
        installed_at=conn.installed_at,
        last_sync_at=conn.last_sync_at,
        last_sync_error=conn.last_sync_error,
        webhooks_enabled=settings.shopify_webhooks_enabled,
    )


@router.post("/connect", response_model=ShopifyConnectResponse, summary="Start Shopify OAuth")
def shopify_connect(
    payload: ShopifyConnectRequest,
    ctx: DomainContext = Depends(get_domain_context),
    current_user: User = Depends(get_current_user),
) -> ShopifyConnectResponse:
    if not settings.shopify_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Shopify is not configured on the server.",
        )
    _require_manager(ctx)
    try:
        shop = oauth.normalize_shop(payload.shop)
    except InvalidShopError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    state = oauth.sign_state(
        workspace_id=str(ctx.workspace.id),
        domain_id=str(ctx.domain.id),
        user_id=str(current_user.id),
        shop=shop,
        return_path=payload.return_path,
    )
    return ShopifyConnectResponse(install_url=oauth.build_install_url(shop, state))


@router.delete("", status_code=status.HTTP_204_NO_CONTENT, summary="Disconnect Shopify")
def shopify_disconnect(
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> Response:
    _require_manager(ctx)
    connection_service.disconnect(db, ctx.domain)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/sync", response_model=DiscoveryRunOut, summary="Import products & pages from Shopify")
def shopify_sync(
    background_tasks: BackgroundTasks,
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> DiscoveryRunOut:
    """Start a background import of every product and page into Page Discovery.
    Progress is reported by the normal Page Discovery /status endpoint."""
    try:
        connection_service.get_active(db, ctx.domain)
    except ShopifyNotConnectedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    _throttle("sync", ctx, SYNC_LIMIT)
    existing = page_discovery_service.latest_run(db, ctx.domain)
    if existing is not None and existing.status == "running" and existing.started_at is not None:
        started = existing.started_at if existing.started_at.tzinfo else existing.started_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - started < timedelta(minutes=15):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail="A scan is already running for this site. Wait for it to finish or stop it first.")
    background_tasks.add_task(sync.run_shopify_sync, ctx.workspace.id, ctx.domain.id)
    return DiscoveryRunOut(source="shopify", status="running", started_at=datetime.now(timezone.utc))


# --- editor: load / publish / history / undo --------------------------------
def _page(db: Session, ctx: DomainContext, page_id: str) -> SitePage:
    try:
        parsed = uuid.UUID(page_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Page not found")
    page = db.get(SitePage, parsed)
    if page is None or page.domain_id != ctx.domain.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Page not found")
    return page


def _editor_out(db: Session, ctx: DomainContext, fields: dict) -> ShopifyEditorFields:
    conn = connection_service.get(db, ctx.domain)
    admin_url = None
    if conn is not None:
        kind = "pages" if fields["external_type"] == "page" else "products"
        admin_url = f"https://{conn.shop}/admin/{kind}/{fields['external_id'].rsplit('/', 1)[-1]}"
    return ShopifyEditorFields(**fields, admin_url=admin_url)


def _shopify_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, publish.NotShopifyPageError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if isinstance(exc, ShopifyNotConnectedError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, publish.ShopifyItemGoneError):
        return HTTPException(status_code=status.HTTP_410_GONE, detail=str(exc))
    if isinstance(exc, ShopifyUserError):
        return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc) or "Shopify request failed.")


@editor_router.get("", response_model=ShopifyEditorFields, summary="Live Shopify fields for the editor")
def shopify_editor_fields(
    page_id: str,
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> ShopifyEditorFields:
    page = _page(db, ctx, page_id)
    try:
        item = publish.load_live(db, ctx.domain, page)
    except (publish.NotShopifyPageError, ShopifyNotConnectedError, publish.ShopifyItemGoneError, ShopifyApiError) as exc:
        raise _shopify_http_error(exc) from exc
    return _editor_out(db, ctx, publish.editor_fields(item))


@editor_router.put("", response_model=ShopifyPublishResponse, summary="Publish edits to Shopify")
def shopify_publish(
    page_id: str,
    payload: ShopifyPublishRequest,
    ctx: DomainContext = Depends(get_domain_context),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Writes to the live store, so owners/admins only. 409 (with Shopify's
    current values) when the item changed in Shopify after the editor loaded it."""
    _require_manager(ctx)
    _throttle("publish", ctx, PUBLISH_LIMIT)
    page = _page(db, ctx, page_id)
    data = publish.PublishInput(**payload.model_dump())
    try:
        result = publish.push(db, ctx.domain, page, data, user_id=current_user.id)
    except publish.ShopifyConflictError as exc:
        current = _editor_out(db, ctx, exc.current).model_dump(mode="json")
        return JSONResponse(status_code=status.HTTP_409_CONFLICT,
                            content={"detail": str(exc), "code": "shopify_conflict", "current": current})
    except (publish.NotShopifyPageError, ShopifyNotConnectedError, publish.ShopifyItemGoneError, ShopifyApiError) as exc:
        raise _shopify_http_error(exc) from exc
    return ShopifyPublishResponse(
        status=result["status"], changed=result["changed"], redirect_created=result["redirect_created"],
        warnings=result["warnings"], item=_editor_out(db, ctx, result["item"]),
        log_id=str(result["log_id"]) if result["log_id"] else None,
    )


@editor_router.get("/history", response_model=list[ShopifyPushLogOut], summary="Publish history")
def shopify_history(
    page_id: str,
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> list[ShopifyPushLogOut]:
    page = _page(db, ctx, page_id)
    out = []
    for e in publish.history(db, page):
        user = db.get(User, e.user_id) if e.user_id else None
        before, after = e.before or {}, e.after or {}
        changed = sorted(k for k in set(before) | set(after)
                         if k != "updated_at" and before.get(k) != after.get(k)) if e.status == "ok" else []
        out.append(ShopifyPushLogOut(
            id=str(e.id), action=e.action, status=e.status, error=e.error,
            redirect_created=e.redirect_created, changed=changed, before=before, after=after,
            created_at=e.created_at, user_email=user.email if user else None))
    return out


@editor_router.post("/revert/{log_id}", response_model=ShopifyPublishResponse, summary="Undo a publish")
def shopify_revert(
    page_id: str,
    log_id: str,
    ctx: DomainContext = Depends(get_domain_context),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ShopifyPublishResponse:
    _require_manager(ctx)
    _throttle("publish", ctx, PUBLISH_LIMIT)
    page = _page(db, ctx, page_id)
    try:
        parsed = uuid.UUID(log_id)
        result = publish.revert(db, ctx.domain, page, parsed, user_id=current_user.id)
    except (ValueError, LookupError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Nothing to undo for this entry.") from exc
    except (publish.NotShopifyPageError, ShopifyNotConnectedError, publish.ShopifyItemGoneError, ShopifyApiError) as exc:
        raise _shopify_http_error(exc) from exc
    return ShopifyPublishResponse(
        status=result["status"], changed=result["changed"], redirect_created=result["redirect_created"],
        warnings=result["warnings"], item=_editor_out(db, ctx, result["item"]),
        log_id=str(result["log_id"]) if result["log_id"] else None,
    )


# --- public: OAuth callback -------------------------------------------------
@public_router.get("/callback", include_in_schema=False)
def shopify_callback(
    request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Verify Shopify's HMAC + our signed state, check the store's domain
    matches the workspace domain, then store the encrypted token."""
    params = dict(request.query_params)
    payload = oauth.decode_state(params.get("state", ""))
    return_path = oauth.safe_return_path((payload or {}).get("ret"))

    def _bounce(result: str, **extra: str) -> RedirectResponse:
        query = urllib.parse.urlencode({"shopify": result, **extra})
        sep = "&" if "?" in return_path else "?"
        return RedirectResponse(url=f"{settings.FRONTEND_URL}{return_path}{sep}{query}")

    if params.get("error"):
        return _bounce("denied")
    if payload is None or not params.get("code"):
        return _bounce("error")
    if not oauth.verify_query_hmac(params):
        return _bounce("error")
    shop = (params.get("shop") or "").lower()
    if not oauth.SHOP_RE.match(shop) or shop != payload.get("shop"):
        return _bounce("error")

    try:
        workspace_uuid = uuid.UUID(str(payload.get("wid")))
    except ValueError:
        return _bounce("error")
    user = user_service.get_user_by_id(db, payload.get("uid", ""))
    if user is None:
        return _bounce("error")
    membership = workspace_service.get_membership(db, workspace_uuid, user)
    if membership is None or membership[1] not in MANAGER_ROLES:
        return _bounce("error")
    workspace = membership[0]
    domain = domain_service.get_domain_in_workspace(db, workspace, payload.get("did", ""))
    if domain is None:
        return _bounce("error")

    try:
        token = oauth.exchange_code(shop, params["code"])
        identity = domain_match.fetch_identity(shop, token.access_token)
    except (ShopifyApiError, ShopifyNotConfiguredError) as exc:
        log.warning("shopify callback failed for %s: %s", shop, exc)
        return _bounce("error")

    if not domain_match.matches(domain.domain, identity):
        # Do not keep a token for a store that isn't this workspace's site.
        connection_service._revoke_remote(shop, token.access_token)
        return _bounce(
            "domain_mismatch",
            found=identity.primary_host or identity.myshopify_domain,
            expected=domain.domain,
        )

    conn = connection_service.save(db, domain=domain, user_id=user.id, identity=identity, token=token)
    connection_service.subscribe_webhooks(db, conn)
    # First import runs after the redirect is sent.
    background_tasks.add_task(sync.run_shopify_sync, workspace.id, domain.id)
    return _bounce("connected")


# --- public: webhooks -------------------------------------------------------
async def _raw_body(request: Request) -> bytes:
    return await request.body()


@public_router.post("/webhooks", include_in_schema=False)
def shopify_webhooks(
    body: bytes = Depends(_raw_body),
    x_shopify_hmac_sha256: str | None = Header(default=None),
    x_shopify_topic: str | None = Header(default=None),
    x_shopify_shop_domain: str | None = Header(default=None),
    x_shopify_webhook_id: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> dict:
    if not webhooks.verify_hmac(body, x_shopify_hmac_sha256):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid HMAC")
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        data = {}
    outcome = webhooks.handle(
        db,
        topic=x_shopify_topic or "",
        shop=x_shopify_shop_domain or "",
        webhook_id=x_shopify_webhook_id or "",
        payload=data if isinstance(data, dict) else {},
    )
    return {"ok": True, "result": outcome}
