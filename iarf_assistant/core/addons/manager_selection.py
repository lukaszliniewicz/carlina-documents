"""Select the explicitly declared add-on manager composition."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from flask import Flask

from ..addon_manager_core import AddonManagerCore


def create_addon_manager(
    app: Flask,
    *,
    manage_lifecycle: bool = True,
    allow_lifecycle_maintenance: bool = False,
    allowed_addon_ids: frozenset[str] | None = None,
    container_services: bool = True,
) -> AddonManagerCore:
    """Lazily import and construct the explicitly selected manager class."""

    if type(container_services) is not bool:
        raise TypeError("Container service selection must be a boolean.")

    if container_services:
        from ..addon_manager import AddonManager

        manager_class = AddonManager
    else:
        from .native_manager import NativeAddonManager

        manager_class = NativeAddonManager

    manager_factory = cast(Callable[..., AddonManagerCore], manager_class)
    return manager_factory(
        app,
        manage_lifecycle=manage_lifecycle,
        allow_lifecycle_maintenance=allow_lifecycle_maintenance,
        allowed_addon_ids=allowed_addon_ids,
        container_services=container_services,
    )
