from __future__ import annotations

import json
import os
import time
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import urlsplit

from carlina.product_identity import (
    PRODUCT_IDENTITY,
    product_environment_value,
)
from carlina.runtime_composition import RuntimeComposition, resolve_runtime_composition
from carlina.runtime_profiles import RuntimeProfile
from iarf_assistant.models import Setting

from .frontend_builds import inspect_frontend_build
from .frontend_orchestration import (
    FRONTEND_API_VERSION,
    get_runtime_core_frontend,
)
from .startup_readiness import startup_readiness_snapshot

SUPPORTED_FRONTEND_API_MAJORS = (1,)
CORE_FRONTEND_FLAGS = (
    "frontend.shell",
    "frontend.chat",
    "frontend.context_inspector",
    "frontend.run_studio",
    "frontend.settings_profile",
    "frontend.logs",
)
TRUTHY_ENV_VALUES = {"1", "on", "true", "yes"}
FRONTEND_API_VERSION_HEADER = "X-Carlina-Frontend-API-Version"
FRONTEND_IMPLEMENTATION_HEADER = "X-Carlina-Frontend-Implementation"
FRONTEND_STATUS_CACHE_HEADER = "X-Carlina-Frontend-Status-Cache"
_LEGACY_FRONTEND_API_VERSION_HEADER = "X-IARF-Frontend-API-Version"
_LEGACY_FRONTEND_IMPLEMENTATION_HEADER = "X-IARF-Frontend-Implementation"
_LEGACY_FRONTEND_STATUS_CACHE_HEADER = "X-IARF-Frontend-Status-Cache"


def _set_compatible_frontend_header(
    response: Any,
    canonical_name: str,
    legacy_name: str,
    value: Any,
) -> Any:
    serialized = str(value)
    response.headers[canonical_name] = serialized
    response.headers[legacy_name] = serialized
    return response


def set_frontend_api_version_headers(
    response: Any,
    version: str = FRONTEND_API_VERSION,
) -> Any:
    """Publish the canonical frontend API version and transition alias."""

    return _set_compatible_frontend_header(
        response,
        FRONTEND_API_VERSION_HEADER,
        _LEGACY_FRONTEND_API_VERSION_HEADER,
        version,
    )


def set_frontend_implementation_headers(response: Any, value: str) -> Any:
    """Publish the frontend implementation contract on document responses."""

    return _set_compatible_frontend_header(
        response,
        FRONTEND_IMPLEMENTATION_HEADER,
        _LEGACY_FRONTEND_IMPLEMENTATION_HEADER,
        value,
    )


def set_frontend_status_cache_headers(response: Any, value: str) -> Any:
    """Publish safe frontend status-cache diagnostics under both names."""

    return _set_compatible_frontend_header(
        response,
        FRONTEND_STATUS_CACHE_HEADER,
        _LEGACY_FRONTEND_STATUS_CACHE_HEADER,
        value,
    )


def _boolean_map(value: Any) -> dict[str, bool]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): bool(flag)
        for key, flag in value.items()
        if str(key).startswith("frontend.")
    }


def _setting_feature_flags() -> dict[str, bool]:
    try:
        setting = Setting.query.filter_by(key="frontend_feature_flags").first()
    except Exception:
        return {}
    if not setting or not setting.value:
        return {}
    try:
        return _boolean_map(json.loads(setting.value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def evaluate_frontend_feature_flags(
    *,
    addon_ids: list[str] | tuple[str, ...] = (),
    user_settings: dict[str, Any] | None = None,
    global_flags: dict[str, Any] | None = None,
    environment: dict[str, str] | None = None,
    profile: RuntimeProfile = RuntimeProfile.WORKBENCH,
) -> dict[str, bool]:
    names = [
        *CORE_FRONTEND_FLAGS,
        *[f"frontend.addon.{addon_id}" for addon_id in sorted(set(addon_ids))],
    ]
    # Phase 7 closes the rollout window for migrated core routes. Fresh
    # installations therefore use Svelte without requiring a hidden database
    # flag. Addon flags remain independently overrideable while packages can
    # still be activated and rebuilt at runtime.
    evaluated = {name: True for name in names}

    evaluated.update(
        {
            key: value
            for key, value in _boolean_map(global_flags).items()
            if key in evaluated and key.startswith("frontend.addon.")
        }
    )
    core_settings = (
        (user_settings or {}).get("core")
        if isinstance(user_settings, dict)
        else {}
    )
    user_flags = (
        core_settings.get("frontend_feature_flags")
        if isinstance(core_settings, dict)
        else {}
    )
    evaluated.update(
        {
            key: value
            for key, value in _boolean_map(user_flags).items()
            if key in evaluated and key.startswith("frontend.addon.")
        }
    )

    active_environment = environment if environment is not None else os.environ
    emergency_disabled = (
        str(
            product_environment_value(
                "FRONTEND_DISABLE",
                default="",
                environment=active_environment,
            )
        )
        .strip()
        .lower()
        in TRUTHY_ENV_VALUES
    )
    if emergency_disabled:
        return {name: False for name in names}
    if profile is RuntimeProfile.DOCUMENTS:
        for name in (
            "frontend.chat",
            "frontend.context_inspector",
            "frontend.run_studio",
            "frontend.logs",
        ):
            evaluated[name] = False
    return evaluated


def _frontend_composition(
    manager: Any, composition: RuntimeComposition | None
) -> RuntimeComposition:
    if composition is not None:
        return composition
    app = getattr(manager, "app", None)
    selected = (getattr(app, "extensions", {}) or {}).get(
        "carlina_runtime_composition"
    )
    if isinstance(selected, RuntimeComposition):
        return selected
    return resolve_runtime_composition(RuntimeProfile.WORKBENCH, environment={})


def resolve_frontend_feature_flags_for_user(
    *,
    user: Any,
    manager: Any = None,
    global_flags: dict[str, Any] | None = None,
    environment: dict[str, str] | None = None,
    composition: RuntimeComposition | None = None,
) -> dict[str, bool]:
    """Resolve the rollout flags used by both document routes and bootstrap."""
    addon_ids = [
        str(manifest.get("id") or addon_dir.name)
        for manifest, addon_dir in (
            _manager_manifest_records(manager) if manager else []
        )
    ]
    return evaluate_frontend_feature_flags(
        addon_ids=addon_ids,
        user_settings=getattr(user, "addon_settings", None),
        global_flags=(
            global_flags
            if global_flags is not None
            else _setting_feature_flags()
        ),
        environment=environment,
        profile=_frontend_composition(manager, composition).profile,
    )


def normalize_frontend_path(raw_path: Any) -> str:
    value = str(raw_path or "/").strip()
    parsed = urlsplit(value)
    if (
        not value.startswith("/")
        or value.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or "\\" in value
    ):
        return "/"
    return parsed.path or "/"


def frontend_api_major(version_range: Any) -> int | None:
    value = str(version_range or "").strip()
    if value.startswith("^"):
        value = value[1:]
    try:
        major = int(value.split(".", 1)[0])
    except (TypeError, ValueError):
        return None
    return major if major >= 0 else None


def frontend_api_compatible(version_range: Any) -> bool:
    major = frontend_api_major(version_range)
    return major in SUPPORTED_FRONTEND_API_MAJORS


def resolve_addon_asset_url(addon_id: Any, raw_path: Any) -> str | None:
    safe_addon_id = str(addon_id or "").strip()
    value = str(raw_path or "").strip().replace("\\", "/")
    if (
        not safe_addon_id
        or not value
        or "://" in value
        or value.startswith("//")
        or "\\" in safe_addon_id
        or "/" in safe_addon_id
    ):
        return None

    if value.startswith("/"):
        allowed_prefix = f"/addons/{safe_addon_id}/"
        if not value.startswith(allowed_prefix):
            return None
        candidate = value
    else:
        relative = value.removeprefix("static/").lstrip("/")
        candidate = f"/addons/{safe_addon_id}/static/{relative}"

    parsed = urlsplit(candidate)
    parts = PurePosixPath(parsed.path).parts
    if parsed.scheme or parsed.netloc or ".." in parts:
        return None
    return parsed.path


def _surface_route(
    surface: dict[str, Any],
    route_resolver: Callable[[str], str | None] | None = None,
) -> str | None:
    raw_route = surface.get("route")
    if raw_route:
        route = normalize_frontend_path(raw_route)
        return route if route != "/" else None

    view_func = str(surface.get("view_func") or "").strip()
    if not view_func or route_resolver is None:
        return None
    try:
        resolved = route_resolver(view_func)
    except Exception:
        return None
    route = normalize_frontend_path(resolved)
    return route if resolved and route != "/" else None


def _public_contribution(
    contribution: dict[str, Any],
    route_resolver: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    surfaces = contribution.get("surfaces") or []
    routes = [
        route
        for route in (
            _surface_route(surface, route_resolver)
            for surface in surfaces
            if isinstance(surface, dict)
        )
        if route
    ]
    entrypoints = [
        value
        for value in (contribution.get("entrypoints") or [])
        if isinstance(value, dict)
    ]
    entrypoint_kinds = sorted(
        {
            str(entrypoint.get("kind"))
            for entrypoint in entrypoints
            if entrypoint.get("kind")
        }
    )
    navigation_entrypoint = next(
        (
            entrypoint
            for entrypoint in entrypoints
            if entrypoint.get("kind")
            in {"workspace_launcher", "sidebar", "tools_tab"}
        ),
        {},
    )
    presentation_entrypoint = navigation_entrypoint or next(
        (
            entrypoint
            for entrypoint in entrypoints
            if entrypoint.get("kind")
            in {"command_palette", "quick_access"}
        ),
        {},
    )
    component_id = contribution.get("component_id")
    return {
        "id": str(contribution.get("id") or ""),
        "addon_id": str(contribution.get("addon_id") or ""),
        "kind": str(contribution.get("kind") or ""),
        "title": str(contribution.get("title") or contribution.get("name") or ""),
        "description": str(contribution.get("description") or ""),
        "routes": list(dict.fromkeys(routes)),
        "entrypoint_kinds": entrypoint_kinds,
        "component_id": component_id,
        "section": str(
            presentation_entrypoint.get("section")
            or ("Components" if component_id else "Workspaces")
        ),
        "icon": str(presentation_entrypoint.get("icon") or ""),
        "status": str((contribution.get("status") or {}).get("state") or "unknown"),
    }


def build_frontend_navigation(
    *,
    user: Any,
    feature_flags: dict[str, bool],
    profile: RuntimeProfile = RuntimeProfile.WORKBENCH,
    addons: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return the small, server-owned navigation model for the core shell."""

    def item(
        item_id: str,
        label: str,
        href: str,
        icon: str,
        section: str,
        *,
        client_route: bool = False,
    ) -> dict[str, Any]:
        return {
            "id": item_id,
            "label": label,
            "href": href,
            "icon": icon,
            "section": section,
            "client_route": client_route,
        }

    if profile is RuntimeProfile.DOCUMENTS:
        navigation = [
            item("documents", "Documents", "/", "book", "primary", client_route=True)
        ]
        product_links = {
            "doc_processor": ("Extract & review", "/addons/doc_processor/"),
            "translator": ("Translate", "/addons/translator/"),
        }
        for addon in addons or []:
            addon_id = addon["id"]
            if addon_id not in product_links:
                continue
            build = addon.get("build") or {}
            label, href = product_links[addon_id]
            if (
                addon.get("active")
                and addon.get("compatible")
                and addon.get("module_url")
                and addon.get("frontend_api_range")
                and build.get("status") == "built"
                and not build.get("rebuild_needed")
                and feature_flags.get(f"frontend.addon.{addon_id}")
                and href in addon.get("legacy_routes", [])
            ):
                navigation.append(
                    item(f"addon:{addon_id}", label, href, "book", "primary", client_route=True)
                )
        navigation.append(
            item("settings", "Settings", "/settings", "settings", "utility", client_route=True)
        )
        return navigation

    navigation = [
        item(
            "chat",
            "Chat",
            "/",
            "chat",
            "primary",
            client_route=True,
        ),
        item(
            "workspaces",
            "Workspaces",
            "/workspaces",
            "workspaces",
            "primary",
            client_route=True,
        ),
        item(
            "runtime",
            "Runtime",
            "/runs",
            "activity",
            "primary",
            client_route=True,
        ),
        item(
            "knowledge",
            "Knowledge",
            "/knowledge/",
            "book",
            "primary",
            client_route=True,
        ),
    ]
    navigation.append(
        item(
            "settings",
            "Profile & settings",
            "/settings",
            "settings",
            "utility",
            client_route=True,
        )
    )
    if bool(getattr(user, "is_admin", False)):
        navigation.append(
            item(
                "logs",
                "Logs",
                "/logs",
                "logs",
                "utility",
                client_route=True,
            )
        )
    return navigation


def _public_ui_component(component: dict[str, Any]) -> dict[str, Any]:
    addon_id = str(component.get("addon_id") or "")
    component_id = str(component.get("id") or "")
    frontend_api_range = str(
        component.get("api_version")
        or component.get("frontend_api_version")
        or ""
    )
    module_url = resolve_addon_asset_url(
        addon_id,
        component.get("module_url") or component.get("module"),
    )
    css_urls = [
        resolved
        for resolved in (
            resolve_addon_asset_url(addon_id, value)
            for value in (component.get("css_assets") or [])
        )
        if resolved
    ]
    legacy_css_assets = component.get("legacy_css_assets") or (
        component.get("css_assets") if not module_url else []
    )
    legacy_css_urls = [
        resolved
        for resolved in (
            resolve_addon_asset_url(addon_id, value)
            for value in legacy_css_assets
        )
        if resolved
    ]
    legacy_js_urls = [
        resolved
        for resolved in (
            resolve_addon_asset_url(addon_id, value)
            for value in (component.get("js_assets") or [])
        )
        if resolved
    ]
    input_schema = component.get("options_schema")
    output_schema = component.get("result_schema")
    javascript_api = (
        component.get("javascript_api")
        if isinstance(component.get("javascript_api"), dict)
        else {}
    )
    return {
        "id": component_id,
        "addon_id": addon_id,
        "name": str(component.get("name") or component.get("id") or ""),
        "description": str(component.get("description") or ""),
        "frontend_api_range": frontend_api_range or None,
        "compatible": (
            frontend_api_compatible(frontend_api_range)
            if module_url
            else True
        ),
        "module_url": module_url,
        "css_urls": css_urls,
        "input_schema_id": (
            str(component.get("input_schema_id"))
            if component.get("input_schema_id")
            else None
        ),
        "output_schema_id": (
            str(component.get("output_schema_id"))
            if component.get("output_schema_id")
            else None
        ),
        "input_schema": input_schema if isinstance(input_schema, dict) and input_schema else None,
        "output_schema": output_schema if isinstance(output_schema, dict) and output_schema else None,
        "legacy": {
            "css_urls": legacy_css_urls,
            "js_urls": legacy_js_urls,
            "global_names": sorted(
                {
                    str(value)
                    for value in [
                        component.get("legacy_namespace"),
                        component.get("namespace"),
                        component.get("helper_id"),
                        javascript_api.get("legacy_namespace"),
                        javascript_api.get("namespace"),
                    ]
                    if value
                }
            ),
        },
        "status": str((component.get("status") or {}).get("state") or "unknown"),
    }


def _manager_manifest_records(manager: Any) -> list[tuple[dict[str, Any], Path]]:
    records = []
    root = Path(getattr(getattr(manager, "app", None), "root_path", ""))
    addons_root = root / "addons"
    for addon_id, record in sorted((getattr(manager, "manifests", {}) or {}).items()):
        manifest = (
            record.get("manifest")
            if isinstance(record, dict) and isinstance(record.get("manifest"), dict)
            else record
        )
        if isinstance(manifest, dict):
            addon_dir = (
                manager.get_addon_directory(addon_id)
                if hasattr(manager, "get_addon_directory")
                else addons_root / str(addon_id)
            )
            records.append((manifest, Path(addon_dir)))
    return records


def _public_addons(
    manager: Any,
    contributions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    active_ids = set((getattr(manager, "loaded_addons", {}) or {}).keys())
    contribution_routes: dict[str, list[str]] = {}
    for contribution in contributions:
        contribution_routes.setdefault(contribution["addon_id"], []).extend(
            contribution["routes"]
        )

    addons = []
    for manifest, addon_dir in _manager_manifest_records(manager):
        addon_id = str(manifest.get("id") or addon_dir.name)
        frontend = manifest.get("frontend") or {}
        if not isinstance(frontend, dict):
            frontend = {}
        module_url = resolve_addon_asset_url(
            addon_id,
            frontend.get("module_url") or frontend.get("module"),
        )
        css_urls = [
            url
            for url in (
                resolve_addon_asset_url(addon_id, value)
                for value in (frontend.get("css_assets") or [])
            )
            if url
        ]
        api_range = str(frontend.get("api_version") or "")
        if hasattr(manager, "get_frontend_build_status"):
            build_status = manager.get_frontend_build_status(addon_id)
        else:
            build_status = inspect_frontend_build(
                manifest,
                addon_dir,
                verify_sources=False,
            )
        addons.append(
            {
                "id": addon_id,
                "name": str(manifest.get("name") or addon_id),
                "version": str(manifest.get("version") or "0.0.0"),
                "active": addon_id in active_ids,
                "tier": frontend.get("tier"),
                "frontend_api_range": api_range or None,
                "compatible": (
                    frontend_api_compatible(api_range)
                    if module_url
                    else True
                ),
                "module_url": module_url,
                "css_urls": css_urls,
                "legacy_routes": sorted(set(contribution_routes.get(addon_id, []))),
                "build": {
                    "status": build_status.get("status"),
                    "rebuild_needed": bool(build_status.get("rebuild_needed")),
                    "log_source_id": build_status.get("log_source_id"),
                },
            }
        )
    return addons


def build_frontend_bootstrap(
    *,
    user: Any,
    manager: Any,
    current_path: Any,
    project_root: str | Path,
    global_flags: dict[str, Any] | None = None,
    environment: dict[str, str] | None = None,
    route_resolver: Callable[[str], str | None] | None = None,
    timings: dict[str, float] | None = None,
    composition: RuntimeComposition | None = None,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    selected_composition = _frontend_composition(manager, composition)
    registry_started_at = time.perf_counter()
    raw_contributions = (
        manager.get_ui_contribution_manifest()
        if manager and hasattr(manager, "get_ui_contribution_manifest")
        else []
    )
    contributions = [
        _public_contribution(value, route_resolver)
        for value in raw_contributions
        if isinstance(value, dict)
    ]
    raw_components = (
        manager.get_ui_component_manifest()
        if manager and hasattr(manager, "get_ui_component_manifest")
        else []
    )
    components = [
        _public_ui_component(value)
        for value in raw_components
        if isinstance(value, dict)
    ]
    if timings is not None:
        timings["registry_ms"] = (
            time.perf_counter() - registry_started_at
        ) * 1000

    addons_started_at = time.perf_counter()
    addons = _public_addons(manager, contributions) if manager else []
    if timings is not None:
        timings["addons_ms"] = (
            time.perf_counter() - addons_started_at
        ) * 1000

    flags_started_at = time.perf_counter()
    feature_flags = resolve_frontend_feature_flags_for_user(
        user=user,
        manager=manager,
        global_flags=global_flags,
        environment=environment,
        composition=selected_composition,
    )
    if timings is not None:
        timings["feature_flags_ms"] = (
            time.perf_counter() - flags_started_at
        ) * 1000

    core_started_at = time.perf_counter()
    core_status = get_runtime_core_frontend(project_root)
    core_assets = core_status.get("assets") or {}
    runtime_readiness = startup_readiness_snapshot(
        getattr(manager, "app", None) if manager else None
    )
    if timings is not None:
        timings["core_status_ms"] = (
            time.perf_counter() - core_started_at
        ) * 1000

    payload = {
        "frontend_api_version": FRONTEND_API_VERSION,
        "supported_frontend_api_majors": list(
            SUPPORTED_FRONTEND_API_MAJORS
        ),
        "product": PRODUCT_IDENTITY.public_dict(),
        "application": {
            "profile": selected_composition.profile.value,
            "label": (
                "Documents"
                if selected_composition.profile is RuntimeProfile.DOCUMENTS
                else "Workbench"
            ),
            "home_url": "/",
        },
        "current_path": normalize_frontend_path(current_path),
        "user": {
            "id": str(getattr(user, "id", "")),
            "display_name": str(
                getattr(user, "email", None)
                or getattr(user, "display_name", None)
                or "User"
            ),
            "is_admin": bool(getattr(user, "is_admin", False)),
        },
        "feature_flags": feature_flags,
        "navigation": build_frontend_navigation(
            user=user,
            feature_flags=feature_flags,
            profile=selected_composition.profile,
            addons=addons,
        ),
        "capabilities": {
            "same_origin_modules": True,
            "addon_mount_contract": True,
            "ui_component_mount_contract": True,
            "legacy_ui_component_adapter": True,
            "history_api_routing": True,
            "server_driven_navigation": True,
            "assistant_services": selected_composition.assistant_services,
            "runtime_restart": True,
        },
        "runtime_readiness": runtime_readiness,
        "build": {
            "core": {
                "id": core_status.get("build_id"),
                "status": core_status.get("status"),
                "entry_url": core_assets.get("entry_url"),
                "css_urls": list(core_assets.get("css_urls") or []),
            }
        },
        "addons": addons,
        "ui_contributions": contributions,
        "ui_components": components,
    }
    if timings is not None:
        timings["total_ms"] = (
            time.perf_counter() - started_at
        ) * 1000
    return payload


__all__ = [
    "CORE_FRONTEND_FLAGS",
    "FRONTEND_API_VERSION",
    "FRONTEND_API_VERSION_HEADER",
    "FRONTEND_IMPLEMENTATION_HEADER",
    "FRONTEND_STATUS_CACHE_HEADER",
    "SUPPORTED_FRONTEND_API_MAJORS",
    "build_frontend_navigation",
    "build_frontend_bootstrap",
    "evaluate_frontend_feature_flags",
    "frontend_api_compatible",
    "frontend_api_major",
    "normalize_frontend_path",
    "resolve_addon_asset_url",
    "resolve_frontend_feature_flags_for_user",
    "set_frontend_api_version_headers",
    "set_frontend_implementation_headers",
    "set_frontend_status_cache_headers",
]
