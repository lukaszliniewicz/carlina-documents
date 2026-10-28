from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from content_storage_support import bind_content_policy
from flask import Flask

from iarf_assistant.addons.doc_processor import routes
from iarf_assistant.addons.doc_processor.models import (
    DocumentProject,
    ProcessedDocument,
    ProjectDocument,
    ProjectRun,
    ProjectRunDocument,
)
from iarf_assistant.addons.doc_processor.project_service import (
    build_effective_settings,
    schedule_project_run,
)
from iarf_assistant.extensions import db
from iarf_assistant.models import User


@pytest.fixture()
def project_app(tmp_path):
    app = Flask(__name__, root_path=str(tmp_path), instance_path=str(tmp_path))
    bind_content_policy(app)
    app.config.update(
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="test",
        TESTING=True,
    )
    db.init_app(app)
    with app.app_context():
        db.create_all()
        db.session.add(User(id=1, email="projects-safety@example.test", password_hash="test"))
        db.session.commit()
        yield app
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def test_project_run_serialization_includes_counts_and_progress(project_app):
    project = DocumentProject(user_id=1, name="Serialization")
    documents = [
        ProcessedDocument(user_id=1, filename=f"{index}.txt", file_path=f"/uploads/{index}.txt")
        for index in range(3)
    ]
    run = ProjectRun(project=project, user_id=1, status="running", concurrency_limit=2)
    db.session.add_all([project, run, *documents])
    db.session.flush()
    for position, (document, status) in enumerate(
        zip(documents, ("completed", "error", "processing"), strict=True)
    ):
        db.session.add(ProjectDocument(project=project, document=document, position=position))
        db.session.add(
            ProjectRunDocument(
                run=run,
                document=document,
                position=position,
                status=status,
            )
        )
    db.session.commit()

    payload = project.to_dict()

    assert payload["document_count"] == 3
    assert payload["latest_run"]["counts"] == {
        "total": 3,
        "completed": 1,
        "error": 1,
        "processing": 1,
    }
    assert payload["latest_run"]["progress"] == 67
    assert len(payload["latest_run"]["documents"]) == 3


@pytest.mark.parametrize("value", [0, 11, "not-an-integer"])
def test_project_routes_reject_invalid_concurrency(project_app, value):
    with project_app.test_request_context(
        "/addons/doc_processor/api/projects",
        method="POST",
        json={"name": "Invalid concurrency", "document_concurrency": value},
    ), patch.object(routes, "current_user", SimpleNamespace(id=1)):
        response, status_code = routes.create_project.__wrapped__()

    assert status_code == 400
    assert "concurrency" in response.get_json()["error"].lower()
    assert DocumentProject.query.count() == 0


def test_project_document_add_is_atomic_when_an_id_is_missing(project_app):
    project = DocumentProject(user_id=1, name="Atomic add")
    document = ProcessedDocument(user_id=1, filename="valid.txt", file_path="/uploads/valid.txt")
    db.session.add_all([project, document])
    db.session.commit()

    with project_app.test_request_context(
        f"/addons/doc_processor/api/projects/{project.id}/documents",
        method="POST",
        json={"document_ids": [document.id, "00000000-0000-0000-0000-000000000000"]},
    ), patch.object(routes, "current_user", SimpleNamespace(id=1)):
        response, status_code = routes.add_project_documents.__wrapped__(project.id)

    assert status_code == 404
    assert response.get_json()["missing_document_ids"] == [
        "00000000-0000-0000-0000-000000000000"
    ]
    assert ProjectDocument.query.filter_by(project_id=project.id).count() == 0


def test_project_reorder_requires_the_complete_unique_document_set(project_app):
    project = DocumentProject(user_id=1, name="Strict order")
    documents = [
        ProcessedDocument(user_id=1, filename=f"{index}.txt", file_path=f"/uploads/{index}.txt")
        for index in range(2)
    ]
    db.session.add_all([project, *documents])
    db.session.flush()
    for position, document in enumerate(documents):
        db.session.add(ProjectDocument(project=project, document=document, position=position))
    db.session.commit()

    with project_app.test_request_context(
        f"/addons/doc_processor/api/projects/{project.id}/documents/order",
        method="PUT",
        json={"document_ids": [documents[0].id]},
    ), patch.object(routes, "current_user", SimpleNamespace(id=1)):
        response, status_code = routes.reorder_project_documents.__wrapped__(project.id)

    assert status_code == 400
    assert "exactly once" in response.get_json()["error"]
    assert [entry.document_id for entry in project.documents] == [document.id for document in documents]


def test_start_and_delete_reject_a_project_with_an_active_overlapping_run(project_app):
    project = DocumentProject(user_id=1, name="Active run")
    document = ProcessedDocument(user_id=1, filename="active.txt", file_path="/uploads/active.txt")
    run = ProjectRun(project=project, user_id=1, status="running", concurrency_limit=1)
    db.session.add_all([project, document, run])
    db.session.flush()
    db.session.add(ProjectDocument(project=project, document=document, position=0))
    db.session.add(
        ProjectRunDocument(
            run=run,
            document=document,
            position=0,
            status="processing",
        )
    )
    db.session.commit()

    with project_app.test_request_context(
        f"/addons/doc_processor/api/projects/{project.id}/runs",
        method="POST",
    ), patch.object(routes, "current_user", SimpleNamespace(id=1)):
        response, status_code = routes.start_project_run.__wrapped__(project.id)
    assert status_code == 409
    assert response.get_json()["conflicting_documents"][0]["id"] == document.id
    assert ProjectRun.query.filter_by(project_id=project.id).count() == 1

    with project_app.test_request_context(
        f"/addons/doc_processor/api/projects/{project.id}",
        method="DELETE",
    ), patch.object(routes, "current_user", SimpleNamespace(id=1)):
        response, status_code = routes.delete_project.__wrapped__(project.id)
    assert status_code == 409
    assert db.session.get(DocumentProject, project.id) is not None


def test_scheduler_recovers_from_enqueue_failure_and_preserves_owner(project_app):
    class FailFirstQueue:
        def __init__(self):
            self.calls = []

        def enqueue(self, task_name, payload):
            self.calls.append((task_name, payload))
            if len(self.calls) == 1:
                raise RuntimeError("queue unavailable")
            return "job-2"

    uploads_dir = Path(project_app.root_path) / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    for index in range(2):
        (uploads_dir / f"{index}.txt").write_text("source", encoding="utf-8")
    project = DocumentProject(user_id=1, name="Queue recovery")
    documents = [
        ProcessedDocument(
            user_id=1,
            filename=f"{index}.txt",
            file_path=f"/uploads/{index}.txt",
            settings={"engine_id": "auto"},
        )
        for index in range(2)
    ]
    run = ProjectRun(project=project, user_id=1, status="queued", concurrency_limit=1)
    db.session.add_all([project, run, *documents])
    db.session.flush()
    entries = []
    for position, document in enumerate(documents):
        entry = ProjectRunDocument(
            run=run,
            document=document,
            position=position,
            status="queued",
            effective_settings=build_effective_settings(document.settings),
        )
        entries.append(entry)
        db.session.add(entry)
    db.session.commit()
    queue = FailFirstQueue()

    schedule_project_run(run.id, queue)

    assert [entry.status for entry in entries] == ["error", "processing"]
    assert entries[0].completed_at is not None
    assert entries[1].job_id == "job-2"
    assert len(queue.calls) == 2
    assert all(payload["user_id"] == 1 for _task, payload in queue.calls)
