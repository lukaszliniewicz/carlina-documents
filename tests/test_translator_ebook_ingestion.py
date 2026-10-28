"""Real ebook bytes through ingestion, canonical HTML and Translator blocks."""

import base64
import io
import zipfile

import pytest
from bs4 import BeautifulSoup
from PIL import Image

from iarf_assistant.addons.doc_processor import metadata_service, parsers, semantic_document
from iarf_assistant.addons.translator import (
    block_contract, block_planner, block_rendering, compact_markup,
    target_rendering, target_validation,
)
from test_doc_processor_epub_ingestion import write_epub


def ebook_source(tmp_path, kind):
    uploads = tmp_path / "uploads"
    owner = uploads / "1"
    owner.mkdir(parents=True)
    picture = io.BytesIO()
    Image.new("RGB", (3, 3), "blue").save(picture, format="PNG")
    if kind == "fb2":
        path = owner / "book.fb2"
        path.write_text(
            '<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0" xmlns:l="http://www.w3.org/1999/xlink">'
            '<description><title-info><book-title>Book</book-title><lang>en</lang></title-info></description>'
            '<body><section><title><p>Chapter</p></title>'
            '<p id="ref">Text <emphasis>with emphasis</emphasis> <a type="note" l:href="#note">1</a>.</p>'
            '<cite><p>Quoted text</p></cite><poem><stanza><v>First verse</v><v>Second verse</v></stanza></poem>'
            '<table><tr><th>Term</th><td>Cell value</td></tr></table>'
            '<image l:href="#picture" alt="Blue square"/>'
            '</section></body><body name="notes"><section id="note"><p>Note text <a l:href="#ref">Back</a></p></section></body>'
            '<binary id="picture" content-type="image/png">' + base64.b64encode(picture.getvalue()).decode() + '</binary></FictionBook>',
            encoding="utf-8",
        )
    else:
        path = write_epub(
            owner / "book.epub", version="2.0" if kind == "epub2" else "3.0",
            chapter='<html lang="en"><body><h1>Chapter</h1>'
            '<p id="réf">Text <em>with emphasis</em> <a role="doc-noteref" epub:type="noteref" href="a-notes.xhtml#note">1</a>.</p>'
            '<blockquote><p>Quoted text</p></blockquote><div class="poem"><p>First verse<br class="line" title="Pause"/>Second verse</p></div>'
            '<table><tr><th>Term</th><td>Cell value</td></tr></table>'
            '<img src="../images/picture.png" alt="Blue square"/></body></html>',
        )
        with zipfile.ZipFile(path, "a") as archive:
            archive.writestr("OPS/images/picture.png", picture.getvalue())
    return path, uploads


@pytest.mark.parametrize("kind", ["fb2", "epub2", "epub3"])
def test_ebook_blocks_have_exact_coverage_notes_assets_and_valid_target(tmp_path, kind):
    path, uploads = ebook_source(tmp_path, kind)
    status, source = parsers.get_parser(str(path), 1, None).process()
    assert status == "COMPLETED"
    canonical = semantic_document.canonicalize_document_html(
        source, user_id=1, source_file_path=str(path), uploads_dir=uploads,
    )
    assert not canonical.errors
    snapshot = block_contract.AttestedDocumentSnapshot(
        id="book", document_id="book", output_html=canonical.html,
        content_hash=canonical.content_hash, semantic_content_hash=canonical.content_hash,
        metadata=metadata_service.extract_source_document_metadata(path), provenance={},
        semantic_contract_version=canonical.contract_version,
    )
    attestation = block_contract.SourceProviderAttestation(
        provider_addon_id="doc-processor", provider_capability_id="document.processing",
        owner_id="1", resource_id="book",
    )
    plan = block_planner.plan_document_blocks(
        snapshot, project_id="project", source_language="en", source_attestation=attestation,
    )
    block_planner.validate_canonical_plan(plan)
    assert block_rendering.render_source_identity(plan) == canonical.html
    assert type(plan).from_dict(plan.to_dict()).plan_hash == plan.plan_hash
    assert {"heading", "paragraph", "blockquote", "table_cell", "note_body"} <= {block.role for block in plan.blocks}
    prose = "\n".join(block.projection for block in plan.blocks)
    for text in ("Chapter", "Text", "with emphasis", "Quoted text", "First verse", "Second verse", "Term", "Cell value", "Note text"):
        assert prose.count(text) == 1
    assert prose.index("Chapter") < prose.index("Text") < prose.index("Note text")
    assert all(entry.disposition != "translatable" or entry.block_id for entry in plan.coverage.entries)

    candidates = {}
    for number, block in enumerate(plan.blocks, 1):
        compact = compact_markup.compact_source_block(block, number=number)
        candidate = compact_markup.normalize_compact_result(
            plan=plan, plan_id="plan", block=block, expected_number=number,
            value={key: compact[key] for key in ("n", "content", "fields")},
        )
        assert target_validation.validate_target_candidate(plan, "plan", block, candidate).valid
        candidates[block.id] = candidate
    target = target_rendering.render_target_document(plan, "plan", candidates, target_language="pl")
    assert target_rendering.validate_target_semantic_document(target).valid
    soup = BeautifulSoup(target, "html.parser")
    assert soup.em.get_text() == "with emphasis"
    assert soup.img["src"].startswith("/uploads/1/")
    assert (uploads / soup.img["src"].removeprefix("/uploads/")).is_file()
    for anchor in soup.find_all("a"):
        assert anchor["href"].startswith("#")
        assert soup.find(id=anchor["href"][1:]) is not None
