"""Focused persistence checks for Translator review coverage evidence."""

from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from alembic import command
from content_storage_support import bind_content_policy
from flask import Flask
from flask_login import LoginManager
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy import text
from test_translator_migrations import _migrate_phase5_database, _migration_config

from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION
from iarf_assistant.addons.translator import review_coverage
from iarf_assistant.addons.translator.block_contract import AttestedDocumentSnapshot
from iarf_assistant.addons.translator.block_persistence import (
    persist_source_plan,
    source_attestation_for_project,
)
from iarf_assistant.addons.translator.block_planner import plan_document_blocks
from iarf_assistant.addons.translator.block_workspace_api import (
    BlockWorkspaceIntegrityError,
    load_block_source_plan,
    persist_manual_candidate,
)
from iarf_assistant.addons.translator.models import (
    TranslationBlockReviewHead,
    TranslationProject,
    TranslationReviewCoverage,
    TranslationTargetBlockActive,
)
from iarf_assistant.addons.translator.scholarly_review import (
    ReviewActor,
    ScholarlyReviewError,
)
from iarf_assistant.addons.translator.target_contract import (
    TARGET_BLOCK_RESULT_CONTRACT_VERSION,
    structural_fingerprint,
)
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.extensions import db
from iarf_assistant.models import User


@pytest.fixture
def app(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'coverage.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="review-coverage-test",
        TESTING=True,
    )
    db.init_app(application)
    login_manager = LoginManager()
    login_manager.init_app(application)

    @login_manager.user_loader
    def load_user(user_id: str):
        return db.session.get(User, int(user_id))

    keys = InstallationKeyStore(tmp_path / "master-keys.json")
    keys.initialize()
    application.extensions["carlina_encryption"] = EncryptionService(keys)
    context = application.app_context()
    context.push()
    db.create_all()
    with db.engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    try:
        yield application
    finally:
        db.session.remove()
        with db.engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            db.metadata.drop_all(connection)
        db.engine.dispose()
        context.pop()


def _project_and_plan(name: str = "Coverage"):
    source_html = (
        "<html><body><p>First source sentence. Second source sentence.</p>"
        "<p>Additional source block.</p></body></html>"
    )
    owner = User(email=f"{name.lower()}@example.test", password_hash="test")
    db.session.add(owner)
    db.session.flush()
    digest = hashlib.sha256(source_html.encode()).hexdigest()
    project = TranslationProject(
        user_id=owner.id,
        name=name,
        source_lang="en",
        target_lang="pl",
        status="setup",
        engine_contract="document-block-v1",
        document_id=f"doc-{name.lower()}",
        source_content_hash=digest,
        settings={
            "source_type": "document",
            "source_reference": {
                "version": 1,
                "capability": "document.processing",
                "provider_addon_id": "doc-provider",
                "provider_capability_id": "document-processing",
                "resource_type": "document",
                "resource_id": f"doc-{name.lower()}",
            },
        },
    )
    db.session.add(project)
    db.session.flush()
    snapshot = AttestedDocumentSnapshot(
        id=project.document_id,
        document_id=project.document_id,
        output_html=source_html,
        content_hash=digest,
        semantic_content_hash=digest,
        metadata={},
        provenance={},
        captured_at="2026-10-01T12:00:00+00:00",
        semantic_contract_version=SEMANTIC_HTML_CONTRACT_VERSION,
    )
    plan = plan_document_blocks(
        snapshot,
        project_id=project.id,
        source_attestation=source_attestation_for_project(project),
        source_language="en",
    )
    record, _created = persist_source_plan(project, plan)
    db.session.commit()
    return project, record, list(plan.blocks), owner


def _candidate(workspace, block, *, suffix: str = "") -> dict[str, object]:
    slots = {slot.id: slot for slot in block.slots}
    parts: list[dict[str, object]] = []
    suffix_added = False
    for wire_part in block.wire:
        if wire_part.kind == "token":
            parts.append({"kind": "token", "token_id": wire_part.ref_id})
            continue
        slot = slots[wire_part.ref_id]
        if slot.disposition == "literal-preserved":
            parts.append({"kind": "literal", "literal_slot_id": slot.id})
        elif slot.disposition == "translatable":
            text_value = slot.source_value
            if suffix and not suffix_added:
                text_value += suffix
                suffix_added = True
            parts.append({"kind": "text", "text": text_value})
    return {
        "contract_version": TARGET_BLOCK_RESULT_CONTRACT_VERSION,
        "source_plan_id": workspace.record.id,
        "source_plan_hash": workspace.plan.plan_hash,
        "block_id": block.id,
        "structural_fingerprint": structural_fingerprint(workspace.plan, block),
        "parts": parts,
        "lexical_attributes": [],
    }


def _activate_target(project, block, *, expected_version: int = 0, suffix: str = ""):
    workspace = load_block_source_plan(project)
    current_block = next(item for item in workspace.plan.blocks if item.id == block.id)
    return persist_manual_candidate(
        project,
        current_block.id,
        _candidate(workspace, current_block, suffix=suffix),
        expected_active_version=expected_version,
        author_user_id=project.user_id,
    )


def _entry(block, target_version: int, *, scope="block", anchors=()):
    return {
        "block_id": block.id,
        "expected_target_version": target_version,
        "scope": scope,
        "source_anchor_ids": list(anchors),
    }


def test_coverage_is_encrypted_idempotent_and_stales_with_target(app):
    project, _record, blocks, owner = _project_and_plan()
    block = next(item for item in blocks if item.anchors)
    _activate_target(project, block)
    active = db.session.get(
        TranslationTargetBlockActive,
        {
            "source_plan_id": load_block_source_plan(project).record.id,
            "block_id": block.id,
        },
    )
    assert active is not None
    actor = ReviewActor("owner", owner.id)
    request = {
        "entries": [_entry(block, active.version)],
        "kind": "structural",
        "comment": "Private coverage comment.",
        "actor": actor,
        "idempotency_key": "coverage-batch-v1",
    }

    recorded = review_coverage.record_review_coverage(project, **request)
    assert recorded["coverage_id"]
    assert recorded["reviewer_kind"] == "agent"
    assert recorded["entries"] == [
        {
            "block_id": block.id,
            "target_revision_id": active.revision_id,
            "target_version": 1,
            "scope": "block",
            "source_anchor_ids": [],
        }
    ]
    assert TranslationBlockReviewHead.query.count() == 0

    raw_payload = db.session.execute(
        text(
            "SELECT payload_json FROM translation_review_coverage "
            "WHERE id = :coverage_id"
        ),
        {"coverage_id": recorded["coverage_id"]},
    ).scalar_one()
    assert "Private coverage comment." not in raw_payload
    assert raw_payload.startswith("enc:")
    db.session.commit()

    _activate_target(project, block, expected_version=1, suffix=" revised")
    replay = review_coverage.record_review_coverage(project, **request)
    assert replay == recorded
    with pytest.raises(
        ScholarlyReviewError, match="review coverage idempotency conflict"
    ):
        review_coverage.record_review_coverage(
            project,
            **{**request, "comment": "Changed under the same key."},
        )

    manifest = review_coverage.review_coverage_manifest(project, block_ids=[block.id])
    current_block = manifest["blocks"][0]
    assert current_block["target_version"] == 2
    assert current_block["coverage"] == []
    assert current_block["kinds"]["structural"]["complete"] is False
    assert manifest["summary"]["structural"]["missing"] == 1
    assert TranslationReviewCoverage.query.count() == 1
    with pytest.raises(ScholarlyReviewError, match="review coverage required"):
        review_coverage.snapshot_export_review(project, "structural")

    snapshot = review_coverage.snapshot_export_review(project)
    expected_hash = hashlib.sha256(
        json.dumps(
            {key: value for key, value in snapshot.items() if key != "sha256"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert snapshot["sha256"] == expected_hash
    assert review_coverage.validate_export_review_snapshot(project, snapshot)

    review_coverage.record_review_coverage(
        project,
        entries=[_entry(block, 2)],
        kind="target_reading",
        actor=actor,
        idempotency_key="coverage-added-after-snapshot",
    )
    assert review_coverage.validate_export_review_snapshot(project, snapshot)

    tampered_snapshot = json.loads(json.dumps(snapshot))
    tampered_snapshot["coverage_manifest"]["summary"]["structural"]["complete"] += 1
    tampered_snapshot["sha256"] = hashlib.sha256(
        json.dumps(
            {key: value for key, value in tampered_snapshot.items() if key != "sha256"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    with pytest.raises(ScholarlyReviewError, match="review coverage export stale"):
        review_coverage.validate_export_review_snapshot(project, tampered_snapshot)


def test_page_manifest_reuses_verified_active_target_objects(app, monkeypatch):
    project, _record, blocks, _owner = _project_and_plan("CoverageProjection")
    block = blocks[0]
    _activate_target(project, block)
    workspace = load_block_source_plan(project)
    active_targets, _projections = review_coverage._rehydrate_workspace_objects(
        workspace
    )

    def unexpected_rehydrate(_workspace):
        raise AssertionError("page manifest rehydrated every target again")

    monkeypatch.setattr(
        review_coverage, "_rehydrate_workspace_objects", unexpected_rehydrate
    )
    manifest = review_coverage.review_coverage_manifest(
        project,
        block_ids=[block.id],
        _workspace_plan=workspace,
        _active_targets=active_targets,
    )
    assert len(manifest["blocks"]) == 1
    assert manifest["blocks"][0]["target_version"] == 1


def test_concurrent_same_key_insert_returns_committed_winner(app, monkeypatch):
    project, _record, blocks, owner = _project_and_plan("CoverageRace")
    block = blocks[0]
    _activate_target(project, block)
    db.session.commit()
    project_id = project.id
    owner_id = owner.id
    entry = _entry(block, 1)
    actor = ReviewActor("owner", owner_id)
    request = {
        "entries": [entry],
        "kind": "bilingual",
        "actor": actor,
        "idempotency_key": "concurrent-coverage-key",
    }
    original_insert = review_coverage._persist_new_row
    loser_ready = threading.Event()
    allow_loser_insert = threading.Event()
    worker_state = threading.local()

    def gated_insert(row):
        if getattr(worker_state, "wait_for_winner", False):
            loser_ready.set()
            if not allow_loser_insert.wait(timeout=10):
                raise AssertionError("same-key winner did not commit")
        return original_insert(row)

    monkeypatch.setattr(review_coverage, "_persist_new_row", gated_insert)

    def losing_writer():
        worker_state.wait_for_winner = True
        with app.app_context():
            try:
                worker_project = db.session.get(TranslationProject, project_id)
                result = review_coverage.record_review_coverage(
                    worker_project, **request
                )
                db.session.commit()
                return result
            finally:
                db.session.remove()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(losing_writer)
        assert loser_ready.wait(timeout=10)
        winning_result = review_coverage.record_review_coverage(project, **request)
        db.session.commit()
        allow_loser_insert.set()
        assert future.result(timeout=10) == winning_result
    assert TranslationReviewCoverage.query.count() == 1


def test_export_snapshot_lock_fences_active_targets_without_committing(app):
    project, _record, blocks, _owner = _project_and_plan("CoverageExportLock")
    _activate_target(project, blocks[0])
    db.session.commit()
    snapshot = review_coverage.snapshot_export_review(project)
    uncommitted_user = User(
        email="export-lock-rollback@example.test", password_hash="test"
    )
    db.session.add(uncommitted_user)

    assert review_coverage.lock_export_review_snapshot(project, snapshot)
    db.session.rollback()
    assert User.query.filter_by(email="export-lock-rollback@example.test").count() == 0

    stale_snapshot = review_coverage.snapshot_export_review(project)
    current = db.session.get(
        TranslationTargetBlockActive,
        {"source_plan_id": snapshot["source_plan_id"], "block_id": blocks[0].id},
    )
    assert current is not None
    _activate_target(
        project, blocks[0], expected_version=current.version, suffix=" later"
    )
    with pytest.raises(ScholarlyReviewError, match="review export stale"):
        review_coverage.lock_export_review_snapshot(project, stale_snapshot)


def test_export_snapshot_lock_blocks_independent_target_writer_until_rollback(app):
    project, _record, blocks, _owner = _project_and_plan("CoverageExportRace")
    block = blocks[0]
    _activate_target(project, block)
    db.session.commit()
    project_id = project.id
    snapshot = review_coverage.snapshot_export_review(project)
    active_key = {
        "source_plan_id": snapshot["source_plan_id"],
        "block_id": block.id,
    }
    before = db.session.get(TranslationTargetBlockActive, active_key)
    assert before is not None
    before_revision_id = before.revision_id
    before_version = before.version

    assert review_coverage.lock_export_review_snapshot(project, snapshot)

    def write_target(attempt_started: threading.Event):
        with app.app_context():
            try:
                db.session.connection().exec_driver_sql("PRAGMA busy_timeout=100")
                worker_project = db.session.get(TranslationProject, project_id)
                assert worker_project is not None
                attempt_started.set()
                result = _activate_target(
                    worker_project,
                    block,
                    expected_version=before_version,
                    suffix=" fenced writer",
                )
                return "committed", result
            except BlockWorkspaceIntegrityError as exc:
                db.session.rollback()
                return "failed", exc
            finally:
                db.session.remove()

    with ThreadPoolExecutor(max_workers=1) as executor:
        first_attempt = threading.Event()
        first = executor.submit(write_target, first_attempt)
        assert first_attempt.wait(timeout=10)
        first_status, first_result = first.result(timeout=10)
        assert first_status == "failed"
        assert isinstance(first_result, BlockWorkspaceIntegrityError)
        cause = first_result
        saw_sqlite_lock = False
        while cause is not None:
            if "database is locked" in str(cause).lower():
                saw_sqlite_lock = True
                break
            cause = cause.__cause__ or cause.__context__
        assert saw_sqlite_lock

        db.session.expire_all()
        unchanged = db.session.get(TranslationTargetBlockActive, active_key)
        assert unchanged is not None
        assert unchanged.revision_id == before_revision_id
        assert unchanged.version == before_version

        db.session.rollback()

        retry_attempt = threading.Event()
        retry = executor.submit(write_target, retry_attempt)
        assert retry_attempt.wait(timeout=10)
        retry_status, retry_result = retry.result(timeout=10)
        assert retry_status == "committed"
        assert retry_result["target"]["active_version"] == before_version + 1

    db.session.expire_all()
    updated = db.session.get(TranslationTargetBlockActive, active_key)
    assert updated is not None
    assert updated.revision_id != before_revision_id
    assert updated.version == before_version + 1


def test_anchor_scope_union_completes_kind_and_snapshot_detects_edits(app):
    project, _record, _blocks, owner = _project_and_plan("AnchorCoverage")
    workspace = load_block_source_plan(project)
    block = next(item for item in workspace.plan.blocks if len(item.anchors) >= 2)
    anchor_ids = [anchor.id for anchor in block.anchors]
    assert len(anchor_ids) >= 2
    _activate_target(project, block)
    actor = ReviewActor("owner", owner.id)

    first = review_coverage.record_review_coverage(
        project,
        entries=[_entry(block, 1, scope="anchors", anchors=anchor_ids[:1])],
        kind="structural",
        actor=actor,
        idempotency_key="anchor-half-one",
    )
    partial = review_coverage.review_coverage_manifest(project, block_ids=[block.id])
    assert partial["blocks"][0]["kinds"]["structural"]["complete"] is False
    assert (
        partial["blocks"][0]["kinds"]["structural"]["reviewed_source_anchor_ids"]
        == anchor_ids[:1]
    )
    assert partial["summary"]["structural"]["partial"] == 1

    second = review_coverage.record_review_coverage(
        project,
        entries=[_entry(block, 1, scope="anchors", anchors=anchor_ids[1:])],
        kind="structural",
        reviewer_kind="human",
        actor=actor,
        idempotency_key="anchor-half-two",
    )
    complete = review_coverage.review_coverage_manifest(project, block_ids=[block.id])
    block_coverage = complete["blocks"][0]["kinds"]["structural"]
    assert block_coverage["complete"] is True
    assert block_coverage["coverage_ids"] == [
        first["coverage_id"],
        second["coverage_id"],
    ]
    assert block_coverage["reviewed_source_anchor_ids"] == anchor_ids
    assert block_coverage["reviewed_source_anchor_count"] == len(anchor_ids)
    assert complete["summary"]["structural"]["complete"] == 1
    assert TranslationBlockReviewHead.query.count() == 0
    with pytest.raises(ScholarlyReviewError, match="review coverage required"):
        review_coverage.snapshot_export_review(project, "target_reading")

    for other_block in workspace.plan.blocks:
        if other_block.id == block.id:
            continue
        _activate_target(project, other_block)
        review_coverage.record_review_coverage(
            project,
            entries=[_entry(other_block, 1)],
            kind="structural",
            actor=actor,
            idempotency_key=f"cover-whole-{other_block.id}",
        )
    expected_coverage_count = 2 + len(workspace.plan.blocks) - 1

    snapshot = review_coverage.snapshot_export_review(project, "structural")
    assert review_coverage.validate_export_review_snapshot(project, snapshot)
    _activate_target(project, block, expected_version=1, suffix=" revised")
    with pytest.raises(ScholarlyReviewError, match="review coverage export stale"):
        review_coverage.validate_export_review_snapshot(project, snapshot)
    stale = review_coverage.review_coverage_manifest(project, block_ids=[block.id])
    assert stale["blocks"][0]["coverage"] == []
    assert stale["summary"]["structural"]["missing"] == 1
    assert TranslationReviewCoverage.query.count() == expected_coverage_count


def test_coverage_validation_cas_duplicates_and_owner_fence_are_atomic(app):
    project, _record, blocks, owner = _project_and_plan("CoverageOwner")
    paragraphs = [item for item in blocks if item.anchors]
    assert len(paragraphs) >= 2
    for block in paragraphs[:2]:
        _activate_target(project, block)
    actor = ReviewActor("owner", owner.id)
    good = _entry(paragraphs[0], 1)

    with pytest.raises(ScholarlyReviewError, match="review coverage target version"):
        review_coverage.record_review_coverage(
            project,
            entries=[good, _entry(paragraphs[1], 99)],
            kind="bilingual",
            actor=actor,
            idempotency_key="atomic-version-failure",
        )
    assert TranslationReviewCoverage.query.count() == 0

    with pytest.raises(ScholarlyReviewError, match="review coverage block"):
        review_coverage.record_review_coverage(
            project,
            entries=[good, good],
            kind="structural",
            actor=actor,
            idempotency_key="duplicate-block",
        )
    with pytest.raises(ScholarlyReviewError, match="review coverage source anchors"):
        review_coverage.record_review_coverage(
            project,
            entries=[
                _entry(
                    paragraphs[0],
                    1,
                    scope="anchors",
                    anchors=["anchor-from-another-block"],
                )
            ],
            kind="structural",
            actor=actor,
            idempotency_key="invalid-anchor",
        )
    assert TranslationReviewCoverage.query.count() == 0

    second_project, _other_record, _other_blocks, second_owner = _project_and_plan(
        "DifferentOwner"
    )
    with pytest.raises(ScholarlyReviewError, match="review mutation forbidden"):
        review_coverage.record_review_coverage(
            project,
            entries=[good],
            kind="structural",
            actor=ReviewActor("owner", second_owner.id),
            idempotency_key="foreign-owner",
        )
    other_manifest = review_coverage.review_coverage_manifest(second_project)
    assert all(not block["coverage"] for block in other_manifest["blocks"])


def test_coverage_manifest_fails_closed_on_tampered_receipt(app):
    project, _record, blocks, owner = _project_and_plan("TamperedCoverage")
    block = next(item for item in blocks if item.anchors)
    _activate_target(project, block)
    recorded = review_coverage.record_review_coverage(
        project,
        entries=[_entry(block, 1)],
        kind="bilingual",
        actor=ReviewActor("owner", owner.id),
        idempotency_key="tampered-coverage",
    )
    db.session.commit()
    row = db.session.get(TranslationReviewCoverage, recorded["coverage_id"])
    assert row is not None
    row.payload_json = {**row.payload_json, "comment": "tampered"}
    with pytest.raises(ScholarlyReviewError, match="review coverage integrity"):
        review_coverage.review_coverage_manifest(project)


def test_migration_028_is_additive_and_refuses_coverage_loss(tmp_path: Path) -> None:
    database_path = tmp_path / "review-coverage-migration.db"
    config = _migration_config(database_path)
    application = _migrate_phase5_database(
        database_path,
        tmp_path / "review-coverage-migration-instance",
        revision="027_shared_glossaries",
    )
    with application.app_context():
        command.upgrade(config, "028_review_coverage")
        assert "translation_review_coverage" in set(
            sqlalchemy_inspect(db.engine).get_table_names()
        )
        command.downgrade(config, "027_shared_glossaries")
        assert "translation_review_coverage" not in set(
            sqlalchemy_inspect(db.engine).get_table_names()
        )

        command.upgrade(config, "028_review_coverage")
        with db.engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.execute(
                text(
                    "INSERT INTO translation_review_coverage "
                    "(id, project_id, source_plan_id, owner_user_id, kind, "
                    "reviewer_kind, actor_type, payload_json, record_hash, "
                    "integrity_receipt, created_at) VALUES "
                    "('coverage-row', 'project', 'plan', 1, 'structural', "
                    "'agent', 'owner', 'encrypted', :record_hash, :receipt, "
                    "CURRENT_TIMESTAMP)"
                ),
                {"record_hash": "r" * 64, "receipt": "i" * 64},
            )
        with pytest.raises(RuntimeError, match="cannot downgrade"):
            command.downgrade(config, "027_shared_glossaries")
        assert (
            db.session.execute(
                text("SELECT version_num FROM alembic_version_translator")
            ).scalar_one()
            == "028_review_coverage"
        )
        db.session.remove()
        db.engine.dispose()
