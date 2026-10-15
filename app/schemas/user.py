"""Pydantic schemas for user registration requests and responses."""

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

# Password policy: min 8 chars, at least one letter and one digit.
_HAS_LETTER = re.compile(r"[A-Za-z]")
_HAS_DIGIT = re.compile(r"\d")


class RegisterRequest(BaseModel):
    """Incoming payload for POST /api/auth/register."""

    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    email: EmailStr = Field(max_length=320)
    password: str = Field(min_length=8, max_length=128)
    # Plan chosen on the signup pricing table; defaults to free.
    plan_code: str = Field(default="free", max_length=50)

    @field_validator("first_name", "last_name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("This field is required")
        return value

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        # Normalize so Raj@Example.com and raj@example.com are the same user.
        return value.strip().lower()

    @field_validator("password")
    @classmethod
    def _validate_password(cls, value: str) -> str:
        if not _HAS_LETTER.search(value) or not _HAS_DIGIT.search(value):
            raise ValueError(
                "Password must contain at least one letter and one number"
            )
        return value


class LoginRequest(BaseModel):
    """Incoming payload for POST /api/auth/login."""

    email: EmailStr = Field(max_length=320)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return value.strip().lower()


class UserResponse(BaseModel):
    """Safe user representation returned by the API. Never includes secrets."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    first_name: str
    last_name: str
    is_active: bool
    is_verified: bool
    is_onboarded: bool
    created_at: datetime


class LoginResponse(BaseModel):
    """Returned on successful login. The JWT is set as an httpOnly cookie;
    it is intentionally NOT included in the body."""

    user: UserResponse
