"""Replaceable dense embedding projections for the Translator block runtime.

This module intentionally has no dependency on the legacy Translator segment
index.  Source plans, anchors, and witnessed translation-memory entries remain
the authority; this module only records an encrypted retrieval projection and
an independently rebuildable sqlite-vec namespace.
"""

from __future__ import annotations

import base64
import hashlib
import math
import re
import struct
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, cast

from sqlalchemy import bindparam, case, inspect, or_, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from carlina.core.utc import utc_now_db

from ...extensions import db
from ...models import ApiKey, LLMModel, LLMProvider, Setting
from .analysis_contract import private_witness, private_witness_candidates
from .block_contract import BlockContractError, canonical_json
from .block_persistence import rehydrate_source_plan
from .models import (
    TranslationEmbeddingActive,
    TranslationEmbeddingEntry,
    TranslationEmbeddingGeneration,
    TranslationMemoryEntry,
    TranslationProject,
    TranslationSourcePlan,
)
from .translation_memory_persistence import rehydrate_translation_memory_entry

PROJECTION_KINDS = frozenset({"block", "anchor", "memory"})
EMBEDDING_CONTRACT_VERSION = "translator-embedding-projection-v1"
EXTRACTOR_VERSION = "translator-source-subjects-v1"
GENERATION_PURPOSE = "translator-embedding-generation-v1"
ENTRY_PURPOSE = "translator-embedding-entry-v1"
SUBJECT_PURPOSE = "translator-embedding-subject-v1"
SOURCE_PURPOSE = "translator-embedding-source-v1"
POINTER_PURPOSE = "translator-embedding-pointer-v1"
_ZERO_WITNESS = "0" * 64
_NAMESPACE_RE = re.compile(r"^translator_emb_[a-z0-9_]{1,112}$")


class EmbeddingProjectionError(BlockContractError):
    """Fail-closed projection contract error."""


class EmbeddingActivationConflict(EmbeddingProjectionError):
    """The active pointer changed since the caller's expected value."""


class EmbeddingBackendError(EmbeddingProjectionError):
    """A backend/provider/vector-output fault that can fail a candidate."""


class EmbeddingBackend(Protocol):
    """Dense-only backend contract used by generation build and retrieval."""

    def embedding_identity(self) -> Mapping[str, Any]: ...

    def create(self, *, namespace: str, dimensions: int) -> None: ...

    def embed_documents(
        self,
        texts: list[str],
        *,
        generation: TranslationEmbeddingGeneration,
    ) -> list[list[float]]: ...

    def upsert(
        self,
        row_ids: list[int],
        embeddings: list[list[float]],
        *,
        include_lexical_text: bool = False,
    ) -> None: ...

    def row_ids(self, *, namespace: str) -> list[int]: ...

    def vector_witness(
        self, *, namespace: str, row_ids: list[int], dimensions: int
    ) -> str: ...

    def search_semantic(
        self,
        query_text: str,
        *,
        namespace: str,
        dimensions: int,
        model: str,
        task_type: str,
        top_k: int,
        allowed_row_ids: list[int],
    ) -> list[tuple[int, float, float]]: ...

    def drop(self, *, namespace: str, dimensions: int) -> None: ...


def _session(session: Any | None) -> Any:
    if session is None:
        return db.session()
    # ``db.session`` is a scoped-session proxy, while source-plan ownership
    # checks need the concrete Session identity used by SQLAlchemy state.
    if callable(session) and hasattr(session, "registry"):
        return session()
    return session


def _generation_for_session(
    generation: TranslationEmbeddingGeneration | str,
    *,
    session: Any,
) -> TranslationEmbeddingGeneration:
    """Reload a generation by identity in the UoW that owns side effects.

    Callers may hand us an expired, detached, or foreign-session ORM object.
    Never use that object's potentially stale fields for provider I/O or
    lifecycle transitions; the active session is the sole source of truth.
    """

    generation_id = (
        generation.id
        if isinstance(generation, TranslationEmbeddingGeneration)
        else str(generation)
    )
    if not generation_id:
        raise EmbeddingProjectionError("embedding-generation-record")
    for candidate in session.identity_map.values():
        if (
            isinstance(candidate, TranslationEmbeddingGeneration)
            and candidate.id == generation_id
            and (state := inspect(candidate)) is not None
            and state.modified
        ):
            raise EmbeddingProjectionError("embedding-generation-session-dirty")
    with session.no_autoflush:
        row = session.get(
            TranslationEmbeddingGeneration,
            generation_id,
            populate_existing=True,
        )
    if row is None:
        raise EmbeddingProjectionError("embedding-generation-missing")
    return row


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(timespec="microseconds")
    return str(value)


def _hash_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


_BACKEND_IDENTITY_KEYS = frozenset(
    {
        "owner_user_id",
        "model_record_id",
        "model_id",
        "provider_id",
        "api_key_id",
        "dimensions",
        "task_type",
        "query_prefix",
        "passage_prefix",
        "profile_hash",
        "signature_hash",
    }
)


def _expected_backend_identity(
    generation: TranslationEmbeddingGeneration,
) -> dict[str, Any]:
    return {
        "owner_user_id": generation.owner_user_id,
        "model_record_id": generation.model_record_id,
        "model_id": generation.model_id,
        "provider_id": generation.provider_id,
        "api_key_id": generation.api_key_id,
        "dimensions": generation.dimensions,
        "task_type": generation.task_type,
        "query_prefix": generation.query_prefix,
        "passage_prefix": generation.passage_prefix,
        "profile_hash": _hash_json(generation.profile_json),
        "signature_hash": generation.signature_hash,
    }


def _verify_backend_identity(
    backend: Any, generation: TranslationEmbeddingGeneration
) -> None:
    """Authenticate the injected vector backend before any semantic query."""

    identity_method = getattr(backend, "embedding_identity", None)
    if not callable(identity_method):
        raise EmbeddingProjectionError("embedding-backend-identity")
    try:
        identity = identity_method()
    except Exception as exc:
        raise EmbeddingProjectionError("embedding-backend-identity") from exc
    expected = _expected_backend_identity(generation)
    if (
        not isinstance(identity, Mapping)
        or set(identity) != _BACKEND_IDENTITY_KEYS
        or any(
            type(identity[key]) is not type(expected[key])
            or identity[key] != expected[key]
            for key in _BACKEND_IDENTITY_KEYS
        )
    ):
        raise EmbeddingProjectionError("embedding-backend-identity")


def _bounded_error_code(exc: BaseException) -> str:
    if isinstance(exc, EmbeddingBackendError) and str(exc):
        message = re.sub(r"[^a-z0-9_]+", "_", str(exc).casefold()).strip("_")
        if message:
            return message[:64]
    name = type(exc).__name__.casefold()
    name = re.sub(r"[^a-z0-9_]+", "_", name).strip("_")
    return (name or "embedding_failed")[:64]


def _require_plan(
    record: TranslationSourcePlan, *, session: Any
) -> tuple[Any, TranslationProject, int]:
    if not isinstance(record, TranslationSourcePlan):
        raise EmbeddingProjectionError("embedding-source-plan-type")
    state = inspect(record)
    if state is None or not state.persistent or state.session is not session:
        raise EmbeddingProjectionError("embedding-source-plan-session")
    project = session.get(TranslationProject, record.project_id)
    if project is None or not isinstance(project.user_id, int):
        raise EmbeddingProjectionError("embedding-project-owner")
    plan = rehydrate_source_plan(record)
    if plan.plan_hash != record.plan_hash:
        raise EmbeddingProjectionError("embedding-plan-hash")
    return plan, project, int(project.user_id)


def _model_revision(model: LLMModel) -> str:
    overrides = (
        model.capability_overrides
        if isinstance(model.capability_overrides, Mapping)
        else {}
    )
    revision = overrides.get("revision") or overrides.get("model_revision")
    return str(revision or _iso(model.updated_at) or "database-configured")


def _setting_value(session: Any, key: str) -> str | None:
    row = session.get(Setting, key)
    value = str(row.value or "").strip() if row is not None else ""
    return value or None


def _resolve_model(
    *,
    owner_user_id: int,
    model_id: str | None,
    dimensions: int | None,
    session: Any,
) -> tuple[LLMModel, int]:
    """Resolve the exact visible model with the caller's explicit session.

    The Knowledge resolver is intentionally not used here: it reads the
    scoped application session and can therefore observe a different UoW from
    the prepared generation.
    """
    requested = str(model_id or "").strip()
    if not requested:
        requested = _setting_value(session, "knowledge_embedding_model") or ""
    if not requested:
        requested = _setting_value(session, "rag_embedding_model") or ""

    def eligible_query():
        return (
            session.query(LLMModel)
            .join(LLMProvider, LLMModel.provider_id == LLMProvider.id)
            .filter(
                LLMModel.is_active.is_(True),
                LLMModel.model_type == "embedding",
                or_(
                    (LLMModel.user_id == owner_user_id)
                    & or_(
                        LLMProvider.user_id == owner_user_id,
                        LLMProvider.user_id.is_(None),
                    ),
                    (LLMModel.user_id.is_(None)) & LLMProvider.user_id.is_(None),
                ),
            )
        )

    model = None
    if requested:
        model = (
            eligible_query()
            .filter(LLMModel.model_id == requested)
            .order_by(
                case((LLMModel.user_id == owner_user_id, 0), else_=1), LLMModel.id
            )
            .first()
        )
        # A configured/requested model is an explicit contract.  Falling back
        # to a different model would silently change dimensions and semantics.
        if model is None:
            raise EmbeddingProjectionError("embedding-model-unavailable")
    else:
        model = (
            eligible_query()
            .order_by(
                case((LLMModel.user_id == owner_user_id, 0), else_=1), LLMModel.id
            )
            .first()
        )
    if model is None or model.provider is None:
        raise EmbeddingProjectionError("embedding-model-unavailable")
    configured = (
        dimensions
        if dimensions is not None
        else _setting_value(session, "knowledge_embedding_dimensions")
    )
    try:
        selected = int(configured or model.default_dimensions or 0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise EmbeddingProjectionError("embedding-dimensions") from exc
    native = int(model.default_dimensions or selected)
    if (
        selected <= 0
        or (not model.supports_mrl and selected != native)
        or (model.supports_mrl and selected > native)
    ):
        raise EmbeddingProjectionError("embedding-dimensions")
    return model, selected


def _credential_witness(key: ApiKey, *, provider: LLMProvider) -> str:
    return private_witness(
        {
            "id": int(key.id),
            "owner_user_id": key.user_id,
            "provider_id": int(key.provider_id),
            "provider_name": provider.name,
            "created_at": _iso(key.created_at),
            "api_key": key.api_key,
            "api_base": key.api_base,
            "api_version": key.api_version,
            "is_active": bool(key.is_active),
        },
        purpose="translator-embedding-credential-v1",
    )


def _resolve_credential(
    *, model: LLMModel, owner_user_id: int, session: Any, api_key_id: int | None = None
) -> ApiKey:
    provider = session.get(LLMProvider, model.provider_id)
    if provider is None or provider.name != getattr(model.provider, "name", None):
        raise EmbeddingProjectionError("embedding-provider-drift")
    visible_owner_clause = (
        ApiKey.user_id == owner_user_id
        if provider.user_id is not None
        else or_(ApiKey.user_id == owner_user_id, ApiKey.user_id.is_(None))
    )
    query = session.query(ApiKey).filter(
        ApiKey.provider_id == provider.id,
        ApiKey.is_active.is_(True),
        visible_owner_clause,
    )
    if api_key_id is not None:
        query = query.filter(ApiKey.id == int(api_key_id))
    rows = query.order_by(
        case((ApiKey.user_id == owner_user_id, 0), else_=1), ApiKey.id
    ).all()
    if not rows:
        raise EmbeddingProjectionError("embedding-credential-unavailable")
    return rows[0]


def _profile_snapshot(
    model: LLMModel, *, task_type: str, dimensions: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    from ...embedding_profiles import DynamicEmbeddingProfile

    profile = DynamicEmbeddingProfile(model, task_type=task_type)
    try:
        document_kwargs = profile.request_kwargs_for_documents(dimensions=dimensions)
        query_kwargs = profile.request_kwargs_for_query(dimensions=dimensions)
    except Exception as exc:
        raise EmbeddingProjectionError("embedding-profile-invalid") from exc
    overrides = (
        model.capability_overrides
        if isinstance(model.capability_overrides, Mapping)
        else {}
    )
    normalization = str(
        overrides.get("normalization")
        or ("provider_l2" if model.prefix_style == "gemini_v2" else "profile_defined")
    )
    profile_values = {
        "contract_version": EMBEDDING_CONTRACT_VERSION,
        "class": type(profile).__name__,
        "model_record_id": int(model.id),
        "model_id": model.model_id,
        "task_type": task_type,
        "dimensions": dimensions,
        "prefix_style": model.prefix_style or "none",
        "query_prefix": model.custom_query_prefix,
        "passage_prefix": model.custom_passage_prefix,
        "document_request_kwargs": document_kwargs,
        "query_request_kwargs": query_kwargs,
        "supports_mrl": bool(model.supports_mrl),
        "normalization": normalization,
        "max_document_batch_size": profile.max_document_batch_size,
    }
    signature = {
        **profile_values,
        "projection_kind": None,
        "owner_user_id": None,
        "project_id": None,
        "source_plan_id": None,
        "provider_id": int(model.provider_id),
        "provider_name": model.provider.name if model.provider is not None else None,
        "provider_kind": model.provider.provider_kind if model.provider is not None else None,
        "model_owner_user_id": model.user_id,
        "model_revision": _model_revision(model),
        "model_updated_at": _iso(model.updated_at),
        "distance_metric": "cosine_equivalent_l2",
        "extractor_version": EXTRACTOR_VERSION,
    }
    return profile_values, signature


def _generation_values(row: TranslationEmbeddingGeneration) -> dict[str, Any]:
    return {
        "contract_version": EMBEDDING_CONTRACT_VERSION,
        "id": row.id,
        "owner_user_id": row.owner_user_id,
        "project_id": row.project_id,
        "source_plan_id": row.source_plan_id,
        "source_plan_hash": row.source_plan_hash,
        "projection_kind": row.projection_kind,
        "model_record_id": row.model_record_id,
        "model_owner_user_id": row.model_owner_user_id,
        "provider_id": row.provider_id,
        "provider_name": row.provider_name,
        "provider_kind": row.provider_kind,
        "api_key_id": row.api_key_id,
        "api_key_owner_user_id": row.api_key_owner_user_id,
        "api_key_created_at": _iso(row.api_key_created_at),
        "credential_witness": row.credential_witness,
        "model_id": row.model_id,
        "model_revision": row.model_revision,
        "model_updated_at": _iso(row.model_updated_at),
        "dimensions": row.dimensions,
        "task_type": row.task_type,
        "normalization": row.normalization,
        "distance_metric": row.distance_metric,
        "prefix_style": row.prefix_style,
        "query_prefix": row.query_prefix,
        "passage_prefix": row.passage_prefix,
        "extractor_version": row.extractor_version,
        "profile": row.profile_json,
        "signature": row.signature_json,
        "signature_hash": row.signature_hash,
        "namespace": row.namespace,
        "entry_count": row.entry_count,
        "previous_generation_id": row.previous_generation_id,
    }


def _state_values(row: TranslationEmbeddingGeneration) -> dict[str, Any]:
    return {
        "contract_version": EMBEDDING_CONTRACT_VERSION,
        "record_hash": row.record_hash,
        "status": row.status,
        "entry_count": row.entry_count,
        "health": row.health_json,
        "error": row.error_json,
        "vector_witness": row.vector_witness,
        "state_version": row.state_version,
        "created_at": _iso(row.created_at),
        "prepared_at": _iso(row.prepared_at),
        "completed_at": _iso(row.completed_at),
        "activated_at": _iso(row.activated_at),
        "retired_at": _iso(row.retired_at),
    }


def _refresh_state_receipt(row: TranslationEmbeddingGeneration) -> None:
    row.state_receipt = private_witness(
        _state_values(row), purpose="translator-embedding-state-v1"
    )


def _verify_lifecycle(row: TranslationEmbeddingGeneration) -> None:
    """Validate the monotonic lifecycle independently of encrypted receipts."""

    if int(row.state_version or 0) < 1 or not row.created_at or not row.prepared_at:
        raise EmbeddingProjectionError("embedding-generation-lifecycle")
    if row.prepared_at < row.created_at:
        raise EmbeddingProjectionError("embedding-generation-lifecycle")
    if row.completed_at is not None and row.completed_at < row.prepared_at:
        raise EmbeddingProjectionError("embedding-generation-lifecycle")
    if row.activated_at is not None and (
        row.completed_at is None or row.activated_at < row.completed_at
    ):
        raise EmbeddingProjectionError("embedding-generation-lifecycle")
    if row.retired_at is not None and (
        row.activated_at is None or row.retired_at < row.activated_at
    ):
        raise EmbeddingProjectionError("embedding-generation-lifecycle")
    if row.status == "building":
        valid = (
            row.state_version == 1
            and row.completed_at is None
            and row.activated_at is None
            and row.retired_at is None
            and row.error_json is None
            and row.vector_witness == _ZERO_WITNESS
        )
    elif row.status == "ready":
        valid = (
            row.state_version >= 2
            and row.completed_at is not None
            and row.activated_at is None
            and row.retired_at is None
            and row.error_json is None
            and row.vector_witness != _ZERO_WITNESS
            and isinstance(row.health_json, Mapping)
            and row.health_json.get("vector_witness") == row.vector_witness
        )
    elif row.status == "failed":
        valid = (
            row.state_version >= 2
            and row.completed_at is not None
            and row.activated_at is None
            and row.retired_at is None
            and isinstance(row.error_json, Mapping)
            and row.vector_witness == _ZERO_WITNESS
        )
    elif row.status == "active":
        valid = (
            row.state_version >= 3
            and row.completed_at is not None
            and row.activated_at is not None
            and row.retired_at is None
            and row.error_json is None
            and row.vector_witness != _ZERO_WITNESS
        )
    elif row.status == "retired":
        valid = (
            row.state_version >= 4
            and row.completed_at is not None
            and row.activated_at is not None
            and row.retired_at is not None
            and row.error_json is None
            and row.vector_witness != _ZERO_WITNESS
        )
    else:
        valid = False
    if not valid:
        raise EmbeddingProjectionError("embedding-generation-lifecycle")


def _entry_values(row: TranslationEmbeddingEntry) -> dict[str, Any]:
    return {
        "contract_version": EMBEDDING_CONTRACT_VERSION,
        "id": row.id,
        "generation_id": row.generation_id,
        "owner_user_id": row.owner_user_id,
        "project_id": row.project_id,
        "source_plan_id": row.source_plan_id,
        "subject_type": row.subject_type,
        "subject_id": row.subject_id,
        "ordinal": row.ordinal,
        "vector_row_id": row.vector_row_id,
        "source_witness_hash": row.source_witness_hash,
        "subject_receipt": row.subject_receipt,
    }


def _pointer_values(row: TranslationEmbeddingActive) -> dict[str, Any]:
    return {
        "contract_version": EMBEDDING_CONTRACT_VERSION,
        "owner_user_id": row.owner_user_id,
        "project_id": row.project_id,
        "source_plan_id": row.source_plan_id,
        "projection_kind": row.projection_kind,
        "generation_id": row.generation_id,
        "generation_record_hash": row.generation_record_hash,
        "generation_state_receipt": row.generation_state_receipt,
        "version": row.version,
        "updated_at": _iso(row.updated_at),
    }


def _pointer_reference(row: TranslationEmbeddingActive) -> dict[str, Any]:
    """Return the bounded authenticated identity needed for historical search."""

    return {
        "owner_user_id": row.owner_user_id,
        "project_id": row.project_id,
        "source_plan_id": row.source_plan_id,
        "projection_kind": row.projection_kind,
        "generation_id": row.generation_id,
        "generation_record_hash": row.generation_record_hash,
        "generation_state_receipt": row.generation_state_receipt,
        "version": int(row.version),
        "updated_at": _iso(row.updated_at),
        "pointer_receipt": row.pointer_receipt,
    }


def _verify_pointer_reference(
    reference: Mapping[str, Any],
    *,
    generation: TranslationEmbeddingGeneration,
    session: Any,
    require_current: bool = False,
) -> dict[str, Any]:
    required = {
        "owner_user_id", "project_id", "source_plan_id", "projection_kind",
        "generation_id", "generation_record_hash", "generation_state_receipt",
        "version", "updated_at", "pointer_receipt",
    }
    if set(reference) != required:
        raise EmbeddingProjectionError("embedding-pointer-reference")
    if (
        reference["owner_user_id"] != generation.owner_user_id
        or reference["project_id"] != generation.project_id
        or reference["source_plan_id"] != generation.source_plan_id
        or reference["projection_kind"] != generation.projection_kind
        or reference["generation_id"] != generation.id
        or reference["generation_record_hash"] != generation.record_hash
        or not isinstance(reference["version"], int)
        or isinstance(reference["version"], bool)
        or reference["version"] < 1
        or not isinstance(reference["updated_at"], str)
        or not isinstance(reference["generation_state_receipt"], str)
        or len(reference["generation_state_receipt"]) != 64
        or not isinstance(reference["pointer_receipt"], str)
        or len(reference["pointer_receipt"]) != 64
    ):
        raise EmbeddingProjectionError("embedding-pointer-reference")
    pointer_material = {
        "contract_version": EMBEDDING_CONTRACT_VERSION,
        "owner_user_id": reference["owner_user_id"],
        "project_id": reference["project_id"],
        "source_plan_id": reference["source_plan_id"],
        "projection_kind": reference["projection_kind"],
        "generation_id": reference["generation_id"],
        "generation_record_hash": reference["generation_record_hash"],
        "generation_state_receipt": reference["generation_state_receipt"],
        "version": reference["version"],
        "updated_at": reference["updated_at"],
    }
    if reference["pointer_receipt"] not in private_witness_candidates(
        pointer_material, purpose=POINTER_PURPOSE
    ):
        raise EmbeddingProjectionError("embedding-pointer-reference")
    if require_current:
        pointer = session.get(
            TranslationEmbeddingActive,
            {
                "owner_user_id": generation.owner_user_id,
                "project_id": generation.project_id,
                "source_plan_id": generation.source_plan_id,
                "projection_kind": generation.projection_kind,
            },
            populate_existing=True,
        )
        if pointer is None or _pointer_reference(pointer) != dict(reference):
            raise EmbeddingProjectionError("embedding-pointer-drift")
        _verify_active_pointer(pointer, session=session)
    return dict(reference)


def _subject_receipt(
    *,
    kind: str,
    subject_id: str,
    source_plan_id: str,
    source_plan_hash: str,
    source_witness: str,
) -> str:
    return private_witness(
        {
            "contract_version": EMBEDDING_CONTRACT_VERSION,
            "kind": kind,
            "subject_id": subject_id,
            "source_plan_id": source_plan_id,
            "source_plan_hash": source_plan_hash,
            "source_witness": source_witness,
        },
        purpose=SUBJECT_PURPOSE,
    )


def _subjects(
    record: TranslationSourcePlan,
    *,
    kind: str,
    plan: Any,
    project: TranslationProject,
    owner_user_id: int,
    session: Any,
) -> list[tuple[str, str]]:
    if kind == "block":
        return [(block.id, str(block.projection)) for block in plan.blocks]
    if kind == "anchor":
        return [
            (anchor.id, str(block.projection[anchor.start : anchor.end]))
            for block in plan.blocks
            for anchor in block.anchors
        ]
    if kind != "memory":
        raise EmbeddingProjectionError("embedding-kind")
    rows = (
        session.query(TranslationMemoryEntry)
        .filter_by(
            owner_user_id=owner_user_id,
            project_id=project.id,
            source_plan_id=record.id,
        )
        .order_by(TranslationMemoryEntry.created_at, TranslationMemoryEntry.id)
        .all()
    )
    result: list[tuple[str, str]] = []
    for row in rows:
        evidence = rehydrate_translation_memory_entry(row, record, session=session)
        result.append((row.id, str(evidence["source_text"])))
    return result


def _verify_live_model(
    row: TranslationEmbeddingGeneration, *, owner_user_id: int, session: Any
) -> LLMModel:
    model = session.get(LLMModel, row.model_record_id)
    provider = session.get(LLMProvider, row.provider_id)
    if (
        model is None
        or provider is None
        or model.provider_id != provider.id
        or model.model_id != row.model_id
        or model.provider_id != row.provider_id
        or provider.name != row.provider_name
        or provider.provider_kind != row.provider_kind
        or model.user_id != row.model_owner_user_id
        or model.is_active is not True
        or model.model_type != "embedding"
        or (model.user_id is not None and model.user_id != owner_user_id)
        or (provider.user_id is not None and provider.user_id != owner_user_id)
        or _model_revision(model) != row.model_revision
        or _iso(model.updated_at) != _iso(row.model_updated_at)
    ):
        raise EmbeddingProjectionError("embedding-model-drift")
    native = int(model.default_dimensions or 0)
    if (
        row.dimensions <= 0
        or native <= 0
        or (not model.supports_mrl and row.dimensions != native)
        or (model.supports_mrl and row.dimensions > native)
    ):
        raise EmbeddingProjectionError("embedding-model-drift")
    return model


def _verify_live_credential(
    row: TranslationEmbeddingGeneration,
    *,
    model: LLMModel,
    owner_user_id: int,
    session: Any,
) -> ApiKey:
    key = _resolve_credential(
        model=model,
        owner_user_id=owner_user_id,
        session=session,
        api_key_id=row.api_key_id,
    )
    provider = session.get(LLMProvider, model.provider_id)
    if provider is None or int(key.id) != int(row.api_key_id):
        raise EmbeddingProjectionError("embedding-credential-drift")
    if key.user_id != row.api_key_owner_user_id or _iso(key.created_at) != _iso(
        row.api_key_created_at
    ):
        raise EmbeddingProjectionError("embedding-credential-drift")
    candidates = private_witness_candidates(
        {
            "id": int(key.id),
            "owner_user_id": key.user_id,
            "provider_id": int(key.provider_id),
            "provider_name": provider.name,
            "created_at": _iso(key.created_at),
            "api_key": key.api_key,
            "api_base": key.api_base,
            "api_version": key.api_version,
            "is_active": bool(key.is_active),
        },
        purpose="translator-embedding-credential-v1",
    )
    if row.credential_witness not in candidates:
        raise EmbeddingProjectionError("embedding-credential-drift")
    return key


def _verify_generation(
    row: TranslationEmbeddingGeneration,
    *,
    session: Any,
    owner_user_id: int | None = None,
    project_id: str | None = None,
    source_plan_id: str | None = None,
    kind: str | None = None,
    verify_entries: bool = True,
) -> tuple[Any, TranslationProject, int, list[TranslationEmbeddingEntry]]:
    if not isinstance(row, TranslationEmbeddingGeneration) or not row.id:
        raise EmbeddingProjectionError("embedding-generation-record")
    if not _NAMESPACE_RE.fullmatch(str(row.namespace or "")):
        raise EmbeddingProjectionError("embedding-generation-namespace")
    if row.projection_kind not in PROJECTION_KINDS:
        raise EmbeddingProjectionError("embedding-generation-kind")
    if owner_user_id is not None and row.owner_user_id != owner_user_id:
        raise EmbeddingProjectionError("embedding-generation-owner")
    if project_id is not None and row.project_id != project_id:
        raise EmbeddingProjectionError("embedding-generation-project")
    if source_plan_id is not None and row.source_plan_id != source_plan_id:
        raise EmbeddingProjectionError("embedding-generation-plan")
    if kind is not None and row.projection_kind != kind:
        raise EmbeddingProjectionError("embedding-generation-kind")
    project = session.get(TranslationProject, row.project_id)
    record = session.get(TranslationSourcePlan, row.source_plan_id)
    if (
        project is None
        or record is None
        or project.user_id != row.owner_user_id
        or record.project_id != row.project_id
    ):
        raise EmbeddingProjectionError("embedding-generation-scope")
    plan = rehydrate_source_plan(record)
    if plan.plan_hash != row.source_plan_hash:
        raise EmbeddingProjectionError("embedding-generation-plan-hash")
    if row.signature_hash != _hash_json(row.signature_json):
        raise EmbeddingProjectionError("embedding-generation-signature")
    _verify_lifecycle(row)
    if row.state_receipt not in private_witness_candidates(
        _state_values(row), purpose="translator-embedding-state-v1"
    ):
        raise EmbeddingProjectionError("embedding-generation-state-receipt")
    if row.record_hash not in private_witness_candidates(
        _generation_values(row), purpose=GENERATION_PURPOSE
    ):
        raise EmbeddingProjectionError("embedding-generation-receipt")
    entries = list(
        session.query(TranslationEmbeddingEntry)
        .filter_by(generation_id=row.id)
        .order_by(TranslationEmbeddingEntry.ordinal)
        .all()
    )
    if len(entries) != int(row.entry_count):
        raise EmbeddingProjectionError("embedding-generation-cardinality")
    if verify_entries:
        for entry in entries:
            _verify_entry(
                entry,
                generation=row,
                plan=plan,
                record=record,
                project=project,
                session=session,
            )
    return plan, project, int(project.user_id), entries


def _verify_entry(
    row: TranslationEmbeddingEntry,
    *,
    row_generation: TranslationEmbeddingGeneration | None = None,
    generation: TranslationEmbeddingGeneration | None = None,
    plan: Any,
    record: TranslationSourcePlan,
    project: TranslationProject,
    session: Any,
) -> str:
    generation = generation or row_generation
    if generation is None:
        generation = session.get(TranslationEmbeddingGeneration, row.generation_id)
    if generation is None or row.generation_id != generation.id:
        raise EmbeddingProjectionError("embedding-entry-generation")
    if (
        row.owner_user_id != generation.owner_user_id
        or row.project_id != generation.project_id == project.id
        or row.source_plan_id != generation.source_plan_id == record.id
        or row.subject_type != generation.projection_kind
        or row.ordinal < 0
        or row.vector_row_id <= 0
    ):
        raise EmbeddingProjectionError("embedding-entry-scope")
    expected_text: str | None = None
    if row.subject_type == "block":
        item = next((item for item in plan.blocks if item.id == row.subject_id), None)
        if item is not None:
            expected_text = str(item.projection)
    elif row.subject_type == "anchor":
        item = next(
            (
                anchor
                for block in plan.blocks
                for anchor in block.anchors
                if anchor.id == row.subject_id
            ),
            None,
        )
        if item is not None:
            block = next(
                (
                    block
                    for block in plan.blocks
                    if any(anchor.id == row.subject_id for anchor in block.anchors)
                ),
                None,
            )
            if block is not None:
                anchor = next(
                    anchor for anchor in block.anchors if anchor.id == row.subject_id
                )
                expected_text = str(block.projection[anchor.start : anchor.end])
    elif row.subject_type == "memory":
        memory = session.get(TranslationMemoryEntry, row.subject_id)
        if memory is not None:
            evidence = rehydrate_translation_memory_entry(
                memory, record, session=session
            )
            expected_text = str(evidence["source_text"])
    if expected_text is None or row.source_text != expected_text:
        raise EmbeddingProjectionError("embedding-entry-subject")
    witness_candidates = private_witness_candidates(
        expected_text, purpose=SOURCE_PURPOSE
    )
    if row.source_witness_hash not in witness_candidates:
        raise EmbeddingProjectionError("embedding-entry-source-witness")
    expected_subject = _subject_receipt(
        kind=row.subject_type,
        subject_id=row.subject_id,
        source_plan_id=record.id,
        source_plan_hash=record.plan_hash,
        source_witness=row.source_witness_hash,
    )
    if row.subject_receipt != expected_subject:
        raise EmbeddingProjectionError("embedding-entry-subject-receipt")
    if row.record_hash not in private_witness_candidates(
        _entry_values(row), purpose=ENTRY_PURPOSE
    ):
        raise EmbeddingProjectionError("embedding-entry-receipt")
    return expected_text


def prepare_embedding_generation(
    source_plan: TranslationSourcePlan,
    *,
    projection_kind: str,
    model_id: str | None = None,
    dimensions: int | None = None,
    task_type: str = "similarity",
    session: Any | None = None,
) -> TranslationEmbeddingGeneration:
    """Prepare an immutable generation and all encrypted subject rows.

    The function only flushes the caller's session.  It never commits a caller
    transaction, so a dirty unrelated UoW can be rolled back independently.
    """

    active_session = _session(session)
    if projection_kind not in PROJECTION_KINDS:
        raise EmbeddingProjectionError("embedding-kind")
    task = str(task_type or "similarity").strip().lower()
    if not task or len(task) > 64:
        raise EmbeddingProjectionError("embedding-task")
    plan, project, owner_user_id = _require_plan(source_plan, session=active_session)
    model, selected_dimensions = _resolve_model(
        owner_user_id=owner_user_id,
        model_id=model_id,
        dimensions=dimensions,
        session=active_session,
    )
    credential = _resolve_credential(
        model=model, owner_user_id=owner_user_id, session=active_session
    )
    provider = active_session.get(LLMProvider, model.provider_id)
    if provider is None:
        raise EmbeddingProjectionError("embedding-provider-unavailable")
    profile, base_signature = _profile_snapshot(
        model, task_type=task, dimensions=selected_dimensions
    )
    subjects = _subjects(
        source_plan,
        kind=projection_kind,
        plan=plan,
        project=project,
        owner_user_id=owner_user_id,
        session=active_session,
    )
    if not subjects:
        raise EmbeddingProjectionError("embedding-empty-generation")
    signature = {
        **base_signature,
        "projection_kind": projection_kind,
        "owner_user_id": owner_user_id,
        "project_id": project.id,
        "source_plan_id": source_plan.id,
        "source_plan_hash": plan.plan_hash,
        "api_key_id": int(credential.id),
        "api_key_owner_user_id": credential.user_id,
        "api_key_created_at": _iso(credential.created_at),
        "credential_witness": _credential_witness(credential, provider=provider),
    }
    signature_hash = _hash_json(signature)
    generation_id = str(uuid.uuid4())
    namespace = f"translator_emb_{uuid.uuid4().hex}"
    now = utc_now_db()
    generation = cast(Any, TranslationEmbeddingGeneration)(
        id=generation_id,
        owner_user_id=owner_user_id,
        project_id=project.id,
        source_plan_id=source_plan.id,
        source_plan_hash=plan.plan_hash,
        projection_kind=projection_kind,
        model_record_id=model.id,
        model_owner_user_id=model.user_id,
        provider_id=model.provider_id,
        provider_name=provider.name,
        provider_kind=provider.provider_kind,
        api_key_id=credential.id,
        api_key_owner_user_id=credential.user_id,
        api_key_created_at=credential.created_at,
        credential_witness=_credential_witness(credential, provider=provider),
        model_id=model.model_id,
        model_revision=_model_revision(model),
        model_updated_at=model.updated_at,
        dimensions=selected_dimensions,
        task_type=task,
        normalization=str(signature["normalization"]),
        distance_metric="cosine_equivalent_l2",
        prefix_style=model.prefix_style or "none",
        query_prefix=model.custom_query_prefix,
        passage_prefix=model.custom_passage_prefix,
        extractor_version=EXTRACTOR_VERSION,
        profile_json=profile,
        signature_json=signature,
        signature_hash=signature_hash,
        namespace=namespace,
        status="building",
        entry_count=0,
        record_hash=_ZERO_WITNESS,
        state_version=1,
        state_receipt=_ZERO_WITNESS,
        vector_witness=_ZERO_WITNESS,
        created_at=now,
        prepared_at=now,
    )
    active = (
        active_session.query(TranslationEmbeddingActive)
        .filter_by(
            owner_user_id=owner_user_id,
            project_id=project.id,
            source_plan_id=source_plan.id,
            projection_kind=projection_kind,
        )
        .one_or_none()
    )
    if active is not None:
        _verify_active_pointer(active, session=active_session)
        generation.previous_generation_id = active.generation_id
    active_session.add(generation)
    active_session.flush()
    for ordinal, (subject_id, source_text) in enumerate(subjects):
        if not isinstance(source_text, str) or not source_text:
            raise EmbeddingProjectionError("embedding-source-empty")
        source_witness = private_witness(source_text, purpose=SOURCE_PURPOSE)
        entry = cast(Any, TranslationEmbeddingEntry)(
            id=str(uuid.uuid4()),
            generation_id=generation.id,
            owner_user_id=owner_user_id,
            project_id=project.id,
            source_plan_id=source_plan.id,
            subject_type=projection_kind,
            subject_id=subject_id,
            ordinal=ordinal,
            vector_row_id=ordinal + 1,
            source_text=source_text,
            source_witness_hash=source_witness,
            subject_receipt=_subject_receipt(
                kind=projection_kind,
                subject_id=subject_id,
                source_plan_id=source_plan.id,
                source_plan_hash=plan.plan_hash,
                source_witness=source_witness,
            ),
            record_hash="0" * 64,
            created_at=now,
        )
        entry.record_hash = private_witness(_entry_values(entry), purpose=ENTRY_PURPOSE)
        active_session.add(entry)
    generation.entry_count = len(subjects)
    generation.record_hash = private_witness(
        _generation_values(generation), purpose=GENERATION_PURPOSE
    )
    _refresh_state_receipt(generation)
    active_session.flush()
    return generation


def _backend_call(backend: Any, method: str, *args: Any, **kwargs: Any) -> Any:
    function = getattr(backend, method, None)
    if not callable(function):
        raise EmbeddingBackendError(f"embedding-backend-{method}")
    try:
        return function(*args, **kwargs)
    except EmbeddingBackendError:
        raise
    except Exception as exc:
        raise EmbeddingBackendError(f"embedding-backend-{method}") from exc


def _durability_session(session: Any) -> Any:
    bind = session.get_bind()
    return sessionmaker(bind=bind, expire_on_commit=False, future=True)()


def _prove_durable_prepared_generation(
    generation: TranslationEmbeddingGeneration, *, session: Any
) -> None:
    """Prove preparation is visible from a separate committed read session."""
    reader = _durability_session(session)
    try:
        visible = reader.get(
            TranslationEmbeddingGeneration,
            generation.id,
            populate_existing=True,
        )
        if visible is None or visible.status != "building":
            raise EmbeddingProjectionError("embedding-generation-not-durable")
        _verify_generation(visible, session=reader, verify_entries=True)
    except EmbeddingProjectionError:
        raise
    except Exception as exc:
        raise EmbeddingProjectionError("embedding-generation-durability") from exc
    finally:
        reader.close()


def _backend_vector_witness(
    backend: Any, *, namespace: str, row_ids: list[int], dimensions: int
) -> str:
    value = _backend_call(
        backend,
        "vector_witness",
        namespace=namespace,
        row_ids=sorted(int(value) for value in row_ids),
        dimensions=dimensions,
    )
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise EmbeddingBackendError("embedding-vector-witness")
    return value


def _embed_documents(
    backend: Any,
    texts: list[str],
    *,
    generation: TranslationEmbeddingGeneration,
) -> list[list[float]]:
    profile_values = (
        generation.profile_json if isinstance(generation.profile_json, Mapping) else {}
    )
    batch_size = profile_values.get("max_document_batch_size")
    if batch_size is None:
        batch_size = getattr(backend, "max_document_batch_size", None)
    try:
        batch_size = int(batch_size) if batch_size else len(texts) or 1
    except (TypeError, ValueError, OverflowError) as exc:
        raise EmbeddingBackendError("embedding-batch-size") from exc
    if batch_size <= 0:
        raise EmbeddingBackendError("embedding-batch-size")
    all_vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        vectors = _backend_call(
            backend, "embed_documents", batch, generation=generation
        )
        try:
            all_vectors.extend(
                _validate_vectors(
                    vectors, expected_count=len(batch), dimensions=generation.dimensions
                )
            )
        except EmbeddingProjectionError as exc:
            raise EmbeddingBackendError(str(exc)) from exc
    if len(all_vectors) != len(texts):
        raise EmbeddingBackendError("embedding-cardinality")
    return all_vectors


def _validate_vectors(
    vectors: Any, *, expected_count: int, dimensions: int
) -> list[list[float]]:
    if not isinstance(vectors, Sequence) or isinstance(vectors, (str, bytes)):
        raise EmbeddingProjectionError("embedding-cardinality")
    if len(vectors) != expected_count:
        raise EmbeddingProjectionError("embedding-cardinality")
    normalized: list[list[float]] = []
    for vector in vectors:
        if (
            not isinstance(vector, Sequence)
            or isinstance(vector, (str, bytes))
            or len(vector) != dimensions
        ):
            raise EmbeddingProjectionError("embedding-dimension")
        values: list[float] = []
        for item in vector:
            values.append(_canonical_float32(item))
        normalized.append(values)
    return normalized


def _canonical_float32(value: Any) -> float:
    """Return the exact finite value that the dense backend will store.

    Python's float is normally binary64; checking it before sqlite-vec's
    float32 serialization lets values such as 1e40 pass and overflow later.
    Packing first makes overflow and non-finite results a contract error before
    any NumPy warning or backend write can occur.
    """

    try:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("non-finite embedding value")
        packed = struct.pack("!f", number)
        canonical = struct.unpack("!f", packed)[0]
    except (TypeError, ValueError, OverflowError, struct.error) as exc:
        raise EmbeddingProjectionError("embedding-nonfinite") from exc
    if not math.isfinite(canonical):
        raise EmbeddingProjectionError("embedding-nonfinite")
    return canonical


def _mark_failed(
    generation: TranslationEmbeddingGeneration, *, session: Any, exc: BaseException
) -> TranslationEmbeddingGeneration:
    generation.status = "failed"
    generation.vector_witness = _ZERO_WITNESS
    generation.error_json = {
        "contract_version": EMBEDDING_CONTRACT_VERSION,
        "code": _bounded_error_code(exc),
    }
    generation.completed_at = utc_now_db()
    generation.state_version = int(generation.state_version or 0) + 1
    _refresh_state_receipt(generation)
    session.flush()
    return generation


def build_embedding_generation(
    generation: TranslationEmbeddingGeneration | str,
    *,
    backend: EmbeddingBackend,
    session: Any | None = None,
) -> TranslationEmbeddingGeneration:
    """Build a prepared generation and mark it ready only after exact checks."""

    active_session = _session(session)
    row = _generation_for_session(generation, session=active_session)
    # These checks are intentionally outside the backend/provider failure
    # boundary.  An uncommitted or tampered relational candidate must never be
    # rewritten as an ordinary failed provider build.
    _prove_durable_prepared_generation(row, session=active_session)
    _plan, _project, owner_user_id, entries = _verify_generation(
        row, session=active_session, verify_entries=True
    )
    if row.status != "building":
        raise EmbeddingProjectionError("embedding-generation-state")
    model = _verify_live_model(row, owner_user_id=owner_user_id, session=active_session)
    _verify_live_credential(
        row, model=model, owner_user_id=owner_user_id, session=active_session
    )
    row_ids = [entry.vector_row_id for entry in entries]
    texts = [entry.source_text for entry in entries]
    if hasattr(backend, "__dict__"):
        backend.__dict__["namespace"] = row.namespace
    try:
        _backend_call(
            backend, "create", namespace=row.namespace, dimensions=row.dimensions
        )
    except EmbeddingBackendError as exc:
        try:
            _backend_call(
                backend, "drop", namespace=row.namespace, dimensions=row.dimensions
            )
        except EmbeddingBackendError:
            pass
        return _mark_failed(row, session=active_session, exc=exc)

    # Revalidate the exact model/key after backend setup and immediately before
    # provider I/O.  A concurrent credential switch is a relational fault.
    try:
        model = _verify_live_model(
            row, owner_user_id=owner_user_id, session=active_session
        )
        _verify_live_credential(
            row, model=model, owner_user_id=owner_user_id, session=active_session
        )
    except EmbeddingProjectionError:
        try:
            _backend_call(
                backend, "drop", namespace=row.namespace, dimensions=row.dimensions
            )
        except EmbeddingBackendError:
            pass
        raise

    try:
        vectors = _embed_documents(backend, texts, generation=row)
        _backend_call(
            backend,
            "upsert",
            row_ids,
            vectors,
            include_lexical_text=False,
        )
        actual_ids = {
            int(value)
            for value in _backend_call(backend, "row_ids", namespace=row.namespace)
        }
        if actual_ids != set(row_ids):
            raise EmbeddingBackendError("embedding-row-membership")
        witness = _backend_vector_witness(
            backend, namespace=row.namespace, row_ids=row_ids, dimensions=row.dimensions
        )
    except EmbeddingBackendError as exc:
        try:
            _backend_call(
                backend, "drop", namespace=row.namespace, dimensions=row.dimensions
            )
        except EmbeddingBackendError:
            pass
        return _mark_failed(row, session=active_session, exc=exc)

    row.vector_witness = witness
    row.status = "ready"
    row.completed_at = utc_now_db()
    row.error_json = None
    row.health_json = {
        "contract_version": EMBEDDING_CONTRACT_VERSION,
        "vector_count": len(row_ids),
        "dense_only": True,
        "vector_witness": witness,
    }
    row.state_version = int(row.state_version or 0) + 1
    _refresh_state_receipt(row)
    active_session.flush()
    return row


def _verify_active_pointer(
    row: TranslationEmbeddingActive, *, session: Any
) -> TranslationEmbeddingGeneration:
    generation = session.get(
        TranslationEmbeddingGeneration, row.generation_id, populate_existing=True
    )
    if generation is None or row.version is None or int(row.version) < 1:
        raise EmbeddingProjectionError("embedding-active-pointer")
    if (
        row.owner_user_id != generation.owner_user_id
        or row.project_id != generation.project_id
        or row.source_plan_id != generation.source_plan_id
        or row.projection_kind != generation.projection_kind
        or row.generation_record_hash != generation.record_hash
        or row.generation_state_receipt != generation.state_receipt
        or generation.status != "active"
        or int(generation.entry_count) <= 0
        or row.pointer_receipt
        not in private_witness_candidates(_pointer_values(row), purpose=POINTER_PURPOSE)
    ):
        raise EmbeddingProjectionError("embedding-active-pointer")
    _verify_generation(
        generation,
        session=session,
        owner_user_id=row.owner_user_id,
        project_id=row.project_id,
        source_plan_id=row.source_plan_id,
        kind=row.projection_kind,
        verify_entries=True,
    )
    return generation


def activate_embedding_generation(
    generation: TranslationEmbeddingGeneration | str,
    *,
    expected_active_generation_id: str | None,
    backend: EmbeddingBackend,
    session: Any | None = None,
    expected_active_pointer_version: int | None = None,
    expected_active_version: int | None = None,
) -> TranslationEmbeddingGeneration:
    """CAS-activate a verified ready generation without committing the UoW."""

    active_session = _session(session)
    row = _generation_for_session(generation, session=active_session)
    _plan, project, owner_user_id, entries = _verify_generation(
        row, session=active_session
    )
    if int(row.entry_count) <= 0:
        raise EmbeddingProjectionError("embedding-empty-generation")
    current = (
        active_session.query(TranslationEmbeddingActive)
        .filter_by(
            owner_user_id=owner_user_id,
            project_id=project.id,
            source_plan_id=row.source_plan_id,
            projection_kind=row.projection_kind,
        )
        .populate_existing()
        .one_or_none()
    )
    current_id = current.generation_id if current is not None else None
    if current is not None:
        _verify_active_pointer(current, session=active_session)
    if current_id != expected_active_generation_id:
        raise EmbeddingActivationConflict("embedding-active-generation-conflict")
    expected_version = (
        expected_active_pointer_version
        if expected_active_pointer_version is not None
        else expected_active_version
    )
    current_version = int(current.version) if current is not None else 0
    if current is None:
        if expected_version not in (None, 0):
            raise EmbeddingActivationConflict("embedding-active-version-conflict")
    elif expected_version is not None and current_version != int(expected_version):
        raise EmbeddingActivationConflict("embedding-active-version-conflict")
    if row.previous_generation_id != current_id:
        raise EmbeddingActivationConflict("embedding-previous-generation-conflict")
    if row.status != "ready":
        raise EmbeddingProjectionError("embedding-generation-not-ready")
    model = _verify_live_model(row, owner_user_id=owner_user_id, session=active_session)
    _verify_live_credential(
        row, model=model, owner_user_id=owner_user_id, session=active_session
    )
    try:
        actual_ids = {
            int(value)
            for value in _backend_call(backend, "row_ids", namespace=row.namespace)
        }
        expected_ids = {entry.vector_row_id for entry in entries}
        if actual_ids != expected_ids:
            raise EmbeddingProjectionError("embedding-row-membership")
        witness = _backend_vector_witness(
            backend,
            namespace=row.namespace,
            row_ids=[entry.vector_row_id for entry in entries],
            dimensions=row.dimensions,
        )
    except EmbeddingBackendError as exc:
        raise EmbeddingProjectionError("embedding-vector-unavailable") from exc
    if witness != row.vector_witness:
        raise EmbeddingProjectionError("embedding-vector-witness")
    now = utc_now_db()
    try:
        # Lifecycle mutations and the pointer fence share one savepoint.  If a
        # caller deliberately catches a CAS conflict, it must not be able to
        # commit a retired predecessor or an unpointed active candidate.
        with active_session.begin_nested():
            previous = None
            if current is not None:
                previous = active_session.get(
                    TranslationEmbeddingGeneration, current_id
                )
                if previous is None:
                    raise EmbeddingProjectionError(
                        "embedding-previous-generation-missing"
                    )
                _verify_generation(
                    previous, session=active_session, verify_entries=False
                )
                previous.status = "retired"
                previous.retired_at = now
                previous.state_version = int(previous.state_version or 0) + 1
                _refresh_state_receipt(previous)

            # Compute the future generation receipt before constructing the
            # pointer; it is valid only for this exact lifecycle state.
            row.status = "active"
            row.activated_at = now
            row.state_version = int(row.state_version or 0) + 1
            _refresh_state_receipt(row)
            if current is None:
                # The uniqueness constraint is the first-writer fence.
                candidate_pointer = cast(Any, TranslationEmbeddingActive)(
                    owner_user_id=owner_user_id,
                    project_id=project.id,
                    source_plan_id=row.source_plan_id,
                    projection_kind=row.projection_kind,
                    generation_id=row.id,
                    generation_record_hash=row.record_hash,
                    generation_state_receipt=row.state_receipt,
                    version=1,
                    updated_at=now,
                )
                candidate_pointer.pointer_receipt = private_witness(
                    _pointer_values(candidate_pointer), purpose=POINTER_PURPOSE
                )
                active_session.add(candidate_pointer)
                active_session.flush()
                current = candidate_pointer
            else:
                next_version = current_version + 1
                pointer_values = {
                    "generation_id": row.id,
                    "generation_record_hash": row.record_hash,
                    "generation_state_receipt": row.state_receipt,
                    "version": next_version,
                    "updated_at": now,
                }
                # The receipt covers the exact values written by the CAS.
                pointer_payload = {
                    "contract_version": EMBEDDING_CONTRACT_VERSION,
                    "owner_user_id": owner_user_id,
                    "project_id": project.id,
                    "source_plan_id": row.source_plan_id,
                    "projection_kind": row.projection_kind,
                    **pointer_values,
                    "updated_at": _iso(now),
                }
                pointer_receipt = private_witness(
                    pointer_payload, purpose=POINTER_PURPOSE
                )
                result = active_session.execute(
                    update(TranslationEmbeddingActive)
                    .where(
                        TranslationEmbeddingActive.owner_user_id == owner_user_id,
                        TranslationEmbeddingActive.project_id == project.id,
                        TranslationEmbeddingActive.source_plan_id == row.source_plan_id,
                        TranslationEmbeddingActive.projection_kind
                        == row.projection_kind,
                        TranslationEmbeddingActive.generation_id == current_id,
                        TranslationEmbeddingActive.version == current_version,
                    )
                    .values(**pointer_values, pointer_receipt=pointer_receipt)
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    raise EmbeddingActivationConflict("embedding-active-cas-conflict")
                active_session.expire(current)
            active_session.flush()
    except IntegrityError as exc:
        raise EmbeddingActivationConflict("embedding-active-insert-conflict") from exc
    active_session.flush()
    return row


def active_embedding_generation(
    *,
    owner_user_id: int,
    project_id: str,
    source_plan_id: str,
    projection_kind: str,
    session: Any | None = None,
) -> TranslationEmbeddingGeneration | None:
    active_session = _session(session)
    pointer = (
        active_session.query(TranslationEmbeddingActive)
        .filter_by(
            owner_user_id=owner_user_id,
            project_id=project_id,
            source_plan_id=source_plan_id,
            projection_kind=projection_kind,
        )
        .one_or_none()
    )
    if pointer is None:
        return None
    return _verify_active_pointer(pointer, session=active_session)


def verified_active_embedding_reference(
    *,
    owner_user_id: int,
    project_id: str,
    source_plan_id: str,
    projection_kind: str,
    session: Any | None = None,
) -> tuple[TranslationEmbeddingGeneration, dict[str, Any]]:
    """Return the authenticated active generation and its immutable pointer proof."""

    active_session = _session(session)
    pointer = active_session.get(
        TranslationEmbeddingActive,
        {
            "owner_user_id": owner_user_id,
            "project_id": project_id,
            "source_plan_id": source_plan_id,
            "projection_kind": projection_kind,
        },
        populate_existing=True,
    )
    if pointer is None:
        raise EmbeddingProjectionError("embedding-unavailable")
    generation = _verify_active_pointer(pointer, session=active_session)
    return generation, _pointer_reference(pointer)


def verify_pinned_embedding_reference(
    reference: Mapping[str, Any],
    *,
    session: Any | None = None,
) -> tuple[TranslationEmbeddingGeneration, dict[str, Any]]:
    """Verify a historical generation/pointer proof without requiring it current."""

    active_session = _session(session)
    if not isinstance(reference, Mapping):
        raise EmbeddingProjectionError("embedding-pointer-reference")
    generation_id = reference.get("generation_id")
    if not isinstance(generation_id, str) or not generation_id:
        raise EmbeddingProjectionError("embedding-generation-record")
    generation = active_session.get(
        TranslationEmbeddingGeneration, generation_id, populate_existing=True
    )
    if generation is None:
        raise EmbeddingProjectionError("embedding-generation-missing")
    _verify_generation(
        generation,
        session=active_session,
        owner_user_id=reference.get("owner_user_id"),
        project_id=reference.get("project_id"),
        source_plan_id=reference.get("source_plan_id"),
        kind=reference.get("projection_kind"),
        verify_entries=True,
    )
    if generation.status not in {"active", "retired"}:
        raise EmbeddingProjectionError("embedding-generation-not-searchable")
    return generation, _verify_pointer_reference(
        reference, generation=generation, session=active_session
    )


@dataclass(frozen=True, slots=True)
class EmbeddingSearchResponse:
    """Content-bearing retrieval result with explicit degraded state."""

    status: str
    results: tuple[dict[str, Any], ...] = ()
    reason: str | None = None
    metadata: Mapping[str, Any] | None = None

    @property
    def degraded(self) -> bool:
        return self.status != "ready"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "degraded": self.degraded,
            "reason": self.reason,
            "results": list(self.results),
            "metadata": dict(self.metadata or {}),
        }

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]


def search_embedding_generation(
    *,
    owner_user_id: int,
    project_id: str,
    source_plan_id: str,
    projection_kind: str,
    query_text: str,
    backend: EmbeddingBackend,
    top_k: int = 5,
    classification: str | None = None,
    session: Any | None = None,
) -> EmbeddingSearchResponse:
    """Search only relationally verified rows, degrading on vector failures."""

    active_session = _session(session)
    row = active_embedding_generation(
        owner_user_id=owner_user_id,
        project_id=project_id,
        source_plan_id=source_plan_id,
        projection_kind=projection_kind,
        session=active_session,
    )
    if row is None:
        return EmbeddingSearchResponse("degraded", reason="embedding-unavailable")
    pointer = (
        active_session.query(TranslationEmbeddingActive)
        .filter_by(
            owner_user_id=owner_user_id,
            project_id=project_id,
            source_plan_id=source_plan_id,
            projection_kind=projection_kind,
        )
        .one_or_none()
    )
    if pointer is None:
        raise EmbeddingProjectionError("embedding-active-pointer")
    metadata: dict[str, Any] = {
        "generation_id": row.id,
        "generation_record_hash": row.record_hash,
        "generation_state": row.status,
        "generation_state_receipt": row.state_receipt,
        "pointer": _pointer_reference(pointer),
        "query_hash": hashlib.sha256(query_text.encode("utf-8")).hexdigest(),
        "top_k": top_k,
    }
    plan, project, owner, entries = _verify_generation(
        row,
        session=active_session,
        owner_user_id=owner_user_id,
        project_id=project_id,
        source_plan_id=source_plan_id,
        kind=projection_kind,
    )
    model = _verify_live_model(row, owner_user_id=owner, session=active_session)
    _verify_live_credential(
        row, model=model, owner_user_id=owner, session=active_session
    )
    if classification is not None and (
        projection_kind != "memory"
        or not isinstance(classification, str)
        or not classification.strip()
    ):
        raise EmbeddingProjectionError("embedding-classification")

    all_entries = {entry.vector_row_id: entry for entry in entries}
    try:
        stored_ids = {
            int(value)
            for value in _backend_call(backend, "row_ids", namespace=row.namespace)
        }
        expected_ids = set(all_entries)
        if stored_ids != expected_ids:
            return EmbeddingSearchResponse(
                "degraded", reason="embedding-row-membership", metadata=metadata
            )
        witness = _backend_vector_witness(
            backend,
            namespace=row.namespace,
            row_ids=sorted(expected_ids),
            dimensions=row.dimensions,
        )
        if witness != row.vector_witness:
            return EmbeddingSearchResponse(
                "degraded", reason="embedding-vector-witness", metadata=metadata
            )
    except EmbeddingBackendError as exc:
        return EmbeddingSearchResponse(
            "degraded", reason=_bounded_error_code(exc), metadata=metadata
        )

    allow = all_entries
    if classification is not None:
        allow = {}
        for entry in entries:
            memory = active_session.get(TranslationMemoryEntry, entry.subject_id)
            if memory is None:
                raise EmbeddingProjectionError("embedding-memory-missing")
            # _verify_entry already authenticated the memory receipt; this
            # lookup only applies the relational classification allow-list.
            if memory.classification == classification:
                allow[entry.vector_row_id] = entry
    if not allow:
        metadata["selected_entry_record_hashes"] = []
        return EmbeddingSearchResponse("ready", (), metadata=metadata)

    try:
        returned = _backend_call(
            backend,
            "search_semantic",
            query_text,
            namespace=row.namespace,
            dimensions=row.dimensions,
            model=row.model_id,
            task_type=row.task_type,
            top_k=max(1, int(top_k or 5)),
            allowed_row_ids=sorted(allow),
        )
        results: list[dict[str, Any]] = []
        for candidate in returned or ():
            if isinstance(candidate, Mapping):
                vector_row_id = candidate.get("row_id")
                score = candidate.get("score", 0.0)
                distance = candidate.get("distance", 0.0)
            else:
                try:
                    vector_row_id, score, distance = candidate
                except (TypeError, ValueError) as exc:
                    raise EmbeddingBackendError("embedding-search-result") from exc
            try:
                vector_row_id = int(cast(Any, vector_row_id))
                score = float(cast(Any, score))
                distance = float(cast(Any, distance))
            except (TypeError, ValueError, OverflowError) as exc:
                raise EmbeddingBackendError("embedding-search-result") from exc
            if not math.isfinite(score) or not math.isfinite(distance):
                raise EmbeddingBackendError("embedding-search-result")
            entry = allow.get(vector_row_id)
            if entry is None:
                raise EmbeddingBackendError("embedding-search-out-of-scope")
            text_value = _verify_entry(
                entry,
                generation=row,
                plan=plan,
                record=active_session.get(TranslationSourcePlan, row.source_plan_id),
                project=project,
                session=active_session,
            )
            results.append(
                {
                    "entry_id": entry.id,
                    "vector_row_id": entry.vector_row_id,
                    "subject_type": entry.subject_type,
                    "subject_id": entry.subject_id,
                    "source_text": text_value,
                    "score": score,
                    "distance": distance,
                }
            )
        metadata["selected_entry_record_hashes"] = [
            all_entries[item["vector_row_id"]].record_hash for item in results
        ]
        return EmbeddingSearchResponse("ready", tuple(results), metadata=metadata)
    except EmbeddingBackendError as exc:
        return EmbeddingSearchResponse(
            "degraded", reason=_bounded_error_code(exc), metadata=metadata
        )


def search_pinned_embedding_generation(
    *,
    reference: Mapping[str, Any],
    query_text: str,
    query_profile: Mapping[str, Any],
    backend: EmbeddingBackend,
    top_k: int = 5,
    classification: str = "contextual_precedent",
    session: Any | None = None,
) -> EmbeddingSearchResponse:
    """Search exactly the generation and pointer receipt named by a baseline."""

    active_session = _session(session)
    generation, pointer_reference = verify_pinned_embedding_reference(
        reference, session=active_session
    )
    if classification != "contextual_precedent":
        raise EmbeddingProjectionError("embedding-classification")
    if not isinstance(query_text, str) or not query_text.strip():
        raise EmbeddingProjectionError("embedding-query")
    if not isinstance(query_profile, Mapping):
        raise EmbeddingProjectionError("embedding-query-profile")
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 1:
        raise EmbeddingProjectionError("embedding-top-k")
    plan, project, owner, entries = _verify_generation(
        generation,
        session=active_session,
        owner_user_id=generation.owner_user_id,
        project_id=generation.project_id,
        source_plan_id=generation.source_plan_id,
        kind="memory",
    )
    if generation.status not in {"active", "retired"}:
        raise EmbeddingProjectionError("embedding-generation-not-searchable")
    model = _verify_live_model(
        generation, owner_user_id=owner, session=active_session
    )
    _verify_live_credential(
        generation, model=model, owner_user_id=owner, session=active_session
    )
    _verify_backend_identity(backend, generation)
    metadata: dict[str, Any] = {
        "generation_id": generation.id,
        "generation_record_hash": generation.record_hash,
        "generation_state": generation.status,
        "generation_state_receipt": generation.state_receipt,
        "signature_hash": generation.signature_hash,
        "model_record_id": generation.model_record_id,
        "model_id": generation.model_id,
        "provider_id": generation.provider_id,
        "pointer": dict(pointer_reference),
        "query_hash": hashlib.sha256(query_text.encode("utf-8")).hexdigest(),
        "query_profile_hash": _hash_json(query_profile),
        "top_k": top_k,
    }
    all_entries = {entry.vector_row_id: entry for entry in entries}
    allowed: dict[int, TranslationEmbeddingEntry] = {}
    for entry in entries:
        memory = active_session.get(TranslationMemoryEntry, entry.subject_id)
        if memory is None or memory.classification != classification:
            continue
        allowed[entry.vector_row_id] = entry
    if not allowed:
        metadata["selected_entry_record_hashes"] = []
        return EmbeddingSearchResponse("ready", (), metadata=metadata)
    try:
        stored_ids = {
            int(value)
            for value in _backend_call(
                backend, "row_ids", namespace=generation.namespace
            )
        }
        if stored_ids != set(all_entries):
            return EmbeddingSearchResponse(
                "degraded", reason="embedding-row-membership", metadata=metadata
            )
        witness = _backend_vector_witness(
            backend,
            namespace=generation.namespace,
            row_ids=sorted(all_entries),
            dimensions=generation.dimensions,
        )
        if witness != generation.vector_witness:
            return EmbeddingSearchResponse(
                "degraded", reason="embedding-vector-witness", metadata=metadata
            )
        returned = _backend_call(
            backend,
            "search_semantic",
            query_text,
            namespace=generation.namespace,
            dimensions=generation.dimensions,
            model=generation.model_id,
            task_type=generation.task_type,
            top_k=top_k,
            allowed_row_ids=sorted(allowed),
        )
        results: list[dict[str, Any]] = []
        for candidate in returned or ():
            if isinstance(candidate, Mapping):
                vector_row_id = candidate.get("row_id")
                score = candidate.get("score", 0.0)
                distance = candidate.get("distance", 0.0)
            else:
                try:
                    vector_row_id, score, distance = candidate
                except (TypeError, ValueError) as exc:
                    raise EmbeddingBackendError("embedding-search-result") from exc
            try:
                vector_row_id = int(cast(Any, vector_row_id))
                score = float(cast(Any, score))
                distance = float(cast(Any, distance))
            except (TypeError, ValueError, OverflowError) as exc:
                raise EmbeddingBackendError("embedding-search-result") from exc
            if not math.isfinite(score) or not math.isfinite(distance):
                raise EmbeddingBackendError("embedding-search-result")
            entry = allowed.get(vector_row_id)
            if entry is None:
                raise EmbeddingBackendError("embedding-search-out-of-scope")
            text_value = _verify_entry(
                entry,
                generation=generation,
                plan=plan,
                record=active_session.get(
                    TranslationSourcePlan, generation.source_plan_id
                ),
                project=project,
                session=active_session,
            )
            memory = active_session.get(TranslationMemoryEntry, entry.subject_id)
            if memory is None:
                raise EmbeddingProjectionError("embedding-memory-missing")
            evidence = rehydrate_translation_memory_entry(
                memory,
                active_session.get(TranslationSourcePlan, generation.source_plan_id),
                session=active_session,
            )
            results.append(
                {
                    "entry_id": entry.id,
                    "embedding_entry_record_hash": entry.record_hash,
                    "memory_entry_record_hash": memory.record_hash,
                    "vector_row_id": entry.vector_row_id,
                    "subject_type": entry.subject_type,
                    "subject_id": entry.subject_id,
                    "source_text": text_value,
                    "score": score,
                    "distance": distance,
                    "memory": evidence,
                }
            )
        metadata["selected_entry_record_hashes"] = [
            item["embedding_entry_record_hash"] for item in results
        ]
        metadata["selected_memory_entry_record_hashes"] = [
            item["memory_entry_record_hash"] for item in results
        ]
        return EmbeddingSearchResponse("ready", tuple(results), metadata=metadata)
    except EmbeddingBackendError as exc:
        return EmbeddingSearchResponse(
            "degraded", reason=_bounded_error_code(exc), metadata=metadata
        )


class TranslatorDenseVectorStore:
    """Production sqlite-vec adapter which creates only the dense table."""

    def __init__(
        self,
        *,
        user_id: int,
        model: str,
        task_type: str,
        dimensions: int,
        session: Any | None = None,
        model_record: LLMModel | None = None,
        expected_api_key_id: int | None = None,
        expected_signature_hash: str | None = None,
    ):
        from ...embedding_profiles import (
            DynamicEmbeddingProfile,
            EmbeddingProfile,
        )

        self.user_id = user_id
        self.model = model
        self.task_type = task_type
        self.dimensions = int(dimensions)
        self.expected_api_key_id = expected_api_key_id
        self.expected_signature_hash = expected_signature_hash
        self.model_record = model_record
        self.namespace: str = ""
        self.session = _session(session)
        self.profile = (
            DynamicEmbeddingProfile(model_record, task_type=task_type)
            if model_record is not None
            else EmbeddingProfile(model=model, task_type=task_type)
        )

    def embedding_identity(self) -> Mapping[str, Any]:
        """Expose non-secret configuration identity for authenticated search."""

        if self.model_record is None or self.expected_signature_hash is None:
            raise EmbeddingBackendError("embedding-backend-identity")
        profile, _signature = _profile_snapshot(
            self.model_record,
            task_type=self.task_type,
            dimensions=self.dimensions,
        )
        return {
            "owner_user_id": self.user_id,
            "model_record_id": int(self.model_record.id),
            "model_id": self.model,
            "provider_id": int(self.model_record.provider_id),
            "api_key_id": self.expected_api_key_id,
            "dimensions": self.dimensions,
            "task_type": self.task_type,
            "query_prefix": self.model_record.custom_query_prefix,
            "passage_prefix": self.model_record.custom_passage_prefix,
            "profile_hash": _hash_json(profile),
            "signature_hash": self.expected_signature_hash,
        }

    def _assert_transport_session(self) -> None:
        # LLMService's credential transport is currently bound to the normal
        # application session.  Refuse a custom UoW before any provider I/O;
        # all vector SQL still uses the explicitly supplied session.
        if self.session is not _session(None):
            raise EmbeddingBackendError("embedding-llm-session-unsupported")

    @staticmethod
    def _table(namespace: str, dimensions: int) -> str:
        if not _NAMESPACE_RE.fullmatch(namespace):
            raise EmbeddingProjectionError("embedding-namespace")
        return f"vec_{namespace}_{int(dimensions)}"

    def create(self, *, namespace: str, dimensions: int) -> None:
        from ...core.vector_backend import SQLITE_VEC_AVAILABLE

        if not SQLITE_VEC_AVAILABLE:
            raise EmbeddingProjectionError("embedding-backend-unavailable")
        self.session.execute(
            text(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {self._table(namespace, dimensions)} "
                f"USING vec0(embedding float[{int(dimensions)}]);"
            )
        )

    def embed_documents(
        self, texts: list[str], *, generation: TranslationEmbeddingGeneration
    ) -> list[list[float]]:
        from ...llm_service import LLMService

        self._assert_transport_session()
        max_batch = self.profile.max_document_batch_size
        if max_batch is not None and len(texts) > max_batch:
            raise EmbeddingBackendError("embedding-provider-batch-size")
        formatted = self.profile.prepare_documents(texts=texts)
        kwargs = self.profile.request_kwargs_for_documents(
            dimensions=generation.dimensions
        )
        response = LLMService(user_id=self.user_id).embedding(
            model=generation.model_id,
            input=formatted,
            embedding_profile=self.profile,
            source="translator_embedding_projection",
            log_to_db=False,
            expected_api_key_id=generation.api_key_id,
            **kwargs,
        )
        items = list(getattr(response, "data", None) or [])
        vectors: list[list[float]] = []
        for item in items:
            item_value = cast(Any, item)
            embedding = (
                item_value.get("embedding")
                if isinstance(item_value, Mapping)
                else getattr(item_value, "embedding", None)
            )
            if not isinstance(embedding, Sequence) or isinstance(
                embedding, (str, bytes)
            ):
                raise EmbeddingBackendError("embedding-provider-cardinality")
            vectors.append(list(cast(Sequence[Any], embedding)))
        return vectors

    def upsert(
        self,
        row_ids: list[int],
        embeddings: list[list[float]],
        *,
        include_lexical_text: bool = False,
    ) -> None:
        if include_lexical_text:
            raise EmbeddingProjectionError("embedding-fts-forbidden")
        from ...core.vector_backend import numpy_module as _numpy
        from ...core.vector_backend import sqlite_vec_module as _sqlite_vec

        table = self._table(str(getattr(self, "namespace", "")), self.dimensions)
        # ``build_embedding_generation`` installs the generation namespace on
        # the backend before calling this method.
        self.session.execute(
            text(f"DELETE FROM {table} WHERE rowid IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ),
            {"ids": row_ids or [-1]},
        )
        serialized = [
            {
                "id": int(row_id),
                "emb": _sqlite_vec().serialize_float32(
                    _numpy().array(vector, dtype=_numpy().float32)
                ),
            }
            for row_id, vector in zip(row_ids, embeddings, strict=True)
        ]
        if serialized:
            self.session.execute(
                text(f"INSERT INTO {table}(rowid, embedding) VALUES (:id, :emb)"),
                serialized,
            )

    def row_ids(self, *, namespace: str) -> list[int]:
        table = self._table(namespace, self.dimensions)
        return [
            int(value)
            for (value,) in self.session.execute(
                text(f"SELECT rowid FROM {table}")
            ).all()
        ]

    def vector_witness(
        self, *, namespace: str, row_ids: list[int], dimensions: int
    ) -> str:
        table = self._table(namespace, dimensions)
        expected = sorted(int(value) for value in row_ids)
        rows = self.session.execute(
            text(f"SELECT rowid, embedding FROM {table} ORDER BY rowid")
        ).all()
        values: list[dict[str, Any]] = []
        for row_id, vector in rows:
            if isinstance(vector, memoryview):
                raw = vector.tobytes()
            elif isinstance(vector, (bytes, bytearray)):
                raw = bytes(vector)
            elif hasattr(vector, "tobytes"):
                raw = vector.tobytes()
            else:
                raw = str(vector).encode("utf-8")
            values.append(
                {"row_id": int(row_id), "vector": base64.b64encode(raw).decode("ascii")}
            )
        if [item["row_id"] for item in values] != expected:
            raise EmbeddingBackendError("embedding-row-membership")
        return hashlib.sha256(canonical_json(values).encode("utf-8")).hexdigest()

    def search_semantic(
        self,
        query_text: str,
        *,
        namespace: str,
        dimensions: int,
        model: str,
        task_type: str,
        top_k: int,
        allowed_row_ids: list[int],
    ) -> list[tuple[int, float, float]]:
        from ...core.vector_backend import numpy_module as _numpy
        from ...core.vector_backend import sqlite_vec_module as _sqlite_vec

        table = self._table(namespace, dimensions)
        if not allowed_row_ids:
            return []
        self._assert_transport_session()
        formatted = self.profile.prepare_query(query_text)
        kwargs = self.profile.request_kwargs_for_query(dimensions=dimensions)
        from ...llm_service import LLMService

        response = LLMService(user_id=self.user_id).embedding(
            model=model,
            input=[formatted],
            embedding_profile=self.profile,
            source="translator_embedding_query",
            log_to_db=False,
            expected_api_key_id=getattr(self, "expected_api_key_id", None),
            **kwargs,
        )
        items = list(getattr(response, "data", None) or [])
        if len(items) != 1:
            raise EmbeddingProjectionError("embedding-query-cardinality")
        item = items[0]
        vector = item.get("embedding") if isinstance(item, Mapping) else item.embedding
        vector = _validate_vectors([vector], expected_count=1, dimensions=dimensions)[0]
        params = {
            "query_vector": _sqlite_vec().serialize_float32(
                _numpy().array(vector, dtype=_numpy().float32)
            ),
            "top_k": max(1, int(top_k)),
        }
        statement = text(
            f"SELECT rowid, distance FROM {table} "
            "WHERE embedding MATCH :query_vector AND k = :top_k "
            "AND rowid IN :allowed"
        ).bindparams(bindparam("allowed", expanding=True))
        params["allowed"] = [int(value) for value in allowed_row_ids]
        return [
            (int(row_id), 1.0 / (1.0 + max(0.0, float(distance))), float(distance))
            for row_id, distance in self.session.execute(statement, params).all()
        ]

    def drop(self, *, namespace: str, dimensions: int) -> None:
        table = self._table(namespace, dimensions)
        self.session.execute(text(f"DROP TABLE IF EXISTS {table}"))


def build_with_dense_backend(
    generation: TranslationEmbeddingGeneration, *, session: Any | None = None
) -> TranslationEmbeddingGeneration:
    """Convenience production adapter; callers still own transaction commit."""

    active_session = _session(session)
    generation_row = _generation_for_session(generation, session=active_session)
    model_record = active_session.get(
        LLMModel, generation_row.model_record_id, populate_existing=True
    )
    if model_record is None:
        raise EmbeddingProjectionError("embedding-model-unavailable")
    backend = TranslatorDenseVectorStore(
        user_id=generation_row.owner_user_id,
        model=generation_row.model_id,
        task_type=generation_row.task_type,
        dimensions=generation_row.dimensions,
        session=active_session,
        model_record=model_record,
        expected_api_key_id=generation_row.api_key_id,
        expected_signature_hash=generation_row.signature_hash,
    )
    backend.namespace = generation_row.namespace
    return build_embedding_generation(
        generation_row, backend=backend, session=active_session
    )


__all__ = [
    "EMBEDDING_CONTRACT_VERSION",
    "PROJECTION_KINDS",
    "EmbeddingActivationConflict",
    "EmbeddingBackend",
    "EmbeddingBackendError",
    "EmbeddingProjectionError",
    "EmbeddingSearchResponse",
    "TranslatorDenseVectorStore",
    "activate_embedding_generation",
    "active_embedding_generation",
    "build_embedding_generation",
    "build_with_dense_backend",
    "prepare_embedding_generation",
    "search_embedding_generation",
    "search_pinned_embedding_generation",
    "verified_active_embedding_reference",
    "verify_pinned_embedding_reference",
]
