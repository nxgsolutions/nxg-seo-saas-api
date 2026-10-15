"""Google Search Console routes.

Three routers are exported:
  * ``router``          - workspace-scoped connect/status/disconnect
  * ``data_router``     - domain-scoped Search Analytics data
  * ``callback_router`` - the global OAuth redirect target (no auth cookie
                          required; the signed ``state`` identifies the caller)
"""

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import (
    DomainContext,
    WorkspaceContext,
    get_current_user,
    get_domain_context,
    get_workspace_context,
)
from app.core.config import settings
from app.db.session import get_db
from app.models.user import User
from app.schemas.gsc import (
    GscConnectRequest,
    GscConnectResponse,
    GscKeywordPerformance,
    GscPagePerformance,
    GscSite,
    GscStatus,
)
from app.jobs import gsc_sync
from app.services.pagination import PageQuery, page_query, paginate
from app.services import gsc_service, user_service, workspace_service

router = APIRouter(prefix="/workspaces/{workspace_id}/gsc", tags=["gsc"])
data_router = APIRouter(
    prefix="/workspaces/{workspace_id}/domains/{domain_id}/gsc", tags=["gsc"]
)
callback_router = APIRouter(prefix="/gsc", tags=["gsc"])


def _handle_service_error(exc: Exception) -> HTTPException:
    """Map a gsc_service exception to an HTTP error the frontend can branch on."""
    if isinstance(exc, gsc_service.GscNotConfiguredError):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google Search Console is not configured on the server.",
        )
    if isinstance(exc, gsc_service.GscNotConnectedError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Google Search Console is not connected for this workspace.",
        )
    if isinstance(exc, gsc_service.GscPropertyError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=str(exc) or "Google Search Console request failed.",
    )


@router.get("/status", response_model=GscStatus, summary="GSC connection status")
def gsc_status(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> GscStatus:
    conn = gsc_service.get_connection(db, ctx.workspace)
    if conn is None:
        return GscStatus(configured=settings.gsc_configured, connected=False)

    sites: list[GscSite] = []
    try:
        token = gsc_service._valid_access_token(db, conn)
        sites = [
            GscSite(
                site_url=s.get("siteUrl", ""),
                permission_level=s.get("permissionLevel"),
            )
            for s in gsc_service.list_sites(token)
        ]
    except gsc_service.GscApiError:
        # Connected but Google is momentarily unreachable / token unusable;
        # still report connected so the UI doesn't push a reconnect loop.
        sites = []

    return GscStatus(
        configured=settings.gsc_configured,
        connected=True,
        google_email=conn.google_email,
        sites=sites,
    )


@router.post("/connect", response_model=GscConnectResponse, summary="Start OAuth flow")
def gsc_connect(
    payload: GscConnectRequest,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    current_user: User = Depends(get_current_user),
) -> GscConnectResponse:
    if not settings.gsc_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google Search Console is not configured on the server.",
        )
    # Only owners/admins should be able to link an account.
    if ctx.role not in ("owner", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only workspace owners or admins can connect Search Console.",
        )
    state = gsc_service.sign_state(
        workspace_id=str(ctx.workspace.id),
        user_id=str(current_user.id),
        return_path=payload.return_path or "/dashboard",
    )
    return GscConnectResponse(auth_url=gsc_service.build_auth_url(state))


@router.delete(
    "", status_code=status.HTTP_204_NO_CONTENT, summary="Disconnect GSC"
)
def gsc_disconnect(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> None:
    if ctx.role not in ("owner", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only workspace owners or admins can disconnect Search Console.",
        )
    gsc_service.disconnect(db, ctx.workspace)


@data_router.get(
    "/page", response_model=GscPagePerformance, summary="GSC data for one page"
)
def gsc_page(
    url: str = Query(..., description="The exact page URL to report on"),
    days: int = Query(28, ge=1, le=180),
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> GscPagePerformance:
    try:
        data = gsc_service.page_performance(db, ctx.workspace, ctx.domain, url, days)
    except Exception as exc:  # noqa: BLE001 - mapped to HTTP below
        raise _handle_service_error(exc)
    return GscPagePerformance(**data)


@data_router.get(
    "/keywords",
    response_model=GscKeywordPerformance,
    summary="GSC ranking keywords + trend for a site",
)
def gsc_keywords(
    days: int = Query(28, ge=1, le=180),
    listing: PageQuery = Depends(page_query),
    ctx: DomainContext = Depends(get_domain_context),
    db: Session = Depends(get_db),
) -> GscKeywordPerformance:
    """Totals, trend and rank distribution cover every keyword; `keywords` is
    ONE page (sort: clicks | impressions | ctr | position | keyword; `q` filters)."""
    try:
        data = gsc_service.keyword_performance(db, ctx.workspace, ctx.domain, days)
    except Exception as exc:  # noqa: BLE001 - mapped to HTTP below
        raise _handle_service_error(exc)
    term = listing.search()
    rows = [r for r in data["keywords"] if not term or term in r["keyword"].lower()]
    sort = listing.sort if listing.sort in _GSC_SORTS else "clicks"
    data["keywords"], data["pagination"] = paginate(
        rows, page=listing.page, page_size=listing.page_size,
        sort_key=(lambda r: r["keyword"].lower()) if sort == "keyword" else sort,
        descending=listing.descending)
    return GscKeywordPerformance(**data)


_GSC_SORTS = {"clicks", "impressions", "ctr", "position", "keyword"}


@callback_router.get("/callback", include_in_schema=False)
def gsc_callback(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """OAuth redirect target. Validates the signed state, exchanges the code,
    stores the connection, and bounces the browser back to the app."""
    payload = gsc_service.decode_state(state or "")
    return_path = (payload or {}).get("ret", "/dashboard")

    def _bounce(status_q: str) -> RedirectResponse:
        sep = "&" if "?" in return_path else "?"
        return RedirectResponse(
            url=f"{settings.FRONTEND_URL}{return_path}{sep}gsc={status_q}"
        )

    if error:
        return _bounce("denied")
    if payload is None or not code:
        return _bounce("error")

    try:
        workspace_uuid = uuid.UUID(payload["wid"])
        user = user_service.get_user_by_id(db, payload["uid"])
    except (ValueError, KeyError, TypeError):
        return _bounce("error")
    if user is None:
        return _bounce("error")

    membership = workspace_service.get_membership(db, workspace_uuid, user)
    if membership is None:
        return _bounce("error")
    workspace, _role = membership

    try:
        tokens = gsc_service.exchange_code(code)
        gsc_service.save_connection(db, workspace, tokens)
    except (gsc_service.GscApiError, gsc_service.GscNotConfiguredError):
        return _bounce("error")

    # Store data now (and start tracking top queries) instead of waiting for the
    # nightly sync — runs after the redirect is sent.
    background_tasks.add_task(gsc_sync.first_sync, workspace.id)
    return _bounce("connected")
