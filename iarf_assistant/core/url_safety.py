"""Compatibility alias for :mod:`carlina.core.url_safety`."""

from carlina._compat import alias_module as _alias_module
from carlina.core.url_safety import (
    validate_external_url,
    validate_external_urls,
)

__all__ = ["validate_external_url", "validate_external_urls"]

_alias_module(__name__, "carlina.core.url_safety")
