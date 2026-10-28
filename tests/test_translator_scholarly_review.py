"""Focused persistence tests for the scholarly-review service boundary."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from content_storage_support import bind_content_policy
from flask import Flask
from flask_login import LoginManager, login_user
from sqlalchemy.exc import OperationalError

import iarf_assistant.addons.translator.scholarly_review as review_service
from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION
from carlina.core.utc import utc_now_db
from iarf_assistant.addons.translator import api_client
from iarf_assistant.addons.translator.alignment_persistence import (
    persist_alignment_revision,
    persist_target_span_set,
)
from iarf_assistant.addons.translator.block_contract import (
    AttestedDocumentSnapshot,
)
from iarf_assistant.addons.translator.block_persistence import (
    persist_source_plan,
    source_attestation_for_project,
)
from iarf_assistant.addons.translator.block_planner import plan_document_blocks
from iarf_assistant.addons.translator.block_workspace_api import (
    build_block_glossary_payload,
    load_block_source_plan,
    persist_manual_candidate,
)
from iarf_assistant.addons.translator.models import (
    TranslationGlossaryActive,
    TranslationGlossarySuggestion,
    TranslationProject,
    TranslationReaderNote,
    TranslationReaderNoteRevision,
    TranslationResearchDossier,
    TranslationResearchDossierRevision,
    TranslationShareGrant,
    TranslationTargetBlockActive,
    TranslationTargetBlockRevision,
)
from iarf_assistant.addons.translator.scholarly_review import (
    ReviewActor,
    ScholarlyReviewError,
    annotation_profile_notes_enabled,
    attach_retranslation_dossier,
    block_translation_dossier_pin,
    create_glossary_suggestion,
    create_reader_note,
    create_retranslation_request,
    issue_share_grant,
    list_block_reviews,
    list_glossary_suggestions,
    list_reader_notes,
    materialize_model_reader_notes,
    normalize_annotation_profile,
    record_block_review,
    redeem_share_grant,
    resolve_glossary_suggestion,
    resolve_session,
    resolve_share_grant,
    retranslation_dossier_pin,
    revalidate_reader_notes_for_target,
    revise_reader_note,
    revoke_share_grant,
    submit_research_dossier,
    transition_reader_note,
    transition_retranslation_request,
    validate_session_csrf,
)
from iarf_assistant.addons.translator.target_contract import (
    TARGET_BLOCK_RESULT_CONTRACT_VERSION,
    TargetBlockCandidate,
    TargetTextPart,
    structural_fingerprint,
)
from iarf_assistant.addons.translator.target_persistence import (
    activate_target_revision,
    persist_target_revision,
)
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.database_utils import DatabaseBusyError
from iarf_assistant.extensions import db
from iarf_assistant.models import User


@pytest.fixture
def app(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CARLINA_SQLITE_BUSY_TIMEOUT_MS", "250")
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'review.db'}",
        SQLALCHEMY_ENGINE_OPTIONS={"connect_args": {"timeout": 0.05}},
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="scholarly-review-test",
        TESTING=True,
    )
    db.init_app(application)
    login_manager = LoginManager()
    login_manager.init_app(application)

    @login_manager.user_loader
    def _load_user(user_id: str):
        return db.session.get(User, int(user_id))

    context = application.app_context()
    context.push()
    key_store = InstallationKeyStore(tmp_path / "master-keys.json")
    key_store.initialize()
    application.extensions["carlina_encryption"] = EncryptionService(key_store)
    db.create_all()
    with db.engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys = ON")
    try:
        yield application
    finally:
        db.session.remove()
        with db.engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
            db.metadata.drop_all(connection)
        db.engine.dispose()
        context.pop()


def _project_and_plan(
    source_html: str = "<html><body><p>Dao is a term.</p><p>Second source.</p></body></html>"
) -> tuple[TranslationProject, object, list[object], User]:
    owner = User(email="scholarly-owner@example.test", password_hash="test")
    db.session.add(owner)
    db.session.flush()
    project = TranslationProject(
        user_id=owner.id,
        name="Scholarly review",
        source_lang="en",
        target_lang="pl",
        status="setup",
        engine_contract="document-block-v1",
        document_id="doc-review",
        source_content_hash=hashlib.sha256(source_html.encode()).hexdigest(),
        settings={
            "source_type": "document",
            "source_reference": {
                "version": 1,
                "capability": "document.processing",
                "provider_addon_id": "doc-provider",
                "provider_capability_id": "document-processing",
                "resource_type": "document",
                "resource_id": "doc-review",
            },
        },
    )
    db.session.add(project)
    db.session.flush()
    digest = hashlib.sha256(source_html.encode()).hexdigest()
    snapshot = AttestedDocumentSnapshot(
        id="doc-review",
        document_id="doc-review",
        output_html=source_html,
        content_hash=digest,
        semantic_content_hash=digest,
        metadata={},
        provenance={},
        captured_at="2026-09-01T12:00:00+00:00",
        semantic_contract_version=SEMANTIC_HTML_CONTRACT_VERSION,
    )
    plan = plan_document_blocks(
        snapshot,
        project_id=project.id,
        source_attestation=source_attestation_for_project(project),
        source_language="en",
    )
    record, _ = persist_source_plan(project, plan)
    db.session.commit()
    return project, record, list(plan.blocks), owner


def test_profiles_and_dossier_idempotency_keep_external_unverified_label(app):
    project, _record, blocks, owner = _project_and_plan()
    assert normalize_annotation_profile(False) == "none"
    assert normalize_annotation_profile(True) == "restrained"
    assert annotation_profile_notes_enabled("study")
    payload = {
        "document_brief": "A short scholarly brief.",
        "terminology": [
            {
                "source_term": "Dao",
                "target_term": "Dao",
                "notes": "Keep the source term.",
                "source_block_ids": [blocks[0].id],
                "confidence": 0.75,
            }
        ],
    }
    first = submit_research_dossier(
        project,
        client_submission_id="client-1",
        payload=payload,
        provenance={"method": "claimed-external", "models": ["model-x"]},
        scope={"kind": "blocks", "block_ids": [blocks[0].id]},
        actor=ReviewActor("owner", owner.id),
    )
    repeated = submit_research_dossier(
        project,
        client_submission_id="client-1",
        payload=payload,
        provenance={"method": "claimed-external", "models": ["model-x"]},
        scope={"kind": "blocks", "block_ids": [blocks[0].id]},
        actor=ReviewActor("owner", owner.id),
    )
    assert repeated["revision_id"] == first["revision_id"]
    assert first["project_id"] == project.id
    assert first["source_plan_id"] == _record.id
    assert first["conclusion_provenance"] == "external_unverified"
    assert first["evidence_verification_state"] == "unverified"
    appended = submit_research_dossier(
        project,
        client_submission_id="client-2",
        payload=payload,
        provenance={"method": "claimed-external", "models": ["model-x"]},
        scope={"kind": "blocks", "block_ids": [blocks[0].id]},
        actor=ReviewActor("owner", owner.id),
        dossier_id=first["dossier_id"],
        expected_revision=1,
    )
    assert appended["version"] == 2
    original_retry = submit_research_dossier(
        project,
        client_submission_id="client-1",
        payload=payload,
        provenance={"method": "claimed-external", "models": ["model-x"]},
        scope={"kind": "blocks", "block_ids": [blocks[0].id]},
        actor=ReviewActor("owner", owner.id),
    )
    assert original_retry["revision_id"] == first["revision_id"]
    with pytest.raises(ScholarlyReviewError, match="dossier idempotency conflict"):
        submit_research_dossier(
            project,
            client_submission_id="client-1",
            payload={**payload, "document_brief": "changed"},
            provenance={},
            scope={"kind": "blocks", "block_ids": [blocks[0].id]},
            actor=ReviewActor("owner", owner.id),
        )


def test_dossier_annotations_and_glossary_suggestions_are_not_active(app):
    project, _record, blocks, owner = _project_and_plan()
    payload = {
        "annotation_proposals": [
            {
                "block_id": blocks[0].id,
                "anchor_kind": "source",
                "anchor_text": "Dao",
                "occurrence_index": 0,
                "body": "A proposed annotation.",
                "reference_ids": [],
            }
        ],
        "terminology": [
            {
                "source_term": "Dao",
                "target_term": "Dao",
                "notes": None,
                "source_block_ids": [blocks[0].id],
                "confidence": None,
            },
            {
                "source_term": "Dao",
                "target_term": "Dao",
                "notes": "A second evidence item.",
                "source_block_ids": [blocks[0].id],
                "confidence": None,
            },
        ],
    }
    submit_research_dossier(
        project,
        client_submission_id="client-annotations",
        payload=payload,
        provenance={},
        scope={"kind": "blocks", "block_ids": [blocks[0].id]},
        actor=ReviewActor("owner", owner.id),
    )
    assert list_reader_notes(project) == []
    proposed = list_reader_notes(project, include_proposed=True)
    assert len(proposed) == 1
    assert proposed[0]["state"] == "proposed"
    suggestions = list_glossary_suggestions(project)
    assert len(suggestions) == 2
    assert all(item["state"] == "proposed" for item in suggestions)
    glossary = build_block_glossary_payload(project)
    assert glossary["proposals"] == []
    assert len(glossary["suggestions"]) == 2
    assert TranslationGlossaryActive.query.count() == 0


def _retry_dossier_payload(block_id: str) -> dict[str, object]:
    return {
        "document_brief": "A retried scholarly brief.",
        "terminology": [
            {
                "source_term": "Dao",
                "target_term": "Dao",
                "notes": "Retain the source term.",
                "source_block_ids": [block_id],
                "confidence": 0.8,
            }
        ],
    }


def test_mcp_dossier_retries_complete_transaction_after_independent_sqlite_lock(
    app, monkeypatch
):
    project, _record, blocks, owner = _project_and_plan()
    client = api_client.TranslatorClient(
        SimpleNamespace(), {"current_user_id": owner.id}
    )
    payload = _retry_dossier_payload(blocks[0].id)
    project_id = str(project.id)
    lock_ready = threading.Event()
    flush_entered = threading.Event()
    allow_flush = threading.Event()
    flush_finished = threading.Event()
    database_path = Path(db.engine.url.database)

    def hold_lock() -> None:
        connection = sqlite3.connect(
            database_path, timeout=0.01, check_same_thread=False
        )
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE translation_project SET name = name WHERE id = ?",
                (project_id,),
            )
            lock_ready.set()
            assert flush_entered.wait(5)
            allow_flush.set()
            assert flush_finished.wait(5)
            connection.commit()
        finally:
            connection.close()

    real_flush = review_service._flush
    project_loads: list[str] = []
    real_project = client._project

    def load_project(value: str):
        project_loads.append(value)
        return real_project(value)

    def observe_flush() -> None:
        flush_entered.set()
        assert allow_flush.wait(5)
        try:
            real_flush()
        finally:
            flush_finished.set()

    monkeypatch.setattr(review_service, "_flush", observe_flush)
    monkeypatch.setattr(client, "_project", load_project)
    db.session.rollback()
    writer = threading.Thread(target=hold_lock, daemon=True)
    writer.start()
    assert lock_ready.wait(5)
    try:
        first = client.research_dossier_submit(
            project_id,
            "mcp-lock-retry",
            payload,
            {},
            {"kind": "blocks", "block_ids": [blocks[0].id]},
        )
    finally:
        writer.join(timeout=5)
    assert not writer.is_alive()
    assert len(project_loads) > 1

    repeated = client.research_dossier_submit(
        project_id,
        "mcp-lock-retry",
        payload,
        {},
        {"kind": "blocks", "block_ids": [blocks[0].id]},
    )
    with pytest.raises(ScholarlyReviewError, match="dossier idempotency conflict"):
        client.research_dossier_submit(
            project_id,
            "mcp-lock-retry",
            {**payload, "document_brief": "A conflicting brief."},
            {},
            {"kind": "blocks", "block_ids": [blocks[0].id]},
        )

    assert repeated["revision_id"] == first["revision_id"]
    assert TranslationResearchDossier.query.filter_by(project_id=project_id).count() == 1
    assert (
        TranslationResearchDossierRevision.query.filter_by(project_id=project_id).count()
        == 1
    )
    assert (
        TranslationGlossarySuggestion.query.filter_by(project_id=project_id).count()
        == 1
    )


def test_mcp_dossier_lock_retry_exhaustion_leaves_no_partial_rows(app, monkeypatch):
    project, _record, blocks, owner = _project_and_plan()
    client = api_client.TranslatorClient(
        SimpleNamespace(), {"current_user_id": owner.id}
    )
    project_id = str(project.id)
    lock_ready = threading.Event()
    release_lock = threading.Event()
    database_path = Path(db.engine.url.database)

    def hold_lock() -> None:
        connection = sqlite3.connect(
            database_path, timeout=0.01, check_same_thread=False
        )
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE translation_project SET name = name WHERE id = ?",
                (project_id,),
            )
            lock_ready.set()
            assert release_lock.wait(5)
            connection.rollback()
        finally:
            connection.close()

    writer = threading.Thread(target=hold_lock, daemon=True)
    db.session.rollback()
    writer.start()
    assert lock_ready.wait(5)
    monkeypatch.setenv("CARLINA_DB_WRITE_RETRY_SECONDS", "0.2")
    try:
        with pytest.raises(DatabaseBusyError):
            client.research_dossier_submit(
                project_id,
                "mcp-lock-exhausted",
                _retry_dossier_payload(blocks[0].id),
                {},
                {"kind": "blocks", "block_ids": [blocks[0].id]},
            )
    finally:
        release_lock.set()
        writer.join(timeout=5)
    assert not writer.is_alive()
    assert TranslationResearchDossier.query.filter_by(project_id=project_id).count() == 0
    assert (
        TranslationResearchDossierRevision.query.filter_by(project_id=project_id).count()
        == 0
    )
    assert (
        TranslationGlossarySuggestion.query.filter_by(project_id=project_id).count()
        == 0
    )


def test_dossier_glossary_operational_error_is_rethrown_unchanged(app, monkeypatch):
    project, _record, _blocks, owner = _project_and_plan()
    expected = OperationalError(
        "glossary write failed", {}, sqlite3.OperationalError("database is locked")
    )

    def fail_glossary(*_args: object, **_kwargs: object):
        raise expected

    monkeypatch.setattr(
        review_service, "persist_analysis_glossary_proposals", fail_glossary
    )
    with pytest.raises(OperationalError) as failure:
        submit_research_dossier(
            project,
            client_submission_id="glossary-operational-error",
            payload={},
            provenance={},
            scope={"kind": "document", "block_ids": []},
            actor=ReviewActor("owner", owner.id),
        )
    assert failure.value is expected
    db.session.rollback()


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "https://alice:password@example.test/source",
        "https://example.test/source?token=secret",
        "https://example.test/source#secret",
        "https://example.test/source\nmore",
    ],
)
def test_dossier_references_reject_secret_bearing_urls(app, unsafe_url):
    project, _record, _blocks, owner = _project_and_plan()
    payload = {
        "references": [
            {
                "reference_id": "ref-1",
                "title": "Reference",
                "authors": "Researcher",
                "url": unsafe_url,
                "locator": None,
                "note": None,
            }
        ]
    }
    with pytest.raises(ScholarlyReviewError, match="dossier reference url"):
        submit_research_dossier(
            project,
            client_submission_id="unsafe-reference",
            payload=payload,
            provenance={},
            scope={"kind": "document", "block_ids": []},
            actor=ReviewActor("owner", owner.id),
        )


def test_reader_note_query_is_bound_to_the_exact_source_plan(app, monkeypatch):
    project, record, blocks, _owner = _project_and_plan()
    observed: list[dict[str, object] | None] = []

    def no_notes(_model, filters=None):
        observed.append(filters)
        return []

    monkeypatch.setattr(review_service, "_query_all", no_notes)
    assert review_service._note_rows(project, record, blocks) == []
    assert observed == [{"project_id": project.id, "source_plan_id": record.id}]


def test_scholarly_revision_receipts_fail_closed_after_tampering(app):
    project, _record, blocks, owner = _project_and_plan()
    actor = ReviewActor("owner", owner.id)
    dossier = submit_research_dossier(
        project,
        client_submission_id="receipt-dossier",
        payload={"document_brief": "Verified conclusions."},
        provenance={"method": "external"},
        scope={"kind": "document", "block_ids": []},
        actor=actor,
    )
    note = create_reader_note(
        project,
        block_id=blocks[0].id,
        body="Verified note.",
        actor=actor,
    )
    db.session.commit()

    dossier_revision = db.session.get(
        TranslationResearchDossierRevision, dossier["revision_id"]
    )
    note_revision = db.session.get(
        TranslationReaderNoteRevision, note["revision_id"]
    )
    assert dossier_revision is not None and len(dossier_revision.integrity_receipt) == 64
    assert note_revision is not None and len(note_revision.integrity_receipt) == 64

    dossier_revision.payload_json = {"document_brief": "Tampered conclusions."}
    with pytest.raises(ScholarlyReviewError, match="dossier integrity"):
        review_service.get_research_dossier(
            project, dossier["dossier_id"], include_payload=True
        )
    db.session.rollback()

    note_revision = db.session.get(TranslationReaderNoteRevision, note["revision_id"])
    assert note_revision is not None
    note_revision.body = "Tampered note."
    with pytest.raises(ScholarlyReviewError, match="note integrity"):
        list_reader_notes(project)


def test_reader_note_lifecycle_and_compare_and_swap(app):
    project, _record, blocks, owner = _project_and_plan()
    actor = ReviewActor("owner", owner.id)
    note = create_reader_note(
        project,
        block_id=blocks[0].id,
        body="A useful note.",
        citations=["ref-1"],
        actor=actor,
    )
    revised = revise_reader_note(
        project,
        note["id"],
        expected_head_version=1,
        body="An edited note.",
        actor=actor,
    )
    assert revised["version"] == 2
    with pytest.raises(ScholarlyReviewError) as conflict:
        revise_reader_note(
            project,
            note["id"],
            expected_head_version=1,
            body="stale",
            actor=actor,
        )
    assert conflict.value.code == "note-version-conflict"
    assert conflict.value.latest["version"] == 2
    active = transition_reader_note(
        project,
        note["id"],
        expected_head_version=2,
        next_state="needs_review",
        actor=actor,
    )
    assert active["state"] == "needs_review"
    retired = transition_reader_note(
        project,
        note["id"],
        expected_head_version=3,
        next_state="retired",
        actor=actor,
    )
    assert retired["state"] == "retired"
    assert list_reader_notes(project) == []


def test_reader_note_rejects_reserved_internal_citation(app):
    project, _record, blocks, owner = _project_and_plan()
    with pytest.raises(ScholarlyReviewError) as exc:
        create_reader_note(
            project,
            block_id=blocks[0].id,
            body="A forged legacy marker must not be accepted.",
            citations=["legacy-target-note-v1:forged"],
            actor=ReviewActor("owner", owner.id),
        )
    assert exc.value.code == "note-citations"


def test_reader_note_writes_are_durably_idempotent(app):
    project, _record, blocks, owner = _project_and_plan()
    actor = ReviewActor("owner", owner.id)
    created = create_reader_note(
        project,
        block_id=blocks[0].id,
        body="Stable note.",
        actor=actor,
        idempotency_key="create-note-1",
    )
    replayed = create_reader_note(
        project,
        block_id=blocks[0].id,
        body="Stable note.",
        actor=actor,
        idempotency_key="create-note-1",
    )
    assert replayed == created
    assert TranslationReaderNote.query.count() == 1
    assert TranslationReaderNoteRevision.query.count() == 1

    with pytest.raises(ScholarlyReviewError) as conflict:
        create_reader_note(
            project,
            block_id=blocks[0].id,
            body="Different note.",
            actor=actor,
            idempotency_key="create-note-1",
        )
    assert conflict.value.code == "note-idempotency-conflict"

    revised = revise_reader_note(
        project,
        created["id"],
        expected_head_version=1,
        body="Stable revision.",
        actor=actor,
        idempotency_key="revise-note-1",
    )
    revision_replay = revise_reader_note(
        project,
        created["id"],
        expected_head_version=1,
        body="Stable revision.",
        actor=actor,
        idempotency_key="revise-note-1",
    )
    assert revision_replay == revised
    assert TranslationReaderNoteRevision.query.count() == 2

    with pytest.raises(ScholarlyReviewError) as revision_conflict:
        revise_reader_note(
            project,
            created["id"],
            expected_head_version=1,
            body="Different revision.",
            actor=actor,
            idempotency_key="revise-note-1",
        )
    assert revision_conflict.value.code == "note-idempotency-conflict"


def test_target_note_revalidation_is_idempotent_by_target_revision(app):
    project, record, _blocks, owner = _project_and_plan()
    workspace = load_block_source_plan(project)
    block = workspace.plan.blocks[0]
    candidate = {
        "contract_version": TARGET_BLOCK_RESULT_CONTRACT_VERSION,
        "source_plan_id": record.id,
        "source_plan_hash": workspace.plan.plan_hash,
        "block_id": block.id,
        "structural_fingerprint": structural_fingerprint(workspace.plan, block),
        "parts": [{"kind": "text", "text": "Dao is a term."}],
        "lexical_attributes": [],
    }
    persist_manual_candidate(
        project, block.id, candidate, expected_active_version=0, author_user_id=owner.id
    )
    active = db.session.get(
        TranslationTargetBlockActive,
        {"source_plan_id": record.id, "block_id": block.id},
    )
    assert active is not None
    note = create_reader_note(
        project,
        block_id=block.id,
        body="Target note.",
        actor=ReviewActor("owner", owner.id),
        anchor={"kind": "target", "anchor_text": "Dao", "occurrence_index": 0},
    )
    first = revalidate_reader_notes_for_target(
        project,
        block_id=block.id,
        target_revision_id=active.revision_id,
        target_version=active.version,
    )
    assert first[0]["revision_id"] == note["revision_id"]
    second = revalidate_reader_notes_for_target(
        project,
        block_id=block.id,
        target_revision_id=active.revision_id,
        target_version=active.version,
    )
    assert second[0]["revision_id"] == note["revision_id"]
    assert second[0]["version"] == 1


def test_target_note_anchor_includes_preserved_hebrew_literal(app, monkeypatch):
    project, record, _blocks, owner = _project_and_plan(
        "<html><body><h2><span lang=\"he\">&#x05d0;</span> Heading</h2></body></html>"
    )
    workspace = load_block_source_plan(project)
    block = next(
        item
        for item in workspace.plan.blocks
        if any(slot.disposition == "literal-preserved" for slot in item.slots)
    )
    parts: list[TargetTextPart] = []
    slots = {slot.id: slot for slot in block.slots}
    for wire_part in block.wire:
        if wire_part.kind == "token":
            parts.append(TargetTextPart(kind="token", token_id=wire_part.ref_id))
            continue
        slot = slots[wire_part.ref_id]
        if slot.disposition == "literal-preserved":
            parts.append(TargetTextPart(kind="literal", literal_slot_id=slot.id))
        elif slot.disposition == "translatable":
            parts.append(TargetTextPart(kind="text", text=slot.source_value))
    candidate = {
        "contract_version": TARGET_BLOCK_RESULT_CONTRACT_VERSION,
        "source_plan_id": record.id,
        "source_plan_hash": workspace.plan.plan_hash,
        "block_id": block.id,
        "structural_fingerprint": structural_fingerprint(workspace.plan, block),
        "parts": [part.to_dict() for part in parts],
        "lexical_attributes": [],
    }
    persist_manual_candidate(
        project, block.id, candidate, expected_active_version=0, author_user_id=owner.id
    )
    active = db.session.get(
        TranslationTargetBlockActive,
        {"source_plan_id": record.id, "block_id": block.id},
    )
    assert active is not None
    target_revision = db.session.get(TranslationTargetBlockRevision, active.revision_id)
    assert target_revision is not None
    note = create_reader_note(
        project,
        block_id=block.id,
        body="Hebrew heading note.",
        actor=ReviewActor("owner", owner.id),
        anchor={"kind": "target", "anchor_text": "א", "occurrence_index": 0},
    )
    assert note["anchor_text"] == "א"
    assert note["target_revision_id"] == active.revision_id
    persisted_note_revision = db.session.get(
        TranslationReaderNoteRevision, note["revision_id"]
    )
    assert persisted_note_revision is not None
    assert persisted_note_revision.target_revision_id == active.revision_id
    monkeypatch.setattr(
        review_service,
        "_workspace",
        lambda _project: pytest.fail("activation context unexpectedly reloaded"),
    )
    first = revalidate_reader_notes_for_target(
        project,
        block_id=block.id,
        target_revision_id=active.revision_id,
        target_version=active.version,
        _source_plan=workspace.record,
        _blocks=workspace.plan.blocks,
        _current_target=(active, target_revision),
    )
    second = revalidate_reader_notes_for_target(
        project,
        block_id=block.id,
        target_revision_id=active.revision_id,
        target_version=active.version,
        _source_plan=workspace.record,
        _blocks=workspace.plan.blocks,
        _current_target=(active, target_revision),
    )
    assert first[0]["revision_id"] == note["revision_id"]
    assert second[0]["revision_id"] == note["revision_id"]
    assert second[0]["version"] == 1
    assert (
        materialize_model_reader_notes(
            project,
            target_revision,
            [],
            _source_plan=workspace.record,
            _blocks=workspace.plan.blocks,
            _current_target=(active, target_revision),
        )
        == []
    )


def test_legacy_model_note_materialization_uses_activation_context(app, monkeypatch):
    project, record, _blocks, _owner = _project_and_plan()
    workspace = load_block_source_plan(project)
    block = workspace.plan.blocks[0]
    candidate = TargetBlockCandidate(
        source_plan_id=record.id,
        source_plan_hash=workspace.plan.plan_hash,
        block_id=block.id,
        structural_fingerprint=structural_fingerprint(workspace.plan, block),
        parts=(TargetTextPart(kind="text", text="Dao is a term."),),
        lexical_attributes=(),
    )
    target_revision = persist_target_revision(
        record,
        candidate,
        target_language="pl",
        author_type="model",
        verified_plan=workspace.plan,
        notes=[{"anchor": "Dao", "body": "Legacy model note."}],
    )
    span_set = persist_target_span_set(record, target_revision)
    alignment = persist_alignment_revision(record, span_set)
    pointer = activate_target_revision(
        record,
        block_id=block.id,
        revision_id=target_revision.id,
        alignment_revision_id=alignment.id,
        expected_version=0,
    )
    monkeypatch.setattr(
        review_service,
        "_workspace",
        lambda _project: pytest.fail("activation context unexpectedly reloaded"),
    )
    first = materialize_model_reader_notes(
        project,
        target_revision,
        _source_plan=record,
        _blocks={item.id: item for item in workspace.plan.blocks},
        _current_target=(pointer, target_revision, candidate),
    )
    second = materialize_model_reader_notes(
        project,
        target_revision,
        _source_plan=record,
        _blocks=workspace.plan.blocks,
        _current_target=(pointer, target_revision, candidate),
    )
    assert len(first) == len(second) == 1
    assert first[0]["id"] == second[0]["id"]
    assert first[0]["author_type"] == "model"
    assert first[0]["state"] == "active"
    assert TranslationReaderNote.query.filter_by(project_id=project.id).count() == 1

    with pytest.raises(ScholarlyReviewError, match="target projection"):
        materialize_model_reader_notes(
            project,
            target_revision,
            _source_plan=record,
            _blocks=workspace.plan.blocks,
            _current_target={"other-block": (pointer, target_revision, candidate)},
        )


def test_share_grants_store_only_digests_and_csrf_is_rotatable(app):
    project, _record, _blocks, owner = _project_and_plan()
    owner_actor = ReviewActor("owner", owner.id)
    projection, raw_grant = issue_share_grant(
        project, label="Reviewer", role="reviewer", actor=owner_actor
    )
    assert raw_grant.startswith("trg_")
    stored = db.session.get(TranslationShareGrant, projection["id"])
    assert raw_grant not in repr(stored)
    reviewer = resolve_share_grant(project, raw_grant)
    assert reviewer.role == "reviewer"
    _session, raw_session, raw_csrf = redeem_share_grant(project, raw_grant)
    assert raw_session.startswith("trs_")
    assert validate_session_csrf(project, raw_session, raw_csrf)
    _rotated, next_csrf = __import__(
        "iarf_assistant.addons.translator.scholarly_review",
        fromlist=["rotate_session_csrf"],
    ).rotate_session_csrf(project, raw_session)
    assert not validate_session_csrf(project, raw_session, raw_csrf)
    assert validate_session_csrf(project, raw_session, next_csrf)
    assert resolve_session(project, raw_session).role == "reviewer"
    revoke_share_grant(project, projection["id"], actor=owner_actor)
    with pytest.raises(ScholarlyReviewError, match="grant invalid"):
        resolve_share_grant(project, raw_grant)

    viewer_projection, viewer_raw = issue_share_grant(
        project, label="Viewer", role="viewer", actor=owner_actor
    )
    viewer = resolve_share_grant(project, viewer_raw)
    assert list_reader_notes(project, actor=viewer) == []
    with pytest.raises(ScholarlyReviewError, match="review mutation forbidden"):
        create_reader_note(
            project, block_id=_blocks[0].id, body="No write.", actor=viewer
        )
    with pytest.raises(ScholarlyReviewError, match="review read forbidden"):
        list_reader_notes(project, include_proposed=True, actor=viewer)
    assert viewer_projection["status"] == "active"


def test_review_cas_and_reviewer_cannot_execute_retranslation(app):
    project, _record, blocks, owner = _project_and_plan()
    owner_actor = ReviewActor("owner", owner.id)
    _grant, raw = issue_share_grant(
        project, label="Reviewer", role="reviewer", actor=owner_actor
    )
    reviewer = resolve_share_grant(project, raw)
    suggestion = create_glossary_suggestion(
        project,
        "Dao",
        "Dao",
        "Keep the spelling.",
        reviewer,
        "suggestion-1",
    )
    assert suggestion["proposer_kind"] == "reviewer"
    repeat_suggestion = create_glossary_suggestion(
        project,
        "Dao",
        "Dao",
        "Keep the spelling.",
        reviewer,
        "suggestion-1",
    )
    assert repeat_suggestion["id"] == suggestion["id"]
    with pytest.raises(ScholarlyReviewError, match="idempotency conflict"):
        create_glossary_suggestion(
            project,
            "Dao",
            "The Way",
            "Keep the spelling.",
            reviewer,
            "suggestion-1",
        )
    with app.test_request_context():
        login_user(owner)
        accepted = resolve_glossary_suggestion(
            project,
            suggestion["id"],
            1,
            "accepted",
            actor=owner_actor,
        )
    assert accepted["accepted_revision_id"]
    assert TranslationGlossaryActive.query.count() == 1
    review = record_block_review(
        project,
        blocks[0].id,
        "approved",
        "Looks good.",
        expected_version=0,
        expected_target_version=None,
        actor=reviewer,
        idempotency_key="review-1",
    )
    assert review["decision"] == "approved"
    assert review["effective_decision"] == "pending"
    assert review["stale"] is True
    review_replay = record_block_review(
        project,
        blocks[0].id,
        "approved",
        "Looks good.",
        expected_version=0,
        expected_target_version=None,
        actor=reviewer,
        idempotency_key="review-1",
    )
    assert review_replay == review
    with pytest.raises(ScholarlyReviewError, match="idempotency conflict"):
        record_block_review(
            project,
            blocks[0].id,
            "needs_changes",
            "Different use of the same request key.",
            expected_version=0,
            expected_target_version=None,
            actor=reviewer,
            idempotency_key="review-1",
        )
    with pytest.raises(ScholarlyReviewError, match="review version conflict"):
        record_block_review(
            project,
            blocks[0].id,
            "needs_changes",
            "stale",
            expected_version=0,
            expected_target_version=None,
            actor=reviewer,
            idempotency_key="review-2",
        )
    request = create_retranslation_request(
        project,
        scope={"kind": "blocks", "block_ids": [blocks[0].id]},
        custom_prompt="Please reconsider the wording.",
        annotation_profile="restrained",
        research_choice="await_external",
        dossier_revision_id=None,
        rationale="Reader feedback.",
        idempotency_key="request-1",
        actor=reviewer,
    )
    repeat_request = create_retranslation_request(
        project,
        scope={"kind": "blocks", "block_ids": [blocks[0].id]},
        custom_prompt="Please reconsider the wording.",
        annotation_profile="restrained",
        research_choice="await_external",
        dossier_revision_id=None,
        rationale="Reader feedback.",
        idempotency_key="request-1",
        actor=reviewer,
    )
    assert repeat_request["id"] == request["id"]
    with pytest.raises(ScholarlyReviewError, match="retranslation owner required"):
        transition_retranslation_request(
            db.session.get(
                __import__(
                    "iarf_assistant.addons.translator.models",
                    fromlist=["TranslationRetranslationRequest"],
                ).TranslationRetranslationRequest,
                request["id"],
            ),
            1,
            "started",
            actor=reviewer,
        )
    transitioned = transition_retranslation_request(
        db.session.get(
            __import__(
                "iarf_assistant.addons.translator.models",
                fromlist=["TranslationRetranslationRequest"],
            ).TranslationRetranslationRequest,
            request["id"],
        ),
        1,
        "started",
        actor=owner_actor,
    )
    assert transitioned["state"] == "started"


def test_historical_approval_does_not_follow_a_new_active_target(app):
    project, record, _blocks, owner = _project_and_plan()
    workspace = load_block_source_plan(project)
    block = workspace.plan.blocks[0]

    def candidate(text: str) -> dict[str, object]:
        return {
            "contract_version": TARGET_BLOCK_RESULT_CONTRACT_VERSION,
            "source_plan_id": record.id,
            "source_plan_hash": workspace.plan.plan_hash,
            "block_id": block.id,
            "structural_fingerprint": structural_fingerprint(workspace.plan, block),
            "parts": [{"kind": "text", "text": text}],
            "lexical_attributes": [],
        }

    persist_manual_candidate(
        project,
        block.id,
        candidate("Dao is a term."),
        expected_active_version=0,
        author_user_id=owner.id,
    )
    original_target = db.session.get(
        TranslationTargetBlockActive,
        {"source_plan_id": record.id, "block_id": block.id},
    )
    assert original_target is not None
    original_target_revision_id = original_target.revision_id
    original_target_version = original_target.version
    approved = record_block_review(
        project,
        block.id,
        "approved",
        "Reviewed v1.",
        expected_version=0,
        expected_target_version=original_target_version,
        actor=ReviewActor("owner", owner.id),
        idempotency_key="approve-v1",
    )
    assert approved["decision"] == "approved"
    assert approved["effective_decision"] == "approved"
    assert approved["target_revision_id"] == original_target_revision_id

    persist_manual_candidate(
        project,
        block.id,
        candidate("Dao is a revised term."),
        expected_active_version=1,
        author_user_id=owner.id,
    )
    current_target = db.session.get(
        TranslationTargetBlockActive,
        {"source_plan_id": record.id, "block_id": block.id},
    )
    assert current_target is not None
    assert current_target.revision_id != original_target_revision_id

    current_review = list_block_reviews(
        project, block_ids=[block.id], actor=ReviewActor("owner", owner.id)
    )[0]
    assert current_review["decision"] == "approved"
    assert current_review["effective_decision"] == "pending"
    assert current_review["stale"] is True
    assert current_review["target_revision_id"] == original_target_revision_id
    assert current_review["current_target_revision_id"] == current_target.revision_id
    assert current_review["current_target_version"] == current_target.version == 2
    assert current_review["author_type"] == "owner"
    assert current_review["author_user_id"] == owner.id


def test_retranslation_rejects_fresh_scope_and_owner_attaches_covering_dossier(app):
    project, _record, blocks, owner = _project_and_plan()
    actor = ReviewActor("owner", owner.id)
    with pytest.raises(ScholarlyReviewError, match="retranslation research choice"):
        create_retranslation_request(
            project,
            scope={"kind": "blocks", "block_ids": [blocks[0].id]},
            custom_prompt="",
            annotation_profile="none",
            research_choice="fresh_scope",
            dossier_revision_id=None,
            rationale="",
            idempotency_key="fresh-scope",
            actor=actor,
        )

    request = create_retranslation_request(
        project,
        scope={"kind": "blocks", "block_ids": [block.id for block in blocks]},
        custom_prompt="Use the submitted evidence.",
        annotation_profile="study",
        research_choice="await_external",
        dossier_revision_id=None,
        rationale="Research is being supplied separately.",
        idempotency_key="await-external",
        actor=actor,
    )
    partial = submit_research_dossier(
        project,
        client_submission_id="partial-dossier",
        payload={"document_brief": "Only the first block is covered."},
        provenance={},
        scope={"kind": "blocks", "block_ids": [blocks[0].id]},
        actor=actor,
    )
    with pytest.raises(ScholarlyReviewError, match="retranslation dossier scope"):
        attach_retranslation_dossier(
            project,
            request["id"],
            dossier_revision_id=partial["revision_id"],
            expected_version=1,
            actor=actor,
        )
    with pytest.raises(ScholarlyReviewError, match="retranslation dossier scope"):
        create_retranslation_request(
            project,
            scope={"kind": "blocks", "block_ids": [block.id for block in blocks]},
            custom_prompt="",
            annotation_profile="study",
            research_choice="reuse_dossier",
            dossier_revision_id=partial["revision_id"],
            rationale="",
            idempotency_key="partial-reuse",
            actor=actor,
        )
    complete = submit_research_dossier(
        project,
        client_submission_id="document-dossier",
        payload={"document_brief": "The whole document is covered."},
        provenance={},
        scope={"kind": "document", "block_ids": []},
        actor=actor,
    )
    attached = attach_retranslation_dossier(
        project,
        request["id"],
        dossier_revision_id=complete["revision_id"],
        expected_version=1,
        actor=actor,
    )
    assert attached["research_choice"] == "reuse_dossier"
    assert attached["dossier_revision_id"] == complete["revision_id"]
    row = db.session.get(
        __import__(
            "iarf_assistant.addons.translator.models",
            fromlist=["TranslationRetranslationRequest"],
        ).TranslationRetranslationRequest,
        attached["id"],
    )
    assert retranslation_dossier_pin(row)["revision_id"] == complete["revision_id"]
    assert attached["version"] == 2
    with pytest.raises(ScholarlyReviewError, match="retranslation version conflict"):
        attach_retranslation_dossier(
            project,
            request["id"],
            dossier_revision_id=complete["revision_id"],
            expected_version=1,
            actor=actor,
        )


def test_block_translation_pin_requires_full_plan_and_authenticates_revision(app):
    project, _record, blocks, owner = _project_and_plan()
    full = submit_research_dossier(
        project,
        client_submission_id="full-block-start",
        payload={"document_brief": "The whole document is covered."},
        provenance={},
        scope={"kind": "document", "block_ids": []},
        actor=ReviewActor("owner", owner.id),
    )
    source_plan = load_block_source_plan(project)
    pin = block_translation_dossier_pin(
        project,
        source_plan.record,
        full["revision_id"],
        blocks=source_plan.plan.blocks,
    )
    revision = db.session.get(TranslationResearchDossierRevision, full["revision_id"])
    assert pin == {
        "revision_id": full["revision_id"],
        "record_hash": revision.record_hash,
        "content_digest": revision.content_digest,
        "conclusion_provenance": "external_unverified",
        "evidence_verification_state": "unverified",
    }

    revision.record_hash = "0" * 64
    with pytest.raises(ScholarlyReviewError, match="dossier integrity"):
        block_translation_dossier_pin(
            project,
            source_plan.record,
            full["revision_id"],
            blocks=source_plan.plan.blocks,
        )

    partial = submit_research_dossier(
        project,
        client_submission_id="partial-block-start",
        payload={"document_brief": "Only one block is covered."},
        provenance={},
        scope={"kind": "blocks", "block_ids": [blocks[0].id]},
        actor=ReviewActor("owner", owner.id),
    )
    with pytest.raises(ScholarlyReviewError, match="retranslation dossier scope"):
        block_translation_dossier_pin(
            project,
            source_plan.record,
            partial["revision_id"],
            blocks=source_plan.plan.blocks,
        )


def test_expiry_is_enforced_without_commit_ownership(app):
    project, _record, _blocks, owner = _project_and_plan()
    with pytest.raises(ScholarlyReviewError):
        issue_share_grant(
            project,
            label="Expired",
            role="viewer",
            actor=ReviewActor("owner", owner.id),
            expires_at=utc_now_db() - timedelta(seconds=1),
        )
