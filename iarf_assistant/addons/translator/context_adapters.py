"""Window-time adapters for verified contextual translation memory."""

from __future__ import annotations

import json
from collections.abc import Mapping, MutableMapping
from typing import Any, cast

from .block_contract import BlockContractError, canonical_json, sha256_text
from .context_baselines import rehydrate_context_baseline
from .embedding_projection import (
    EmbeddingProjectionError,
    search_pinned_embedding_generation,
)
from .models import (
    TranslationContextBaseline,
    TranslationSourcePlan,
)
from .window_contract import ContextSlot

_SEGMENT_MEMORY_QUERY_LIMIT = 6
_SEGMENT_MEMORY_MATCH_LIMIT = 6
_SEGMENT_MEMORY_MATCHES_PER_QUERY = 2
_SEGMENT_MEMORY_SLOT_BYTES = 8_000
_SEGMENT_MEMORY_QUERY_CHARS = 750
_SEGMENT_MEMORY_QUERY_EXCLUDED_ROLES = frozenset(
    {"heading", "document_title", "source_toc_entry", "accessibility_text"}
)


def _segment_memory_query_text_is_eligible(text: Any) -> bool:
    return (
        isinstance(text, str)
        and bool(text.strip())
        and len(text) <= _SEGMENT_MEMORY_QUERY_CHARS
        and sum(character.isalnum() for character in text) >= 16
    )


_SAFE_MEMORY_FIELDS = frozenset(
    {
        "id",
        "source_text",
        "target_text",
        "source_evidence",
        "target_evidence",
        "alignment_group_ids",
        "provenance",
        "classification",
        "granularity",
        "scope_kind",
        "source_language",
        "target_language",
        "semantic_role",
        "structure_fingerprint",
    }
)
_SAFE_PROVENANCE_KEYS = frozenset(
    {
        "alignment_group_ids",
        "alignment_method",
        "alignment_confidence",
        "alignment_review_state",
        "audience",
        "confidence",
        "contract_version",
        "derivation",
        "document_context",
        "domain",
        "locale",
        "note",
        "notes",
        "quality",
        "reason",
        "register",
        "semantic_role",
        "style",
    }
)


def _project_provenance(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _project_provenance(item)
            for key, item in value.items()
            if key in _SAFE_PROVENANCE_KEYS
        }
    if isinstance(value, list):
        return [_project_provenance(item) for item in value]
    return value


def safe_memory_projection(memory: Mapping[str, Any]) -> dict[str, Any]:
    """Project verified memory evidence without leaking parent identities."""

    if not isinstance(memory, Mapping):
        raise BlockContractError("context-memory-result")
    if not _SAFE_MEMORY_FIELDS.issubset(memory):
        raise BlockContractError("context-memory-result")
    projection = {key: memory[key] for key in _SAFE_MEMORY_FIELDS}
    projection["provenance"] = _project_provenance(projection["provenance"])
    return projection


class ContextMemoryDegraded(BlockContractError):
    """Optional retrieval infrastructure is unavailable for one window."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__("context-memory-degraded")


def _memory_item(baseline: TranslationContextBaseline) -> Any | None:
    items = [
        item
        for item in cast(Any, baseline.items)
        if item.item_kind == "translation_memory"
    ]
    if len(items) > 1:
        raise BlockContractError("baseline-memory-selection")
    return items[0] if items else None


def contextual_memory_slot(
    baseline: TranslationContextBaseline,
    source_plan: TranslationSourcePlan,
    *,
    query_text: str,
    query_profile: Mapping[str, Any],
    top_k: int,
    backend: Any,
    require_memory: bool = False,
    session: Any | None = None,
) -> ContextSlot | None:
    """Resolve one baseline-pinned memory generation into a packet slot.

    The baseline's generation and historical active-pointer receipt are the
    only retrieval authority.  A newer active pointer is never substituted.
    A vector/backend failure omits this optional slot, or raises a typed
    failure when the caller explicitly requires memory.
    """

    baseline = rehydrate_context_baseline(baseline, source_plan)
    item = _memory_item(baseline)
    if item is None:
        return None
    if not isinstance(query_text, str) or not query_text.strip():
        raise BlockContractError("context-memory-query")
    if not isinstance(query_profile, Mapping):
        raise BlockContractError("context-memory-profile")
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 1:
        raise BlockContractError("context-memory-top-k")
    reference = item.reference_json
    if not isinstance(reference, Mapping):
        raise BlockContractError("baseline-memory-reference")
    pointer = reference.get("pointer")
    if not isinstance(pointer, Mapping):
        raise BlockContractError("baseline-memory-reference")
    try:
        response = search_pinned_embedding_generation(
            reference=cast(Mapping[str, Any], pointer),
            query_text=query_text,
            query_profile=cast(Mapping[str, Any], query_profile),
            backend=backend,
            top_k=top_k,
            classification="contextual_precedent",
            session=session,
        )
    except EmbeddingProjectionError as exc:
        # Baseline rehydration has already authenticated the relational
        # generation/pointer reference.  A missing or unavailable projection
        # is a typed optional degradation; receipt/scope/tamper failures stay
        # hard failures and cannot be turned into silent omission.
        if exc.code in {
            "embedding-unavailable",
            "embedding-vector-unavailable",
            "embedding-row-membership",
            "embedding-vector-witness",
        }:
            if require_memory:
                raise ContextMemoryDegraded(exc.code) from exc
            return None
        raise
    if response.status != "ready":
        reason = response.reason or "embedding-unavailable"
        if require_memory:
            raise ContextMemoryDegraded(reason)
        return None
    if not response.results:
        return None
    metadata = dict(response.metadata or {})
    expected_query_hash = sha256_text(query_text)
    expected_profile_hash = sha256_text(canonical_json(dict(query_profile)))
    if (
        metadata.get("query_hash") != expected_query_hash
        or metadata.get("query_profile_hash") != expected_profile_hash
        or metadata.get("generation_id") != reference["generation_id"]
        or metadata.get("generation_record_hash") != reference["generation_record_hash"]
        or metadata.get("pointer") != dict(pointer)
    ):
        raise BlockContractError("context-memory-provenance")
    pointer_data = metadata.get("pointer")
    if not isinstance(pointer_data, Mapping):
        raise BlockContractError("context-memory-provenance")
    matches: list[dict[str, Any]] = []
    embedding_ids: list[str] = []
    embedding_hashes: list[str] = []
    memory_ids: list[str] = []
    memory_hashes: list[str] = []
    for result in response.results:
        memory = result.get("memory")
        if not isinstance(memory, Mapping):
            raise BlockContractError("context-memory-result")
        if memory.get("classification") != "contextual_precedent":
            raise BlockContractError("context-memory-result")
        projected = safe_memory_projection(cast(Mapping[str, Any], memory))
        embedding_id = result.get("entry_id")
        embedding_hash = result.get("embedding_entry_record_hash")
        memory_hash = result.get("memory_entry_record_hash")
        if (
            not isinstance(embedding_id, str)
            or not isinstance(embedding_hash, str)
            or len(embedding_hash) != 64
            or not isinstance(memory_hash, str)
            or len(memory_hash) != 64
            or not isinstance(projected["id"], str)
            or not projected["id"]
        ):
            raise BlockContractError("context-memory-result")
        embedding_ids.append(embedding_id)
        embedding_hashes.append(embedding_hash)
        memory_ids.append(projected["id"])
        memory_hashes.append(memory_hash)
        matches.append(
            {
                "score": result.get("score"),
                "distance": result.get("distance"),
                "memory": projected,
            }
        )
    content = canonical_json(
        {
            "contract_version": "translation-memory-context-v1",
            "query_hash": metadata.get("query_hash"),
            "query_profile_hash": metadata.get("query_profile_hash"),
            "matches": matches,
        }
    )
    dependency = {
        "baseline_id": baseline.id,
        "baseline_hash": baseline.record_hash,
        "generation_id": reference["generation_id"],
        "generation_record_hash": reference["generation_record_hash"],
        "generation_state_receipt": reference["generation_state_receipt"],
        "pointer_version": pointer_data["version"],
        "pointer_receipt": pointer_data["pointer_receipt"],
        "query_hash": metadata.get("query_hash"),
        "query_profile_hash": metadata.get("query_profile_hash"),
        "top_k": top_k,
        "embedding_entry_ids": embedding_ids,
        "embedding_entry_record_hashes": embedding_hashes,
        "memory_entry_ids": memory_ids,
        "memory_entry_record_hashes": memory_hashes,
    }
    return ContextSlot(
        "translation_memory",
        content,
        {
            "resolver": "phase5-memory-window-v1",
            "baseline_id": baseline.id,
            "baseline_hash": baseline.record_hash,
            "generation_id": reference["generation_id"],
            "generation_record_hash": reference["generation_record_hash"],
            "pointer_receipt": pointer_data["pointer_receipt"],
            "pointer_reference": dict(pointer_data),
            "match_count": len(matches),
        },
        str(reference["generation_id"]),
        str(pointer_data["version"]),
        sha256_text(content),
        cast(Mapping[str, Any], dependency),
        required=require_memory,
    )


def contextual_segment_memory_slot(
    baseline: TranslationContextBaseline,
    source_plan: TranslationSourcePlan,
    *,
    query_segments: list[Mapping[str, Any]],
    query_profile: Mapping[str, Any],
    backend: Any,
    session: Any | None = None,
    _query_cache: MutableMapping[tuple[str, str, str, str], ContextSlot | None]
    | None = None,
) -> ContextSlot | None:
    """Resolve bounded, sentence-level queries against one frozen memory pin.

    Query text is passed to the embedding adapter by itself. Locator metadata is
    retained only in the resulting packet slot, where replay can authenticate
    each query against the exact writable source anchor.
    """

    if not isinstance(query_segments, list) or len(query_segments) > (
        _SEGMENT_MEMORY_QUERY_LIMIT
    ):
        raise BlockContractError("context-memory-query")
    if not isinstance(query_profile, Mapping):
        raise BlockContractError("context-memory-profile")
    profile = dict(query_profile)
    query_result_cache: MutableMapping[
        tuple[str, str, str, str], ContextSlot | None
    ] = _query_cache if _query_cache is not None else {}
    query_profile_hash = sha256_text(canonical_json(profile))
    normalized_queries: list[dict[str, Any]] = []
    distinct_query_texts: set[str] = set()
    for query in query_segments:
        if not isinstance(query, Mapping) or set(query) != {
            "block_id",
            "source_anchor_id",
            "source_sentence_order",
            "text",
        }:
            raise BlockContractError("context-memory-query")
        block_id = query.get("block_id")
        anchor_id = query.get("source_anchor_id")
        sentence_order = query.get("source_sentence_order")
        text = query.get("text")
        if (
            not isinstance(block_id, str)
            or not block_id
            or len(block_id) > 128
            or not isinstance(anchor_id, str)
            or not anchor_id
            or len(anchor_id) > 128
            or not isinstance(sentence_order, int)
            or isinstance(sentence_order, bool)
            or sentence_order < 1
            or not _segment_memory_query_text_is_eligible(text)
        ):
            raise BlockContractError("context-memory-query")
        distinct_query_texts.add(text)
        normalized_queries.append(
            {
                "block_id": block_id,
                "source_anchor_id": anchor_id,
                "source_sentence_order": sentence_order,
                "query_hash": sha256_text(text),
                "matches": [],
                "text": text,
            }
        )
    if len(distinct_query_texts) > _SEGMENT_MEMORY_QUERY_LIMIT:
        raise BlockContractError("context-memory-query")
    if not normalized_queries:
        return None

    baseline = rehydrate_context_baseline(baseline, source_plan)
    item = _memory_item(baseline)
    if item is None:
        return None

    dependency_template: dict[str, Any] | None = None
    provenance_template: dict[str, Any] | None = None
    first_slot: ContextSlot | None = None
    matches: list[dict[str, Any]] = []
    embedding_ids: list[str] = []
    embedding_hashes: list[str] = []
    memory_ids: list[str] = []
    memory_hashes: list[str] = []
    indices_by_memory_id: dict[str, int] = {}
    observations_by_memory_id: dict[str, tuple[str, str, str, Mapping[str, Any]]] = {}
    oversized_memory_ids: set[str] = set()

    def packet_material() -> dict[str, Any]:
        return {
            "contract_version": "translation-memory-context-v2",
            "match_kind": "semantic",
            "granularity": "alignment_group",
            "query_hash": "0" * 64,
            "query_profile_hash": query_profile_hash,
            "query_segments": [
                {key: value for key, value in query.items() if key != "text"}
                for query in normalized_queries
            ],
            "matches": matches,
        }

    for query in normalized_queries:
        text = cast(str, query["text"])
        cache_key = (baseline.id, baseline.record_hash, query_profile_hash, text)
        child = query_result_cache.get(cache_key)
        cache_hit = cache_key in query_result_cache
        if not cache_hit:
            child = contextual_memory_slot(
                baseline,
                source_plan,
                query_text=text,
                query_profile=profile,
                top_k=_SEGMENT_MEMORY_MATCHES_PER_QUERY,
                backend=backend,
                require_memory=False,
                session=session,
            )
            query_result_cache[cache_key] = child
        if child is None:
            continue
        if child.slot_type != "translation_memory" or not child.dependency:
            raise BlockContractError("context-memory-provenance")
        if first_slot is None:
            first_slot = child
        try:
            child_material = json.loads(child.content)
        except (TypeError, ValueError) as exc:
            raise BlockContractError("context-memory-provenance") from exc
        child_dependency = dict(child.dependency)
        if (
            not isinstance(child_material, dict)
            or child_material.get("contract_version") != "translation-memory-context-v1"
            or child_material.get("query_hash") != sha256_text(text)
            or child_material.get("query_profile_hash") != query_profile_hash
            or child_dependency.get("query_hash") != sha256_text(text)
            or child_dependency.get("query_profile_hash") != query_profile_hash
            or child_dependency.get("top_k") != _SEGMENT_MEMORY_MATCHES_PER_QUERY
        ):
            raise BlockContractError("context-memory-provenance")
        if dependency_template is None:
            dependency_template = child_dependency
            provenance_template = dict(child.provenance)
        else:
            for key in (
                "baseline_id",
                "baseline_hash",
                "generation_id",
                "generation_record_hash",
                "generation_state_receipt",
                "pointer_version",
                "pointer_receipt",
                "query_profile_hash",
                "top_k",
            ):
                if child_dependency.get(key) != dependency_template.get(key):
                    raise BlockContractError("context-memory-provenance")
            if child.provenance.get("pointer_reference") != (
                provenance_template or {}
            ).get("pointer_reference"):
                raise BlockContractError("context-memory-provenance")

        child_matches = child_material.get("matches")
        entry_ids = child_dependency.get("embedding_entry_ids")
        entry_hashes = child_dependency.get("embedding_entry_record_hashes")
        child_memory_ids = child_dependency.get("memory_entry_ids")
        child_memory_hashes = child_dependency.get("memory_entry_record_hashes")
        if (
            not isinstance(child_matches, list)
            or len(child_matches) > _SEGMENT_MEMORY_MATCHES_PER_QUERY
            or not isinstance(entry_ids, list)
            or not isinstance(entry_hashes, list)
            or not isinstance(child_memory_ids, list)
            or not isinstance(child_memory_hashes, list)
            or not (
                len(child_matches)
                == len(entry_ids)
                == len(entry_hashes)
                == len(child_memory_ids)
                == len(child_memory_hashes)
            )
        ):
            raise BlockContractError("context-memory-result")

        seen_for_query: set[str] = set()
        for child_match, embedding_id, embedding_hash, memory_id, memory_hash in zip(
            child_matches,
            entry_ids,
            entry_hashes,
            child_memory_ids,
            child_memory_hashes,
            strict=True,
        ):
            if not isinstance(child_match, Mapping) or set(child_match) != {
                "score",
                "distance",
                "memory",
            }:
                raise BlockContractError("context-memory-result")
            memory = child_match.get("memory")
            if not isinstance(memory, Mapping):
                raise BlockContractError("context-memory-result")
            projection = safe_memory_projection(memory)
            observed_id = projection.get("id")
            if (
                not isinstance(embedding_id, str)
                or not isinstance(embedding_hash, str)
                or len(embedding_hash) != 64
                or not isinstance(memory_id, str)
                or not isinstance(memory_hash, str)
                or len(memory_hash) != 64
                or observed_id != memory_id
            ):
                raise BlockContractError("context-memory-result")
            previous = observations_by_memory_id.get(memory_id)
            current = (embedding_id, embedding_hash, memory_hash, projection)
            if previous is not None and previous != current:
                raise BlockContractError("context-memory-provenance")
            observations_by_memory_id[memory_id] = current
            if memory_id in seen_for_query:
                continue
            seen_for_query.add(memory_id)

            match_index = indices_by_memory_id.get(memory_id)
            if match_index is None:
                if (
                    memory_id in oversized_memory_ids
                    or len(matches) >= _SEGMENT_MEMORY_MATCH_LIMIT
                ):
                    continue
                candidate_index = len(matches)
                matches.append({"memory": projection})
                embedding_ids.append(embedding_id)
                embedding_hashes.append(embedding_hash)
                memory_ids.append(memory_id)
                memory_hashes.append(memory_hash)
                query["matches"].append(
                    {
                        "match_index": candidate_index,
                        "score": child_match.get("score"),
                        "distance": child_match.get("distance"),
                    }
                )
                try:
                    candidate_bytes = len(
                        canonical_json(packet_material()).encode("utf-8")
                    )
                except (TypeError, ValueError) as exc:
                    raise BlockContractError("context-memory-result") from exc
                if candidate_bytes > _SEGMENT_MEMORY_SLOT_BYTES:
                    matches.pop()
                    embedding_ids.pop()
                    embedding_hashes.pop()
                    memory_ids.pop()
                    memory_hashes.pop()
                    query["matches"].pop()
                    oversized_memory_ids.add(memory_id)
                    continue
                indices_by_memory_id[memory_id] = candidate_index
                match_index = candidate_index
            elif not any(
                reference["match_index"] == match_index
                for reference in query["matches"]
            ):
                query["matches"].append(
                    {
                        "match_index": match_index,
                        "score": child_match.get("score"),
                        "distance": child_match.get("distance"),
                    }
                )
                try:
                    candidate_bytes = len(
                        canonical_json(packet_material()).encode("utf-8")
                    )
                except (TypeError, ValueError) as exc:
                    raise BlockContractError("context-memory-result") from exc
                if candidate_bytes > _SEGMENT_MEMORY_SLOT_BYTES:
                    query["matches"].pop()
                    continue
            if len(query["matches"]) > _SEGMENT_MEMORY_MATCHES_PER_QUERY:
                query["matches"].pop()

    if not matches or dependency_template is None or first_slot is None:
        return None
    query_hash_material = [
        {key: value for key, value in query.items() if key not in {"matches", "text"}}
        for query in normalized_queries
    ]
    query_hash = sha256_text(canonical_json(query_hash_material))
    material = packet_material()
    material["query_hash"] = query_hash
    content = canonical_json(material)
    if len(content.encode("utf-8")) > _SEGMENT_MEMORY_SLOT_BYTES:
        raise BlockContractError("context-memory-size")

    dependency = {
        "baseline_id": dependency_template["baseline_id"],
        "baseline_hash": dependency_template["baseline_hash"],
        "generation_id": dependency_template["generation_id"],
        "generation_record_hash": dependency_template["generation_record_hash"],
        "generation_state_receipt": dependency_template["generation_state_receipt"],
        "pointer_version": dependency_template["pointer_version"],
        "pointer_receipt": dependency_template["pointer_receipt"],
        "query_hash": query_hash,
        "query_profile_hash": query_profile_hash,
        "top_k": _SEGMENT_MEMORY_MATCHES_PER_QUERY,
        "embedding_entry_ids": embedding_ids,
        "embedding_entry_record_hashes": embedding_hashes,
        "memory_entry_ids": memory_ids,
        "memory_entry_record_hashes": memory_hashes,
    }
    provenance = dict(provenance_template or first_slot.provenance)
    provenance.update(
        {
            "resolver": "segment-memory-window-v1",
            "match_count": len(matches),
            "match_kind": "semantic",
            "granularity": "alignment_group",
        }
    )
    return ContextSlot(
        "translation_memory",
        content,
        provenance,
        first_slot.artifact_id,
        first_slot.artifact_version,
        sha256_text(content),
        cast(Mapping[str, Any], dependency),
        required=False,
    )


retrieve_contextual_memory = contextual_memory_slot


__all__ = [
    "ContextMemoryDegraded",
    "contextual_memory_slot",
    "contextual_segment_memory_slot",
    "retrieve_contextual_memory",
    "safe_memory_projection",
]
