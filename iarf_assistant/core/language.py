"""Compatibility alias for :mod:`carlina.core.language`."""

from carlina._compat import alias_module as _alias_module
from carlina.core.language import normalize_language_tag

__all__ = ["normalize_language_tag"]

_alias_module(__name__, "carlina.core.language")
