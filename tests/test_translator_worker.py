import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from bs4 import BeautifulSoup
from content_storage_support import bind_content_policy
from flask import Flask
from sqlalchemy import text

# Keep the old package-name references consolidated for the exact rename ratchet.
# isort: off
from iarf_assistant import extensions as app_extensions, models as app_models
from iarf_assistant.addons.translator import (
    block_contract,
    models as translator_models,
    operations as translator_operations,
    prompts as translator_prompts,
    worker,
)
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
# isort: on

GlossaryTerm = translator_models.GlossaryTerm
TranslationProject = translator_models.TranslationProject
TranslationNote = translator_models.TranslationNote
TranslationSegment = translator_models.TranslationSegment
DEFAULT_TRANSLATOR_SYSTEM_PROMPT = translator_prompts.DEFAULT_TRANSLATOR_SYSTEM_PROMPT
TRANSLATOR_NOTE_POLICY_DISABLED = translator_prompts.TRANSLATOR_NOTE_POLICY_DISABLED
TRANSLATOR_NOTE_POLICY_ENABLED = translator_prompts.TRANSLATOR_NOTE_POLICY_ENABLED
db = app_extensions.db
User = app_models.User


def test_translator_note_is_separate_from_qa_context_note():
    content = (
        '<target id="one" needs_review="true">'
        "<text>Hanshi Festival was observed.</text>"
        "<note>Check the source chronology.</note>"
        "<translator_note><anchor>Hanshi Festival</anchor>"
        "<body>It traditionally involved a no-fire observance.</body>"
        "</translator_note></target>"
    )

    parsed, missing, errors = worker._validate_target_output(
        content,
        ["one"],
        translator_notes_enabled=True,
    )

    assert not missing
    assert not errors
    assert parsed["one"]["note"] == "Check the source chronology."
    assert parsed["one"]["translator_note"] == {
        "anchor": "Hanshi Festival",
        "body": "It traditionally involved a no-fire observance.",
    }


def test_translator_note_is_forbidden_when_setting_is_off():
    content = (
        '<target id="one"><text>Hanshi Festival.</text>'
        "<translator_note><anchor>Hanshi Festival</anchor>"
        "<body>Context.</body></translator_note></target>"
    )

    parsed, missing, errors = worker._validate_target_output(
        content,
        ["one"],
        translator_notes_enabled=False,
    )

    assert parsed == {}
    assert missing == ["one"]
    assert any("translator_note is disabled" in error for error in errors)


def test_translator_note_rejects_bad_anchor_topology_text_and_batch_cap():
    cross_node, _, cross_errors = worker._validate_target_output(
        '<target id="one"><text><b>Hanshi</b> Festival.</text>'
        "<translator_note><anchor>Hanshi Festival</anchor>"
        "<body>Context.</body></translator_note></target>",
        ["one"],
        translator_notes_enabled=True,
    )
    repeated, _, repeated_errors = worker._validate_target_output(
        '<target id="one"><text>Dao and Dao.</text>'
        "<translator_note><anchor>Dao</anchor><body>Context.</body>"
        "</translator_note></target>",
        ["one"],
        translator_notes_enabled=True,
    )
    four_targets = "".join(
        f'<target id="{index}"><text>Term {index}.</text>'
        f"<translator_note><anchor>Term {index}</anchor><body>Context.</body>"
        "</translator_note></target>"
        for index in range(4)
    )
    capped, _, cap_errors = worker._validate_target_output(
        four_targets,
        [str(index) for index in range(4)],
        translator_notes_enabled=True,
    )

    assert cross_node == {}
    assert any("anchor does not occur exactly once" in error for error in cross_errors)
    assert repeated == {}
    assert any(
        "anchor does not occur exactly once" in error for error in repeated_errors
    )
    assert set(capped) == {str(index) for index in range(4)}
    assert any("at most 3 translator_note" in error for error in cap_errors)


def test_translator_note_policy_is_conservative_and_names_target_language():
    enabled = TRANSLATOR_NOTE_POLICY_ENABLED.format(target_lang="Polish")

    assert "Emit zero" in enabled
    assert "Hanshi Festival" in enabled
    assert "Lantern Festival" in enabled
    assert "dao" in enabled
    assert "Polish term" in enabled
    assert "ordinary objects" in enabled
    assert "Emit no <translator_note>" in TRANSLATOR_NOTE_POLICY_DISABLED


def test_passive_model_selection_returns_roles_without_readiness():
    project = SimpleNamespace(
        settings={
            "execution_mode": "passive",
            "translator_model": "stale/provider-model",
            "analyzer_model": "stale/analyzer-model",
            "researcher_model": "stale/researcher-model",
        },
        user_id=7,
    )
    with patch.object(worker, "require_model_ready", side_effect=AssertionError):
        assert worker._selected_model(project, "translator_model") == (
            "carlina.passive.translator"
        )
        assert worker._selected_model(project, "analyzer_model") == (
            "carlina.passive.translator.analysis"
        )
        assert worker._selected_model(project, "researcher_model") == (
            "carlina.passive.translator.research"
        )


def test_passive_translation_memory_never_opens_semantic_store():
    project = SimpleNamespace(settings={"execution_mode": "passive"})
    with patch.object(
        worker,
        "translation_memory_store",
        side_effect=AssertionError("passive translation must not embed source text"),
    ):
        assert worker._query_translation_memory(project, ["private source"]) == ""


def test_passive_ingestion_never_embeds_source(translator_db_app):
    owner = User(email="passive-index@example.test", password_hash="test")
    project = TranslationProject(
        user=owner,
        name="Passive indexing",
        source_lang="en",
        target_lang="pl",
        settings={"execution_mode": "passive"},
    )
    db.session.add_all([owner, project])
    db.session.commit()
    translator_db_app.rag_service = SimpleNamespace(is_available=True)

    with patch.object(
        worker,
        "translation_memory_store",
        side_effect=AssertionError("passive ingestion must not embed source text"),
    ):
        assert worker._assign_vec_ids_and_embed(project.id) is None


@pytest.fixture
def translator_db_app(tmp_path):
    application = Flask(__name__)
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'translator-worker.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="translator-worker-test",
        TESTING=True,
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


def test_translation_note_content_is_encrypted_and_projection_is_allowlisted(
    translator_db_app,
):
    owner = User(email="notes@example.test", password_hash="test")
    db.session.add(owner)
    db.session.flush()
    project = TranslationProject(
        user_id=owner.id,
        name="Notes",
        source_lang="en",
        target_lang="pl",
        settings={"enable_translator_notes": True},
    )
    db.session.add(project)
    db.session.flush()
    segment = TranslationSegment(
        project_id=project.id,
        order_index=0,
        block_index=0,
        html_tag="p",
        original_text="Hanshi Festival.",
        translated_text="Festiwal Hanshi.",
        status="needs_review",
    )
    db.session.add(segment)
    db.session.flush()
    note = TranslationNote(
        project_id=project.id,
        segment_id=segment.id,
        order_index=0,
        anchor_text="Festiwal Hanshi",
        body="Tradycyjnie wiązał się z **powstrzymaniem** od rozpalania ognia.",
        author_type="model",
        state="active",
        segment_version=segment.version,
        version=1,
    )
    db.session.add(note)
    db.session.commit()

    raw_anchor, raw_body = db.session.execute(
        text("SELECT anchor_text, body FROM translation_note")
    ).one()
    assert str(raw_anchor).startswith("enc:v2:")
    assert str(raw_body).startswith("enc:v2:")
    assert "Festiwal Hanshi" not in str(raw_anchor)
    assert "Tradycyjnie" not in str(raw_body)
    assert set(note.to_dict()) == {
        "id",
        "anchor_text",
        "body",
        "body_html",
        "author_type",
        "state",
        "segment_version",
        "version",
    }
    assert "**powstrzymaniem**" in note.to_dict()["body"]
    assert "<strong>powstrzymaniem</strong>" in note.to_dict()["body_html"]


def test_parse_target_output_rejects_repeated_ids_and_unsafe_markup():
    duplicate = """
    <target id="segment-1"><text>First.</text></target>
    <target id="segment-1"><text>Second.</text></target>
    """
    parsed, missing = worker._parse_target_output(duplicate, ["segment-1"])
    assert parsed == {}
    assert missing == ["segment-1"]


def test_block_ingestion_handler_rejects_legacy_projects_before_plan_access():
    project = SimpleNamespace(id="project-1", user_id=7, engine_contract="segment-v1")
    operation = SimpleNamespace(
        kind="plan_document_blocks",
        source_plan_id="plan-1",
        source_plan_hash="a" * 64,
    )
    payload = {
        "project_id": project.id,
        "operation_id": "operation-1",
        "user_id": project.user_id,
        "_operation": operation,
    }
    queue = SimpleNamespace()
    with (
        patch.object(worker.db.session, "get", return_value=project),
        patch.object(worker, "rehydrate_source_plan") as rehydrate,
    ):
        with pytest.raises(ValueError, match="block engine contract"):
            worker.plan_document_blocks.__wrapped__(payload, queue, "job-1")
    rehydrate.assert_not_called()


def test_block_ingestion_handler_rejects_missing_or_foreign_pins():
    project = SimpleNamespace(
        id="project-1",
        user_id=7,
        engine_contract="document-block-v1",
        source_content_hash="b" * 64,
    )
    operation = SimpleNamespace(
        kind="plan_document_blocks", source_plan_id=None, source_plan_hash=None
    )
    payload = {
        "project_id": project.id,
        "operation_id": "operation-1",
        "user_id": project.user_id,
        "_operation": operation,
    }
    with patch.object(worker.db.session, "get", return_value=project):
        with pytest.raises(ValueError, match="missing source-plan pins"):
            worker.plan_document_blocks.__wrapped__(payload, SimpleNamespace(), "job-1")


def test_block_ingestion_handler_rejects_wrong_operation_kind():
    project = SimpleNamespace(
        id="project-1",
        user_id=7,
        engine_contract="document-block-v1",
        source_content_hash="b" * 64,
    )
    operation = SimpleNamespace(
        kind="translate", source_plan_id="plan-1", source_plan_hash="a" * 64
    )
    payload = {
        "project_id": project.id,
        "operation_id": "operation-1",
        "user_id": project.user_id,
        "_operation": operation,
    }
    with patch.object(worker.db.session, "get", return_value=project):
        with pytest.raises(ValueError, match="block-plan operation"):
            worker.plan_document_blocks.__wrapped__(payload, SimpleNamespace(), "job-1")

    parsed, missing = worker._parse_target_output(
        '<target id="segment-1"><text><script>bad()</script>Safe</text></target>',
        ["segment-1"],
    )
    assert parsed == {}
    assert missing == ["segment-1"]


def test_block_translation_handler_persists_typed_partial_outcome(translator_db_app):
    translator_db_app.addon_manager = SimpleNamespace()
    owner = User(email="partial-blocks@example.test", password_hash="test")
    project = TranslationProject(
        user=owner,
        name="Partial blocks",
        source_lang="en",
        target_lang="pl",
        engine_contract="document-block-v1",
    )
    operation = translator_models.TranslationOperation(
        project=project,
        owner=owner,
        kind="document_block_translate",
        idempotency_key="partial-blocks",
        request_fingerprint="a" * 64,
        state="running",
    )
    db.session.add(operation)
    db.session.commit()
    result = {
        "contract_version": "translator-block-partial-v1",
        "source_plan_id": "plan-1",
        "source_plan_hash": "b" * 64,
        "workflow_config_id": "workflow-1",
        "workflow_config_hash": "c" * 64,
        "baseline_id": "baseline-1",
        "baseline_hash": "d" * 64,
        "census_id": "census-1",
        "census_hash": "e" * 64,
        "analysis_artifact_ids": {},
        "research_run_id": None,
        "window_attempt_ids": [],
        "active_revision_ids": [],
        "alignment_revision_ids": [],
        "translated_block_count": 0,
        "block_failure": {
            "attempt_id": "attempt-1",
            "window_order": 1,
            "affected_block_ids": ["block-1"],
            "dispatch_state": "normalization_failed",
            "failure_kind": "normalization_failed",
            "recovery_action": "translate_remaining",
            "remaining_dispatches": 0,
        },
    }
    partial = worker.BlockWorkflowPartialOutcome(
        operation=operation, result=result, progress=50, window_order=1
    )
    queue = MagicMock()
    with (
        patch.object(worker, "LLMService"),
        patch.object(worker, "run_document_block_workflow", side_effect=partial),
    ):
        response = worker.translate_document_blocks.__wrapped__(
            {"project_id": project.id, "_operation": operation}, queue, "job-partial"
        )

    db.session.expire_all()
    stored = db.session.get(translator_models.TranslationOperation, operation.id)
    assert stored is not None
    assert stored.state == "partial"
    assert stored.error_code == "block_window_incomplete"
    assert (
        stored.message
        == "Translation paused at window 1; completed windows were preserved."
    )
    assert (
        stored.result_json["block_failure"]["recovery_action"] == "translate_remaining"
    )
    assert response["operation"]["state"] == "partial"
    assert queue.set_status.call_args.args[0] == "job-partial"


def test_finish_synchronizes_only_linked_retranslation_terminals(translator_db_app):
    owner = User(email="retranslation-finish@example.test", password_hash="test")
    project = TranslationProject(
        user=owner,
        name="Retranslation finish",
        source_lang="en",
        target_lang="pl",
        engine_contract="document-block-v1",
    )
    db.session.add_all([owner, project])
    db.session.flush()
    source_plan = translator_models.TranslationSourcePlan(
        id="plan-1",
        project_id=project.id,
        contract_version="document-block-v1",
        source_document_id="doc-1",
        source_language="en",
        language_profile="default",
        namespace="source-plan-test",
        snapshot_contract_version="snapshot-v1",
        semantic_contract_version="semantic-v1",
        semantic_contract_mode="strict",
        semantic_content_hash="c" * 64,
        source_content_hash="c" * 64,
        snapshot_captured_at=worker.utc_now_db(),
        planner_version="planner-v1",
        planner_settings_hash="e" * 64,
        plan_hash="b" * 64,
        source_attestation={},
        planner_settings={},
        coverage_witness={},
        block_count=0,
        anchor_count=0,
    )
    db.session.add(source_plan)
    db.session.flush()
    operation = translator_models.TranslationOperation(
        project=project,
        owner=owner,
        kind="document_block_translate",
        idempotency_key="retranslation-finish",
        request_fingerprint="a" * 64,
        source_plan_id="plan-1",
        source_plan_hash="b" * 64,
        source_content_hash="c" * 64,
        state="running",
    )
    request = translator_models.TranslationRetranslationRequest(
        project_id=project.id,
        source_plan_id="plan-1",
        requester_user_id=owner.id,
        share_grant_id=None,
        scope_kind="blocks",
        scope_json={
            "requested": {"kind": "blocks", "block_ids": ["block-1"]},
            "resolved_block_ids": ["block-1"],
        },
        custom_prompt="",
        annotation_profile="none",
        research_choice="fresh_document",
        rationale="",
        state="started",
        operation_id=operation.id,
        idempotency_key="retranslation-request",
        request_fingerprint="d" * 64,
        version=2,
    )
    db.session.add(operation)
    db.session.flush()
    request.project_id = project.id
    request.requester_user_id = owner.id
    request.operation_id = operation.id
    db.session.add(request)
    db.session.commit()

    worker._finish(
        operation,
        MagicMock(),
        "job-retranslation-finish",
        "completed",
        message="done",
        result={},
    )

    db.session.refresh(request)
    assert request.state == "completed"
    assert request.version == 3


def test_finish_projects_cancellation_winner_to_queue_and_return_value(
    translator_db_app,
):
    owner = User(email="cancelled-finish@example.test", password_hash="test")
    project = TranslationProject(
        user=owner,
        name="Cancelled finish",
        source_lang="en",
        target_lang="pl",
        settings={
            "execution_mode": "passive",
            "mcp_execution_id": "c13a15c1-9f4c-4d49-a3ac-bb73f8061a6a",
        },
    )
    operation = translator_models.TranslationOperation(
        project=project,
        owner=owner,
        kind="document_block_translate",
        idempotency_key="cancelled-finish",
        request_fingerprint="f" * 64,
        state="cancel_requested",
        cancel_requested_at=worker.utc_now_db(),
        message="Cancellation requested.",
    )
    db.session.add_all([owner, project, operation])
    db.session.commit()
    queue = MagicMock()

    response = worker._finish(
        operation,
        queue,
        "job-cancelled-finish",
        "partial",
        message="Stale partial result.",
        result={"must": "not escape"},
        error_code="stale_partial",
        progress=75,
    )

    assert response["operation"]["state"] == "cancelled"
    assert response["message"] == "Cancelled at a safe worker boundary."
    assert response["result"] is None
    assert queue.set_status.call_args.args[1] == {
        "status": "cancelled",
        "progress": 75,
        "message": "Cancelled at a safe worker boundary.",
    }
    db.session.refresh(project)
    assert project.settings["execution_mode"] == "managed"
    assert "mcp_execution_id" not in project.settings


@pytest.mark.parametrize("legacy_payload", [False, True])
@pytest.mark.parametrize("reviewed_export", [False, True])
@pytest.mark.parametrize("windows_path", [False, True])
@pytest.mark.parametrize("image_mode", ["auto", "jpeg", "webp"])
def test_block_export_uses_verified_request_and_pinned_provider(
    translator_db_app,
    tmp_path,
    monkeypatch,
    legacy_payload,
    reviewed_export,
    windows_path,
    image_mode,
):
    translator_db_app.instance_path = str(tmp_path / "instance")
    project = SimpleNamespace(
        id="project-1",
        user_id=7,
        name="Example Book",
        engine_contract="document-block-v1",
        settings={"semantic_export_reference": {"version": 1}},
    )
    source_plan = SimpleNamespace(
        id="plan-1",
        project_id=project.id,
        plan_hash="b" * 64,
        source_content_hash="a" * 64,
    )
    operation = SimpleNamespace(
        id="operation-1",
        kind="document_block_export",
        source_plan_id=source_plan.id,
        source_plan_hash=source_plan.plan_hash,
        source_content_hash=source_plan.source_content_hash,
        request_fingerprint="c" * 64,
    )
    plan = SimpleNamespace(project_id=project.id)
    provider_reference = {
        "version": 1,
        "capability": "document.semantic_export",
        "provider_addon_id": "doc_processor",
        "provider_capability_id": "semantic_export",
    }
    data = b"verified epub bytes"
    artifact = {
        "format": "epub",
        "media_type": "application/epub+zip",
        "byte_length": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "validation_profile": "document-processor-semantic-export",
        "validation_profile_version": "1",
    }
    provider = SimpleNamespace(
        provider_reference=lambda: provider_reference,
        client=SimpleNamespace(
            semantic_export=lambda _request: {"artifact": artifact, "data": data}
        ),
    )
    translator_db_app.addon_manager = SimpleNamespace(
        get_loaded_addon=lambda _addon_id: SimpleNamespace(context=object())
    )
    request_value = SimpleNamespace(
        semantic_html_sha256="d" * 64,
        to_dict=lambda: {"semantic": "request"},
    )
    queue = MagicMock()
    payload = {
        "project_id": project.id,
        "_operation": operation,
        "format": "epub",
        "export_metadata": {"title": "Pinned title"},
        "semantic_export_provider": provider_reference,
    }
    if not legacy_payload:
        payload["numbering_revision"] = 0
    if image_mode != "auto":
        payload["epub_image_mode"] = image_mode
    manifest = {"sha256": "e" * 64, "requirement": "bilingual"}
    if reviewed_export:
        payload.update(review_manifest=manifest, review_requirement="bilingual")
    fingerprint_payload = {
        "format": "epub",
        "export_metadata": payload["export_metadata"],
        "semantic_export_provider": provider_reference,
        "source_plan_id": operation.source_plan_id,
        "source_plan_hash": operation.source_plan_hash,
        "source_content_hash": operation.source_content_hash,
    }
    if not legacy_payload:
        fingerprint_payload["numbering_revision"] = 0
    if image_mode != "auto":
        fingerprint_payload["epub_image_mode"] = image_mode
    if reviewed_export:
        fingerprint_payload.update(
            review_manifest=manifest, review_requirement="bilingual"
        )
    operation.request_fingerprint = worker.canonical_request_fingerprint(
        fingerprint_payload
    )

    if windows_path:

        class FakeWindowsDirectory:
            path = tmp_path / "instance" / "translator_exports" / operation.id

            def reserve_stage(self, _format, _final_name):
                self.path.mkdir(parents=True, exist_ok=True)
                stage = self.path / ".stage-test.epub"
                return stage.name, stage

            def publish(self, stage_name, final_name):
                (self.path / stage_name).replace(self.path / final_name)
                return len(data), hashlib.sha256(data).hexdigest()

            def remove_stage(self, stage_name):
                (self.path / stage_name).unlink(missing_ok=True)

            def close(self):
                pass

        monkeypatch.setattr(worker, "os", SimpleNamespace(name="nt"))
        monkeypatch.setattr(
            worker.WindowsArtifactDirectory,
            "prepare",
            lambda *_args: FakeWindowsDirectory(),
        )

    with (
        patch.object(
            worker.db.session,
            "get",
            side_effect=[project, source_plan],
        ),
        patch.object(worker, "rehydrate_source_plan", return_value=plan),
        patch.object(
            worker,
            "resolve_capability_provider",
            return_value=provider,
        ) as resolve_provider,
        patch(
            "iarf_assistant.addons.translator.numbering_sync.numbering_revision",
            return_value=0,
        ),
        patch.object(
            worker,
            "build_semantic_export_request",
            return_value=request_value,
        ) as build_request,
        patch.object(worker, "_check_cancel"),
        patch.object(worker, "update_progress", return_value=operation),
        patch.object(worker.db.session, "commit"),
        patch.object(worker, "_renew_lease", return_value=operation),
        patch.object(worker, "finish_operation") as finish,
        patch(
            "iarf_assistant.addons.translator.review_coverage.validate_export_review_snapshot"
        ) as validate_review,
        patch(
            "iarf_assistant.addons.translator.review_coverage.lock_export_review_snapshot"
        ) as lock_review,
    ):
        result = worker.export_document_blocks.__wrapped__(payload, queue, "job-1")

    assert result["format"] == "epub"
    assert result["mimetype"] == "application/epub+zip"
    output = (
        tmp_path
        / "instance"
        / "translator_exports"
        / operation.id
        / result["download_name"]
    )
    assert output.read_bytes() == data
    resolve_provider.assert_called_once()
    build_request.assert_called_once_with(
        source_plan,
        export_format="epub",
        start_h2_on_new_page=False,
        operation_witness={
            "id": operation.id,
            "request_fingerprint": operation.request_fingerprint,
            "source_content_hash": operation.source_content_hash,
            "source_plan_id": operation.source_plan_id,
            "source_plan_hash": operation.source_plan_hash,
            "numbering_revision": 0,
        },
        provider_witness=provider_reference,
        export_metadata={"title": "Pinned title"},
        cover_uri=None,
        epub_cover=None,
        epub_options=None,
        epub_image_mode=image_mode,
    )
    finish.assert_called_once_with(
        operation,
        "completed",
        message="Semantic export complete.",
        result={
            "format": "epub",
            "artifact": artifact,
            "delivery": result,
            **({"review_manifest": manifest} if reviewed_export else {}),
        },
    )
    if reviewed_export:
        validate_review.assert_called_once_with(project, manifest)
        lock_review.assert_called_once_with(project, manifest)
    else:
        validate_review.assert_not_called()
        lock_review.assert_not_called()


def test_block_export_rejects_queue_metadata_drift_even_after_checkpoint_replaced():
    operation = SimpleNamespace(
        checkpoint_json={"phase": "semantic_export"},
        source_content_hash="a" * 64, source_plan_id="plan-1", source_plan_hash="b" * 64,
    )
    payload = {"format": "epub", "export_metadata": {"title": "Pinned title"}, "semantic_export_provider": {}}
    operation.request_fingerprint = worker.canonical_request_fingerprint({
        **payload, "source_content_hash": operation.source_content_hash,
        "source_plan_id": operation.source_plan_id, "source_plan_hash": operation.source_plan_hash,
    })
    assert worker._export_metadata_for_operation(payload, operation) == {"title": "Pinned title"}

    with pytest.raises(ValueError, match="metadata changed"):
        worker._export_metadata_for_operation(
            {**payload, "export_metadata": {"title": "Changed title"}}, operation
        )


def test_block_export_review_manifest_is_bound_to_admission_fingerprint():
    operation = SimpleNamespace(source_content_hash="a" * 64, source_plan_id="plan-1", source_plan_hash="b" * 64)
    payload = {"format": "epub", "semantic_export_provider": {}, "review_requirement": "bilingual", "review_manifest": {"sha256": "c" * 64}}
    operation.request_fingerprint = worker.canonical_request_fingerprint({
        **payload, "source_content_hash": operation.source_content_hash,
        "source_plan_id": operation.source_plan_id, "source_plan_hash": operation.source_plan_hash,
    })
    assert worker._export_metadata_for_operation(payload, operation) == {}
    for changed in ({**payload, "review_manifest": {"sha256": "d" * 64}}, {key: value for key, value in payload.items() if key != "review_requirement"}):
        with pytest.raises(ValueError, match="metadata changed"):
            worker._export_metadata_for_operation(changed, operation)


def test_parse_target_output_accepts_safe_inline_markup_and_rejects_active_links():
    parsed, missing = worker._parse_target_output(
        '<target id="a"><text><b>Joined</b> <a href="https://example.test">target</a>.</text></target>',
        ["a"],
    )
    assert missing == []
    assert (
        parsed["a"]["text"]
        == '<b>Joined</b> <a href="https://example.test">target</a>.'
    )

    for unsafe in (
        '<a href="javascript:alert(1)">bad</a>',
        '<img src="x" onerror="alert(1)">',
        '<b onmouseover="alert(1)">bad</b>',
        '<span style="display:none">bad</span>',
    ):
        parsed, missing = worker._parse_target_output(
            f'<target id="a"><text>{unsafe}</text></target>', ["a"]
        )
        assert parsed == {}
        assert missing == ["a"]
    for href in ("/relative/path", "#chapter-two"):
        parsed, missing = worker._parse_target_output(
            f'<target id="a"><text><a href="{href}">safe</a></text></target>',
            ["a"],
        )
        assert missing == []
        assert href in parsed["a"]["text"]


def test_parse_target_output_accepts_the_segmenter_inline_fidelity_contract():
    markup = (
        "Hello<br/>World <kbd>Ctrl</kbd><wbr/>"
        "<ins>now</ins> <del>later</del> <samp>ok</samp> <var>x</var>."
    )
    parsed, missing = worker._parse_target_output(
        f'<target id="a"><text>{markup}</text></target>', ["a"]
    )

    assert missing == []
    assert parsed["a"]["text"] == markup


def test_target_contract_preserves_epub_footnote_structure_and_attributes():
    source = (
        '<sup id="ref-1" class="noteref"><a href="#fn-1" '
        'epub:type="noteref" role="doc-noteref" class="footnote-link">1</a></sup>'
    )
    parsed, missing, errors = worker._validate_target_output(
        f'<target id="a"><text>{source}</text></target>',
        ["a"],
        source_markup_by_id={"a": source},
    )

    assert missing == []
    assert errors == []
    payload = BeautifulSoup(parsed["a"]["text"], "html.parser")
    assert payload.sup.attrs == {"class": ["noteref"], "id": "ref-1"}
    assert payload.a.attrs == {
        "class": ["footnote-link"],
        "epub:type": "noteref",
        "href": "#fn-1",
        "role": "doc-noteref",
    }

    changed_link = source.replace('href="#fn-1"', 'href="#fn-2"')
    parsed, missing, errors = worker._validate_target_output(
        f'<target id="a"><text>{changed_link}</text></target>',
        ["a"],
        source_markup_by_id={"a": source},
    )
    assert parsed == {}
    assert missing == ["a"]
    assert errors == ["inline structure mismatch for a"]


@pytest.mark.parametrize(
    ("source", "moved"),
    [
        (
            'before <a href="#note">note</a> after',
            '<a href="#note">before note</a> after',
        ),
        (
            'text <sup><a href="#fn" epub:type="noteref">1</a></sup> after',
            '<sup><a href="#fn" epub:type="noteref">text 1</a></sup> after',
        ),
        (
            "plain <em>emphasized <strong>deep</strong></em> plain",
            "<em>plain emphasized <strong>deep</strong></em> plain",
        ),
    ],
)
def test_target_contract_rejects_text_moved_across_inline_boundaries(source, moved):
    parsed, missing, errors = worker._validate_target_output(
        f'<target id="a"><text>{moved}</text></target>',
        ["a"],
        source_markup_by_id={"a": source},
    )

    assert parsed == {}
    assert missing == ["a"]
    assert errors == ["inline structure mismatch for a"]


def test_target_contract_allows_changed_text_in_the_same_inline_slots():
    source = 'before <a href="#note">note</a> after'
    translated = 'wcześniej <a href="#note">przypis</a> później'
    parsed, missing, errors = worker._validate_target_output(
        f'<target id="a"><text>{translated}</text></target>',
        ["a"],
        source_markup_by_id={"a": source},
    )

    assert missing == []
    assert errors == []
    assert parsed["a"]["text"] == translated


def test_target_contract_preserves_multilingual_hidden_inline_semantics():
    source = '<span xml:lang="fr" hidden>Bonjour</span>'
    parsed, missing, errors = worker._validate_target_output(
        f'<target id="a"><text>{source}</text></target>',
        ["a"],
        source_markup_by_id={"a": source},
    )

    assert missing == []
    assert errors == []
    assert "xml:lang" in parsed["a"]["text"]
    changed = source.replace('xml:lang="fr"', 'xml:lang="de"')
    parsed, missing, errors = worker._validate_target_output(
        f'<target id="a"><text>{changed}</text></target>',
        ["a"],
        source_markup_by_id={"a": source},
    )
    assert parsed == {}
    assert missing == ["a"]
    assert errors == ["inline structure mismatch for a"]


def test_parse_target_output_rejects_nonliteral_review_and_oversized_fields():
    parsed, missing = worker._parse_target_output(
        '<target id="a" needs_review="TRUE"><text>Safe</text></target>', ["a"]
    )
    assert parsed == {}
    assert missing == ["a"]
    parsed, missing = worker._parse_target_output(
        f'<target id="a"><text>{"x" * (worker.MAX_TARGET_TEXT_LENGTH + 1)}</text></target>',
        ["a"],
    )
    assert parsed == {}
    assert missing == ["a"]


def test_translation_prompt_matches_exact_id_parser_contract():
    assert "A+B" not in DEFAULT_TRANSLATOR_SYSTEM_PROMPT
    assert "exactly once" in DEFAULT_TRANSLATOR_SYSTEM_PROMPT
    assert 'needs_review="true"' in worker.TARGET_OUTPUT_CONTRACT
    assert "exactly one non-empty `<text>" in worker.TARGET_OUTPUT_CONTRACT
    assert "only top-level sibling `<target>`" in worker.TARGET_OUTPUT_CONTRACT


def test_minimal_saved_translation_prompt_still_receives_canonical_output_contract(
    translator_db_app,
):
    project = SimpleNamespace(
        id="project-1",
        source_lang="English",
        target_lang="Polish",
        settings={"custom_prompt": ""},
        working_memory=None,
        user=SimpleNamespace(
            addon_settings={
                "translator": {
                    "prompt_translator_agent": (
                        "Translate {source_lang} to {target_lang}; keep literal JSON "
                        '{"tone":"literary"} and escaped {{source_lang}}.'
                    )
                }
            }
        ),
    )
    batch = [SimpleNamespace(id="segment-1", original_text="Source text.")]

    with patch.object(worker, "_query_translation_memory", return_value=""):
        messages = worker._translation_messages(project, batch)

    system = messages[0]["content"]
    assert system.startswith(
        'Translate English to Polish; keep literal JSON {"tone":"literary"} '
        "and escaped {source_lang}."
    )
    assert "exactly one non-empty `<text>...</text>` child" in system
    assert "no XML/HTML wrapper" in system


def test_analysis_chunks_bound_oversized_source_without_silent_loss():
    source = "word " * 25_000
    chunks = worker._analysis_chunks(
        [SimpleNamespace(original_text=source)], maximum=1_000, overlap=200
    )
    assert chunks
    assert all(len(chunk) <= 1_000 for chunk in chunks)
    assert "".join(chunks).replace("\n", " ").count("word") >= 25_000


def test_translate_batch_conditional_write_does_not_overwrite_human_edit(
    translator_db_app,
):
    owner = User(email="worker@example.test", password_hash="test")
    db.session.add(owner)
    db.session.flush()
    project = TranslationProject(
        user_id=owner.id,
        name="Draft",
        source_lang="en",
        target_lang="pl",
    )
    db.session.add(project)
    db.session.flush()
    segment = TranslationSegment(
        project_id=project.id,
        order_index=0,
        block_index=0,
        html_tag="p",
        outer_tag="p",
        original_text="Source.",
        status="pending",
        version=1,
    )
    db.session.add(segment)
    db.session.commit()

    def model_response(**_kwargs):
        TranslationSegment.query.filter_by(id=segment.id).update(
            {
                TranslationSegment.translated_text: "Human edit.",
                TranslationSegment.status: "human_approved",
                TranslationSegment.version: TranslationSegment.version + 1,
            },
            synchronize_session=False,
        )
        db.session.commit()
        message = SimpleNamespace(
            tool_calls=[],
            content=f'<target id="{segment.id}"><text>AI edit.</text></target>',
            model_dump=lambda: {"role": "assistant"},
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    queue = MagicMock()
    queue.is_cancel_requested.return_value = False
    with (
        patch.object(worker, "_check_cancel"),
        patch.object(
            worker, "_renew_lease", side_effect=lambda operation, *_: operation
        ) as renew_lease,
        patch.object(worker, "_selected_model", return_value="configured"),
        patch.object(
            worker,
            "LLMService",
            return_value=SimpleNamespace(completion=model_response),
        ),
    ):
        written, conflicts, failed = worker._translate_batch(
            project,
            SimpleNamespace(),
            queue,
            "job-1",
            [segment],
            expected_versions={segment.id: 1},
        )
    db.session.expire_all()
    current = db.session.get(TranslationSegment, segment.id)
    assert (written, conflicts, failed) == (0, 1, 0)
    assert current.translated_text == "Human edit."
    assert current.status == "human_approved"
    assert current.version == 2
    assert renew_lease.called


def test_worker_delivery_reenters_after_operation_claim_crash(
    translator_db_app,
):
    owner = User(email="claim-recovery@example.test", password_hash="test")
    db.session.add(owner)
    db.session.flush()
    project = TranslationProject(
        user_id=owner.id,
        name="Claim recovery",
        source_lang="en",
        target_lang="pl",
    )
    db.session.add(project)
    db.session.flush()
    operation, _created = translator_operations.create_operation(
        project,
        kind="translate",
        request_payload={},
        idempotency_key="claim-recovery",
    )
    translator_operations.claim_operation(operation.id, "job-recovery")
    db.session.commit()

    observed = []
    guarded = worker._guard_project_job(
        lambda payload, _task_queue, job_id: observed.append((payload, job_id))
    )
    payload = {
        "project_id": project.id,
        "operation_id": operation.id,
        "user_id": owner.id,
    }
    task_queue = SimpleNamespace(set_status=lambda *_args, **_kwargs: None)

    result = guarded(payload, task_queue, "job-recovery")

    assert result is None
    assert len(observed) == 1
    assert observed[0][1] == "job-recovery"


def test_worker_publishes_actionable_batch_budget_failure(translator_db_app):
    owner = User(email="batch-budget-failure@example.test", password_hash="test")
    db.session.add(owner)
    db.session.flush()
    project = TranslationProject(
        user_id=owner.id,
        name="Batch budget failure",
        source_lang="en",
        target_lang="pl",
    )
    db.session.add(project)
    db.session.flush()
    operation, _created = translator_operations.create_operation(
        project,
        kind="document_block_translate",
        request_payload={},
        idempotency_key="batch-budget-failure",
    )
    db.session.commit()

    def reject_window(*_args, **_kwargs):
        raise block_contract.BlockContractError("window-oversized-block")

    queue = MagicMock()
    queue.is_cancel_requested.return_value = False
    guarded = worker._guard_project_job(reject_window)
    payload = {
        "project_id": project.id,
        "operation_id": operation.id,
        "user_id": owner.id,
    }

    with pytest.raises(
        block_contract.BlockContractError, match="window-oversized-block"
    ):
        guarded(payload, queue, "job-batch-budget")

    db.session.expire_all()
    stored = db.session.get(translator_models.TranslationOperation, operation.id)
    assert stored.state == "failed"
    assert stored.error_code == "translation_batch_too_small"
    assert stored.message == (
        "The translation batch budget is too small for the next complete block "
        "and its required context. Raise the budget and try again."
    )
    queue.set_status.assert_called_with(
        "job-batch-budget",
        {"status": "failed", "message": stored.message},
    )


def test_document_segment_worker_handler_is_removed():
    assert not hasattr(worker, "segment_document")


def test_translation_memory_is_same_owner_language_pair_only(translator_db_app):
    owner = User(email="memory@example.test", password_hash="test")
    db.session.add(owner)
    db.session.flush()

    def project(name, target):
        value = TranslationProject(
            user_id=owner.id,
            name=name,
            source_lang="en",
            target_lang=target,
        )
        db.session.add(value)
        db.session.flush()
        return value

    current, compatible, incompatible = (
        project("Current", "pl"),
        project("Compatible", "pl"),
        project("Incompatible", "de"),
    )
    for value, vector_id, source, target in (
        (current, 11, "Current source", "Current target"),
        (compatible, 12, "Past source", "Past target"),
        (incompatible, 13, "German source", "German target"),
        (current, 14, "Unreviewed source", "Unreviewed target"),
    ):
        db.session.add(
            TranslationSegment(
                project_id=value.id,
                order_index=0,
                block_index=0,
                html_tag="p",
                outer_tag="p",
                original_text=source,
                translated_text=target,
                status="needs_review" if vector_id == 14 else "approved",
                vec_id=vector_id,
            )
        )
    db.session.commit()
    store = MagicMock()
    store.search_semantic.return_value = [(12, 0.9, 0.1)]
    translator_db_app.rag_service = SimpleNamespace(is_available=True)
    with patch.object(worker, "translation_memory_store", return_value=store):
        context = worker._query_translation_memory(current, ["query"])
    allowed = store.search_semantic.call_args.kwargs["allowed_row_ids"]
    assert 12 in allowed
    assert 13 not in allowed
    assert 14 not in allowed
    assert "Past target" in context
    assert "German target" not in context


def test_stable_vector_ids_are_positive_deterministic_and_unique_sample():
    ids = [worker._stable_vector_id(f"segment-{index}") for index in range(500)]
    assert ids == [worker._stable_vector_id(f"segment-{index}") for index in range(500)]
    assert len(ids) == len(set(ids))
    assert all(0 < value < 2**63 for value in ids)


def test_translation_provider_failures_stop_after_two_attempts(translator_db_app):
    owner = User(email="retry-translation@example.test", password_hash="test")
    project = TranslationProject(
        user=owner, name="Retry", source_lang="en", target_lang="pl"
    )
    segment = TranslationSegment(
        project=project,
        order_index=0,
        block_index=0,
        html_tag="p",
        outer_tag="p",
        original_text="Source.",
        version=1,
    )
    db.session.add_all([owner, project, segment])
    db.session.commit()
    completion = MagicMock(side_effect=RuntimeError("provider down"))
    with (
        patch.object(worker, "_check_cancel"),
        patch.object(
            worker, "_renew_lease", side_effect=lambda operation, *_: operation
        ),
        patch.object(worker, "_selected_model", return_value="configured"),
        patch.object(
            worker,
            "LLMService",
            return_value=SimpleNamespace(completion=completion),
        ),
    ):
        assert worker._translate_batch(
            project,
            SimpleNamespace(),
            MagicMock(),
            "job-1",
            [segment],
            expected_versions={segment.id: 1},
        ) == (0, 0, 1)
    assert completion.call_count == 2


def test_translation_tool_burst_executes_only_the_total_budget(translator_db_app):
    owner = User(email="tool-burst@example.test", password_hash="test")
    project = TranslationProject(
        user=owner, name="Burst", source_lang="en", target_lang="pl"
    )
    segment = TranslationSegment(
        project=project,
        order_index=0,
        block_index=0,
        html_tag="p",
        outer_tag="p",
        original_text="Source.",
        version=1,
    )
    db.session.add_all([owner, project, segment])
    db.session.commit()
    calls = [
        SimpleNamespace(
            id=f"call-{index}",
            function=SimpleNamespace(
                name="search_source_document", arguments='{"query":"x"}'
            ),
        )
        for index in range(20)
    ]
    message = SimpleNamespace(
        tool_calls=calls,
        content=None,
        model_dump=lambda: {"role": "assistant"},
    )
    handler = MagicMock(
        return_value={
            "role": "tool",
            "tool_call_id": "call",
            "name": "search_source_document",
            "content": "{}",
        }
    )
    with (
        patch.object(worker, "_check_cancel"),
        patch.object(
            worker, "_renew_lease", side_effect=lambda operation, *_: operation
        ),
        patch.object(worker, "_max_tool_calls", return_value=2),
        patch.object(worker, "_selected_model", return_value="configured"),
        patch.object(
            worker,
            "LLMService",
            return_value=SimpleNamespace(
                completion=MagicMock(
                    return_value=SimpleNamespace(
                        choices=[SimpleNamespace(message=message)]
                    )
                )
            ),
        ),
        patch.object(worker, "_handle_agent_tool_call", handler),
    ):
        assert worker._translate_batch(
            project,
            SimpleNamespace(),
            MagicMock(),
            "job-1",
            [segment],
            expected_versions={segment.id: 1},
        ) == (0, 0, 1)
    assert handler.call_count == 2


def test_translation_memory_prompt_is_bounded(translator_db_app):
    owner = User(email="memory-bounds@example.test", password_hash="test")
    project = TranslationProject(
        user=owner, name="Memory bounds", source_lang="en", target_lang="pl"
    )
    db.session.add_all([owner, project])
    db.session.flush()
    for index in range(8):
        db.session.add(
            TranslationSegment(
                project=project,
                order_index=index,
                block_index=index,
                html_tag="p",
                outer_tag="p",
                original_text=f"source-{index}-" + "x" * 2_000,
                translated_text=f"target-{index}-" + "y" * 2_000,
                status="approved",
                vec_id=100 + index,
            )
        )
    db.session.commit()
    store = MagicMock()
    store.search_semantic.return_value = [(100 + index, 0.1, 0.1) for index in range(8)]
    translator_db_app.rag_service = SimpleNamespace(is_available=True)
    with patch.object(worker, "translation_memory_store", return_value=store):
        context = worker._query_translation_memory(project, ["query"])
    assert 0 < context.count("Target:") <= worker.MAX_TRANSLATION_MEMORY_HITS
    assert len(context) <= worker.MAX_TRANSLATION_MEMORY_TOTAL_LENGTH + 100
    assert "x" * (worker.MAX_TRANSLATION_MEMORY_SOURCE_LENGTH + 1) not in context
    assert "y" * (worker.MAX_TRANSLATION_MEMORY_TARGET_LENGTH + 1) not in context


def test_translate_segments_reports_exact_aggregate_counts(translator_db_app):
    owner = User(email="aggregate-counts@example.test", password_hash="test")
    project = TranslationProject(
        user=owner,
        name="Aggregate counts",
        source_lang="en",
        target_lang="pl",
        engine_contract="segment-v1",
        settings={"source_type": "subtitle_studio", "chunk_size_chars": 1},
    )
    segments = [
        TranslationSegment(
            project=project,
            order_index=index,
            block_index=index,
            html_tag="p",
            outer_tag="p",
            original_text=source,
            status="pending",
            version=1,
        )
        for index, source in enumerate(("A", "B"))
    ]
    db.session.add_all([owner, project, *segments])
    db.session.commit()
    finished = MagicMock(side_effect=lambda *_args, **kwargs: kwargs["result"])
    handler = worker.translate_segments.__wrapped__
    with (
        patch.object(worker, "_selected_model", return_value="configured"),
        patch.object(worker, "_check_cancel"),
        patch.object(worker, "update_progress", side_effect=lambda op, **_kwargs: op),
        patch.object(
            worker,
            "_translate_batch",
            side_effect=[(1, 0, 0), (0, 1, 0)],
        ),
        patch.object(worker, "_finish", finished),
    ):
        result = handler(
            {"project_id": project.id, "_operation": SimpleNamespace()},
            MagicMock(),
            "job-1",
        )
    assert result == {
        "translated": 1,
        "failed": 0,
        "conflicts": 1,
        "total": 2,
        "research_model_calls": 0,
    }
    assert finished.call_args.args[3] == "partial"


def test_analysis_provider_failures_stop_after_two_attempts(translator_db_app):
    owner = User(email="retry-analysis@example.test", password_hash="test")
    project = TranslationProject(
        user=owner,
        name="Retry analysis",
        source_lang="en",
        target_lang="pl",
        settings={"analyzer_model": "configured"},
    )
    segment = TranslationSegment(
        project=project,
        order_index=0,
        block_index=0,
        html_tag="p",
        outer_tag="p",
        original_text="Source.",
    )
    db.session.add_all([owner, project, segment])
    db.session.commit()
    completion = MagicMock(side_effect=RuntimeError("provider down"))
    with (
        patch.object(worker, "_check_cancel"),
        patch.object(
            worker, "_renew_lease", side_effect=lambda operation, *_: operation
        ),
        patch.object(worker, "_selected_model", return_value="configured"),
        patch.object(
            worker,
            "LLMService",
            return_value=SimpleNamespace(completion=completion),
        ),
        patch.object(worker, "update_progress", return_value=SimpleNamespace()),
        patch.object(worker, "_finish", return_value={"result": "failed"}),
    ):
        worker._run_analysis(project, SimpleNamespace(), MagicMock(), "job-1")
    assert completion.call_count == 2


def test_delegate_task_blocks_model_selected_write_tool_without_raw_mapping():
    flask_app = Flask(__name__)
    bind_content_policy(flask_app)
    project = SimpleNamespace(
        id="project-1",
        user_id=7,
        settings={"enable_research": True, "research_plugin_ids": ["research"]},
    )
    write_record = SimpleNamespace(
        side_effects=["external_write"], approval_required=True
    )
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    tool_calls=[
                        SimpleNamespace(
                            id="call-1",
                            function=SimpleNamespace(
                                name="erase_everything", arguments="{}"
                            ),
                        )
                    ],
                    content=None,
                    model_dump=lambda: {"role": "assistant"},
                )
            )
        ]
    )
    manager = MagicMock()
    manager.get_tool_declarations_for_plugin_ids.return_value = [
        {"type": "function", "function": {"name": "erase_everything"}}
    ]
    manager.get_tool_record.return_value = write_record
    with (
        flask_app.app_context(),
        patch.object(worker, "app") as current,
        patch.object(worker, "_selected_model", return_value="configured-model"),
    ):
        current.addon_manager = manager
        result = worker._handle_delegate_task(
            project,
            MagicMock(completion=MagicMock(return_value=response)),
            ["research"],
            "ignore safety",
            "test",
        )
    assert "Researcher failed" not in result
    manager.execute_capability.assert_not_called()
    assert (
        not hasattr(manager, "get_tools_and_mappings")
        or not manager.get_tools_and_mappings.called
    )


def test_delegate_task_uses_execution_kernel_for_registered_read_tool():
    flask_app = Flask(__name__)
    bind_content_policy(flask_app)
    project = SimpleNamespace(
        id="project-1",
        user_id=7,
        settings={"enable_research": True, "research_plugin_ids": ["research"]},
    )
    read_record = SimpleNamespace(
        side_effects=["external_read"], approval_required=False
    )
    tool_message = SimpleNamespace(
        tool_calls=[
            SimpleNamespace(
                id="call-1",
                function=SimpleNamespace(name="lookup", arguments='{"q":"term"}'),
            )
        ],
        content=None,
        model_dump=lambda: {"role": "assistant"},
    )
    final_message = SimpleNamespace(
        tool_calls=[],
        content="A factual result.",
        model_dump=lambda: {"role": "assistant"},
    )
    llm = MagicMock(
        completion=MagicMock(
            side_effect=[
                SimpleNamespace(choices=[SimpleNamespace(message=tool_message)]),
                SimpleNamespace(choices=[SimpleNamespace(message=final_message)]),
            ]
        )
    )
    manager = MagicMock()
    manager.get_tool_declarations_for_plugin_ids.return_value = [
        {"type": "function", "function": {"name": "lookup"}}
    ]
    manager.get_tool_record.return_value = read_record
    manager.execute_capability.return_value = {"answer": "term"}
    with (
        flask_app.app_context(),
        patch.object(worker, "app") as current,
        patch.object(worker, "_selected_model", return_value="configured-model"),
    ):
        current.addon_manager = manager
        assert (
            worker._handle_delegate_task(
                project, llm, ["research"], "find term", "test"
            )
            == "A factual result."
        )
    manager.execute_capability.assert_called_once_with(
        "lookup",
        {"q": "term"},
        {"verbose": False, "current_user_id": 7, "db_session": worker.db.session},
        requester_user_id=7,
        audit_log_source_id="job.translator",
        log_source_ids=("job.translator",),
    )
    assert all(
        "max_completion_tokens" not in call.kwargs
        for call in llm.completion.call_args_list
    )


def test_research_result_is_not_silently_truncated_to_four_thousand_characters():
    conclusion = "evidence " * 1_000

    assert len(conclusion) > 4_000
    assert worker._bounded_research_result(conclusion) == conclusion


def test_provider_output_aliases_use_the_capability_admitted_limit():
    parameters = {
        "max_completion_tokens": 128_000,
        "max_output_tokens": 64_000,
        "unrelated": True,
    }

    worker.llm_service._apply_admitted_output_limit(
        parameters, admitted_output_tokens=8_192
    )

    assert parameters == {
        "max_completion_tokens": 8_192,
        "max_output_tokens": 8_192,
        "unrelated": True,
    }


def test_research_requires_explicit_project_enablement():
    project = SimpleNamespace(settings={"research_plugin_ids": ["research"]})
    assert worker._research_allowed_plugins(project, ["research"]) == []


def test_research_provider_outage_is_safe_and_bounded():
    flask_app = Flask(__name__)
    bind_content_policy(flask_app)
    project = SimpleNamespace(
        id="project-1",
        user_id=7,
        settings={"enable_research": True, "research_plugin_ids": ["research"]},
    )
    manager = MagicMock()
    manager.get_tool_declarations_for_plugin_ids.return_value = [
        {"type": "function", "function": {"name": "lookup"}}
    ]
    llm = MagicMock(completion=MagicMock(side_effect=RuntimeError("offline")))
    with (
        flask_app.app_context(),
        patch.object(worker, "app") as current,
        patch.object(worker, "_selected_model", return_value="configured-model"),
    ):
        current.addon_manager = manager
        assert (
            worker._handle_delegate_task(project, llm, ["research"], "find", "test")
            == "Research unavailable: the researcher model did not respond."
        )
    assert llm.completion.call_count == 2


def test_research_honors_the_operation_wide_model_call_budget():
    flask_app = Flask(__name__)
    bind_content_policy(flask_app)
    project = SimpleNamespace(
        id="project-1",
        user_id=7,
        settings={"enable_research": True, "research_plugin_ids": ["research"]},
    )
    manager = MagicMock()
    manager.get_tool_declarations_for_plugin_ids.return_value = [
        {"type": "function", "function": {"name": "lookup"}}
    ]
    llm = MagicMock()
    with (
        flask_app.app_context(),
        patch.object(worker, "app") as current,
        patch.object(worker, "_selected_model", return_value="configured-model"),
    ):
        current.addon_manager = manager
        result = worker._handle_delegate_task(
            project,
            llm,
            ["research"],
            "find",
            "test",
            research_budget={"remaining": 0},
        )

    assert "operation-wide model-call budget" in result
    llm.completion.assert_not_called()


def test_tool_args_are_strict_and_memory_capacity_is_truthful(translator_db_app):
    owner = User(email="tool-args@example.test", password_hash="test")
    project = TranslationProject(
        user=owner, name="Tools", source_lang="en", target_lang="pl"
    )
    db.session.add_all([owner, project])
    db.session.commit()
    malformed = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name="submit_glossary_terms", arguments="[]"),
    )
    response = worker._handle_agent_tool_call(
        project, MagicMock(), malformed, delegate_source="test"
    )
    assert json.loads(response["content"])["status"] == "error"
    assert GlossaryTerm.query.filter_by(project_id=project.id).count() == 0

    valid = SimpleNamespace(
        id="call-2",
        function=SimpleNamespace(
            name="submit_glossary_terms",
            arguments=json.dumps(
                {
                    "terms": [
                        {
                            "source_term": "API",
                            "target_term": "API",
                            "notes": "A proper noun",
                            "needs_review": False,
                        }
                    ]
                }
            ),
        ),
    )
    response = worker._handle_agent_tool_call(
        project, MagicMock(), valid, delegate_source="test"
    )
    assert json.loads(response["content"])["added"] == 1
    project.working_memory = "x" * worker.MAX_WORKING_MEMORY_LENGTH
    updated, message = worker._append_working_memory(project, "This will not fit.")
    assert not updated
    assert "capacity" in message


def test_glossary_markdown_escapes_untrusted_cells():
    markdown = worker._build_glossary_markdown(
        [
            SimpleNamespace(
                source_term="a|b",
                target_term="c\nd",
                context_notes="e|f\ng",
                needs_review=False,
                status="approved",
                is_user_defined=False,
            )
        ]
    )
    assert "a\\|b" in markdown
    assert "c<br>d" in markdown
    assert "e\\|f<br>g" in markdown


def test_glossary_prompt_prioritizes_relevant_terms_before_truncation():
    terms = [
        SimpleNamespace(
            source_term="Unrelated",
            target_term="Poboczny",
            context_notes="",
            needs_review=False,
            status="approved",
            is_user_defined=True,
        ),
        SimpleNamespace(
            source_term="RelevantTerm",
            target_term="Właściwy termin",
            context_notes="",
            needs_review=False,
            status="approved",
            is_user_defined=False,
        ),
    ]
    with patch.object(worker, "MAX_GLOSSARY_PROMPT_TERMS", 1):
        markdown = worker._build_glossary_markdown(
            terms, source_text="This batch contains RelevantTerm."
        )

    assert "RelevantTerm" in markdown
    assert "Unrelated" not in markdown
    assert "truncated" in markdown


def test_glossary_tool_refines_at_capacity_but_cannot_add_more(
    translator_db_app, monkeypatch
):
    owner = User(email="glossary-capacity@example.test", password_hash="test")
    project = TranslationProject(
        user=owner, name="Bounded glossary", source_lang="en", target_lang="pl"
    )
    db.session.add_all([owner, project])
    db.session.flush()
    db.session.add(
        GlossaryTerm(
            project_id=project.id,
            source_term="Existing",
            target_term="Old",
            context_notes="",
            is_user_defined=False,
            needs_review=False,
            status="approved",
        )
    )
    db.session.commit()
    monkeypatch.setattr(worker, "MAX_PROJECT_GLOSSARY_TERMS", 1)
    call = SimpleNamespace(
        id="call-capacity",
        function=SimpleNamespace(
            name="submit_glossary_terms",
            arguments=json.dumps(
                {
                    "terms": [
                        {
                            "source_term": "Existing",
                            "target_term": "Refined",
                            "notes": "",
                            "needs_review": False,
                        },
                        {
                            "source_term": "New",
                            "target_term": "Nowy",
                            "notes": "",
                            "needs_review": False,
                        },
                    ]
                }
            ),
        ),
    )

    payload = json.loads(
        worker._handle_agent_tool_call(
            project, MagicMock(), call, delegate_source="test"
        )["content"]
    )

    assert payload["status"] == "partial"
    assert payload["refined"] == 1
    assert payload["capacity"] == 1
    assert GlossaryTerm.query.filter_by(project_id=project.id).count() == 1
    assert (
        GlossaryTerm.query.filter_by(project_id=project.id).one().target_term
        == "Refined"
    )


def test_glossary_cache_refreshes_exact_unicode_source_term(translator_db_app):
    owner = User(email="glossary-unicode@example.test", password_hash="test")
    project = TranslationProject(
        user=owner, name="Unicode glossary", source_lang="pl", target_lang="en"
    )
    db.session.add_all([owner, project])
    db.session.flush()
    term = GlossaryTerm(
        project_id=project.id,
        source_term="Źródło",
        target_term="Source",
        context_notes="",
        is_user_defined=True,
        needs_review=False,
        status="approved",
    )
    db.session.add(term)
    db.session.commit()

    cache: list[GlossaryTerm] = []
    worker._refresh_glossary_cache(cache, project.id, ["Źródło"])

    assert [row.source_term for row in cache] == ["Źródło"]
