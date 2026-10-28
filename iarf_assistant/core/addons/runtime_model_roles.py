"""Tenant-safe runtime model-role snapshots and compact planning cards."""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from flask import has_app_context, has_request_context
from sqlalchemy.exc import SQLAlchemyError

from ...models import LLMModel
from ..model_access import resolve_effective_model_records
from ..model_capabilities import load_litellm_model_metadata
from ..model_router import resolve_model_tier, user_llm_settings
from ..settings_cache import get_settings_snapshot

SettingsSource = Literal[
    "defaults",
    "runtime_settings",
    "defaults_unresolved",
]
logger = logging.getLogger(__name__)
POLICY_SETTING_DEFAULTS: Mapping[str, str] = MappingProxyType(
    {
        "disable_tool_approvals": "false",
        "max_tool_calls": "30",
        "chat_tool_budget_extension_max": "4",
    }
)


@dataclass(frozen=True, slots=True)
class RuntimePlanningContext:
    settings: Mapping[str, str]
    settings_source: SettingsSource
    user_id: int | None
    user_is_admin: bool
    user_model_settings: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RuntimeModelSnapshot:
    id: int
    user_id: int | None
    model_id: str
    display_name: str
    provider_name: str | None
    provider_kind: str | None
    context_window: int | None
    max_output_tokens: int | None
    supports_vision: bool
    supports_pdf: bool
    supports_reasoning: bool
    input_cost_per_million_tokens: float | None
    output_cost_per_million_tokens: float | None


def runtime_planning_context() -> RuntimePlanningContext:
    """Capture settings and request-user preferences without retaining proxies."""

    values = dict(POLICY_SETTING_DEFAULTS)
    source: SettingsSource = "defaults"
    if has_app_context():
        try:
            snapshot = get_settings_snapshot()
            values.update(
                {
                    str(key): str(value)
                    for key, value in snapshot.items()
                    if value is not None
                }
            )
            source = "runtime_settings"
        except Exception as exc:
            logger.debug(
                "Unable to resolve runtime planning settings: %s",
                exc,
                exc_info=True,
            )
            source = "defaults_unresolved"

    user_id: int | None = None
    is_admin = False
    preferences: dict[str, Any] = {}
    if has_request_context():
        try:
            from flask_login import current_user

            if getattr(current_user, "is_authenticated", False):
                user_id = int(current_user.get_id())
                is_admin = bool(getattr(current_user, "is_admin", False))
                preferences = user_llm_settings(
                    getattr(current_user, "addon_settings", None)
                )
        except Exception as exc:
            logger.debug(
                "Unable to resolve runtime planning user context: %s",
                exc,
                exc_info=True,
            )

    return RuntimePlanningContext(
        settings=MappingProxyType(values),
        settings_source=source,
        user_id=user_id,
        user_is_admin=is_admin,
        user_model_settings=MappingProxyType(preferences),
    )


def _model_snapshot(record: LLMModel) -> RuntimeModelSnapshot:
    provider = record.provider
    return RuntimeModelSnapshot(
        id=int(record.id),
        user_id=int(record.user_id) if record.user_id is not None else None,
        model_id=str(record.model_id),
        display_name=str(record.display_name),
        provider_name=str(provider.name) if provider is not None else None,
        provider_kind=(str(provider.provider_kind) if provider is not None else None),
        context_window=record.context_window,
        max_output_tokens=record.max_output_tokens,
        supports_vision=bool(record.supports_vision),
        supports_pdf=bool(record.supports_pdf),
        supports_reasoning=bool(record.supports_reasoning),
        input_cost_per_million_tokens=record.input_cost_per_million_tokens,
        output_cost_per_million_tokens=record.output_cost_per_million_tokens,
    )


def load_runtime_model_snapshots(
    model_ids: Collection[str],
    *,
    user_id: int | None,
) -> dict[str, RuntimeModelSnapshot]:
    """Load one permitted row per model ID with the provider eagerly joined."""

    records = resolve_effective_model_records(model_ids, user_id=user_id)
    return {
        model_id: _model_snapshot(record)
        for model_id, record in records.items()
    }


def _safe_int(info: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = info.get(key)
        if value is None:
            continue
        try:
            parsed = int(value)
        except TypeError, ValueError:
            continue
        if parsed > 0:
            return parsed
    return None


def _safe_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().casefold()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    return None


def _cost_per_million(
    info: Mapping[str, Any],
    per_million_key: str,
    per_token_key: str,
) -> float | None:
    value = info.get(per_million_key)
    if value is not None:
        try:
            return float(value)
        except TypeError, ValueError:
            pass
    value = info.get(per_token_key)
    if value is not None:
        try:
            return float(value) * 1_000_000
        except TypeError, ValueError:
            pass
    return None


def litellm_model_metadata(
    model_id: str,
    provider_name: str | None = None,
    provider_kind: str | None = None,
) -> dict[str, Any]:
    info = load_litellm_model_metadata(
        model_id,
        provider_name=provider_name,
        provider_kind=provider_kind,
    )
    if not info:
        return {}
    return {
        "metadata_catalog_model_id": str(info.get("_catalog_model_id") or model_id),
        "context_window_tokens": _safe_int(
            info,
            "max_input_tokens",
            "max_context_tokens",
            "max_tokens",
        ),
        "max_output_tokens": _safe_int(
            info,
            "max_output_tokens",
            "output_token_limit",
            "max_tokens",
        ),
        "supports_vision": info.get("supports_vision"),
        "supports_pdf": info.get("supports_pdf_input"),
        "supports_reasoning": (
            info.get("supports_reasoning")
            or info.get("supports_reasoning_effort")
            or info.get("supports_thinking")
        ),
        "input_cost_per_million_tokens": _cost_per_million(
            info,
            "input_cost_per_million_tokens",
            "input_cost_per_token",
        ),
        "output_cost_per_million_tokens": _cost_per_million(
            info,
            "output_cost_per_million_tokens",
            "output_cost_per_token",
        ),
    }


def model_context_budget_card(
    context_window_tokens: int | None,
    output_token_limit: int | None = None,
) -> dict[str, Any]:
    if not context_window_tokens:
        return {
            "context_window_tokens": None,
            "recommended_output_reserve_tokens": None,
            "recommended_max_input_tokens": None,
            "recommended_worker_input_budget_tokens": None,
            "strategy": "Context window unknown; use conservative chunks and discover metadata before large batching.",
        }

    reserve_candidates = [4096, max(1024, int(context_window_tokens * 0.1))]
    if output_token_limit:
        reserve_candidates.append(
            max(1024, min(output_token_limit, int(output_token_limit * 0.75)))
        )
    output_reserve = min(
        context_window_tokens // 3,
        min(max(reserve_candidates), 8192),
    )
    output_reserve = max(512, output_reserve)
    recommended_input = max(1024, context_window_tokens - output_reserve)
    return {
        "context_window_tokens": context_window_tokens,
        "recommended_output_reserve_tokens": output_reserve,
        "recommended_max_input_tokens": recommended_input,
        "recommended_worker_input_budget_tokens": min(recommended_input, 30000),
        "strategy": (
            "Keep worker prompts below recommended_worker_input_budget_tokens by default; "
            "increase only after a quality/cost check, even if the raw context window is larger."
        ),
    }


def _metadata_confidence(
    metadata_sources: Mapping[str, str],
    *,
    has_model_record: bool,
    has_catalog: bool,
) -> str:
    if metadata_sources and all(
        source == "db_model_record" for source in metadata_sources.values()
    ):
        return "db_model_record"
    if has_model_record and has_catalog:
        return "db_plus_litellm_catalog"
    if has_catalog:
        return "litellm_catalog"
    return "missing"


def _legacy_role_boolean_value(
    catalog: Mapping[str, Any],
    metadata_sources: dict[str, str],
    field_name: str,
    db_value: bool | None,
    catalog_key: str | None = None,
) -> bool:
    """Resolve non-null legacy booleans consistently with model execution."""

    if db_value is True:
        metadata_sources[field_name] = "db_model_record"
        return True
    catalog_value = _safe_bool(catalog.get(catalog_key or field_name))
    if catalog_value is not None:
        metadata_sources[field_name] = "litellm_model_info"
        return catalog_value
    if db_value is not None:
        metadata_sources[field_name] = "db_model_record"
        return bool(db_value)
    return False


@dataclass(frozen=True, slots=True)
class _ResolvedRoleFields:
    display_name: str
    context_window: int | None
    supports_vision: bool
    supports_pdf: bool
    supports_reasoning: bool
    input_cost: float | None
    output_cost: float | None
    availability: str


def _role_field_value(
    catalog: Mapping[str, Any],
    metadata_sources: dict[str, str],
    field_name: str,
    db_value: Any,
) -> Any:
    if db_value is not None:
        metadata_sources[field_name] = "db_model_record"
        return db_value
    value = catalog.get(field_name)
    if value is not None:
        metadata_sources[field_name] = "litellm_model_info"
    return value


def _resolve_role_fields(
    model_record: RuntimeModelSnapshot | None,
    model_id: str,
    catalog: Mapping[str, Any],
    metadata_sources: dict[str, str],
) -> _ResolvedRoleFields:
    db = model_record
    context_window = _role_field_value(
        catalog,
        metadata_sources,
        "context_window_tokens",
        db.context_window if db else None,
    )
    input_cost = _role_field_value(
        catalog,
        metadata_sources,
        "input_cost_per_million_tokens",
        db.input_cost_per_million_tokens if db else None,
    )
    output_cost = _role_field_value(
        catalog,
        metadata_sources,
        "output_cost_per_million_tokens",
        db.output_cost_per_million_tokens if db else None,
    )
    return _ResolvedRoleFields(
        display_name=db.display_name if db else model_id.split("/", 1)[-1],
        context_window=context_window,
        supports_vision=_legacy_role_boolean_value(
            catalog,
            metadata_sources,
            "supports_vision",
            db.supports_vision if db else None,
        ),
        supports_pdf=_legacy_role_boolean_value(
            catalog,
            metadata_sources,
            "supports_pdf",
            db.supports_pdf if db else None,
        ),
        supports_reasoning=_legacy_role_boolean_value(
            catalog,
            metadata_sources,
            "supports_reasoning",
            db.supports_reasoning if db else None,
        ),
        input_cost=input_cost,
        output_cost=output_cost,
        availability=(
            "configured"
            if db
            else (
                "configured_metadata_only"
                if metadata_sources
                else "configured_missing_model_record"
            )
        ),
    )


def apply_model_metadata_to_role_card(
    card: dict[str, Any],
    model_record: RuntimeModelSnapshot | None,
    model_id: str,
) -> dict[str, Any]:
    provider_name = model_record.provider_name if model_record else None
    provider_kind = model_record.provider_kind if model_record else None
    if not provider_name:
        try:
            from ...llm_provider_utils import infer_provider_name

            provider_name = infer_provider_name(model_id)
        except ImportError, TypeError, ValueError:
            provider_name = None

    catalog = litellm_model_metadata(model_id, provider_name, provider_kind)
    metadata_sources: dict[str, str] = {}
    fields = _resolve_role_fields(
        model_record,
        model_id,
        catalog,
        metadata_sources,
    )

    output_token_limit = _safe_int(catalog, "max_output_tokens", "max_tokens")
    missing_metadata = [
        field
        for field, value in (
            ("context_window_tokens", fields.context_window),
            ("input_cost_per_million_tokens", fields.input_cost),
            ("output_cost_per_million_tokens", fields.output_cost),
        )
        if value is None
    ]
    result = dict(card)
    result.update(
        {
            "availability": fields.availability,
            "display_name": fields.display_name,
            "provider": provider_name,
            "context_window_tokens": fields.context_window,
            "supports_vision": fields.supports_vision,
            "supports_pdf": fields.supports_pdf,
            "supports_reasoning": fields.supports_reasoning,
            "input_cost_per_million_tokens": fields.input_cost,
            "output_cost_per_million_tokens": fields.output_cost,
            "context_budget": model_context_budget_card(
                fields.context_window,
                output_token_limit,
            ),
            "metadata_sources": metadata_sources,
            "metadata_confidence": _metadata_confidence(
                metadata_sources,
                has_model_record=model_record is not None,
                has_catalog=bool(catalog),
            ),
            "details": "Resolved from runtime settings and model metadata.",
        }
    )
    if catalog.get("metadata_catalog_model_id"):
        result["metadata_catalog_model_id"] = catalog["metadata_catalog_model_id"]
    if missing_metadata:
        result["missing_metadata"] = missing_metadata
        result["details"] = (
            "Resolved from runtime settings; some model metadata is unavailable."
        )
    return result


class AddonRuntimeModelRolesMixin:
    """Resolve role cards from one settings and one tenant-bounded model query."""

    def _runtime_context_snapshot(self) -> RuntimePlanningContext:
        return runtime_planning_context()

    def _runtime_setting_snapshot(self) -> tuple[dict[str, str], str]:
        context = self._runtime_context_snapshot()
        return dict(context.settings), context.settings_source

    def _runtime_model_role_cards(
        self,
        context: RuntimePlanningContext | None = None,
    ) -> list[dict[str, Any]]:
        context = context or self._runtime_context_snapshot()
        role_cards = [
            {
                "id": "fast",
                "setting_key": "default_fast_model",
                "best_for": "cheap classification, extraction, triage, and narrow worker tasks",
            },
            {
                "id": "medium",
                "setting_key": "default_medium_model",
                "best_for": "planning, synthesis, and quality checks",
            },
            {
                "id": "powerful",
                "setting_key": "default_powerful_model",
                "best_for": "hard reasoning, final synthesis, and difficult repair",
            },
        ]
        resolved: dict[str, tuple[str, str]] = {
            str(card["id"]): resolve_model_tier(
                str(card["id"]),
                system_settings=context.settings,
                user_settings=context.user_model_settings,
            )
            for card in role_cards
        }
        try:
            models = load_runtime_model_snapshots(
                [model_id for model_id, _source in resolved.values()],
                user_id=context.user_id,
            )
        except (SQLAlchemyError, ValueError) as exc:
            logger.debug(
                "Unable to resolve runtime model role records: %s",
                exc,
                exc_info=True,
            )
            models = {}

        results: list[dict[str, Any]] = []
        for card in role_cards:
            role_id = str(card["id"])
            model_id, source = resolved[role_id]
            base = {
                **card,
                "model_id": model_id,
                "model_setting_source": source,
                "details": "Resolved by runtime settings.",
            }
            results.append(
                apply_model_metadata_to_role_card(
                    base,
                    models.get(model_id),
                    model_id,
                )
            )
        return results

    def _litellm_model_metadata(
        self,
        model_id: str,
        provider_name: str | None = None,
        provider_kind: str | None = None,
    ) -> dict[str, Any]:
        return litellm_model_metadata(model_id, provider_name, provider_kind)

    def _model_context_budget_card(
        self,
        context_window_tokens: int | None,
        output_token_limit: int | None = None,
    ) -> dict[str, Any]:
        return model_context_budget_card(
            context_window_tokens,
            output_token_limit,
        )

    def _apply_model_metadata_to_role_card(
        self,
        card: dict[str, Any],
        model_record: RuntimeModelSnapshot | None,
        model_id: str,
    ) -> None:
        updated = apply_model_metadata_to_role_card(
            card,
            model_record,
            model_id,
        )
        card.clear()
        card.update(updated)


__all__ = [
    "POLICY_SETTING_DEFAULTS",
    "AddonRuntimeModelRolesMixin",
    "RuntimeModelSnapshot",
    "RuntimePlanningContext",
    "apply_model_metadata_to_role_card",
    "litellm_model_metadata",
    "load_runtime_model_snapshots",
    "model_context_budget_card",
    "runtime_planning_context",
]
