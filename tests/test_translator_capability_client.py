from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from content_storage_support import bind_content_policy
from flask import Flask

# Keep the old package-name references consolidated for the exact rename ratchet.
# isort: off
from iarf_assistant import extensions as app_extensions, models as app_models
from iarf_assistant.addons.translator import (
    TranslatorAddon,
    api_client as translator_api_client,
    block_contract,
    block_persistence as translator_block_persistence,
    models as translator_models,
    operation_delivery as translator_operation_delivery,
    operations as translator_operations,
    routes,
    source_providers,
    source_snapshot,
)
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
# isort: on

TranslatorClient = translator_api_client.TranslatorClient
BlockContractError = block_contract.BlockContractError
TranslationProject = translator_models.TranslationProject
TranslationRevision = translator_models.TranslationRevision
TranslationSourceAnchor = translator_models.TranslationSourceAnchor
TranslationSourcePlan = translator_models.TranslationSourcePlan
TranslationOperation = translator_models.TranslationOperation
create_operation = translator_operations.create_operation
ResolvedSourceProvider = source_providers.ResolvedSourceProvider
MAX_DOCUMENT_SNAPSHOT_BYTES = source_snapshot.MAX_DOCUMENT_SNAPSHOT_BYTES
MAX_SUBTITLE_UNIT_CHARS = source_snapshot.MAX_SUBTITLE_UNIT_CHARS
MAX_SUBTITLE_UNITS = source_snapshot.MAX_SUBTITLE_UNITS
db = app_extensions.db
ApiKey = app_models.ApiKey
LLMModel = app_models.LLMModel
LLMProvider = app_models.LLMProvider
User = app_models.User
McpExecution = app_models.McpExecution
McpWorkItem = app_models.McpWorkItem


@pytest.fixture
def app(tmp_path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'translator.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="translator-client-test",
        TESTING=True,
        CARLINA_UPLOADS_DIR=str(uploads),
    )
    db.init_app(application)
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
        db.drop_all()
        db.engine.dispose()
        context.pop()


def _user(email: str) -> User:
    user = User(email=email, password_hash="test")
    db.session.add(user)
    db.session.commit()
    return user


def _ready_chat_model(owner: User) -> str:
    """Create the minimal configured+credential-backed model contract."""

    provider = LLMProvider(
        user_id=owner.id,
        name=f"translator_test_{owner.id}",
        display_name="Translator test provider",
    )
    db.session.add(provider)
    db.session.flush()
    model_id = f"translator_test_{owner.id}/chat"
    db.session.add_all(
        [
            ApiKey(
                user_id=owner.id,
                provider_id=provider.id,
                name="Test",
                api_key="test-secret",  # pragma: allowlist secret
                is_active=True,
            ),
            LLMModel(
                user_id=owner.id,
                provider_id=provider.id,
                model_id=model_id,
                display_name="Translator test chat",
                is_active=True,
                model_type="chat",
            ),
        ]
    )
    db.session.commit()
    return model_id


class _Queue:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.enqueued = []
        self.statuses = {}

    def enqueue(self, handler, payload):
        if self.fail:
            raise RuntimeError("queue offline")
        job_id = f"job-{len(self.enqueued) + 1}"
        self.enqueued.append((handler, payload))
        return job_id

    def get_status(self, job_id):
        return self.statuses.get(job_id, {"status": "queued"})


class _Context:
    def __init__(self, queue=None):
        self.queue = queue or _Queue()

    def get_task_queue(self, addon_id):
        assert addon_id == "translator"
        return self.queue


def _translation_project(owner: User, *, name: str = "Delete me") -> TranslationProject:
    project = TranslationProject(
        user_id=owner.id,
        name=name,
        source_lang="en",
        target_lang="de",
        status="completed",
        settings={},
    )
    db.session.add(project)
    db.session.commit()
    return project


def test_capability_client_delete_project_removes_terminal_mcp_history(app):
    owner = _user("delete-owner@example.test")
    project = _translation_project(owner)
    execution = McpExecution(
        owner_user_id=owner.id,
        addon_id="translator",
        resource_kind="project",
        resource_id=project.id,
        mode="passive",
        status="completed",
        request_json={"kind": "translation"},
    )
    db.session.add(execution)
    db.session.commit()
    execution_id = execution.id
    project_id = project.id

    client = TranslatorClient(_Context(), {"current_user_id": owner.id})
    result = client.delete_project(
        project_id,
        idempotency_key="delete-project-1",
        confirmation="delete",
    )

    assert result == {"success": True, "project_id": project_id}
    assert db.session.get(TranslationProject, project_id) is None
    assert db.session.get(McpExecution, execution_id) is None
    with pytest.raises(translator_api_client.TranslationProjectDeleteError) as retry:
        client.delete_project(
            project_id,
            idempotency_key="delete-project-1",
            confirmation="delete",
        )
    assert retry.value.code == "project_not_found"


def test_capability_client_delete_project_refuses_live_mcp_execution(app):
    owner = _user("delete-live@example.test")
    project = _translation_project(owner)
    execution = McpExecution(
        owner_user_id=owner.id,
        addon_id="translator",
        resource_kind="project",
        resource_id=project.id,
        mode="passive",
        status="running",
        request_json={"kind": "translation"},
    )
    db.session.add(execution)
    db.session.flush()
    work = McpWorkItem(
        execution_id=execution.id,
        request_fingerprint="a" * 64,
        request_json={"prompt": "translate"},
        state="leased",
        lease_token="00000000-0000-4000-8000-000000000001",
        lease_owner="worker-1",
        lease_expires_at=translator_api_client.utc_now_db(),
    )
    db.session.add(work)
    db.session.commit()

    client = TranslatorClient(_Context(), {"current_user_id": owner.id})
    with pytest.raises(translator_api_client.TranslationProjectDeleteError) as blocked:
        client.delete_project(
            project.id,
            idempotency_key="delete-project-live-1",
            confirmation="delete",
        )
    assert blocked.value.code == "project_active"
    assert "leased MCP work" in blocked.value.detail
    assert db.session.get(TranslationProject, project.id) is not None
    assert db.session.get(McpExecution, execution.id) is not None


def test_capability_client_delete_project_refuses_active_translator_operation(app):
    owner = _user("delete-operation@example.test")
    project = _translation_project(owner)
    operation, created = create_operation(
        project,
        kind="translate_document_blocks",
        request_payload={"kind": "translation"},
        idempotency_key="active-delete-test",
    )
    assert created is True
    db.session.commit()
    operation_id = operation.id

    client = TranslatorClient(_Context(), {"current_user_id": owner.id})
    with pytest.raises(translator_api_client.TranslationProjectDeleteError) as blocked:
        client.delete_project(
            project.id,
            idempotency_key="delete-active-operation-1",
            confirmation="delete",
        )
    assert blocked.value.code == "project_active"
    assert db.session.get(TranslationProject, project.id) is not None
    fresh_operation = db.session.get(TranslationOperation, operation_id)
    assert fresh_operation is not None
    assert fresh_operation.state == "queued"


def test_capability_client_delete_project_is_owner_scoped_and_requires_confirmation(
    app,
):
    owner = _user("delete-owner-a@example.test")
    other = _user("delete-owner-b@example.test")
    project = _translation_project(owner)

    other_client = TranslatorClient(_Context(), {"current_user_id": other.id})
    with pytest.raises(translator_api_client.TranslationProjectDeleteError) as missing:
        other_client.delete_project(
            project.id,
            idempotency_key="delete-project-other-1",
            confirmation="delete",
        )
    assert missing.value.code == "project_not_found"

    owner_client = TranslatorClient(_Context(), {"current_user_id": owner.id})
    with pytest.raises(
        translator_api_client.TranslationProjectDeleteError
    ) as unconfirmed:
        owner_client.delete_project(
            project.id,
            idempotency_key="delete-project-owner-1",
            confirmation="no",
        )
    assert unconfirmed.value.code == "confirmation_required"
    assert db.session.get(TranslationProject, project.id) is not None


def _provider(capability: str, client) -> ResolvedSourceProvider:
    return ResolvedSourceProvider(
        requirement_id="source",
        capability=capability,
        provider_addon_id="provider-addon",
        provider_capability_id="provider-capability",
        client=client,
    )


def test_translator_addon_resolves_unpinned_semantic_export_provider():
    calls = []

    def get_capability_provider(requirement_id, user_id, **selection):
        calls.append((requirement_id, user_id, selection))
        return SimpleNamespace(
            provider_addon_id="doc_processor",
            provider_capability_id="semantic_export",
            initialize_client=lambda _config: object(),
        )

    addon = TranslatorAddon.__new__(TranslatorAddon)
    addon.context = SimpleNamespace(get_capability_provider=get_capability_provider)

    client = addon.initialize_client({"current_user_id": 7})
    resolved = client._semantic_export_provider()

    assert resolved.capability == "document.semantic_export"
    assert calls == [
        (
            "semantic_export",
            7,
            {"provider_addon_id": None, "provider_capability_id": None},
        )
    ]


def _strict_document_snapshot(document_id: str, html: str) -> dict:
    digest = hashlib.sha256(html.encode("utf-8")).hexdigest()
    return {
        "id": document_id,
        "document_id": document_id,
        "snapshot_contract_version": "document-snapshot-v1",
        "status": "completed",
        "output_html": html,
        "content_hash": digest,
        "semantic_contract_version": "1",
        "semantic_contract_mode": "completed",
        "semantic_content_hash": digest,
        "filename": f"{document_id}.html",
        "captured_at": "2026-09-02T12:00:00+00:00",
        "source_updated_at": "2026-09-02T11:59:00+00:00",
        "metadata": {},
        "provenance": {},
    }


def _semantic_export_provider():
    return SimpleNamespace(
        provider_reference=lambda: {
            "version": 1,
            "capability": "document.semantic_export",
            "provider_addon_id": "provider-addon",
            "provider_capability_id": "semantic-export",
        }
    )


def test_legacy_text_translation_entrypoints_are_removed(app):
    owner = _user("owner@example.test")
    client = TranslatorClient(_Context(), {"current_user_id": owner.id})

    assert not hasattr(client, "create_text_project")
    with pytest.raises(ValueError, match="document translation is block-only"):
        client.translate(text="No legacy path.", target_lang="Polish")
    assert TranslationProject.query.filter_by(user_id=owner.id).count() == 0


def test_document_source_is_pinned_and_block_plan_job_carries_owner(app):
    owner = _user("owner@example.test")
    queue = _Queue()
    model_id = _ready_chat_model(owner)
    source_html = "<main><p>Source document.</p></main>"
    document_client = SimpleNamespace(
        get_document_snapshot=lambda document_id: _strict_document_snapshot(
            document_id, source_html
        )
    )
    provider = _provider("document.processing", document_client)
    client = TranslatorClient(
        _Context(queue),
        {"current_user_id": owner.id},
        document_provider_factory=lambda _reference: provider,
        semantic_export_provider_factory=lambda _reference: _semantic_export_provider(),
    )

    result = client.create_from_document(
        "doc-7",
        name="Book",
        target_lang="German",
        translator_model=model_id,
        enable_pre_analysis=False,
    )

    project = db.session.get(TranslationProject, result["id"])
    assert project.document_id == "doc-7"
    assert project.source_snapshot == source_html
    assert project.source_content_hash == hashlib.sha256(
        source_html.encode("utf-8")
    ).hexdigest()
    assert project.settings["source_reference"] == {
        "version": 1,
        "capability": "document.processing",
        "provider_addon_id": "provider-addon",
        "provider_capability_id": "provider-capability",
        "resource_type": "document",
        "resource_id": "doc-7",
    }
    assert queue.enqueued[0][0] == "plan_document_blocks"
    assert queue.enqueued[0][1]["project_id"] == project.id
    assert queue.enqueued[0][1]["user_id"] == owner.id
    assert queue.enqueued[0][1]["operation_id"]


def test_aligned_document_creation_freezes_strict_sentence_boundaries(app):
    owner = _user("aligned@example.test")
    queue = _Queue()
    model_id = _ready_chat_model(owner)
    source_html = "<main><p>משפט ראשון. משפט שני. משפט שלישי.</p></main>"
    split_calls = []

    class SentenceSegmenter:
        def split_text(self, projection, *, raise_on_failure):
            split_calls.append((projection, raise_on_failure))
            return ["משפט ראשון.", "משפט שני.", "משפט שלישי."]

    provider = _provider(
        "document.processing",
        SimpleNamespace(
            get_document_snapshot=lambda document_id: _strict_document_snapshot(
                document_id, source_html
            )
        ),
    )
    client = TranslatorClient(
        _Context(queue),
        {"current_user_id": owner.id},
        document_provider_factory=lambda _reference: provider,
        semantic_export_provider_factory=lambda _reference: _semantic_export_provider(),
        sentence_segmenter_factory=SentenceSegmenter,
    )

    result = client.create_from_document(
        "doc-aligned",
        name="Aligned",
        target_lang="English",
        translator_model=model_id,
        enable_pre_analysis=False,
        translation_unit_mode="aligned_sentences",
    )

    project = db.session.get(TranslationProject, result["id"])
    plan = TranslationSourcePlan.query.filter_by(project_id=project.id).one()
    anchors = TranslationSourceAnchor.query.filter_by(source_plan_id=plan.id).all()
    assert split_calls == [
        ("משפט ראשון. משפט שני. משפט שלישי.", True),
    ]
    assert plan.planner_settings["source_sentence_manifest"] == {
        "contract_version": "source-sentence-manifest-v1",
        "projections": {
            hashlib.sha256("משפט ראשון. משפט שני. משפט שלישי.".encode()).hexdigest(): [
                12,
                22,
                33,
            ]
        },
    }
    assert [anchor.source_text for anchor in anchors] == [
        "משפט ראשון. ",
        "משפט שני. ",
        "משפט שלישי.",
    ]
    assert [anchor.splitter_profile for anchor in anchors] == [
        "sentence-segmenter",
        "sentence-segmenter",
        "sentence-segmenter",
    ]
    assert [anchor.splitter_version for anchor in anchors] == [
        "text.segmentation-v1",
        "text.segmentation-v1",
        "text.segmentation-v1",
    ]
    assert plan.anchor_count == 3


def test_aligned_document_creation_releases_write_transaction_before_segmentation(
    app, monkeypatch
):
    owner = _user("aligned-transaction@example.test")
    model_id = _ready_chat_model(owner)
    source_html = "<main><p>Erster Satz. Zweiter Satz.</p></main>"
    provider = _provider(
        "document.processing",
        SimpleNamespace(
            get_document_snapshot=lambda document_id: _strict_document_snapshot(
                document_id, source_html
            )
        ),
    )
    commit_calls = []
    original_commit = db.session.commit

    def tracked_commit():
        commit_calls.append(True)
        return original_commit()

    monkeypatch.setattr(db.session, "commit", tracked_commit)

    class SentenceSegmenter:
        def split_text(self, projection, *, raise_on_failure):
            assert commit_calls, "source capture must commit before segmentation starts"
            return ["Erster Satz.", "Zweiter Satz."]

    client = TranslatorClient(
        _Context(),
        {"current_user_id": owner.id},
        document_provider_factory=lambda _reference: provider,
        semantic_export_provider_factory=lambda _reference: _semantic_export_provider(),
        sentence_segmenter_factory=SentenceSegmenter,
    )

    result = client.create_from_document(
        "doc-aligned-transaction",
        name="Aligned transaction",
        target_lang="English",
        translator_model=model_id,
        enable_pre_analysis=False,
        translation_unit_mode="aligned_sentences",
    )

    assert result["id"]
    assert commit_calls


def test_document_creation_does_not_resolve_sentence_segmenter_for_block_mode(app):
    owner = _user("block-only@example.test")
    model_id = _ready_chat_model(owner)
    source_html = "<main><p>Block mode remains unchanged.</p></main>"
    provider = _provider(
        "document.processing",
        SimpleNamespace(
            get_document_snapshot=lambda document_id: _strict_document_snapshot(
                document_id, source_html
            )
        ),
    )
    factory_calls = []

    def segmenter_factory():
        factory_calls.append(True)
        raise AssertionError("block mode must not resolve Sentence Segmenter")

    client = TranslatorClient(
        _Context(),
        {"current_user_id": owner.id},
        document_provider_factory=lambda _reference: provider,
        semantic_export_provider_factory=lambda _reference: _semantic_export_provider(),
        sentence_segmenter_factory=segmenter_factory,
    )

    client.create_from_document(
        "doc-block",
        name="Block",
        target_lang="English",
        translator_model=model_id,
        enable_pre_analysis=False,
        translation_unit_mode="block",
    )

    assert factory_calls == []


def test_aligned_document_creation_reuses_project_before_segmentation(app):
    owner = _user("aligned-retry@example.test")
    queue = _Queue()
    model_id = _ready_chat_model(owner)
    source_html = "<main><p>משפט ראשון. משפט שני.</p></main>"
    source_calls = []
    split_calls = []

    class SentenceSegmenter:
        def split_text(self, projection, *, raise_on_failure):
            split_calls.append((projection, raise_on_failure))
            return ["משפט ראשון.", "משפט שני."]

    provider = _provider(
        "document.processing",
        SimpleNamespace(
            get_document_snapshot=lambda document_id: (
                source_calls.append(document_id)
                or _strict_document_snapshot(document_id, source_html)
            )
        ),
    )
    client = TranslatorClient(
        _Context(queue),
        {"current_user_id": owner.id},
        document_provider_factory=lambda _reference: provider,
        semantic_export_provider_factory=lambda _reference: _semantic_export_provider(),
        sentence_segmenter_factory=SentenceSegmenter,
    )
    request = {
        "name": "Aligned retry",
        "target_lang": "English",
        "translator_model": model_id,
        "enable_pre_analysis": False,
        "translation_unit_mode": "aligned_sentences",
        "idempotency_key": "aligned-retry-1",
    }

    first = client.create_from_document("doc-aligned-retry", **request)
    second = client.create_from_document("doc-aligned-retry", **request)

    assert second["id"] == first["id"]
    assert source_calls == ["doc-aligned-retry"]
    assert split_calls == [
        ("משפט ראשון. משפט שני.", True),
    ]
    assert len(queue.enqueued) == 1


@pytest.mark.parametrize("idempotency_key", [None, "aligned-unavailable-rollback"])
def test_aligned_document_creation_rolls_back_unavailable_segmenter(app, idempotency_key):
    owner = _user("aligned-unavailable@example.test")
    model_id = _ready_chat_model(owner)
    queue = _Queue()

    def segmenter_factory():
        raise RuntimeError("PRIVATE_PROVIDER_DETAILS")

    client = TranslatorClient(
        _Context(queue),
        {"current_user_id": owner.id},
        sentence_segmenter_factory=segmenter_factory,
    )

    with pytest.raises(RuntimeError, match="working Sentence Segmenter") as caught:
        client.create_from_document(
            "doc-unavailable",
            name="Unavailable",
            target_lang="English",
            translator_model=model_id,
            enable_pre_analysis=False,
            translation_unit_mode="aligned_sentences",
            idempotency_key=idempotency_key,
        )

    assert "PRIVATE_PROVIDER_DETAILS" not in str(caught.value)
    assert TranslationProject.query.filter_by(user_id=owner.id).count() == 0
    assert TranslationSourcePlan.query.count() == 0
    assert TranslationOperation.query.count() == 0
    assert queue.enqueued == []


@pytest.mark.parametrize("idempotency_key", [None, "aligned-malformed-rollback"])
def test_aligned_document_creation_rolls_back_malformed_sentence_partition(app, idempotency_key):
    owner = _user("aligned-malformed@example.test")
    model_id = _ready_chat_model(owner)
    queue = _Queue()
    source_html = "<main><p>משפט ראשון. משפט שני.</p></main>"
    provider = _provider(
        "document.processing",
        SimpleNamespace(
            get_document_snapshot=lambda document_id: _strict_document_snapshot(
                document_id, source_html
            )
        ),
    )

    segments = ["משפט ראשון."]

    class SentenceSegmenter:
        def split_text(self, _projection, *, raise_on_failure):
            assert raise_on_failure is True
            return segments

    client = TranslatorClient(
        _Context(queue),
        {"current_user_id": owner.id},
        document_provider_factory=lambda _reference: provider,
        semantic_export_provider_factory=lambda _reference: _semantic_export_provider(),
        sentence_segmenter_factory=SentenceSegmenter,
    )

    request = {
        "name": "Malformed",
        "target_lang": "English",
        "translator_model": model_id,
        "enable_pre_analysis": False,
        "translation_unit_mode": "aligned_sentences",
        "idempotency_key": idempotency_key,
    }
    with pytest.raises(BlockContractError) as caught:
        client.create_from_document("doc-malformed", **request)

    assert caught.value.code == "source-sentence-segmentation-coverage"
    assert TranslationProject.query.filter_by(user_id=owner.id).count() == 0
    assert TranslationSourcePlan.query.count() == 0
    assert TranslationOperation.query.count() == 0
    assert queue.enqueued == []

    segments.append("משפט שני.")
    result = client.create_from_document("doc-malformed", **request)
    assert result["id"]
    assert TranslationProject.query.filter_by(user_id=owner.id).count() == 1
    assert TranslationSourcePlan.query.count() == 1
    assert TranslationOperation.query.count() == 1
    assert len(queue.enqueued) == 1


def test_document_create_retry_repairs_an_unbound_initial_delivery(app):
    owner = _user("document-repair@example.test")
    queue = _Queue()
    model_id = _ready_chat_model(owner)
    source_html = "<p>Recover the pinned source.</p>"
    source_calls = []
    provider = _provider(
        "document.processing",
        SimpleNamespace(
            get_document_snapshot=lambda document_id: (
                source_calls.append(document_id)
                or _strict_document_snapshot(document_id, source_html)
            )
        ),
    )
    client = TranslatorClient(
        _Context(queue),
        {"current_user_id": owner.id},
        document_provider_factory=lambda _reference: provider,
        semantic_export_provider_factory=lambda _reference: _semantic_export_provider(),
    )
    arguments = {
        "name": "Repairable document",
        "target_lang": "Polish",
        "translator_model": model_id,
        "enable_pre_analysis": False,
        "idempotency_key": "document-repair-1",
    }
    first = client.create_from_document("doc-repair", **arguments)
    project = db.session.get(TranslationProject, first["id"])
    operation = project.operations[0]
    original_operation_id = operation.id
    operation.queue_job_id = None
    queue.enqueued.clear()
    db.session.commit()

    second = client.create_from_document("doc-repair", **arguments)

    assert second["id"] == first["id"]
    assert second["job_id"] == "job-1"
    assert source_calls == ["doc-repair"]
    assert queue.enqueued[0][1]["operation_id"] == original_operation_id
    assert project.operations[0].queue_job_id == "job-1"


def test_capability_document_snapshot_rejects_html_larger_than_20_mib(app):
    owner = _user("owner@example.test")
    oversized_html = "x" * (MAX_DOCUMENT_SNAPSHOT_BYTES + 1)
    provider = _provider(
        "document.processing",
        SimpleNamespace(
            get_document_snapshot=lambda _document_id: _strict_document_snapshot(
                "doc-1", oversized_html
            )
        ),
    )
    client = TranslatorClient(
        _Context(),
        {"current_user_id": owner.id},
        document_provider_factory=lambda _reference: provider,
        semantic_export_provider_factory=lambda _reference: _semantic_export_provider(),
    )
    model_id = _ready_chat_model(owner)

    with pytest.raises(ValueError, match="snapshot-html"):
        client.create_from_document(
            "doc-1",
            name="Oversized",
            target_lang="Polish",
            translator_model=model_id,
            enable_pre_analysis=False,
        )


def test_capability_research_settings_parse_booleans_and_require_ready_model(app):
    owner = _user("owner@example.test")
    model_id = _ready_chat_model(owner)
    client = TranslatorClient(_Context(), {"current_user_id": owner.id})

    disabled = client._settings(
        translator_model=model_id,
        enable_pre_analysis="false",
        enable_research="false",
        research_plugin_ids=["web.research"],
    )
    assert disabled["enable_pre_analysis"] is False
    assert disabled["enable_research"] is False
    assert disabled["research_plugin_ids"] == ["web.research"]

    enabled = client._settings(
        translator_model=model_id,
        researcher_model=model_id,
        enable_pre_analysis=False,
        enable_research=True,
        research_plugin_ids=["web.research"],
    )
    assert enabled["enable_research"] is True
    with pytest.raises(ValueError, match="enable_research must be a boolean"):
        client._settings(
            translator_model=model_id,
            enable_pre_analysis=False,
            enable_research="definitely",
        )
    with pytest.raises(ValueError, match="researcher model is required"):
        client._settings(
            translator_model=model_id,
            enable_pre_analysis=False,
            enable_research=True,
            research_plugin_ids=["web.research"],
        )


def test_capability_subtitle_creation_rejects_oversized_source_unit(app):
    owner = _user("owner@example.test")
    model_id = _ready_chat_model(owner)
    provider = _provider(
        "subtitles.authoring",
        SimpleNamespace(
            prepare_translation_source=lambda _project_id: {
                "project": {"id": 12, "name": "Episode", "language": "English"},
                "units": [
                    {
                        "unit_id": "u1",
                        "source_text": "x" * (MAX_SUBTITLE_UNIT_CHARS + 1),
                    }
                ],
            }
        ),
    )
    client = TranslatorClient(
        _Context(),
        {"current_user_id": owner.id},
        subtitle_provider_factory=lambda _reference: provider,
    )

    with pytest.raises(ValueError, match="exceeds 50,000"):
        client.create_from_subtitles(
            12,
            name="Too large",
            target_lang="Polish",
            translator_model=model_id,
            enable_pre_analysis=False,
        )


def test_capability_subtitle_creation_idempotency_fences_provider_resolution(app):
    owner = _user("owner@example.test")
    model_id = _ready_chat_model(owner)
    calls = []
    available = True

    class SubtitleClient:
        def prepare_translation_source(self, project_id):
            calls.append(project_id)
            if not available:
                raise RuntimeError("provider went away")
            return {
                "project": {"id": project_id, "name": "Episode", "language": "English"},
                "units": [{"unit_id": "u1", "source_text": "A source line."}],
            }

    provider = _provider("subtitles.authoring", SubtitleClient())
    client = TranslatorClient(
        _Context(),
        {"current_user_id": owner.id},
        subtitle_provider_factory=lambda _reference: provider,
    )
    arguments = {
        "name": "Idempotent subtitles",
        "target_lang": "Polish",
        "translator_model": model_id,
        "researcher_model": model_id,
        "enable_pre_analysis": False,
        "enable_research": True,
        "research_plugin_ids": ["provider.b", "provider.a"],
        "idempotency_key": "subtitle-create-1",
    }
    first = client.create_from_subtitles(12, **arguments)
    available = False
    second = client.create_from_subtitles(
        12,
        **{**arguments, "research_plugin_ids": ["provider.a", "provider.b"]},
    )

    assert first["id"] == second["id"]
    assert calls == [12]
    with pytest.raises(RuntimeError, match="idempotency key"):
        client.create_from_subtitles(12, **{**arguments, "name": "Changed"})
    assert calls == [12]


def test_capability_subtitle_creation_bounds_and_projects_provider_metadata(app):
    owner = _user("owner@example.test")
    model_id = _ready_chat_model(owner)
    provider = _provider(
        "subtitles.authoring",
        SimpleNamespace(
            prepare_translation_source=lambda _project_id: {
                "project": {
                    "id": 12,
                    "name": "Episode 1",
                    "language": "English",
                    "media_filename": "episode-1.wav",
                    "private_source_path": "/not/for/translator",
                },
                "units": [
                    {
                        "unit_id": "unit-1",
                        "source_text": "Hello there.",
                        "cue_start_index": 4,
                        "cue_end_index": 5,
                        "cue_ids": ["cue-a", "cue-b"],
                        "start": 1.25,
                        "end": 3.75,
                        "alignment_mode": "word",
                        "tokens": [{"text": "private source token"}],
                        "private_metadata": {"secret": "never persist"},
                    }
                ],
                "render_settings": {
                    "max_chars": 42,
                    "alignment_config": {"threshold": [0.2, {"mode": "strict"}]},
                    "private_setting": "never persist",
                },
            }
        ),
    )
    client = TranslatorClient(
        _Context(),
        {"current_user_id": owner.id},
        subtitle_provider_factory=lambda _reference: provider,
    )

    result = client.create_from_subtitles(
        12,
        name="Metadata boundary",
        target_lang="Polish",
        translator_model=model_id,
        enable_pre_analysis=False,
    )
    persisted = db.session.get(TranslationProject, result["id"])
    metadata = next(iter(persisted.settings["subtitle"]["units"].values()))

    assert metadata == {
        "unit_id": "unit-1",
        "cue_start_index": 4,
        "cue_end_index": 5,
        "cue_ids": ["cue-a", "cue-b"],
        "start": 1.25,
        "end": 3.75,
        "alignment_mode": "word",
    }
    assert persisted.settings["subtitle"]["render_settings"] == {
        "max_chars": 42,
        "alignment_config": {"threshold": [0.2, {"mode": "strict"}]},
    }
    assert "private" not in str(persisted.settings)
    assert "source_text" not in str(persisted.settings)
    assert "Hello there." in persisted.source_snapshot


@pytest.mark.parametrize(
    "source",
    [
        {
            "project": {"id": 12, "name": "Episode"},
            "units": [
                {
                    "unit_id": "u1",
                    "source_text": "A line.",
                    "cue_ids": [str(index) for index in range(257)],
                }
            ],
        },
        {
            "project": {"id": 12, "name": "Episode"},
            "units": [{"unit_id": "u1", "source_text": "A line."}],
            "render_settings": {
                "alignment_config": {"a": {"b": {"c": {"d": {"e": 1}}}}}
            },
        },
        {
            "project": {"id": 12, "name": "Episode"},
            "units": [{"unit_id": "u1", "source_text": "A line."}]
            * (MAX_SUBTITLE_UNITS + 1),
        },
    ],
)
def test_capability_subtitle_creation_rejects_malicious_provider_shapes(app, source):
    owner = _user("owner@example.test")
    model_id = _ready_chat_model(owner)
    provider = _provider(
        "subtitles.authoring",
        SimpleNamespace(prepare_translation_source=lambda _project_id: source),
    )
    client = TranslatorClient(
        _Context(),
        {"current_user_id": owner.id},
        subtitle_provider_factory=lambda _reference: provider,
    )

    with pytest.raises(ValueError):
        client.create_from_subtitles(
            12,
            name="Unsafe source",
            target_lang="Polish",
            translator_model=model_id,
            enable_pre_analysis=False,
        )
    assert TranslationProject.query.filter_by(user_id=owner.id).count() == 0


def test_subtitle_source_review_and_export_use_selected_provider(app):
    owner = _user("owner@example.test")
    references = []

    class SubtitleClient:
        def prepare_translation_source(self, project_id):
            assert project_id == 12
            return {
                "project": {
                    "id": 12,
                    "name": "Episode",
                    "language": "English",
                    "media_filename": "episode.wav",
                },
                "units": [
                    {
                        "unit_id": "unit-1",
                        "source_text": "Hello there.",
                        "start": 1.0,
                        "end": 2.5,
                    }
                ],
                "render_settings": {"max_chars": 42},
            }

        def render_translation_segments(self, segments, *, settings):
            assert settings == {"max_chars": 42}
            return [
                {
                    "cue_index": 0,
                    "start": 1.0,
                    "end": 2.5,
                    "text": segments[0]["text"],
                }
            ]

        def export_translation_cues(self, cues, export_format):
            assert export_format == "srt"
            return {
                "content": cues[0]["text"],
                "extension": "srt",
                "mimetype": "application/x-subrip",
            }

    provider = _provider("subtitles.authoring", SubtitleClient())

    def subtitle_factory(reference):
        references.append(reference)
        return provider

    client = TranslatorClient(
        _Context(),
        {"current_user_id": owner.id},
        subtitle_provider_factory=subtitle_factory,
    )
    model_id = _ready_chat_model(owner)
    project = client.create_from_subtitles(
        12,
        name="Episode translation",
        target_lang="Polish",
        translator_model=model_id,
        enable_pre_analysis=False,
    )
    segment_id = project["segments"][0]["id"]

    reviewed = client.review(
        project["id"],
        segment_id,
        translated_text="Dzien dobry.",
        expected_version=project["segments"][0]["version"],
    )
    exported = client.export(project["id"], export_format="srt")

    assert reviewed["status"] == "human_approved"
    assert exported["content"] == "Dzien dobry."
    assert (
        TranslationRevision.query.filter_by(
            segment_id=segment_id,
            user_id=owner.id,
        ).count()
        == 1
    )
    assert references[-1]["provider_addon_id"] == "provider-addon"
    persisted = db.session.get(TranslationProject, project["id"])
    assert "source_text" not in str(persisted.settings["subtitle"]["units"])
    assert "Hello there." in persisted.source_snapshot


def test_html_export_blocks_cross_user_media_and_escapes_metadata(app):
    owner = _user("owner@example.test")
    other = _user("other@example.test")
    other_file = Path(app.config["CARLINA_UPLOADS_DIR"]) / str(other.id) / "private.png"
    other_file.parent.mkdir(parents=True)
    other_file.write_bytes(b"not really an image")
    project = TranslationProject(
        user_id=owner.id,
        name="<script>title</script>",
        source_lang="English",
        target_lang='pl" onload="alert(1)',
        status="ready",
        skeleton_html=f'<p>Safe</p><img src="/uploads/{other.id}/private.png">',
        engine_contract="segment-v1",
        settings={"source_type": "subtitle_studio"},
    )
    db.session.add(project)
    db.session.commit()

    result = TranslatorClient(_Context(), {"current_user_id": owner.id}).export(
        project.id
    )

    assert str(other_file.resolve()) not in result["content"]
    assert "data:image/gif;base64" in result["content"]
    assert "<script>title</script>" not in result["content"]
    assert "&lt;script&gt;title&lt;/script&gt;" in result["content"]
    assert 'onload="alert(1)' not in result["content"]


def test_document_creation_queue_failure_retains_frozen_project_for_retry(app):
    owner = _user("owner@example.test")
    queue = _Queue(fail=True)
    source_calls = []
    source_html = "<p>Private source.</p>"
    provider = _provider(
        "document.processing",
        SimpleNamespace(
            get_document_snapshot=lambda document_id: (
                source_calls.append(document_id)
                or _strict_document_snapshot(document_id, source_html)
            )
        ),
    )
    client = TranslatorClient(
        _Context(queue),
        {"current_user_id": owner.id},
        document_provider_factory=lambda _reference: provider,
        semantic_export_provider_factory=lambda _reference: _semantic_export_provider(),
    )
    model_id = _ready_chat_model(owner)
    request = {
        "name": "Retry document",
        "target_lang": "Polish",
        "translator_model": model_id,
        "enable_pre_analysis": False,
        "idempotency_key": "document-create-queue-failure",
    }

    with pytest.raises(RuntimeError, match="queue offline"):
        client.create_from_document("doc-1", **request)
    project = TranslationProject.query.filter_by(user_id=owner.id).one()
    failed = TranslationOperation.query.filter_by(project_id=project.id).one()
    frozen_pins = (
        failed.source_plan_id,
        failed.source_plan_hash,
        failed.source_content_hash,
    )
    assert failed.state == "failed"
    assert failed.error_code == "queue_publish_failed"
    assert failed.queue_job_id is None
    assert frozen_pins[0] == translator_block_persistence.active_source_plan_pointer(
        project.id
    ).source_plan_id

    queue.fail = False
    source_html = "<p>Changed source must not replace the frozen plan.</p>"
    recovered = client.create_from_document("doc-1", **request)
    assert recovered["job_id"] == "job-1"
    assert recovered["id"] == project.id
    assert TranslationProject.query.filter_by(user_id=owner.id).count() == 1
    successor = TranslationOperation.query.filter_by(queue_job_id="job-1").one()
    assert successor.id != failed.id
    assert successor.idempotency_key == f"recover-{failed.id}"
    assert (
        successor.source_plan_id,
        successor.source_plan_hash,
        successor.source_content_hash,
    ) == frozen_pins
    assert source_calls == ["doc-1"]


def test_document_creation_recovers_ambiguous_publication_from_frozen_plan(app):
    class DeliveredThenFailedQueue(_Queue):
        raise_after_delivery = True

        def enqueue(self, handler, payload):
            job_id = super().enqueue(handler, payload)
            if self.raise_after_delivery:
                raise RuntimeError("publication outcome unknown")
            return job_id

    owner = _user("publication-owner@example.test")
    queue = DeliveredThenFailedQueue()
    source_calls = []
    source_html = "<p>Frozen source.</p>"
    provider = _provider(
        "document.processing",
        SimpleNamespace(
            get_document_snapshot=lambda document_id: (
                source_calls.append(document_id)
                or _strict_document_snapshot(document_id, source_html)
            )
        ),
    )
    client = TranslatorClient(
        _Context(queue),
        {"current_user_id": owner.id},
        document_provider_factory=lambda _reference: provider,
        semantic_export_provider_factory=lambda _reference: _semantic_export_provider(),
    )
    request = {
        "name": "Recover frozen document",
        "target_lang": "Polish",
        "translator_model": _ready_chat_model(owner),
        "enable_pre_analysis": False,
        "idempotency_key": "document-ambiguous-publication",
    }

    with pytest.raises(RuntimeError, match="publication outcome unknown"):
        client.create_from_document("doc-1", **request)

    project = TranslationProject.query.filter_by(user_id=owner.id).one()
    failed = TranslationOperation.query.filter_by(project_id=project.id).one()
    frozen_pins = (
        failed.source_plan_id,
        failed.source_plan_hash,
        failed.source_content_hash,
    )
    assert failed.state == "failed"
    assert failed.error_code == "queue_publish_failed"
    assert failed.queue_job_id is None
    assert frozen_pins[0] == translator_block_persistence.active_source_plan_pointer(
        project.id
    ).source_plan_id
    assert queue.enqueued[0][1]["operation_id"] == failed.id
    with pytest.raises(translator_operations.TranslationOperationNotRunnable):
        translator_operations.claim_operation(
            failed.id,
            "job-1",
            owner_user_id=owner.id,
            project_id=project.id,
        )

    queue.raise_after_delivery = False
    source_html = "<p>Changed source must not replace the frozen plan.</p>"
    recovered = client.create_from_document("doc-1", **request)
    successor = TranslationOperation.query.filter_by(queue_job_id="job-2").one()
    assert recovered["id"] == project.id
    assert recovered["job_id"] == "job-2"
    assert successor.id != failed.id
    assert successor.idempotency_key == f"recover-{failed.id}"
    assert (
        successor.source_plan_id,
        successor.source_plan_hash,
        successor.source_content_hash,
    ) == frozen_pins
    assert source_calls == ["doc-1"]
    assert TranslationProject.query.filter_by(user_id=owner.id).count() == 1


def test_document_creation_retains_project_after_queue_binding_ambiguity(
    app, monkeypatch
):
    owner = _user("owner@example.test")
    queue = _Queue()
    source_html = "<p>Private source.</p>"
    provider = _provider(
        "document.processing",
        SimpleNamespace(
            get_document_snapshot=lambda document_id: _strict_document_snapshot(
                document_id, source_html
            )
        ),
    )
    client = TranslatorClient(
        _Context(queue),
        {"current_user_id": owner.id},
        document_provider_factory=lambda _reference: provider,
        semantic_export_provider_factory=lambda _reference: _semantic_export_provider(),
    )
    model_id = _ready_chat_model(owner)

    monkeypatch.setattr(
        translator_operation_delivery,
        "attach_queue_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            translator_api_client.TranslationOperationTransitionConflict(
                "ambiguous-operation", None
            )
        ),
    )
    with pytest.raises(RuntimeError, match="lost its queue binding"):
        client.create_from_document(
            "doc-1",
            name="Ambiguous queue document",
            target_lang="Polish",
            translator_model=model_id,
            enable_pre_analysis=False,
            idempotency_key="document-create-binding-ambiguity",
        )

    project = TranslationProject.query.filter_by(user_id=owner.id).one()
    operation = project.operations[0]
    assert operation.state == "queued"
    assert operation.queue_job_id is None
    assert queue.enqueued[0][1]["operation_id"] == operation.id


def test_job_status_rejects_cross_project_worker_results(app):
    owner = _user("owner@example.test")
    queue = _Queue()
    model_id = _ready_chat_model(owner)
    provider = _provider(
        "document.processing",
        SimpleNamespace(
            get_document_snapshot=lambda document_id: _strict_document_snapshot(
                document_id, "<main><p>Translate this.</p></main>"
            )
        ),
    )
    client = TranslatorClient(
        _Context(queue),
        {"current_user_id": owner.id},
        document_provider_factory=lambda _reference: provider,
        semantic_export_provider_factory=lambda _reference: _semantic_export_provider(),
    )
    result = client.create_from_document(
        "doc-status",
        name="Status",
        target_lang="Polish",
        translator_model=model_id,
        enable_pre_analysis=False,
    )
    queue.statuses[result["job_id"]] = {
        "status": "completed",
        "result": {
            "user_id": owner.id,
            "project_id": "another-project",
        },
    }

    with pytest.raises(PermissionError, match="another project"):
        client.job_status(result["id"], result["job_id"])


def test_idempotent_unbound_operation_is_republished(app):
    owner = _user("owner@example.test")
    queue = _Queue()
    project_row = TranslationProject(
        user_id=owner.id,
        name="Retry export",
        source_lang="English",
        target_lang="Polish",
        status="ready",
        engine_contract="segment-v1",
        source_content_hash="a" * 64,
        settings={"source_type": "subtitle_studio"},
    )
    db.session.add(project_row)
    db.session.flush()
    operation, created = create_operation(
        project_row,
        kind="export",
        request_payload={"format": "docx"},
        idempotency_key="retry-export",
        source_content_hash=project_row.source_content_hash,
        checkpoint={"format": "docx"},
    )
    assert created
    db.session.commit()

    with app.test_request_context(headers={"Idempotency-Key": "retry-export"}):
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(routes, "_translator_queue", lambda: queue)
            republished, error = routes._publish_operation(
                project_row,
                kind="export",
                handler="export_project",
                request_payload={"format": "docx"},
                queue_payload={"format": "docx"},
            )

    assert error is None
    assert republished.id == operation.id
    assert republished.queue_job_id == "job-1"
    assert queue.enqueued == [
        (
            "export_project",
            {
                "project_id": project_row.id,
                "user_id": owner.id,
                "operation_id": operation.id,
                "format": "docx",
            },
        )
    ]


def test_capability_client_republishes_an_unbound_queued_operation(app, monkeypatch):
    owner = _user("owner@example.test")
    queue = _Queue()
    client = TranslatorClient(_Context(queue), {"current_user_id": owner.id})
    project = TranslationProject(
        user_id=owner.id,
        name="Retry capability export",
        source_lang="English",
        target_lang="Polish",
        status="ready",
        engine_contract="segment-v1",
        source_content_hash="a" * 64,
        settings={"source_type": "subtitle_studio"},
    )
    db.session.add(project)
    db.session.commit()
    orphan = SimpleNamespace(id="orphan-operation", queue_job_id=None, state="queued")
    monkeypatch.setattr(
        translator_operation_delivery,
        "create_operation",
        lambda *_args, **_kwargs: (orphan, False),
    )
    monkeypatch.setattr(
        translator_operation_delivery,
        "attach_queue_job",
        lambda _operation, _job_id: orphan,
    )

    job_id = client._record_job(
        project,
        handler="export_project",
        operation="export",
        payload={"format": "docx"},
    )

    assert job_id == "job-1"
    assert queue.enqueued == [
        (
            "export_project",
            {
                "project_id": project.id,
                "user_id": owner.id,
                "operation_id": "orphan-operation",
                "format": "docx",
            },
        )
    ]


def _async_source_client(owner, queue, source):
    return TranslatorClient(
        _Context(queue), {"current_user_id": owner.id},
        document_provider_factory=lambda _reference: _provider("document.processing", SimpleNamespace(
            get_document_snapshot=lambda document_id: _strict_document_snapshot(document_id, source[0]),
        )),
        semantic_export_provider_factory=lambda _reference: _semantic_export_provider(),
        sentence_segmenter_factory=lambda: pytest.fail("Creation must not resolve or call the splitter."),
    )


def _async_options(**extra):
    return dict(name="Async book", target_lang="English", source_lang="German",
                execution_mode="passive", translation_unit_mode="aligned_sentences",
                enable_pre_analysis=False, defer_source_planning=True, **extra)


def test_async_creation_returns_before_segmentation_and_worker_uses_frozen_source(app, monkeypatch):
    from iarf_assistant.addons.translator import worker as worker_module
    from iarf_assistant.addons.translator.models import TranslationOperation, TranslationSourcePlan
    owner = _user("async-source@example.test")
    queue = _Queue()
    queue.set_status = lambda job_id, status: queue.statuses.__setitem__(job_id, status)
    source = ["<main><p>One. Two.</p></main>"]
    client = _async_source_client(owner, queue, source)
    created = client.create_from_document("async-doc", **_async_options(idempotency_key="stable-create"))
    project = db.session.get(TranslationProject, created["id"])
    operation = TranslationOperation.query.filter_by(project_id=project.id).one()
    assert operation.state == "queued" and operation.source_plan_id is None
    assert TranslationSourcePlan.query.filter_by(project_id=project.id).count() == 0
    assert "output_html" not in operation.checkpoint_json["snapshot_envelope"]
    assert "One. Two." not in json.dumps(queue.enqueued)
    with pytest.raises(RuntimeError) as early_start:
        client.start_block_translation(project.id)
    assert early_start.value.code == "source_planning_not_ready"
    assert len(queue.enqueued) == 1
    source[0] = "<main><p>Changed after creation.</p></main>"
    retry = client.create_from_document("async-doc", **_async_options(idempotency_key="stable-create"))
    assert retry["id"] == project.id and len(queue.enqueued) == 1
    calls = []
    def split(text):
        calls.append(text)
        assert "Changed" not in text
        assert TranslationOperation.query.filter_by(project_id=project.id).one().state == "running"
        return ["One.", "Two."]
    monkeypatch.setattr(worker_module, "_planning_sentence_splitter", lambda _project: split)
    worker_module.plan_document_blocks(queue.enqueued[0][1], queue, created["job_id"])
    assert calls
    db.session.expire_all()
    assert db.session.get(TranslationOperation, operation.id).state == "completed"
    assert TranslationSourcePlan.query.filter_by(project_id=project.id).count() == 1
    again = client.create_from_document("async-doc", **_async_options(idempotency_key="stable-create"))
    assert again["id"] == project.id and len(queue.enqueued) == 1


def test_async_creation_without_key_deduplicates_snapshot_but_allows_new_snapshot_or_key(app):
    owner = _user("async-dedupe@example.test")
    queue = _Queue()
    source = ["<main><p>Original source.</p></main>"]
    client = _async_source_client(owner, queue, source)
    first = client.create_from_document("async-doc", **_async_options())
    duplicate = client.create_from_document("async-doc", **_async_options())
    assert first["id"] == duplicate["id"] and len(queue.enqueued) == 1
    intentional = client.create_from_document("async-doc", **_async_options(idempotency_key="another-project"))
    assert intentional["id"] != first["id"]
    source[0] = "<main><p>Corrected source.</p></main>"
    changed = client.create_from_document("async-doc", **_async_options())
    assert changed["id"] not in {first["id"], intentional["id"]}


@pytest.mark.parametrize("batch_supported", [False, True])
def test_async_segmentation_failure_retains_project_and_retry_reuses_snapshot(app, monkeypatch, batch_supported):
    from iarf_assistant.addons.translator import worker as worker_module
    from iarf_assistant.addons.translator.models import TranslationOperation, TranslationSourcePlan
    owner = _user("async-failure@example.test")
    queue = _Queue()
    queue.set_status = lambda job_id, status: queue.statuses.__setitem__(job_id, status)
    source = ["<main><p>One. Two.</p></main>"]
    client = _async_source_client(owner, queue, source)
    created = client.create_from_document("async-doc", **_async_options(idempotency_key="retry-source"))
    def fail(text):
        raise RuntimeError("synthetic segmenter unavailable")
    if batch_supported:
        fail.split_batch = fail
    monkeypatch.setattr(worker_module, "_planning_sentence_splitter", lambda _project: fail)
    with pytest.raises(ValueError):
        worker_module.plan_document_blocks(queue.enqueued[0][1], queue, created["job_id"])
    project = db.session.get(TranslationProject, created["id"])
    assert project is not None
    assert TranslationSourcePlan.query.filter_by(project_id=project.id).count() == 0
    assert TranslationOperation.query.filter_by(project_id=project.id).one().state == "failed"
    source[0] = "<main><p>Changed live output.</p></main>"
    retried = client.create_from_document("async-doc", **_async_options(idempotency_key="retry-source"))
    assert retried["id"] == project.id and retried["job_id"] != created["job_id"]
    assert "One. Two." in project.source_snapshot


def test_new_projects_reuse_segmentation_but_keep_separate_source_plans(app, tmp_path, monkeypatch):
    from iarf_assistant.addons.translator import worker as worker_module
    owner = _user("cache-projects@example.test")
    queue = _Queue()
    queue.set_status = lambda job, status: queue.statuses.__setitem__(job, status)
    source = ["<main><p>One. Two.</p></main>"]
    client = _async_source_client(owner, queue, source)
    calls = []
    def split(text):
        calls.append(text)
        return ["One.", "Two."]
    split.segmentation_profile = {"runtime": "a" * 64, "stride": 128, "block_size": 256}
    monkeypatch.setattr(worker_module, "_planning_sentence_splitter", lambda _project: split)
    app.addon_manager = SimpleNamespace(get_loaded_addon=lambda _: SimpleNamespace(
        context=SimpleNamespace(data_dir=tmp_path / "owned-cache")))
    first = client.create_from_document("async-doc", **_async_options(idempotency_key="cache-first"))
    worker_module.plan_document_blocks(queue.enqueued[-1][1], queue, first["job_id"])
    count = len(calls)
    assert count > 0
    second = client.create_from_document("async-doc", **{**_async_options(
        idempotency_key="cache-second"), "target_lang": "German"})
    worker_module.plan_document_blocks(queue.enqueued[-1][1], queue, second["job_id"])
    assert len(calls) == count
    assert first["id"] != second["id"]
    assert TranslationSourcePlan.query.count() == 2
    projection = client.mcp_job_projection(second["id"])
    assert projection["source_planning"]["segmentation_cache_hits"] > 0
    assert projection["source_planning"]["segmentation_cache_misses"] == 0
