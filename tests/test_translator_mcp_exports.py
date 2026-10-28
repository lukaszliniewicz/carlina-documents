"""Bounded owner-key document export admission and retrieval."""

from __future__ import annotations

import base64
import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from content_storage_support import bind_content_policy
from flask import Flask
from carlina.core.epub_export import (
    epub_value_sha256,
    normalize_epub_options,
)

from iarf_assistant import extensions as app_extensions
from iarf_assistant import models as app_models
from iarf_assistant.addons.translator import (
    api_client,
    mcp_exports,
    operations,
    review_coverage,
    worker,
)
from iarf_assistant.addons.translator.mcp_exports import McpExportError
from iarf_assistant.addons.translator.models import (
    TranslationOperation,
    TranslationProject,
    TranslationSourcePlan,
    TranslationSourcePlanActive,
)
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.core.task_queue import TaskQueue
from iarf_assistant.core.utc import utc_now_db

db = app_extensions.db
User = app_models.User
EPUB = b"PK\x03\x04synthetic epub fixture\x00\x01\x02"
PROVIDER_REFERENCE = {
    "version": 1,
    "capability": "document.semantic_export",
    "provider_addon_id": "doc_processor",
    "provider_capability_id": "semantic_export",
}


def _synthetic_review_snapshot(project, state, requirement="none"):
    """Build a deterministic manifest shape without claiming semantic review."""

    coverage_rows = [
        {
            "coverage_id": state["coverage_id"],
            "kind": "structural",
            "reviewer_kind": "agent",
            "scope": "anchors",
            "source_anchor_ids": ["synthetic-anchor-1"],
        }
    ]
    if requirement != "none":
        coverage_rows.append(
            {
                "coverage_id": state["required_coverage_id"],
                "kind": requirement,
                "reviewer_kind": "human",
                "scope": "block",
                "source_anchor_ids": [],
            }
        )

    kind_projection = {}
    summary = {}
    for kind in ("structural", "target_reading", "bilingual"):
        rows = [row for row in coverage_rows if row["kind"] == kind]
        complete = any(row["scope"] == "block" for row in rows)
        reviewed_anchor_ids = (
            ["synthetic-anchor-1"]
            if complete or any(row["source_anchor_ids"] for row in rows)
            else []
        )
        kind_projection[kind] = {
            "complete": complete,
            "coverage_ids": [row["coverage_id"] for row in rows],
            "reviewed_source_anchor_ids": reviewed_anchor_ids,
            "reviewed_source_anchor_count": len(reviewed_anchor_ids),
        }
        state_name = "complete" if complete else "partial" if rows else "missing"
        summary[kind] = {
            "complete": int(state_name == "complete"),
            "partial": int(state_name == "partial"),
            "missing": int(state_name == "missing"),
        }

    coverage_manifest = {
        "contract": "translator-review-coverage-v1",
        "project_id": str(project.id),
        "source_plan_id": state["source_plan_id"],
        "source_plan_hash": state["source_plan_hash"],
        "blocks": [
            {
                "block_id": "synthetic-block-1",
                "target_revision_id": state["target_revision_id"],
                "target_version": state["target_version"],
                "coverage": coverage_rows,
                "kinds": kind_projection,
            }
        ],
        "summary": summary,
    }
    return review_coverage._export_snapshot(coverage_manifest, requirement)


class Queue:
    def __init__(self):
        self.jobs: list[tuple[str, dict]] = []
        self.statuses: dict[str, dict] = {}
        self.fail_enqueue = False

    def enqueue(self, handler, payload):
        if self.fail_enqueue:
            raise RuntimeError("synthetic queue unavailable")
        job_id = f"job-{len(self.jobs) + 1}"
        self.jobs.append((handler, payload))
        return job_id

    def get_status(self, job_id):
        return self.statuses.get(job_id, {"status": "queued"})


@pytest.fixture
def setup(tmp_path, monkeypatch):
    app = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(app)
    app.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'exports.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="synthetic-export-test",
        TESTING=True,
    )
    db.init_app(app)
    with app.app_context():
        key_store = InstallationKeyStore(tmp_path / "keys.json")
        key_store.initialize()
        app.extensions["carlina_encryption"] = EncryptionService(key_store)
        db.create_all()
        owner = User(email="export-owner@example.test", password_hash="test")
        other = User(email="export-other@example.test", password_hash="test")
        db.session.add_all([owner, other])
        db.session.flush()
        project = TranslationProject(
            user_id=owner.id,
            name="Synthetic export",
            source_lang="en",
            target_lang="pl",
            document_id="source-1",
            engine_contract="document-block-v1",
            status="completed",
            source_content_hash="a" * 64,
            settings={
                "semantic_export_reference": PROVIDER_REFERENCE,
                "export_metadata": {"title": "  Saved edition  "},
            },
        )
        db.session.add(project)
        db.session.flush()
        source_plan = TranslationSourcePlan(
            project_id=project.id,
            contract_version="document-block-v1",
            source_document_id="source-1",
            source_language="en",
            language_profile="default",
            namespace="namespace",
            snapshot_contract_version="snapshot-v1",
            semantic_contract_version="semantic-html-v1",
            semantic_contract_mode="strict",
            semantic_content_hash="b" * 64,
            source_content_hash="a" * 64,
            snapshot_captured_at=utc_now_db(),
            planner_version="planner-v1",
            planner_settings_hash="c" * 64,
            plan_hash="d" * 64,
            source_attestation={},
            planner_settings={},
            coverage_witness={},
            block_count=0,
            anchor_count=0,
        )
        db.session.add(source_plan)
        db.session.flush()
        db.session.add(
            TranslationSourcePlanActive(
                project_id=project.id, source_plan_id=source_plan.id, version=1
            )
        )
        db.session.commit()
        plan = SimpleNamespace(
            project_id=project.id, snapshot=SimpleNamespace(content_hash="a" * 64)
        )
        monkeypatch.setattr(operations, "rehydrate_source_plan", lambda _record: plan)
        monkeypatch.setattr(
            api_client,
            "load_block_source_plan",
            lambda _project: SimpleNamespace(record=source_plan, plan=plan),
        )
        queue = Queue()
        queue.review_snapshot_state = {
            "source_plan_id": str(source_plan.id),
            "source_plan_hash": source_plan.plan_hash,
            "target_revision_id": "synthetic-target-revision-1",
            "target_version": 1,
            "coverage_id": "synthetic-coverage-1",
            "required_coverage_id": "synthetic-required-coverage-1",
        }
        queue.review_snapshot_calls = []

        def synthetic_snapshot(snapshot_project, requirement="none"):
            queue.review_snapshot_calls.append(requirement)
            return _synthetic_review_snapshot(
                snapshot_project, queue.review_snapshot_state, requirement
            )

        monkeypatch.setattr(
            review_coverage, "snapshot_export_review", synthetic_snapshot
        )
        references = []

        def provider_factory(reference):
            references.append(reference)
            return SimpleNamespace(provider_reference=lambda: PROVIDER_REFERENCE)

        context = SimpleNamespace(get_task_queue=lambda _addon_id: queue)
        client = api_client.TranslatorClient(
            context,
            {"current_user_id": owner.id},
            semantic_export_provider_factory=provider_factory,
        )
        other_client = api_client.TranslatorClient(
            context,
            {"current_user_id": other.id},
            semantic_export_provider_factory=provider_factory,
        )
        yield app, project, client, other_client, queue, references
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def _start(
    setup, *, key="export-1", export_format="epub", review_requirement="none"
):
    _app, project, client, _other, _queue, _references = setup
    return client.export_document(
        project.id,
        export_format=export_format,
        idempotency_key=key,
        review_requirement=review_requirement,
    )


def _publish(
    setup,
    operation_id,
    *,
    data=EPUB,
    export_format="epub",
    filename="Synthetic_export.epub",
):
    app, project, _client, _other, queue, _references = setup
    operation = db.session.get(TranslationOperation, operation_id)
    root = Path(app.instance_path) / "translator_exports" / operation_id
    root.mkdir(parents=True, exist_ok=True)
    output = root / filename
    output.write_bytes(data)
    artifact = {
        "format": export_format,
        "media_type": {
            "epub": "application/epub+zip",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "html": "text/html",
            "pdf": "application/pdf",
        }[export_format],
        "byte_length": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "validation_profile": "test-semantic-export",
        "validation_profile_version": "1",
    }
    checkpoint = dict(operation.checkpoint_json or {})
    if export_format == "epub" and "epub_options" in checkpoint:
        from carlina.core.epub_export import normalize_epub_quality

        options = normalize_epub_options(checkpoint["epub_options"])
        input_sha256 = checkpoint.get("semantic_html_sha256", "a" * 64)
        checkpoint["semantic_html_sha256"] = input_sha256
        artifact["quality"] = normalize_epub_quality(
            {
                "schema": "epub-quality-v1",
                "profile": "publishing-v1",
                "input_sha256": input_sha256,
                "options_sha256": epub_value_sha256(options),
                "artifact_sha256": artifact["sha256"],
                "content_sha256": hashlib.sha256(data).hexdigest(),
                "checks": {
                    "structure": "passed",
                    "epubcheck": "not_run",
                    "ace": "not_run",
                    "manual": "not_reviewed",
                },
                "counts": {
                    "notes": 0,
                    "references": 0,
                    "backlinks": 0,
                    "headings": 0,
                    "images": 0,
                    "tables": 0,
                    "pages": 0,
                },
                "issues": [],
                "issue_count": 0,
                "epubcheck_version": None,
            }
        )
        operation.checkpoint_json = checkpoint
    operations.transition_operation(operation, "running")
    db.session.flush()
    operations.finish_operation(
        operation, "completed", result={"format": export_format, "artifact": artifact}
    )
    db.session.commit()
    queue.statuses[operation.queue_job_id] = {
        "status": "completed",
        "result": {
            "kind": "file_download",
            "output_path": str(output),
            "download_name": filename,
            "mimetype": artifact["media_type"],
            "user_id": project.user_id,
            "project_id": project.id,
            "format": export_format,
        },
    }
    return output


def _store_delivery(setup, operation_id):
    _app, _project, _client, _other, queue, _references = setup
    operation = db.session.get(TranslationOperation, operation_id)
    delivery = copy.deepcopy(queue.statuses[operation.queue_job_id]["result"])
    operation.result_json = {**operation.result_json, "delivery": delivery}
    db.session.commit()
    return delivery


def test_durable_delivery_survives_missing_unavailable_and_duplicate_queue(
    setup, monkeypatch
):
    _app, project, client, _other, queue, _references = setup
    started = _start(setup)
    output = _publish(setup, started["operation_id"])
    _store_delivery(setup, started["operation_id"])
    queue.statuses.pop(started["queue_job_id"])
    assert (
        client.export_document_get(project.id, started["operation_id"])["filename"]
        == output.name
    )
    original_get_status = queue.get_status
    monkeypatch.setattr(
        queue, "get_status", lambda _job: (_ for _ in ()).throw(RuntimeError("offline"))
    )
    assert (
        client.export_document_get(project.id, started["operation_id"])["ready"] is True
    )
    monkeypatch.setattr(queue, "get_status", original_get_status)
    queue.statuses[started["queue_job_id"]] = {
        "status": "completed",
        "result": {
            "duplicate": True,
            "message": "Duplicate translator delivery ignored.",
        },
    }
    assert (
        client.export_document_get(project.id, started["operation_id"])["ready"] is True
    )


def test_completed_legacy_export_survives_a_recreated_queue(setup, monkeypatch):
    app, project, client, other, _queue, _references = setup
    started = _start(setup)
    output = _publish(setup, started["operation_id"])
    operation = db.session.get(TranslationOperation, started["operation_id"])
    original_result = copy.deepcopy(operation.result_json)
    original_version = operation.version
    restored_queue = TaskQueue(app.instance_path, "translator", mirror_runtime=False)
    monkeypatch.setattr(client, "_queue", lambda: restored_queue)
    try:
        assert restored_queue.get_status(started["queue_job_id"])["status"] == "unknown"
        metadata = client.export_document_get(project.id, started["operation_id"])
        assert metadata["ready"] is True
        assert metadata["filename"] == output.name
        assert metadata["operation_id"] == started["operation_id"]
        assert metadata["queue_job_id"] == started["queue_job_id"]
        chunks = []
        offset = 0
        while True:
            result = client.export_document_get(
                project.id,
                started["operation_id"],
                include_content=True,
                offset=offset,
                limit=7,
                expected_sha256=metadata["sha256"],
            )
            chunk = base64.b64decode(result["content_base64"])
            assert hashlib.sha256(chunk).hexdigest() == result["chunk_sha256"]
            chunks.append(chunk)
            offset = result["next_offset"]
            if result["eof"]:
                break
        assert b"".join(chunks) == EPUB
        assert operation.result_json == original_result
        assert operation.version == original_version
        assert restored_queue.get_status(started["queue_job_id"])["status"] == "unknown"
        with pytest.raises(LookupError):
            other.export_document_get(project.id, started["operation_id"])
    finally:
        restored_queue.cache.close()


def test_unknown_queue_record_with_delivery_does_not_trigger_legacy_recovery(setup):
    _app, project, client, _other, queue, _references = setup
    started = _start(setup)
    _publish(setup, started["operation_id"])
    queue.statuses[started["queue_job_id"]] = {
        "status": "unknown",
        "result": {"user_id": 999},
    }
    with pytest.raises(McpExportError) as invalid:
        client.export_document_get(project.id, started["operation_id"])
    assert invalid.value.code == "artifact_invalid"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("user_id", 999),
        ("project_id", "other-project"),
        ("format", "docx"),
        ("mimetype", "text/plain"),
        ("output_path", "/tmp/elsewhere.epub"),
        ("download_name", "elsewhere.epub"),
    ],
)
def test_invalid_durable_delivery_never_falls_back_to_valid_queue(setup, field, value):
    _app, project, client, _other, _queue, _references = setup
    started = _start(setup)
    _publish(setup, started["operation_id"])
    delivery = _store_delivery(setup, started["operation_id"])
    operation = db.session.get(TranslationOperation, started["operation_id"])
    operation.result_json = {
        **operation.result_json,
        "delivery": {**delivery, field: value},
    }
    db.session.commit()
    with pytest.raises(McpExportError) as invalid:
        client.export_document_get(project.id, started["operation_id"])
    assert invalid.value.code == "artifact_invalid"
    assert invalid.value.detail == invalid.value.message


def test_completed_legacy_replay_recovers_original_bytes_and_public_read(setup):
    _app, project, client, _other, queue, _references = setup
    started = _start(setup)
    output = _publish(setup, started["operation_id"])
    operation = db.session.get(TranslationOperation, started["operation_id"])
    original_version = operation.version
    original_result = copy.deepcopy(operation.result_json)
    project.name = "Renamed after export"
    db.session.commit()
    (output.parent / ".stage-old.epub").write_bytes(b"abandoned staging")
    queue.statuses.pop(started["queue_job_id"])
    payload = {
        "project_id": project.id,
        "operation_id": started["operation_id"],
        "user_id": project.user_id,
    }
    delivery = worker.export_document_blocks(payload, queue, started["queue_job_id"])
    assert delivery["output_path"] == str(output)
    assert delivery["download_name"] == output.name
    assert (
        hashlib.sha256(output.read_bytes()).hexdigest()
        == original_result["artifact"]["sha256"]
    )
    db.session.expire_all()
    operation = db.session.get(TranslationOperation, started["operation_id"])
    assert operation.version == original_version
    assert operation.result_json == original_result
    assert (
        client.export_document_get(project.id, started["operation_id"])["ready"]
        is False
    )
    queue.statuses[started["queue_job_id"]] = {
        "status": "completed",
        "result": {
            "duplicate": True,
            "message": "Duplicate translator delivery ignored.",
        },
    }
    readback = client.export_document_get(
        project.id, started["operation_id"], include_content=True
    )
    assert base64.b64decode(readback["content_base64"]) == EPUB
    assert readback["filename"] == output.name
    with pytest.raises(operations.TranslationOperationClaimConflict):
        worker.export_document_blocks(payload, queue, "different-job")


@pytest.mark.parametrize("durable", [False, True])
def test_export_read_accepts_safe_leading_hyphen_filename(setup, durable):
    _app, project, client, _other, queue, _references = setup
    started = _start(setup)
    output = _publish(setup, started["operation_id"], filename="-Example.epub")
    if durable:
        _store_delivery(setup, started["operation_id"])
        queue.statuses.pop(started["queue_job_id"])
    else:
        queue.statuses[started["queue_job_id"]] = {
            "status": "completed",
            "result": {
                "duplicate": True,
                "message": "Duplicate translator delivery ignored.",
            },
        }
    assert (
        client.export_document_get(project.id, started["operation_id"])["filename"]
        == output.name
    )


@pytest.mark.parametrize(
    "damage", ["missing", "ambiguous", "symlink", "directory_symlink", "size", "hash"]
)
@pytest.mark.parametrize("queue_state", ["completed", "unknown"])
def test_legacy_recovery_refuses_unverifiable_file(setup, tmp_path, damage, queue_state):
    _app, project, client, _other, queue, _references = setup
    started = _start(setup)
    output = _publish(setup, started["operation_id"])
    queue.statuses[started["queue_job_id"]] = {
        "status": "completed",
        "result": {
            "duplicate": True,
            "message": "Duplicate translator delivery ignored.",
        },
    }
    if queue_state == "unknown":
        queue.statuses[started["queue_job_id"]] = {"status": "unknown"}
    if damage == "missing":
        output.unlink()
    elif damage == "ambiguous":
        (output.parent / "other.epub").write_bytes(EPUB)
    elif damage == "symlink":
        outside = tmp_path / "outside.epub"
        outside.write_bytes(EPUB)
        output.unlink()
        output.symlink_to(outside)
    elif damage == "directory_symlink":
        saved = output.parent.with_name(output.parent.name + "-saved")
        output.parent.rename(saved)
        output.parent.symlink_to(saved, target_is_directory=True)
    elif damage == "size":
        output.write_bytes(EPUB + b"extra")
    else:
        output.write_bytes(bytes([EPUB[0] ^ 1]) + EPUB[1:])
    with pytest.raises(McpExportError) as invalid:
        client.export_document_get(project.id, started["operation_id"])
    assert invalid.value.code in {"artifact_invalid", "artifact_unavailable"}


@pytest.mark.parametrize("queue_state", ["completed", "unknown"])
def test_legacy_recovery_rechecks_exact_reader_descriptor_after_replacement(
    setup, monkeypatch, queue_state
):
    _app, project, client, _other, queue, _references = setup
    started = _start(setup)
    output = _publish(setup, started["operation_id"])
    queue.statuses[started["queue_job_id"]] = {
        "status": "completed",
        "result": {
            "duplicate": True,
            "message": "Duplicate translator delivery ignored.",
        },
    }
    if queue_state == "unknown":
        queue.statuses[started["queue_job_id"]] = {"status": "unknown"}
    original_open = mcp_exports._open_artifact

    def replace_before_reader_open(path, operation_id, export_format):
        replacement = output.with_name("replacement.stage")
        replacement.write_bytes(bytes([EPUB[0] ^ 1]) + EPUB[1:])
        replacement.replace(output)
        return original_open(path, operation_id, export_format)

    monkeypatch.setattr(mcp_exports, "_open_artifact", replace_before_reader_open)
    with pytest.raises(McpExportError) as invalid:
        client.export_document_get(
            project.id, started["operation_id"], include_content=True
        )
    assert invalid.value.code == "artifact_invalid"


@pytest.mark.parametrize(
    "duplicate",
    [
        {"duplicate": True, "message": "Different duplicate"},
        {"duplicate": 1, "message": "Duplicate translator delivery ignored."},
        {
            "duplicate": True,
            "message": "Duplicate translator delivery ignored.",
            "extra": 1,
        },
    ],
)
def test_legacy_recovery_requires_exact_duplicate_and_completed_operation(
    setup, duplicate
):
    _app, project, client, _other, queue, _references = setup
    started = _start(setup)
    _publish(setup, started["operation_id"])
    queue.statuses[started["queue_job_id"]] = {
        "status": "completed",
        "result": duplicate,
    }
    with pytest.raises(McpExportError) as malformed:
        client.export_document_get(project.id, started["operation_id"])
    assert malformed.value.code == "artifact_invalid"
    operation = db.session.get(TranslationOperation, started["operation_id"])
    operation.state = "failed"
    db.session.commit()
    queue.statuses[started["queue_job_id"]]["result"] = {
        "duplicate": True,
        "message": "Duplicate translator delivery ignored.",
    }
    assert (
        client.export_document_get(project.id, started["operation_id"])["ready"]
        is False
    )


def test_export_admits_exact_pins_and_idempotent_retry(setup):
    _app, project, _client, other, queue, references = setup
    first = _start(setup)
    review_manifest = _synthetic_review_snapshot(
        project, queue.review_snapshot_state, "none"
    )
    publishing_options = normalize_epub_options({})
    assert first["contract"] == "translator-mcp-export-v1"
    assert first["format"] == "epub"
    assert first["state"] == "queued"
    assert first["operation_id"]
    assert first["queue_job_id"] == "job-1"
    assert references == [PROVIDER_REFERENCE]
    assert len(queue.jobs) == 1

    handler, payload = queue.jobs[0]
    assert handler == "export_document_blocks"
    assert payload == {
        "project_id": project.id,
        "user_id": project.user_id,
        "operation_id": first["operation_id"],
        "format": "epub",
        "numbering_revision": 0,
        "export_metadata": {"title": "Saved edition"},
        "epub_options": publishing_options,
        "epub_image_mode": "jpeg",
        "source_content_hash": "a" * 64,
        "source_plan_id": TranslationSourcePlan.query.filter_by(project_id=project.id)
        .one()
        .id,
        "source_plan_hash": "d" * 64,
        "semantic_export_provider": PROVIDER_REFERENCE,
        "review_manifest": review_manifest,
    }
    operation = db.session.get(TranslationOperation, first["operation_id"])
    assert operation.request_fingerprint == operations.canonical_request_fingerprint(
        {
            "format": "epub",
            "numbering_revision": 0,
            "export_metadata": {"title": "Saved edition"},
            "epub_options": publishing_options,
            "epub_image_mode": "jpeg",
            "source_content_hash": "a" * 64,
            "source_plan_id": TranslationSourcePlan.query.filter_by(
                project_id=project.id
            ).one().id,
            "source_plan_hash": "d" * 64,
            "semantic_export_provider": PROVIDER_REFERENCE,
            "review_manifest": review_manifest,
        }
    )
    assert _start(setup) == first
    assert len(queue.jobs) == 1
    with pytest.raises(RuntimeError, match="idempotency"):
        _start(setup, export_format="docx")
    project.settings = {**project.settings, "export_metadata": {"title": "Changed"}}
    db.session.commit()
    with pytest.raises(RuntimeError, match="idempotency"):
        _start(setup)
    with pytest.raises(RuntimeError, match="in progress"):
        _start(setup, key="different-key")
    with pytest.raises(LookupError):
        other.export_document(project.id, idempotency_key="other")
    assert len(queue.jobs) == 1


def test_exact_retry_reuses_snapshot_and_new_key_captures_fresh_manifest(setup):
    _app, _project, _client, _other, queue, _references = setup
    first = _start(setup)
    first_manifest = queue.jobs[0][1]["review_manifest"]
    operation = db.session.get(TranslationOperation, first["operation_id"])
    first_fingerprint = operation.request_fingerprint

    queue.review_snapshot_state.update(
        target_revision_id="synthetic-target-revision-2",
        target_version=2,
        coverage_id="synthetic-coverage-2",
    )
    assert _start(setup) == first
    assert queue.review_snapshot_calls == ["none"]
    assert queue.jobs[0][1]["review_manifest"] == first_manifest
    assert operation.request_fingerprint == first_fingerprint

    _publish(setup, first["operation_id"])
    second = _start(setup, key="export-2")
    second_manifest = queue.jobs[1][1]["review_manifest"]
    assert second["operation_id"] != first["operation_id"]
    assert queue.review_snapshot_calls == ["none", "none"]
    assert second_manifest != first_manifest
    assert second_manifest["targets"][0]["target_revision_id"] == (
        "synthetic-target-revision-2"
    )
    assert second_manifest["coverage_manifest"]["blocks"][0]["coverage"][0][
        "coverage_id"
    ] == "synthetic-coverage-2"


def test_epub_options_are_fingerprinted_and_reused_on_omitted_retry(setup):
    _app, project, client, _other, queue, _references = setup
    started = client.export_document(project.id, idempotency_key="options-profile")
    operation = db.session.get(TranslationOperation, started["operation_id"])
    expected_options = normalize_epub_options({})

    assert queue.jobs[0][1]["epub_options"] == expected_options
    assert operation.checkpoint_json["epub_options"] == expected_options
    assert "epub_options" in queue.jobs[0][1]
    assert client.export_document(
        project.id, idempotency_key="options-profile"
    ) == started
    assert len(queue.jobs) == 1
    with pytest.raises(RuntimeError, match="idempotency"):
        client.export_document(
            project.id,
            idempotency_key="options-profile",
            epub_options={"direction": "rtl"},
        )


@pytest.mark.parametrize("mode", ["jpeg", "webp"])
def test_epub_image_mode_is_fingerprinted_and_reused_on_omitted_retry(setup, mode):
    _app, project, client, _other, queue, _references = setup
    started = client.export_document(project.id, idempotency_key="image-mode", epub_image_mode=mode)
    operation = db.session.get(TranslationOperation, started["operation_id"])
    assert queue.jobs[0][1]["epub_image_mode"] == mode
    assert operation.checkpoint_json["epub_image_mode"] == mode
    assert client.export_document(project.id, idempotency_key="image-mode") == started
    assert len(queue.jobs) == 1
    for changed in ("auto", "webp" if mode == "jpeg" else "jpeg"):
        with pytest.raises(RuntimeError, match="idempotency"):
            client.export_document(project.id, idempotency_key="image-mode", epub_image_mode=changed)
    worker._export_metadata_for_operation(queue.jobs[0][1], operation)
    with pytest.raises(ValueError, match="changed before execution"):
        worker._export_metadata_for_operation({**queue.jobs[0][1], "epub_image_mode": "auto"}, operation)


@pytest.mark.parametrize("mode", [None, "preserve", "JPEG", 1, True, []])
def test_export_rejects_invalid_image_modes(setup, mode):
    _app, project, client, _other, queue, _references = setup
    with pytest.raises(ValueError, match="epub-image-mode-invalid"):
        client.export_document(project.id, idempotency_key="bad-mode", epub_image_mode=mode)
    assert queue.jobs == []


def test_new_epub_export_defaults_to_jpeg_and_reuses_saved_mode(setup):
    _app, project, client, _other, queue, _references = setup
    first = client.export_document(project.id, idempotency_key="jpeg-default")
    operation = db.session.get(TranslationOperation, first["operation_id"])
    assert queue.jobs[0][1]["epub_image_mode"] == "jpeg"
    assert operation.checkpoint_json["epub_image_mode"] == "jpeg"
    assert client.export_document(project.id, idempotency_key="jpeg-default") == first
    assert len(queue.jobs) == 1


def test_explicit_auto_replays_as_legacy_absent_mode(setup):
    _app, project, client, _other, queue, _references = setup
    explicit_auto = client.export_document(
        project.id, idempotency_key="explicit-auto", epub_image_mode="auto"
    )
    operation = db.session.get(TranslationOperation, explicit_auto["operation_id"])
    assert "epub_image_mode" not in queue.jobs[0][1]
    assert "epub_image_mode" not in operation.checkpoint_json
    # This queued record models an older request whose absent mode meant Auto.
    assert client.export_document(project.id, idempotency_key="explicit-auto") == explicit_auto


def test_non_epub_export_omission_keeps_auto(setup):
    _app, project, client, _other, queue, _references = setup
    client.export_document(
        project.id, export_format="docx", idempotency_key="non-epub-default"
    )
    assert "epub_image_mode" not in queue.jobs[0][1]


def test_epub_review_hash_is_fingerprinted_and_reused_on_omitted_retry(setup):
    _app, project, client, _other, queue, _references = setup
    review_hash = "A" * 64
    started = client.export_document(
        project.id,
        idempotency_key="review-hash-export",
        epub_review_hash=review_hash,
    )
    operation = db.session.get(TranslationOperation, started["operation_id"])

    assert queue.jobs[0][1]["epub_review_hash"] == review_hash.lower()
    assert operation.checkpoint_json["epub_review_hash"] == review_hash.lower()
    assert client.export_document(
        project.id, idempotency_key="review-hash-export"
    ) == started
    assert len(queue.jobs) == 1
    with pytest.raises(RuntimeError, match="idempotency"):
        client.export_document(
            project.id,
            idempotency_key="review-hash-export",
            epub_review_hash="b" * 64,
        )


def test_export_get_projects_only_witnessed_publishing_quality(setup):
    _app, project, client, _other, _queue, _references = setup
    started = _start(setup, key="quality-export")
    input_sha256 = "a" * 64
    options = normalize_epub_options({})
    output = _publish(setup, started["operation_id"])
    operation = db.session.get(TranslationOperation, started["operation_id"])
    operation.checkpoint_json = {
        **operation.checkpoint_json,
        "semantic_html_sha256": input_sha256,
    }
    artifact = dict(operation.result_json["artifact"])
    quality = {
        "schema": "epub-quality-v1",
        "profile": "publishing-v1",
        "input_sha256": input_sha256,
        "options_sha256": epub_value_sha256(options),
        "artifact_sha256": artifact["sha256"],
        "content_sha256": hashlib.sha256(b"verified content").hexdigest(),
        "checks": {
            "structure": "passed",
            "epubcheck": "not_run",
            "ace": "not_run",
            "manual": "not_reviewed",
        },
        "counts": {
            "notes": 0,
            "references": 0,
            "backlinks": 0,
            "headings": 0,
            "images": 0,
            "tables": 0,
            "pages": 0,
        },
        "issues": [],
        "issue_count": 0,
        "epubcheck_version": None,
    }
    artifact["quality"] = quality
    operation.result_json = {"format": "epub", "artifact": artifact}
    db.session.commit()

    readback = client.export_document_get(project.id, started["operation_id"])

    assert output.is_file()
    assert readback["ready"] is True
    assert readback["quality"] == quality
    assert client.export_document(
        project.id, idempotency_key="quality-export"
    )["quality"] == quality

    altered = dict(quality)
    altered["options_sha256"] = "b" * 64
    artifact["quality"] = altered
    operation.result_json = {"format": "epub", "artifact": artifact}
    db.session.commit()
    with pytest.raises(McpExportError) as mismatch:
        client.export_document_get(project.id, started["operation_id"])
    assert mismatch.value.code == "artifact_invalid"

    unknown = {**quality, "source_html": "private"}
    artifact["quality"] = unknown
    operation.result_json = {"format": "epub", "artifact": artifact}
    db.session.commit()
    with pytest.raises(McpExportError) as invalid:
        client.export_document_get(project.id, started["operation_id"])
    assert invalid.value.code == "artifact_invalid"


def test_historical_epub_retry_keeps_omitted_options_absent(setup):
    _app, project, client, _other, queue, _references = setup
    legacy = client.export_document(
        project.id, idempotency_key="historical-options", epub_options=None
    )
    operation = db.session.get(TranslationOperation, legacy["operation_id"])

    assert "epub_options" not in queue.jobs[0][1]
    assert "epub_options" not in operation.checkpoint_json
    assert "epub_review_hash" not in queue.jobs[0][1]
    assert "epub_review_hash" not in operation.checkpoint_json
    assert client.export_document(
        project.id, idempotency_key="historical-options"
    ) == legacy
    assert len(queue.jobs) == 1


def test_non_epub_export_rejects_explicit_epub_options(setup):
    _app, project, client, _other, queue, _references = setup

    with pytest.raises(ValueError, match="EPUB publishing options require"):
        client.export_document(
            project.id,
            export_format="docx",
            idempotency_key="non-epub-options",
            epub_options={},
        )
    assert not queue.jobs


def test_non_epub_export_rejects_epub_review_hash(setup):
    _app, project, client, _other, queue, _references = setup

    with pytest.raises(ValueError, match="EPUB review hashes require"):
        client.export_document(
            project.id,
            export_format="docx",
            idempotency_key="non-epub-review-hash",
            epub_review_hash="a" * 64,
        )
    assert not queue.jobs


def test_review_requirement_is_forwarded_and_pinned(setup):
    _app, project, _client, _other, queue, _references = setup
    started = _start(
        setup, key="review-required", review_requirement="target_reading"
    )
    job_payload = queue.jobs[0][1]
    operation = db.session.get(TranslationOperation, started["operation_id"])
    manifest = job_payload["review_manifest"]

    assert job_payload["review_requirement"] == "target_reading"
    assert manifest["requirement"] == "target_reading"
    assert operation.checkpoint_json["review_requirement"] == "target_reading"
    assert operation.checkpoint_json["review_manifest"] == manifest
    assert started["review"]["requirement"] == "target_reading"
    assert operation.request_fingerprint == operations.canonical_request_fingerprint(
        {
            "format": "epub",
            "numbering_revision": 0,
            "export_metadata": {"title": "Saved edition"},
            "epub_options": normalize_epub_options({}),
            "epub_image_mode": "jpeg",
            "source_content_hash": "a" * 64,
            "source_plan_id": TranslationSourcePlan.query.filter_by(
                project_id=project.id
            ).one().id,
            "source_plan_hash": "d" * 64,
            "semantic_export_provider": PROVIDER_REFERENCE,
            "review_requirement": "target_reading",
            "review_manifest": manifest,
        }
    )


def test_pdf_heading_flag_is_pinned_only_when_true(setup):
    _app, project, client, _other, queue, _references = setup
    first = client.export_document(
        project.id, export_format="pdf", idempotency_key="pdf-flag",
        start_h2_on_new_page=True,
    )
    assert first["format"] == "pdf"
    assert queue.jobs[0][1]["start_h2_on_new_page"] is True
    operation = db.session.get(TranslationOperation, first["operation_id"])
    assert operation.checkpoint_json["start_h2_on_new_page"] is True
    assert client.export_document(
        project.id, export_format="pdf", idempotency_key="pdf-flag",
        start_h2_on_new_page=True,
    ) == first
    with pytest.raises(RuntimeError, match="idempotency"):
        client.export_document(
            project.id, export_format="pdf", idempotency_key="pdf-flag",
            start_h2_on_new_page=False,
        )
    for invalid in (1, None, "true"):
        with pytest.raises(ValueError, match="start_h2_on_new_page"):
            client.export_document(
                project.id, export_format="pdf", idempotency_key="invalid-flag",
                start_h2_on_new_page=invalid,
            )
    with pytest.raises(ValueError, match="start_h2_on_new_page"):
        client.export_document(
            project.id, export_format="epub", idempotency_key="invalid-format",
            start_h2_on_new_page=True,
        )


def test_exact_operation_pending_lag_and_chunk_reassembly(setup):
    _app, project, client, other, queue, _references = setup
    started = _start(setup)
    operation_id = started["operation_id"]
    assert client.export_document_get(project.id, operation_id)["ready"] is False
    output = _publish(setup, operation_id)
    queue.statuses.pop(started["queue_job_id"])
    assert client.export_document_get(project.id, operation_id)["ready"] is False
    queue.statuses[started["queue_job_id"]] = {
        "status": "completed",
        "result": {
            "kind": "file_download",
            "output_path": str(output),
            "download_name": output.name,
            "mimetype": "application/epub+zip",
            "user_id": project.user_id,
            "project_id": project.id,
            "format": "epub",
        },
    }
    # A later operation must not alter this exact operation's retrieval.
    later = _start(setup, key="later")
    assert later["operation_id"] != operation_id
    metadata = client.export_document_get(project.id, operation_id)
    assert metadata["ready"] is True
    assert metadata["filename"] == output.name
    assert "content_base64" not in metadata and "output_path" not in metadata
    review_manifest = queue.jobs[0][1]["review_manifest"]
    assert metadata["review"] == {
        "requirement": "none",
        "manifest_sha256": review_manifest["sha256"],
        "summary": review_manifest["coverage_manifest"]["summary"],
    }
    assert "review_manifest" not in metadata and "targets" not in metadata
    digest = metadata["sha256"]
    chunks = []
    offset = 0
    while True:
        result = client.export_document_get(
            project.id,
            operation_id,
            include_content=True,
            offset=offset,
            limit=7,
            expected_sha256=digest,
        )
        chunk = base64.b64decode(result["content_base64"])
        assert hashlib.sha256(chunk).hexdigest() == result["chunk_sha256"]
        chunks.append(chunk)
        offset = result["next_offset"]
        if result["eof"]:
            break
    assert b"".join(chunks) == EPUB
    assert hashlib.sha256(b"".join(chunks)).hexdigest() == digest
    end = client.export_document_get(
        project.id, operation_id, include_content=True, offset=len(EPUB)
    )
    assert end["content_base64"] == "" and end["eof"] is True
    with pytest.raises(LookupError):
        other.export_document_get(project.id, operation_id)


def test_queue_publish_failure_retry_returns_exact_terminal_operation(setup):
    _app, project, client, _other, queue, _references = setup
    queue.fail_enqueue = True
    with pytest.raises(RuntimeError, match="synthetic queue unavailable"):
        _start(setup)
    queue.fail_enqueue = False
    retry = _start(setup)
    assert retry["state"] == "failed"
    assert retry["queue_job_id"] is None
    assert retry["error_code"] == "queue_publish_failed"
    assert "synthetic queue" not in retry["message"]
    operation = TranslationOperation.query.filter_by(
        project_id=project.id, idempotency_key="export-1"
    ).one()
    assert retry["operation_id"] == operation.id
    assert queue.jobs == []


@pytest.mark.parametrize(
    ("export_format", "data", "media_type"),
    [
        ("html", b"<html><body>synthetic</body></html>", "text/html"),
        (
            "docx",
            b"PK\x03\x04synthetic word archive",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        ("pdf", b"%PDF-1.7\nsynthetic", "application/pdf"),
    ],
)
def test_html_and_docx_metadata_and_content(setup, export_format, data, media_type):
    _app, project, client, _other, _queue, _references = setup
    started = _start(setup, export_format=export_format)
    _publish(
        setup,
        started["operation_id"],
        data=data,
        export_format=export_format,
        filename=f"Synthetic_export.{export_format}",
    )
    metadata = client.export_document_get(project.id, started["operation_id"])
    assert metadata["format"] == export_format
    assert metadata["media_type"] == media_type
    assert metadata["byte_length"] == len(data)
    assert "content_base64" not in metadata
    content = client.export_document_get(project.id, started["operation_id"], include_content=True)
    assert base64.b64decode(content["content_base64"]) == data
    assert content["eof"] is True


def test_rejects_offset_beyond_end_and_missing_secure_open_primitive(setup, monkeypatch):
    _app, project, client, _other, _queue, _references = setup
    started = _start(setup)
    _publish(setup, started["operation_id"])
    with pytest.raises(McpExportError) as offset:
        client.export_document_get(project.id, started["operation_id"], offset=len(EPUB) + 1)
    assert offset.value.code == "invalid_offset"
    monkeypatch.setattr(mcp_exports.os, "supports_dir_fd", set())
    with pytest.raises(McpExportError) as unsupported:
        client.export_document_get(project.id, started["operation_id"])
    assert unsupported.value.code == "artifact_access_unsupported"


@pytest.mark.parametrize(
    "field,value",
    [
        ("user_id", 999),
        ("project_id", "other-project"),
        ("format", "docx"),
        ("kind", "other"),
        ("mimetype", "text/plain"),
        ("output_path", "/tmp/elsewhere.epub"),
    ],
)
def test_rejects_mismatched_delivery(setup, field, value):
    _app, project, client, _other, queue, _references = setup
    started = _start(setup)
    _publish(setup, started["operation_id"])
    queue.statuses[started["queue_job_id"]]["result"][field] = value
    with pytest.raises(McpExportError, match="record|location|filename"):
        client.export_document_get(project.id, started["operation_id"])


def test_rejects_symlink_missing_size_and_checksum(setup, tmp_path):
    _app, project, client, _other, queue, _references = setup
    started = _start(setup)
    output = _publish(setup, started["operation_id"])
    with pytest.raises(McpExportError) as mismatch:
        client.export_document_get(
            project.id, started["operation_id"], expected_sha256="0" * 64
        )
    assert mismatch.value.code == "checksum_mismatch"
    output.write_bytes(EPUB + b"tamper")
    with pytest.raises(McpExportError) as size:
        client.export_document_get(project.id, started["operation_id"])
    assert size.value.code == "artifact_invalid"
    output.unlink()
    with pytest.raises(McpExportError) as missing:
        client.export_document_get(project.id, started["operation_id"])
    assert missing.value.code == "artifact_unavailable"
    outside = tmp_path / "outside.epub"
    outside.write_bytes(EPUB)
    output.symlink_to(outside)
    with pytest.raises(McpExportError):
        client.export_document_get(project.id, started["operation_id"])
    output.unlink()
    output.write_bytes(EPUB)
    op_dir = output.parent
    op_dir.rename(op_dir.with_name(op_dir.name + "-saved"))
    op_dir.symlink_to(
        op_dir.with_name(op_dir.name + "-saved"), target_is_directory=True
    )
    with pytest.raises(McpExportError):
        client.export_document_get(project.id, started["operation_id"])
    assert queue.jobs


def test_rejects_traversal_wrong_operation_directory_and_artifact_witness(setup):
    app, project, client, _other, queue, _references = setup
    started = _start(setup)
    operation_id = started["operation_id"]
    output = _publish(setup, operation_id)
    delivery = queue.statuses[started["queue_job_id"]]["result"]
    other_dir = Path(app.instance_path) / "translator_exports" / "other-operation"
    other_dir.mkdir(parents=True)
    wrong_file = other_dir / output.name
    wrong_file.write_bytes(EPUB)
    delivery["output_path"] = str(wrong_file)
    with pytest.raises(McpExportError) as wrong_operation:
        client.export_document_get(project.id, operation_id)
    assert wrong_operation.value.code == "artifact_invalid"
    delivery["output_path"] = str(
        output.parent / ".." / "other-operation" / output.name
    )
    with pytest.raises(McpExportError) as traversal:
        client.export_document_get(project.id, operation_id)
    assert traversal.value.code == "artifact_invalid"
    delivery["output_path"] = str(output)
    operation = db.session.get(TranslationOperation, operation_id)
    artifact = dict(operation.result_json["artifact"])
    artifact["sha256"] = "0" * 64
    artifact["quality"] = {
        **artifact["quality"],
        "artifact_sha256": "0" * 64,
    }
    operation.result_json = {"format": "epub", "artifact": artifact}
    db.session.commit()
    result = client.export_document_get(project.id, operation_id)
    assert result["sha256"] == "0" * 64
    # The stored digest is the worker's creation witness; chunk reads do not
    # rehash the whole file. A caller detects this corruption on reassembly.
    chunk = client.export_document_get(project.id, operation_id, include_content=True)
    assert (
        hashlib.sha256(base64.b64decode(chunk["content_base64"])).hexdigest()
        != result["sha256"]
    )
    artifact["sha256"] = "malformed"
    operation.result_json = {"format": "epub", "artifact": artifact}
    db.session.commit()
    with pytest.raises(McpExportError) as corrupt:
        client.export_document_get(project.id, operation_id)
    assert corrupt.value.code == "artifact_invalid"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"include_content": 1},
        {"offset": True},
        {"offset": -1},
        {"limit": False},
        {"limit": 0},
        {"limit": 65537},
        {"expected_sha256": "invalid"},
        {"operation_id": "../other"},
    ],
)
def test_rejects_malformed_read_arguments(setup, kwargs):
    _app, project, client, _other, _queue, _references = setup
    started = _start(setup)
    operation_id = kwargs.pop("operation_id", started["operation_id"])
    with pytest.raises(McpExportError):
        client.export_document_get(project.id, operation_id, **kwargs)


def test_rejects_invalid_start_arguments_without_queueing(setup):
    _app, project, client, _other, queue, _references = setup
    for export_format in ("PDF", "EPUB", "", None, True):
        with pytest.raises(ValueError):
            client.export_document(
                project.id, export_format=export_format, idempotency_key="valid"
            )
    for key in ("", "  ", None, 1, "x" * 129):
        with pytest.raises(ValueError):
            client.export_document(project.id, idempotency_key=key)
    assert queue.jobs == []
