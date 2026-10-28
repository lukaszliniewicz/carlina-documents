import hashlib
import zipfile
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup
from flask import Flask

from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION
from iarf_assistant.addons.doc_processor.document_review import DocumentReview
from iarf_assistant.addons.doc_processor.epub_packager import EPUBPackager
from iarf_assistant.addons.doc_processor.note_topology_repair import (
    reference_candidates,
    repair_note_topology,
)
from iarf_assistant.addons.doc_processor.semantic_document import (
    canonicalize_document_html,
)
from iarf_assistant.addons.translator.block_contract import (
    AttestedDocumentSnapshot,
    SourceProviderAttestation,
)
from iarf_assistant.addons.translator.block_planner import plan_document_blocks
from iarf_assistant.addons.translator.note_relations import build_note_relations


def review_fixture():
    return DocumentReview(
        '<main><span class="pagebreak" id="doc-page-31"></span><h2>Chapter 1</h2>'
        '<p id="first">First<sup><a id="r1" role="doc-noteref" href="#n1">29</a></sup>.</p>'
        '<span class="pagebreak" id="doc-page-70"></span><h2>Chapter 4</h2>'
        '<p id="wrong">Later<sup><a id="r4" role="doc-noteref" href="#n1">29</a></sup>.</p>'
        '<p id="bare">Again<sup>29</sup>. x<sup>2</sup>.</p>'
        '<ol start="29"><li id="n4">A different note with <em>formatting</em>.</li></ol>'
        '<section role="doc-endnotes"><aside role="doc-endnote" id="n1">First note.'
        '<a role="doc-backlink" id="b1" href="#r1">↩</a>'
        '<a role="doc-backlink" id="b4" href="#r4">↩</a>'
        "</aside></section></main>"
    )


def node_id(review, html_id):
    return next(
        node.element_id for node in review._nodes if node.tag.get("id") == html_id
    )


def operations(review):
    note = node_id(review, "n4")
    return [
        {
            "action": "promote_note",
            "element_id": note,
            "label": "29",
            "scope_id": "chapter-4",
        },
        {
            "action": "link_reference",
            "element_id": node_id(review, "wrong"),
            "marker_index": 0,
            "note_element_id": note,
            "expected_label": "29",
            "expected_href": "#n1",
        },
        {
            "action": "link_reference",
            "element_id": node_id(review, "bare"),
            "marker_index": 0,
            "note_element_id": note,
            "expected_label": "29",
            "expected_href": None,
        },
    ]


def test_repairs_wrong_and_bare_refs_without_rewriting_note_prose_or_other_links():
    review = review_fixture()
    original = str(review._soup)
    output, summary = repair_note_topology(review, operations(review))
    soup = BeautifulSoup(output, "html.parser")
    assert str(review._soup) == original
    assert soup.find(id="r1")["href"] == "#n1"
    assert soup.find(id="r4")["href"] == "#n4"
    assert soup.find(id="bare").a["href"] == "#n4"
    assert soup.find(id="bare").find_all("sup")[1].get_text() == "2"
    assert soup.find(id="bare").find_all("sup")[1].find("a") is None
    assert soup.find(id="n4")["role"] == "doc-endnote"
    assert soup.find(id="n4")["data-footnote-scope"] == "chapter-4"
    assert soup.find(id="n4").em.get_text() == "formatting"
    assert soup.find(id="b4").parent is soup.find(id="n4")
    assert soup.find(id="b1").parent is soup.find(id="n1")
    assert soup.ol["start"] == "29"
    assert len(summary) == 3
    semantic = canonicalize_document_html(output, user_id=1)
    assert not semantic.errors
    assert 'data-footnote-scope="chapter-4"' in semantic.html


def test_candidate_inventory_is_paginated_and_exposes_exact_witnesses():
    review = review_fixture()
    result = reference_candidates(review, offset=1, limit=2)
    assert result["total_references_and_candidates"] == 4
    assert result["next_offset"] == 3
    assert result["items"][0]["element_id"] == node_id(review, "wrong")
    assert result["items"][0]["page_numbers"] == [70]
    assert result["items"][0]["href"] == "#n1"
    assert result["items"][1]["kind"] == "superscript_candidate"
    assert result["items"][1]["href"] is None
    assert result["items"][1]["marker_index"] == 0


def test_retarget_does_not_move_cross_references_from_another_note():
    review = review_fixture()
    unrelated = review._soup.new_tag("aside", id="unrelated", role="doc-endnote")
    crossref = review._soup.new_tag("a", href="#r4", id="crossref")
    crossref.string = "See that passage."
    unrelated.append(crossref)
    review._soup.main.append(unrelated)
    review = DocumentReview(str(review._soup))
    output, _ = repair_note_topology(review, operations(review))
    soup = BeautifulSoup(output, "html.parser")
    assert soup.find(id="crossref").parent is soup.find(id="unrelated")
    assert soup.find(id="crossref").get_text() == "See that passage."


def test_repaired_identities_reach_translator_context_and_epub(tmp_path):
    review = review_fixture()
    output, _ = repair_note_topology(review, operations(review))
    semantic = canonicalize_document_html(output, user_id=1)
    digest = hashlib.sha256(semantic.html.encode()).hexdigest()
    snapshot = AttestedDocumentSnapshot(
        id="doc",
        document_id="doc",
        output_html=semantic.html,
        content_hash=digest,
        semantic_content_hash=digest,
        metadata={},
        provenance={},
        semantic_contract_version=SEMANTIC_HTML_CONTRACT_VERSION,
    )
    plan = plan_document_blocks(
        snapshot,
        project_id="project",
        source_language="en",
        source_attestation=SourceProviderAttestation(
            provider_addon_id="doc-processor",
            provider_capability_id="document.processing",
            owner_id="owner-1",
            resource_id="doc",
        ),
    )
    relations = build_note_relations(plan)
    assert relations.unresolved == {}
    assert set(relations.notes) == {"n1", "n4"}
    assert sorted(relations.references.values()) == [("n1",), ("n4",), ("n4",)]
    assert {block.id for block in relations.notes["n1"]}.isdisjoint(
        {block.id for block in relations.notes["n4"]}
    )
    destination = tmp_path / "repaired.epub"
    document = SimpleNamespace(
        id="synthetic-note-repair",
        filename="source.pdf",
        file_path=str(tmp_path / "source.pdf"),
        settings={"document_metadata": {"title": "Local notes", "language": "en"}},
        metadata_record=None,
        output_html=semantic.html,
        output_markdown=None,
    )
    with Flask(__name__).app_context():
        EPUBPackager(document, str(destination)).build()
    with zipfile.ZipFile(destination) as archive:
        contents = {
            name: BeautifulSoup(archive.read(name), "xml")
            for name in archive.namelist()
            if name.startswith("EPUB/content-") and name.endswith(".xhtml")
        }
    for ref_id, target_id, text in (
        ("r1", "n1", "First note."),
        ("r4", "n4", "A different note"),
    ):
        name, reference = next(
            (name, soup.find(id=ref_id))
            for name, soup in contents.items()
            if soup.find(id=ref_id)
        )
        member, fragment = reference["href"].split("#")
        target = contents["EPUB/" + member if member else name].find(id=fragment)
        assert fragment == target_id
        assert text in target.get_text(" ", strip=True)


@pytest.mark.parametrize(
    "mutation",
    [
        {"expected_label": "28"},
        {"expected_href": "#other"},
        {"marker_index": True},
        {"marker_index": -1},
        {"marker_index": 99},
        {"extra": "not permitted"},
    ],
)
def test_wrong_witness_or_shape_rejected_without_mutation(mutation):
    review = review_fixture()
    original = str(review._soup)
    ops = operations(review)
    ops[1].update(mutation)
    with pytest.raises(ValueError):
        repair_note_topology(review, ops)
    assert str(review._soup) == original


def test_duplicate_operations_and_non_note_destination_rejected():
    review = review_fixture()
    ops = operations(review)
    with pytest.raises(ValueError, match="more than once"):
        repair_note_topology(review, [*ops, ops[1]])
    with pytest.raises(ValueError, match="destination"):
        repair_note_topology(review, ops[1:])


@pytest.mark.parametrize(
    "body",
    [
        '<p id="body"><code>x<sup>2</sup></code></p>',
        '<p id="body" hidden>x<sup>2</sup></p>',
        '<p id="body"><a href="https://example.test">x<sup>2</sup></a></p>',
        '<p id="body"><sup><em>2</em></sup></p>',
    ],
)
def test_protected_or_complex_superscripts_not_offered(body):
    review = DocumentReview("<main>" + body + "</main>")
    assert reference_candidates(review)["items"] == []


def test_promoted_block_cannot_also_be_reference_owner():
    review = DocumentReview('<main><p id="n">Note<sup>2</sup></p></main>')
    element = node_id(review, "n")
    with pytest.raises(ValueError, match="body text block"):
        repair_note_topology(
            review,
            [
                {
                    "action": "promote_note",
                    "element_id": element,
                    "label": "2",
                    "scope_id": "s",
                },
                {
                    "action": "link_reference",
                    "element_id": element,
                    "marker_index": 0,
                    "note_element_id": element,
                    "expected_label": "2",
                    "expected_href": None,
                },
            ],
        )


def test_plain_item_inside_endnotes_container_can_be_promoted():
    review = DocumentReview(
        '<main><p id="body">Text<sup>1</sup></p>'
        '<section role="doc-endnotes"><ol><li id="note">A note.</li></ol></section></main>'
    )
    output, _ = repair_note_topology(
        review,
        [
            {
                "action": "promote_note",
                "element_id": node_id(review, "note"),
                "label": "1",
                "scope_id": "chapter-1",
            }
        ],
    )
    assert BeautifulSoup(output, "html.parser").find(id="note")["role"] == "doc-endnote"


def test_unicode_marker_candidate_can_be_linked_without_changing_its_label():
    review = DocumentReview(
        '<main><p id="body">Text<sup class="footnote-unresolved">α</sup></p>'
        '<aside id="note" role="doc-endnote">Note.</aside></main>'
    )
    candidate = reference_candidates(review)["items"][0]
    assert candidate["label"] == "α"
    output, _ = repair_note_topology(review, [{
        "action": "link_reference", "element_id": candidate["element_id"],
        "marker_index": candidate["marker_index"], "note_element_id": node_id(review, "note"),
        "expected_label": "α", "expected_href": None,
    }])
    assert BeautifulSoup(output, "html.parser").find(id="body").a.get_text() == "α"


def test_existing_note_scope_and_generated_ids_are_deterministic():
    review = DocumentReview(
        '<main><p>Text<sup id="sup2">2</sup></p>'
        '<aside role="doc-endnote">Note.</aside></main>'
    )
    body, note = review._nodes
    ops = [
        {
            "action": "set_note_scope",
            "element_id": note.element_id,
            "scope_id": "section-2",
        },
        {
            "action": "link_reference",
            "element_id": body.element_id,
            "marker_index": 0,
            "note_element_id": note.element_id,
            "expected_label": "2",
            "expected_href": None,
        },
    ]
    first, _ = repair_note_topology(review, ops)
    second, _ = repair_note_topology(review, ops)
    assert first == second
    soup = BeautifulSoup(first, "html.parser")
    assert len(soup.find_all(id="sup2")) == 1
    assert soup.find(id="sup2").name == "a"
    assert soup.find(id="sup2")["data-footnote-scope"] == "section-2"


@pytest.mark.parametrize("scope", ["", "with space", "x" * 129, "<x>", None])
def test_invalid_scope_rejected(scope):
    review = review_fixture()
    ops = operations(review)
    ops[0]["scope_id"] = scope
    with pytest.raises(ValueError, match="scope_id"):
        repair_note_topology(review, ops)
