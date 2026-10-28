"""Compatibility alias for :mod:`carlina.core.http_client`."""

from carlina._compat import alias_module as _alias_module
from carlina.core.http_client import (
    DEFAULT_HTTP_TIMEOUT,
    BoundedSession,
    read_bounded_response,
)

__all__ = ["DEFAULT_HTTP_TIMEOUT", "BoundedSession", "read_bounded_response"]

_alias_module(__name__, "carlina.core.http_client")
