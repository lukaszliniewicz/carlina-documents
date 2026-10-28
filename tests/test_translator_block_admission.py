"""Frozen block admission persists before queue delivery."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from content_storage_support import bind_content_policy
from flask import Flask

from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION
from iarf_assistant.addons.translator.block_admission import (
    record_block_translation_job,
)
from iarf_assistant.addons.translator.block_contract import (
    DOCUMENT_BLOCK_ENGINE_CONTRACT,
    AttestedDocumentSnapshot,
)
from iarf_assistant.addons.translator.block_persistence import (
    plan_and_persist_source_snapshot,
)
from iarf_assistant.addons.translator.models import (
    TranslationBlockWorkflowConfig,
    TranslationOperation,
    TranslationProject,
    TranslationSourcePlan,
)
from iarf_assistant.addons.translator.operation_delivery import deliver_operation
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.extensions import db
from iarf_assistant.models import User


@pytest.fixture
def app(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'block-admission.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="synthetic-block-admission-test",
        TESTING=True,
    )
    db.init_app(application)
    application.extensions["carlina_encryption"] = EncryptionService(
        InstallationKeyStore.ephemeral()
    )
    with application.app_context():
        db.create_all()
        try:
            yield application
        finally:
            db.session.remove()
            db.drop_all()
            db.engine.dispose()


def _state() -> tuple[TranslationProject, TranslationSourcePlan]:
    owner = User(email="block-admission@example.test", password_hash="x")
    db.session.add(owner)
    db.session.flush()
    project = TranslationProject(
        user_id=owner.id,
        name="Synthetic block admission",
        source_lang="en",
        target_lang="pl",
        document_id="doc-1",
        engine_contract=DOCUMENT_BLOCK_ENGINE_CONTRACT,
        settings={
            "execution_mode": "passive",
            "enable_pre_analysis": False,
            "enable_research": False,
            "source_reference": {
                "version": 1,
                "capability": "document.processing",
                "provider_addon_id": "synthetic-provider",
                "provider_capability_id": "process",
                "resource_type": "document",
                "resource_id": "doc-1",
            },
        },
    )
    db.session.add(project)
    db.session.flush()
    html = "<html><body><p>Synthetic source.</p></body></html>"
    digest = hashlib.sha256(html.encode()).hexdigest()
    snapshot = AttestedDocumentSnapshot(
        id="doc-1",
        document_id="doc-1",
        output_html=html,
        content_hash=digest,
        semantic_content_hash=digest,
        metadata={},
        provenance={},
        captured_at="2026-08-26T00:00:00+00:00",
        semantic_contract_version=SEMANTIC_HTML_CONTRACT_VERSION,
    )
    plan, created = plan_and_persist_source_snapshot(
        project, snapshot, source_language="en"
    )
    assert created
    db.session.commit()
    return project, plan


def _executor_profile() -> dict[str, Any]:
    return {
        "schema_version": "carlina.external-executor.v1",
        "max_input_tokens": 64_000,
        "max_output_tokens": 16_000,
        "input_modalities": ["text"],
        "supports_tools": True,
        "supports_structured_output": True,
        "supports_reasoning": True,
        "user_confirmed": True,
    }


def _dossier(revision_id: str = "dossier-revision-1") -> dict[str, str]:
    return {
        "revision_id": revision_id,
        "record_hash": "a" * 64,
        "content_digest": "b" * 64,
        "conclusion_provenance": "external_unverified",
        "evidence_verification_state": "unverified",
    }


def _arguments(
    project: TranslationProject, plan: TranslationSourcePlan
) -> dict[str, Any]:
    return {
        "owner_user_id": project.user_id,
        "source_plan": plan,
        "execution_profile": {
            "window_profile": {
                "profile_id": "document-block-window",
                "profile_version": "1",
                "capacity_version": "model-capacity-v1",
                "max_blocks": 4,
                "max_input_tokens": 32_000,
                "output_reserve_tokens": 2_048,
            },
            "executor_profile": _executor_profile(),
        },
        "request_payload": {
            "source_content_hash": plan.source_content_hash,
            "source_plan_id": plan.id,
            "source_plan_hash": plan.plan_hash,
        },
        "idempotency_key": "same-block-admission",
        "context_mode": "self_contained",
        "research_dossier": _dossier(),
    }


def test_config_and_operation_commit_before_publish_and_exact_retry(app: Flask):
    project, plan = _state()
    published: list[tuple[str, str]] = []
    enqueued: list[str] = []

    def publish(
        publishing_project: TranslationProject,
        operation: TranslationOperation,
        *,
        handler: str,
        payload: dict[str, Any],
    ) -> str:
        database_path = app.config["SQLALCHEMY_DATABASE_URI"].removeprefix("sqlite:///")
        with closing(sqlite3.connect(database_path, timeout=0.1)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            operation_row = connection.execute(
                "SELECT state, queue_job_id, source_plan_id, source_plan_hash "
                "FROM translation_operation WHERE id = ?",
                (operation.id,),
            ).fetchone()
            config_count = connection.execute(
                "SELECT count(*) FROM translation_block_workflow_config "
                "WHERE operation_id = ?",
                (operation.id,),
            ).fetchone()
            connection.rollback()
        assert operation_row == ("queued", None, plan.id, plan.plan_hash) or (
            operation_row == ("queued", "queue-1", plan.id, plan.plan_hash)
        )
        assert config_count == (1,)
        published.append((operation.id, handler))

        def enqueue(_handler: str, _payload: dict[str, Any]) -> str:
            enqueued.append(operation.id)
            return "queue-1"

        return deliver_operation(
            publishing_project,
            operation,
            owner_user_id=project.user_id,
            handler=handler,
            payload=payload,
            enqueue=enqueue,
        )

    arguments = _arguments(project, plan)
    first = record_block_translation_job(project, **arguments, publish=publish)
    assert first == "queue-1"
    assert published == [(published[0][0], "translate_document_blocks")]
    assert enqueued == [published[0][0]]
    operation = db.session.get(TranslationOperation, published[0][0])
    assert operation is not None
    config = TranslationBlockWorkflowConfig.query.filter_by(
        operation_id=operation.id
    ).one()
    first_receipt = config.config_hash
    assert config.config_json["research_dossier"] == _dossier()

    assert (
        record_block_translation_job(project, **arguments, publish=publish) == "queue-1"
    )
    assert published == [(operation.id, "translate_document_blocks")] * 2
    assert enqueued == [operation.id]
    assert (
        TranslationBlockWorkflowConfig.query.filter_by(operation_id=operation.id)
        .one()
        .config_hash
        == first_receipt
    )

    with pytest.raises(RuntimeError, match="foreign research dossier"):
        record_block_translation_job(
            project,
            **{**arguments, "research_dossier": _dossier("different-revision")},
            publish=publish,
        )
    assert len(published) == 2
    assert (
        TranslationBlockWorkflowConfig.query.filter_by(
            operation_id=operation.id
        ).count()
        == 1
    )


def test_second_active_block_admission_keeps_single_flight_fence(app: Flask):
    project, plan = _state()
    arguments = _arguments(project, plan)
    published: list[str] = []

    def publish(
        _project: TranslationProject,
        operation: TranslationOperation,
        *,
        handler: str,
        payload: dict[str, Any],
    ) -> str:
        assert handler == "translate_document_blocks"
        assert payload == arguments["request_payload"]
        published.append(operation.id)
        return "delivery-deferred"

    assert (
        record_block_translation_job(project, **arguments, publish=publish)
        == "delivery-deferred"
    )
    with pytest.raises(RuntimeError, match="already has translator operation"):
        record_block_translation_job(
            project,
            **{**arguments, "idempotency_key": "different-operation"},
            publish=publish,
        )
    assert len(published) == 1
    assert TranslationOperation.query.filter_by(project_id=project.id).count() == 1
