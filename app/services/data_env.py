"""Which DataForSEO environment ("live" or "sandbox") a piece of work runs in.

A workspace flagged `is_sandbox` calls the free DataForSEO Sandbox (dummy data)
and every provider row it writes (serp_snapshots, domain_analytics_snapshots)
is tagged environment='sandbox'. Reads filter on the same value, so sandbox
results never show up in a live workspace — even though those tables are shared
per keyword / per domain across workspaces. DATAFORSEO_FORCE_SANDBOX makes
everything sandbox.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.domain import Domain
from app.models.workspace import Workspace

LIVE = "live"
SANDBOX = "sandbox"


def default_env() -> str:
    return SANDBOX if settings.DATAFORSEO_FORCE_SANDBOX else LIVE


def env_for_workspace(workspace: Workspace | None) -> str:
    if settings.DATAFORSEO_FORCE_SANDBOX or (workspace is not None and workspace.is_sandbox):
        return SANDBOX
    return LIVE


def env_for_workspace_id(db: Session, workspace_id) -> str:
    return env_for_workspace(db.get(Workspace, workspace_id) if workspace_id else None)


def env_for_domain(db: Session, domain: Domain) -> str:
    return env_for_workspace_id(db, domain.workspace_id)
