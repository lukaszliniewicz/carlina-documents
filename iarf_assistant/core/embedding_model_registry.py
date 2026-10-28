from __future__ import annotations

from typing import Any

from iarf_assistant.extensions import db
from iarf_assistant.models import LLMModel, LLMProvider, Setting


VERTEX_GEMINI_EMBEDDING_2_MODEL_ID = "vertex_ai/gemini-embedding-2"
VERTEX_GEMINI_EMBEDDING_2_DIMENSIONS = 3072
VERTEX_GEMINI_EMBEDDING_2_INPUT_TOKENS = 8192
LMSTUDIO_QWEN3_EMBEDDING_8B_MODEL_ID = "lmstudio/qwen3-embedding-8b-local"
LMSTUDIO_QWEN3_EMBEDDING_8B_DIMENSIONS = 4096
LMSTUDIO_QWEN3_EMBEDDING_8B_INPUT_TOKENS = 32768
LMSTUDIO_QWEN3_EMBEDDING_8B_ARTIFACT = (
    "Qwen/Qwen3-Embedding-8B-GGUF/Qwen3-Embedding-8B-Q4_K_M.gguf"
)
LMSTUDIO_QWEN3_EMBEDDING_8B_SHA256 = (
    "3fcd3febec8b3fd64435204db75bf0dd73b91e8d0661e0331acfe7e7c3120b85"
)
QWEN3_KNOWLEDGE_QUERY_INSTRUCTION = (
    "Given a user knowledge search query, retrieve relevant passages "
    "that answer the query"
)


def ensure_vertex_gemini_embedding_2(
    *,
    user_id: int | None = None,
    active: bool = True,
    select_for_legacy_rag: bool = False,
    dimensions: int = VERTEX_GEMINI_EMBEDDING_2_DIMENSIONS,
    commit: bool = True,
) -> tuple[LLMModel, bool]:
    """Register the current GA Vertex Gemini embedding model idempotently.

    Selecting it for the legacy RAG table is deliberately separate: that table
    is not model-versioned and must be rebuilt before changing embedding space.
    Canonical Knowledge Fabric generations do not have that limitation.
    """
    dimensions = int(dimensions)
    if dimensions not in {128, 256, 512, 768, 1536, 2048, 3072}:
        raise ValueError(
            "Gemini Embedding 2 dimensions must be an approved MRL size "
            "(128, 256, 512, 768, 1536, 2048, or 3072)."
        )
    provider = LLMProvider.query.filter_by(name="vertex_ai").one_or_none()
    if provider is None:
        raise ValueError("The Vertex AI provider is not configured.")
    model = LLMModel.query.filter_by(
        user_id=user_id,
        model_id=VERTEX_GEMINI_EMBEDDING_2_MODEL_ID,
    ).one_or_none()
    created = model is None
    if model is None:
        model = LLMModel(
            user_id=user_id,
            provider_id=provider.id,
            model_id=VERTEX_GEMINI_EMBEDDING_2_MODEL_ID,
            display_name="Gemini Embedding 2 (Vertex AI)",
        )
        db.session.add(model)
    model.provider_id = provider.id
    model.display_name = "Gemini Embedding 2 (Vertex AI)"
    model.is_active = bool(active)
    model.manually_added = True
    model.context_window = VERTEX_GEMINI_EMBEDDING_2_INPUT_TOKENS
    model.max_output_tokens = None
    model.capability_overrides = {
        "max_input_tokens": VERTEX_GEMINI_EMBEDDING_2_INPUT_TOKENS,
        "max_output_tokens": 1,
        "modalities": ["text", "image", "audio", "video", "pdf"],
        "output_dimensions": {
            "default": VERTEX_GEMINI_EMBEDDING_2_DIMENSIONS,
            "selected": dimensions,
            "mrl": True,
        },
        "transport": "embedContent",
        "max_independent_inputs_per_request": 1,
        "provider_normalizes_reduced_dimensions": True,
    }
    model.supports_vision = True
    model.supports_pdf = True
    model.supports_audio_in = True
    model.supports_reasoning = False
    model.input_cost_per_million_tokens = 0.20
    model.output_cost_per_million_tokens = 0.0
    model.model_type = "embedding"
    model.default_dimensions = dimensions
    model.supports_mrl = True
    model.prefix_style = "gemini_v2"
    model.custom_query_prefix = None
    model.custom_passage_prefix = None
    db.session.flush()

    if select_for_legacy_rag:
        values = {
            "rag_embedding_model": model.model_id,
            "rag_embedding_dimensions": str(dimensions),
            "rag_task_type": "search",
        }
        for key, value in values.items():
            setting = db.session.get(Setting, key)
            if setting is None:
                setting = Setting(key=key, value=value)
                db.session.add(setting)
            else:
                setting.value = value
    if commit:
        db.session.commit()
    return model, created


def vertex_gemini_embedding_2_signature(
    *,
    dimensions: int = VERTEX_GEMINI_EMBEDDING_2_DIMENSIONS,
    task: str = "search result",
    extractor: str,
    extractor_version: str,
    chunker: str,
    chunker_version: str,
    chunker_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "embedding_provider": "vertex_ai",
        "embedding_model": VERTEX_GEMINI_EMBEDDING_2_MODEL_ID,
        "embedding_model_revision": "gemini-embedding-2-ga",
        "dimensions": int(dimensions),
        "task_type": str(task or "search result"),
        "prefix_style": "gemini_v2_instruction",
        "normalization": "provider_l2",
        "distance_metric": "cosine_equivalent_l2",
        "extractor": extractor,
        "extractor_version": extractor_version,
        "chunker": chunker,
        "chunker_version": chunker_version,
        "chunker_config": dict(chunker_config or {}),
        "language": "multilingual",
        "lexical_schema_version": 2,
    }


def ensure_lmstudio_qwen3_embedding_8b(
    *,
    user_id: int | None = None,
    active: bool = True,
    select_for_legacy_rag: bool = False,
    dimensions: int = LMSTUDIO_QWEN3_EMBEDDING_8B_DIMENSIONS,
    commit: bool = True,
) -> tuple[LLMModel, bool]:
    """Register the tested LM Studio Qwen3 Embedding 8B endpoint model.

    The LM Studio endpoint currently returns the full 4096-dimensional vector
    even when ``dimensions`` is supplied. Reduced MRL spaces are therefore
    generated by the Qwen3 embedding profile and are versioned separately.
    """
    dimensions = int(dimensions)
    if not 32 <= dimensions <= LMSTUDIO_QWEN3_EMBEDDING_8B_DIMENSIONS:
        raise ValueError("Qwen3 Embedding 8B dimensions must be from 32 to 4096.")
    provider = LLMProvider.query.filter_by(name="lmstudio").one_or_none()
    if provider is None:
        raise ValueError("The LM Studio provider is not configured.")
    model = LLMModel.query.filter_by(
        user_id=user_id,
        model_id=LMSTUDIO_QWEN3_EMBEDDING_8B_MODEL_ID,
    ).one_or_none()
    created = model is None
    if model is None:
        model = LLMModel(
            user_id=user_id,
            provider_id=provider.id,
            model_id=LMSTUDIO_QWEN3_EMBEDDING_8B_MODEL_ID,
            display_name="Qwen3 Embedding 8B (LM Studio, Q4_K_M)",
        )
        db.session.add(model)
    model.provider_id = provider.id
    model.display_name = "Qwen3 Embedding 8B (LM Studio, Q4_K_M)"
    model.is_active = bool(active)
    model.manually_added = True
    model.context_window = LMSTUDIO_QWEN3_EMBEDDING_8B_INPUT_TOKENS
    model.max_output_tokens = None
    model.capability_overrides = {
        "max_input_tokens": LMSTUDIO_QWEN3_EMBEDDING_8B_INPUT_TOKENS,
        "max_output_tokens": 1,
        "modalities": ["text"],
        "output_dimensions": {
            "native": LMSTUDIO_QWEN3_EMBEDDING_8B_DIMENSIONS,
            "selected": dimensions,
            "minimum": 32,
            "mrl": True,
        },
        "transport": "openai_compatible_embeddings",
        "transport_dimensions_parameter_effective": False,
        "reduced_dimension_transform": "leading_dimensions_then_client_l2",
        "independent_input_batching": True,
        "artifact": LMSTUDIO_QWEN3_EMBEDDING_8B_ARTIFACT,
        "artifact_sha256": LMSTUDIO_QWEN3_EMBEDDING_8B_SHA256,
    }
    model.supports_vision = False
    model.supports_pdf = False
    model.supports_audio_in = False
    model.supports_reasoning = False
    model.input_cost_per_million_tokens = 0.0
    model.output_cost_per_million_tokens = 0.0
    model.model_type = "embedding"
    model.default_dimensions = dimensions
    model.supports_mrl = True
    model.prefix_style = "qwen3_embedding"
    model.custom_query_prefix = (
        f"Instruct: {QWEN3_KNOWLEDGE_QUERY_INSTRUCTION}\nQuery: "
    )
    model.custom_passage_prefix = ""
    db.session.flush()

    if select_for_legacy_rag:
        values = {
            "rag_embedding_model": model.model_id,
            "rag_embedding_dimensions": str(dimensions),
            "rag_task_type": "search",
        }
        for key, value in values.items():
            setting = db.session.get(Setting, key)
            if setting is None:
                setting = Setting(key=key, value=value)
                db.session.add(setting)
            else:
                setting.value = value
    if commit:
        db.session.commit()
    return model, created


def lmstudio_qwen3_embedding_8b_signature(
    *,
    dimensions: int = LMSTUDIO_QWEN3_EMBEDDING_8B_DIMENSIONS,
    extractor: str,
    extractor_version: str,
    chunker: str,
    chunker_version: str,
    chunker_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    dimensions = int(dimensions)
    if not 32 <= dimensions <= LMSTUDIO_QWEN3_EMBEDDING_8B_DIMENSIONS:
        raise ValueError("Qwen3 Embedding 8B dimensions must be from 32 to 4096.")
    return {
        "embedding_provider": "lmstudio",
        "embedding_model": LMSTUDIO_QWEN3_EMBEDDING_8B_MODEL_ID,
        "embedding_model_revision": LMSTUDIO_QWEN3_EMBEDDING_8B_ARTIFACT,
        "embedding_artifact_sha256": LMSTUDIO_QWEN3_EMBEDDING_8B_SHA256,
        "quantization": "Q4_K_M",
        "dimensions": dimensions,
        "native_dimensions": LMSTUDIO_QWEN3_EMBEDDING_8B_DIMENSIONS,
        "task_type": "knowledge_retrieval",
        "query_instruction": QWEN3_KNOWLEDGE_QUERY_INSTRUCTION,
        "document_instruction": None,
        "prefix_style": "qwen3_embedding_query_only",
        "dimension_projection": "leading_mrl_dimensions_client_side",
        "normalization": "client_l2_after_projection",
        "distance_metric": "cosine_equivalent_l2",
        "extractor": extractor,
        "extractor_version": extractor_version,
        "chunker": chunker,
        "chunker_version": chunker_version,
        "chunker_config": dict(chunker_config or {}),
        "language": "multilingual",
        "lexical_schema_version": 2,
    }
