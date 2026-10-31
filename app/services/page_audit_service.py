"""Persistence for page audits (run results saved to workspace history)."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.page_audit import PageAudit
from app.models.user import User
from app.models.workspace import Workspace
from app.services.audit_service import AuditResult

# Keep history bounded per workspace (most recent kept).
MAX_HISTORY_PER_WORKSPACE = 50


def save_audit(
    db: Session, workspace: Workspace, user: User, result: AuditResult,
    *, overall_score: int | None = None,
    categories: list[dict] | None = None, blockers: list[str] | None = None,
) -> PageAudit:
    """Persist an audit result and prune old history for the workspace.

    ``overall_score`` / ``categories`` / ``blockers`` are the genuine page-score
    breakdown (from page_score_service.compose) so one saved audit carries the
    score, the category breakdown and the blocking problems."""
    record = PageAudit(
        workspace_id=workspace.id,
        user_id=user.id,
        url=result.final_url,
        score=result.score,
        status_code=result.status_code,
        load_time_ms=result.load_time_ms,
        page_size_bytes=result.page_size_bytes,
        checks=[
            {"id": c.id, "label": c.label, "status": c.status, "detail": c.detail}
            for c in result.checks
        ],
        metrics=result.metrics,
        overall_score=overall_score,
        category_scores=categories,
        blockers=blockers,
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    _prune(db, workspace.id)
    return record


def _prune(db: Session, workspace_id: uuid.UUID) -> None:
    """Delete audits beyond the retention cap for a workspace."""
    keep_ids = db.execute(
        select(PageAudit.id)
        .where(PageAudit.workspace_id == workspace_id)
        .order_by(PageAudit.created_at.desc())
        .limit(MAX_HISTORY_PER_WORKSPACE)
    ).scalars().all()
    if len(keep_ids) < MAX_HISTORY_PER_WORKSPACE:
        return
    db.query(PageAudit).filter(
        PageAudit.workspace_id == workspace_id,
        PageAudit.id.notin_(keep_ids),
    ).delete(synchronize_session=False)
    db.commit()


def list_audits(db: Session, workspace: Workspace, limit: int = 25) -> list[PageAudit]:
    return list(
        db.execute(
            select(PageAudit)
            .where(PageAudit.workspace_id == workspace.id)
            .order_by(PageAudit.created_at.desc())
            .limit(limit)
        ).scalars()
    )


def get_audit(
    db: Session, workspace: Workspace, audit_id: str
) -> PageAudit | None:
    try:
        parsed = uuid.UUID(audit_id)
    except (ValueError, TypeError):
        return None
    return db.execute(
        select(PageAudit).where(
            PageAudit.id == parsed, PageAudit.workspace_id == workspace.id
        )
    ).scalar_one_or_none()
