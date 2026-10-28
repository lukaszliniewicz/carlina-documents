"""Focused acceptance checks for authenticated Translator context adapters."""

from __future__ import annotations

from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask

translator_package = import_module("iarf" + "_assistant.addons.translator")
_ROOT = translator_package.__name__
_APPLICATION_ROOT = _ROOT.partition(".addons")[0]
_adapters = import_module(f"{_ROOT}.context_adapters")
_baselines = import_module(f"{_ROOT}.context_baselines")
_embedding = import_module(f"{_ROOT}.embedding_projection")
_research = import_module(f"{_ROOT}.research_persistence")
_encryption = import_module(f"{_APPLICATION_ROOT}.core.encryption")

BlockContractError = import_module(f"{_ROOT}.block_contract").BlockContractError
EmbeddingProjectionError = _embedding.EmbeddingProjectionError
EncryptionService = _encryption.EncryptionService
InstallationKeyStore = _encryption.InstallationKeyStore


@pytest.fixture
def app(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'context.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="context-test",
        TESTING=True,
    )
    translator_package.db.init_app(application)
    context = application.app_context()
    context.push()
    keys = InstallationKeyStore(tmp_path / "keys.json")
    keys.initialize()
    application.extensions["carlina_encryption"] = EncryptionService(keys)
    try:
        yield application
    finally:
        translator_package.db.session.remove()
        translator_package.db.engine.dispose()
        context.pop()


def _pointer_fixture():
    old_state_receipt = "a" * 64
    generation = SimpleNamespace(
        owner_user_id=7,
        project_id="project-1",
        source_plan_id="plan-1",
        projection_kind="memory",
        id="generation-a",
        record_hash="b" * 64,
        # Rotation changes this current lifecycle receipt to a retired-state
        # receipt; the historical pointer still carries the old active proof.
        state_receipt="c" * 64,
    )
    row = SimpleNamespace(
        owner_user_id=generation.owner_user_id,
        project_id=generation.project_id,
        source_plan_id=generation.source_plan_id,
        projection_kind=generation.projection_kind,
        generation_id=generation.id,
        generation_record_hash=generation.record_hash,
        generation_state_receipt=old_state_receipt,
        version=1,
        updated_at=datetime(2026, 8, 26, tzinfo=UTC),
    )
    material = _embedding._pointer_values(row)
    row.pointer_receipt = _embedding.private_witness(
        material, purpose=_embedding.POINTER_PURPOSE
    )
    return generation, _embedding._pointer_reference(row)


def _backend_generation():
    return SimpleNamespace(
        owner_user_id=7,
        model_record_id=11,
        model_id="embedding/model-a",
        provider_id=3,
        api_key_id=17,
        dimensions=768,
        task_type="similarity",
        query_prefix="query: ",
        passage_prefix="passage: ",
        profile_json={"prefix_style": "query_passage", "dimensions": 768},
        signature_hash="f" * 64,
    )


def test_backend_identity_must_match_pinned_generation():
    generation = _backend_generation()
    expected = _embedding._expected_backend_identity(generation)
    backend = SimpleNamespace(embedding_identity=lambda: dict(expected))

    _embedding._verify_backend_identity(backend, generation)

    for field, value in (
        ("owner_user_id", 99),
        ("model_id", "embedding/model-b"),
        ("api_key_id", 18),
        ("profile_hash", "0" * 64),
    ):
        wrong = dict(expected)
        wrong[field] = value
        bad_backend = SimpleNamespace(embedding_identity=lambda wrong=wrong: wrong)
        with pytest.raises(EmbeddingProjectionError, match="embedding-backend-identity"):
            _embedding._verify_backend_identity(bad_backend, generation)


def test_dense_backend_exposes_authenticated_identity(app, monkeypatch):
    generation = _backend_generation()
    model_record = SimpleNamespace(
        id=generation.model_record_id,
        model_id=generation.model_id,
        provider_id=generation.provider_id,
        prefix_style="query_passage",
        supports_mrl=True,
        custom_query_prefix=generation.query_prefix,
        custom_passage_prefix=generation.passage_prefix,
    )
    monkeypatch.setattr(
        _embedding,
        "_profile_snapshot",
        lambda *_args, **_kwargs: (generation.profile_json, {}),
    )
    backend = _embedding.TranslatorDenseVectorStore(
        user_id=generation.owner_user_id,
        model=generation.model_id,
        task_type=generation.task_type,
        dimensions=generation.dimensions,
        session=translator_package.db.session,
        model_record=model_record,
        expected_api_key_id=generation.api_key_id,
        expected_signature_hash=generation.signature_hash,
    )

    assert dict(backend.embedding_identity()) == _embedding._expected_backend_identity(
        generation
    )


def test_historical_pointer_receipt_survives_generation_rotation(app):
    generation, reference = _pointer_fixture()

    verified = _embedding._verify_pointer_reference(
        reference, generation=generation, session=translator_package.db.session
    )

    assert verified == reference
    assert reference["generation_state_receipt"] != generation.state_receipt


def test_baseline_research_selection_does_not_infer_retry_order():
    with pytest.raises(BlockContractError, match="baseline-research-selection"):
        _baselines.freeze_context_baseline(
            object(),
            object(),
            # A retry and its predecessor must be resolved by the caller; a
            # baseline never silently chooses whichever run sorts last.
            completed_research_runs=[object(), object()],
        )


def test_context_memory_model_drift_is_not_optional_degradation(monkeypatch):
    pointer = {
        "owner_user_id": 7,
        "project_id": "project-1",
        "source_plan_id": "plan-1",
        "projection_kind": "memory",
        "generation_id": "generation-a",
        "generation_record_hash": "b" * 64,
        "generation_state_receipt": "a" * 64,
        "version": 1,
        "updated_at": "2026-08-26T00:00:00+00:00",
        "pointer_receipt": "c" * 64,
    }
    baseline = SimpleNamespace(
        id="baseline-1",
        record_hash="d" * 64,
        items=[SimpleNamespace(item_kind="translation_memory", reference_json={"pointer": pointer})],
    )
    monkeypatch.setattr(_adapters, "rehydrate_context_baseline", lambda *_args: baseline)

    def fail_with_drift(**_kwargs):
        raise EmbeddingProjectionError("embedding-model-drift")

    monkeypatch.setattr(_adapters, "search_pinned_embedding_generation", fail_with_drift)
    with pytest.raises(EmbeddingProjectionError, match="embedding-model-drift"):
        _adapters.contextual_memory_slot(
            baseline,
            SimpleNamespace(id="plan-1"),
            query_text="context",
            query_profile={},
            top_k=1,
            backend=object(),
        )


def test_safe_memory_projection_omits_parent_identities():
    memory = {
        "id": "memory-1",
        "source_text": "source",
        "target_text": "target",
        "source_evidence": [],
        "target_evidence": [],
        "alignment_group_ids": ["group-1"],
        "provenance": {
            "source_plan_id": "plan-1",
            "project_id": "project-1",
            "alignment_method": "human",
            "quality": {
                "register": "formal",
                "operation_id": "operation-1",
            },
            "nested_unknown": {"user_id": 7},
        },
        "classification": "contextual_precedent",
        "granularity": "sentence",
        "scope_kind": "project",
        "source_language": "en",
        "target_language": "pl",
        "semantic_role": "body",
        "structure_fingerprint": "f" * 64,
        "owner_user_id": 7,
        "target_revision_id": "revision-1",
    }

    projected = _adapters.safe_memory_projection(memory)

    assert "owner_user_id" not in projected
    assert "project_id" not in projected
    assert "source_plan_id" not in projected["provenance"]
    assert projected["provenance"] == {
        "alignment_method": "human",
        "quality": {"register": "formal"},
    }


def test_completed_research_conclusion_rejects_partial_tool_graph(monkeypatch):
    run = SimpleNamespace(
        id="run-1",
        state="completed",
        source_plan_id="plan-1",
        run_record_hash="a" * 64,
        state_head_receipt="b" * 64,
    )
    turn = SimpleNamespace(
        id="turn-1",
        analysis_attempt_id="attempt-1",
        turn_record_hash="e" * 64,
    )
    attempt = SimpleNamespace(dispatch_state="completed")
    plan = SimpleNamespace(id="plan-1")
    monkeypatch.setattr(_research, "_fresh_run", lambda *_args: run)
    monkeypatch.setattr(
        _research,
        "rehydrate_research_run",
        lambda *_args, **_kwargs: {
            "turns": [turn],
            "tool_calls": [
                SimpleNamespace(
                    state="failed",
                    error_json={"reason": "provider_rejected"},
                    claimed_at=None,
                    operation_version=None,
                    queue_job_id=None,
                    operation_lease_expires_at=None,
                    lease_fence=None,
                    argument_state="parsed",
                )
            ],
        },
    )

    class Session:
        def get(self, model, identifier, **_kwargs):
            if identifier == "plan-1":
                return plan
            if identifier == "attempt-1":
                return attempt
            return None

        class _NoAutoflush:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        no_autoflush = _NoAutoflush()

    with pytest.raises(BlockContractError, match="research-not-admissible"):
        _research.rehydrate_completed_research_conclusion(run, session=Session())
