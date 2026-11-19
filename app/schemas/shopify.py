"""Pydantic schemas for the Shopify integration."""

from datetime import datetime

from pydantic import BaseModel, Field


class ShopifyConnectRequest(BaseModel):
    shop: str = Field(..., min_length=1, max_length=255)
    # Where the browser returns after the OAuth callback (relative path only).
    return_path: str = "/dashboard/settings"


class ShopifyConnectResponse(BaseModel):
    install_url: str


class ShopifyStatus(BaseModel):
    configured: bool
    connected: bool
    status: str | None = None  # active | revoked
    shop: str | None = None
    shop_name: str | None = None
    primary_host: str | None = None
    shop_hosts: list[str] = []
    scopes: str | None = None
    installed_at: datetime | None = None
    last_sync_at: datetime | None = None
    last_sync_error: str | None = None
    webhooks_enabled: bool = False


class ShopifyImage(BaseModel):
    id: str
    alt: str = ""
    url: str | None = None


class ShopifyEditorFields(BaseModel):
    """Live values of one Shopify product/page for the editor."""

    external_type: str  # product | page
    external_id: str
    title: str
    body_html: str
    seo_title: str = ""        # empty = storefront falls back to the title
    seo_description: str = ""
    handle: str
    url: str
    published: bool
    updated_at: datetime | None = None
    images: list[ShopifyImage] = []
    admin_url: str | None = None


class ShopifyPublishRequest(BaseModel):
    # Send only what the user edited; omitted (null) fields are left unchanged.
    title: str | None = Field(default=None, max_length=255)
    body_html: str | None = Field(default=None, max_length=500_000)
    seo_title: str | None = Field(default=None, max_length=255)
    seo_description: str | None = Field(default=None, max_length=1000)
    handle: str | None = Field(default=None, max_length=255)
    image_alts: dict[str, str] = Field(default_factory=dict)
    # updated_at the editor loaded; a newer Shopify version -> 409 unless force.
    expected_updated_at: datetime | None = None
    force: bool = False


class ShopifyPublishResponse(BaseModel):
    status: str  # published | unchanged
    changed: list[str] = []
    redirect_created: bool = False
    warnings: list[str] = []
    item: ShopifyEditorFields
    log_id: str | None = None


class ShopifyPushLogOut(BaseModel):
    id: str
    action: str
    status: str
    error: str | None = None
    redirect_created: bool = False
    changed: list[str] = []
    before: dict = {}
    after: dict = {}
    created_at: datetime
    user_email: str | None = None
