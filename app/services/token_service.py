"""Refresh-token lifecycle: issue, rotate (with reuse detection), and revoke.

Refresh tokens are opaque, high-entropy strings. Only their SHA-256 hash is
persisted. The raw token is returned to the caller once (to set as a cookie)
and never stored.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.refresh_token import RefreshToken
from app.models.user import User


def _hash_token(raw_token: str) -> str:
    """SHA-256 hex digest. Fast hashing is fine: the token has full entropy."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def issue_refresh_token(db: Session, user: User) -> str:
    """Create and persist a new refresh token; return the raw token value."""
    raw_token = secrets.token_urlsafe(48)
    record = RefreshToken(
        user_id=user.id,
        token_hash=_hash_token(raw_token),
        expires_at=_now() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    )
    db.add(record)
    db.commit()
    return raw_token


def _get_by_raw(db: Session, raw_token: str) -> RefreshToken | None:
    return db.execute(
        select(RefreshToken).where(
            RefreshToken.token_hash == _hash_token(raw_token)
        )
    ).scalar_one_or_none()


def _revoke_all_for_user(db: Session, user_id) -> None:
    db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=_now())
    )


def rotate_refresh_token(db: Session, raw_token: str) -> tuple[User, str] | None:
    """Validate and rotate a refresh token.

    Returns (user, new_raw_token) on success, or None if the token is invalid,
    expired, or reused. On reuse of an already-revoked token, ALL of that user's
    refresh tokens are revoked (compromise mitigation).
    """
    record = _get_by_raw(db, raw_token)
    if record is None:
        return None

    # Reuse detection: a revoked token being presented signals theft.
    if record.revoked_at is not None:
        _revoke_all_for_user(db, record.user_id)
        db.commit()
        return None

    if record.expires_at <= _now():
        return None

    user = db.get(User, record.user_id)
    if user is None or not user.is_active:
        return None

    # Rotate: issue a new token and mark the old one revoked + replaced.
    new_raw = secrets.token_urlsafe(48)
    new_record = RefreshToken(
        user_id=user.id,
        token_hash=_hash_token(new_raw),
        expires_at=_now() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    )
    db.add(new_record)
    db.flush()

    record.revoked_at = _now()
    record.replaced_by = new_record.id
    db.commit()
    return user, new_raw


def revoke_refresh_token(db: Session, raw_token: str) -> None:
    """Revoke a single refresh token if it exists and is still active."""
    record = _get_by_raw(db, raw_token)
    if record is not None and record.revoked_at is None:
        record.revoked_at = _now()
        db.commit()
