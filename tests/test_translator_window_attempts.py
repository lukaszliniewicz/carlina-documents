"""Phase-3 internal window/evidence acceptance tests (no provider I/O)."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from content_storage_support import bind_content_policy
from flask import Flask
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION
from carlina.core.utc import utc_now_db
from iarf_assistant.addons.translator import (
    alignment_persistence,
    block_notes,
    block_window_runtime,
    target_persistence,
    window_execution,
    window_persistence,
)
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
    TranslationOperation,
    TranslationProject,
    TranslationTargetAlignmentActive,
    TranslationTargetBlockActive,
    TranslationTargetBlockNote,
    TranslationTargetBlockRevision,
    TranslationWindowAttempt,
    TranslationWindowAttemptRevision,
)
from iarf_assistant.addons.translator.operations import (
    TranslationOperationCancellation,
    claim_operation,
    request_cancellation,
)
from iarf_assistant.addons.translator.target_contract import (
    TargetBlockCandidate,
    TargetTextPart,
    structural_fingerprint,
)
from iarf_assistant.addons.translator.target_persistence import (
    activate_target_revision,
    persist_target_revision,
    rehydrate_target_block_notes,
)
from iarf_assistant.addons.translator.window_contract import (
    COMPACT_WRITABLE_SOURCE_CONTRACT_VERSION,
    NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
    PARTS_WRITABLE_SOURCE_CONTRACT_VERSION,
    PROVIDER_MESSAGE_MAX_BYTES,
    SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION,
    STUDY_NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
    WRITABLE_SOURCE_CONTRACT_VERSION,
    ContextSlot,
    TranslationWindow,
    plan_windows,
    reduce_slots,
    render_admitted_packet,
    render_provider_packet,
)
from iarf_assistant.addons.translator.window_execution import (
    execute_window_attempt,
    normalize_window_response,
)
from iarf_assistant.addons.translator.window_persistence import (
    VerifiedWindowEvidence,
    append_attempt_event,
    persist_window_attempt,
    predecessor_snapshot,
    rehydrate_attempt_events,
    rehydrate_window_attempt,
    seal_attempt_state_head,
)
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.core.task_queue import ExternalWorkPending
from iarf_assistant.extensions import db
from iarf_assistant.models import User


@pytest.fixture
def app(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'windows.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="window-test",
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
        db.engine.dispose()
        context.pop()


def _state(html: str = "<html><body><p>One.</p><p>Two.</p></body></html>"):
    user = User(email="windows@example.test", password_hash="x")
    db.session.add(user)
    db.session.flush()
    project = TranslationProject(
        user_id=user.id,
        name="Windows",
        source_lang="en",
        target_lang="pl",
        engine_contract="document-block-v1",
        settings={
            "translator_model": "configured/test",
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
    db.session.flush()
    digest = hashlib.sha256(html.encode()).hexdigest()
    snapshot = AttestedDocumentSnapshot(
        id="doc-1",
        document_id="doc-1",
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
        idempotency_key="window-test",
        request_fingerprint="a" * 64,
        state="running",
        source_content_hash=plan.snapshot.content_hash,
        source_plan_id=record.id,
        source_plan_hash=plan.plan_hash,
        queue_job_id="window-worker",
        lease_expires_at=utc_now_db() + timedelta(minutes=5),
    )
    db.session.add(operation)
    db.session.commit()
    return project, plan, record, operation


def _window(plan):
    return TranslationWindow(
        1,
        tuple(block.id for block in plan.blocks),
        tuple(structural_fingerprint(plan, block) for block in plan.blocks),
    )


def _packet():
    return render_provider_packet(
        slots=(
            _slot(
                "control_contract",
                "Return the exact DTO.",
                {"contract_version": "translation-block-window-prompt-v2"},
            ),
            _slot(
                "project_policy",
                "Polish quality translation.",
                {"policy": {"target_language": "pl", "register": "formal"}},
            ),
            _slot("research_evidence", "Bounded untrusted evidence.", {"id": "r1"}),
            _slot(
                "writable_source",
                _writable_source_content(),
                {"block_ids": ["block-1"]},
            ),
        ),
        target_language="pl",
        requested_output_tokens=512,
        native_json_schema=True,
    )


def _packet_with_research_dependency():
    content = '{"conclusion":"verified once"}'
    conclusion_hash = hashlib.sha256(content.encode()).hexdigest()
    research = ContextSlot(
        "research_evidence",
        content,
        {"baseline_id": "baseline-1", "baseline_hash": "b" * 64},
        "research-run-1",
        "1",
        conclusion_hash,
        {
            "run_id": "run-1",
            "run_record_hash": "1" * 64,
            "state_head_receipt": "2" * 64,
            "turn_id": "turn-1",
            "turn_record_hash": "3" * 64,
            "attempt_id": "attempt-1",
            "attempt_record_hash": "4" * 64,
            "attempt_request_hash": "5" * 64,
            "conclusion_hash": conclusion_hash,
        },
    )
    slots = tuple(
        research if slot.slot_type == "research_evidence" else slot
        for slot in _packet().slots
    )
    return (
        render_provider_packet(
            slots=slots,
            target_language="pl",
            requested_output_tokens=512,
            native_json_schema=True,
        ),
        research,
    )


def _writable_source_content():
    fingerprint = "b" * 64
    return json.dumps(
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
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _notes_packet(plan, writable_source_contract_version):
    rows = []
    for index, block in enumerate(plan.blocks, 1):
        row = {
            "n": index,
            "role": block.role,
            "content": block.projection,
            "fields": [],
        }
        if (
            writable_source_contract_version
            == SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION
        ):
            row["sentences"] = [{"n": 1, "text": block.projection}]
        rows.append(row)
    writable = {
        "contract_version": writable_source_contract_version,
        "blocks": rows,
    }
    if (
        writable_source_contract_version
        == SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION
    ):
        writable["annotation_profile"] = block_notes.ANNOTATION_PROFILE_STUDY
    content = json.dumps(writable, sort_keys=True, separators=(",", ":"))
    return render_provider_packet(
        slots=(
            _slot(
                "control_contract",
                "Return the exact DTO.",
                {"contract_version": "translation-block-window-prompt-v2"},
            ),
            _slot(
                "project_policy",
                json.dumps(
                    {"target_language": "pl", "register": "formal"},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                {"policy": {"target_language": "pl", "register": "formal"}},
            ),
            _slot(
                "writable_source",
                content,
                {"block_ids": [block.id for block in plan.blocks]},
            ),
        ),
        target_language="pl",
        requested_output_tokens=512,
        native_json_schema=True,
    )


def _notes_response(plan, writable_source_contract_version):
    results = []
    for index, block in enumerate(plan.blocks, 1):
        content = (
            "Term alpha beta gamma delta epsilon."
            if index == 1
            else "Second translated block."
        )
        item = {
            "n": index,
            "content": content,
            "fields": [],
            "notes": (
                [
                    {"anchor": anchor, "body": f"Study note for {anchor}."}
                    for anchor in ("Term", "alpha", "beta", "gamma", "delta", "epsilon")
                ]
                if index == 1
                else []
            ),
        }
        if (
            writable_source_contract_version
            == SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION
        ):
            item["alignment"] = {
                "target_sentences": [content],
                "groups": [{"source": [1], "target": [1]}],
            }
        results.append(item)
    return {"results": results}


def _slot(slot_type, content, provenance, dependency=None):
    return ContextSlot(
        slot_type,
        content,
        provenance,
        f"{slot_type}-artifact",
        "v1",
        hashlib.sha256(content.encode()).hexdigest(),
        dependency or {},
    )


def _admission():
    return {"admitted": True, "mode": "manual", "requested_output_tokens": 512}


def _raw_digest(value):
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _candidate(plan, record, block, text="Przekład."):
    return TargetBlockCandidate(
        source_plan_id=record.id,
        source_plan_hash=plan.plan_hash,
        block_id=block.id,
        structural_fingerprint=structural_fingerprint(plan, block),
        parts=(TargetTextPart(kind="text", text=text),),
        lexical_attributes=(),
    )


def test_windows_are_adjacent_whole_blocks_and_reducible_slots_are_absent(app):
    _project, plan, _record, _operation = _state()
    windows = plan_windows(plan, maximum_blocks=2, fits=lambda blocks: len(blocks) == 1)
    assert [window.block_ids for window in windows] == [
        (plan.blocks[0].id,),
        (plan.blocks[1].id,),
    ]
    with pytest.raises(BlockContractError, match="window-oversized-block"):
        plan_windows(plan, maximum_blocks=2, fits=lambda _blocks: False)
    slots = reduce_slots(_packet().slots, keep_reducible=0)
    rendered = render_provider_packet(
        slots=slots,
        target_language="pl",
        requested_output_tokens=512,
        native_json_schema=False,
    )
    assert "research_evidence" not in rendered.messages[0]["content"]
    assert rendered.response_format is None and rendered.transport == "strict_text_json"
    assert "STRICT JSON ONLY" in rendered.messages[1]["content"]
    assert (
        "put every translatable attribute slot in lexical_attributes"
        in rendered.messages[1]["content"]
    )
    assert (
        "never copy a protected literal's source_value into a text part"
        in rendered.messages[1]["content"]
    )
    assert "emit no nonblank text part" in rendered.messages[1]["content"]
    assert "<untrusted_read_only_data>" in rendered.messages[1]["content"]
    native = _packet()
    assert native.response_format is not None
    assert native.messages[0]["content"] != native.messages[1]["content"]
    admitted, evidence = render_admitted_packet(
        slots=_packet().slots,
        target_language="pl",
        requested_output_tokens=512,
        native_json_schema=False,
        admit=lambda packet: {
            "admitted": all(
                slot.slot_type != "research_evidence" for slot in packet.slots
            )
        },
    )
    assert evidence["admitted"] and all(
        slot.slot_type != "research_evidence" for slot in admitted.slots
    )
    with pytest.raises(BlockContractError, match="window-oversized-block"):
        render_admitted_packet(
            slots=_packet().slots,
            target_language="pl",
            requested_output_tokens=512,
            native_json_schema=False,
            admit=lambda _packet: {"admitted": False},
        )


def test_wire_message_limit_reduces_optional_context_before_admission(app):
    huge_evidence = "x" * PROVIDER_MESSAGE_MAX_BYTES
    slots = (
        _slot(
            "control_contract",
            "Return the exact DTO.",
            {"contract_version": "translation-block-window-prompt-v2"},
        ),
        _slot(
            "project_policy",
            "Polish quality translation.",
            {"policy": {"target_language": "pl", "register": "formal"}},
        ),
        _slot("research_evidence", huge_evidence, {"id": "large"}),
        _slot(
            "writable_source",
            _writable_source_content(),
            {"block_ids": ["block-1"]},
        ),
    )

    packet, admission = render_admitted_packet(
        slots=slots,
        target_language="pl",
        requested_output_tokens=512,
        native_json_schema=False,
        admit=lambda _packet: {"admitted": True},
    )

    assert admission["admitted"]
    assert all(slot.slot_type != "research_evidence" for slot in packet.slots)
    assert all(
        len(message["content"].encode("utf-8")) <= PROVIDER_MESSAGE_MAX_BYTES
        for message in packet.messages
    )


def test_strict_text_packet_rejects_a_noncanonical_writable_envelope(app):
    slots = tuple(
        _slot(
            "writable_source",
            "block ids are immutable",
            {"block_ids": ["block-1"]},
        )
        if slot.slot_type == "writable_source"
        else slot
        for slot in _packet().slots
    )

    with pytest.raises(BlockContractError, match="window-writable-response-contract"):
        render_provider_packet(
            slots=slots,
            target_language="pl",
            requested_output_tokens=512,
            native_json_schema=False,
        )


def test_analysis_terminology_is_the_last_optional_slot_reduced(app):
    slots = (
        _slot(
            "control_contract",
            "Return the exact DTO.",
            {"contract_version": "translation-block-window-prompt-v2"},
        ),
        _slot("research_evidence", "Research.", {"id": "research"}),
        _slot("terminology_guidance", "Terminology.", {"id": "terms"}),
        _slot("writable_source", "Source.", {"block_ids": ["x"]}),
    )
    reduced = reduce_slots(slots, keep_reducible=1)
    assert [slot.slot_type for slot in reduced] == [
        "control_contract",
        "terminology_guidance",
        "writable_source",
    ]


def test_attempt_packet_event_chain_and_lineage_are_tamper_evident(app):
    _project, plan, record, operation = _state()
    window = _window(plan)
    attempt = persist_window_attempt(
        operation=operation,
        source_plan=record,
        window=window,
        packet=_packet(),
        settings={"quality": "high"},
        model_profile={"resolved": "configured/test"},
        admission=_admission(),
    )
    original_packet_witness = attempt.packet_hash
    assert (
        rehydrate_window_attempt(attempt, record).payload_hash == _packet().payload_hash
    )
    assert rehydrate_attempt_events(attempt)[0].event_type == "request_prepared"
    retry = persist_window_attempt(
        operation=operation,
        source_plan=record,
        window=window,
        packet=_packet(),
        settings={"quality": "high"},
        model_profile={"resolved": "configured/test"},
        admission=_admission(),
        retry_of_attempt_id=attempt.id,
    )
    assert retry.retry_of_attempt_hash == attempt.attempt_record_hash
    with pytest.raises(BlockContractError, match="window-retry-packet"):
        persist_window_attempt(
            operation=operation,
            source_plan=record,
            window=window,
            packet=_packet(),
            settings={"quality": "lower"},
            model_profile={"resolved": "configured/test"},
            admission=_admission(),
            retry_of_attempt_id=attempt.id,
        )
    with pytest.raises(BlockContractError, match="window-admission"):
        persist_window_attempt(
            operation=operation,
            source_plan=record,
            window=window,
            packet=_packet(),
            settings={"quality": "high"},
            model_profile={"resolved": "configured/test"},
            admission={"admitted": False, "requested_output_tokens": 512},
        )
    attempt.model_profile_json = {"resolved": "tampered"}
    with pytest.raises(BlockContractError, match="window-attempt-evidence"):
        rehydrate_window_attempt(attempt, record)
    attempt.model_profile_json = {"resolved": "configured/test"}
    attempt.packet_hash = "0" * 64
    with pytest.raises(BlockContractError, match="window-packet-hash"):
        rehydrate_window_attempt(attempt, record)
    attempt.packet_hash = original_packet_witness
    attempt.packet_json = {"tampered": True}
    with pytest.raises(BlockContractError, match="window-packet"):
        rehydrate_window_attempt(retry, record)
    attempt.packet_json = _packet().to_dict()
    event = attempt.events[0]
    event.predecessor_event_hash = "0" * 64
    with pytest.raises(BlockContractError, match="window-event-chain"):
        rehydrate_attempt_events(attempt)


def test_verified_window_evidence_reuses_exact_plan_and_research_graph(
    app, monkeypatch
):
    _project, plan, record, operation = _state()
    packet, research = _packet_with_research_dependency()
    evidence = VerifiedWindowEvidence.from_verified_baseline(
        operation=operation,
        source_plan=record,
        plan=plan,
        baseline_slots=(research,),
    )
    missing_research = VerifiedWindowEvidence.from_verified_baseline(
        operation=operation,
        source_plan=record,
        plan=plan,
        baseline_slots=(),
    )
    monkeypatch.setattr(
        window_persistence,
        "_record_plan",
        lambda _record: pytest.fail("verified plan was rehydrated again"),
    )
    attempt = persist_window_attempt(
        operation=operation,
        source_plan=record,
        window=_window(plan),
        packet=packet,
        settings={"quality": "high"},
        model_profile={"resolved": "configured/test"},
        admission=_admission(),
        verified_evidence=evidence,
    )
    assert (
        rehydrate_window_attempt(
            attempt, record, verified_evidence=evidence
        ).payload_hash
        == packet.payload_hash
    )
    with pytest.raises(BlockContractError, match="window-verified-evidence"):
        rehydrate_window_attempt(attempt, record, verified_evidence=missing_research)

    retry = persist_window_attempt(
        operation=operation,
        source_plan=record,
        window=_window(plan),
        packet=packet,
        settings={"quality": "high"},
        model_profile={"resolved": "configured/test"},
        admission=_admission(),
        retry_of_attempt_id=attempt.id,
        verified_evidence=evidence,
    )
    assert retry.retry_of_attempt_hash == attempt.attempt_record_hash
    assert (
        rehydrate_window_attempt(retry, record, verified_evidence=evidence).payload_hash
        == packet.payload_hash
    )


def test_window_attempt_requires_the_operations_exact_source_plan_pin(app):
    project, plan, _record, operation = _state()
    alternate_plan = plan_document_blocks(
        plan.snapshot,
        project_id=project.id,
        source_attestation=source_attestation_for_project(project),
        source_language="en",
        settings={"planning_variant": "same-content-alternate-plan"},
    )
    alternate_record, _created = persist_source_plan(project, alternate_plan)
    db.session.flush()
    assert alternate_plan.snapshot.content_hash == operation.source_content_hash
    assert alternate_record.id != operation.source_plan_id
    assert alternate_plan.plan_hash != operation.source_plan_hash

    with pytest.raises(BlockContractError, match="window-operation-plan"):
        persist_window_attempt(
            operation=operation,
            source_plan=alternate_record,
            window=_window(alternate_plan),
            packet=_packet(),
            settings={"quality": "high"},
            model_profile={"resolved": "configured/test"},
            admission=_admission(),
        )


def test_window_receipts_encrypt_exact_settings_and_reject_same_scope_transplant(app):
    _project, plan, record, operation = _state()
    window = _window(plan)
    source = persist_window_attempt(
        operation=operation,
        source_plan=record,
        window=window,
        packet=_packet(),
        settings={"quality": "high", "glossary": "strict"},
        model_profile={"resolved": "configured/test"},
        admission=_admission(),
    )
    recipient = persist_window_attempt(
        operation=operation,
        source_plan=record,
        window=window,
        packet=_packet(),
        settings={"quality": "high", "glossary": "different"},
        model_profile={"resolved": "configured/test"},
        admission=_admission(),
    )
    db.session.commit()
    settings_ciphertext = db.session.execute(
        text("SELECT settings_json FROM translation_window_attempt WHERE id = :id"),
        {"id": source.id},
    ).scalar_one()
    assert str(settings_ciphertext).startswith("enc:v2:")
    assert "strict" not in str(settings_ciphertext)
    assert (
        rehydrate_window_attempt(source, record).payload_hash == _packet().payload_hash
    )

    source_row = (
        db.session.execute(
            text(
                "SELECT packet_json, block_ids_json, block_fingerprints_json, predecessor_json, "
                "model_profile_json, admission_json, context_manifest_json, settings_json, "
                "packet_hash, prompt_hash, settings_hash, context_hash, context_manifest_hash, "
                "model_profile_hash, predecessor_hash, retry_of_attempt_id, retry_of_attempt_hash, "
                "rebase_of_attempt_id, rebase_of_attempt_hash, attempt_record_hash, state_head_receipt "
                "FROM translation_window_attempt WHERE id = :id"
            ),
            {"id": source.id},
        )
        .mappings()
        .one()
    )
    packet_data = _packet().to_dict()
    prompt_data = {
        "messages": packet_data["messages"],
        "response_format": packet_data["response_format"],
        "transport": packet_data["transport"],
    }
    # Encrypted payloads must not leave a raw SHA-256 guess oracle beside the
    # ciphertext. These fixed-width columns are purpose-separated keyed witnesses.
    assert source_row["packet_hash"] != _raw_digest(packet_data)
    assert source_row["prompt_hash"] != _raw_digest(prompt_data)
    assert source_row["settings_hash"] != _raw_digest(
        {"quality": "high", "glossary": "strict"}
    )
    assert source.events[0].payload_hash != _raw_digest(source.events[0].payload_json)
    db.session.execute(
        text(
            "UPDATE translation_window_attempt SET "
            "packet_json = :packet_json, block_ids_json = :block_ids_json, "
            "block_fingerprints_json = :block_fingerprints_json, predecessor_json = :predecessor_json, "
            "model_profile_json = :model_profile_json, admission_json = :admission_json, "
            "context_manifest_json = :context_manifest_json, settings_json = :settings_json, "
            "packet_hash = :packet_hash, prompt_hash = :prompt_hash, settings_hash = :settings_hash, "
            "context_hash = :context_hash, context_manifest_hash = :context_manifest_hash, "
            "model_profile_hash = :model_profile_hash, predecessor_hash = :predecessor_hash, "
            "retry_of_attempt_id = :retry_of_attempt_id, retry_of_attempt_hash = :retry_of_attempt_hash, "
            "rebase_of_attempt_id = :rebase_of_attempt_id, rebase_of_attempt_hash = :rebase_of_attempt_hash, "
            "attempt_record_hash = :attempt_record_hash, state_head_receipt = :state_head_receipt "
            "WHERE id = :id"
        ),
        {**source_row, "id": recipient.id},
    )
    db.session.commit()
    db.session.expire_all()
    with pytest.raises(BlockContractError, match="window-attempt-hash"):
        rehydrate_window_attempt(
            db.session.get(TranslationWindowAttempt, recipient.id), record
        )
    source.settings_json = {"quality": "tampered"}
    db.session.commit()
    db.session.expire_all()
    with pytest.raises(BlockContractError, match="window-settings-hash"):
        rehydrate_window_attempt(
            db.session.get(TranslationWindowAttempt, source.id), record
        )


def test_exact_retry_reuses_verified_private_witnesses_across_key_rotation(app):
    _project, plan, record, operation = _state()
    attempt = persist_window_attempt(
        operation=operation,
        source_plan=record,
        window=_window(plan),
        packet=_packet(),
        settings={"quality": "high"},
        model_profile={"resolved": "configured/test"},
        admission=_admission(),
    )
    db.session.commit()
    original_witnesses = tuple(
        getattr(attempt, field)
        for field in (
            "packet_hash",
            "prompt_hash",
            "settings_hash",
            "context_hash",
            "context_manifest_hash",
            "model_profile_hash",
            "predecessor_hash",
        )
    )
    app.extensions["carlina_encryption"].key_store.rotate()

    retry = persist_window_attempt(
        operation=operation,
        source_plan=record,
        window=_window(plan),
        packet=_packet(),
        settings={"quality": "high"},
        model_profile={"resolved": "configured/test"},
        admission=_admission(),
        retry_of_attempt_id=attempt.id,
    )
    assert (
        tuple(
            getattr(retry, field)
            for field in (
                "packet_hash",
                "prompt_hash",
                "settings_hash",
                "context_hash",
                "context_manifest_hash",
                "model_profile_hash",
                "predecessor_hash",
            )
        )
        == original_witnesses
    )
    assert (
        rehydrate_window_attempt(retry, record).payload_hash == _packet().payload_hash
    )


def test_window_state_head_seals_events_links_and_survives_key_rotation(app):
    _project, plan, record, operation = _state()
    window = _window(plan)
    attempt = persist_window_attempt(
        operation=operation,
        source_plan=record,
        window=window,
        packet=_packet(),
        settings={"quality": "high"},
        model_profile={"resolved": "configured/test"},
        admission=_admission(),
    )
    db.session.commit()
    result = {
        "results": [_candidate(plan, record, block).to_dict() for block in plan.blocks]
    }
    assert execute_window_attempt(
        attempt,
        operation=operation,
        source_plan=record,
        window=window,
        provider_call=lambda _payload: result,
    )
    attempt_id = attempt.id
    record_id = record.id
    db.session.commit()
    app.extensions["carlina_encryption"].key_store.rotate()
    db.session.remove()
    rotated_record = db.session.get(type(record), record_id)
    rotated_attempt = db.session.get(TranslationWindowAttempt, attempt_id)
    assert rotated_record is not None and rotated_attempt is not None
    assert (
        rehydrate_window_attempt(rotated_attempt, rotated_record).payload_hash
        == _packet().payload_hash
    )
    assert len(rehydrate_attempt_events(rotated_attempt)) == 4

    db.session.execute(
        text(
            "DELETE FROM translation_window_attempt_event WHERE attempt_id = :attempt "
            "AND sequence = 4"
        ),
        {"attempt": attempt_id},
    )
    db.session.commit()
    db.session.expire_all()
    with pytest.raises(
        BlockContractError, match="window-event-transcript|window-state-head"
    ):
        rehydrate_window_attempt(
            db.session.get(TranslationWindowAttempt, attempt_id), rotated_record
        )


def test_window_state_head_rejects_dispatch_and_staged_link_rewrites(app):
    _project, plan, record, operation = _state()
    window = _window(plan)

    def staged_attempt() -> TranslationWindowAttempt:
        attempt = persist_window_attempt(
            operation=operation,
            source_plan=record,
            window=window,
            packet=_packet(),
            settings={"quality": "high"},
            model_profile={"resolved": "configured/test"},
            admission=_admission(),
        )
        db.session.commit()
        result = {
            "results": [
                _candidate(plan, record, block).to_dict() for block in plan.blocks
            ]
        }
        execute_window_attempt(
            attempt,
            operation=operation,
            source_plan=record,
            window=window,
            provider_call=lambda _payload: result,
        )
        db.session.commit()
        return attempt

    first = staged_attempt()
    db.session.execute(
        text(
            "UPDATE translation_window_attempt SET dispatch_version = 99 WHERE id = :id"
        ),
        {"id": first.id},
    )
    db.session.commit()
    db.session.expire_all()
    with pytest.raises(BlockContractError, match="window-state-head"):
        rehydrate_window_attempt(
            db.session.get(TranslationWindowAttempt, first.id), record
        )

    second = staged_attempt()
    db.session.execute(
        text(
            "UPDATE translation_window_attempt_revision SET candidate_hash = :hash "
            "WHERE attempt_id = :attempt"
        ),
        {"hash": "0" * 64, "attempt": second.id},
    )
    db.session.commit()
    db.session.expire_all()
    with pytest.raises(BlockContractError, match="window-revision-link"):
        rehydrate_window_attempt(
            db.session.get(TranslationWindowAttempt, second.id), record
        )


def test_strict_and_native_normalization_require_exact_window_cardinality(app):
    _project, plan, record, _operation = _state()
    window = _window(plan)
    payload = {
        "results": [_candidate(plan, record, block).to_dict() for block in plan.blocks]
    }
    assert (
        len(
            normalize_window_response(
                payload, plan=plan, plan_id=record.id, window=window
            )
        )
        == 2
    )
    assert (
        len(
            normalize_window_response(
                json.dumps(payload), plan=plan, plan_id=record.id, window=window
            )
        )
        == 2
    )
    assert (
        len(
            normalize_window_response(
                f"```json\n{json.dumps(payload)}\n```",
                plan=plan,
                plan_id=record.id,
                window=window,
            )
        )
        == 2
    )
    with pytest.raises(BlockContractError, match="window-response-json"):
        normalize_window_response(
            f"Here is the result:\n```json\n{json.dumps(payload)}\n```",
            plan=plan,
            plan_id=record.id,
            window=window,
        )
    with pytest.raises(BlockContractError, match="window-response-cardinality"):
        normalize_window_response(
            {"results": payload["results"][:1]},
            plan=plan,
            plan_id=record.id,
            window=window,
        )

    compact_payload = {
        "results": [
            {"n": index, "content": block.projection, "fields": []}
            for index, block in enumerate(plan.blocks, 1)
        ]
    }
    assert len(
        normalize_window_response(
            compact_payload,
            plan=plan,
            plan_id=record.id,
            window=window,
            writable_source_contract_version=WRITABLE_SOURCE_CONTRACT_VERSION,
        )
    ) == len(plan.blocks)
    assert len(
        normalize_window_response(
            compact_payload,
            plan=plan,
            plan_id=record.id,
            window=window,
            writable_source_contract_version=(COMPACT_WRITABLE_SOURCE_CONTRACT_VERSION),
        )
    ) == len(plan.blocks)

    echoed_roles = {
        "results": [
            {**item, "role": block.role}
            for item, block in zip(compact_payload["results"], plan.blocks, strict=True)
        ]
    }
    assert len(
        normalize_window_response(
            echoed_roles,
            plan=plan,
            plan_id=record.id,
            window=window,
            writable_source_contract_version=WRITABLE_SOURCE_CONTRACT_VERSION,
        )
    ) == len(plan.blocks)
    echoed_roles["results"][0]["role"] = "model-authored-role"
    with pytest.raises(BlockContractError, match="window-response-contract"):
        normalize_window_response(
            echoed_roles,
            plan=plan,
            plan_id=record.id,
            window=window,
            writable_source_contract_version=WRITABLE_SOURCE_CONTRACT_VERSION,
        )
    live_like_payload = {
        "results": [
            {
                "n": index,
                "content": (
                    'Powiedział „termin", potem kontynuował.'
                    if index == 1
                    else block.projection
                ),
                "fields": [],
            }
            for index, block in enumerate(plan.blocks, 1)
        ]
    }
    serialized = json.dumps(live_like_payload, ensure_ascii=False)
    malformed_quote = serialized.replace('\\"', '"', 1)
    normalized_quotes = normalize_window_response(
        f"```json\n{malformed_quote}\n```",
        plan=plan,
        plan_id=record.id,
        window=window,
        writable_source_contract_version=WRITABLE_SOURCE_CONTRACT_VERSION,
    )
    assert len(normalized_quotes) == len(plan.blocks)
    assert tuple(
        part.text for part in normalized_quotes[0].parts if part.kind == "text"
    ) == ('Powiedział „termin", potem kontynuował.',)

    missing_delimiter = serialized.replace('1, "content"', '1 "content"', 1)
    with pytest.raises(BlockContractError, match="window-response-json"):
        normalize_window_response(
            missing_delimiter,
            plan=plan,
            plan_id=record.id,
            window=window,
            writable_source_contract_version=WRITABLE_SOURCE_CONTRACT_VERSION,
        )

    with pytest.raises(BlockContractError, match="window-response-contract"):
        normalize_window_response(
            compact_payload,
            plan=plan,
            plan_id=record.id,
            window=window,
            writable_source_contract_version=(PARTS_WRITABLE_SOURCE_CONTRACT_VERSION),
        )
    with pytest.raises(BlockContractError, match="window-response-contract"):
        normalize_window_response(
            payload,
            plan=plan,
            plan_id=record.id,
            window=window,
            writable_source_contract_version=WRITABLE_SOURCE_CONTRACT_VERSION,
        )
    with pytest.raises(BlockContractError, match="window-response-contract"):
        normalize_window_response(
            {
                "results": [
                    {**item, "notes": []} for item in compact_payload["results"]
                ]
            },
            plan=plan,
            plan_id=record.id,
            window=window,
            writable_source_contract_version=WRITABLE_SOURCE_CONTRACT_VERSION,
        )


def test_v6_window_normalization_allows_study_notes_and_enforces_study_caps(app):
    _project, plan, record, _operation = _state(
        "<html><body>"
        + "".join(f"<p>Source {index}.</p>" for index in range(1, 6))
        + "</body></html>"
    )
    accepted_blocks = plan.blocks[:4]
    accepted_window = TranslationWindow(
        1,
        tuple(block.id for block in accepted_blocks),
        tuple(structural_fingerprint(plan, block) for block in accepted_blocks),
    )
    results = []
    for index, _block in enumerate(accepted_blocks, 1):
        translated = f"Term {index} alpha beta gamma delta."
        results.append(
            {
                "n": index,
                "content": translated,
                "fields": [],
                "notes": [
                    {"anchor": anchor, "body": f"Study context for {anchor}."}
                    for anchor in ("Term", str(index), "alpha", "beta", "gamma", "delta")
                ],
            }
        )

    candidates, notes = normalize_window_response(
        {"results": results},
        plan=plan,
        plan_id=record.id,
        window=accepted_window,
        writable_source_contract_version=STUDY_NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
    )
    assert len(candidates) == len(accepted_blocks) == 4
    assert len(notes) == 24

    overflow_results = []
    for index, _block in enumerate(plan.blocks, 1):
        translated = f"Term {index} alpha beta gamma delta."
        overflow_results.append(
            {
                "n": index,
                "content": translated,
                "fields": [],
                "notes": [
                    {"anchor": anchor, "body": f"Study context for {anchor}."}
                    for anchor in ("Term", str(index), "alpha", "beta", "gamma")
                ],
            }
        )

    with pytest.raises(BlockContractError, match="block-note-window-cap"):
        normalize_window_response(
            {"results": overflow_results},
            plan=plan,
            plan_id=record.id,
            window=_window(plan),
            writable_source_contract_version=STUDY_NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
        )


def test_v4_window_normalization_restores_host_owned_outer_wrappers(app):
    _project, plan, record, _operation = _state(
        '<html><body><p><a href="#note"><em>Whole block.</em></a></p>'
        '<aside id="note" role="doc-footnote"><p>Note.</p></aside></body></html>'
    )
    candidates = normalize_window_response(
        {
            "results": [
                {"n": 1, "content": "Cały blok.", "fields": []},
                {"n": 2, "content": "Nota.", "fields": []},
            ]
        },
        plan=plan,
        plan_id=record.id,
        window=_window(plan),
        writable_source_contract_version=WRITABLE_SOURCE_CONTRACT_VERSION,
    )

    assert [part.kind for part in candidates[0].parts] == [
        "token",
        "token",
        "text",
        "token",
        "token",
    ]
    assert candidates[0].parts[2].text == "Cały blok."


def test_v5_window_normalization_enforces_note_cap_across_blocks(app):
    _project, plan, record, _operation = _state(
        "<html><body><p>One.</p><p>Two.</p><p>Three.</p><p>Four.</p></body></html>"
    )
    results = []
    for index, block in enumerate(plan.blocks, 1):
        translated = f"Termin {index}."
        results.append(
            {
                "n": index,
                "content": translated,
                "fields": [],
                "notes": [
                    {
                        "anchor": translated,
                        "body": f"Necessary cultural context {index}.",
                    }
                ],
            }
        )

    assert len(results) == 4
    with pytest.raises(BlockContractError, match="block-note-window-cap"):
        normalize_window_response(
            {"results": results},
            plan=plan,
            plan_id=record.id,
            window=_window(plan),
            writable_source_contract_version=NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
        )


@pytest.mark.parametrize(
    "writable_source_contract_version",
    [
        STUDY_NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
        SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION,
    ],
)
def test_frozen_study_profile_stages_rehydrates_and_recovers_without_provider_replay(
    app, monkeypatch, writable_source_contract_version
):
    project, plan, record, operation = _state(
        "<html><body><p>Source term.</p><p>Second.</p></body></html>"
    )
    window = _window(plan)
    packet = _notes_packet(plan, writable_source_contract_version)
    attempt = persist_window_attempt(
        operation=operation,
        source_plan=record,
        window=window,
        packet=packet,
        settings={"annotation_profile": block_notes.ANNOTATION_PROFILE_STUDY},
        model_profile={"resolved": "configured/test"},
        admission=_admission(),
    )
    db.session.commit()

    project.settings = {
        **project.settings,
        "annotation_profile": block_notes.ANNOTATION_PROFILE_NONE,
        "enable_translator_notes": False,
    }
    db.session.commit()
    response = _notes_response(plan, writable_source_contract_version)
    original_persist = window_execution.persist_target_revision
    persist_calls = 0
    provider_calls = 0

    def fail_later_candidate(*args, **kwargs):
        nonlocal persist_calls
        persist_calls += 1
        if persist_calls == 2:
            raise BlockContractError("forced-staging-failure")
        return original_persist(*args, **kwargs)

    def provider(_payload):
        nonlocal provider_calls
        provider_calls += 1
        return response

    monkeypatch.setattr(
        window_execution, "persist_target_revision", fail_later_candidate
    )
    with pytest.raises(BlockContractError, match="forced-staging-failure"):
        execute_window_attempt(
            attempt,
            operation=operation,
            source_plan=record,
            window=window,
            provider_call=provider,
        )
    assert provider_calls == 1
    assert db.session.query(TranslationTargetBlockRevision).count() == 0

    db.session.expire_all()
    stored = db.session.get(TranslationWindowAttempt, attempt.id)
    assert stored is not None and stored.dispatch_state == "staging_failed"
    monkeypatch.setattr(window_execution, "persist_target_revision", original_persist)
    revision_ids = window_execution.resume_window_attempt(
        stored,
        operation=operation,
        source_plan=record,
        window=window,
    )
    assert len(revision_ids) == 2
    assert provider_calls == 1

    first_revision = db.session.get(TranslationTargetBlockRevision, revision_ids[0])
    assert first_revision is not None
    rehydrated = rehydrate_target_block_notes(first_revision, record)
    assert [note.order_index for note in rehydrated] == list(range(6))
    assert [note.anchor_text for note in rehydrated] == [
        "Term",
        "alpha",
        "beta",
        "gamma",
        "delta",
        "epsilon",
    ]
    assert db.session.query(TranslationTargetBlockNote).count() == 6


def test_coordinator_stages_all_revisions_without_activation(app):
    _project, plan, record, operation = _state()
    window = _window(plan)
    attempt = persist_window_attempt(
        operation=operation,
        source_plan=record,
        window=window,
        packet=_packet(),
        settings={"quality": "high"},
        model_profile={"resolved": "configured/test"},
        admission=_admission(),
    )
    db.session.commit()
    calls = 0

    def provider(_payload):
        nonlocal calls
        calls += 1
        return {
            "results": [
                _candidate(plan, record, block).to_dict() for block in plan.blocks
            ]
        }

    revision_ids = execute_window_attempt(
        attempt,
        operation=operation,
        source_plan=record,
        window=window,
        provider_call=provider,
    )
    assert calls == 1 and len(revision_ids) == len(plan.blocks)
    assert db.session.query(TranslationTargetBlockActive).count() == 0
    assert db.session.query(TranslationWindowAttemptRevision).count() == len(
        plan.blocks
    )
    stored = db.session.execute(
        text("SELECT packet_json FROM translation_window_attempt WHERE id = :id"),
        {"id": attempt.id},
    ).scalar_one()
    assert str(stored).startswith("enc:v2:")


def test_staging_reuses_one_verified_source_plan_for_every_candidate(app, monkeypatch):
    _project, plan, record, operation = _state()
    window = _window(plan)
    attempt = persist_window_attempt(
        operation=operation,
        source_plan=record,
        window=window,
        packet=_packet(),
        settings={"quality": "high"},
        model_profile={"resolved": "configured/test"},
        admission=_admission(),
    )
    db.session.commit()

    rehydrates: list[str] = []
    original_window_rehydrate = window_execution.rehydrate_source_plan
    original_target_rehydrate = target_persistence.rehydrate_source_plan

    def counted_window_rehydrate(source_plan):
        rehydrates.append("window")
        return original_window_rehydrate(source_plan)

    def counted_target_rehydrate(source_plan):
        rehydrates.append("target")
        return original_target_rehydrate(source_plan)

    # Set up the durable attempt before instrumentation so this count covers
    # only response staging and its per-candidate persistence calls.
    monkeypatch.setattr(
        window_execution, "rehydrate_source_plan", counted_window_rehydrate
    )
    monkeypatch.setattr(
        target_persistence, "rehydrate_source_plan", counted_target_rehydrate
    )
    execute_window_attempt(
        attempt,
        operation=operation,
        source_plan=record,
        window=window,
        provider_call=lambda _payload: {
            "results": [
                _candidate(plan, record, block).to_dict() for block in plan.blocks
            ]
        },
    )

    assert rehydrates == ["window"]


def test_finalization_reuses_one_verified_source_plan_for_every_candidate(
    app, monkeypatch
):
    _project, plan, record, operation = _state()
    window = _window(plan)
    attempt = persist_window_attempt(
        operation=operation,
        source_plan=record,
        window=window,
        packet=_packet(),
        settings={"quality": "high"},
        model_profile={"resolved": "configured/test"},
        admission=_admission(),
    )
    db.session.commit()
    execute_window_attempt(
        attempt,
        operation=operation,
        source_plan=record,
        window=window,
        provider_call=lambda _payload: {
            "results": [
                _candidate(plan, record, block).to_dict() for block in plan.blocks
            ]
        },
    )
    db.session.expire_all()
    staged = db.session.get(TranslationWindowAttempt, attempt.id)
    assert staged is not None and staged.dispatch_state == "staged"
    db.session.rollback()
    evidence = VerifiedWindowEvidence.from_verified_baseline(
        operation=operation,
        source_plan=record,
        plan=plan,
        baseline_slots=(),
    )
    db.session.rollback()

    rehydrates: list[str] = []
    original_runtime_rehydrate = block_window_runtime.rehydrate_source_plan
    original_alignment_rehydrate = alignment_persistence.rehydrate_source_plan
    original_target_rehydrate = target_persistence.rehydrate_source_plan

    def counted_runtime_rehydrate(source_plan):
        rehydrates.append("runtime")
        return original_runtime_rehydrate(source_plan)

    def counted_alignment_rehydrate(source_plan):
        rehydrates.append("alignment")
        return original_alignment_rehydrate(source_plan)

    def counted_target_rehydrate(source_plan):
        rehydrates.append("target")
        return original_target_rehydrate(source_plan)

    # The invocation-local evidence already proves the immutable source.
    # Finalization rechecks its binding and all mutable dependencies, without
    # repeating a whole-document rehydration for each recovered window.
    monkeypatch.setattr(
        block_window_runtime,
        "rehydrate_source_plan",
        counted_runtime_rehydrate,
    )
    monkeypatch.setattr(
        alignment_persistence,
        "rehydrate_source_plan",
        counted_alignment_rehydrate,
    )
    monkeypatch.setattr(
        target_persistence,
        "rehydrate_source_plan",
        counted_target_rehydrate,
    )
    receipt = block_window_runtime.finalize_staged_window_attempt(
        staged,
        operation=operation,
        source_plan=record,
        window=window,
        verified_evidence=evidence,
    )

    assert receipt.block_ids == window.block_ids
    assert rehydrates == []


def test_target_validation_failure_is_a_retryable_normalization_state(app):
    _project, plan, record, operation = _state()
    window = _window(plan)
    attempt = persist_window_attempt(
        operation=operation,
        source_plan=record,
        window=window,
        packet=_packet(),
        settings={"quality": "high"},
        model_profile={"resolved": "configured/test"},
        admission=_admission(),
    )
    db.session.commit()
    results = [_candidate(plan, record, block).to_dict() for block in plan.blocks]
    results[0]["structural_fingerprint"] = "0" * 64

    with pytest.raises(BlockContractError, match="target-validation-failed"):
        execute_window_attempt(
            attempt,
            operation=operation,
            source_plan=record,
            window=window,
            provider_call=lambda _payload: {"results": results},
        )

    db.session.expire_all()
    stored = db.session.get(TranslationWindowAttempt, attempt.id)
    assert stored is not None
    assert stored.dispatch_state == "normalization_failed"
    assert rehydrate_attempt_events(stored)[-1].payload_json == {
        "code": "target-validation-failed",
        "finding_codes": ["target-structural-fingerprint"],
    }


def test_predecessor_snapshot_rejects_missing_or_mismatched_alignment_witness(app):
    _project, plan, record, _operation = _state()
    revision = persist_target_revision(
        record,
        _candidate(plan, record, plan.blocks[0]),
        target_language="pl",
        author_type="system",
    )
    alignment = persist_alignment_revision(
        record, persist_target_span_set(record, revision)
    )
    activate_target_revision(
        record,
        block_id=revision.block_id,
        revision_id=revision.id,
        alignment_revision_id=alignment.id,
        expected_version=0,
    )
    assert (
        predecessor_snapshot(record, _window(plan))[revision.block_id]["revision_id"]
        == revision.id
    )
    pointer = db.session.get(
        TranslationTargetAlignmentActive,
        {"source_plan_id": record.id, "block_id": revision.block_id},
    )
    assert pointer is not None
    db.session.delete(pointer)
    db.session.flush()
    with pytest.raises(BlockContractError, match="window-predecessor-alignment"):
        predecessor_snapshot(record, _window(plan))


def test_window_dependency_reads_reject_tampered_active_pair_receipt(app):
    _project, plan, record, _operation = _state()
    revision = persist_target_revision(
        record,
        _candidate(plan, record, plan.blocks[0]),
        target_language="pl",
        author_type="system",
    )
    alignment = persist_alignment_revision(
        record, persist_target_span_set(record, revision)
    )
    pointer = activate_target_revision(
        record,
        block_id=revision.block_id,
        revision_id=revision.id,
        alignment_revision_id=alignment.id,
        expected_version=0,
    )
    db.session.commit()
    db.session.execute(
        text(
            "UPDATE translation_target_block_active SET updated_at = :timestamp "
            "WHERE source_plan_id = :plan AND block_id = :block"
        ),
        {
            "timestamp": pointer.updated_at - timedelta(microseconds=1),
            "plan": record.id,
            "block": revision.block_id,
        },
    )
    db.session.commit()
    db.session.expire_all()
    with pytest.raises(BlockContractError, match="window-predecessor-alignment"):
        predecessor_snapshot(record, _window(plan))


def test_independent_dispatch_does_not_flush_or_read_dirty_caller_state(app):
    _project, plan, record, operation = _state()
    predecessor = plan.blocks[0]
    revision = persist_target_revision(
        record,
        _candidate(plan, record, predecessor, "Earlier."),
        target_language="pl",
        author_type="system",
    )
    alignment = persist_alignment_revision(
        record, persist_target_span_set(record, revision)
    )
    activate_target_revision(
        record,
        block_id=predecessor.id,
        revision_id=revision.id,
        alignment_revision_id=alignment.id,
        expected_version=0,
    )
    window = _window(plan)
    attempt = persist_window_attempt(
        operation=operation,
        source_plan=record,
        window=window,
        packet=_packet(),
        settings={"quality": "high"},
        model_profile={"resolved": "configured/test"},
        admission=_admission(),
    )
    db.session.commit()
    attempt_id, record_id, operation_id = attempt.id, record.id, operation.id
    persisted_queue_job_id = operation.queue_job_id
    operation.queue_job_id = "unflushed-caller-mutation"
    assert db.session.is_modified(operation)
    calls = 0
    errors: list[Exception] = []

    def provider(_payload):
        nonlocal calls
        calls += 1
        return {
            "results": [
                _candidate(plan, record, block).to_dict() for block in plan.blocks
            ]
        }

    def run_independently():
        with app.app_context():
            try:
                local_attempt = db.session.get(TranslationWindowAttempt, attempt_id)
                local_record = db.session.get(type(record), record_id)
                local_operation = db.session.get(TranslationOperation, operation_id)
                assert local_attempt and local_record and local_operation
                execute_window_attempt(
                    local_attempt,
                    operation=local_operation,
                    source_plan=local_record,
                    window=window,
                    provider_call=provider,
                )
            except (BlockContractError, SQLAlchemyError) as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                db.session.remove()

    worker = threading.Thread(target=run_independently, daemon=True)
    worker.start()
    worker.join(timeout=10)
    assert not worker.is_alive() and not errors
    assert calls == 1
    assert operation.queue_job_id == "unflushed-caller-mutation"
    assert db.session.is_modified(operation)
    with db.session.no_autoflush:
        queue_job_id = db.session.execute(
            text("SELECT queue_job_id FROM translation_operation WHERE id = :id"),
            {"id": operation_id},
        ).scalar_one()
    assert queue_job_id == persisted_queue_job_id
    db.session.rollback()


def test_independent_dependency_fence_ignores_stale_caller_pointer_cache(app):
    _project, plan, record, operation = _state()
    revision = persist_target_revision(
        record,
        _candidate(plan, record, plan.blocks[0], "Earlier."),
        target_language="pl",
        author_type="system",
    )
    alignment = persist_alignment_revision(
        record, persist_target_span_set(record, revision)
    )
    activate_target_revision(
        record,
        block_id=revision.block_id,
        revision_id=revision.id,
        alignment_revision_id=alignment.id,
        expected_version=0,
    )
    window = _window(plan)
    attempt = persist_window_attempt(
        operation=operation,
        source_plan=record,
        window=window,
        packet=_packet(),
        settings={"quality": "high"},
        model_profile={"resolved": "configured/test"},
        admission=_admission(),
    )
    db.session.commit()
    cached_pointer = db.session.get(
        TranslationTargetBlockActive,
        {"source_plan_id": record.id, "block_id": revision.block_id},
    )
    assert cached_pointer is not None
    original_timestamp = cached_pointer.updated_at
    independent = sessionmaker(bind=db.engine, expire_on_commit=False)()
    try:
        independent.execute(
            text(
                "UPDATE translation_target_block_active SET updated_at = :timestamp "
                "WHERE source_plan_id = :plan AND block_id = :block"
            ),
            {
                "timestamp": original_timestamp - timedelta(microseconds=1),
                "plan": record.id,
                "block": revision.block_id,
            },
        )
        independent.commit()
    finally:
        independent.close()
    assert cached_pointer.updated_at == original_timestamp
    with pytest.raises(BlockContractError, match="window-dependency-alignment"):
        window_execution._fresh_dependency_fence(attempt.id, record.id, window)
    assert cached_pointer.updated_at == original_timestamp


def test_post_response_dependency_fence_rejects_removed_alignment_witness(app):
    _project, plan, record, operation = _state()
    revision = persist_target_revision(
        record,
        _candidate(plan, record, plan.blocks[0]),
        target_language="pl",
        author_type="system",
    )
    alignment = persist_alignment_revision(
        record, persist_target_span_set(record, revision)
    )
    activate_target_revision(
        record,
        block_id=revision.block_id,
        revision_id=revision.id,
        alignment_revision_id=alignment.id,
        expected_version=0,
    )
    window = _window(plan)
    attempt = persist_window_attempt(
        operation=operation,
        source_plan=record,
        window=window,
        packet=_packet(),
        settings={"quality": "high"},
        model_profile={"resolved": "configured/test"},
        admission=_admission(),
    )
    db.session.commit()

    def provider(_payload):
        pointer = db.session.get(
            TranslationTargetAlignmentActive,
            {"source_plan_id": record.id, "block_id": revision.block_id},
        )
        assert pointer is not None
        db.session.delete(pointer)
        db.session.commit()
        return {
            "results": [
                _candidate(plan, record, block).to_dict() for block in plan.blocks
            ]
        }

    with pytest.raises(BlockContractError, match="window-dependency-alignment"):
        execute_window_attempt(
            attempt,
            operation=operation,
            source_plan=record,
            window=window,
            provider_call=provider,
        )


@pytest.mark.parametrize(
    "context_type", ["continuity", "exact_precedent", "sentence_precedent", "linked_notes"]
)
def test_final_staging_serializes_context_pointer_activation(app, monkeypatch, context_type):
    _project, plan, record, operation = _state()
    predecessor, writable = plan.blocks
    initial = persist_target_revision(
        record,
        _candidate(plan, record, predecessor, "Poprzedni."),
        target_language="pl",
        author_type="system",
    )
    replacement = persist_target_revision(
        record,
        _candidate(plan, record, predecessor, "Zmieniony."),
        target_language="pl",
        author_type="system",
    )
    initial_alignment = persist_alignment_revision(
        record, persist_target_span_set(record, initial)
    )
    replacement_alignment = persist_alignment_revision(
        record, persist_target_span_set(record, replacement)
    )
    activate_target_revision(
        record,
        block_id=predecessor.id,
        revision_id=initial.id,
        alignment_revision_id=initial_alignment.id,
        expected_version=0,
    )
    db.session.commit()
    pointer = db.session.get(
        TranslationTargetBlockActive,
        {"source_plan_id": record.id, "block_id": predecessor.id},
    )
    assert pointer is not None and pointer.version == 1

    window = TranslationWindow(
        1,
        (writable.id,),
        (structural_fingerprint(plan, writable),),
    )
    context_target = {
        "revision_id": initial.id,
        "revision_record_hash": initial.revision_record_hash,
        "active_pointer_version": 1,
    }
    context_dependency = (
        {"predecessor_block_id": predecessor.id, **context_target}
        if context_type == "continuity"
        else {
            "source_plan_id": record.id,
            "active_targets": [{"block_id": predecessor.id, **context_target}],
        }
    )
    packet = render_provider_packet(
        slots=(
            _slot(
                "control_contract",
                "Return the exact DTO.",
                {"contract_version": "translation-block-window-prompt-v2"},
            ),
            _slot(
                "project_policy",
                "Polish quality translation.",
                {"policy": {"target_language": "pl", "register": "formal"}},
            ),
            _slot(
                context_type,
                "Poprzedni.",
                {"id": "active-predecessor"},
                context_dependency,
            ),
            _slot(
                "writable_source",
                json.dumps(
                    {
                        "contract_version": "translation-block-writable-source-v1",
                        "source_plan": {"id": record.id, "plan_hash": plan.plan_hash},
                        "window": {
                            "order": window.order,
                            "block_ids": [writable.id],
                            "block_fingerprints": [
                                structural_fingerprint(plan, writable)
                            ],
                        },
                        "blocks": [
                            {
                                "block": writable.to_dict(),
                                "structural_fingerprint": structural_fingerprint(
                                    plan, writable
                                ),
                            }
                        ],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                {"block_ids": [writable.id]},
            ),
        ),
        target_language="pl",
        requested_output_tokens=512,
        native_json_schema=True,
    )
    attempt = persist_window_attempt(
        operation=operation,
        source_plan=record,
        window=window,
        packet=packet,
        settings={"quality": "high"},
        model_profile={"resolved": "configured/test"},
        admission=_admission(),
    )
    db.session.commit()

    lock_attempted = threading.Event()
    activation_finished = threading.Event()
    activation_errors: list[Exception] = []
    activation_thread: list[threading.Thread] = []
    original_lock = target_persistence.lock_target_dependency_scope
    original_persist = window_execution.persist_target_revision

    def observed_activation_lock(source_plan_id):
        lock_attempted.set()
        return original_lock(source_plan_id)

    monkeypatch.setattr(
        target_persistence,
        "lock_target_dependency_scope",
        observed_activation_lock,
    )

    def activate_replacement():
        with app.app_context():
            try:
                local_record = db.session.get(type(record), record.id)
                activate_target_revision(
                    local_record,
                    block_id=predecessor.id,
                    revision_id=replacement.id,
                    alignment_revision_id=replacement_alignment.id,
                    expected_version=1,
                )
                db.session.commit()
            except (BlockContractError, SQLAlchemyError) as exc:  # pragma: no cover
                activation_errors.append(exc)
                db.session.rollback()
            finally:
                db.session.remove()
                activation_finished.set()

    interleaved = False

    def persist_with_competing_activation(*args, **kwargs):
        nonlocal interleaved
        if not interleaved:
            interleaved = True
            thread = threading.Thread(target=activate_replacement, daemon=True)
            activation_thread.append(thread)
            thread.start()
            assert lock_attempted.wait(timeout=2)
            # Staging already owns the shared plan lock, so activation cannot
            # commit until this staging transaction has committed or rolled back.
            assert not activation_finished.wait(timeout=0.1)
        return original_persist(*args, **kwargs)

    monkeypatch.setattr(
        window_execution,
        "persist_target_revision",
        persist_with_competing_activation,
    )
    response = {"results": [_candidate(plan, record, writable).to_dict()]}
    revision_ids = execute_window_attempt(
        attempt,
        operation=operation,
        source_plan=record,
        window=window,
        provider_call=lambda _payload: response,
    )
    assert len(revision_ids) == 1
    activation_thread[0].join(timeout=5)
    assert activation_finished.is_set() and not activation_errors
    db.session.expire_all()
    stored_attempt = db.session.get(type(attempt), attempt.id)
    stored_pointer = db.session.get(
        TranslationTargetBlockActive,
        {"source_plan_id": record.id, "block_id": predecessor.id},
    )
    assert stored_attempt.dispatch_state == "staged"
    assert stored_pointer.revision_id == replacement.id
    assert stored_pointer.version == 2


def test_dispatch_is_durable_and_response_failures_are_not_replayed(app):
    _project, plan, record, operation = _state()
    window = _window(plan)
    attempt = persist_window_attempt(
        operation=operation,
        source_plan=record,
        window=window,
        packet=_packet(),
        settings={"quality": "high"},
        model_profile={"resolved": "configured/test"},
        admission=_admission(),
    )
    with pytest.raises(BlockContractError, match="window-attempt-uncommitted"):
        execute_window_attempt(
            attempt,
            operation=operation,
            source_plan=record,
            window=window,
            provider_call=lambda _payload: {"results": []},
        )
    db.session.rollback()
    attempt = persist_window_attempt(
        operation=operation,
        source_plan=record,
        window=window,
        packet=_packet(),
        settings={"quality": "high"},
        model_profile={"resolved": "configured/test"},
        admission=_admission(),
    )
    db.session.commit()
    calls = 0

    def malformed(_payload):
        nonlocal calls
        calls += 1
        return "not json"

    with pytest.raises(BlockContractError, match="window-response-json"):
        execute_window_attempt(
            attempt,
            operation=operation,
            source_plan=record,
            window=window,
            provider_call=malformed,
        )
    assert calls == 1
    db.session.expire_all()
    stored = db.session.get(type(attempt), attempt.id)
    assert stored.dispatch_state == "normalization_failed"
    assert {event.event_type for event in rehydrate_attempt_events(stored)} >= {
        "provider_response",
        "normalization_failed",
    }
    assert rehydrate_attempt_events(stored)[-1].payload_json == {
        "code": "window-response-json"
    }
    with pytest.raises(BlockContractError, match="window-dispatch-replay"):
        execute_window_attempt(
            stored,
            operation=operation,
            source_plan=record,
            window=window,
            provider_call=malformed,
        )


def test_provider_failure_leaves_a_verifiable_terminal_state_head(app):
    _project, plan, record, operation = _state()
    window = _window(plan)
    attempt = persist_window_attempt(
        operation=operation,
        source_plan=record,
        window=window,
        packet=_packet(),
        settings={"quality": "high"},
        model_profile={"resolved": "configured/test"},
        admission=_admission(),
    )
    db.session.commit()
    with pytest.raises(RuntimeError, match="provider boom"):
        execute_window_attempt(
            attempt,
            operation=operation,
            source_plan=record,
            window=window,
            provider_call=lambda _payload: (_ for _ in ()).throw(
                RuntimeError("provider boom")
            ),
        )
    db.session.expire_all()
    stored = db.session.get(TranslationWindowAttempt, attempt.id)
    assert stored is not None and stored.dispatch_state == "provider_failed"
    assert (
        rehydrate_window_attempt(stored, record).payload_hash == _packet().payload_hash
    )


def test_passive_dispatch_polls_same_external_work_after_queue_reentry(app):
    _project, plan, record, operation = _state()
    window = _window(plan)
    attempt = persist_window_attempt(
        operation=operation,
        source_plan=record,
        window=window,
        packet=_packet(),
        settings={"quality": "high"},
        model_profile={"resolved": "external/test"},
        admission=_admission(),
    )
    db.session.commit()

    def pending(_payload):
        raise ExternalWorkPending(
            "Waiting for external model output.",
            delay_seconds=5,
            work_item_id="external-work-1",
        )

    with pytest.raises(ExternalWorkPending, match="external model output"):
        execute_window_attempt(
            attempt,
            operation=operation,
            source_plan=record,
            window=window,
            provider_call=pending,
        )

    db.session.expire_all()
    stored = db.session.get(TranslationWindowAttempt, attempt.id)
    assert stored is not None and stored.dispatch_state == "external_pending"
    assert [event.event_type for event in rehydrate_attempt_events(stored)] == [
        "request_prepared",
        "provider_dispatched",
        "external_work_pending",
    ]

    # The queue deliberately retries the same delivery and the operation claim
    # advances its lease generation. The attempt must poll its already-created
    # work item rather than becoming an uncertain managed-provider dispatch.
    reentered = claim_operation(
        operation.id,
        "window-worker",
        owner_user_id=operation.owner_user_id,
        project_id=operation.project_id,
    )
    db.session.commit()
    assert reentered.version > stored.operation_version

    response = {
        "results": [_candidate(plan, record, block).to_dict() for block in plan.blocks]
    }
    revisions = execute_window_attempt(
        stored,
        operation=reentered,
        source_plan=record,
        window=window,
        provider_call=lambda _payload: response,
        allow_external_reentry=True,
    )
    assert len(revisions) == len(plan.blocks)
    db.session.expire_all()
    completed = db.session.get(TranslationWindowAttempt, attempt.id)
    assert completed is not None and completed.dispatch_state == "staged"
    assert [event.event_type for event in rehydrate_attempt_events(completed)] == [
        "request_prepared",
        "provider_dispatched",
        "external_work_pending",
        "provider_response",
        "revisions_staged",
    ]


def test_passive_reentry_recovers_crash_after_dispatch_before_pending_state(app):
    _project, plan, record, operation = _state()
    window = _window(plan)
    attempt = persist_window_attempt(
        operation=operation,
        source_plan=record,
        window=window,
        packet=_packet(),
        settings={"quality": "high"},
        model_profile={"resolved": "external/test"},
        admission=_admission(),
    )
    db.session.commit()

    # Model the process-death boundary precisely: the durable dispatch claim was
    # committed, but the passive adapter's pending signal was never recorded.
    window_execution._claim_dispatch(
        attempt.id,
        operation.id,
        operation.version,
        operation.queue_job_id,
    )
    db.session.expire_all()
    dispatched = db.session.get(TranslationWindowAttempt, attempt.id)
    assert dispatched is not None and dispatched.dispatch_state == "dispatched"

    reentered = claim_operation(
        operation.id,
        "window-worker",
        owner_user_id=operation.owner_user_id,
        project_id=operation.project_id,
    )
    db.session.commit()
    with pytest.raises(ExternalWorkPending):
        execute_window_attempt(
            dispatched,
            operation=reentered,
            source_plan=record,
            window=window,
            provider_call=lambda _payload: (_ for _ in ()).throw(
                ExternalWorkPending(
                    "Existing external work is still pending.",
                    delay_seconds=5,
                    work_item_id="external-work-after-crash",
                )
            ),
            allow_external_reentry=True,
        )

    db.session.expire_all()
    recovered = db.session.get(TranslationWindowAttempt, attempt.id)
    assert recovered is not None and recovered.dispatch_state == "external_pending"
    assert [event.event_type for event in rehydrate_attempt_events(recovered)] == [
        "request_prepared",
        "provider_dispatched",
        "external_work_pending",
    ]


def test_two_independent_dispatchers_call_provider_once(app):
    _project, plan, record, operation = _state()
    window = _window(plan)
    attempt = persist_window_attempt(
        operation=operation,
        source_plan=record,
        window=window,
        packet=_packet(),
        settings={"quality": "high"},
        model_profile={"resolved": "configured/test"},
        admission=_admission(),
    )
    db.session.commit()
    attempt_id, record_id, operation_id = attempt.id, record.id, operation.id
    barrier = threading.Barrier(2)
    counter_lock = threading.Lock()
    calls = 0
    outcomes: list[str] = []

    def provider(_payload):
        nonlocal calls
        with counter_lock:
            calls += 1
        return {
            "results": [
                _candidate(plan, record, block).to_dict() for block in plan.blocks
            ]
        }

    def runner():
        with app.app_context():
            try:
                local_attempt = db.session.get(TranslationWindowAttempt, attempt_id)
                local_record = db.session.get(type(record), record_id)
                local_operation = db.session.get(TranslationOperation, operation_id)
                assert local_attempt and local_record and local_operation
                execute_window_attempt(
                    local_attempt,
                    operation=local_operation,
                    source_plan=local_record,
                    window=window,
                    provider_call=provider,
                    before_dispatch_claim=lambda: barrier.wait(timeout=5),
                )
                outcomes.append("winner")
            except BlockContractError as exc:
                outcomes.append(str(exc))
            finally:
                db.session.remove()

    threads = [threading.Thread(target=runner, daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert calls == 1
    assert outcomes.count("winner") == 1
    assert any(item == "window-dispatch-replay" for item in outcomes)
    db.session.expire_all()
    stored = db.session.get(TranslationWindowAttempt, attempt_id)
    assert stored is not None and stored.dispatch_state == "staged"
    assert (
        rehydrate_window_attempt(stored, record).payload_hash == _packet().payload_hash
    )


def test_independent_event_writer_rollback_is_atomic(app):
    _project, plan, record, operation = _state()
    attempt = persist_window_attempt(
        operation=operation,
        source_plan=record,
        window=_window(plan),
        packet=_packet(),
        settings={"quality": "high"},
        model_profile={"resolved": "configured/test"},
        admission=_admission(),
    )
    db.session.commit()
    attempt_id = attempt.id
    original_state_head = attempt.state_head_receipt

    independent = sessionmaker(bind=db.engine, expire_on_commit=False)()
    try:
        local = independent.get(TranslationWindowAttempt, attempt_id)
        assert local is not None
        append_attempt_event(
            local,
            "provider_dispatched",
            {"rollback_probe": True},
            session=independent,
        )
        assert (
            independent.execute(
                text(
                    "SELECT COUNT(*) FROM translation_window_attempt_event "
                    "WHERE attempt_id = :attempt_id"
                ),
                {"attempt_id": attempt_id},
            ).scalar_one()
            == 2
        )
        independent.rollback()
    finally:
        independent.close()

    db.session.expire_all()
    stored = db.session.get(TranslationWindowAttempt, attempt_id)
    assert stored is not None
    assert stored.state_head_receipt == original_state_head
    assert (
        db.session.execute(
            text(
                "SELECT COUNT(*) FROM translation_window_attempt_event "
                "WHERE attempt_id = :attempt_id"
            ),
            {"attempt_id": attempt_id},
        ).scalar_one()
        == 1
    )


def test_two_independent_event_writers_get_a_typed_sequence_conflict(app, monkeypatch):
    _project, plan, record, operation = _state()
    attempt = persist_window_attempt(
        operation=operation,
        source_plan=record,
        window=_window(plan),
        packet=_packet(),
        settings={"quality": "high"},
        model_profile={"resolved": "configured/test"},
        admission=_admission(),
    )
    db.session.commit()
    attempt_id = attempt.id
    barrier = threading.Barrier(2)
    original_now = window_execution.utc_now_db

    def synchronized_now():
        if threading.current_thread().name.startswith("event-racer"):
            barrier.wait(timeout=5)
        return original_now()

    monkeypatch.setattr(
        "iarf_assistant.addons.translator.window_persistence.utc_now_db",
        synchronized_now,
    )
    outcomes: list[str] = []

    def runner():
        with app.app_context():
            session = sessionmaker(bind=db.engine, expire_on_commit=False)()
            try:
                local = session.get(TranslationWindowAttempt, attempt_id)
                assert local is not None
                append_attempt_event(
                    local, "provider_dispatched", {"writer": "same"}, session=session
                )
                session.commit()
                outcomes.append("winner")
            except BlockContractError as exc:
                session.rollback()
                outcomes.append(str(exc))
            finally:
                session.close()

    threads = [
        threading.Thread(target=runner, daemon=True, name=f"event-racer-{index}")
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert outcomes.count("winner") == 1
    assert outcomes.count("window-event-conflict") == 1
    db.session.expire_all()
    stored = db.session.get(TranslationWindowAttempt, attempt_id)
    assert stored is not None
    stored.dispatch_state = "dispatched"
    stored.dispatch_version += 1
    stored.dispatch_claimed_at = utc_now_db()
    seal_attempt_state_head(stored)
    db.session.commit()
    assert [event.event_type for event in rehydrate_attempt_events(stored)] == [
        "request_prepared",
        "provider_dispatched",
    ]


def test_post_response_cancellation_is_durable_and_never_stages(app):
    _project, plan, record, operation = _state()
    window = _window(plan)
    attempt = persist_window_attempt(
        operation=operation,
        source_plan=record,
        window=window,
        packet=_packet(),
        settings={"quality": "high"},
        model_profile={"resolved": "configured/test"},
        admission=_admission(),
    )
    db.session.commit()

    def cancel_after_provider(_payload):
        request_cancellation(operation, expected_version=operation.version)
        db.session.commit()
        return {
            "results": [
                _candidate(plan, record, block).to_dict() for block in plan.blocks
            ]
        }

    with pytest.raises(TranslationOperationCancellation):
        execute_window_attempt(
            attempt,
            operation=operation,
            source_plan=record,
            window=window,
            provider_call=cancel_after_provider,
        )
    db.session.expire_all()
    stored = db.session.get(type(attempt), attempt.id)
    assert stored.dispatch_state == "fence_failed"
    assert db.session.query(TranslationTargetBlockRevision).count() == 0
    assert {event.event_type for event in rehydrate_attempt_events(stored)} >= {
        "provider_response",
        "post_response_fence_failed",
    }


def test_coordinator_stages_revisions_all_or_nothing(app, monkeypatch):
    _project, plan, record, operation = _state()
    window = _window(plan)
    attempt = persist_window_attempt(
        operation=operation,
        source_plan=record,
        window=window,
        packet=_packet(),
        settings={"quality": "high"},
        model_profile={"resolved": "configured/test"},
        admission=_admission(),
    )
    db.session.commit()
    original = window_execution.persist_target_revision
    staged = 0

    def fail_second(*args, **kwargs):
        nonlocal staged
        staged += 1
        if staged == 2:
            raise BlockContractError("forced-staging-failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(window_execution, "persist_target_revision", fail_second)
    provider_calls = 0

    def provider(_payload):
        nonlocal provider_calls
        provider_calls += 1
        return {
            "results": [
                _candidate(plan, record, block).to_dict() for block in plan.blocks
            ]
        }

    with pytest.raises(BlockContractError, match="forced-staging-failure"):
        execute_window_attempt(
            attempt,
            operation=operation,
            source_plan=record,
            window=window,
            provider_call=provider,
        )
    assert db.session.query(TranslationTargetBlockActive).count() == 0
    assert db.session.query(TranslationTargetBlockRevision).count() == 0
    assert db.session.query(TranslationWindowAttemptRevision).count() == 0
    db.session.expire_all()
    stored = db.session.get(type(attempt), attempt.id)
    assert stored.dispatch_state == "staging_failed"
    assert provider_calls == 1
    monkeypatch.setattr(window_execution, "persist_target_revision", original)
    revision_ids = window_execution.resume_window_attempt(
        stored,
        operation=operation,
        source_plan=record,
        window=window,
    )
    assert len(revision_ids) == len(plan.blocks)
    assert provider_calls == 1
    db.session.expire_all()
    recovered = db.session.get(type(attempt), attempt.id)
    assert recovered is not None
    assert [event.event_type for event in rehydrate_attempt_events(recovered)] == [
        "request_prepared",
        "provider_dispatched",
        "provider_response",
        "staging_failure",
        "revisions_staged",
    ]


def test_dispatch_claim_rechecks_cancellation_inside_conditional_cas(app):
    _project, plan, record, operation = _state()
    window = _window(plan)
    attempt = persist_window_attempt(
        operation=operation,
        source_plan=record,
        window=window,
        packet=_packet(),
        settings={"quality": "high"},
        model_profile={"resolved": "configured/test"},
        admission=_admission(),
    )
    db.session.commit()
    calls = 0

    def cancel_at_barrier():
        request_cancellation(operation, expected_version=operation.version)
        db.session.commit()

    def provider(_payload):
        nonlocal calls
        calls += 1
        return {"results": []}

    with pytest.raises(TranslationOperationCancellation):
        execute_window_attempt(
            attempt,
            operation=operation,
            source_plan=record,
            window=window,
            provider_call=provider,
            before_dispatch_claim=cancel_at_barrier,
        )
    assert calls == 0


def test_coordinator_checks_cancellation_and_source_fences_before_provider(app):
    _project, plan, record, operation = _state()
    window = _window(plan)
    attempt = persist_window_attempt(
        operation=operation,
        source_plan=record,
        window=window,
        packet=_packet(),
        settings={"quality": "high"},
        model_profile={"resolved": "configured/test"},
        admission=_admission(),
    )
    db.session.commit()
    operation.cancel_requested_at = utc_now_db()
    operation.state = "cancel_requested"
    db.session.commit()
    calls = 0

    def provider(_payload):
        nonlocal calls
        calls += 1
        return {"results": []}

    with pytest.raises(TranslationOperationCancellation):
        execute_window_attempt(
            attempt,
            operation=operation,
            source_plan=record,
            window=window,
            provider_call=provider,
        )
    assert calls == 0
    operation.state = "running"
    operation.cancel_requested_at = None
    db.session.commit()
    db.session.execute(
        text(
            "UPDATE translation_window_attempt SET source_plan_hash = :hash "
            "WHERE id = :id"
        ),
        {"hash": "0" * 64, "id": attempt.id},
    )
    db.session.commit()
    with pytest.raises(BlockContractError, match="window-source-drift"):
        window_execution._fresh_fence(
            attempt.id,
            operation.id,
            operation.version,
        )
    assert calls == 0


def test_coordinator_rejects_an_attempt_bound_to_another_operation(app):
    _project, plan, record, operation = _state()
    window = _window(plan)
    attempt = persist_window_attempt(
        operation=operation,
        source_plan=record,
        window=window,
        packet=_packet(),
        settings={"quality": "high"},
        model_profile={"resolved": "configured/test"},
        admission=_admission(),
    )
    db.session.commit()
    operation.state = "completed"
    db.session.flush()
    other = TranslationOperation(
        project_id=operation.project_id,
        owner_user_id=operation.owner_user_id,
        kind="document_block_translate",
        idempotency_key="window-other",
        request_fingerprint="b" * 64,
        state="running",
        source_content_hash=operation.source_content_hash,
    )
    db.session.add(other)
    db.session.flush()
    with pytest.raises(BlockContractError, match="window-attempt-operation"):
        execute_window_attempt(
            attempt,
            operation=other,
            source_plan=record,
            window=window,
            provider_call=lambda _payload: {"results": []},
        )
