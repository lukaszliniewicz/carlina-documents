from __future__ import annotations

import base64
import hashlib
import importlib
import os
import stat
import sys
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Lock
from types import SimpleNamespace

import pytest
from content_storage_support import bind_content_policy
from flask import Flask
from sqlalchemy import inspect

from carlina.core.epub_export import epub_value_sha256, normalize_epub_options
from iarf_assistant.addons.doc_processor import mcp_exports, worker
from iarf_assistant.addons.doc_processor.epub_quality import direct_epub_review_sha256
from iarf_assistant.addons.doc_processor.mcp_review import _revision
from iarf_assistant.addons.doc_processor.mcp_service import (
    DocProcessorMcpService,
    DocProcessorMcpServiceError,
)
from iarf_assistant.addons.doc_processor.models import (
    DocumentMetadata,
    ProcessedDocument,
)
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.core.task_queue import TaskQueue
from iarf_assistant.extensions import db
from iarf_assistant.models import User

CONTRACT = "document-mcp-export-v1"
TASK = "mcp_export_document"
FORMATS = ("epub", "docx", "html", "pdf", "md")
HTML = "<main><h1>Synthetic book</h1><p>Export body.</p></main>"
FORMAT_BYTES = {
    "epub": b"PK\x03\x04synthetic epub bytes",
    "docx": b"synthetic docx bytes",
    "html": b"<html>synthetic</html>",
    "pdf": b"%PDF-1.7\nsynthetic pdf bytes",
    "md": b"# Synthetic book\n\nExport body.\n",
}
MIMETYPES = {
    "epub": "application/epub+zip",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "html": "text/html",
    "pdf": "application/pdf",
    "md": "text/markdown",
}


@pytest.fixture
def app(tmp_path):
    application = Flask(
        "doc-processor-mcp-export-test",
        instance_path=str(tmp_path / "instance"),
    )
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'exports.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="synthetic-export-test",
        TESTING=True,
        CARLINA_UPLOADS_DIR=str(tmp_path / "uploads"),
    )
    db.init_app(application)
    application.extensions["carlina_encryption"] = EncryptionService(
        InstallationKeyStore.ephemeral()
    )
    with application.app_context():
        db.create_all()
        db.session.add_all(
            [
                User(id=1, email="export-one@example.test", password_hash="test"),
                User(id=2, email="export-two@example.test", password_hash="test"),
            ]
        )
        db.session.commit()
    yield application
    with application.app_context():
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


class ExportContext:
    def __init__(self, application, queue, *, fail_worker_pool=0):
        self.application = application
        self.queue = queue
        self.worker_pool_calls = []
        self.fail_worker_pool = fail_worker_pool

    def get_task_queue(self, addon_id):
        assert addon_id == "doc_processor"
        return self.queue

    def ensure_worker_pool(self, script_name, *, num_workers=1):
        self.worker_pool_calls.append((script_name, num_workers))
        if self.fail_worker_pool:
            self.fail_worker_pool -= 1
            raise RuntimeError("synthetic worker launch failure")


def _service(application, queue, owner=1, *, fail_worker_pool=0):
    context = ExportContext(application, queue, fail_worker_pool=fail_worker_pool)
    return DocProcessorMcpService(context, owner), context


def _document(
    *,
    document_id="doc-export",
    owner=1,
    html=HTML,
    status="completed",
    filename="synthetic book.txt",
):
    document = ProcessedDocument(
        id=document_id,
        user_id=owner,
        filename=filename,
        file_path=f"/uploads/{owner}/synthetic-source.txt",
        status=status,
        output_html=html,
        output_markdown="# Synthetic book\n\nExport body.\n",
        settings={"document_metadata": {"title": "Synthetic book"}},
    )
    db.session.add(document)
    db.session.commit()
    return document


def _metadata(document, export_format, revision, *, start_h2_on_new_page=False):
    identity = {
        "contract": CONTRACT,
        "user_id": document.user_id,
        "document_id": document.id,
        "format": export_format,
        "expected_revision": revision,
        "start_h2_on_new_page": start_h2_on_new_page,
    }
    request_hash = mcp_exports._canonical_hash(identity)
    return {
        "contract": CONTRACT,
        "document_id": document.id,
        "format": export_format,
        "revision": revision,
        "request_hash": request_hash,
    }


def _error_code(call, *args, code, **kwargs):
    with pytest.raises(DocProcessorMcpServiceError) as raised:
        call(*args, **kwargs)
    assert raised.value.code == code
    return raised.value


def _patch_binary_renderer(
    monkeypatch, export_format, *, captured_html=None, captured_metadata=None
):
    if export_format == "epub":
        packager_module = importlib.import_module(
            "iarf_assistant.addons.doc_processor.epub_packager"
        )
        routes = importlib.import_module("iarf_assistant.addons.doc_processor.routes")
        from iarf_assistant.addons.doc_processor.metadata_service import (
            get_canonical_document_metadata,
        )

        class FakePackager:
            def __init__(self, document, output, **_options):
                self.document = document
                if captured_html is not None:
                    captured_html.append(document.output_html)
                if captured_metadata is not None:
                    captured_metadata.append(get_canonical_document_metadata(document))
                self.output = Path(output)
                self.options = _options.get("options")

            def build(self):
                self.output.write_bytes(FORMAT_BYTES["epub"])

            def quality_report(self, **_checks):
                artifact = self.output.read_bytes()
                return {
                    "schema": "epub-quality-v1",
                    "profile": "publishing-v1",
                    "input_sha256": hashlib.sha256(
                        self.document.output_html.encode("utf-8")
                    ).hexdigest(),
                    "options_sha256": epub_value_sha256(self.options),
                    "artifact_sha256": hashlib.sha256(artifact).hexdigest(),
                    "content_sha256": hashlib.sha256(b"publication").hexdigest(),
                    "checks": {
                        "structure": _checks.get("structure", "not_run"),
                        "epubcheck": _checks.get("epubcheck", "not_run"),
                        "ace": "not_run",
                        "manual": "not_reviewed",
                    },
                    "counts": {
                        "notes": 0,
                        "references": 0,
                        "backlinks": 0,
                        "headings": 1,
                        "images": 0,
                        "tables": 0,
                        "pages": 0,
                    },
                    "issues": [],
                    "issue_count": 0,
                    "epubcheck_version": _checks.get("epubcheck_version"),
                }

        monkeypatch.setattr(packager_module, "EPUBPackager", FakePackager)
        validation_module = importlib.import_module(
            "iarf_assistant.addons.doc_processor.epub_validation"
        )
        from iarf_assistant.addons.doc_processor.epub_validation import (
            EpubValidationResult,
        )

        monkeypatch.setattr(
            validation_module,
            "validate_epub_file",
            lambda _path, **_kwargs: EpubValidationResult(True, None, "not_run"),
        )
        monkeypatch.setattr(routes, "_validate_epub_file", lambda _path: (True, None))
    elif export_format == "pdf":
        print_export = importlib.import_module(
            "iarf_assistant.addons.doc_processor.print_export"
        )

        def render_pdf(html, *, metadata, **_kwargs):
            if captured_html is not None:
                captured_html.append(html)
            if captured_metadata is not None:
                captured_metadata.append(metadata)
            return FORMAT_BYTES["pdf"]

        monkeypatch.setattr(print_export, "render_pdf_export", render_pdf)
        monkeypatch.setattr(
            worker,
            "_get_client",
            lambda _user_id: SimpleNamespace(
                context=SimpleNamespace(run_managed_process=lambda *_args: None)
            ),
        )
    elif export_format == "docx":
        fake_pandoc = types.ModuleType("pypandoc")

        def convert_text(_content, *, outputfile, **_kwargs):
            Path(outputfile).write_bytes(b"synthetic intermediate docx")

        fake_pandoc.convert_text = convert_text
        monkeypatch.setitem(sys.modules, "pypandoc", fake_pandoc)
        docx_preprocessor = importlib.import_module(
            "iarf_assistant.addons.doc_processor.docx_preprocessor"
        )
        docx_notes = importlib.import_module(
            "iarf_assistant.addons.doc_processor.docx_notes"
        )
        print_export = importlib.import_module(
            "iarf_assistant.addons.doc_processor.print_export"
        )
        routes = importlib.import_module("iarf_assistant.addons.doc_processor.routes")

        def preprocess_html(html, **_kwargs):
            if captured_html is not None:
                captured_html.append(html)
            return html

        monkeypatch.setattr(
            docx_preprocessor, "preprocess_html_for_docx", preprocess_html
        )
        monkeypatch.setattr(
            docx_notes, "preserve_repeated_docx_notes", lambda data, _html: data
        )

        def finish_docx(data, _html, *, title, **_kwargs):
            if captured_metadata is not None:
                captured_metadata.append({"title": title})
            return FORMAT_BYTES["docx"]

        monkeypatch.setattr(print_export, "finish_docx_export", finish_docx)
        monkeypatch.setattr(routes, "_resolve_local_path", lambda _path: None)
        monkeypatch.setattr(
            worker, "ensure_default_reference_doc", lambda: "/reference.docx"
        )


def _patch_replacing_epub_failure(monkeypatch, replacement: bytes) -> None:
    packager_module = importlib.import_module(
        "iarf_assistant.addons.doc_processor.epub_packager"
    )

    class ReplacingFailingPackager:
        def __init__(self, _document, output, **_options):
            self.output = Path(output)

        def build(self):
            self.output.unlink()
            self.output.write_bytes(replacement)
            raise RuntimeError("synthetic converter failure")

    monkeypatch.setattr(packager_module, "EPUBPackager", ReplacingFailingPackager)


class ConcurrentDocumentEditQueue:
    """Apply an independent-session edit after the first progress commit."""

    def __init__(self, queue, application, document_id, replacement_html):
        self.queue = queue
        self.application = application
        self.document_id = document_id
        self.replacement_html = replacement_html
        self.session_expired_after_status = False
        self.edit_applied = False

    def set_status(self, job_id, status_data, input_payload=None):
        result = self.queue.set_status(job_id, status_data, input_payload)
        if (
            not self.edit_applied
            and status_data.get("message") == "Preparing document export..."
        ):
            worker_document = next(
                value
                for value in db.session.identity_map.values()
                if isinstance(value, ProcessedDocument)
                and inspect(value).identity == (self.document_id,)
            )
            self.session_expired_after_status = inspect(worker_document).expired
            with self.application.app_context():
                concurrent_document = db.session.get(
                    ProcessedDocument, self.document_id
                )
                concurrent_document.output_html = self.replacement_html
                concurrent_document.metadata_record.title = "Later metadata title"
                concurrent_document.metadata_record.contributors = {
                    "authors": ["Later author"]
                }
                db.session.commit()
            self.edit_applied = True
        return result

    def __getattr__(self, name):
        return getattr(self.queue, name)


@pytest.mark.parametrize("export_format", FORMATS)
def test_five_formats_write_private_artifact_and_reassemble_chunks(
    app, monkeypatch, export_format
):
    with app.app_context():
        document = _document()
        original_html = document.output_html
        original_settings = dict(document.settings)
        revision = _revision(original_html)
        queue = TaskQueue(app.instance_path, "doc_processor")
        service, _context = _service(app, queue)
        started = service.export_document(
            document.id,
            export_format=export_format,
            idempotency_key=f"format-{export_format}",
            expected_revision=revision,
        )
        operation_id = started["operation_id"]
        claim, delivery = queue.queue.claim(lease_seconds=30)
        payload = delivery["payload"]

        if export_format in {"epub", "docx", "pdf"}:
            _patch_binary_renderer(monkeypatch, export_format)
            expected_bytes = FORMAT_BYTES[export_format]
        else:
            routes = importlib.import_module(
                "iarf_assistant.addons.doc_processor.routes"
            )
            rendered_text, _media_type = routes._render_text_document_export(
                document, export_format
            )
            expected_bytes = rendered_text.encode("utf-8")
        result = worker.mcp_export_document(payload, queue, operation_id)
        queue.set_status(operation_id, {"status": "completed", "result": result})
        assert queue.queue.ack(claim) is True

        path = Path(result["output_path"])
        assert path.is_file()
        assert path.read_bytes() == expected_bytes
        assert path.parent == (
            Path(app.instance_path) / "doc_processor_exports" / operation_id
        )
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert result["byte_length"] == len(expected_bytes)
        assert result["sha256"] == hashlib.sha256(expected_bytes).hexdigest()
        assert result["revision"] == revision
        assert result["format"] == export_format
        assert result["mimetype"] == MIMETYPES[export_format]
        assert result["download_name"] == f"synthetic_book.{export_format}"

        # The document may be renamed after export; the queue result is the
        # durable filename witness for this operation.
        document.filename = "renamed after export.pdf"
        db.session.commit()
        metadata = service.export_document_get(document.id, operation_id)
        assert metadata["ready"] is True
        assert metadata["filename"] == f"synthetic_book.{export_format}"
        assert metadata["sha256"] == result["sha256"]
        assert "content_base64" not in metadata

        pieces = []
        offset = 0
        while True:
            chunk = service.export_document_get(
                document.id,
                operation_id,
                include_content=True,
                offset=offset,
                limit=7,
                expected_sha256=result["sha256"],
            )
            part = base64.b64decode(chunk["content_base64"], validate=True)
            assert hashlib.sha256(part).hexdigest() == chunk["chunk_sha256"]
            assert chunk["offset"] == offset
            assert chunk["next_offset"] == offset + len(part)
            pieces.append(part)
            offset = chunk["next_offset"]
            if chunk["eof"]:
                break
        assembled = b"".join(pieces)
        assert assembled == expected_bytes
        assert hashlib.sha256(assembled).hexdigest() == result["sha256"]
        assert document.output_html == original_html
        assert document.settings == original_settings
        if export_format != "epub":
            queue.set_status(
                operation_id,
                {"result": {**result, "quality": {}}},
            )
            _error_code(
                service.export_document_get,
                document.id,
                operation_id,
                code="artifact_invalid",
            )
        queue.close()


def test_export_document_failure_keeps_default_output_cleanup(
    app, monkeypatch, tmp_path
):
    replacement = b"replacement left by a failing converter"
    with app.app_context():
        document = _document()
        output_path = tmp_path / "default-cleanup.epub"
        output_path.write_bytes(b"reserved output")
        _patch_replacing_epub_failure(monkeypatch, replacement)
        task_queue = SimpleNamespace(set_status=lambda *_args, **_kwargs: None)

        with pytest.raises(RuntimeError, match="synthetic converter failure"):
            worker.export_document(
                {"document_id": document.id, "format": "epub"},
                task_queue,
                "default-cleanup",
                output_path=output_path,
            )

        assert not output_path.exists()


def test_direct_publishing_worker_rejects_source_revision_change_before_render(
    app, monkeypatch, tmp_path
):
    with app.app_context():
        document = _document()
        packager_module = importlib.import_module(
            "iarf_assistant.addons.doc_processor.epub_packager"
        )

        class UnexpectedPackager:
            def __init__(self, *_args, **_kwargs):
                raise AssertionError("stale source must fail before rendering")

        monkeypatch.setattr(packager_module, "EPUBPackager", UnexpectedPackager)
        output_path = tmp_path / "stale-direct-export.epub"
        with pytest.raises(
            RuntimeError, match=r"The extraction changed before export rendering\."
        ):
            worker.export_document(
                {
                    "document_id": document.id,
                    "format": "epub",
                    "epub_options": normalize_epub_options({}),
                    "expected_revision": "0" * 64,
                },
                SimpleNamespace(set_status=lambda *_args, **_kwargs: None),
                "stale-direct-export",
                output_path=output_path,
            )
        assert not output_path.exists()


def test_direct_publishing_worker_binds_and_rechecks_review_witness(
    app, monkeypatch, tmp_path
):
    with app.app_context():
        document = _document()
        revision = _revision(document.output_html)
        from iarf_assistant.addons.doc_processor.metadata_service import (
            get_canonical_document_metadata,
        )

        options = normalize_epub_options({"direction": "ltr"})
        cover = {"mode": "generated"}
        review_hash = direct_epub_review_sha256(
            revision,
            get_canonical_document_metadata(document),
            options,
            cover=cover,
            image_mode="webp",
        )
        _patch_binary_renderer(monkeypatch, "epub")
        task_queue = SimpleNamespace(set_status=lambda *_args, **_kwargs: None)
        output_path = tmp_path / "reviewed-direct.epub"
        result = worker.export_document(
            {
                "document_id": document.id,
                "format": "epub",
                "epub_options": options,
                "expected_revision": revision,
                "epub_cover": cover,
                "epub_image_mode": "webp",
                "epub_review_hash": review_hash,
            },
            task_queue,
            "reviewed-direct",
            output_path=output_path,
        )
        assert result["epub_review_hash"] == review_hash
        assert result["quality"]["input_sha256"] == revision
        assert result["quality"]["options_sha256"] == epub_value_sha256(options)

        document.settings = {
            **document.settings,
            "document_metadata": {"title": "Changed after review"},
        }
        db.session.commit()
        stale_statuses = []
        stale_queue = SimpleNamespace(
            set_status=lambda _job, value: stale_statuses.append(value)
        )
        stale_path = tmp_path / "stale-direct.epub"
        with pytest.raises(RuntimeError, match="choices changed"):
            worker.export_document(
                {
                    "document_id": document.id,
                    "format": "epub",
                    "epub_options": options,
                    "expected_revision": revision,
                    "epub_cover": cover,
                    "epub_image_mode": "webp",
                    "epub_review_hash": review_hash,
                },
                stale_queue,
                "stale-direct",
                output_path=stale_path,
            )
        assert stale_statuses == [{"error_code": "epub-review-stale"}]
        assert not stale_path.exists()


def test_mcp_review_witness_is_carried_and_verified_in_ready_receipt(app, monkeypatch):
    with app.app_context():
        document = _document()
        revision = _revision(document.output_html)
        queue = TaskQueue(app.instance_path, "doc_processor")
        service, _context = _service(app, queue)
        from iarf_assistant.addons.doc_processor.metadata_service import (
            get_canonical_document_metadata,
        )

        options = normalize_epub_options({"direction": "ltr"})
        cover = {"mode": "generated"}
        review_hash = direct_epub_review_sha256(
            revision,
            get_canonical_document_metadata(document),
            options,
            cover=cover,
            image_mode="auto",
        )
        started = service.export_document(
            document.id,
            idempotency_key="review-witness-ready",
            expected_revision=revision,
            epub_options=options,
            epub_cover=cover,
            epub_review_hash=review_hash,
            epub_image_mode="auto",
        )
        operation_id = started["operation_id"]
        _patch_binary_renderer(monkeypatch, "epub")
        claim, delivery = queue.queue.claim(lease_seconds=30)
        result = worker.mcp_export_document(delivery["payload"], queue, operation_id)
        queue.set_status(operation_id, {"status": "completed", "result": result})
        assert queue.queue.ack(claim) is True

        ready = service.export_document_get(document.id, operation_id)
        assert ready["ready"] is True
        assert ready["epub_review_hash"] == review_hash
        assert ready["quality"]["artifact_sha256"] == result["sha256"]
        assert ready["quality"]["input_sha256"] == revision
        assert ready["quality"]["options_sha256"] == epub_value_sha256(options)
        queue.set_status(
            operation_id,
            {"result": {**result, "epub_review_hash": "0" * 64}},
        )
        _error_code(
            service.export_document_get,
            document.id,
            operation_id,
            code="artifact_invalid",
        )
        queue.close()


def test_windows_mcp_leaves_replaced_stage_for_identity_guarded_cleanup(
    app, monkeypatch
):
    replacement = b"replacement left by a failing converter"

    class IdentityGuardedWindowsDirectory:
        def __init__(self, instance_path, operation_id):
            self.path = Path(instance_path) / "doc_processor_exports" / operation_id
            self.path.mkdir(parents=True)
            self._reservations = {}
            self.remove_called = False
            self.removal_refused = False
            self.closed = False

        def reserve_stage(self, export_format, _final_name):
            stage_name = ".stage-0123456789abcdef0123456789abcdef." + export_format
            stage_path = self.path / stage_name
            stage_path.write_bytes(b"reserved output")
            witness_path = self.path / f".witness-{stage_name[7:]}"
            os.link(stage_path, witness_path)
            info = witness_path.stat()
            self._reservations[stage_name] = (
                (info.st_dev, info.st_ino),
                witness_path,
            )
            return stage_name, stage_path

        def publish(self, *_args):
            raise AssertionError("a failing converter must not publish")

        def remove_stage(self, stage_name):
            self.remove_called = True
            stage_path = self.path / stage_name
            info = stage_path.stat()
            expected_identity, _witness_path = self._reservations[stage_name]
            if (info.st_dev, info.st_ino) != expected_identity:
                self.removal_refused = True
                raise OSError("stage identity changed")
            stage_path.unlink()

        def close(self):
            if self.closed:
                return
            self.closed = True
            for _identity, witness_path in self._reservations.values():
                witness_path.unlink(missing_ok=True)

    with app.app_context():
        document = _document()
        queue = TaskQueue(app.instance_path, "doc_processor")
        service, _context = _service(app, queue)
        started = service.export_document(
            document.id,
            export_format="epub",
            idempotency_key="failed-windows-cleanup",
            expected_revision=_revision(document.output_html),
        )
        _claim, delivery = queue.queue.claim(lease_seconds=30)
        operation_id = started["operation_id"]
        directory = IdentityGuardedWindowsDirectory(app.instance_path, operation_id)
        monkeypatch.setattr(
            worker, "WindowsArtifactDirectory", IdentityGuardedWindowsDirectory
        )
        monkeypatch.setattr(
            worker,
            "_prepare_mcp_export_directory",
            lambda _job_id: directory,
        )
        _patch_replacing_epub_failure(monkeypatch, replacement)

        try:
            with pytest.raises(RuntimeError, match="synthetic converter failure"):
                worker.mcp_export_document(delivery["payload"], queue, operation_id)

            assert directory.remove_called is True
            assert directory.removal_refused is True
            assert directory.closed is True
            stage_path = directory.path / ".stage-0123456789abcdef0123456789abcdef.epub"
            assert stage_path.read_bytes() == replacement
        finally:
            queue.close()


@pytest.mark.parametrize("export_format", FORMATS)
def test_worker_renders_checked_revision_after_progress_commit_and_concurrent_edit(
    app, monkeypatch, export_format
):
    checked_html = "<main><p>CHECKED-REVISION-TEXT</p></main>"
    concurrent_html = "<main><p>CONCURRENT-REVISION-TEXT</p></main>"
    with app.app_context():
        document = _document(html=checked_html)
        document.metadata_record = DocumentMetadata(
            document_id=document.id,
            title="Checked metadata title",
            contributors={"authors": ["Checked author"]},
        )
        db.session.commit()
        expected_revision = _revision(document.output_html)
        payload = {
            "contract": CONTRACT,
            "user_id": document.user_id,
            "document_id": document.id,
            "format": export_format,
            "expected_revision": expected_revision,
            "start_h2_on_new_page": False,
        }
        operation_id = mcp_exports._operation_id(
            document.user_id, document.id, f"snapshot-{export_format}"
        )
        queue = TaskQueue(app.instance_path, "doc_processor")
        editing_queue = ConcurrentDocumentEditQueue(
            queue, app, document.id, concurrent_html
        )
        captured_html = []
        captured_metadata = []

        if export_format in {"epub", "docx", "pdf"}:
            _patch_binary_renderer(
                monkeypatch,
                export_format,
                captured_html=captured_html,
                captured_metadata=captured_metadata,
            )
        else:
            routes = importlib.import_module(
                "iarf_assistant.addons.doc_processor.routes"
            )
            from iarf_assistant.addons.doc_processor.metadata_service import (
                get_canonical_document_metadata,
            )

            render_text = routes._render_text_document_export

            def capture_text(document_snapshot, requested_format):
                captured_html.append(document_snapshot.output_html)
                captured_metadata.append(
                    get_canonical_document_metadata(document_snapshot)
                )
                return render_text(document_snapshot, requested_format)

            monkeypatch.setattr(routes, "_render_text_document_export", capture_text)

        result = worker.mcp_export_document(payload, editing_queue, operation_id)

        assert editing_queue.edit_applied is True
        assert editing_queue.session_expired_after_status is True
        assert len(captured_html) == 1
        assert "CHECKED-REVISION-TEXT" in captured_html[0]
        assert "CONCURRENT-REVISION-TEXT" not in captured_html[0]
        assert len(captured_metadata) == 1
        assert captured_metadata[0]["title"] == "Checked metadata title"
        if export_format != "docx":
            assert captured_metadata[0]["contributors"]["authors"] == ["Checked author"]
        assert result["revision"] == expected_revision
        assert result["document_id"] == document.id
        assert result["user_id"] == document.user_id

        with app.app_context():
            persisted = db.session.get(ProcessedDocument, document.id)
            assert persisted.output_html == concurrent_html
            assert persisted.metadata_record.title == "Later metadata title"
            assert persisted.metadata_record.contributors == {
                "authors": ["Later author"]
            }
        assert Path(result["output_path"]).is_file()
        queue.close()


def test_publishing_epub_validates_and_projects_quality_receipt(app, monkeypatch):
    with app.app_context():
        document = _document()
        revision = _revision(document.output_html)
        options = normalize_epub_options({"direction": "rtl"})
        queue = TaskQueue(app.instance_path, "doc_processor")
        service, _context = _service(app, queue)
        started = service.export_document(
            document.id,
            idempotency_key="publishing-receipt",
            expected_revision=revision,
            epub_options=options,
        )
        operation_id = started["operation_id"]
        claim, delivery = queue.queue.claim(lease_seconds=30)
        validation_calls = []
        report = {}

        packager_module = importlib.import_module(
            "iarf_assistant.addons.doc_processor.epub_packager"
        )
        validation_module = importlib.import_module(
            "iarf_assistant.addons.doc_processor.epub_validation"
        )
        from iarf_assistant.addons.doc_processor.epub_validation import (
            EpubValidationResult,
        )

        class ReportingPackager:
            def __init__(self, target_document, output, **kwargs):
                self.document = target_document
                self.output = Path(output)
                self.options = kwargs.get("options")

            def build(self):
                self.output.write_bytes(b"synthetic publishing EPUB")

            def quality_report(
                self, *, structure, epubcheck, epubcheck_version, ace, ace_version
            ):
                assert validation_calls
                artifact_bytes = self.output.read_bytes()
                report.update(
                    {
                        "schema": "epub-quality-v1",
                        "profile": self.options["profile"],
                        # Deliberately report the packager's derived input;
                        # the worker must bind the final receipt to the request.
                        "input_sha256": "0" * 64,
                        "options_sha256": epub_value_sha256(self.options),
                        "artifact_sha256": hashlib.sha256(artifact_bytes).hexdigest(),
                        "content_sha256": hashlib.sha256(b"publication").hexdigest(),
                        "checks": {
                            "structure": structure,
                            "epubcheck": epubcheck,
                            "ace": ace,
                            "manual": "not_reviewed",
                        },
                        "counts": {
                            "notes": 0,
                            "references": 0,
                            "backlinks": 0,
                            "headings": 1,
                            "images": 0,
                            "tables": 0,
                            "pages": 0,
                        },
                        "issues": [],
                        "issue_count": 0,
                        "epubcheck_version": epubcheck_version,
                        "ace_version": ace_version,
                    }
                )
                return dict(report)

        def validate(_path, **kwargs):
            validation_calls.append(kwargs)
            return EpubValidationResult(True, None, "not_run")

        monkeypatch.setattr(packager_module, "EPUBPackager", ReportingPackager)
        monkeypatch.setattr(validation_module, "validate_epub_file", validate)
        result = worker.mcp_export_document(delivery["payload"], queue, operation_id)
        assert validation_calls
        assert validation_calls[0]["config"] is not None
        assert report["checks"]["structure"] == "passed"
        assert report["checks"]["epubcheck"] == "not_run"
        assert report["options_sha256"] == epub_value_sha256(options)
        assert report["input_sha256"] == "0" * 64
        assert result["quality"]["input_sha256"] == revision
        assert result["quality"]["options_sha256"] == epub_value_sha256(options)
        queue.set_status(operation_id, {"status": "completed", "result": result})
        assert queue.queue.ack(claim) is True

        document.output_html = "<main><p>Edited after publishing export.</p></main>"
        db.session.commit()
        projected = service.export_document_get(document.id, operation_id)
        assert projected["quality"] == result["quality"]
        assert "Export body" not in repr(projected["quality"])
        _error_code(
            service.export_document,
            document.id,
            idempotency_key="publishing-receipt",
            expected_revision=revision,
            epub_options={"direction": "ltr"},
            code="idempotency_conflict",
        )

        invalid_results = []
        missing_quality = dict(result)
        missing_quality.pop("quality")
        invalid_results.append(missing_quality)
        invalid_results.extend(
            {
                **result,
                "quality": {**result["quality"], key: value},
            }
            for key, value in (
                ("artifact_sha256", "0" * 64),
                ("input_sha256", "0" * 64),
                ("options_sha256", "0" * 64),
                ("profile", "other-profile"),
            )
        )
        for invalid_result in invalid_results:
            queue.set_status(operation_id, {"result": invalid_result})
            _error_code(
                service.export_document_get,
                document.id,
                operation_id,
                code="artifact_invalid",
            )

        queue.set_status(
            operation_id,
            {
                "result": result,
                "epub_options": normalize_epub_options({"direction": "ltr"}),
            },
        )
        _error_code(
            service.export_document_get,
            document.id,
            operation_id,
            code="artifact_invalid",
        )
        queue.close()


def test_admission_is_owner_revision_and_option_fenced(app):
    with app.app_context():
        document = _document()
        revision = _revision(document.output_html)
        queue = TaskQueue(app.instance_path, "doc_processor")
        service, context = _service(app, queue)
        before = (document.status, document.output_html, dict(document.settings))

        started = service.export_document(
            document.id,
            idempotency_key="admit-once",
            expected_revision=revision,
        )
        assert started["status"] == "queued"
        assert started["ready"] is False
        assert len(started["operation_id"]) == 64
        assert context.worker_pool_calls == [("worker.py", 1)]
        claim, delivery = queue.queue.claim(lease_seconds=30)
        payload = delivery["payload"]
        assert payload == {
            "contract": CONTRACT,
            "user_id": 1,
            "document_id": document.id,
            "format": "epub",
            "expected_revision": revision,
            "start_h2_on_new_page": False,
            "epub_options": normalize_epub_options({}),
            "epub_image_mode": "jpeg",
        }
        assert "admit-once" not in repr(payload)
        assert "request_hash" not in payload
        assert queue.queue.ack(claim) is True
        metadata = queue.get_status(started["operation_id"])
        assert metadata["contract"] == CONTRACT
        assert metadata["request_hash"]
        assert metadata["revision"] == revision
        assert metadata["epub_options"] == normalize_epub_options({})
        assert metadata["request_hash"] == mcp_exports._canonical_hash(
            mcp_exports._request_identity(
                user_id=document.user_id,
                document_id=document.id,
                export_format="epub",
                expected_revision=revision,
                start_h2_on_new_page=False,
                epub_options=normalize_epub_options({}),
                epub_image_mode="jpeg",
            )
        )
        assert (document.status, document.output_html, document.settings) == before

        _error_code(
            _service(app, queue, owner=2)[0].export_document,
            document.id,
            idempotency_key="foreign",
            expected_revision=revision,
            code="invalid_export_request",
        )
        _error_code(
            service.export_document,
            document.id,
            idempotency_key="stale",
            expected_revision="0" * 64,
            code="revision_conflict",
        )
        _error_code(
            service.export_document,
            document.id,
            export_format="docx",
            idempotency_key="non-epub-options",
            expected_revision=revision,
            epub_options={},
            code="invalid_export_request",
        )
        document.status = "created"
        db.session.commit()
        _error_code(
            service.export_document,
            document.id,
            idempotency_key="not-done",
            expected_revision=revision,
            code="document_not_completed",
        )
        document.status = "completed"
        document.output_html = ""
        db.session.commit()
        empty_revision = _revision("")
        _error_code(
            service.export_document,
            document.id,
            idempotency_key="empty-output",
            expected_revision=empty_revision,
            code="document_not_completed",
        )
        queue.close()


def test_historical_epub_retry_keeps_absent_options_and_original_identity(
    app, monkeypatch
):
    with app.app_context():
        document = _document()
        revision = _revision(document.output_html)
        queue = TaskQueue(app.instance_path, "doc_processor")
        service, _context = _service(app, queue)
        key = "retained-historical-export"
        identity = mcp_exports._request_identity(
            user_id=document.user_id,
            document_id=document.id,
            export_format="epub",
            expected_revision=revision,
            start_h2_on_new_page=False,
        )
        operation_id = mcp_exports._operation_id(document.user_id, document.id, key)
        queue.enqueue_once(
            TASK,
            identity,
            job_id=operation_id,
            status_metadata={
                "contract": CONTRACT,
                "document_id": document.id,
                "format": "epub",
                "revision": revision,
                "request_hash": mcp_exports._canonical_hash(identity),
            },
        )

        retry = service.export_document(
            document.id,
            idempotency_key=key,
            expected_revision=revision,
        )
        assert retry["operation_id"] == operation_id
        assert "epub_options" not in queue.get_status(operation_id)
        claim, delivery = queue.queue.claim(lease_seconds=30)
        assert delivery["payload"] == identity
        assert "epub_options" not in delivery["payload"]
        _patch_binary_renderer(monkeypatch, "epub")
        result = worker.mcp_export_document(delivery["payload"], queue, operation_id)
        queue.set_status(operation_id, {"status": "completed", "result": result})
        assert queue.queue.ack(claim) is True
        document.output_html = "<main><p>Edited after export.</p></main>"
        db.session.commit()
        projected = service.export_document_get(document.id, operation_id)
        assert projected["ready"] is True
        assert projected["revision"] == revision
        assert "quality" not in projected
        queue.set_status(operation_id, {"result": {**result, "quality": {}}})
        _error_code(
            service.export_document_get,
            document.id,
            operation_id,
            code="artifact_invalid",
        )
        _error_code(
            service.export_document,
            document.id,
            idempotency_key=key,
            expected_revision=revision,
            epub_options={"direction": "rtl"},
            code="idempotency_conflict",
        )
        queue.close()


def test_new_mcp_export_defaults_to_jpeg_but_keeps_explicit_auto_and_non_epub_auto(app):
    with app.app_context():
        document = _document()
        revision = _revision(document.output_html)
        queue = TaskQueue(app.instance_path, "doc_processor")
        service, _context = _service(app, queue)

        service.export_document(
            document.id,
            idempotency_key="new-jpeg-default",
            expected_revision=revision,
        )
        claim, delivery = queue.queue.claim(lease_seconds=30)
        assert delivery["payload"]["epub_image_mode"] == "jpeg"
        assert queue.queue.ack(claim) is True

        service.export_document(
            document.id,
            idempotency_key="explicit-auto",
            expected_revision=revision,
            epub_image_mode="auto",
        )
        claim, delivery = queue.queue.claim(lease_seconds=30)
        assert "epub_image_mode" not in delivery["payload"]
        assert queue.queue.ack(claim) is True

        service.export_document(
            document.id,
            export_format="docx",
            idempotency_key="non-epub-default",
            expected_revision=revision,
        )
        claim, delivery = queue.queue.claim(lease_seconds=30)
        assert "epub_image_mode" not in delivery["payload"]
        assert queue.queue.ack(claim) is True
        queue.close()


@pytest.mark.parametrize("mode", ["auto", "jpeg", "webp", None])
def test_mcp_epub_preflight_is_owner_revision_guarded_and_read_only(app, mode):
    with app.app_context():
        document = _document()
        revision = _revision(document.output_html)
        before = (document.status, document.output_html, dict(document.settings))
        queue = TaskQueue(app.instance_path, "doc_processor")
        service, _context = _service(app, queue)

        result = service.epub_preflight(
            document.id,
            **({"epub_image_mode": mode} if mode is not None else {}),
            epub_options={"chapter_level": 1},
            epub_cover={"mode": "generated"},
            expected_revision=revision,
        )
        assert result["revision"] == revision
        assert result["options"] == normalize_epub_options({"chapter_level": 1})
        assert result["quality"]["input_sha256"] == revision
        assert result["quality"]["options_sha256"] == epub_value_sha256(
            normalize_epub_options({"chapter_level": 1})
        )
        from iarf_assistant.addons.doc_processor.metadata_service import (
            get_canonical_document_metadata,
        )

        assert result["review_hash"] == direct_epub_review_sha256(
            revision,
            get_canonical_document_metadata(document),
            normalize_epub_options({"chapter_level": 1}),
            cover={"mode": "generated"},
            image_mode=mode or "jpeg",
        )
        assert result["outline"]["headings"]
        assert (document.status, document.output_html, document.settings) == before
        _error_code(
            _service(app, queue, owner=2)[0].epub_preflight,
            document.id,
            expected_revision=revision,
            code="not_found",
        )
        _error_code(
            service.epub_preflight,
            document.id,
            expected_revision="0" * 64,
            code="revision_conflict",
        )
        with pytest.raises(IndexError):
            queue.queue.claim(lease_seconds=30)
        queue.close()


@pytest.mark.parametrize("mode", ["auto", "jpeg", "webp"])
def test_mcp_epub_review_witness_binds_source_choices_and_retry_identity(app, mode):
    with app.app_context():
        document = _document()
        revision = _revision(document.output_html)
        queue = TaskQueue(app.instance_path, "doc_processor")
        service, _context = _service(app, queue)
        from iarf_assistant.addons.doc_processor.metadata_service import (
            get_canonical_document_metadata,
        )

        options = normalize_epub_options({"direction": "ltr"})
        cover = {"mode": "generated"}
        review_hash = direct_epub_review_sha256(
            revision,
            get_canonical_document_metadata(document),
            options,
            cover=cover,
            image_mode=mode,
        )
        started = service.export_document(
            document.id,
            idempotency_key="review-witness-valid",
            expected_revision=revision,
            epub_options={"direction": "ltr"},
            epub_cover=cover,
            epub_review_hash=review_hash,
            epub_image_mode=mode,
        )
        operation_id = started["operation_id"]
        status = queue.get_status(operation_id)
        assert status["epub_review_hash"] == review_hash
        assert status["epub_options"] == options
        assert status["epub_cover"] == cover
        assert status["request_hash"] == mcp_exports._canonical_hash(
            mcp_exports._request_identity(
                user_id=document.user_id,
                document_id=document.id,
                export_format="epub",
                expected_revision=revision,
                start_h2_on_new_page=False,
                epub_options=options,
                epub_cover=cover,
                epub_review_hash=review_hash,
                epub_image_mode=mode,
            )
        )
        claim, delivery = queue.queue.claim(lease_seconds=30)
        assert delivery["payload"]["epub_review_hash"] == review_hash
        assert delivery["payload"]["epub_options"] == options
        assert delivery["payload"]["epub_cover"] == cover
        assert delivery["payload"].get("epub_image_mode", "auto") == mode
        assert queue.queue.ack(claim) is True

        retry = service.export_document(
            document.id,
            idempotency_key="review-witness-valid",
            expected_revision=revision,
        )
        assert retry["operation_id"] == operation_id
        assert retry["epub_review_hash"] == review_hash
        _error_code(
            service.export_document,
            document.id,
            idempotency_key="review-witness-valid",
            expected_revision=revision,
            epub_image_mode="jpeg" if mode == "auto" else "auto",
            code="idempotency_conflict",
        )

        _error_code(
            service.export_document,
            document.id,
            idempotency_key="review-witness-options-stale",
            expected_revision=revision,
            epub_options={"direction": "rtl"},
            epub_cover=cover,
            epub_review_hash=review_hash,
            code="epub-review-stale",
        )
        _error_code(
            service.export_document,
            document.id,
            idempotency_key="review-witness-cover-stale",
            expected_revision=revision,
            epub_options={"direction": "ltr"},
            epub_cover={"mode": "pdf_page", "page": 3},
            epub_review_hash=review_hash,
            code="epub-review-stale",
        )
        document.settings = {
            **document.settings,
            "document_metadata": {"title": "Changed title"},
        }
        db.session.commit()
        assert _revision(document.output_html) == revision
        _error_code(
            service.export_document,
            document.id,
            idempotency_key="review-witness-metadata-stale",
            expected_revision=revision,
            epub_options={"direction": "ltr"},
            epub_cover=cover,
            epub_review_hash=review_hash,
            code="epub-review-stale",
        )
        queue.close()


def test_mcp_epub_worker_rechecks_review_witness_before_rendering(app, monkeypatch):
    with app.app_context():
        document = _document()
        revision = _revision(document.output_html)
        queue = TaskQueue(app.instance_path, "doc_processor")
        service, _context = _service(app, queue)
        from iarf_assistant.addons.doc_processor.metadata_service import (
            get_canonical_document_metadata,
        )

        options = normalize_epub_options({"direction": "ltr"})
        cover = {"mode": "generated"}
        review_hash = direct_epub_review_sha256(
            revision,
            get_canonical_document_metadata(document),
            options,
            cover=cover,
            image_mode="auto",
        )
        started = service.export_document(
            document.id,
            idempotency_key="review-witness-worker-stale",
            expected_revision=revision,
            epub_options={"direction": "ltr"},
            epub_cover=cover,
            epub_review_hash=review_hash,
            epub_image_mode="auto",
        )
        operation_id = started["operation_id"]
        document.settings = {
            **document.settings,
            "document_metadata": {"title": "Changed before worker"},
        }
        db.session.commit()
        _patch_binary_renderer(monkeypatch, "epub")
        claim, delivery = queue.queue.claim(lease_seconds=30)
        with pytest.raises(RuntimeError, match="choices changed"):
            worker.mcp_export_document(delivery["payload"], queue, operation_id)
        status = queue.get_status(operation_id)
        assert status["error_code"] == "epub-review-stale"
        assert queue.queue.ack(claim) is True
        queue.close()


@pytest.mark.parametrize(
    "arguments",
    [
        {"idempotency_key": "", "expected_revision": "a" * 64},
        {"idempotency_key": "   ", "expected_revision": "a" * 64},
        {"idempotency_key": "x" * 129, "expected_revision": "a" * 64},
        {"idempotency_key": "bad-revision", "expected_revision": "A" * 64},
        {
            "idempotency_key": "bad-format",
            "expected_revision": "a" * 64,
            "export_format": "TXT",
        },
        {
            "idempotency_key": "bad-bool",
            "expected_revision": "a" * 64,
            "start_h2_on_new_page": 1,
        },
        {
            "idempotency_key": "bad-heading-format",
            "expected_revision": "a" * 64,
            "start_h2_on_new_page": True,
            "export_format": "md",
        },
    ],
)
def test_admission_strictly_validates_options(app, arguments):
    with app.app_context():
        document = _document()
        queue = TaskQueue(app.instance_path, "doc_processor")
        service, _context = _service(app, queue)
        options = {
            "document_id": document.id,
            "export_format": arguments.get("export_format", "epub"),
            "idempotency_key": arguments["idempotency_key"],
            "expected_revision": arguments["expected_revision"],
            "start_h2_on_new_page": arguments.get("start_h2_on_new_page", False),
        }
        _error_code(service.export_document, code="invalid_export_request", **options)
        assert queue.get_status("unused")["status"] == "unknown"
        queue.close()


def test_same_key_replays_after_edit_and_queue_reconnect(app):
    with app.app_context():
        document = _document()
        revision = _revision(document.output_html)
        queue = TaskQueue(app.instance_path, "doc_processor")
        service, _context = _service(app, queue)
        first = service.export_document(
            document.id,
            idempotency_key="reconnect-retry",
            expected_revision=revision,
        )
        queue.close()

        document.output_html = HTML + "<p>A later edit.</p>"
        db.session.commit()
        reopened = TaskQueue(app.instance_path, "doc_processor")
        reconnected, context = _service(app, reopened)
        retry = reconnected.export_document(
            document.id,
            idempotency_key="reconnect-retry",
            expected_revision=revision,
        )
        assert retry["operation_id"] == first["operation_id"]
        assert retry["status"] == "queued"
        assert context.worker_pool_calls == [("worker.py", 1)]
        _error_code(
            reconnected.export_document,
            document.id,
            export_format="docx",
            idempotency_key="reconnect-retry",
            expected_revision=revision,
            code="idempotency_conflict",
        )
        claim, delivery = reopened.queue.claim(lease_seconds=30)
        assert delivery["payload"]["document_id"] == document.id
        assert reopened.queue.ack(claim) is True
        with pytest.raises(IndexError):
            reopened.queue.claim(lease_seconds=30)
        reopened.close()


def test_replay_retries_worker_pool_after_initial_launch_failure(app):
    with app.app_context():
        document = _document()
        revision = _revision(document.output_html)
        queue = TaskQueue(app.instance_path, "doc_processor")
        service, context = _service(app, queue, fail_worker_pool=1)
        _error_code(
            service.export_document,
            document.id,
            idempotency_key="launch-retry",
            expected_revision=revision,
            code="export_failed",
        )
        durable_status = queue.get_status(
            mcp_exports._operation_id(1, document.id, "launch-retry")
        )
        assert durable_status["status"] == "queued"
        document.output_html = HTML + "<p>Changed after admission.</p>"
        db.session.commit()

        replay = service.export_document(
            document.id,
            idempotency_key="launch-retry",
            expected_revision=revision,
        )
        assert replay["status"] == "queued"
        assert len(context.worker_pool_calls) == 2
        claim, delivery = queue.queue.claim(lease_seconds=30)
        assert delivery["payload"]["expected_revision"] == revision
        assert queue.queue.ack(claim) is True
        with pytest.raises(IndexError):
            queue.queue.claim(lease_seconds=30)
        queue.close()


class BarrierStatusQueue:
    def __init__(self, queue, barrier):
        self.queue = queue
        self.barrier = barrier
        self._lock = Lock()
        self._first = True

    def get_status(self, job_id):
        status = self.queue.get_status(job_id)
        with self._lock:
            wait = self._first
            self._first = False
        if wait:
            self.barrier.wait(timeout=10)
        return status

    def __getattr__(self, name):
        return getattr(self.queue, name)


def test_independent_service_contexts_race_to_one_durable_enqueue(app):
    with app.app_context():
        document = _document()
        revision = _revision(document.output_html)
    first_queue = TaskQueue(app.instance_path, "doc_processor")
    second_queue = TaskQueue(app.instance_path, "doc_processor")
    barrier = Barrier(2)
    first_service, _ = _service(app, BarrierStatusQueue(first_queue, barrier))
    second_service, _ = _service(app, BarrierStatusQueue(second_queue, barrier))

    def submit(service):
        with app.app_context():
            return service.export_document(
                "doc-export",
                idempotency_key="same-concurrent-key",
                expected_revision=revision,
            )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first, second = list(executor.map(submit, (first_service, second_service)))
        assert first["operation_id"] == second["operation_id"]
        assert first["status"] == second["status"] == "queued"
        claim, delivery = first_queue.queue.claim(lease_seconds=30)
        assert delivery["payload"]["document_id"] == "doc-export"
        assert first_queue.queue.ack(claim) is True
        with pytest.raises(IndexError):
            second_queue.queue.claim(lease_seconds=30)
    finally:
        first_queue.close()
        second_queue.close()


def test_reader_validates_exact_queue_identity_and_sanitizes_failure(app):
    with app.app_context():
        document = _document()
        revision = _revision(document.output_html)
        queue = TaskQueue(app.instance_path, "doc_processor")
        service, _context = _service(app, queue)
        started = service.export_document(
            document.id,
            idempotency_key="status-identity",
            expected_revision=revision,
        )
        operation_id = started["operation_id"]

        queue.set_status(operation_id, {"document_id": "other-document"})
        _error_code(
            service.export_document_get,
            document.id,
            operation_id,
            code="export_not_found",
        )

        queue.set_status(
            operation_id,
            {
                "document_id": document.id,
                "status": "failed",
                "error": "/private/path/settings.json: synthetic failure",
                "message": "raw worker message with path",
            },
        )
        failed = service.export_document_get(document.id, operation_id)
        assert failed["status"] == "failed"
        assert failed["error_code"] == "export_failed"
        assert failed["message"] == "The document export did not complete."
        assert (
            "error" not in failed and "path" not in failed and "settings" not in failed
        )
        queue.close()


def _completed_status(service, queue, document, *, key="reader-artifact"):
    revision = _revision(document.output_html)
    started = service.export_document(
        document.id,
        export_format="md",
        idempotency_key=key,
        expected_revision=revision,
    )
    operation_id = started["operation_id"]
    download_name = "saved_export.md"
    artifact = (
        Path(service.context.application.instance_path)
        / "doc_processor_exports"
        / operation_id
        / download_name
    )
    artifact.parent.mkdir(parents=True, mode=0o700)
    artifact.write_bytes(FORMAT_BYTES["md"])
    artifact.chmod(0o600)
    result = {
        "kind": "file_download",
        "user_id": document.user_id,
        "document_id": document.id,
        "operation_id": operation_id,
        "format": "md",
        "revision": revision,
        "download_name": download_name,
        "mimetype": MIMETYPES["md"],
        "output_path": str(artifact),
        "byte_length": len(FORMAT_BYTES["md"]),
        "sha256": hashlib.sha256(FORMAT_BYTES["md"]).hexdigest(),
    }
    queue.set_status(operation_id, {"status": "completed", "result": result})
    return operation_id, artifact, result


def test_reader_rejects_bad_result_path_identity_and_size(app):
    with app.app_context():
        document = _document()
        queue = TaskQueue(app.instance_path, "doc_processor")
        service, _context = _service(app, queue)
        operation_id, artifact, result = _completed_status(service, queue, document)

        queue.set_status(
            operation_id,
            {"result": {**result, "document_id": "other-document"}},
        )
        _error_code(
            service.export_document_get,
            document.id,
            operation_id,
            code="artifact_invalid",
        )
        queue.set_status(
            operation_id,
            {
                "result": {
                    **result,
                    "output_path": str(artifact.parent.parent / "outside.md"),
                }
            },
        )
        _error_code(
            service.export_document_get,
            document.id,
            operation_id,
            code="artifact_invalid",
        )
        queue.set_status(operation_id, {"result": result})
        artifact.write_bytes(b"short")
        _error_code(
            service.export_document_get,
            document.id,
            operation_id,
            code="artifact_invalid",
        )
        _error_code(
            service.export_document_get,
            document.id,
            operation_id,
            expected_sha256="0" * 64,
            code="checksum_mismatch",
        )
        queue.close()


@pytest.mark.parametrize("component", ["export_root", "operation_dir", "artifact"])
def test_reader_rejects_symlinked_export_components(app, tmp_path, component):
    with app.app_context():
        document = _document()
        queue = TaskQueue(app.instance_path, "doc_processor")
        service, _context = _service(app, queue)
        operation_id, artifact, result = _completed_status(
            service, queue, document, key=f"symlink-{component}"
        )
        outside = tmp_path / f"outside-{component}"
        outside.mkdir()
        external_file = outside / artifact.name
        external_file.write_bytes(FORMAT_BYTES["md"])
        artifact.unlink()

        if component == "artifact":
            artifact.symlink_to(external_file)
        elif component == "operation_dir":
            artifact.parent.rmdir()
            artifact.parent.symlink_to(outside, target_is_directory=True)
            result["output_path"] = str(artifact)
        else:
            artifact.parent.rmdir()
            export_root = Path(app.instance_path) / "doc_processor_exports"
            export_root.rmdir()
            export_root.symlink_to(outside, target_is_directory=True)
            result["output_path"] = str(artifact)
        queue.set_status(operation_id, {"result": result})

        _error_code(
            service.export_document_get,
            document.id,
            operation_id,
            include_content=True,
            code="artifact_unavailable",
        )
        assert external_file.read_bytes() == FORMAT_BYTES["md"]
        queue.close()


@pytest.mark.parametrize(
    "options",
    [
        {"include_content": 1},
        {"offset": True},
        {"offset": -1},
        {"limit": 0},
        {"limit": 65_537},
        {"expected_sha256": "A" * 64},
    ],
)
def test_read_options_are_strict(app, options):
    with app.app_context():
        document = _document()
        queue = TaskQueue(app.instance_path, "doc_processor")
        service, _context = _service(app, queue)
        _error_code(
            service.export_document_get,
            document.id,
            "f" * 64,
            code="invalid_export_request",
            **options,
        )
        _error_code(
            service.export_document_get,
            document.id,
            "../unsafe",
            code="invalid_export_request",
        )
        queue.close()


def test_worker_rejects_stale_revision_and_symlink_root_before_staging(app, tmp_path):
    with app.app_context():
        document = _document()
        original_revision = _revision(document.output_html)
        payload = {
            "contract": CONTRACT,
            "user_id": document.user_id,
            "document_id": document.id,
            "format": "md",
            "expected_revision": original_revision,
            "start_h2_on_new_page": False,
        }
        operation_id = mcp_exports._operation_id(1, document.id, "stale-worker")
        document.output_html = HTML + "<p>new extraction</p>"
        db.session.commit()
        queue = TaskQueue(app.instance_path, "doc_processor")
        with pytest.raises(RuntimeError, match="changed before export"):
            worker.mcp_export_document(payload, queue, operation_id)
        assert not (Path(app.instance_path) / "doc_processor_exports").exists()

        document.output_html = HTML
        db.session.commit()
        outside = tmp_path / "outside-worker-root"
        outside.mkdir()
        export_root = Path(app.instance_path) / "doc_processor_exports"
        export_root.symlink_to(outside, target_is_directory=True)
        with pytest.raises(OSError):
            worker.mcp_export_document(payload, queue, operation_id)
        assert list(outside.iterdir()) == []
        queue.close()


def test_duplicate_publish_is_atomic_and_never_overwrites_winner(tmp_path):
    operation_dir = tmp_path / "private"
    operation_dir.mkdir(mode=0o700)
    first_stage = operation_dir / ".stage-first.md"
    second_stage = operation_dir / ".stage-second.md"
    first_stage.write_bytes(b"winner-one")
    second_stage.write_bytes(b"winner-two")
    descriptor = os.open(operation_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    barrier = Barrier(2)

    def publish(stage_name):
        barrier.wait(timeout=5)
        try:
            return worker._publish_mcp_export(descriptor, stage_name, "export.md")
        except FileExistsError:
            return None

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(
                executor.map(publish, (first_stage.name, second_stage.name))
            )
        assert sum(outcome is not None for outcome in outcomes) == 1
        published_bytes = (operation_dir / "export.md").read_bytes()
        assert published_bytes in {b"winner-one", b"winner-two"}
        assert (first_stage.exists()) != (second_stage.exists())
        assert (operation_dir / "export.md").read_bytes() == published_bytes
    finally:
        os.close(descriptor)
