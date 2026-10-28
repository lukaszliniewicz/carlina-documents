"""Synthetic, file-backed recovery of accepted passive PDF page evidence."""

from __future__ import annotations

import json
from types import SimpleNamespace

import fitz
import pytest
from content_storage_support import bind_content_policy
from flask import Flask
from sqlalchemy.orm.attributes import flag_modified

from carlina.content_storage import content_codec_from_context
from carlina.runtime_profiles import ContentPolicy, RuntimeProfile
from iarf_assistant.addons.doc_processor.assembly_recovery import (
    AssemblyRecoveryService,
    _topology_signature,
)
from iarf_assistant.addons.doc_processor.batching import normalize_document_batch_plan
from iarf_assistant.addons.doc_processor.execution_recovery import (
    _source_proof,
    settings_fingerprint,
)
from iarf_assistant.addons.doc_processor.mcp_service import (
    DocProcessorMcpService,
    DocProcessorMcpServiceError,
)
from iarf_assistant.addons.doc_processor.models import ProcessedDocument
from iarf_assistant.addons.doc_processor.parsers.pdf_utils import (
    normalize_extracted_page_html,
)
from iarf_assistant.extensions import db
from iarf_assistant.models import McpExecution, McpWorkItem, User

PAGE_ONE = (
    '<main_content><ol class="continues" start="4"><li value="4">four'
    '<ol type="a" start="1"><li value="1">alpha-first</li>'
    '<li value="1">alpha-second</li></ol></li></ol>'
    '</main_content><footnotes></footnotes>'
)
REPAIRED_ONE = (
    '<main_content><ol class="continues" start="4"><li value="4">four'
    '<ol type="a" start="1"><li value="1">alpha-first</li></ol></li></ol>'
    '<ol type="a" start="1"><li value="1">alpha-second</li></ol>'
    '</main_content><footnotes></footnotes>'
)
PAGE_TWO = (
    '<main_content><ol class="continued" type="a" start="1"><li>beta</li></ol>'
    '</main_content><footnotes></footnotes>'
)


@pytest.fixture(params=list(ContentPolicy))
def recovery(tmp_path, request):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    policy = request.param
    bind_content_policy(
        application,
        profile=(
            RuntimeProfile.DOCUMENTS
            if policy is ContentPolicy.PLAINTEXT_V1
            else RuntimeProfile.WORKBENCH
        ),
        content_policy=policy,
        legacy=False,
    )
    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'recovery.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="synthetic-assembly-recovery",
        TESTING=True,
        CARLINA_UPLOADS_DIR=str(upload_root),
    )
    db.init_app(application)
    with application.app_context():
        db.create_all()
        db.session.add_all([
            User(id=1, email="recovery-one@example.test", password_hash="synthetic"),
            User(id=2, email="recovery-two@example.test", password_hash="synthetic"),
        ])
        db.session.flush()
        source = upload_root / "source.pdf"
        pdf = fitz.open()
        pdf.new_page()
        pdf.new_page()
        pdf.save(source)
        pdf.close()
        plan = normalize_document_batch_plan({
            "schema_version": "carlina.document-batches.v1",
            "scheduling_mode": "sequential",
            "batches": [{"batch_id": "all", "start_page": 1, "end_page": 2}],
        })
        settings = {
            "mcp_batch_plan": plan,
            "mcp_last_execution_id": "source-run",
            "mcp_page_blank_policy": "explicit-v1",
            "pdf_options": {"toc_handling": "omit"},
            "pdf_state": {"html_parts": [normalize_extracted_page_html(PAGE_ONE), None],
                          "boundary_flags": {}},
        }
        document = ProcessedDocument(
            id="source-document", user_id=1, filename="source.pdf",
            file_path=str(source), status="error", settings=settings,
            output_html="<p>partial</p>", error_message="list-continuation-style-conflict",
        )
        contract = {"schema_version": 1, "source": _source_proof(source),
                    "initial_settings_sha256": settings_fingerprint(settings)}
        execution = McpExecution(
            id="source-run", owner_user_id=1, addon_id="doc_processor",
            resource_kind="document", resource_id=document.id, mode="passive",
            status="failed", request_json={"batch_plan": plan, "recovery": contract},
        )
        accepted = {"content": json.dumps({"pages": [
            {"page_number": 1, "html": PAGE_ONE, "blank": False},
            {"page_number": 2, "html": PAGE_TWO, "blank": False},
        ]})}
        work = McpWorkItem(
            id="accepted-work", execution_id=execution.id, request_fingerprint="synthetic-work",
            request_json={"synthetic": True}, accepted_response_json=accepted,
            batch_id="all", dispatch_phase="page_extraction", unit_index=1,
            is_batch_terminal_unit=True, state="completed", version=3,
        )
        db.session.add(document)
        db.session.flush()
        db.session.add(execution)
        db.session.flush()
        db.session.add(work)
        db.session.commit()
        service = AssemblyRecoveryService(
            DocProcessorMcpService(SimpleNamespace(application=application), 1)
        )
        yield service, document, execution, work, source
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def _repair(service: AssemblyRecoveryService) -> tuple[str, list[dict]]:
    inspection = service.run("source-run")
    fetched = service.run("source-run", "fetch", page_numbers=[1, 2])
    assert fetched["pages"][0]["html"]
    return inspection["snapshot_revision"], [{
        "page_number": 1,
        "expected_sha256": fetched["pages"][0]["sha256"],
        "html": REPAIRED_ONE,
    }]


def test_recovery_assembles_without_altering_original_evidence(recovery):
    service, document, execution, work, _source = recovery
    before = dict(work.accepted_response_json)
    inspection = service.run(execution.id)
    assert inspection["ready"] is False
    assert inspection["conflict"] == "list-continuation-style-conflict"
    revision, repairs = _repair(service)
    preview = service.run(execution.id, "preview", repairs=repairs,
                          expected_revision=revision)
    applied = service.run(execution.id, "apply", repairs=repairs,
                          expected_revision=revision,
                          preview_token=preview["preview_token"],
                          idempotency_key="synthetic-one")
    assert applied["idempotent"] is False
    assert document.status == "completed"
    assert "four" in document.output_markdown
    assert "beta" in document.output_markdown  # accepted response without final checkpoint
    assert "pdf_state" not in document.settings
    assert document.settings["mcp_last_execution_id"] == applied["new_execution_id"]
    encoded_audit = document.settings["mcp_assembly_recovery_audit"]
    policy = service.documents.context.application.extensions[
        "carlina_profile_binding"
    ].content_policy
    expected_prefix = "plain:v1:" if policy is ContentPolicy.PLAINTEXT_V1 else "enc:v2:"
    assert encoded_audit.startswith(expected_prefix)
    assert ("alpha" in encoded_audit) is (policy is ContentPolicy.PLAINTEXT_V1)
    audit = json.loads(
        content_codec_from_context(legacy_strategy="system").decode_text(encoded_audit)
    )
    assert audit["repairs"][0]["original_html"] == normalize_extracted_page_html(PAGE_ONE)
    assert audit["repairs"][0]["new_html"] == normalize_extracted_page_html(REPAIRED_ONE)
    assert execution.status == "failed"
    assert work.accepted_response_json == before
    assert service.run(execution.id, "apply", repairs=repairs,
                       expected_revision=revision,
                       preview_token=preview["preview_token"],
                       idempotency_key="synthetic-one")["idempotent"] is True
    with pytest.raises(DocProcessorMcpServiceError) as error:
        service.run(execution.id, "apply", repairs=[], expected_revision=revision,
                    preview_token=preview["preview_token"],
                    idempotency_key="synthetic-one")
    assert error.value.code == "idempotency_conflict"


@pytest.mark.parametrize("mutation", ["text", "ordinal", "stale", "missing_work", "owner"])
def test_recovery_rejects_changed_evidence(recovery, mutation):
    service, document, execution, work, _source = recovery
    revision, repairs = _repair(service)
    if mutation == "text":
        repairs[0]["html"] = REPAIRED_ONE.replace("alpha", "changed")
    elif mutation == "ordinal":
        repairs[0]["html"] = REPAIRED_ONE.replace('type="a"', 'type="A"')
    elif mutation == "stale":
        revision = "0" * 64
    elif mutation == "missing_work":
        work.accepted_response_json = None
        db.session.commit()
    else:
        service = AssemblyRecoveryService(
            DocProcessorMcpService(service.documents.context, 2)
        )
    with pytest.raises(DocProcessorMcpServiceError):
        service.run(execution.id, "preview", repairs=repairs, expected_revision=revision)
    assert document.status == "error"


def test_preview_token_and_source_proof_are_fenced(recovery):
    service, document, execution, _work, source = recovery
    revision, repairs = _repair(service)
    preview = service.run(execution.id, "preview", repairs=repairs,
                          expected_revision=revision)
    with pytest.raises(DocProcessorMcpServiceError) as error:
        service.run(execution.id, "apply", repairs=repairs,
                    expected_revision=revision, preview_token="0" * 64,
                    idempotency_key="stale-preview")
    assert error.value.code == "preview_conflict"
    assert document.status == "error"
    assert preview["changed_pages"] == [1]
    source.write_bytes(source.read_bytes() + b"\n% synthetic source mutation")
    with pytest.raises(DocProcessorMcpServiceError) as error:
        service.run(execution.id)
    assert error.value.code == "recovery_source_changed"


def test_newer_execution_prevents_recovery(recovery):
    service, document, execution, _work, _source = recovery
    newer = McpExecution(
        id="newer-run", owner_user_id=document.user_id,
        addon_id="doc_processor", resource_kind="document", resource_id=document.id,
        mode="passive", status="queued", request_json={},
    )
    db.session.add(newer)
    db.session.commit()
    with pytest.raises(DocProcessorMcpServiceError) as error:
        service.run(execution.id)
    assert error.value.code == "execution_conflict"


def test_failed_knowledge_sync_rolls_back_all_recovery_writes(recovery, monkeypatch):
    from iarf_assistant.addons.doc_processor import assembly_recovery

    service, document, execution, work, _source = recovery
    original = dict(work.accepted_response_json)
    execution_count = McpExecution.query.count()
    revision, repairs = _repair(service)
    preview = service.run(execution.id, "preview", repairs=repairs,
                          expected_revision=revision)

    def unavailable(_document):
        raise RuntimeError("synthetic knowledge sync interruption")

    monkeypatch.setattr(assembly_recovery, "sync_processed_document_source", unavailable)
    with pytest.raises(RuntimeError, match="synthetic knowledge sync interruption"):
        service.run(execution.id, "apply", repairs=repairs,
                    expected_revision=revision,
                    preview_token=preview["preview_token"],
                    idempotency_key="failed-sync")
    db.session.expire_all()
    restored = db.session.get(ProcessedDocument, document.id)
    assert restored.status == "error"
    assert "mcp_assembly_recovery_receipt" not in restored.settings
    assert "pdf_state" in restored.settings
    assert execution.status == "failed"
    assert work.accepted_response_json == original
    assert McpExecution.query.count() == execution_count


def test_unresolved_semantic_reference_blocks_preview(recovery):
    service, document, execution, work, _source = recovery
    accepted = dict(work.accepted_response_json)
    payload = json.loads(accepted["content"])
    payload["pages"][1]["html"] = PAGE_TWO.replace(
        "beta", '<a href="#unresolved-target">beta</a>'
    )
    work.accepted_response_json = {"content": json.dumps(payload)}
    db.session.commit()
    revision, repairs = _repair(service)
    with pytest.raises(DocProcessorMcpServiceError) as error:
        service.run(execution.id, "preview", repairs=repairs,
                    expected_revision=revision)
    assert error.value.code == "semantic_conflict"
    assert document.status == "error"


def test_list_cannot_move_from_main_content_to_footnotes(recovery):
    service, document, execution, work, _source = recovery
    accepted = dict(work.accepted_response_json)
    revision, repairs = _repair(service)
    moved = REPAIRED_ONE.replace(
        '<ol type="a" start="1"><li value="1">alpha-second</li></ol>'
        '</main_content><footnotes></footnotes>',
        '</main_content><footnotes><ol type="a" start="1">'
        '<li value="1">alpha-second</li></ol></footnotes>',
    )
    assert moved != REPAIRED_ONE
    assert _topology_signature(normalize_extracted_page_html(REPAIRED_ONE)) != (
        _topology_signature(normalize_extracted_page_html(moved))
    )
    repairs[0]["html"] = moved
    with pytest.raises(DocProcessorMcpServiceError) as error:
        service.run(execution.id, "preview", repairs=repairs,
                    expected_revision=revision)
    assert error.value.code == "invalid_repair"
    assert document.status == "error"
    assert work.accepted_response_json == accepted


@pytest.mark.parametrize(
    ("source_label", "changed_label"),
    [("alpha\u00a0first", "alpha first"), ("alpha  first", "alpha first")],
)
def test_repair_cannot_change_raw_source_spacing(recovery, source_label, changed_label):
    service, document, execution, work, _source = recovery
    original_page = PAGE_ONE.replace("alpha-first", source_label)
    payload = json.loads(work.accepted_response_json["content"])
    payload["pages"][0]["html"] = original_page
    work.accepted_response_json = {"content": json.dumps(payload)}
    settings = dict(document.settings)
    state = dict(settings["pdf_state"])
    state["html_parts"] = [normalize_extracted_page_html(original_page), None]
    settings["pdf_state"] = state
    document.settings = settings
    flag_modified(document, "settings")
    db.session.commit()
    accepted = dict(work.accepted_response_json)
    revision, repairs = _repair(service)
    repairs[0]["html"] = REPAIRED_ONE.replace("alpha-first", changed_label)
    with pytest.raises(DocProcessorMcpServiceError) as error:
        service.run(execution.id, "preview", repairs=repairs,
                    expected_revision=revision)
    assert error.value.code == "invalid_repair"
    assert document.status == "error"
    assert work.accepted_response_json == accepted


def test_apply_rechecks_work_version_from_independent_app_context(recovery, monkeypatch):
    service, document, execution, work, _source = recovery
    accepted = dict(work.accepted_response_json)
    revision, repairs = _repair(service)
    preview = service.run(execution.id, "preview", repairs=repairs,
                          expected_revision=revision)
    original_candidate = service._candidate
    mutated = False

    def mutate_between_snapshot_and_reservation(snapshot, proposed):
        nonlocal mutated
        if not mutated:
            mutated = True
            with service.documents.context.application.app_context():
                external_work = db.session.get(McpWorkItem, work.id)
                assert external_work is not None
                external_work.version += 1
                db.session.commit()
                db.session.remove()
        return original_candidate(snapshot, proposed)

    monkeypatch.setattr(service, "_candidate", mutate_between_snapshot_and_reservation)
    with pytest.raises(DocProcessorMcpServiceError) as error:
        service.run(execution.id, "apply", repairs=repairs,
                    expected_revision=revision,
                    preview_token=preview["preview_token"],
                    idempotency_key="independent-version-change")
    assert error.value.code == "revision_conflict"
    db.session.expire_all()
    restored = db.session.get(ProcessedDocument, document.id)
    assert restored.status == "error"
    assert "mcp_assembly_recovery_receipt" not in restored.settings
    assert db.session.get(McpWorkItem, work.id).version == 4
    assert db.session.get(McpWorkItem, work.id).accepted_response_json == accepted
    assert McpExecution.query.count() == 1
