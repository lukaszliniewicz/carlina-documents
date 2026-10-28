from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from flask import Blueprint, current_app, jsonify, request
from flask_login import current_user, login_required
from sqlalchemy import case, func, or_
from sqlalchemy.orm.attributes import flag_modified

from carlina.core.runtime_policy import (
    FULL_ACCESS,
    MAX_TOOL_CALL_ITERATIONS,
    REVIEW,
    resolve_runtime_policy,
)
from iarf_assistant.core.memory_defaults import (
    MEMORY_RECALL_INCLUDE_TIER2_DEFAULT,
    MEMORY_TIER1_LIMIT_ORG,
    MEMORY_TIER1_LIMIT_USER,
    get_default_memory_category_definitions,
)
from iarf_assistant.core.frontend_contracts import (
    FRONTEND_API_VERSION,
    set_frontend_api_version_headers,
)
from .core.model_capabilities import (
    capability_limit_projection,
    resolve_model_capabilities,
)
from iarf_assistant.extensions import db
from iarf_assistant.llm_provider_utils import provider_is_openai_compatible
from iarf_assistant.models import (
    ApiKey,
    CredentialDelegation,
    LLMModel,
    LLMProvider,
    PluginCredential,
    Setting,
    User,
)
from iarf_assistant.prompts import (
    CHIEF_ASSISTANT_DEFAULT_PROMPT,
    DIRECT_ASSISTANT_DEFAULT_PROMPT,
    JUNIOR_ASSISTANT_MANDATE_DEFAULT_PROMPT,
)
from iarf_assistant.security import admin_required

frontend_settings_blueprint = Blueprint("frontend_settings_api", __name__)

MAX_PROVIDER_CONNECTIONS = 100
MAX_PROVIDER_MODELS = 200
MAX_ROUTING_MODELS = 400
MAX_SHARING_PROVIDERS = 100
MAX_SHARING_RECIPIENTS = 200
MAX_DELEGATIONS = 200
IMAGE_DETAIL_VALUES = {
    "auto",
    "high",
    "low",
    "medium",
    "omit",
    "ultra_high",
}
DEFAULT_MODEL_KEYS = {
    "fast": "default_fast_model",
    "medium": "default_medium_model",
    "powerful": "default_powerful_model",
}
MAX_GUIDANCE_PROFILE_CHARS = 40_000
MAX_GUIDANCE_PROMPT_CHARS = 100_000
ASSISTANT_POLICY_TOOL_CALLS_MIN = 1
ASSISTANT_POLICY_TOOL_CALLS_MAX = MAX_TOOL_CALL_ITERATIONS
ASSISTANT_POLICY_MEMORY_LIMIT_MIN = 10
ASSISTANT_POLICY_MEMORY_LIMIT_MAX = 500
GUIDANCE_PROMPTS = {
    "chief_assistant": {
        "setting_key": "prompt_chief_assistant",
        "name": "Chief assistant",
        "description": (
            "Sets the coordinating voice, planning posture, and delegation "
            "standards for complex work."
        ),
        "default": CHIEF_ASSISTANT_DEFAULT_PROMPT,
    },
    "junior_assistant_mandate": {
        "setting_key": "prompt_junior_assistant_mandate",
        "name": "Junior assistant mandate",
        "description": (
            "Defines the boundaries and reporting discipline for delegated "
            "assistant work."
        ),
        "default": JUNIOR_ASSISTANT_MANDATE_DEFAULT_PROMPT,
    },
    "direct_assistant_default": {
        "setting_key": "prompt_direct_assistant_default",
        "name": "Direct assistant",
        "description": (
            "Provides the default system guidance for direct conversations "
            "that do not use a specialist mandate."
        ),
        "default": DIRECT_ASSISTANT_DEFAULT_PROMPT,
    },
}


def _iso(value: Any) -> str | None:
    if not value or not hasattr(value, "isoformat"):
        return None
    rendered = value.isoformat()
    if isinstance(value, datetime) and value.tzinfo is None:
        return f"{rendered}Z"
    return rendered


def _humanize(value: Any, *, fallback: str = "") -> str:
    text = str(value or "").strip().replace("-", " ").replace("_", " ")
    return " ".join(text.split()).title() if text else fallback


def _setting_text(key: str) -> str:
    setting = db.session.get(Setting, key)
    return str(setting.value or "") if setting else ""


def _bounded_setting_int(
    key: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(_setting_text(key).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _setting_bool(key: str, *, default: bool) -> bool:
    raw = _setting_text(key).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _bounded_text(value: Any, *, max_chars: int) -> tuple[str, bool]:
    rendered = str(value or "")
    return rendered[:max_chars], len(rendered) > max_chars


def _guidance_payload() -> dict[str, Any]:
    organization_profile, organization_truncated = _bounded_text(
        _setting_text("org_profile"),
        max_chars=MAX_GUIDANCE_PROFILE_CHARS,
    )
    prompt_payloads = []
    for prompt_id, descriptor in GUIDANCE_PROMPTS.items():
        custom_value = _setting_text(str(descriptor["setting_key"]))
        customized = bool(custom_value.strip())
        effective_value = (
            custom_value if customized else str(descriptor["default"])
        )
        value, truncated = _bounded_text(
            effective_value,
            max_chars=MAX_GUIDANCE_PROMPT_CHARS,
        )
        default_value, default_truncated = _bounded_text(
            descriptor["default"],
            max_chars=MAX_GUIDANCE_PROMPT_CHARS,
        )
        prompt_payloads.append(
            {
                "id": prompt_id,
                "name": str(descriptor["name"]),
                "description": str(descriptor["description"]),
                "value": value,
                "default_value": default_value,
                "customized": customized,
                "truncated": bool(truncated or default_truncated),
            }
        )

    organization_enabled = (
        _setting_text("org_functions_enabled").strip().lower() == "true"
    )
    return {
        "frontend_api_version": FRONTEND_API_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat().replace(
            "+00:00",
            "Z",
        ),
        "summary": {
            "organization_enabled": organization_enabled,
            "customized_prompts": sum(
                1 for prompt in prompt_payloads if prompt["customized"]
            ),
            "total_prompts": len(prompt_payloads),
        },
        "organization": {
            "enabled": organization_enabled,
            "profile": organization_profile,
            "truncated": organization_truncated,
        },
        "prompts": prompt_payloads,
        "actions": {
            "update_url": "/api/frontend/settings/guidance",
        },
        "limits": {
            "organization_profile_chars": MAX_GUIDANCE_PROFILE_CHARS,
            "prompt_chars": MAX_GUIDANCE_PROMPT_CHARS,
        },
    }


def _assistant_policy_payload() -> dict[str, Any]:
    configured_max_tool_calls_text = _setting_text("max_tool_calls")
    policy = resolve_runtime_policy(
        {
            "max_tool_calls": configured_max_tool_calls_text,
            "disable_tool_approvals": _setting_text(
                "disable_tool_approvals"
            ),
        }
    )
    effective_max_tool_calls = int(policy["max_tool_calls"])
    try:
        configured_max_tool_calls = int(configured_max_tool_calls_text)
    except (TypeError, ValueError):
        configured_max_tool_calls = effective_max_tool_calls
    approval_mode = str(policy["approval_profile"])
    user_hot_limit = _bounded_setting_int(
        "memory_tier1_limit_user",
        default=MEMORY_TIER1_LIMIT_USER,
        minimum=ASSISTANT_POLICY_MEMORY_LIMIT_MIN,
        maximum=ASSISTANT_POLICY_MEMORY_LIMIT_MAX,
    )
    organization_hot_limit = _bounded_setting_int(
        "memory_tier1_limit_org",
        default=MEMORY_TIER1_LIMIT_ORG,
        minimum=ASSISTANT_POLICY_MEMORY_LIMIT_MIN,
        maximum=ASSISTANT_POLICY_MEMORY_LIMIT_MAX,
    )
    include_archive = _setting_bool(
        "memory_recall_include_tier2_default",
        default=MEMORY_RECALL_INCLUDE_TIER2_DEFAULT,
    )
    tool_budget_supported = (
        ASSISTANT_POLICY_TOOL_CALLS_MIN
        <= configured_max_tool_calls
        <= ASSISTANT_POLICY_TOOL_CALLS_MAX
    )
    return {
        "frontend_api_version": FRONTEND_API_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat().replace(
            "+00:00",
            "Z",
        ),
        "summary": {
            "approval_mode": approval_mode,
            "tool_budget": effective_max_tool_calls,
            "user_hot_limit": user_hot_limit,
            "archive_recall_default": include_archive,
            "configuration_needs_review": not tool_budget_supported,
        },
        "runtime": {
            "approval_mode": approval_mode,
            "max_tool_calls": configured_max_tool_calls,
            "effective_max_tool_calls": effective_max_tool_calls,
            "tool_budget_supported": tool_budget_supported,
        },
        "memory": {
            "user_hot_limit": user_hot_limit,
            "organization_hot_limit": organization_hot_limit,
            "include_archive_by_default": include_archive,
            "fallback_categories": (
                get_default_memory_category_definitions()
            ),
        },
        "defaults": {
            "runtime": {
                "approval_mode": REVIEW,
                "max_tool_calls": 30,
            },
            "memory": {
                "user_hot_limit": MEMORY_TIER1_LIMIT_USER,
                "organization_hot_limit": MEMORY_TIER1_LIMIT_ORG,
                "include_archive_by_default": (
                    MEMORY_RECALL_INCLUDE_TIER2_DEFAULT
                ),
            },
        },
        "limits": {
            "max_tool_calls": {
                "minimum": ASSISTANT_POLICY_TOOL_CALLS_MIN,
                "maximum": ASSISTANT_POLICY_TOOL_CALLS_MAX,
            },
            "hot_memory": {
                "minimum": ASSISTANT_POLICY_MEMORY_LIMIT_MIN,
                "maximum": ASSISTANT_POLICY_MEMORY_LIMIT_MAX,
            },
        },
        "actions": {
            "update_url": "/api/frontend/settings/policy",
        },
    }


def _set_setting(key: str, value: str) -> None:
    setting = db.session.get(Setting, key)
    if setting:
        setting.value = value
    else:
        db.session.add(Setting(key=key, value=value))


def _endpoint_label(value: Any) -> str | None:
    """Return a useful endpoint label without query credentials or user info."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return "Custom endpoint configured"
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return "Custom endpoint configured"
    host = parsed.hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"
    path = parsed.path.rstrip("/")
    if len(path) > 48:
        path = f"{path[:47]}…"
    return f"{parsed.scheme}://{host}{path}"


def _provider_visible(provider: LLMProvider | None) -> bool:
    return bool(
        provider
        and (
            provider.user_id is None
            or provider.user_id == int(current_user.id)
        )
    )


def _visible_provider_query():
    return LLMProvider.query.filter(
        or_(
            LLMProvider.user_id.is_(None),
            LLMProvider.user_id == int(current_user.id),
        )
    )


def _model_visibility_filter():
    return or_(
        LLMModel.user_id.is_(None),
        LLMModel.user_id == int(current_user.id),
    )


def _key_visibility_filter():
    return or_(
        ApiKey.user_id.is_(None),
        ApiKey.user_id == int(current_user.id),
    )


def _provider_is_user_defined(provider: LLMProvider) -> bool:
    return (
        provider_is_openai_compatible(provider)
        and provider.name not in {"lmstudio", "vllm"}
    )


def _can_manage_provider(provider: LLMProvider) -> bool:
    return bool(
        _provider_is_user_defined(provider)
        and (
            provider.user_id == int(current_user.id)
            or (
                provider.user_id is None
                and bool(getattr(current_user, "is_admin", False))
            )
        )
    )


def _reasoning_levels(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [
        item.strip()
        for item in str(value or "").split(",")
        if item.strip()
    ]


def _model_payload(model: LLMModel) -> dict[str, Any]:
    is_admin = bool(getattr(current_user, "is_admin", False))
    is_personal = model.user_id == int(current_user.id)
    can_edit = bool(is_admin or is_personal)
    capability_profile = resolve_model_capabilities(
        str(model.model_id),
        user_id=int(current_user.id),
        model_record=model,
    )
    return {
        "db_id": int(model.id),
        "id": str(model.model_id),
        "name": str(model.display_name or model.model_id),
        "provider_id": int(model.provider_id),
        "provider_name": str(
            (model.provider.display_name or model.provider.name)
            if model.provider
            else "Unknown provider"
        ),
        "scope": (
            "global"
            if model.user_id is None
            else "personal" if is_personal else "member"
        ),
        "scope_label": (
            "Shared"
            if model.user_id is None
            else "Personal" if is_personal else "Member-specific"
        ),
        "active": bool(model.is_active),
        "manual": bool(model.manually_added),
        "type": str(model.model_type or "chat"),
        "context_window": model.context_window,
        "max_output_tokens": model.max_output_tokens,
        "limits": {
            "input": capability_limit_projection(capability_profile, "input"),
            "output": capability_limit_projection(capability_profile, "output"),
        },
        "capabilities": {
            "vision": bool(model.supports_vision),
            "pdf": bool(model.supports_pdf),
            "reasoning": bool(model.supports_reasoning),
            "audio": bool(model.supports_audio_in),
            "reasoning_levels": _reasoning_levels(
                model.supported_reasoning_levels
            ),
        },
        "embedding": {
            "default_dimensions": model.default_dimensions,
            "supports_mrl": bool(model.supports_mrl),
            "prefix_style": str(model.prefix_style or "none"),
            "has_custom_query_prefix": bool(model.custom_query_prefix),
            "has_custom_passage_prefix": bool(
                model.custom_passage_prefix
            ),
        },
        "costs": {
            "input": model.input_cost_per_million_tokens,
            "cached_input": model.input_cost_per_million_cached_tokens,
            "output": model.output_cost_per_million_tokens,
        },
        "permissions": {
            "edit": can_edit,
            "delete": bool(can_edit and model.manually_added),
            "toggle_active": can_edit,
            "toggle_capabilities": is_admin,
        },
        "actions": {
            "update_url": f"/api/llm/models/{int(model.id)}",
            "delete_url": f"/api/llm/models/{int(model.id)}",
            "toggle_active_url": (
                f"/api/llm/models/{int(model.id)}/toggle-active"
            ),
            "toggle_vision_url": (
                f"/api/llm/models/{int(model.id)}/toggle-vision"
            ),
            "toggle_pdf_url": (
                f"/api/llm/models/{int(model.id)}/toggle-pdf"
            ),
            "toggle_reasoning_url": (
                f"/api/llm/models/{int(model.id)}/toggle-reasoning"
            ),
            "toggle_audio_url": (
                f"/api/llm/models/{int(model.id)}/toggle-audio"
            ),
        },
        "updated_at": _iso(model.updated_at),
    }


def _connection_payload(key: ApiKey) -> dict[str, Any]:
    is_admin = bool(getattr(current_user, "is_admin", False))
    is_personal = key.user_id == int(current_user.id)
    can_manage = bool(is_admin or is_personal)
    try:
        endpoint_label = _endpoint_label(key.api_base)
    except Exception:
        current_app.logger.warning(
            "Could not decrypt endpoint metadata for API key %s.",
            key.id,
        )
        endpoint_label = "Endpoint configured"
    return {
        "id": int(key.id),
        "name": str(key.name or "Connection"),
        "scope": "global" if key.user_id is None else "personal",
        "scope_label": "Shared" if key.user_id is None else "Personal",
        "active": bool(key.is_active),
        "endpoint_label": endpoint_label,
        "endpoint_configured": bool(endpoint_label),
        "api_version": str(key.api_version or "") or None,
        "created_at": _iso(key.created_at),
        "permissions": {
            "update": can_manage,
            "delete": can_manage,
            "activate": bool(
                is_admin if key.user_id is None else is_personal
            ),
        },
        "actions": {
            "update_url": f"/api/llm/keys/{int(key.id)}",
            "delete_url": f"/api/llm/keys/{int(key.id)}",
            "activate_url": (
                f"/api/llm/keys/{int(key.id)}/set-active"
            ),
        },
    }


def _provider_payload(
    provider: LLMProvider,
    *,
    connection_count: int,
    active_connection_count: int,
    model_count: int,
    active_model_count: int,
) -> dict[str, Any]:
    connected = active_connection_count > 0
    available = connection_count > 0
    return {
        "id": int(provider.id),
        "key": str(provider.name),
        "name": str(provider.display_name or _humanize(provider.name)),
        "scope": "global" if provider.user_id is None else "personal",
        "scope_label": (
            "Shared provider"
            if provider.user_id is None
            else "Personal endpoint"
        ),
        "kind": str(provider.provider_kind or "native"),
        "kind_label": (
            "OpenAI-compatible endpoint"
            if provider_is_openai_compatible(provider)
            else _humanize(provider.provider_kind, fallback="Native provider")
        ),
        "discovery": str(provider.discovery_type or "manual"),
        "discovery_label": _humanize(
            provider.discovery_type,
            fallback="Manual",
        ),
        "status": (
            "ready" if connected else "configured" if available else "attention"
        ),
        "status_label": (
            "Ready"
            if connected
            else "Connection inactive"
            if available
            else "Connection needed"
        ),
        "connection_count": int(connection_count),
        "active_connection_count": int(active_connection_count),
        "model_count": int(model_count),
        "active_model_count": int(active_model_count),
        "requires_endpoint": bool(provider.requires_api_base),
        "supports_custom_models": bool(provider.supports_custom_models),
        "supports_discovery": bool(
            provider.discovery_type == "automatic"
            or provider_is_openai_compatible(provider)
        ),
        "permissions": {
            "add_connection": True,
            "add_model": bool(provider.supports_custom_models),
            "delete_provider": _can_manage_provider(provider),
            "sync_models": bool(getattr(current_user, "is_admin", False)),
        },
        "actions": {
            "detail_url": (
                f"/api/frontend/settings/intelligence/providers/"
                f"{int(provider.id)}"
            ),
            "connections_url": (
                f"/api/llm/providers/{int(provider.id)}/keys"
            ),
            "models_url": (
                f"/api/llm/managed-models?provider_id={int(provider.id)}"
            ),
            "delete_url": f"/api/llm/providers/{int(provider.id)}",
            "sync_url": "/api/llm/sync-models",
        },
    }


def _provider_counts(
    provider_ids: list[int],
) -> tuple[dict[int, tuple[int, int]], dict[int, tuple[int, int]]]:
    if not provider_ids:
        return {}, {}
    key_rows = (
        db.session.query(
            ApiKey.provider_id,
            func.count(ApiKey.id),
            func.sum(case((ApiKey.is_active.is_(True), 1), else_=0)),
        )
        .filter(
            ApiKey.provider_id.in_(provider_ids),
            _key_visibility_filter(),
        )
        .group_by(ApiKey.provider_id)
        .all()
    )
    model_query = db.session.query(
        LLMModel.provider_id,
        func.count(LLMModel.id),
        func.sum(case((LLMModel.is_active.is_(True), 1), else_=0)),
    ).filter(LLMModel.provider_id.in_(provider_ids))
    model_visibility = _model_visibility_filter()
    if model_visibility is not None:
        model_query = model_query.filter(model_visibility)
    model_rows = model_query.group_by(LLMModel.provider_id).all()
    return (
        {
            int(provider_id): (int(total or 0), int(active or 0))
            for provider_id, total, active in key_rows
        },
        {
            int(provider_id): (int(total or 0), int(active or 0))
            for provider_id, total, active in model_rows
        },
    )


def _active_routing_models() -> list[LLMModel]:
    global_models = (
        LLMModel.query.join(LLMProvider)
        .filter(
            LLMModel.is_active.is_(True),
            LLMModel.user_id.is_(None),
            LLMProvider.user_id.is_(None),
            LLMModel.model_type == "chat",
        )
        .all()
    )
    personal_models = (
        LLMModel.query.join(LLMProvider)
        .filter(
            LLMModel.user_id == int(current_user.id),
            or_(
                LLMProvider.user_id.is_(None),
                LLMProvider.user_id == int(current_user.id),
            ),
            LLMModel.model_type == "chat",
            or_(
                LLMModel.is_active.is_(True),
                LLMModel.manually_added.is_(True),
            ),
        )
        .all()
    )
    by_model_id = {str(model.model_id): model for model in global_models}
    for model in personal_models:
        by_model_id[str(model.model_id)] = model
    return sorted(
        by_model_id.values(),
        key=lambda model: (
            str(model.provider.display_name or model.provider.name).lower()
            if model.provider
            else "",
            str(model.display_name or model.model_id).lower(),
        ),
    )[:MAX_ROUTING_MODELS]


def _default_model_values() -> dict[str, str]:
    records = {
        key: db.session.get(Setting, setting_key)
        for key, setting_key in DEFAULT_MODEL_KEYS.items()
    }
    return {
        key: str(record.value or "") if record else ""
        for key, record in records.items()
    }


def _image_detail_configs(
    allowed_model_ids: set[str],
) -> dict[str, str]:
    addon_settings = (
        current_user.addon_settings
        if isinstance(current_user.addon_settings, dict)
        else {}
    )
    llm_settings = addon_settings.get("llm_settings")
    if not isinstance(llm_settings, dict):
        return {}
    configs = llm_settings.get("image_detail_configs")
    if not isinstance(configs, dict):
        return {}
    return {
        str(model_id): str(value)
        for model_id, value in configs.items()
        if (
            str(model_id) in allowed_model_ids
            and str(value) in IMAGE_DETAIL_VALUES
        )
    }


def _dashboard_payload() -> dict[str, Any]:
    providers = _visible_provider_query().order_by(
        func.lower(func.coalesce(LLMProvider.display_name, LLMProvider.name))
    ).all()
    provider_ids = [int(provider.id) for provider in providers]
    key_counts, model_counts = _provider_counts(provider_ids)
    provider_payloads = []
    for provider in providers:
        connection_total, active_connections = key_counts.get(
            int(provider.id),
            (0, 0),
        )
        model_total, active_models = model_counts.get(
            int(provider.id),
            (0, 0),
        )
        provider_payloads.append(
            _provider_payload(
                provider,
                connection_count=connection_total,
                active_connection_count=active_connections,
                model_count=model_total,
                active_model_count=active_models,
            )
        )

    routing_models = _active_routing_models()
    model_payloads = [_model_payload(model) for model in routing_models]
    default_values = _default_model_values()
    delegation_count = CredentialDelegation.query.filter_by(
        delegator_id=int(current_user.id)
    ).count()
    generated_at = datetime.now(timezone.utc).isoformat().replace(
        "+00:00",
        "Z",
    )
    return {
        "frontend_api_version": FRONTEND_API_VERSION,
        "generated_at": generated_at,
        "permissions": {
            "manage_global": bool(getattr(current_user, "is_admin", False)),
            "share_credentials": True,
        },
        "summary": {
            "providers": len(provider_payloads),
            "connected_providers": sum(
                1
                for provider in provider_payloads
                if provider["active_connection_count"] > 0
            ),
            "connections": sum(
                provider["connection_count"]
                for provider in provider_payloads
            ),
            "active_models": sum(
                provider["active_model_count"]
                for provider in provider_payloads
            ),
            "routing_models": len(model_payloads),
            "delegations": int(delegation_count),
        },
        "providers": provider_payloads,
        "routing": {
            "defaults": default_values,
            "models": model_payloads,
            "can_manage_defaults": bool(
                getattr(current_user, "is_admin", False)
            ),
        },
        "image_detail": {
            "configs": _image_detail_configs(
                {model["id"] for model in model_payloads}
            ),
            "allowed_values": sorted(IMAGE_DETAIL_VALUES),
        },
        "links": {
            "sharing": "/api/frontend/settings/sharing",
            "preferences": (
                "/api/frontend/settings/intelligence/preferences"
            ),
            "create_connection": "/api/llm/keys",
            "create_endpoint": "/api/llm/openai-compatible-providers",
            "create_model": "/api/llm/models",
        },
        "limits": {
            "connections_per_provider": MAX_PROVIDER_CONNECTIONS,
            "models_per_provider": MAX_PROVIDER_MODELS,
            "routing_models": MAX_ROUTING_MODELS,
        },
    }


def _loaded_delegatable_addons() -> list[dict[str, str]]:
    manager = getattr(current_app, "addon_manager", None)
    if manager is None:
        return []
    result: dict[str, dict[str, str]] = {}
    try:
        details = manager.get_all_addons_details()
    except Exception:
        details = []
    for addon in details if isinstance(details, list) else []:
        if not isinstance(addon, dict):
            continue
        addon_id = str(addon.get("id") or "").strip()
        if (
            addon_id
            and addon.get("is_active")
            and addon.get("delegatable")
        ):
            result[addon_id] = {
                "id": addon_id,
                "name": str(addon.get("name") or _humanize(addon_id)),
            }
    loaded = getattr(manager, "loaded_addons", None)
    if isinstance(loaded, dict):
        for addon_id, addon in loaded.items():
            manifest = getattr(addon, "manifest", None)
            if not isinstance(manifest, dict):
                continue
            if not manifest.get("delegatable_credentials"):
                continue
            normalized_id = str(addon_id)
            result.setdefault(
                normalized_id,
                {
                    "id": normalized_id,
                    "name": str(
                        manifest.get("name") or _humanize(normalized_id)
                    ),
                },
            )
    return sorted(result.values(), key=lambda item: item["name"].lower())[
        :MAX_SHARING_PROVIDERS
    ]


def _settings_backed_credential_available(provider_id: str) -> bool:
    manager = getattr(current_app, "addon_manager", None)
    loaded = getattr(manager, "loaded_addons", None)
    addon = loaded.get(provider_id) if isinstance(loaded, dict) else None
    manifest = getattr(addon, "manifest", None)
    if not addon or not isinstance(manifest, dict):
        return False
    if manifest.get("custom_settings_ui", False):
        return False
    schema = manifest.get("settings_schema")
    if isinstance(schema, list):
        user_fields = [
            str(field.get("id"))
            for field in schema
            if isinstance(field, dict)
            and field.get("scope") == "user"
            and field.get("id")
        ]
        addon_settings = (
            current_user.addon_settings
            if isinstance(current_user.addon_settings, dict)
            else {}
        )
        user_settings = addon_settings.get(provider_id)
        if isinstance(user_settings, dict) and any(
            user_settings.get(field) for field in user_fields
        ):
            return True
    try:
        return bool(addon.is_configured())
    except Exception:
        return False


def _sharing_payload() -> dict[str, Any]:
    addons = _loaded_delegatable_addons()
    provider_names = {addon["id"] for addon in addons}
    credentials = (
        PluginCredential.query.filter(
            PluginCredential.user_id == int(current_user.id),
            PluginCredential.provider_name.in_(list(provider_names)),
        )
        .order_by(
            PluginCredential.provider_name,
            PluginCredential.created_at.desc(),
        )
        .all()
        if provider_names
        else []
    )
    credentials_by_provider: dict[str, list[PluginCredential]] = defaultdict(
        list
    )
    for credential in credentials:
        credentials_by_provider[str(credential.provider_name)].append(
            credential
        )

    provider_payloads = []
    for addon in addons:
        provider_id = addon["id"]
        items = [
            {
                "id": int(credential.id),
                "provider_id": provider_id,
                "label": str(
                    credential.credential_name or f"{addon['name']} account"
                ),
                "active": bool(credential.is_active),
                "source": "owned",
                "source_label": "Owned credential",
                "created_at": _iso(credential.created_at),
            }
            for credential in credentials_by_provider.get(provider_id, [])
        ]
        if _settings_backed_credential_available(provider_id):
            items.append(
                {
                    "id": None,
                    "provider_id": provider_id,
                    "label": f"{addon['name']} account",
                    "active": True,
                    "source": "settings",
                    "source_label": "Configured account",
                    "created_at": None,
                }
            )
        provider_payloads.append(
            {
                "id": provider_id,
                "name": addon["name"],
                "configured": bool(items),
                "credentials": items,
            }
        )

    delegations = (
        CredentialDelegation.query.filter_by(
            delegator_id=int(current_user.id)
        )
        .order_by(CredentialDelegation.provider_name)
        .limit(MAX_DELEGATIONS)
        .all()
    )
    recipients = (
        User.query.filter(User.id != int(current_user.id))
        .order_by(func.lower(User.email))
        .limit(MAX_SHARING_RECIPIENTS)
        .all()
    )
    delegation_payloads = []
    for delegation in delegations:
        credential = delegation.credential
        owns_credential = bool(
            credential
            and credential.user_id == int(current_user.id)
            and credential.provider_name == delegation.provider_name
        )
        delegation_payloads.append(
            {
                "provider_id": str(delegation.provider_name),
                "provider_name": next(
                    (
                        provider["name"]
                        for provider in provider_payloads
                        if provider["id"] == delegation.provider_name
                    ),
                    _humanize(delegation.provider_name),
                ),
                "credential_id": (
                    int(credential.id) if owns_credential else None
                ),
                "credential_name": str(
                    credential.credential_name
                    if owns_credential
                    else _humanize(delegation.provider_name)
                ),
                "delegatee_id": int(delegation.delegatee_id),
                "delegatee_email": str(
                    delegation.delegatee.email
                    if delegation.delegatee
                    else "Former member"
                ),
            }
        )
    return {
        "frontend_api_version": FRONTEND_API_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat().replace(
            "+00:00",
            "Z",
        ),
        "summary": {
            "providers": len(provider_payloads),
            "configured_providers": sum(
                1 for provider in provider_payloads if provider["configured"]
            ),
            "credentials": sum(
                len(provider["credentials"])
                for provider in provider_payloads
            ),
            "delegations": len(delegation_payloads),
            "recipients": len(recipients),
        },
        "providers": provider_payloads,
        "delegations": delegation_payloads,
        "recipients": [
            {"id": int(user.id), "email": str(user.email)}
            for user in recipients
        ],
        "actions": {
            "create_url": "/api/credential-delegations",
            "remove_url": "/api/credential-delegations",
        },
        "limits": {
            "providers": MAX_SHARING_PROVIDERS,
            "recipients": MAX_SHARING_RECIPIENTS,
            "delegations": MAX_DELEGATIONS,
        },
    }


@frontend_settings_blueprint.after_request
def _frontend_settings_headers(response):
    set_frontend_api_version_headers(response)
    response.headers["Cache-Control"] = "private, no-cache"
    return response


@frontend_settings_blueprint.route(
    "/api/frontend/settings/guidance",
    methods=["GET"],
)
@login_required
@admin_required
def get_frontend_assistant_guidance():
    return jsonify(_guidance_payload())


@frontend_settings_blueprint.route(
    "/api/frontend/settings/policy",
    methods=["GET"],
)
@login_required
@admin_required
def get_frontend_assistant_policy():
    return jsonify(_assistant_policy_payload())


@frontend_settings_blueprint.route(
    "/api/frontend/settings/policy",
    methods=["PUT"],
)
@login_required
@admin_required
def update_frontend_assistant_policy():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(
            {"error": "An assistant policy object is required."}
        ), 400

    unknown_fields = set(data) - {
        "runtime",
        "memory",
        "acknowledge_full_access",
    }
    if unknown_fields:
        return jsonify(
            {
                "error": (
                    "Unsupported assistant policy field: "
                    f"{sorted(unknown_fields)[0]}."
                )
            }
        ), 400

    acknowledgement = data.get("acknowledge_full_access", False)
    if not isinstance(acknowledgement, bool):
        return jsonify(
            {"error": "Full-access acknowledgement must be boolean."}
        ), 400

    supplied = False
    updates: dict[str, str] = {}
    current_policy = _assistant_policy_payload()
    if "runtime" in data:
        runtime = data.get("runtime")
        if not isinstance(runtime, dict):
            return jsonify(
                {"error": "Runtime policy must be an object."}
            ), 400
        unknown_runtime_fields = set(runtime) - {
            "approval_mode",
            "max_tool_calls",
        }
        if unknown_runtime_fields:
            return jsonify(
                {
                    "error": (
                        "Unsupported runtime policy field: "
                        f"{sorted(unknown_runtime_fields)[0]}."
                    )
                }
            ), 400
        if "approval_mode" in runtime:
            approval_mode = runtime.get("approval_mode")
            if approval_mode not in {REVIEW, FULL_ACCESS}:
                return jsonify(
                    {
                        "error": (
                            "Approval mode must be review or full_access."
                        )
                    }
                ), 400
            enabling_full_access = (
                approval_mode == FULL_ACCESS
                and current_policy["runtime"]["approval_mode"]
                != FULL_ACCESS
            )
            if enabling_full_access and not acknowledgement:
                return jsonify(
                    {
                        "error": (
                            "Switching to full access requires an explicit "
                            "acknowledgement."
                        )
                    }
                ), 400
            updates["disable_tool_approvals"] = (
                "true" if approval_mode == FULL_ACCESS else "false"
            )
            supplied = True
        if "max_tool_calls" in runtime:
            max_tool_calls = runtime.get("max_tool_calls")
            if (
                isinstance(max_tool_calls, bool)
                or not isinstance(max_tool_calls, int)
                or not (
                    ASSISTANT_POLICY_TOOL_CALLS_MIN
                    <= max_tool_calls
                    <= ASSISTANT_POLICY_TOOL_CALLS_MAX
                )
            ):
                return jsonify(
                    {
                        "error": (
                            "Tool budget must be a whole number between "
                            f"{ASSISTANT_POLICY_TOOL_CALLS_MIN} and "
                            f"{ASSISTANT_POLICY_TOOL_CALLS_MAX}."
                        )
                    }
                ), 400
            updates["max_tool_calls"] = str(max_tool_calls)
            supplied = True

    if "memory" in data:
        memory = data.get("memory")
        if not isinstance(memory, dict):
            return jsonify(
                {"error": "Memory policy must be an object."}
            ), 400
        unknown_memory_fields = set(memory) - {
            "user_hot_limit",
            "organization_hot_limit",
            "include_archive_by_default",
        }
        if unknown_memory_fields:
            return jsonify(
                {
                    "error": (
                        "Unsupported memory policy field: "
                        f"{sorted(unknown_memory_fields)[0]}."
                    )
                }
            ), 400
        for field, setting_key in (
            ("user_hot_limit", "memory_tier1_limit_user"),
            (
                "organization_hot_limit",
                "memory_tier1_limit_org",
            ),
        ):
            if field not in memory:
                continue
            limit = memory.get(field)
            if (
                isinstance(limit, bool)
                or not isinstance(limit, int)
                or not (
                    ASSISTANT_POLICY_MEMORY_LIMIT_MIN
                    <= limit
                    <= ASSISTANT_POLICY_MEMORY_LIMIT_MAX
                )
            ):
                return jsonify(
                    {
                        "error": (
                            "Hot-memory limits must be whole numbers "
                            f"between {ASSISTANT_POLICY_MEMORY_LIMIT_MIN} "
                            f"and {ASSISTANT_POLICY_MEMORY_LIMIT_MAX}."
                        )
                    }
                ), 400
            updates[setting_key] = str(limit)
            supplied = True
        if "include_archive_by_default" in memory:
            include_archive = memory.get(
                "include_archive_by_default"
            )
            if not isinstance(include_archive, bool):
                return jsonify(
                    {
                        "error": (
                            "Archive recall default must be boolean."
                        )
                    }
                ), 400
            updates["memory_recall_include_tier2_default"] = (
                "true" if include_archive else "false"
            )
            supplied = True

    if not supplied:
        return jsonify(
            {"error": "No supported assistant policy settings were provided."}
        ), 400

    for setting_key, value in updates.items():
        _set_setting(setting_key, value)
    db.session.commit()
    return jsonify(
        {
            "success": True,
            "message": "Assistant policy updated.",
            "policy": _assistant_policy_payload(),
        }
    )


@frontend_settings_blueprint.route(
    "/api/frontend/settings/guidance",
    methods=["PUT"],
)
@login_required
@admin_required
def update_frontend_assistant_guidance():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "A guidance settings object is required."}), 400

    unknown_fields = set(data) - {"organization", "prompts"}
    if unknown_fields:
        return jsonify(
            {
                "error": (
                    "Unsupported guidance field: "
                    f"{sorted(unknown_fields)[0]}."
                )
            }
        ), 400

    supplied = False
    updates: dict[str, str] = {}
    if "organization" in data:
        organization = data.get("organization")
        if not isinstance(organization, dict):
            return jsonify(
                {"error": "Organization guidance must be an object."}
            ), 400
        unknown_organization_fields = set(organization) - {
            "enabled",
            "profile",
        }
        if unknown_organization_fields:
            return jsonify(
                {
                    "error": (
                        "Unsupported organization field: "
                        f"{sorted(unknown_organization_fields)[0]}."
                    )
                }
            ), 400
        if "enabled" in organization:
            enabled = organization.get("enabled")
            if not isinstance(enabled, bool):
                return jsonify(
                    {"error": "Organization guidance state must be boolean."}
                ), 400
            updates["org_functions_enabled"] = (
                "true" if enabled else "false"
            )
            supplied = True
        if "profile" in organization:
            profile = organization.get("profile")
            if not isinstance(profile, str):
                return jsonify(
                    {"error": "Organization profile must be text."}
                ), 400
            profile = profile.strip()
            if len(profile) > MAX_GUIDANCE_PROFILE_CHARS:
                return jsonify(
                    {
                        "error": (
                            "Organization profile exceeds the "
                            f"{MAX_GUIDANCE_PROFILE_CHARS:,}-character limit."
                        )
                    }
                ), 400
            updates["org_profile"] = profile
            supplied = True

    if "prompts" in data:
        prompts = data.get("prompts")
        if not isinstance(prompts, dict):
            return jsonify({"error": "Prompts must be an object."}), 400
        unknown_prompt_ids = set(prompts) - set(GUIDANCE_PROMPTS)
        if unknown_prompt_ids:
            return jsonify(
                {
                    "error": (
                        "Unknown assistant prompt: "
                        f"{sorted(unknown_prompt_ids)[0]}."
                    )
                }
            ), 400
        for prompt_id, raw_value in prompts.items():
            if not isinstance(raw_value, str):
                return jsonify(
                    {"error": f"{GUIDANCE_PROMPTS[prompt_id]['name']} must be text."}
                ), 400
            value = raw_value.strip()
            if len(value) > MAX_GUIDANCE_PROMPT_CHARS:
                return jsonify(
                    {
                        "error": (
                            f"{GUIDANCE_PROMPTS[prompt_id]['name']} exceeds "
                            f"the {MAX_GUIDANCE_PROMPT_CHARS:,}-character limit."
                        )
                    }
                ), 400
            default_value = str(
                GUIDANCE_PROMPTS[prompt_id]["default"]
            ).strip()
            updates[str(GUIDANCE_PROMPTS[prompt_id]["setting_key"])] = (
                "" if not value or value == default_value else value
            )
            supplied = True

    if not supplied:
        return jsonify(
            {"error": "No supported guidance settings were provided."}
        ), 400

    for setting_key, value in updates.items():
        _set_setting(setting_key, value)
    db.session.commit()
    return jsonify(
        {
            "success": True,
            "message": "Assistant guidance updated.",
            "guidance": _guidance_payload(),
        }
    )


@frontend_settings_blueprint.route(
    "/api/frontend/settings/intelligence",
    methods=["GET"],
)
@login_required
def get_frontend_intelligence_settings():
    return jsonify(_dashboard_payload())


@frontend_settings_blueprint.route(
    "/api/frontend/settings/intelligence/providers/<int:provider_id>",
    methods=["GET"],
)
@login_required
def get_frontend_provider_settings(provider_id: int):
    provider = db.session.get(LLMProvider, provider_id)
    if not _provider_visible(provider):
        return jsonify({"error": "Provider not found."}), 404

    connection_query = ApiKey.query.filter(
        ApiKey.provider_id == provider_id,
        _key_visibility_filter(),
    )
    connections = (
        connection_query.order_by(
            ApiKey.is_active.desc(),
            ApiKey.created_at.desc(),
        )
        .limit(MAX_PROVIDER_CONNECTIONS)
        .all()
    )
    model_query = LLMModel.query.filter_by(provider_id=provider_id)
    model_visibility = _model_visibility_filter()
    if model_visibility is not None:
        model_query = model_query.filter(model_visibility)
    model_total = model_query.count()
    models = (
        model_query.order_by(
            LLMModel.model_type,
            func.lower(LLMModel.display_name),
        )
        .limit(MAX_PROVIDER_MODELS)
        .all()
    )
    active_connection_count = connection_query.filter(
        ApiKey.is_active.is_(True)
    ).count()
    active_model_count = model_query.filter(
        LLMModel.is_active.is_(True)
    ).count()
    return jsonify(
        {
            "frontend_api_version": FRONTEND_API_VERSION,
            "provider": _provider_payload(
                provider,
                connection_count=connection_query.count(),
                active_connection_count=active_connection_count,
                model_count=model_total,
                active_model_count=active_model_count,
            ),
            "connections": [
                _connection_payload(connection) for connection in connections
            ],
            "models": [_model_payload(model) for model in models],
            "model_total": int(model_total),
            "limits": {
                "connections": MAX_PROVIDER_CONNECTIONS,
                "models": MAX_PROVIDER_MODELS,
            },
        }
    )


@frontend_settings_blueprint.route(
    "/api/frontend/settings/intelligence/preferences",
    methods=["PUT"],
)
@login_required
def update_frontend_intelligence_preferences():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "A settings object is required."}), 400

    changed = False
    if "image_detail_configs" in data:
        raw_configs = data.get("image_detail_configs")
        if not isinstance(raw_configs, dict) or len(raw_configs) > 500:
            return jsonify(
                {"error": "Image detail settings must be a small object."}
            ), 400
        normalized_configs: dict[str, str] = {}
        for raw_model_id, raw_value in raw_configs.items():
            model_id = str(raw_model_id or "").strip()
            value = str(raw_value or "").strip()
            if (
                not model_id
                or len(model_id) > 255
                or value not in IMAGE_DETAIL_VALUES
            ):
                return jsonify(
                    {"error": "One or more image detail settings are invalid."}
                ), 400
            normalized_configs[model_id] = value
        allowed_model_ids = {
            str(model.model_id) for model in _active_routing_models()
        }
        if not set(normalized_configs).issubset(allowed_model_ids):
            return jsonify(
                {
                    "error": (
                        "Image detail settings may only reference models "
                        "available to the current user."
                    )
                }
            ), 400

        user = db.session.get(User, int(current_user.id))
        settings = dict(user.addon_settings or {})
        llm_settings = dict(settings.get("llm_settings") or {})
        llm_settings["image_detail_configs"] = normalized_configs
        settings["llm_settings"] = llm_settings
        user.addon_settings = settings
        flag_modified(user, "addon_settings")
        changed = True

    if "default_models" in data:
        if not bool(getattr(current_user, "is_admin", False)):
            return jsonify(
                {"error": "Administrator access required."}
            ), 403
        default_models = data.get("default_models")
        if not isinstance(default_models, dict):
            return jsonify(
                {"error": "Default model settings must be an object."}
            ), 400
        for slot, setting_key in DEFAULT_MODEL_KEYS.items():
            if slot not in default_models:
                continue
            model_id = str(default_models.get(slot) or "").strip()
            if len(model_id) > 255:
                return jsonify({"error": "Model identifier is too long."}), 400
            if model_id:
                model = (
                    LLMModel.query.join(
                        LLMProvider,
                        LLMModel.provider_id == LLMProvider.id,
                    )
                    .filter(
                        LLMModel.model_id == model_id,
                        LLMModel.user_id.is_(None),
                        LLMModel.is_active.is_(True),
                        LLMModel.model_type == "chat",
                        LLMProvider.user_id.is_(None),
                    )
                    .first()
                )
                if not model:
                    return jsonify(
                        {
                            "error": (
                                f"The {slot} default must use an active "
                                "shared chat model."
                            )
                        }
                    ), 400
            setting = db.session.get(Setting, setting_key)
            if setting:
                setting.value = model_id
            else:
                db.session.add(Setting(key=setting_key, value=model_id))
            changed = True

    if not changed:
        return jsonify({"error": "No supported settings were provided."}), 400

    db.session.commit()
    return jsonify(
        {
            "success": True,
            "message": "Model preferences updated.",
        }
    )


@frontend_settings_blueprint.route(
    "/api/frontend/settings/sharing",
    methods=["GET"],
)
@login_required
def get_frontend_credential_sharing():
    return jsonify(_sharing_payload())


def register_frontend_settings_blueprint(app) -> None:
    app.register_blueprint(frontend_settings_blueprint)


__all__ = [
    "ASSISTANT_POLICY_MEMORY_LIMIT_MAX",
    "ASSISTANT_POLICY_MEMORY_LIMIT_MIN",
    "ASSISTANT_POLICY_TOOL_CALLS_MAX",
    "ASSISTANT_POLICY_TOOL_CALLS_MIN",
    "DEFAULT_MODEL_KEYS",
    "GUIDANCE_PROMPTS",
    "IMAGE_DETAIL_VALUES",
    "MAX_DELEGATIONS",
    "MAX_GUIDANCE_PROFILE_CHARS",
    "MAX_GUIDANCE_PROMPT_CHARS",
    "MAX_PROVIDER_CONNECTIONS",
    "MAX_PROVIDER_MODELS",
    "MAX_ROUTING_MODELS",
    "MAX_SHARING_PROVIDERS",
    "MAX_SHARING_RECIPIENTS",
    "frontend_settings_blueprint",
    "register_frontend_settings_blueprint",
]
