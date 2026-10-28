from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from content_storage_support import bind_content_policy
from flask import Flask
from flask_login import LoginManager

from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION
from iarf_assistant import models
from iarf_assistant.addons.translator import api_client
from iarf_assistant.addons.translator.block_contract import (
    DOCUMENT_BLOCK_ENGINE_CONTRACT,
    AttestedDocumentSnapshot,
    sha256_text,
)
from iarf_assistant.addons.translator.block_export import BlockExportError
from iarf_assistant.addons.translator.block_persistence import (
    persist_source_plan,
    source_attestation_for_project,
)
from iarf_assistant.addons.translator.block_planner import plan_document_blocks
from iarf_assistant.addons.translator.export_metadata import ExportMetadataError
from iarf_assistant.addons.translator.export_metadata_generation import (
    CONTRACT_VERSION,
    DRAFT_FIELDS,
    DRAFT_SCHEMA,
    merge_metadata_draft,
    prepare_metadata_draft,
)
from iarf_assistant.addons.translator.models import (
    TranslationOperation,
    TranslationProject,
)
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.extensions import db


@pytest.fixture
def app(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'metadata-generation.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="metadata-generation-test",
        TESTING=True,
    )
    db.init_app(application)
    login_manager = LoginManager()
    login_manager.init_app(application)

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(models.User, int(user_id))

    context = application.app_context()
    context.push()
    key_store = InstallationKeyStore(tmp_path / "master-keys.json")
    key_store.initialize()
    application.extensions["carlina_encryption"] = EncryptionService(key_store)
    db.create_all()
    try:
        yield application
    finally:
        db.session.remove()
        with db.engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            db.metadata.drop_all(connection)
        db.engine.dispose()
        context.pop()


def _user(email: str) -> models.User:
    user = models.User(email=email, password_hash="test")
    db.session.add(user)
    db.session.commit()
    return user


def _project(user: models.User, *, metadata: dict | None = None, version: int = 0):
    project = TranslationProject(
        user_id=user.id,
        name="Synthetic metadata generation project",
        source_lang="en",
        target_lang="pl",
        status="setup",
        engine_contract=DOCUMENT_BLOCK_ENGINE_CONTRACT,
        settings={
            "source_type": "document",
            "source_reference": {
                "version": 1,
                "capability": "document.processing",
                "provider_addon_id": "doc-provider",
                "provider_capability_id": "document-processing",
                "resource_type": "document",
                "resource_id": "doc-1",
            },
            "export_metadata": {} if metadata is None else metadata,
            "export_metadata_version": version,
        },
    )
    db.session.add(project)
    db.session.commit()
    return project


def _plan(project: TranslationProject, *, metadata: dict | None = None):
    html = (
        "<html><body><h1>Source title</h1>"
        "<p>First source paragraph with enough context.</p>"
        "<h2>First chapter</h2>"
        "<p>Second source paragraph.</p>"
        "<blockquote>Source quotation.</blockquote>"
        "</body></html>"
    )
    snapshot = AttestedDocumentSnapshot(
        id="doc-1",
        document_id="doc-1",
        output_html=html,
        content_hash=sha256_text(html),
        semantic_content_hash=sha256_text(html),
        metadata={} if metadata is None else metadata,
        provenance={"source": "synthetic"},
        filename="synthetic.html",
        captured_at="2026-09-01T12:00:00+00:00",
        semantic_contract_version=SEMANTIC_HTML_CONTRACT_VERSION,
    )
    planned = plan_document_blocks(
        snapshot,
        project_id=project.id,
        source_attestation=source_attestation_for_project(project),
        source_language="en",
    )
    record, _created = persist_source_plan(project, planned)
    db.session.commit()
    return planned, record


def _client(user_id: int) -> api_client.TranslatorClient:
    return api_client.TranslatorClient(
        SimpleNamespace(get_task_queue=lambda _addon_id: Mock()),
        {"current_user_id": user_id},
    )


def _valid_response(**overrides):
    response = {
        "title": "Translated title",
        "subtitle": "",
        "authors": ["Source Author"],
        "description": "A bounded description.",
        "subjects": ["History"],
    }
    response.update(overrides)
    return response


def _configured_generation(monkeypatch, response=None, *, error=None):
    workflow_config = __import__(
        "iarf_assistant.addons.translator.block_workflow_config",
        fromlist=["_model_identity", "_reasoning"],
    )
    provider_adapter = __import__(
        "iarf_assistant.addons.translator.block_provider_adapter",
        fromlist=["call_analysis"],
    )
    llm_service = __import__("iarf_assistant.llm_service", fromlist=["LLMService"])
    monkeypatch.setattr(
        workflow_config,
        "_model_identity",
        lambda _model_id, *, owner_user_id: {
            "model_id": "configured/model",
            "capability_profile": {
                "supports_structured_output": False,
                "structured_output_mode": None,
            },
            "owner_user_id": owner_user_id,
        },
    )
    monkeypatch.setattr(
        workflow_config,
        "_reasoning",
        lambda model_id, user, slot: {
            "model_id": model_id,
            "slot": slot,
            "addon_id": "translator",
            "config": None,
        },
    )
    calls = []

    def fake_call_analysis(service, *, phase, packet, action_id):
        calls.append((service, phase, packet, action_id))
        if error is not None:
            raise error
        return {"content": json.dumps(response or _valid_response())}

    monkeypatch.setattr(provider_adapter, "call_analysis", fake_call_analysis)
    monkeypatch.setattr(llm_service, "LLMService", lambda **kwargs: SimpleNamespace(**kwargs))
    return calls


def test_prepare_metadata_draft_bounds_context_and_includes_source_metadata(app):
    owner = _user("prepare@example.test")
    source_metadata = {
        "title": "T" * 900,
        "subtitle": "Source subtitle",
        "authors": ["A" * 500, "Source Author"],
        "author": "Source Author",
        "description": "D" * 4096,
        "abstract": "A" * 4096,
        "subjects": ["S" * 400, "History"],
        "keywords": ["K" * 400],
        "publisher": "Must not be exposed as source field",
    }
    project = _project(owner)
    _plan(project, metadata=source_metadata)

    packet = prepare_metadata_draft(
        project,
        metadata={},
        expected_version=4,
    )

    assert packet["contract_version"] == CONTRACT_VERSION
    assert packet["expected_version"] == 4
    assert packet["editable_fields"] == list(DRAFT_FIELDS)
    assert packet["response_schema"] == DRAFT_SCHEMA
    assert packet["response_schema"]["additionalProperties"] is False
    assert set(packet["response_schema"]["properties"]) == set(DRAFT_FIELDS)
    context = packet["context"]
    assert set(context) == {
        "source_language",
        "target_language",
        "source_metadata",
        "source_excerpts",
        "translated_headings",
        "coverage",
    }
    assert set(context["source_metadata"]) <= {
        "title",
        "subtitle",
        "authors",
        "author",
        "description",
        "abstract",
        "subjects",
        "keywords",
    }
    assert len(context["source_metadata"]["title"]) == 500
    assert len(context["source_metadata"]["description"]) == 4000
    assert len(context["source_metadata"]["authors"][0]) == 300
    assert len(context["source_metadata"]["subjects"][0]) == 300
    assert len(context["source_metadata"]["keywords"][0]) == 300
    assert len(json.dumps(context["source_metadata"], ensure_ascii=False).encode()) <= 7000
    assert context["source_excerpts"]
    assert len(context["source_excerpts"]) <= 7
    assert all(0 < len(item["text"]) <= 700 for item in context["source_excerpts"])
    assert all(set(item) == {"order", "role", "text", "truncated"} for item in context["source_excerpts"])
    assert context["coverage"] == "Selected excerpts only; not a whole-book analysis."
    assert "publisher" not in context["source_metadata"]


def test_prepare_metadata_draft_preserves_protected_values_and_is_immutable(app):
    owner = _user("protected@example.test")
    project = _project(
        owner,
        metadata={
            "title": "Existing title",
            "authors": ["Existing author"],
            "translators": ["Existing translator"],
            "editors": ["Existing editor"],
            "rights": "Existing rights",
            "identifiers": [{"scheme": "ISBN", "value": "123"}],
            "cover_upload_path": "/uploads/existing-cover.png",
        },
        version=7,
    )
    _plan(project, metadata={"title": "Source title", "authors": ["Source author"]})
    original_settings = copy.deepcopy(project.settings)

    packet = _client(owner.id).export_metadata_prepare(project.id)

    assert packet["metadata"] == original_settings["export_metadata"]
    assert packet["editable_fields"] == ["subtitle", "description", "subjects"]
    assert set(packet["metadata"]) == {
        "title",
        "authors",
        "translators",
        "editors",
        "rights",
        "identifiers",
        "cover_upload_path",
    }
    packet["metadata"]["identifiers"][0]["value"] = "mutated-only-in-packet"
    packet["context"]["source_metadata"]["title"] = "mutated-only-in-packet"
    db.session.refresh(project)
    assert project.settings == original_settings


def test_prepare_metadata_draft_includes_only_verified_translated_headings(app, monkeypatch):
    owner = _user("heading@example.test")
    project = _project(owner)
    _plan(project, metadata={"title": "Source title"})

    generation = __import__(
        "iarf_assistant.addons.translator.export_metadata_generation",
        fromlist=["db", "load_block_source_plan", "verify_active_pair", "rehydrate_target_revision", "target_alignment_projection"],
    )
    monkeypatch.setattr(generation, "db", SimpleNamespace(session=SimpleNamespace(get=Mock())))
    fake_active = SimpleNamespace(revision_id="revision-1")
    fake_alignment = SimpleNamespace()
    seen_heading = False
    seen_alignment = False
    seen_revision = False

    def get_target(model, _key):
        nonlocal seen_heading, seen_alignment, seen_revision
        if model.__name__ == "TranslationTargetBlockActive" and not seen_heading:
            seen_heading = True
            return fake_active
        if (
            model.__name__ == "TranslationTargetAlignmentActive"
            and seen_heading
            and not seen_alignment
        ):
            seen_alignment = True
            return fake_alignment
        if (
            model.__name__ == "TranslationTargetBlockRevision"
            and seen_alignment
            and not seen_revision
        ):
            seen_revision = True
            return SimpleNamespace()
        return None

    generation.db.session.get.side_effect = get_target
    monkeypatch.setattr(generation, "verify_active_pair", Mock())
    monkeypatch.setattr(generation, "rehydrate_target_revision", Mock(return_value=SimpleNamespace()))
    monkeypatch.setattr(generation, "target_alignment_projection", Mock(return_value="Translated heading"))

    packet = prepare_metadata_draft(project, metadata={}, expected_version=0)

    assert packet["context"]["translated_headings"]
    assert packet["context"]["translated_headings"][0] == {
        "source": "Source title",
        "translation": "Translated heading",
    }
    generation.verify_active_pair.assert_called_once()


@pytest.mark.parametrize(
    "response",
    [
        {"title": "Missing other required fields"},
        {**_valid_response(), "invented_field": "forbidden"},
        {**_valid_response(), "authors": [""]},
        {**_valid_response(), "title": "T" * 501},
    ],
)
def test_merge_metadata_draft_rejects_schema_errors(response):
    packet = {
        "metadata": {"title": "Existing title", "translators": ["Translator"]},
        "editable_fields": ["subtitle", "description", "subjects"],
    }
    with pytest.raises(ExportMetadataError, match="export-metadata-draft-schema"):
        merge_metadata_draft(packet, response)


def test_merge_metadata_draft_fills_empty_fields_only_and_preserves_every_other_value():
    packet = {
        "metadata": {
            "title": "User title",
            "subtitle": "",
            "authors": ["User author"],
            "description": "",
            "subjects": [],
            "translators": ["User translator"],
            "editors": ["User editor"],
            "rights": "User rights",
            "identifiers": [{"scheme": "ISBN", "value": "123"}],
            "cover_upload_path": "/uploads/user-cover.png",
        },
        "editable_fields": ["subtitle", "description", "subjects"],
    }
    original = copy.deepcopy(packet)

    merged = merge_metadata_draft(
        packet,
        {
            "title": "Model title must be ignored",
            "subtitle": "Model subtitle",
            "authors": ["Model author must be ignored"],
            "description": "Model description",
            "subjects": ["Model subject"],
        },
    )

    assert merged == {
        **original["metadata"],
        "subtitle": "Model subtitle",
        "description": "Model description",
        "subjects": ["Model subject"],
    }
    assert packet == original


def test_api_is_owner_scoped_for_prepare_and_generate(app):
    owner = _user("owner@example.test")
    other = _user("other@example.test")
    project = _project(owner)
    _plan(project, metadata={"title": "Source title"})
    client = _client(other.id)

    with pytest.raises(LookupError, match="Translation project not found"):
        client.export_metadata_prepare(project.id)
    with pytest.raises(LookupError, match="Translation project not found"):
        client.export_metadata_generate(
            project.id,
            metadata={},
            expected_version=0,
            model_id="configured/model",
        )


def test_managed_generate_requires_configured_model_without_provider_call(app, monkeypatch):
    owner = _user("no-model@example.test")
    project = _project(owner)
    _plan(project, metadata={"title": "Source title"})
    provider_call = Mock()
    monkeypatch.setattr(
        "iarf_assistant.addons.translator.block_provider_adapter.call_analysis",
        provider_call,
    )

    with pytest.raises(BlockExportError, match="export-metadata-model-required"):
        _client(owner.id).export_metadata_generate(
            project.id,
            metadata={},
            expected_version=0,
        )

    provider_call.assert_not_called()


def test_managed_generate_enforces_active_and_stale_metadata_fences(app, monkeypatch):
    owner = _user("fences@example.test")
    project = _project(owner, version=3)
    _plan(project, metadata={"title": "Source title"})
    client = _client(owner.id)
    provider_call = Mock()
    monkeypatch.setattr(
        "iarf_assistant.addons.translator.block_provider_adapter.call_analysis",
        provider_call,
    )
    active = TranslationOperation(
        project_id=project.id,
        owner_user_id=owner.id,
        kind="translate",
        idempotency_key="active-metadata",
        request_fingerprint="a" * 64,
        state="queued",
    )
    db.session.add(active)
    db.session.commit()

    with pytest.raises(BlockExportError, match="export-metadata-active-operation"):
        client.export_metadata_generate(
            project.id,
            metadata={},
            expected_version=3,
            model_id="configured/model",
        )
    provider_call.assert_not_called()

    db.session.delete(active)
    db.session.commit()
    with pytest.raises(BlockExportError, match="export-metadata-version-conflict"):
        client.export_metadata_generate(
            project.id,
            metadata={},
            expected_version=2,
            model_id="configured/model",
        )
    provider_call.assert_not_called()


def test_managed_generate_provider_failure_leaves_saved_values_unchanged(app, monkeypatch):
    owner = _user("provider-failure@example.test")
    saved = {
        "title": "Saved title",
        "translators": ["Saved translator"],
        "rights": "Saved rights",
        "identifiers": [{"scheme": "ISBN", "value": "123"}],
    }
    project = _project(owner, metadata=saved, version=1)
    _plan(project, metadata={"title": "Source title"})
    original = copy.deepcopy(project.settings)
    _configured_generation(monkeypatch, error=RuntimeError("synthetic provider failure"))

    with pytest.raises(RuntimeError, match="synthetic provider failure"):
        _client(owner.id).export_metadata_generate(
            project.id,
            metadata={},
            expected_version=1,
            model_id="configured/model",
        )

    db.session.refresh(project)
    assert project.settings == original


def test_managed_generate_rejects_metadata_version_changed_during_provider_call(app, monkeypatch):
    owner = _user("mid-call-fence@example.test")
    project = _project(owner, version=2)
    _plan(project, metadata={"title": "Source title"})

    def mutate_saved_version(_service, *, phase, packet, action_id):
        del phase, packet, action_id
        project.settings = {
            **project.settings,
            "export_metadata_version": 3,
        }
        db.session.commit()
        return {"content": json.dumps(_valid_response())}

    workflow_config = __import__(
        "iarf_assistant.addons.translator.block_workflow_config",
        fromlist=["_model_identity", "_reasoning"],
    )
    provider_adapter = __import__(
        "iarf_assistant.addons.translator.block_provider_adapter",
        fromlist=["call_analysis"],
    )
    llm_service = __import__("iarf_assistant.llm_service", fromlist=["LLMService"])
    monkeypatch.setattr(
        workflow_config,
        "_model_identity",
        lambda _model_id, *, owner_user_id: {
            "model_id": "configured/model",
            "capability_profile": {
                "supports_structured_output": False,
                "structured_output_mode": None,
            },
            "owner_user_id": owner_user_id,
        },
    )
    monkeypatch.setattr(
        workflow_config,
        "_reasoning",
        lambda model_id, user, slot: {
            "model_id": model_id,
            "slot": slot,
            "addon_id": "translator",
            "config": None,
        },
    )
    monkeypatch.setattr(provider_adapter, "call_analysis", mutate_saved_version)
    monkeypatch.setattr(llm_service, "LLMService", lambda **kwargs: SimpleNamespace(**kwargs))

    with pytest.raises(BlockExportError, match="export-metadata-version-conflict"):
        _client(owner.id).export_metadata_generate(
            project.id,
            metadata={},
            expected_version=2,
            model_id="configured/model",
        )


def test_managed_generate_returns_proposal_without_persisting_metadata(app, monkeypatch):
    owner = _user("managed-success@example.test")
    saved = {
        "title": "Saved title",
        "translators": ["Saved translator"],
        "editors": ["Saved editor"],
        "rights": "Saved rights",
        "identifiers": [{"scheme": "ISBN", "value": "123"}],
        "cover_upload_path": "/uploads/saved-cover.png",
    }
    project = _project(owner, metadata=saved, version=6)
    _plan(project, metadata={"title": "Source title", "authors": ["Source Author"]})
    original = copy.deepcopy(project.settings)
    calls = _configured_generation(
        monkeypatch,
        response=_valid_response(
            title="Model title must be ignored",
            subtitle="Translated subtitle",
            authors=["Source Author"],
            description="Model description",
            subjects=["History"],
        ),
    )

    result = _client(owner.id).export_metadata_generate(
        project.id,
        metadata={},
        expected_version=6,
        model_id="configured/model",
    )

    assert result["contract_version"] == CONTRACT_VERSION
    assert result["expected_version"] == 6
    assert result["metadata"] == {
        **saved,
        "subtitle": "Translated subtitle",
        "description": "Model description",
        "subjects": ["History"],
        "authors": ["Source Author"],
    }
    assert len(calls) == 1
    _service, phase, request, action_id = calls[0]
    assert phase["model"]["model_id"] == "configured/model"
    assert request["messages"][0]["role"] == "system"
    assert request["messages"][1]["role"] == "user"
    assert request["response_format"] is None
    assert action_id.startswith("translator_export_metadata_")
    db.session.refresh(project)
    assert project.settings == original
