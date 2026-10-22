"""Workspace routes (including workspace-scoped domains)."""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.api.deps import (
    WorkspaceContext,
    get_current_user,
    get_workspace_context,
)
from app.db.session import get_db
from app.models.user import User
from app.schemas.page_audit import (
    PageAuditDetail,
    PageAuditRunRequest,
    PageAuditSummary,
)
from app.schemas.domain import DomainCreate, DomainResponse, DomainUpdate
from app.schemas.workspace import (
    WorkspaceCreate,
    WorkspaceMemberAdd,
    WorkspaceMemberResponse,
    WorkspaceMemberRoleUpdate,
    WorkspaceResponse,
    WorkspaceUpdate,
)
from app.jobs import gsc_sync
from app.services import (
    audit_service,
    gsc_service,
    page_audit_service,
    page_history_service,
    domain_service,
    workspace_service,
)
from app.services.audit_service import (
    BlockedUrlError,
    FetchError,
    InvalidUrlError,
)
from app.services.exceptions import (
    AlreadyMemberError,
    CannotChangeOwnerRoleError,
    CannotRemoveOwnerError,
    UserNotFoundError,
)
from app.services.domain_service import (
    DomainAlreadyExistsError,
    InvalidDomainError,
)

router = APIRouter(prefix="/workspaces", tags=["workspaces"])

_MANAGER_ROLES = {workspace_service.ROLE_OWNER, workspace_service.ROLE_ADMIN}


def _ws_response(db: Session, ws, role: str) -> WorkspaceResponse:
    """Build the API response for a workspace (single source of truth)."""
    return WorkspaceResponse(
        id=ws.id, name=ws.name, slug=ws.slug, role=role,
        member_count=workspace_service.member_count(db, ws.id),
        created_at=ws.created_at, is_sandbox=ws.is_sandbox,
        default_country=ws.default_country, default_language=ws.default_language,
        timezone=ws.timezone,
    )


@router.get(
    "",
    response_model=list[WorkspaceResponse],
    summary="List the current user's workspaces",
)
def list_workspaces(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[WorkspaceResponse]:
    """Return every workspace the user belongs to (with their role).

    Guarantees at least one workspace by provisioning a default if needed.
    """
    workspace_service.ensure_default_workspace(db, current_user)
    pairs = workspace_service.list_user_workspaces(db, current_user)
    return [_ws_response(db, ws, role) for ws, role in pairs]


@router.post(
    "",
    response_model=WorkspaceResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new workspace",
    responses={422: {"description": "Validation error"}},
)
def create_workspace(
    payload: WorkspaceCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WorkspaceResponse:
    """Create a workspace owned by the current user. `is_sandbox=true` makes a
    DataForSEO Sandbox workspace (free dummy data, isolated from live data) —
    refused where the sandbox is disabled (production by default)."""
    if payload.is_sandbox and not settings.dataforseo_sandbox_allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Sandbox workspaces are disabled in this environment.",
        )
    ws = workspace_service.create_workspace(
        db, current_user, payload.name, is_sandbox=payload.is_sandbox)
    return _ws_response(db, ws, workspace_service.ROLE_OWNER)


@router.get(
    "/{workspace_id}",
    response_model=WorkspaceResponse,
    summary="Get a workspace the current user is a member of",
    responses={404: {"description": "Workspace not found"}},
)
def get_workspace(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> WorkspaceResponse:
    return _ws_response(db, ctx.workspace, ctx.role)


@router.patch(
    "/{workspace_id}",
    response_model=WorkspaceResponse,
    summary="Update a workspace: rename and/or change its defaults (owner/admin only)",
    responses={
        403: {"description": "Insufficient role"},
        404: {"description": "Workspace not found"},
    },
)
def update_workspace(
    payload: WorkspaceUpdate,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> WorkspaceResponse:
    if ctx.role not in _MANAGER_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to update this workspace.",
        )
    ws = workspace_service.update_workspace(
        db, ctx.workspace,
        name=payload.name, default_country=payload.default_country,
        default_language=payload.default_language, timezone=payload.timezone,
    )
    return _ws_response(db, ws, ctx.role)


@router.get(
    "/{workspace_id}/domains",
    response_model=list[DomainResponse],
    summary="List domains in a workspace",
    responses={404: {"description": "Workspace not found"}},
)
def list_domains(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> list[DomainResponse]:
    return [
        DomainResponse.model_validate(w)
        for w in domain_service.list_domains(db, ctx.workspace)
    ]


@router.post(
    "/{workspace_id}/domains",
    response_model=DomainResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a domain to a workspace",
    responses={
        404: {"description": "Workspace not found"},
        409: {"description": "Domain already exists in this workspace"},
        422: {"description": "Validation error / invalid domain"},
    },
)
def create_domain(
    payload: DomainCreate,
    background_tasks: BackgroundTasks,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> DomainResponse:
    try:
        domain = domain_service.create_domain(
            db, ctx.workspace, payload.url, payload.name,
            category_id=payload.category_id,
        )
    except InvalidDomainError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Enter a valid domain (e.g. example.com).",
        )
    except DomainAlreadyExistsError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This domain is already in the workspace.",
        )
    # Search Console already connected: sync this domain now (and start tracking
    # its top queries) rather than at the next nightly run.
    if gsc_service.get_connection(db, ctx.workspace) is not None:
        background_tasks.add_task(gsc_sync.first_sync, ctx.workspace.id, domain.id)
    # Auto-derive the domain's niche from its homepage (best-effort, non-blocking).
    background_tasks.add_task(domain_service.autoderive_topics_task, ctx.workspace.id, domain.id)
    # Auto-detect the domain's industry category from its site when the caller
    # didn't set one (best-effort, non-blocking) — no manual category picking.
    if payload.category_id is None:
        background_tasks.add_task(domain_service.autodetect_category_task, ctx.workspace.id, domain.id)
    return DomainResponse.model_validate(domain)


@router.patch(
    "/{workspace_id}/domains/{domain_id}",
    response_model=DomainResponse,
    summary="Update a domain's market + niche (country, language, topics, brands, local)",
    responses={404: {"description": "Domain or workspace not found"}},
)
def update_domain(
    domain_id: str,
    payload: DomainUpdate,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> DomainResponse:
    domain = domain_service.get_domain_in_workspace(db, ctx.workspace, domain_id)
    if domain is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Domain not found")
    update_kwargs = dict(
        name=payload.name, country=payload.country, language=payload.language,
        topic_terms=payload.topic_terms, brand_terms=payload.brand_terms,
        is_local=payload.is_local,
    )
    # Only touch the category when the client actually sent the field, so an
    # omitted key leaves it unchanged and an explicit null clears it.
    if "category_id" in payload.model_fields_set:
        update_kwargs["category_id"] = payload.category_id
    domain = domain_service.update_domain(db, domain, **update_kwargs)
    return DomainResponse.model_validate(domain)


@router.post(
    "/{workspace_id}/domains/{domain_id}/derive-topics",
    summary="Suggest niche topic terms for a domain by crawling its site (no save)",
    responses={404: {"description": "Domain or workspace not found"}},
)
def derive_domain_topics(
    domain_id: str,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> dict:
    domain = domain_service.get_domain_in_workspace(db, ctx.workspace, domain_id)
    if domain is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Domain not found")
    return {"terms": domain_service.suggest_topic_terms(domain.domain)}


@router.delete(
    "/{workspace_id}/domains/{domain_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a domain and all of its data (owner/admin)",
    responses={
        403: {"description": "Insufficient role"},
        404: {"description": "Domain or workspace not found"},
    },
)
def delete_domain(
    domain_id: str,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> Response:
    if ctx.role not in _MANAGER_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the workspace owner or an admin can delete a domain.",
        )
    if not domain_service.delete_domain(db, ctx.workspace, domain_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Domain not found"
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{workspace_id}/page-audits",
    response_model=PageAuditDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Run a page audit and save it to the workspace history",
    responses={
        400: {"description": "The page could not be fetched"},
        404: {"description": "Workspace not found"},
        422: {"description": "Invalid or disallowed URL"},
    },
)
def run_page_audit(
    payload: PageAuditRunRequest,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PageAuditDetail:
    """Audit the URL server-side and persist the result in this workspace."""
    try:
        result = audit_service.run_audit(payload.url, payload.device)
    except (InvalidUrlError, BlockedUrlError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )
    except FetchError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        )

    record = page_audit_service.save_audit(db, ctx.workspace, current_user, result)
    # Page history: timestamped snapshot + what changed since the last check.
    checks = [{"id": c.id, "label": c.label, "status": c.status} for c in result.checks]
    page_history_service.safe_record(
        db, ctx.workspace, result.final_url, html=result.html, source="audit", source_id=record.id,
        device=payload.device, audit_score=result.score, checks=checks,
        tech=page_history_service.audit_tech(result.status_code, result.load_time_ms, result.page_size_bytes,
                                             result.metrics, checks),
    )
    return _audit_out(db, ctx, record)


@router.get(
    "/{workspace_id}/page-audits",
    response_model=list[PageAuditSummary],
    summary="List recent page audits in the workspace",
    responses={404: {"description": "Workspace not found"}},
)
def list_page_audits(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> list[PageAuditSummary]:
    return [
        PageAuditSummary.model_validate(a)
        for a in page_audit_service.list_audits(db, ctx.workspace)
    ]


@router.get(
    "/{workspace_id}/page-audits/{audit_id}",
    response_model=PageAuditDetail,
    summary="Get a saved page audit",
    responses={404: {"description": "Audit not found"}},
)
def get_page_audit(
    audit_id: str,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> PageAuditDetail:
    record = page_audit_service.get_audit(db, ctx.workspace, audit_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Audit not found"
        )
    return _audit_out(db, ctx, record)


def _audit_out(db: Session, ctx: WorkspaceContext, record) -> PageAuditDetail:
    """An audit + its page-history link and the changes found since the page's previous check."""
    out = PageAuditDetail.model_validate(record)
    out.page_id, out.changes = page_history_service.for_run(db, ctx.workspace, record.id)
    return out


@router.get(
    "/{workspace_id}/members",
    response_model=list[WorkspaceMemberResponse],
    summary="List members of a workspace",
    responses={404: {"description": "Workspace not found"}},
)
def list_members(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> list[WorkspaceMemberResponse]:
    members = workspace_service.list_members(db, ctx.workspace)
    return [
        WorkspaceMemberResponse(
            user_id=m.user_id,
            email=m.user.email,
            first_name=m.user.first_name,
            last_name=m.user.last_name,
            role=m.role,
            created_at=m.created_at,
        )
        for m in members
    ]


@router.post(
    "/{workspace_id}/members",
    response_model=WorkspaceMemberResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add an existing user to the workspace (owner/admin)",
    responses={
        403: {"description": "Insufficient role"},
        404: {"description": "No account with that email / workspace not found"},
        409: {"description": "Already a member"},
        422: {"description": "Validation error"},
    },
)
def add_member(
    payload: WorkspaceMemberAdd,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> WorkspaceMemberResponse:
    if ctx.role not in _MANAGER_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to manage members.",
        )
    try:
        member = workspace_service.add_member(
            db, ctx.workspace, payload.email, payload.role
        )
    except UserNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No account exists with that email.",
        )
    except AlreadyMemberError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That user is already a member.",
        )
    return WorkspaceMemberResponse(
        user_id=member.user_id,
        email=member.user.email,
        first_name=member.user.first_name,
        last_name=member.user.last_name,
        role=member.role,
        created_at=member.created_at,
    )


@router.patch(
    "/{workspace_id}/members/{user_id}",
    response_model=WorkspaceMemberResponse,
    summary="Change a member's role (owner/admin)",
    responses={
        403: {"description": "Insufficient role"},
        404: {"description": "Member not found"},
        409: {"description": "Cannot change the owner's role"},
    },
)
def update_member_role(
    user_id: str,
    payload: WorkspaceMemberRoleUpdate,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> WorkspaceMemberResponse:
    if ctx.role not in _MANAGER_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to manage members.",
        )
    try:
        member = workspace_service.set_member_role(
            db, ctx.workspace, user_id, payload.role
        )
    except CannotChangeOwnerRoleError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The owner's role cannot be changed.",
        )
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Member not found"
        )
    return WorkspaceMemberResponse(
        user_id=member.user_id,
        email=member.user.email,
        first_name=member.user.first_name,
        last_name=member.user.last_name,
        role=member.role,
        created_at=member.created_at,
    )


@router.delete(
    "/{workspace_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a member from the workspace (owner/admin)",
    responses={
        403: {"description": "Insufficient role"},
        404: {"description": "Member or workspace not found"},
        409: {"description": "Cannot remove the owner"},
    },
)
def remove_member(
    user_id: str,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> Response:
    if ctx.role not in _MANAGER_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to manage members.",
        )
    try:
        removed = workspace_service.remove_member(db, ctx.workspace, user_id)
    except CannotRemoveOwnerError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The workspace owner cannot be removed.",
        )
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Member not found"
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
