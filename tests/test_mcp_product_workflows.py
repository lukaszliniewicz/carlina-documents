from __future__ import annotations

import asyncio
import hashlib
import socket
import uuid
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest
from content_storage_support import bind_content_policy
from flask import Flask
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from iarf_assistant.addons.doc_processor import models as _doc_models  # noqa: F401
from iarf_assistant.addons.translator import (
    alignment_persistence,
    block_continuation,
    block_persistence,
    block_translation,
    block_workflow_runtime,
    models,
    operation_delivery,
    operations,
    target_contract,
    target_persistence,
    worker as translator_worker,
)
from iarf_assistant.addons.translator.api_client import TranslatorClient
from iarf_assistant.addons.translator.models import (
    TranslationBlockWorkflowConfig,
    TranslationOperation,
    TranslationResearchDossierRevision,
)
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.core.mcp_gateway_runtime import start_mcp_gateway
from iarf_assistant.core.mcp_jobs import get_execution, issue_access_key
from iarf_assistant.extensions import db
from iarf_assistant.models import LLMModel, LLMProvider, McpExecution, User

EXECUTOR_PROFILE = {
    "schema_version": "carlina.external-executor.v1",
    "max_input_tokens": 32_000,
    "max_output_tokens": 4_000,
    "input_modalities": ["text", "image"],
    "supports_tools": True,
    "supports_structured_output": True,
    "supports_reasoning": True,
    "user_confirmed": True,
}


class _Queue:
    def __init__(self):
        self.enqueued: list[tuple[str, dict]] = []

    def enqueue(self, task_name: str, payload: dict) -> str:
        job_id = f"job-{len(self.enqueued) + 1}"
        self.enqueued.append((task_name, dict(payload)))
        return job_id

    def get_status(self, job_id: str) -> dict:
        return getattr(self, "statuses", {}).get(job_id, {"status": "queued", "job_id": job_id})

    def set_status(self, job_id: str, status: dict):
        if not hasattr(self, "statuses"):
            self.statuses = {}
        self.statuses[job_id] = status


class _DocContext:
    def __init__(self, application: Flask, queue: _Queue):
        self.application = application
        self.queue = queue

    @staticmethod
    def capability_candidates(_domain, include_inactive=False):
        del include_inactive
        return ()

    @staticmethod
    def ensure_worker_pool(_script_name, *, num_workers=1):
        del num_workers

    def get_task_queue(self, addon_id: str):
        assert addon_id == "doc_processor"
        return self.queue


class _TranslatorContext:
    def __init__(self, queue: _Queue):
        self.queue = queue

    def get_task_queue(self, addon_id: str):
        assert addon_id == "translator"
        return self.queue


def _run_source_planning(gateway, operation):
    """Drive the real queued planner instead of faking its terminal state."""
    payload = next(
        payload for handler, payload in gateway.translator_queue.enqueued
        if handler == "plan_document_blocks" and payload["operation_id"] == operation.id
    )
    translator_worker.plan_document_blocks(payload, gateway.translator_queue, operation.queue_job_id)
    db.session.refresh(operation)
    assert operation.state == "completed"


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@pytest.fixture
def product_gateway(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    upload_root = tmp_path / "uploads"
    source_root = tmp_path / "sources"
    source_root.mkdir()
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'products.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="mcp-product-test",
        TESTING=True,
        CARLINA_UPLOADS_DIR=str(upload_root),
    )
    db.init_app(application)
    application.extensions["carlina_encryption"] = EncryptionService(
        InstallationKeyStore.ephemeral()
    )
    doc_queue = _Queue()
    translator_queue = _Queue()
    doc_addon = SimpleNamespace(context=_DocContext(application, doc_queue))
    translator_context = _TranslatorContext(translator_queue)
    source_html = "<main><h1>Harness book</h1><p>Hello from the MCP harness.</p></main>"
    source_hash = hashlib.sha256(source_html.encode("utf-8")).hexdigest()
    document_provider = SimpleNamespace(
        client=SimpleNamespace(
            get_document_snapshot=lambda document_id: {
                "id": document_id,
                "document_id": document_id,
                "snapshot_contract_version": "document-snapshot-v1",
                "status": "completed",
                "output_html": source_html,
                "content_hash": source_hash,
                "semantic_contract_version": "1",
                "semantic_contract_mode": "completed",
                "semantic_content_hash": source_hash,
                "filename": "harness-book.html",
                "captured_at": "2026-09-02T12:00:00+00:00",
                "source_updated_at": "2026-09-02T11:59:00+00:00",
                "metadata": {"title": "Harness book"},
                "provenance": {},
            }
        ),
        reference=lambda **kwargs: {
            "version": 1,
            "capability": "document.processing",
            "provider_addon_id": "doc_processor",
            "provider_capability_id": "document.processing",
            **kwargs,
        },
    )
    semantic_export_provider = SimpleNamespace(
        provider_reference=lambda: {
            "version": 1,
            "capability": "document.semantic_export",
            "provider_addon_id": "doc_processor",
            "provider_capability_id": "document.semantic_export",
        }
    )
    translator_addon = SimpleNamespace(
        initialize_client=lambda config: TranslatorClient(
            translator_context,
            config,
            document_provider_factory=lambda _reference: document_provider,
            semantic_export_provider_factory=lambda _reference: semantic_export_provider,
        )
    )
    addons = {"doc_processor": doc_addon, "translator": translator_addon}
    application.addon_manager = SimpleNamespace(
        get_loaded_addon=lambda addon_id: addons.get(addon_id)
    )
    with application.app_context():
        db.create_all()
        owner = User(id=1, email="products@example.test", password_hash="x")
        provider = LLMProvider(
            id=1,
            user_id=1,
            name="harness_subscription",
            display_name="Harness subscription",
        )
        model = LLMModel(
            id=1,
            user_id=1,
            provider_id=1,
            model_id="harness_subscription/chat",
            display_name="Harness model profile",
            is_active=True,
            model_type="chat",
            context_window=32_000,
            max_output_tokens=4_000,
        )
        db.session.add_all((owner, provider, model))
        db.session.commit()
        issued = issue_access_key(
            1,
            "product workflow test",
            [
                "mcp.access",
                "mcp.work",
                "mcp.doc_processor",
                "mcp.translator",
            ],
            [str(source_root)],
        )

    port = _unused_port()
    handle = start_mcp_gateway(application, host="127.0.0.1", port=port)
    try:
        yield SimpleNamespace(
            app=application,
            endpoint=f"http://127.0.0.1:{port}/mcp",
            token=issued.raw_token,
            source_root=source_root,
            doc_queue=doc_queue,
            translator_queue=translator_queue,
        )
    finally:
        handle.stop()
        with application.app_context():
            db.session.remove()
            db.drop_all()
            db.engine.dispose()


async def _with_client(product_gateway, calls):
    async with httpx2.AsyncClient(
        headers={"authorization": f"Bearer {product_gateway.token}"}
    ) as http:
        transport = streamable_http_client(product_gateway.endpoint, http_client=http)
        async with Client(transport, mode="2026-07-28") as client:
            return await calls(client)


@pytest.mark.parametrize("include_details", [False, True])
def test_document_import_configure_start_and_poll_over_mcp(
    product_gateway, include_details
):
    source = product_gateway.source_root / "notes.txt"
    source.write_text("A small synthetic document.", encoding="utf-8")

    async def calls(client: Client):
        imported = await client.call_tool(
            "carlina_document_import",
            {"source_path": str(source), "settings": {"engine": "native"}},
        )
        document_id = imported.structured_content["id"]
        configured = await client.call_tool(
            "carlina_document_configure",
            {"document_id": document_id, "settings": {"engine": "native"}},
        )
        started = await client.call_tool(
            "carlina_document_start",
            {
                "document_id": document_id,
                "mode": "passive",
                "executor_profile": EXECUTOR_PROFILE,
                "include_details": include_details,
            },
        )
        assert not started.is_error, started.content
        polled = await client.call_tool(
            "carlina_job_get",
            {"execution_id": started.structured_content["execution"]["execution_id"]},
        )
        return imported, configured, started, polled

    imported, configured, started, polled = asyncio.run(
        _with_client(product_gateway, calls)
    )
    assert not imported.is_error
    assert not configured.is_error
    assert not started.is_error
    assert started.structured_content["execution"]["mode"] == "passive"
    if include_details:
        assert started.structured_content["queue_job_id"] == "job-1"
    else:
        assert "queue_job_id" not in started.structured_content
        assert "settings" not in started.structured_content["document"]
        assert "metadata" not in started.structured_content["document"]
    assert polled.structured_content["product"]["state"] == "queued"
    assert product_gateway.doc_queue.enqueued[0][0] == "process_document"


@pytest.mark.parametrize("recoverable_state", ["failed", "partial", "cancelled"])
def test_translator_create_start_and_poll_passive_without_api_key(
    product_gateway, recoverable_state
):
    async def calls(client: Client):
        created = await client.call_tool(
            "carlina_translator_create",
            {
                "name": "Harness translation",
                "source_lang": "Hebrew",
                "target_lang": "English",
                "document_id": "doc-ready",
                "mode": "passive",
                "settings": {
                    "enable_pre_analysis": False,
                    "annotation_profile": "study",
                },
            },
        )
        assert not created.is_error
        with product_gateway.app.app_context():
            operation = TranslationOperation.query.filter_by(
                project_id=created.structured_content["project_id"],
                kind="plan_document_blocks",
            ).one()
            _run_source_planning(product_gateway, operation)
        started = await client.call_tool(
            "carlina_translator_start",
            {
                "project_id": created.structured_content["project_id"],
                "mode": "passive",
                "executor_profile": EXECUTOR_PROFILE,
            },
        )
        assert not started.is_error, started.content
        polled = await client.call_tool(
            "carlina_job_get",
            {"execution_id": started.structured_content["execution"]["execution_id"]},
        )
        return created, started, polled

    created, started, polled = asyncio.run(_with_client(product_gateway, calls))
    assert not created.is_error
    assert (
        created.structured_content["project"]["settings"]["annotation_profile"]
        == "study"
    )
    assert created.structured_content["project"]["source_lang"] == "he"
    assert created.structured_content["project"]["target_lang"] == "en"
    assert not started.is_error
    assert started.structured_content["execution"]["mode"] == "passive"
    assert polled.structured_content["product"]["state"] == "queued"
    assert [item[0] for item in product_gateway.translator_queue.enqueued] == [
        "plan_document_blocks",
        "translate_document_blocks",
    ]

    with product_gateway.app.app_context():
        first = TranslationOperation.query.filter_by(
            project_id=created.structured_content["project_id"],
            owner_user_id=1,
            kind="document_block_translate",
        ).one()
        first_id = first.id
        first_key = first.idempotency_key
        assert first_key == f"translate-{first.source_plan_hash[:48]}-ctxv2-self_contained"
        first_queue_job_id = first.queue_job_id
        first_pins = (
            first.source_plan_id,
            first.source_plan_hash,
            first.source_content_hash,
        )
        first_config = TranslationBlockWorkflowConfig.query.filter_by(
            operation_id=first.id
        ).one()
        assert first_config.config_json["translation_context"] == {
            "contract_version": "translation-context-v2",
            "continuity_mode": "self_contained",
        }
        assert first_config.config_json["protected_targets"] == {
            "contract_version": "translator-protected-targets-v1",
            "targets": [],
        }
        assert first_config.config_json["translation_scope"] == {
            "contract_version": "translator-translation-scope-v1",
            "mode": "pending",
            "block_ids": [],
        }
        first.state = recoverable_state
        if recoverable_state == "cancelled":
            first.cancel_requested_at = first.updated_at
        first.error_code = "synthetic_failure"
        first.message = "Synthetic MCP retry failure."
        db.session.commit()
        assert first.state == recoverable_state

    async def restart(client: Client):
        restarted = await client.call_tool(
            "carlina_translator_start",
            {
                "project_id": created.structured_content["project_id"],
                "mode": "passive",
                "executor_profile": EXECUTOR_PROFILE,
            },
        )
        assert not restarted.is_error, restarted.content
        polled_restart = await client.call_tool(
            "carlina_job_get",
            {"execution_id": restarted.structured_content["execution"]["execution_id"]},
        )
        return restarted, polled_restart

    async def poll_incomplete(client: Client):
        return await client.call_tool(
            "carlina_job_get",
            {"execution_id": started.structured_content["execution"]["execution_id"]},
        )

    incomplete = asyncio.run(_with_client(product_gateway, poll_incomplete))
    assert not incomplete.is_error, incomplete.content
    assert incomplete.structured_content["execution"]["status"] == (
        "cancelled" if recoverable_state == "cancelled" else "failed"
    )
    assert incomplete.structured_content["product"]["latest_operation"]["state"] == (
        recoverable_state
    )

    restarted, polled_restart = asyncio.run(_with_client(product_gateway, restart))
    assert not restarted.is_error
    assert polled_restart.structured_content["product"]["state"] == "queued"

    with product_gateway.app.app_context():
        first = db.session.get(TranslationOperation, first_id)
        assert first is not None
        assert first.idempotency_key == first_key
        assert first.state == recoverable_state
        assert first.queue_job_id == first_queue_job_id
        second = (
            TranslationOperation.query.filter_by(
                project_id=created.structured_content["project_id"],
                owner_user_id=1,
                kind="document_block_translate",
            )
            .order_by(
                TranslationOperation.created_at.desc(), TranslationOperation.id.desc()
            )
            .first()
        )
        assert second is not None
        assert second.id != first_id
        assert second.idempotency_key == f"recover-{first_id}"
        assert (
            second.source_plan_id,
            second.source_plan_hash,
            second.source_content_hash,
        ) == first_pins
        assert second.queue_job_id == "job-3"
        second_id = second.id
        assert (
            product_gateway.translator_queue.enqueued[1][1]["operation_id"]
            == first_id
        )
        assert (
            product_gateway.translator_queue.enqueued[2][1]["operation_id"]
            == second_id
        )
        second.state = recoverable_state
        if recoverable_state == "cancelled":
            second.cancel_requested_at = second.updated_at
        second.error_code = "synthetic_failure"
        second.message = "Synthetic chained retry failure."
        db.session.commit()

    chained, chained_poll = asyncio.run(_with_client(product_gateway, restart))
    assert not chained.is_error, chained.content
    assert chained_poll.structured_content["product"]["state"] == "queued"

    with product_gateway.app.app_context():
        latest = (
            TranslationOperation.query.filter_by(
                project_id=created.structured_content["project_id"],
                owner_user_id=1,
                kind="document_block_translate",
            )
            .order_by(
                TranslationOperation.created_at.desc(), TranslationOperation.id.desc()
            )
            .first()
        )
        assert latest is not None
        assert latest.id not in {first_id, second_id}
        assert latest.idempotency_key == f"recover-{second_id}"
        assert latest.queue_job_id == "job-4"
        latest_id = latest.id
        latest_key = latest.idempotency_key

    assert [item[0] for item in product_gateway.translator_queue.enqueued] == [
        "plan_document_blocks",
        "translate_document_blocks",
        "translate_document_blocks",
        "translate_document_blocks",
    ]
    assert (
        product_gateway.translator_queue.enqueued[3][1]["operation_id"] == latest_id
    )
    assert (
        chained_poll.structured_content["product"]["latest_operation"]["id"]
        == latest_id
    )

    # A later idempotent start keeps addressing the newest recovery operation;
    # it must never fall back to the original failed base-key operation.
    with product_gateway.app.app_context():
        latest = db.session.get(TranslationOperation, latest_id)
        assert latest is not None
        latest.state = "completed"
        latest.message = "Synthetic recovery complete."
        db.session.commit()

    with product_gateway.app.app_context():
        translator_addon = product_gateway.app.addon_manager.get_loaded_addon(
            "translator"
        )
        repeated = translator_addon.initialize_client(
            {"current_user_id": 1}
        ).start_block_translation(
            created.structured_content["project_id"],
            executor_profile=EXECUTOR_PROFILE,
        )
        assert repeated["job_id"] == "job-4"
        operations = TranslationOperation.query.filter_by(
            project_id=created.structured_content["project_id"],
            owner_user_id=1,
            kind="document_block_translate",
        ).all()
        assert len(operations) == 3
        latest = db.session.get(TranslationOperation, latest_id)
        assert latest is not None
        assert latest.idempotency_key == latest_key

    assert len(product_gateway.translator_queue.enqueued) == 4


def _activate_product_target_pairs(source_plan_id, *, block_limit=1):
    source_record = db.session.get(
        models.TranslationSourcePlan, source_plan_id
    )
    assert source_record is not None
    plan = block_persistence.rehydrate_source_plan(source_record)
    pairs = []
    for block in plan.blocks[:block_limit]:
        candidate = {
            "source_plan_id": source_record.id,
            "source_plan_hash": plan.plan_hash,
            "block_id": block.id,
            "structural_fingerprint": target_contract.structural_fingerprint(
                plan, block
            ),
            "contract_version": "target-block-result-v1",
            "parts": [{"kind": "text", "text": block.projection}],
            "lexical_attributes": [],
        }
        revision = target_persistence.persist_target_revision(
            source_record,
            candidate,
            target_language="en",
            author_type="model",
        )
        span_set = alignment_persistence.persist_target_span_set(
            source_record, revision, verified_plan=plan
        )
        alignment = alignment_persistence.persist_alignment_revision(
            source_record, span_set, verified_plan=plan
        )
        pointer = target_persistence.activate_target_revision(
            source_record,
            block_id=block.id,
            revision_id=revision.id,
            alignment_revision_id=alignment.id,
            expected_version=0,
            verified_plan=plan,
        )
        pairs.append((pointer, alignment))
    db.session.commit()
    db.session.expire_all()
    return source_record, plan, pairs


def test_translator_recovery_freezes_active_pairs_and_scope(product_gateway):
    async def create_and_start(client: Client):
        created = await client.call_tool(
            "carlina_translator_create",
            {
                "name": "Protected pair translation",
                "source_lang": "English",
                "target_lang": "English",
                "document_id": "doc-protected-pairs",
                "mode": "passive",
                "settings": {
                    "enable_pre_analysis": False,
                    "annotation_profile": "study",
                },
            },
        )
        assert not created.is_error, created.content
        project_id = created.structured_content["project_id"]
        with product_gateway.app.app_context():
            planning = TranslationOperation.query.filter_by(
                project_id=project_id,
                kind="plan_document_blocks",
            ).one()
            _run_source_planning(product_gateway, planning)
        started = await client.call_tool(
            "carlina_translator_start",
            {
                "project_id": project_id,
                "mode": "passive",
                "executor_profile": EXECUTOR_PROFILE,
            },
        )
        assert not started.is_error, started.content
        return project_id

    project_id = asyncio.run(_with_client(product_gateway, create_and_start))
    with product_gateway.app.app_context():
        first = TranslationOperation.query.filter_by(
            project_id=project_id,
            owner_user_id=1,
            kind="document_block_translate",
        ).one()
        first_config = TranslationBlockWorkflowConfig.query.filter_by(
            operation_id=first.id
        ).one()
        assert first_config.config_json["protected_targets"]["targets"] == []
        source_record, plan, pairs = _activate_product_target_pairs(
            first.source_plan_id
        )
        assert len(plan.blocks) >= 2
        assert len(pairs) == 1
        target_witness = [
            (
                pointer.block_id,
                pointer.revision_id,
                pointer.version,
                pointer.pair_receipt,
                db.session.get(
                    models.TranslationTargetBlockRevision, pointer.revision_id
                ).revision_record_hash,
            )
            for pointer, _alignment in pairs
        ]
        alignment_witness = [
            (
                pointer.block_id,
                alignment.id,
                alignment.id,
                alignment.record_hash,
            )
            for pointer, alignment in pairs
        ]
        first.state = "partial"
        first.error_code = "synthetic_partial"
        first.message = "Synthetic partial operation for recovery."
        db.session.commit()

        translator_addon = product_gateway.app.addon_manager.get_loaded_addon(
            "translator"
        )
        restarted = translator_addon.initialize_client(
            {"current_user_id": 1}
        ).start_block_translation(
            project_id,
            executor_profile=EXECUTOR_PROFILE,
        )
        assert restarted["job_id"] == "job-3"
        second = (
            TranslationOperation.query.filter_by(
                project_id=project_id,
                owner_user_id=1,
                kind="document_block_translate",
            )
            .order_by(
                TranslationOperation.created_at.desc(), TranslationOperation.id.desc()
            )
            .first()
        )
        assert second is not None and second.id != first.id
        assert second.idempotency_key == f"recover-{first.id}"
        second_config = TranslationBlockWorkflowConfig.query.filter_by(
            operation_id=second.id
        ).one()
        protected = second_config.config_json["protected_targets"]
        targets = protected["targets"]
        assert [item["block_id"] for item in targets] == [pairs[0][0].block_id]
        assert [item["revision_id"] for item in targets] == [
            pointer.revision_id for pointer, _alignment in pairs
        ]
        assert [item["revision_record_hash"] for item in targets] == [
            db.session.get(
                models.TranslationTargetBlockRevision,
                pointer.revision_id,
            ).revision_record_hash
            for pointer, _alignment in pairs
        ]
        verified_ids = block_continuation.verify_protected_targets(
            source_record, protected
        )
        excluded_ids = block_workflow_runtime._translation_scope_exclusions(
            plan_block_ids=frozenset(block.id for block in plan.blocks),
            translation_scope=second_config.config_json["translation_scope"],
            verified_active_target_ids=verified_ids,
        )
        assert verified_ids == {pairs[0][0].block_id}
        assert excluded_ids == verified_ids
        planned = block_translation.plan_ordered_window_materials(
            plan=plan,
            source_plan_id=source_record.id,
            frozen_config=second_config.config_json,
            workflow_config={
                "id": second_config.id,
                "config_hash": second_config.config_hash,
                "config_receipt": second_config.config_receipt,
            },
            execution_profile=(
                block_workflow_runtime.execution_profile_from_frozen_phase(
                    second_config.config_json["translation"]
                )
            ),
            window_profile=block_translation.WindowProfile(
                **second_config.config_json["window_profile"]
            ),
            token_counter=lambda **_kwargs: 10,
            excluded_block_ids=excluded_ids,
        )
        assert [
            block_id
            for material in planned.materials
            for block_id in material.window.block_ids
        ] == [block.id for block in plan.blocks[1:]]
        assert all(
            pairs[0][0].block_id not in material.window.block_ids
            for material in planned.materials
        )
        db.session.expire_all()
        assert [
            (
                pointer.block_id,
                pointer.revision_id,
                pointer.version,
                pointer.pair_receipt,
                db.session.get(
                    models.TranslationTargetBlockRevision, pointer.revision_id
                ).revision_record_hash,
            )
            for pointer, _alignment in pairs
        ] == target_witness
        assert [
            (
                pointer.block_id,
                db.session.get(
                    models.TranslationTargetAlignmentActive,
                    {
                        "source_plan_id": source_record.id,
                        "block_id": pointer.block_id,
                    },
                ).alignment_revision_id,
                db.session.get(
                    models.TranslationAlignmentRevision,
                    alignment.id,
                ).id,
                db.session.get(
                    models.TranslationAlignmentRevision,
                    alignment.id,
                ).record_hash,
            )
            for pointer, alignment in pairs
        ] == alignment_witness


def test_translator_start_forwards_and_persists_explicit_dossier_pin(product_gateway):
    async def calls(client: Client):
        created = await client.call_tool(
            "carlina_translator_create",
            {
                "name": "Pinned translation",
                "target_lang": "pl",
                "document_id": "doc-pinned",
                "mode": "passive",
                "settings": {"enable_pre_analysis": False},
            },
        )
        assert not created.is_error, created.content
        project_id = created.structured_content["project_id"]
        with product_gateway.app.app_context():
            operation = TranslationOperation.query.filter_by(
                project_id=project_id,
                kind="plan_document_blocks",
            ).one()
            _run_source_planning(product_gateway, operation)
            dossier = product_gateway.app.addon_manager.get_loaded_addon(
                "translator"
            ).initialize_client({"current_user_id": 1}).research_dossier_submit(
                project_id,
                "mcp-pinned-dossier",
                {"document_brief": "The selected external dossier."},
                {},
                {"kind": "document", "block_ids": []},
            )
            revision_id = dossier["revision_id"]
        started = await client.call_tool(
            "carlina_translator_start",
            {
                "project_id": project_id,
                "mode": "passive",
                "executor_profile": EXECUTOR_PROFILE,
                "research_dossier_revision_id": revision_id,
            },
        )
        return project_id, revision_id, started

    project_id, revision_id, started = asyncio.run(_with_client(product_gateway, calls))
    assert not started.is_error, started.content
    execution_id = started.structured_content["execution"]["execution_id"]
    with product_gateway.app.app_context():
        execution = get_execution(execution_id, owner_user_id=1)
        assert execution is not None
        assert execution.request_json["research_dossier_revision_id"] == revision_id
        operation = TranslationOperation.query.filter_by(
            project_id=project_id,
            owner_user_id=1,
            kind="document_block_translate",
        ).one()
        assert operation.checkpoint_json["research_dossier_revision_id"] == revision_id
        config = TranslationBlockWorkflowConfig.query.filter_by(
            operation_id=operation.id
        ).one()
        revision = db.session.get(TranslationResearchDossierRevision, revision_id)
        assert revision is not None
        assert config.config_json["research_dossier"] == {
            "revision_id": revision_id,
            "record_hash": revision.record_hash,
            "content_digest": revision.content_digest,
            "conclusion_provenance": "external_unverified",
            "evidence_verification_state": "unverified",
        }
        first_operation_id = operation.id

        operation.checkpoint_json = {
            "phase": "translation",
            "completed_windows": 8,
            "remaining_blocks": 42,
        }
        operation.state = "failed"
        operation.progress = 50
        db.session.commit()
        second_dossier = product_gateway.app.addon_manager.get_loaded_addon(
            "translator"
        ).initialize_client({"current_user_id": 1}).research_dossier_submit(
            project_id,
            "mcp-pinned-dossier-2",
            {"document_brief": "A different selected external dossier."},
            {},
            {"kind": "document", "block_ids": []},
        )
        second_revision_id = second_dossier["revision_id"]

    async def start_second(client: Client):
        return await client.call_tool(
            "carlina_translator_start",
            {
                "project_id": project_id,
                "mode": "passive",
                "executor_profile": EXECUTOR_PROFILE,
                "research_dossier_revision_id": second_revision_id,
            },
        )

    second_started = asyncio.run(_with_client(product_gateway, start_second))
    assert not second_started.is_error, second_started.content
    with product_gateway.app.app_context():
        operations = TranslationOperation.query.filter_by(
            project_id=project_id,
            owner_user_id=1,
            kind="document_block_translate",
        ).all()
        assert len(operations) == 2
        assert len({item.idempotency_key for item in operations}) == 2
        first_operation = next(item for item in operations if item.id == first_operation_id)
        second_operation = next(
            item for item in operations
            if item.checkpoint_json.get("research_dossier_revision_id") == second_revision_id
        )
        second_operation.checkpoint_json = {
            "phase": "context_baseline",
            "baseline_id": "baseline-second-dossier",
        }
        # Historical buggy recoveries used the dossier-blind key below.
        # Keep that collision in the fixture so resuming the first dossier
        # proves that new recovery identities are dossier-aware.
        second_operation.idempotency_key = f"recover-{first_operation_id}"
        second_operation.state = "failed"
        db.session.commit()

    async def resume_first(client: Client):
        return await client.call_tool(
            "carlina_translator_start",
            {
                "project_id": project_id,
                "mode": "passive",
                "executor_profile": EXECUTOR_PROFILE,
                "research_dossier_revision_id": revision_id,
            },
        )

    resumed = asyncio.run(_with_client(product_gateway, resume_first))
    assert not resumed.is_error, resumed.content
    with product_gateway.app.app_context():
        operations = TranslationOperation.query.filter_by(
            project_id=project_id,
            owner_user_id=1,
            kind="document_block_translate",
        ).order_by(TranslationOperation.created_at.asc()).all()
        assert len(operations) == 3
        recovery = operations[-1]
        assert recovery.idempotency_key == (
            f"recover-{first_operation_id}-dossier-{revision_id}"
        )
        assert recovery.checkpoint_json["research_dossier_revision_id"] == revision_id


def test_inactive_translator_start_fails_the_durable_mcp_execution(
    product_gateway, monkeypatch
):
    manager = product_gateway.app.addon_manager
    loaded_addon = manager.get_loaded_addon
    monkeypatch.setattr(
        manager,
        "get_loaded_addon",
        lambda addon_id: None if addon_id == "translator" else loaded_addon(addon_id),
    )

    async def calls(client: Client):
        return await client.call_tool(
            "carlina_translator_start",
            {
                "project_id": "synthetic-inactive-project",
                "mode": "passive",
                "executor_profile": EXECUTOR_PROFILE,
            },
        )

    started = asyncio.run(_with_client(product_gateway, calls))
    assert started.is_error
    assert product_gateway.translator_queue.enqueued == []
    with product_gateway.app.app_context():
        execution = McpExecution.query.filter_by(
            owner_user_id=1,
            addon_id="translator",
            resource_id="synthetic-inactive-project",
        ).one()
        assert execution.status == "failed"
        assert execution.queue_job_id is None
        assert execution.mode == "passive"


async def _create_planned_passive_translation(client, product_gateway, document_id):
    created = await client.call_tool(
        "carlina_translator_create",
        {
            "name": "Queue recovery translation",
            "target_lang": "pl",
            "document_id": document_id,
            "mode": "passive",
            "settings": {"enable_pre_analysis": False},
        },
    )
    assert not created.is_error, created.content
    project_id = created.structured_content["project_id"]
    with product_gateway.app.app_context():
        operation = TranslationOperation.query.filter_by(
            project_id=project_id, kind="plan_document_blocks"
        ).one()
        _run_source_planning(product_gateway, operation)
    return project_id


@pytest.mark.parametrize("delivered_before_error", [False, True])
def test_translator_start_recovers_from_publication_failure(
    product_gateway, monkeypatch, delivered_before_error
):
    enqueue = product_gateway.translator_queue.enqueue

    def failed_enqueue(handler, payload):
        assert handler == "translate_document_blocks"
        if delivered_before_error:
            enqueue(handler, payload)
        raise RuntimeError("synthetic publication failure")

    async def calls(client: Client):
        project_id = await _create_planned_passive_translation(
            client, product_gateway, "doc-publication-recovery"
        )
        request = {
            "project_id": project_id,
            "mode": "passive",
            "executor_profile": EXECUTOR_PROFILE,
        }
        monkeypatch.setattr(product_gateway.translator_queue, "enqueue", failed_enqueue)
        started = await client.call_tool("carlina_translator_start", request)
        assert started.is_error
        with product_gateway.app.app_context():
            failed = TranslationOperation.query.filter_by(
                project_id=project_id, kind="document_block_translate"
            ).one()
            failed_id = failed.id
            pins = (
                failed.source_plan_id,
                failed.source_plan_hash,
                failed.source_content_hash,
            )
            assert failed.state == "failed"
            assert failed.error_code == "queue_publish_failed"
            assert failed.queue_job_id is None
            assert TranslationBlockWorkflowConfig.query.filter_by(
                operation_id=failed_id
            ).count() == 1
            execution = McpExecution.query.filter_by(
                resource_id=project_id, queue_job_id=None
            ).one()
            assert execution.status == "failed"
            if delivered_before_error:
                with pytest.raises(operations.TranslationOperationNotRunnable):
                    operations.claim_operation(
                        failed_id,
                        "job-2",
                        owner_user_id=1,
                        project_id=project_id,
                    )

        monkeypatch.setattr(product_gateway.translator_queue, "enqueue", enqueue)
        resumed = await client.call_tool("carlina_translator_start", request)
        assert not resumed.is_error, resumed.content
        polled = await client.call_tool(
            "carlina_job_get",
            {"execution_id": resumed.structured_content["execution"]["execution_id"]},
        )
        return project_id, failed_id, pins, resumed, polled

    project_id, failed_id, pins, resumed, polled = asyncio.run(
        _with_client(product_gateway, calls)
    )
    assert not polled.is_error
    assert polled.structured_content["product"]["state"] == "queued"
    with product_gateway.app.app_context():
        recovery = TranslationOperation.query.filter_by(
            project_id=project_id, kind="document_block_translate", state="queued"
        ).one()
        assert recovery.idempotency_key == f"recover-{failed_id}"
        assert recovery.queue_job_id == resumed.structured_content["execution"]["queue_job_id"]
        assert (
            recovery.source_plan_id,
            recovery.source_plan_hash,
            recovery.source_content_hash,
        ) == pins


def test_translator_start_preserves_ambiguous_binding_until_delivery_grace(
    product_gateway, monkeypatch
):
    attach = operation_delivery.attach_queue_job

    def failed_binding(operation, _job_id):
        raise operations.TranslationOperationTransitionConflict(operation.id, None)

    async def calls(client: Client):
        project_id = await _create_planned_passive_translation(
            client, product_gateway, "doc-binding-recovery"
        )
        request = {
            "project_id": project_id,
            "mode": "passive",
            "executor_profile": EXECUTOR_PROFILE,
        }
        monkeypatch.setattr(operation_delivery, "attach_queue_job", failed_binding)
        started = await client.call_tool("carlina_translator_start", request)
        assert started.is_error
        with product_gateway.app.app_context():
            pending = TranslationOperation.query.filter_by(
                project_id=project_id, kind="document_block_translate"
            ).one()
            pending_id = pending.id
            assert pending.state == "queued"
            assert pending.queue_job_id is None
            assert pending.error_code is None
            assert product_gateway.translator_queue.enqueued[-1][1]["operation_id"] == pending_id
            execution = McpExecution.query.filter_by(
                resource_id=project_id, queue_job_id=None
            ).one()
            assert execution.status == "failed"

        monkeypatch.setattr(operation_delivery, "attach_queue_job", attach)
        immediate = await client.call_tool("carlina_translator_start", request)
        assert immediate.is_error
        assert len(product_gateway.translator_queue.enqueued) == 2
        with product_gateway.app.app_context():
            pending = db.session.get(TranslationOperation, pending_id)
            assert pending.state == "queued"
            pending.updated_at -= operations.QUEUED_DELIVERY_GRACE + timedelta(seconds=1)
            db.session.commit()

        resumed = await client.call_tool("carlina_translator_start", request)
        assert not resumed.is_error, resumed.content
        return project_id, pending_id, resumed

    project_id, pending_id, resumed = asyncio.run(_with_client(product_gateway, calls))
    with product_gateway.app.app_context():
        pending = db.session.get(TranslationOperation, pending_id)
        assert pending.state == "failed"
        assert pending.error_code == "queue_delivery_lost"
        recovery = TranslationOperation.query.filter_by(
            project_id=project_id, kind="document_block_translate", state="queued"
        ).one()
        assert recovery.idempotency_key == f"recover-{pending_id}"
        assert recovery.source_plan_id == pending.source_plan_id
        assert recovery.source_plan_hash == pending.source_plan_hash
        assert recovery.queue_job_id == resumed.structured_content["execution"]["queue_job_id"]
        with pytest.raises(operations.TranslationOperationNotRunnable):
            operations.claim_operation(
                pending_id, "job-2", owner_user_id=1, project_id=project_id
            )


def test_translator_start_rejects_missing_dossier_before_queue_dispatch(
    product_gateway,
):
    async def calls(client: Client):
        created = await client.call_tool(
            "carlina_translator_create",
            {
                "name": "Invalid pinned translation",
                "target_lang": "pl",
                "document_id": "doc-invalid-pin",
                "mode": "passive",
                "settings": {"enable_pre_analysis": False},
            },
        )
        assert not created.is_error, created.content
        project_id = created.structured_content["project_id"]
        with product_gateway.app.app_context():
            operation = TranslationOperation.query.filter_by(
                project_id=project_id,
                kind="plan_document_blocks",
            ).one()
            _run_source_planning(product_gateway, operation)
        started = await client.call_tool(
            "carlina_translator_start",
            {
                "project_id": project_id,
                "mode": "passive",
                "executor_profile": EXECUTOR_PROFILE,
                "research_dossier_revision_id": str(
                    uuid.UUID("b7cfac40-68aa-4e77-901f-6af6ad44d540")
                ),
            },
        )
        return project_id, started

    project_id, started = asyncio.run(_with_client(product_gateway, calls))
    assert started.is_error
    assert [item[0] for item in product_gateway.translator_queue.enqueued] == [
        "plan_document_blocks"
    ]
    with product_gateway.app.app_context():
        assert (
            TranslationOperation.query.filter_by(
                project_id=project_id,
                owner_user_id=1,
                kind="document_block_translate",
            ).count()
            == 0
        )
