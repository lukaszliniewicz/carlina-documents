"""Immutable settings snapshots for the document-block translation workflow.

This module is deliberately a persistence boundary, not an orchestrator.  It
freezes the exact model/credential identities, conditional phase settings, and
window admission profile before a worker may perform any model I/O.  Callers
own the surrounding transaction and must commit the returned row themselves.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any, cast

from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from carlina.core.utc import utc_now_db

from ...core.executor_profile import ExecutorProfile, normalize_executor_profile
from ...core.model_capabilities import (
    ModelCapabilityProfile,
    resolve_model_capabilities,
)
from ...core.model_readiness import (
    ModelNotReadyError,
    require_model_ready,
    resolve_model_connection,
)
from ...extensions import db
from ...llm_provider_utils import (
    canonical_provider_name,
    normalize_openai_compatible_base_url,
)
from ...models import ApiKey, LLMModel, LLMProvider, Setting, User
from ...reasoning import (
    is_reasoning_level_supported,
    normalize_reasoning_level,
    resolve_reasoning_config,
)
from .analysis_contract import private_witness
from .analysis_scope_contract import (
    ANALYSIS_SCOPE_PLANNER_CONTRACT_VERSION,
    SUPPORTED_ANALYSIS_SCOPE_PLANNER_CONTRACTS,
)
from .block_continuation import (
    normalize_protected_targets,
    normalize_translation_scope,
)
from .block_contract import (
    DOCUMENT_BLOCK_ENGINE_CONTRACT,
    BlockContractError,
    canonical_json,
)
from .block_notes import normalize_annotation_profile
from .block_persistence import rehydrate_source_plan
from .integrity_receipts import make_receipt, verify_receipt
from .models import (
    TranslationBlockWorkflowConfig,
    TranslationOperation,
    TranslationSourcePlan,
)
from .research_input_contract import (
    RESEARCH_INPUT_PLANNER_CONTRACT_VERSION,
    SUPPORTED_RESEARCH_INPUT_PLANNER_CONTRACTS,
)
from .research_persistence import _research_budget, _tool_declarations
from .translation_units import (
    TRANSLATION_UNIT_MODE_ALIGNED_SENTENCES,
    normalize_translation_unit_mode,
)
from .window_contract import (
    COMPACT_WRITABLE_SOURCE_CONTRACT_VERSION,
    LEGACY_WRITABLE_SOURCE_CONTRACT_VERSION,
    LOCATED_SOURCE_WRITABLE_CONTRACT_VERSION,
    NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
    PARTS_WRITABLE_SOURCE_CONTRACT_VERSION,
    SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION,
    SINGLE_COPY_WRITABLE_SOURCE_CONTRACT_VERSION,
    STUDY_NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
    WRITABLE_SOURCE_CONTRACT_VERSION,
)

WORKFLOW_CONFIG_VERSION = "document-block-workflow-config-v1"
DOCUMENT_STRUCTURE_CONTEXT_VERSION = "document-structure-context-v1"
CONFIG_RECEIPT_PURPOSE = "translator-block-workflow-config-v1"
EMBEDDING_CREDENTIAL_PURPOSE = "translator-embedding-credential-v1"
MAX_OWNER_POLICY_BYTES = 20_000
MAX_RESEARCH_MODEL_CALLS = 128
MAX_RESEARCH_OUTPUT_TOKENS = 1_000_000
MAX_WINDOW_PROFILE_BYTES = 8_192
MAX_MODEL_CAPABILITY_PROFILE_BYTES = 64 * 1024
MODEL_TRANSPORT_IDENTITY_VERSION = "translator-model-transport-v1"
EXTERNAL_EXECUTOR_IDENTITY_KIND = "external_executor"
EXTERNAL_EXECUTOR_IDENTITY_VERSION = "translator-external-executor-v1"
EXTERNAL_EXECUTOR_ROLE = "translator"

TRANSLATION_CONTEXT_MODE_SELF_CONTAINED = "self_contained"
TRANSLATION_CONTEXT_MODE_RETAINED = "retained"
TRANSLATION_CONTEXT_MODES = frozenset(
    {
        TRANSLATION_CONTEXT_MODE_SELF_CONTAINED,
        TRANSLATION_CONTEXT_MODE_RETAINED,
    }
)
TRANSLATION_CONTEXT_CONTRACT_VERSION = "translation-context-v2"
LEGACY_TRANSLATION_CONTEXT = {"contract_version": "translation-context-v1"}

TRANSLATION_REASONING_SLOT = "translation"
ANALYSIS_REASONING_SLOT = "terminology_analysis"
RESEARCH_REASONING_SLOT = "researcher"

_HASH_CHARS = frozenset("0123456789abcdef")
_WINDOW_V1_KEYS = frozenset(
    {
        "profile_id",
        "profile_version",
        "capacity_version",
        "max_blocks",
        "max_input_tokens",
        "output_reserve_tokens",
    }
)
_WINDOW_V2_KEYS = _WINDOW_V1_KEYS | {"target_input_tokens"}
_CONFIG_REQUIRED_KEYS = frozenset(
    {
        "contract_version",
        "engine_contract",
        "source_language",
        "target_language",
        "source_plan",
        "enable_pre_analysis",
        "enable_research",
        "translation",
        "window_profile",
    }
)
_CONFIG_OPTIONAL_KEYS = frozenset(
    {
        "pre_analysis",
        "research",
        "research_dossier",
        "embedding_projection",
        "owner_policy",
        "protected_targets",
        "translation_scope",
        "document_structure_context",
        "translation_context",
        "foreign_language_policy",
        "writable_source_contract_version",
        "enable_translator_notes",
        "annotation_profile",
        "translation_unit_mode",
    }
)
_RESEARCH_DOSSIER_KEYS = frozenset(
    {
        "revision_id",
        "record_hash",
        "content_digest",
        "conclusion_provenance",
        "evidence_verification_state",
    }
)
_EXECUTABLE_RESEARCH_DOSSIER_PROVENANCE = "external_unverified"
_EXECUTABLE_RESEARCH_DOSSIER_VERIFICATION_STATE = "unverified"


def _error(code: str) -> BlockContractError:
    return BlockContractError(code)


def _valid_translation_context(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    if set(value) == {"contract_version"}:
        return (
            type(value.get("contract_version")) is str
            and value.get("contract_version") == "translation-context-v1"
        )
    return (
        set(value) == {"contract_version", "continuity_mode"}
        and type(value.get("contract_version")) is str
        and value.get("contract_version") == TRANSLATION_CONTEXT_CONTRACT_VERSION
        and type(value.get("continuity_mode")) is str
        and value.get("continuity_mode") in TRANSLATION_CONTEXT_MODES
    )


def normalize_translation_context_mode(value: object) -> str:
    """Validate the strict saved/per-run translation context mode enum."""

    if type(value) is not str or value not in TRANSLATION_CONTEXT_MODES:
        raise _error("workflow-config-translation-context-mode")
    return value


def resolve_translation_context_mode(
    settings: Mapping[str, Any],
    *,
    override: object = None,
    execution_mode: object = None,
) -> str:
    """Resolve the immutable context policy for one managed or passive start."""

    saved = normalize_translation_context_mode(
        settings.get(
            "translation_context_mode", TRANSLATION_CONTEXT_MODE_SELF_CONTAINED
        )
    )
    requested = (
        normalize_translation_context_mode(override)
        if override is not None
        else None
    )
    effective_execution_mode = (
        settings.get("execution_mode", "managed")
        if execution_mode is None
        else execution_mode
    )
    if effective_execution_mode == "managed":
        return TRANSLATION_CONTEXT_MODE_SELF_CONTAINED
    if effective_execution_mode == "passive":
        return requested or saved
    raise _error("workflow-config-execution-mode")


def translation_context_policy(context_mode: object) -> dict[str, str]:
    """Return the exact frozen v2 translation context policy object."""

    return {
        "contract_version": TRANSLATION_CONTEXT_CONTRACT_VERSION,
        "continuity_mode": normalize_translation_context_mode(context_mode),
    }


def _string(value: object, code: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise _error(code)
    if len(value.encode("utf-8")) > maximum or not value.isprintable():
        raise _error(code)
    return value


def _optional_string(value: object, code: str, *, maximum: int = 256) -> str | None:
    if value is None:
        return None
    return _string(value, code, maximum=maximum)


def _hash(value: object, code: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _HASH_CHARS for character in value)
    ):
        raise _error(code)
    return value


def _integer(value: object, code: str, *, minimum: int, maximum: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < minimum
        or value > maximum
    ):
        raise _error(code)
    return value


def _iso(value: object, code: str) -> str:
    if not isinstance(value, datetime):
        raise _error(code)
    return value.isoformat(timespec="microseconds")


def _canonical_mapping(value: object, code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _error(code)
    try:
        encoded = canonical_json(dict(value)).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise _error(code) from exc
    if len(encoded) > MAX_WINDOW_PROFILE_BYTES:
        raise _error(code)
    # round-trip makes tuples and unusual scalar values fail before persistence
    # while preserving the sole canonical JSON representation.
    try:
        import json

        restored = json.loads(encoded)
    except (TypeError, ValueError, RecursionError) as exc:
        raise _error(code) from exc
    if restored != dict(value):
        raise _error(code)
    return cast(dict[str, Any], restored)


def _normalize_window_profile(value: object) -> dict[str, Any]:
    item = _canonical_mapping(value, "workflow-config-window-profile")
    profile_version = _string(
        item.get("profile_version"),
        "workflow-config-window-profile-version",
        maximum=64,
    )
    expected_keys = (
        _WINDOW_V1_KEYS
        if profile_version == "1"
        else _WINDOW_V2_KEYS
        if profile_version in {"2", "3", "4", "5"}
        else frozenset()
    )
    if set(item) != expected_keys:
        raise _error("workflow-config-window-profile")
    result = {
        "profile_id": _string(item["profile_id"], "workflow-config-window-profile-id"),
        "profile_version": profile_version,
        "capacity_version": _string(
            item["capacity_version"], "workflow-config-capacity-version", maximum=64
        ),
        "max_blocks": _integer(
            item["max_blocks"],
            "workflow-config-window-blocks",
            minimum=1,
            maximum=65_536,
        ),
        "max_input_tokens": _integer(
            item["max_input_tokens"],
            "workflow-config-window-input-capacity",
            minimum=1,
            maximum=10_000_000,
        ),
        "output_reserve_tokens": _integer(
            item["output_reserve_tokens"],
            "workflow-config-window-output-reserve",
            minimum=1,
            maximum=1_000_000,
        ),
    }
    if result["output_reserve_tokens"] >= result["max_input_tokens"]:
        raise _error("workflow-config-window-output-reserve")
    if profile_version in {"2", "3", "4", "5"}:
        result["target_input_tokens"] = _integer(
            item["target_input_tokens"],
            "workflow-config-window-input-target",
            minimum=1,
            maximum=1_000_000,
        )
        if result["target_input_tokens"] > result["max_input_tokens"]:
            raise _error("workflow-config-window-input-target")
    return result


def _normalize_owner_policy(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _error("workflow-config-owner-policy")
    if not value.strip():
        return None
    if len(value.encode("utf-8")) > MAX_OWNER_POLICY_BYTES:
        raise _error("workflow-config-owner-policy")
    return value


def _credential_witness(key: ApiKey, provider: LLMProvider) -> str:
    """Witness the selected credential without retaining its secret."""

    return private_witness(
        {
            "id": int(key.id),
            "owner_user_id": key.user_id,
            "provider_id": int(key.provider_id),
            "provider_name": provider.name,
            "created_at": _iso(key.created_at, "workflow-config-key-time"),
            "api_key": key.api_key,
            "api_base": key.api_base,
            "api_version": key.api_version,
            "is_active": bool(key.is_active),
        },
        purpose=EMBEDDING_CREDENTIAL_PURPOSE,
    )


def _model_transport_identity(key: ApiKey, provider: LLMProvider) -> dict[str, Any]:
    """Freeze provider routing; URL fields may require credential protection."""

    provider_name = canonical_provider_name(provider.name)
    provider_kind = provider.provider_kind
    api_base = key.api_base
    if provider_name == "vertex_ai":
        effective_base = api_base or "my-project,us-central1"
        parts = effective_base.split(",")
        return {
            "contract_version": MODEL_TRANSPORT_IDENTITY_VERSION,
            "provider_name": provider_name,
            "provider_kind": provider_kind,
            "api_base": effective_base,
            "api_version": key.api_version,
            "vertex_project": parts[0].strip(),
            "vertex_location": (parts[1].strip() if len(parts) > 1 else "us-central1"),
        }
    return {
        "contract_version": MODEL_TRANSPORT_IDENTITY_VERSION,
        "provider_name": provider_name,
        "provider_kind": provider_kind,
        "api_base": normalize_openai_compatible_base_url(
            provider_name, api_base, provider_kind
        ),
        "api_version": key.api_version,
        "vertex_project": None,
        "vertex_location": None,
    }


def _normalize_research_dossier(value: object) -> dict[str, str]:
    """Normalize the immutable identity pin for one dossier revision."""

    if not isinstance(value, Mapping) or set(value) != _RESEARCH_DOSSIER_KEYS:
        raise _error("workflow-config-research-dossier")
    revision_id = _string(
        value.get("revision_id"),
        "workflow-config-research-dossier",
        maximum=128,
    )
    record_hash = _hash(value.get("record_hash"), "workflow-config-research-dossier")
    content_digest = _hash(
        value.get("content_digest"), "workflow-config-research-dossier"
    )
    conclusion_provenance = value.get("conclusion_provenance")
    if (
        not isinstance(conclusion_provenance, str)
        or conclusion_provenance != _EXECUTABLE_RESEARCH_DOSSIER_PROVENANCE
    ):
        raise _error("workflow-config-research-dossier")
    evidence_verification_state = value.get("evidence_verification_state")
    if (
        not isinstance(evidence_verification_state, str)
        or evidence_verification_state
        != _EXECUTABLE_RESEARCH_DOSSIER_VERIFICATION_STATE
    ):
        raise _error("workflow-config-research-dossier")
    return {
        "revision_id": revision_id,
        "record_hash": record_hash,
        "content_digest": content_digest,
        "conclusion_provenance": conclusion_provenance,
        "evidence_verification_state": evidence_verification_state,
    }


def _model_revision(model: LLMModel) -> str:
    overrides = (
        model.capability_overrides
        if isinstance(model.capability_overrides, Mapping)
        else {}
    )
    return str(
        overrides.get("revision")
        or overrides.get("model_revision")
        or _iso(model.updated_at, "workflow-config-model-time")
    )


def _capability_profile(
    model_id: str, *, owner_user_id: int, model: LLMModel
) -> dict[str, Any]:
    """Freeze the derived provider transport/capacity contract as plain JSON."""

    try:
        encoded = canonical_json(
            resolve_model_capabilities(
                model_id,
                user_id=owner_user_id,
                model_record=model,
            ).to_dict()
        ).encode("utf-8")
        restored = json.loads(encoded)
    except (TypeError, ValueError, RecursionError) as exc:
        raise _error("workflow-config-model-capabilities") from exc
    if len(encoded) > MAX_MODEL_CAPABILITY_PROFILE_BYTES or not isinstance(
        restored, dict
    ):
        raise _error("workflow-config-model-capabilities")
    return cast(dict[str, Any], restored)


def _external_executor_identity(
    value: ExecutorProfile | Mapping[str, Any],
) -> dict[str, Any]:
    """Freeze one caller-declared, provider-neutral translation capability.

    The external profile is a capability envelope, not a model/provider
    selection.  Keeping the profile and its derived admission DTO together
    lets rehydration verify passive work without looking up Carlina models or
    credentials.
    """

    profile = (
        value
        if isinstance(value, ExecutorProfile)
        else normalize_executor_profile(value)
    )
    # Normalize dataclass tuples (notably ``missing_metadata``) to their JSON
    # representation before freezing the identity. Encrypted JSON persistence
    # restores those tuples as lists; freezing the pre-serialization shape made
    # an otherwise valid passive identity fail its own rehydration check.
    encoded = canonical_json(
        profile.capability_profile(EXTERNAL_EXECUTOR_ROLE).to_dict()
    ).encode("utf-8")
    capability = json.loads(encoded)
    if not isinstance(capability, dict):  # Defensive: the DTO must stay an object.
        raise _error("workflow-config-external-identity")
    return {
        "identity_kind": EXTERNAL_EXECUTOR_IDENTITY_KIND,
        "identity_version": EXTERNAL_EXECUTOR_IDENTITY_VERSION,
        "role": EXTERNAL_EXECUTOR_ROLE,
        # Role token required by the existing provider-neutral window profile;
        # this is not a configured model or catalogue identity.
        "model_id": EXTERNAL_EXECUTOR_ROLE,
        "executor_profile": profile.to_dict(),
        "capability_profile": capability,
        "capability_profile_hash": hashlib.sha256(encoded).hexdigest(),
    }


def _model_identity(model_id: object, *, owner_user_id: int) -> dict[str, Any]:
    selected = _string(model_id, "workflow-config-model-id", maximum=255)
    try:
        readiness = require_model_ready(
            selected, user_id=owner_user_id, required_model_type="chat"
        )
    except (ModelNotReadyError, ValueError) as exc:
        raise _error("workflow-config-model-unready") from exc
    if not readiness.ready:
        raise _error("workflow-config-model-unready")
    connection = resolve_model_connection(selected, user_id=owner_user_id)
    model = connection.model_record
    provider = connection.provider
    key = connection.api_key_record
    # Environment-only compatibility credentials are intentionally not enough
    # for an immutable operation snapshot: there must be a saved key identity.
    if model is None or provider is None or key is None:
        raise _error("workflow-config-model-credential")
    if (
        model.model_id != selected
        or model.provider_id != provider.id
        or model.is_active is not True
        or model.model_type != "chat"
        or key.is_active is not True
        or key.provider_id != provider.id
    ):
        raise _error("workflow-config-model-unready")
    capability_profile = _capability_profile(
        selected, owner_user_id=owner_user_id, model=model
    )
    return {
        "model_id": selected,
        "model_record_id": int(model.id),
        "model_owner_user_id": model.user_id,
        "model_revision": _model_revision(model),
        "model_updated_at": _iso(model.updated_at, "workflow-config-model-time"),
        "provider_id": int(provider.id),
        "provider_name": _string(provider.name, "workflow-config-provider"),
        "provider_kind": _string(
            provider.provider_kind, "workflow-config-provider-kind"
        ),
        "provider_owner_user_id": provider.user_id,
        "api_key_id": int(key.id),
        "api_key_owner_user_id": key.user_id,
        "api_key_created_at": _iso(key.created_at, "workflow-config-key-time"),
        "credential_source": _string(
            connection.credential_source or "",
            "workflow-config-credential-source",
            maximum=16,
        ),
        "credential_witness": _credential_witness(key, provider),
        "transport_identity": _model_transport_identity(key, provider),
        "capability_profile": capability_profile,
        "capability_profile_hash": hashlib.sha256(
            canonical_json(capability_profile).encode("utf-8")
        ).hexdigest(),
    }


def _verify_model_identity(identity: object, *, owner_user_id: int) -> None:
    if not isinstance(identity, Mapping):
        raise _error("workflow-config-model-evidence")
    if (
        set(identity)
        == {
            "identity_kind",
            "identity_version",
            "role",
            "model_id",
            "executor_profile",
            "capability_profile",
            "capability_profile_hash",
        }
        and identity.get("identity_kind") == EXTERNAL_EXECUTOR_IDENTITY_KIND
    ):
        _validate_model_shape(identity)
        return
    legacy_keys = {
        "model_id",
        "model_record_id",
        "model_owner_user_id",
        "model_revision",
        "model_updated_at",
        "provider_id",
        "provider_name",
        "provider_kind",
        "provider_owner_user_id",
        "api_key_id",
        "api_key_owner_user_id",
        "api_key_created_at",
        "credential_source",
        "credential_witness",
        "capability_profile",
        "capability_profile_hash",
    }
    current_keys = legacy_keys | {"transport_identity"}
    if frozenset(identity) not in {frozenset(legacy_keys), frozenset(current_keys)}:
        raise _error("workflow-config-model-evidence")
    _validate_model_capability_profile(identity)
    selected = _string(identity["model_id"], "workflow-config-model-id", maximum=255)
    current = _model_identity(selected, owner_user_id=owner_user_id)
    stable_keys = set(identity) - {
        "capability_profile",
        "capability_profile_hash",
    }
    if any(identity[key] != current[key] for key in stable_keys):
        raise _error("workflow-config-model-drift")


def _reasoning(
    model_id: str,
    user: User,
    slot: str,
    *,
    explicit_level: str | None = None,
) -> dict[str, Any]:
    if explicit_level is None:
        try:
            config = resolve_reasoning_config(
                user,
                model_id,
                None,
                addon_id="translator",
                reasoning_slot=slot,
            )
        except TypeError, ValueError:
            config = None
    else:
        try:
            level = normalize_reasoning_level(explicit_level)
        except ValueError as exc:
            raise _error("workflow-config-reasoning") from exc
        if level == "default":
            config = None
        elif is_reasoning_level_supported(user, model_id, level):
            config = {"level": level}
        else:
            raise _error("workflow-config-reasoning")
    return {
        "addon_id": "translator",
        "slot": slot,
        "model_id": model_id,
        "config": {"level": config["level"]} if config is not None else None,
    }


def _normalize_research(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _error("workflow-config-research")
    if set(value) != {
        "budget",
        "tool_declarations",
        "selected_provider_ids",
        "selected_addon_ids",
    }:
        raise _error("workflow-config-research")
    try:
        budget = _research_budget(value["budget"])
        declarations = _tool_declarations(value["tool_declarations"])
    except (TypeError, ValueError, BlockContractError) as exc:
        raise _error("workflow-config-research") from exc
    if (
        budget["max_model_calls"] > MAX_RESEARCH_MODEL_CALLS
        or budget["output_tokens"] > MAX_RESEARCH_OUTPUT_TOKENS
    ):
        raise _error("workflow-config-research-budget")

    def ids(raw: object, code: str) -> list[str]:
        if not isinstance(raw, (list, tuple)) or len(raw) > 128:
            raise _error(code)
        result: list[str] = []
        for item in raw:
            value = _string(item, code, maximum=128)
            if value in result:
                raise _error(code)
            result.append(value)
        return sorted(result)

    providers = ids(
        value["selected_provider_ids"], "workflow-config-research-providers"
    )
    addons = ids(value["selected_addon_ids"], "workflow-config-research-addons")
    if any(
        item["provider_id"] not in providers or item["addon_id"] not in addons
        for item in declarations
    ):
        raise _error("workflow-config-research-selection")
    return {
        "budget": budget,
        "tool_declarations": list(declarations),
        "selected_provider_ids": providers,
        "selected_addon_ids": addons,
    }


def _embedding_projection(owner_user_id: int) -> dict[str, Any] | None:
    """Freeze an optional embedding projection identity without building it."""

    from . import embedding_projection as projection

    setting_names = ("knowledge_embedding_model", "rag_embedding_model")
    for name in setting_names:
        setting = db.session.get(Setting, name)
        selected = str(setting.value or "").strip() if setting is not None else ""
        if not selected:
            continue
        try:
            model, dimensions = projection._resolve_model(
                owner_user_id=owner_user_id,
                model_id=selected,
                dimensions=None,
                session=db.session,
            )
            credential = projection._resolve_credential(
                model=model, owner_user_id=owner_user_id, session=db.session
            )
            provider = db.session.get(LLMProvider, model.provider_id)
            if provider is None:
                continue
            profile, signature = projection._profile_snapshot(
                model, task_type="similarity", dimensions=dimensions
            )
            return {
                "enabled": True,
                "setting_name": name,
                "model_record_id": int(model.id),
                "model_id": model.model_id,
                "model_owner_user_id": model.user_id,
                "model_revision": projection._model_revision(model),
                "model_updated_at": _iso(
                    model.updated_at, "workflow-config-embedding-model-time"
                ),
                "provider_id": int(provider.id),
                "provider_name": _string(
                    provider.name, "workflow-config-embedding-provider"
                ),
                "provider_kind": _string(
                    provider.provider_kind, "workflow-config-embedding-provider-kind"
                ),
                "provider_owner_user_id": provider.user_id,
                "api_key_id": int(credential.id),
                "api_key_owner_user_id": credential.user_id,
                "api_key_created_at": _iso(
                    credential.created_at, "workflow-config-embedding-key-time"
                ),
                "credential_witness": projection._credential_witness(
                    credential, provider=provider
                ),
                "dimensions": int(dimensions),
                "task_type": "similarity",
                "profile_id": profile.get("class"),
                "profile_version": profile.get("contract_version"),
                "profile_hash": hashlib.sha256(
                    canonical_json(profile).encode("utf-8")
                ).hexdigest(),
                "signature_hash": hashlib.sha256(
                    canonical_json(signature).encode("utf-8")
                ).hexdigest(),
            }
        except projection.EmbeddingProjectionError:
            continue
        except AttributeError, BlockContractError, KeyError, TypeError, ValueError:
            continue
    return None


def _verify_embedding_projection(value: object, *, owner_user_id: int) -> None:
    _verify_embedding_shape(value)
    expected = cast(Mapping[str, Any], value)
    from . import embedding_projection as projection

    try:
        model, dimensions = projection._resolve_model(
            owner_user_id=owner_user_id,
            model_id=_string(
                expected["model_id"],
                "workflow-config-embedding-model",
                maximum=255,
            ),
            dimensions=_integer(
                expected["dimensions"],
                "workflow-config-embedding-dimensions",
                minimum=1,
                maximum=10_000_000,
            ),
            session=db.session,
        )
        credential = projection._resolve_credential(
            model=model,
            owner_user_id=owner_user_id,
            session=db.session,
            api_key_id=_integer(
                expected["api_key_id"],
                "workflow-config-embedding-key",
                minimum=1,
                maximum=2**63 - 1,
            ),
        )
        provider = db.session.get(LLMProvider, model.provider_id)
        if provider is None:
            raise projection.EmbeddingProjectionError("embedding-provider-drift")
        profile, signature = projection._profile_snapshot(
            model,
            task_type="similarity",
            dimensions=dimensions,
        )
        current = {
            "enabled": True,
            "setting_name": expected["setting_name"],
            "model_record_id": int(model.id),
            "model_id": model.model_id,
            "model_owner_user_id": model.user_id,
            "model_revision": projection._model_revision(model),
            "model_updated_at": _iso(
                model.updated_at, "workflow-config-embedding-model-time"
            ),
            "provider_id": int(provider.id),
            "provider_name": _string(
                provider.name, "workflow-config-embedding-provider"
            ),
            "provider_kind": _string(
                provider.provider_kind, "workflow-config-embedding-provider-kind"
            ),
            "provider_owner_user_id": provider.user_id,
            "api_key_id": int(credential.id),
            "api_key_owner_user_id": credential.user_id,
            "api_key_created_at": _iso(
                credential.created_at, "workflow-config-embedding-key-time"
            ),
            "credential_witness": projection._credential_witness(
                credential, provider=provider
            ),
            "dimensions": int(dimensions),
            "task_type": "similarity",
            "profile_id": profile.get("class"),
            "profile_version": profile.get("contract_version"),
            "profile_hash": hashlib.sha256(
                canonical_json(profile).encode("utf-8")
            ).hexdigest(),
            "signature_hash": hashlib.sha256(
                canonical_json(signature).encode("utf-8")
            ).hexdigest(),
        }
    except (
        AttributeError,
        BlockContractError,
        KeyError,
        TypeError,
        ValueError,
        projection.EmbeddingProjectionError,
    ) as exc:
        raise _error("workflow-config-embedding-drift") from exc
    if dict(expected) != current:
        raise _error("workflow-config-embedding-drift")


def _scope(
    operation: TranslationOperation, source_plan: TranslationSourcePlan
) -> tuple[TranslationOperation, TranslationSourcePlan, int]:
    operation_state = (
        inspect(operation) if isinstance(operation, TranslationOperation) else None
    )
    plan_state = (
        inspect(source_plan) if isinstance(source_plan, TranslationSourcePlan) else None
    )
    if (
        operation_state is None
        or not operation_state.persistent
        or operation_state.deleted
        or operation_state.modified
        or plan_state is None
        or not plan_state.persistent
        or plan_state.deleted
        or plan_state.modified
    ):
        raise _error("workflow-config-durable-scope")
    record = cast(TranslationSourcePlan, source_plan)
    try:
        rehydrate_source_plan(record)
    except Exception as exc:
        raise _error("workflow-config-source-plan") from exc
    project_owner = db.session.get(User, operation.owner_user_id)
    if (
        operation.project_id != record.project_id
        or operation.kind != "document_block_translate"
        or operation.owner_user_id != getattr(record.project, "user_id", None)
        or operation.source_plan_id != record.id
        or operation.source_plan_hash != record.plan_hash
        or operation.source_content_hash != record.source_content_hash
        or record.project_id != getattr(record.project, "id", None)
        or record.project.engine_contract != DOCUMENT_BLOCK_ENGINE_CONTRACT
        or not isinstance(operation.owner_user_id, int)
        or project_owner is None
    ):
        raise _error("workflow-config-scope")
    return operation, record, int(operation.owner_user_id)


def _config_receipt_values(row: TranslationBlockWorkflowConfig) -> dict[str, Any]:
    return {
        "id": row.id,
        "operation_id": row.operation_id,
        "owner_user_id": row.owner_user_id,
        "project_id": row.project_id,
        "source_plan_id": row.source_plan_id,
        "source_plan_hash": row.source_plan_hash,
        "source_content_hash": row.source_content_hash,
        "config_hash": row.config_hash,
        "config_json": row.config_json,
        "created_at": _iso(row.created_at, "workflow-config-created-at"),
    }


def _validate_config(config: object) -> dict[str, Any]:
    if not isinstance(config, Mapping) or not _CONFIG_REQUIRED_KEYS.issubset(config):
        raise _error("workflow-config-keys")
    if set(config) - _CONFIG_REQUIRED_KEYS - _CONFIG_OPTIONAL_KEYS:
        raise _error("workflow-config-keys")
    if (
        config.get("contract_version") != WORKFLOW_CONFIG_VERSION
        or config.get("engine_contract") != DOCUMENT_BLOCK_ENGINE_CONTRACT
    ):
        raise _error("workflow-config-contract")
    for key in ("source_language", "target_language"):
        _string(config.get(key), "workflow-config-language", maximum=64)
    for key in ("enable_pre_analysis", "enable_research"):
        if not isinstance(config.get(key), bool):
            raise _error("workflow-config-phase-flag")
    if "research_dossier" in config:
        _normalize_research_dossier(config["research_dossier"])
        if (
            config["enable_pre_analysis"]
            or config["enable_research"]
            or "pre_analysis" in config
            or "research" in config
        ):
            raise _error("workflow-config-research-dossier")
    legacy_notes = config.get("enable_translator_notes")
    if "enable_translator_notes" in config and not isinstance(legacy_notes, bool):
        raise _error("workflow-config-notes-flag")
    if "annotation_profile" in config:
        try:
            annotation_profile = normalize_annotation_profile(
                config["annotation_profile"], legacy_notes
            )
        except BlockContractError as exc:
            raise _error("workflow-config-annotation-profile") from exc
        if annotation_profile != config["annotation_profile"]:
            raise _error("workflow-config-annotation-profile")
        if "enable_translator_notes" not in config or legacy_notes != (
            annotation_profile != "none"
        ):
            raise _error("workflow-config-notes-flag")
    else:
        try:
            normalize_annotation_profile(None, legacy_notes)
        except BlockContractError as exc:
            raise _error("workflow-config-annotation-profile") from exc
    try:
        translation_unit_mode = normalize_translation_unit_mode(
            config.get("translation_unit_mode")
        )
    except BlockContractError as exc:
        raise _error("workflow-config-translation-unit-mode") from exc
    source_plan = config.get("source_plan")
    if not isinstance(source_plan, Mapping) or set(source_plan) != {
        "id",
        "plan_hash",
        "content_hash",
    }:
        raise _error("workflow-config-source")
    _string(source_plan["id"], "workflow-config-source")
    _hash(source_plan["plan_hash"], "workflow-config-plan-hash")
    _hash(source_plan["content_hash"], "workflow-config-content-hash")
    _normalize_window_profile(config.get("window_profile"))
    writable_contract = config.get(
        "writable_source_contract_version",
        LEGACY_WRITABLE_SOURCE_CONTRACT_VERSION,
    )
    if writable_contract not in {
        LEGACY_WRITABLE_SOURCE_CONTRACT_VERSION,
        PARTS_WRITABLE_SOURCE_CONTRACT_VERSION,
        COMPACT_WRITABLE_SOURCE_CONTRACT_VERSION,
        WRITABLE_SOURCE_CONTRACT_VERSION,
        NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
        STUDY_NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
        SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION,
        SINGLE_COPY_WRITABLE_SOURCE_CONTRACT_VERSION,
        LOCATED_SOURCE_WRITABLE_CONTRACT_VERSION,
    }:
        raise _error("workflow-config-writable-source-contract")
    if translation_unit_mode == TRANSLATION_UNIT_MODE_ALIGNED_SENTENCES:
        if (
            "annotation_profile" not in config
            or "enable_translator_notes" not in config
            or "writable_source_contract_version" not in config
            or writable_contract not in {
                SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION,
                SINGLE_COPY_WRITABLE_SOURCE_CONTRACT_VERSION,
                LOCATED_SOURCE_WRITABLE_CONTRACT_VERSION,
            }
        ):
            raise _error("workflow-config-writable-source-contract")
    elif writable_contract in {
        SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION,
        SINGLE_COPY_WRITABLE_SOURCE_CONTRACT_VERSION,
        LOCATED_SOURCE_WRITABLE_CONTRACT_VERSION,
    }:
        raise _error("workflow-config-writable-source-contract")
    elif (
        "annotation_profile" in config and "writable_source_contract_version" in config
    ):
        expected_contract = {
            "none": WRITABLE_SOURCE_CONTRACT_VERSION,
            "restrained": NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
            "study": STUDY_NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
        }[annotation_profile]
        if writable_contract != expected_contract:
            raise _error("workflow-config-writable-source-contract")
    if config.get("protected_targets") is not None:
        normalize_protected_targets(config["protected_targets"])
    if config.get("translation_scope") is not None:
        normalize_translation_scope(config["translation_scope"])
    structure_context = config.get("document_structure_context")
    if structure_context is not None and structure_context != {
        "contract_version": DOCUMENT_STRUCTURE_CONTEXT_VERSION
    }:
        raise _error("workflow-config-structure-context")
    translation_context = config.get("translation_context")
    if translation_context is not None and not _valid_translation_context(
        translation_context
    ):
        raise _error("workflow-config-translation-context")
    if "foreign_language_policy" in config:
        if config["foreign_language_policy"] != "preserve-passages-v2":
            raise _error("workflow-config-foreign-language-policy")
        if (
            not isinstance(translation_context, Mapping)
            or translation_context.get("contract_version")
            not in {"translation-context-v1", TRANSLATION_CONTEXT_CONTRACT_VERSION}
        ):
            raise _error("workflow-config-foreign-language-policy-context")
    if config.get("owner_policy") is not None:
        _normalize_owner_policy(config["owner_policy"])
    translation = config.get("translation")
    if not isinstance(translation, Mapping) or set(translation) != {
        "model",
        "reasoning",
    }:
        raise _error("workflow-config-translation")
    _validate_model_shape(translation.get("model"))
    _validate_reasoning_shape(translation.get("reasoning"))
    if config.get("enable_pre_analysis"):
        phase = config.get("pre_analysis")
        if not isinstance(phase, Mapping) or frozenset(phase) not in {
            frozenset({"model", "reasoning"}),
            frozenset({"model", "reasoning", "scope_planner_contract"}),
        }:
            raise _error("workflow-config-analysis")
        if "scope_planner_contract" in phase and (
            phase["scope_planner_contract"]
            not in SUPPORTED_ANALYSIS_SCOPE_PLANNER_CONTRACTS
        ):
            raise _error("workflow-config-analysis-planner")
        _validate_model_shape(phase.get("model"))
        _validate_reasoning_shape(phase.get("reasoning"))
    elif "pre_analysis" in config:
        raise _error("workflow-config-disabled-phase")
    if config.get("enable_research"):
        phase = config.get("research")
        if not isinstance(phase, Mapping) or frozenset(phase) not in {
            frozenset({"model", "reasoning", "selected"}),
            frozenset(
                {
                    "model",
                    "reasoning",
                    "selected",
                    "input_planner_contract",
                }
            ),
        }:
            raise _error("workflow-config-research")
        if "input_planner_contract" in phase and (
            phase["input_planner_contract"]
            not in SUPPORTED_RESEARCH_INPUT_PLANNER_CONTRACTS
        ):
            raise _error("workflow-config-research-planner")
        _validate_model_shape(phase.get("model"))
        _validate_reasoning_shape(phase.get("reasoning"))
        selected = phase.get("selected")
        if not isinstance(selected, Mapping) or set(selected) != {
            "budget",
            "tool_declarations",
            "selected_provider_ids",
            "selected_addon_ids",
        }:
            raise _error("workflow-config-research")
        normalized_research = _normalize_research(selected)
        research_model = phase.get("model")
        capability_profile = (
            research_model.get("capability_profile")
            if isinstance(research_model, Mapping)
            else None
        )
        if not isinstance(capability_profile, Mapping) or normalized_research["budget"][
            "output_tokens"
        ] > capability_profile.get("max_output_tokens", 0):
            raise _error("workflow-config-research-budget")
    elif "research" in config:
        raise _error("workflow-config-disabled-phase")
    embedding = config.get("embedding_projection")
    if embedding is not None:
        _verify_embedding_shape(embedding)
    return cast(dict[str, Any], dict(config))


def _verify_embedding_shape(value: object) -> None:
    expected = {
        "enabled",
        "setting_name",
        "model_record_id",
        "model_id",
        "model_owner_user_id",
        "model_revision",
        "model_updated_at",
        "provider_id",
        "provider_name",
        "provider_kind",
        "provider_owner_user_id",
        "api_key_id",
        "api_key_owner_user_id",
        "api_key_created_at",
        "credential_witness",
        "dimensions",
        "task_type",
        "profile_id",
        "profile_version",
        "profile_hash",
        "signature_hash",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != expected
        or value.get("enabled") is not True
    ):
        raise _error("workflow-config-embedding-evidence")


def _validate_model_shape(value: object) -> None:
    external_keys = {
        "identity_kind",
        "identity_version",
        "role",
        "model_id",
        "executor_profile",
        "capability_profile",
        "capability_profile_hash",
    }
    if isinstance(value, Mapping) and frozenset(value) == frozenset(external_keys):
        if (
            value.get("identity_kind") != EXTERNAL_EXECUTOR_IDENTITY_KIND
            or value.get("identity_version") != EXTERNAL_EXECUTOR_IDENTITY_VERSION
            or value.get("role") != EXTERNAL_EXECUTOR_ROLE
            or value.get("model_id") != EXTERNAL_EXECUTOR_ROLE
        ):
            raise _error("workflow-config-external-identity")
        try:
            profile = normalize_executor_profile(value.get("executor_profile"))
        except (TypeError, ValueError) as exc:
            raise _error("workflow-config-external-identity") from exc
        expected = _external_executor_identity(profile)
        if dict(value) != expected:
            raise _error("workflow-config-external-identity")
        return
    legacy = {
        "model_id",
        "model_record_id",
        "model_owner_user_id",
        "model_revision",
        "model_updated_at",
        "provider_id",
        "provider_name",
        "provider_kind",
        "provider_owner_user_id",
        "api_key_id",
        "api_key_owner_user_id",
        "api_key_created_at",
        "credential_source",
        "credential_witness",
        "capability_profile",
        "capability_profile_hash",
    }
    current = legacy | {"transport_identity"}
    if not isinstance(value, Mapping) or frozenset(value) not in {
        frozenset(legacy),
        frozenset(current),
    }:
        raise _error("workflow-config-model-evidence")
    if "transport_identity" in value:
        transport = value["transport_identity"]
        if not isinstance(transport, Mapping) or set(transport) != {
            "contract_version",
            "provider_name",
            "provider_kind",
            "api_base",
            "api_version",
            "vertex_project",
            "vertex_location",
        }:
            raise _error("workflow-config-model-transport")
        if transport.get("contract_version") != MODEL_TRANSPORT_IDENTITY_VERSION:
            raise _error("workflow-config-model-transport")
        if transport.get("provider_name") != canonical_provider_name(
            _string(value.get("provider_name"), "workflow-config-provider")
        ) or transport.get("provider_kind") != value.get("provider_kind"):
            raise _error("workflow-config-model-transport")
        _optional_string(
            transport.get("api_base"),
            "workflow-config-model-transport",
            maximum=512,
        )
        _optional_string(
            transport.get("api_version"),
            "workflow-config-model-transport",
            maximum=100,
        )
        vertex_project = _optional_string(
            transport.get("vertex_project"),
            "workflow-config-model-transport",
            maximum=256,
        )
        vertex_location = _optional_string(
            transport.get("vertex_location"),
            "workflow-config-model-transport",
            maximum=256,
        )
        if (transport.get("provider_name") == "vertex_ai") != (
            vertex_project is not None and vertex_location is not None
        ):
            raise _error("workflow-config-model-transport")
    _validate_model_capability_profile(value)


def _validate_model_capability_profile(identity: Mapping[str, Any]) -> None:
    value = identity.get("capability_profile")
    if not isinstance(value, Mapping) or set(value) != set(
        ModelCapabilityProfile.__dataclass_fields__
    ):
        raise _error("workflow-config-model-capabilities")
    try:
        encoded = canonical_json(dict(value)).encode("utf-8")
        restored = json.loads(encoded)
    except (TypeError, ValueError, RecursionError) as exc:
        raise _error("workflow-config-model-capabilities") from exc
    if (
        len(encoded) > MAX_MODEL_CAPABILITY_PROFILE_BYTES
        or restored != dict(value)
        or identity.get("capability_profile_hash")
        != hashlib.sha256(encoded).hexdigest()
    ):
        raise _error("workflow-config-model-capabilities")
    if (
        value.get("model_id") != identity.get("model_id")
        or value.get("provider_name") != identity.get("provider_name")
        or value.get("provider_kind") != identity.get("provider_kind")
    ):
        raise _error("workflow-config-model-capabilities")
    for key in (
        "max_input_tokens",
        "max_output_tokens",
        "input_safety_margin_tokens",
        "usable_input_tokens",
    ):
        raw = value.get(key)
        if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
            raise _error("workflow-config-model-capabilities")
    if (
        value["max_input_tokens"] < 1
        or value["max_output_tokens"] < 1
        or value["usable_input_tokens"] < 1
        or value["usable_input_tokens"] > value["max_input_tokens"]
        or value["input_safety_margin_tokens"]
        != value["max_input_tokens"] - value["usable_input_tokens"]
    ):
        raise _error("workflow-config-model-capabilities")


def _validate_reasoning_shape(value: object) -> None:
    if not isinstance(value, Mapping) or set(value) != {
        "addon_id",
        "slot",
        "model_id",
        "config",
    }:
        raise _error("workflow-config-reasoning")
    if value.get("addon_id") != "translator":
        raise _error("workflow-config-reasoning")
    _string(value.get("slot"), "workflow-config-reasoning", maximum=64)
    _string(value.get("model_id"), "workflow-config-reasoning", maximum=255)
    setting = value.get("config")
    if setting is not None and (
        not isinstance(setting, Mapping)
        or set(setting) != {"level"}
        or setting.get("level")
        not in {"off", "minimal", "low", "medium", "high", "xhigh"}
    ):
        raise _error("workflow-config-reasoning")


def freeze_block_workflow_config(
    operation: TranslationOperation,
    source_plan: TranslationSourcePlan,
    *,
    user: User,
    settings: Mapping[str, Any],
    window_profile: Mapping[str, Any],
    research: Mapping[str, Any] | None = None,
    protected_targets: Mapping[str, Any] | None = None,
    translation_scope: Mapping[str, Any] | None = None,
    translation_reasoning: str | None = None,
    executor_profile: ExecutorProfile | Mapping[str, Any] | None = None,
    research_dossier: Mapping[str, Any] | None = None,
    context_mode: object = None,
) -> dict[str, Any]:
    """Build a strict, source/model-bound config without touching the DB."""

    if not isinstance(user, User) or not isinstance(user.id, int):
        raise _error("workflow-config-user")
    if not isinstance(settings, Mapping):
        raise _error("workflow-config-settings")
    operation, record, owner_user_id = _scope(operation, source_plan)
    if user.id != owner_user_id:
        raise _error("workflow-config-user")
    legacy_notes = settings.get("enable_translator_notes")
    if "enable_translator_notes" in settings and not isinstance(legacy_notes, bool):
        raise _error("workflow-config-notes-flag")
    try:
        annotation_profile = normalize_annotation_profile(
            settings.get("annotation_profile"), legacy_notes
        )
    except BlockContractError as exc:
        raise _error("workflow-config-annotation-profile") from exc
    try:
        translation_unit_mode = normalize_translation_unit_mode(
            settings.get("translation_unit_mode")
        )
    except BlockContractError as exc:
        raise _error("workflow-config-translation-unit-mode") from exc
    pre_analysis = settings.get("enable_pre_analysis", False)
    enable_research = settings.get("enable_research", False)
    if not isinstance(pre_analysis, bool) or not isinstance(enable_research, bool):
        raise _error("workflow-config-phase-flag")
    normalized_research_dossier = (
        _normalize_research_dossier(research_dossier)
        if research_dossier is not None
        else None
    )
    if normalized_research_dossier is not None and (pre_analysis or enable_research):
        raise _error("workflow-config-research-dossier")
    execution_mode = str(settings.get("execution_mode") or "managed").strip().lower()
    if execution_mode not in {"managed", "passive"}:
        raise _error("workflow-config-execution-mode")
    if execution_mode == "passive":
        if pre_analysis or enable_research:
            raise _error("workflow-config-passive-phase")
        if executor_profile is None:
            raise _error("workflow-config-external-identity")
        try:
            external_profile = normalize_executor_profile(executor_profile)
        except (TypeError, ValueError) as exc:
            raise _error("workflow-config-external-identity") from exc
        translation_model = _external_executor_identity(external_profile)
        translation_reasoning_material = {
            "addon_id": "translator",
            "slot": TRANSLATION_REASONING_SLOT,
            "model_id": EXTERNAL_EXECUTOR_ROLE,
            "config": None,
        }
    else:
        selected = _string(
            settings.get("translator_model"),
            "workflow-config-translator-model",
            maximum=255,
        )
        translation_model = _model_identity(selected, owner_user_id=owner_user_id)
        translation_reasoning_material = _reasoning(
            selected,
            user,
            TRANSLATION_REASONING_SLOT,
            explicit_level=translation_reasoning,
        )
    config: dict[str, Any] = {
        "contract_version": WORKFLOW_CONFIG_VERSION,
        "engine_contract": DOCUMENT_BLOCK_ENGINE_CONTRACT,
        "translation_unit_mode": translation_unit_mode,
        "source_language": _string(
            record.source_language, "workflow-config-language", maximum=64
        ),
        "target_language": _string(
            record.project.target_lang, "workflow-config-language", maximum=64
        ),
        "source_plan": {
            "id": record.id,
            "plan_hash": record.plan_hash,
            "content_hash": record.source_content_hash,
        },
        "enable_pre_analysis": pre_analysis,
        "enable_research": enable_research,
        "translation": {
            "model": translation_model,
            "reasoning": translation_reasoning_material,
        },
        "pre_analysis": None,
        "research": None,
        "owner_policy": None,
        "window_profile": _normalize_window_profile(window_profile),
        "writable_source_contract_version": (
            LOCATED_SOURCE_WRITABLE_CONTRACT_VERSION
            if translation_unit_mode == TRANSLATION_UNIT_MODE_ALIGNED_SENTENCES
            else {
                "none": WRITABLE_SOURCE_CONTRACT_VERSION,
                "restrained": NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
                "study": STUDY_NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
            }[annotation_profile]
        ),
        "document_structure_context": {
            "contract_version": DOCUMENT_STRUCTURE_CONTEXT_VERSION
        },
        "translation_context": (
            LEGACY_TRANSLATION_CONTEXT.copy()
            if context_mode is None
            else translation_context_policy(context_mode)
        ),
        "foreign_language_policy": "preserve-passages-v2",
        "annotation_profile": annotation_profile,
        "enable_translator_notes": annotation_profile != "none",
    }
    if normalized_research_dossier is not None:
        config["research_dossier"] = normalized_research_dossier
    # Passive starts must not inspect Carlina's embedding/model catalogue.
    if execution_mode == "managed":
        config["embedding_projection"] = _embedding_projection(owner_user_id)
    if protected_targets is not None:
        config["protected_targets"] = normalize_protected_targets(protected_targets)
    if translation_scope is not None:
        config["translation_scope"] = normalize_translation_scope(translation_scope)
    if pre_analysis:
        analyzer = _string(
            settings.get("analyzer_model"),
            "workflow-config-analysis-model",
            maximum=255,
        )
        config["pre_analysis"] = {
            "model": _model_identity(analyzer, owner_user_id=owner_user_id),
            "reasoning": _reasoning(analyzer, user, ANALYSIS_REASONING_SLOT),
            "scope_planner_contract": ANALYSIS_SCOPE_PLANNER_CONTRACT_VERSION,
        }
    if enable_research:
        researcher = _string(
            settings.get("researcher_model"),
            "workflow-config-research-model",
            maximum=255,
        )
        if research is None:
            raise _error("workflow-config-research")
        normalized = _normalize_research(research)
        config["research"] = {
            "model": _model_identity(researcher, owner_user_id=owner_user_id),
            "reasoning": _reasoning(researcher, user, RESEARCH_REASONING_SLOT),
            "selected": normalized,
            "input_planner_contract": RESEARCH_INPUT_PLANNER_CONTRACT_VERSION,
        }
    custom_prompt = _normalize_owner_policy(settings.get("custom_prompt"))
    explicit_policy = _normalize_owner_policy(settings.get("owner_policy"))
    if (
        custom_prompt is not None
        and explicit_policy is not None
        and custom_prompt != explicit_policy
    ):
        raise _error("workflow-config-owner-policy")
    owner_policy = explicit_policy or custom_prompt
    if owner_policy is None:
        config.pop("owner_policy")
    else:
        config["owner_policy"] = owner_policy
    # Disabled phases are represented by a flag plus absent material, never by
    # null placeholders in the canonical encrypted payload.
    if not pre_analysis:
        config.pop("pre_analysis")
    if not enable_research:
        config.pop("research")
    if config.get("embedding_projection") is None:
        config.pop("embedding_projection", None)
    return _validate_config(config)


def persist_block_workflow_config(
    operation: TranslationOperation,
    source_plan: TranslationSourcePlan,
    config: Mapping[str, Any],
) -> TranslationBlockWorkflowConfig:
    """Persist or re-use one exact config row; never commits the transaction."""

    operation, record, owner_user_id = _scope(operation, source_plan)
    material = _validate_config(config)
    source = material["source_plan"]
    if (
        source.get("id") != record.id
        or source.get("plan_hash") != record.plan_hash
        or source.get("content_hash") != record.source_content_hash
    ):
        raise _error("workflow-config-source")
    config_hash = hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()
    existing = TranslationBlockWorkflowConfig.query.filter_by(
        operation_id=operation.id
    ).one_or_none()
    if existing is not None:
        rehydrate_block_workflow_config(
            existing, operation=operation, source_plan=record
        )
        if existing.config_hash != config_hash or existing.config_json != material:
            raise _error("workflow-config-conflict")
        return existing
    now = utc_now_db()
    # Assign the identity before sealing the receipt.  SQLAlchemy's Python
    # default is otherwise populated only during flush, which would make the
    # retained receipt attest to ``id=None``.
    row = TranslationBlockWorkflowConfig()
    row.id = str(uuid.uuid4())
    row.operation_id = operation.id
    row.owner_user_id = owner_user_id
    row.project_id = record.project_id
    row.source_plan_id = record.id
    row.source_plan_hash = record.plan_hash
    row.source_content_hash = record.source_content_hash
    row.config_hash = config_hash
    row.config_json = material
    row.config_receipt = "0" * 64
    row.created_at = now
    row.config_receipt = make_receipt(
        purpose=CONFIG_RECEIPT_PURPOSE, values=_config_receipt_values(row)
    )
    try:
        with db.session.begin_nested():
            db.session.add(row)
            db.session.flush()
    except IntegrityError as exc:
        winner = TranslationBlockWorkflowConfig.query.filter_by(
            operation_id=operation.id
        ).one_or_none()
        if winner is None:
            raise _error("workflow-config-persist") from exc
        rehydrate_block_workflow_config(winner, operation=operation, source_plan=record)
        if winner.config_hash != config_hash or winner.config_json != material:
            raise _error("workflow-config-conflict") from exc
        return winner
    return row


def _rehydrate_block_workflow_config(
    row: TranslationBlockWorkflowConfig,
    *,
    operation: TranslationOperation | None = None,
    source_plan: TranslationSourcePlan | None = None,
    require_live_models: bool,
) -> TranslationBlockWorkflowConfig:
    """Verify one retained snapshot, optionally against mutable live readiness."""

    if not isinstance(row, TranslationBlockWorkflowConfig) or not row.id:
        raise _error("workflow-config-missing")
    operation = operation or db.session.get(TranslationOperation, row.operation_id)
    source_plan = source_plan or db.session.get(
        TranslationSourcePlan, row.source_plan_id
    )
    if operation is None or source_plan is None:
        raise _error("workflow-config-scope")
    operation, record, owner_user_id = _scope(operation, source_plan)
    if (
        row.operation_id != operation.id
        or row.owner_user_id != owner_user_id
        or row.project_id != record.project_id
        or row.source_plan_id != record.id
        or row.source_plan_hash != record.plan_hash
        or row.source_content_hash != record.source_content_hash
    ):
        raise _error("workflow-config-scope")
    material = _validate_config(row.config_json)
    expected_hash = hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()
    if row.config_hash != expected_hash:
        raise _error("workflow-config-hash")
    verify_receipt(
        stored=row.config_receipt,
        purpose=CONFIG_RECEIPT_PURPOSE,
        values=_config_receipt_values(row),
        code="workflow-config-receipt",
    )
    source = material["source_plan"]
    if source != {
        "id": record.id,
        "plan_hash": record.plan_hash,
        "content_hash": record.source_content_hash,
    }:
        raise _error("workflow-config-source")
    if require_live_models:
        _verify_model_identity(
            material["translation"]["model"], owner_user_id=owner_user_id
        )
        if material.get("enable_pre_analysis"):
            _verify_model_identity(
                material["pre_analysis"]["model"], owner_user_id=owner_user_id
            )
        if material.get("enable_research"):
            _verify_model_identity(
                material["research"]["model"], owner_user_id=owner_user_id
            )
        if material.get("embedding_projection") is not None:
            _verify_embedding_projection(
                material["embedding_projection"], owner_user_id=owner_user_id
            )
    return row


def rehydrate_block_workflow_config(
    row: TranslationBlockWorkflowConfig,
    *,
    operation: TranslationOperation | None = None,
    source_plan: TranslationSourcePlan | None = None,
) -> TranslationBlockWorkflowConfig:
    """Verify a config selected for execution, including current readiness."""

    return _rehydrate_block_workflow_config(
        row,
        operation=operation,
        source_plan=source_plan,
        require_live_models=True,
    )


def rehydrate_historical_block_workflow_config(
    row: TranslationBlockWorkflowConfig,
    *,
    operation: TranslationOperation | None = None,
    source_plan: TranslationSourcePlan | None = None,
) -> TranslationBlockWorkflowConfig:
    """Verify immutable historical evidence without requiring old credentials.

    A rotated credential, retired model row, or changed live capability record
    must not make an installation-key-authenticated historical snapshot
    unverifiable.  New execution still uses ``rehydrate_block_workflow_config``
    and therefore retains the strict current-readiness gate.
    """

    return _rehydrate_block_workflow_config(
        row,
        operation=operation,
        source_plan=source_plan,
        require_live_models=False,
    )


def safe_block_workflow_config_projection(
    row: TranslationBlockWorkflowConfig,
) -> dict[str, Any]:
    """Return a bounded diagnostic projection without encrypted policy text."""

    material = _validate_config(row.config_json)
    result: dict[str, Any] = {
        "id": row.id,
        "operation_id": row.operation_id,
        "project_id": row.project_id,
        "source_plan_id": row.source_plan_id,
        "source_plan_hash": row.source_plan_hash,
        "config_hash": row.config_hash,
        "contract_version": material["contract_version"],
        "enable_pre_analysis": material["enable_pre_analysis"],
        "enable_research": material["enable_research"],
        "model_identity_kind": material["translation"]["model"].get(
            "identity_kind", "managed"
        ),
        "model_identity_role": material["translation"]["model"].get("role", "managed"),
        "has_embedding_projection": "embedding_projection" in material,
        "window_profile": material["window_profile"],
        "annotation_profile": material.get("annotation_profile", "none"),
        "translation_unit_mode": normalize_translation_unit_mode(
            material.get("translation_unit_mode")
        ),
        "writable_source_contract_version": material.get(
            "writable_source_contract_version",
            LEGACY_WRITABLE_SOURCE_CONTRACT_VERSION,
        ),
        "created_at": _iso(row.created_at, "workflow-config-created-at"),
    }
    return result


__all__ = [
    "ANALYSIS_REASONING_SLOT",
    "DOCUMENT_STRUCTURE_CONTEXT_VERSION",
    "RESEARCH_REASONING_SLOT",
    "TRANSLATION_REASONING_SLOT",
    "WORKFLOW_CONFIG_VERSION",
    "freeze_block_workflow_config",
    "persist_block_workflow_config",
    "rehydrate_block_workflow_config",
    "rehydrate_historical_block_workflow_config",
    "safe_block_workflow_config_projection",
]
