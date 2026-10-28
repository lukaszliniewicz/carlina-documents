"""Phase-5A internal contracts: no routes, workers, providers, or legacy writes."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Barrier, Thread

import pytest
from content_storage_support import bind_content_policy
from flask import Flask, current_app
from flask_login import LoginManager, login_user, logout_user
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION
from carlina.core.utc import utc_now_db
from iarf_assistant.addons.translator import glossary_persistence as glossary_module
from iarf_assistant.addons.translator.analysis_census import build_semantic_census
from iarf_assistant.addons.translator.analysis_contract import (
    ContextArtifactPayload,
    GlossaryPayload,
    glossary_lookup_digest,
)
from iarf_assistant.addons.translator.analysis_persistence import (
    persist_context_artifact_revision,
    persist_semantic_census,
    rehydrate_context_artifact_revision,
    rehydrate_semantic_census,
)
from iarf_assistant.addons.translator.block_contract import (
    AttestedDocumentSnapshot,
    BlockContractError,
    canonical_json,
)
from iarf_assistant.addons.translator.block_persistence import (
    persist_source_plan,
    source_attestation_for_project,
)
from iarf_assistant.addons.translator.block_planner import plan_document_blocks
from iarf_assistant.addons.translator.context_baselines import (
    context_slots_from_baseline,
    freeze_context_baseline,
    rehydrate_context_baseline,
)
from iarf_assistant.addons.translator.glossary_persistence import (
    _revision_hash,
    activate_glossary_revision,
    approve_glossary_proposal,
    create_glossary_concept,
    find_glossary_revisions_for_term,
    glossary_lock_preview,
    persist_glossary_occurrences,
    persist_glossary_revision,
    record_glossary_impact,
    rehydrate_active_glossary_revision,
    rehydrate_glossary_revision,
    verify_context_impact,
    verify_glossary_active,
    verify_glossary_occurrence,
)
from iarf_assistant.addons.translator.models import (
    TranslationContextArtifact,
    TranslationContextArtifactRevision,
    TranslationContextBaseline,
    TranslationContextImpact,
    TranslationGlossaryActive,
    TranslationGlossaryOccurrence,
    TranslationGlossaryRevision,
    TranslationOperation,
    TranslationProject,
    TranslationSemanticCensus,
    TranslationSourcePlan,
)
from iarf_assistant.addons.translator.operations import (
    TranslationOperationConflict,
    create_operation,
    finish_operation,
)
from iarf_assistant.addons.translator.window_contract import (
    WINDOW_PROMPT_CONTRACT_VERSION,
    ContextSlot,
    render_provider_packet,
)
from iarf_assistant.core.encryption import (
    EncryptionService,
    InstallationKeyStore,
    keyed_lookup_candidates,
)
from iarf_assistant.extensions import db
from iarf_assistant.models import RuntimeApprovalRequest, User

FIXTURE = Path(__file__).parent / "fixtures" / "translator_block" / "v1"


@pytest.fixture
def app(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'phase5.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="phase5-test",
        TESTING=True,
        SQLALCHEMY_ENGINE_OPTIONS={"connect_args": {"timeout": 30}},
    )
    db.init_app(application)
    login_manager = LoginManager()
    login_manager.init_app(application)

    @login_manager.user_loader
    def load_user(user_id: str):
        return db.session.get(User, int(user_id))

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
        db.engine.dispose()
        context.pop()


def _state(suffix: str = ""):
    token = f"phase5{suffix}"
    user = User(email=f"{token}@example.test", password_hash="x")
    db.session.add(user)
    db.session.flush()
    project = TranslationProject(
        user_id=user.id,
        name="Phase 5",
        source_lang="en",
        target_lang="pl",
        engine_contract="document-block-v1",
        settings={
            "source_reference": {
                "version": 1,
                "capability": "document.processing",
                "provider_addon_id": "doc",
                "provider_capability_id": "process",
                "resource_type": "document",
                "resource_id": f"{token}-doc",
            }
        },
    )
    db.session.add(project)
    db.session.flush()
    html = "<html><body><h1 aria-label='Heading'>Title</h1><p>Code <code>one()</code>. Next sentence.</p><blockquote>Quoted text.</blockquote><ol><li>List item.</li></ol><figure><figcaption>Figure caption.</figcaption></figure><aside role='doc-footnote'>Note text.</aside></body></html>"
    digest = hashlib.sha256(html.encode()).hexdigest()
    snapshot = AttestedDocumentSnapshot(
        id=f"{token}-doc",
        document_id=f"{token}-doc",
        output_html=html,
        content_hash=digest,
        semantic_content_hash=digest,
        metadata={},
        provenance={},
        captured_at="2026-08-25T00:00:00+00:00",
        semantic_contract_version=SEMANTIC_HTML_CONTRACT_VERSION,
    )
    plan = plan_document_blocks(
        snapshot,
        project_id=project.id,
        source_attestation=source_attestation_for_project(project),
        source_language="en",
    )
    record, _ = persist_source_plan(project, plan)
    operation = TranslationOperation(
        project_id=project.id,
        owner_user_id=user.id,
        kind="document_block_translate",
        idempotency_key=token,
        request_fingerprint="a" * 64,
        state="running",
        source_content_hash=plan.snapshot.content_hash,
        source_plan_id=record.id,
        source_plan_hash=plan.plan_hash,
        lease_expires_at=utc_now_db() + timedelta(minutes=5),
    )
    db.session.add(operation)
    db.session.commit()
    return project, plan, record, operation


def _writable_source_content() -> str:
    fingerprint = "b" * 64
    return canonical_json(
        {
            "contract_version": "translation-block-writable-source-v1",
            "source_plan": {"id": "plan-1", "plan_hash": "a" * 64},
            "window": {
                "order": 1,
                "block_ids": ["block-1"],
                "block_fingerprints": [fingerprint],
            },
            "blocks": [
                {
                    "block": {"id": "block-1"},
                    "structural_fingerprint": fingerprint,
                }
            ],
        }
    )


def _approved_revision(record, concept, source: str, target: str):
    proposal = persist_glossary_revision(
        record, concept, payload=GlossaryPayload(source, target, scope="document"),
        author_type="model", revision_state="proposal",
    )
    payload = GlossaryPayload(source, target, scope="document")
    approval = RuntimeApprovalRequest(
        requester_user_id=record.project.user_id, approver_user_id=None,
        subject_type="translator.glossary-lock.v1", subject_id=proposal.id,
        preview_payload=glossary_lock_preview(record, concept, proposal, payload),
        status="pending", resolved_at=None,
    )
    db.session.add(approval)
    db.session.commit()
    with _request_as(record.project.user_id):
        locked = approve_glossary_proposal(
            record, proposal, approved_payload=payload, approval=approval
        )
    return proposal, locked


def _locked(record, source: str, target: str):
    concept = create_glossary_concept(record)
    proposal, locked = _approved_revision(record, concept, source, target)
    return concept, proposal, locked


class _request_as:
    """A real Flask-Login request identity for the dedicated human action."""

    def __init__(self, user_id: int):
        self.user_id = user_id
        self.context = None

    def __enter__(self):
        self.context = current_app.test_request_context("/phase5-approval")
        self.context.push()
        # Request setup must not accidentally persist a deliberately stale
        # caller object before the approval function proves it ignores it.
        with db.session.no_autoflush:
            user = db.session.get(User, self.user_id)
        assert user is not None
        login_user(user)
        return user

    def __exit__(self, exc_type, exc, traceback):
        logout_user()
        assert self.context is not None
        self.context.pop()


def test_census_is_complete_deterministic_encrypted_and_never_mutates_legacy(app):
    project, plan, record, _operation = _state()
    census = build_semantic_census(plan)
    assert {item.occurrence_kind for item in census.occurrences} >= {
        "slot",
        "anchor",
        "protected_inline",
    }
    assert census == build_semantic_census(plan)
    row = persist_semantic_census(record, census)
    db.session.commit()
    assert rehydrate_semantic_census(row, record).census_hash == census.census_hash
    raw = db.session.execute(
        text(
            "SELECT value_text, structural_json FROM translation_semantic_occurrence LIMIT 1"
        )
    ).one()
    assert "enc:" in raw.value_text and "Title" not in raw.value_text
    raw_witness = db.session.execute(
        text("SELECT value_hash, occurrence_hash FROM translation_semantic_occurrence LIMIT 1")
    ).one()
    assert raw_witness.value_hash != hashlib.sha256(b"Title").hexdigest()
    assert raw_witness.occurrence_hash != hashlib.sha256(b"Title").hexdigest()
    assert project.working_memory is None
    assert (
        db.session.execute(
            text("SELECT COUNT(*) FROM translation_segment")
        ).scalar_one()
        == 0
    )
    row.occurrences[0].value_hash = "0" * 64
    with pytest.raises(BlockContractError, match="semantic-census-occurrence"):
        rehydrate_semantic_census(row, record)


def test_coverage_identity_is_keyed_and_rotation_safe_in_raw_sqlite(app):
    _project, plan, record, _operation = _state()
    census = persist_semantic_census(record)
    db.session.commit()
    entry = next(
        item
        for item in plan.coverage.entries
        if "/@aria-label" in item.locator
    )
    occurrence = db.session.execute(
        text(
            "SELECT slot_id FROM translation_semantic_occurrence "
            "WHERE census_id = :census AND occurrence_kind = 'coverage' "
            "AND start_offset = :start AND end_offset = :end"
        ),
        {
            "census": census.id,
            "start": entry.raw_start,
            "end": entry.raw_end,
        },
    ).scalar_one()
    predictable = "coverage:" + hashlib.sha256(
        canonical_json(
            {
                "locator": entry.locator,
                "raw_start": entry.raw_start,
                "raw_end": entry.raw_end,
                "source_hash": entry.source_hash,
            }
        ).encode()
    ).hexdigest()
    assert occurrence != predictable
    assert entry.source_hash not in occurrence
    app.extensions["carlina_encryption"].key_store.rotate()
    assert rehydrate_semantic_census(census, record).source_plan_hash == plan.plan_hash


def test_census_logical_identity_is_stable_and_idempotent_across_rotations(app):
    _project, plan, record, _operation = _state()
    census = persist_semantic_census(record)
    db.session.commit()
    initial_id = census.id
    logical_hash = build_semantic_census(plan).census_hash
    for _ in range(3):
        app.extensions["carlina_encryption"].key_store.rotate()
        rebuilt = build_semantic_census(plan)
        assert rebuilt.census_hash == logical_hash
        assert rehydrate_semantic_census(census, record).census_hash == logical_hash
        assert persist_semantic_census(record).id == initial_id
        assert TranslationSemanticCensus.query.count() == 1


def _census_race_worker(
    application: Flask,
    barrier: Barrier,
    *,
    plan_id: str,
    outcomes: list[tuple[str, str]],
) -> None:
    """Race two independent sessions through census and occurrence creation."""

    with application.app_context():
        try:
            connection = db.session.connection()
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.exec_driver_sql("PRAGMA busy_timeout=30000")
            record = db.session.get(TranslationSourcePlan, plan_id)
            assert record is not None
            assert TranslationSemanticCensus.query.filter_by(
                source_plan_id=plan_id
            ).one_or_none() is None
            barrier.wait(timeout=15)
            census = persist_semantic_census(record)
            db.session.commit()
            outcomes.append(("ok", census.id))
        except BlockContractError as exc:
            db.session.rollback()
            outcomes.append(("domain", exc.code))
        except Exception as exc:  # noqa: BLE001 - expose unexpected race failures
            db.session.rollback()
            outcomes.append(("unexpected", f"{type(exc).__name__}:{exc}"))
        finally:
            db.session.remove()


def test_census_creation_is_a_real_two_session_race(app):
    _project, _plan, record, _operation = _state("-census-race")
    plan_id = record.id
    db.session.commit()
    db.session.remove()

    barrier = Barrier(2)
    outcomes: list[tuple[str, str]] = []
    workers = [
        Thread(
            target=_census_race_worker,
            args=(app, barrier),
            kwargs={"plan_id": plan_id, "outcomes": outcomes},
        )
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=20)
        assert not worker.is_alive(), "census race worker did not finish"

    assert sorted(kind for kind, _detail in outcomes) == ["ok", "ok"]
    assert len({detail for _kind, detail in outcomes}) == 1
    census = TranslationSemanticCensus.query.filter_by(source_plan_id=plan_id).one()
    assert rehydrate_semantic_census(census).census_hash == census.census_hash
    assert TranslationSemanticCensus.query.filter_by(source_plan_id=plan_id).count() == 1


@pytest.mark.parametrize(
    "fixture_name",
    ("semantic-kitchen-sink.html", "nested-notes.html", "inline-template.html"),
)
def test_census_preserves_semantic_fixture_provenance(app, fixture_name: str):
    project, _plan, _record, _operation = _state()
    html = (FIXTURE / fixture_name).read_text(encoding="utf-8")
    digest = hashlib.sha256(html.encode()).hexdigest()
    snapshot = AttestedDocumentSnapshot(
        id="phase5-doc",
        document_id="phase5-doc",
        output_html=html,
        content_hash=digest,
        semantic_content_hash=digest,
        metadata={},
        provenance={},
        captured_at="2026-08-25T00:00:00+00:00",
        semantic_contract_version=SEMANTIC_HTML_CONTRACT_VERSION,
    )
    plan = plan_document_blocks(
        snapshot,
        project_id=project.id,
        source_attestation=source_attestation_for_project(project),
        source_language="en",
    )
    census = build_semantic_census(plan)
    kinds = {item.occurrence_kind for item in census.occurrences}
    assert {"slot", "anchor"} <= kinds
    coverage = [item for item in census.occurrences if item.occurrence_kind == "coverage"]
    assert len(coverage) == len(plan.coverage.entries)
    assert all(item.structural["terminology_eligible"] is False for item in coverage)
    assert all(
        item.start_offset is None
        or (item.end_offset is not None and item.end_offset >= item.start_offset)
        for item in census.occurrences
    )
    if fixture_name == "inline-template.html":
        assert any(
            item.disposition == "literal-preserved" for item in census.occurrences
        )
    if fixture_name == "semantic-kitchen-sink.html":
        assert any(
            item.occurrence_kind == "protected_inline" for item in census.occurrences
        )


def test_artifacts_and_glossary_are_append_only_scope_bound_and_private(app):
    _project, _plan, record, _operation = _state()
    brief = ContextArtifactPayload(
        "document_brief",
        "en",
        {"summary": "A document.", "domains": ["test"], "audience": "reader"},
    )
    revision = persist_context_artifact_revision(
        record,
        kind="document_brief",
        payload=brief,
        analysis_state="model",
        profile_id="brief",
        profile_version="1",
        model={"name": "test-model", "temperature": 0},
        prompt={"system": "preserve structure", "version": 3},
        settings={"evaluation": False, "max_tokens": 512},
        input_evidence={"source_plan_id": record.id},
    )
    assert revision.model_json == {"name": "test-model", "temperature": 0}
    assert revision.prompt_json == {"system": "preserve structure", "version": 3}
    assert revision.settings_json == {"evaluation": False, "max_tokens": 512}
    next_revision = persist_context_artifact_revision(
        record,
        kind="document_brief",
        payload=brief,
        analysis_state="human",
        profile_id="brief",
        profile_version="1",
        artifact=revision.artifact,
    )
    assert (
        next_revision.parent_revision_id == revision.id
        and next_revision.revision_number == 2
    )
    concept = create_glossary_concept(record)
    proposal = persist_glossary_revision(
        record,
        concept,
            payload=GlossaryPayload("Title", "Tytul", scope="document"),
        author_type="model",
        revision_state="proposal",
    )
    approval = RuntimeApprovalRequest(
        requester_user_id=record.project.user_id, approver_user_id=None,
        subject_type="translator.glossary-lock.v1", subject_id=proposal.id,
        preview_payload=glossary_lock_preview(record, concept, proposal, GlossaryPayload("Title", "Tytul", scope="document")),
        status="pending", resolved_at=None,
    )
    db.session.add(approval)
    db.session.commit()
    with _request_as(record.project.user_id):
        locked = approve_glossary_proposal(
            record,
            proposal,
            approved_payload=GlossaryPayload("Title", "Tytul", scope="document"),
            approval=approval,
        )
    active = activate_glossary_revision(locked, expected_version=0)
    assert active.revision_id == locked.id and locked.author_type == "human"
    with pytest.raises(BlockContractError, match="glossary-revision-authority"):
        persist_glossary_revision(
            record,
            concept,
                payload=GlossaryPayload("Title", "Bad", scope="document"),
                author_type="model",
                revision_state="locked",
            )
    assert proposal.keyed_lookup_digest == glossary_lookup_digest("title")
    assert rehydrate_glossary_revision(locked, record).target_term == "Tytul"
    raw = db.session.execute(
        text(
            "SELECT payload_json FROM translation_block_glossary_revision WHERE id = :id"
        ),
        {"id": locked.id},
    ).scalar_one()
    assert "Tytul" not in raw and "enc:" in raw
    artifact_raw = db.session.execute(
        text(
            "SELECT model_json, prompt_json, settings_json "
            "FROM translation_context_artifact_revision WHERE id = :id"
        ),
        {"id": revision.id},
    ).one()
    assert all(
        "preserve structure" not in value and "enc:v2:" in value
        for value in artifact_raw
    )
    tamper_savepoint = db.session.begin_nested()
    try:
        db.session.execute(
            text(
                "UPDATE translation_context_artifact_revision "
                "SET model_json = 'enc:v2:tampered' WHERE id = :id"
            ),
            {"id": revision.id},
        )
        db.session.expire(revision, ["model_json"])
        with pytest.raises(BlockContractError, match="analysis-artifact"):
            rehydrate_context_artifact_revision(revision, record)
    finally:
        tamper_savepoint.rollback()
    persist_glossary_occurrences(
        locked,
        [{"coordinate_system": "source_projection", "block_id": record.blocks[0].id, "anchor_id": record.anchors[0].id, "start_offset": 0, "end_offset": len("Title")}],
    )


def test_artifact_json_validation_happens_before_any_row_flush(app):
    _project, _plan, record, _operation = _state("-artifact-json")
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    invalid_inputs = (
        {"nested": {"not_finite": float("nan")}},
        {"nested": [float("inf")]},
        {1: "non-string JSON key"},
        {"tuple": ("not", "an", "exact", "JSON", "array")},
        cyclic,
    )
    for invalid in invalid_inputs:
        with pytest.raises(BlockContractError, match="analysis-artifact-input"):
            persist_context_artifact_revision(
                record,
                kind="document_brief",
                payload=ContextArtifactPayload(
                    "document_brief",
                    "en",
                    {"summary": "Summary", "domains": ["test"], "audience": "reader"},
                ),
                analysis_state="model",
                profile_id="brief",
                profile_version="1",
                input_evidence=invalid,
            )
    db.session.commit()
    assert TranslationContextArtifact.query.count() == 0
    assert TranslationContextArtifactRevision.query.count() == 0


def test_artifact_profile_identity_round_trips_without_text_coercion(app):
    _project, _plan, record, _operation = _state("-artifact-profile-roundtrip")
    revision = persist_context_artifact_revision(
        record,
        kind="document_brief",
        payload=ContextArtifactPayload(
            "document_brief",
            "en",
            {"summary": "Summary", "domains": ["test"], "audience": "reader"},
        ),
        analysis_state="model",
        profile_id="brief-v1",
        profile_version="2026.08",
    )
    db.session.commit()
    db.session.expire(revision)
    reloaded = db.session.get(TranslationContextArtifactRevision, revision.id)
    assert reloaded is not None
    assert (reloaded.profile_id, reloaded.profile_version) == (
        "brief-v1",
        "2026.08",
    )
    assert rehydrate_context_artifact_revision(reloaded, record).kind == "document_brief"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("profile_id", True),
        ("profile_version", 1),
        ("profile_id", "   "),
        ("profile_version", "\t"),
        ("profile_id", "x" * 65),
        ("profile_version", "x" * 65),
    ),
)
def test_artifact_profile_identity_rejects_non_string_blank_and_oversized(
    app, field, value
):
    _project, _plan, record, _operation = _state(
        f"-artifact-profile-invalid-{field}-{len(str(value))}"
    )
    kwargs = {
        "profile_id": "brief",
        "profile_version": "1",
    }
    kwargs[field] = value
    with pytest.raises(BlockContractError, match="analysis-artifact-identity"):
        persist_context_artifact_revision(
            record,
            kind="document_brief",
            payload=ContextArtifactPayload(
                "document_brief",
                "en",
                {"summary": "Summary", "domains": ["test"], "audience": "reader"},
            ),
            analysis_state="model",
            **kwargs,
        )
    db.session.commit()
    assert TranslationContextArtifact.query.count() == 0
    assert TranslationContextArtifactRevision.query.count() == 0


def test_mutated_artifact_payload_is_revalidated_before_new_artifact_write(app):
    _project, _plan, record, _operation = _state("-artifact-payload-mutation")
    payload = ContextArtifactPayload(
        "document_brief",
        "en",
        {"summary": "Summary", "domains": ["test"], "audience": "reader"},
    )
    payload.content["domains"].append(object())
    with pytest.raises(BlockContractError, match="analysis-artifact-payload"):
        persist_context_artifact_revision(
            record,
            kind="document_brief",
            payload=payload,
            analysis_state="model",
            profile_id="brief",
            profile_version="1",
        )
    db.session.commit()
    assert TranslationContextArtifact.query.count() == 0
    assert TranslationContextArtifactRevision.query.count() == 0

    caller_domains = ["caller-owned"]
    isolated = ContextArtifactPayload(
        "document_brief",
        "en",
        {
            "summary": "Summary",
            "domains": caller_domains,
            "audience": "reader",
        },
    )
    caller_domains.append("after-construction")
    assert isolated.to_dict()["content"]["domains"] == ["caller-owned"]


def test_new_artifact_and_revision_share_one_rollback_safe_savepoint(app, monkeypatch):
    _project, _plan, record, _operation = _state("-artifact-savepoint")
    payload = ContextArtifactPayload(
        "document_brief",
        "en",
        {"summary": "Summary", "domains": ["test"], "audience": "reader"},
    )
    original_flush = db.session.flush

    def fail_revision_flush(*_args, **_kwargs):
        if any(
            isinstance(item, TranslationContextArtifactRevision)
            for item in db.session.new
        ):
            raise IntegrityError("forced revision failure", {}, RuntimeError("boom"))
        return original_flush(*_args, **_kwargs)

    monkeypatch.setattr(db.session, "flush", fail_revision_flush)
    with pytest.raises(IntegrityError, match="forced revision failure"):
        persist_context_artifact_revision(
            record,
            kind="document_brief",
            payload=payload,
            analysis_state="model",
            profile_id="brief",
            profile_version="1",
        )
    db.session.commit()
    assert TranslationContextArtifact.query.count() == 0
    assert TranslationContextArtifactRevision.query.count() == 0


def test_frozen_baseline_adapts_verified_locked_values_as_untrusted_context(app):
    _project, _plan, record, operation = _state()
    artifact = persist_context_artifact_revision(
        record,
        kind="style_guide",
        payload=ContextArtifactPayload(
            "style_guide",
            "en",
            {"register": "formal", "guidance": ["use exact terms"], "prohibitions": []},
        ),
        analysis_state="human",
        profile_id="style",
        profile_version="1",
    )
    _concept, _proposal, locked = _locked(record, "Widget", "Widzet")
    baseline = freeze_context_baseline(
        operation, record, artifact_revisions=[artifact], glossary_revisions=[locked]
    )
    assert (
        freeze_context_baseline(
            operation,
            record,
            artifact_revisions=[artifact],
            glossary_revisions=[locked],
        )
        is baseline
    )
    slots = context_slots_from_baseline(baseline, record)
    glossary = next(slot for slot in slots if slot.slot_type == "glossary_constraints")
    assert (
        glossary.trust_class == "trusted_instruction" and "Widzet" in glossary.content
    )
    writable_content = _writable_source_content()
    packet = render_provider_packet(
        slots=(
            __import__(
                "iarf_assistant.addons.translator.window_contract",
                fromlist=["ContextSlot"],
            ).ContextSlot(
                "control_contract",
                "Return DTO",
                {"contract_version": "translation-block-window-prompt-v2"},
                "control",
                "1",
                hashlib.sha256(b"Return DTO").hexdigest(),
                {},
            ),
            __import__(
                "iarf_assistant.addons.translator.window_contract",
                fromlist=["ContextSlot"],
            ).ContextSlot(
                "project_policy",
                "Quality",
                {"policy": {"target_language": "pl", "register": "formal"}},
                "policy",
                "1",
                hashlib.sha256(b"Quality").hexdigest(),
                {},
            ),
            *slots,
            __import__(
                "iarf_assistant.addons.translator.window_contract",
                fromlist=["ContextSlot"],
            ).ContextSlot(
                "writable_source",
                writable_content,
                {"blocks": ["block-1"]},
                "source",
                "1",
                hashlib.sha256(writable_content.encode()).hexdigest(),
                {},
            ),
        ),
        target_language="pl",
        requested_output_tokens=64,
        native_json_schema=False,
    )
    assert (
        "Widzet" in packet.messages[1]["content"]
        and "<untrusted_read_only_data>" in packet.messages[1]["content"]
    )
    assert (
        rehydrate_context_baseline(baseline, record).record_hash == baseline.record_hash
    )
    baseline.manifest_hash = "0" * 64
    with pytest.raises(BlockContractError, match="baseline-record"):
        rehydrate_context_baseline(baseline, record)


def test_model_analysis_terminology_is_advisory_baseline_evidence(app):
    _project, _plan, record, operation = _state("-analysis-terminology")
    proposal = {
        "source_term": "running head",
        "target_term": "żywa pagina",
        "notes": "Publishing term; do not confuse it with a section heading.",
        "scope": "document",
        "source_block_ids": ["block-1"],
        "confidence": 0.95,
        "status": "proposed",
    }
    artifact = persist_context_artifact_revision(
        record,
        kind="document_brief",
        payload=ContextArtifactPayload(
            "document_brief",
            "en",
            {"summary": "A book.", "domains": ["publishing"], "audience": "reader"},
        ),
        analysis_state="model",
        profile_id="analysis-reduce-v1",
        profile_version="1",
        evidence={
            "reduction_mode": "provider",
            "glossary_proposals": [proposal],
        },
    )
    baseline = freeze_context_baseline(
        operation,
        record,
        artifact_revisions=[artifact],
    )
    slots = context_slots_from_baseline(baseline, record)
    terminology = next(
        slot for slot in slots if slot.slot_type == "terminology_guidance"
    )
    assert terminology.trust_class == "untrusted_reference"
    assert terminology.provenance["artifact_revision_hash"] == artifact.record_hash
    assert "running head" in terminology.content
    control_content = "Return DTO"
    writable_content = _writable_source_content()
    packet = render_provider_packet(
        slots=(
            ContextSlot(
                "control_contract",
                control_content,
                {"contract_version": WINDOW_PROMPT_CONTRACT_VERSION},
                "control",
                "1",
                hashlib.sha256(control_content.encode()).hexdigest(),
                {},
            ),
            *slots,
            ContextSlot(
                "writable_source",
                writable_content,
                {"blocks": ["block-1"]},
                "source",
                "1",
                hashlib.sha256(writable_content.encode()).hexdigest(),
                {},
            ),
        ),
        target_language="pl",
        requested_output_tokens=64,
        native_json_schema=False,
    )
    assert "advisory evidence, not a binding mapping" in packet.messages[0]["content"]
    assert "running head" in packet.messages[1]["content"]


def test_malformed_model_analysis_terminology_fails_closed(app):
    _project, _plan, record, operation = _state("-bad-analysis-terminology")
    artifact = persist_context_artifact_revision(
        record,
        kind="document_brief",
        payload=ContextArtifactPayload(
            "document_brief",
            "en",
            {"summary": "A book.", "domains": [], "audience": "reader"},
        ),
        analysis_state="model",
        profile_id="analysis-reduce-v1",
        profile_version="1",
        evidence={"glossary_proposals": "not-a-list"},
    )
    baseline = freeze_context_baseline(
        operation,
        record,
        artifact_revisions=[artifact],
    )
    with pytest.raises(BlockContractError, match="baseline-analysis-terminology"):
        context_slots_from_baseline(baseline, record)


def test_baseline_retry_validates_selection_before_reusing_existing_manifest(app):
    _project, _plan, record, operation = _state("-baseline-selection")
    artifact = persist_context_artifact_revision(
        record,
        kind="document_brief",
        payload=ContextArtifactPayload(
            "document_brief",
            "en",
            {"summary": "Summary", "domains": ["test"], "audience": "reader"},
        ),
        analysis_state="model",
        profile_id="brief",
        profile_version="1",
    )
    baseline = freeze_context_baseline(operation, record, artifact_revisions=[artifact])
    db.session.commit()
    with pytest.raises(BlockContractError, match="baseline-artifact-selection"):
        freeze_context_baseline(operation, record, artifact_revisions=[object()])
    assert rehydrate_context_baseline(baseline, record) is baseline


def _baseline_race_worker(
    application: Flask,
    barrier: Barrier,
    *,
    operation_id: str,
    plan_id: str,
    outcomes: list[tuple[str, str]],
) -> None:
    """Race two independent sessions through the complete baseline insert."""

    with application.app_context():
        try:
            connection = db.session.connection()
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.exec_driver_sql("PRAGMA busy_timeout=30000")
            operation = db.session.get(TranslationOperation, operation_id)
            record = db.session.get(TranslationSourcePlan, plan_id)
            assert operation is not None and record is not None
            # Both sessions prove that they reached the same pre-insert point;
            # the unique operation key must arbitrate the actual creation.
            assert db.session.query(TranslationContextBaseline).filter_by(
                operation_id=operation_id
            ).one_or_none() is None
            barrier.wait(timeout=15)
            baseline = freeze_context_baseline(operation, record)
            db.session.commit()
            outcomes.append(("ok", baseline.id))
        except BlockContractError as exc:
            db.session.rollback()
            outcomes.append(("domain", exc.code))
        except Exception as exc:  # noqa: BLE001 - expose unexpected race failures
            db.session.rollback()
            outcomes.append(("unexpected", f"{type(exc).__name__}:{exc}"))
        finally:
            db.session.remove()


def test_baseline_creation_is_a_real_two_session_race(app):
    _project, _plan, record, operation = _state("-baseline-race")
    operation_id, plan_id = operation.id, record.id
    db.session.commit()
    db.session.remove()

    barrier = Barrier(2)
    outcomes: list[tuple[str, str]] = []
    workers = [
        Thread(
            target=_baseline_race_worker,
            args=(app, barrier),
            kwargs={
                "operation_id": operation_id,
                "plan_id": plan_id,
                "outcomes": outcomes,
            },
        )
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=20)
        assert not worker.is_alive(), "baseline race worker did not finish"

    assert sorted(kind for kind, _detail in outcomes) == ["ok", "ok"]
    assert len({detail for _kind, detail in outcomes}) == 1
    assert db.session.query(TranslationContextBaseline).filter_by(
        operation_id=operation_id
    ).count() == 1


def test_operation_plan_pin_is_project_scoped_and_retry_conflicts_on_explicit_change(app):
    project, plan, record, operation = _state("-operation-pins")
    finish_operation(operation, "completed")
    pinned, created = create_operation(
        project,
        kind="document_block_translate",
        request_payload={"plan": "phase5"},
        idempotency_key="operation-pin",
        source_content_hash=plan.snapshot.content_hash,
        source_plan_id=record.id,
        source_plan_hash=plan.plan_hash,
    )
    assert created is True
    retry, retry_created = create_operation(
        project,
        kind="document_block_translate",
        request_payload={"plan": "phase5"},
        idempotency_key="operation-pin",
    )
    assert retry_created is False
    assert retry.id == pinned.id
    with pytest.raises(TranslationOperationConflict):
        create_operation(
            project,
            kind="document_block_translate",
            request_payload={"plan": "phase5"},
            idempotency_key="operation-pin",
            source_content_hash="0" * 64,
        )
    with pytest.raises(ValueError, match="source_content_hash is required"):
        create_operation(
            project,
            kind="document_block_translate",
            request_payload={"plan": "phase5"},
            idempotency_key="operation-pin",
            source_plan_id=record.id,
            source_plan_hash="0" * 64,
        )


def test_operation_source_pins_are_immutable_and_baseline_reloads_clean_state(app):
    project, plan, record, operation = _state("-operation-immutable")
    alternate_plan = plan_document_blocks(
        plan.snapshot,
        project_id=project.id,
        source_attestation=source_attestation_for_project(project),
        source_language="en",
        settings={"splitter_profile": "alternate"},
    )
    alternate_record, _ = persist_source_plan(project, alternate_plan)
    db.session.commit()
    assert alternate_plan.snapshot.content_hash == plan.snapshot.content_hash
    assert alternate_plan.plan_hash != plan.plan_hash

    operation.source_plan_id = alternate_record.id
    with pytest.raises(BlockContractError, match="baseline-operation-source"):
        freeze_context_baseline(operation, record)
    db.session.rollback()

    with pytest.raises(BlockContractError, match="operation-pin-immutable"):
        operation.source_plan_hash = alternate_plan.plan_hash
        db.session.flush()
    db.session.rollback()
    with pytest.raises(IntegrityError):
        db.session.execute(
            text(
                "UPDATE translation_operation SET source_plan_id = :plan_id, "
                "source_plan_hash = :plan_hash WHERE id = :operation_id"
            ),
            {
                "plan_id": alternate_record.id,
                "plan_hash": alternate_plan.plan_hash,
                "operation_id": operation.id,
            },
        )
    db.session.rollback()
    durable = db.session.get(TranslationOperation, operation.id)
    assert durable is not None
    assert durable.source_plan_id == record.id
    assert durable.source_plan_hash == plan.plan_hash


@pytest.mark.parametrize(
    ("kind", "content"),
    (
        ("document_brief", {"summary": True, "domains": [], "audience": "a"}),
        ("style_guide", {"register": "formal", "guidance": [1], "prohibitions": []}),
        ("entity_register", {"entities": [{"name": "x", "type": "person", "aliases": [], "extra": "x"}]}),
        ("entity_register", {"entities": [{"name": "x", "type": "person", "aliases": [False]}]}),
    ),
)
def test_artifact_schemas_reject_wrong_types_unknown_keys_and_nested_values(app, kind, content):
    with pytest.raises(BlockContractError, match="analysis-artifact-schema"):
        ContextArtifactPayload(kind, "en", content)


def test_glossary_occurrences_require_exact_eligible_projection_coordinates(app):
    _project, _plan, record, _operation = _state()
    _concept, _proposal, revision = _locked(record, "Title", "Tytul")
    db.session.commit()
    base = {
        "coordinate_system": "source_projection",
        "block_id": record.blocks[0].id,
        "anchor_id": record.anchors[0].id,
        "start_offset": 0,
        "end_offset": len("Title"),
    }
    persist_glossary_occurrences(revision, [base])
    for key, value in (("coordinate_system", "raw_html"), ("end_offset", 1)):
        invalid = dict(base)
        invalid[key] = value
        with pytest.raises(BlockContractError):
            persist_glossary_occurrences(revision, [invalid])


def test_glossary_occurrence_validation_is_atomic_and_caller_rollback_owned(app):
    _project, _plan, record, _operation = _state()
    _concept, _proposal, revision = _locked(record, "Title", "Tytul")
    db.session.commit()
    valid = {
        "coordinate_system": "source_projection",
        "block_id": record.blocks[0].id,
        "anchor_id": record.anchors[0].id,
        "start_offset": 0,
        "end_offset": len("Title"),
    }
    invalid = {**valid, "block_id": "missing-block"}
    with pytest.raises(BlockContractError, match="glossary-occurrence-owner"):
        persist_glossary_occurrences(revision, [valid, invalid])
    db.session.rollback()
    assert TranslationGlossaryOccurrence.query.count() == 0

    persist_glossary_occurrences(revision, [valid])
    db.session.rollback()
    assert TranslationGlossaryOccurrence.query.count() == 0


def test_glossary_occurrence_non_lock_operational_error_is_not_reused(
    app, monkeypatch
):
    _project, _plan, record, _operation = _state("-occurrence-operational")
    _concept, _proposal, revision = _locked(record, "Title", "Tytul")
    db.session.commit()
    occurrence = {
        "coordinate_system": "source_projection",
        "block_id": record.blocks[0].id,
        "anchor_id": record.anchors[0].id,
        "start_offset": 0,
        "end_offset": len("Title"),
    }

    def fail_flush(*_args, **_kwargs):
        raise OperationalError("forced non-lock failure", {}, RuntimeError("boom"))

    monkeypatch.setattr(db.session, "flush", fail_flush)
    with pytest.raises(OperationalError, match="forced non-lock failure"):
        persist_glossary_occurrences(revision, [occurrence])
    db.session.rollback()
    assert TranslationGlossaryOccurrence.query.count() == 0


def test_baseline_fences_operation_hash_and_attestation_before_prompt(app):
    _project, _plan, record, operation = _state()
    # Source identity is now immutable after creation.  A stale/forged ORM
    # update must fail before baseline validation can consume it; reload the
    # durable operation after the caller rollback for the actual baseline path.
    with pytest.raises(BlockContractError, match="operation-pin-immutable"):
        operation.source_content_hash = "0" * 64
        db.session.flush()
    db.session.rollback()
    operation = db.session.get(TranslationOperation, operation.id)
    assert operation is not None
    _concept, _proposal, locked = _locked(record, "Widget", "Widzet")
    baseline = freeze_context_baseline(operation, record, glossary_revisions=[locked])
    glossary = next(slot for slot in context_slots_from_baseline(baseline, record) if slot.slot_type == "glossary_constraints")
    from iarf_assistant.addons.translator.window_contract import ContextSlot

    forged = ContextSlot(
        glossary.slot_type, glossary.content,
        {**glossary.provenance, "binding_attestation": "1:forged:" + "0" * 64},
        glossary.artifact_id, glossary.artifact_version, glossary.artifact_hash, glossary.dependency,
    )
    writable_content = _writable_source_content()
    controls = (
        ContextSlot("control_contract", "Return DTO", {"contract_version": "translation-block-window-prompt-v2"}, "control", "1", hashlib.sha256(b"Return DTO").hexdigest(), {}),
        ContextSlot("writable_source", writable_content, {"blocks": ["block-1"]}, "source", "1", hashlib.sha256(writable_content.encode()).hexdigest(), {}),
    )
    with pytest.raises(BlockContractError, match="window-trusted-glossary"):
        render_provider_packet(slots=(controls[0], forged, controls[1]), target_language="pl", requested_output_tokens=8, native_json_schema=False)


def test_synthetic_glossary_resolver_is_testing_only_and_exactly_scoped(app):
    from iarf_assistant.addons.translator.window_contract import ContextSlot

    writable_content = _writable_source_content()
    controls = (
        ContextSlot(
            "control_contract", "Return DTO",
            {"contract_version": "translation-block-window-prompt-v2"}, "control", "1",
            hashlib.sha256(b"Return DTO").hexdigest(), {},
        ),
        ContextSlot(
            "writable_source", writable_content, {"blocks": ["block-1"]}, "source", "1",
            hashlib.sha256(writable_content.encode()).hexdigest(), {},
        ),
    )
    content = '{"locked_terms":[{"source":"Widget","target":"Widzet"}]}'
    synthetic = ContextSlot(
        "glossary_constraints", content,
        {"resolver": "synthetic-test", "locked_terms": [{"source": "Widget", "target": "Widzet"}]},
        "synthetic-test:phase5", "1", hashlib.sha256(content.encode()).hexdigest(), {},
    )
    assert render_provider_packet(
        slots=(controls[0], synthetic, controls[1]), target_language="pl",
        requested_output_tokens=8, native_json_schema=False,
    ).transport == "strict_text_json"
    app.config["TESTING"] = False
    try:
        with pytest.raises(BlockContractError, match="window-trusted-glossary"):
            render_provider_packet(
                slots=(controls[0], synthetic, controls[1]), target_language="pl",
                requested_output_tokens=8, native_json_schema=False,
            )
    finally:
        app.config["TESTING"] = True


def test_glossary_lookup_and_baseline_attestation_survive_key_rotation(app):
    _project, _plan, record, operation = _state()
    _concept, proposal, locked = _locked(record, "Widget", "Widzet")
    census = persist_semantic_census(record)
    artifact = persist_context_artifact_revision(
        record,
        kind="document_brief",
        payload=ContextArtifactPayload(
            "document_brief", "en",
            {"summary": "Widget guide", "domains": ["test"], "audience": "reader"},
        ),
        analysis_state="model",
        profile_id="brief",
        profile_version="1",
    )
    baseline = freeze_context_baseline(
        operation, record, artifact_revisions=[artifact], glossary_revisions=[locked]
    )
    db.session.commit()
    old_lookup = locked.keyed_lookup_digest
    old_attestation = baseline.binding_attestation
    raw = db.session.execute(
        text(
            "SELECT keyed_lookup_digest, source_digest, target_digest, payload_json "
            "FROM translation_block_glossary_revision WHERE id = :id"
        ),
        {"id": locked.id},
    ).one()
    assert all("Widget" not in value and "Widzet" not in value for value in raw)
    app.extensions["carlina_encryption"].key_store.rotate()
    assert old_lookup in keyed_lookup_candidates("widget", purpose="translator-glossary:unicode-casefold-v1:1")
    assert locked in find_glossary_revisions_for_term(record, "Widget")
    assert rehydrate_semantic_census(census, record).source_plan_hash == record.plan_hash
    assert rehydrate_context_artifact_revision(artifact, record).kind == "document_brief"
    assert rehydrate_glossary_revision(proposal, record).source_term == "Widget"
    assert rehydrate_glossary_revision(locked, record).target_term == "Widzet"
    assert rehydrate_context_baseline(baseline, record) is baseline
    assert old_attestation != glossary_lookup_digest("Widget")


def test_context_impact_composite_revision_scope_fk_rejects_foreign_reference(app):
    project, _plan, record, _operation = _state()
    db.session.execute(text("PRAGMA foreign_keys = ON"))
    with pytest.raises(IntegrityError):
        db.session.execute(
            text(
                "INSERT INTO translation_context_impact "
                "(id, project_id, source_plan_id, block_id, old_glossary_revision_id, reason_json, impact_state, created_at) "
                "VALUES ('impact-fk', :project, :plan, :block, 'foreign-revision', 'enc:v2:test', 'needs_review', '2026-08-25T00:00:00')"
            ),
            {"project": project.id, "plan": record.id, "block": record.blocks[0].id},
        )


def _active_race_worker(
    application: Flask,
    barrier: Barrier,
    revision_id: str,
    expected_version: int,
    outcomes: list[tuple[str, str]],
) -> None:
    """Run one CAS attempt on an independent Flask/SQLAlchemy session."""

    with application.app_context():
        try:
            connection = db.session.connection()
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.exec_driver_sql("PRAGMA busy_timeout=30000")
            revision = db.session.get(TranslationGlossaryRevision, revision_id)
            assert revision is not None
            barrier.wait(timeout=15)
            active = activate_glossary_revision(
                revision, expected_version=expected_version
            )
            db.session.commit()
            outcomes.append(("ok", f"{active.revision_id}:{active.version}"))
        except BlockContractError as exc:
            db.session.rollback()
            outcomes.append(("domain", exc.code))
        except Exception as exc:  # noqa: BLE001 - report thread failures to the parent test
            db.session.rollback()
            outcomes.append(("unexpected", f"{type(exc).__name__}:{exc}"))
        finally:
            db.session.remove()


def _run_active_race(
    application: Flask, revision_ids: tuple[str, str], expected_version: int
) -> list[tuple[str, str]]:
    barrier = Barrier(2)
    outcomes: list[tuple[str, str]] = []
    threads = [
        Thread(
            target=_active_race_worker,
            args=(application, barrier, revision_id, expected_version, outcomes),
        )
        for revision_id in revision_ids
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive(), "CAS race worker did not finish"
    return outcomes


def test_glossary_active_creation_is_a_real_two_session_cas_race(app):
    _project, _plan, record, _operation = _state()
    _concept, _proposal, locked = _locked(record, "Widget", "Widzet")
    locked_id = locked.id
    concept_id = locked.concept_id
    db.session.commit()
    db.session.remove()

    outcomes = _run_active_race(app, (locked_id, locked_id), expected_version=0)
    assert sorted(kind for kind, _detail in outcomes) == ["domain", "ok"]
    assert [detail for kind, detail in outcomes if kind == "domain"] == [
        "glossary-active-conflict"
    ]
    assert all("IntegrityError" not in detail for _kind, detail in outcomes)
    assert all("OperationalError" not in detail for _kind, detail in outcomes)

    active = db.session.get(TranslationGlossaryActive, concept_id)
    assert active is not None
    assert (active.revision_id, active.version) == (locked_id, 1)


def test_glossary_active_update_is_a_real_two_session_cas_race(app):
    _project, _plan, record, _operation = _state()
    concept, _proposal, first = _locked(record, "Widget", "Widzet")
    second_proposal = persist_glossary_revision(
        record,
        concept,
        payload=GlossaryPayload("Widget", "Gadzет", scope="document"),
        author_type="model",
        revision_state="proposal",
    )
    payload = GlossaryPayload("Widget", "Gadzет", scope="document")
    approval = RuntimeApprovalRequest(
        requester_user_id=record.project.user_id,
        approver_user_id=None,
        subject_type="translator.glossary-lock.v1",
        subject_id=second_proposal.id,
        preview_payload=glossary_lock_preview(
            record, concept, second_proposal, payload
        ),
        status="pending",
        resolved_at=None,
    )
    db.session.add(approval)
    db.session.commit()
    with _request_as(record.project.user_id):
        second = approve_glossary_proposal(
            record, second_proposal, approved_payload=payload, approval=approval
        )
    second_id = second.id
    concept_id = concept.id
    activate_glossary_revision(first, expected_version=0)
    db.session.commit()
    db.session.remove()

    outcomes = _run_active_race(app, (second_id, second_id), expected_version=1)
    assert sorted(kind for kind, _detail in outcomes) == ["domain", "ok"]
    assert [detail for kind, detail in outcomes if kind == "domain"] == [
        "glossary-active-conflict"
    ]
    assert all("IntegrityError" not in detail for _kind, detail in outcomes)
    assert all("OperationalError" not in detail for _kind, detail in outcomes)

    active = db.session.get(TranslationGlossaryActive, concept_id)
    assert active is not None
    assert (active.revision_id, active.version) == (second_id, 2)


def test_active_glossary_reader_refreshes_stale_pointer_without_flushing_caller(app):
    project, _plan, record, _operation = _state("-active-refresh")
    concept, _proposal, first = _locked(record, "Widget", "Widzet")
    _second_proposal, second = _approved_revision(
        record, concept, "Widget", "Przyrzad"
    )
    active = activate_glossary_revision(first, expected_version=0)
    db.session.commit()
    concept_id = concept.id
    second_id = second.id
    original_project_name = project.name

    # Keep both a stale active pointer and unrelated dirty caller state in the
    # scoped session while another worker advances durable truth.
    assert active.revision_id == first.id and active.version == 1
    active.version = 99
    project.name = "caller-only dirty name"
    errors: list[str] = []

    def advance_pointer() -> None:
        with app.app_context():
            try:
                revision = db.session.get(TranslationGlossaryRevision, second_id)
                assert revision is not None
                activate_glossary_revision(revision, expected_version=1)
                db.session.commit()
            except Exception as exc:  # noqa: BLE001 - surface worker failure
                db.session.rollback()
                errors.append(f"{type(exc).__name__}:{exc}")
            finally:
                db.session.remove()

    worker = Thread(target=advance_pointer, daemon=True)
    worker.start()
    worker.join(timeout=15)
    assert not worker.is_alive() and not errors

    resolved = rehydrate_active_glossary_revision(concept_id)
    assert resolved.id == second_id
    assert active.revision_id == second_id and active.version == 2
    assert project.name == "caller-only dirty name"
    with db.engine.connect() as connection:
        durable_name = connection.execute(
            text("SELECT name FROM translation_project WHERE id = :id"),
            {"id": project.id},
        ).scalar_one()
    assert durable_name == original_project_name


def test_phase5a_orm_tables_have_named_constraint_and_index_parity(app):
    target_tables = (
        "translation_source_anchor",
        "translation_semantic_census",
        "translation_semantic_occurrence",
        "translation_context_artifact",
        "translation_context_artifact_revision",
        "translation_block_glossary_concept",
        "translation_block_glossary_revision",
        "translation_block_glossary_active",
        "translation_block_glossary_occurrence",
        "translation_context_baseline",
        "translation_context_baseline_item",
        "translation_context_impact",
    )
    inspector = sqlalchemy_inspect(db.engine)
    for table_name in target_tables:
        table = db.metadata.tables[table_name]
        assert [
            (column.name, column.nullable) for column in table.columns
        ] == [
            (column["name"], column["nullable"])
            for column in inspector.get_columns(table_name)
        ]
        model_unique = sorted(
            (constraint.name, tuple(column.name for column in constraint.columns))
            for constraint in table.constraints
            if constraint.__class__.__name__ == "UniqueConstraint"
            and constraint.name is not None
        )
        db_unique = sorted(
            (constraint["name"], tuple(constraint["column_names"]))
            for constraint in inspector.get_unique_constraints(table_name)
            if constraint["name"] is not None
        )
        assert model_unique == db_unique
        model_indexes = sorted(
            (index.name, tuple(column.name for column in index.columns), index.unique)
            for index in table.indexes
        )
        db_indexes = sorted(
            (index["name"], tuple(index["column_names"]), bool(index["unique"]))
            for index in inspector.get_indexes(table_name)
        )
        assert model_indexes == db_indexes
        model_fks = sorted(
            (
                constraint.name,
                tuple(column.name for column in constraint.columns),
                tuple(element.target_fullname for element in constraint.elements),
            )
            for constraint in table.constraints
            if constraint.__class__.__name__ == "ForeignKeyConstraint"
        )
        db_fks = sorted(
            (
                fk["name"],
                tuple(fk["constrained_columns"]),
                tuple(
                    f"{fk['referred_table']}.{column}"
                    for column in fk["referred_columns"]
                ),
            )
            for fk in inspector.get_foreign_keys(table_name)
        )
        assert model_fks == db_fks


def _proposal_and_approval(
    record,
    *,
    approval_changes: dict[str, object] | None = None,
    payload: GlossaryPayload | None = None,
):
    concept = create_glossary_concept(record)
    proposal = persist_glossary_revision(
        record,
        concept,
        payload=GlossaryPayload("Matrix term", "Termin macierzowy", scope="document"),
        author_type="model",
        revision_state="proposal",
    )
    payload = payload or GlossaryPayload(
        "Matrix term", "Termin macierzowy", scope="document"
    )
    approval = RuntimeApprovalRequest(
        requester_user_id=record.project.user_id,
        approver_user_id=None,
        subject_type="translator.glossary-lock.v1",
        subject_id=proposal.id,
        preview_payload=glossary_lock_preview(record, concept, proposal, payload),
        status="pending",
        resolved_at=None,
    )
    for field, value in (approval_changes or {}).items():
        setattr(approval, field, value)
    db.session.add(approval)
    db.session.commit()
    return concept, proposal, payload, approval


@pytest.mark.parametrize(
    "changes",
    (
        {"status": "approved"},
        {"status": "rejected"},
        {"resolved_at": utc_now_db()},
        {"expires_at": utc_now_db() - timedelta(seconds=1)},
        {"subject_type": "other.subject"},
        {"subject_id": "wrong-proposal"},
        {"requester_user_id": 99999},
        {"approver_user_id": 99999},
        {"preview_payload": None},
        {"preview_payload": {"scope": "document"}},
    ),
)
def test_glossary_approval_matrix_rejects_untrusted_requests(app, changes):
    _project, _plan, record, _operation = _state()
    changes = dict(changes)
    if "requester_user_id" in changes or "approver_user_id" in changes:
        foreign = User(email="foreign@example.test", password_hash="x")
        db.session.add(foreign)
        db.session.flush()
        if "requester_user_id" in changes:
            changes["requester_user_id"] = foreign.id
        if "approver_user_id" in changes:
            changes["approver_user_id"] = foreign.id
    _concept, proposal, payload, approval = _proposal_and_approval(
        record, approval_changes=changes
    )
    with _request_as(record.project.user_id), pytest.raises(
        BlockContractError, match="glossary-approval-request"
    ):
        approve_glossary_proposal(
            record, proposal, approved_payload=payload, approval=approval
        )


def test_glossary_approval_scope_is_checked_before_cas(app):
    _project, _plan, record, _operation = _state()
    _concept, proposal, payload, approval = _proposal_and_approval(record)
    mismatched = GlossaryPayload(
        payload.source_term,
        payload.target_term,
        scope="chapter",
    )
    with _request_as(record.project.user_id), pytest.raises(
        BlockContractError, match="glossary-approval-scope"
    ):
        approve_glossary_proposal(
            record, proposal, approved_payload=mismatched, approval=approval
        )
    durable_approval = db.session.get(RuntimeApprovalRequest, approval.id)
    assert durable_approval is not None and durable_approval.status == "pending"


def test_approved_glossary_lock_survives_wall_clock_expiry(app, monkeypatch):
    _project, _plan, record, _operation = _state()
    _concept, proposal, payload, approval = _proposal_and_approval(record)
    approval.expires_at = utc_now_db() + timedelta(hours=1)
    db.session.commit()
    with _request_as(record.project.user_id):
        locked = approve_glossary_proposal(
            record, proposal, approved_payload=payload, approval=approval
        )
    assert locked.approved_at is not None
    monkeypatch.setattr(
        glossary_module,
        "utc_now_db",
        lambda: locked.approved_at + timedelta(days=1),
    )
    assert rehydrate_glossary_revision(locked, record).target_term == payload.target_term


@pytest.mark.parametrize(
    "preview_field",
    ("proposal_record_hash", "approved_payload_hash", "scope"),
)
def test_glossary_approval_rejects_each_preview_lineage_tamper(app, preview_field):
    _project, _plan, record, _operation = _state()
    concept, proposal, payload, approval = _proposal_and_approval(record)
    forged = dict(approval.preview_payload)
    forged[preview_field] = "0" * 64 if preview_field != "scope" else "other"
    approval.preview_payload = forged
    db.session.commit()
    with _request_as(record.project.user_id), pytest.raises(
        BlockContractError, match="glossary-approval-request"
    ):
        approve_glossary_proposal(
            record, proposal, approved_payload=payload, approval=approval
        )
    assert concept.id == proposal.concept_id


def test_glossary_direct_human_lock_and_reused_approval_are_rejected(app):
    _project, _plan, record, _operation = _state()
    concept, proposal, payload, approval = _proposal_and_approval(record)
    with pytest.raises(BlockContractError, match="glossary-revision-authority"):
        persist_glossary_revision(
            record,
            concept,
            payload=payload,
            author_type="human",
            revision_state="locked",
        )
    with _request_as(record.project.user_id):
        locked = approve_glossary_proposal(
            record, proposal, approved_payload=payload, approval=approval
        )
    locked_id = locked.id
    db.session.commit()
    locked.approval_receipt = "0" * 64
    # Recompute the public envelope as an attacker who can write the row might;
    # rehydration must still reject the forged keyed receipt itself.
    locked.record_hash = _revision_hash(locked)
    db.session.commit()
    with pytest.raises(BlockContractError, match="glossary-revision-authority"):
        activate_glossary_revision(locked)
    db.session.rollback()
    with db.session.begin_nested(), pytest.raises(IntegrityError):
        db.session.execute(
            text(
                "UPDATE translation_block_glossary_revision "
                "SET approval_receipt = NULL WHERE id = :id"
            ),
            {"id": locked_id},
        )
    with _request_as(record.project.user_id), pytest.raises(
        BlockContractError, match="glossary-approval-request"
    ):
        approve_glossary_proposal(
            record, proposal, approved_payload=payload, approval=approval
        )
    db.session.rollback()
    locked = db.session.get(TranslationGlossaryRevision, locked_id)
    assert locked is not None and locked.revision_state == "locked"


def test_glossary_approval_consumes_only_owner_pending_request_and_ignores_caller_mutation(app):
    _project, _plan, record, _operation = _state()
    _concept, proposal, payload, approval = _proposal_and_approval(record)
    approval_id = approval.id
    owner_id = record.project.user_id

    # A caller can mutate a stale object, but no-autoflush/populate-existing
    # must recover the durable pending request instead of trusting it.
    db.session.expunge(approval)
    approval.preview_payload = {"forged": "caller-only"}
    with _request_as(owner_id):
        locked = approve_glossary_proposal(
            record, proposal, approved_payload=payload, approval=approval
        )
    assert locked.approval_request_id == approval_id

    _concept, pending_proposal, pending_payload, pending = _proposal_and_approval(record)
    foreign = User(email="phase5-foreign-actor@example.test", password_hash="x")
    db.session.add(foreign)
    db.session.commit()
    with pytest.raises(BlockContractError, match="glossary-approval-actor"):
        approve_glossary_proposal(
            record, pending_proposal, approved_payload=pending_payload, approval=pending.id
        )
    with _request_as(foreign.id), pytest.raises(
        BlockContractError, match="glossary-approval-actor"
    ):
        approve_glossary_proposal(
            record, pending_proposal, approved_payload=pending_payload, approval=pending.id
        )


def test_glossary_raw_approval_status_flip_is_not_a_consumable_human_action(app):
    _project, _plan, record, _operation = _state()
    _concept, proposal, payload, approval = _proposal_and_approval(record)
    # The former private row-persistence helper was an authority bypass: a
    # caller could import it after flipping status in raw SQL.  Lock creation
    # must now have no callable path outside the authenticated CAS boundary.
    assert not hasattr(glossary_module, "_persist_locked_revision")
    db.session.execute(
        text(
            "UPDATE runtime_approval_request "
            "SET status = 'approved', approver_user_id = :owner, resolved_at = :now "
            "WHERE id = :id"
        ),
        {"owner": record.project.user_id, "now": utc_now_db(), "id": approval.id},
    )
    db.session.commit()
    with _request_as(record.project.user_id), pytest.raises(
        BlockContractError, match="glossary-approval-request"
    ):
        approve_glossary_proposal(
            record, proposal, approved_payload=payload, approval=approval.id
        )
    assert (
        TranslationGlossaryRevision.query.filter_by(
            concept_id=proposal.concept_id, revision_state="locked"
        ).count()
        == 0
    )


def _approval_race_worker(
    application: Flask,
    barrier: Barrier,
    *,
    owner_id: int,
    plan_id: str,
    proposal_id: str,
    approval_id: str,
    payload: GlossaryPayload,
    outcomes: list[tuple[str, str]],
):
    with application.app_context(), application.test_request_context("/phase5-approval-race"):
            user = db.session.get(User, owner_id)
            assert user is not None
            login_user(user)
            try:
                record = db.session.get(TranslationSourcePlan, plan_id)
                proposal = db.session.get(TranslationGlossaryRevision, proposal_id)
                assert record is not None and proposal is not None
                barrier.wait(timeout=10)
                locked = approve_glossary_proposal(
                    record, proposal, approved_payload=payload, approval=approval_id
                )
                db.session.commit()
                outcomes.append(("ok", locked.id))
            except BlockContractError as exc:
                db.session.rollback()
                outcomes.append(("domain", exc.code))
            except Exception as exc:  # noqa: BLE001 - test must expose unexpected races
                db.session.rollback()
                outcomes.append(("unexpected", f"{type(exc).__name__}:{exc}"))
            finally:
                logout_user()
                db.session.remove()


def test_glossary_pending_approval_cas_allows_exactly_one_concurrent_consumption(app):
    _project, _plan, record, _operation = _state()
    _concept, proposal, payload, approval = _proposal_and_approval(record)
    owner_id, plan_id, proposal_id, approval_id = (
        record.project.user_id,
        record.id,
        proposal.id,
        approval.id,
    )
    db.session.remove()
    barrier = Barrier(2)
    outcomes: list[tuple[str, str]] = []
    workers = [
        Thread(
            target=_approval_race_worker,
            args=(app, barrier),
            kwargs={
                "owner_id": owner_id,
                "plan_id": plan_id,
                "proposal_id": proposal_id,
                "approval_id": approval_id,
                "payload": payload,
                "outcomes": outcomes,
            },
        )
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=20)
        assert not worker.is_alive(), "approval CAS worker did not finish"
    assert sorted(kind for kind, _detail in outcomes) == ["domain", "ok"]
    # Depending on whether the loser loaded the request before or after the
    # winner committed, it sees either the failed pending-state CAS or the
    # already-consumed canonical request. Both are the same fail-closed
    # outcome; the invariant is exactly one durable lock.
    assert next(detail for kind, detail in outcomes if kind == "domain") in {
        "glossary-approval-request",
        "glossary-approval-used",
    }
    assert all(kind != "unexpected" for kind, _detail in outcomes)


def test_phase5a_rehydration_rejects_lineage_and_provenance_tampering(app):
    _project, _plan, record, operation = _state()
    artifact = persist_context_artifact_revision(
        record,
        kind="document_brief",
        payload=ContextArtifactPayload(
            "document_brief",
            "en",
            {"summary": "Summary", "domains": ["test"], "audience": "reader"},
        ),
        analysis_state="model",
        profile_id="brief",
        profile_version="1",
    )
    _concept, _proposal, glossary = _locked(record, "Widget", "Widzet")
    baseline = freeze_context_baseline(
        operation, record, artifact_revisions=[artifact], glossary_revisions=[glossary]
    )
    census = persist_semantic_census(record)
    db.session.commit()

    for field, value in (
        ("artifact_id", "wrong-artifact"),
        ("project_id", "wrong-project"),
        ("source_plan_id", "wrong-plan"),
        ("source_plan_hash", "0" * 64),
        ("output_hash", "0" * 64),
        ("record_hash", "0" * 64),
    ):
        setattr(artifact, field, value)
        with pytest.raises(BlockContractError):
            rehydrate_context_artifact_revision(artifact, record)
        db.session.rollback()
        artifact = db.session.get(type(artifact), artifact.id)
        assert artifact is not None

    for field, value in (
        ("operation_id", "wrong-operation"),
        ("project_id", "wrong-project"),
        ("owner_user_id", 99999),
        ("source_plan_id", "wrong-plan"),
        ("source_plan_hash", "0" * 64),
        ("manifest_hash", "0" * 64),
        ("binding_attestation", "0" * 64),
        ("record_hash", "0" * 64),
    ):
        setattr(baseline, field, value)
        with pytest.raises(BlockContractError):
            rehydrate_context_baseline(baseline, record)
        db.session.rollback()
        baseline = db.session.get(TranslationContextBaseline, baseline.id)
        assert baseline is not None

    for field, value in (
        ("project_id", "wrong-project"),
        ("source_plan_id", "wrong-plan"),
        ("source_plan_hash", "0" * 64),
        ("census_hash", "0" * 64),
        ("coverage_hash", "0" * 64),
        ("record_hash", "0" * 64),
    ):
        setattr(census, field, value)
        with pytest.raises(BlockContractError):
            rehydrate_semantic_census(census, record)
        db.session.rollback()
        census = db.session.get(TranslationSemanticCensus, census.id)
        assert census is not None

    for field, value in (
        ("project_id", "wrong-project"),
        ("source_plan_id", "wrong-plan"),
        ("concept_id", "wrong-concept"),
        ("approval_request_id", "wrong-approval"),
        ("approved_by_user_id", 99999),
        ("payload_hash", "0" * 64),
        ("record_hash", "0" * 64),
    ):
        setattr(glossary, field, value)
        with pytest.raises(BlockContractError):
            rehydrate_glossary_revision(glossary, record)
        db.session.rollback()
        glossary = db.session.get(TranslationGlossaryRevision, glossary.id)
        assert glossary is not None


def test_census_rejects_before_write_and_baseline_rejects_substituted_source(app):
    _project, plan, record, operation = _state()
    census = build_semantic_census(plan)
    before = db.session.query(TranslationSemanticCensus).count()
    forged_census = replace(census, source_plan_hash="0" * 64)
    with pytest.raises(BlockContractError, match="semantic-census-witness"):
        persist_semantic_census(record, forged_census)
    assert db.session.query(TranslationSemanticCensus).count() == before
    db.session.rollback()

    _other_project, _other_plan, other_record, _other_operation = _state("-other")
    artifact = persist_context_artifact_revision(
        record,
        kind="style_guide",
        payload=ContextArtifactPayload(
            "style_guide",
            "en",
            {"register": "formal", "guidance": [], "prohibitions": []},
        ),
        analysis_state="human",
        profile_id="style",
        profile_version="1",
    )
    baseline = freeze_context_baseline(operation, record, artifact_revisions=[artifact])
    with pytest.raises(BlockContractError, match="baseline-record"):
        rehydrate_context_baseline(baseline, other_record)


def test_glossary_impact_validation_is_atomic_and_caller_rollback_owned(app):
    _project, _plan, record, _operation = _state()
    _concept, _proposal, revision = _locked(record, "Title", "Tytul")
    db.session.commit()
    valid_block = record.blocks[0].id
    with pytest.raises(BlockContractError, match="glossary-impact-block"):
        record_glossary_impact(
            old_revision=None,
            new_revision=revision,
            block_ids=[valid_block, "missing-block"],
            reason={"source": "test"},
        )
    db.session.rollback()
    assert TranslationContextImpact.query.count() == 0

    record_glossary_impact(
        old_revision=None,
        new_revision=revision,
        block_ids=[valid_block],
        reason={"source": "test"},
    )
    db.session.rollback()
    assert TranslationContextImpact.query.count() == 0


def test_glossary_impact_non_lock_operational_error_is_not_reused(app, monkeypatch):
    _project, _plan, record, _operation = _state("-impact-operational")
    _concept, _proposal, revision = _locked(record, "Title", "Tytul")
    db.session.commit()

    def fail_flush(*_args, **_kwargs):
        raise OperationalError("forced non-lock failure", {}, RuntimeError("boom"))

    monkeypatch.setattr(db.session, "flush", fail_flush)
    with pytest.raises(OperationalError, match="forced non-lock failure"):
        record_glossary_impact(
            old_revision=None,
            new_revision=revision,
            block_ids=[record.blocks[0].id],
            reason={"source": "test"},
        )
    db.session.rollback()
    assert TranslationContextImpact.query.count() == 0


def test_equivalent_scope_locks_conflict_and_occurrence_impact_retries_dedupe(app):
    _project, _plan, record, _operation = _state()
    _concept_a, _proposal_a, first = _locked(record, "Title", "Tytul")
    _concept_b, _proposal_b, second = _locked(record, "TITLE", "Naglowek")
    db.session.commit()
    operation = db.session.query(TranslationOperation).filter_by(project_id=record.project_id).one()
    with pytest.raises(BlockContractError, match="baseline-glossary-conflict"):
        freeze_context_baseline(operation, record, glossary_revisions=[first, second])
    db.session.rollback()

    base = {
        "coordinate_system": "source_projection",
        "block_id": record.blocks[0].id,
        "anchor_id": None,
        "start_offset": 0,
        "end_offset": len("Title"),
    }
    first_rows = persist_glossary_occurrences(first, [base])
    second_rows = persist_glossary_occurrences(first, [base])
    assert first_rows[0].id == second_rows[0].id
    record_glossary_impact(
        old_revision=None,
        new_revision=first,
        block_ids=[record.blocks[0].id],
        reason={"source": "test"},
    )
    with pytest.raises(BlockContractError, match="glossary-impact-conflict"):
        record_glossary_impact(
            old_revision=None,
            new_revision=first,
            block_ids=[record.blocks[0].id],
            reason={"source": "different-but-same-event"},
        )
    assert len(TranslationGlossaryOccurrence.query.filter_by(revision_id=first.id).all()) == 1
    assert len(
        TranslationContextImpact.query.filter_by(
            source_plan_id=record.id, new_glossary_revision_id=first.id
        ).all()
    ) == 1


def test_glossary_active_occurrence_and_impact_receipts_seal_mutable_rows_and_rotate(
    app,
):
    _project, _plan, record, _operation = _state("-receipt-mutable")
    concept, _proposal, first = _locked(record, "Title", "Tytul")
    second_proposal = persist_glossary_revision(
        record,
        concept,
        payload=GlossaryPayload("Title", "Naglowek", scope="document"),
        author_type="model",
        revision_state="proposal",
    )
    approval = RuntimeApprovalRequest(
        requester_user_id=record.project.user_id,
        approver_user_id=None,
        subject_type="translator.glossary-lock.v1",
        subject_id=second_proposal.id,
        preview_payload=glossary_lock_preview(
            record,
            concept,
            second_proposal,
            GlossaryPayload("Title", "Naglowek", scope="document"),
        ),
        status="pending",
        resolved_at=None,
    )
    db.session.add(approval)
    db.session.commit()
    with _request_as(record.project.user_id):
        second = approve_glossary_proposal(
            record,
            second_proposal,
            approved_payload=GlossaryPayload("Title", "Naglowek", scope="document"),
            approval=approval,
        )
    active = activate_glossary_revision(first, expected_version=0)
    coordinate = {
        "coordinate_system": "source_projection",
        "block_id": record.blocks[0].id,
        "anchor_id": None,
        "start_offset": 0,
        "end_offset": len("Title"),
    }
    occurrence = persist_glossary_occurrences(first, [coordinate])[0]
    impact = record_glossary_impact(
        old_revision=None,
        new_revision=first,
        block_ids=[record.blocks[0].id],
        reason={"source": "receipt-test"},
    )[0]
    db.session.commit()

    assert verify_glossary_active(active) is first
    assert rehydrate_active_glossary_revision(concept.id) is first
    assert verify_glossary_occurrence(occurrence) is occurrence
    assert verify_context_impact(impact) is impact
    raw = db.session.execute(
        text(
            "SELECT active_receipt FROM translation_block_glossary_active "
            "WHERE concept_id = :id"
        ),
        {"id": concept.id},
    ).scalar_one()
    assert len(raw) == 64

    # Retained keys verify every receipt without rewriting any old evidence.
    app.extensions["carlina_encryption"].key_store.rotate()
    db.session.expire_all()
    assert verify_glossary_active(active).id == first.id
    assert verify_glossary_occurrence(occurrence).id == occurrence.id
    assert verify_context_impact(impact).id == impact.id

    db.session.execute(
        text(
            "UPDATE translation_block_glossary_active SET version = version + 1 "
            "WHERE concept_id = :id"
        ),
        {"id": concept.id},
    )
    db.session.expire(active)
    with pytest.raises(BlockContractError, match="glossary-active-receipt"):
        verify_glossary_active(active)
    db.session.rollback()
    active = db.session.get(TranslationGlossaryActive, concept.id)
    assert active is not None
    db.session.execute(
        text(
            "UPDATE translation_block_glossary_active SET revision_id = :revision "
            "WHERE concept_id = :concept"
        ),
        {"revision": second.id, "concept": concept.id},
    )
    db.session.expire(active)
    with pytest.raises(BlockContractError, match="glossary-active-receipt"):
        verify_glossary_active(active)
    db.session.rollback()
    active = db.session.get(TranslationGlossaryActive, concept.id)
    assert active is not None
    db.session.execute(
        text(
            "UPDATE translation_block_glossary_active "
            "SET updated_at = :updated_at WHERE concept_id = :concept"
        ),
        {"updated_at": utc_now_db() + timedelta(seconds=1), "concept": concept.id},
    )
    db.session.expire(active)
    with pytest.raises(BlockContractError, match="glossary-active-receipt"):
        verify_glossary_active(active)
    db.session.rollback()
    occurrence = db.session.get(TranslationGlossaryOccurrence, occurrence.id)
    assert occurrence is not None
    occurrence.end_offset += 1
    with pytest.raises(BlockContractError, match="glossary-occurrence-receipt"):
        verify_glossary_occurrence(occurrence)
    db.session.rollback()
    impact = db.session.get(TranslationContextImpact, impact.id)
    assert impact is not None
    impact.reason_json = {"source": "forged"}
    with pytest.raises(BlockContractError, match="glossary-impact-receipt"):
        verify_context_impact(impact)


def test_row_receipts_reject_same_scope_artifact_and_glossary_transplants(app):
    _project, _plan, record, _operation = _state("-receipt-transplant")
    first_artifact = persist_context_artifact_revision(
        record,
        kind="document_brief",
        payload=ContextArtifactPayload(
            "document_brief", "en", {"summary": "First", "domains": [], "audience": "a"}
        ),
        analysis_state="model",
        profile_id="brief",
        profile_version="1",
        model={"model": "one"},
        settings={"temperature": 0.2},
    )
    second_artifact = persist_context_artifact_revision(
        record,
        kind="document_brief",
        payload=ContextArtifactPayload(
            "document_brief", "en", {"summary": "Second", "domains": [], "audience": "b"}
        ),
        analysis_state="model",
        profile_id="brief",
        profile_version="1",
        model={"model": "two"},
        settings={"temperature": 0.3},
    )
    _first_concept, first_proposal, _first_locked = _locked(
        record, "Widget", "Widzet"
    )
    second_concept = create_glossary_concept(record)
    second_proposal = persist_glossary_revision(
        record,
        second_concept,
        payload=GlossaryPayload("Gadget", "Gadzet", scope="document"),
        author_type="model",
        revision_state="proposal",
    )
    db.session.commit()

    artifact_columns = (
        "schema_hash, profile_hash, model_hash, prompt_hash, settings_hash, input_hash, "
        "output_hash, evidence_hash, model_json, prompt_json, settings_json, input_json, "
        "output_json, evidence_json"
    )
    values = db.session.execute(
        text(
            f"SELECT {artifact_columns} FROM translation_context_artifact_revision "
            "WHERE id = :id"
        ),
        {"id": first_artifact.id},
    ).one()
    assignments = ", ".join(
        f"{column.strip()} = :{column.strip()}" for column in artifact_columns.split(",")
    )
    db.session.execute(
        text(
            "UPDATE translation_context_artifact_revision SET "
            f"{assignments} WHERE id = :id"
        ),
        {**dict(values._mapping), "id": second_artifact.id},
    )
    db.session.expire(second_artifact)
    with pytest.raises(BlockContractError, match="analysis-artifact-record"):
        rehydrate_context_artifact_revision(second_artifact, record)
    db.session.rollback()

    # This simulates an attacker who copies encrypted payload and every public
    # semantic witness but cannot manufacture the row-scoped receipt.
    proposal_columns = (
        "normalization_hash, keyed_lookup_digest, source_digest, target_digest, "
        "payload_hash, payload_json"
    )
    values = db.session.execute(
        text(
            f"SELECT {proposal_columns} FROM translation_block_glossary_revision "
            "WHERE id = :id"
        ),
        {"id": first_proposal.id},
    ).one()
    assignments = ", ".join(
        f"{column.strip()} = :{column.strip()}" for column in proposal_columns.split(",")
    )
    db.session.execute(
        text(
            "UPDATE translation_block_glossary_revision SET "
            f"{assignments} WHERE id = :id"
        ),
        {**dict(values._mapping), "id": second_proposal.id},
    )
    db.session.expire(second_proposal)
    with pytest.raises(BlockContractError, match="glossary-revision"):
        rehydrate_glossary_revision(second_proposal, record)
