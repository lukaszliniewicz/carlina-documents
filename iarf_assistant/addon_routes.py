import json
import logging
import math
import re
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from flask import Blueprint, current_app, jsonify, request
from flask_login import current_user, login_required
from packaging.version import InvalidVersion, Version
from sqlalchemy import func
from sqlalchemy.orm.attributes import flag_modified

from iarf_assistant.core.addons.capabilities import CapabilityCatalog
from iarf_assistant.core.addons.capability_runtime import (
    resolve_persisted_capabilities,
    resolved_contracts_from_manager,
)
from iarf_assistant.core.addons.connections import (
    ConnectionError,
    ConnectionService,
    SecretBroker,
)
from iarf_assistant.core.addons.contracts import AddonContractError
from iarf_assistant.core.addons.doctor import run_addon_doctor
from iarf_assistant.core.addons.installer import (
    AddonInstallError,
    AddonPackageInstaller,
    ArchiveMember,
    extract_addon_archive,
    validate_addon_archive,
)
from iarf_assistant.core.addons.settings import AddonConnectionSettingsUpdate
from iarf_assistant.core.frontend_contracts import (
    FRONTEND_API_VERSION,
    set_frontend_api_version_headers,
)
from iarf_assistant.core.logging_observability import redact_text
from iarf_assistant.extensions import db
from iarf_assistant.models import (
    Addon,
    AddonConnection,
    AddonLifecycleEvent,
    CapabilityBinding,
    Setting,
    User,
)
from iarf_assistant.security import admin_required

addon_blueprint = Blueprint("addon_api", __name__)
ADDON_SETTINGS_MAX_FIELDS = 200
ADDON_SETTING_MAX_TEXT = 100_000
ADDON_SETTING_MAX_SECRET = 10_000


def _addon_operations_module():
    from iarf_assistant.core.addons import operations

    return operations


def configured_addon_catalog_service(app):
    """Load catalog lifecycle services only when a catalog route is used."""

    return _addon_operations_module().configured_addon_catalog_service(app)


def get_addon_operation_service(app):
    """Load durable lifecycle orchestration only when it is requested."""

    return _addon_operations_module().get_addon_operation_service(app)


def serialize_addon_operation(record):
    return _addon_operations_module().serialize_addon_operation(record)


def evaluate_addon_admission(**kwargs):
    from iarf_assistant.core.addons.admission import (
        evaluate_addon_admission as evaluate,
    )

    return evaluate(**kwargs)


def _validated_addon_archive(
    zip_ref: zipfile.ZipFile,
) -> tuple[dict, str, list[tuple[zipfile.ZipInfo, PurePosixPath]]]:
    """Compatibility wrapper around the Add-on API v1 archive validator."""

    validated = validate_addon_archive(zip_ref)
    return (
        validated.manifest,
        validated.addon_id,
        [(member.info, member.relative_path) for member in validated.members],
    )


def _extract_addon_archive(
    zip_ref: zipfile.ZipFile,
    target_dir: Path,
    members: list[tuple[zipfile.ZipInfo, PurePosixPath]],
) -> None:
    """Compatibility wrapper around the bounded Add-on API v1 extractor."""

    extract_addon_archive(
        zip_ref,
        target_dir,
        [ArchiveMember(info, relative_path) for info, relative_path in members],
    )


def _addon_manager():
    return getattr(current_app, "addon_manager", None)


def _resolved_addon_contracts(manager) -> dict:
    return resolved_contracts_from_manager(manager)


def _capability_resolution_payload(
    manager,
    addon_id: str,
    *,
    user_id: int,
) -> dict | None:
    contracts = _resolved_addon_contracts(manager)
    if addon_id not in contracts:
        return None
    return resolve_persisted_capabilities(
        db.session,
        contracts=contracts,
        consumer_addon_id=addon_id,
        user_id=user_id,
    ).to_dict()


def _record_capability_lifecycle_event(
    manager,
    addon_id: str,
    *,
    event: str,
    success: bool,
    detail: str,
    metadata: dict[str, Any],
) -> None:
    lifecycle = getattr(manager, "lifecycle", None)
    addon = db.session.get(Addon, addon_id)
    if lifecycle is None or addon is None:
        return
    try:
        lifecycle.record_event(
            addon,
            event=event,
            success=success,
            detail=detail,
            metadata=metadata,
            actor_user_id=current_user.id,
        )
    except Exception:
        db.session.rollback()
        logging.exception(
            "Failed to journal capability lifecycle event for '%s'.",
            addon_id,
        )


def _serialized_addon_operation(record) -> dict[str, Any]:
    payload = serialize_addon_operation(record)
    registry = getattr(current_app, "log_registry", None)
    if registry is None:
        return payload
    links = []
    for source_id in payload.get("log_source_ids") or []:
        try:
            links.append(registry.get_link_for_source(source_id))
        except KeyError:
            continue
    payload["log_links"] = links
    return payload


def _dependency_review_ids(values: Any) -> set[str]:
    if not isinstance(values, (list, tuple)):
        return set()
    return {
        value.strip()
        for value in values[:100]
        if isinstance(value, str) and value.strip() and len(value) <= 128
    }


def _required_dependency_notice_ids(notices: Any) -> set[str]:
    if not isinstance(notices, list):
        return set()
    return {
        notice.get("id")
        for notice in notices[:50]
        if isinstance(notice, dict)
        and isinstance(notice.get("id"), str)
        and notice.get("review_required", True)
    }


def _resolve_addon_record(addon_id: str, *, require_code: bool = True):
    addon_id = str(addon_id or "").strip()
    if not addon_id:
        return None, (jsonify({"error": "Addon ID is required."}), 400)

    manager = _addon_manager()
    if not manager:
        return None, (jsonify({"error": "Addon manager is not initialized."}), 503)

    if require_code and addon_id not in (manager.manifests or {}):
        return None, (jsonify({"error": f"Addon '{addon_id}' is not installed."}), 404)

    addon_record = db.session.get(Addon, addon_id)
    if not addon_record:
        return None, (
            jsonify(
                {"error": f"Addon '{addon_id}' is not registered in the database."}
            ),
            404,
        )

    return addon_record, None


def _frontend_setting_kind(field: dict) -> str:
    declared = (
        str(
            field.get("ui_component")
            or field.get("component")
            or field.get("widget")
            or field.get("type")
            or "text"
        )
        .strip()
        .lower()
    )
    aliases = {
        "bool": "checkbox",
        "boolean": "checkbox",
        "switch": "checkbox",
        "toggle": "checkbox",
        "integer": "number",
        "float": "number",
        "decimal": "number",
        "dropdown": "select",
        "select_input": "select",
        "model": "model_select",
        "llm_model": "model_select",
        "llm_model_select": "model_select",
        "combo": "combobox",
        "autocomplete": "combobox",
        "map": "mapping",
        "object_map": "mapping",
        "key_value_map": "mapping",
        "key_value_list": "mapping",
        "string": "text",
        "text_input": "text",
    }
    return aliases.get(declared, declared)


def _frontend_setting_options(field: dict) -> list[dict[str, str]]:
    raw_options = field.get("options")
    if isinstance(raw_options, dict):
        raw_options = raw_options.get("items")
    if not isinstance(raw_options, list):
        return []

    options = []
    for raw in raw_options[:500]:
        if isinstance(raw, dict):
            value = raw.get("value", raw.get("id", raw.get("key", "")))
            label = raw.get("label", raw.get("name", value))
        else:
            value = raw
            label = raw
        if value is None:
            continue
        value_text = str(value)
        label_text = str(label if label is not None else value)
        if len(value_text) > 1_000 or len(label_text) > 1_000:
            continue
        options.append({"value": value_text, "label": label_text})
    return options


def _frontend_setting_source(field: dict) -> str | None:
    raw_options = field.get("options")
    source = raw_options.get("source") if isinstance(raw_options, dict) else None
    source = (
        source
        or field.get("options_source")
        or field.get("option_source")
        or field.get("source")
    )
    if str(source or "").strip() in {"llm_models", "models", "system_models"}:
        return "llm_models"
    if _frontend_setting_kind(field) == "model_select":
        return "llm_models"
    return None


def _frontend_mapping_column(field: Any, fallback_name: str) -> dict:
    field = field if isinstance(field, dict) else {}
    kind = _frontend_setting_kind(field)
    if kind not in {"text", "combobox", "select"}:
        kind = "text"
    return {
        "name": str(field.get("name") or fallback_name)[:200],
        "kind": kind,
        "placeholder": str(field.get("placeholder") or "")[:500],
        "options": _frontend_setting_options(field),
        "options_source": _frontend_setting_source(field),
    }


def _frontend_sensitive_field(field: dict) -> bool:
    return bool(field.get("sensitive") or _frontend_setting_kind(field) == "password")


def _frontend_setting_value(kind: str, value: Any) -> Any:
    if value is None:
        return ""
    if kind == "checkbox":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    if kind == "mapping":
        parsed = value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except TypeError, ValueError:
                parsed = {}
        if not isinstance(parsed, dict):
            return []
        return [
            {"key": str(key), "value": str(item)}
            for key, item in list(parsed.items())[:100]
        ]
    if isinstance(value, (str, int, float)):
        return value
    return str(value)


def _frontend_number_metadata(value: Any) -> int | float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except TypeError, ValueError:
        return None
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else number


def _frontend_textarea_rows(value: Any) -> int:
    try:
        rows = int(value)
    except TypeError, ValueError:
        rows = 5
    return max(2, min(rows, 30))


def _frontend_addon_manifest(addon_id: str) -> tuple[dict | None, Any]:
    manager = _addon_manager()
    if manager is None:
        return None, None
    loaded = getattr(manager, "loaded_addons", {}) or {}
    addon = loaded.get(addon_id)
    if addon is not None and isinstance(getattr(addon, "manifest", None), dict):
        return addon.manifest, addon
    manifest_record = (getattr(manager, "manifests", {}) or {}).get(addon_id)
    if isinstance(manifest_record, dict):
        manifest = manifest_record.get("manifest", manifest_record)
        if isinstance(manifest, dict):
            return manifest, None
    return None, None


def _connector_definition_payload(
    addon_id: str,
    connector_definition_id: str | None = None,
) -> dict[str, Any] | None:
    """Read one connector definition from the package's immutable contract."""

    manager = _addon_manager()
    contract = (
        _resolved_addon_contracts(manager).get(addon_id)
        if manager is not None
        else None
    )
    if contract is not None:
        referenced = {
            provider.connector_definition
            for provider in contract.capability_providers
            if provider.connector_definition
        }
        definition = next(
            (
                item
                for item in contract.connector_definitions
                if item.id in referenced
                and (
                    connector_definition_id is None
                    or item.id == connector_definition_id
                )
            ),
            None,
        )
        if definition is not None:
            return {
                "id": definition.id,
                "mode": definition.mode,
                "label": definition.label,
                "fields": [
                    {
                        "id": field.id,
                        "name": field.name,
                        "kind": field.kind,
                        "storage": field.storage,
                        "required": field.required,
                        "placeholder": field.placeholder,
                        "description": field.description,
                    }
                    for field in definition.fields
                ],
                "setup_url": definition.setup_url,
                "callback_path": definition.callback_path,
                "disconnect_url": definition.disconnect_url,
                "test_method": definition.test_method,
            }

    # Small test/dev managers may expose raw manifests without package records.
    manifest, _addon = _frontend_addon_manifest(addon_id)
    if not isinstance(manifest, dict):
        return None
    referenced = {
        str(provider.get("connector_definition") or "").strip()
        for provider in ((manifest.get("provides") or {}).get("capabilities") or [])
        if isinstance(provider, dict) and provider.get("connector_definition")
    }
    for raw_definition in manifest.get("connectors") or []:
        if not isinstance(raw_definition, dict):
            continue
        definition_id = str(raw_definition.get("id") or "").strip()
        if definition_id not in referenced:
            continue
        if connector_definition_id and definition_id != connector_definition_id:
            continue
        return {
            "id": definition_id,
            "mode": str(raw_definition.get("mode") or ""),
            "label": str(raw_definition.get("label") or definition_id),
            "fields": [
                dict(item)
                for item in raw_definition.get("fields") or []
                if isinstance(item, dict)
            ],
            "setup_url": str(raw_definition.get("setup_url") or ""),
            "callback_path": str(raw_definition.get("callback_path") or ""),
            "disconnect_url": str(raw_definition.get("disconnect_url") or ""),
            "test_method": str(raw_definition.get("test_method") or ""),
        }
    return None


def _frontend_connection_payload(addon_id: str) -> dict | None:
    definition = _connector_definition_payload(addon_id)
    if definition is None:
        return None

    connections = tuple(
        connection
        for connection in ConnectionService(db.session).visible_to_user(
            current_user.id,
            connector_addon_id=addon_id,
        )
        if connection.connector_definition_id == definition["id"]
    )
    delegated_from = sorted(
        {
            str(connection.owner.email)
            for connection in connections
            if connection.owner_user_id != current_user.id and connection.owner
        }
    )
    items = []
    for connection in connections:
        owned = connection.owner_user_id == current_user.id
        if definition["mode"] == "oauth" and definition["disconnect_url"]:
            delete_url = definition["disconnect_url"]
            delete_method = "POST"
            delete_body = {"connection_id": connection.id}
        else:
            delete_url = f"/api/addon-connections/{connection.id}"
            delete_method = "DELETE"
            delete_body = None
        items.append(
            {
                "id": str(connection.id),
                "name": str(connection.label)[:300],
                "active": bool(connection.is_default),
                "state": str(connection.state),
                "access": "owner" if owned else "delegated",
                "can_manage": owned,
                "health_status": connection.health_status,
                "created_at": (
                    connection.created_at.isoformat() if connection.created_at else None
                ),
                "activate_url": (
                    f"/api/addon-connections/{connection.id}/activate"
                    if owned
                    else None
                ),
                "delete_url": delete_url if owned else None,
                "delete_method": delete_method,
                "delete_body": delete_body,
            }
        )

    callback_url = None
    if definition["callback_path"] and current_user.is_admin:
        callback_url = request.url_root.rstrip("/") + definition["callback_path"]
    mode = definition["mode"]
    return {
        "definition_id": definition["id"],
        "label": definition["label"],
        "mode": mode,
        "delegated_from": ", ".join(delegated_from) or None,
        "items": items,
        "connect_url": (
            definition["setup_url"] if mode in {"oauth", "managed"} else None
        ),
        "callback_url": callback_url,
        "create_url": (
            f"/api/addon-connections/{addon_id}/{definition['id']}"
            if mode == "credentials"
            else None
        ),
        "create_fields": [
            {
                key: value
                for key, value in field.items()
                if key
                in {
                    "id",
                    "name",
                    "kind",
                    "required",
                    "placeholder",
                    "description",
                }
            }
            for field in definition["fields"]
        ],
        "tested_on_create": bool(definition["test_method"]),
    }


def _frontend_addon_settings_payload(addon_id: str) -> dict:
    manifest, _addon = _frontend_addon_manifest(addon_id)
    if manifest is None:
        raise LookupError(f"Addon '{addon_id}' is not available.")

    raw_schema = manifest.get("settings_schema")
    raw_schema = raw_schema if isinstance(raw_schema, list) else []
    user_settings_root = (
        current_user.addon_settings
        if isinstance(current_user.addon_settings, dict)
        else {}
    )
    user_settings = user_settings_root.get(addon_id)
    user_settings = user_settings if isinstance(user_settings, dict) else {}

    fields = []
    for raw_field in raw_schema[:ADDON_SETTINGS_MAX_FIELDS]:
        if not isinstance(raw_field, dict):
            continue
        field_id = str(raw_field.get("id") or "").strip()
        scope = str(raw_field.get("scope") or "").strip().lower()
        if not field_id or scope not in {"global", "user"}:
            continue
        kind = _frontend_setting_kind(raw_field)
        if kind not in {
            "checkbox",
            "combobox",
            "mapping",
            "model_select",
            "number",
            "password",
            "select",
            "text",
            "textarea",
            "url",
        }:
            kind = "text"
        sensitive = _frontend_sensitive_field(raw_field)
        if scope == "global":
            setting = db.session.get(Setting, f"{addon_id}_{field_id}")
            stored_value = setting.value if setting else None
        else:
            stored_value = user_settings.get(field_id)
        has_stored_value = stored_value is not None and stored_value != ""
        default_value = raw_field.get("default", "")
        visible_value = stored_value if has_stored_value else default_value
        fields.append(
            {
                "id": field_id,
                "name": str(
                    raw_field.get("name") or field_id.replace("_", " ").title()
                )[:300],
                "description": str(raw_field.get("description") or "")[:4_000],
                "kind": kind,
                "scope": scope,
                "scope_label": (
                    "Shared installation" if scope == "global" else "Your account"
                ),
                "section": str(
                    raw_field.get("section")
                    or raw_field.get("group")
                    or ("Shared installation" if scope == "global" else "Your account")
                )[:300],
                "placeholder": str(raw_field.get("placeholder") or "")[:1_000],
                "required": bool(raw_field.get("required", False)),
                "sensitive": sensitive,
                "configured": bool(has_stored_value),
                "editable": bool(scope == "user" or current_user.is_admin),
                "value": (
                    None if sensitive else _frontend_setting_value(kind, visible_value)
                ),
                "options": _frontend_setting_options(raw_field),
                "options_source": _frontend_setting_source(raw_field),
                "minimum": _frontend_number_metadata(raw_field.get("min")),
                "maximum": _frontend_number_metadata(raw_field.get("max")),
                "step": _frontend_number_metadata(raw_field.get("step")),
                "rows": _frontend_textarea_rows(raw_field.get("rows")),
                "requires_restart": bool(raw_field.get("requires_restart", False)),
                "mapping": (
                    {
                        "key": _frontend_mapping_column(
                            raw_field.get("key_field")
                            or raw_field.get("keyField")
                            or raw_field.get("key"),
                            "Key",
                        ),
                        "value": _frontend_mapping_column(
                            raw_field.get("value_field")
                            or raw_field.get("valueField")
                            or raw_field.get("value"),
                            "Value",
                        ),
                        "add_label": str(raw_field.get("add_label") or "Add row")[:200],
                    }
                    if kind == "mapping"
                    else None
                ),
            }
        )

    return {
        "frontend_api_version": FRONTEND_API_VERSION,
        "addon": {
            "id": addon_id,
            "name": str(manifest.get("name") or addon_id)[:300],
            "description": str(manifest.get("description") or "")[:4_000],
        },
        "permissions": {
            "manage_global": bool(current_user.is_admin),
        },
        "fields": fields,
        "connections": _frontend_connection_payload(addon_id),
        "actions": {
            "update_url": f"/api/frontend/settings/addons/{addon_id}",
        },
    }


_SKIP_SETTING = object()


def _coerce_frontend_addon_setting(field: dict, value: Any) -> Any:
    kind = _frontend_setting_kind(field)
    sensitive = _frontend_sensitive_field(field)
    if sensitive and (value is None or str(value) == ""):
        return _SKIP_SETTING

    if kind == "checkbox":
        if isinstance(value, bool):
            return "true" if value else "false"
        normalized = str(value).strip().lower()
        if normalized not in {"0", "1", "false", "true", "no", "yes", "off", "on"}:
            raise ValueError("must be an on/off choice")
        return "true" if normalized in {"1", "true", "yes", "on"} else "false"

    if kind == "number":
        if value is None or value == "":
            if field.get("required"):
                raise ValueError("is required")
            return ""
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("must be a number") from exc
        if not math.isfinite(number):
            raise ValueError("must be a finite number")
        minimum = _frontend_number_metadata(field.get("min"))
        maximum = _frontend_number_metadata(field.get("max"))
        if minimum is not None and number < minimum:
            raise ValueError(f"must be at least {minimum}")
        if maximum is not None and number > maximum:
            raise ValueError(f"must be at most {maximum}")
        return str(value).strip()

    if kind == "mapping":
        if value is None or value == "":
            return "{}"
        if isinstance(value, list):
            mapped = {}
            for row in value[:100]:
                if not isinstance(row, dict):
                    raise TypeError("contains an invalid row")
                key = str(row.get("key") or "").strip()
                item_value = str(row.get("value") or "").strip()
                if not key:
                    continue
                if len(key) > 1_000 or len(item_value) > 10_000:
                    raise ValueError("contains an oversized row")
                mapped[key] = item_value
        elif isinstance(value, dict):
            mapped = {str(key): str(item) for key, item in list(value.items())[:100]}
        else:
            raise ValueError("must be a list of key/value rows")
        return json.dumps(mapped, ensure_ascii=False, sort_keys=True)

    if not isinstance(value, (str, int, float)):
        raise TypeError("must be text")
    normalized = str(value).replace("\x00", "")
    limit = ADDON_SETTING_MAX_SECRET if sensitive else ADDON_SETTING_MAX_TEXT
    if len(normalized) > limit:
        raise ValueError(f"must be no longer than {limit:,} characters")
    if field.get("required") and not normalized.strip():
        raise ValueError("is required")

    options = _frontend_setting_options(field)
    if kind == "select" and options:
        allowed = {option["value"] for option in options}
        if normalized not in allowed:
            raise ValueError("must use one of the available choices")
    return normalized


@addon_blueprint.route("/api/addon-registries", methods=["GET"])
@login_required
@admin_required
def get_addon_registries():
    """Returns the addon registry snapshot for diagnostics."""
    manager = _addon_manager()
    if manager:
        return jsonify(manager.get_registry_manifest())
    return jsonify({})


@addon_blueprint.route("/api/addons/all", methods=["GET"])
@login_required
def get_all_addons():
    """Returns a list of all discovered addons with their state."""
    manager = _addon_manager()
    if manager:
        return jsonify(
            manager.get_all_addons_details(
                requester_user_id=current_user.id,
                requester_is_admin=bool(current_user.is_admin),
            )
        )
    return jsonify([])


@addon_blueprint.route("/api/addons/inventory", methods=["GET"])
@login_required
def get_addon_inventory():
    """Return the compact shell projection without detail/catalogue probes."""

    manager = _addon_manager()
    if manager is None:
        return jsonify(
            {
                "frontend_api_version": FRONTEND_API_VERSION,
                "items": [],
                "count": 0,
                "usage_count_scope": (
                    "installation_wide"
                    if current_user.is_admin
                    else "requester_visible"
                ),
                "cache": None,
            }
        )
    requester_is_admin = bool(current_user.is_admin)
    items = manager.get_addon_inventory_summaries(
        requester_user_id=current_user.id,
        requester_is_admin=requester_is_admin,
    )
    return jsonify(
        {
            "frontend_api_version": FRONTEND_API_VERSION,
            "items": items,
            "count": len(items),
            "usage_count_scope": (
                "installation_wide" if requester_is_admin else "requester_visible"
            ),
            "cache": manager.get_addon_inventory_cache_status(),
        }
    )


@addon_blueprint.route("/api/addons/<addon_id>/detail", methods=["GET"])
@login_required
def get_addon_inventory_detail(addon_id: str):
    """Load one package's runtime/build/provenance detail on demand."""

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", addon_id):
        return jsonify({"error": "A valid Addon ID is required."}), 400
    manager = _addon_manager()
    if manager is None:
        return jsonify({"error": "Addon manager is not initialized."}), 503
    detail = manager.get_addon_detail(
        addon_id,
        requester_user_id=current_user.id,
        requester_is_admin=bool(current_user.is_admin),
    )
    if detail is None:
        return jsonify({"error": "Addon not found."}), 404
    return jsonify({"detail_level": "full", **detail})


@addon_blueprint.route("/api/addons/inventory/cache/reset", methods=["POST"])
@login_required
@admin_required
def reset_addon_inventory_cache():
    """Safely reset only rebuildable manifest projection state."""

    manager = _addon_manager()
    if manager is None:
        return jsonify({"error": "Addon manager is not initialized."}), 503
    manager.invalidate_addon_inventory("operator_reset")
    return jsonify(
        {
            "success": True,
            "cache": manager.get_addon_inventory_cache_status(),
        }
    )


@addon_blueprint.route("/api/addons/catalog", methods=["GET"])
@login_required
def get_addon_catalog():
    """Return the authenticated first-party catalog and deterministic diffs."""

    from iarf_assistant.core.addons.catalog_service import (
        AddonCatalogSourceError,
        catalog_listing,
    )

    try:
        service = configured_addon_catalog_service(current_app)
    except AddonCatalogSourceError as exc:
        return jsonify(
            {
                "error": str(exc),
                "status": "catalog_unavailable",
            }
        ), 503
    if service is None:
        return jsonify(
            {
                "frontend_api_version": FRONTEND_API_VERSION,
                "configured": False,
                "channel": str(
                    current_app.config.get("ADDON_CATALOG_CHANNEL") or "stable"
                ),
                "packages": [],
            }
        )
    try:
        loaded = service.load()
    except AddonCatalogSourceError as exc:
        logging.warning("Add-on catalog unavailable: %s", exc)
        return jsonify(
            {
                "error": str(exc),
                "status": "catalog_unavailable",
            }
        ), 503

    manager = _addon_manager()
    installed: dict[str, dict[str, Any]] = {}
    for addon in Addon.query.all():
        contract = (
            dict(addon.resolved_contract_json)
            if isinstance(addon.resolved_contract_json, dict)
            else {}
        )
        if manager is not None:
            try:
                contract["dependency_notices"] = manager.get_addon_dependency_notices(
                    addon.id
                )
            except AttributeError, KeyError, TypeError, ValueError:
                contract.setdefault("dependency_notices", [])
        installed[addon.id] = {
            "version": addon.version,
            "contract": contract,
        }
    return jsonify(
        {
            "frontend_api_version": FRONTEND_API_VERSION,
            **catalog_listing(loaded, installed=installed),
        }
    )


@addon_blueprint.route("/api/addons/catalog/install", methods=["POST"])
@login_required
@admin_required
def install_addon_from_catalog():
    """Install the exact catalog generation previously reviewed by the user."""

    from iarf_assistant.core.addons.catalog import AddonCatalogError
    from iarf_assistant.core.addons.catalog_service import AddonCatalogSourceError

    addon_operations = _addon_operations_module()

    manager = _addon_manager()
    if manager is None:
        return jsonify({"error": "Addon manager is not initialized."}), 503
    app_paths = current_app.extensions.get("carlina_paths")
    if app_paths is None:
        return jsonify({"error": "Carlina storage paths are not initialized."}), 503
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "A JSON install review is required."}), 400
    addon_id = str(payload.get("addon_id") or "").strip()
    version = str(payload.get("version") or "").strip()
    reviewed_catalog_sha256 = str(payload.get("catalog_sha256") or "").strip()
    reviewed_archive_sha256 = str(payload.get("archive_sha256") or "").strip()
    if not addon_id or not version:
        return jsonify(
            {"error": "Addon ID and reviewed target version are required."}
        ), 400
    if not reviewed_catalog_sha256 or not reviewed_archive_sha256:
        return jsonify(
            {
                "error": (
                    "Catalog and package digests from the review screen are required."
                )
            }
        ), 400

    try:
        service = configured_addon_catalog_service(current_app)
        if service is None:
            raise AddonCatalogSourceError("No add-on catalog source is configured.")
        loaded = service.load()
    except AddonCatalogSourceError as exc:
        return jsonify({"error": str(exc), "status": "catalog_unavailable"}), 503
    try:
        package = loaded.index.package(addon_id)
        release = package.select_release(
            requested_version=version,
            stable=loaded.index.channel == "stable",
        )
    except AddonCatalogError as exc:
        return jsonify({"error": str(exc)}), 400
    if release is None:
        return jsonify({"error": "No compatible package release was found."}), 400
    if (
        loaded.source_sha256 != reviewed_catalog_sha256
        or release.archive.sha256 != reviewed_archive_sha256
    ):
        return jsonify(
            {
                "error": (
                    "The catalog changed after review. Refresh and review the "
                    "current package metadata before installing."
                ),
                "status": "catalog_changed",
                "catalog_sha256": loaded.source_sha256,
                "archive_sha256": release.archive.sha256,
            }
        ), 409

    existing = db.session.get(Addon, addon_id)
    if existing is not None and addon_id in (manager.manifests or {}):
        try:
            if Version(release.version) <= Version(existing.version):
                return jsonify(
                    {
                        "error": (
                            f"Version {existing.version} is already installed; "
                            "catalog installs do not perform downgrades."
                        ),
                        "status": "not_an_upgrade",
                    }
                ), 409
        except InvalidVersion:
            return jsonify({"error": "The installed package version is invalid."}), 409

    release_contract = dict(release.to_dict()["contract"])
    dependency_notices = release_contract.get("dependency_notices")
    dependency_notices = (
        dependency_notices if isinstance(dependency_notices, list) else []
    )
    reviewed_notice_ids = _dependency_review_ids(payload.get("dependency_review"))
    missing_notice_ids = sorted(
        _required_dependency_notice_ids(dependency_notices) - reviewed_notice_ids
    )
    if missing_notice_ids:
        return jsonify(
            {
                "error": (
                    "Review and acknowledge the package's declared dependency "
                    "licenses before installation."
                ),
                "status": "dependency_review_required",
                "addon_id": addon_id,
                "missing_notice_ids": missing_notice_ids,
                "dependency_notices": dependency_notices,
                "catalog_sha256": loaded.source_sha256,
                "archive_sha256": release.archive.sha256,
            }
        ), 409

    has_in_process_runtime = any(
        isinstance(unit, dict) and unit.get("mode") == "in_process"
        for unit in release_contract.get("runtime_units") or []
    )
    has_host_migration_code = isinstance(
        release_contract.get("database_migration"),
        dict,
    )
    if (has_in_process_runtime or has_host_migration_code) and payload.get(
        "approve_native_code"
    ) is not True:
        return jsonify(
            {
                "error": (
                    "Installing host-executed add-on code requires explicit approval of "
                    "full host-code access."
                )
            }
        ), 400

    admission = evaluate_addon_admission(
        paths=app_paths,
        contract=release_contract,
        archive_size=release.archive.size,
        manager=manager,
    )
    if not admission["allowed"]:
        return jsonify(
            {
                "error": " ".join(admission["blocking_reasons"]),
                "status": "admission_failed",
                "admission": admission,
            }
        ), 409

    try:
        operation = get_addon_operation_service(current_app).enqueue_catalog(
            addon_id=addon_id,
            version=release.version,
            catalog_sha256=loaded.source_sha256,
            archive_sha256=release.archive.sha256,
            archive_size=release.archive.size,
            owner_user_id=current_user.id,
            operator_approved=payload.get("approve_native_code") is True,
            reviewed_notice_ids=reviewed_notice_ids,
            admission=admission,
        )
        return jsonify(
            {
                "success": True,
                "message": (f"Add-on '{addon_id}' installation was queued."),
                "addon_id": addon_id,
                "job_id": operation.id,
                "operation": _serialized_addon_operation(operation),
                "catalog_sha256": loaded.source_sha256,
                "catalog_signer_key_id": loaded.signer_key_id,
            }
        ), 202
    except addon_operations.AddonOperationConflict as exc:
        return jsonify(
            {
                "error": str(exc),
                "status": "operation_in_progress",
                "operation_id": exc.operation_id,
            }
        ), 409
    except addon_operations.AddonOperationError as exc:
        return jsonify({"error": str(exc)}), 409


def _normalized_connector_value(field: dict[str, Any], value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field['name']} must be text.")
    normalized = value.strip()
    if field.get("required", True) and not normalized:
        raise ValueError(f"{field['name']} is required.")
    limit = (
        ADDON_SETTING_MAX_SECRET
        if field.get("kind") == "password"
        else ADDON_SETTING_MAX_TEXT
    )
    if len(normalized) > limit:
        raise ValueError(f"{field['name']} is too long.")
    if normalized and field.get("kind") == "url":
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"{field['name']} must be an HTTP or HTTPS URL.")
        normalized = normalized.rstrip("/")
    return normalized


def _connection_test_succeeded(result: Any) -> bool:
    if result is None or result is False:
        return False
    if isinstance(result, dict):
        return not result.get("error") and result.get("success", True) is not False
    if isinstance(result, str) and result.lower().startswith("error"):
        return False
    return bool(result)


@addon_blueprint.route(
    "/api/addon-connections/<connector_addon_id>/<connector_definition_id>",
    methods=["POST"],
)
@login_required
def create_addon_connection(connector_addon_id, connector_definition_id):
    """Create a credential Connection from a validated manifest declaration."""

    definition = _connector_definition_payload(
        connector_addon_id,
        connector_definition_id,
    )
    if definition is None:
        return jsonify({"error": "The connector definition is not available."}), 404
    if definition["mode"] != "credentials":
        return jsonify({"error": "This connector uses its managed setup flow."}), 409
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "A JSON object is required."}), 400

    declared_ids = {field["id"] for field in definition["fields"]}
    unknown = sorted(set(data) - declared_ids)
    if unknown:
        return jsonify(
            {"error": f"Unknown connector fields: {', '.join(unknown)}."}
        ), 400
    try:
        values = {
            field["id"]: _normalized_connector_value(
                field,
                data.get(field["id"], ""),
            )
            for field in definition["fields"]
        }
    except (KeyError, TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400

    label = next(
        (
            values[field["id"]]
            for field in definition["fields"]
            if field.get("storage") == "label"
        ),
        definition["label"],
    )
    config = {
        field["id"]: values[field["id"]]
        for field in definition["fields"]
        if field.get("storage") == "config"
    }
    secret = {
        field["id"]: values[field["id"]]
        for field in definition["fields"]
        if field.get("storage") == "secret"
    }
    manager = _addon_manager()
    try:
        service = ConnectionService(db.session)
        has_default = (
            db.session.query(AddonConnection.id)
            .filter(
                AddonConnection.owner_user_id == current_user.id,
                AddonConnection.connector_addon_id == connector_addon_id,
                AddonConnection.connector_definition_id == connector_definition_id,
                AddonConnection.is_default.is_(True),
            )
            .first()
            is not None
        )
        connection = service.create(
            owner_user_id=current_user.id,
            connector_addon_id=connector_addon_id,
            connector_definition_id=connector_definition_id,
            label=label,
            state="ready",
            is_default=not has_default,
            config=config,
            metadata={"created_via": "manifest_connector_contract"},
        )
        db.session.flush()
        SecretBroker(
            db.session,
            actor_user_id=current_user.id,
            requester_addon_id=connector_addon_id,
        ).store(connection.id, secret)

        tested = False
        if definition["test_method"]:
            _manifest, addon = _frontend_addon_manifest(connector_addon_id)
            if addon is None:
                raise ConnectionError(
                    "Activate the connector before testing a new Connection."
                )
            client = addon.initialize_client(
                {
                    "current_user_id": int(current_user.id),
                    "connection_id": connection.id,
                    "verbose": False,
                }
            )
            test = getattr(client, definition["test_method"], None)
            if not callable(test) or not _connection_test_succeeded(test()):
                raise ConnectionError(
                    "The connector could not authenticate with those credentials."
                )
            tested = True
            connection.health_status = "healthy"
            connection.health_checked_at = datetime.now(UTC)

        db.session.commit()
        if manager is not None:
            _record_capability_lifecycle_event(
                manager,
                connector_addon_id,
                event="connection_created",
                success=True,
                detail=f"Created Connection {connection.id}.",
                metadata={
                    "connection_id": connection.id,
                    "connector_definition_id": connector_definition_id,
                    "tested": tested,
                },
            )
        return jsonify(
            {
                "success": True,
                "tested": tested,
                "connection": connection.to_dict(),
            }
        ), 201
    except (ConnectionError, TypeError, ValueError) as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400
    except Exception:
        db.session.rollback()
        logging.exception(
            "Could not create %s Connection.",
            connector_addon_id,
        )
        return jsonify(
            {"error": "The Connection could not be created or verified."}
        ), 502


@addon_blueprint.route(
    "/api/addon-connections/<connection_id>/activate",
    methods=["POST"],
)
@login_required
def activate_addon_connection(connection_id):
    try:
        connection = ConnectionService(db.session).make_default(
            connection_id,
            owner_user_id=current_user.id,
        )
        db.session.commit()
        return jsonify({"success": True, "connection": connection.to_dict()})
    except ConnectionError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 404


@addon_blueprint.route(
    "/api/addon-connections/<connection_id>",
    methods=["DELETE"],
)
@login_required
def delete_addon_connection(connection_id):
    try:
        connection = ConnectionService(db.session).delete_owned(
            connection_id,
            owner_user_id=current_user.id,
        )
        connector_addon_id = connection.connector_addon_id
        connector_definition_id = connection.connector_definition_id
        db.session.commit()
        manager = _addon_manager()
        if manager is not None:
            _record_capability_lifecycle_event(
                manager,
                connector_addon_id,
                event="connection_deleted",
                success=True,
                detail=f"Deleted Connection {connection_id}.",
                metadata={
                    "connection_id": connection_id,
                    "connector_definition_id": connector_definition_id,
                },
            )
        return jsonify({"success": True})
    except ConnectionError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 404


@addon_blueprint.route("/api/addon-connections", methods=["GET"])
@login_required
def get_addon_connections():
    """Return Connections visible to the current user without secret payloads."""

    connector_addon_id = str(request.args.get("connector_addon_id") or "").strip()
    connections = ConnectionService(db.session).visible_to_user(
        current_user.id,
        connector_addon_id=connector_addon_id or None,
    )
    connection_ids = [connection.id for connection in connections]
    binding_counts: dict[str, int] = {}
    if connection_ids:
        for connection_id, count in (
            db.session.query(
                CapabilityBinding.connection_id,
                func.count(CapabilityBinding.id),
            )
            .filter(
                CapabilityBinding.connection_id.in_(connection_ids),
                CapabilityBinding.status == "enabled",
            )
            .group_by(CapabilityBinding.connection_id)
        ):
            binding_counts[str(connection_id)] = int(count)
    return jsonify(
        {
            "frontend_api_version": FRONTEND_API_VERSION,
            "items": [
                {
                    **connection.to_dict(),
                    "access": (
                        "owner"
                        if connection.owner_user_id == current_user.id
                        else "delegated"
                    ),
                    "binding_count": binding_counts.get(connection.id, 0),
                }
                for connection in connections
            ],
        }
    )


@addon_blueprint.route(
    "/api/addons/<addon_id>/capability-resolution",
    methods=["GET"],
)
@login_required
def get_addon_capability_resolution(addon_id):
    manager = _addon_manager()
    if manager is None:
        return jsonify({"error": "Addon manager is not initialized."}), 503
    try:
        payload = _capability_resolution_payload(
            manager,
            addon_id,
            user_id=current_user.id,
        )
    except LookupError as exc:
        return jsonify({"error": str(exc)}), 404
    if payload is None:
        return jsonify({"error": f"Add-on '{addon_id}' is not available."}), 404
    return jsonify(payload)


@addon_blueprint.route("/api/addons/doctor", methods=["GET"])
@login_required
def get_addon_doctor_report():
    """Return a secret-free, user-scoped add-on diagnostic report."""

    manager = _addon_manager()
    if manager is None:
        return jsonify({"error": "Addon manager is not initialized."}), 503
    report = run_addon_doctor(
        db.session,
        contracts=_resolved_addon_contracts(manager),
        user_id=current_user.id,
        manager=manager,
    )
    return jsonify(
        {
            "frontend_api_version": FRONTEND_API_VERSION,
            **report.to_dict(),
        }
    )


@addon_blueprint.route(
    "/api/addons/<addon_id>/capability-bindings",
    methods=["PUT"],
)
@login_required
def put_addon_capability_binding(addon_id):
    """Validate and persist an explicit provider choice for one requirement."""

    manager = _addon_manager()
    if manager is None:
        return jsonify({"error": "Addon manager is not initialized."}), 503
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "A JSON object is required."}), 400
    contracts = _resolved_addon_contracts(manager)
    consumer = contracts.get(addon_id)
    if consumer is None:
        return jsonify({"error": f"Add-on '{addon_id}' is not available."}), 404

    requirement_id = str(data.get("requirement_id") or "").strip()
    provider_addon_id = str(data.get("provider_addon_id") or "").strip()
    provider_capability_id = str(data.get("provider_capability_id") or "").strip()
    connection_id = str(data.get("connection_id") or "").strip() or None
    selected = data.get("selected", True)
    if not isinstance(selected, bool):
        return jsonify({"error": "selected must be a boolean."}), 400
    requirement = next(
        (
            item
            for item in consumer.capability_requirements
            if item.id == requirement_id
        ),
        None,
    )
    provider_contract = contracts.get(provider_addon_id)
    provider = next(
        (
            item
            for item in (
                provider_contract.capability_providers if provider_contract else ()
            )
            if item.id == provider_capability_id
        ),
        None,
    )
    if requirement is None or provider is None:
        return jsonify(
            {"error": "The requirement or provider declaration is unknown."}
        ), 400
    match = next(
        (
            item
            for item in CapabilityCatalog([provider_contract]).matches(requirement)
            if item.candidate.provider.id == provider.id
        ),
        None,
    )
    if match is None or not match.compatible:
        return jsonify(
            {
                "error": "The selected provider is incompatible with the requirement.",
                "reasons": list(match.reasons) if match else [],
            }
        ), 409
    provider_state = db.session.get(Addon, provider_addon_id)
    if provider_state is None or not provider_state.is_active:
        return jsonify(
            {
                "error": "Activate the selected provider before binding it.",
                "status": "provider_inactive",
            }
        ), 409

    scope = requirement.scope
    if scope == "installation":
        if not current_user.is_admin:
            return jsonify({"error": "Administrator access is required."}), 403
        scope_id = ""
    elif scope == "user":
        scope_id = str(current_user.id)
    else:
        return jsonify(
            {
                "error": (
                    f"Interactive {scope!r} capability bindings are not supported yet."
                )
            }
        ), 409

    if provider.connector_definition:
        connection = (
            db.session.get(AddonConnection, connection_id) if connection_id else None
        )
        visible_ids = {
            item.id
            for item in ConnectionService(db.session).visible_to_user(current_user.id)
        }
        if (
            connection is None
            or connection.id not in visible_ids
            or connection.state != "ready"
            or connection.connector_addon_id != provider_addon_id
            or connection.connector_definition_id != provider.connector_definition
        ):
            return jsonify(
                {
                    "error": "Choose a visible Connection for the selected connector provider.",
                    "status": "connection_required",
                }
            ), 409
    elif connection_id is not None:
        return jsonify({"error": "This provider does not accept a Connection."}), 400

    try:
        if not selected:
            binding_query = db.session.query(CapabilityBinding).filter(
                CapabilityBinding.consumer_addon_id == addon_id,
                CapabilityBinding.requirement_id == requirement.id,
                CapabilityBinding.provider_addon_id == provider_addon_id,
                CapabilityBinding.provider_capability_id == provider.id,
                CapabilityBinding.scope == scope,
                CapabilityBinding.scope_id == scope_id,
                CapabilityBinding.status == "enabled",
            )
            binding_query = (
                binding_query.filter(CapabilityBinding.connection_id == connection_id)
                if connection_id is not None
                else binding_query.filter(CapabilityBinding.connection_id.is_(None))
            )
            disabled_count = binding_query.update(
                {"status": "disabled"},
                synchronize_session=False,
            )
            if disabled_count == 0:
                db.session.rollback()
                return jsonify({"error": "The selected binding is not active."}), 404
            db.session.commit()
            _record_capability_lifecycle_event(
                manager,
                addon_id,
                event="capability_binding_removed",
                success=True,
                detail=f"Removed provider binding for {requirement.id}.",
                metadata={
                    "requirement_id": requirement.id,
                    "provider_addon_id": provider_addon_id,
                    "provider_capability_id": provider.id,
                    "connection_id": connection_id,
                    "scope": scope,
                    "scope_id": scope_id,
                },
            )
            return jsonify(
                {
                    "success": True,
                    "disabled": True,
                    "resolution": _capability_resolution_payload(
                        manager,
                        addon_id,
                        user_id=current_user.id,
                    ),
                }
            )
        if requirement.cardinality != "many":
            (
                db.session.query(CapabilityBinding)
                .filter(
                    CapabilityBinding.consumer_addon_id == addon_id,
                    CapabilityBinding.requirement_id == requirement.id,
                    CapabilityBinding.scope == scope,
                    CapabilityBinding.scope_id == scope_id,
                    CapabilityBinding.status == "enabled",
                )
                .update({"status": "disabled"}, synchronize_session=False)
            )
        binding = ConnectionService(db.session).bind(
            consumer_addon_id=addon_id,
            requirement_id=requirement.id,
            provider_addon_id=provider_addon_id,
            provider_capability_id=provider.id,
            contract_version=consumer.version,
            connection_id=connection_id,
            scope=scope,
            scope_id=scope_id,
            priority=int(data.get("priority") or 0),
            created_by_user_id=current_user.id,
        )
        db.session.commit()
        payload = _capability_resolution_payload(
            manager,
            addon_id,
            user_id=current_user.id,
        )
        _record_capability_lifecycle_event(
            manager,
            addon_id,
            event="capability_binding_saved",
            success=True,
            detail=f"Selected provider binding for {requirement.id}.",
            metadata={
                "requirement_id": requirement.id,
                "provider_addon_id": provider_addon_id,
                "provider_capability_id": provider.id,
                "connection_id": connection_id,
                "scope": scope,
                "scope_id": scope_id,
            },
        )
        return jsonify(
            {
                "success": True,
                "binding_id": binding.id,
                "resolution": payload,
            }
        )
    except (ConnectionError, TypeError, ValueError) as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400
    except Exception:
        db.session.rollback()
        logging.exception("Failed to bind capability for add-on '%s'.", addon_id)
        return jsonify({"error": "Could not save the capability binding."}), 500


@addon_blueprint.route("/api/addons/<addon_id>/lifecycle-events", methods=["GET"])
@login_required
@admin_required
def get_addon_lifecycle_events(addon_id):
    addon_record, error = _resolve_addon_record(addon_id, require_code=False)
    if error:
        return error
    try:
        limit = min(200, max(1, int(request.args.get("limit", 50))))
    except TypeError, ValueError:
        return jsonify({"error": "limit must be an integer from 1 to 200."}), 400
    rows = (
        AddonLifecycleEvent.query.filter_by(addon_id=addon_record.id)
        .order_by(AddonLifecycleEvent.id.desc())
        .limit(limit)
        .all()
    )
    return jsonify([row.to_dict() for row in rows])


@addon_blueprint.route("/api/addons/refresh", methods=["POST"])
@login_required
@admin_required
def refresh_addons():
    """Rescans addon manifests from disk and rebuilds registries without full restart."""
    manager = _addon_manager()
    if not manager:
        return jsonify({"error": "Addon manager is not initialized."}), 503

    try:
        manager._sync_addons_from_filesystem()
        manager.rebuild_registries()
        return jsonify(
            {
                "success": True,
                "message": "Addon discovery refreshed.",
                "discovered_count": len(manager.manifests or {}),
            }
        )
    except Exception as e:
        logging.exception("Failed to refresh addon discovery.")
        return jsonify({"error": str(e)}), 500


@addon_blueprint.route("/api/addons/delegatable", methods=["GET"])
@login_required
def get_delegatable_addons():
    """Returns a list of active addons that have delegatable credentials."""
    manager = _addon_manager()
    if manager:
        all_addons = manager.get_all_addons_details(
            requester_user_id=current_user.id,
            requester_is_admin=bool(current_user.is_admin),
        )
        delegatable_addons = [
            addon
            for addon in all_addons
            if addon["is_active"] and addon.get("delegatable")
        ]
        return jsonify(delegatable_addons)
    return jsonify([])


@addon_blueprint.route("/api/addons/install", methods=["POST"])
@login_required
@admin_required
def install_addon():
    addon_operations = _addon_operations_module()
    app = current_app
    manager = _addon_manager()
    if not manager:
        return jsonify({"error": "Addon manager is not initialized."}), 503
    if "addon_zip" not in request.files:
        return jsonify({"error": "No addon ZIP file provided."}), 400

    file = request.files["addon_zip"]
    if file.filename == "" or not file.filename.lower().endswith(".zip"):
        return jsonify({"error": "Invalid file. Please upload a ZIP file."}), 400

    app_paths = app.extensions.get("carlina_paths")
    if app_paths is None:
        return jsonify({"error": "Carlina storage paths are not initialized."}), 503
    operator_approved = str(
        request.form.get("approve_native_code") or ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    installer = AddonPackageInstaller(app_paths)
    staged = None

    try:
        staged = installer.stage(
            file.stream,
            original_filename=file.filename,
            operator_approved=operator_approved,
        )
        reviewed_notice_ids = _dependency_review_ids(
            request.form.getlist("dependency_review")
        )
        manifest = staged.contract.to_dict(include_manifest=True)["manifest"]
        notices = manager._normalize_dependency_notices(manifest)
        notices = notices if isinstance(notices, list) else []
        missing_notice_ids = sorted(
            _required_dependency_notice_ids(notices) - reviewed_notice_ids
        )
        if missing_notice_ids:
            return jsonify(
                {
                    "error": (
                        "Review and acknowledge the package's declared dependency "
                        "licenses before installation."
                    ),
                    "status": "dependency_review_required",
                    "addon_id": staged.contract.addon_id,
                    "missing_notice_ids": missing_notice_ids,
                    "dependency_notices": notices,
                }
            ), 409

        admission = evaluate_addon_admission(
            paths=app_paths,
            contract=staged.contract,
            archive_size=int(staged.provenance.get("archive_size") or 0) or None,
            manager=manager,
        )
        if not admission["allowed"]:
            return jsonify(
                {
                    "error": " ".join(admission["blocking_reasons"]),
                    "status": "admission_failed",
                    "admission": admission,
                }
            ), 409

        operation = get_addon_operation_service(app).enqueue_upload(
            staged,
            owner_user_id=current_user.id,
            operator_approved=operator_approved,
            reviewed_notice_ids=reviewed_notice_ids,
            admission=admission,
        )
        staged = None
        return jsonify(
            {
                "success": True,
                "message": (f"Add-on '{operation.source_id}' installation was queued."),
                "addon_id": operation.source_id,
                "job_id": operation.id,
                "operation": _serialized_addon_operation(operation),
            }
        ), 202
    except addon_operations.AddonOperationConflict as exc:
        return jsonify(
            {
                "error": str(exc),
                "status": "operation_in_progress",
                "operation_id": exc.operation_id,
            }
        ), 409
    except Exception as e:
        logging.exception("Error installing addon.")
        client_error = isinstance(
            e,
            (
                AddonContractError,
                AddonInstallError,
                addon_operations.AddonOperationError,
                ValueError,
                zipfile.BadZipFile,
            ),
        )
        payload = {"error": str(e)}
        if isinstance(e, AddonContractError):
            payload["diagnostics"] = [item.to_dict() for item in e.diagnostics]
        return jsonify(payload), 400 if client_error else 500
    finally:
        if staged is not None:
            installer.cleanup_stage(staged)


@addon_blueprint.route("/api/addons/operations", methods=["GET"])
@login_required
@admin_required
def list_addon_operations():
    service = get_addon_operation_service(current_app)
    addon_id = str(request.args.get("addon_id") or "").strip() or None
    try:
        limit = int(request.args.get("limit") or 50)
    except TypeError, ValueError:
        return jsonify({"error": "limit must be an integer."}), 400
    records = service.list(limit=limit, addon_id=addon_id)
    return jsonify(
        {
            "items": [_serialized_addon_operation(record) for record in records],
            "count": len(records),
        }
    )


@addon_blueprint.route("/api/addons/operations/<operation_id>", methods=["GET"])
@login_required
@admin_required
def get_addon_operation(operation_id):
    record = get_addon_operation_service(current_app).get(operation_id)
    if record is None:
        return jsonify({"error": "Add-on operation not found."}), 404
    return jsonify(_serialized_addon_operation(record))


@addon_blueprint.route(
    "/api/addons/operations/<operation_id>/cancel",
    methods=["POST"],
)
@login_required
@admin_required
def cancel_addon_operation(operation_id):
    try:
        record = get_addon_operation_service(current_app).request_cancel(
            operation_id,
            requested_by=current_user.id,
        )
    except KeyError:
        return jsonify({"error": "Add-on operation not found."}), 404
    return jsonify(_serialized_addon_operation(record))


@addon_blueprint.route(
    "/api/addons/operations/<operation_id>/retry",
    methods=["POST"],
)
@login_required
@admin_required
def retry_addon_operation(operation_id):
    addon_operations = _addon_operations_module()
    try:
        record = get_addon_operation_service(current_app).retry(
            operation_id,
            requested_by=current_user.id,
        )
    except KeyError:
        return jsonify({"error": "Add-on operation not found."}), 404
    except addon_operations.AddonOperationConflict as exc:
        return jsonify(
            {
                "error": str(exc),
                "status": "operation_in_progress",
                "operation_id": exc.operation_id,
            }
        ), 409
    except addon_operations.AddonOperationError as exc:
        return jsonify({"error": str(exc)}), 409
    return jsonify(_serialized_addon_operation(record)), 202


@addon_blueprint.route("/api/addons/uninstall", methods=["POST"])
@login_required
@admin_required
def uninstall_addon():
    manager = _addon_manager()
    if not manager:
        return jsonify({"error": "Addon manager is not initialized."}), 503
    data = request.json
    addon_id = data.get("addon_id")
    if not addon_id:
        return jsonify({"error": "Addon ID is required."}), 400
    try:
        result = manager.remove_addon_code(addon_id)
        return jsonify(
            {
                "success": True,
                "message": f"Add-on '{addon_id}' code was removed; its data and schema were preserved.",
                "addon_id": addon_id,
                "requires_restart": True,
                "data_preserved": True,
                "lifecycle_state": result["lifecycle_state"],
                "removed_path": result["removed_path"],
            }
        )
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500


@addon_blueprint.route("/api/addons/purge-data", methods=["POST"])
@login_required
@admin_required
def purge_addon_data():
    """Destructively purge one inactive add-on after exact confirmation."""

    manager = _addon_manager()
    if not manager:
        return jsonify({"error": "Addon manager is not initialized."}), 503
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    addon_id = str(data.get("addon_id") or "").strip()
    if not addon_id:
        return jsonify({"error": "Addon ID is required."}), 400
    expected = f"PURGE {addon_id}"
    if str(data.get("confirmation") or "").strip() != expected:
        return jsonify(
            {"error": f"Type '{expected}' exactly to purge add-on data."}
        ), 400
    try:
        result = manager.purge_addon_data(addon_id)
        return jsonify(
            {
                "success": True,
                "message": (
                    f"Add-on '{addon_id}' data was purged after creating a safety backup."
                ),
                "addon_id": addon_id,
                "requires_restart": False,
                **result,
            }
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 409
    except Exception as exc:
        db.session.rollback()
        logging.exception("Failed to purge data for add-on '%s'.", addon_id)
        return jsonify({"error": str(exc)}), 500


@addon_blueprint.route("/api/addons/activate", methods=["POST"])
@login_required
@admin_required
def api_activate_addon():
    manager = _addon_manager()
    if not manager:
        return jsonify({"error": "Addon manager is not initialized."}), 503
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    addon_id = data.get("addon_id")
    if not isinstance(addon_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", addon_id
    ):
        return jsonify({"error": "A valid Addon ID is required."}), 400
    notices = (
        manager.get_addon_dependency_notices(addon_id)
        if hasattr(manager, "get_addon_dependency_notices")
        else []
    )
    notices = notices if isinstance(notices, list) else []
    required_notice_ids = _required_dependency_notice_ids(notices)
    raw_dependency_review = data.get("dependency_review")
    raw_dependency_review = (
        raw_dependency_review if isinstance(raw_dependency_review, list) else []
    )
    acknowledged_notice_ids = _dependency_review_ids(raw_dependency_review)
    missing_notice_ids = sorted(required_notice_ids - acknowledged_notice_ids)
    if missing_notice_ids:
        return jsonify(
            {
                "error": (
                    "Review and acknowledge the declared dependency licenses "
                    "before activation."
                ),
                "status": "dependency_review_required",
                "missing_notice_ids": missing_notice_ids,
                "dependency_notices": notices,
            }
        ), 409
    capability_resolution = _capability_resolution_payload(
        manager,
        addon_id,
        user_id=current_user.id,
    )
    if capability_resolution is not None and not capability_resolution["activatable"]:
        _record_capability_lifecycle_event(
            manager,
            addon_id,
            event="capability_activation_blocked",
            success=False,
            detail="Required capabilities were unresolved during activation.",
            metadata={
                "setup_requests": capability_resolution.get("setup_requests", []),
            },
        )
        return jsonify(
            {
                "error": (
                    "Choose or configure the required capabilities before activation."
                ),
                "status": "capability_setup_required",
                "capability_resolution": capability_resolution,
            }
        ), 409

    app_paths = current_app.extensions.get("carlina_paths")
    if app_paths is not None:
        addon_operations = _addon_operations_module()
        package = (getattr(manager, "_addon_packages", None) or {}).get(addon_id)
        contract = getattr(package, "contract", None)
        if contract is None:
            return jsonify(
                {"error": (f"Validated code for add-on '{addon_id}' is not available.")}
            ), 404
        admission = evaluate_addon_admission(
            paths=app_paths,
            contract=contract,
            archive_size=None,
            manager=manager,
        )
        if not admission["allowed"]:
            return jsonify(
                {
                    "error": " ".join(admission["blocking_reasons"]),
                    "status": "admission_failed",
                    "admission": admission,
                }
            ), 409
        try:
            operation = get_addon_operation_service(current_app).enqueue_activation(
                addon_id=addon_id,
                owner_user_id=current_user.id,
                reviewed_notice_ids=sorted(
                    acknowledged_notice_ids & required_notice_ids
                ),
                admission=admission,
            )
            return jsonify(
                {
                    "success": True,
                    "message": f"Addon '{addon_id}' activation was queued.",
                    "addon_id": addon_id,
                    "job_id": operation.id,
                    "operation": _serialized_addon_operation(operation),
                }
            ), 202
        except addon_operations.AddonOperationConflict as exc:
            return jsonify(
                {
                    "error": str(exc),
                    "status": "operation_in_progress",
                    "operation_id": exc.operation_id,
                }
            ), 409
        except (
            addon_operations.AddonOperationAdmissionError,
            addon_operations.AddonOperationError,
        ) as exc:
            return jsonify({"error": str(exc)}), 409

    # Focused Flask factories without Carlina's path contract retain a direct
    # activation seam for unit tests and embedding compatibility.
    try:
        manager.activate_addon(
            addon_id,
            dependency_review=sorted(acknowledged_notice_ids & required_notice_ids),
        )
        if app_paths is not None:
            from iarf_assistant.core.restart_state import require_restart

            require_restart(app_paths, reason=f"addon:{addon_id}:activation")
        return jsonify(
            {
                "success": True,
                "message": f"Addon '{addon_id}' activated. Please restart the server to apply changes.",
                "addon_id": addon_id,
                "requires_restart": True,
            }
        )
    except Exception as e:
        logging.exception("Failed to activate addon '%s'.", addon_id)
        return jsonify({"error": str(e)}), 500


@addon_blueprint.route("/api/addons/deactivate", methods=["POST"])
@login_required
@admin_required
def api_deactivate_addon():
    manager = _addon_manager()
    if not manager:
        return jsonify({"error": "Addon manager is not initialized."}), 503
    data = request.json
    addon_id = data.get("addon_id")
    if not addon_id:
        return jsonify({"error": "Addon ID is required."}), 400
    try:
        manager.deactivate_addon(addon_id)
        app_paths = current_app.extensions.get("carlina_paths")
        if app_paths is not None:
            from iarf_assistant.core.restart_state import require_restart

            require_restart(app_paths, reason=f"addon:{addon_id}:deactivation")
        return jsonify(
            {
                "success": True,
                "message": f"Addon '{addon_id}' deactivated. Please restart the server to apply changes.",
                "addon_id": addon_id,
                "requires_restart": True,
            }
        )
    except Exception as e:
        logging.exception("Failed to deactivate addon '%s'.", addon_id)
        return jsonify({"error": str(e)}), 500


@addon_blueprint.route("/api/addons/<addon_id>/workers/stop", methods=["POST"])
@login_required
@admin_required
def stop_addon_workers(addon_id):
    addon_record, error = _resolve_addon_record(addon_id)
    if error:
        return error

    manager = _addon_manager()
    try:
        stopped = manager.stop_addon_workers(addon_record.id)
        state = manager.get_worker_state(addon_record.id)
        return jsonify(
            {
                "success": True,
                "message": f"Stopped {stopped} worker process(es) for addon '{addon_record.id}'.",
                "addon_id": addon_record.id,
                "stopped_workers": stopped,
                "worker_state": state,
            }
        )
    except Exception as e:
        logging.exception("Failed to stop workers for addon '%s'.", addon_record.id)
        return jsonify({"error": str(e)}), 500


@addon_blueprint.route("/api/addons/<addon_id>/workers/restart", methods=["POST"])
@login_required
@admin_required
def restart_addon_workers(addon_id):
    addon_record, error = _resolve_addon_record(addon_id)
    if error:
        return error

    if not addon_record.is_active:
        return jsonify(
            {
                "error": f"Addon '{addon_record.id}' is inactive. Activate it before restarting workers."
            }
        ), 409

    data = request.get_json(silent=True) or {}
    num_workers = data.get("num_workers")
    entrypoint = data.get("entrypoint")

    if num_workers is not None:
        try:
            num_workers = int(num_workers)
        except TypeError, ValueError:
            return jsonify(
                {"error": "num_workers must be an integer greater than zero."}
            ), 400
        if num_workers < 1:
            return jsonify({"error": "num_workers must be greater than zero."}), 400

    if entrypoint is not None:
        entrypoint = str(entrypoint).strip()
        if not entrypoint:
            return jsonify({"error": "entrypoint cannot be empty when provided."}), 400

    manager = _addon_manager()
    try:
        details = manager.restart_addon_workers(
            addon_record.id,
            num_workers=num_workers,
            entrypoint=entrypoint,
        )
        return jsonify(
            {
                "success": True,
                "message": (
                    f"Restarted addon '{addon_record.id}' workers using entrypoint "
                    f"'{details['entrypoint']}' with target count {details['requested_workers']}."
                ),
                **details,
            }
        )
    except (ValueError, FileNotFoundError) as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logging.exception("Failed to restart workers for addon '%s'.", addon_record.id)
        return jsonify({"error": str(e)}), 500


@addon_blueprint.route(
    "/api/addons/<addon_id>/workers/<int:pid>/terminate", methods=["POST"]
)
@login_required
@admin_required
def terminate_addon_worker_pid(addon_id, pid):
    addon_record, error = _resolve_addon_record(addon_id)
    if error:
        return error

    manager = _addon_manager()
    try:
        details = manager.terminate_addon_worker_pid(addon_record.id, pid)
        return jsonify(
            {
                "success": True,
                "message": f"Terminated worker PID {pid} for addon '{addon_record.id}'.",
                **details,
            }
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except KeyError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logging.exception(
            "Failed to terminate worker PID %s for addon '%s'.", pid, addon_record.id
        )
        return jsonify({"error": str(e)}), 500


def _scheduled_job_action(addon_id: str, job_id: str, action: str):
    addon_record, error = _resolve_addon_record(addon_id)
    if error:
        return error

    manager = _addon_manager()
    try:
        if action == "pause":
            state = manager.pause_scheduled_job(addon_record.id, job_id)
        elif action == "resume":
            state = manager.resume_scheduled_job(addon_record.id, job_id)
        elif action == "trigger":
            state = manager.trigger_scheduled_job_now(addon_record.id, job_id)
        else:
            return jsonify({"error": "Unsupported scheduled job action."}), 400

        action_label = {
            "pause": "Paused",
            "resume": "Resumed",
            "trigger": "Triggered",
        }.get(action, "Updated")
        return jsonify(
            {
                "success": True,
                "message": f"{action_label} scheduled job '{job_id}' for addon '{addon_record.id}'.",
                "addon_id": addon_record.id,
                "job_id": job_id,
                "scheduled_job_state": state,
            }
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except KeyError as e:
        return jsonify({"error": str(e)}), 404
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        logging.exception(
            "Failed scheduled job action '%s' for addon '%s' job '%s'.",
            action,
            addon_record.id,
            job_id,
        )
        return jsonify({"error": str(e)}), 500


@addon_blueprint.route(
    "/api/addons/<addon_id>/jobs/<path:job_id>/pause", methods=["POST"]
)
@login_required
@admin_required
def pause_addon_scheduled_job(addon_id, job_id):
    return _scheduled_job_action(addon_id, job_id, "pause")


@addon_blueprint.route(
    "/api/addons/<addon_id>/jobs/<path:job_id>/resume", methods=["POST"]
)
@login_required
@admin_required
def resume_addon_scheduled_job(addon_id, job_id):
    return _scheduled_job_action(addon_id, job_id, "resume")


@addon_blueprint.route(
    "/api/addons/<addon_id>/jobs/<path:job_id>/trigger", methods=["POST"]
)
@login_required
@admin_required
def trigger_addon_scheduled_job(addon_id, job_id):
    return _scheduled_job_action(addon_id, job_id, "trigger")


@addon_blueprint.route("/api/addon-settings-schemas", methods=["GET"])
@login_required
def get_addon_settings_schemas():
    """Returns a list of settings schemas from all loaded addons."""
    manager = _addon_manager()
    if manager:
        schemas = manager.get_settings_schemas()
        return jsonify(schemas)
    return jsonify([])


@addon_blueprint.route(
    "/api/frontend/settings/addons/<addon_id>",
    methods=["GET", "PUT"],
)
@login_required
def manage_frontend_addon_settings(addon_id):
    """Read or update one addon's bounded, secret-safe settings contract."""
    addon_record, error = _resolve_addon_record(addon_id)
    if error:
        return error
    manifest, addon = _frontend_addon_manifest(addon_id)
    if manifest is None:
        return jsonify({"error": f"Addon '{addon_id}' is unavailable."}), 404

    if request.method == "GET":
        try:
            payload = _frontend_addon_settings_payload(addon_id)
        except LookupError as exc:
            return jsonify({"error": str(exc)}), 404
        response = jsonify(payload)
        response.headers["Cache-Control"] = "private, no-cache"
        set_frontend_api_version_headers(response)
        return response

    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("values"), dict):
        return jsonify({"error": "A settings value object is required."}), 400
    values = data["values"]
    if not values:
        return jsonify({"error": "No addon settings were provided."}), 400
    if len(values) > ADDON_SETTINGS_MAX_FIELDS:
        return jsonify({"error": "Too many addon settings were provided."}), 400

    schema = manifest.get("settings_schema")
    schema = schema if isinstance(schema, list) else []
    fields = {
        str(field.get("id")): field
        for field in schema[:ADDON_SETTINGS_MAX_FIELDS]
        if isinstance(field, dict)
        and field.get("id")
        and field.get("scope") in {"global", "user"}
    }
    unknown = set(values) - set(fields)
    if unknown:
        return jsonify(
            {"error": f"Unsupported addon setting: {sorted(unknown)[0]}."}
        ), 400

    prepared = {}
    errors = []
    for field_id, value in values.items():
        field = fields[field_id]
        if field.get("scope") == "global" and not current_user.is_admin:
            errors.append(f"{field.get('name') or field_id} is administrator-managed.")
            continue
        try:
            prepared[field_id] = _coerce_frontend_addon_setting(
                field,
                value,
            )
        except (TypeError, ValueError) as exc:
            errors.append(f"{field.get('name') or field_id} {exc}.")
    if errors:
        return jsonify({"error": " ".join(errors[:5])}), 400

    changed_fields = {
        field_id for field_id, value in prepared.items() if value is not _SKIP_SETTING
    }
    if not changed_fields:
        return jsonify(
            {
                "success": True,
                "message": "No secret replacements were supplied.",
                "restart_required": False,
                "settings": _frontend_addon_settings_payload(addon_id),
            }
        )

    try:
        user = db.session.get(User, current_user.id)
        if user is None:
            return jsonify({"error": "Current user was not found."}), 404
        current_settings = dict(user.addon_settings or {})
        addon_user_settings = dict(current_settings.get(addon_id) or {})
        effective_user_values = dict(addon_user_settings)
        connection_settings_hook = (
            getattr(addon, "on_connection_settings_updated", None)
            if addon is not None
            else None
        )

        for field_id in changed_fields:
            field = fields[field_id]
            value = prepared[field_id]
            if field.get("scope") == "global":
                setting_key = f"{addon_id}_{field_id}"
                setting = db.session.get(Setting, setting_key)
                if setting is None:
                    setting = Setting(key=setting_key, value=value)
                    db.session.add(setting)
                else:
                    setting.value = value
            else:
                effective_user_values[field_id] = value
                if callable(connection_settings_hook) and _frontend_sensitive_field(
                    field
                ):
                    addon_user_settings.pop(field_id, None)
                else:
                    addon_user_settings[field_id] = value

        # A connector-aware hook also cleans sensitive legacy values the first
        # time its settings are touched. The complete value set exists only in
        # memory for this transaction.
        if callable(connection_settings_hook):
            for field_id, field in fields.items():
                if field.get("scope") != "user":
                    continue
                if field_id in addon_user_settings:
                    effective_user_values.setdefault(
                        field_id,
                        addon_user_settings[field_id],
                    )
                if _frontend_sensitive_field(field):
                    addon_user_settings.pop(field_id, None)
            connection_settings_hook(
                AddonConnectionSettingsUpdate(
                    addon_id=addon_id,
                    user_id=int(user.id),
                    values=effective_user_values,
                    changed_fields=frozenset(changed_fields),
                )
            )

        if any(
            fields[field_id].get("scope") == "user" for field_id in changed_fields
        ) or callable(connection_settings_hook):
            current_settings[addon_id] = addon_user_settings
            user.addon_settings = current_settings
            flag_modified(user, "addon_settings")
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logging.exception(
            "Could not update frontend addon settings for %s.",
            addon_id,
        )
        return jsonify(
            {"error": (f"Addon settings could not be saved: {redact_text(str(exc))}")}
        ), 500

    if addon is not None and hasattr(addon, "on_settings_updated"):
        try:
            addon.on_settings_updated()
        except Exception:
            logging.exception(
                "Addon %s rejected its post-save settings callback.",
                addon_id,
            )

    restart_required = any(
        bool(fields[field_id].get("requires_restart")) for field_id in changed_fields
    )
    if restart_required:
        app_paths = current_app.extensions.get("carlina_paths")
        if app_paths is not None:
            from iarf_assistant.core.restart_state import require_restart

            require_restart(app_paths, reason=f"addon:{addon_id}:settings")
    response = jsonify(
        {
            "success": True,
            "message": (
                "Settings saved. Restart-required changes are marked below."
                if restart_required
                else "Addon settings saved."
            ),
            "restart_required": restart_required,
            "settings": _frontend_addon_settings_payload(addon_id),
        }
    )
    response.headers["Cache-Control"] = "private, no-cache"
    set_frontend_api_version_headers(response)
    return response


@addon_blueprint.route("/api/addon-settings", methods=["GET", "PUT"])
@login_required
def manage_addon_settings():
    """GETs or PUTs addon settings for the current user and global scope."""
    manager = _addon_manager()
    if not manager:
        return jsonify({"error": "Addon manager is not initialized."}), 503

    if request.method == "GET":
        global_settings = {
            s.key: s.value for s in Setting.query.filter(Setting.key.like("%_%")).all()
        }
        user_settings = current_user.addon_settings or {}
        return jsonify(
            {
                "global": global_settings,
                "user": user_settings,
            }
        )

    data = request.json

    all_addon_schemas = manager.get_settings_schemas()
    valid_global_keys = set()
    global_key_to_addon_id = {}
    valid_user_keys = {}
    custom_ui_addons = set()

    for addon_schema in all_addon_schemas:
        addon_id = addon_schema["addon_id"]
        if addon_schema.get("custom_settings_ui"):
            custom_ui_addons.add(addon_id)
        valid_user_keys[addon_id] = set()
        for field in addon_schema["schema"]:
            if field.get("scope") == "global":
                setting_key = f"{addon_id}_{field['id']}"
                valid_global_keys.add(setting_key)
                global_key_to_addon_id[setting_key] = addon_id
            elif field.get("scope") == "user":
                valid_user_keys[addon_id].add(field["id"])

    if "global" in data and current_user.is_admin:
        for key, value in data["global"].items():
            if key not in valid_global_keys:
                logging.warning(
                    f"Attempted to save invalid global setting '{key}'. Skipping."
                )
                continue

            setting = db.session.get(Setting, key)
            if setting:
                setting.value = value
            else:
                setting = Setting(key=key, value=value)
                db.session.add(setting)

    if "user" in data:
        user = db.session.get(User, current_user.id)
        current_settings = (user.addon_settings or {}).copy()
        for addon_id, settings_to_update in data["user"].items():
            if addon_id not in valid_user_keys:
                logging.warning(
                    f"Attempted to save settings for invalid addon '{addon_id}'. Skipping."
                )
                continue

            if addon_id not in current_settings:
                current_settings[addon_id] = {}

            for key, value in settings_to_update.items():
                is_custom_ui_addon = addon_id in custom_ui_addons
                if not is_custom_ui_addon and key not in valid_user_keys.get(
                    addon_id, set()
                ):
                    logging.warning(
                        f"Attempted to save invalid user setting '{key}' for addon '{addon_id}'. Skipping."
                    )
                    continue

                is_password_field = (
                    "password" in key.lower()
                    or "secret" in key.lower()
                    or "key" in key.lower()
                )
                if is_password_field and not value:
                    continue
                current_settings[addon_id][key] = value

        user.addon_settings = current_settings
        flag_modified(user, "addon_settings")

    try:
        db.session.commit()

        updated_addons = set()
        # Track which addons have settings marked requires_restart that were actually changed.
        restart_required_for = set()

        if "global" in data and current_user.is_admin:
            for key in data["global"].keys():
                addon_id = global_key_to_addon_id.get(key)
                if addon_id:
                    updated_addons.add(addon_id)

        if "user" in data:
            updated_addons.update(data["user"].keys())

        for addon_id in updated_addons:
            addon = manager.loaded_addons.get(addon_id)
            if addon and hasattr(addon, "on_settings_updated"):
                addon.on_settings_updated()

            # Check whether any of the changed keys have requires_restart:true in the schema.
            schema_fields = (
                (addon.manifest.get("settings_schema") or []) if addon else []
            )
            restart_fields = {
                f"{addon_id}_{field['id']}"
                for field in schema_fields
                if field.get("requires_restart")
            }
            changed_global_keys = (
                set(data.get("global", {}).keys()) if current_user.is_admin else set()
            )
            changed_user_keys = {
                f"{addon_id}_{k}"
                for k in (data.get("user", {}).get(addon_id) or {}).keys()
            }
            if restart_fields & (changed_global_keys | changed_user_keys):
                restart_required_for.add(addon_id)

        response_payload: dict = {"success": True, "message": "Settings updated."}
        if restart_required_for:
            response_payload["restart_required_for"] = sorted(restart_required_for)
        return jsonify(response_payload)
    except Exception as e:
        db.session.rollback()
        logging.exception("Error updating addon settings.")
        return jsonify({"error": str(e)}), 500


@addon_blueprint.route("/api/addons/<addon_id>/docker/restart", methods=["POST"])
@login_required
@admin_required
def restart_docker_service(addon_id):
    """
    Stops and removes the running Docker container for *addon_id*.
    The next request that needs the service will reboot it automatically.
    This is the recommended way to apply settings that have requires_restart:true.
    """
    addon_record, error = _resolve_addon_record(addon_id)
    if error:
        return error

    if not addon_record.is_active:
        return jsonify({"error": f"Addon '{addon_id}' is not active."}), 409

    manager = _addon_manager()
    if not manager:
        return jsonify({"error": "Addon manager is not initialized."}), 503

    addon = manager.loaded_addons.get(addon_id)
    if not addon or not addon.manifest.get("docker"):
        return jsonify(
            {"error": f"Addon '{addon_id}' does not have a Docker service."}
        ), 400

    try:
        manager._stop_and_remove_container(
            addon_id, remove_image=False, manifest=addon.manifest
        )
        return jsonify(
            {
                "success": True,
                "message": f"Docker service for '{addon_id}' stopped. It will restart automatically on next use.",
            }
        )
    except Exception as e:
        logging.exception("Failed to restart Docker service for addon '%s'.", addon_id)
        return jsonify({"error": str(e)}), 500


def register_addon_blueprint(app):
    app.register_blueprint(addon_blueprint)
