"""Focused acceptance tests for the document-block workspace API boundary."""

from __future__ import annotations

import hashlib
import re
from datetime import timedelta
from importlib import import_module
from pathlib import Path
from threading import Barrier, Event, Thread
from types import SimpleNamespace

import pytest
from content_storage_support import bind_content_policy
from flask import Flask, g
from flask.testing import FlaskClient
from flask_login import LoginManager, login_user, logout_user
from sqlalchemy import text

from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION
from carlina.core.utc import utc_iso, utc_now_db

_APPLICATION_ROOT = "iarf" + "_assistant"
_TRANSLATOR_ROOT = f"{_APPLICATION_ROOT}.addons.translator"
routes = import_module(f"{_TRANSLATOR_ROOT}.routes")
_alignment_contract = import_module(f"{_TRANSLATOR_ROOT}.alignment_contract")
_block_contract = import_module(f"{_TRANSLATOR_ROOT}.block_contract")
_block_persistence = import_module(f"{_TRANSLATOR_ROOT}.block_persistence")
_block_planner = import_module(f"{_TRANSLATOR_ROOT}.block_planner")
_block_workspace = import_module(f"{_TRANSLATOR_ROOT}.block_workspace_api")
_compact_markup = import_module(f"{_TRANSLATOR_ROOT}.compact_markup")
_glossary_persistence = import_module(f"{_TRANSLATOR_ROOT}.glossary_persistence")
_models = import_module(f"{_TRANSLATOR_ROOT}.models")
_operation_service = import_module(f"{_TRANSLATOR_ROOT}.operations")
_target_contract = import_module(f"{_TRANSLATOR_ROOT}.target_contract")
_target_persistence = import_module(f"{_TRANSLATOR_ROOT}.target_persistence")
_source_plan_corrections = import_module(
    f"{_TRANSLATOR_ROOT}.source_plan_correction_persistence"
)
_scholarly_review = import_module(f"{_TRANSLATOR_ROOT}.scholarly_review")
_alignment_persistence = import_module(f"{_TRANSLATOR_ROOT}.alignment_persistence")
_sentence_alignment = import_module(f"{_TRANSLATOR_ROOT}.sentence_alignment")
_encryption = import_module(f"{_APPLICATION_ROOT}.core.encryption")
_query_metrics = import_module(f"{_APPLICATION_ROOT}.core.persistence.query_metrics")
db = import_module(f"{_APPLICATION_ROOT}.extensions").db
User = import_module(f"{_APPLICATION_ROOT}.models").User

AttestedDocumentSnapshot = _block_contract.AttestedDocumentSnapshot
persist_source_plan = _block_persistence.persist_source_plan
source_attestation_for_project = _block_persistence.source_attestation_for_project
plan_document_blocks = _block_planner.plan_document_blocks
BlockWorkspaceIntegrityError = _block_workspace.BlockWorkspaceIntegrityError
build_workspace_payload = _block_workspace.build_workspace_payload
persist_manual_candidate = _block_workspace.persist_manual_candidate
build_block_glossary_payload = _block_workspace.build_block_glossary_payload
persist_human_glossary_entry = _block_workspace.persist_human_glossary_entry
persist_analysis_glossary_proposals = (
    _glossary_persistence.persist_analysis_glossary_proposals
)
TranslationProject = _models.TranslationProject
TranslationSourcePlan = _models.TranslationSourcePlan
TranslationOperation = _models.TranslationOperation
TranslationGlossaryActive = _models.TranslationGlossaryActive
TranslationGlossaryRevision = _models.TranslationGlossaryRevision
TranslationShareGrant = _models.TranslationShareGrant
TranslationTargetEditIdempotency = _models.TranslationTargetEditIdempotency
TranslationTargetAlignmentActive = _models.TranslationTargetAlignmentActive
TranslationTargetRevisionShareAttribution = (
    _models.TranslationTargetRevisionShareAttribution
)
verify_target_revision_share_attribution = (
    _target_persistence.verify_target_revision_share_attribution
)
TargetBlockCandidate = _target_contract.TargetBlockCandidate
TargetTextPart = _target_contract.TargetTextPart
AlignmentGroup = _alignment_contract.AlignmentGroup
normalize_sentence_alignment = _sentence_alignment.normalize_sentence_alignment
structural_fingerprint = _target_contract.structural_fingerprint
EncryptionService = _encryption.EncryptionService
InstallationKeyStore = _encryption.InstallationKeyStore
measure_queries = _query_metrics.measure_queries
correct_active_source_plan = _source_plan_corrections.correct_active_source_plan
SourcePlanCorrectionError = _source_plan_corrections.SourcePlanCorrectionError
ReviewActor = _scholarly_review.ReviewActor
create_reader_note = _scholarly_review.create_reader_note
record_block_review = _scholarly_review.record_block_review


@pytest.fixture
def app(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'workspace.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="workspace-test",
        TESTING=True,
    )
    db.init_app(application)
    login_manager = LoginManager()
    login_manager.init_app(application)

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

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
        # SQLite cannot topologically drop the append-only revision lineage
        # and its paired active pointers with FK enforcement enabled.
        with db.engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            db.metadata.drop_all(connection)
        db.engine.dispose()
        context.pop()


def _project(name: str = "Workspace") -> TranslationProject:
    owner = User(email=f"{name.lower()}@example.test", password_hash="test")
    db.session.add(owner)
    db.session.flush()
    project = TranslationProject(
        user_id=owner.id,
        name=name,
        source_lang="en",
        target_lang="pl",
        status="setup",
        engine_contract="document-block-v1",
        document_id="doc-1",
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
        },
    )
    db.session.add(project)
    db.session.commit()
    return project


def _plan(
    project: TranslationProject,
    html: str = "<html><body><p>Hello.</p><p>World.</p></body></html>",
):
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
    return planned, record


def _candidate(plan, record, block, text: str) -> TargetBlockCandidate:
    return TargetBlockCandidate(
        source_plan_id=record.id,
        source_plan_hash=plan.plan_hash,
        block_id=block.id,
        structural_fingerprint=structural_fingerprint(plan, block),
        parts=(TargetTextPart(kind="text", text=text),),
        lexical_attributes=(),
    )


def _seed_operation(
    project: TranslationProject,
    record: TranslationSourcePlan,
    *,
    state: str,
    kind: str = "document_block_export",
) -> TranslationOperation:
    now = utc_now_db()
    operation = TranslationOperation(
        project_id=project.id,
        owner_user_id=project.user_id,
        kind=kind,
        idempotency_key=f"seed-{state}-{kind}",
        request_fingerprint="a" * 64,
        state=state,
        source_content_hash=record.source_content_hash,
        source_plan_id=record.id,
        source_plan_hash=record.plan_hash,
        started_at=now if state == "running" else None,
        finished_at=now
        if state in {"completed", "partial", "failed", "cancelled"}
        else None,
        cancel_requested_at=now if state == "cancel_requested" else None,
    )
    db.session.add(operation)
    db.session.commit()
    return operation


def _seed_provider_alignment(
    project: TranslationProject,
    plan,
    record,
    block,
    text: str,
    target_sentences: list[str],
    groups: list[dict[str, list[int]]],
):
    """Persist a synthetic provider manifest so edit tests start with real witnesses."""

    candidate = _candidate(plan, record, block, text)
    revision = _target_persistence.persist_target_revision(
        record,
        candidate,
        target_language=project.target_lang,
        author_type="model",
        parent_revision_id=None,
    )
    manifest = normalize_sentence_alignment(
        block=block,
        candidate=candidate,
        value={"target_sentences": target_sentences, "groups": groups},
        target_language=project.target_lang,
    )
    span_set = _alignment_persistence.persist_target_span_set(
        record,
        revision,
        verified_plan=plan,
        manifest=manifest,
    )
    spans = _alignment_persistence.rehydrate_target_span_set(
        span_set,
        record,
        verified_plan=plan,
    )
    alignment_groups = tuple(
        AlignmentGroup(
            tuple(block.anchors[item - 1].id for item in group.source_ordinals),
            tuple(spans[item - 1].id for item in group.target_ordinals),
            "provider-manifest-v1",
            0.99,
            "needs_review",
            {"source": "synthetic-test"},
        )
        for group in manifest.groups
    )
    alignment = _alignment_persistence.persist_alignment_revision(
        record,
        span_set,
        groups=alignment_groups,
        method="provider-manifest-v1",
        review_state="needs_review",
        verified_plan=plan,
    )
    _target_persistence.activate_target_revision(
        record,
        block_id=block.id,
        revision_id=revision.id,
        alignment_revision_id=alignment.id,
        expected_version=0,
        verified_plan=plan,
    )
    db.session.commit()
    return revision, alignment


def _active_alignment_material(record, block):
    pointer = db.session.get(
        TranslationTargetAlignmentActive,
        {"source_plan_id": record.id, "block_id": block.id},
    )
    assert pointer is not None
    alignment = db.session.get(
        _models.TranslationAlignmentRevision,
        pointer.alignment_revision_id,
    )
    assert alignment is not None
    span_set = db.session.get(_models.TranslationTargetSpanSet, alignment.span_set_id)
    assert span_set is not None
    spans = _alignment_persistence.rehydrate_target_span_set(span_set, record)
    groups = _alignment_persistence.rehydrate_alignment_revision(alignment, record)
    anchors_by_id = {anchor.id: anchor for anchor in block.anchors}
    spans_by_id = {span.id: span for span in spans}
    return alignment, [
        (
            tuple(anchors_by_id[item].order for item in group.source_anchor_ids),
            tuple(
                span_set.projection[spans_by_id[item].start : spans_by_id[item].end]
                for item in group.target_span_ids
            ),
        )
        for group in groups
    ]


def _share_grant(
    project: TranslationProject,
    grant_id: str,
    *,
    project_id: str | None = None,
    owner_user_id: int | None = None,
    role: str = "reviewer",
    status: str = "active",
    expires_at=None,
    revoked_at=None,
) -> TranslationShareGrant:
    grant = TranslationShareGrant(
        id=grant_id,
        project_id=project.id if project_id is None else project_id,
        owner_user_id=(project.user_id if owner_user_id is None else owner_user_id),
        label=grant_id,
        role=role,
        token_digest=hashlib.sha512(grant_id.encode("utf-8")).hexdigest(),
        token_hint="hint",
        status=status,
        expires_at=expires_at,
        revoked_at=revoked_at,
        created_at=utc_now_db(),
    )
    db.session.add(grant)
    db.session.flush()
    return grant


def test_workspace_is_ordered_and_paginated_without_sensitive_projection(app):
    project = _project()
    _planned, record = _plan(project)
    payload = build_workspace_payload(project, offset=1, limit=1)
    assert payload["contract"] == "translator-block-workspace-v5"
    assert payload["pagination"] == {
        "offset": 1,
        "limit": 1,
        "total": 2,
        "has_more": False,
    }
    assert payload["target_summary"] == {
        "total_blocks": 2,
        "active_targets": 0,
        "structurally_valid_active_targets": 0,
        "document_complete": False,
    }
    assert [item["order"] for item in payload["blocks"]] == [2]
    block = payload["blocks"][0]
    assert set(block) == {
        "id",
        "order",
        "role",
        "source",
        "target",
        "reader_notes",
        "review",
    }
    assert set(block["source"]) == {"editor"}
    assert block["source"]["editor"]["contract_version"] == (
        "translation-block-editor-v1"
    )
    assert payload["project"]["engine_contract"] == "document-block-v1"
    assert "settings" not in payload["project"]
    assert "source_reference" not in str(payload)
    assert "protected_inline" not in str(payload)
    assert "structural_fingerprint" not in str(payload)
    assert payload["source_plan"] == {"id": record.id, "version": 1}


def test_aligned_workspace_projects_verified_safe_alignment_groups(app):
    project = _project("Aligned workspace")
    project.settings = {
        **project.settings,
        "translation_unit_mode": "aligned_sentences",
    }
    db.session.commit()
    plan, record = _plan(project)
    block = plan.blocks[0]
    candidate = _candidate(plan, record, block, "Cel.")

    persist_manual_candidate(
        project,
        block.id,
        candidate,
        0,
        author_user_id=project.user_id,
    )
    payload = build_workspace_payload(project, offset=0, limit=1)

    assert payload["contract"] == "translator-block-workspace-v6"
    assert payload["review_mode"] == "aligned_sentences"
    alignment = payload["blocks"][0]["target"]["alignment"]
    assert alignment["origin"] == "automatic_fallback"
    assert alignment["review_state"] == "needs_review"
    assert alignment["groups"] == [
        {
            "order": 1,
            "kind": "one_to_one",
            "source_sentences": [{"order": 1, "text": "Hello."}],
            "target_sentences": [{"order": 1, "text": "Cel."}],
        }
    ]
    assert set(alignment) == {"origin", "review_state", "groups"}
    assert all(
        set(group) == {"order", "kind", "source_sentences", "target_sentences"}
        for group in alignment["groups"]
    )
    assert "alignment_revision_id" not in str(alignment)
    assert "confidence" not in str(alignment)
    assert "evidence" not in str(alignment)


def test_manual_candidate_carries_provider_groups_through_multiple_edits(app):
    project = _project("Carried alignment")
    project.settings = {
        **project.settings,
        "translation_unit_mode": "aligned_sentences",
    }
    db.session.commit()
    plan, record = _plan(
        project,
        "<html><body><p>Source one. Source two.</p></body></html>",
    )
    block = plan.blocks[0]
    _seed_provider_alignment(
        project,
        plan,
        record,
        block,
        "Pierwsze zdanie. Drugie zdanie. Trzecie zdanie.",
        ["Pierwsze zdanie.", "Drugie zdanie.", "Trzecie zdanie."],
        [{"source": [1], "target": [1, 2]}, {"source": [2], "target": [3]}],
    )

    first = persist_manual_candidate(
        project,
        block.id,
        _candidate(
            plan,
            record,
            block,
            "Pierwsze zdanie. Zmienione drugie zdanie. Trzecie zdanie.",
        ),
        1,
        author_user_id=project.user_id,
    )
    assert first["target"]["active_version"] == 2
    assert first["target"]["editor"]["content"] == (
        "Pierwsze zdanie. Zmienione drugie zdanie. Trzecie zdanie."
    )
    alignment, groups = _active_alignment_material(record, block)
    assert alignment.method == "editor-carried-v1"
    assert alignment.review_state == "needs_review"
    assert [
        (source, tuple(item.strip() for item in target)) for source, target in groups
    ] == [
        ((1,), ("Pierwsze zdanie.", "Zmienione drugie zdanie.")),
        ((2,), ("Trzecie zdanie.",)),
    ]
    assert (
        build_workspace_payload(project, offset=0, limit=1)["blocks"][0]["target"][
            "alignment"
        ]["origin"]
        == "carried_forward"
    )

    second = persist_manual_candidate(
        project,
        block.id,
        _candidate(
            plan,
            record,
            block,
            "Pierwsze zdanie. Jeszcze inne drugie zdanie. Trzecie zdanie.",
        ),
        2,
        author_user_id=project.user_id,
    )
    assert second["target"]["active_version"] == 3
    assert second["target"]["editor"]["content"] == (
        "Pierwsze zdanie. Jeszcze inne drugie zdanie. Trzecie zdanie."
    )
    alignment, groups = _active_alignment_material(record, block)
    assert alignment.method == "editor-carried-v1"
    assert alignment.review_state == "needs_review"
    assert [
        (source, tuple(item.strip() for item in target)) for source, target in groups
    ] == [
        ((1,), ("Pierwsze zdanie.", "Jeszcze inne drugie zdanie.")),
        ((2,), ("Trzecie zdanie.",)),
    ]


def test_manual_candidate_keeps_automatic_fallback_after_punctuation_edit(app):
    project = _project("Fallback alignment")
    project.settings = {
        **project.settings,
        "translation_unit_mode": "aligned_sentences",
    }
    db.session.commit()
    plan, record = _plan(
        project,
        "<html><body><p>Source one.</p></body></html>",
    )
    block = plan.blocks[0]
    _seed_provider_alignment(
        project,
        plan,
        record,
        block,
        "Pierwsze zdanie. Drugie zdanie.",
        ["Pierwsze zdanie.", "Drugie zdanie."],
        [{"source": [1], "target": [1, 2]}],
    )

    punctuation_edit = persist_manual_candidate(
        project,
        block.id,
        _candidate(plan, record, block, "Pierwsze zdanie! Drugie zdanie."),
        1,
        author_user_id=project.user_id,
    )
    assert punctuation_edit["target"]["active_version"] == 2
    alignment, groups = _active_alignment_material(record, block)
    assert alignment.method == "deterministic-count-v1"
    assert alignment.review_state == "needs_review"
    assert [
        (source, tuple(item.strip() for item in target)) for source, target in groups
    ] == [
        ((1,), ("Pierwsze zdanie!", "Drugie zdanie.")),
    ]
    assert (
        build_workspace_payload(project, offset=0, limit=1)["blocks"][0]["target"][
            "alignment"
        ]["origin"]
        == "automatic_fallback"
    )

    wording_edit = persist_manual_candidate(
        project,
        block.id,
        _candidate(
            plan,
            record,
            block,
            "Pierwsze zdanie! Zmienione drugie zdanie.",
        ),
        2,
        author_user_id=project.user_id,
    )
    assert wording_edit["target"]["active_version"] == 3
    alignment, groups = _active_alignment_material(record, block)
    assert alignment.method == "deterministic-count-v1"
    assert alignment.review_state == "needs_review"
    assert [
        (source, tuple(item.strip() for item in target)) for source, target in groups
    ] == [
        ((1,), ("Pierwsze zdanie!", "Zmienione drugie zdanie.")),
    ]
    assert (
        build_workspace_payload(project, offset=0, limit=1)["blocks"][0]["target"][
            "alignment"
        ]["origin"]
        == "automatic_fallback"
    )

    with pytest.raises(
        BlockWorkspaceIntegrityError, match="alignment-parent-carry-unsafe"
    ):
        persist_manual_candidate(
            project,
            block.id,
            _candidate(
                plan,
                record,
                block,
                "Pierwsze zdanie! Zmienione drugie zdanie.",
            ),
            3,
            author_user_id=project.user_id,
            restore_parent_alignment=True,
        )
    assert (
        _models.TranslationTargetBlockRevision.query.filter_by(
            source_plan_id=record.id,
            block_id=block.id,
        ).count()
        == 3
    )
    active = db.session.get(
        _models.TranslationTargetBlockActive,
        {"source_plan_id": record.id, "block_id": block.id},
    )
    assert active is not None
    assert active.version == 3


def test_manual_candidate_recovery_restores_immediate_parent_alignment_idempotently(
    app,
):
    project = _project("Alignment recovery")
    project.settings = {
        **project.settings,
        "translation_unit_mode": "aligned_sentences",
    }
    db.session.commit()
    plan, record = _plan(
        project,
        "<html><body><p>Source one. Source two.</p></body></html>",
    )
    block = plan.blocks[0]
    parent, _parent_alignment = _seed_provider_alignment(
        project,
        plan,
        record,
        block,
        "Pierwsze zdanie. Drugie zdanie. Trzecie zdanie.",
        ["Pierwsze zdanie.", "Drugie zdanie.", "Trzecie zdanie."],
        [{"source": [1], "target": [1, 2]}, {"source": [2], "target": [3]}],
    )
    candidate = _candidate(
        plan,
        record,
        block,
        "Pierwsze zdanie. Zmienione drugie zdanie. Trzecie zdanie.",
    )

    # Reproduce the pre-fix persistence shape: the child points at the
    # provider-aligned parent, but its own alignment uses the automatic count
    # fallback instead of carrying the parent's verified membership.
    legacy_revision = _target_persistence.persist_target_revision(
        record,
        candidate,
        target_language=project.target_lang,
        author_type="human",
        author_user_id=project.user_id,
        parent_revision_id=parent.id,
    )
    legacy_span_set = _alignment_persistence.persist_target_span_set(
        record,
        legacy_revision,
        verified_plan=plan,
    )
    legacy_alignment = _alignment_persistence.persist_alignment_revision(
        record,
        legacy_span_set,
        verified_plan=plan,
    )
    _target_persistence.activate_target_revision(
        record,
        block_id=block.id,
        revision_id=legacy_revision.id,
        alignment_revision_id=legacy_alignment.id,
        expected_version=1,
        verified_plan=plan,
    )
    db.session.commit()
    assert legacy_alignment.method == "deterministic-count-v1"

    recovered = persist_manual_candidate(
        project,
        block.id,
        candidate,
        2,
        author_user_id=project.user_id,
        idempotency_key="alignment-recovery",
        restore_parent_alignment=True,
    )
    assert recovered["target"]["active_version"] == 3
    assert recovered["target"]["editor"]["content"] == (
        "Pierwsze zdanie. Zmienione drugie zdanie. Trzecie zdanie."
    )
    assert (
        _models.TranslationTargetBlockRevision.query.filter_by(
            source_plan_id=record.id,
            block_id=block.id,
        ).count()
        == 3
    )
    alignment, groups = _active_alignment_material(record, block)
    assert alignment.method == "editor-carried-v1"
    assert alignment.review_state == "needs_review"
    assert [
        (source, tuple(item.strip() for item in target)) for source, target in groups
    ] == [
        ((1,), ("Pierwsze zdanie.", "Zmienione drugie zdanie.")),
        ((2,), ("Trzecie zdanie.",)),
    ]
    assert (
        build_workspace_payload(project, offset=0, limit=1)["blocks"][0]["target"][
            "alignment"
        ]["origin"]
        == "carried_forward"
    )

    retry = persist_manual_candidate(
        project,
        block.id,
        candidate,
        2,
        author_user_id=project.user_id,
        idempotency_key="alignment-recovery",
        restore_parent_alignment=True,
    )
    assert retry == recovered
    assert (
        _models.TranslationTargetBlockRevision.query.filter_by(
            source_plan_id=record.id,
            block_id=block.id,
        ).count()
        == 3
    )
    with pytest.raises(
        BlockWorkspaceIntegrityError, match="candidate-idempotency-conflict"
    ):
        persist_manual_candidate(
            project,
            block.id,
            candidate,
            2,
            author_user_id=project.user_id,
            idempotency_key="alignment-recovery",
            restore_parent_alignment=False,
        )
    with pytest.raises(BlockWorkspaceIntegrityError, match="target-active-conflict"):
        persist_manual_candidate(
            project,
            block.id,
            _candidate(
                plan,
                record,
                block,
                "Pierwsze zdanie. Stale second sentence. Trzecie zdanie.",
            ),
            2,
            author_user_id=project.user_id,
        )
    assert (
        _models.TranslationTargetBlockRevision.query.filter_by(
            source_plan_id=record.id,
            block_id=block.id,
        ).count()
        == 3
    )


def test_alignment_recovery_rejects_missing_parent_and_unsafe_initial_target_atomically(
    app,
):
    project = _project("Missing alignment parent")
    plan, record = _plan(
        project,
        "<html><body><p>Source one. Source two.</p></body></html>",
    )
    block = plan.blocks[0]
    _seed_provider_alignment(
        project,
        plan,
        record,
        block,
        "Pierwsze zdanie. Drugie zdanie. Trzecie zdanie.",
        ["Pierwsze zdanie.", "Drugie zdanie.", "Trzecie zdanie."],
        [{"source": [1], "target": [1, 2]}, {"source": [2], "target": [3]}],
    )
    with pytest.raises(
        BlockWorkspaceIntegrityError, match="alignment-parent-unavailable"
    ):
        persist_manual_candidate(
            project,
            block.id,
            _candidate(plan, record, block, "Pierwsze zdanie. Drugie zdanie. Final."),
            1,
            author_user_id=project.user_id,
            restore_parent_alignment=True,
        )
    assert (
        _models.TranslationTargetBlockRevision.query.filter_by(
            source_plan_id=record.id,
            block_id=block.id,
        ).count()
        == 1
    )

    unsafe_project = _project("Unsafe initial alignment")
    unsafe_plan, unsafe_record = _plan(
        unsafe_project,
        "<html><body><p>Source one. Source two.</p></body></html>",
    )
    unsafe_block = unsafe_plan.blocks[0]
    with pytest.raises(
        BlockWorkspaceIntegrityError, match="alignment-parent-carry-unsafe"
    ):
        persist_manual_candidate(
            unsafe_project,
            unsafe_block.id,
            _candidate(
                unsafe_plan,
                unsafe_record,
                unsafe_block,
                "Pierwsze zdanie. Drugie zdanie. Trzecie.",
            ),
            0,
            author_user_id=unsafe_project.user_id,
            restore_parent_alignment=True,
        )
    assert (
        _models.TranslationTargetBlockRevision.query.filter_by(
            source_plan_id=unsafe_record.id,
            block_id=unsafe_block.id,
        ).count()
        == 0
    )


def test_alignment_recovery_is_owner_only(app):
    project = _project("Reviewer recovery")
    plan, record = _plan(
        project,
        "<html><body><p>Source one. Source two.</p></body></html>",
    )
    block = plan.blocks[0]
    grant = _share_grant(project, "reviewer-recovery-grant")
    with pytest.raises(BlockWorkspaceIntegrityError, match="candidate-authority"):
        persist_manual_candidate(
            project,
            block.id,
            _candidate(
                plan,
                record,
                block,
                "Pierwsze zdanie. Drugie zdanie. Trzecie.",
            ),
            0,
            share_grant_id=grant.id,
            restore_parent_alignment=True,
        )
    assert (
        _models.TranslationTargetBlockRevision.query.filter_by(
            source_plan_id=record.id,
            block_id=block.id,
        ).count()
        == 0
    )


def test_workspace_projects_access_notes_and_reviews_without_showing_proposals_to_viewers(
    app,
):
    project = _project("Review projection")
    plan, _record = _plan(project)
    block_id = plan.blocks[0].id
    owner_actor = ReviewActor("owner", project.user_id)
    create_reader_note(
        project,
        block_id=block_id,
        body="Published explanation.",
        actor=owner_actor,
        state="active",
    )
    create_reader_note(
        project,
        block_id=block_id,
        body="Draft explanation.",
        actor=owner_actor,
        state="proposed",
    )
    record_block_review(
        project,
        block_id,
        "needs_changes",
        "Clarify the reference.",
        0,
        None,
        owner_actor,
    )
    db.session.commit()

    owner = build_workspace_payload(
        project,
        offset=0,
        limit=1,
        access_role="owner",
        access_label="Owner",
        actor=owner_actor,
    )
    assert owner["access"] == {"role": "owner", "label": "Owner"}
    assert {
        (note["state"], note["body"]) for note in owner["blocks"][0]["reader_notes"]
    } == {
        ("active", "Published explanation."),
        ("proposed", "Draft explanation."),
    }
    review = owner["blocks"][0]["review"]
    assert {key: review[key] for key in ("decision", "version", "rationale", "updated_at", "actor")} == {
        "decision": "needs_changes",
        "version": 1,
        "rationale": "Clarify the reference.",
        "updated_at": owner["blocks"][0]["review"]["updated_at"],
        "actor": {"type": "owner", "label": "Owner"},
    }
    assert review["effective_decision"] == "pending"
    assert review["stale"] is True
    assert review["target_revision_id"] is None
    assert review["current_target_revision_id"] is None
    assert review["current_target_version"] is None
    assert set(review["coverage"]) == {"structural", "target_reading", "bilingual"}
    assert all(not kind["complete"] for kind in review["coverage"].values())

    viewer_grant = _share_grant(project, "viewer-projection", role="viewer")
    db.session.commit()
    viewer = build_workspace_payload(
        project,
        offset=0,
        limit=1,
        access_role="viewer",
        access_label="Study reader",
        actor=ReviewActor("viewer", share_grant_id=viewer_grant.id),
    )
    assert viewer["access"] == {"role": "viewer", "label": "Study reader"}
    assert [note["body"] for note in viewer["blocks"][0]["reader_notes"]] == [
        "Published explanation."
    ]
    assert "Draft explanation" not in repr(viewer)


def test_workspace_target_summary_distinguishes_partial_and_complete(app):
    project = _project()
    plan, record = _plan(project)

    persist_manual_candidate(
        project,
        plan.blocks[0].id,
        _candidate(plan, record, plan.blocks[0], "Cześć."),
        0,
        author_user_id=project.user_id,
    )
    partial = build_workspace_payload(project, offset=0, limit=10)
    assert partial["target_summary"] == {
        "total_blocks": 2,
        "active_targets": 1,
        "structurally_valid_active_targets": 1,
        "document_complete": False,
    }

    persist_manual_candidate(
        project,
        plan.blocks[1].id,
        _candidate(plan, record, plan.blocks[1], "Świat."),
        0,
        author_user_id=project.user_id,
    )
    complete = build_workspace_payload(project, offset=0, limit=10)
    assert complete["target_summary"] == {
        "total_blocks": 2,
        "active_targets": 2,
        "structurally_valid_active_targets": 2,
        "document_complete": True,
    }


def test_workspace_query_budget_is_bounded_and_independent_of_page_limit(app):
    project = _project("QueryBudget")
    html = (
        "<html><body>"
        + "".join(f"<p>Source paragraph {index}.</p>" for index in range(1, 41))
        + "</body></html>"
    )
    plan, record = _plan(project, html)
    assert len(plan.blocks) == 40
    for index, block in enumerate(plan.blocks, start=1):
        persist_manual_candidate(
            project,
            block.id,
            _candidate(plan, record, block, f"Akapit docelowy {index}."),
            0,
            author_user_id=project.user_id,
        )

    observed_counts = []
    for limit in (1, 40):
        db.session.expire_all()
        project = db.session.get(TranslationProject, project.id)
        with measure_queries() as metrics:
            payload = build_workspace_payload(project, offset=0, limit=limit)
        assert len(payload["blocks"]) == limit
        observed_counts.append(metrics.statement_count)

    assert observed_counts[0] == observed_counts[1]
    assert observed_counts[1] <= 16


def test_workspace_reuses_the_verified_source_plan(app, monkeypatch):
    project = _project()
    plan, record = _plan(project)
    persist_manual_candidate(
        project,
        plan.blocks[0].id,
        _candidate(plan, record, plan.blocks[0], "Cześć."),
        0,
        author_user_id=project.user_id,
    )

    def unexpected_rehydration(*_args, **_kwargs):
        pytest.fail("workspace validation rehydrated its already-verified source plan")

    monkeypatch.setattr(
        _target_persistence,
        "rehydrate_source_plan",
        unexpected_rehydration,
    )
    monkeypatch.setattr(
        _alignment_persistence,
        "rehydrate_source_plan",
        unexpected_rehydration,
    )

    payload = build_workspace_payload(project, offset=0, limit=10)

    assert payload["target_summary"]["active_targets"] == 1
    assert payload["blocks"][0]["target"]["editor"]["content"] == "Cześć."


def test_workspace_target_summary_fails_closed_for_tampered_target(app):
    project = _project()
    plan, record = _plan(project, "<html><body><p>Hello.</p></body></html>")
    persist_manual_candidate(
        project,
        plan.blocks[0].id,
        _candidate(plan, record, plan.blocks[0], "Cześć."),
        0,
        author_user_id=project.user_id,
    )
    revision = record.target_revisions[0]
    revision.rendered_fragment = "<p>tampered</p>"
    db.session.commit()

    with pytest.raises(BlockWorkspaceIntegrityError, match="target-revision-corrupt"):
        build_workspace_payload(project, offset=0, limit=10)


def test_workspace_uses_explicit_active_source_plan(app):
    project = _project()
    with pytest.raises(
        BlockWorkspaceIntegrityError, match="source-plan-active-missing"
    ):
        build_workspace_payload(project, offset=0, limit=10)
    _planned, active = _plan(project)
    duplicate = TranslationSourcePlan(
        id="foreign-plan",
        project_id=project.id,
        contract_version="translation-source-plan-v1",
        source_document_id="doc-1",
        source_language="en",
        language_profile="default",
        namespace="00000000-0000-0000-0000-000000000001",
        snapshot_contract_version="document-snapshot-v1",
        semantic_contract_version=SEMANTIC_HTML_CONTRACT_VERSION,
        semantic_contract_mode="completed",
        semantic_content_hash="0" * 64,
        source_content_hash="0" * 64,
        snapshot_captured_at=project.source_captured_at,
        planner_version="document-block-planner-v1",
        planner_settings_hash="0" * 64,
        plan_hash="0" * 64,
        source_attestation={},
        planner_settings={},
        coverage_witness={},
        block_count=0,
        anchor_count=0,
    )
    db.session.add(duplicate)
    db.session.commit()
    payload = build_workspace_payload(project, offset=0, limit=10)
    assert payload["source_plan"] == {"id": active.id, "version": 1}


def test_sentence_edit_persists_versioned_target_and_explicit_user_alignment(app):
    project = _project("Sentence edit")
    project.settings = {
        **project.settings,
        "translation_unit_mode": "aligned_sentences",
    }
    db.session.commit()
    plan, record = _plan(
        project,
        "<html><body><p>Source one. Source two.</p></body></html>",
    )
    block = plan.blocks[0]
    _seed_provider_alignment(
        project,
        plan,
        record,
        block,
        "Uno.  Dos.",
        ["Uno.", "Dos."],
        [{"source": [1], "target": [1]}, {"source": [2], "target": [2]}],
    )

    saved = persist_manual_candidate(
        project,
        block.id,
        None,
        1,
        author_user_id=project.user_id,
        sentence_edit={
            "action": "replace",
            "sentence_order": 2,
            "text": "Dwa!",
        },
    )
    assert saved["target"]["active_version"] == 2
    assert saved["target"]["editor"]["content"] == "Uno.  Dwa!"
    alignment, groups = _active_alignment_material(record, block)
    assert alignment.method == "editor-segment-v1"
    assert alignment.review_state == "needs_review"
    assert [
        (source, tuple(item.strip() for item in target)) for source, target in groups
    ] == [((1,), ("Uno.",)), ((2,), ("Dwa!",))]
    assert (
        build_workspace_payload(project, offset=0, limit=1)["blocks"][0]["target"][
            "alignment"
        ]["origin"]
        == "user_edited"
    )
    carried = persist_manual_candidate(
        project,
        block.id,
        _candidate(plan, record, block, "Uno.  Dwie!"),
        2,
        author_user_id=project.user_id,
    )
    assert carried["target"]["active_version"] == 3
    alignment, _groups = _active_alignment_material(record, block)
    assert alignment.method == "editor-carried-v1"


def test_sentence_edit_split_merge_replay_and_fences(app):
    project = _project("Sentence split merge")
    project.settings = {
        **project.settings,
        "translation_unit_mode": "aligned_sentences",
    }
    db.session.commit()
    plan, record = _plan(
        project,
        "<html><body><p>Source one. Source two.</p></body></html>",
    )
    block = plan.blocks[0]
    _seed_provider_alignment(
        project,
        plan,
        record,
        block,
        "Uno.  Dos.",
        ["Uno.", "Dos."],
        [{"source": [1], "target": [1]}, {"source": [2], "target": [2]}],
    )
    split_edit = {
        "action": "split",
        "sentence_order": 1,
        "text": "Uno.  Extra.",
        "offset": 4,
    }
    first = persist_manual_candidate(
        project,
        block.id,
        None,
        1,
        author_user_id=project.user_id,
        idempotency_key="sentence-split",
        sentence_edit=split_edit,
    )
    retry = persist_manual_candidate(
        project,
        block.id,
        None,
        1,
        author_user_id=project.user_id,
        idempotency_key="sentence-split",
        sentence_edit=split_edit,
    )
    assert retry == first
    assert first["target"]["editor"]["content"] == "Uno.  Extra.  Dos."
    with pytest.raises(
        BlockWorkspaceIntegrityError, match="candidate-idempotency-conflict"
    ):
        persist_manual_candidate(
            project,
            block.id,
            None,
            1,
            author_user_id=project.user_id,
            idempotency_key="sentence-split",
            sentence_edit={
                "action": "replace",
                "sentence_order": 1,
                "text": "Other.",
            },
        )

    merged = persist_manual_candidate(
        project,
        block.id,
        None,
        2,
        author_user_id=project.user_id,
        sentence_edit={"action": "merge_next", "sentence_order": 1},
    )
    assert merged["target"]["active_version"] == 3
    assert merged["target"]["editor"]["content"] == "Uno.  Extra.  Dos."
    alignment, groups = _active_alignment_material(record, block)
    assert alignment.method == "editor-segment-v1"
    assert [
        (source, tuple(item.strip() for item in target)) for source, target in groups
    ] == [((1,), ("Uno.  Extra.",)), ((2,), ("Dos.",))]

    operation = _seed_operation(project, record, state="running")
    with pytest.raises(
        BlockWorkspaceIntegrityError, match="target-active-operation"
    ):
        persist_manual_candidate(
            project,
            block.id,
            None,
            3,
            author_user_id=project.user_id,
            sentence_edit={"action": "replace", "sentence_order": 1, "text": "Blocked."},
        )
    db.session.delete(operation)
    db.session.commit()


def test_block_merge_advances_plan_and_carries_only_unchanged_targets(app):
    project = _project("Merge")
    plan, record = _plan(
        project,
        "<html><body><p>Hello.</p><p>World.</p><p>Again.</p></body></html>",
    )
    for block, text in zip(
        plan.blocks, ("Witaj.", "Świecie.", "Ponownie."), strict=True
    ):
        persist_manual_candidate(
            project,
            block.id,
            _candidate(plan, record, block, text),
            0,
            author_user_id=project.user_id,
        )

    result = correct_active_source_plan(
        project,
        block_id=plan.blocks[0].id,
        action="merge_next",
        expected_source_plan_id=record.id,
        expected_active_version=1,
        idempotency_key="merge-first-two",
        owner_user_id=project.user_id,
    )

    assert result["affected_block_count"] == 2
    assert result["reused_target_count"] == 1
    assert result["source_plan"]["version"] == 2
    payload = build_workspace_payload(project, offset=0, limit=10)
    assert payload["source_plan"] == result["source_plan"]
    assert payload["target_summary"] == {
        "total_blocks": 2,
        "active_targets": 1,
        "structurally_valid_active_targets": 1,
        "document_complete": False,
    }
    assert payload["blocks"][0]["source"]["editor"]["content"] == "Hello. World."
    assert payload["blocks"][0]["target"] is None
    assert payload["blocks"][1]["target"]["editor"]["content"] == "Ponownie."

    retried = correct_active_source_plan(
        project,
        block_id=plan.blocks[0].id,
        action="merge_next",
        expected_source_plan_id=record.id,
        expected_active_version=1,
        idempotency_key="merge-first-two",
        owner_user_id=project.user_id,
    )
    assert retried == result
    assert TranslationSourcePlan.query.filter_by(project_id=project.id).count() == 2


def test_block_correction_carry_forward_has_a_bounded_query_budget(app):
    project = _project("CorrectionPerformance")
    html = (
        "<html><body>"
        + "".join(f"<p>Source paragraph {index}.</p>" for index in range(1, 25))
        + "</body></html>"
    )
    plan, record = _plan(project, html)
    for index, block in enumerate(plan.blocks, start=1):
        persist_manual_candidate(
            project,
            block.id,
            _candidate(plan, record, block, f"Akapit docelowy {index}."),
            0,
            author_user_id=project.user_id,
        )

    db.session.expire_all()
    project = db.session.get(TranslationProject, project.id)
    with measure_queries() as metrics:
        result = correct_active_source_plan(
            project,
            block_id=plan.blocks[0].id,
            action="merge_next",
            expected_source_plan_id=record.id,
            expected_active_version=1,
            idempotency_key="performance-merge",
            owner_user_id=project.user_id,
        )
    assert result["reused_target_count"] == 22
    assert metrics.statement_count <= 500


def test_block_exclusion_removes_wrapper_but_retains_source_evidence(app):
    project = _project("Exclude")
    html = "<html><body><p>Remove me.</p><p>Keep me.</p></body></html>"
    plan, record = _plan(project, html)
    persist_manual_candidate(
        project,
        plan.blocks[1].id,
        _candidate(plan, record, plan.blocks[1], "Zachowaj mnie."),
        0,
        author_user_id=project.user_id,
    )

    result = correct_active_source_plan(
        project,
        block_id=plan.blocks[0].id,
        action="exclude",
        expected_source_plan_id=record.id,
        expected_active_version=1,
        idempotency_key="exclude-first",
        owner_user_id=project.user_id,
    )

    active = db.session.get(TranslationSourcePlan, result["source_plan"]["id"])
    restored = _block_persistence.rehydrate_source_plan(active)
    assert (
        _block_contract.sha256_text(
            _block_contract.canonical_json(restored.to_dict()["planner_settings"])
        )
        == active.planner_settings_hash
    )
    assert (
        _block_contract.sha256_text(
            _block_contract.canonical_json(restored.coverage.to_dict()["entries"])
        )
        == restored.coverage.content_hash
    )
    assert _target_persistence.render_persisted_target_document(active) == (
        '<html lang="pl" xml:lang="pl"><body>'
        '<p lang="pl" xml:lang="pl">Zachowaj mnie.</p></body></html>'
    )
    assert record.id != active.id
    assert record.block_count == 2


def test_block_merge_omits_complete_model_wrappers_and_reapplies_one(app):
    project = _project("FormattedMerge")
    html = "<html><body><p><em>Alpha.</em></p><p><em>Beta.</em></p></body></html>"
    plan, record = _plan(project, html)

    result = correct_active_source_plan(
        project,
        block_id=plan.blocks[0].id,
        action="merge_next",
        expected_source_plan_id=record.id,
        expected_active_version=1,
        idempotency_key="merge-formatted",
        owner_user_id=project.user_id,
    )

    payload = build_workspace_payload(project, offset=0, limit=10)
    source_editor = payload["blocks"][0]["source"]["editor"]
    assert source_editor["content"] == "<em>Alpha. Beta.</em>"
    active_record = db.session.get(
        TranslationSourcePlan,
        result["source_plan"]["id"],
    )
    active_plan = _block_persistence.rehydrate_source_plan(active_record)
    assert (
        _compact_markup.compact_source_block(
            active_plan.blocks[0],
            number=1,
            implicit_outer_wrappers=True,
        )["content"]
        == "Alpha. Beta."
    )
    saved = persist_manual_candidate(
        project,
        payload["blocks"][0]["id"],
        {**source_editor, "content": "<em>Alfa. Beta.</em>"},
        0,
        author_user_id=project.user_id,
    )
    assert saved["target"]["editor"]["content"] == "<em>Alfa. Beta.</em>"
    assert _target_persistence.render_persisted_target_document(active_record) == (
        '<html lang="pl" xml:lang="pl"><body>'
        '<p lang="pl" xml:lang="pl"><em lang="pl" xml:lang="pl">'
        "Alfa. Beta.</em></p></body></html>"
    )


def test_block_correction_rejects_stale_plan_and_active_operation(app):
    project = _project("CorrectionFences")
    plan, record = _plan(
        project,
        "<html><body><p>One.</p><p>Two.</p><p>Three.</p></body></html>",
    )
    first = correct_active_source_plan(
        project,
        block_id=plan.blocks[0].id,
        action="merge_next",
        expected_source_plan_id=record.id,
        expected_active_version=1,
        idempotency_key="first-correction",
        owner_user_id=project.user_id,
    )
    with pytest.raises(
        SourcePlanCorrectionError,
        match="source-plan-active-conflict",
    ):
        correct_active_source_plan(
            project,
            block_id=plan.blocks[2].id,
            action="exclude",
            expected_source_plan_id=record.id,
            expected_active_version=1,
            idempotency_key="stale-correction",
            owner_user_id=project.user_id,
        )

    active_record = db.session.get(TranslationSourcePlan, first["source_plan"]["id"])
    active_plan = _block_persistence.rehydrate_source_plan(active_record)
    operation = TranslationOperation(
        project_id=project.id,
        owner_user_id=project.user_id,
        kind="document_block_translate",
        idempotency_key="active-translation",
        request_fingerprint="a" * 64,
        state="queued",
        source_content_hash=active_record.source_content_hash,
        source_plan_id=active_record.id,
        source_plan_hash=active_record.plan_hash,
    )
    db.session.add(operation)
    db.session.commit()
    with pytest.raises(
        SourcePlanCorrectionError,
        match="source-plan-correction-active-operation",
    ):
        correct_active_source_plan(
            project,
            block_id=active_plan.blocks[-1].id,
            action="exclude",
            expected_source_plan_id=active_record.id,
            expected_active_version=2,
            idempotency_key="busy-correction",
            owner_user_id=project.user_id,
        )


def test_partial_operation_projection_exposes_only_safe_block_failure():
    operation = SimpleNamespace(
        id="operation-1",
        kind="document_block_translate",
        state="partial",
        progress=74.0,
        message="Translation paused at window 2; completed windows were preserved.",
        error_code="block_window_incomplete",
        created_at=None,
        started_at=None,
        finished_at=None,
        updated_at=None,
        result_json={
            "contract_version": "translator-block-partial-v1",
            "source_plan_id": "plan-1",
            "source_plan_hash": "a" * 64,
            "block_failure": {
                "attempt_id": "attempt-2",
                "window_order": 2,
                "affected_block_ids": ["block-2"],
                "dispatch_state": "normalization_failed",
                "failure_kind": "normalization_failed",
                "recovery_action": "translate_remaining",
                "remaining_dispatches": 0,
            },
            "error_detail": "provider response with private source text",
            "provider_response": "private output",
        },
    )

    payload = _block_workspace._safe_operation(operation)

    assert payload["block_failure"] == {
        "attempt_id": "attempt-2",
        "window_order": 2,
        "affected_block_ids": ["block-2"],
        "dispatch_state": "normalization_failed",
        "failure_kind": "normalization_failed",
        "recovery_action": "translate_remaining",
        "remaining_dispatches": 0,
    }
    assert "error_detail" not in payload
    assert "provider_response" not in payload


def test_manual_candidate_activation_is_paired_and_keeps_parent_lineage(app):
    project = _project()
    plan, record = _plan(project, "<html><body><p>Hello.</p></body></html>")
    block = plan.blocks[0]
    first = persist_manual_candidate(
        project,
        block.id,
        _candidate(plan, record, block, "Cześć."),
        0,
        author_user_id=project.user_id,
    )
    assert first["target"]["active_version"] == 1
    second = persist_manual_candidate(
        project,
        block.id,
        _candidate(plan, record, block, "Witaj."),
        1,
        author_user_id=project.user_id,
    )
    assert second["target"]["active_version"] == 2
    revisions = list(record.target_revisions)
    assert len(revisions) == 2
    assert revisions[1].parent_revision_id == revisions[0].id
    assert second["target"]["validation"] == {"valid": True, "findings": []}
    assert set(second["target"]) == {
        "active_version",
        "editor",
        "translator_notes",
        "validation",
    }
    assert second["target"]["translator_notes"] == []


def test_manual_compact_editor_is_restored_before_persistence(app):
    project = _project()
    plan, record = _plan(project, "<html><body><p>Hello.</p></body></html>")
    block = plan.blocks[0]
    source_editor = build_workspace_payload(project, offset=0, limit=10)["blocks"][0][
        "source"
    ]["editor"]
    edited = {**source_editor, "content": "Cześć."}

    saved = persist_manual_candidate(
        project,
        block.id,
        edited,
        0,
        author_user_id=project.user_id,
    )

    assert saved["target"]["editor"] == edited
    stored = _target_persistence.rehydrate_target_revision(
        record.target_revisions[0], record
    )
    assert [part.to_dict() for part in stored.parts] == [
        {"kind": "text", "text": "Cześć."}
    ]


def test_manual_candidate_stale_version_returns_without_new_revision(app):
    project = _project()
    plan, record = _plan(project, "<html><body><p>Hello.</p></body></html>")
    block = plan.blocks[0]
    persist_manual_candidate(
        project,
        block.id,
        _candidate(plan, record, block, "Cześć."),
        0,
        author_user_id=project.user_id,
    )
    with pytest.raises(BlockWorkspaceIntegrityError, match="target-active-conflict"):
        persist_manual_candidate(
            project,
            block.id,
            _candidate(plan, record, block, "Witaj."),
            0,
            author_user_id=project.user_id,
        )
    assert len(record.target_revisions) == 1


@pytest.mark.parametrize("state", ["queued", "running", "cancel_requested"])
def test_manual_candidate_rejects_any_active_operation_without_mutation(app, state):
    project = _project(f"Active edit fence {state}")
    plan, record = _plan(project, "<html><body><p>Hello.</p></body></html>")
    block = plan.blocks[0]
    first = persist_manual_candidate(
        project,
        block.id,
        _candidate(plan, record, block, "Cześć."),
        0,
        author_user_id=project.user_id,
        idempotency_key=f"seed-target-{state}",
    )
    active_before = db.session.get(
        _models.TranslationTargetBlockActive,
        {"source_plan_id": record.id, "block_id": block.id},
    )
    alignment_before = db.session.get(
        TranslationTargetAlignmentActive,
        {"source_plan_id": record.id, "block_id": block.id},
    )
    assert active_before is not None
    assert alignment_before is not None
    operation = _seed_operation(project, record, state=state)
    revision_count = _models.TranslationTargetBlockRevision.query.count()
    receipt_count = TranslationTargetEditIdempotency.query.count()

    with pytest.raises(BlockWorkspaceIntegrityError, match="target-active-operation"):
        persist_manual_candidate(
            project,
            block.id,
            _candidate(plan, record, block, "Witaj."),
            1,
            author_user_id=project.user_id,
            idempotency_key=f"blocked-target-{state}",
        )

    db.session.expire_all()
    active_after = db.session.get(
        _models.TranslationTargetBlockActive,
        {"source_plan_id": record.id, "block_id": block.id},
    )
    alignment_after = db.session.get(
        TranslationTargetAlignmentActive,
        {"source_plan_id": record.id, "block_id": block.id},
    )
    operation_after = db.session.get(TranslationOperation, operation.id)
    assert first["target"]["active_version"] == 1
    assert active_after is not None
    assert alignment_after is not None
    assert (
        active_after.revision_id,
        active_after.version,
        alignment_after.revision_id,
        alignment_after.version,
    ) == (
        active_before.revision_id,
        active_before.version,
        alignment_before.revision_id,
        alignment_before.version,
    )
    assert _models.TranslationTargetBlockRevision.query.count() == revision_count
    assert TranslationTargetEditIdempotency.query.count() == receipt_count
    assert operation_after is not None
    assert operation_after.state == state


def test_manual_candidate_exact_replay_remains_available_during_active_operation(app):
    project = _project("Active edit replay")
    plan, record = _plan(project, "<html><body><p>Hello.</p></body></html>")
    block = plan.blocks[0]
    candidate = _candidate(plan, record, block, "Cześć.")
    first = persist_manual_candidate(
        project,
        block.id,
        candidate,
        0,
        author_user_id=project.user_id,
        idempotency_key="replay-during-active-operation",
    )
    operation = _seed_operation(project, record, state="running")
    revision_count = _models.TranslationTargetBlockRevision.query.count()
    receipt_count = TranslationTargetEditIdempotency.query.count()

    replay = persist_manual_candidate(
        project,
        block.id,
        candidate,
        0,
        author_user_id=project.user_id,
        idempotency_key="replay-during-active-operation",
    )

    assert replay == first
    assert _models.TranslationTargetBlockRevision.query.count() == revision_count
    assert TranslationTargetEditIdempotency.query.count() == receipt_count
    assert db.session.get(TranslationOperation, operation.id).state == "running"


def test_manual_candidate_allows_new_edit_after_terminal_operation(app):
    project = _project("Terminal edit fence")
    plan, record = _plan(project, "<html><body><p>Hello.</p></body></html>")
    block = plan.blocks[0]
    persist_manual_candidate(
        project,
        block.id,
        _candidate(plan, record, block, "Cześć."),
        0,
        author_user_id=project.user_id,
        idempotency_key="terminal-seed-target",
    )
    _seed_operation(project, record, state="completed")

    saved = persist_manual_candidate(
        project,
        block.id,
        _candidate(plan, record, block, "Witaj."),
        1,
        author_user_id=project.user_id,
        idempotency_key="terminal-target-edit",
    )

    assert saved["target"]["active_version"] == 2
    assert saved["target"]["editor"]["content"] == "Witaj."
    assert _models.TranslationTargetBlockRevision.query.count() == 2


def test_manual_candidate_and_operation_admission_share_plan_writer_fence(
    app, monkeypatch: pytest.MonkeyPatch
):
    project = _project("Concurrent edit admission")
    plan, record = _plan(project, "<html><body><p>Hello.</p></body></html>")
    block = plan.blocks[0]
    candidate = _candidate(plan, record, block, "Cześć.").to_dict()
    project_id = project.id
    source_plan_id = record.id
    owner_user_id = project.user_id
    lock_barrier = Barrier(2)
    lock_events: list[str] = []
    outcomes: list[tuple[str, str]] = []
    original_edit_lock = _block_workspace.require_active_source_plan
    original_operation_lock = _operation_service.require_active_source_plan

    def synchronized_edit_lock(*args, **kwargs):
        lock_barrier.wait(timeout=15)
        pointer = original_edit_lock(*args, **kwargs)
        lock_events.append("edit")
        return pointer

    def synchronized_operation_lock(*args, **kwargs):
        lock_barrier.wait(timeout=15)
        pointer = original_operation_lock(*args, **kwargs)
        lock_events.append("operation")
        return pointer

    monkeypatch.setattr(
        _block_workspace, "require_active_source_plan", synchronized_edit_lock
    )
    monkeypatch.setattr(
        _operation_service,
        "require_active_source_plan",
        synchronized_operation_lock,
    )
    db.session.remove()

    def edit_worker() -> None:
        with app.app_context():
            try:
                current_project = db.session.get(TranslationProject, project_id)
                assert current_project is not None
                persist_manual_candidate(
                    current_project,
                    block.id,
                    candidate,
                    0,
                    author_user_id=owner_user_id,
                    idempotency_key="concurrent-edit-admission",
                )
                outcomes.append(("edit", "saved"))
            except BlockWorkspaceIntegrityError as exc:
                db.session.rollback()
                outcomes.append(("edit", exc.code))
            except Exception as exc:  # noqa: BLE001 - expose race failures
                db.session.rollback()
                outcomes.append(("edit", f"unexpected:{type(exc).__name__}"))
            finally:
                db.session.remove()

    def operation_worker() -> None:
        with app.app_context():
            try:
                current_project = db.session.get(TranslationProject, project_id)
                current_record = db.session.get(TranslationSourcePlan, source_plan_id)
                assert current_project is not None
                assert current_record is not None
                operation, created = _operation_service.create_operation(
                    current_project,
                    kind="document_block_export",
                    request_payload={"format": "html"},
                    idempotency_key="concurrent-operation-admission",
                    source_content_hash=current_record.source_content_hash,
                    source_plan_id=current_record.id,
                    source_plan_hash=current_record.plan_hash,
                )
                db.session.commit()
                outcomes.append(("operation", "created" if created else "reused"))
                assert operation.state == "queued"
            except _operation_service.TranslationOperationActiveConflict:
                db.session.rollback()
                outcomes.append(("operation", "active-conflict"))
            except Exception as exc:  # noqa: BLE001 - expose race failures
                db.session.rollback()
                outcomes.append(("operation", f"unexpected:{type(exc).__name__}"))
            finally:
                db.session.remove()

    workers = [
        Thread(target=edit_worker, daemon=True),
        Thread(target=operation_worker, daemon=True),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=20)
        assert not worker.is_alive(), "edit/admission race worker did not finish"

    assert sorted(outcomes) in (
        [("edit", "saved"), ("operation", "created")],
        [("edit", "target-active-operation"), ("operation", "created")],
    )
    assert sorted(lock_events) == ["edit", "operation"]
    assert (
        TranslationOperation.query.filter_by(
            project_id=project_id, state="queued"
        ).count()
        == 1
    )


def test_manual_candidate_idempotency_replays_without_revision_and_fences_payload(app):
    project = _project("IdempotentCandidate")
    plan, record = _plan(project, "<html><body><p>Hello.</p></body></html>")
    block = plan.blocks[0]
    first = persist_manual_candidate(
        project,
        block.id,
        _candidate(plan, record, block, "Cześć."),
        0,
        author_user_id=project.user_id,
        idempotency_key="candidate-retry",
    )
    replay = persist_manual_candidate(
        project,
        block.id,
        _candidate(plan, record, block, "Cześć."),
        0,
        author_user_id=project.user_id,
        idempotency_key="candidate-retry",
    )

    assert replay == first
    assert len(record.target_revisions) == 1
    assert TranslationTargetEditIdempotency.query.count() == 1
    stored_result = db.session.execute(
        text(
            "SELECT safe_result_json FROM translation_target_edit_idempotency "
            "WHERE idempotency_key = :key"
        ),
        {"key": "candidate-retry"},
    ).scalar_one()
    assert "Cześć" not in stored_result
    persist_manual_candidate(
        project,
        block.id,
        _candidate(plan, record, block, "Witaj."),
        1,
        author_user_id=project.user_id,
        idempotency_key="candidate-advance",
    )
    assert len(record.target_revisions) == 2
    replay_after_advance = persist_manual_candidate(
        project,
        block.id,
        _candidate(plan, record, block, "Cześć."),
        0,
        author_user_id=project.user_id,
        idempotency_key="candidate-retry",
    )
    assert replay_after_advance == first
    assert len(record.target_revisions) == 2
    with pytest.raises(
        BlockWorkspaceIntegrityError, match="candidate-idempotency-conflict"
    ):
        persist_manual_candidate(
            project,
            block.id,
            _candidate(plan, record, block, "Witaj."),
            0,
            author_user_id=project.user_id,
            idempotency_key="candidate-retry",
        )
    assert len(record.target_revisions) == 2


def test_manual_candidate_identical_two_session_race_converges(app):
    project = _project("IdempotentCandidateRace")
    plan, record = _plan(project, "<html><body><p>Hello.</p></body></html>")
    block = plan.blocks[0]
    candidate = _candidate(plan, record, block, "Cześć.").to_dict()
    project_id = project.id
    source_plan_id = record.id
    owner_user_id = project.user_id
    block_id = block.id
    db.session.remove()
    barrier = Barrier(2)
    outcomes: list[tuple[str, object]] = []

    def run() -> None:
        with app.app_context():
            try:
                connection = db.session.connection()
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.exec_driver_sql("PRAGMA busy_timeout=30000")
                current_project = db.session.get(TranslationProject, project_id)
                assert current_project is not None
                barrier.wait(timeout=15)
                result = persist_manual_candidate(
                    current_project,
                    block_id,
                    candidate,
                    0,
                    author_user_id=owner_user_id,
                    idempotency_key="candidate-race",
                )
                outcomes.append(("ok", result))
            except BlockWorkspaceIntegrityError as exc:
                db.session.rollback()
                outcomes.append(("domain", exc.code))
            except Exception as exc:  # noqa: BLE001 - expose unexpected race failure
                db.session.rollback()
                outcomes.append(("unexpected", f"{type(exc).__name__}:{exc}"))
            finally:
                db.session.remove()

    workers = [Thread(target=run, daemon=True) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=20)
        assert not worker.is_alive(), "target-edit race worker did not finish"

    assert sorted(kind for kind, _result in outcomes) == ["ok", "ok"]
    assert outcomes[0][1] == outcomes[1][1]
    assert TranslationTargetEditIdempotency.query.count() == 1
    persisted_plan = db.session.get(TranslationSourcePlan, source_plan_id)
    assert persisted_plan is not None
    assert len(persisted_plan.target_revisions) == 1


def test_manual_candidate_idempotency_is_actor_scoped_and_validated(app):
    project = _project("ActorScopedCandidate")
    plan, record = _plan(project, "<html><body><p>Hello.</p></body></html>")
    block = plan.blocks[0]
    with pytest.raises(BlockWorkspaceIntegrityError, match="candidate-idempotency"):
        persist_manual_candidate(
            project,
            block.id,
            _candidate(plan, record, block, "Cześć."),
            0,
            author_user_id=project.user_id,
            idempotency_key=" ",
        )
    grant = _share_grant(project, "actor-scoped-grant")
    persist_manual_candidate(
        project,
        block.id,
        _candidate(plan, record, block, "Cześć."),
        0,
        share_grant_id=grant.id,
        idempotency_key="same-key",
    )
    persist_manual_candidate(
        project,
        block.id,
        _candidate(plan, record, block, "Witaj."),
        1,
        author_user_id=project.user_id,
        idempotency_key="same-key",
    )
    assert TranslationTargetEditIdempotency.query.count() == 2


def test_manual_candidate_revalidates_plan_after_a_concurrent_correction(
    app, monkeypatch: pytest.MonkeyPatch
):
    project = _project("SupersededCandidate")
    plan, record = _plan(
        project, "<html><body><p>Merge.</p><p>Next.</p><p>Keep.</p></body></html>"
    )
    block = plan.blocks[2]
    candidate = _candidate(plan, record, block, "Zachowaj.")
    project_id = project.id
    source_plan_id = record.id
    owner_user_id = project.user_id
    loaded = Event()
    release = Event()
    outcome: list[tuple[str, str]] = []
    original_load = _block_workspace.load_block_source_plan

    def pause_after_plan_load(current_project):
        workspace_plan = original_load(current_project)
        loaded.set()
        assert release.wait(timeout=15), "source-plan correction did not finish"
        return workspace_plan

    monkeypatch.setattr(
        _block_workspace, "load_block_source_plan", pause_after_plan_load
    )

    def edit_stale_plan() -> None:
        with app.app_context():
            try:
                current_project = db.session.get(TranslationProject, project_id)
                assert current_project is not None
                persist_manual_candidate(
                    current_project,
                    block.id,
                    candidate,
                    0,
                    author_user_id=owner_user_id,
                    idempotency_key="stale-plan-target-edit",
                )
                outcome.append(("ok", "saved"))
            except BlockWorkspaceIntegrityError as exc:
                db.session.rollback()
                outcome.append(("domain", exc.code))
            finally:
                db.session.remove()

    worker = Thread(target=edit_stale_plan, daemon=True)
    worker.start()
    assert loaded.wait(timeout=15), "target edit did not load its source plan"

    try:
        correct_active_source_plan(
            project,
            block_id=plan.blocks[0].id,
            action="merge_next",
            expected_source_plan_id=record.id,
            expected_active_version=1,
            idempotency_key="supersede-before-edit",
            owner_user_id=owner_user_id,
        )
    finally:
        release.set()
    worker.join(timeout=20)
    assert not worker.is_alive(), "target edit race worker did not finish"

    assert outcome == [("domain", "source-plan-active-conflict")]
    predecessor = db.session.get(TranslationSourcePlan, source_plan_id)
    assert predecessor is not None
    assert predecessor.target_revisions == []
    assert TranslationTargetEditIdempotency.query.count() == 0


def test_target_revision_orm_delete_cascades_populated_edit_receipt(app):
    project = _project("DeleteIdempotentCandidateRevision")
    plan, record = _plan(project, "<html><body><p>Hello.</p></body></html>")
    block = plan.blocks[0]
    revision = _target_persistence.persist_target_revision(
        record,
        _candidate(plan, record, block, "Cześć."),
        target_language=project.target_lang,
        author_type="human",
        author_user_id=project.user_id,
        parent_revision_id=None,
    )
    db.session.add(
        TranslationTargetEditIdempotency(
            project_id=project.id,
            source_plan_id=record.id,
            block_id=block.id,
            target_revision_id=revision.id,
            actor_type="owner",
            actor_id=f"user:{project.user_id}",
            idempotency_key="delete-revision-receipt",
            request_fingerprint="f" * 64,
            expected_active_version=0,
            active_version=1,
            safe_result_json={"id": block.id},
        )
    )
    db.session.commit()
    assert TranslationTargetEditIdempotency.query.count() == 1

    db.session.delete(revision)
    db.session.commit()

    assert db.session.get(_models.TranslationTargetBlockRevision, revision.id) is None
    assert TranslationTargetEditIdempotency.query.count() == 0


def test_manual_candidate_owner_mode_remains_backward_compatible(app):
    project = _project("OwnerCompatibility")
    plan, record = _plan(project, "<html><body><p>Hello.</p></body></html>")

    persist_manual_candidate(
        project,
        plan.blocks[0].id,
        _candidate(plan, record, plan.blocks[0], "Cześć."),
        0,
        author_user_id=project.user_id,
    )

    revision = record.target_revisions[0]
    assert revision.author_type == "human"
    assert revision.author_user_id == project.user_id
    assert TranslationTargetRevisionShareAttribution.query.count() == 0


def test_target_revision_reviewer_author_cannot_impersonate_user(app):
    project = _project("ReviewerAuthorShape")
    plan, record = _plan(project, "<html><body><p>Hello.</p></body></html>")

    with pytest.raises(_block_contract.BlockContractError, match="target-author-user"):
        _target_persistence.persist_target_revision(
            record,
            _candidate(plan, record, plan.blocks[0], "Cześć."),
            target_language=project.target_lang,
            author_type="reviewer",
            author_user_id=project.user_id,
        )
    assert record.target_revisions == []


def test_manual_candidate_reviewer_mode_records_share_attribution_without_user(
    app,
):
    project = _project("ReviewerCandidate")
    plan, record = _plan(project, "<html><body><p>Hello.</p></body></html>")
    grant = _share_grant(project, "reviewer-grant")

    persist_manual_candidate(
        project,
        plan.blocks[0].id,
        _candidate(plan, record, plan.blocks[0], "Cześć."),
        0,
        author_user_id=None,
        share_grant_id=grant.id,
    )

    revision = record.target_revisions[0]
    assert revision.author_type == "reviewer"
    assert revision.author_user_id is None
    attribution = TranslationTargetRevisionShareAttribution.query.one()
    assert (
        attribution.project_id,
        attribution.source_plan_id,
        attribution.block_id,
        attribution.target_revision_id,
        attribution.share_grant_id,
    ) == (project.id, record.id, plan.blocks[0].id, revision.id, grant.id)
    public_values = {
        "project_id": attribution.project_id,
        "source_plan_id": attribution.source_plan_id,
        "block_id": attribution.block_id,
        "target_revision_id": attribution.target_revision_id,
        "share_grant_id": attribution.share_grant_id,
        "created_at": utc_iso(attribution.created_at, timespec="microseconds"),
    }
    assert (
        attribution.record_hash
        == hashlib.sha256(
            _block_contract.canonical_json(public_values).encode("utf-8")
        ).hexdigest()
    )
    assert len(attribution.integrity_receipt) == 64
    assert verify_target_revision_share_attribution(attribution) is attribution
    attribution.share_grant_id = "tampered-grant"
    with pytest.raises(
        _block_contract.BlockContractError, match="candidate-share-attribution"
    ):
        verify_target_revision_share_attribution(attribution)


def test_manual_candidate_rejects_invalid_reviewer_authority_without_revision(app):
    project = _project("InvalidReviewerAuthority")
    plan, record = _plan(project, "<html><body><p>Hello.</p></body></html>")
    now = utc_now_db()
    other = _project("OtherReviewerAuthority")
    invalid_grants = (
        _share_grant(project, "viewer-grant", role="viewer"),
        _share_grant(project, "revoked-status-grant", status="revoked"),
        _share_grant(
            project,
            "revoked-at-grant",
            revoked_at=now,
        ),
        _share_grant(
            project,
            "expired-grant",
            expires_at=now - timedelta(seconds=1),
        ),
        _share_grant(
            project,
            "cross-project-grant",
            project_id=other.id,
        ),
    )
    db.session.commit()

    for grant in invalid_grants:
        with pytest.raises(BlockWorkspaceIntegrityError, match="candidate-authority"):
            persist_manual_candidate(
                project,
                plan.blocks[0].id,
                _candidate(plan, record, plan.blocks[0], "Cześć."),
                0,
                share_grant_id=grant.id,
            )
        assert TranslationTargetRevisionShareAttribution.query.count() == 0
        assert len(record.target_revisions) == 0

    with pytest.raises(BlockWorkspaceIntegrityError, match="candidate-authority"):
        persist_manual_candidate(
            project,
            plan.blocks[0].id,
            _candidate(plan, record, plan.blocks[0], "Cześć."),
            0,
            share_grant_id="missing-grant",
        )
    assert TranslationTargetRevisionShareAttribution.query.count() == 0
    assert len(record.target_revisions) == 0

    with pytest.raises(BlockWorkspaceIntegrityError, match="candidate-authority"):
        persist_manual_candidate(
            project,
            plan.blocks[0].id,
            _candidate(plan, record, plan.blocks[0], "Cześć."),
            0,
            author_user_id=project.user_id,
            share_grant_id=invalid_grants[0].id,
        )
    assert len(record.target_revisions) == 0


def test_block_glossary_owner_can_create_and_revise_terms_with_notes(app):
    project = _project()
    _plan(project, "<html><body><p>A widget.</p></body></html>")
    owner = db.session.get(User, project.user_id)
    assert owner is not None
    with app.test_request_context("/block-glossary"):
        login_user(owner)
        created = persist_human_glossary_entry(
            project,
            source_term="Widget",
            target_term="Widżet",
            notes="Keep the product register neutral.",
            author_user_id=project.user_id,
        )
        assert created["contract"] == "translator-block-glossary-v3"
        assert created["entries"] == [
            {
                "concept_id": created["entries"][0]["concept_id"],
                "active_version": 1,
                "source_term": "Widget",
                "target_term": "Widżet",
                "notes": "Keep the product register neutral.",
                "scope": "document",
                "updated_at": created["entries"][0]["updated_at"],
            }
        ]
        concept_id = created["entries"][0]["concept_id"]
        revised = persist_human_glossary_entry(
            project,
            concept_id=concept_id,
            source_term="Widget",
            target_term="Moduł",
            notes="Use the domain-specific term.",
            expected_active_version=1,
            author_user_id=project.user_id,
        )
        assert revised["entries"][0]["active_version"] == 2
        assert revised["entries"][0]["target_term"] == "Moduł"
        assert revised["entries"][0]["notes"] == "Use the domain-specific term."
        assert build_block_glossary_payload(project) == revised


def test_block_glossary_owner_can_lock_contextual_target_variants(app):
    project = _project("GlossaryVariants")
    _plan(project, "<html><body><p>Eine freireligiöse Gemeinde.</p></body></html>")
    owner = db.session.get(User, project.user_id)
    assert owner is not None
    with app.test_request_context("/block-glossary"):
        login_user(owner)
        created = persist_human_glossary_entry(
            project,
            source_term="freireligiös",
            target_term="free-religious",
            accepted_target_forms=[
                "Free Religious",
                " free-religious ",
                "Free Religious",
            ],
            notes="Use the proper-name adjective for the historical movement.",
            author_user_id=project.user_id,
        )
        entry = created["entries"][0]
        assert entry["usage"]["match_policy"] == "preferred"
        assert entry["usage"]["source_forms"] == ["freireligiös"]
        assert entry["usage"]["accepted_target_forms"] == ["Free Religious"]
        assert entry["usage"]["provenance"] == "human-owner"

        revised = persist_human_glossary_entry(
            project,
            concept_id=entry["concept_id"],
            source_term="freireligiös",
            target_term="free-religious",
            accepted_target_forms=[],
            notes=entry["notes"],
            expected_active_version=1,
            author_user_id=project.user_id,
        )
        assert revised["entries"][0]["active_version"] == 2
        assert revised["entries"][0]["usage"]["accepted_target_forms"] == []
        logout_user()


def test_glossary_projection_does_not_rehydrate_document_wide_source_plan(
    app, monkeypatch
):
    project = _project("GlossaryProjection")
    _planned, record = _plan(
        project,
        "<html><body>"
        + "".join(f"<p>Source paragraph {index}.</p>" for index in range(1, 81))
        + "</body></html>",
    )

    with app.test_request_context("/block-glossary"):
        login_user(record.project.user)
        persist_human_glossary_entry(
            project,
            source_term="Widget",
            target_term="Moduł",
            notes="Document terminology",
            author_user_id=project.user_id,
        )
        db.session.commit()
        logout_user()

    def unexpected_document_rehydration(*_args, **_kwargs):
        pytest.fail("glossary projection rehydrated unrelated source blocks")

    monkeypatch.setattr(
        _block_workspace,
        "rehydrate_source_plan",
        unexpected_document_rehydration,
    )
    payload = build_block_glossary_payload(project)

    assert payload["source_plan_id"] == record.id
    assert payload["entries"][0]["source_term"] == "Widget"


def test_glossary_projection_still_fails_closed_for_plan_binding_tamper(app):
    project = _project("GlossaryBinding")
    _planned, record = _plan(project)
    record.plan_hash = "0" * 64

    with pytest.raises(BlockWorkspaceIntegrityError, match="source-plan-corrupt"):
        build_block_glossary_payload(project)
        logout_user()


def test_block_glossary_rejects_duplicate_and_stale_edits(app):
    project = _project()
    _plan(project, "<html><body><p>A widget.</p></body></html>")
    owner = db.session.get(User, project.user_id)
    assert owner is not None
    with app.test_request_context("/block-glossary"):
        login_user(owner)
        created = persist_human_glossary_entry(
            project,
            source_term="Widget",
            target_term="Widżet",
            notes=None,
            author_user_id=project.user_id,
        )
        with pytest.raises(BlockWorkspaceIntegrityError, match="glossary-duplicate"):
            persist_human_glossary_entry(
                project,
                source_term="widget",
                target_term="Duplicate",
                notes=None,
                author_user_id=project.user_id,
            )
        with pytest.raises(
            BlockWorkspaceIntegrityError, match="glossary-active-conflict"
        ):
            persist_human_glossary_entry(
                project,
                concept_id=created["entries"][0]["concept_id"],
                source_term="Widget",
                target_term="Stale",
                notes=None,
                expected_active_version=0,
                author_user_id=project.user_id,
            )
        assert (
            build_block_glossary_payload(project)["entries"][0]["target_term"]
            == "Widżet"
        )
        logout_user()


def test_analysis_glossary_suggestion_requires_exact_owner_review_before_lock(app):
    project = _project("GlossarySuggestion")
    _planned, record = _plan(
        project,
        "<html><body><p>A signal is not merely a sign.</p></body></html>",
    )
    proposals = persist_analysis_glossary_proposals(
        record,
        (
            {
                "source_term": "signal",
                "target_term": "sygnał",
                "notes": "Keep distinct from sign.",
                "scope": "document",
                "source_block_ids": ["redacted-block-witness"],
                "confidence": 0.86,
                "status": "proposed",
            },
        ),
    )
    db.session.commit()

    projected = build_block_glossary_payload(project)
    assert projected["entries"] == []
    assert projected["proposals"] == [
        {
            "proposal_id": proposals[0].id,
            "concept_id": proposals[0].concept_id,
            "source_term": "signal",
            "target_term": "sygnał",
            "notes": "Keep distinct from sign.",
            "scope": "document",
            "proposed_at": projected["proposals"][0]["proposed_at"],
        }
    ]
    assert TranslationGlossaryActive.query.count() == 0

    owner = db.session.get(User, project.user_id)
    assert owner is not None
    with app.test_request_context("/block-glossary"):
        login_user(owner)
        with pytest.raises(
            BlockWorkspaceIntegrityError,
            match="glossary-proposal-conflict",
        ):
            persist_human_glossary_entry(
                project,
                concept_id=proposals[0].concept_id,
                expected_proposal_id="stale-proposal",
                expected_active_version=0,
                source_term="signal",
                target_term="znak sygnałowy",
                notes="Reviewed wording.",
                author_user_id=project.user_id,
            )
        assert TranslationGlossaryRevision.query.count() == 1
        assert TranslationGlossaryActive.query.count() == 0

        approved = persist_human_glossary_entry(
            project,
            concept_id=proposals[0].concept_id,
            expected_proposal_id=proposals[0].id,
            expected_active_version=0,
            source_term="signal",
            target_term="znak sygnałowy",
            notes="Reviewed wording.",
            author_user_id=project.user_id,
        )
        logout_user()

    assert approved["proposals"] == []
    assert approved["entries"][0]["target_term"] == "znak sygnałowy"
    assert approved["entries"][0]["active_version"] == 1
    active = TranslationGlossaryActive.query.one()
    locked = db.session.get(TranslationGlossaryRevision, active.revision_id)
    assert locked is not None
    assert locked.author_type == "human"
    assert locked.revision_state == "locked"
    assert locked.parent_revision_id == proposals[0].id


def test_legacy_routes_fail_closed_for_block_projects(app):
    project = _project()
    application = app
    application.register_blueprint(routes.blueprint, url_prefix="/addons/translator")
    with application.test_request_context(
        f"/addons/translator/api/projects/{project.id}/segments"
    ):
        g.project = project
        g.is_guest = False
        response, status = routes.get_segments.__wrapped__(project.id)
    assert status == 409
    body = response.get_json()
    assert body["engine_contract"] == "document-block-v1"
    assert body["workspace_url"].endswith(f"/api/projects/{project.id}/block-workspace")


def test_project_status_route_does_not_rehydrate_the_block_plan(app):
    project = _project("StatusPoll")
    with app.test_request_context(
        f"/addons/translator/api/projects/{project.id}/status"
    ):
        g.project = project
        g.is_guest = False
        with measure_queries() as metrics:
            response = routes.get_project_status.__wrapped__(project.id)
    payload = response.get_json()
    assert metrics.statement_count <= 2
    assert payload == {
        "contract": "translator-project-status-v1",
        "project_id": project.id,
        "status": "setup",
        "latest_operation": {},
    }


def test_generic_workspace_shells_remain_available_for_block_projects(app, monkeypatch):
    project = _project()
    project.share_token = "shared-block-project"
    db.session.commit()
    application = app
    application.register_blueprint(routes.blueprint, url_prefix="/addons/translator")

    monkeypatch.setattr(routes, "current_user", SimpleNamespace(id=project.user_id))
    monkeypatch.setattr(
        routes, "render_frontend_shell", lambda **_kwargs: "owner-shell"
    )
    monkeypatch.setattr(
        routes, "render_template", lambda *_args, **_kwargs: "guest-shell"
    )

    with application.test_request_context(f"/addons/translator/workspace/{project.id}"):
        assert routes.workspace.__wrapped__(project.id) == "owner-shell"
    with application.test_request_context(
        "/addons/translator/shared/shared-block-project"
    ):
        assert routes.shared_workspace("shared-block-project") == "guest-shell"


@pytest.mark.parametrize("role", ["viewer", "reviewer"])
@pytest.mark.parametrize("proxied", [False, True], ids=["loopback", "forwarded-https"])
def test_share_page_bootstraps_csrf_for_complete_review_flow(
    app, monkeypatch, role, proxied
):
    """Exercise the rendered page and real grants with both CSRF layers enabled."""

    # The persistence fixture holds an outer app context. HTTP requests need
    # separate contexts so Flask-Login and Flask-WTF cannot reuse its g cache.
    class RequestClient(FlaskClient):
        def open(self, *args, **kwargs):
            with app.app_context():
                return super().open(*args, **kwargs)

    app.test_client_class = RequestClient
    for prefix in ("CARLINA", "IARF"):
        for key in ("WEB_PROFILE", "EXTERNAL_URL", "TRUSTED_HOSTS", "PROXY_HOPS"):
            monkeypatch.delenv(f"{prefix}_{key}", raising=False)
    origin = "https://review.example.test" if proxied else "http://localhost"
    forwarding = {}
    if proxied:
        monkeypatch.setenv("CARLINA_WEB_PROFILE", "server")
        monkeypatch.setenv("CARLINA_EXTERNAL_URL", origin)
        monkeypatch.setenv("CARLINA_TRUSTED_HOSTS", "review.example.test,localhost")
        monkeypatch.setenv("CARLINA_PROXY_HOPS", "1")
        forwarding = {
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "review.example.test",
            "X-Forwarded-Port": "443",
        }
    security = import_module(f"{_APPLICATION_ROOT}.core.web_security")
    app.root_path = str(Path(routes.__file__).resolve().parents[2])
    security.configure_web_security(app)
    app.config["CARLINA_ENFORCE_CSRF_IN_TESTS"] = True
    app.register_blueprint(routes.blueprint, url_prefix="/addons/translator")
    project = _project("Sharing CSRF")
    plan, _record = _plan(project)
    review_path = f"/addons/translator/review/{project.id}"
    grants_path = f"/addons/translator/api/projects/{project.id}/share-grants"

    def bootstrap(client):
        response = client.get(review_path, base_url=origin, headers=forwarding)
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        token = re.search(r'<meta name="csrf-token" content="([^"]+)">', html)
        assert token is not None
        assert html.index("js/csrf.js") < html.index('type="module"')
        assert "/static/frontend/assets/core-" in html
        assert "output.css" not in html
        return {
            **forwarding,
            "Origin": origin,
            "Referer": f"{origin}{review_path}",
            "X-CSRF-Token": token.group(1),
        }

    owner = app.test_client()
    with owner.session_transaction(base_url=origin) as session:
        session["_user_id"] = str(project.user_id)
        session["_fresh"] = True
    owner_headers = bootstrap(owner)
    issued = owner.post(
        grants_path,
        base_url=origin,
        headers=owner_headers,
        json={"label": "Synthetic reader", "role": role, "expires_at": None},
    )
    assert issued.status_code == 201
    grant = issued.get_json()
    guest = app.test_client()
    headers = bootstrap(guest)
    redeem_path = "/addons/translator/api/share/redeem"
    body = {"project_id": project.id, "secret": grant["secret"]}
    assert (
        guest.post(
            redeem_path, base_url=origin, headers=forwarding, json=body
        ).status_code
        == 400
    )
    assert (
        guest.post(
            redeem_path,
            base_url=origin,
            headers={**headers, "Origin": "https://evil.example"},
            json=body,
        ).status_code
        == 403
    )
    redeemed = guest.post(redeem_path, base_url=origin, headers=headers, json=body)
    assert redeemed.status_code == 200
    assert redeemed.get_json()["role"] == role
    assert "HttpOnly" in redeemed.headers["Set-Cookie"]
    assert ("Secure;" in redeemed.headers["Set-Cookie"]) is proxied
    workspace_path = f"/addons/translator/api/projects/{project.id}/block-workspace"
    workspace = guest.get(workspace_path, base_url=origin, headers=forwarding)
    assert workspace.status_code == 200
    assert workspace.get_json()["access"]["role"] == role

    # A fresh page load restores its session using the host token and obtains
    # the separate Translator token needed for edits.
    headers = bootstrap(guest)
    restore_path = "/addons/translator/api/share/session"
    assert (
        guest.post(
            restore_path,
            base_url=origin,
            headers=forwarding,
            json={"project_id": project.id},
        ).status_code
        == 400
    )
    restored = guest.post(
        restore_path,
        base_url=origin,
        headers=headers,
        json={"project_id": project.id},
    )
    assert restored.status_code == 200
    edit_headers = {
        **headers,
        "X-Translator-CSRF": restored.get_json()["csrf"],
        "Idempotency-Key": "synthetic-share-note",
    }
    note_path = f"/addons/translator/api/projects/{project.id}/blocks/{plan.blocks[0].id}/reader-notes"
    note = {
        "body": "A synthetic review note.",
        "anchor": {"kind": "block"},
        "citation_ids": [],
    }
    assert (
        guest.post(note_path, base_url=origin, headers=headers, json=note).status_code
        == 403
    )
    assert (
        guest.post(
            note_path,
            base_url=origin,
            headers={
                key: value
                for key, value in edit_headers.items()
                if key != "X-CSRF-Token"
            },
            json=note,
        ).status_code
        == 400
    )
    edited = guest.post(note_path, base_url=origin, headers=edit_headers, json=note)
    assert edited.status_code == (201 if role == "reviewer" else 403)
    revoked = owner.delete(
        f"{grants_path}/{grant['grant']['id']}",
        base_url=origin,
        headers=owner_headers,
    )
    assert revoked.status_code == 200
    assert (
        guest.get(workspace_path, base_url=origin, headers=forwarding).status_code
        == 404
    )
    denied = guest.post(redeem_path, base_url=origin, headers=headers, json=body)
    assert denied.status_code == 400
    assert denied.get_json()["code"] == "grant-invalid"
