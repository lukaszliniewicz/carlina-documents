"""Focused activation tests for target/review projection persistence."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from content_storage_support import bind_content_policy
from flask import Flask

import iarf_assistant.addons.translator.scholarly_review as review_service
from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION
from iarf_assistant.addons.translator import api_client
from iarf_assistant.addons.translator.alignment_persistence import (
    persist_alignment_revision,
    persist_target_span_set,
)
from iarf_assistant.addons.translator.block_contract import (
    AttestedDocumentSnapshot,
    BlockContractError,
)
from iarf_assistant.addons.translator.block_persistence import (
    persist_source_plan,
    source_attestation_for_project,
)
from iarf_assistant.addons.translator.block_planner import plan_document_blocks
from iarf_assistant.addons.translator.models import (
    TranslationProject,
    TranslationReaderNoteRevision,
    TranslationTargetAlignmentActive,
    TranslationTargetBlockActive,
    TranslationTargetBlockNote,
    TranslationTargetBlockRevision,
)
from iarf_assistant.addons.translator.scholarly_review import (
    ReviewActor,
    ScholarlyReviewError,
    create_reader_note,
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
)
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.extensions import db
from iarf_assistant.models import User


@pytest.fixture
def app(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'target-persistence.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="target-persistence-test",
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
        with db.engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            db.metadata.drop_all(connection)
        db.engine.dispose()
        context.pop()


def _project(email: str = "target-persistence@example.test") -> TranslationProject:
    owner = User(email=email, password_hash="x")
    db.session.add(owner)
    db.session.flush()
    project = TranslationProject(
        user_id=owner.id,
        name="Target persistence",
        source_lang="en",
        target_lang="pl",
        engine_contract="document-block-v1",
        document_id="doc-target-persistence",
        source_content_hash=hashlib.sha256(b"Dao is a term.").hexdigest(),
        settings={
            "source_reference": {
                "version": 1,
                "capability": "document.processing",
                "provider_addon_id": "doc-provider",
                "provider_capability_id": "document-processing",
                "resource_type": "document",
                "resource_id": "doc-target-persistence",
            }
        },
    )
    db.session.add(project)
    db.session.flush()
    return project


def _plan_for(
    project: TranslationProject,
    source_html: str = "<html><body><p>Dao is a term.</p></body></html>",
):
    digest = hashlib.sha256(source_html.encode()).hexdigest()
    project.source_content_hash = digest
    snapshot = AttestedDocumentSnapshot(
        id="doc-target-persistence",
        document_id="doc-target-persistence",
        output_html=source_html,
        content_hash=digest,
        semantic_content_hash=digest,
        metadata={},
        provenance={},
        captured_at="2026-09-05T12:00:00+00:00",
        semantic_contract_version=SEMANTIC_HTML_CONTRACT_VERSION,
    )
    plan = plan_document_blocks(
        snapshot,
        project_id=project.id,
        source_attestation=source_attestation_for_project(project),
        source_language="en",
    )
    record, _created = persist_source_plan(project, plan)
    db.session.flush()
    return plan, record


def _candidate(plan, record, block, text: str) -> TargetBlockCandidate:
    return TargetBlockCandidate(
        source_plan_id=record.id,
        source_plan_hash=plan.plan_hash,
        block_id=block.id,
        structural_fingerprint=structural_fingerprint(plan, block),
        parts=(TargetTextPart(kind="text", text=text),),
        lexical_attributes=(),
    )


def _alignment(record, revision: TranslationTargetBlockRevision):
    span_set = persist_target_span_set(record, revision)
    return persist_alignment_revision(record, span_set)


@pytest.mark.parametrize("bulk", [False, True])
def test_activation_materializes_legacy_model_note_once_and_reuses_context(
    app, monkeypatch, bulk
):
    project = _project()
    plan, record = _plan_for(project)
    block = plan.blocks[0]
    revision = persist_target_revision(
        record,
        _candidate(plan, record, block, "Dao is a term."),
        target_language="pl",
        author_type="model",
        notes=[{"anchor": "Dao", "body": "A model explanation."}],
    )
    alignment = _alignment(record, revision)
    monkeypatch.setattr(
        review_service,
        "_workspace",
        lambda _project: pytest.fail("activation unexpectedly reloaded workspace"),
    )

    if bulk:
        pointer, = activate_initial_target_revisions(
            record, [(revision, alignment)], verified_plan=plan,
        )
    else:
        pointer = activate_target_revision(
            record,
            block_id=block.id,
            revision_id=revision.id,
            alignment_revision_id=alignment.id,
            expected_version=0,
        )
    reader_revisions = TranslationReaderNoteRevision.query.all()
    assert len(reader_revisions) == 1
    assert reader_revisions[0].state == "active"
    assert reader_revisions[0].target_revision_id == revision.id
    assert TranslationTargetBlockNote.query.count() == 1

    first_pointer_version = pointer.version
    replay = activate_target_revision(
        record,
        block_id=block.id,
        revision_id=revision.id,
        alignment_revision_id=alignment.id,
        expected_version=first_pointer_version,
    )
    assert replay.version == first_pointer_version + 1
    assert TranslationReaderNoteRevision.query.count() == 1


def test_finished_project_mcp_read_edit_readback_keeps_note_identity_and_full_body(app):
    project = _project()
    plan, record = _plan_for(project)
    block = plan.blocks[0]
    revision = persist_target_revision(
        record,
        _candidate(plan, record, block, "Dao is a term."),
        target_language="pl",
        author_type="model",
        notes=[{"anchor": "Dao", "body": "A model explanation."}],
    )
    alignment = _alignment(record, revision)
    activate_target_revision(
        record,
        block_id=block.id,
        revision_id=revision.id,
        alignment_revision_id=alignment.id,
        expected_version=0,
    )
    project.status = "completed"
    db.session.commit()
    client = api_client.TranslatorClient(None, {"current_user_id": project.user_id})

    def fetch():
        return client.review_fetch(project.id, block_ids=[block.id])["items"][0]

    initial = fetch()
    note = initial["reader_notes"][0]
    legacy = TranslationTargetBlockNote.query.one()
    legacy_receipt = legacy.record_hash
    assert note["id"] != legacy.id
    assert note["version"] == 1
    assert note["anchor"] == {"kind": "target", "text": "Dao", "occurrence_index": 0}
    assert note["citation_ids"] == []  # Internal legacy identity stays private.
    assert not {"record_hash", "revision_id", "author_user_id"}.intersection(note)

    long_body = "Context. " * 140 + "**Endnote**"
    revised = client.reader_note_revise(
        project.id, note["id"], long_body, note["version"], "revise-1"
    )
    current = fetch()["reader_notes"][0]
    assert current["id"] == note["id"]
    assert current["version"] == revised["version"] == 2
    assert current["body"] == long_body
    assert current["body_html"].endswith("<strong>Endnote</strong>")
    matches = client.review_search(project.id, "Endnote", fields=["annotations"])
    assert matches["items"][0]["object_id"] == f"note:{note['id']}"
    with pytest.raises(ScholarlyReviewError) as stale:
        client.reader_note_revise(
            project.id, note["id"], "Stale overwrite", 1, "stale-1"
        )
    assert stale.value.code == "note-version-conflict"
    assert fetch()["reader_notes"][0]["body"] == long_body

    retired = client.reader_note_set_state(
        project.id, note["id"], "retired", 2, "retire-1"
    )
    assert retired["version"] == 3
    assert fetch()["reader_notes"] == []
    assert (
        client.review_search(project.id, "model explanation", fields=["annotations"])[
            "items"
        ]
        == []
    )
    restored = client.reader_note_set_state(
        project.id, note["id"], "active", 3, "restore-1"
    )
    assert fetch()["reader_notes"][0]["version"] == restored["version"] == 4

    target = fetch()["target"]
    client.target_update(
        project.id,
        block.id,
        {**target["editor"], "content": "Dao is a precise term."},
        target["active_version"],
        "target-1",
    )
    changed = fetch()
    assert changed["target"]["active_version"] == 2
    assert changed["target"]["editor"]["content"] == "Dao is a precise term."
    assert changed["reader_notes"][0]["body"] == long_body
    assert legacy.record_hash == legacy_receipt
    assert legacy.body == "A model explanation."

    for stale_anchor in (
        {"target_version": target["active_version"]},
        {"target_revision_id": revision.id},
    ):
        with pytest.raises(ScholarlyReviewError) as stale_attachment:
            client.reader_note_revise(
                project.id,
                note["id"],
                "Stale attachment",
                changed["reader_notes"][0]["version"],
                f"stale-anchor-{next(iter(stale_anchor))}",
                anchor={
                    "kind": "target",
                    "anchor_text": "Dao",
                    "occurrence_index": 0,
                    **stale_anchor,
                },
            )
        assert stale_attachment.value.code == "note-target-fence"
    assert fetch()["reader_notes"][0]["body"] == long_body

    owner_note = client.reader_note_create(
        project.id, block.id, "An owner annotation.", {"kind": "block"}, "create-1"
    )
    assert owner_note["id"] in {row["id"] for row in fetch()["reader_notes"]}
    assert project.status == "completed"


def test_activation_revalidates_target_anchor_against_new_revision(app):
    project = _project()
    plan, record = _plan_for(project)
    block = plan.blocks[0]
    first = persist_target_revision(
        record,
        _candidate(plan, record, block, "Dao is a term."),
        target_language="pl",
        author_type="model",
    )
    first_alignment = _alignment(record, first)
    pointer = activate_target_revision(
        record,
        block_id=block.id,
        revision_id=first.id,
        alignment_revision_id=first_alignment.id,
        expected_version=0,
    )
    note = create_reader_note(
        project,
        block_id=block.id,
        body="Keep reviewing this target term.",
        actor=ReviewActor("owner", project.user_id),
        anchor={"kind": "target", "anchor_text": "Dao", "occurrence_index": 0},
    )
    second = persist_target_revision(
        record,
        _candidate(plan, record, block, "A changed translation."),
        target_language="pl",
        author_type="model",
        parent_revision_id=first.id,
    )
    second_alignment = _alignment(record, second)
    activate_target_revision(
        record,
        block_id=block.id,
        revision_id=second.id,
        alignment_revision_id=second_alignment.id,
        expected_version=pointer.version,
    )

    revisions = TranslationReaderNoteRevision.query.filter_by(note_id=note["id"]).all()
    assert len(revisions) == 2
    assert revisions[-1].target_revision_id == second.id
    assert revisions[-1].state == "needs_review"


def test_reader_note_maintenance_failure_rolls_back_both_active_pointers(
    app, monkeypatch
):
    project = _project()
    plan, record = _plan_for(project)
    block = plan.blocks[0]
    first = persist_target_revision(
        record,
        _candidate(plan, record, block, "First translation."),
        target_language="pl",
        author_type="model",
    )
    first_alignment = _alignment(record, first)
    pointer = activate_target_revision(
        record,
        block_id=block.id,
        revision_id=first.id,
        alignment_revision_id=first_alignment.id,
        expected_version=0,
    )
    second = persist_target_revision(
        record,
        _candidate(plan, record, block, "Second translation."),
        target_language="pl",
        author_type="model",
        parent_revision_id=first.id,
    )
    second_alignment = _alignment(record, second)

    def fail(*_args, **_kwargs):
        raise ScholarlyReviewError("injected-maintenance-failure")

    monkeypatch.setattr(review_service, "materialize_model_reader_notes", fail)
    with pytest.raises(BlockContractError, match="target-reader-note-maintenance"):
        activate_target_revision(
            record,
            block_id=block.id,
            revision_id=second.id,
            alignment_revision_id=second_alignment.id,
            expected_version=pointer.version,
        )

    db.session.expire_all()
    active = db.session.get(
        TranslationTargetBlockActive,
        {"source_plan_id": record.id, "block_id": block.id},
    )
    active_alignment = db.session.get(
        TranslationTargetAlignmentActive,
        {"source_plan_id": record.id, "block_id": block.id},
    )
    assert active is not None and active.revision_id == first.id
    assert active.version == pointer.version
    assert active_alignment is not None and active_alignment.revision_id == first.id
    assert active_alignment.alignment_revision_id == first_alignment.id


def test_scholarly_review_failure_does_not_leak_its_content(app, monkeypatch):
    project = _project()
    plan, record = _plan_for(project)
    block = plan.blocks[0]
    revision = persist_target_revision(
        record,
        _candidate(plan, record, block, "Translation."),
        target_language="pl",
        author_type="model",
    )
    alignment = _alignment(record, revision)

    def fail(*_args, **_kwargs):
        raise ScholarlyReviewError("contains-private-content", "private text")

    monkeypatch.setattr(review_service, "materialize_model_reader_notes", fail)
    with pytest.raises(BlockContractError) as error:
        activate_target_revision(
            record,
            block_id=block.id,
            revision_id=revision.id,
            alignment_revision_id=alignment.id,
            expected_version=0,
        )
    assert error.value.code == "target-reader-note-maintenance"
    assert "private" not in str(error.value)


def test_activation_rejects_a_verified_plan_pinned_to_another_project(app):
    project = _project()
    plan, record = _plan_for(project)
    block = plan.blocks[0]
    revision = persist_target_revision(
        record,
        _candidate(plan, record, block, "Translation."),
        target_language="pl",
        author_type="model",
    )
    alignment = _alignment(record, revision)
    wrong_project = _project("wrong-target-persistence@example.test")
    wrong_plan, _wrong_record = _plan_for(wrong_project)

    with pytest.raises(BlockContractError, match="target-source-plan-pin"):
        activate_target_revision(
            record,
            block_id=block.id,
            revision_id=revision.id,
            alignment_revision_id=alignment.id,
            expected_version=0,
            verified_plan=wrong_plan,
        )


@pytest.mark.parametrize("annotation_profile", ["restrained", "study"])
def test_bulk_activation_materializes_notes_for_every_block(app, annotation_profile):
    project = _project()
    plan, record = _plan_for(
        project, "<html><body><p>Dao is a term.</p><p>Dao has another sense.</p></body></html>",
    )
    pairs = []
    for block in plan.blocks:
        revision = persist_target_revision(
            record, _candidate(plan, record, block, "Dao is a term."),
            target_language="pl", author_type="model", verified_plan=plan,
            notes=[{"anchor": "Dao", "body": "A model explanation."}],
            annotation_profile=annotation_profile,
        )
        pairs.append((revision, _alignment(record, revision)))
    activate_initial_target_revisions(record, pairs, verified_plan=plan)
    db.session.commit()
    notes = TranslationReaderNoteRevision.query.all()
    assert len(notes) == len(plan.blocks)
    assert {note.target_revision_id for note in notes} == {pair[0].id for pair in pairs}
    assert all(note.state == "active" for note in notes)


@pytest.mark.parametrize("phase", ["materialize_model_reader_notes", "revalidate_reader_notes_for_target"])
def test_bulk_note_failure_rolls_back_entire_window(app, monkeypatch, phase):
    project = _project()
    plan, record = _plan_for(
        project, "<html><body><p>Dao is a term.</p><p>Dao has another sense.</p></body></html>",
    )
    pairs = []
    for block in plan.blocks:
        revision = persist_target_revision(
            record, _candidate(plan, record, block, "Dao is a term."),
            target_language="pl", author_type="model", verified_plan=plan,
            notes=[{"anchor": "Dao", "body": "A model explanation."}],
        )
        pairs.append((revision, _alignment(record, revision)))
    db.session.commit()
    original = getattr(review_service, phase)
    calls = []

    def fail_second(*args, **kwargs):
        calls.append(phase)
        if len(calls) == 2:
            raise ScholarlyReviewError("injected-maintenance-failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(review_service, phase, fail_second)
    with pytest.raises(BlockContractError, match="target-reader-note-maintenance"):
        activate_initial_target_revisions(record, pairs, verified_plan=plan)
    # Even a caller commit cannot publish the first pair or its note after the
    # second block's failure. The whole activation savepoint must roll back.
    db.session.commit()
    assert len(calls) == 2
    assert TranslationTargetBlockActive.query.count() == 0
    assert TranslationTargetAlignmentActive.query.count() == 0
    assert TranslationReaderNoteRevision.query.count() == 0
    assert TranslationTargetBlockRevision.query.count() == 2


def test_bulk_initial_activation_conflict_does_not_activate_remaining_blocks(app):
    project = _project()
    plan, record = _plan_for(
        project, "<html><body><p>Dao is a term.</p><p>Dao has another sense.</p></body></html>",
    )
    pairs = []
    for block in plan.blocks:
        revision = persist_target_revision(
            record, _candidate(plan, record, block, "Dao is a term."),
            target_language="pl", author_type="model", verified_plan=plan,
        )
        pairs.append((revision, _alignment(record, revision)))
    activate_initial_target_revisions(record, pairs[:1], verified_plan=plan)
    db.session.commit()
    with pytest.raises(BlockContractError, match="target-active-conflict"):
        activate_initial_target_revisions(record, pairs, verified_plan=plan)
    db.session.commit()
    assert TranslationTargetBlockActive.query.count() == 1
    assert TranslationTargetAlignmentActive.query.count() == 1
    assert TranslationTargetBlockActive.query.one().block_id == plan.blocks[0].id
