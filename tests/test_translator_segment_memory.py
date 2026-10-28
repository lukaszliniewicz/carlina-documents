"""Bounded segment-query memory slots and retained-packet replay checks."""

from __future__ import annotations

import json
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from flask import Flask

translator_package = import_module("iarf_assistant.addons.translator")
_ROOT = translator_package.__name__
_adapters = import_module(f"{_ROOT}.context_adapters")
_runtime = import_module(f"{_ROOT}.block_workflow_runtime")
_window_persistence = import_module(f"{_ROOT}.window_persistence")
_models = import_module(f"{_ROOT}.models")
_contract = import_module(f"{_ROOT}.block_contract")

BlockContractError = _contract.BlockContractError
ContextSlot = import_module(f"{_ROOT}.window_contract").ContextSlot
canonical_json = _contract.canonical_json
sha256_text = _contract.sha256_text


@pytest.fixture
def app(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'segment-memory.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="segment-memory-test",
        TESTING=True,
    )
    translator_package.db.init_app(application)
    context = application.app_context()
    context.push()
    try:
        yield application
    finally:
        translator_package.db.session.remove()
        translator_package.db.engine.dispose()
        context.pop()


def _pointer():
    return {
        "owner_user_id": 7,
        "project_id": "project-1",
        "source_plan_id": "plan-1",
        "projection_kind": "memory",
        "generation_id": "generation-1",
        "generation_record_hash": "b" * 64,
        "generation_state_receipt": "c" * 64,
        "version": 1,
        "updated_at": "2026-09-01T00:00:00+00:00",
        "pointer_receipt": "d" * 64,
    }


def _baseline():
    pointer = _pointer()
    reference = {
        "pointer": pointer,
        "generation_id": pointer["generation_id"],
        "generation_record_hash": pointer["generation_record_hash"],
        "generation_state_receipt": pointer["generation_state_receipt"],
    }
    return SimpleNamespace(
        id="baseline-1",
        record_hash="a" * 64,
        operation_id="operation-1",
        source_plan_id="plan-1",
        project_id="project-1",
        owner_user_id=7,
        items=[
            SimpleNamespace(item_kind="translation_memory", reference_json=reference)
        ],
    )


def _memory(
    identifier: str,
    *,
    granularity: str = "sentence",
    source_evidence: list[dict[str, Any]] | None = None,
    target_evidence: list[dict[str, Any]] | None = None,
    source_text: str = "The complete source passage remains available.",
    target_text: str = "Pełny przekład pozostaje dostępny.",
) -> dict[str, Any]:
    return {
        "id": identifier,
        "source_text": source_text,
        "target_text": target_text,
        "source_evidence": source_evidence or [{"anchor_id": f"{identifier}-source"}],
        "target_evidence": target_evidence or [{"span_id": f"{identifier}-target"}],
        "alignment_group_ids": [f"group-{identifier}"],
        "provenance": {"alignment_method": "reviewed"},
        "classification": "contextual_precedent",
        "granularity": granularity,
        "scope_kind": "project",
        "source_language": "en",
        "target_language": "pl",
        "semantic_role": "body",
        "structure_fingerprint": "f" * 64,
    }


def _child_slot(
    text: str,
    query_profile: dict[str, Any],
    memories: list[tuple[dict[str, Any], float, float]],
) -> ContextSlot:
    query_hash = sha256_text(text)
    profile_hash = sha256_text(canonical_json(query_profile))
    matches = []
    embedding_ids = []
    embedding_hashes = []
    memory_ids = []
    memory_hashes = []
    for index, (memory, score, distance) in enumerate(memories, start=1):
        matches.append(
            {
                "score": score,
                "distance": distance,
                "memory": _adapters.safe_memory_projection(memory),
            }
        )
        embedding_ids.append(f"embedding-{memory['id']}")
        embedding_hashes.append(str(index) * 64)
        memory_ids.append(memory["id"])
        memory_hashes.append(chr(96 + index) * 64)
    content = canonical_json(
        {
            "contract_version": "translation-memory-context-v1",
            "query_hash": query_hash,
            "query_profile_hash": profile_hash,
            "matches": matches,
        }
    )
    dependency = {
        "baseline_id": "baseline-1",
        "baseline_hash": "a" * 64,
        "generation_id": "generation-1",
        "generation_record_hash": "b" * 64,
        "generation_state_receipt": "c" * 64,
        "pointer_version": 1,
        "pointer_receipt": "d" * 64,
        "query_hash": query_hash,
        "query_profile_hash": profile_hash,
        "top_k": 2,
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
            "baseline_id": "baseline-1",
            "baseline_hash": "a" * 64,
            "generation_id": "generation-1",
            "generation_record_hash": "b" * 64,
            "pointer_receipt": "d" * 64,
            "pointer_reference": _pointer(),
            "match_count": len(matches),
        },
        "generation-1",
        "1",
        sha256_text(content),
        dependency,
    )


def _query(
    block_id: str,
    anchor_id: str,
    order: int,
    text: str,
) -> dict[str, Any]:
    return {
        "block_id": block_id,
        "source_anchor_id": anchor_id,
        "source_sentence_order": order,
        "text": text,
    }


def test_segment_queries_deduplicate_natural_text_and_map_complete_groups(monkeypatch):
    baseline = _baseline()
    profile = {
        "contract_version": "translation-memory-query-v2",
        "embedding_projection": {"projection_kind": "memory", "dimensions": 4},
    }
    one_to_many = _memory(
        "memory-one-to-many",
        granularity="block",
        source_evidence=[{"anchor_id": "source-1"}],
        target_evidence=[{"span_id": "target-1"}, {"span_id": "target-2"}],
    )
    many_to_one = _memory(
        "memory-many-to-one",
        granularity="block",
        source_evidence=[{"anchor_id": "source-2"}, {"anchor_id": "source-3"}],
        target_evidence=[{"span_id": "target-3"}],
    )
    third = _memory("memory-third")
    calls: list[tuple[str, int, dict[str, Any]]] = []

    def contextual(_baseline, _plan, **kwargs):
        text = kwargs["query_text"]
        calls.append((text, kwargs["top_k"], dict(kwargs["query_profile"])))
        results = {
            "A sentence repeated in two distinct source locations.": [
                (one_to_many, 0.91, 0.09),
                (many_to_one, 0.83, 0.17),
            ],
            "A later sentence has different natural source wording.": [
                (one_to_many, 0.72, 0.28),
                (third, 0.62, 0.38),
            ],
        }
        return _child_slot(text, profile, results[text])

    monkeypatch.setattr(
        _adapters, "rehydrate_context_baseline", lambda value, *_a, **_k: value
    )
    monkeypatch.setattr(_adapters, "contextual_memory_slot", contextual)
    repeated = "A sentence repeated in two distinct source locations."
    later = "A later sentence has different natural source wording."
    slot = _adapters.contextual_segment_memory_slot(
        baseline,
        SimpleNamespace(id="plan-1"),
        query_segments=[
            _query("block-1", "anchor-1", 1, repeated),
            _query("block-2", "anchor-2", 1, repeated),
            _query("block-3", "anchor-3", 1, later),
        ],
        query_profile=profile,
        backend=object(),
    )

    assert slot is not None
    assert [call[0] for call in calls] == [repeated, later]
    assert all(call[1] == 2 for call in calls)
    assert all(call[2] == profile for call in calls)
    material = json.loads(slot.content)
    assert material["contract_version"] == "translation-memory-context-v2"
    assert material["match_kind"] == "semantic"
    assert material["granularity"] == "alignment_group"
    assert [row["matches"] for row in material["query_segments"]] == [
        [
            {"match_index": 0, "score": 0.91, "distance": 0.09},
            {"match_index": 1, "score": 0.83, "distance": 0.17},
        ],
        [
            {"match_index": 0, "score": 0.91, "distance": 0.09},
            {"match_index": 1, "score": 0.83, "distance": 0.17},
        ],
        [
            {"match_index": 0, "score": 0.72, "distance": 0.28},
            {"match_index": 2, "score": 0.62, "distance": 0.38},
        ],
    ]
    assert material["query_segments"][0]["block_id"] == "block-1"
    assert material["query_segments"][1]["source_anchor_id"] == "anchor-2"
    assert [match["memory"]["id"] for match in material["matches"]] == [
        "memory-one-to-many",
        "memory-many-to-one",
        "memory-third",
    ]
    assert material["matches"][0]["memory"]["granularity"] == "block"
    assert material["matches"][0]["memory"]["target_evidence"] == [
        {"span_id": "target-1"},
        {"span_id": "target-2"},
    ]
    assert material["matches"][1]["memory"]["source_evidence"] == [
        {"anchor_id": "source-2"},
        {"anchor_id": "source-3"},
    ]
    assert set(material["matches"][0]) == {"memory"}
    assert set(slot.dependency) == {
        "baseline_id",
        "baseline_hash",
        "generation_id",
        "generation_record_hash",
        "generation_state_receipt",
        "pointer_version",
        "pointer_receipt",
        "query_hash",
        "query_profile_hash",
        "top_k",
        "embedding_entry_ids",
        "embedding_entry_record_hashes",
        "memory_entry_ids",
        "memory_entry_record_hashes",
    }
    assert slot.dependency["memory_entry_ids"] == [
        match["memory"]["id"] for match in material["matches"]
    ]
    assert len(slot.content.encode("utf-8")) <= 8_000


def test_segment_memory_caps_distinct_queries_matches_and_whole_result_bytes(
    monkeypatch,
):
    baseline = _baseline()
    profile = {"contract_version": "translation-memory-query-v2"}
    monkeypatch.setattr(
        _adapters, "rehydrate_context_baseline", lambda value, *_a, **_k: value
    )
    calls = []

    def contextual(_baseline, _plan, **kwargs):
        calls.append(kwargs["query_text"])
        index = len(calls)
        return _child_slot(
            kwargs["query_text"],
            profile,
            [
                (_memory(f"memory-{index}-a"), 0.9, 0.1),
                (_memory(f"memory-{index}-b"), 0.8, 0.2),
            ],
        )

    monkeypatch.setattr(_adapters, "contextual_memory_slot", contextual)
    queries = [
        _query(
            f"block-{index}",
            f"anchor-{index}",
            1,
            f"This distinct natural sentence number {index} has enough letters.",
        )
        for index in range(1, 7)
    ]
    slot = _adapters.contextual_segment_memory_slot(
        baseline,
        SimpleNamespace(id="plan-1"),
        query_segments=queries,
        query_profile=profile,
        backend=object(),
    )
    assert slot is not None
    assert len(calls) == 6
    material = json.loads(slot.content)
    assert len(material["query_segments"]) == 6
    assert len(material["matches"]) == 6
    assert all(len(row["matches"]) <= 2 for row in material["query_segments"])
    assert len(slot.dependency["memory_entry_ids"]) == 6

    with pytest.raises(BlockContractError, match="context-memory-query"):
        _adapters.contextual_segment_memory_slot(
            baseline,
            SimpleNamespace(id="plan-1"),
            query_segments=queries
            + [
                _query(
                    "block-7",
                    "anchor-7",
                    1,
                    "A seventh eligible sentence with enough words.",
                )
            ],
            query_profile=profile,
            backend=object(),
        )

    oversized = _memory("oversized", source_text="S" * 7_000, target_text="T" * 3_000)
    monkeypatch.setattr(
        _adapters,
        "contextual_memory_slot",
        lambda _baseline, _plan, **kwargs: _child_slot(
            kwargs["query_text"], profile, [(oversized, 0.9, 0.1)]
        ),
    )
    assert (
        _adapters.contextual_segment_memory_slot(
            baseline,
            SimpleNamespace(id="plan-1"),
            query_segments=queries[:1],
            query_profile=profile,
            backend=object(),
        )
        is None
    )


def test_segment_memory_rejects_conflicting_duplicate_memory_witnesses(monkeypatch):
    baseline = _baseline()
    profile = {"contract_version": "translation-memory-query-v2"}
    first = _memory("same-memory", source_text="Original complete source.")
    changed = _memory("same-memory", source_text="Conflicting source projection.")
    monkeypatch.setattr(
        _adapters, "rehydrate_context_baseline", lambda value, *_a, **_k: value
    )

    def contextual(_baseline, _plan, **kwargs):
        memory = first if kwargs["query_text"].startswith("First") else changed
        return _child_slot(kwargs["query_text"], profile, [(memory, 0.9, 0.1)])

    monkeypatch.setattr(_adapters, "contextual_memory_slot", contextual)
    with pytest.raises(BlockContractError, match="context-memory-provenance"):
        _adapters.contextual_segment_memory_slot(
            baseline,
            SimpleNamespace(id="plan-1"),
            query_segments=[
                _query(
                    "block-1",
                    "anchor-1",
                    1,
                    "First query has at least sixteen letters.",
                ),
                _query(
                    "block-2",
                    "anchor-2",
                    1,
                    "Second query has at least sixteen letters.",
                ),
            ],
            query_profile=profile,
            backend=object(),
        )


def test_legacy_memory_slot_keeps_exact_v1_content_bytes(monkeypatch):
    baseline = _baseline()
    memory = _memory("legacy-memory")
    query_text = canonical_json(
        [{"block_id": "block-1", "projection": "The complete legacy block."}]
    )
    query_profile = {
        "contract_version": "translation-memory-query-v1",
        "embedding_projection": {"projection_kind": "memory"},
    }
    query_hash = sha256_text(query_text)
    query_profile_hash = sha256_text(canonical_json(query_profile))
    pointer = baseline.items[0].reference_json["pointer"]
    response = SimpleNamespace(
        status="ready",
        reason=None,
        metadata={
            "query_hash": query_hash,
            "query_profile_hash": query_profile_hash,
            "generation_id": "generation-1",
            "generation_record_hash": "b" * 64,
            "pointer": pointer,
        },
        results=[
            {
                "score": 0.9,
                "distance": 0.1,
                "memory": memory,
                "entry_id": "embedding-legacy",
                "embedding_entry_record_hash": "e" * 64,
                "memory_entry_record_hash": "f" * 64,
            }
        ],
    )
    monkeypatch.setattr(
        _adapters, "rehydrate_context_baseline", lambda value, *_a, **_k: value
    )
    monkeypatch.setattr(
        _adapters,
        "search_pinned_embedding_generation",
        lambda **_kwargs: response,
    )

    slot = _adapters.contextual_memory_slot(
        baseline,
        SimpleNamespace(id="plan-1"),
        query_text=query_text,
        query_profile=query_profile,
        top_k=5,
        backend=object(),
    )

    assert slot is not None
    assert slot.content == canonical_json(
        {
            "contract_version": "translation-memory-context-v1",
            "query_hash": query_hash,
            "query_profile_hash": query_profile_hash,
            "matches": [
                {
                    "score": 0.9,
                    "distance": 0.1,
                    "memory": _adapters.safe_memory_projection(memory),
                }
            ],
        }
    )


def _runtime_memory_inputs(monkeypatch, config):
    baseline = _baseline()
    pointer = _pointer()
    generation = SimpleNamespace(
        owner_user_id=7,
        model_record_id="model-record-1",
        model_id="embedding-model",
        provider_id="provider-1",
        api_key_id="key-1",
        dimensions=4,
        task_type="similarity",
        signature_hash="e" * 64,
        namespace="memory-space",
        id="generation-1",
        record_hash="b" * 64,
    )
    baseline.items[0].reference_json.update(
        {
            "model_record_id": generation.model_record_id,
            "model_id": generation.model_id,
            "provider_id": generation.provider_id,
        }
    )
    model = SimpleNamespace(id=generation.model_record_id)
    monkeypatch.setattr(
        _runtime,
        "verify_pinned_embedding_reference",
        lambda candidate: (
            (generation, SimpleNamespace())
            if candidate == pointer
            else pytest.fail("unexpected pointer")
        ),
    )
    monkeypatch.setattr(
        _runtime,
        "TranslatorDenseVectorStore",
        lambda **_kwargs: SimpleNamespace(namespace=None),
    )
    monkeypatch.setattr(
        _runtime.db.session,
        "get",
        lambda model_type, identifier, **_kwargs: (
            model
            if model_type is _runtime.LLMModel and identifier == model.id
            else None
        ),
    )
    return baseline, pointer, generation


def test_managed_v2_selection_uses_eligible_anchors_and_legacy_callback_bytes(
    monkeypatch, app
):
    projection = {"projection_kind": "memory", "dimensions": 4}
    config = {
        "embedding_projection": projection,
        "translation_context": {
            "contract_version": "translation-context-v2",
            "continuity_mode": "retained",
        },
    }
    baseline, _pointer_value, _generation = _runtime_memory_inputs(monkeypatch, config)
    observed: dict[str, Any] = {}

    def segment_slot(_baseline, _plan, **kwargs):
        observed.update(kwargs)
        return "v2-slot"

    monkeypatch.setattr(_runtime, "contextual_segment_memory_slot", segment_slot)
    text_a = "First source sentence has enough natural language characters."
    text_a2 = "Second source sentence has enough natural language characters."
    text_b = "A note sentence has enough natural language characters."
    text_c = "A table cell sentence has enough natural language characters."

    def anchor(identifier, order, text):
        return SimpleNamespace(id=identifier, order=order, start=0, end=len(text))

    blocks = (
        SimpleNamespace(
            id="block-table",
            order=4,
            role="table_cell",
            projection=text_c,
            anchors=(anchor("anchor-c", 1, text_c),),
        ),
        SimpleNamespace(
            id="block-accessibility",
            order=5,
            role="accessibility_text",
            projection=text_b,
            anchors=(anchor("anchor-x", 1, text_b),),
        ),
        SimpleNamespace(
            id="block-note",
            order=3,
            role="note_body",
            projection=text_b,
            anchors=(anchor("anchor-b", 1, text_b),),
        ),
        SimpleNamespace(
            id="block-heading",
            order=2,
            role="heading",
            projection=text_a,
            anchors=(anchor("anchor-h", 1, text_a),),
        ),
        SimpleNamespace(
            id="block-paragraph",
            order=1,
            role="paragraph",
            projection=f"{text_a}{text_a2}",
            anchors=(
                anchor("anchor-a1", 1, text_a),
                SimpleNamespace(
                    id="anchor-a2",
                    order=2,
                    start=len(text_a),
                    end=len(text_a) + len(text_a2),
                ),
            ),
        ),
    )
    callback = _runtime._pinned_memory_callback(
        baseline=baseline,
        source_plan=SimpleNamespace(id="plan-1"),
        config=config,
    )
    assert callback is not None
    assert callback(blocks) == "v2-slot"
    selected = observed["query_segments"]
    assert [(row["block_id"], row["source_anchor_id"]) for row in selected] == [
        ("block-paragraph", "anchor-a1"),
        ("block-note", "anchor-b"),
        ("block-table", "anchor-c"),
        ("block-paragraph", "anchor-a2"),
    ]
    assert [row["text"] for row in selected] == [text_a, text_b, text_c, text_a2]
    assert observed["query_profile"] == {
        "contract_version": "translation-memory-query-v2",
        "embedding_projection": projection,
    }
    assert observed["session"] is _runtime.db.session
    assert len(observed["_query_cache"]) == 0

    legacy_config = {"embedding_projection": projection}
    legacy_baseline, _pointer_value, _generation = _runtime_memory_inputs(
        monkeypatch, legacy_config
    )
    legacy_seen = {}

    def legacy_slot(_baseline, _plan, **kwargs):
        legacy_seen.update(kwargs)
        return "legacy-slot"

    monkeypatch.setattr(_runtime, "contextual_memory_slot", legacy_slot)
    legacy = _runtime._pinned_memory_callback(
        baseline=legacy_baseline,
        source_plan=SimpleNamespace(id="plan-1"),
        config=legacy_config,
    )
    assert legacy is not None
    assert legacy((blocks[-1],)) == "legacy-slot"
    assert legacy_seen["query_text"] == canonical_json(
        [{"block_id": "block-paragraph", "projection": blocks[-1].projection}]
    )
    assert legacy_seen["query_profile"] == {
        "contract_version": "translation-memory-query-v1",
        "embedding_projection": projection,
    }
    assert legacy_seen["top_k"] == 5


def test_passive_v2_memory_path_never_builds_embedding_backend(monkeypatch):
    monkeypatch.setattr(_runtime, "passive_execution_active", lambda: True)
    monkeypatch.setattr(
        _runtime,
        "verify_pinned_embedding_reference",
        lambda *_args, **_kwargs: pytest.fail(
            "passive path consulted embedding memory"
        ),
    )
    callback = _runtime._pinned_memory_callback(
        baseline=_baseline(),
        source_plan=SimpleNamespace(id="plan-1"),
        config={
            "translation_context": {
                "contract_version": "translation-context-v2",
                "continuity_mode": "self_contained",
            }
        },
    )
    assert callback is None


def _valid_v2_replay_fixture():
    source_a = "The first exact sentence identifies the query anchor."
    source_b = "The neighboring sentence has separate source identity."
    projection = f"{source_a}{source_b}"
    block = SimpleNamespace(
        id="block-1",
        role="paragraph",
        projection=projection,
        anchors=(
            SimpleNamespace(
                id="anchor-a",
                order=1,
                start=0,
                end=len(source_a),
                source_hash=sha256_text(source_a),
            ),
            SimpleNamespace(
                id="anchor-b",
                order=2,
                start=len(source_a),
                end=len(projection),
                source_hash=sha256_text(source_b),
            ),
        ),
    )
    rows = [
        {
            "block_id": block.id,
            "source_anchor_id": "anchor-a",
            "source_sentence_order": 1,
            "query_hash": sha256_text(source_a),
            "matches": [{"match_index": 0, "score": 0.91, "distance": 0.09}],
        },
        {
            "block_id": block.id,
            "source_anchor_id": "anchor-b",
            "source_sentence_order": 2,
            "query_hash": sha256_text(source_b),
            "matches": [{"match_index": 0, "score": 0.72, "distance": 0.28}],
        },
    ]
    aggregate_query_hash = sha256_text(
        canonical_json(
            [
                {key: value for key, value in row.items() if key != "matches"}
                for row in rows
            ]
        )
    )
    memory = _memory("memory-1", granularity="block")
    match = {"memory": _adapters.safe_memory_projection(memory)}
    profile_hash = "9" * 64
    material = {
        "contract_version": "translation-memory-context-v2",
        "match_kind": "semantic",
        "granularity": "alignment_group",
        "query_hash": aggregate_query_hash,
        "query_profile_hash": profile_hash,
        "query_segments": rows,
        "matches": [match],
    }
    content = canonical_json(material)
    pointer = _pointer()
    dependency = {
        "baseline_id": "baseline-1",
        "baseline_hash": "a" * 64,
        "generation_id": "generation-1",
        "generation_record_hash": "b" * 64,
        "generation_state_receipt": "c" * 64,
        "pointer_version": 1,
        "pointer_receipt": "d" * 64,
        "query_hash": aggregate_query_hash,
        "query_profile_hash": profile_hash,
        "top_k": 2,
        "embedding_entry_ids": ["embedding-memory-1"],
        "embedding_entry_record_hashes": ["1" * 64],
        "memory_entry_ids": ["memory-1"],
        "memory_entry_record_hashes": ["a" * 64],
    }
    slot = ContextSlot(
        "translation_memory",
        content,
        {"pointer_reference": pointer},
        "generation-1",
        "1",
        sha256_text(content),
        dependency,
    )
    baseline = SimpleNamespace(
        id="baseline-1",
        record_hash="a" * 64,
        operation_id="operation-1",
        source_plan_id="plan-1",
        project_id="project-1",
        owner_user_id=7,
        items=[
            SimpleNamespace(
                item_kind="translation_memory",
                reference_json={
                    "pointer": pointer,
                    "generation_id": "generation-1",
                    "generation_record_hash": "b" * 64,
                    "generation_state_receipt": "c" * 64,
                },
            )
        ],
    )
    embedding_entry = SimpleNamespace(
        id="embedding-memory-1",
        subject_type="memory",
        subject_id="memory-1",
        record_hash="1" * 64,
    )
    generation = SimpleNamespace(
        id="generation-1", record_hash="b" * 64, entries=[embedding_entry]
    )
    memory_row = SimpleNamespace(id="memory-1", record_hash="a" * 64)
    evidence = memory
    attempt = SimpleNamespace(
        owner_user_id=7,
        project_id="project-1",
        operation_id="operation-1",
        block_ids_json=["block-1"],
    )
    record = SimpleNamespace(id="plan-1", project_id="project-1")
    plan = SimpleNamespace(blocks=(block,))
    return {
        "source_a": source_a,
        "source_b": source_b,
        "slot": slot,
        "material": material,
        "dependency": dependency,
        "pointer": pointer,
        "baseline": baseline,
        "generation": generation,
        "memory_row": memory_row,
        "evidence": evidence,
        "attempt": attempt,
        "record": record,
        "plan": plan,
    }


def _install_replay_fakes(monkeypatch, fixture):
    baseline = fixture["baseline"]
    generation = fixture["generation"]
    memory_row = fixture["memory_row"]

    def get(model, identifier, **_kwargs):
        if model is _models.TranslationContextBaseline and identifier == baseline.id:
            return baseline
        if model is _models.TranslationMemoryEntry and identifier == memory_row.id:
            return memory_row
        return None

    monkeypatch.setattr(_window_persistence.db.session, "get", get)
    monkeypatch.setattr(
        _window_persistence,
        "rehydrate_context_baseline",
        lambda value, *_args, **_kwargs: value,
    )
    monkeypatch.setattr(
        _window_persistence,
        "verify_pinned_embedding_reference",
        lambda _pointer: (
            generation,
            {"generation_state_receipt": "c" * 64},
        ),
    )
    monkeypatch.setattr(
        _window_persistence,
        "rehydrate_translation_memory_entry",
        lambda *_args, **_kwargs: fixture["evidence"],
    )


def test_v2_packet_rehydrates_without_embedding_backend_calls(monkeypatch, app):
    fixture = _valid_v2_replay_fixture()
    _install_replay_fakes(monkeypatch, fixture)
    _window_persistence._verify_research_and_memory_dependencies(
        fixture["record"],
        fixture["attempt"],
        [fixture["slot"]],
        verified_plan=fixture["plan"],
    )
    assert [
        query_match["score"]
        for row in json.loads(fixture["slot"].content)["query_segments"]
        for query_match in row["matches"]
    ] == [0.91, 0.72]


@pytest.mark.parametrize(
    "tamper",
    [
        "locator",
        "neighbor-hash",
        "generation",
        "memory-hash",
        "index",
        "reference-score",
        "oversize",
    ],
)
def test_v2_replay_rejects_tampered_segment_or_witness(monkeypatch, app, tamper):
    fixture = _valid_v2_replay_fixture()
    _install_replay_fakes(monkeypatch, fixture)
    slot = fixture["slot"]
    material = json.loads(slot.content)
    dependency = dict(slot.dependency)
    if tamper == "locator":
        material["query_segments"][0]["source_anchor_id"] = "unknown-anchor"
    elif tamper == "neighbor-hash":
        material["query_segments"][0]["query_hash"] = sha256_text(fixture["source_b"])
        aggregate = sha256_text(
            canonical_json(
                [
                    {key: value for key, value in row.items() if key != "matches"}
                    for row in material["query_segments"]
                ]
            )
        )
        material["query_hash"] = aggregate
        dependency["query_hash"] = aggregate
    elif tamper == "generation":
        dependency["generation_id"] = "another-generation"
    elif tamper == "memory-hash":
        dependency["memory_entry_record_hashes"] = ["f" * 64]
    elif tamper == "index":
        material["query_segments"][0]["matches"][0]["match_index"] = True
    elif tamper == "reference-score":
        material["query_segments"][0]["matches"][0]["score"] = "0.91"
    else:
        material["matches"][0]["memory"]["source_text"] = "S" * 9_000
    content = canonical_json(material)
    if tamper not in {"generation", "memory-hash"}:
        dependency["query_hash"] = material["query_hash"]
    tampered_slot = ContextSlot(
        "translation_memory",
        content,
        dict(slot.provenance),
        slot.artifact_id,
        slot.artifact_version,
        sha256_text(content),
        dependency,
    )
    with pytest.raises(BlockContractError, match="window-memory-dependency"):
        _window_persistence._verify_research_and_memory_dependencies(
            fixture["record"],
            fixture["attempt"],
            [tampered_slot],
            verified_plan=fixture["plan"],
        )
