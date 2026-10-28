"""Numbering overlays preserve immutable translation evidence."""

from __future__ import annotations

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from content_storage_support import bind_content_policy
from flask import Flask
from sqlalchemy import inspect as sqlalchemy_inspect

import iarf_assistant.addons.translator.numbering_sync as sync_service
from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION
from iarf_assistant.addons.translator.alignment_persistence import (
    persist_alignment_revision,
    persist_target_span_set,
)
from iarf_assistant.addons.translator.block_contract import AttestedDocumentSnapshot
from iarf_assistant.addons.translator.block_export import build_semantic_export_request
from iarf_assistant.addons.translator.block_persistence import (
    persist_source_plan,
    rehydrate_source_plan,
    source_attestation_for_project,
)
from iarf_assistant.addons.translator.block_planner import plan_document_blocks
from iarf_assistant.addons.translator.models import (
    TranslationNumberingOverlay,
    TranslationOperation,
    TranslationProject,
)
from iarf_assistant.addons.translator.numbering_sync import (
    NumberingSyncError,
    apply_numbering_sync,
    numbering_state,
    preview_numbering_sync,
    project_source_numbering,
    require_no_numbering_overlay,
    undo_numbering_sync,
)
from iarf_assistant.addons.translator.target_contract import (
    TargetBlockCandidate,
    TargetTextPart,
    structural_fingerprint,
)
from iarf_assistant.addons.translator.target_persistence import (
    activate_initial_target_revisions,
    activate_target_revision,
    persist_target_revision,
    render_persisted_target_document,
)
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.extensions import db
from iarf_assistant.models import (
    ApiKey,
    LLMModel,
    LLMProvider,
    RuntimeApprovalRequest,
    User,
)

PINNED = "<html><body><ol><li>Alpha</li><li>Beta</li></ol></body></html>"
CORRECTED = "<html><body><ol start='3'><li>Alpha</li><li>Beta</li></ol></body></html>"


@pytest.fixture
def app(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'numbering.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="numbering-test",
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
        with db.engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            db.metadata.drop_all(connection)
        db.engine.dispose()
        context.pop()


def _snapshot(html: str) -> AttestedDocumentSnapshot:
    digest = hashlib.sha256(html.encode()).hexdigest()
    return AttestedDocumentSnapshot(
        id="doc-numbering",
        document_id="doc-numbering",
        output_html=html,
        content_hash=digest,
        semantic_content_hash=digest,
        metadata={},
        provenance={},
        captured_at="2026-09-27T10:00:00Z",
        semantic_contract_version=SEMANTIC_HTML_CONTRACT_VERSION,
    )


def _project(email: str, source_html: str = PINNED):
    owner = User(email=email, password_hash="x")
    db.session.add(owner)
    db.session.flush()
    project = TranslationProject(
        user_id=owner.id,
        name="Numbering test",
        source_lang="en",
        target_lang="pl",
        engine_contract="document-block-v1",
        document_id="doc-numbering",
        settings={
            "source_reference": {
                "version": 1,
                "capability": "document.processing",
                "provider_addon_id": "doc-provider",
                "provider_capability_id": "document-processing",
                "resource_type": "document",
                "resource_id": "doc-numbering",
            }
        },
    )
    db.session.add(project)
    db.session.flush()
    plan = plan_document_blocks(
        _snapshot(source_html),
        project_id=project.id,
        source_attestation=source_attestation_for_project(project),
        source_language="en",
    )
    record, _ = persist_source_plan(project, plan)
    db.session.commit()
    return project, record


def _loader(html: str = CORRECTED):
    return lambda: _snapshot(html).to_dict()


def _apply(project, record, *, key="apply-1"):
    preview = preview_numbering_sync(
        project, owner_user_id=project.user_id, load_current_source=_loader()
    )
    assert len(preview["changes"]) == 1
    result = apply_numbering_sync(
        project,
        owner_user_id=project.user_id,
        load_current_source=_loader(),
        preview_token=preview["preview_token"],
        selected_ids=[preview["changes"][0]["id"]],
        idempotency_key=key,
    )
    return preview, result


def test_apply_undo_replay_and_encrypted_storage(app):
    project, record = _project("numbering@example.test")
    preview, applied = _apply(project, record)
    assert preview["revision"] == 0
    assert applied["revision"] == 1
    assert 'start="3"' in project_source_numbering(record)
    assert record.source_content_hash == hashlib.sha256(PINNED.encode()).hexdigest()
    assert project.source_snapshot == PINNED
    assert (
        apply_numbering_sync(
            project,
            owner_user_id=project.user_id,
            load_current_source=_loader("<html><body>later edit</body></html>"),
            preview_token=preview["preview_token"],
            selected_ids=[preview["changes"][0]["id"]],
            idempotency_key="apply-1",
        )
        == applied
    )
    with pytest.raises(NumberingSyncError, match="numbering-idempotency-conflict"):
        undo_numbering_sync(
            project,
            owner_user_id=project.user_id,
            load_current_source=_loader(),
            preview_token=preview["preview_token"],
            idempotency_key="apply-1",
        )
    row = db.session.get(TranslationNumberingOverlay, project.id)
    assert row is not None
    raw = (
        db.session.connection()
        .exec_driver_sql(
            "SELECT payload FROM translation_numbering_overlay WHERE project_id = ?",
            (project.id,),
        )
        .scalar_one()
    )
    assert "Alpha" not in raw and "Beta" not in raw
    with pytest.raises(NumberingSyncError, match="numbering-source-plan-conflict"):
        require_no_numbering_overlay(project.id)
    next_preview = preview_numbering_sync(
        project, owner_user_id=project.user_id, load_current_source=_loader()
    )
    undone = undo_numbering_sync(
        project,
        owner_user_id=project.user_id,
        load_current_source=_loader(),
        preview_token=next_preview["preview_token"],
        idempotency_key="undo-1",
    )
    assert undone["revision"] == 2
    record_id, project_id = record.id, project.id
    db.session.remove()
    restored_record = db.session.get(type(record), record_id)
    assert restored_record is not None
    assert numbering_state(restored_record) == {"revision": 2, "entries": []}
    assert project_source_numbering(restored_record) == PINNED
    require_no_numbering_overlay(project_id)


def test_stale_upstream_owner_and_independent_projects(app):
    first, first_record = _project("first-numbering@example.test")
    second, second_record = _project("second-numbering@example.test")
    first_preview = preview_numbering_sync(
        first, owner_user_id=first.user_id, load_current_source=_loader()
    )
    with pytest.raises(NumberingSyncError, match="numbering-owner-conflict"):
        preview_numbering_sync(
            first, owner_user_id=second.user_id, load_current_source=_loader()
        )
    with pytest.raises(NumberingSyncError, match="numbering-preview-stale"):
        apply_numbering_sync(
            first,
            owner_user_id=first.user_id,
            load_current_source=_loader(
                "<html><body><ol start='4'><li>Alpha</li><li>Beta</li></ol></body></html>"
            ),
            preview_token=first_preview["preview_token"],
            selected_ids=[first_preview["changes"][0]["id"]],
            idempotency_key="stale-1",
        )
    _apply(first, first_record)
    assert numbering_state(first_record)["revision"] == 1
    assert numbering_state(second_record)["revision"] == 0
    assert project_source_numbering(second_record) == PINNED


def test_stale_overlay_and_active_job_are_fenced(app):
    project, record = _project("fenced-numbering@example.test")
    preview = preview_numbering_sync(
        project, owner_user_id=project.user_id, load_current_source=_loader()
    )
    operation = TranslationOperation(
        project_id=project.id,
        owner_user_id=project.user_id,
        kind="translate",
        idempotency_key="numbering-active-job",
        request_fingerprint="a" * 64,
        state="running",
    )
    db.session.add(operation)
    db.session.commit()
    with pytest.raises(NumberingSyncError, match="numbering-operation-active"):
        apply_numbering_sync(
            project,
            owner_user_id=project.user_id,
            load_current_source=_loader(),
            preview_token=preview["preview_token"],
            selected_ids=[preview["changes"][0]["id"]],
            idempotency_key="while-job",
        )
    operation.state = "completed"
    db.session.commit()
    first = apply_numbering_sync(
        project,
        owner_user_id=project.user_id,
        load_current_source=_loader(),
        preview_token=preview["preview_token"],
        selected_ids=[preview["changes"][0]["id"]],
        idempotency_key="first",
    )
    assert first["revision"] == 1
    with pytest.raises(NumberingSyncError, match="numbering-preview-stale"):
        apply_numbering_sync(
            project,
            owner_user_id=project.user_id,
            load_current_source=_loader(),
            preview_token=preview["preview_token"],
            selected_ids=[preview["changes"][0]["id"]],
            idempotency_key="stale-overlay",
        )
    assert numbering_state(record)["revision"] == 1


def test_target_projection_changes_only_numbering(app):
    project, record = _project("target-numbering@example.test")
    plan = rehydrate_source_plan(record)
    pairs = []
    for block in plan.blocks:
        candidate = TargetBlockCandidate(
            source_plan_id=record.id,
            source_plan_hash=plan.plan_hash,
            block_id=block.id,
            structural_fingerprint=structural_fingerprint(plan, block),
            parts=(TargetTextPart(kind="text", text=f"Target {block.order}."),),
            lexical_attributes=(),
        )
        revision = persist_target_revision(
            record,
            candidate,
            target_language="pl",
            author_type="model",
            verified_plan=plan,
            notes=(
                [{"anchor": "Target", "body": "Preserved translator note."}]
                if block.order == 1
                else ()
            ),
        )
        alignment = persist_alignment_revision(
            record, persist_target_span_set(record, revision)
        )
        pairs.append((revision, alignment))
    activate_initial_target_revisions(record, pairs, verified_plan=plan)
    db.session.commit()
    stale_target_preview = preview_numbering_sync(
        project, owner_user_id=project.user_id, load_current_source=_loader()
    )
    first_revision, first_alignment = pairs[0]
    activate_target_revision(
        record,
        block_id=first_revision.block_id,
        revision_id=first_revision.id,
        alignment_revision_id=first_alignment.id,
        expected_version=1,
    )
    db.session.commit()
    with pytest.raises(NumberingSyncError, match="numbering-preview-stale"):
        apply_numbering_sync(
            project,
            owner_user_id=project.user_id,
            load_current_source=_loader(),
            preview_token=stale_target_preview["preview_token"],
            selected_ids=[stale_target_preview["changes"][0]["id"]],
            idempotency_key="stale-target",
        )
    before = render_persisted_target_document(record)
    export_before = build_semantic_export_request(record, export_format="html")
    evidence_tables = (
        "translation_target_block_revision",
        "translation_target_block_active",
        "translation_alignment_revision",
        "translation_target_alignment_active",
        "translation_target_block_note",
        "translation_reader_note",
        "translation_reader_note_revision",
    )

    def stored_evidence():
        return {
            table: db.session.connection()
            .exec_driver_sql(f"SELECT * FROM {table} ORDER BY 1")
            .fetchall()
            for table in evidence_tables
        }

    evidence_before = stored_evidence()
    assert evidence_before["translation_target_block_note"]
    _apply(project, record)
    after = render_persisted_target_document(record)
    export_after = build_semantic_export_request(record, export_format="html")
    evidence_after = stored_evidence()
    assert evidence_after == evidence_before
    assert before.replace("<ol>", '<ol start="3">', 1) == after
    assert export_before.metadata["source-plan"]["numbering-revision"] == 0
    assert export_after.metadata["source-plan"]["numbering-revision"] == 1
    undo_preview = preview_numbering_sync(
        project, owner_user_id=project.user_id, load_current_source=_loader()
    )
    undo_numbering_sync(
        project,
        owner_user_id=project.user_id,
        load_current_source=_loader(),
        preview_token=undo_preview["preview_token"],
        idempotency_key="export-undo",
    )
    export_undone = build_semantic_export_request(record, export_format="html")
    assert export_undone.semantic_html == export_before.semantic_html
    assert export_undone.metadata["source-plan"]["numbering-revision"] == 2


def test_independent_contexts_serialize_same_preview(app):
    project, record = _project("race-numbering@example.test")
    preview = preview_numbering_sync(
        project, owner_user_id=project.user_id, load_current_source=_loader()
    )
    project_id, owner_id, record_id = project.id, project.user_id, record.id
    change_id = preview["changes"][0]["id"]
    barrier = threading.Barrier(2)
    db.session.remove()

    def attempt(key: str) -> str:
        with app.app_context():
            try:
                local_project = db.session.get(TranslationProject, project_id)
                assert local_project is not None
                barrier.wait(timeout=5)
                result = apply_numbering_sync(
                    local_project,
                    owner_user_id=owner_id,
                    load_current_source=_loader(),
                    preview_token=preview["preview_token"],
                    selected_ids=[change_id],
                    idempotency_key=key,
                )
                return str(result["revision"])
            except NumberingSyncError as exc:
                return exc.code
            finally:
                db.session.remove()

    with ThreadPoolExecutor(max_workers=2) as pool:
        left = pool.submit(attempt, "race-left")
        right = pool.submit(attempt, "race-right")
        outcomes = {left.result(timeout=15), right.result(timeout=15)}
    assert outcomes == {"1", "numbering-preview-stale"}
    persisted_record = db.session.get(type(record), record_id)
    assert persisted_record is not None
    assert numbering_state(persisted_record)["revision"] == 1


def test_undo_walks_applies_and_stops(app):
    pinned = (
        "<html><body><ol><li>Alpha</li></ol>"
        "<ol><li>Beta</li></ol></body></html>"
    )
    corrected = (
        "<html><body><ol start='3'><li>Alpha</li></ol>"
        "<ol start='7'><li>Beta</li></ol></body></html>"
    )
    project, record = _project("undo-stack@example.test", pinned)
    first_preview = preview_numbering_sync(
        project, owner_user_id=project.user_id, load_current_source=_loader(corrected)
    )
    assert len(first_preview["changes"]) == 2
    first = apply_numbering_sync(
        project,
        owner_user_id=project.user_id,
        load_current_source=_loader(corrected),
        preview_token=first_preview["preview_token"],
        selected_ids=[first_preview["changes"][0]["id"]],
        idempotency_key="apply-a",
    )
    second_preview = preview_numbering_sync(
        project, owner_user_id=project.user_id, load_current_source=_loader(corrected)
    )
    second = apply_numbering_sync(
        project,
        owner_user_id=project.user_id,
        load_current_source=_loader(corrected),
        preview_token=second_preview["preview_token"],
        selected_ids=[second_preview["changes"][0]["id"]],
        idempotency_key="apply-b",
    )
    assert [first["revision"], second["revision"]] == [1, 2]
    undo_b_preview = preview_numbering_sync(
        project, owner_user_id=project.user_id, load_current_source=_loader(corrected)
    )
    undo_b = undo_numbering_sync(
        project,
        owner_user_id=project.user_id,
        load_current_source=_loader(corrected),
        preview_token=undo_b_preview["preview_token"],
        idempotency_key="undo-b",
    )
    assert undo_b["revision"] == 3 and undo_b["can_undo"]
    assert 'start="3"' in project_source_numbering(record)
    assert 'start="7"' not in project_source_numbering(record)
    undo_a_preview = preview_numbering_sync(
        project, owner_user_id=project.user_id, load_current_source=_loader(corrected)
    )
    undo_a = undo_numbering_sync(
        project,
        owner_user_id=project.user_id,
        load_current_source=_loader(corrected),
        preview_token=undo_a_preview["preview_token"],
        idempotency_key="undo-a",
    )
    assert undo_a["revision"] == 4 and not undo_a["can_undo"]
    assert project_source_numbering(record) == pinned
    empty_preview = preview_numbering_sync(
        project, owner_user_id=project.user_id, load_current_source=_loader(corrected)
    )
    assert not empty_preview["can_undo"]
    with pytest.raises(NumberingSyncError, match="numbering-undo-unavailable"):
        undo_numbering_sync(
            project,
            owner_user_id=project.user_id,
            load_current_source=_loader(corrected),
            preview_token=empty_preview["preview_token"],
            idempotency_key="third-undo",
        )
    assert apply_numbering_sync(
        project,
        owner_user_id=project.user_id,
        load_current_source=_loader(corrected),
        preview_token=first_preview["preview_token"],
        selected_ids=[first_preview["changes"][0]["id"]],
        idempotency_key="apply-a",
    ) == first
    new_apply = apply_numbering_sync(
        project,
        owner_user_id=project.user_id,
        load_current_source=_loader(corrected),
        preview_token=empty_preview["preview_token"],
        selected_ids=[empty_preview["changes"][0]["id"]],
        idempotency_key="apply-after-undo",
    )
    assert new_apply["revision"] == 5


@pytest.mark.parametrize("limit_kind", ["history", "payload"])
def test_apply_reserves_capacity_for_undo(app, monkeypatch, limit_kind):
    pinned = (
        "<html><body><ol><li>Alpha</li></ol>"
        "<ol><li>Beta</li></ol></body></html>"
    )
    corrected = (
        "<html><body><ol start='3'><li>Alpha</li></ol>"
        "<ol start='7'><li>Beta</li></ol></body></html>"
    )
    project, record = _project(f"reserve-{limit_kind}@example.test", pinned)
    first_preview = preview_numbering_sync(
        project, owner_user_id=project.user_id, load_current_source=_loader(corrected)
    )
    apply_numbering_sync(
        project,
        owner_user_id=project.user_id,
        load_current_source=_loader(corrected),
        preview_token=first_preview["preview_token"],
        selected_ids=[first_preview["changes"][0]["id"]],
        idempotency_key="reserve-first",
    )
    row = db.session.get(TranslationNumberingOverlay, project.id)
    assert row is not None
    if limit_kind == "history":
        monkeypatch.setattr(sync_service, "MAX_HISTORY", 3)
        monkeypatch.setattr(sync_service, "MAX_RECEIPTS", 3)
    else:
        # Select the smallest payload limit that still admits the first
        # action's promised undo. A second apply must be rejected under it.
        low = sync_service._payload_bytes(row.payload)
        high = sync_service.MAX_PAYLOAD_BYTES
        while low < high:
            middle = (low + high) // 2
            monkeypatch.setattr(sync_service, "MAX_PAYLOAD_BYTES", middle)
            try:
                sync_service._reserve_undo_capacity(row.payload, record)
            except NumberingSyncError:
                low = middle + 1
            else:
                high = middle
        monkeypatch.setattr(sync_service, "MAX_PAYLOAD_BYTES", high)
    second_preview = preview_numbering_sync(
        project, owner_user_id=project.user_id, load_current_source=_loader(corrected)
    )
    with pytest.raises(NumberingSyncError, match="numbering-(undo-capacity|overlay-full)"):
        apply_numbering_sync(
            project,
            owner_user_id=project.user_id,
            load_current_source=_loader(corrected),
            preview_token=second_preview["preview_token"],
            selected_ids=[second_preview["changes"][0]["id"]],
            idempotency_key="reserve-second",
        )
    undo_preview = preview_numbering_sync(
        project, owner_user_id=project.user_id, load_current_source=_loader(corrected)
    )
    undone = undo_numbering_sync(
        project,
        owner_user_id=project.user_id,
        load_current_source=_loader(corrected),
        preview_token=undo_preview["preview_token"],
        idempotency_key="reserve-undo",
    )
    assert undone["revision"] == 2
    assert numbering_state(record)["entries"] == []
    require_no_numbering_overlay(project.id)


def test_migration_026_upgrade_and_reapply(tmp_path: Path):
    database_path = tmp_path / "numbering-migration.db"
    migrations = (
        Path(__file__).resolve().parents[1]
        / "iarf_assistant"
        / "addons"
        / "translator"
        / "migrations"
    )
    config = Config(str(migrations / "alembic.ini"))
    config.set_main_option("script_location", str(migrations))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    application = Flask(__name__, instance_path=str(tmp_path / "migration-instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{database_path}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="numbering-migration-test",
    )
    db.init_app(application)
    with application.app_context():
        User.__table__.create(db.engine)
        LLMProvider.__table__.create(db.engine)
        ApiKey.__table__.create(db.engine)
        LLMModel.__table__.create(db.engine)
        with db.engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE workflow_run (id VARCHAR(36) PRIMARY KEY)"
            )
        RuntimeApprovalRequest.__table__.create(db.engine)
        command.upgrade(config, "025_target_edit_idempotency")
        command.upgrade(config, "026_numbering_overlay")
        command.upgrade(config, "026_numbering_overlay")
        columns = {
            column["name"]
            for column in sqlalchemy_inspect(db.engine).get_columns(
                "translation_numbering_overlay"
            )
        }
        assert {
            "project_id",
            "owner_user_id",
            "source_plan_id",
            "source_plan_hash",
            "revision",
            "payload",
            "updated_at",
        } == columns
        db.session.remove()
        db.engine.dispose()
