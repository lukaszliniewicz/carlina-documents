from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from content_storage_support import bind_content_policy
from flask import Flask
from sqlalchemy import text

from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION

# Keep the old package-name references consolidated for the exact rename ratchet.
# isort: off
from iarf_assistant.addons.translator import (
    block_contract as translator_block_contract,
    block_persistence as translator_block_persistence,
    block_planner as translator_block_planner,
    models as translator_models,
)
# isort: on

AttestedDocumentSnapshot = translator_block_contract.AttestedDocumentSnapshot
BlockContractError = translator_block_contract.BlockContractError
DomainTranslationSourcePlan = translator_block_contract.TranslationSourcePlan
effective_engine_contract = translator_block_persistence.effective_engine_contract
persist_source_plan = translator_block_persistence.persist_source_plan
plan_and_persist_source_snapshot = (
    translator_block_persistence.plan_and_persist_source_snapshot
)
rehydrate_source_plan = translator_block_persistence.rehydrate_source_plan
source_attestation_for_project = (
    translator_block_persistence.source_attestation_for_project
)
plan_document_blocks = translator_block_planner.plan_document_blocks
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.extensions import db
from iarf_assistant.models import User

TranslationProject = translator_models.TranslationProject
TranslationSourcePlan = translator_models.TranslationSourcePlan
TranslationBlock = translator_models.TranslationBlock
TranslationSourceAnchor = translator_models.TranslationSourceAnchor


@pytest.fixture
def app(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'block-persistence.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="block-persistence-test",
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


@pytest.fixture(autouse=True)
def isolated_source_identity_cache(monkeypatch):
    cache_type = type(translator_block_persistence._SOURCE_IDENTITY_CACHE)
    monkeypatch.setattr(
        translator_block_persistence,
        "_SOURCE_IDENTITY_CACHE",
        cache_type(),
    )


def _project(name: str = "Blocks") -> TranslationProject:
    owner = User(email=f"{name.lower()}@example.test", password_hash="test")
    db.session.add(owner)
    db.session.flush()
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
    db.session.commit()
    return project


def _snapshot(html: str, *, captured: str = "2026-08-25T12:00:00Z") -> AttestedDocumentSnapshot:
    digest = hashlib.sha256(html.encode()).hexdigest()
    return AttestedDocumentSnapshot(
        id="doc-1",
        document_id="doc-1",
        output_html=html,
        content_hash=digest,
        semantic_content_hash=digest,
        metadata={"private_sentinel": "PRIVATE-METADATA"},
        provenance={
            "knowledge_source_id": "source-1",
            "private_sentinel": "PRIVATE-PROVENANCE-IGNORED",
        },
        filename="private-book.html",
        captured_at=captured,
        source_updated_at="2026-08-24T12:00:00Z",
        semantic_contract_version=SEMANTIC_HTML_CONTRACT_VERSION,
    )


def _plan(project: TranslationProject, *, html: str | None = None, captured: str = "2026-08-25T12:00:00Z"):
    snapshot = _snapshot(html or '<html><body><p>Hello <em>world</em>.</p></body></html>', captured=captured)
    return plan_document_blocks(
        snapshot,
        project_id=project.id,
        source_attestation=source_attestation_for_project(project),
        source_language="en",
    )


def test_engine_contract_is_explicit_and_bounded() -> None:
    with pytest.raises(BlockContractError, match="engine-contract-missing"):
        effective_engine_contract(None)
    assert effective_engine_contract("segment-v1") == "segment-v1"
    assert effective_engine_contract("document-block-v1") == "document-block-v1"
    for value in ("", "unknown", 3, object()):
        with pytest.raises(BlockContractError):
            effective_engine_contract(value)


def test_persist_decrypts_round_trip_without_commit(app):
    project = _project()
    plan = _plan(project)
    record, created = persist_source_plan(project, plan)
    assert created is True
    assert record.id
    assert project.document_id == "doc-1"
    assert project.source_content_hash == plan.snapshot.content_hash
    assert project.source_captured_at is not None
    restored = rehydrate_source_plan(record)
    assert restored._hash_payload() == plan._hash_payload()
    assert restored.snapshot.output_html == plan.snapshot.output_html
    assert restored.snapshot.provenance == {"knowledge_source_id": "source-1"}
    assert len(record.blocks) == len(plan.blocks)
    assert len(record.anchors) == sum(len(block.anchors) for block in plan.blocks)
    assert db.session.query(TranslationSourcePlan).count() == 1
    # The adapter leaves the outer transaction in the caller's hands.
    db.session.rollback()
    assert db.session.query(TranslationSourcePlan).count() == 0
    assert project.document_id is None
    assert project.source_snapshot is None


def test_rehydrate_reuses_successful_source_identity_validation(app, monkeypatch):
    project = _project()
    record, _created = persist_source_plan(project, _plan(project))
    with translator_block_persistence._SOURCE_IDENTITY_CACHE_LOCK:
        translator_block_persistence._SOURCE_IDENTITY_CACHE.clear()
    calls = 0
    original = translator_block_persistence.render_source_identity

    def counted(plan):
        nonlocal calls
        calls += 1
        return original(plan)

    monkeypatch.setattr(
        translator_block_persistence, "render_source_identity", counted
    )
    rehydrate_source_plan(record)
    rehydrate_source_plan(record)
    assert calls == 1


def test_source_identity_cache_key_includes_complete_plan_dto(app, monkeypatch):
    project = _project()
    first = _plan(project)
    changed_snapshot = AttestedDocumentSnapshot(
        **{
            **first.snapshot.to_dict(),
            "metadata": {"changed": "metadata-only"},
        }
    )
    second = plan_document_blocks(
        changed_snapshot,
        project_id=project.id,
        source_attestation=source_attestation_for_project(project),
        source_language="en",
    )
    assert second.plan_hash == first.plan_hash
    calls = 0
    original = translator_block_persistence.render_source_identity

    def counted(plan):
        nonlocal calls
        calls += 1
        return original(plan)

    monkeypatch.setattr(
        translator_block_persistence, "render_source_identity", counted
    )
    translator_block_persistence._verify_source_identity(first)
    translator_block_persistence._verify_source_identity(second)
    translator_block_persistence._verify_source_identity(first)
    assert calls == 2


def test_source_identity_failures_are_not_cached(app, monkeypatch):
    project = _project()
    plan = _plan(project)
    calls = 0

    def fail(_plan):
        nonlocal calls
        calls += 1
        raise BlockContractError("identity-test-failure")

    monkeypatch.setattr(
        translator_block_persistence, "render_source_identity", fail
    )
    for _ in range(2):
        with pytest.raises(BlockContractError, match="identity-test-failure"):
            translator_block_persistence._verify_source_identity(plan)
    assert calls == 2
    assert not translator_block_persistence._SOURCE_IDENTITY_CACHE


def test_source_identity_cache_is_bounded_and_evicts_lru_entries(app):
    project = _project()
    base_snapshot = _snapshot("<html><body><p>Cache.</p></body></html>")
    plans = []
    cache_limit = translator_block_persistence._SOURCE_IDENTITY_CACHE_MAX_SIZE
    for index in range(cache_limit):
        snapshot = AttestedDocumentSnapshot(
            **{
                **base_snapshot.to_dict(),
                "metadata": {"cache-entry": index},
            }
        )
        plan = plan_document_blocks(
            snapshot,
            project_id=project.id,
            source_attestation=source_attestation_for_project(project),
            source_language="en",
        )
        plans.append(plan)
        translator_block_persistence._verify_source_identity(plan)

    def cache_key(plan):
        return translator_block_persistence.sha256_text(
            translator_block_persistence.canonical_json(plan.to_dict())
        )

    assert len(translator_block_persistence._SOURCE_IDENTITY_CACHE) == cache_limit
    translator_block_persistence._verify_source_identity(plans[0])
    new_snapshot = AttestedDocumentSnapshot(
        **{
            **base_snapshot.to_dict(),
            "metadata": {"cache-entry": cache_limit},
        }
    )
    new_plan = plan_document_blocks(
        new_snapshot,
        project_id=project.id,
        source_attestation=source_attestation_for_project(project),
        source_language="en",
    )
    translator_block_persistence._verify_source_identity(new_plan)
    cache_keys = list(translator_block_persistence._SOURCE_IDENTITY_CACHE)
    assert len(cache_keys) == cache_limit
    assert cache_key(plans[0]) == cache_keys[-2]
    assert cache_key(plans[1]) not in cache_keys
    assert cache_key(new_plan) == cache_keys[-1]


def test_raw_source_fields_are_encrypted_and_structural_fields_are_plain(app):
    project = _project()
    plan = _plan(project)
    record, _created = persist_source_plan(project, plan)
    db.session.flush()
    raw = db.session.execute(
        text(
            "SELECT p.source_snapshot, r.source_filename, r.source_metadata, "
            "r.source_attestation, r.planner_settings, r.source_provenance, "
            "b.source_fragment, b.source_projection, b.source_slots, "
            "b.protected_inline, b.locator, b.classification_summary, "
            "a.source_text FROM translation_project p "
            "JOIN translation_source_plan r ON r.project_id = p.id "
            "JOIN translation_block b ON b.source_plan_id = r.id "
            "JOIN translation_source_anchor a ON a.source_plan_id = r.id"
        )
    ).one()
    for value in raw[:5] + raw[6:10] + (raw[12],):
        assert str(value).startswith("enc:v2:")
        assert "PRIVATE-METADATA" not in str(value)
        assert "Hello" not in str(value)
    assert "PRIVATE-PROVENANCE-IGNORED" not in str(raw[5])
    assert json.loads(raw[10]) == {
        "dom": plan.blocks[0].locator,
        "source_start": plan.blocks[0].source_start,
        "source_end": plan.blocks[0].source_end,
    }
    classification = json.loads(raw[11])
    assert "Hello" not in str(classification)
    assert record.source_attestation == plan.source_attestation.to_dict()
    assert record.planner_settings == plan.to_dict()["planner_settings"]
    assert record.source_provenance == {"knowledge_source_id": "source-1"}


def test_same_plan_retry_allows_volatile_snapshot_changes(app):
    project = _project()
    first = _plan(project)
    record, created = persist_source_plan(project, first)
    assert created
    second_snapshot = _snapshot(
        first.snapshot.output_html, captured="2026-08-25T18:00:00Z"
    )
    second_snapshot = AttestedDocumentSnapshot(
        **{
            **second_snapshot.to_dict(),
            "metadata": {"changed": "PRIVATE-METADATA-2"},
        }
    )
    second = plan_document_blocks(
        second_snapshot,
        project_id=project.id,
        source_attestation=source_attestation_for_project(project),
        source_language="en",
    )
    retried, created = persist_source_plan(project, second)
    assert created is False
    assert retried.id == record.id
    assert db.session.query(TranslationSourcePlan).count() == 1
    assert db.session.query(TranslationBlock).count() == len(first.blocks)


def test_projects_are_bound_into_plan_and_block_ids(app):
    first_project = _project("First")
    second_project = _project("Second")
    first, _ = persist_source_plan(first_project, _plan(first_project))
    second, _ = persist_source_plan(second_project, _plan(second_project))
    assert first.id != second.id
    assert first.blocks[0].id != second.blocks[0].id


def test_source_attestation_reference_is_strict_and_owner_bound(app):
    project = _project()
    assert source_attestation_for_project(project).to_dict() == {
        "version": 1,
        "capability": "document.processing",
        "provider_addon_id": "doc-provider",
        "provider_capability_id": "document-processing",
        "resource_type": "document",
        "resource_id": "doc-1",
        "owner_id": str(project.user_id),
    }
    original = dict(project.settings["source_reference"])
    for field, value in (
        ("capability", "other.capability"),
        ("resource_type", "other"),
    ):
        project.settings = {"source_reference": {**original, field: value}}
        with pytest.raises(BlockContractError):
            source_attestation_for_project(project)
    project.settings = {"source_reference": {**original, "unexpected": "x"}}
    with pytest.raises(BlockContractError):
        source_attestation_for_project(project)
    project.settings = {}
    with pytest.raises(BlockContractError):
        source_attestation_for_project(project)


def test_persist_rejects_missing_source_reference_before_rows(app):
    project = _project()
    plan = _plan(project)
    project.settings = {}
    with pytest.raises(BlockContractError):
        persist_source_plan(project, plan)
    db.session.rollback()
    assert db.session.query(TranslationSourcePlan).count() == 0


def test_persist_rejects_changed_provider_pin_before_any_rows(app):
    for index, (field, value) in enumerate(
        (
        ("provider_addon_id", "changed-provider"),
        ("provider_capability_id", "changed-capability"),
        ("resource_id", "other-document"),
        )
    ):
        project = _project(f"Changed-{index}")
        plan = _plan(project)
        reference = dict(project.settings["source_reference"])
        project.settings = {"source_reference": {**reference, field: value}}
        with pytest.raises(BlockContractError):
            persist_source_plan(project, plan)
        db.session.rollback()
        assert db.session.query(TranslationSourcePlan).count() == 0
        assert project.source_snapshot is None


def test_persist_rejects_changed_owner_pin_before_any_rows(app):
    project = _project()
    plan = _plan(project)
    project.user_id = project.user_id + 1
    with pytest.raises(BlockContractError):
        persist_source_plan(project, plan)
    db.session.rollback()
    assert db.session.query(TranslationSourcePlan).count() == 0


def test_reviewer_dropped_block_rehashed_plan_fails_before_write(app):
    project = _project()
    plan = _plan(
        project,
        html="<html><body><p>First.</p><p>Second.</p></body></html>",
    )
    assert len(plan.blocks) >= 2
    dropped = DomainTranslationSourcePlan.create(
        snapshot=plan.snapshot,
        source_attestation=plan.source_attestation,
        project_id=plan.project_id,
        planner_version=plan.planner_version,
        source_language=plan.source_language,
        language_profile=plan.language_profile,
        planner_settings=plan.planner_settings,
        settings_fingerprint=plan.settings_fingerprint,
        namespace=plan.namespace,
        blocks=plan.blocks[:-1],
        coverage=plan.coverage,
    )
    with pytest.raises(BlockContractError):
        persist_source_plan(project, dropped)
    assert db.session.query(TranslationSourcePlan).count() == 0
    assert project.source_snapshot is None


@pytest.mark.parametrize("engine", [None, "segment-v1", "unknown", ""])
def test_non_block_projects_are_rejected(app, engine):
    project = _project()
    project.engine_contract = engine
    with pytest.raises(BlockContractError):
        persist_source_plan(project, _plan(project))


def test_project_and_plan_pins_are_immutable(app):
    project = _project()
    plan = _plan(project)
    with pytest.raises(BlockContractError):
        persist_source_plan(_project("Other"), plan)
    project.document_id = "another-document"
    with pytest.raises(BlockContractError):
        persist_source_plan(project, plan)


@pytest.mark.parametrize("captured", [None, "2026-08-25T12:00:00", "bad"])
def test_persistence_requires_aware_capture_timestamp(app, captured):
    project = _project()
    snapshot = _snapshot("<html><body><p>Text.</p></body></html>")
    with pytest.raises(BlockContractError):
        snapshot = AttestedDocumentSnapshot(
            **{**snapshot.to_dict(), "captured_at": captured}
        )
        plan = plan_document_blocks(
            snapshot,
            project_id=project.id,
            source_attestation=source_attestation_for_project(project),
            source_language="en",
        )
        persist_source_plan(project, plan)


def test_rehydrate_rejects_tampered_count_payload_fragment_anchor_and_coverage(app):
    project = _project()
    record, _ = persist_source_plan(project, _plan(project))
    record.block_count = 99
    with pytest.raises(BlockContractError):
        rehydrate_source_plan(record)
    record.block_count = 1
    block = record.blocks[0]
    block.source_fragment = "tampered"
    with pytest.raises(BlockContractError):
        rehydrate_source_plan(record)
    block.source_fragment = project.source_snapshot[block.locator["source_start"] : block.locator["source_end"]]
    block.source_slots = {"contract_version": "bad", "slots": [], "wire": []}
    with pytest.raises(BlockContractError):
        rehydrate_source_plan(record)


def test_rehydrate_rejects_tampered_attestation_and_planner_settings(app):
    project = _project()
    record, _ = persist_source_plan(project, _plan(project))
    record.source_attestation = {
        **record.source_attestation,
        "owner_id": "wrong-owner",
    }
    with pytest.raises(BlockContractError):
        rehydrate_source_plan(record)
    record.source_attestation = source_attestation_for_project(project).to_dict()
    record.planner_settings = {"tampered": True}
    with pytest.raises(BlockContractError):
        rehydrate_source_plan(record)


def test_rehydrate_rejects_mutated_project_source_pins(app):
    project = _project()
    record, _ = persist_source_plan(project, _plan(project))
    project.document_id = "other-document"
    with pytest.raises(BlockContractError, match="persisted-project-source-pin"):
        rehydrate_source_plan(record)
    project.document_id = record.source_document_id
    project.source_content_hash = "0" * 64
    with pytest.raises(BlockContractError, match="persisted-project-source-pin"):
        rehydrate_source_plan(record)


def test_warm_identity_cache_does_not_bypass_persisted_boundary_checks(app):
    project = _project()
    record, _created = persist_source_plan(project, _plan(project))
    rehydrate_source_plan(record)
    assert translator_block_persistence._SOURCE_IDENTITY_CACHE

    record.block_count = 99
    with pytest.raises(BlockContractError, match="persisted-plan-count"):
        rehydrate_source_plan(record)
    record.block_count = len(record.blocks)

    original_snapshot = project.source_snapshot
    project.source_snapshot = "<html><body><p>Tampered.</p></body></html>"
    with pytest.raises(BlockContractError, match="persisted-project-source-hash"):
        rehydrate_source_plan(record)
    project.source_snapshot = original_snapshot

    original_owner = project.user_id
    project.user_id += 1
    with pytest.raises(BlockContractError, match="persisted-attestation-pin"):
        rehydrate_source_plan(record)
    project.user_id = original_owner

    original_hash = record.plan_hash
    record.plan_hash = "0" * 64
    with pytest.raises(BlockContractError, match="persisted-plan-hash"):
        rehydrate_source_plan(record)
    record.plan_hash = original_hash


def test_code_only_plan_persists_with_zero_blocks(app):
    project = _project()
    plan = _plan(project, html="<html><body><pre>code only</pre></body></html>")
    assert plan.blocks == ()
    record, created = persist_source_plan(project, plan)
    assert created is True
    assert record.block_count == 0
    assert record.anchor_count == 0
    assert rehydrate_source_plan(record).blocks == ()


def test_child_failure_rolls_back_plan_rows_and_project_pin(app, monkeypatch):
    project = _project()
    plan = _plan(project)
    original_flush = db.session.flush
    calls = 0

    def fail_on_child_flush(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = original_flush(*args, **kwargs)
        if calls >= 2:
            raise RuntimeError("injected-child-failure")
        return result

    monkeypatch.setattr(db.session, "flush", fail_on_child_flush)
    with pytest.raises(RuntimeError, match="injected-child-failure"):
        persist_source_plan(project, plan)
    monkeypatch.setattr(db.session, "flush", original_flush)
    assert db.session.query(TranslationSourcePlan).count() == 0
    assert db.session.query(TranslationBlock).count() == 0
    assert db.session.query(TranslationSourceAnchor).count() == 0
    assert project.document_id is None
    assert project.source_snapshot is None


def test_convenience_api_plans_and_persists_without_provider_call(app):
    project = _project()
    html = "<html><body><p>Convenience.</p></body></html>"
    snapshot = _snapshot(html)
    record, created = plan_and_persist_source_snapshot(
        project, snapshot, source_language="en"
    )
    assert created is True
    assert record.source_document_id == "doc-1"


def test_convenience_api_persists_segmenter_anchors_without_rehydrate_call(app):
    project = _project()
    snapshot = _snapshot(
        "<html><body><p>First.  Second.</p><p title=\"Accessible.\">Third.</p></body></html>"
    )
    calls: list[str] = []

    def segment(projection: str) -> list[str]:
        calls.append(projection)
        return {
            "First.  Second.": ["First.", "Second."],
            "Accessible.": ["Accessible."],
            "Third.": ["Third."],
        }[projection]

    record, created = plan_and_persist_source_snapshot(
        project,
        snapshot,
        source_language="en",
        sentence_segmenter=segment,
    )
    assert created is True
    assert set(calls) == {"First.  Second.", "Third.", "Accessible."}
    assert len(calls) == 3
    expected = {
        block.id: [
            (anchor.start, anchor.end, anchor.splitter_profile, anchor.splitter_version)
            for anchor in block.anchors
        ]
        for block in rehydrate_source_plan(record).blocks
    }
    before_rehydrate = list(calls)
    restored = rehydrate_source_plan(record)
    assert calls == before_rehydrate
    assert {
        block.id: [
            (anchor.start, anchor.end, anchor.splitter_profile, anchor.splitter_version)
            for anchor in block.anchors
        ]
        for block in restored.blocks
    } == expected
    assert all(
        anchor.splitter_profile == "sentence-segmenter"
        and anchor.splitter_version == "text.segmentation-v1"
        for block in restored.blocks
        for anchor in block.anchors
    )
