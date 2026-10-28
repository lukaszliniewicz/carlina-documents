"""SQLite evidence for Translator operation admission and delivery."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from threading import Thread
from typing import Any

import pytest
from content_storage_support import bind_content_policy
from flask import Flask

from iarf_assistant.addons.translator.models import (
    TranslationOperation,
    TranslationProject,
)
from iarf_assistant.addons.translator.operation_delivery import (
    deliver_operation,
    record_job,
)
from iarf_assistant.addons.translator.operations import (
    TranslationOperationTransitionConflict,
    create_operation,
    finish_operation,
    transition_operation,
)
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.extensions import db
from iarf_assistant.models import User


@pytest.fixture
def app(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'delivery.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="synthetic-delivery-test",
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


def _project() -> TranslationProject:
    owner = User(email="delivery@example.test", password_hash="x")
    db.session.add(owner)
    db.session.flush()
    project = TranslationProject(
        user_id=owner.id,
        name="Synthetic delivery",
        source_lang="en",
        target_lang="pl",
        status="ready",
        settings={},
        source_content_hash="a" * 64,
    )
    db.session.add(project)
    db.session.commit()
    return project


def _database_path(app: Flask) -> str:
    return app.config["SQLALCHEMY_DATABASE_URI"].removeprefix("sqlite:///")


def _independent_operation(app: Flask, operation_id: str) -> tuple[Any, ...]:
    with closing(sqlite3.connect(_database_path(app), timeout=0.1)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT id, owner_user_id, project_id, kind, state, queue_job_id "
            "FROM translation_operation WHERE id = ?",
            (operation_id,),
        ).fetchone()
        connection.rollback()
    assert row is not None
    return row


def test_record_job_commits_before_publish_and_skips_bound_and_terminal(app: Flask):
    project = _project()
    observed: list[tuple[Any, ...]] = []
    enqueued: list[tuple[str, dict[str, Any]]] = []

    def enqueue(handler: str, payload: dict[str, Any]) -> str:
        enqueued.append((handler, payload))
        return "queue-1"

    def publish(
        publishing_project: TranslationProject,
        record: TranslationOperation,
        *,
        handler: str,
        payload: dict[str, Any],
    ) -> str:
        observed.append(_independent_operation(app, record.id))
        return deliver_operation(
            publishing_project,
            record,
            owner_user_id=project.user_id,
            handler=handler,
            payload=payload,
            enqueue=enqueue,
        )

    arguments = {
        "handler": "export_project",
        "operation": "export",
        "payload": {"format": "docx"},
        "idempotency_key": "stable-export",
        "publish": publish,
    }
    first = record_job(project, **arguments)
    assert first == "queue-1"
    assert observed == [
        (observed[0][0], project.user_id, project.id, "export", "queued", None)
    ]
    assert enqueued == [
        (
            "export_project",
            {
                "project_id": project.id,
                "user_id": project.user_id,
                "operation_id": observed[0][0],
                "format": "docx",
            },
        )
    ]
    assert record_job(project, **arguments) == "queue-1"
    assert len(observed) == len(enqueued) == 1
    operation = db.session.get(TranslationOperation, observed[0][0])
    assert operation is not None
    transition_operation(operation, "running")
    finish_operation(operation, "completed")
    db.session.commit()
    assert record_job(project, **arguments) == "queue-1"
    assert len(observed) == len(enqueued) == 1


def test_exact_unbound_retry_republishes_same_operation(app: Flask):
    project = _project()
    unbound, created = create_operation(
        project,
        kind="export",
        request_payload={"format": "docx"},
        idempotency_key="retry-export",
        source_content_hash=project.source_content_hash,
        checkpoint={"format": "docx"},
    )
    assert created
    db.session.commit()
    published: list[str] = []

    def publish(
        publishing_project: TranslationProject,
        record: TranslationOperation,
        *,
        handler: str,
        payload: dict[str, Any],
    ) -> str:
        published.append(record.id)
        return deliver_operation(
            publishing_project,
            record,
            owner_user_id=project.user_id,
            handler=handler,
            payload=payload,
            enqueue=lambda _handler, _payload: "queue-retry",
        )

    assert (
        record_job(
            project,
            handler="export_project",
            operation="export",
            payload={"format": "docx"},
            idempotency_key="retry-export",
            publish=publish,
        )
        == "queue-retry"
    )
    assert published == [unbound.id]
    assert _independent_operation(app, unbound.id)[-1] == "queue-retry"


def test_lazy_queue_lookup_failure_is_durably_failed(app: Flask):
    project = _project()

    def queue_lookup(_handler: str, _payload: dict[str, Any]) -> str:
        raise RuntimeError("queue lookup unavailable")

    with pytest.raises(RuntimeError, match="queue lookup unavailable"):
        record_job(
            project,
            handler="export_project",
            operation="export",
            payload={"format": "docx"},
            idempotency_key="queue-failure",
            publish=lambda publishing_project, record, *, handler, payload: (
                deliver_operation(
                    publishing_project,
                    record,
                    owner_user_id=project.user_id,
                    handler=handler,
                    payload=payload,
                    enqueue=queue_lookup,
                )
            ),
        )

    operation = TranslationOperation.query.filter_by(project_id=project.id).one()
    assert _independent_operation(app, operation.id)[-2:] == ("failed", None)
    assert operation.error_code == "queue_publish_failed"
    assert operation.error_detail == "queue lookup unavailable"


@pytest.mark.parametrize("bind_worker", [True, False])
def test_independent_writer_binding_conflict_reconciles_or_preserves_gap(
    app: Flask, bind_worker: bool
):
    project = _project()
    operation, created = create_operation(
        project,
        kind="export",
        request_payload={"format": "docx"},
        idempotency_key="binding-conflict",
        source_content_hash=project.source_content_hash,
        checkpoint={"format": "docx"},
    )
    assert created
    db.session.commit()
    failures: list[BaseException] = []

    def fast_writer() -> None:
        try:
            with closing(sqlite3.connect(_database_path(app), timeout=1)) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE translation_operation SET version = version + 1, "
                    "queue_job_id = ? WHERE id = ?",
                    ("worker-bound" if bind_worker else None, operation.id),
                )
                connection.commit()
        except sqlite3.Error as exc:
            failures.append(exc)

    def enqueue(_handler: str, _payload: dict[str, Any]) -> str:
        writer = Thread(target=fast_writer, name="independent-queue-binder")
        writer.start()
        writer.join(timeout=5)
        assert not writer.is_alive()
        assert not failures
        return "producer-bound"

    if bind_worker:
        result = deliver_operation(
            project,
            operation,
            owner_user_id=project.user_id,
            handler="export_project",
            payload={"format": "docx"},
            enqueue=enqueue,
        )
        assert result == "worker-bound"
        assert _independent_operation(app, operation.id)[-2:] == (
            "queued",
            "worker-bound",
        )
    else:
        with pytest.raises(RuntimeError, match="lost its queue binding") as error:
            deliver_operation(
                project,
                operation,
                owner_user_id=project.user_id,
                handler="export_project",
                payload={"format": "docx"},
                enqueue=enqueue,
            )
        assert isinstance(error.value.__cause__, TranslationOperationTransitionConflict)
        assert _independent_operation(app, operation.id)[-2:] == ("queued", None)
