import numpy as np

QWEN3_RETRIEVAL_INSTRUCTIONS = {
    "search": (
        "Given a user knowledge search query, retrieve relevant passages "
        "that answer the query"
    ),
    "retrieval_query": (
        "Given a user knowledge search query, retrieve relevant passages "
        "that answer the query"
    ),
    "qa": (
        "Given a question, retrieve relevant passages that contain the "
        "information needed to answer it"
    ),
    "fact": (
        "Given a claim, retrieve relevant passages that provide evidence "
        "for checking the claim"
    ),
    "code": (
        "Given a code search query, retrieve relevant code or documentation "
        "that satisfies the query"
    ),
}


class EmbeddingProfile:
    """Provider/model-specific embedding behavior wrapper."""

    def __init__(self, model: str, task_type: str = "search"):
        self.model = model or ""
        self.task_type = task_type if isinstance(task_type, str) and task_type else "search"

    @property
    def normalized_task_type(self) -> str:
        return self.task_type.lower()

    @property
    def max_document_batch_size(self) -> int | None:
        """Provider-safe number of independent documents in one request."""
        return None

    def prepare_documents(self, texts: list, titles: list[str] = None) -> list:
        return texts

    def prepare_query(self, query):
        return query

    def request_kwargs_for_documents(self, dimensions: int = None) -> dict:
        kwargs = {}
        if dimensions is not None:
            kwargs["dimensions"] = dimensions
        return kwargs

    def request_kwargs_for_query(self, dimensions: int = None) -> dict:
        kwargs = {}
        if dimensions is not None:
            kwargs["dimensions"] = dimensions
        return kwargs

    def sanitize_litellm_params(self, litellm_params: dict) -> dict:
        # Some providers reject unknown passthrough args; keep payload conservative.
        litellm_params.pop("auto_truncate", None)
        return litellm_params

    def postprocess_response(self, response, requested_dim: int = None):
        return response


class GeminiEmbedding2Profile(EmbeddingProfile):
    """Gemini Embedding 2 profile with task prefixes and MRL normalization."""

    @property
    def max_document_batch_size(self) -> int | None:
        # Vertex routes this model through embedContent. A flat list is
        # interleaved into one multimodal content object, so independent
        # documents must be sent separately.
        return 1

    def prepare_documents(self, texts: list, titles: list[str] = None) -> list:
        if self.normalized_task_type == "similarity":
            return texts

        formatted = []
        for idx, content in enumerate(texts):
            if isinstance(content, str):
                title = titles[idx] if titles and idx < len(titles) else "none"
                formatted.append(f"title: {title or 'none'} | text: {content}")
            else:
                formatted.append(content)
        return formatted

    def prepare_query(self, query):
        if not isinstance(query, str):
            return query

        task = self.normalized_task_type
        if task == "similarity":
            return query
        if task in ["search", "retrieval_query"]:
            return f"task: search result | query: {query}"
        if task == "qa":
            return f"task: question answering | query: {query}"
        if task == "fact":
            return f"task: fact checking | query: {query}"
        if task == "code":
            return f"task: code retrieval | query: {query}"
        return query

    def sanitize_litellm_params(self, litellm_params: dict) -> dict:
        litellm_params = super().sanitize_litellm_params(litellm_params)
        # Gemini Embedding 2 uses string prefixes instead of the old task_type enum.
        litellm_params.pop("task_type", None)
        return litellm_params

    def postprocess_response(self, response, requested_dim: int = None):
        # Gemini Embedding 2 is provider-normalized at both full and reduced
        # MRL dimensions. Preserve provider values and only validate shape and
        # numeric sanity; re-normalizing would hide transport/model regressions.
        for item in response.data:
            emb_val = item.get("embedding") if isinstance(item, dict) else getattr(item, "embedding")
            emb_np = np.array(emb_val, dtype=np.float32)
            if requested_dim and len(emb_np) != int(requested_dim):
                raise ValueError(
                    "Gemini Embedding 2 returned "
                    f"{len(emb_np)} dimensions; requested {requested_dim}."
                )
            if not np.all(np.isfinite(emb_np)):
                raise ValueError("Gemini Embedding 2 returned non-finite values.")
            norm_val = float(np.linalg.norm(emb_np))
            if norm_val and not 0.95 <= norm_val <= 1.05:
                raise ValueError(
                    "Gemini Embedding 2 returned a vector outside the expected "
                    f"L2-normalized range (norm={norm_val:.6f})."
                )
        return response


class GeminiEmbedding001Profile(EmbeddingProfile):
    """Gemini Embedding 001 profile with task_type enum mapping."""

    @property
    def max_document_batch_size(self) -> int | None:
        # The Vertex endpoint accepts one input text per request.
        return 1

    def request_kwargs_for_documents(self, dimensions: int = None) -> dict:
        kwargs = super().request_kwargs_for_documents(dimensions=dimensions)
        kwargs["task_type"] = self._map_doc_task(self.normalized_task_type)
        return kwargs

    def request_kwargs_for_query(self, dimensions: int = None) -> dict:
        kwargs = super().request_kwargs_for_query(dimensions=dimensions)
        kwargs["task_type"] = self._map_query_task(self.normalized_task_type)
        return kwargs

    def postprocess_response(self, response, requested_dim: int = None):
        # Unlike Gemini Embedding 2, 001 does not normalize truncated MRL
        # vectors. Normalize reduced vectors client-side as Google requires.
        if not requested_dim or requested_dim >= 3072:
            return response
        for item in response.data:
            emb_val = item.get("embedding") if isinstance(item, dict) else getattr(item, "embedding")
            emb_np = np.array(emb_val, dtype=np.float32)
            if len(emb_np) != int(requested_dim):
                raise ValueError(
                    "Gemini Embedding 001 returned "
                    f"{len(emb_np)} dimensions; requested {requested_dim}."
                )
            norm_val = np.linalg.norm(emb_np)
            if norm_val > 0:
                emb_np = emb_np / norm_val
            if isinstance(item, dict):
                item["embedding"] = emb_np.tolist()
            else:
                item.embedding = emb_np.tolist()
        return response

    @staticmethod
    def _map_query_task(task_type: str) -> str:
        if task_type == "similarity":
            return "SEMANTIC_SIMILARITY"
        if task_type in ["search", "retrieval_query"]:
            return "RETRIEVAL_QUERY"
        if task_type == "qa":
            return "QUESTION_ANSWERING"
        if task_type == "fact":
            return "FACT_VERIFICATION"
        if task_type == "code":
            return "CODE_RETRIEVAL_QUERY"
        return "RETRIEVAL_QUERY"

    @staticmethod
    def _map_doc_task(task_type: str) -> str:
        if task_type == "similarity":
            return "SEMANTIC_SIMILARITY"
        return "RETRIEVAL_DOCUMENT"


class Qwen3EmbeddingProfile(EmbeddingProfile):
    """Qwen3 Embedding asymmetric retrieval and MRL behavior.

    Qwen's published retrieval contract instructs queries but leaves
    documents unprefixed. OpenAI-compatible local servers may accept a
    ``dimensions`` parameter without applying it, so reduced dimensions are
    produced deterministically by taking the leading MRL dimensions and then
    L2-normalizing them client-side.
    """

    def prepare_documents(self, texts: list, titles: list[str] = None) -> list:
        return texts

    def prepare_query(self, query):
        if not isinstance(query, str):
            return query
        if self.normalized_task_type == "similarity":
            return query
        instruction = QWEN3_RETRIEVAL_INSTRUCTIONS.get(
            self.normalized_task_type,
            QWEN3_RETRIEVAL_INSTRUCTIONS["search"],
        )
        return f"Instruct: {instruction}\nQuery: {query}"

    def sanitize_litellm_params(self, litellm_params: dict) -> dict:
        litellm_params = super().sanitize_litellm_params(litellm_params)
        litellm_params.pop("task_type", None)
        model_id = str(litellm_params.get("model") or "").lower()
        if model_id.startswith(("openai/", "lmstudio/", "lm_studio/")):
            # LM Studio 0.4.20 accepts this field for Qwen3 Embedding but
            # returns the full vector unchanged. Avoid relying on a silent
            # no-op; postprocess_response owns the signed MRL transform.
            litellm_params.pop("dimensions", None)
        return litellm_params

    def postprocess_response(self, response, requested_dim: int = None):
        for item in response.data:
            emb_val = (
                item.get("embedding")
                if isinstance(item, dict)
                else getattr(item, "embedding")
            )
            emb_np = np.array(emb_val, dtype=np.float32)
            if not np.all(np.isfinite(emb_np)):
                raise ValueError("Qwen3 Embedding returned non-finite values.")
            if requested_dim:
                requested_dim = int(requested_dim)
                if len(emb_np) < requested_dim:
                    raise ValueError(
                        "Qwen3 Embedding returned "
                        f"{len(emb_np)} dimensions; requested {requested_dim}."
                    )
                emb_np = emb_np[:requested_dim]
            norm_val = float(np.linalg.norm(emb_np))
            if norm_val <= 0:
                raise ValueError("Qwen3 Embedding returned a zero vector.")
            emb_np = emb_np / norm_val
            if isinstance(item, dict):
                item["embedding"] = emb_np.tolist()
            else:
                item.embedding = emb_np.tolist()
        return response


class DynamicEmbeddingProfile(EmbeddingProfile):
    """Dynamic profile that configures behavior based on LLMModel DB settings."""

    def __init__(self, db_model, task_type: str = "search"):
        super().__init__(model=db_model.model_id, task_type=task_type)
        self.prefix_style = db_model.prefix_style or "none"
        self.supports_mrl = db_model.supports_mrl
        self.custom_query_prefix = db_model.custom_query_prefix
        self.custom_doc_prefix = db_model.custom_passage_prefix
        
        # Instantiate delegates for Gemini styles if configured
        self._delegate = None
        if self.prefix_style == "gemini_v2":
            self._delegate = GeminiEmbedding2Profile(model=db_model.model_id, task_type=task_type)
        elif self.prefix_style == "gemini_v1":
            self._delegate = GeminiEmbedding001Profile(model=db_model.model_id, task_type=task_type)
        elif self.prefix_style == "qwen3_embedding":
            self._delegate = Qwen3EmbeddingProfile(model=db_model.model_id, task_type=task_type)

    def prepare_documents(self, texts: list, titles: list[str] = None) -> list:
        if self._delegate:
            return self._delegate.prepare_documents(texts, titles)
            
        if self.prefix_style in ("query_passage", "passage"):
            return [f"passage: {t}" if isinstance(t, str) else t for t in texts]
            
        if self.prefix_style == "custom" and self.custom_doc_prefix:
            return [f"{self.custom_doc_prefix}{t}" if isinstance(t, str) else t for t in texts]
            
        return super().prepare_documents(texts, titles)

    @property
    def max_document_batch_size(self) -> int | None:
        if self._delegate:
            return self._delegate.max_document_batch_size
        return super().max_document_batch_size

    def prepare_query(self, query):
        if self._delegate:
            return self._delegate.prepare_query(query)
            
        if not isinstance(query, str):
            return query
            
        if self.prefix_style == "query_passage":
            return f"query: {query}"
            
        if self.prefix_style == "custom" and self.custom_query_prefix:
            return f"{self.custom_query_prefix}{query}"
            
        return super().prepare_query(query)

    def request_kwargs_for_documents(self, dimensions: int = None) -> dict:
        if self._delegate:
            kwargs = self._delegate.request_kwargs_for_documents(dimensions=dimensions)
        else:
            kwargs = super().request_kwargs_for_documents(dimensions=dimensions)
            
        # Strip dimensions if MRL is not supported
        if not self.supports_mrl:
            kwargs.pop("dimensions", None)
        return kwargs

    def request_kwargs_for_query(self, dimensions: int = None) -> dict:
        if self._delegate:
            kwargs = self._delegate.request_kwargs_for_query(dimensions=dimensions)
        else:
            kwargs = super().request_kwargs_for_query(dimensions=dimensions)
            
        # Strip dimensions if MRL is not supported
        if not self.supports_mrl:
            kwargs.pop("dimensions", None)
        return kwargs

    def sanitize_litellm_params(self, litellm_params: dict) -> dict:
        if self._delegate:
            litellm_params = self._delegate.sanitize_litellm_params(litellm_params)
        else:
            litellm_params = super().sanitize_litellm_params(litellm_params)
            
        # To avoid LiteLLM UnsupportedParamsError bugs with local OpenAI-compatible models,
        # we strip the 'dimensions' parameter from the API request if the model is routed
        # via the OpenAI provider in LiteLLM but is not a native OpenAI text-embedding-3 model.
        # For all other models, we keep it if MRL is supported.
        model_id = litellm_params.get("model", "")
        is_non_native_openai = (model_id.startswith("openai/") and "text-embedding-3" not in model_id)
        
        if not self.supports_mrl or is_non_native_openai:
            litellm_params.pop("dimensions", None)
            
        return litellm_params

    def postprocess_response(self, response, requested_dim: int = None):
        if self._delegate:
            return self._delegate.postprocess_response(response, requested_dim=requested_dim)
            
        response = super().postprocess_response(response, requested_dim=requested_dim)
        
        # If a specific lower dimension is requested (e.g. for Matryoshka/MRL),
        # but the API returned a full-size embedding (e.g. because the API dropped
        # the parameter or doesn't support MRL parameter in the request),
        # we truncate and L2 normalize client-side.
        if requested_dim and response and hasattr(response, "data"):
            for item in response.data:
                emb_val = item.get("embedding") if isinstance(item, dict) else getattr(item, "embedding", None)
                if emb_val and len(emb_val) > requested_dim:
                    emb_np = np.array(emb_val[:requested_dim], dtype=np.float32)
                    norm_val = np.linalg.norm(emb_np)
                    if norm_val > 0:
                        emb_np = emb_np / norm_val
                    if isinstance(item, dict):
                        item["embedding"] = emb_np.tolist()
                    else:
                        item.embedding = emb_np.tolist()
        return response


def resolve_embedding_profile(
    model: str,
    task_type: str = "search",
    *,
    user_id: int | None = None,
) -> EmbeddingProfile:
    from flask import has_app_context
    if has_app_context():
        try:
            from iarf_assistant.core.model_capabilities import resolve_model_record

            db_model = resolve_model_record(model, user_id=user_id)
            if db_model:
                return DynamicEmbeddingProfile(db_model=db_model, task_type=task_type)
        except Exception as e:
            import logging
            logging.warning(f"Failed to query DB for embedding profile of model '{model}': {e}")

    lowered = (model or "").lower()
    if "gemini-embedding-2" in lowered:
        return GeminiEmbedding2Profile(model=model, task_type=task_type)
    if "gemini-embedding-001" in lowered:
        return GeminiEmbedding001Profile(model=model, task_type=task_type)
    if "qwen3" in lowered and "embedding" in lowered:
        return Qwen3EmbeddingProfile(model=model, task_type=task_type)
    return EmbeddingProfile(model=model, task_type=task_type)
