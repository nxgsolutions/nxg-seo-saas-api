"""Workspace + membership business logic."""

import re
import secrets
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.tenant import Tenant, TenantMember
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember
from app.services.exceptions import (
    AlreadyMemberError,
    CannotChangeOwnerRoleError,
    CannotRemoveOwnerError,
    UserNotFoundError,
)

ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    """Lowercase, hyphenated slug base. Falls back to 'workspace' if empty."""
    slug = _SLUG_STRIP.sub("-", value.strip().lower()).strip("-")
    return slug[:120] or "workspace"


def _unique_slug(db: Session, base: str) -> str:
    """Return a slug guaranteed unique, appending a short suffix on collision."""
    candidate = base
    while db.execute(
        select(Workspace.id).where(Workspace.slug == candidate)
    ).first():
        candidate = f"{base}-{secrets.token_hex(3)}"
    return candidate


def _unique_tenant_slug(db: Session, base: str) -> str:
    """A tenant slug guaranteed unique, appending a short suffix on collision."""
    candidate = base
    while db.execute(select(Tenant.id).where(Tenant.slug == candidate)).first():
        candidate = f"{base}-{secrets.token_hex(3)}"
    return candidate


def get_or_create_tenant(db: Session, owner: User, name: str) -> Tenant:
    """Find the owner's tenant (their organization) or create it.

    A user's workspaces all live under one personal tenant; the first workspace
    creates it, later ones reuse it. The owner is registered as a tenant member
    with role=owner."""
    existing = db.execute(
        select(Tenant)
        .where(Tenant.owner_id == owner.id)
        .order_by(Tenant.created_at.asc())
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    tenant = Tenant(
        name=name.strip() or "My Organization",
        slug=_unique_tenant_slug(db, slugify(name)),
        owner_id=owner.id,
    )
    db.add(tenant)
    db.flush()  # assign tenant.id
    db.add(TenantMember(tenant_id=tenant.id, user_id=owner.id, role=ROLE_OWNER))
    return tenant


def create_workspace(
    db: Session, owner: User, name: str, *, is_sandbox: bool = False
) -> Workspace:
    """Create a workspace under the owner's tenant, adding the owner as its first
    member (role=owner). Provisions the tenant on the owner's first workspace.
    `is_sandbox` routes its DataForSEO calls to the free Sandbox (data_env)."""
    tenant = get_or_create_tenant(db, owner, name)
    workspace = Workspace(
        tenant_id=tenant.id,
        name=name.strip(),
        slug=_unique_slug(db, slugify(name)),
        owner_id=owner.id,
        is_sandbox=is_sandbox,
    )
    db.add(workspace)
    db.flush()  # assign workspace.id before creating the membership

    db.add(
        WorkspaceMember(
            workspace_id=workspace.id, user_id=owner.id, role=ROLE_OWNER
        )
    )
    db.commit()
    db.refresh(workspace)
    return workspace


def ensure_default_workspace(db: Session, user: User) -> Workspace:
    """Guarantee the user belongs to at least one workspace.

    Idempotent: if the user already has a membership, returns their first
    workspace; otherwise creates a personal default workspace.
    """
    existing = db.execute(
        select(Workspace)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(WorkspaceMember.user_id == user.id)
        .order_by(Workspace.created_at.asc())
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    name = f"{user.first_name}'s Workspace".strip()
    return create_workspace(db, user, name)


def list_user_workspaces(db: Session, user: User) -> list[tuple[Workspace, str]]:
    """Return (workspace, role) pairs for every workspace the user belongs to."""
    rows = db.execute(
        select(Workspace, WorkspaceMember.role)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(WorkspaceMember.user_id == user.id)
        .order_by(Workspace.created_at.asc())
    ).all()
    return [(ws, role) for ws, role in rows]


def get_membership(
    db: Session, workspace_id, user: User
) -> tuple[Workspace, str] | None:
    """Return (workspace, role) if the user is a member, else None."""
    row = db.execute(
        select(Workspace, WorkspaceMember.role)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(
            Workspace.id == workspace_id,
            WorkspaceMember.user_id == user.id,
        )
    ).first()
    if row is None:
        return None
    return row[0], row[1]


def rename_workspace(db: Session, workspace: Workspace, name: str) -> Workspace:
    """Rename a workspace (slug is left stable to avoid breaking references)."""
    workspace.name = name.strip()
    db.commit()
    db.refresh(workspace)
    return workspace


def update_workspace(
    db: Session, workspace: Workspace, *, name: str | None = None,
    default_country: str | None = None, default_language: str | None = None,
    timezone: str | None = None,
) -> Workspace:
    """Partial update: apply only the fields provided (slug stays stable)."""
    if name is not None:
        workspace.name = name.strip()
    if default_country is not None:
        workspace.default_country = default_country.strip().lower()[:2]
    if default_language is not None:
        workspace.default_language = default_language.strip().lower()[:10]
    if timezone is not None:
        workspace.timezone = timezone.strip()[:64]
    db.commit()
    db.refresh(workspace)
    return workspace


def list_members(db: Session, workspace: Workspace) -> list[WorkspaceMember]:
    """All members of a workspace, with their user rows eagerly available."""
    return list(
        db.execute(
            select(WorkspaceMember)
            .where(WorkspaceMember.workspace_id == workspace.id)
            .order_by(WorkspaceMember.created_at.asc())
        ).scalars()
    )


def add_member(
    db: Session, workspace: Workspace, email: str, role: str
) -> WorkspaceMember:
    """Add an existing user (by normalized email) to the workspace.

    Raises UserNotFoundError / AlreadyMemberError.
    """
    user = db.execute(
        select(User).where(User.email == email)
    ).scalar_one_or_none()
    if user is None:
        raise UserNotFoundError(email)

    existing = db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace.id,
            WorkspaceMember.user_id == user.id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise AlreadyMemberError(email)

    member = WorkspaceMember(
        workspace_id=workspace.id, user_id=user.id, role=role
    )
    db.add(member)
    db.commit()
    db.refresh(member)
    return member


def remove_member(db: Session, workspace: Workspace, user_id: str) -> bool:
    """Remove a member by user id. Raises CannotRemoveOwnerError for the owner.

    Returns False if the user is not a member.
    """
    try:
        parsed = uuid.UUID(user_id)
    except (ValueError, TypeError):
        return False

    member = db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace.id,
            WorkspaceMember.user_id == parsed,
        )
    ).scalar_one_or_none()
    if member is None:
        return False
    if member.role == ROLE_OWNER or workspace.owner_id == parsed:
        raise CannotRemoveOwnerError()

    db.delete(member)
    db.commit()
    return True


def set_member_role(
    db: Session, workspace: Workspace, user_id: str, role: str
) -> WorkspaceMember | None:
    """Change a member's role. Returns None if not a member.

    Raises CannotChangeOwnerRoleError for the owner.
    """
    try:
        parsed = uuid.UUID(user_id)
    except (ValueError, TypeError):
        return None

    member = db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace.id,
            WorkspaceMember.user_id == parsed,
        )
    ).scalar_one_or_none()
    if member is None:
        return None
    if member.role == ROLE_OWNER or workspace.owner_id == parsed:
        raise CannotChangeOwnerRoleError()

    member.role = role
    db.commit()
    db.refresh(member)
    return member


def member_count(db: Session, workspace_id) -> int:
    return (
        db.execute(
            select(func.count(WorkspaceMember.id)).where(
                WorkspaceMember.workspace_id == workspace_id
            )
        ).scalar_one()
    )
