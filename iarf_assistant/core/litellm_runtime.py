"""Compatibility alias for :mod:`carlina.core.litellm_runtime`."""

from carlina._compat import alias_module as _alias_module
from carlina.core.litellm_runtime import (
    LazyLiteLLMProxy,
    get_litellm,
    litellm,
    litellm_is_loaded,
    load_litellm,
)

__all__ = [
    "LazyLiteLLMProxy",
    "get_litellm",
    "litellm",
    "litellm_is_loaded",
    "load_litellm",
]

_alias_module(__name__, "carlina.core.litellm_runtime")
