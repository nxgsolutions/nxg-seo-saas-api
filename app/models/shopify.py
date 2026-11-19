"""Shopify store connections (one store per workspace domain) and webhook dedupe.

The access token is stored encrypted (``app.core.crypto``) and is never
returned by the API. A connection is only saved after the store's primary or
custom domain has been verified to match the workspace domain.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ShopifyConnection(Base):
    __tablename__ = "shopify_connections"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    # One store per workspace domain.
    domain_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("domains.id", ondelete="CASCADE"), nullable=False, unique=True)
    # The permanent "<name>.myshopify.com" address.
    shop: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    shop_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    primary_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Every host the shop answers on (primary + custom + myshopify).
    shop_hosts: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    access_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Only set for expiring offline tokens.
    refresh_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scopes: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_version: Mapped[str] = mapped_column(String(10), nullable=False)
    # 'active' | 'revoked'
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'active'"))
    installed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    installed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    uninstalled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class ShopifyWebhookEvent(Base):
    """Processed webhook ids, so Shopify's at-least-once retries apply once."""

    __tablename__ = "shopify_webhook_events"

    webhook_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    topic: Mapped[str] = mapped_column(String(100), nullable=False)
    shop: Mapped[str] = mapped_column(String(255), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())


class ShopifyPushLog(Base):
    """One publish (or undo) of a page to Shopify, with before/after values."""

    __tablename__ = "shopify_push_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    page_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("site_pages.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    external_id: Mapped[str] = mapped_column(String(100), nullable=False)
    # 'publish' | 'revert'
    action: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'publish'"))
    before: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    after: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # 'ok' | 'conflict' | 'error'
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    redirect_created: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
