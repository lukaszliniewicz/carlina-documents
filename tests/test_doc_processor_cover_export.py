from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import fitz
import pytest
from flask import Flask
from PIL import Image

from carlina.core.epub_cover import normalize_epub_cover
from carlina.core.semantic_export import SemanticExportRequest, SemanticExportResource
from iarf_assistant.addons.doc_processor import models as doc_models
from iarf_assistant.addons.doc_processor import routes, semantic_export
from iarf_assistant.addons.doc_processor.cover_source import owned_pdf_source
from iarf_assistant.addons.doc_processor.epub_packager import EPUBPackager
from iarf_assistant.addons.doc_processor.semantic_export import (
    DocumentResourceResolver,
    SemanticExportError,
    capture_snapshot_resources,
    export_semantic_document,
)

OPF_NS = {
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
}
XHTML_NS = {"xhtml": "http://www.w3.org/1999/xhtml"}


def _png(path: Path, color: str) -> bytes:
    Image.new("RGB", (32, 24), color).save(path, format="PNG")
    return path.read_bytes()


def _epub_document(
    html: str,
    *,
    title: str = "Main & Title",
    authors: list[str] | None = None,
) -> SimpleNamespace:
    metadata = {
        "title": title,
        "language": "en",
        "contributors": {"authors": authors or ["Ada Author"]},
    }
    return SimpleNamespace(
        id="doc-epub-cover",
        filename="fixture.pdf",
        file_path=None,
        settings={"document_metadata": metadata},
        metadata_record=None,
        output_html=html,
        output_markdown=None,
    )


def _build_epub(document, output: Path, *, cover: object = None, roots=()) -> Path:
    with Flask(__name__).app_context():
        EPUBPackager(
            document,
            str(output),
            cover=cover,
            allowed_resource_roots=list(roots),
        ).build()
    return output


def _package_parts(archive: zipfile.ZipFile):
    package = ET.fromstring(archive.read("EPUB/package.opf"))
    manifest = {
        str(item.get("id")): item
        for item in package.findall("opf:manifest/opf:item", OPF_NS)
    }
    spine = [
        manifest[str(item.get("idref"))]
        for item in package.findall("opf:spine/opf:itemref", OPF_NS)
    ]
    content = [
        ET.fromstring(archive.read(f"EPUB/{item.get('href')}"))
        for item in spine
        if item.get("media-type") == "application/xhtml+xml"
    ]
    return package, manifest, spine, content


def _note_link_signature(
    roots: list[ET.Element],
) -> tuple[set[str], set[tuple[str, str]]]:
    ids = {
        str(node.get("id")) for root in roots for node in root.iter() if node.get("id")
    }
    links = {
        (str(node.get("role")), str(node.get("href")).rsplit("#", 1)[-1])
        for root in roots
        for node in root.iter()
        if node.get("role") in {"doc-noteref", "doc-backlink"}
    }
    return ids, links


def test_normalize_epub_cover_accepts_only_the_closed_mode_shapes():
    assert normalize_epub_cover() == {}
    assert normalize_epub_cover(None) == {}
    assert normalize_epub_cover({"mode": "existing"}) == {}
    assert normalize_epub_cover({"mode": "generated"}) == {"mode": "generated"}
    assert normalize_epub_cover({"mode": "pdf_page", "page": 3}) == {
        "mode": "pdf_page",
        "page": 3,
    }


@pytest.mark.parametrize(
    "value",
    [
        {"mode": "unknown"},
        {"mode": "generated", "page": 1},
        {"mode": "existing", "page": 1},
        {"mode": "pdf_page"},
        {"mode": "pdf_page", "page": 0},
        {"mode": "pdf_page", "page": -1},
        {"mode": "pdf_page", "page": True},
        {"mode": "pdf_page", "page": "2"},
        {"mode": "pdf_page", "page": 2.0},
        {"mode": "generated", "unexpected": True},
        {"mode": "pdf_page", "page": 2, "path": "/private/source.pdf"},
        "generated",
    ],
)
def test_normalize_epub_cover_rejects_ambiguous_or_non_strict_values(value):
    with pytest.raises(ValueError):
        normalize_epub_cover(value)


def test_packager_keeps_default_source_cover_and_replaces_it_with_separate_generated_cover(
    tmp_path: Path,
):
    legacy_cover = tmp_path / "legacy-cover.png"
    body_image = tmp_path / "body-image.png"
    legacy_bytes = _png(legacy_cover, "red")
    body_bytes = _png(body_image, "blue")
    source_html = (
        "<main><figure class='cover' role='doc-cover'><img class='cover' src='"
        f"{legacy_cover.as_posix()}"
        "' alt='Original cover'/></figure><h1 id='book-title'>Main &amp; Title</h1>"
        "<h2>Chapter One</h2><p id='ref1'>Text "
        '<a href="#fn1" role="doc-noteref">1</a></p>'
        '<section id="notes" role="doc-endnotes"><ol>'
        '<li id="fn1" role="doc-endnote">Note '
        '<a href="#ref1" role="doc-backlink">Return</a></li>'
        "</ol></section><img src='"
        f"{body_image.as_posix()}"
        "' alt='Body image'/></main>"
    )
    baseline_document = _epub_document(source_html)
    generated_document = _epub_document(source_html)
    baseline_path = _build_epub(
        baseline_document, tmp_path / "existing-cover.epub", roots=[tmp_path]
    )
    generated_path = _build_epub(
        generated_document,
        tmp_path / "generated-cover.epub",
        cover={"mode": "generated"},
        roots=[tmp_path],
    )

    assert baseline_document.output_html == source_html
    assert generated_document.output_html == source_html
    with zipfile.ZipFile(baseline_path) as baseline:
        _package, baseline_manifest, _spine, baseline_content = _package_parts(baseline)
        baseline_covers = [
            item
            for item in baseline_manifest.values()
            if "cover-image" in str(item.get("properties") or "").split()
        ]
        assert len(baseline_covers) == 1
        baseline_cover_bytes = baseline.read(f"EPUB/{baseline_covers[0].get('href')}")
        with Image.open(io.BytesIO(baseline_cover_bytes)) as image:
            assert image.convert("RGB").getpixel((0, 0)) == (255, 0, 0)
        assert (
            baseline_content[0].find(".//xhtml:img[@alt='Original cover']", XHTML_NS)
            is not None
        )

    with zipfile.ZipFile(generated_path) as generated:
        _package, manifest, spine, content = _package_parts(generated)
        cover_items = [
            item
            for item in manifest.values()
            if "cover-image" in str(item.get("properties") or "").split()
        ]
        assert len(cover_items) == 1
        assert cover_items[0].get("media-type") == "image/png"
        assert len(spine) >= 2
        assert content[0].find(".//xhtml:figure[@class='cover']", XHTML_NS) is not None
        assert content[0].find(".//xhtml:img", XHTML_NS) is not None
        assert content[0].find("xhtml:head/xhtml:style", XHTML_NS) is not None
        assert all(
            root.find("xhtml:head/xhtml:style", XHTML_NS) is None
            for root in content[1:]
        )
        assert any("Chapter One" in " ".join(root.itertext()) for root in content[1:])

        cover_bytes = generated.read(f"EPUB/{cover_items[0].get('href')}")
        with Image.open(io.BytesIO(cover_bytes)) as image:
            assert image.format == "PNG"
            assert image.size == (1200, 1800)
            assert image.mode == "RGB"

        image_resources = [
            item
            for item in manifest.values()
            if str(item.get("media-type") or "").startswith("image/")
        ]
        archived_images = [
            generated.read(f"EPUB/{item.get('href')}") for item in image_resources
        ]
        assert len(image_resources) == 2
        assert body_bytes in archived_images
        assert legacy_bytes not in archived_images

        ids, links = _note_link_signature(content)
        baseline_ids, baseline_links = _note_link_signature(baseline_content)
        assert {"book-title", "ref1", "fn1"} <= ids
        assert {"book-title", "ref1", "fn1"} <= baseline_ids
        assert links == baseline_links
        assert ("doc-noteref", "fn1") in links
        assert ("doc-backlink", "ref1") in links


class _ExportQueue:
    def __init__(self):
        self.jobs: list[tuple[str, dict[str, object]]] = []

    def enqueue(self, task_name: str, payload: dict[str, object]) -> str:
        self.jobs.append((task_name, payload))
        return "cover-job"


def _call_export_route(monkeypatch, query: str, body=None):
    app = Flask(__name__)
    app.register_blueprint(routes.blueprint)
    queue = _ExportQueue()
    document = SimpleNamespace(
        id="doc-route-cover",
        filename="Fixture.pdf",
        settings={},
        output_html="<p>Fixture</p>",
        output_markdown=None,
    )
    monkeypatch.setattr(routes, "current_user", SimpleNamespace(id=7))
    monkeypatch.setattr(routes, "_get_user_document", lambda _document_id: document)
    monkeypatch.setattr(routes, "_enqueue_export_job", queue.enqueue)
    with app.test_request_context(
        f"/addons/doc_processor/api/documents/doc-route-cover/export?{query}",
        method="POST" if body is not None else "GET",
        json=body,
    ):
        response, status = routes.export_document.__wrapped__("doc-route-cover")
    return response, status, queue


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("format=epub&epub_cover_mode=generated", {"mode": "generated"}),
        (
            "format=epub&epub_cover_mode=pdf_page&epub_cover_page=2",
            {"mode": "pdf_page", "page": 2},
        ),
    ],
)
def test_export_route_queues_epub_cover_choice_only_when_selected(
    monkeypatch, query: str, expected: dict[str, object]
):
    _response, status, queue = _call_export_route(monkeypatch, query)
    assert status == 202
    assert queue.jobs[0][0] == "export_document"
    assert queue.jobs[0][1]["epub_cover"] == expected


def test_export_route_default_payload_has_no_cover_key(monkeypatch):
    _response, status, queue = _call_export_route(monkeypatch, "format=epub")
    assert status == 202
    assert "epub_cover" not in queue.jobs[0][1]


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("auto", "auto"),
        ("jpeg", "jpeg"),
        ("webp", "webp"),
        ("true", "webp"),
        ("false", "auto"),
        ("preserve", "auto"),
    ],
)
def test_export_route_forwards_image_mode_and_retains_aliases(
    monkeypatch, mode, expected
):
    _response, status, queue = _call_export_route(
        monkeypatch, f"format=epub&epub_image_mode={mode}"
    )
    assert status == 202
    assert queue.jobs[0][1]["epub_image_mode"] == expected


def test_export_route_forwards_json_image_mode(monkeypatch):
    _response, status, queue = _call_export_route(
        monkeypatch, "format=epub", {"epub_image_mode": "jpeg"}
    )
    assert status == 202
    assert queue.jobs[0][1]["epub_image_mode"] == "jpeg"


@pytest.mark.parametrize("mode", [None, "JPEG", "preserve", True, 1, []])
def test_export_route_rejects_invalid_json_image_mode(monkeypatch, mode):
    _response, status, queue = _call_export_route(
        monkeypatch, "format=epub", {"epub_image_mode": mode}
    )
    assert status == 400
    assert queue.jobs == []


@pytest.mark.parametrize(
    "query",
    [
        "format=epub&epub_cover_mode=unknown",
        "format=epub&epub_cover_mode=pdf_page",
        "format=epub&epub_cover_mode=pdf_page&epub_cover_page=0",
        "format=epub&epub_cover_mode=pdf_page&epub_cover_page=1.5",
        "format=epub&epub_cover_mode=generated&epub_cover_mode=existing",
        "format=epub&epub_cover_mode=pdf_page&epub_cover_page=1&epub_cover_page=2",
        "format=docx&epub_cover_mode=generated",
    ],
)
def test_export_route_rejects_invalid_duplicate_or_non_epub_cover_choices(
    monkeypatch, query: str
):
    _response, status, queue = _call_export_route(monkeypatch, query)
    assert status == 400
    assert queue.jobs == []


def test_owned_pdf_source_is_owner_scoped_to_uploads_and_pdf_files(
    tmp_path: Path,
):
    uploads = tmp_path / "uploads"
    owner_docs = uploads / "7" / "doc_processor_docs"
    owner_docs.mkdir(parents=True)
    pdf_path = owner_docs / "source.pdf"
    pdf_path.write_bytes(_make_pdf_bytes())
    non_pdf_path = owner_docs / "source.txt"
    non_pdf_path.write_text("synthetic", encoding="utf-8")
    foreign_pdf = uploads / "8" / "doc_processor_docs" / "foreign.pdf"
    foreign_pdf.parent.mkdir(parents=True)
    foreign_pdf.write_bytes(_make_pdf_bytes())
    app = Flask(__name__)
    app.config["CARLINA_UPLOADS_DIR"] = str(uploads)

    document = SimpleNamespace(
        user_id=7,
        file_path="/uploads/7/doc_processor_docs/source.pdf",
    )
    with app.app_context():
        assert owned_pdf_source(document, user_id=7) == pdf_path.resolve()
        for invalid in (
            SimpleNamespace(
                user_id=7, file_path="/uploads/7/doc_processor_docs/missing.pdf"
            ),
            SimpleNamespace(
                user_id=7, file_path="/uploads/7/doc_processor_docs/source.txt"
            ),
            SimpleNamespace(
                user_id=8, file_path="/uploads/8/doc_processor_docs/foreign.pdf"
            ),
            SimpleNamespace(
                user_id=7, file_path="/uploads/8/doc_processor_docs/foreign.pdf"
            ),
        ):
            with pytest.raises(ValueError, match="source PDF is unavailable"):
                owned_pdf_source(invalid, user_id=7)

        escape = owner_docs / "escape.pdf"
        try:
            escape.symlink_to(foreign_pdf)
        except (NotImplementedError, OSError) as exc:
            pytest.skip(f"symlink creation is unavailable: {exc}")
        with pytest.raises(ValueError, match="source PDF is unavailable"):
            owned_pdf_source(
                SimpleNamespace(
                    user_id=7, file_path="/uploads/7/doc_processor_docs/escape.pdf"
                ),
                user_id=7,
            )


def _make_pdf_bytes() -> bytes:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Synthetic cover source")
    data = document.tobytes()
    document.close()
    return data


class _DocumentQuery:
    def __init__(self, document):
        self.document = document
        self.filters: dict[str, object] = {}

    def filter_by(self, **filters):
        self.filters = filters
        return self

    def first(self):
        if self.document is None:
            return None
        if str(getattr(self.document, "id", "")) == str(self.filters.get("id")) and int(
            getattr(self.document, "user_id", -1)
        ) == int(self.filters.get("user_id", -2)):
            return self.document
        return None


def _resolver_fixture(tmp_path: Path, monkeypatch):
    uploads = tmp_path / "uploads"
    doc_root = uploads / "7" / "doc_processor_docs"
    doc_root.mkdir(parents=True)
    pdf_path = doc_root / "source.pdf"
    pdf_bytes = _make_pdf_bytes()
    pdf_path.write_bytes(pdf_bytes)
    image_path = doc_root / "figure.png"
    _png(image_path, "purple")
    source_html = (
        '<main><h1 id="source-title">Source Book</h1><img alt="Figure" src="'
        "/uploads/7/doc_processor_docs/figure.png"
        '"/></main>'
    )
    document = SimpleNamespace(
        id="owned-doc-7",
        user_id=7,
        file_path="/uploads/7/doc_processor_docs/source.pdf",
        output_html=source_html,
        settings={},
    )
    app = Flask(__name__)
    app.config["CARLINA_UPLOADS_DIR"] = str(uploads)
    monkeypatch.setattr(semantic_export, "flag_modified", lambda *_args: None)
    with app.app_context():
        resources, opaque_html = capture_snapshot_resources(document)
    request_resources = tuple(
        SemanticExportResource.from_dict(resource) for resource in resources
    )
    pdf_digest = hashlib.sha256(pdf_bytes).hexdigest()
    snapshot = {
        "document-id": document.id,
        "content-hash": hashlib.sha256(opaque_html.encode("utf-8")).hexdigest(),
        "pdf-sha256": pdf_digest,
    }
    request = SemanticExportRequest(
        semantic_html=opaque_html,
        semantic_html_sha256=hashlib.sha256(opaque_html.encode("utf-8")).hexdigest(),
        metadata={"source-snapshot": snapshot},
        resources=request_resources,
        format="epub",
        include_source_toc=False,
        profile_id="cover-test",
        profile_version="1",
    )
    return app, document, request, pdf_digest


def test_pdf_cover_resolver_binds_owned_document_and_frozen_opaque_html(
    tmp_path: Path, monkeypatch
):
    app, document, request, _digest = _resolver_fixture(tmp_path, monkeypatch)
    query = _DocumentQuery(document)
    monkeypatch.setattr(doc_models, "ProcessedDocument", SimpleNamespace(query=query))
    resolver = DocumentResourceResolver(user_id=7)

    with app.app_context():
        png = resolver.resolve_pdf_cover(request, 1)

    assert query.filters == {"id": document.id, "user_id": 7}
    with Image.open(io.BytesIO(png)) as image:
        assert image.format == "PNG"
        assert image.mode == "RGB"


@pytest.mark.parametrize("lookup", ["missing", "foreign"])
def test_pdf_cover_resolver_refuses_missing_or_foreign_owned_document(
    tmp_path: Path, monkeypatch, lookup: str
):
    app, document, request, _digest = _resolver_fixture(tmp_path, monkeypatch)
    if lookup == "missing":
        query = _DocumentQuery(None)
    else:
        foreign = SimpleNamespace(**vars(document))
        foreign.user_id = 8
        query = _DocumentQuery(foreign)
    monkeypatch.setattr(doc_models, "ProcessedDocument", SimpleNamespace(query=query))
    resolver = DocumentResourceResolver(user_id=7)

    with app.app_context(), pytest.raises(SemanticExportError, match="cover-source"):
        resolver.resolve_pdf_cover(request, 1)


def test_pdf_cover_resolver_rejects_changed_html_and_frozen_pdf_digest_mismatch(
    tmp_path: Path, monkeypatch
):
    app, document, request, _digest = _resolver_fixture(tmp_path, monkeypatch)
    query = _DocumentQuery(document)
    monkeypatch.setattr(doc_models, "ProcessedDocument", SimpleNamespace(query=query))
    resolver = DocumentResourceResolver(user_id=7)

    document.output_html += "<p>Edited after snapshot</p>"
    with (
        app.app_context(),
        pytest.raises(SemanticExportError, match="cover-source-changed"),
    ):
        resolver.resolve_pdf_cover(request, 1)
    document.output_html = document.output_html.removesuffix(
        "<p>Edited after snapshot</p>"
    )

    snapshot = dict(request.metadata["source-snapshot"])
    snapshot["pdf-sha256"] = "0" * 64
    mismatched_request = SemanticExportRequest(
        semantic_html=request.semantic_html,
        semantic_html_sha256=request.semantic_html_sha256,
        metadata={"source-snapshot": snapshot},
        resources=request.resources,
        format="epub",
        include_source_toc=False,
        profile_id="cover-test",
        profile_version="1",
    )
    with app.app_context(), pytest.raises(ValueError, match="PDF source hash mismatch"):
        resolver.resolve_pdf_cover(mismatched_request, 1)


def test_semantic_generated_cover_exports_without_resolving_pdf_and_keeps_translation_metadata(
    tmp_path: Path,
):
    class NoSourceResolver:
        def resolve(self, _resource):
            raise AssertionError("generated cover must not resolve source resources")

        def resolve_pdf_cover(self, _request, _page):
            raise AssertionError("generated cover must not resolve the source PDF")

    html = "<main><h1>Translated chapter</h1><p>Translated body.</p></main>"
    request = SemanticExportRequest(
        semantic_html=html,
        semantic_html_sha256=hashlib.sha256(html.encode("utf-8")).hexdigest(),
        metadata={
            "title": "Translated Title",
            "contributors": {"authors": ["Translated Author"]},
            "epub-cover": {"mode": "generated"},
        },
        resources=(),
        format="epub",
        include_source_toc=False,
        profile_id="translated-cover-test",
        profile_version="1",
    )
    artifact = export_semantic_document(request, resolver=NoSourceResolver())
    output = tmp_path / "translated.epub"
    output.write_bytes(artifact.data)

    with zipfile.ZipFile(output) as archive:
        package, manifest, spine, content = _package_parts(archive)
        cover_items = [
            item
            for item in manifest.values()
            if "cover-image" in str(item.get("properties") or "").split()
        ]
        assert len(cover_items) == 1
        assert cover_items[0].get("media-type") == "image/png"
        assert content[0].find(".//xhtml:figure[@class='cover']", XHTML_NS) is not None
        cover_bytes = archive.read(f"EPUB/{cover_items[0].get('href')}")
        with Image.open(io.BytesIO(cover_bytes)) as image:
            assert image.format == "PNG"
        assert [
            node.text for node in package.findall("opf:metadata/dc:title", OPF_NS)
        ] == ["Translated Title"]
        assert [
            node.text for node in package.findall("opf:metadata/dc:creator", OPF_NS)
        ] == ["Translated Author"]
        assert spine
