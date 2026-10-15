"""Pydantic schemas for domains."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DomainCreate(BaseModel):
    """Payload to add a domain to a workspace.

    `url` accepts a bare domain or a full URL; the service normalizes it to a
    hostname. An optional display `name` defaults to the domain.
    """

    url: str = Field(min_length=3, max_length=255)
    name: str | None = Field(default=None, max_length=200)

    @field_validator("url")
    @classmethod
    def _strip_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Domain URL is required")
        return value

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class DomainUpdate(BaseModel):
    """Partial update of a domain: rename and/or change its market + niche.

    Every field is optional; only the ones provided are changed. country/language
    may be cleared to "" to fall back to the workspace default."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    country: str | None = Field(default=None, max_length=2)
    language: str | None = Field(default=None, max_length=10)
    topic_terms: str | None = Field(default=None, max_length=2000)
    brand_terms: str | None = Field(default=None, max_length=2000)
    is_local: bool | None = None

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("Name cannot be empty")
        return value

    @field_validator("country", "language")
    @classmethod
    def _lower(cls, value: str | None) -> str | None:
        # "" is a deliberate clear (fall back to the workspace default).
        return value.strip().lower() if value is not None else None


class DomainResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    domain: str
    country: str | None = None
    language: str | None = None
    topic_terms: str | None = None
    brand_terms: str | None = None
    is_local: bool = False
    created_at: datetime
