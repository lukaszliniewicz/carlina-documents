"""HTTP registration and lifecycle dispatch gates for add-on blueprints."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Protocol

from flask import Blueprint, Flask, jsonify, request

logger = logging.getLogger(__name__)


class AddonRouteInstance(Protocol):
    """Loaded add-on surface required by the HTTP adapter."""

    id: str
    blueprint: Blueprint


class AddonRouteManager(Protocol):
    """Runtime state consulted by the HTTP adapter."""

    loaded_addons: Mapping[str, Any]
    _dispatch_enabled_addons: set[str]


def register_addon_blueprints(app: Flask, manager: AddonRouteManager) -> None:
    """Register loaded add-on routes behind the live lifecycle dispatch gate."""

    route_owners: dict[str, str] = app.extensions.setdefault(
        "carlina_addon_route_owners",
        {},
    )
    if not app.extensions.get("carlina_addon_route_guard_registered"):

        @app.before_request
        def guard_addon_route_dispatch():
            endpoint = request.endpoint
            if endpoint is None:
                return None
            addon_id = route_owners.get(endpoint)
            if addon_id is None:
                return None
            active_manager = getattr(app, "addon_manager", manager)
            enabled = getattr(active_manager, "_dispatch_enabled_addons", set())
            if addon_id in enabled:
                return None
            return (
                jsonify(
                    {
                        "error": f"Add-on '{addon_id}' is inactive.",
                        "status": "addon_inactive",
                    }
                ),
                503,
            )

        app.extensions["carlina_addon_route_guard_registered"] = True

    for candidate in manager.loaded_addons.values():
        if not hasattr(candidate, "blueprint"):
            continue
        instance: AddonRouteInstance = candidate
        before = set(app.view_functions)
        app.register_blueprint(instance.blueprint)
        for endpoint in set(app.view_functions) - before:
            route_owners[endpoint] = instance.id
        logger.info(
            "Registered blueprint for add-on %s with prefix %s",
            instance.id,
            instance.blueprint.url_prefix,
        )


__all__ = ["AddonRouteManager", "register_addon_blueprints"]
