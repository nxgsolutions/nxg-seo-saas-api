"""Symmetric encryption for secrets stored at rest (third-party OAuth tokens).

Uses Fernet (AES-128-CBC + HMAC-SHA256) keyed by ``TOKEN_ENCRYPTION_KEY``.
Never log plaintext or ciphertext values.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


class EncryptionNotConfiguredError(Exception):
    """TOKEN_ENCRYPTION_KEY is missing or not a valid Fernet key."""


class DecryptionError(Exception):
    """A stored value could not be decrypted (wrong key or tampered data)."""


@lru_cache(maxsize=4)
def _fernet(key: str) -> Fernet:
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise EncryptionNotConfiguredError("TOKEN_ENCRYPTION_KEY is not a valid Fernet key") from exc


def _get() -> Fernet:
    key = settings.TOKEN_ENCRYPTION_KEY.strip()
    if not key:
        raise EncryptionNotConfiguredError("TOKEN_ENCRYPTION_KEY is not set")
    return _fernet(key)


def encrypt(plaintext: str) -> str:
    return _get().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _get().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise DecryptionError("Stored secret could not be decrypted") from exc
