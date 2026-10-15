"""Pydantic schemas for workspaces and members."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class WorkspaceCreate(BaseModel):
    """Payload to create a new workspace."""

    name: str = Field(min_length=1, max_length=120)
    # DataForSEO Sandbox workspace (free dummy data, isolated from live data).
    is_sandbox: bool = False

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Workspace name is required")
        return value


class WorkspaceUpdate(BaseModel):
    """Partial update of a workspace: rename and/or change its defaults.

    Every field is optional; only the ones provided are changed."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    default_country: str | None = Field(default=None, min_length=2, max_length=2)
    default_language: str | None = Field(default=None, min_length=2, max_length=10)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("Workspace name is required")
        return value

    @field_validator("default_country", "default_language")
    @classmethod
    def _lower(cls, value: str | None) -> str | None:
        return value.strip().lower() if value else value


class WorkspaceResponse(BaseModel):
    """A workspace plus the current user's role and member count."""

    id: uuid.UUID
    name: str
    slug: str
    role: str
    member_count: int
    created_at: datetime
    is_sandbox: bool = False
    default_country: str = "in"
    default_language: str = "en"
    timezone: str = "UTC"


class WorkspaceMemberAdd(BaseModel):
    """Add an existing user to a workspace by email."""

    email: EmailStr = Field(max_length=320)
    role: str = Field(default="member")

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("role")
    @classmethod
    def _validate_role(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in ("admin", "member"):
            raise ValueError("Role must be 'admin' or 'member'")
        return value


class WorkspaceMemberRoleUpdate(BaseModel):
    """Change a member's role (owner is not assignable here)."""

    role: str = Field(default="member")

    @field_validator("role")
    @classmethod
    def _validate_role(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in ("admin", "member"):
            raise ValueError("Role must be 'admin' or 'member'")
        return value


class WorkspaceMemberResponse(BaseModel):
    """A member of a workspace (safe user fields + role)."""

    model_config = ConfigDict(from_attributes=True)

    user_id: uuid.UUID
    email: EmailStr
    first_name: str
    last_name: str
    role: str
    created_at: datetime
