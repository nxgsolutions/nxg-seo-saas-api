"""Shopify integration exceptions."""


class ShopifyError(Exception):
    """Base class for Shopify integration errors."""


class ShopifyNotConfiguredError(ShopifyError):
    """App credentials or the token encryption key are not set on the server."""


class ShopifyNotConnectedError(ShopifyError):
    """The domain has no active Shopify connection."""


class InvalidShopError(ShopifyError):
    """The shop value is not a valid <name>.myshopify.com address."""


class ShopifyApiError(ShopifyError):
    """A Shopify API call failed."""


class ShopifyAuthError(ShopifyApiError):
    """The access token was rejected (app uninstalled or token revoked)."""


class ShopifyUserError(ShopifyApiError):
    """A mutation returned userErrors (validation failure on Shopify's side)."""

    def __init__(self, errors: list[dict]) -> None:
        self.errors = errors
        msg = "; ".join(str(e.get("message", e)) for e in errors) or "Shopify rejected the change"
        super().__init__(msg)
