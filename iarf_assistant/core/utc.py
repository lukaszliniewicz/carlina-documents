"""Compatibility alias for :mod:`carlina.core.utc`."""

from carlina._compat import alias_module as _alias_module
from carlina.core.utc import (
    as_utc,
    utc_iso,
    utc_now,
    utc_now_db,
)

__all__ = ["as_utc", "utc_iso", "utc_now", "utc_now_db"]

_alias_module(__name__, "carlina.core.utc")
