"""Integration status for the Settings → Integrations panel."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import WorkspaceContext, get_workspace_context
from app.core.config import settings
from app.db.session import get_db
from app.services import gsc_service, local_ai

router = APIRouter(prefix="/workspaces/{workspace_id}/integrations", tags=["integrations"])


class IntegrationsStatus(BaseModel):
    gsc_configured: bool
    gsc_connected: bool
    gsc_email: str | None = None
    psi_configured: bool
    dataforseo_configured: bool
    ollama_available: bool
    ollama_model: str
    anthropic_configured: bool
    stripe_configured: bool


@router.get("", response_model=IntegrationsStatus, summary="Status of all integrations")
def get_status(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> IntegrationsStatus:
    conn = gsc_service.get_connection(db, ctx.workspace)
    return IntegrationsStatus(
        gsc_configured=settings.gsc_configured,
        gsc_connected=conn is not None,
        gsc_email=conn.google_email if conn else None,
        psi_configured=bool(settings.PSI_API_KEY),
        dataforseo_configured=settings.dataforseo_configured,
        ollama_available=local_ai.available(),
        ollama_model=settings.OLLAMA_MODEL,
        anthropic_configured=settings.ai_configured,
        stripe_configured=settings.stripe_configured,
    )
