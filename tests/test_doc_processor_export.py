import base64
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from xml.etree import ElementTree as ET

import pytest
from bs4 import BeautifulSoup
from flask import Flask

from iarf_assistant.addons.doc_processor import docx_notes, routes, worker


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}
PREPARED = (
    '<p><a class="footnote-ref" href="#fn1">1</a>'
    '<a class="footnote-ref" href="#fn2">2</a>'
    '<a class="footnote-ref" href="#fn2">2</a></p>'
)


def _pandoc_fixture() -> bytes:
    document = (
        f'<w:document xmlns:w="{W}" xmlns:retained="urn:retained"><w:body>'
        '<w:bookmarkStart w:id="12" w:name="CarlinaNote13"/>'
        "<w:p>"
        + "".join(
            '<w:r><w:rPr><w:rStyle w:val="FootnoteReference"/></w:rPr>'
            f'<w:footnoteReference w:id="{number}"/></w:r>'
            for number in (9, 10, 11)
        )
        + '</w:p><w:bookmarkEnd w:id="12"/></w:body></w:document>'
    )
    notes = (
        f'<w:footnotes xmlns:w="{W}">'
        + "".join(
            f'<w:footnote w:id="{number}"><w:p><w:r><w:t>{body}</w:t></w:r></w:p></w:footnote>'
            for number, body in (
                (9, "Translator’s note: Added context."),
                (10, "2024 is the date."),
                (11, "2024 is the date."),
            )
        )
        + "</w:footnotes>"
    )
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("word/document.xml", document)
        archive.writestr("word/footnotes.xml", notes)
        archive.writestr("word/media/unchanged.bin", b"unchanged-resource")
    return stream.getvalue()


def test_repeated_note_uses_one_native_footnote_and_a_bookmarked_field():
    result = docx_notes.preserve_repeated_docx_notes(_pandoc_fixture(), PREPARED)
    with zipfile.ZipFile(io.BytesIO(result)) as archive:
        document_bytes = archive.read("word/document.xml")
        document = ET.fromstring(document_bytes)
        notes = ET.fromstring(archive.read("word/footnotes.xml"))
        assert archive.read("word/media/unchanged.bin") == b"unchanged-resource"
    assert b'xmlns:retained="urn:retained"' in document_bytes
    assert [
        ref.get(f"{{{W}}}id") for ref in document.findall(".//w:footnoteReference", NS)
    ] == ["9", "10"]
    field = document.find(".//w:fldSimple", NS)
    assert field is not None
    assert field.get(f"{{{W}}}instr") == " NOTEREF CarlinaNote14 \\h \\f "
    assert field.find(".//w:t", NS).text == "2"
    bookmark = document.find('.//w:bookmarkStart[@w:name="CarlinaNote14"]', NS)
    assert bookmark is not None
    assert (
        document.find(f'.//w:bookmarkEnd[@w:id="{bookmark.get(f"{{{W}}}id")}"]', NS)
        is not None
    )
    assert len(notes) == 2
    assert "".join(notes.itertext()).count("2024 is the date.") == 1
    assert "Translator’s note:" in "".join(notes.itertext())


def test_single_reference_artifact_is_byte_exact_and_shape_mismatch_fails_closed():
    unchanged = b"no archive inspection is needed"
    assert (
        docx_notes.preserve_repeated_docx_notes(
            unchanged, '<p><a class="footnote-ref" href="#fn1">1</a></p>'
        )
        is unchanged
    )
    with pytest.raises(ValueError, match="semantic-export-docx-note-reference-shape"):
        docx_notes.preserve_repeated_docx_notes(
            _pandoc_fixture(), PREPARED + '<a class="footnote-ref" href="#fn2">2</a>'
        )


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


class DocProcessorExportTests(unittest.TestCase):
    def _app(self):
        app = Flask(__name__)
        app.secret_key = "test"
        app.register_blueprint(routes.blueprint)
        return app

    @staticmethod
    def _run_worker(handler, *args):
        app = Flask("doc-processor-worker-test")
        with app.app_context():
            return handler(*args)

    def test_pdf_exports_queue_owner_and_strict_heading_option(self):
        app = self._app()
        document = SimpleNamespace(
            id="doc-pdf", user_id=7, filename="synthetic.html",
            output_html="<h1>Synthetic</h1>", output_markdown=None,
            file_path="/uploads/7/synthetic.html", settings={},
            metadata_record=None,
        )
        project = SimpleNamespace(
            id="project-pdf", user_id=7, name="Synthetic project",
            documents=[SimpleNamespace(document=SimpleNamespace(**vars(document), status="completed"))],
        )
        queue = FakeQueue()
        with (
            app.test_request_context("/addons/doc_processor/api/documents/doc-pdf/export?format=pdf&start_h2_on_new_page=1"),
            patch.object(routes, "current_user", SimpleNamespace(id=7)),
            patch.object(routes, "_get_user_document", return_value=document),
            patch.object(routes, "_get_task_queue", return_value=queue),
            patch.object(routes, "_ensure_workers"),
        ):
            response, status = routes.export_document.__wrapped__("doc-pdf")
        self.assertEqual(status, 202)
        self.assertTrue(response.get_json()["success"])
        self.assertEqual(queue.enqueued[0][1]["format"], "pdf")
        self.assertIs(queue.enqueued[0][1]["start_h2_on_new_page"], True)
        for mode in ("separate", "concatenated"):
            with (
                app.test_request_context(f"/addons/doc_processor/api/projects/project-pdf/export?format=pdf&mode={mode}&start_h2_on_new_page=true"),
                patch.object(routes, "current_user", SimpleNamespace(id=7)),
                patch("iarf_assistant.addons.doc_processor.routes.DocumentProject") as model,
                patch.object(routes, "_get_task_queue", return_value=queue),
                patch.object(routes, "_ensure_workers"),
            ):
                model.query.filter_by.return_value.first_or_404.return_value = project
                response, status = routes.export_project.__wrapped__("project-pdf")
            self.assertEqual(status, 202)
            self.assertEqual(queue.enqueued[-1][1]["format"], "pdf")
            self.assertEqual(queue.enqueued[-1][1]["mode"], mode)
            self.assertIs(queue.enqueued[-1][1]["start_h2_on_new_page"], True)
        with (
            app.test_request_context("/addons/doc_processor/api/documents/doc-pdf/export?format=pdf&start_h2_on_new_page=yes"),
            patch.object(routes, "current_user", SimpleNamespace(id=7)),
            patch.object(routes, "_get_user_document", return_value=document),
        ):
            response, status = routes.export_document.__wrapped__("doc-pdf")
        self.assertEqual(status, 400)

    def test_pdf_worker_receipt_is_owner_scoped_and_missing_runtime_fails(self):
        document = SimpleNamespace(
            id="doc-pdf-worker", user_id=7, filename="synthetic.html",
            output_html="<h1>Synthetic</h1><p>Body.</p>", output_markdown=None,
            file_path="/uploads/7/synthetic.html", settings={},
            metadata_record=None,
        )
        payload = {
            "document_id": document.id, "user_id": 7,
            "format": "pdf", "start_h2_on_new_page": True,
        }
        runner = lambda *args, **kwargs: None
        with (
            patch.object(worker.db.session, "get", return_value=document),
            patch.object(worker, "_get_client", return_value=SimpleNamespace(
                context=SimpleNamespace(run_managed_process=runner),
            )),
            patch(
                "iarf_assistant.addons.doc_processor.print_export.render_pdf_export",
                return_value=b"%PDF-1.7\nsynthetic",
            ) as render,
        ):
            result = self._run_worker(worker.export_document, payload, FakeQueue(), "pdf-job")
        try:
            self.assertEqual(result["mimetype"], "application/pdf")
            self.assertEqual(result["user_id"], 7)
            self.assertEqual(Path(result["output_path"]).read_bytes(), b"%PDF-1.7\nsynthetic")
            self.assertIs(render.call_args.kwargs["start_h2_on_new_page"], True)
            self.assertIs(render.call_args.kwargs["run_managed_process"], runner)
        finally:
            Path(result["output_path"]).unlink(missing_ok=True)
        with (
            patch.object(worker.db.session, "get", return_value=document),
            patch.object(worker, "_get_client", return_value=SimpleNamespace(context=SimpleNamespace())),
            patch(
                "iarf_assistant.addons.doc_processor.print_export.render_pdf_export"
            ) as render,
        ):
            with self.assertRaisesRegex(RuntimeError, "runtime is unavailable"):
                self._run_worker(worker.export_document, payload, FakeQueue(), "pdf-job")
        render.assert_not_called()

    def test_pdf_project_worker_renders_concatenated_and_separate_results(self):
        document = SimpleNamespace(
            id="doc-project-pdf", user_id=7, filename="source.html",
            status="completed", output_html="<h1>Synthetic title</h1><h2>Chapter</h2><p>Body.</p>",
            output_markdown=None, file_path="/uploads/7/source.html",
            settings={"document_metadata": {"title": "Synthetic title"}},
            metadata_record=None,
        )
        project = SimpleNamespace(
            id="project-pdf", name="Synthetic collection", user_id=7,
            documents=[SimpleNamespace(document=document)],
        )
        runner = lambda *args, **kwargs: None
        for mode in ("concatenated", "separate"):
            rendered = []

            def fake_render(html, **kwargs):
                rendered.append((html, kwargs))
                return b"%PDF-1.7\nsynthetic"

            with (
                patch.object(worker.db.session, "get", return_value=project),
                patch.object(worker, "_get_client", return_value=SimpleNamespace(
                    context=SimpleNamespace(run_managed_process=runner),
                )),
                patch(
                    "iarf_assistant.addons.doc_processor.print_export.render_pdf_export",
                    side_effect=fake_render,
                ),
            ):
                result = self._run_worker(
                    worker.export_project,
                    {"project_id": project.id, "user_id": 7, "format": "pdf", "mode": mode},
                    FakeQueue(), f"pdf-project-{mode}",
                )
            try:
                self.assertEqual(result["user_id"], 7)
                self.assertEqual(len(rendered), 1)
                self.assertIs(rendered[0][1]["run_managed_process"], runner)
                if mode == "concatenated":
                    self.assertEqual(result["mimetype"], "application/pdf")
                    self.assertEqual(Path(result["output_path"]).read_bytes(), b"%PDF-1.7\nsynthetic")
                    soup = BeautifulSoup(rendered[0][0], "html.parser")
                    self.assertEqual([h.get_text(strip=True) for h in soup.find_all("h1")], ["Synthetic title"])
                    self.assertEqual(soup.h2.get_text(strip=True), "Chapter")
                else:
                    self.assertEqual(result["mimetype"], "application/zip")
                    with zipfile.ZipFile(result["output_path"]) as archive:
                        self.assertEqual(archive.namelist(), ["source.pdf"])
                        self.assertEqual(archive.read("source.pdf"), b"%PDF-1.7\nsynthetic")
            finally:
                Path(result["output_path"]).unlink(missing_ok=True)

    def test_sync_concatenated_markdown_export(self):
        # 1. Test synchronous concatenated Markdown export and check that headers are demoted.
        app = self._app()
        project_mock = SimpleNamespace(
            id="project-1", name="My Test Project", user_id=7, documents=[]
        )

        doc1 = SimpleNamespace(
            id="doc-1",
            filename="doc1.pdf",
            status="completed",
            output_markdown="# Heading 1\nSome markdown text.\n## Heading 2\nMore text.",
            output_html=None,
            settings={"document_metadata": {"title": "Doc One Title"}},
            file_path="/uploads/doc1.pdf",
        )
        doc2 = SimpleNamespace(
            id="doc-2",
            filename="doc2.pdf",
            status="completed",
            output_markdown="# Heading A\nAnother text.\n### Heading B\nSubtext.",
            output_html=None,
            settings={"document_metadata": {"title": "Doc Two Title"}},
            file_path="/uploads/doc2.pdf",
        )
        # Running document should be filtered out
        doc_running = SimpleNamespace(
            id="doc-run",
            filename="running.pdf",
            status="running",
            output_markdown="# Running title",
            output_html=None,
            settings={},
            file_path="/uploads/running.pdf",
        )

        project_mock.documents = [
            SimpleNamespace(document=doc1),
            SimpleNamespace(document=doc2),
            SimpleNamespace(document=doc_running),
        ]

        with app.test_request_context(
            "/addons/doc_processor/api/projects/project-1/export?format=md&mode=concatenated"
        ):
            with patch.object(routes, "current_user", SimpleNamespace(id=7)):
                with patch(
                    "iarf_assistant.addons.doc_processor.routes.DocumentProject"
                ) as MockDocumentProject:
                    MockDocumentProject.query.filter_by.return_value.first_or_404.return_value = project_mock
                    response = routes.export_project.__wrapped__("project-1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["Content-Type"], "text/markdown; charset=utf-8"
        )
        self.assertIn(
            'attachment; filename="My_Test_Project.md"',
            response.headers["Content-Disposition"],
        )

        content = response.get_data(as_text=True)
        # Verify document titles are prepended as top-level headings
        self.assertIn("# Doc One Title\n", content)
        self.assertIn("# Doc Two Title\n", content)

        # Verify that original headings are demoted by one level
        # Doc 1: # Heading 1 -> ## Heading 1, ## Heading 2 -> ### Heading 2
        self.assertIn("## Heading 1\n", content)
        self.assertIn("### Heading 2\n", content)
        # Doc 2: # Heading A -> ## Heading A, ### Heading B -> #### Heading B
        self.assertIn("## Heading A\n", content)
        self.assertIn("#### Heading B\n", content)

        # Verify running document was NOT included
        self.assertNotIn("Running title", content)

        # Verify separator
        self.assertIn("\n\n---\n\n", content)

    def test_sync_concatenated_html_export(self):
        # 2. Test synchronous concatenated HTML export.
        app = self._app()
        project_mock = SimpleNamespace(
            id="project-1", name="My Test Project", user_id=7, documents=[]
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "source.pdf"
            image_path = Path(temp_dir) / "figure.png"
            source_path.write_bytes(b"pdf")
            image_path.write_bytes(b"\x89PNG\r\n\x1a\nimage-data")

            doc1 = SimpleNamespace(
                id="doc-1",
                filename="doc1.pdf",
                status="completed",
                output_markdown=None,
                output_html="<h1>Heading 1</h1><p>Some HTML.</p><h2>Heading 2</h2><img src='figure.png'>",
                settings={"document_metadata": {"title": "Doc One Title"}},
                file_path=str(source_path),
            )

            project_mock.documents = [SimpleNamespace(document=doc1)]

            with app.test_request_context(
                "/addons/doc_processor/api/projects/project-1/export?format=html&mode=concatenated"
            ):
                with patch.object(routes, "current_user", SimpleNamespace(id=7)):
                    with patch(
                        "iarf_assistant.addons.doc_processor.routes.DocumentProject"
                    ) as MockDocumentProject:
                        MockDocumentProject.query.filter_by.return_value.first_or_404.return_value = project_mock
                        response = routes.export_project.__wrapped__("project-1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Type"], "text/html; charset=utf-8")

        content = response.get_data(as_text=True)
        # Check title and doctype
        self.assertTrue(content.lstrip().lower().startswith("<!doctype html>"))
        # Check that sections are wrapped in <section> with class and data-filename
        self.assertIn(
            '<section class="project-document" data-filename="doc1.pdf">', content
        )
        # Check prepended top-level title
        self.assertIn("<h1>Doc One Title</h1>", content)
        # Check demoted headers (h1 -> h2, h2 -> h3)
        self.assertIn('id="project-doc-1-heading-1">Heading 1</h2>', content)
        self.assertIn('id="project-doc-1-heading-2">Heading 2</h3>', content)
        # Check base64 embedded image
        self.assertIn("data:image/png;base64,", content)

    def test_sync_concatenated_html_renders_repeated_document_diagram_ids(self):
        app = self._app()
        documents = []
        original_html = []
        for index, label in enumerate(("Początek α", "Początek β"), start=1):
            html = (
                '<main><p><a href="#diagram-1">Figure</a> '
                '<a href="#diagram-1--a">Label</a></p>'
                '<figure class="mermaid-diagram-v1" id="diagram-1">'
                '<pre class="mermaid-template"><code>flowchart TD\n'
                'n1["{{a}}"]</code></pre>'
                '<div class="mermaid-labels">'
                f'<p id="diagram-1--a">{label}</p></div>'
                f'<figcaption>Diagram {index}</figcaption></figure></main>'
            )
            original_html.append(html)
            documents.append(
                SimpleNamespace(
                    id=f"diagram-doc-{index}",
                    filename=f"diagram{index}.html",
                    status="completed",
                    output_markdown=None,
                    output_html=html,
                    settings={"document_metadata": {"title": f"Document {index}"}},
                    file_path=f"/uploads/7/diagram{index}.html",
                )
            )
        project = SimpleNamespace(
            id="project-diagrams",
            name="Diagram project",
            user_id=7,
            documents=[SimpleNamespace(document=doc) for doc in documents],
        )
        with (
            app.test_request_context(
                "/addons/doc_processor/api/projects/project-diagrams/export?format=html&mode=concatenated"
            ),
            patch.object(routes, "current_user", SimpleNamespace(id=7)),
            patch("iarf_assistant.addons.doc_processor.routes.DocumentProject") as model,
        ):
            model.query.filter_by.return_value.first_or_404.return_value = project
            response = routes.export_project.__wrapped__("project-diagrams")

        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
        sections = soup.select("section.project-document")
        self.assertEqual(len(sections), 2)
        self.assertEqual(len(soup.select("figure.diagram-rendered-v1 img")), 2)
        ids = [str(tag["id"]) for tag in soup.find_all(id=True)]
        self.assertEqual(len(ids), len(set(ids)))
        for index, (section, label) in enumerate(
            zip(sections, ("Początek α", "Początek β"), strict=True), start=1
        ):
            figure = section.find("figure")
            self.assertEqual(figure["id"], f"project-doc-{index}-diagram-1")
            self.assertIn("diagram-rendered-v1", figure.get("class", []))
            self.assertEqual(
                figure.figcaption.get_text(" ", strip=True), f"Diagram {index}"
            )
            self.assertEqual(
                [anchor["href"] for anchor in section.main.p.find_all("a")],
                [
                    f"#project-doc-{index}-diagram-1",
                    f"#project-doc-{index}-diagram-1--a",
                ],
            )
            self.assertEqual(
                section.find(id=f"project-doc-{index}-diagram-1--a").get_text(), label
            )
            description = section.select_one(".diagram-description-v1")
            self.assertIsNotNone(section.find("a", href=f"#{description['id']}"))
            source = figure.img["src"]
            self.assertTrue(source.startswith("data:image/svg+xml;base64,"))
            svg = base64.b64decode(source.partition(",")[2], validate=True)
            self.assertIn(label.encode(), svg)
        self.assertEqual([doc.output_html for doc in documents], original_html)

    def test_sync_zip_export(self):
        # 3. Test synchronous ZIP export for Markdown/HTML.
        app = self._app()
        project_mock = SimpleNamespace(
            id="project-1", name="My Test Project", user_id=7, documents=[]
        )

        doc1 = SimpleNamespace(
            id="doc-1",
            filename="doc1.pdf",
            status="completed",
            output_markdown="# Heading 1\nSome markdown text.",
            output_html=None,
            settings={"document_metadata": {"title": "Doc One Title"}},
            file_path="/uploads/doc1.pdf",
        )

        project_mock.documents = [SimpleNamespace(document=doc1)]

        with app.test_request_context(
            "/addons/doc_processor/api/projects/project-1/export?format=md&mode=separate"
        ):
            with patch.object(routes, "current_user", SimpleNamespace(id=7)):
                with patch(
                    "iarf_assistant.addons.doc_processor.routes.DocumentProject"
                ) as MockDocumentProject:
                    MockDocumentProject.query.filter_by.return_value.first_or_404.return_value = project_mock
                    response = routes.export_project.__wrapped__("project-1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Type"], "application/zip")
        self.assertIn(
            'attachment; filename="My_Test_Project.zip"',
            response.headers["Content-Disposition"],
        )

        # Verify ZIP contains the file
        zip_data = io.BytesIO(response.get_data())
        with zipfile.ZipFile(zip_data) as zf:
            namelist = zf.namelist()
            self.assertEqual(len(namelist), 1)
            self.assertEqual(namelist[0], "doc1.md")
            self.assertEqual(
                zf.read("doc1.md").decode("utf-8"), "# Heading 1\nSome markdown text."
            )

    def test_epub_export_enqueuing(self):
        # 4. Test EPUB export enqueuing (both concatenated and separate).
        app = self._app()
        project_mock = SimpleNamespace(
            id="project-1", name="My Test Project", user_id=7, documents=[]
        )
        doc1 = SimpleNamespace(
            id="doc-1",
            filename="doc1.pdf",
            status="completed",
            output_markdown="# Heading 1",
            output_html=None,
            settings={},
            file_path="/uploads/doc1.pdf",
        )
        project_mock.documents = [SimpleNamespace(document=doc1)]
        fake_queue = FakeQueue()

        # Concatenated EPUB ignores the obsolete source-TOC presentation query.
        with app.test_request_context(
            "/addons/doc_processor/api/projects/project-1/export?format=epub&mode=concatenated&epub_image_mode=webp&include_source_toc=true"
        ):
            with patch.object(routes, "current_user", SimpleNamespace(id=7)):
                with patch(
                    "iarf_assistant.addons.doc_processor.routes.DocumentProject"
                ) as MockDocumentProject:
                    with (
                        patch.object(
                            routes, "_get_task_queue", return_value=fake_queue
                        ),
                        patch.object(routes, "_ensure_workers"),
                    ):
                        MockDocumentProject.query.filter_by.return_value.first_or_404.return_value = project_mock
                        response, status_code = routes.export_project.__wrapped__(
                            "project-1"
                        )

        self.assertEqual(status_code, 202)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["job_id"], "job-123")
        self.assertEqual(len(fake_queue.enqueued), 1)
        self.assertEqual(fake_queue.enqueued[0][0], "export_project")
        self.assertEqual(fake_queue.enqueued[0][1]["mode"], "concatenated")
        self.assertEqual(fake_queue.enqueued[0][1]["epub_image_mode"], "webp")
        self.assertIs(fake_queue.enqueued[0][1]["include_source_toc"], False)

        # Separate EPUB
        fake_queue_separate = FakeQueue()
        with app.test_request_context(
            "/addons/doc_processor/api/projects/project-1/export?format=epub&mode=separate"
        ):
            with patch.object(routes, "current_user", SimpleNamespace(id=7)):
                with patch(
                    "iarf_assistant.addons.doc_processor.routes.DocumentProject"
                ) as MockDocumentProject:
                    with (
                        patch.object(
                            routes, "_get_task_queue", return_value=fake_queue_separate
                        ),
                        patch.object(routes, "_ensure_workers"),
                    ):
                        MockDocumentProject.query.filter_by.return_value.first_or_404.return_value = project_mock
                        response, status_code = routes.export_project.__wrapped__(
                            "project-1"
                        )

        self.assertEqual(status_code, 202)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["job_id"], "job-123")
        self.assertEqual(len(fake_queue_separate.enqueued), 1)
        self.assertEqual(fake_queue_separate.enqueued[0][0], "export_project")
        self.assertEqual(fake_queue_separate.enqueued[0][1]["mode"], "separate")
        self.assertEqual(fake_queue_separate.enqueued[0][1]["epub_image_mode"], "jpeg")
        self.assertIs(fake_queue_separate.enqueued[0][1]["include_source_toc"], False)

    def test_worker_export_project_concatenated(self):
        # 5. Worker task test: Mock pypandoc and check concatenated EPUB compilation
        project_mock = SimpleNamespace(
            id="project-1", name="My Test Project", user_id=7, documents=[]
        )

        doc1 = SimpleNamespace(
            id="doc-1",
            filename="doc1.pdf",
            status="completed",
            output_markdown=None,
            output_html="<h1>Heading 1</h1><p>Doc 1 Content</p>",
            settings={"document_metadata": {"title": "Doc One Title"}},
            file_path="/uploads/doc1.pdf",
        )

        project_mock.documents = [SimpleNamespace(document=doc1)]

        payload = {
            "project_id": "project-1",
            "user_id": 7,
            "format": "epub",
            "mode": "concatenated",
        }

        fake_queue = FakeQueue()

        with patch.object(worker.db.session, "get", return_value=project_mock):
            result = self._run_worker(
                worker.export_project, payload, fake_queue, "job-123"
            )

        self.assertEqual(result["kind"], "file_download")
        self.assertEqual(result["download_name"], "My_Test_Project.epub")
        self.assertEqual(result["mimetype"], "application/epub+zip")
        self.assertEqual(result["user_id"], 7)
        self.assertTrue(Path(result["output_path"]).exists())
        self.assertEqual(
            routes._validate_epub_file(result["output_path"]), (True, None)
        )
        # Clean up
        Path(result["output_path"]).unlink(missing_ok=True)

    def test_worker_export_project_separate(self):
        # 6. Worker task test: Mock separate EPUB conversion and ZIP output
        project_mock = SimpleNamespace(
            id="project-1", name="My Test Project", user_id=7, documents=[]
        )

        doc1 = SimpleNamespace(
            id="doc-1",
            filename="doc1.pdf",
            status="completed",
            output_markdown=None,
            output_html="<h1>Heading 1</h1><p>Doc 1 Content</p>",
            settings={"document_metadata": {"title": "Doc One Title"}},
            file_path="/uploads/doc1.pdf",
        )
        doc2 = SimpleNamespace(
            id="doc-2",
            filename="doc2.pdf",
            status="completed",
            output_markdown=None,
            output_html="<h1>Heading 2</h1><p>Doc 2 Content</p>",
            settings={"document_metadata": {"title": "Doc Two Title"}},
            file_path="/uploads/doc2.pdf",
        )

        project_mock.documents = [
            SimpleNamespace(document=doc1),
            SimpleNamespace(document=doc2),
        ]

        payload = {
            "project_id": "project-1",
            "user_id": 7,
            "format": "epub",
            "mode": "separate",
        }

        fake_queue = FakeQueue()

        with patch.object(worker.db.session, "get", return_value=project_mock):
            result = self._run_worker(
                worker.export_project, payload, fake_queue, "job-123"
            )

        self.assertEqual(result["kind"], "file_download")
        self.assertEqual(result["download_name"], "My_Test_Project.zip")
        self.assertEqual(result["mimetype"], "application/zip")
        self.assertTrue(Path(result["output_path"]).exists())

        # Verify ZIP contents
        with zipfile.ZipFile(result["output_path"]) as zf:
            namelist = zf.namelist()
            self.assertEqual(len(namelist), 2)
            self.assertIn("doc1.epub", namelist)
            self.assertIn("doc2.epub", namelist)
            for member in ("doc1.epub", "doc2.epub"):
                epub_bytes = zf.read(member)
                self.assertTrue(epub_bytes.startswith(b"PK"))
                with zipfile.ZipFile(io.BytesIO(epub_bytes)) as epub_zip:
                    self.assertEqual(epub_zip.namelist()[0], "mimetype")

        # Clean up
        Path(result["output_path"]).unlink(missing_ok=True)

    def test_worker_separate_epub_export_fails_closed_on_validation_error(self):
        project_mock = SimpleNamespace(
            id="project-1",
            name="Invalid EPUB Project",
            user_id=7,
            documents=[
                SimpleNamespace(
                    document=SimpleNamespace(
                        id="doc-1",
                        filename="doc1.pdf",
                        status="completed",
                        output_markdown=None,
                        output_html="<h1>Heading</h1><p>Content</p>",
                        settings={"document_metadata": {"title": "Document"}},
                        file_path="/uploads/doc1.pdf",
                    )
                )
            ],
        )
        payload = {
            "project_id": "project-1",
            "user_id": 7,
            "format": "epub",
            "mode": "separate",
        }
        output_path = (
            Path(tempfile.gettempdir()) / "Invalid_EPUB_Project-job-invalid.zip"
        )
        output_path.unlink(missing_ok=True)

        with patch.object(worker.db.session, "get", return_value=project_mock):
            with patch.object(
                routes,
                "_validate_epub_file",
                return_value=(False, "deliberate validation failure"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "deliberate validation failure"
                ):
                    self._run_worker(
                        worker.export_project, payload, FakeQueue(), "job-invalid"
                    )

        self.assertFalse(output_path.exists())

    def test_docx_preprocessor_footnotes_and_math(self):
        from iarf_assistant.addons.doc_processor.docx_preprocessor import (
            preprocess_html_for_docx,
        )

        raw_html = (
            '<p>This is some text with a footnote ref<a href="#fn-1-abc" class="footnote-ref" id="fnref-1">1</a>. '
            "And some inline math $E=mc^2$ and block math $$x = y$$.</p>"
            '<div id="fn-1-abc" class="footnote"><span class="footnote-marker">1. </span>Footnote content text</div>'
        )

        preprocessed = preprocess_html_for_docx(raw_html)
        soup = BeautifulSoup(preprocessed, "html.parser")

        # Check math delimiters
        self.assertIn("\\(E=mc^2\\)", preprocessed)
        self.assertIn("\\[x = y\\]", preprocessed)
        self.assertNotIn("$", preprocessed)

        # Check footnote ref rewriting
        ref_link = soup.find("a", class_="footnote-ref")
        self.assertIsNotNone(ref_link)
        self.assertEqual(ref_link["href"], "#fn1")
        self.assertEqual(ref_link["id"], "fnref1")
        self.assertEqual(ref_link.get_text(strip=True), "1")

        # Check section footnotes structure
        section = soup.find("section", class_="footnotes")
        self.assertIsNotNone(section)
        self.assertEqual(section["role"], "doc-endnotes")

        li = section.find("li", id="fn1")
        self.assertIsNotNone(li)
        self.assertEqual(li["role"], "doc-endnote")
        self.assertIn("Footnote content text", li.text)

        # Check backlink
        backlink = li.find("a", class_="footnote-back")
        self.assertIsNotNone(backlink)
        self.assertEqual(backlink["href"], "#fnref1")
        self.assertEqual(backlink.get_text(strip=True), "↩")

    def test_docx_preprocessor_preserves_unlinked_note_body(self):
        from iarf_assistant.addons.doc_processor.docx_preprocessor import (
            preprocess_html_for_docx,
        )

        raw_html = (
            '<p>Text with <a href="#fn-1" class="footnote-ref">1</a>.</p>'
            '<div id="fn-1" class="footnote"><p>2024 is the publication year.</p></div>'
            '<div id="fn-2" class="footnote"><p>Orphan note <strong>must survive</strong>.</p></div>'
        )

        soup = BeautifulSoup(preprocess_html_for_docx(raw_html), "html.parser")
        section = soup.find("section", class_="footnotes")
        assert section is not None
        assert section.find("li", id="fn1") is not None
        assert (
            section.find("li", id="fn1")
            .get_text(" ", strip=True)
            .startswith("2024 is the publication year.")
        )
        unlinked = section.find("li", id="fn-2", class_="unlinked-footnote")
        assert unlinked is not None
        assert "Orphan note" in unlinked.get_text(" ", strip=True)
        assert "must survive" in unlinked.get_text(" ", strip=True)
        unlinked_list = section.find("ol", class_="unlinked-notes")
        assert unlinked_list is not None
        assert unlinked_list.get("data-unlinked-notes") == "true"
        assert "Unlinked source notes" not in section.get_text(" ", strip=True)
        assert "These notes were present" not in section.get_text(" ", strip=True)

    def test_docx_preprocessor_normalizes_whole_block_note_backlinks(self):
        from iarf_assistant.addons.doc_processor.docx_preprocessor import (
            preprocess_html_for_docx,
        )

        raw_html = (
            '<p>First<a id="translator-reader-note-a-ref" '
            'href="#translator-reader-note-a" role="doc-noteref" '
            'epub:type="noteref">1</a> and again<a '
            'id="translator-reader-note-a-ref-2" '
            'href="#translator-reader-note-a" role="doc-noteref" '
            'epub:type="noteref">1</a>. Second<a '
            'id="translator-reader-note-b-ref" '
            'href="#translator-reader-note-b" role="doc-noteref" '
            'epub:type="noteref">2</a>.</p>'
            '<section id="translator-doc-endnotes" role="doc-endnotes" '
            'epub:type="endnotes">'
            '<aside id="translator-reader-note-a" role="doc-footnote" '
            'epub:type="footnote"><p>First note '
            '<a href="#translator-reader-note-a-ref" role="doc-backlink" '
            'epub:type="backlink">↩</a> '
            '<a href="#note-a-detail" class="ordinary-link">More detail</a>'
            '</p><p id="note-a-detail">Detail.</p></aside>'
            '<aside id="translator-reader-note-b" role="doc-footnote" '
            'epub:type="footnote"><p>Second note '
            '<a href="#translator-reader-note-b-ref" '
            'role="reader doc-backlink" epub:type="annotation backlink" '
            'class="footnote-backlink">↩</a></p></aside>'
            "</section>"
        )

        soup = BeautifulSoup(preprocess_html_for_docx(raw_html), "html.parser")
        references = soup.select("a.footnote-ref")
        self.assertEqual(
            [(ref["id"], ref["href"]) for ref in references],
            [
                ("fnref1", "#fn1"),
                ("fnref1-2", "#fn1"),
                ("fnref2", "#fn2"),
            ],
        )

        first_note = soup.find("li", id="fn1")
        second_note = soup.find("li", id="fn2")
        self.assertIsNotNone(first_note)
        self.assertIsNotNone(second_note)
        self.assertEqual(
            [
                (link.get("href"), link.get_text(strip=True))
                for link in first_note.find_all("a")
            ],
            [("#note-a-detail", "More detail"), ("#fnref1", "↩")],
        )
        self.assertEqual(
            [
                (link.get("href"), link.get_text(strip=True))
                for link in second_note.find_all("a")
            ],
            [("#fnref2", "↩")],
        )
        self.assertEqual(len(first_note.select("a[href='#fnref1']")), 1)
        self.assertEqual(len(second_note.select("a[href='#fnref2']")), 1)

    def test_docx_preprocessor_keeps_broken_unrelated_links_rejected(self):
        from iarf_assistant.addons.doc_processor.docx_preprocessor import (
            preprocess_html_for_docx,
        )

        with self.assertRaisesRegex(ValueError, "Unresolved internal EPUB link"):
            preprocess_html_for_docx(
                '<p>Text<a href="#fn-1" class="footnote-ref">1</a>.</p>'
                '<aside id="fn-1" role="doc-footnote"><p>Note '
                '<a href="#missing">Broken ordinary link</a>.</p></aside>'
            )

    def test_docx_preprocessor_fails_before_packaging_on_broken_note_reference(self):
        from iarf_assistant.addons.doc_processor.docx_preprocessor import (
            preprocess_html_for_docx,
        )

        with self.assertRaisesRegex(ValueError, "Unresolved internal EPUB link"):
            preprocess_html_for_docx(
                '<p>Text<a href="#missing" class="footnote-ref">1</a>.</p>'
            )

    def test_docx_preprocessor_limits_images_to_explicit_document_roots(self):
        from iarf_assistant.addons.doc_processor.docx_preprocessor import (
            preprocess_html_for_docx,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            document_dir = root / "document"
            document_dir.mkdir()
            source_path = document_dir / "source.pdf"
            source_path.write_bytes(b"%PDF")
            contained = document_dir / "figure.png"
            contained.write_bytes(b"png")
            outside = root / "private.png"
            outside.write_bytes(b"private")
            html = (
                '<img id="contained" src="figure.png">'
                f'<img id="outside-file" src="{outside.as_uri()}">'
                '<img id="outside-relative" src="../private.png">'
                '<img id="remote" src="https://tracker.example/pixel.png">'
            )

            processed = preprocess_html_for_docx(
                html,
                doc_file_path=str(source_path),
                uploads_dir=root,
            )

        soup = BeautifulSoup(processed, "html.parser")
        self.assertEqual(Path(soup.find(id="contained")["src"]), contained.resolve())
        self.assertFalse(soup.find(id="outside-file").has_attr("src"))
        self.assertFalse(soup.find(id="outside-relative").has_attr("src"))
        self.assertFalse(soup.find(id="remote").has_attr("src"))

    def test_worker_export_document_webp_image_mode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            image_path = temp_path / "figure.png"
            from PIL import Image, features

            if not features.check("webp"):
                self.skipTest("Pillow WebP support is unavailable")
            Image.new("RGB", (32, 32), "green").save(image_path)
            doc_mock = SimpleNamespace(
                id="doc-webp",
                filename="illustrated.pdf",
                status="completed",
                user_id=7,
                output_html=f"<main><h1>Illustrated</h1><img src='{image_path.as_posix()}' alt='Green square'></main>",
                output_markdown=None,
                settings={
                    "document_metadata": {"title": "Illustrated", "language": "en"}
                },
                metadata_record=None,
                file_path=str(temp_path / "illustrated.pdf"),
            )
            payload = {
                "document_id": "doc-webp",
                "user_id": 7,
                "format": "epub",
                "epub_image_mode": "webp",
            }

            with patch.object(worker.db.session, "get", return_value=doc_mock):
                result = self._run_worker(
                    worker.export_document, payload, FakeQueue(), "job-webp"
                )

            try:
                with zipfile.ZipFile(result["output_path"]) as epub_zip:
                    package = epub_zip.read("EPUB/package.opf").decode("utf-8")
                    self.assertIn('media-type="image/webp"', package)
                    self.assertTrue(
                        any(name.endswith(".webp") for name in epub_zip.namelist())
                    )
            finally:
                Path(result["output_path"]).unlink(missing_ok=True)

    def test_worker_export_document_docx(self):
        doc_mock = SimpleNamespace(
            id="doc-123",
            filename="my_paper.pdf",
            status="completed",
            user_id=7,
            output_html=(
                "<nav class='source-toc'><h2>Contents</h2>"
                "<div>Title ... 1</div></nav><h1>Title</h1><p>Content.</p>"
            ),
            output_markdown=None,
            settings={},
            file_path="/uploads/my_paper.pdf",
        )

        payload = {"document_id": "doc-123", "user_id": 7, "format": "docx"}

        fake_queue = FakeQueue()

        def fake_convert_text(source, to, format, outputfile, extra_args):
            self.assertEqual(to, "docx")
            self.assertEqual(format, "html")
            heading = BeautifulSoup(source, "html.parser").find("h1")
            self.assertIsNotNone(heading)
            self.assertEqual(heading.get_text(strip=True), "Title")
            self.assertNotIn("source-toc", source)
            self.assertNotIn("Title ... 1", source)
            Path(outputfile).write_bytes(b"dummy_docx_data")

        with patch.object(worker.db.session, "get", return_value=doc_mock):
            with patch("pypandoc.convert_text", side_effect=fake_convert_text):
                with patch(
                    "iarf_assistant.addons.doc_processor.worker.ensure_default_reference_doc",
                    return_value="/tmp/ref.docx",
                ), patch(
                    "iarf_assistant.addons.doc_processor.print_export.finish_docx_export",
                    side_effect=lambda data, *args, **kwargs: data,
                ):
                    result = self._run_worker(
                        worker.export_document, payload, fake_queue, "job-123"
                    )

        self.assertEqual(result["kind"], "file_download")
        self.assertEqual(result["download_name"], "my_paper.docx")
        self.assertEqual(
            result["mimetype"],
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        self.assertEqual(result["user_id"], 7)
        self.assertTrue(Path(result["output_path"]).exists())
        Path(result["output_path"]).unlink(missing_ok=True)

    def test_worker_export_project_docx_concatenated(self):
        project_mock = SimpleNamespace(
            id="project-1", name="My Test Project", user_id=7, documents=[]
        )

        doc1 = SimpleNamespace(
            id="doc-1",
            filename="doc1.pdf",
            status="completed",
            output_markdown=None,
            output_html="<h1>Heading 1</h1><p>Doc 1 Content</p>",
            settings={"document_metadata": {"title": "Doc One Title"}},
            file_path="/uploads/doc1.pdf",
        )

        project_mock.documents = [SimpleNamespace(document=doc1)]

        payload = {
            "project_id": "project-1",
            "user_id": 7,
            "format": "docx",
            "mode": "concatenated",
        }

        fake_queue = FakeQueue()

        def fake_convert_file(source_file, to, format, outputfile, extra_args):
            self.assertEqual(to, "docx")
            self.assertEqual(format, "html")
            self.assertTrue(Path(source_file).exists())
            html_content = Path(source_file).read_text(encoding="utf-8")
            self.assertIn("<h1>Doc One Title</h1>", html_content)
            Path(outputfile).write_bytes(b"dummy_docx_data")

        with patch.object(worker.db.session, "get", return_value=project_mock):
            with patch("pypandoc.convert_file", side_effect=fake_convert_file):
                with patch(
                    "iarf_assistant.addons.doc_processor.worker.ensure_default_reference_doc",
                    return_value="/tmp/ref.docx",
                ), patch(
                    "iarf_assistant.addons.doc_processor.print_export.finish_docx_export",
                    side_effect=lambda data, *args, **kwargs: data,
                ):
                    result = self._run_worker(
                        worker.export_project, payload, fake_queue, "job-123"
                    )

        self.assertEqual(result["kind"], "file_download")
        self.assertEqual(result["download_name"], "My_Test_Project.docx")
        self.assertEqual(
            result["mimetype"],
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        self.assertTrue(Path(result["output_path"]).exists())
        Path(result["output_path"]).unlink(missing_ok=True)

    def test_worker_concatenated_docx_namespaces_documents_and_omits_source_tocs(
        self,
    ):
        repeated_html = (
            '<main><span id="doc-page-1" class="pagebreak" role="doc-pagebreak" '
            'epub:type="pagebreak" title="1" aria-label="1"></span>'
            '<h1 id="chapter">Chapter</h1><p>Text<sup><a id="fnref-1" '
            'class="footnote-ref" role="doc-noteref" epub:type="noteref" '
            'href="#fn-1">1</a></sup></p>'
            '<table><tr><th id="head">Head</th><td headers="head">Cell</td></tr></table>'
            '<nav class="source-toc"><h2 class="source-toc-title">Contents</h2>'
            '<div class="toc-entry toc-depth-1"><span class="toc-entry-title">'
            'Chapter</span> <span class="toc-page-label">1</span></div></nav>'
            '<section id="footnotes" role="doc-endnotes" epub:type="endnotes">'
            '<ol><li id="fn-1" class="footnote" role="doc-endnote" '
            'epub:type="endnote">A note.</li></ol></section></main>'
        )
        documents = [
            SimpleNamespace(
                id=f"doc-{index}",
                user_id=7,
                filename=f"doc{index}.pdf",
                status="completed",
                output_markdown=None,
                output_html=repeated_html,
                settings={"document_metadata": {"title": f"Document {index}"}},
                file_path=f"/uploads/7/doc_processor_docs/doc{index}.pdf",
            )
            for index in (1, 2)
        ]
        project_mock = SimpleNamespace(
            id="project-multi",
            name="Multi PDF Project",
            user_id=7,
            documents=[SimpleNamespace(document=doc) for doc in documents],
        )
        captured = {}

        def fake_convert_file(source_file, to, format, outputfile, extra_args):
            captured["html"] = Path(source_file).read_text(encoding="utf-8")
            Path(outputfile).write_bytes(b"dummy_docx_data")

        payload = {
            "project_id": "project-multi",
            "user_id": 7,
            "format": "docx",
            "mode": "concatenated",
        }
        with (
            patch.object(worker.db.session, "get", return_value=project_mock),
            patch("pypandoc.convert_file", side_effect=fake_convert_file),
            patch(
                "iarf_assistant.addons.doc_processor.worker.ensure_default_reference_doc",
                return_value="/tmp/ref.docx",
            ),
            patch(
                "iarf_assistant.addons.doc_processor.print_export.finish_docx_export",
                side_effect=lambda data, *args, **kwargs: data,
            ),
        ):
            result = self._run_worker(
                worker.export_project, payload, FakeQueue(), "job-multi"
            )

        try:
            soup = BeautifulSoup(captured["html"], "html.parser")
            ids = [str(tag.get("id")) for tag in soup.find_all(id=True)]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertIn("project-doc-1-doc-page-1", ids)
            self.assertIn("project-doc-2-doc-page-1", ids)
            self.assertEqual(len(soup.select("nav.source-toc")), 0)
            for ref in soup.select("a.footnote-ref"):
                self.assertIsNotNone(soup.find(id=ref["href"].lstrip("#")))
            for cell in soup.find_all("td", headers=True):
                self.assertIsNotNone(soup.find(id=cell["headers"]))
        finally:
            Path(result["output_path"]).unlink(missing_ok=True)
