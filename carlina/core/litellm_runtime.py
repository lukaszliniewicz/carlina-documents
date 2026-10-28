from __future__ import annotations

import importlib
import threading
from types import ModuleType
from typing import Any

_LOCK = threading.RLock()
_MODULE: ModuleType | None = None
_CONFIGURED = False
_CONFIGURATION_IN_PROGRESS = False


def load_litellm() -> ModuleType:
    """Imports LiteLLM without running application callback configuration."""
    global _MODULE
    if _MODULE is not None:
        return _MODULE
    with _LOCK:
        if _MODULE is None:
            module = importlib.import_module("litellm")
            module.__dict__["drop_params"] = True
            _MODULE = module
    return _MODULE


def get_litellm() -> ModuleType:
    """Returns the lazily imported, process-configured LiteLLM module."""
    global _CONFIGURED
    global _CONFIGURATION_IN_PROGRESS

    module = load_litellm()
    with _LOCK:
        if _CONFIGURED or _CONFIGURATION_IN_PROGRESS:
            return module
        _CONFIGURATION_IN_PROGRESS = True

    try:
        from iarf_assistant.llm_logging import configure_litellm_logging

        configure_litellm_logging(module)
    except Exception:
        with _LOCK:
            _CONFIGURATION_IN_PROGRESS = False
        raise
    else:
        with _LOCK:
            _CONFIGURED = True
            _CONFIGURATION_IN_PROGRESS = False
    return module


def litellm_is_loaded() -> bool:
    with _LOCK:
        return _MODULE is not None


class LazyLiteLLMProxy:
    """Compatibility proxy that imports LiteLLM on first attribute access."""

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        return getattr(get_litellm(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(get_litellm(), name, value)

    def __delattr__(self, name: str) -> None:
        delattr(get_litellm(), name)

    def __dir__(self) -> list[str]:
        return sorted(set(dir(type(self))) | set(dir(get_litellm())))

    def __repr__(self) -> str:
        state = "loaded" if litellm_is_loaded() else "deferred"
        return f"<LazyLiteLLMProxy {state}>"


litellm = LazyLiteLLMProxy()


__all__ = [
    "LazyLiteLLMProxy",
    "get_litellm",
    "litellm",
    "litellm_is_loaded",
    "load_litellm",
]
