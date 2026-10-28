"""Compatibility import for Carlina's canonical product identity."""

from carlina.product_identity import (
    LEGACY_ENVIRONMENT_PREFIX,
    PRODUCT_IDENTITY,
    ProductIdentity,
    environment_value,
    product_environment_value,
)

__all__ = [
    "LEGACY_ENVIRONMENT_PREFIX",
    "PRODUCT_IDENTITY",
    "ProductIdentity",
    "environment_value",
    "product_environment_value",
]
