import io
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from content_storage_support import bind_content_policy
from flask import Flask
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from carlina.core.utc import utc_now_db
from iarf_assistant.addons.doc_processor import project_service, routes
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
    sync_project_run_document,
)
from iarf_assistant.core import safe_sqlite_store
from iarf_assistant.core.task_queue import TaskQueue
from iarf_assistant.extensions import db
from iarf_assistant.models import User


class FakeQueue:
    def __init__(self):
        self.enqueued = []
        self.status = {}
        self.status_updates = []
        self.cancel_requests = []

    def enqueue(self, task_name, payload):
        job_id = f"job-{len(self.enqueued) + 1}"
        self.enqueued.append((task_name, payload))
        return job_id

    def get_status(self, job_id):
        return self.status

    def set_status(self, job_id, status):
        self.status = {**self.status, **status}
        self.status_updates.append((job_id, status))

    def request_cancel(self, job_id, requested_by=None, reason=None):
        self.cancel_requests.append((job_id, requested_by, reason))
        self.set_status(job_id, {"cancel_requested": True, "cancel_reason": reason})


class LockingFakeCache:
    def __init__(self):
        self.values = {}
        self.signals = set()

    def add(self, key, value, *, expire=None):
        if key in self.values:
            return False
        self.values[key] = value
        return True

    def delete_if_value(self, key, value):
        if self.values.get(key) != value:
            return False
        del self.values[key]
        return True

    def acquire_or_signal(self, key, token, *, expire, signal_key, signal_expire=None):
        if key not in self.values:
            self.values[key] = token
            self.signals.discard(signal_key)
            return "acquired"
        self.signals.add(signal_key)
        return "signaled"

    def heartbeat(self, key, token, *, expire, signal_key, signal_expire=None):
        if self.values.get(key) != token:
            return "lost", False
        if signal_key in self.signals:
            self.signals.remove(signal_key)
            return "owned", True
        return "owned", False

    def renew_or_release(self, key, token, *, expire, signal_key, signal_expire=None):
        if self.values.get(key) != token:
            return "lost"
        if signal_key in self.signals:
            self.signals.remove(signal_key)
            return "signaled"
        del self.values[key]
        return "released"


class TokenReplacingCache(LockingFakeCache):
    def __init__(self):
        super().__init__()
        self.heartbeat_calls = 0

    def heartbeat(self, key, token, *, expire, signal_key, signal_expire=None):
        self.heartbeat_calls += 1
        if self.heartbeat_calls == 3:
            self.values[key] = "replacement-token"
            return "lost", False
        return super().heartbeat(
            key,
            token,
            expire=expire,
            signal_key=signal_key,
            signal_expire=signal_expire,
        )


class SynchronousTransitionQueue(FakeQueue):
    def __init__(self, transition_status):
        super().__init__()
        self.cache = LockingFakeCache()
        self.transition_status = transition_status

    def enqueue(self, task_name, payload):
        job_id = super().enqueue(task_name, payload)
        if len(self.enqueued) == 1:
            entry = db.session.get(ProjectRunDocument, payload["project_run_document_id"])
            entry.document.status = self.transition_status
            sync_project_run_document(entry.id, self.transition_status, task_queue=self)
        return job_id


class SynchronousCompletionQueue(SynchronousTransitionQueue):
    def __init__(self):
        super().__init__("completed")


class HeartbeatLossQueue(FakeQueue):
    def __init__(self):
        super().__init__()
        self.cache = TokenReplacingCache()


def _make_app(temp_dir):
    app = Flask(__name__, root_path=temp_dir, instance_path=temp_dir)
    bind_content_policy(app)
    app.config.update(
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="test",
        TESTING=True,
    )
    db.init_app(app)
    return app


def _make_file_app(root_path, database_path):
    app = Flask(__name__, root_path=str(root_path), instance_path=str(root_path))
    bind_content_policy(app)
    app.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{database_path}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="test",
        TESTING=True,
    )
    db.init_app(app)
    return app


def test_effective_project_settings_force_sequential_pages_without_disabling_previous_page():
    settings = build_effective_settings(
        {"engine_id": "llm_vision", "pdf_options": {"parallel_pages": 7, "append_previous": True}},
        {"pdf_options": {"metadata_prepass": True}},
    )

    assert settings["pdf_options"]["parallel_pages"] == 1
    assert settings["pdf_options"]["append_previous"] is True
    assert settings["pdf_options"]["metadata_prepass"] is True


def test_scheduler_skips_manual_crop_review_as_a_concurrency_slot():
    with tempfile.TemporaryDirectory() as temp_dir:
        app = _make_app(temp_dir)
        with app.app_context():
            db.create_all()
            user = User(id=1, email="projects@example.test", password_hash="test")
            db.session.add(user)
            db.session.commit()
            uploads_dir = Path(temp_dir) / "uploads"
            uploads_dir.mkdir(parents=True)
            for filename in ("review.pdf", "first.txt", "second.txt"):
                (uploads_dir / filename).write_bytes(b"source")
            pdf = ProcessedDocument(
                user_id=1,
                filename="review.pdf",
                file_path="/uploads/review.pdf",
                settings={"engine_id": "llm_vision", "pdf_options": {"skip_cropping": False, "metadata_prepass": False}},
            )
            first = ProcessedDocument(user_id=1, filename="first.txt", file_path="/uploads/first.txt", settings={"engine_id": "auto"})
            second = ProcessedDocument(user_id=1, filename="second.txt", file_path="/uploads/second.txt", settings={"engine_id": "auto"})
            project = DocumentProject(user_id=1, name="Book set", settings={"document_concurrency": 2})
            db.session.add_all([pdf, first, second, project])
            db.session.flush()
            for position, document in enumerate((pdf, first, second)):
                db.session.add(ProjectDocument(project=project, document=document, position=position))
            run = ProjectRun(project=project, user_id=1, concurrency_limit=2, status="queued")
            db.session.add(run)
            db.session.flush()
            for position, document in enumerate((pdf, first, second)):
                db.session.add(
                    ProjectRunDocument(
                        run=run,
                        document=document,
                        position=position,
                        effective_settings=build_effective_settings(document.settings),
                    )
                )
            db.session.commit()

            queue = FakeQueue()
            schedule_project_run(run.id, queue)

            statuses = [entry.status for entry in run.documents]
            assert statuses == ["needs_cropping", "processing", "processing"]
            assert [task for task, _payload in queue.enqueued] == ["process_document", "process_document"]
            assert all(payload["project_run_document_id"] for _task, payload in queue.enqueued)

            db.session.remove()
            db.drop_all()
            db.engine.dispose()


def test_completed_project_document_schedules_the_next_one():
    with tempfile.TemporaryDirectory() as temp_dir:
        app = _make_app(temp_dir)
        with app.app_context():
            db.create_all()
            user = User(id=1, email="projects@example.test", password_hash="test")
            db.session.add(user)
            db.session.commit()
            uploads_dir = Path(temp_dir) / "uploads"
            uploads_dir.mkdir(parents=True)
            for filename in ("0.txt", "1.txt"):
                (uploads_dir / filename).write_bytes(b"source")
            documents = [
                ProcessedDocument(user_id=1, filename=f"{index}.txt", file_path=f"/uploads/{index}.txt", settings={"engine_id": "auto"})
                for index in range(2)
            ]
            project = DocumentProject(user_id=1, name="Sequential queue", settings={"document_concurrency": 1})
            run = ProjectRun(project=project, user_id=1, concurrency_limit=1, status="queued")
            db.session.add_all([project, run, *documents])
            db.session.flush()
            entries = []
            for position, document in enumerate(documents):
                entry = ProjectRunDocument(
                    run=run,
                    document=document,
                    position=position,
                    effective_settings=build_effective_settings(document.settings),
                )
                db.session.add(entry)
                entries.append(entry)
            db.session.commit()

            queue = FakeQueue()
            schedule_project_run(run.id, queue)
            assert [entry.status for entry in entries] == ["processing", "queued"]

            sync_project_run_document(entries[0].id, "completed", task_queue=queue)
            assert [entry.status for entry in entries] == ["completed", "processing"]
            assert len(queue.enqueued) == 2

            db.session.remove()
            db.drop_all()
            db.engine.dispose()


def test_scheduler_preserves_synchronous_completion_and_fills_freed_slot():
    with tempfile.TemporaryDirectory() as temp_dir:
        app = _make_app(temp_dir)
        uploads_dir = Path(temp_dir) / "uploads"
        uploads_dir.mkdir(parents=True)
        for filename in ("first.txt", "second.txt"):
            (uploads_dir / filename).write_text("source", encoding="utf-8")
        with app.app_context():
            db.create_all()
            db.session.add(User(id=1, email="projects@example.test", password_hash="test"))
            documents = [
                ProcessedDocument(
                    user_id=1,
                    filename=filename,
                    file_path=f"/uploads/{filename}",
                    settings={"engine_id": "auto"},
                )
                for filename in ("first.txt", "second.txt")
            ]
            project = DocumentProject(user_id=1, name="Synchronous completion")
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

            queue = SynchronousCompletionQueue()
            schedule_project_run(run.id, queue)

            assert [task for task, _payload in queue.enqueued] == [
                "process_document",
                "process_document",
            ]
            db.session.expire_all()
            first_entry = db.session.get(ProjectRunDocument, entries[0].id)
            first_document = db.session.get(ProcessedDocument, documents[0].id)
            second_entry = db.session.get(ProjectRunDocument, entries[1].id)
            second_document = db.session.get(ProcessedDocument, documents[1].id)
            assert first_entry.status == "completed"
            assert first_entry.job_id is None
            assert first_document.status == "completed"
            assert "active_job_id" not in first_document.settings
            assert "active_project_run_document_id" not in first_document.settings
            assert second_entry.status == "processing"
            assert second_entry.job_id == "job-2"
            assert second_document.settings["active_job_id"] == "job-2"
            assert second_document.settings["active_project_run_document_id"] == second_entry.id

            db.session.remove()
            db.drop_all()
            db.engine.dispose()


def test_scheduler_cas_preserves_synchronous_manual_review_and_fills_slot():
    with tempfile.TemporaryDirectory() as temp_dir:
        app = _make_app(temp_dir)
        uploads_dir = Path(temp_dir) / "uploads"
        uploads_dir.mkdir(parents=True)
        (uploads_dir / "review.pdf").write_bytes(b"pdf")
        (uploads_dir / "second.txt").write_text("source", encoding="utf-8")
        with app.app_context():
            db.create_all()
            db.session.add(User(id=1, email="projects@example.test", password_hash="test"))
            review_document = ProcessedDocument(
                user_id=1,
                filename="review.pdf",
                file_path="/uploads/review.pdf",
                settings={
                    "engine_id": "llm_vision",
                    "pdf_options": {"skip_cropping": False, "metadata_prepass": True},
                },
            )
            second_document = ProcessedDocument(
                user_id=1,
                filename="second.txt",
                file_path="/uploads/second.txt",
                settings={"engine_id": "auto"},
            )
            project = DocumentProject(user_id=1, name="Synchronous manual review")
            run = ProjectRun(project=project, user_id=1, status="queued", concurrency_limit=1)
            review_entry = ProjectRunDocument(
                run=run,
                document=review_document,
                position=0,
                status="queued",
                effective_settings=build_effective_settings(review_document.settings),
            )
            second_entry = ProjectRunDocument(
                run=run,
                document=second_document,
                position=1,
                status="queued",
                effective_settings=build_effective_settings(second_document.settings),
            )
            db.session.add_all([project, run, review_document, second_document, review_entry, second_entry])
            db.session.commit()

            queue = SynchronousTransitionQueue("needs_cropping")
            schedule_project_run(run.id, queue)

            assert [task for task, _payload in queue.enqueued] == [
                "prepare_crop_review",
                "process_document",
            ]
            db.session.expire_all()
            refreshed_review_entry = db.session.get(ProjectRunDocument, review_entry.id)
            refreshed_review_document = db.session.get(ProcessedDocument, review_document.id)
            refreshed_second_entry = db.session.get(ProjectRunDocument, second_entry.id)
            assert refreshed_review_entry.status == "needs_cropping"
            assert refreshed_review_entry.job_id is None
            assert refreshed_review_document.status == "needs_cropping"
            assert "active_job_id" not in refreshed_review_document.settings
            assert refreshed_second_entry.status == "processing"
            assert refreshed_second_entry.job_id == "job-2"

            db.session.remove()
            db.drop_all()
            db.engine.dispose()


def test_scheduler_uses_real_task_queue_cache_lock(tmp_path):
    app = _make_app(str(tmp_path))
    source_path = tmp_path / "uploads" / "1" / "source.txt"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("source", encoding="utf-8")
    with app.app_context():
        db.create_all()
        db.session.add(User(id=1, email="projects@example.test", password_hash="test"))
        document = ProcessedDocument(
            user_id=1,
            filename="source.txt",
            file_path="/uploads/1/source.txt",
            settings={"engine_id": "auto"},
        )
        project = DocumentProject(user_id=1, name="Real queue lock")
        run = ProjectRun(project=project, user_id=1, status="queued", concurrency_limit=1)
        entry = ProjectRunDocument(
            run=run,
            document=document,
            position=0,
            status="queued",
            effective_settings=build_effective_settings(document.settings),
        )
        db.session.add_all([document, project, run, entry])
        db.session.commit()

        with TaskQueue(str(tmp_path), "doc_processor") as queue:
            schedule_project_run(run.id, queue)
            assert queue.queue.popleft()["task_name"] == "process_document"
            assert queue.cache.get(f"project_schedule_lock:{run.id}") is None

        db.session.expire_all()
        refreshed_entry = db.session.get(ProjectRunDocument, entry.id)
        assert refreshed_entry.status == "processing"
        assert refreshed_entry.job_id

        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def test_scheduler_stops_safely_when_heartbeat_token_is_replaced():
    with tempfile.TemporaryDirectory() as temp_dir:
        app = _make_app(temp_dir)
        uploads_dir = Path(temp_dir) / "uploads"
        uploads_dir.mkdir(parents=True)
        (uploads_dir / "source.txt").write_text("source", encoding="utf-8")
        with app.app_context():
            db.create_all()
            db.session.add(User(id=1, email="projects@example.test", password_hash="test"))
            document = ProcessedDocument(
                user_id=1,
                filename="source.txt",
                file_path="/uploads/source.txt",
                settings={"engine_id": "auto"},
            )
            project = DocumentProject(user_id=1, name="Heartbeat ownership loss")
            run = ProjectRun(project=project, user_id=1, status="queued", concurrency_limit=1)
            entry = ProjectRunDocument(
                run=run,
                document=document,
                position=0,
                status="queued",
                effective_settings=build_effective_settings(document.settings),
            )
            db.session.add_all([project, run, document, entry])
            db.session.commit()

            queue = HeartbeatLossQueue()
            schedule_project_run(run.id, queue)

            db.session.expire_all()
            refreshed_entry = db.session.get(ProjectRunDocument, entry.id)
            refreshed_document = db.session.get(ProcessedDocument, document.id)
            assert queue.cache.heartbeat_calls == 3
            assert queue.enqueued == []
            assert queue.cache.values[f"project_schedule_lock:{run.id}"] == "replacement-token"
            assert refreshed_entry.status == "queued"
            assert refreshed_entry.started_at is None
            assert refreshed_entry.job_id is None
            assert refreshed_document.status == "created"
            assert "active_project_run_document_id" not in refreshed_document.settings

            db.session.remove()
            db.drop_all()
            db.engine.dispose()


def test_scheduler_heartbeats_through_expiry_and_drains_mid_pass_wake(tmp_path, monkeypatch):
    class FakeClock:
        def __init__(self):
            self.value = 100.0

        def __call__(self):
            return self.value

        def advance(self, seconds):
            self.value += seconds

    class ClockedCompletionQueue(TaskQueue):
        def __init__(self, *args, run_id, contender_queue, clock, **kwargs):
            super().__init__(*args, **kwargs)
            self.run_id = run_id
            self.contender_queue = contender_queue
            self.clock = clock
            self.enqueue_count = 0

        def enqueue(self, task_name, payload):
            job_id = super().enqueue(task_name, payload)
            self.enqueue_count += 1
            # Each supported enqueue remains shorter than the five-second
            # lease, while the complete pass crosses its original expiry.
            self.clock.advance(4.0)
            if self.enqueue_count == 1:
                entry = db.session.get(ProjectRunDocument, payload["project_run_document_id"])
                entry.document.status = "completed"
                sync_project_run_document(entry.id, "completed")
                schedule_project_run(self.run_id, self.contender_queue)
            return job_id

    clock = FakeClock()
    monkeypatch.setattr(safe_sqlite_store.time, "time", clock)
    monkeypatch.setattr(project_service, "PROJECT_SCHEDULER_LEASE_SECONDS", 5.0)
    monkeypatch.setattr(project_service, "PROJECT_SCHEDULER_SIGNAL_SECONDS", 5.0)

    app = _make_app(str(tmp_path))
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir(parents=True)
    for filename in ("first.txt", "second.txt"):
        (uploads_dir / filename).write_text("source", encoding="utf-8")

    with app.app_context():
        db.create_all()
        db.session.add(User(id=1, email="projects@example.test", password_hash="test"))
        documents = [
            ProcessedDocument(
                user_id=1,
                filename=filename,
                file_path=f"/uploads/{filename}",
                settings={"engine_id": "auto"},
            )
            for filename in ("first.txt", "second.txt")
        ]
        project = DocumentProject(user_id=1, name="Heartbeat expiry")
        run = ProjectRun(project=project, user_id=1, status="queued", concurrency_limit=1)
        entries = [
            ProjectRunDocument(
                run=run,
                document=document,
                position=position,
                status="queued",
                effective_settings=build_effective_settings(document.settings),
            )
            for position, document in enumerate(documents)
        ]
        db.session.add_all([project, run, *documents, *entries])
        db.session.commit()

        contender_queue = TaskQueue(str(tmp_path), "doc_processor")
        owner_queue = ClockedCompletionQueue(
            str(tmp_path),
            "doc_processor",
            run_id=run.id,
            contender_queue=contender_queue,
            clock=clock,
        )
        heartbeat_results = []
        original_heartbeat = owner_queue.cache.heartbeat

        def record_heartbeat(*args, **kwargs):
            result = original_heartbeat(*args, **kwargs)
            heartbeat_results.append(result)
            return result

        owner_queue.cache.heartbeat = record_heartbeat
        try:
            schedule_project_run(run.id, owner_queue)

            db.session.expire_all()
            first_entry = db.session.get(ProjectRunDocument, entries[0].id)
            first_document = db.session.get(ProcessedDocument, documents[0].id)
            second_entry = db.session.get(ProjectRunDocument, entries[1].id)
            assert clock.value > 105.0
            assert ("owned", True) in heartbeat_results
            assert all(state == "owned" for state, _consumed in heartbeat_results)
            assert owner_queue.cache.get(f"project_schedule_lock:{run.id}") is None
            assert first_entry.status == "completed"
            assert first_entry.job_id is None
            assert "active_job_id" not in first_document.settings
            assert second_entry.status == "processing"
            assert second_entry.job_id
            assert owner_queue.queue.popleft()["task_name"] == "process_document"
            assert owner_queue.queue.popleft()["task_name"] == "process_document"
        finally:
            owner_queue.close()
            contender_queue.close()
            db.session.remove()
            db.drop_all()
            db.engine.dispose()


def test_scheduler_drains_separate_session_wake_after_cas_commit(tmp_path):
    database_path = tmp_path / "doc-processor.sqlite3"
    owner_app = _make_file_app(tmp_path, database_path)
    source_dir = tmp_path / "uploads"
    source_dir.mkdir(parents=True)
    for filename in ("first.txt", "second.txt"):
        (source_dir / filename).write_text("source", encoding="utf-8")

    with owner_app.app_context():
        db.create_all()
        db.session.add(User(id=1, email="projects@example.test", password_hash="test"))
        documents = [
            ProcessedDocument(
                user_id=1,
                filename=filename,
                file_path=f"/uploads/{filename}",
                settings={"engine_id": "auto"},
            )
            for filename in ("first.txt", "second.txt")
        ]
        project = DocumentProject(user_id=1, name="Separate session wake")
        run = ProjectRun(project=project, user_id=1, status="queued", concurrency_limit=1)
        entries = [
            ProjectRunDocument(
                run=run,
                document=document,
                position=position,
                status="queued",
                effective_settings=build_effective_settings(document.settings),
            )
            for position, document in enumerate(documents)
        ]
        db.session.add_all([project, run, *documents, *entries])
        db.session.commit()
        run_id = run.id
        first_entry_id = entries[0].id
        second_entry_id = entries[1].id
        first_document_id = documents[0].id

        owner_queue = TaskQueue(str(tmp_path), "doc_processor")
        contender_queue = TaskQueue(str(tmp_path), "doc_processor")
        original_renew = owner_queue.cache.renew_or_release
        triggered = False

        def renew_with_worker_transition(*args, **kwargs):
            nonlocal triggered
            if not triggered:
                triggered = True
                # Simulate a worker's separate database session completing the
                # first entry after the scheduler CAS committed, but before
                # the owner releases its lease.  Select only unencrypted
                # columns: this test intentionally does not share the
                # installation key material between app/session instances.
                with Session(bind=db.engine) as worker_session:
                    worker_settings = worker_session.execute(
                        select(ProcessedDocument.__table__.c.settings).where(
                            ProcessedDocument.__table__.c.id == first_document_id
                        )
                    ).scalar_one()
                    worker_settings = dict(worker_settings or {})
                    worker_settings.pop("active_project_run_document_id", None)
                    worker_settings.pop("active_job_id", None)
                    worker_settings.pop("progress", None)
                    worker_settings.pop("progress_msg", None)
                    completed_at = utc_now_db()
                    worker_session.execute(
                        update(ProjectRunDocument.__table__)
                        .where(ProjectRunDocument.__table__.c.id == first_entry_id)
                        .values(status="completed", job_id=None, completed_at=completed_at)
                    )
                    worker_session.execute(
                        update(ProcessedDocument.__table__)
                        .where(ProcessedDocument.__table__.c.id == first_document_id)
                        .values(status="completed", settings=worker_settings)
                    )
                    worker_session.commit()
                # A contender in the separate session now observes the live
                # lease and records coalesced wake demand.
                schedule_project_run(run_id, contender_queue)
            return original_renew(*args, **kwargs)

        owner_queue.cache.renew_or_release = renew_with_worker_transition
        try:
            schedule_project_run(run_id, owner_queue)
            db.session.expire_all()
            refreshed_first_entry = db.session.get(ProjectRunDocument, first_entry_id)
            refreshed_first_document = db.session.get(ProcessedDocument, first_document_id)
            refreshed_second_entry = db.session.get(ProjectRunDocument, second_entry_id)
            assert triggered is True
            assert [owner_queue.queue.popleft()["task_name"] for _ in range(2)] == [
                "process_document",
                "process_document",
            ]
            assert refreshed_first_entry.status == "completed"
            assert refreshed_first_entry.job_id is None
            assert "active_job_id" not in refreshed_first_document.settings
            assert refreshed_second_entry.status == "processing"
            assert refreshed_second_entry.job_id
        finally:
            owner_queue.close()
            contender_queue.close()
            db.session.remove()
            db.drop_all()
            db.engine.dispose()


@pytest.mark.parametrize("suffix", ["txt", "fb2"])
def test_project_upload_creates_ordered_documents_without_starting_processing(suffix):
    def source(title):
        if suffix == "txt":
            return title.encode()
        return (
            '<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">'
            f'<description><title-info><book-title>{title}</book-title></title-info></description>'
            f'<body><section><p>{title}</p></section></body></FictionBook>'
        ).encode()

    with tempfile.TemporaryDirectory() as temp_dir:
        app = _make_app(temp_dir)
        with app.app_context():
            db.create_all()
            db.session.add(User(id=1, email="projects@example.test", password_hash="test"))
            db.session.commit()
            project = DocumentProject(user_id=1, name="Fresh uploads", settings={"document_concurrency": 2})
            db.session.add(project)
            db.session.commit()

            with app.test_request_context(
                f"/addons/doc_processor/api/projects/{project.id}/documents/upload",
                method="POST",
                data={
                    "files": [
                        (io.BytesIO(source("first")), f"first.{suffix}"),
                        (io.BytesIO(source("second")), f"second.{suffix}"),
                    ],
                    "engine": "native",
                    "append_previous": "true",
                    "parallel_pages": "7",
                },
                content_type="multipart/form-data",
            ):
                with patch.object(routes, "current_user", SimpleNamespace(id=1)), patch(
                    "iarf_assistant.core.model_router.ModelRouter.get_model_for_tier",
                    return_value="model/default",
                ):
                    response, status_code = routes.upload_project_documents.__wrapped__(project.id)

            payload = response.get_json()
            assert status_code == 201
            assert [entry["document"]["filename"] for entry in payload["project"]["documents"]] == [f"first.{suffix}", f"second.{suffix}"]
            assert all(entry["document"]["status"] == "created" for entry in payload["project"]["documents"])
            assert all(entry["document"]["source_available"] for entry in payload["project"]["documents"])
            uploaded = ProcessedDocument.query.filter_by(user_id=1).order_by(ProcessedDocument.created_at.asc()).all()
            if suffix == "fb2":
                assert [document.settings["document_metadata"]["title"] for document in uploaded] == ["first", "second"]
            assert all(document.settings["pdf_options"]["append_previous"] is True for document in uploaded)
            assert all(document.settings["pdf_options"]["parallel_pages"] == 1 for document in uploaded)

            db.session.remove()
            db.drop_all()
            db.engine.dispose()


def test_project_run_rejects_missing_sources_before_queueing_jobs():
    with tempfile.TemporaryDirectory() as temp_dir:
        app = _make_app(temp_dir)
        with app.app_context():
            db.create_all()
            db.session.add(User(id=1, email="projects@example.test", password_hash="test"))
            db.session.commit()
            missing = ProcessedDocument(
                user_id=1,
                filename="missing.pdf",
                file_path="/uploads/1/missing.pdf",
                status="created",
                settings={"engine_id": "llm_vision"},
            )
            project = DocumentProject(user_id=1, name="Missing source", settings={"document_concurrency": 2})
            db.session.add_all([missing, project])
            db.session.flush()
            db.session.add(ProjectDocument(project=project, document=missing, position=0))
            db.session.commit()
            queue = FakeQueue()

            with app.test_request_context(f"/addons/doc_processor/api/projects/{project.id}/runs", method="POST"):
                with patch.object(routes, "current_user", SimpleNamespace(id=1)), patch.object(
                    routes, "_get_task_queue", return_value=queue
                ):
                    response, status_code = routes.start_project_run.__wrapped__(project.id)

            assert status_code == 409
            assert response.get_json()["missing_documents"][0]["filename"] == "missing.pdf"
            assert queue.enqueued == []
            assert ProjectRun.query.count() == 0

            db.session.remove()
            db.drop_all()
            db.engine.dispose()


def test_project_run_control_pause_resume_and_stop():
    with tempfile.TemporaryDirectory() as temp_dir:
        app = _make_app(temp_dir)
        with app.app_context():
            db.create_all()
            db.session.add(User(id=1, email="projects@example.test", password_hash="test"))
            db.session.commit()
            uploads_dir = Path(temp_dir) / "uploads"
            uploads_dir.mkdir(parents=True)
            for filename in ("active.txt", "queued.txt"):
                (uploads_dir / filename).write_bytes(b"source")

            active_doc = ProcessedDocument(user_id=1, filename="active.txt", file_path="/uploads/active.txt", status="processing")
            queued_doc = ProcessedDocument(user_id=1, filename="queued.txt", file_path="/uploads/queued.txt", status="created")
            project = DocumentProject(user_id=1, name="Controlled Project")
            run = ProjectRun(project=project, user_id=1, concurrency_limit=1, status="running")
            db.session.add_all([project, run, active_doc, queued_doc])
            db.session.flush()
            active_entry = ProjectRunDocument(
                run=run,
                document=active_doc,
                position=0,
                status="processing",
                job_id="job-active",
                effective_settings={"engine_id": "auto"},
            )
            queued_entry = ProjectRunDocument(
                run=run,
                document=queued_doc,
                position=1,
                status="queued",
                effective_settings={"engine_id": "auto"},
            )
            db.session.add_all([active_entry, queued_entry])
            db.session.commit()

            fake_queue = FakeQueue()
            with app.test_request_context(
                f"/addons/doc_processor/api/project-runs/{run.id}/control",
                method="POST",
                json={"action": "pause"},
            ):
                with patch.object(routes, "current_user", SimpleNamespace(id=1)), patch.object(
                    routes, "_get_task_queue", return_value=fake_queue
                ):
                    response = routes.control_project_run.__wrapped__(run.id)

            pause_payload = response.get_json()
            assert pause_payload["pause_requested"] is True
            assert fake_queue.status_updates[-1] == ("job-active", {"status": "pausing", "pause_requested": True})

            with app.test_request_context(
                f"/addons/doc_processor/api/project-runs/{run.id}/control",
                method="POST",
                json={"action": "resume"},
            ):
                with patch.object(routes, "current_user", SimpleNamespace(id=1)), patch.object(
                    routes, "_get_task_queue", return_value=fake_queue
                ):
                    response = routes.control_project_run.__wrapped__(run.id)

            resume_payload = response.get_json()
            assert resume_payload["pause_requested"] is False
            assert resume_payload["stop_requested"] is False
            assert db.session.get(ProjectRunDocument, queued_entry.id).status == "queued"

            with app.test_request_context(
                f"/addons/doc_processor/api/project-runs/{run.id}/control",
                method="POST",
                json={"action": "stop"},
            ):
                with patch.object(routes, "current_user", SimpleNamespace(id=1)), patch.object(
                    routes, "_get_task_queue", return_value=fake_queue
                ):
                    response = routes.control_project_run.__wrapped__(run.id)

            stop_payload = response.get_json()
            assert stop_payload["stop_requested"] is True
            assert stop_payload["status"] == "stopping"
            assert fake_queue.cancel_requests == [("job-active", 1, "User requested project stop")]
            assert db.session.get(ProjectRunDocument, queued_entry.id).status == "cancelled"
            assert db.session.get(ProcessedDocument, queued_doc.id).status == "cancelled"

            db.session.remove()
            db.drop_all()
            db.engine.dispose()


def test_paused_project_resume_rejects_deleted_source_without_mutation():
    with tempfile.TemporaryDirectory() as temp_dir:
        app = _make_app(temp_dir)
        uploads_dir = Path(temp_dir) / "uploads"
        source_path = uploads_dir / "paused.txt"
        source_path.parent.mkdir(parents=True)
        source_path.write_text("source", encoding="utf-8")
        with app.app_context():
            db.create_all()
            db.session.add(User(id=1, email="projects@example.test", password_hash="test"))
            document = ProcessedDocument(
                user_id=1,
                filename="paused.txt",
                file_path="/uploads/paused.txt",
                status="paused",
                settings={"keep": "document-settings", "active_project_run_document_id": "entry-1"},
            )
            project = DocumentProject(user_id=1, name="Paused project")
            run = ProjectRun(project=project, user_id=1, status="paused", pause_requested=True)
            entry = ProjectRunDocument(
                id="entry-1",
                run=run,
                document=document,
                position=0,
                status="paused",
                effective_settings={"keep": "entry-settings"},
            )
            db.session.add_all([document, project, run, entry])
            db.session.commit()
            source_path.unlink()
            original_run = (run.status, run.pause_requested, run.stop_requested)
            original_entry = (entry.status, entry.error_message, entry.completed_at)
            original_document = (document.status, dict(document.settings), document.error_message)
            queue = FakeQueue()

            with app.test_request_context(
                f"/addons/doc_processor/api/project-runs/{run.id}/control",
                method="POST",
                json={"action": "resume"},
            ):
                with patch.object(routes, "current_user", SimpleNamespace(id=1)), patch.object(
                    routes, "_get_task_queue", return_value=queue
                ):
                    response = routes.control_project_run.__wrapped__(run.id)

            response, status_code = response
            assert status_code == 409
            assert response.get_json() == {
                "error": "Source file missing.",
                "missing_documents": [{"id": document.id, "filename": "paused.txt"}],
            }
            db.session.expire_all()
            refreshed_run = db.session.get(ProjectRun, run.id)
            refreshed_entry = db.session.get(ProjectRunDocument, entry.id)
            refreshed_document = db.session.get(ProcessedDocument, document.id)
            assert (refreshed_run.status, refreshed_run.pause_requested, refreshed_run.stop_requested) == original_run
            assert (refreshed_entry.status, refreshed_entry.error_message, refreshed_entry.completed_at) == original_entry
            assert (refreshed_document.status, refreshed_document.settings, refreshed_document.error_message) == original_document
            assert queue.enqueued == []

            db.session.remove()
            db.drop_all()
            db.engine.dispose()


def test_scheduler_fence_closes_deleted_source_after_claim_without_enqueue():
    with tempfile.TemporaryDirectory() as temp_dir:
        app = _make_app(temp_dir)
        uploads_dir = Path(temp_dir) / "uploads"
        source_path = uploads_dir / "race.txt"
        source_path.parent.mkdir(parents=True)
        source_path.write_text("source", encoding="utf-8")
        with app.app_context():
            db.create_all()
            db.session.add(User(id=1, email="projects@example.test", password_hash="test"))
            document = ProcessedDocument(
                user_id=1,
                filename="race.txt",
                file_path="/uploads/race.txt",
                status="created",
                settings={"keep": "original"},
            )
            project = DocumentProject(user_id=1, name="Race project")
            run = ProjectRun(project=project, user_id=1, status="queued")
            entry = ProjectRunDocument(
                run=run,
                document=document,
                position=0,
                status="queued",
                effective_settings={"engine_id": "auto"},
            )
            db.session.add_all([document, project, run, entry])
            db.session.commit()
            queue = FakeQueue()
            source_checks = 0

            def source_fence(_document):
                nonlocal source_checks
                source_checks += 1
                if source_checks == 2:
                    source_path.unlink()
                    return None
                return source_path

            with patch(
                "iarf_assistant.addons.doc_processor.project_service._document_source_path",
                side_effect=source_fence,
            ):
                schedule_project_run(run.id, queue)

            db.session.expire_all()
            refreshed_entry = db.session.get(ProjectRunDocument, entry.id)
            refreshed_document = db.session.get(ProcessedDocument, document.id)
            assert source_checks == 2
            assert queue.enqueued == []
            assert refreshed_entry.status == "error"
            assert refreshed_entry.error_message == "Source file missing."
            assert refreshed_entry.job_id is None
            assert refreshed_document.status == "created"
            assert refreshed_document.settings == {"keep": "original"}
            assert "active_project_run_document_id" not in refreshed_document.settings
            assert "active_job_id" not in refreshed_document.settings

            db.session.remove()
            db.drop_all()
            db.engine.dispose()


def test_project_crud_routes():
    with tempfile.TemporaryDirectory() as temp_dir:
        app = _make_app(temp_dir)
        with app.app_context():
            db.create_all()
            db.session.add(User(id=1, email="projects@example.test", password_hash="test"))
            db.session.commit()

            # 1. Create project
            with app.test_request_context(
                "/addons/doc_processor/api/projects",
                method="POST",
                json={"name": "New Project", "document_concurrency": 3},
            ):
                with patch.object(routes, "current_user", SimpleNamespace(id=1)):
                    response, status_code = routes.create_project.__wrapped__()
            
            assert status_code == 201
            data = response.get_json()
            assert data["name"] == "New Project"
            assert data["settings"]["document_concurrency"] == 3
            project_id = data["id"]

            # 2. List projects
            with app.test_request_context("/addons/doc_processor/api/projects", method="GET"):
                with patch.object(routes, "current_user", SimpleNamespace(id=1)):
                    response = routes.list_projects.__wrapped__()
            status_code = response.status_code
            assert status_code == 200
            projects = response.get_json()
            assert len(projects) == 1
            assert projects[0]["id"] == project_id

            # 3. Update project
            with app.test_request_context(
                f"/addons/doc_processor/api/projects/{project_id}",
                method="PUT",
                json={"document_concurrency": 5},
            ):
                with patch.object(routes, "current_user", SimpleNamespace(id=1)):
                    response = routes.update_project.__wrapped__(project_id)
            status_code = response.status_code
            assert status_code == 200
            assert response.get_json()["success"] is True
            proj = db.session.get(DocumentProject, project_id)
            assert proj.settings["document_concurrency"] == 5

            # 4. Delete project
            with app.test_request_context(
                f"/addons/doc_processor/api/projects/{project_id}",
                method="DELETE"
            ):
                with patch.object(routes, "current_user", SimpleNamespace(id=1)):
                    response = routes.delete_project.__wrapped__(project_id)
            status_code = response.status_code
            assert status_code == 200
            assert response.get_json()["success"] is True
            assert db.session.get(DocumentProject, project_id) is None

            db.session.remove()
            db.drop_all()
            db.engine.dispose()


def test_project_document_management_routes():
    with tempfile.TemporaryDirectory() as temp_dir:
        app = _make_app(temp_dir)
        with app.app_context():
            db.create_all()
            db.session.add(User(id=1, email="projects@example.test", password_hash="test"))
            db.session.commit()
            project = DocumentProject(user_id=1, name="Doc Manage Project")
            doc1 = ProcessedDocument(user_id=1, filename="d1.txt", file_path="/uploads/d1.txt")
            doc2 = ProcessedDocument(user_id=1, filename="d2.txt", file_path="/uploads/d2.txt")
            db.session.add_all([project, doc1, doc2])
            db.session.commit()

            # 1. Add documents to project
            with app.test_request_context(
                f"/addons/doc_processor/api/projects/{project.id}/documents",
                method="POST",
                json={"document_ids": [doc1.id, doc2.id]},
            ):
                with patch.object(routes, "current_user", SimpleNamespace(id=1)):
                    response = routes.add_project_documents.__wrapped__(project.id)
            
            status_code = response.status_code
            assert status_code == 200
            data = response.get_json()["project"]
            assert len(data["documents"]) == 2
            assert data["documents"][0]["document_id"] == doc1.id
            assert data["documents"][1]["document_id"] == doc2.id

            # 2. Reorder documents
            with app.test_request_context(
                f"/addons/doc_processor/api/projects/{project.id}/documents/order",
                method="PUT",
                json={"document_ids": [doc2.id, doc1.id]},
            ):
                with patch.object(routes, "current_user", SimpleNamespace(id=1)):
                    response = routes.reorder_project_documents.__wrapped__(project.id)
            status_code = response.status_code
            assert status_code == 200
            
            proj_reloaded = db.session.get(DocumentProject, project.id)
            ordered_doc_ids = [pd.document_id for pd in proj_reloaded.documents]
            assert ordered_doc_ids == [doc2.id, doc1.id]

            # 3. Remove document
            with app.test_request_context(
                f"/addons/doc_processor/api/projects/{project.id}/documents/{doc2.id}",
                method="DELETE",
            ):
                with patch.object(routes, "current_user", SimpleNamespace(id=1)):
                    response = routes.remove_project_document.__wrapped__(project.id, doc2.id)
            status_code = response.status_code
            assert status_code == 200
            data = response.get_json()["project"]
            assert len(data["documents"]) == 1
            assert data["documents"][0]["document_id"] == doc1.id
            assert data["documents"][0]["position"] == 0

            db.session.remove()
            db.drop_all()
            db.engine.dispose()


def test_set_pdf_crop_syncs_project_run():
    from pathlib import Path
    with tempfile.TemporaryDirectory() as temp_dir:
        app = _make_app(temp_dir)
        with app.app_context():
            db.create_all()
            db.session.add(User(id=1, email="projects@example.test", password_hash="test"))
            db.session.commit()
            
            import fitz
            pdf_path = Path(temp_dir) / "test.pdf"
            doc_fitz = fitz.open()
            doc_fitz.new_page()
            doc_fitz.save(str(pdf_path))
            doc_fitz.close()
            
            document = ProcessedDocument(
                user_id=1,
                filename="test.pdf",
                file_path=str(pdf_path),
                settings={"active_project_run_document_id": "run-doc-123"},
                status="needs_cropping",
            )
            project = DocumentProject(user_id=1, name="Crop Sync Proj")
            db.session.add_all([document, project])
            db.session.flush()
            
            run = ProjectRun(project=project, user_id=1, status="running")
            db.session.add(run)
            db.session.flush()
            
            run_doc = ProjectRunDocument(
                id="run-doc-123",
                run=run,
                document=document,
                position=0,
                status="needs_cropping",
            )
            db.session.add(run_doc)
            db.session.commit()
            
            fake_queue = FakeQueue()
            
            with app.test_request_context(
                f"/addons/doc_processor/api/documents/{document.id}/set-crop",
                method="POST",
                json={
                    "crop_box": [10.0, 10.0, 100.0, 100.0],
                    "crop_boxes": {"mode": "auto"},
                },
            ):
                with patch.object(routes, "current_user", SimpleNamespace(id=1)), \
                     patch.object(routes, "_get_task_queue", return_value=fake_queue), \
                     patch.object(routes, "_ensure_workers"):
                    response = routes.set_pdf_crop.__wrapped__(document.id)
                    
            status_code = response.status_code
            assert status_code == 200
            assert response.get_json()["success"] is True
            
            assert len(fake_queue.enqueued) == 1
            task_name, payload = fake_queue.enqueued[0]
            assert task_name == "process_document"
            assert payload["project_run_document_id"] == "run-doc-123"
            
            assert run_doc.status == "processing"
            assert run_doc.job_id == "job-1"
            
            db.session.remove()
            db.drop_all()
            db.engine.dispose()


def test_set_pdf_crop_can_skip_without_pymupdf():
    from pathlib import Path

    from pypdf import PdfWriter

    with tempfile.TemporaryDirectory() as temp_dir:
        app = _make_app(temp_dir)
        with app.app_context():
            db.create_all()
            db.session.add(
                User(id=1, email="crop-skip@example.test", password_hash="test")
            )
            db.session.commit()

            pdf_path = Path(temp_dir) / "test.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=612, height=792)
            with pdf_path.open("wb") as pdf_file:
                writer.write(pdf_file)

            document = ProcessedDocument(
                user_id=1,
                filename="test.pdf",
                file_path=str(pdf_path),
                settings={},
                status="needs_cropping",
            )
            db.session.add(document)
            db.session.commit()

            fake_queue = FakeQueue()
            with app.test_request_context(
                f"/addons/doc_processor/api/documents/{document.id}/set-crop",
                method="POST",
                json={
                    "crop_box": None,
                    "crop_boxes": {
                        "mode": "left_right",
                        "left": None,
                        "right": None,
                        "first_page_side": "right",
                    },
                    "excluded_pages": [],
                },
            ):
                with (
                    patch.object(routes, "current_user", SimpleNamespace(id=1)),
                    patch.object(routes, "fitz", None),
                    patch.object(routes, "_get_task_queue", return_value=fake_queue),
                    patch.object(routes, "_ensure_workers"),
                ):
                    response = routes.set_pdf_crop.__wrapped__(document.id)

            assert response.status_code == 200
            assert response.get_json()["success"] is True
            assert fake_queue.enqueued[0][0] == "process_document"

            db.session.remove()
            db.drop_all()
            db.engine.dispose()


def test_update_pdf_images_syncs_project_run():
    with tempfile.TemporaryDirectory() as temp_dir:
        app = _make_app(temp_dir)
        with app.app_context():
            db.create_all()
            db.session.add(User(id=1, email="projects@example.test", password_hash="test"))
            db.session.commit()
            
            document = ProcessedDocument(
                user_id=1,
                filename="test.pdf",
                file_path="/uploads/test.pdf",
                settings={
                    "active_project_run_document_id": "run-doc-456",
                        "pdf_state": {
                            "phase": "PAUSED_IMAGE_REVIEW",
                            "included_pages": [0],
                            "html_parts": ["<main><img src='foo'></main>"],
                        },
                },
                status="reviewing_images",
            )
            source_path = Path(temp_dir) / "uploads" / "test.pdf"
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_bytes(b"%PDF")
            project = DocumentProject(user_id=1, name="Image Sync Proj")
            db.session.add_all([document, project])
            db.session.flush()
            
            run = ProjectRun(project=project, user_id=1, status="running")
            db.session.add(run)
            db.session.flush()
            
            run_doc = ProjectRunDocument(
                id="run-doc-456",
                run=run,
                document=document,
                position=0,
                status="reviewing_images",
            )
            db.session.add(run_doc)
            db.session.commit()
            
            fake_queue = FakeQueue()
            
            with app.test_request_context(
                f"/addons/doc_processor/api/documents/{document.id}/pdf-images",
                method="PUT",
                json=[{"page_index": 0, "images": [{"img_idx": 0, "bbox": [0, 0, 50, 50], "keep": True}]}],
            ):
                with patch.object(routes, "current_user", SimpleNamespace(id=1)), \
                     patch.object(routes, "_get_task_queue", return_value=fake_queue), \
                     patch.object(routes, "_ensure_workers"):
                    response = routes.update_pdf_images.__wrapped__(document.id)
                    
            status_code = response.status_code
            assert status_code == 200
            assert response.get_json()["success"] is True
            
            assert len(fake_queue.enqueued) == 1
            task_name, payload = fake_queue.enqueued[0]
            assert task_name == "resume_pdf_parsing"
            assert payload["project_run_document_id"] == "run-doc-456"
            
            assert run_doc.status == "processing"
            assert run_doc.job_id == "job-1"
            
            db.session.remove()
            db.drop_all()
            db.engine.dispose()


def test_resume_pdf_parsing_worker_syncs():
    from iarf_assistant.addons.doc_processor import worker
    
    with tempfile.TemporaryDirectory() as temp_dir:
        app = _make_app(temp_dir)
        with app.app_context():
            db.create_all()
            db.session.add(User(id=1, email="projects@example.test", password_hash="test"))
            db.session.commit()
            
            document = ProcessedDocument(
                id="doc-res-1",
                user_id=1,
                filename="test.pdf",
                file_path="/uploads/test.pdf",
                settings={
                    "pdf_state": {
                        "phase": "PAUSED_IMAGE_REVIEW",
                        "included_pages": [0],
                        "html_parts": ["<main></main>"],
                    }
                },
                status="processing",
            )
            source_path = Path(temp_dir) / "uploads" / "test.pdf"
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_bytes(b"%PDF")
            project = DocumentProject(user_id=1, name="Resume Proj")
            db.session.add_all([document, project])
            db.session.flush()
            
            run = ProjectRun(project=project, user_id=1, status="running")
            db.session.add(run)
            db.session.flush()
            
            run_doc = ProjectRunDocument(
                id="run-doc-res-1",
                run=run,
                document=document,
                position=0,
                status="processing",
            )
            db.session.add(run_doc)
            db.session.commit()
            
            fake_queue = FakeQueue()
            
            mock_parser = SimpleNamespace(
                resume_processing=lambda: ("COMPLETED", "<main>Done</main>"),
                metrics={},
            )
            
            with patch("iarf_assistant.addons.doc_processor.worker.get_parser", return_value=mock_parser), \
                 patch("iarf_assistant.addons.doc_processor.worker.LLMService"), \
                 patch("iarf_assistant.addons.doc_processor.worker.db", db):
                
                payload = {
                    "document_id": "doc-res-1",
                    "project_run_document_id": "run-doc-res-1"
                }
                worker.resume_pdf_parsing(payload, fake_queue, "job-abc")
                
            assert run_doc.status == "completed"
            
            db.session.remove()
            db.drop_all()
            db.engine.dispose()


def test_start_project_run_skips_completed_documents():
    from iarf_assistant.addons.doc_processor.project_service import (
        build_effective_settings,
    )
    with tempfile.TemporaryDirectory() as temp_dir:
        app = _make_app(temp_dir)
        with app.app_context():
            db.create_all()
            db.session.add(User(id=1, email="projects@example.test", password_hash="test"))
            db.session.commit()
            uploads_dir = Path(temp_dir) / "uploads"
            uploads_dir.mkdir(parents=True)
            for filename in ("completed.txt", "created.txt"):
                (uploads_dir / filename).write_bytes(b"source")

            # 1 completed doc, 1 created doc
            doc_completed = ProcessedDocument(user_id=1, filename="completed.txt", file_path="/uploads/completed.txt", status="completed")
            doc_created = ProcessedDocument(user_id=1, filename="created.txt", file_path="/uploads/created.txt", status="created")
            project = DocumentProject(user_id=1, name="Skip Completed Project", settings={"document_concurrency": 2})
            db.session.add_all([doc_completed, doc_created, project])
            db.session.flush()
            
            db.session.add(ProjectDocument(project=project, document=doc_completed, position=0))
            db.session.add(ProjectDocument(project=project, document=doc_created, position=1))
            db.session.commit()
            
            with app.test_request_context(
                f"/addons/doc_processor/api/projects/{project.id}/runs",
                method="POST"
            ):
                with patch.object(routes, "current_user", SimpleNamespace(id=1)), \
                     patch("iarf_assistant.addons.doc_processor.routes._resolve_local_path") as mock_resolve, \
                     patch("iarf_assistant.addons.doc_processor.routes._get_task_queue", return_value=FakeQueue()), \
                     patch("iarf_assistant.addons.doc_processor.routes._ensure_workers"):
                    mock_resolve.return_value.exists.return_value = True
                    response, status_code = routes.start_project_run.__wrapped__(project.id)
                    
            assert status_code == 201
            payload = response.get_json()
            run_id = payload["id"]
            
            run_docs = ProjectRunDocument.query.filter_by(run_id=run_id).order_by(ProjectRunDocument.position.asc()).all()
            assert len(run_docs) == 2
            # First one (completed) should be skipped and marked completed
            assert run_docs[0].document_id == doc_completed.id
            assert run_docs[0].status == "completed"
            assert run_docs[0].completed_at is not None
            
            # Second one (created) should be queued/processing
            assert run_docs[1].document_id == doc_created.id
            assert run_docs[1].status in ["queued", "processing"]
            
            db.session.remove()
            db.drop_all()
            db.engine.dispose()


def test_find_and_link_active_project_run_document():
    from iarf_assistant.addons.doc_processor.project_service import (
        find_and_link_active_project_run_document,
    )
    with tempfile.TemporaryDirectory() as temp_dir:
        app = _make_app(temp_dir)
        with app.app_context():
            db.create_all()
            db.session.add(User(id=1, email="projects@example.test", password_hash="test"))
            db.session.commit()
            
            document = ProcessedDocument(user_id=1, filename="test.txt", file_path="/uploads/test.txt")
            project = DocumentProject(user_id=1, name="Active Run Proj")
            db.session.add_all([document, project])
            db.session.flush()
            
            # Create a run that is NOT completed (running)
            run = ProjectRun(project=project, user_id=1, status="running")
            db.session.add(run)
            db.session.flush()
            
            run_doc = ProjectRunDocument(
                run=run,
                document=document,
                position=0,
                status="error",  # failed during the run
            )
            db.session.add(run_doc)
            db.session.commit()
            
            # Find and link should succeed because run.status is "running" (not completed/cancelled)
            entry_id = find_and_link_active_project_run_document(document)
            assert entry_id == run_doc.id
            assert document.settings["active_project_run_document_id"] == run_doc.id
            
            db.session.remove()
            db.drop_all()
            db.engine.dispose()


def test_process_document_links_and_syncs_active_project_run():
    with tempfile.TemporaryDirectory() as temp_dir:
        app = _make_app(temp_dir)
        with app.app_context():
            db.create_all()
            db.session.add(User(id=1, email="projects@example.test", password_hash="test"))
            db.session.commit()
            
            document = ProcessedDocument(user_id=1, filename="test.txt", file_path="/uploads/test.txt", status="error")
            project = DocumentProject(user_id=1, name="Active Run Proj")
            db.session.add_all([document, project])
            db.session.flush()
            
            run = ProjectRun(project=project, user_id=1, status="waiting_for_review")
            db.session.add(run)
            db.session.flush()
            
            run_doc = ProjectRunDocument(
                run=run,
                document=document,
                position=0,
                status="error",
            )
            db.session.add(run_doc)
            db.session.commit()

            source_path = Path(temp_dir) / "uploads" / "test.txt"
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_text("source", encoding="utf-8")
            
            fake_queue = FakeQueue()
            
            with app.test_request_context(
                f"/addons/doc_processor/api/documents/{document.id}/process",
                method="POST",
                json={}
            ):
                with patch.object(routes, "current_user", SimpleNamespace(id=1)), \
                     patch("iarf_assistant.addons.doc_processor.routes._enqueue_job", return_value="job-123") as mock_enqueue, \
                     patch("iarf_assistant.addons.doc_processor.routes._get_task_queue", return_value=fake_queue):
                    response = routes.process_document.__wrapped__(document.id)
                    
            assert response.status_code == 200
            # Verify the project run document status is updated to 'processing'
            assert run_doc.status == "processing"
            # Verify the enqueued task payload has 'project_run_document_id'
            mock_enqueue.assert_called_once_with(
                "process_document",
                {"document_id": document.id, "engine_id": "auto", "project_run_document_id": run_doc.id},
                num_workers=1
            )
            
            db.session.remove()
            db.drop_all()
            db.engine.dispose()


def test_process_document_missing_source_returns_409_without_mutation():
    with tempfile.TemporaryDirectory() as temp_dir:
        app = _make_app(temp_dir)
        with app.app_context():
            db.create_all()
            db.session.add(User(id=1, email="projects@example.test", password_hash="test"))
            document = ProcessedDocument(
                user_id=1,
                filename="missing.txt",
                file_path="/uploads/missing.txt",
                status="error",
                settings={"engine_id": "marker", "keep": "unchanged"},
                error_message="previous failure",
            )
            db.session.add(document)
            db.session.commit()
            original_settings = dict(document.settings)

            with app.test_request_context(
                f"/addons/doc_processor/api/documents/{document.id}/process",
                method="POST",
                json={"engine": "pymupdf"},
            ):
                with patch.object(routes, "current_user", SimpleNamespace(id=1)), patch.object(
                    routes, "_enqueue_job"
                ) as enqueue:
                    response = routes.process_document.__wrapped__(document.id)

            response, status_code = response
            assert status_code == 409
            assert response.get_json() == {"error": "Source file missing."}
            assert enqueue.call_count == 0
            assert document.status == "error"
            assert document.error_message == "previous failure"
            assert document.settings == original_settings

            db.session.remove()
            db.drop_all()
            db.engine.dispose()


def test_process_document_returns_existing_nonterminal_job_without_duplicate_enqueue():
    with tempfile.TemporaryDirectory() as temp_dir:
        app = _make_app(temp_dir)
        app.config["CARLINA_UPLOADS_DIR"] = str(Path(temp_dir) / "uploads")
        with app.app_context():
            db.create_all()
            db.session.add(User(id=1, email="projects@example.test", password_hash="test"))
            document = ProcessedDocument(
                user_id=1,
                filename="source.txt",
                file_path="/uploads/1/source.txt",
                status="processing",
                settings={"active_job_id": "job-existing", "engine_id": "auto"},
            )
            db.session.add(document)
            db.session.commit()
            queue = FakeQueue()
            queue.status = {"status": "processing", "progress": 42}
            original_settings = dict(document.settings)

            with app.test_request_context(
                f"/addons/doc_processor/api/documents/{document.id}/process",
                method="POST",
                json={"engine": "pymupdf"},
            ):
                with patch.object(routes, "current_user", SimpleNamespace(id=1)), \
                     patch.object(routes, "_get_task_queue", return_value=queue), \
                     patch.object(routes, "_enqueue_job") as enqueue:
                    response = routes.process_document.__wrapped__(document.id)

            response, status_code = response
            assert status_code == 202
            assert response.get_json() == {
                "success": True,
                "job_id": "job-existing",
                "idempotent": True,
                "message": "Document processing is already in progress.",
            }
            enqueue.assert_not_called()
            assert document.settings == original_settings

            db.session.remove()
            db.drop_all()
            db.engine.dispose()


def test_process_document_respects_inflight_enqueue_claim_without_duplicate_enqueue():
    with tempfile.TemporaryDirectory() as temp_dir:
        app = _make_app(temp_dir)
        with app.app_context():
            db.create_all()
            db.session.add(User(id=1, email="projects@example.test", password_hash="test"))
            document = ProcessedDocument(
                user_id=1,
                filename="source.txt",
                file_path="/uploads/1/source.txt",
                status="processing",
                settings={
                    "enqueue_claim": {"token": "claim-1", "created_at": time.time()},
                    "engine_id": "auto",
                },
            )
            db.session.add(document)
            db.session.commit()

            with app.test_request_context(
                f"/addons/doc_processor/api/documents/{document.id}/process",
                method="POST",
                json={},
            ):
                with patch.object(routes, "current_user", SimpleNamespace(id=1)), \
                     patch.object(routes, "_enqueue_job") as enqueue:
                    response = routes.process_document.__wrapped__(document.id)

            response, status_code = response
            assert status_code == 409
            assert response.get_json() == {
                "error": "A processing request is already being queued. Please retry shortly."
            }
            enqueue.assert_not_called()
            assert document.settings["enqueue_claim"]["token"] == "claim-1"

            db.session.remove()
            db.drop_all()
            db.engine.dispose()


def test_process_document_recovers_terminal_marker_and_records_new_job():
    class StatusQueue(FakeQueue):
        def __init__(self):
            super().__init__()
            self.status_by_job = {
                "job-stale": {"status": "failed"},
                "job-new": {"status": "queued"},
            }

        def get_status(self, job_id):
            return self.status_by_job.get(job_id, {"status": "unknown"})

    with tempfile.TemporaryDirectory() as temp_dir:
        app = _make_app(temp_dir)
        uploads = Path(temp_dir) / "uploads"
        app.config["CARLINA_UPLOADS_DIR"] = str(uploads)
        source_path = uploads / "1" / "source.txt"
        source_path.parent.mkdir(parents=True)
        source_path.write_text("source", encoding="utf-8")
        with app.app_context():
            db.create_all()
            db.session.add(User(id=1, email="projects@example.test", password_hash="test"))
            document = ProcessedDocument(
                user_id=1,
                filename="source.txt",
                file_path="/uploads/1/source.txt",
                status="processing",
                settings={"active_job_id": "job-stale", "engine_id": "auto"},
            )
            db.session.add(document)
            db.session.commit()
            queue = StatusQueue()

            with app.test_request_context(
                f"/addons/doc_processor/api/documents/{document.id}/process",
                method="POST",
                json={},
            ):
                with patch.object(routes, "current_user", SimpleNamespace(id=1)), \
                     patch.object(routes, "_get_task_queue", return_value=queue), \
                     patch.object(routes, "_enqueue_job", return_value="job-new") as enqueue:
                    response = routes.process_document.__wrapped__(document.id)

            assert response.status_code == 200
            assert response.get_json() == {"success": True, "job_id": "job-new"}
            enqueue.assert_called_once()
            assert document.settings["active_job_id"] == "job-new"

            db.session.remove()
            db.drop_all()
            db.engine.dispose()


def test_process_document_enqueue_failure_clears_active_marker_and_claim():
    with tempfile.TemporaryDirectory() as temp_dir:
        app = _make_app(temp_dir)
        uploads = Path(temp_dir) / "uploads"
        app.config["CARLINA_UPLOADS_DIR"] = str(uploads)
        source_path = uploads / "1" / "source.txt"
        source_path.parent.mkdir(parents=True)
        source_path.write_text("source", encoding="utf-8")
        with app.app_context():
            db.create_all()
            db.session.add(User(id=1, email="projects@example.test", password_hash="test"))
            document = ProcessedDocument(
                user_id=1,
                filename="source.txt",
                file_path="/uploads/1/source.txt",
                status="error",
                settings={"enqueue_claim": "claim-1", "engine_id": "auto"},
            )
            db.session.add(document)
            db.session.commit()
            queue = FakeQueue()

            with app.test_request_context(
                f"/addons/doc_processor/api/documents/{document.id}/process",
                method="POST",
                json={},
            ):
                with patch.object(routes, "current_user", SimpleNamespace(id=1)), \
                     patch.object(routes, "_get_task_queue", return_value=queue), \
                     patch.object(
                         routes,
                         "_enqueue_job",
                         side_effect=RuntimeError("queue unavailable"),
                     ):
                    response = routes.process_document.__wrapped__(document.id)

            response, status_code = response
            assert status_code == 503
            assert response.get_json()["error"] == "Processing is temporarily unavailable. Please try again."
            assert document.status == "error"
            assert "active_job_id" not in document.settings
            assert "enqueue_claim" not in document.settings

            db.session.remove()
            db.drop_all()
            db.engine.dispose()


def test_project_start_uses_configured_upload_root_when_package_root_is_split():
    with tempfile.TemporaryDirectory() as temp_dir:
        package_root = Path(temp_dir) / "installed-package"
        uploads_root = Path(temp_dir) / "live-data" / "uploads"
        package_root.mkdir(parents=True)
        source_path = uploads_root / "1" / "document.txt"
        source_path.parent.mkdir(parents=True)
        source_path.write_text("source", encoding="utf-8")

        app = _make_app(str(package_root))
        app.config["CARLINA_UPLOADS_DIR"] = str(uploads_root)
        with app.app_context():
            db.create_all()
            db.session.add(User(id=1, email="projects@example.test", password_hash="test"))
            document = ProcessedDocument(
                user_id=1,
                filename="document.txt",
                file_path="/uploads/1/document.txt",
                status="created",
                settings={"engine_id": "auto"},
            )
            project = DocumentProject(user_id=1, name="Split root")
            db.session.add_all([document, project])
            db.session.flush()
            db.session.add(ProjectDocument(project=project, document=document, position=0))
            db.session.commit()
            queue = FakeQueue()

            with app.test_request_context(
                f"/addons/doc_processor/api/projects/{project.id}/runs", method="POST"
            ):
                with patch.object(routes, "current_user", SimpleNamespace(id=1)), patch.object(
                    routes, "_get_task_queue", return_value=queue
                ), patch.object(routes, "_ensure_workers"):
                    response, status_code = routes.start_project_run.__wrapped__(project.id)

            assert status_code == 201
            assert queue.enqueued and queue.enqueued[0][0] == "process_document"

            db.session.remove()
            db.drop_all()
            db.engine.dispose()
