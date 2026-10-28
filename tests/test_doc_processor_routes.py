import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from bs4 import BeautifulSoup
from content_storage_support import bind_content_policy
from flask import Flask

from carlina.core.epub_export import normalize_epub_options
from iarf_assistant.addons.doc_processor import routes
from iarf_assistant.addons.doc_processor.epub_quality import direct_epub_review_sha256
from iarf_assistant.addons.doc_processor.mcp_review import _revision
from iarf_assistant.addons.doc_processor.prompts import DEFAULT_PDF_SYSTEM_PROMPT


class FakeQueue:
    def __init__(self, status=None):
        self.enqueued = []
        self.status = status or {}
        self.status_updates = []

    def enqueue(self, task_name, payload):
        self.enqueued.append((task_name, payload))
        return "job-123"

    def get_status(self, job_id):
        return self.status

    def set_status(self, job_id, status):
        self.status = {**self.status, **status}
        self.status_updates.append((job_id, status))

    def request_cancel(self, job_id, requested_by=None, reason=None):
        self.status = {
            **self.status,
            "status": "cancelling",
            "cancel_requested": True,
            "cancel_requested_by": requested_by,
            "message": reason,
        }
        return self.status


class DocProcessorRouteWorkerBoundaryTests(unittest.TestCase):
    def _app(self, root_path=None):
        app = Flask(__name__, root_path=root_path) if root_path else Flask(__name__)
        bind_content_policy(app)
        app.secret_key = "test"
        app.register_blueprint(routes.blueprint)
        return app

    def test_convert_document_route_enqueues_worker_job(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = Flask(__name__, root_path=temp_dir)
            bind_content_policy(app)
            app.secret_key = "test"
            app.register_blueprint(routes.blueprint)
            fake_queue = FakeQueue()

            with app.test_request_context(
                "/addons/doc_processor/api/convert",
                method="POST",
                data={
                    "file": (io.BytesIO(b"hello"), "sample.txt"),
                    "engine": "marker",
                    "force_ocr": "true",
                    "page_range": "1-2",
                },
            ):
                with patch.object(routes, "current_user", SimpleNamespace(id=7)):
                    with (
                        patch.object(
                            routes, "_get_task_queue", return_value=fake_queue
                        ),
                        patch.object(routes, "_ensure_workers"),
                    ):
                        response, status_code = routes.convert_document.__wrapped__()

            self.assertEqual(status_code, 202)
            self.assertEqual(response.get_json()["job_id"], "job-123")
            self.assertEqual(len(fake_queue.enqueued), 1)

            task_name, payload = fake_queue.enqueued[0]
            self.assertEqual(task_name, "convert_document")
            self.assertEqual(payload["filename"], "sample.txt")
            self.assertEqual(payload["user_id"], 7)
            self.assertEqual(payload["engine_id"], "marker")
            self.assertEqual(payload["options"]["force_ocr"], True)
            self.assertEqual(payload["options"]["page_range"], "1-2")
            self.assertTrue(Path(payload["file_path"]).is_file())

    def test_crop_mode_defaults_to_left_right(self):
        self.assertEqual(routes._normalize_crop_mode(None), "left_right")
        self.assertEqual(routes._normalize_crop_mode(""), "left_right")
        self.assertEqual(routes._normalize_crop_mode("single"), "single")

    def test_epub_export_route_enqueues_worker_job(self):
        app = self._app()
        fake_queue = FakeQueue()
        document = SimpleNamespace(
            id="doc-1",
            filename="Example Book.pdf",
            settings={},
            output_html="<p>Hello</p>",
            output_markdown=None,
        )

        with app.test_request_context(
            "/addons/doc_processor/api/documents/doc-1/export?format=epub"
        ):
            with patch.object(routes, "current_user", SimpleNamespace(id=7)):
                with patch.object(routes, "_get_user_document", return_value=document):
                    with (
                        patch.object(
                            routes, "_get_task_queue", return_value=fake_queue
                        ),
                        patch.object(routes, "_ensure_workers"),
                    ):
                        response, status_code = routes.export_document.__wrapped__(
                            "doc-1"
                        )

        payload = response.get_json()
        self.assertEqual(status_code, 202)
        self.assertEqual(payload["job_id"], "job-123")
        self.assertEqual(
            payload["download_url"], "/addons/doc_processor/api/jobs/job-123/download"
        )
        self.assertEqual(
            fake_queue.enqueued,
            [
                (
                    "export_document",
                    {
                        "document_id": "doc-1",
                        "user_id": 7,
                        "format": "epub",
                        "epub_image_mode": "jpeg",
                        "include_source_toc": False,
                        "epub_options": normalize_epub_options({}),
                        "expected_revision": _revision("<p>Hello</p>"),
                    },
                )
            ],
        )

    def test_epub_export_post_passes_reviewed_options_and_revision(self):
        app = self._app()
        fake_queue = FakeQueue()
        html = "<main><h1>Example</h1></main>"
        document = SimpleNamespace(
            id="doc-1",
            filename="Example Book.pdf",
            settings={},
            output_html=html,
            output_markdown=None,
        )
        options = {"direction": "rtl"}
        revision = _revision(html)

        with (
            app.test_request_context(
                "/addons/doc_processor/api/documents/doc-1/export?format=epub",
                method="POST",
                json={"epub_options": options, "expected_revision": revision},
            ),
            patch.object(routes, "current_user", SimpleNamespace(id=7)),
            patch.object(routes, "_get_user_document", return_value=document),
            patch.object(routes, "_get_task_queue", return_value=fake_queue),
            patch.object(routes, "_ensure_workers"),
        ):
            _response, status_code = routes.export_document.__wrapped__("doc-1")

        self.assertEqual(status_code, 202)
        payload = fake_queue.enqueued[0][1]
        self.assertEqual(payload["epub_options"], normalize_epub_options(options))
        self.assertEqual(payload["expected_revision"], revision)
        self.assertEqual(payload["epub_image_mode"], "jpeg")

    def test_epub_export_post_binds_full_review_witness(self):
        app = self._app()
        fake_queue = FakeQueue()
        html = "<main><h1>Example</h1></main>"
        document = SimpleNamespace(
            id="doc-1",
            user_id=7,
            filename="Example Book.pdf",
            file_path=None,
            status="completed",
            settings={"document_metadata": {"title": "Example Book"}},
            output_html=html,
            output_markdown=None,
            metadata_record=None,
        )
        options = normalize_epub_options({"direction": "rtl"})
        cover = {"mode": "generated"}
        revision = _revision(html)
        review_hash = direct_epub_review_sha256(
            revision,
            routes.get_canonical_document_metadata(document),
            options,
            cover=cover,
            image_mode="webp",
        )

        with (
            app.test_request_context(
                "/addons/doc_processor/api/documents/doc-1/export?format=epub&epub_image_mode=webp&epub_cover_mode=generated",
                method="POST",
                json={
                    "epub_options": {"direction": "rtl"},
                    "expected_revision": revision,
                    "epub_review_hash": review_hash,
                },
            ),
            patch.object(routes, "current_user", SimpleNamespace(id=7)),
            patch.object(routes, "_get_user_document", return_value=document),
            patch.object(routes, "_get_task_queue", return_value=fake_queue),
            patch.object(routes, "_ensure_workers"),
        ):
            _response, status_code = routes.export_document.__wrapped__("doc-1")

        self.assertEqual(status_code, 202)
        payload = fake_queue.enqueued[0][1]
        self.assertEqual(payload["epub_review_hash"], review_hash)
        self.assertEqual(payload["epub_cover"], cover)
        self.assertEqual(payload["epub_image_mode"], "webp")

    def test_epub_export_post_rejects_stale_review_witness_for_same_html(self):
        app = self._app()
        fake_queue = FakeQueue()
        html = "<main><h1>Example</h1></main>"
        document = SimpleNamespace(
            id="doc-1",
            user_id=7,
            filename="Example Book.pdf",
            file_path=None,
            status="completed",
            settings={"document_metadata": {"title": "Current title"}},
            output_html=html,
            output_markdown=None,
            metadata_record=None,
        )
        revision = _revision(html)
        old_metadata = {"title": "Prior title"}
        review_hash = direct_epub_review_sha256(
            revision,
            old_metadata,
            normalize_epub_options({"direction": "ltr"}),
            cover={"mode": "generated"},
            image_mode="auto",
        )

        with (
            app.test_request_context(
                "/addons/doc_processor/api/documents/doc-1/export?format=epub&epub_cover_mode=existing&epub_image_mode=auto",
                method="POST",
                json={
                    "epub_options": {"direction": "rtl"},
                    "expected_revision": revision,
                    "epub_review_hash": review_hash,
                },
            ),
            patch.object(routes, "current_user", SimpleNamespace(id=7)),
            patch.object(routes, "_get_user_document", return_value=document),
        ):
            response, status_code = routes.export_document.__wrapped__("doc-1")

        self.assertEqual(status_code, 409)
        self.assertEqual(response.get_json()["error_code"], "epub-review-stale")
        self.assertEqual(fake_queue.enqueued, [])

    def test_epub_export_get_keeps_json_query_compatibility(self):
        app = self._app()
        fake_queue = FakeQueue()
        html = "<main><h1>Example</h1></main>"
        document = SimpleNamespace(
            id="doc-1",
            filename="Example Book.pdf",
            settings={},
            output_html=html,
            output_markdown=None,
        )
        revision = _revision(html)

        with (
            app.test_request_context(
                "/addons/doc_processor/api/documents/doc-1/export?format=epub&epub_options=%7B%22direction%22%3A%22rtl%22%7D&expected_revision="
                + revision
            ),
            patch.object(routes, "current_user", SimpleNamespace(id=7)),
            patch.object(routes, "_get_user_document", return_value=document),
            patch.object(routes, "_get_task_queue", return_value=fake_queue),
            patch.object(routes, "_ensure_workers"),
        ):
            _response, status_code = routes.export_document.__wrapped__("doc-1")

        self.assertEqual(status_code, 202)
        payload = fake_queue.enqueued[0][1]
        self.assertEqual(
            payload["epub_options"], normalize_epub_options({"direction": "rtl"})
        )
        self.assertEqual(payload["expected_revision"], revision)

    def test_epub_export_rejects_non_epub_options_and_stale_post_revision(self):
        app = self._app()
        fake_queue = FakeQueue()
        document = SimpleNamespace(
            id="doc-1",
            filename="Example Book.pdf",
            settings={},
            output_html="<p>Hello</p>",
            output_markdown=None,
        )

        with (
            app.test_request_context(
                "/addons/doc_processor/api/documents/doc-1/export?format=docx&epub_options=%7B%7D"
            ),
            patch.object(routes, "current_user", SimpleNamespace(id=7)),
            patch.object(routes, "_get_user_document", return_value=document),
        ):
            _response, status_code = routes.export_document.__wrapped__("doc-1")
        self.assertEqual(status_code, 400)
        self.assertEqual(fake_queue.enqueued, [])

        with (
            app.test_request_context(
                "/addons/doc_processor/api/documents/doc-1/export?format=epub",
                method="POST",
                json={"expected_revision": "0" * 64},
            ),
            patch.object(routes, "current_user", SimpleNamespace(id=7)),
            patch.object(routes, "_get_user_document", return_value=document),
        ):
            _response, status_code = routes.export_document.__wrapped__("doc-1")
        self.assertEqual(status_code, 409)
        self.assertEqual(fake_queue.enqueued, [])

    def test_epub_preflight_is_revision_bound_and_returns_bounded_report(self):
        app = self._app()
        html = '<main><h1 id="chapter">Chapter</h1><p>Body.</p></main>'
        document = SimpleNamespace(
            id="doc-1",
            user_id=7,
            filename="Example Book.pdf",
            file_path=None,
            status="completed",
            settings={"document_metadata": {"title": "Example Book", "language": "en"}},
            output_html=html,
            output_markdown=None,
            metadata_record=None,
        )
        revision = _revision(html)
        with (
            app.test_request_context(
                "/addons/doc_processor/api/documents/doc-1/epub/preflight",
                method="POST",
                json={
                    "epub_options": {"chapter_level": 2},
                    "expected_revision": revision,
                    "epub_cover": {"mode": "generated"},
                },
            ),
            patch.object(routes, "current_user", SimpleNamespace(id=7)),
            patch.object(routes, "_get_user_document", return_value=document),
            patch(
                "iarf_assistant.addons.doc_processor.epub_editions.current_source_revision",
                return_value="d" * 64,
            ),
        ):
            response = routes.preflight_document_epub.__wrapped__("doc-1")

        payload = response.get_json()
        self.assertEqual(payload["revision"], revision)
        self.assertEqual(
            payload["options"], normalize_epub_options({"chapter_level": 2})
        )
        self.assertEqual(payload["suggested_chapter_level"], 2)
        self.assertEqual(payload["outline"]["headings"][0]["id"], "chapter")
        self.assertEqual(payload["quality"]["input_sha256"], revision)
        self.assertEqual(
            payload["review_hash"],
            direct_epub_review_sha256(
                revision,
                routes.get_canonical_document_metadata(document),
                normalize_epub_options({"chapter_level": 2}),
                cover={"mode": "generated"},
                image_mode="jpeg",
            ),
        )
        self.assertNotIn(html, repr(payload["quality"]))
        self.assertTrue(payload["outline"]["images"][0]["is_selected_cover"])
        # The browser uses null for its explicit "keep existing cover" choice.
        with (
            app.test_request_context(
                "/addons/doc_processor/api/documents/doc-1/epub/preflight",
                method="POST",
                json={"epub_options": {}, "epub_cover": None},
            ),
            patch.object(routes, "current_user", SimpleNamespace(id=7)),
            patch.object(routes, "_get_user_document", return_value=document),
            patch("iarf_assistant.addons.doc_processor.epub_editions.current_source_revision", return_value="d" * 64),
        ):
            unchanged = routes.preflight_document_epub.__wrapped__("doc-1")
        self.assertEqual(unchanged.status_code, 200)
        unchanged_payload = unchanged.get_json()
        self.assertEqual(unchanged_payload["outline"]["images"], [])
        self.assertEqual(
            unchanged_payload["review_hash"],
            direct_epub_review_sha256(
                revision,
                routes.get_canonical_document_metadata(document),
                normalize_epub_options({}),
                image_mode="jpeg",
            ),
        )

    def test_epub_export_route_passes_webp_image_mode(self):
        app = self._app()
        fake_queue = FakeQueue()
        document = SimpleNamespace(
            id="doc-1",
            filename="Example Book.pdf",
            settings={},
            output_html="<p>Hello</p>",
            output_markdown=None,
        )

        with app.test_request_context(
            "/addons/doc_processor/api/documents/doc-1/export?format=epub&epub_image_mode=webp"
        ):
            with patch.object(routes, "current_user", SimpleNamespace(id=7)):
                with patch.object(routes, "_get_user_document", return_value=document):
                    with (
                        patch.object(
                            routes, "_get_task_queue", return_value=fake_queue
                        ),
                        patch.object(routes, "_ensure_workers"),
                    ):
                        response, status_code = routes.export_document.__wrapped__(
                            "doc-1"
                        )

        self.assertEqual(status_code, 202)
        self.assertTrue(response.get_json()["success"])
        self.assertEqual(fake_queue.enqueued[0][1]["epub_image_mode"], "webp")

    def test_epub_export_route_preserves_explicit_auto_image_mode(self):
        app = self._app()
        fake_queue = FakeQueue()
        document = SimpleNamespace(
            id="doc-1",
            filename="Example Book.pdf",
            settings={},
            output_html="<p>Hello</p>",
            output_markdown=None,
        )

        with app.test_request_context(
            "/addons/doc_processor/api/documents/doc-1/export?format=epub&epub_image_mode=auto"
        ):
            with patch.object(routes, "current_user", SimpleNamespace(id=7)):
                with patch.object(routes, "_get_user_document", return_value=document):
                    with (
                        patch.object(routes, "_get_task_queue", return_value=fake_queue),
                        patch.object(routes, "_ensure_workers"),
                    ):
                        response, status_code = routes.export_document.__wrapped__("doc-1")

        self.assertEqual(status_code, 202)
        self.assertTrue(response.get_json()["success"])
        self.assertEqual(fake_queue.enqueued[0][1]["epub_image_mode"], "auto")

    def test_epub_image_mode_query_default_and_legacy_aliases(self):
        app = self._app()
        cases = (
            ("", "jpeg"),
            ("&epub_image_mode=", "auto"),
            ("&epub_image_mode=false", "auto"),
            ("&epub_image_mode=preserve", "auto"),
            ("&epub_image_mode=0", "auto"),
        )
        for query, expected in cases:
            with self.subTest(query=query):
                with app.test_request_context(
                    f"/addons/doc_processor/api/documents/doc-1/export?format=epub{query}"
                ):
                    self.assertEqual(routes._requested_epub_image_mode(), expected)

    def test_epub_export_route_ignores_legacy_visible_source_toc_choice(self):
        app = self._app()
        fake_queue = FakeQueue()
        document = SimpleNamespace(
            id="doc-1",
            filename="Example Book.pdf",
            settings={},
            output_html="<p>Hello</p>",
            output_markdown=None,
        )

        with app.test_request_context(
            "/addons/doc_processor/api/documents/doc-1/export?format=epub&include_source_toc=true"
        ):
            with patch.object(routes, "current_user", SimpleNamespace(id=7)):
                with patch.object(routes, "_get_user_document", return_value=document):
                    with (
                        patch.object(
                            routes, "_get_task_queue", return_value=fake_queue
                        ),
                        patch.object(routes, "_ensure_workers"),
                    ):
                        response, status_code = routes.export_document.__wrapped__(
                            "doc-1"
                        )

        self.assertEqual(status_code, 202)
        self.assertTrue(response.get_json()["success"])
        self.assertIs(fake_queue.enqueued[0][1]["include_source_toc"], False)

    def test_docx_export_route_does_not_send_epub_presentation_options(self):
        app = self._app()
        fake_queue = FakeQueue()
        document = SimpleNamespace(
            id="doc-1",
            filename="Example Book.pdf",
            settings={},
            output_html="<p>Hello</p>",
            output_markdown=None,
        )

        with app.test_request_context(
            "/addons/doc_processor/api/documents/doc-1/export?format=docx"
        ):
            with (
                patch.object(routes, "current_user", SimpleNamespace(id=7)),
                patch.object(routes, "_get_user_document", return_value=document),
                patch.object(routes, "_get_task_queue", return_value=fake_queue),
                patch.object(routes, "_ensure_workers"),
            ):
                response, status_code = routes.export_document.__wrapped__("doc-1")

        self.assertEqual(status_code, 202)
        self.assertTrue(response.get_json()["success"])
        self.assertEqual(
            fake_queue.enqueued[0][1],
            {"document_id": "doc-1", "user_id": 7, "format": "docx"},
        )

    def test_job_status_hides_worker_file_paths(self):
        app = self._app()
        fake_queue = FakeQueue(
            {
                "status": "completed",
                "traceback": "private worker traceback",
                "cancel_requested_by": 7,
                "result": {
                    "kind": "file_download",
                    "output_path": "C:/temp/private-output.epub",
                    "download_name": "book.epub",
                    "mimetype": "application/epub+zip",
                    "user_id": 7,
                },
            }
        )

        with app.test_request_context("/addons/doc_processor/api/jobs/job-123"):
            with (
                patch.object(routes, "current_user", SimpleNamespace(id=7)),
                patch.object(routes, "_get_task_queue", return_value=fake_queue),
            ):
                response = routes.get_job_status.__wrapped__("job-123")

        payload = response.get_json()
        self.assertEqual(payload["status"], "completed")
        self.assertNotIn("traceback", payload)
        self.assertNotIn("cancel_requested_by", payload)
        self.assertNotIn("output_path", payload["result"])
        self.assertNotIn("user_id", payload["result"])
        self.assertEqual(
            payload["result"]["download_url"],
            "/addons/doc_processor/api/jobs/job-123/download",
        )

    def test_job_status_is_hidden_from_other_users(self):
        app = self._app()
        fake_queue = FakeQueue({"status": "processing", "user_id": 8})

        with app.test_request_context("/addons/doc_processor/api/jobs/job-123"):
            with (
                patch.object(routes, "current_user", SimpleNamespace(id=7)),
                patch.object(routes, "_get_task_queue", return_value=fake_queue),
            ):
                response, status_code = routes.get_job_status.__wrapped__("job-123")

        self.assertEqual(status_code, 404)
        self.assertEqual(response.get_json()["error"], "Job was not found.")

    def test_enqueue_job_overrides_spoofed_request_owner(self):
        app = self._app()
        fake_queue = FakeQueue()

        with app.test_request_context(
            "/addons/doc_processor/api/convert", method="POST"
        ):
            with (
                patch.object(routes, "current_user", SimpleNamespace(id=7)),
                patch.object(routes, "_get_task_queue", return_value=fake_queue),
                patch.object(routes, "_ensure_workers"),
            ):
                job_id = routes._enqueue_job("convert_document", {"user_id": 999})

        self.assertEqual(job_id, "job-123")
        self.assertEqual(fake_queue.enqueued[0][1]["user_id"], 7)

    def test_prepare_html_for_epub_keeps_explicit_footnote_href_mapping(self):
        app = self._app()
        html = (
            "<p>Text<sup><a id='fnref-1' class='footnote-ref' role='doc-noteref' "
            "epub:type='noteref' href='#fn-1'>1</a></sup></p>"
            "<section id='footnotes' role='doc-endnotes' epub:type='endnotes'>"
            "<div class='footnote' id='fn-1' role='doc-endnote' epub:type='footnote'>Note.</div>"
            "</section>"
        )
        document = SimpleNamespace(file_path="")

        with app.app_context():
            prepared_html, _resource_paths = routes._prepare_html_for_epub_export(
                html,
                "Test",
                document,
                {},
            )

        soup = BeautifulSoup(prepared_html, "html.parser")
        ref = soup.find("a", class_="footnote-ref")
        self.assertIsNotNone(ref)
        self.assertEqual(ref.get("href"), "#fn-1")
        self.assertEqual(ref.get("epub:type"), "noteref")
        self.assertNotIn("role", ref.attrs)

    def test_prepare_html_for_epub_does_not_fabricate_image_alt_text(self):
        app = self._app()
        document = SimpleNamespace(file_path="")

        with app.app_context():
            prepared_html, _resource_paths = routes._prepare_html_for_epub_export(
                "<figure><img src='figure.png'></figure>",
                "Test",
                document,
                {},
            )

        image = BeautifulSoup(prepared_html, "html.parser").find("img")
        self.assertIsNotNone(image)
        self.assertNotIn("alt", image.attrs)

    def test_pause_document_processing_marks_active_job_for_cooperative_pause(self):
        app = self._app()
        fake_queue = FakeQueue({"status": "processing"})
        document = SimpleNamespace(
            id="doc-1",
            status="processing",
            settings={"active_job_id": "job-123", "engine_id": "llm_vision"},
        )

        with app.test_request_context(
            "/addons/doc_processor/api/documents/doc-1/control",
            method="POST",
            json={"action": "pause"},
        ):
            with (
                patch.object(routes, "current_user", SimpleNamespace(id=7)),
                patch.object(routes, "_get_user_document", return_value=document),
                patch.object(routes, "_get_task_queue", return_value=fake_queue),
                patch.object(routes, "flag_modified"),
                patch.object(routes.db.session, "commit"),
            ):
                response = routes.control_document_processing.__wrapped__("doc-1")

        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["action"], "pause")
        self.assertTrue(fake_queue.status["pause_requested"])
        self.assertEqual(fake_queue.status["status"], "pausing")
        self.assertIn("Pausing", document.settings["progress_msg"])

    def test_stop_document_processing_requests_queue_cancellation(self):
        app = self._app()
        fake_queue = FakeQueue({"status": "processing"})
        document = SimpleNamespace(
            id="doc-1",
            status="processing",
            settings={"active_job_id": "job-123", "engine_id": "llm_vision"},
        )

        with app.test_request_context(
            "/addons/doc_processor/api/documents/doc-1/control",
            method="POST",
            json={"action": "stop"},
        ):
            with (
                patch.object(routes, "current_user", SimpleNamespace(id=7)),
                patch.object(routes, "_get_user_document", return_value=document),
                patch.object(routes, "_get_task_queue", return_value=fake_queue),
                patch.object(routes, "flag_modified"),
                patch.object(routes.db.session, "commit"),
            ):
                response = routes.control_document_processing.__wrapped__("doc-1")

        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["action"], "stop")
        self.assertTrue(fake_queue.status["cancel_requested"])
        self.assertEqual(fake_queue.status["cancel_requested_by"], 7)
        self.assertIn("Stopping", document.settings["progress_msg"])

    def test_html_export_embeds_local_images_and_adds_portable_styles(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "source.pdf"
            image_path = Path(temp_dir) / "figure.png"
            source_path.write_bytes(b"pdf")
            image_path.write_bytes(b"\x89PNG\r\n\x1a\nimage-data")
            document = SimpleNamespace(
                id="doc-1",
                filename="Example Book.pdf",
                file_path=str(source_path),
                settings={"document_metadata": {"title": "Example"}},
                output_html=(
                    "<main><nav class='source-toc'><h2>Contents</h2>"
                    "<div>Chapter One ... 1</div></nav><h1>Chapter One</h1>"
                    "<p>Hello</p><img src='figure.png'><script>bad()</script></main>"
                ),
                output_markdown=None,
            )
            app = self._app()

            with app.test_request_context(
                "/addons/doc_processor/api/documents/doc-1/export?format=html"
            ):
                with patch.object(routes, "_get_user_document", return_value=document):
                    response = routes.export_document.__wrapped__("doc-1")

            exported = response.get_data(as_text=True)
            self.assertTrue(exported.lstrip().lower().startswith("<!doctype html>"))
            self.assertIn("<style>", exported)
            self.assertIn("data:image/png;base64,", exported)
            self.assertIn("<main>", exported)
            self.assertIn("Chapter One", exported)
            self.assertNotIn("source-toc", exported)
            self.assertNotIn("Chapter One ... 1", exported)
            self.assertNotIn("<script", exported)

    def test_source_download_uses_configured_upload_root_when_package_root_is_split(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            package_root = Path(temp_dir) / "installed-package"
            uploads_root = Path(temp_dir) / "live-data" / "uploads"
            package_root.mkdir()
            source_path = (
                uploads_root / "7" / "doc_processor_docs" / "doc-1" / "source.txt"
            )
            source_path.parent.mkdir(parents=True)
            source_path.write_text("source bytes", encoding="utf-8")
            app = self._app(str(package_root))
            app.config["CARLINA_UPLOADS_DIR"] = str(uploads_root)
            document = SimpleNamespace(
                id="doc-1",
                filename="source.txt",
                file_path="/uploads/7/doc_processor_docs/doc-1/source.txt",
            )

            with app.test_request_context(
                "/addons/doc_processor/api/documents/doc-1/source-file?disposition=attachment"
            ):
                with (
                    patch.object(routes, "current_user", SimpleNamespace(id=7)),
                    patch.object(routes.ProcessedDocument, "query") as document_query,
                ):
                    document_query.filter_by.return_value.first_or_404.return_value = (
                        document
                    )
                    response = routes.get_source_file.__wrapped__("doc-1")

            assert response.status_code == 200
            response.direct_passthrough = False
            assert response.get_data() == b"source bytes"

    def test_inline_pdf_source_allows_same_origin_preview_framing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            package_root = Path(temp_dir) / "installed-package"
            uploads_root = Path(temp_dir) / "live-data" / "uploads"
            package_root.mkdir()
            source_path = (
                uploads_root / "7" / "doc_processor_docs" / "doc-1" / "source.pdf"
            )
            source_path.parent.mkdir(parents=True)
            source_path.write_bytes(b"%PDF-1.4\n% synthetic\n")
            app = self._app(str(package_root))
            app.config["CARLINA_UPLOADS_DIR"] = str(uploads_root)
            document = SimpleNamespace(
                id="doc-1",
                filename="source.pdf",
                file_path="/uploads/7/doc_processor_docs/doc-1/source.pdf",
            )

            with app.test_request_context(
                "/addons/doc_processor/api/documents/doc-1/source-file?disposition=inline"
            ):
                with (
                    patch.object(routes, "current_user", SimpleNamespace(id=7)),
                    patch.object(routes.ProcessedDocument, "query") as document_query,
                ):
                    document_query.filter_by.return_value.first_or_404.return_value = (
                        document
                    )
                    response = routes.get_source_file.__wrapped__("doc-1")

            assert response.status_code == 200
            assert response.headers["Content-Type"].startswith("application/pdf")
            assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
            assert (
                response.headers["Content-Security-Policy"] == "frame-ancestors 'self'"
            )

    def test_debug_output_folder_uses_configured_upload_root_when_package_root_is_split(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            package_root = Path(temp_dir) / "installed-package"
            uploads_root = Path(temp_dir) / "live-data" / "uploads"
            package_root.mkdir()
            document_dir = uploads_root / "7" / "doc_processor_docs" / "doc-1"
            debug_dir = document_dir / "debug_pipeline"
            debug_dir.mkdir(parents=True)
            document = SimpleNamespace(
                id="doc-1",
                user_id=7,
                file_path="/uploads/7/doc_processor_docs/doc-1/source.txt",
            )
            app = self._app(str(package_root))
            app.config["CARLINA_UPLOADS_DIR"] = str(uploads_root)

            with app.app_context():
                assert (
                    routes._get_document_output_folder(document)
                    == document_dir.resolve()
                )

    def test_managed_document_cleanup_target_uses_configured_upload_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            package_root = Path(temp_dir) / "installed-package"
            uploads_root = Path(temp_dir) / "live-data" / "uploads"
            package_root.mkdir()
            document_dir = uploads_root / "7" / "doc_processor_docs" / "doc-1"
            source_path = document_dir / "source.txt"
            source_path.parent.mkdir(parents=True)
            source_path.write_text("source", encoding="utf-8")
            document = SimpleNamespace(
                id="doc-1",
                user_id=7,
                file_path="/uploads/7/doc_processor_docs/doc-1/source.txt",
            )
            app = self._app(str(package_root))
            app.config["CARLINA_UPLOADS_DIR"] = str(uploads_root)

            with app.app_context():
                assert (
                    routes._managed_document_storage_dir(document)
                    == document_dir.resolve()
                )

    def test_managed_output_never_uses_another_users_canonical_subtree(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            package_root = Path(temp_dir) / "installed-package"
            uploads_root = Path(temp_dir) / "live-data" / "uploads"
            package_root.mkdir()
            foreign_dir = uploads_root / "8" / "doc_processor_docs" / "foreign-doc"
            foreign_dir.mkdir(parents=True)
            document = SimpleNamespace(
                id="doc-owner-7",
                user_id=7,
                file_path="/uploads/8/doc_processor_docs/foreign-doc/source.pdf",
            )
            app = self._app(str(package_root))
            app.config["CARLINA_UPLOADS_DIR"] = str(uploads_root)

            with app.app_context():
                owner_root = (uploads_root / "7" / "doc_processor_docs").resolve()
                assert routes._get_document_output_folder(document) == owner_root
                assert routes._managed_document_storage_dir(document) is None

    def test_preview_sanitizer_returns_body_fragment_without_active_content(self):
        preview = routes._sanitize_document_html(
            "<!DOCTYPE html><html><head><style>bad</style></head>"
            "<body><main><p onclick='bad()'>Safe</p><script>bad()</script></main></body></html>"
        )

        self.assertIn("<main>", preview)
        self.assertIn("<p>Safe</p>", preview)
        self.assertNotIn("<html", preview)
        self.assertNotIn("<script", preview)
        self.assertNotIn("onclick", preview)


class DocProcessorPromptRouteTests(unittest.TestCase):
    def _app(self, root_path=None):
        app = Flask(__name__, root_path=root_path) if root_path else Flask(__name__)
        bind_content_policy(app)
        app.secret_key = "test"
        app.register_blueprint(routes.blueprint)
        return app

    def test_manage_prompts_get_includes_hidden_fragments(self):
        app = self._app()

        with app.test_request_context("/addons/doc_processor/api/prompts"):
            with (
                patch.object(routes, "current_user", SimpleNamespace(id=7)),
                patch.object(
                    routes,
                    "_get_current_user_addon_settings",
                    return_value={
                        "doc_processor": {"prompt_pdf_system": "Custom system prompt"}
                    },
                ),
            ):
                response = routes.manage_prompts.__wrapped__()

        payload = response.get_json()
        field_map = {
            field["id"]: field
            for section in payload["sections"]
            for field in section["fields"]
        }

        self.assertIn("prompt_pdf_toc_guidance", field_map)
        self.assertIn("prompt_pdf_continuation_with_context", field_map)
        self.assertIn("prompt_pdf_example_image", field_map)
        self.assertEqual(
            field_map["prompt_pdf_system"]["override_text"], "Custom system prompt"
        )

    def test_manage_prompts_put_strips_defaults_and_preserves_unrelated_settings(self):
        app = self._app()
        fake_user = SimpleNamespace(
            addon_settings={
                "doc_processor": {
                    "prompt_pdf_system": "Custom system prompt",
                    "keep_existing_setting": "keep-me",
                },
                "other_addon": {"enabled": True},
            }
        )
        fake_session = SimpleNamespace(get=Mock(return_value=fake_user), commit=Mock())
        fake_addon = SimpleNamespace(on_settings_updated=Mock())

        with app.test_request_context(
            "/addons/doc_processor/api/prompts",
            method="PUT",
            json={
                "prompts": {
                    "prompt_pdf_system": DEFAULT_PDF_SYSTEM_PROMPT,
                    "prompt_pdf_toc_guidance": "Custom TOC guidance",
                    "unknown_prompt": "ignored",
                }
            },
        ):
            with (
                patch.object(routes, "current_user", SimpleNamespace(id=7)),
                patch.object(routes.db, "session", fake_session),
                patch.object(routes, "flag_modified"),
                patch.object(routes, "_get_addon", return_value=fake_addon),
            ):
                response = routes.manage_prompts.__wrapped__()

        payload = response.get_json()

        self.assertTrue(payload["success"])
        self.assertNotIn("prompt_pdf_system", fake_user.addon_settings["doc_processor"])
        self.assertEqual(
            fake_user.addon_settings["doc_processor"]["prompt_pdf_toc_guidance"],
            "Custom TOC guidance",
        )
        self.assertEqual(
            fake_user.addon_settings["doc_processor"]["keep_existing_setting"],
            "keep-me",
        )
        self.assertEqual(fake_user.addon_settings["other_addon"], {"enabled": True})
        fake_session.commit.assert_called_once()
        fake_addon.on_settings_updated.assert_called_once()

    def test_preview_prompts_resolves_hidden_overrides_and_placeholder_context(self):
        app = self._app()

        with app.test_request_context(
            "/addons/doc_processor/api/prompts/preview",
            method="POST",
            json={
                "prompts": {
                    "prompt_pdf_toc_guidance": "Custom TOC guidance",
                },
                "processing_settings": {
                    "append_previous": True,
                    "extract_images": True,
                    "metadata_prepass": True,
                    "removehf": True,
                },
                "assume_previous_context": True,
            },
        ):
            with (
                patch.object(routes, "current_user", SimpleNamespace(id=7)),
                patch.object(
                    routes, "_get_current_user_addon_settings", return_value={}
                ),
            ):
                response = routes.preview_prompts.__wrapped__()

        payload = response.get_json()

        self.assertTrue(payload["extraction"]["placeholder_context_used"])
        self.assertIn("Custom TOC guidance", payload["extraction"]["user_prompt"])
        self.assertTrue(
            any(
                fragment["id"] == "prompt_pdf_toc_guidance"
                and fragment["source"] == "override"
                for fragment in payload["extraction"]["active_fragments"]
            )
        )
        self.assertEqual(
            payload["metadata"]["instruction_prefix"],
            "Document metadata extraction instructions:",
        )
        self.assertEqual(
            payload["boundary"]["instruction_prefix"],
            "Boundary merge classification instructions:",
        )


class DocProcessorMetadataHelpersTests(unittest.TestCase):
    def test_crop_review_prepass_runs_whenever_metadata_prepass_is_enabled(self):
        self.assertTrue(
            routes._requires_crop_review_prepass(
                {
                    "pdf_options": {
                        "metadata_prepass": True,
                        "metadata_delete_flagged_pages": False,
                    }
                }
            )
        )
        self.assertFalse(
            routes._requires_crop_review_prepass(
                {
                    "pdf_options": {
                        "metadata_prepass": False,
                        "metadata_delete_flagged_pages": True,
                    }
                }
            )
        )

    def test_pdf_option_normalization_migrates_legacy_delete_setting(self):
        normalized = routes._normalize_pdf_processing_options(
            {
                "metadata_exclude_flagged_pages": True,
            }
        )

        self.assertTrue(normalized["metadata_delete_flagged_pages"])
        self.assertNotIn("metadata_exclude_flagged_pages", normalized)

    def test_pdf_option_normalization_defaults_to_omitting_source_toc(self):
        self.assertEqual(
            routes._normalize_pdf_processing_options({})["toc_handling"], "omit"
        )
        self.assertEqual(
            routes._normalize_pdf_processing_options({"toc_handling": "preserve"})[
                "toc_handling"
            ],
            "omit",
        )
        self.assertEqual(
            routes._normalize_pdf_processing_options({"toc_handling": "unexpected"})[
                "toc_handling"
            ],
            "omit",
        )

    def test_metadata_flagged_page_indexes_respects_source_pages_when_present(self):
        metadata = {
            "source_pages": [1, 2],
            "pages_to_remove": [
                {
                    "page": 1,
                    "reason": "blank",
                    "safe_to_remove": True,
                    "confidence": 0.99,
                },
                {
                    "page": 4,
                    "reason": "outside pre-pass",
                    "safe_to_remove": True,
                    "confidence": 0.99,
                },
            ],
        }

        flagged = routes._metadata_flagged_page_indexes(metadata, total_pages=10)
        self.assertEqual(flagged, {0})

    def test_metadata_flagged_page_indexes_allows_any_page_without_source_pages(self):
        metadata = {
            "pages_to_remove": [
                {
                    "page": 1,
                    "reason": "blank",
                    "safe_to_remove": True,
                    "confidence": 0.99,
                },
                {
                    "page": 4,
                    "reason": "ad page",
                    "safe_to_remove": True,
                    "confidence": 0.99,
                },
            ],
        }

        flagged = routes._metadata_flagged_page_indexes(metadata, total_pages=10)
        self.assertEqual(flagged, {0, 3})

    def test_page_selection_keeps_physical_side_parity_after_exclusion(self):
        _allowed, included, page_sides, excluded = routes._build_page_selection(
            total_pages=5,
            page_range="",
            excluded_pages=[1],
            first_page_side="right",
        )

        self.assertEqual(included, [0, 2, 3, 4])
        self.assertEqual(excluded, {1})
        self.assertEqual(page_sides[0], "right")
        self.assertEqual(page_sides[1], "left")
        self.assertEqual(page_sides[2], "right")

    def test_crop_review_metadata_suggestions_accepts_worker_progress_callback(self):
        document = SimpleNamespace(
            settings={"pdf_options": {"metadata_prepass": False}}
        )

        suggested = routes._ensure_crop_review_metadata_suggestions(
            document,
            object(),
            total_pages=1,
            progress_callback=lambda _pct, _msg: None,
        )

        self.assertEqual(suggested, set())

    def test_passive_crop_review_prepass_does_not_resolve_model_router(self):
        document_metadata = {
            "pages_to_remove": [
                {
                    "page": 1,
                    "reason": "blank",
                    "safe_to_remove": True,
                    "confidence": 0.99,
                }
            ]
        }
        document = SimpleNamespace(
            file_path="/uploads/1/example.pdf",
            user_id=1,
            settings={
                "pdf_options": {
                    "metadata_prepass": True,
                    "metadata_delete_flagged_pages": True,
                }
            },
        )
        parser = Mock()
        parser._normalize_pdf_options.return_value = {
            "page_range": "",
            "excluded_pages": [],
        }
        parser.settings = {"document_metadata": document_metadata}
        session = Mock()

        with (
            patch(
                "iarf_assistant.addons.doc_processor.parsers.pdf_parser.PdfParser",
                return_value=parser,
            ),
            patch("iarf_assistant.llm_service.LLMService"),
            patch.object(
                routes,
                "get_canonical_document_metadata",
                side_effect=[None, document_metadata],
            ),
            patch.object(routes, "sync_document_metadata"),
            patch.object(routes, "flag_modified"),
            patch.object(routes.db, "session", session),
            patch(
                "iarf_assistant.core.mcp_completion.passive_execution_context",
                return_value=SimpleNamespace(),
            ),
            patch(
                "iarf_assistant.core.model_router.ModelRouter.get_model_for_tier",
                side_effect=AssertionError(
                    "passive crop review must not query ModelRouter"
                ),
            ),
        ):
            suggested = routes._ensure_crop_review_metadata_suggestions(
                document,
                object(),
                total_pages=1,
            )

        self.assertEqual(suggested, {0})
        parser._extract_document_metadata.assert_called_once()
        self.assertEqual(
            parser._extract_document_metadata.call_args.args[4], "document.metadata"
        )

    def test_density_stack_preview_includes_suggested_crop_box(self):
        mask = np.zeros((100, 120), dtype=np.float32)
        mask[20:80, 30:90] = 1.0

        with (
            patch.object(
                routes, "_representative_stack_base_size", return_value=(120, 100)
            ),
            patch.object(
                routes, "_get_crop_preview_mask", return_value=(mask, (120, 100))
            ),
        ):
            preview = routes._build_density_stack_preview(
                object(), [0, 1, 2], excluded_page_indices=[1]
            )

        self.assertIsNotNone(preview)
        self.assertEqual(preview["used_page_count"], 2)
        self.assertEqual(preview["excluded_page_count"], 1)
        self.assertNotIn(2, preview["used_pages"])
        box = preview["suggested_crop_box"]
        self.assertIsNotNone(box)
        self.assertLess(box[0], 0.3)
        self.assertLess(box[1], 0.25)
        self.assertGreater(box[2], 0.7)
        self.assertGreater(box[3], 0.75)

    def test_density_stack_preview_resamples_when_sampled_pages_are_excluded(self):
        mask = np.zeros((20, 24), dtype=np.float32)
        mask[5:15, 6:18] = 1.0

        with (
            patch.object(
                routes,
                "_sample_stack_page_indices",
                side_effect=lambda pages: [pages[0]],
            ),
            patch.object(
                routes, "_representative_stack_base_size", return_value=(24, 20)
            ),
            patch.object(
                routes, "_get_crop_preview_mask", return_value=(mask, (24, 20))
            ),
        ):
            preview = routes._build_density_stack_preview(
                object(), [0, 1, 2], excluded_page_indices=[0]
            )

        self.assertIsNotNone(preview)
        self.assertEqual(preview["used_page_count"], 1)
        self.assertEqual(preview["used_pages"], [2])
        self.assertEqual(preview["excluded_page_count"], 1)

    def test_text_layer_preview_mask_avoids_raster_rendering(self):
        page = SimpleNamespace(
            rect=SimpleNamespace(width=200, height=300),
            get_text=lambda _kind: [(20, 30, 180, 240, "body text", 0, 0)],
        )

        with patch.object(routes, "_render_pdf_page_luminance") as render_luminance:
            mask, base_size = routes._get_crop_preview_mask([page], 0, 720, (100, 150))

        render_luminance.assert_not_called()
        self.assertEqual(base_size, (100, 150))
        self.assertGreater(float(mask.sum()), 0.0)

    def test_text_layer_preview_mask_preserves_line_gaps(self):
        page = SimpleNamespace(
            rect=SimpleNamespace(width=200, height=200),
            get_text=lambda _kind: [
                (20, 40, 180, 50, "first", 0, 0, 0),
                (20, 120, 180, 130, "second", 0, 1, 0),
            ],
        )

        mask = routes._build_text_layer_support_mask(page, (100, 100))

        self.assertIsNotNone(mask)
        self.assertGreater(float(mask[20:26, :].max()), 0.5)
        self.assertGreater(float(mask[60:66, :].max()), 0.5)
        self.assertLess(float(mask[40:50, :].max()), 0.1)

    def test_large_stack_page_selection_is_sampled(self):
        sampled = routes._sample_stack_page_indices(list(range(500)))

        self.assertLessEqual(len(sampled), routes._stack_preview_page_limit(500))
        self.assertEqual(sampled[0], 0)
        self.assertEqual(sampled[-1], 499)
        self.assertEqual(sampled, sorted(set(sampled)))

    def test_pdf_preview_route_passes_excluded_pages_to_stack_builder(self):
        document = SimpleNamespace(
            id="doc-1",
            user_id=1,
            file_path="source.pdf",
            settings={"pdf_options": {}, "crop_boxes": {"mode": "single"}},
        )

        class FakeDocumentQuery:
            def filter_by(self, **_kwargs):
                return self

            def first_or_404(self):
                return document

        class FakePdfDoc:
            def __len__(self):
                return 3

            def close(self):
                pass

        captured_exclusions = []

        def fake_stack(_doc, page_indices, excluded_page_indices=None):
            captured_exclusions.append(set(excluded_page_indices or []))
            return {
                "image_data": "data:image/png;base64,",
                "used_page_count": len(page_indices),
                "used_page_total": len(page_indices),
            }

        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "source.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            app = Flask(__name__, root_path=temp_dir)
            bind_content_policy(app)
            app.secret_key = "test"
            app.register_blueprint(routes.blueprint)
            with app.test_request_context(
                "/addons/doc_processor/api/documents/doc-1/pdf-preview?excluded_pages=1",
            ):
                with (
                    patch.object(routes, "current_user", SimpleNamespace(id=1)),
                    patch.object(
                        routes,
                        "ProcessedDocument",
                        SimpleNamespace(query=FakeDocumentQuery()),
                    ),
                    patch.object(routes, "_resolve_local_path", return_value=pdf_path),
                    patch.object(routes.fitz, "open", return_value=FakePdfDoc()),
                    patch.object(
                        routes, "_build_density_stack_preview", side_effect=fake_stack
                    ),
                ):
                    response = routes.get_pdf_preview.__wrapped__("doc-1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["success"], True)
        self.assertEqual(captured_exclusions, [{1}])

    def test_pdf_preview_route_returns_empty_reason_for_unavailable_stack(self):
        document = SimpleNamespace(
            id="doc-1",
            user_id=1,
            file_path="source.pdf",
            settings={"pdf_options": {}, "crop_boxes": {"mode": "single"}},
        )

        class FakeDocumentQuery:
            def filter_by(self, **_kwargs):
                return self

            def first_or_404(self):
                return document

        class FakePdfDoc:
            def __len__(self):
                return 2

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "source.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            app = Flask(__name__, root_path=temp_dir)
            bind_content_policy(app)
            app.secret_key = "test"
            app.register_blueprint(routes.blueprint)
            with app.test_request_context(
                "/addons/doc_processor/api/documents/doc-1/pdf-preview"
            ):
                with (
                    patch.object(routes, "current_user", SimpleNamespace(id=1)),
                    patch.object(
                        routes,
                        "ProcessedDocument",
                        SimpleNamespace(query=FakeDocumentQuery()),
                    ),
                    patch.object(routes, "_resolve_local_path", return_value=pdf_path),
                    patch.object(routes.fitz, "open", return_value=FakePdfDoc()),
                    patch.object(
                        routes, "_build_density_stack_preview", return_value=None
                    ),
                ):
                    response = routes.get_pdf_preview.__wrapped__("doc-1")

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["success"], True)
        self.assertEqual(payload["included_page_count"], 2)
        self.assertEqual(
            payload["previews"]["single"]["empty_reason"], "stack_unavailable"
        )
        self.assertEqual(payload["previews"]["single"]["used_page_total"], 2)


class DocProcessorHtmlNormalizationTests(unittest.TestCase):
    def test_empty_paragraphs_removed(self):
        html = "<p>First</p><p></p><p>  </p><p>&nbsp;</p><p>Second</p>"
        result = routes._sanitize_document_html(html)
        soup = BeautifulSoup(result, "html.parser")
        paragraphs = [p.text.strip() for p in soup.find_all("p")]
        self.assertEqual(paragraphs, ["First", "Second"])

    def test_double_breaks_split(self):
        html = "<p>First paragraph part.<br><br>Second paragraph part.</p>"
        result = routes._sanitize_document_html(html)
        soup = BeautifulSoup(result, "html.parser")
        paragraphs = [p.text.strip() for p in soup.find_all("p")]
        self.assertEqual(
            paragraphs, ["First paragraph part.", "Second paragraph part."]
        )

    def test_heading_ids_generated(self):
        html = "<h1>Heading One</h1><h2>Heading Two!</h2>"
        result = routes._sanitize_document_html(html)
        soup = BeautifulSoup(result, "html.parser")

        h1 = soup.find("h1")
        h2 = soup.find("h2")

        self.assertEqual(h1.get("id"), "heading-one")
        self.assertEqual(h2.get("id"), "heading-two")

    def test_footnote_container_cleaned(self):
        html = '<div id="footnotes" epub:type="endnotes" role="doc-endnotes"><div class="footnote" role="doc-endnote" epub:type="footnote">Footnote</div></div>'
        soup = BeautifulSoup(html, "html.parser")
        routes._normalize_footnote_refs_for_epub(soup)

        container = soup.find(id="document-footnotes")
        self.assertIsNotNone(container)
        self.assertFalse(container.has_attr("epub:type"))
        self.assertFalse(container.has_attr("role"))

        fn = soup.find(class_="footnote")
        self.assertIsNotNone(fn)
        self.assertFalse(fn.has_attr("epub:type"))
        self.assertFalse(fn.has_attr("role"))


if __name__ == "__main__":
    unittest.main()
