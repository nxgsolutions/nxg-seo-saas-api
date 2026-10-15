"""Onboarding routes."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.user import User
from app.schemas.user import UserResponse
from app.services import user_service

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


@router.post(
    "/complete",
    response_model=UserResponse,
    summary="Mark the current user's onboarding as complete",
)
def complete_onboarding(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserResponse:
    """Idempotently mark onboarding done. The wizard sets up the workspace and
    domain via the workspaces API first, then calls this to finish."""
    user = user_service.mark_onboarded(db, current_user)
    return UserResponse.model_validate(user)
