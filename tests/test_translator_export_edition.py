import base64
import hashlib
import io
import json
import shutil
import subprocess
import zipfile
from types import SimpleNamespace
from unittest.mock import patch
from xml.etree import ElementTree as ET

import pytest
from bs4 import BeautifulSoup
from PIL import Image

from carlina.core.semantic_export import SemanticExportResource
from iarf_assistant.addons.doc_processor.semantic_export import export_semantic_document
from iarf_assistant.addons.translator import export_cover
from iarf_assistant.addons.translator.block_export import (
    BlockExportError,
    build_semantic_export_request,
)
from iarf_assistant.addons.translator.export_cover import (
    ExportCoverError,
    capture_export_cover,
    pin_export_metadata_cover,
    resolve_export_cover,
)

OPF_NS = {
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
}
XHTML_NS = {
    "xhtml": "http://www.w3.org/1999/xhtml",
    "epub": "http://www.idpf.org/2007/ops",
}


def _png_bytes(color: str, size: tuple[int, int] = (20, 14)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


def _source_metadata() -> dict[str, object]:
    return {
        "title": "Старое русское издание",
        "subtitle": "Старый подзаголовок",
        "language": "ru",
        "contributors": {
            "authors": ["Старый автор"],
            "translators": ["Старый переводчик"],
            "editors": ["Старый редактор"],
            "other": [{"name": "Старый художник", "role": "ill"}],
        },
        "publisher": "Старое издательство",
        "publication_date": "1999-01-02",
        "rights": "Старые права",
        "description": "Старое описание",
        "subjects": ["Старая тема"],
        "journal_or_series": "Старая серия",
        "volume": "9",
        "issue": "2",
        "isbn": "OLD-ISBN",
        "identifiers": [{"scheme": "ISBN", "value": "OLD-ISBN"}],
        "source": "source-witness",
        "cover_image_id": "source-cover",
    }


def _source_html(resource_ref: str) -> str:
    return (
        '<html lang="ru"><head><meta charset="utf-8">'
        "<title>Original source title</title></head><body><main>"
        '<h1 id="book-title">Translated body heading</h1>'
        '<figure id="cover-figure" class="cover" role="doc-cover">'
        f'<img id="source-cover" src=".carlina-resources/{resource_ref}" '
        'alt="Old source cover"></figure>'
        '<p id="body-paragraph">Translated paragraph '
        '<sup><a id="ref-1" href="#note-1" role="doc-noteref" '
        'epub:type="noteref" data-footnote-label="1">1</a></sup></p>'
        '<section id="notes" role="doc-endnotes" epub:type="endnotes">'
        '<h2 id="notes-heading">Notes</h2><ol><li id="note-1" '
        'role="doc-endnote" epub:type="endnote"><p id="note-body">'
        'Note body <a href="#body-paragraph" epub:type="backlink">'
        "Back to paragraph</a></p></li></ol></section>"
        "</main></body></html>"
    )


def _plan_record(source_html: str, source_metadata, resource):
    content_hash = hashlib.sha256(source_html.encode("utf-8")).hexdigest()
    snapshot = SimpleNamespace(
        id="document-1",
        document_id="document-1",
        content_hash=content_hash,
        semantic_content_hash=hashlib.sha256(
            f"semantic:{content_hash}".encode()
        ).hexdigest(),
        metadata=source_metadata,
        provenance={"knowledge_source_id": "source-1"},
        resources=(resource,) if resource is not None else (),
        filename="book.html",
        captured_at="2026-09-08T10:00:00+00:00",
        source_updated_at="2026-09-07T10:00:00+00:00",
    )
    plan = SimpleNamespace(plan_hash="e" * 64, snapshot=snapshot)
    record = SimpleNamespace(
        id="plan-1",
        plan_hash=plan.plan_hash,
        source_content_hash=content_hash,
        source_document_id=snapshot.document_id,
        project=SimpleNamespace(target_lang="en"),
    )
    return record, plan


def _build_request(record, plan, source_html, metadata, *, export_format, cover_uri):
    with (
        patch(
            "iarf_assistant.addons.translator.block_export.rehydrate_source_plan",
            return_value=plan,
        ),
        patch(
            "iarf_assistant.addons.translator.block_export.render_persisted_target_document",
            return_value=source_html,
        ),
    ):
        return build_semantic_export_request(
            record,
            export_format=export_format,
            export_metadata=metadata,
            cover_uri=cover_uri,
        )


class _SyntheticResolver:
    def __init__(self, resource, data: bytes):
        self.resource = resource
        self.data = data
        self.calls = []

    def resolve(self, resource):
        self.calls.append(resource)
        if resource == self.resource:
            return self.data
        raise AssertionError("unexpected semantic resource")


def test_image_heavy_epub_keeps_assets_and_omits_internal_source_evidence():
    images = [_png_bytes(f"#{index:02x}2040") for index in range(32)]
    resources = tuple(
        SemanticExportResource(
            ref=f"res_{index:016d}",
            media_type="image/png",
            filename=f"figure-{index}.png",
            byte_length=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            owner_attestation="b" * 64,
        )
        for index, data in enumerate(images)
    )
    manifest_json = json.dumps([resource.to_dict() for resource in resources])
    assert len(manifest_json.encode()) > 4096
    html = "<main><h1 id='chapter'>Image-heavy translation</h1><p>Preserved text.</p>"
    html += "".join(
        f"<figure><img src='.carlina-resources/{resource.ref}' alt='Figure {index}'></figure>"
        for index, resource in enumerate(resources)
    )
    html += "</main>"
    source_metadata = {
        "title": "Source title",
        "internal_evidence": "PRIVATE-SOURCE-EVIDENCE",
    }
    source_metadata_before = json.dumps(source_metadata, sort_keys=True)
    record, plan = _plan_record(html, source_metadata, None)
    plan.snapshot.resources = resources
    request = _build_request(
        record,
        plan,
        html,
        {"title": "English edition"},
        export_format="epub",
        cover_uri=None,
    )
    resolved = []

    class Resolver:
        def resolve(self, resource):
            resolved.append(resource)
            return images[resources.index(resource)]

    artifact = export_semantic_document(request.to_dict(), resolver=Resolver())
    assert resolved == list(resources)
    with zipfile.ZipFile(io.BytesIO(artifact.data)) as archive:
        package = ET.fromstring(archive.read("EPUB/package.opf"))
        image_items = [
            item
            for item in package.findall("opf:manifest/opf:item", OPF_NS)
            if item.get("media-type") == "image/png"
        ]
        assert len(image_items) == len(resources)
        assert {
            hashlib.sha256(archive.read(f"EPUB/{item.get('href')}")).hexdigest()
            for item in image_items
        } == {resource.sha256 for resource in resources}
        content = "\n".join(
            archive.read(name).decode()
            for name in archive.namelist()
            if name.endswith((".xhtml", ".opf", ".ncx"))
        )
        assert "English edition" in content
        assert "Preserved text." in content
        for internal_value in (
            "PRIVATE-SOURCE-EVIDENCE",
            "source-metadata-sha256",
            "source-snapshot",
            "owner_attestation",
            "resources-json",
            request.metadata["source-metadata-sha256"],
        ):
            assert internal_value not in content
    assert json.dumps(plan.snapshot.metadata, sort_keys=True) == source_metadata_before
    assert plan.snapshot.resources == resources


@pytest.fixture
def upload_root(tmp_path, monkeypatch):
    root = tmp_path / "uploads"
    (root / "7").mkdir(parents=True)
    (root / "8").mkdir(parents=True)
    monkeypatch.setattr(export_cover, "uploads_root", lambda: root)
    return root


def _metadata_values(root, namespace, name):
    return [node.text for node in root.findall(f"{namespace}:{name}", OPF_NS)]


@pytest.mark.skipif(shutil.which("pandoc") is None, reason="Pandoc is unavailable")
def test_docx_preserves_edition_credits_subjects_language_and_cover(upload_root):
    (upload_root / "7" / "cover.png").write_bytes(_png_bytes("blue"))
    witness = capture_export_cover(user_id=7, upload_path="/uploads/7/cover.png")
    uri = resolve_export_cover(user_id=7, cover=witness)
    source_html = '<html lang="en"><head><title>Old</title></head><body><p>Translated body</p></body></html>'
    record, plan = _plan_record(source_html, _source_metadata(), None)
    request = _build_request(
        record,
        plan,
        source_html,
        {
            "title": "English edition",
            "authors": ["New Author", "Second Author"],
            "translators": ["First Translator", "Second Translator"],
            "subjects": ["History", "Translation"],
            "cover_upload_path": witness["upload_path"],
        },
        export_format="docx",
        cover_uri=uri,
    )

    def run_converter(argv, **kwargs):
        assert kwargs["runtime_unit"] == "document_processing"
        return subprocess.run(
            argv,
            input=kwargs["input_text"],
            text=True,
            capture_output=True,
            timeout=kwargs["timeout_seconds"],
            check=False,
        )

    artifact = export_semantic_document(
        request,
        resolver=_SyntheticResolver(None, b""),
        run_managed_process=run_converter,
    )
    with zipfile.ZipFile(io.BytesIO(artifact.data)) as archive:
        core = ET.fromstring(archive.read("docProps/core.xml"))
        dc = {"dc": "http://purl.org/dc/elements/1.1/"}
        assert core.find("dc:title", dc).text == "English edition"
        assert core.find("dc:creator", dc).text == "New Author; Second Author"
        assert core.find("dc:language", dc).text == "en"
        custom = ET.fromstring(archive.read("docProps/custom.xml"))
        values = {node.get("name"): "".join(node.itertext()) for node in custom}
        assert (
            values["DC.contributor.translator"] == "First Translator; Second Translator"
        )
        assert values["DC.subject"] == "History; Translation"
        assert "Translated body" in archive.read("word/document.xml").decode()
        cover_bytes = base64.b64decode(uri.split(",", 1)[1])
        assert cover_bytes in [
            archive.read(name)
            for name in archive.namelist()
            if name.startswith("word/media/")
        ]


def test_translation_export_applies_edition_metadata_cover_and_preserves_body(
    tmp_path, upload_root, monkeypatch
):
    old_cover_bytes = _png_bytes("red", (12, 12))
    replacement_source = upload_root / "7" / "replacement.png"
    replacement_bytes = _png_bytes("blue", (26, 18))
    replacement_source.write_bytes(replacement_bytes)
    source_upload_before = replacement_source.read_bytes()

    resource_ref = "res_abcdefghijklmnop"
    resource = SemanticExportResource(
        ref=resource_ref,
        media_type="image/png",
        filename="old-cover.png",
        byte_length=len(old_cover_bytes),
        sha256=hashlib.sha256(old_cover_bytes).hexdigest(),
        owner_attestation="b" * 64,
    )
    source_metadata = _source_metadata()
    source_metadata_before = json.loads(json.dumps(source_metadata, ensure_ascii=False))
    source_html = _source_html(resource_ref)
    source_html_before = source_html
    record, plan = _plan_record(source_html, source_metadata, resource)

    pinned = pin_export_metadata_cover(
        {"cover_upload_path": "/uploads/7/replacement.png"}, user_id=7
    )
    cover_witness = capture_export_cover(
        user_id=7, upload_path="/uploads/7/replacement.png"
    )
    assert pinned["cover_upload_path"] == cover_witness["upload_path"]
    cover_uri = resolve_export_cover(user_id=7, cover=cover_witness)
    replacement_jpeg = base64.b64decode(
        cover_uri.removeprefix("data:image/jpeg;base64,"), validate=True
    )

    export_metadata = {
        "title": "English edition",
        "subtitle": "A translated book",
        "description": "English description",
        "publisher": "New Press",
        "publication_date": "2025-03-04",
        "rights": "CC BY 4.0",
        "journal_or_series": "English Series",
        "volume": "2",
        "authors": ["New Author"],
        "translators": ["New Translator"],
        "editors": ["New Editor"],
        "subjects": ["History", "Translation"],
        "other_contributors": [{"name": "New Illustrator", "role": "ill"}],
        "identifiers": [{"scheme": "DOI", "value": "10.1234/new-edition"}],
        "cover_upload_path": pinned["cover_upload_path"],
    }
    html_request = _build_request(
        record,
        plan,
        source_html,
        export_metadata,
        export_format="html",
        cover_uri=cover_uri,
    )
    epub_request = _build_request(
        record,
        plan,
        source_html,
        export_metadata,
        export_format="epub",
        cover_uri=cover_uri,
    )

    assert html_request.resources == ()
    assert epub_request.resources == ()
    assert html_request.metadata["language"] == "en"
    assert html_request.metadata["title"] == "English edition"
    assert html_request.to_dict()["metadata"]["contributors"] == {
        "authors": ["New Author"],
        "translators": ["New Translator"],
        "editors": ["New Editor"],
        "other": [{"name": "New Illustrator", "role": "ill"}],
    }
    assert "cover_upload_path" not in html_request.to_dict()["metadata"]
    assert (
        html_request.metadata["source-metadata-sha256"]
        == hashlib.sha256(
            json.dumps(
                source_metadata,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )
    assert html_request.metadata["source-snapshot"]["resources-count"] == 1
    assert (
        html_request.metadata["source-snapshot"]["resources-sha256"]
        == hashlib.sha256(
            json.dumps(
                [resource.to_dict()],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )
    assert source_metadata == source_metadata_before
    assert source_html == source_html_before
    assert replacement_source.read_bytes() == source_upload_before

    html_artifact = export_semantic_document(
        html_request,
        resolver=_SyntheticResolver(resource, old_cover_bytes),
    )
    rendered_html = html_artifact.data.decode("utf-8")
    html_soup = BeautifulSoup(rendered_html, "html.parser")
    assert html_soup.title.string == "English edition"
    html_meta = {
        (node.get("name"), node.get("content"))
        for node in html_soup.find_all("meta", attrs={"name": True})
    }
    assert ("DC.language", "en") in html_meta
    assert ("DC.title.alternative", "A translated book") in html_meta
    assert ("description", "English description") in html_meta
    assert ("DC.publisher", "New Press") in html_meta
    assert ("DC.date", "2025-03-04") in html_meta
    assert ("DC.rights", "CC BY 4.0") in html_meta
    assert ("DC.relation.isPartOf", "English Series") in html_meta
    assert ("DC.relation.volume", "2") in html_meta
    assert ("DC.creator", "New Author") in html_meta
    assert ("DC.contributor.translator", "New Translator") in html_meta
    assert ("DC.contributor.editor", "New Editor") in html_meta
    assert ("DC.contributor.ill", "New Illustrator") in html_meta
    assert ("DC.subject", "History") in html_meta
    assert ("DC.subject", "Translation") in html_meta
    assert ("DC.identifier", "10.1234/new-edition") in html_meta
    assert cover_uri in rendered_html
    assert f".carlina-resources/{resource_ref}" not in rendered_html
    expected_paragraph = BeautifulSoup(
        '<p id="body-paragraph">Translated paragraph '
        '<sup><a id="ref-1" href="#note-1" role="doc-noteref" '
        'epub:type="noteref" data-footnote-label="1">1</a></sup></p>',
        "html.parser",
    ).p
    assert html_soup.find(id="body-paragraph") == expected_paragraph
    expected_note_body = BeautifulSoup(
        '<p id="note-body">Note body <a href="#body-paragraph" '
        'epub:type="backlink">Back to paragraph</a></p>',
        "html.parser",
    ).p
    assert html_soup.find(id="note-body") == expected_note_body

    monkeypatch.setattr(
        "iarf_assistant.addons.doc_processor.epub_packager.MAX_CONTENT_DOCUMENT_BYTES",
        1,
    )
    epub_artifact = export_semantic_document(
        epub_request,
        resolver=_SyntheticResolver(resource, old_cover_bytes),
    )
    with zipfile.ZipFile(io.BytesIO(epub_artifact.data)) as archive:
        package = ET.fromstring(archive.read("EPUB/package.opf"))
        content_names = sorted(
            name
            for name in archive.namelist()
            if name.startswith("EPUB/content-") and name.endswith(".xhtml")
        )
        contents = {name: ET.fromstring(archive.read(name)) for name in content_names}
        package_metadata = package.find("opf:metadata", OPF_NS)
        manifest = package.findall("opf:manifest/opf:item", OPF_NS)
        image_items = [
            item for item in manifest if item.get("media-type", "").startswith("image/")
        ]
        cover_item = next(
            item
            for item in image_items
            if "cover-image" in str(item.get("properties") or "").split()
        )
        packaged_cover = archive.read(f"EPUB/{cover_item.get('href')}")

    assert package_metadata is not None
    assert _metadata_values(package_metadata, "dc", "title") == [
        "English edition",
        "A translated book",
    ]
    assert _metadata_values(package_metadata, "dc", "language") == ["en"]
    assert _metadata_values(package_metadata, "dc", "creator") == ["New Author"]
    assert _metadata_values(package_metadata, "dc", "date") == ["2025-03-04"]
    assert _metadata_values(package_metadata, "dc", "publisher") == ["New Press"]
    assert _metadata_values(package_metadata, "dc", "description") == [
        "English description"
    ]
    assert _metadata_values(package_metadata, "dc", "rights") == ["CC BY 4.0"]
    assert _metadata_values(package_metadata, "dc", "subject") == [
        "History",
        "Translation",
    ]
    assert _metadata_values(package_metadata, "dc", "identifier") == [
        "10.1234/new-edition"
    ]
    assert (
        package_metadata.find(
            "opf:meta[@property='belongs-to-collection']", OPF_NS
        ).text
        == "English Series"
    )
    assert (
        package_metadata.find("opf:meta[@property='group-position']", OPF_NS).text
        == "2"
    )
    assert len(image_items) == 1
    assert packaged_cover == replacement_jpeg

    contributors = package_metadata.findall("dc:contributor", OPF_NS)
    contributors_by_text = {node.text: node for node in contributors}
    assert set(contributors_by_text) == {
        "New Editor",
        "New Translator",
        "New Illustrator",
    }
    roles = {
        node.get("refines"): node.text
        for node in package_metadata.findall("opf:meta[@property='role']", OPF_NS)
    }
    contributor_ids = {
        text: f"#{node.get('id')}" for text, node in contributors_by_text.items()
    }
    assert roles[contributor_ids["New Translator"]] == "trl"
    assert roles[contributor_ids["New Editor"]] == "edt"
    assert roles[contributor_ids["New Illustrator"]] == "ill"

    package_text = ET.tostring(package, encoding="unicode")
    for leaked in ("OLD-ISBN", "Старое издательство", "Старый переводчик"):
        assert leaked not in package_text

    rendered_text = " ".join(
        " ".join(" ".join(root.find("xhtml:body", XHTML_NS).itertext()).split())
        for root in contents.values()
    )
    assert "Translated body heading" in rendered_text
    assert "Translated paragraph 1" in rendered_text
    assert "Note body Back to paragraph" in rendered_text
    anchors = [
        anchor
        for root in contents.values()
        for anchor in root.findall(".//xhtml:a[@href]", XHTML_NS)
    ]
    noteref = next(
        anchor
        for anchor in anchors
        if anchor.get("{http://www.idpf.org/2007/ops}type") == "noteref"
    )
    backlink = next(anchor for anchor in anchors if anchor.text == "Back to paragraph")
    assert noteref.get("href", "").endswith("#note-1")
    assert backlink.get("href", "").endswith("#body-paragraph")
    for anchor in anchors:
        href = anchor.get("href", "")
        if not href.startswith("#"):
            target_name, target_id = href.split("#", 1)
            target_root = contents[f"EPUB/{target_name}"]
            assert target_root.find(f".//*[@id='{target_id}']") is not None


def test_export_cover_admission_refuses_missing_tampered_and_foreign_witnesses(
    upload_root,
):
    missing = {"cover_upload_path": "/uploads/7/missing.png"}
    with pytest.raises(ExportCoverError) as missing_error:
        pin_export_metadata_cover(missing, user_id=7)
    assert missing_error.value.code == "export-cover-source-missing"

    source = upload_root / "7" / "cover.png"
    source.write_bytes(_png_bytes("green"))
    witness = capture_export_cover(user_id=7, upload_path="/uploads/7/cover.png")
    canonical = upload_root / witness["upload_path"].removeprefix("/uploads/")
    original = canonical.read_bytes()
    canonical.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
    with pytest.raises(ExportCoverError) as tampered_error:
        resolve_export_cover(user_id=7, cover=witness)
    assert tampered_error.value.code == "export-cover-canonical-hash"

    with pytest.raises(ExportCoverError) as foreign_error:
        resolve_export_cover(user_id=8, cover=witness)
    assert foreign_error.value.code == "export-cover-witness-path"


def test_build_semantic_export_request_requires_resolved_replacement_cover():
    source_html = "<html><body><main><p>Text</p></main></body></html>"
    source_metadata = {"title": "Source", "language": "ru"}
    record, plan = _plan_record(source_html, source_metadata, None)

    with (
        patch(
            "iarf_assistant.addons.translator.block_export.rehydrate_source_plan",
            return_value=plan,
        ),
        patch(
            "iarf_assistant.addons.translator.block_export.render_persisted_target_document",
            return_value=source_html,
        ),
        pytest.raises(BlockExportError, match="export-cover-unresolved"),
    ):
        build_semantic_export_request(
            record,
            export_format="html",
            export_metadata={"cover_upload_path": "/uploads/7/cover.jpg"},
        )
