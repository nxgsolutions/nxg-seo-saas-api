"""Authentication routes."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.rate_limit import auth_rate_limit
from app.core.config import settings
from app.core.security import create_access_token
from app.db.session import get_db
from app.models.user import User
from app.schemas.user import (
    LoginRequest,
    LoginResponse,
    RegisterRequest,
    UserResponse,
)
from app.services import (
    billing_service,
    token_service,
    user_service,
    workspace_service,
)
from app.services.exceptions import EmailAlreadyExistsError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_access_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.AUTH_COOKIE_NAME,
        value=token,
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        httponly=True,
        secure=settings.effective_cookie_secure,
        samesite=settings.COOKIE_SAMESITE,
        path="/",
    )


def _set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.AUTH_REFRESH_COOKIE_NAME,
        value=token,
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60,
        httponly=True,
        secure=settings.effective_cookie_secure,
        samesite=settings.COOKIE_SAMESITE,
        path="/",
    )


def _clear_auth_cookies(response: Response) -> None:
    for name in (settings.AUTH_COOKIE_NAME, settings.AUTH_REFRESH_COOKIE_NAME):
        response.delete_cookie(
            key=name,
            path="/",
            httponly=True,
            secure=settings.effective_cookie_secure,
            samesite=settings.COOKIE_SAMESITE,
        )


def _issue_session(response: Response, db: Session, user: User) -> None:
    """Set both access and refresh cookies for a freshly authenticated user."""
    _set_access_cookie(response, create_access_token(subject=str(user.id)))
    _set_refresh_cookie(response, token_service.issue_refresh_token(db, user))


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new user account",
    dependencies=[Depends(auth_rate_limit)],
    responses={
        409: {"description": "An account with this email already exists"},
        422: {"description": "Validation error"},
        429: {"description": "Too many attempts"},
    },
)
def register(payload: RegisterRequest, db: Session = Depends(get_db)) -> UserResponse:
    """Register a new user and return a safe representation (no secrets).

    A full registration is user + default workspace + subscription. If any step
    after the user row fails, the partially-created account is rolled back so we
    never leave an orphan user that cannot sign in or be re-registered.
    """
    try:
        user = user_service.register_user(db, payload)
    except EmailAlreadyExistsError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        )

    # Every user starts with a personal default workspace + a subscription.
    try:
        workspace = workspace_service.ensure_default_workspace(db, user)
        billing_service.ensure_subscription(db, workspace, payload.plan_code)
    except Exception:
        # Undo the whole account so the email is free to register again.
        logger.exception("Registration failed after creating user %s", user.id)
        _rollback_partial_registration(db, user)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "We couldn't finish setting up your account due to a server "
                "error. No account was created — please try again."
            ),
        )

    return UserResponse.model_validate(user)


def _rollback_partial_registration(db: Session, user: User) -> None:
    """Best-effort cleanup of a half-created account.

    Deleting the user cascades to its tenant, workspaces, memberships and
    subscription via `ondelete=CASCADE` FKs, so the email is free to register
    again.
    """
    try:
        db.rollback()
        fresh = db.get(User, user.id)
        if fresh is not None:
            db.delete(fresh)
            db.commit()
    except Exception:  # pragma: no cover - cleanup must not mask the original error
        logger.exception("Failed to roll back partial registration for %s", user.id)
        db.rollback()


@router.post(
    "/login",
    response_model=LoginResponse,
    summary="Log in and receive session cookies",
    dependencies=[Depends(auth_rate_limit)],
    responses={
        401: {"description": "Invalid email or password"},
        422: {"description": "Validation error"},
        429: {"description": "Too many attempts"},
    },
)
def login(
    payload: LoginRequest,
    response: Response,
    db: Session = Depends(get_db),
) -> LoginResponse:
    """Authenticate a user; on success set httpOnly access + refresh cookies."""
    user = user_service.authenticate_user(db, payload.email, payload.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )

    # Safety net: back-fill a default workspace for pre-existing accounts.
    workspace_service.ensure_default_workspace(db, user)
    _issue_session(response, db, user)
    return LoginResponse(user=UserResponse.model_validate(user))


@router.post(
    "/refresh",
    response_model=LoginResponse,
    summary="Rotate the refresh token and issue a new access token",
    dependencies=[Depends(auth_rate_limit)],
    responses={
        401: {"description": "Invalid or expired refresh token"},
        429: {"description": "Too many attempts"},
    },
)
def refresh(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> LoginResponse | JSONResponse:
    """Exchange a valid refresh cookie for new access + refresh cookies."""
    raw_token = request.cookies.get(settings.AUTH_REFRESH_COOKIE_NAME)
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )

    result = token_service.rotate_refresh_token(db, raw_token)
    if result is None:
        # Invalid/expired/reused — clear cookies so the client stops retrying.
        # Raising HTTPException discards cookies set on the injected Response.
        # Return the actual response carrying the cookie deletions instead.
        expired = JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "Invalid or expired session."},
        )
        _clear_auth_cookies(expired)
        return expired

    user, new_refresh = result
    _set_access_cookie(response, create_access_token(subject=str(user.id)))
    _set_refresh_cookie(response, new_refresh)
    return LoginResponse(user=UserResponse.model_validate(user))


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Log out (revoke refresh token, clear cookies)",
)
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> Response:
    """Revoke the presented refresh token and clear both cookies."""
    raw_token = request.cookies.get(settings.AUTH_REFRESH_COOKIE_NAME)
    if raw_token:
        token_service.revoke_refresh_token(db, raw_token)
    _clear_auth_cookies(response)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get(
    "/me",
    response_model=UserResponse,
    summary="Get the currently authenticated user",
    responses={401: {"description": "Not authenticated"}},
)
def me(current_user: User = Depends(get_current_user)) -> UserResponse:
    """Return the authenticated user's safe profile."""
    return UserResponse.model_validate(current_user)
