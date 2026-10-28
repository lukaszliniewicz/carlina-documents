import hashlib
import io
import subprocess
import traceback
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup
from content_storage_support import bind_content_policy
from flask import Flask
from sqlalchemy import inspect

# Keep the old package-name references consolidated for the exact rename ratchet.
# isort: off
from iarf_assistant import extensions as app_extensions, models as app_models
from iarf_assistant.addons.doc_processor import (
    api_client as doc_processor_api,
    docx_preprocessor as docx_preprocessor_module,
    models as doc_processor_models,
    semantic_export as semantic_export_module,
    semantic_document as semantic_document_module,
)
from iarf_assistant.addons.translator import block_contract, block_planner, block_rendering
from iarf_assistant.addons.doc_processor.semantic_document import SemanticDocumentError
from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION
# isort: on

DocProcessorClient = doc_processor_api.DocProcessorClient
preprocess_html_for_docx = docx_preprocessor_module.preprocess_html_for_docx
SemanticExportError = semantic_export_module.SemanticExportError
ProcessedDocument = doc_processor_models.ProcessedDocument
db = app_extensions.db
User = app_models.User


def _make_app(tmp_path):
    app = Flask(__name__, instance_path=str(tmp_path))
    bind_content_policy(app)
    app.config.update(
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="test",
        TESTING=True,
    )
    db.init_app(app)
    return app


@pytest.mark.parametrize("with_image", [False, True])
def test_book_length_unicode_snapshot_plans_without_entity_expansion(tmp_path, with_image):
    app = _make_app(tmp_path)
    uploads = tmp_path / "uploads" / "1"
    uploads.mkdir(parents=True)
    app.config["CARLINA_UPLOADS_DIR"] = str(tmp_path / "uploads")
    prose = "Свобода личности и духа. " * 80
    html = "<main><h1>Свобода</h1>" + f"<p>{prose}</p>" * 150
    if with_image:
        # Resource capture checks owned bytes; raster decoding is tested elsewhere.
        (uploads / "cover.png").write_bytes(b"synthetic-owned-image")
        html += '<figure><img src="/uploads/1/cover.png" alt="Обложка"/></figure>'
    html += "</main>"
    canonical = semantic_document_module.canonicalize_document_html(
        html, user_id=1, uploads_dir=tmp_path / "uploads",
    )
    assert not canonical.errors
    old_serialization = BeautifulSoup(canonical.html, "html.parser").decode(formatter="html")
    assert old_serialization.count("cy;") > 200_000
    # A retained image-bearing source also needs compact resource substitution.
    stored = old_serialization if with_image else canonical.html
    settings = semantic_document_module.semantic_settings({}, canonical)
    settings["semantic_content_hash"] = hashlib.sha256(stored.encode()).hexdigest()
    with app.app_context():
        db.create_all()
        db.session.add(User(id=1, email="owner@example.test", password_hash="test"))
        document = ProcessedDocument(
            id="unicode-book", user_id=1, filename="book.fb2",
            file_path="/uploads/1/book.fb2", status="completed",
            output_html=stored, settings=settings,
        )
        db.session.add(document)
        db.session.commit()
        snapshot = DocProcessorClient(None, {"current_user_id": 1}).get_document_snapshot(document.id)
        assert document.output_html == stored
        assert "Свобода личности и духа." in snapshot["output_html"]
        assert "&scy;" not in snapshot["output_html"]
        plan = block_planner.plan_document_blocks(
            block_contract.AttestedDocumentSnapshot.from_mapping(snapshot),
            project_id="unicode-project", source_language="ru",
            source_attestation=block_contract.SourceProviderAttestation(
                provider_addon_id="doc-processor", provider_capability_id="document.processing",
                owner_id="1", resource_id=document.id,
            ),
        )
        block_planner.validate_canonical_plan(plan)
        assert block_rendering.render_source_identity(plan) == snapshot["output_html"]
        assert sum(block.projection.count(prose) for block in plan.blocks) == 150
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def test_document_snapshot_is_owned_hash_exact_html_and_allowlisted_provenance(
    tmp_path,
):
    app = _make_app(tmp_path)
    html = "<p>Zażółć gęślą jaźń&nbsp;— exact bytes.</p>"

    with app.app_context():
        db.create_all()
        db.session.add(User(id=1, email="owner@example.test", password_hash="test"))
        db.session.add(
            ProcessedDocument(
                id="doc-owner",
                user_id=1,
                filename="source.html",
                file_path="/uploads/source.html",
                status="completed",
                output_html=html,
                settings={
                    "semantic_contract_version": SEMANTIC_HTML_CONTRACT_VERSION,
                    "semantic_contract_mode": "completed",
                    "semantic_content_hash": hashlib.sha256(
                        html.encode("utf-8")
                    ).hexdigest(),
                    "document_metadata": {
                        "title": "A bounded title",
                        "language": "Polish",
                        "contributors": {
                            "authors": ["Author One", "Author Two"],
                            "other": [{"role": "illustrator", "name": "Artist"}],
                        },
                        "identifiers": [
                            {"scheme": "ISBN", "value": "978-1-2345-6789-0"},
                        ],
                        "publication_date": "2024-01-02",
                        "extraction_notes": ["private extractor trace"],
                        "private_setting": "must not cross the source boundary",
                    },
                    "knowledge_source_id": "knowledge-source-7",
                    "knowledge_source_version_id": "knowledge-version-7",
                    "knowledge_source_version_number": 3,
                    "knowledge_source_content_hash": "provider-hash",
                    "private_path": "/uploads/private/source.html",
                },
            )
        )
        db.session.commit()

        snapshot = DocProcessorClient(
            None, {"current_user_id": 1}
        ).get_document_snapshot("doc-owner")
        listed = DocProcessorClient(
            None, {"current_user_id": 1}
        ).get_documents(status="completed")

        assert snapshot["id"] == snapshot["document_id"] == "doc-owner"
        assert (
            snapshot["snapshot_contract_version"]
            == doc_processor_api.DOCUMENT_SNAPSHOT_CONTRACT_VERSION
        )
        assert snapshot["filename"] == "source.html"
        assert snapshot["status"] == "completed"
        assert snapshot["output_html"] == html
        assert snapshot["semantic_contract_mode"] == "completed"
        assert listed[0]["semantic_contract_mode"] == "completed"
        assert snapshot["metadata"] == {
            "title": "A bounded title",
            "publication_date": "2024-01-02",
            "isbn": "978-1-2345-6789-0",
            "language": "pl",
            "contributors": {
                "authors": ["Author One", "Author Two"],
                "other": [{"role": "illustrator", "name": "Artist"}],
            },
            "identifiers": [
                {"scheme": "ISBN", "value": "978-1-2345-6789-0"},
            ],
        }
        assert "extraction_notes" not in snapshot["metadata"]
        assert "private_setting" not in snapshot["metadata"]
        assert (
            snapshot["content_hash"] == hashlib.sha256(html.encode("utf-8")).hexdigest()
        )
        captured_at = datetime.fromisoformat(snapshot["captured_at"])
        source_updated_at = datetime.fromisoformat(snapshot["source_updated_at"])
        assert captured_at.tzinfo == UTC
        assert source_updated_at.tzinfo == UTC
        assert captured_at >= source_updated_at
        assert snapshot["provenance"] == {
            "knowledge_source_id": "knowledge-source-7",
            "knowledge_source_version_id": "knowledge-version-7",
            "knowledge_source_version_number": 3,
            "knowledge_source_content_hash": "provider-hash",
        }
        assert "private_path" not in snapshot["provenance"]

        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def test_document_snapshot_marks_ordinary_legacy_output_relaxed_legacy(tmp_path):
    app = _make_app(tmp_path)

    with app.app_context():
        db.create_all()
        db.session.add(User(id=1, email="owner@example.test", password_hash="test"))
        db.session.add(
            ProcessedDocument(
                id="doc-legacy",
                user_id=1,
                filename="legacy.html",
                file_path="/uploads/legacy.html",
                status="completed",
                output_html="<p>Legacy source.</p>",
                settings={},
            )
        )
        db.session.commit()

        snapshot = DocProcessorClient(
            None, {"current_user_id": 1}
        ).get_document_snapshot("doc-legacy")

        assert (
            snapshot["snapshot_contract_version"]
            == doc_processor_api.DOCUMENT_SNAPSHOT_CONTRACT_VERSION
        )
        assert snapshot["semantic_contract_mode"] == "relaxed_legacy"
        assert snapshot["output_html"]

        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def test_note_arrangement_survives_record_backed_reads_after_session_reload(tmp_path):
    from iarf_assistant.addons.doc_processor.metadata_service import (
        sync_document_metadata,
    )

    app = _make_app(tmp_path)
    arrangement = {
        "placement": "chapter_endnotes",
        "numbering": "chapter",
        "scope_heading_level": 2,
        "source_pages": [2, 7],
        "evidence": "Markers restart at the chapter boundary.",
    }

    with app.app_context():
        db.create_all()
        db.session.add(User(id=1, email="owner@example.test", password_hash="test"))
        document = ProcessedDocument(
            id="doc-note-arrangement",
            user_id=1,
            filename="source.html",
            file_path="/uploads/1/source.html",
            status="completed",
            output_html="<main><p>Synthetic source.</p></main>",
            settings={
                "document_metadata": {
                    "title": "Settings title",
                    "note_arrangement": arrangement,
                },
                "private_setting": "must not enter metadata DTOs",
            },
        )
        db.session.add(document)
        db.session.flush()
        sync_document_metadata(
            document,
            {"title": "Settings title", "note_arrangement": arrangement},
        )
        document.metadata_record.title = "Record title"
        db.session.commit()
        db.session.remove()

        reloaded = db.session.get(ProcessedDocument, "doc-note-arrangement")
        assert reloaded is not None
        model_view = reloaded.to_dict()
        assert model_view["metadata"]["id"] == reloaded.metadata_record.id
        assert model_view["metadata"]["document_id"] == reloaded.id
        assert "created_at" in model_view["metadata"]
        assert model_view["metadata"]["subtitle"] is None
        client = DocProcessorClient(None, {"current_user_id": 1})
        source_view = client._source_payload(reloaded)
        snapshot = client.get_document_snapshot("doc-note-arrangement")

        for metadata in (
            model_view["metadata"],
            source_view["metadata"],
            snapshot["metadata"],
        ):
            assert metadata["title"] == "Record title"
            assert metadata["note_arrangement"] == arrangement
            assert "private_setting" not in metadata

        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def test_document_snapshot_requires_explicit_mode_for_current_attestation(tmp_path):
    app = _make_app(tmp_path)
    html = "<main><p>Current bytes without an explicit mode.</p></main>"

    with app.app_context():
        db.create_all()
        db.session.add(User(id=1, email="owner@example.test", password_hash="test"))
        db.session.add(
            ProcessedDocument(
                id="doc-missing-mode",
                user_id=1,
                filename="current.html",
                file_path="/uploads/current.html",
                status="completed",
                output_html=html,
                settings={
                    "semantic_contract_version": SEMANTIC_HTML_CONTRACT_VERSION,
                    "semantic_content_hash": hashlib.sha256(
                        html.encode("utf-8")
                    ).hexdigest(),
                },
            )
        )
        db.session.commit()

        snapshot = DocProcessorClient(
            None, {"current_user_id": 1}
        ).get_document_snapshot("doc-missing-mode")

        assert snapshot["output_html"] == html
        assert snapshot["semantic_contract_version"] == SEMANTIC_HTML_CONTRACT_VERSION
        assert (
            snapshot["semantic_content_hash"]
            == hashlib.sha256(html.encode("utf-8")).hexdigest()
        )
        assert snapshot["semantic_contract_mode"] == "relaxed_legacy"

        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def test_document_snapshot_denies_unowned_document_without_leaking_state(tmp_path):
    app = _make_app(tmp_path)

    with app.app_context():
        db.create_all()
        db.session.add_all(
            [
                User(id=1, email="owner@example.test", password_hash="test"),
                User(id=2, email="other@example.test", password_hash="test"),
                ProcessedDocument(
                    id="private-doc",
                    user_id=2,
                    filename="private.html",
                    file_path="/uploads/private.html",
                    status="completed",
                    output_html="<p>private</p>",
                ),
            ]
        )
        db.session.commit()

        with pytest.raises(LookupError, match="Document was not found"):
            DocProcessorClient(None, {"current_user_id": 1}).get_document_snapshot(
                "private-doc"
            )
        with pytest.raises(LookupError, match="Document was not found"):
            DocProcessorClient(None, {"current_user_id": 1}).get_document_snapshot(
                "missing-doc"
            )

        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def test_document_snapshot_rejects_tampered_current_semantic_html(tmp_path):
    app = _make_app(tmp_path)
    original = "<main><p>Original</p></main>"

    with app.app_context():
        db.create_all()
        db.session.add(User(id=1, email="owner@example.test", password_hash="test"))
        db.session.add(
            ProcessedDocument(
                id="doc-tampered",
                user_id=1,
                filename="source.html",
                file_path="/uploads/1/source.html",
                status="completed",
                output_html="<main><p>Tampered</p></main>",
                settings={
                    "semantic_contract_version": SEMANTIC_HTML_CONTRACT_VERSION,
                    "semantic_contract_mode": "completed",
                    "semantic_content_hash": hashlib.sha256(
                        original.encode("utf-8")
                    ).hexdigest(),
                },
            )
        )
        db.session.commit()

        with pytest.raises(SemanticDocumentError, match="content hash"):
            DocProcessorClient(None, {"current_user_id": 1}).get_document_snapshot(
                "doc-tampered"
            )

        db.session.remove()
        db.drop_all()
        db.engine.dispose()


@pytest.mark.parametrize(
    ("status", "output_html", "error"),
    [
        ("processing", "<p>not ready</p>", "not completed"),
        ("completed", "", "no HTML output"),
        ("completed", "   \n\t", "no HTML output"),
    ],
)
def test_document_snapshot_rejects_incomplete_or_blank_output(
    tmp_path, status, output_html, error
):
    app = _make_app(tmp_path)

    with app.app_context():
        db.create_all()
        db.session.add(User(id=1, email="owner@example.test", password_hash="test"))
        db.session.add(
            ProcessedDocument(
                id="doc-invalid",
                user_id=1,
                filename="source.html",
                file_path="/uploads/source.html",
                status=status,
                output_html=output_html,
            )
        )
        db.session.commit()

        with pytest.raises(ValueError, match=error):
            DocProcessorClient(None, {"current_user_id": 1}).get_document_snapshot(
                "doc-invalid"
            )

        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def test_document_snapshot_is_detached_when_provider_document_changes(tmp_path):
    app = _make_app(tmp_path)

    with app.app_context():
        db.create_all()
        db.session.add(User(id=1, email="owner@example.test", password_hash="test"))
        document = ProcessedDocument(
            id="doc-changing",
            user_id=1,
            filename="source.html",
            file_path="/uploads/source.html",
            status="completed",
            output_html="<p>first</p>",
        )
        db.session.add(document)
        db.session.commit()

        client = DocProcessorClient(None, {"current_user_id": 1})
        first = client.get_document_snapshot(document.id)
        document.output_html = "<p>second</p>"
        db.session.commit()
        second = client.get_document_snapshot(document.id)

        assert first["output_html"] == "<p>first</p>"
        assert first["content_hash"] == hashlib.sha256(b"<p>first</p>").hexdigest()
        assert second["output_html"] == "<p>second</p>"
        assert second["content_hash"] != first["content_hash"]

        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def test_semantic_export_capability_pins_opaque_resources_and_exact_html(tmp_path):
    app = _make_app(tmp_path)
    uploads = tmp_path / "uploads" / "1"
    uploads.mkdir(parents=True)
    image = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\x0dIDAT\x08\xd7c\xf8\xcf\xc0\xf0\x1f\x00\x05"
        b"\x80\x02?I\xc2\xfd\x99\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    (uploads / "figure.png").write_bytes(image)
    app.config["CARLINA_UPLOADS_DIR"] = str(tmp_path / "uploads")
    with app.app_context():
        db.create_all()
        db.session.add(User(id=1, email="owner@example.test", password_hash="test"))
        document = ProcessedDocument(
            id="doc-export",
            user_id=1,
            filename="source.html",
            file_path="/uploads/1/source.html",
            status="completed",
            output_html="<main><nav class='source-toc'><ol><li>Old contents</li></ol></nav><h1 id='chapter'>Chapter</h1><figure><img src='/uploads/1/figure.png' alt='Figure'/><figcaption>Caption</figcaption></figure></main>",
            settings={},
        )
        db.session.add(document)
        db.session.commit()
        client = DocProcessorClient(None, {"current_user_id": 1})
        snapshot = client.get_document_snapshot(document.id)
        assert snapshot["resources"] and "/uploads/" not in snapshot["output_html"]
        assert ".carlina-resources/res_" in snapshot["output_html"]
        base = {
            "contract_version": "semantic-export-v1",
            "semantic_contract_version": snapshot["semantic_contract_version"],
            "semantic_html": snapshot["output_html"],
            "semantic_html_sha256": snapshot["content_hash"],
            "metadata": snapshot["metadata"],
            "resources": snapshot["resources"],
            "profile_id": "semantic-default",
            "profile_version": "1",
        }
        html_result = client.semantic_export(
            {**base, "format": "html", "include_source_toc": True}
        )
        assert b".carlina-resources/" not in html_result["data"]
        assert b"data:image/png;base64," in html_result["data"]
        (uploads / "figure-2.png").write_bytes(image + b"second")
        document.output_html = document.output_html.replace(
            "figure.png", "figure-2.png"
        )
        db.session.commit()
        second_snapshot = client.get_document_snapshot(document.id)
        assert second_snapshot["resources"] != snapshot["resources"]
        assert len(document.settings["semantic_export_resources_v1"]) == 2
        # The first pinned request remains resolvable after a later snapshot.
        retained_html = client.semantic_export(
            {**base, "format": "html", "include_source_toc": True}
        )["data"]
        assert retained_html == html_result["data"]
        db.session.commit()
        client.get_document_snapshot(document.id)
        assert not inspect(document).attrs.settings.history.has_changes()
        epub_result = client.semantic_export(
            {**base, "format": "epub", "include_source_toc": False}
        )
        with zipfile.ZipFile(io.BytesIO(epub_result["data"])) as archive:
            nav = archive.read("EPUB/nav.xhtml").decode("utf-8")
            content = "".join(
                archive.read(name).decode("utf-8")
                for name in archive.namelist()
                if name.startswith("EPUB/")
                and name.endswith(".xhtml")
                and name != "EPUB/nav.xhtml"
            )
        assert "Chapter" in nav
        assert "Old contents" not in content
        (uploads / "figure.png").write_bytes(b"changed")
        with pytest.raises(ValueError, match="resource-changed"):
            client.semantic_export(
                {**base, "format": "html", "include_source_toc": True}
            )
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def _semantic_export_base(html: str) -> dict:
    return {
        "contract_version": "semantic-export-v1",
        "semantic_contract_version": SEMANTIC_HTML_CONTRACT_VERSION,
        "semantic_html": html,
        "semantic_html_sha256": hashlib.sha256(html.encode("utf-8")).hexdigest(),
        "metadata": {},
        "resources": [],
        "profile_id": "semantic-default",
        "profile_version": "1",
        "include_source_toc": False,
    }


def _create_semantic_export_document():
    db.create_all()
    db.session.add(User(id=1, email="owner@example.test", password_hash="test"))
    document = ProcessedDocument(
        id="doc-runtime-export",
        user_id=1,
        filename="source.html",
        file_path="/uploads/source.html",
        status="completed",
        output_html="<main><h1>Title</h1><p>Content.</p></main>",
        settings={},
    )
    db.session.add(document)
    db.session.commit()
    return document


def test_semantic_export_docx_uses_owned_runtime_and_skips_it_for_other_formats(
    tmp_path,
):
    app = _make_app(tmp_path)
    calls = []
    document_xml = (
        b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        b'<w:body><w:p><w:r><w:t>Title</w:t></w:r></w:p></w:body></w:document>'
    )
    styles_xml = (
        b'<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        b'<w:style w:type="paragraph" w:styleId="Heading2"/></w:styles>'
    )
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w") as archive:
        archive.writestr("word/document.xml", document_xml)
        archive.writestr("word/styles.xml", styles_xml)
        archive.writestr("docProps/core.xml", b"<core/>")
    artifact_data = archive_buffer.getvalue()

    def run_managed_process(argv, **kwargs):
        calls.append((argv, kwargs))
        output = argv[argv.index("--output") + 1]
        Path(output).write_bytes(artifact_data)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    with app.app_context():
        document = _create_semantic_export_document()
        context = SimpleNamespace(run_managed_process=run_managed_process)
        client = DocProcessorClient(context, {"current_user_id": 1})
        snapshot = client.get_document_snapshot(document.id)
        base = _semantic_export_base(snapshot["output_html"])
        base["semantic_contract_version"] = snapshot["semantic_contract_version"]
        base["semantic_html_sha256"] = snapshot["content_hash"]

        docx_result = client.semantic_export({**base, "format": "docx"})

        with zipfile.ZipFile(io.BytesIO(docx_result["data"])) as archive:
            assert b"Title" in archive.read("word/document.xml")
            assert b"pageBreakBefore" in archive.read("word/styles.xml")
            assert archive.read("docProps/core.xml") == b"<core/>"
        receipt = docx_result["artifact"]
        assert receipt["byte_length"] == len(docx_result["data"])
        assert receipt["sha256"] == hashlib.sha256(docx_result["data"]).hexdigest()
        assert len(calls) == 1
        argv, kwargs = calls[0]
        assert argv == [
            "pandoc",
            "--from=html",
            "--to=docx",
            argv[3],
            "--output",
            argv[5],
            "--quiet",
            "--resource-path",
            argv[8],
        ]
        assert argv[3].startswith("--reference-doc=")
        assert Path(argv[3].split("=", 1)[1]).name == "reference.docx"
        assert Path(argv[3].split("=", 1)[1]).is_file()
        assert Path(argv[5]).parent == Path(argv[8])
        assert Path(argv[5]).name == "export.docx"
        assert set(kwargs) == {
            "runtime_unit",
            "input_text",
            "timeout_seconds",
            "check",
        }
        assert kwargs["runtime_unit"] == "document_processing"
        assert kwargs["timeout_seconds"] == 120
        assert kwargs["check"] is False
        assert kwargs["input_text"] == preprocess_html_for_docx(base["semantic_html"])

        client.semantic_export({**base, "format": "html"})
        client.semantic_export({**base, "format": "epub"})
        assert len(calls) == 1

        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def test_semantic_export_docx_requires_owned_runtime_executor(tmp_path):
    app = _make_app(tmp_path)

    with app.app_context():
        document = _create_semantic_export_document()
        client = DocProcessorClient(None, {"current_user_id": 1})
        snapshot = client.get_document_snapshot(document.id)
        base = _semantic_export_base(snapshot["output_html"])
        base["semantic_contract_version"] = snapshot["semantic_contract_version"]
        base["semantic_html_sha256"] = snapshot["content_hash"]

        with pytest.raises(
            SemanticExportError, match="semantic-export-docx-unavailable"
        ):
            client.semantic_export({**base, "format": "docx"})

        db.session.remove()
        db.drop_all()
        db.engine.dispose()


@pytest.mark.parametrize("failure", ["nonzero", "missing", "timeout", "runtime", "os"])
def test_semantic_export_docx_failures_are_source_free_and_clean_temporary_files(
    tmp_path, failure
):
    app = _make_app(tmp_path)
    marker = f"private-converter-marker-{failure}"
    temporary_directories = []

    def run_managed_process(argv, **kwargs):
        temporary_directories.append(Path(argv[argv.index("--output") + 1]).parent)
        if failure == "nonzero":
            return subprocess.CompletedProcess(argv, 1, stdout=marker, stderr=marker)
        if failure == "missing":
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 120, output=marker, stderr=marker)
        if failure == "runtime":
            raise RuntimeError(marker)
        raise OSError(marker)

    with app.app_context():
        document = _create_semantic_export_document()
        client = DocProcessorClient(
            SimpleNamespace(run_managed_process=run_managed_process),
            {"current_user_id": 1},
        )
        snapshot = client.get_document_snapshot(document.id)
        base = _semantic_export_base(snapshot["output_html"])
        base["semantic_contract_version"] = snapshot["semantic_contract_version"]
        base["semantic_html_sha256"] = snapshot["content_hash"]

        with pytest.raises(
            SemanticExportError, match="semantic-export-docx-conversion-failed"
        ) as raised:
            client.semantic_export({**base, "format": "docx"})

        formatted = "".join(
            traceback.format_exception(
                type(raised.value), raised.value, raised.value.__traceback__
            )
        )
        assert marker not in str(raised.value)
        assert marker not in formatted
        assert temporary_directories
        assert not temporary_directories[0].exists()

        db.session.remove()
        db.drop_all()
        db.engine.dispose()
