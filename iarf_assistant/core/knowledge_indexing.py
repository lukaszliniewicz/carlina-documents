from __future__ import annotations

from typing import Any

from sqlalchemy import case, or_

from iarf_assistant.core.embedding_model_registry import (
    LMSTUDIO_QWEN3_EMBEDDING_8B_MODEL_ID,
    VERTEX_GEMINI_EMBEDDING_2_MODEL_ID,
    lmstudio_qwen3_embedding_8b_signature,
    vertex_gemini_embedding_2_signature,
)
from iarf_assistant.core.knowledge_fabric import (
    activate_index_generation,
    active_index_generation,
    create_index_generation,
)
from iarf_assistant.core.model_capabilities import resolve_model_record
from iarf_assistant.extensions import db
from iarf_assistant.models import LLMModel, Setting


def _setting_value(key: str) -> str | None:
    row = db.session.get(Setting, key)
    value = str(row.value or "").strip() if row is not None else ""
    return value or None


def resolve_knowledge_embedding_config(
    *,
    user_id: int,
    model_id: str | None = None,
    dimensions: int | None = None,
) -> tuple[LLMModel, int]:
    requested_model = (
        str(model_id or "").strip()
        or _setting_value("knowledge_embedding_model")
    )
    model = (
        resolve_model_record(requested_model, user_id=user_id)
        if requested_model
        else None
    )
    if model is None:
        preferred_ids = [
            VERTEX_GEMINI_EMBEDDING_2_MODEL_ID,
            LMSTUDIO_QWEN3_EMBEDDING_8B_MODEL_ID,
            _setting_value("rag_embedding_model"),
        ]
        for candidate in preferred_ids:
            if not candidate:
                continue
            candidate_model = resolve_model_record(candidate, user_id=user_id)
            if (
                candidate_model is not None
                and candidate_model.is_active
                and candidate_model.model_type == "embedding"
            ):
                model = candidate_model
                break
    if model is None:
        model = (
            LLMModel.query
            .filter(
                or_(LLMModel.user_id == user_id, LLMModel.user_id.is_(None)),
                LLMModel.is_active.is_(True),
                LLMModel.model_type == "embedding",
            )
            .order_by(
                case((LLMModel.user_id == user_id, 0), else_=1),
                LLMModel.id.asc(),
            )
            .first()
        )
    if model is None:
        raise ValueError("No active embedding model is configured for Knowledge.")
    if not model.is_active or model.model_type != "embedding":
        raise ValueError(
            f"{model.model_id} is not an active embedding model."
        )

    configured_dimensions = (
        dimensions
        if dimensions is not None
        else _setting_value("knowledge_embedding_dimensions")
    )
    try:
        selected_dimensions = int(
            configured_dimensions or model.default_dimensions or 0
        )
    except (TypeError, ValueError):
        raise ValueError("Knowledge embedding dimensions must be an integer.") from None
    if selected_dimensions <= 0:
        raise ValueError(
            f"{model.model_id} does not declare embedding dimensions."
        )
    native_dimensions = int(model.default_dimensions or selected_dimensions)
    if not model.supports_mrl and selected_dimensions != native_dimensions:
        raise ValueError(
            f"{model.model_id} requires {native_dimensions} dimensions."
        )
    if model.supports_mrl and selected_dimensions > native_dimensions:
        raise ValueError(
            f"{model.model_id} supports at most {native_dimensions} dimensions."
        )
    return model, selected_dimensions


def knowledge_index_signature(
    *,
    model: LLMModel,
    dimensions: int,
) -> dict[str, Any]:
    common = {
        "extractor": "canonical_source_version",
        "extractor_version": "2",
        "chunker": "canonical_source_units",
        "chunker_version": "2",
        "chunker_config": {
            "one_vector_per_unit": True,
            "membership_unit_selectors": True,
        },
    }
    if model.model_id == VERTEX_GEMINI_EMBEDDING_2_MODEL_ID:
        return vertex_gemini_embedding_2_signature(
            dimensions=dimensions,
            task="search result",
            **common,
        )
    if model.model_id == LMSTUDIO_QWEN3_EMBEDDING_8B_MODEL_ID:
        return lmstudio_qwen3_embedding_8b_signature(
            dimensions=dimensions,
            **common,
        )
    provider_name = model.provider.name if model.provider is not None else None
    return {
        "embedding_provider": provider_name,
        "embedding_model": model.model_id,
        "embedding_model_revision": (
            (model.capability_overrides or {}).get("revision")
            or "database-configured"
        ),
        "dimensions": dimensions,
        "task_type": "search",
        "prefix_style": model.prefix_style or "none",
        "custom_query_prefix": model.custom_query_prefix,
        "custom_passage_prefix": model.custom_passage_prefix,
        "normalization": (
            (model.capability_overrides or {}).get("normalization")
            or "profile_defined"
        ),
        "distance_metric": "cosine_equivalent_l2",
        "extractor": common["extractor"],
        "extractor_version": common["extractor_version"],
        "chunker": common["chunker"],
        "chunker_version": common["chunker_version"],
        "chunker_config": common["chunker_config"],
        "lexical_schema_version": 2,
    }


def rebuild_knowledge_collection_index(
    *,
    rag_service,
    user_id: int,
    collection_id: int,
    model_id: str | None = None,
    dimensions: int | None = None,
    batch_size: int = 100,
) -> dict[str, Any]:
    model, selected_dimensions = resolve_knowledge_embedding_config(
        user_id=user_id,
        model_id=model_id,
        dimensions=dimensions,
    )
    signature = knowledge_index_signature(
        model=model,
        dimensions=selected_dimensions,
    )
    active = active_index_generation(
        user_id=user_id,
        collection_id=collection_id,
        projection_type="hybrid",
    )
    generation = create_index_generation(
        user_id=user_id,
        collection_id=collection_id,
        projection_type="hybrid",
        signature=signature,
        commit=True,
    )
    ready = rag_service.build_knowledge_generation(
        generation.id,
        batch_size=batch_size,
    )
    activated = activate_index_generation(
        ready.id,
        expected_active_generation_id=active.id if active else None,
        commit=True,
    )
    return {
        "generation_id": activated.id,
        "previous_generation_id": activated.previous_generation_id,
        "status": activated.status,
        "entry_count": activated.entry_count,
        "embedding_model": model.model_id,
        "dimensions": selected_dimensions,
        "signature_hash": activated.signature_hash,
        "signature": activated.signature_json,
    }
