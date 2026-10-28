from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path(__file__).with_name("manifest_schema.json")
EXPLICIT_MIGRATED = "explicit_migrated"
STRICT_EXPLICIT_LEGACY_FIELDS = {
    "addon_type",
    "plugin_prompt",
    "automation_prompts",
    "helpers",
    "ui_entry_points",
    "ui_surfaces",
}
VALID_FRONTEND_TIERS = {
    "schema",
    "jinja",
    "server_workspace",
    "compiled_workspace",
    "hybrid",
    "external_service",
}
VALID_DOCKER_STRATEGIES = {"pull", "build"}
VALID_DOCKER_LIFECYCLES = {"on_demand", "always_on"}
VALID_GPU_REQUIREMENTS = {"none", "optional", "required"}
VALID_ADDON_KINDS = {"application", "toolkit", "connector", "engine", "component"}
VALID_DISTRIBUTION_CHANNELS = {
    "built_in",
    "first_party",
    "community",
    "private",
    "local_development",
}
VALID_RUNTIME_MODES = {"in_process", "managed_process", "container", "remote"}
VALID_UI_MODES = {
    "none",
    "schema",
    "svelte_application",
    "svelte_component",
    "external",
}
VALID_WORKER_EXECUTION_ENVIRONMENTS = {"app", "addon_pixi"}
VALID_APP_RUNTIME_BRIDGES = {"none", "host_v1"}


@lru_cache(maxsize=8)
def load_manifest_schema(path: Path | None = None) -> dict[str, Any]:
    """Load each immutable schema file once per process."""

    return json.loads((path or SCHEMA_PATH).read_text(encoding="utf-8"))


def _validate_docker_service(path: str, service: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    strategy = service.get("strategy", "pull")
    if strategy not in VALID_DOCKER_STRATEGIES:
        errors.append(f"Field '{path}.strategy' must be one of: build, pull.")

    if strategy == "pull" and service.get("image") is not None and not isinstance(service.get("image"), str):
        errors.append(f"Field '{path}.image' must be a string when provided.")
    if strategy == "build":
        build_context = service.get("build_context")
        if build_context is not None and not isinstance(build_context, str):
            errors.append(f"Field '{path}.build_context' must be a string when provided.")

    for object_field in ("environment", "volumes"):
        value = service.get(object_field)
        if value is not None and not isinstance(value, dict):
            errors.append(f"Field '{path}.{object_field}' must be an object when provided.")

    depends_on = service.get("depends_on")
    if depends_on is not None and (
        not isinstance(depends_on, list)
        or any(not isinstance(item, str) for item in depends_on)
    ):
        errors.append(
            f"Field '{path}.depends_on' must be a list of strings when provided."
        )

    for bool_field in ("primary", "gpu_required"):
        value = service.get(bool_field)
        if value is not None and not isinstance(value, bool):
            errors.append(f"Field '{path}.{bool_field}' must be a boolean when provided.")

    gpu_requirement = service.get("gpu")
    if gpu_requirement is not None and gpu_requirement not in VALID_GPU_REQUIREMENTS:
        errors.append(
            f"Field '{path}.gpu' must be one of: none, optional, required."
        )

    lifecycle = service.get("lifecycle")
    if lifecycle is not None and lifecycle not in VALID_DOCKER_LIFECYCLES:
        errors.append(f"Field '{path}.lifecycle' must be one of: always_on, on_demand.")

    for int_field in ("internal_port", "startup_timeout_seconds", "idle_timeout_seconds"):
        value = service.get(int_field)
        if value is None:
            continue
        if not isinstance(value, int) or value <= 0:
            errors.append(f"Field '{path}.{int_field}' must be a positive integer when provided.")

    for str_field in ("healthcheck_path", "mem_limit", "shm_size"):
        value = service.get(str_field)
        if value is not None and not isinstance(value, str):
            errors.append(f"Field '{path}.{str_field}' must be a string when provided.")

    command = service.get("command")
    if command is not None:
        if isinstance(command, str) or isinstance(command, list) and all(isinstance(item, str) for item in command):
            pass
        else:
            errors.append(f"Field '{path}.command' must be a string or list of strings when provided.")

    return errors


def _validate_docker_config(docker: Any) -> list[str]:
    if docker is None:
        return []
    if not isinstance(docker, dict):
        return ["Field 'docker' must be an object."]

    errors: list[str] = []
    services = docker.get("services")

    if services is not None:
        if not isinstance(services, dict) or not services:
            errors.append("Field 'docker.services' must be a non-empty object when provided.")
        else:
            for service_name, service in services.items():
                service_path = f"docker.services.{service_name}"
                if not isinstance(service_name, str) or not service_name.strip():
                    errors.append("Docker service names must be non-empty strings.")
                    continue
                if not isinstance(service, dict):
                    errors.append(f"Field '{service_path}' must be an object.")
                    continue
                errors.extend(_validate_docker_service(service_path, service))
    else:
        errors.extend(_validate_docker_service("docker", docker))

    for int_field in ("startup_timeout_seconds", "idle_timeout_seconds"):
        value = docker.get(int_field)
        if value is not None and (not isinstance(value, int) or value <= 0):
            errors.append(f"Field 'docker.{int_field}' must be a positive integer when provided.")

    lifecycle = docker.get("lifecycle")
    if lifecycle is not None and lifecycle not in VALID_DOCKER_LIFECYCLES:
        errors.append("Field 'docker.lifecycle' must be one of: always_on, on_demand.")

    build_context = docker.get("build_context")
    if build_context is not None and not isinstance(build_context, str):
        errors.append("Field 'docker.build_context' must be a string when provided.")

    return errors


def validate_manifest_against_schema(manifest: dict[str, Any], schema: dict[str, Any] | None = None) -> list[str]:
    """Small no-dependency validator for the subset enforced by manifest_schema.json."""
    schema = schema or load_manifest_schema()
    errors: list[str] = []

    for field in schema.get("required", []):
        if field not in manifest:
            errors.append(f"Missing required field: {field}")

    for field in ("id", "name", "version"):
        if field in manifest and not isinstance(manifest[field], str):
            errors.append(f"Field '{field}' must be a string.")

    list_fields = (
        "roles",
        "categories",
        "tools",
        "tool_views",
        "ui_components",
        "ui_contributions",
        "providers",
        "services",
        "workers",
        "scheduled_jobs",
        "prompts",
        "guidance",
        "skills",
        "workflows",
        "agent_profiles",
        "settings_schema",
        "permissions",
        "resources",
        "connectors",
    )
    for field in list_fields:
        if field in manifest and not isinstance(manifest[field], list):
            errors.append(f"Field '{field}' must be a list.")

    frontend = manifest.get("frontend")
    if frontend is not None and not isinstance(frontend, dict):
        errors.append("Field 'frontend' must be an object.")
    if isinstance(frontend, dict):
        tier = frontend.get("tier")
        if tier is not None and tier not in VALID_FRONTEND_TIERS:
            errors.append(
                "Field 'frontend.tier' must be one of: "
                + ", ".join(sorted(VALID_FRONTEND_TIERS))
                + "."
            )
        api_version = frontend.get("api_version")
        if api_version is not None and (
            not isinstance(api_version, str)
            or re.fullmatch(r"\^[0-9]+\.[0-9]+", api_version) is None
        ):
            errors.append(
                "Field 'frontend.api_version' must use a compatible range such as '^1.0'."
            )
        module = frontend.get("module")
        if module is not None and (
            not isinstance(module, str)
            or not module.strip()
            or "://" in module
            or module.startswith(("/", "\\"))
            or ".." in Path(module.replace("\\", "/")).parts
        ):
            errors.append(
                "Field 'frontend.module' must be a relative same-addon asset path."
            )
        css_assets = frontend.get("css_assets")
        if css_assets is not None:
            if not isinstance(css_assets, list) or any(
                not isinstance(item, str) for item in css_assets
            ):
                errors.append(
                    "Field 'frontend.css_assets' must be a list of relative same-addon asset paths."
                )
            else:
                for asset in css_assets:
                    if (
                        not asset.strip()
                        or "://" in asset
                        or asset.startswith(("/", "\\"))
                        or ".." in Path(asset.replace("\\", "/")).parts
                    ):
                        errors.append(
                            "Field 'frontend.css_assets' must contain only relative same-addon asset paths."
                        )
                        break

        if "build" in frontend:
            build = frontend.get("build")
            valid_build = (
                isinstance(build, (dict, str))
                or (isinstance(build, list) and all(isinstance(item, str) for item in build))
                or build is True
            )
            if not valid_build:
                errors.append(
                    "Field 'frontend.build' must be an object, command string, command list, or true."
                )

    for index, component in enumerate(manifest.get("ui_components") or []):
        if not isinstance(component, dict):
            errors.append(f"ui_components[{index}] must be an object.")
            continue
        module = component.get("module") or component.get("module_url")
        api_version = component.get("api_version") or component.get("frontend_api_version")
        if module is not None and (
            not isinstance(module, str)
            or not module.strip()
            or "://" in module
            or module.startswith(("/", "\\"))
            or ".." in Path(module.replace("\\", "/")).parts
        ):
            errors.append(
                f"Field 'ui_components[{index}].module' must be a relative same-addon asset path."
            )
        if module and (
            not isinstance(api_version, str)
            or re.fullmatch(r"\^[0-9]+\.[0-9]+", api_version) is None
        ):
            errors.append(
                f"Field 'ui_components[{index}].api_version' must use a compatible range such as '^1.0' when a module is declared."
            )
        for schema_field in ("options_schema", "result_schema"):
            schema_value = component.get(schema_field)
            if schema_value is not None and not isinstance(schema_value, dict):
                errors.append(
                    f"Field 'ui_components[{index}].{schema_field}' must be an object."
                )
        for id_field in ("input_schema_id", "output_schema_id"):
            id_value = component.get(id_field)
            if id_value is not None and (
                not isinstance(id_value, str) or not id_value.strip()
            ):
                errors.append(
                    f"Field 'ui_components[{index}].{id_field}' must be a non-empty string."
                )
        legacy_css_assets = component.get("legacy_css_assets")
        if legacy_css_assets is not None and (
            not isinstance(legacy_css_assets, list)
            or any(not isinstance(item, str) for item in legacy_css_assets)
        ):
            errors.append(
                f"Field 'ui_components[{index}].legacy_css_assets' must be a list of strings."
            )

    dependencies = manifest.get("dependencies")
    if dependencies is not None and not isinstance(dependencies, dict):
        errors.append("Field 'dependencies' must be an object.")

    for object_field in (
        "addon_api",
        "execution",
        "storage",
        "distribution",
        "runtime",
        "ui",
        "provides",
        "requires",
    ):
        value = manifest.get(object_field)
        if value is not None and not isinstance(value, dict):
            errors.append(f"Field '{object_field}' must be an object.")

    errors.extend(_validate_docker_config(manifest.get("docker")))

    schema_version = manifest.get("manifest_schema_version", 1)
    if schema_version == 2:
        kind = manifest.get("kind")
        if kind not in VALID_ADDON_KINDS:
            errors.append(
                "Field 'kind' must be one of: "
                + ", ".join(sorted(VALID_ADDON_KINDS))
                + "."
            )

        distribution = manifest.get("distribution")
        if not isinstance(distribution, dict):
            errors.append("Manifest schema 2 requires a distribution object.")
        elif distribution.get("channel") not in VALID_DISTRIBUTION_CHANNELS:
            errors.append(
                "Field 'distribution.channel' must be one of: "
                + ", ".join(sorted(VALID_DISTRIBUTION_CHANNELS))
                + "."
            )

        runtime = manifest.get("runtime")
        units = runtime.get("units") if isinstance(runtime, dict) else None
        if not isinstance(units, list):
            errors.append(
                "Manifest schema 2 requires runtime.units as a list, including an empty list."
            )
        else:
            for index, unit in enumerate(units):
                if not isinstance(unit, dict):
                    errors.append(f"runtime.units[{index}] must be an object.")
                    continue
                if unit.get("mode") not in VALID_RUNTIME_MODES:
                    errors.append(
                        f"Field 'runtime.units[{index}].mode' must be one of: "
                        + ", ".join(sorted(VALID_RUNTIME_MODES))
                        + "."
                    )

        ui = manifest.get("ui")
        entries = ui.get("entries") if isinstance(ui, dict) else None
        if not isinstance(entries, list):
            errors.append(
                "Manifest schema 2 requires ui.entries as a list, including an empty list."
            )
        else:
            for index, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    errors.append(f"ui.entries[{index}] must be an object.")
                    continue
                if entry.get("mode") not in VALID_UI_MODES:
                    errors.append(
                        f"Field 'ui.entries[{index}].mode' must be one of: "
                        + ", ".join(sorted(VALID_UI_MODES))
                        + "."
                    )

        for block_name in ("provides", "requires"):
            block = manifest.get(block_name)
            capabilities = block.get("capabilities") if isinstance(block, dict) else None
            if not isinstance(capabilities, list):
                errors.append(
                    f"Manifest schema 2 requires {block_name}.capabilities as a list."
                )
        requires = manifest.get("requires")
        if not isinstance(requires, dict) or not isinstance(requires.get("packages"), list):
            errors.append("Manifest schema 2 requires requires.packages as a list.")

        for index, worker in enumerate(manifest.get("workers") or []):
            if not isinstance(worker, dict):
                errors.append(f"workers[{index}] must be an object.")
                continue
            for field in ("id", "entrypoint", "execution_environment"):
                value = worker.get(field)
                if not isinstance(value, str) or not value.strip():
                    errors.append(
                        f"Manifest schema 2 workers[{index}] must declare {field}."
                    )
            execution_environment = worker.get("execution_environment")
            if (
                execution_environment is not None
                and execution_environment not in VALID_WORKER_EXECUTION_ENVIRONMENTS
            ):
                errors.append(
                    f"Field 'workers[{index}].execution_environment' must be one of: "
                    + ", ".join(sorted(VALID_WORKER_EXECUTION_ENVIRONMENTS))
                    + "."
                )
            if worker.get("allow_environment_fallback") is not False:
                errors.append(
                    f"Field 'workers[{index}].allow_environment_fallback' must be false; "
                    "managed workers cannot escape their declared environment."
                )
            app_runtime_bridge = worker.get("app_runtime_bridge", "none")
            if app_runtime_bridge not in VALID_APP_RUNTIME_BRIDGES:
                errors.append(
                    f"Field 'workers[{index}].app_runtime_bridge' must be one of: "
                    + ", ".join(sorted(VALID_APP_RUNTIME_BRIDGES))
                    + "."
                )
            if (
                app_runtime_bridge != "none"
                and execution_environment != "addon_pixi"
            ):
                errors.append(
                    f"Manifest workers[{index}] may declare an app runtime bridge "
                    "only for the addon_pixi execution environment."
                )

    if manifest.get("migration_status") == EXPLICIT_MIGRATED and schema_version == 1:
        if "manifest_schema_version" not in manifest:
            errors.append("Explicit-migrated manifests must declare manifest_schema_version.")
        if not isinstance(manifest.get("roles"), list) or not manifest.get("roles"):
            errors.append("Explicit-migrated manifests must declare non-empty roles.")
        if not isinstance(manifest.get("categories"), list) or not manifest.get("categories"):
            errors.append("Explicit-migrated manifests must declare non-empty categories.")
        if not isinstance(frontend, dict) or not frontend.get("tier"):
            errors.append("Explicit-migrated manifests must declare frontend.tier.")

        legacy_fields = sorted(field for field in STRICT_EXPLICIT_LEGACY_FIELDS if field in manifest)
        if legacy_fields:
            errors.append("Explicit-migrated manifests cannot use legacy fields: " + ", ".join(legacy_fields))

        if isinstance(dependencies, dict):
            if dependencies.get("plugins"):
                errors.append("Explicit-migrated manifests must use dependencies.addons instead of dependencies.plugins.")
            if dependencies.get("helpers"):
                errors.append("Explicit-migrated manifests must use dependencies.ui_components instead of dependencies.helpers.")

    for index, tool in enumerate(manifest.get("tools") or []):
        if not isinstance(tool, dict):
            errors.append(f"tools[{index}] must be an object.")
            continue
        if not tool.get("id"):
            errors.append(f"tools[{index}] must declare id.")

    for index, view in enumerate(manifest.get("tool_views") or []):
        if not isinstance(view, dict):
            errors.append(f"tool_views[{index}] must be an object.")
            continue
        if not view.get("id"):
            errors.append(f"tool_views[{index}] must declare id.")
        if not (view.get("tool") or view.get("tool_id")):
            errors.append(f"tool_views[{index}] must declare tool or tool_id.")
        if not isinstance(view.get("allowed_parameters"), list):
            errors.append(f"tool_views[{index}] must declare allowed_parameters as a list.")

    return errors


def validate_manifest_file(path: Path, schema: dict[str, Any] | None = None) -> list[str]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"Invalid JSON: {exc}"]
    return validate_manifest_against_schema(manifest, schema)
