"""User-related business logic (kept out of the route handlers)."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import hash_password, verify_password
from app.models.user import User
from app.schemas.user import RegisterRequest
from app.services.exceptions import EmailAlreadyExistsError


def get_user_by_email(db: Session, email: str) -> User | None:
    """Look up a user by (already-normalized) email."""
    return db.execute(select(User).where(User.email == email)).scalar_one_or_none()


def mark_onboarded(db: Session, user: User) -> User:
    """Mark the user as onboarded (idempotent — keeps the first timestamp)."""
    if user.onboarded_at is None:
        user.onboarded_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(user)
    return user


def get_user_by_id(db: Session, user_id: str) -> User | None:
    """Look up a user by string UUID. Returns None on invalid id or no match."""
    try:
        parsed = uuid.UUID(user_id)
    except (ValueError, TypeError):
        return None
    return db.get(User, parsed)


def authenticate_user(db: Session, email: str, password: str) -> User | None:
    """Verify credentials. Returns the active user on success, else None.

    `email` is expected already normalized (lowercased) by the schema.
    Records `last_login_at` on success. Uses a constant-time-ish path: it
    always verifies against a hash so a missing user and a wrong password
    take a similar amount of time.
    """
    user = get_user_by_email(db, email)
    if user is None:
        # Verify against a dummy hash to reduce user-enumeration timing signal.
        verify_password(password, _DUMMY_HASH)
        return None

    if not verify_password(password, user.password_hash):
        return None

    if not user.is_active:
        return None

    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(user)
    return user


# A precomputed Argon2 hash used only to equalize timing for unknown emails.
_DUMMY_HASH = hash_password("dummy-password-for-timing")


def register_user(db: Session, payload: RegisterRequest) -> User:
    """Create a new user.

    The email in `payload` is already normalized by the schema validator.

    Raises:
        EmailAlreadyExistsError: if the email is already registered.
    """
    # Fast pre-check for a friendly 409 before hitting the DB constraint.
    if get_user_by_email(db, payload.email) is not None:
        raise EmailAlreadyExistsError(payload.email)

    user = User(
        email=payload.email,
        password_hash=hash_password(payload.password),
        first_name=payload.first_name,
        last_name=payload.last_name,
    )
    db.add(user)

    try:
        db.commit()
    except IntegrityError as exc:
        # Race condition: another request inserted the same email concurrently.
        db.rollback()
        raise EmailAlreadyExistsError(payload.email) from exc

    db.refresh(user)
    return user
