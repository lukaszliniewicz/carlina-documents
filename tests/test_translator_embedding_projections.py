"""Acceptance tests for replaceable Translator dense projections."""

from __future__ import annotations

import hashlib
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest
from content_storage_support import bind_content_policy
from flask import Flask
from sqlalchemy import text
from sqlalchemy.sql.dml import Update

from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION
from iarf_assistant.addons import translator as translator_package

_TRANSLATOR_ROOT = translator_package.__name__
_APPLICATION_ROOT = _TRANSLATOR_ROOT.partition(".addons")[0]
_block_contract = import_module(f"{_TRANSLATOR_ROOT}.block_contract")
_block_persistence = import_module(f"{_TRANSLATOR_ROOT}.block_persistence")
_block_planner = import_module(f"{_TRANSLATOR_ROOT}.block_planner")
_embedding_projection = import_module(f"{_TRANSLATOR_ROOT}.embedding_projection")
_translator_models = import_module(f"{_TRANSLATOR_ROOT}.models")
_encryption = import_module(f"{_APPLICATION_ROOT}.core.encryption")
_application_models = import_module(f"{_APPLICATION_ROOT}.models")

AttestedDocumentSnapshot = _block_contract.AttestedDocumentSnapshot
persist_source_plan = _block_persistence.persist_source_plan
source_attestation_for_project = _block_persistence.source_attestation_for_project
plan_document_blocks = _block_planner.plan_document_blocks
EmbeddingActivationConflict = _embedding_projection.EmbeddingActivationConflict
EmbeddingProjectionError = _embedding_projection.EmbeddingProjectionError
_validate_vectors = _embedding_projection._validate_vectors
activate_embedding_generation = _embedding_projection.activate_embedding_generation
build_embedding_generation = _embedding_projection.build_embedding_generation
prepare_embedding_generation = _embedding_projection.prepare_embedding_generation
search_embedding_generation = _embedding_projection.search_embedding_generation
TranslationEmbeddingActive = _translator_models.TranslationEmbeddingActive
TranslationEmbeddingEntry = _translator_models.TranslationEmbeddingEntry
TranslationProject = _translator_models.TranslationProject
EncryptionService = _encryption.EncryptionService
InstallationKeyStore = _encryption.InstallationKeyStore
ApiKey = _application_models.ApiKey
LLMModel = _application_models.LLMModel
LLMProvider = _application_models.LLMProvider
User = _application_models.User
db = translator_package.db


@pytest.fixture
def app(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'embedding.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="embedding-test",
        TESTING=True,
    )
    db.init_app(application)
    context = application.app_context()
    context.push()
    keys = InstallationKeyStore(tmp_path / "keys.json")
    keys.initialize()
    application.extensions["carlina_encryption"] = EncryptionService(keys)
    db.create_all()
    try:
        yield application
    finally:
        db.session.remove()
        db.drop_all()
        db.engine.dispose()
        context.pop()


class FakeBackend:
    def __init__(self, *, dimensions: int = 4):
        self.dimensions = dimensions
        self.rows: dict[str, dict[int, list[float]]] = {}
        self.calls: list[tuple[str, object]] = []
        self.fail = False
        self.result_override = None

    def create(self, *, namespace: str, dimensions: int):
        self.calls.append(("create", namespace))
        self.rows.setdefault(namespace, {})

    def embed_documents(self, texts, *, generation):
        self.calls.append(("embed", tuple(texts)))
        if self.fail:
            raise RuntimeError("provider failed")
        if self.result_override is not None:
            return self.result_override
        return [[float(index + 1)] * self.dimensions for index, _ in enumerate(texts)]

    def upsert(self, row_ids, embeddings, *, include_lexical_text=False):
        self.calls.append(("upsert", include_lexical_text))
        assert include_lexical_text is False
        namespace = self.calls[0][1]
        self.rows[str(namespace)] = dict(zip(row_ids, embeddings, strict=True))

    def row_ids(self, *, namespace):
        self.calls.append(("rows", namespace))
        return list(self.rows.get(namespace, {}))

    def vector_witness(self, *, namespace: str, row_ids: list[int], dimensions: int):
        import json

        payload = [
            {"row_id": int(row_id), "vector": self.rows[namespace][int(row_id)]}
            for row_id in sorted(row_ids)
        ]
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def search_semantic(
        self,
        query_text,
        *,
        namespace,
        dimensions,
        model,
        task_type,
        top_k,
        allowed_row_ids,
    ):
        self.calls.append(("search", tuple(allowed_row_ids)))
        values = [
            row_id
            for row_id in self.rows.get(namespace, {})
            if row_id in allowed_row_ids
        ]
        return [(row_id, 0.9, 0.1) for row_id in values[:top_k]]

    def drop(self, *, namespace, dimensions):
        self.calls.append(("drop", namespace))
        self.rows.pop(namespace, None)


def _project_and_plan(name: str = "Embedding"):
    owner = User(email=f"{name.lower()}@example.test", password_hash="x")
    db.session.add(owner)
    db.session.flush()
    provider = LLMProvider(name=f"provider-{name.lower()}", user_id=None)
    db.session.add(provider)
    db.session.flush()
    db.session.add(
        ApiKey(
            user_id=owner.id,
            provider_id=provider.id,
            name="embedding-test-key",
            api_key="synthetic-secret",
            api_base=None,
            is_active=True,
        )
    )
    model = LLMModel(
        user_id=owner.id,
        provider_id=provider.id,
        model_id=f"test/{name.lower()}-embedding",
        display_name="Test embedding",
        is_active=True,
        model_type="embedding",
        default_dimensions=4,
        supports_mrl=False,
        prefix_style="none",
        capability_overrides={"revision": "rev-1", "normalization": "unit"},
    )
    db.session.add(model)
    project = TranslationProject(
        user_id=owner.id,
        name=name,
        source_lang="en",
        target_lang="pl",
        engine_contract="document-block-v1",
        settings={
            "source_reference": {
                "version": 1,
                "capability": "document.processing",
                "provider_addon_id": "doc-provider",
                "provider_capability_id": "document-processing",
                "resource_type": "document",
                "resource_id": "doc-1",
            }
        },
    )
    db.session.add(project)
    db.session.flush()
    html = "<html><body><p>Hello. Bye.</p></body></html>"
    digest = hashlib.sha256(html.encode()).hexdigest()
    snapshot = AttestedDocumentSnapshot(
        id="doc-1",
        document_id="doc-1",
        output_html=html,
        content_hash=digest,
        semantic_content_hash=digest,
        metadata={},
        provenance={},
        captured_at="2026-08-26T12:00:00+00:00",
        semantic_contract_version=SEMANTIC_HTML_CONTRACT_VERSION,
    )
    planned = plan_document_blocks(
        snapshot,
        project_id=project.id,
        source_attestation=source_attestation_for_project(project),
        source_language="en",
    )
    record, _ = persist_source_plan(project, planned)
    db.session.commit()
    return project, record, model


def test_prepare_is_scoped_encrypted_and_signature_partitioned(app):
    project, plan, model = _project_and_plan()
    generation = prepare_embedding_generation(
        plan,
        projection_kind="block",
        model_id=model.model_id,
        session=db.session,
    )
    assert generation.signature_hash
    assert generation.signature_json["projection_kind"] == "block"
    assert generation.namespace.startswith("translator_emb_")
    assert TranslationEmbeddingEntry.query.count() == len(generation.entries)
    raw = db.session.execute(
        text("SELECT source_text FROM translation_embedding_entry")
    ).scalar_one()
    assert "Hello." not in str(raw)
    anchor_generation = prepare_embedding_generation(
        plan,
        projection_kind="anchor",
        model_id=model.model_id,
        session=db.session,
    )
    assert anchor_generation.signature_hash != generation.signature_hash
    assert anchor_generation.entry_count == sum(
        len(block.anchors) for block in plan.blocks
    )
    assert project.id == generation.project_id


def test_build_has_exact_cardinality_dense_only_and_cas_activation(app):
    _project, plan, model = _project_and_plan()
    generation = prepare_embedding_generation(
        plan, projection_kind="block", model_id=model.model_id
    )
    db.session.commit()
    backend = FakeBackend()
    built = build_embedding_generation(generation, backend=backend)
    assert built.status == "ready"
    assert ("upsert", False) in backend.calls
    assert not any(name == "fts" for name, _value in backend.calls)
    activated = activate_embedding_generation(
        built,
        expected_active_generation_id=None,
        backend=backend,
    )
    assert activated.status == "active"
    assert TranslationEmbeddingActive.query.count() == 1
    with pytest.raises(EmbeddingActivationConflict):
        activate_embedding_generation(
            built,
            expected_active_generation_id="stale",
            backend=backend,
        )


def test_failed_rebuild_does_not_disturb_previous_active(app):
    _project, plan, model = _project_and_plan()
    backend = FakeBackend()
    first = prepare_embedding_generation(
        plan, projection_kind="block", model_id=model.model_id
    )
    db.session.commit()
    first = build_embedding_generation(first, backend=backend)
    activate_embedding_generation(
        first, expected_active_generation_id=None, backend=backend
    )
    db.session.commit()
    backend.fail = True
    second = prepare_embedding_generation(
        plan, projection_kind="block", model_id=model.model_id
    )
    db.session.commit()
    second = build_embedding_generation(second, backend=backend)
    assert second.status == "failed"
    pointer = TranslationEmbeddingActive.query.one()
    assert pointer.generation_id == first.id


def test_live_model_drift_blocks_before_embedding_call(app):
    _project, plan, model = _project_and_plan()
    generation = prepare_embedding_generation(
        plan, projection_kind="block", model_id=model.model_id
    )
    db.session.commit()
    model.is_active = False
    db.session.flush()
    backend = FakeBackend()
    with pytest.raises(EmbeddingProjectionError, match="embedding-model-drift"):
        build_embedding_generation(generation, backend=backend)
    assert not any(name == "embed" for name, _value in backend.calls)


def test_search_uses_relational_allow_list_and_degrades_on_vector_loss(app):
    project, plan, model = _project_and_plan()
    backend = FakeBackend()
    generation = prepare_embedding_generation(
        plan, projection_kind="block", model_id=model.model_id
    )
    db.session.commit()
    generation = build_embedding_generation(generation, backend=backend)
    activate_embedding_generation(
        generation, expected_active_generation_id=None, backend=backend
    )
    db.session.commit()
    response = search_embedding_generation(
        owner_user_id=project.user_id,
        project_id=project.id,
        source_plan_id=plan.id,
        projection_kind="block",
        query_text="Hello",
        backend=backend,
    )
    assert response.status == "ready"
    assert response.results
    search_calls = [call for call in backend.calls if call[0] == "search"]
    assert search_calls[-1][1] == (1,)
    backend.rows[generation.namespace].clear()
    degraded = search_embedding_generation(
        owner_user_id=project.user_id,
        project_id=project.id,
        source_plan_id=plan.id,
        projection_kind="block",
        query_text="Hello",
        backend=backend,
    )
    assert degraded.status == "degraded"


def test_build_requires_durable_preparation_before_backend_io(app):
    _project, plan, model = _project_and_plan()
    generation = prepare_embedding_generation(
        plan, projection_kind="block", model_id=model.model_id
    )
    backend = FakeBackend()
    with pytest.raises(EmbeddingProjectionError, match="not-durable"):
        build_embedding_generation(generation, backend=backend)
    assert not backend.calls
    db.session.rollback()


@pytest.mark.parametrize(
    "override, expected",
    [
        ([], "cardinality"),
        ([[1.0, 2.0]], "dimension"),
        ([[float("nan")] * 4], "nonfinite"),
        ([[1e40] * 4], "nonfinite"),
    ],
)
def test_invalid_provider_vectors_authenticate_failed_generation(
    app, override, expected
):
    _project, plan, model = _project_and_plan()
    generation = prepare_embedding_generation(
        plan, projection_kind="block", model_id=model.model_id
    )
    db.session.commit()
    backend = FakeBackend()
    backend.result_override = override
    built = build_embedding_generation(generation, backend=backend)
    assert built.status == "failed"
    assert built.error_json["code"] == f"embedding_{expected}"
    assert built.state_receipt


def test_vector_mutation_degrades_search_and_blocks_activation(app):
    _project, plan, model = _project_and_plan()
    backend = FakeBackend()
    generation = prepare_embedding_generation(
        plan, projection_kind="block", model_id=model.model_id
    )
    db.session.commit()
    generation = build_embedding_generation(generation, backend=backend)
    backend.rows[generation.namespace][1][0] = 999.0
    with pytest.raises(EmbeddingProjectionError, match="vector-witness"):
        activate_embedding_generation(
            generation, expected_active_generation_id=None, backend=backend
        )
    db.session.rollback()


def test_relational_pointer_tamper_fails_closed(app):
    project, plan, model = _project_and_plan()
    backend = FakeBackend()
    generation = prepare_embedding_generation(
        plan, projection_kind="block", model_id=model.model_id
    )
    db.session.commit()
    generation = build_embedding_generation(generation, backend=backend)
    activate_embedding_generation(
        generation, expected_active_generation_id=None, backend=backend
    )
    db.session.commit()
    db.session.execute(
        text(
            "UPDATE translation_embedding_active "
            "SET generation_state_receipt = :receipt"
        ),
        {"receipt": "f" * 64},
    )
    db.session.commit()
    with pytest.raises(EmbeddingProjectionError, match="active-pointer"):
        search_embedding_generation(
            owner_user_id=project.user_id,
            project_id=project.id,
            source_plan_id=plan.id,
            projection_kind="block",
            query_text="Hello",
            backend=backend,
        )


def test_requested_wrong_model_type_fails_closed_without_fallback(app):
    _project, plan, model = _project_and_plan()
    model.model_type = "chat"
    db.session.flush()
    with pytest.raises(EmbeddingProjectionError, match="model-unavailable"):
        prepare_embedding_generation(
            plan, projection_kind="block", model_id=model.model_id
        )


def test_credential_content_drift_blocks_provider_io(app):
    _project, plan, model = _project_and_plan()
    generation = prepare_embedding_generation(
        plan, projection_kind="block", model_id=model.model_id
    )
    db.session.commit()
    key = ApiKey.query.one()
    key.api_key = "changed-secret"
    db.session.flush()
    backend = FakeBackend()
    with pytest.raises(EmbeddingProjectionError, match="credential-drift"):
        build_embedding_generation(generation, backend=backend)
    assert not backend.calls


def test_detached_generation_is_reloaded_before_build_and_replacement_activation(app):
    _project, plan, model = _project_and_plan()
    first_backend = FakeBackend()
    first = prepare_embedding_generation(
        plan, projection_kind="block", model_id=model.model_id
    )
    db.session.commit()
    first = build_embedding_generation(first, backend=first_backend)
    activate_embedding_generation(
        first, expected_active_generation_id=None, backend=first_backend
    )
    db.session.commit()

    second = prepare_embedding_generation(
        plan, projection_kind="block", model_id=model.model_id
    )
    db.session.commit()
    second_backend = FakeBackend()
    second = build_embedding_generation(second, backend=second_backend)
    db.session.expunge(second)
    activated = activate_embedding_generation(
        second,
        expected_active_generation_id=first.id,
        backend=second_backend,
    )
    assert activated.id == second.id
    assert TranslationEmbeddingActive.query.one().generation_id == second.id


def test_foreign_session_generation_is_reloaded_before_build(app):
    _project, plan, model = _project_and_plan()
    generation = prepare_embedding_generation(
        plan, projection_kind="block", model_id=model.model_id
    )
    db.session.commit()
    from sqlalchemy.orm import sessionmaker

    foreign_session = sessionmaker(bind=db.engine, expire_on_commit=False)()
    try:
        foreign_generation = foreign_session.get(
            type(generation), generation.id, populate_existing=True
        )
        assert foreign_generation is not None
        backend = FakeBackend()
        built = build_embedding_generation(
            foreign_generation, backend=backend, session=db.session
        )
        assert built.status == "ready"
    finally:
        foreign_session.close()


def test_provider_kind_drift_fails_before_provider_io(app):
    _project, plan, model = _project_and_plan()
    generation = prepare_embedding_generation(
        plan, projection_kind="block", model_id=model.model_id
    )
    db.session.commit()
    provider = db.session.get(LLMProvider, model.provider_id)
    provider.provider_kind = "vertex_ai"
    db.session.flush()
    backend = FakeBackend()
    with pytest.raises(EmbeddingProjectionError, match="model-drift"):
        build_embedding_generation(generation, backend=backend)
    assert not backend.calls


def test_float32_overflow_is_rejected_for_document_and_query_vectors(app):
    with pytest.raises(EmbeddingProjectionError, match="nonfinite"):
        _validate_vectors([[1e40] * 4], expected_count=1, dimensions=4)
    with pytest.raises(EmbeddingProjectionError, match="nonfinite"):
        _validate_vectors([[float("inf")] * 4], expected_count=1, dimensions=4)


def test_user_owned_provider_never_uses_global_credential(app):
    project, plan, model = _project_and_plan()
    provider = db.session.get(LLMProvider, model.provider_id)
    provider.user_id = project.user_id
    ApiKey.query.one().is_active = False
    db.session.add(
        ApiKey(
            user_id=None,
            provider_id=provider.id,
            name="forbidden-global-key",
            api_key="global-secret",
            is_active=True,
        )
    )
    db.session.flush()

    with pytest.raises(EmbeddingProjectionError, match="credential-unavailable"):
        prepare_embedding_generation(
            plan, projection_kind="block", model_id=model.model_id
        )


def test_cas_conflict_rolls_back_lifecycle_mutations(app, monkeypatch):
    _project, plan, model = _project_and_plan()
    backend = FakeBackend()
    first = prepare_embedding_generation(
        plan, projection_kind="block", model_id=model.model_id
    )
    db.session.commit()
    first = build_embedding_generation(first, backend=backend)
    activate_embedding_generation(
        first, expected_active_generation_id=None, backend=backend
    )
    db.session.commit()

    second = prepare_embedding_generation(
        plan, projection_kind="block", model_id=model.model_id
    )
    db.session.commit()
    second_backend = FakeBackend()
    second = build_embedding_generation(second, backend=second_backend)
    original_execute = db.session().execute

    def conflict_on_pointer_update(statement, *args, **kwargs):
        if isinstance(statement, Update):
            return SimpleNamespace(rowcount=0)
        return original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(db.session(), "execute", conflict_on_pointer_update)
    with pytest.raises(EmbeddingActivationConflict, match="cas-conflict"):
        activate_embedding_generation(
            second,
            expected_active_generation_id=first.id,
            backend=second_backend,
        )

    db.session.expire_all()
    assert db.session.get(type(first), first.id).status == "active"
    assert db.session.get(type(second), second.id).status == "ready"
