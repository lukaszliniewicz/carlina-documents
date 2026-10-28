"""Canonical process-environment contracts used by Carlina runtimes."""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping

from carlina.product_identity import (
    LEGACY_ENVIRONMENT_PREFIX,
    PRODUCT_IDENTITY,
    product_environment_value,
)

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
RUNTIME_ROLE_SUFFIX = "RUNTIME_ROLE"
WORKER_RUNTIME_ROLE = "worker"
LEGACY_WORKER_PROCESS_SUFFIX = "WORKER_PROCESS"
WORKER_ADDON_ID_SUFFIX = "WORKER_ADDON_ID"


def _normalized_suffix(suffix: str) -> str:
    normalized = str(suffix or "").strip().upper()
    if not normalized:
        raise ValueError("Environment variable suffix cannot be empty.")
    return normalized


def canonical_environment_name(suffix: str) -> str:
    """Return the canonical environment-variable name for ``suffix``."""

    return f"{PRODUCT_IDENTITY.environment_prefix}{_normalized_suffix(suffix)}"


def legacy_environment_name(suffix: str) -> str:
    """Return the compatibility environment-variable name for ``suffix``."""

    return f"{LEGACY_ENVIRONMENT_PREFIX}{_normalized_suffix(suffix)}"


def product_environment_flag(
    suffix: str,
    *,
    default: bool = False,
    environment: Mapping[str, str] | None = None,
    warn_legacy: bool = True,
) -> bool:
    """Read a product-prefixed boolean using the canonical-first contract."""

    raw_value = product_environment_value(
        suffix,
        environment=environment,
        warn_legacy=warn_legacy,
    )
    if raw_value is None:
        return default
    return str(raw_value).strip().lower() in _TRUE_VALUES


def set_product_environment(
    environment: MutableMapping[str, str],
    suffix: str,
    value: object,
    *,
    include_legacy: bool = False,
) -> None:
    """Set a canonical variable and, when required, its transition alias."""

    serialized = str(value)
    environment[canonical_environment_name(suffix)] = serialized
    legacy_name = legacy_environment_name(suffix)
    if include_legacy:
        environment[legacy_name] = serialized
    else:
        environment.pop(legacy_name, None)


def clear_product_environment(
    environment: MutableMapping[str, str],
    suffix: str,
) -> None:
    """Remove both canonical and compatibility names from an environment."""

    environment.pop(canonical_environment_name(suffix), None)
    environment.pop(legacy_environment_name(suffix), None)


def is_worker_process(
    environment: Mapping[str, str] | None = None,
) -> bool:
    """Return whether the current process is a managed worker.

    The old worker marker takes safety precedence during the compatibility
    window: an older parent must never cause a newer child to spawn workers.
    """

    active_environment = environment if environment is not None else os.environ
    legacy_marker = active_environment.get(
        legacy_environment_name(LEGACY_WORKER_PROCESS_SUFFIX)
    )
    if str(legacy_marker or "").strip().lower() in _TRUE_VALUES:
        return True
    runtime_role = product_environment_value(
        RUNTIME_ROLE_SUFFIX,
        environment=environment,
        warn_legacy=False,
    )
    return str(runtime_role or "").strip().lower() == WORKER_RUNTIME_ROLE


def worker_addon_id(
    environment: Mapping[str, str] | None = None,
) -> str:
    """Return the add-on ID assigned to the current worker, if any."""

    return str(
        product_environment_value(
            WORKER_ADDON_ID_SUFFIX,
            default="",
            environment=environment,
            warn_legacy=False,
        )
        or ""
    ).strip()


def mark_worker_environment(
    environment: MutableMapping[str, str],
    *,
    addon_id: str | None = None,
    include_legacy: bool = True,
) -> None:
    """Mark a child as a worker without allowing stale target inheritance."""

    set_product_environment(
        environment,
        RUNTIME_ROLE_SUFFIX,
        WORKER_RUNTIME_ROLE,
    )
    legacy_worker_name = legacy_environment_name(LEGACY_WORKER_PROCESS_SUFFIX)
    if include_legacy:
        environment[legacy_worker_name] = "1"
    else:
        environment.pop(legacy_worker_name, None)
    normalized_addon_id = str(addon_id or "").strip()
    if normalized_addon_id:
        set_product_environment(
            environment,
            WORKER_ADDON_ID_SUFFIX,
            normalized_addon_id,
            include_legacy=include_legacy,
        )
    else:
        clear_product_environment(environment, WORKER_ADDON_ID_SUFFIX)


__all__ = [
    "LEGACY_WORKER_PROCESS_SUFFIX",
    "RUNTIME_ROLE_SUFFIX",
    "WORKER_ADDON_ID_SUFFIX",
    "WORKER_RUNTIME_ROLE",
    "canonical_environment_name",
    "clear_product_environment",
    "is_worker_process",
    "legacy_environment_name",
    "mark_worker_environment",
    "product_environment_flag",
    "set_product_environment",
    "worker_addon_id",
]
