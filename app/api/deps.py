"""Shared FastAPI dependencies for the API layer."""

import uuid

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import decode_access_token
from app.db.session import get_db
from app.models.user import User
from app.models.domain import Domain
from app.models.workspace import Workspace
from app.services import user_service, domain_service, workspace_service

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


def _extract_token(request: Request) -> str | None:
    """Read the access token from the httpOnly cookie, then the Bearer header."""
    cookie_token = request.cookies.get(settings.AUTH_COOKIE_NAME)
    if cookie_token:
        return cookie_token

    auth = request.headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()

    return None


def get_current_user(
    request: Request, db: Session = Depends(get_db)
) -> User:
    """Resolve the authenticated user from the request, or raise 401."""
    token = _extract_token(request)
    if not token:
        raise _UNAUTHORIZED

    subject = decode_access_token(token)
    if not subject:
        raise _UNAUTHORIZED

    user = user_service.get_user_by_id(db, subject)
    if user is None or not user.is_active:
        raise _UNAUTHORIZED

    return user


class WorkspaceContext:
    """The workspace resolved from the path plus the caller's role in it."""

    def __init__(self, workspace: Workspace, role: str) -> None:
        self.workspace = workspace
        self.role = role


def get_workspace_context(
    workspace_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WorkspaceContext:
    """Resolve a path workspace and enforce that the caller is a member.

    Returns 404 for both non-existent and non-member workspaces so membership
    is not leaked.
    """
    try:
        parsed = uuid.UUID(workspace_id)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found"
        )

    membership = workspace_service.get_membership(db, parsed, current_user)
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found"
        )

    workspace, role = membership
    return WorkspaceContext(workspace, role)


class DomainContext:
    """A workspace membership plus a domain resolved from the path."""

    def __init__(self, workspace: Workspace, role: str, domain: Domain) -> None:
        self.workspace = workspace
        self.role = role
        self.domain = domain


def get_domain_context(
    domain_id: str,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> DomainContext:
    """Resolve a domain within the caller's workspace (404 if not found)."""
    domain = domain_service.get_domain_in_workspace(db, ctx.workspace, domain_id)
    if domain is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Domain not found"
        )
    return DomainContext(ctx.workspace, ctx.role, domain)
