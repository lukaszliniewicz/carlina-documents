from __future__ import annotations

import hashlib

import pytest

from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION
from iarf_assistant.addons.translator.block_contract import (
    AttestedDocumentSnapshot,
    SourceProviderAttestation,
)
from iarf_assistant.addons.translator.block_planner import plan_document_blocks
from iarf_assistant.addons.translator.note_relations import (
    build_note_relations,
    reuse_note_relations,
    window_blocks_are_linked,
)


def _plan(source: str):
    digest = hashlib.sha256(source.encode()).hexdigest()
    snapshot = AttestedDocumentSnapshot(
        id="document-1",
        document_id="document-1",
        output_html=source,
        content_hash=digest,
        semantic_content_hash=digest,
        metadata={},
        provenance={},
        semantic_contract_version=SEMANTIC_HTML_CONTRACT_VERSION,
    )
    return plan_document_blocks(
        snapshot,
        project_id="project-1",
        source_attestation=SourceProviderAttestation(
            provider_addon_id="document-provider",
            provider_capability_id="document.processing",
            owner_id="owner-1",
            resource_id="document-1",
        ),
        source_language="en",
    )


def _by_role(plan, role: str):
    return [block for block in plan.blocks if block.role == role]


def test_note_relation_reuse_is_scoped_to_exact_plan_and_resets_after_failure():
    first_plan = _plan('<html><body><p>First.</p></body></html>')
    second_plan = _plan('<html><body><p>Second.</p></body></html>')

    @reuse_note_relations
    def assemble(*, plan, nested=None):
        first = build_note_relations(plan)
        if nested is not None:
            assert assemble(plan=nested) is not first
        assert build_note_relations(plan) is first
        return first

    first = assemble(plan=first_plan, nested=second_plan)
    assert assemble(plan=first_plan) is not first
    assert build_note_relations(first_plan) is not first
    failed = []

    @reuse_note_relations
    def fail(*, plan):
        failed.append(build_note_relations(plan))
        raise RuntimeError("assembly failed")

    with pytest.raises(RuntimeError, match="assembly failed"):
        fail(plan=first_plan)
    assert build_note_relations(first_plan) is not failed[0]


def test_distinct_markers_and_repeated_target_use_target_identity() -> None:
    plan = _plan(
        '<html><body><p>Main <a class="footnote-ref" href="#n1">*</a> '
        '<a class="footnote-ref" href="#n2">*</a> '
        '<a class="footnote-ref" href="#n1">again</a>.</p>'
        '<aside id="n1" role="doc-footnote"><p>Same body.</p></aside>'
        '<aside id="n2" role="doc-footnote"><p>Same body.</p></aside>'
        "</body></html>"
    )

    relations = build_note_relations(plan)
    main = _by_role(plan, "paragraph")[0]
    note_blocks = _by_role(plan, "note_body")

    assert relations.references[main.id] == ("n1", "n2")
    assert relations.notes["n1"] == (note_blocks[0],)
    assert relations.notes["n2"] == (note_blocks[1],)
    assert relations.unresolved == {}


def test_epub_namespaced_ids_are_accepted_but_relative_links_are_unscoped() -> None:
    plan = _plan(
        '<html><body><p>Body '
        '<a role="doc-noteref" href="#epub-1-note">1</a> '
        '<a role="doc-noteref" href="#epub-2-note">2</a> '
        '<a role="doc-noteref" href="chapter-2.xhtml#epub-2-note">2</a>'
        "</p>"
        '<aside id="epub-1-note" role="doc-footnote"><p>One.</p></aside>'
        '<aside id="epub-2-note" epub:type="endnote"><p>Two.</p></aside>'
        "</body></html>"
    )

    relations = build_note_relations(plan)
    main = _by_role(plan, "paragraph")[0]

    assert relations.references[main.id] == ("epub-1-note", "epub-2-note")
    assert relations.unresolved[main.id][-1].reason == "unscoped_link"
    assert relations.unresolved[main.id][-1].label == "2"


def test_entity_and_percent_decoding_happens_once_for_matching() -> None:
    plan = _plan(
        '<html><body><p>Body '
        '<a role="doc-noteref" href="#a%26b">1</a></p>'
        '<aside id="a&amp;b" role="doc-footnote"><p>Note.</p></aside>'
        "</body></html>"
    )

    relations = build_note_relations(plan)
    assert "a&b" in relations.notes


def test_duplicate_id_is_ambiguous_even_when_only_one_duplicate_is_a_note() -> None:
    plan = _plan(
        '<html><body><p>Body '
        '<a role="doc-noteref" href="#duplicate">1</a></p>'
        '<aside id="duplicate" role="doc-footnote"><p>Note.</p></aside>'
        '<p id="duplicate">Other.</p>'
        "</body></html>"
    )

    relations = build_note_relations(plan)
    main = _by_role(plan, "paragraph")[0]
    assert relations.references == {}
    assert relations.unresolved[main.id][0].reason == "ambiguous_target"


def test_multi_paragraph_note_excludes_heading_neighbors_and_backlinks() -> None:
    plan = _plan(
        '<html><body><p>Body '
        '<a role="doc-noteref" href="#note">1</a></p>'
        '<section id="note" role="doc-endnote"><h2>Notes</h2>'
        '<p>First <a epub:type="backlink" href="#source">↩</a></p>'
        "<p>Second.</p></section>"
        "<p>Neighbor.</p>"
        "</body></html>"
    )

    relations = build_note_relations(plan)
    note_blocks = relations.notes["note"]

    assert [block.projection for block in note_blocks] == ["First ↩", "Second."]
    assert all(block.role == "note_body" for block in note_blocks)
    assert all("source" not in targets for targets in relations.references.values())


def test_nested_note_references_are_direct_edges_without_recursive_expansion() -> None:
    plan = _plan(
        '<html><body><p>Main <a role="doc-noteref" href="#outer">1</a></p>'
        '<aside id="outer" role="doc-footnote"><p>Outer '
        '<a role="doc-noteref" href="#inner">2</a></p></aside>'
        '<aside id="inner" role="doc-footnote"><p>Inner.</p></aside>'
        "</body></html>"
    )

    relations = build_note_relations(plan)
    paragraphs = _by_role(plan, "note_body")

    assert relations.references[_by_role(plan, "paragraph")[0].id] == ("outer",)
    assert relations.references[paragraphs[0].id] == ("inner",)
    assert relations.notes["outer"] == (paragraphs[0],)
    assert relations.notes["inner"] == (paragraphs[1],)


def test_missing_unmapped_and_empty_targets_are_bounded_diagnostics() -> None:
    plan = _plan(
        '<html><body><p>Body '
        '<a role="doc-noteref" href="#missing">Missing</a> '
        '<a role="doc-noteref" href="#ordinary">Ordinary</a> '
        '<a role="doc-noteref" href="#">Empty</a></p>'
        '<p id="ordinary">Ordinary target.</p>'
        "</body></html>"
    )

    relations = build_note_relations(plan)
    diagnostics = relations.unresolved[_by_role(plan, "paragraph")[0].id]
    assert [(item.label, item.reason) for item in diagnostics] == [
        ("Missing", "missing_target"),
        ("Ordinary", "unmapped_note"),
        ("Empty", "missing_target"),
    ]
    assert all(len(item.label) <= 32 for item in diagnostics)


def test_inline_note_target_never_claims_an_unrelated_paragraph() -> None:
    plan = _plan(
        '<html><body><p>Before <a role="doc-noteref" href="#note">1</a> '
        '<span id="note" role="doc-footnote">only note</span> after.</p>'
        "</body></html>"
    )

    relations = build_note_relations(plan)
    assert relations.notes["note"][0].role == "note_body"
    assert relations.notes["note"][0].projection == "only note"


def test_window_scope_accepts_contiguous_or_explicitly_linked_note_suffix() -> None:
    plan = _plan(
        '<html><body><p>First.</p>'
        '<p>Second <a role="doc-noteref" href="#note">1</a>.</p>'
        '<p>Third.</p><aside id="note" role="doc-footnote"><p>Note.</p></aside>'
        "</body></html>"
    )
    body_blocks = _by_role(plan, "paragraph")
    note = _by_role(plan, "note_body")[0]

    assert window_blocks_are_linked(plan, [body_blocks[0].id, body_blocks[1].id])
    assert window_blocks_are_linked(plan, [body_blocks[1].id, note.id])
    assert window_blocks_are_linked(plan, [note.id, body_blocks[1].id]) is False


def test_window_scope_rejects_unrelated_wrong_order_and_suffix_duplicates() -> None:
    plan = _plan(
        '<html><body><p>First.</p>'
        '<p>Second <a role="doc-noteref" href="#note">1</a>.</p>'
        '<p>Third.</p><aside id="note" role="doc-footnote"><p>Note.</p></aside>'
        "</body></html>"
    )
    body_blocks = _by_role(plan, "paragraph")
    note = _by_role(plan, "note_body")[0]

    assert not window_blocks_are_linked(plan, [body_blocks[1].id, note.id, body_blocks[0].id])
    assert not window_blocks_are_linked(plan, [body_blocks[1].id, note.id, note.id])
    assert not window_blocks_are_linked(plan, [body_blocks[1].id, note.id, body_blocks[2].id])


def test_index_is_pure_and_returns_actual_plan_blocks() -> None:
    source = (
        '<html><body><p>Body <a role="doc-noteref" href="#note">1</a></p>'
        '<aside id="note" role="doc-footnote"><p>Note.</p></aside>'
        "</body></html>"
    )
    plan = _plan(source)
    before_hash = plan.plan_hash
    before_html = plan.snapshot.output_html
    relations = build_note_relations(plan)

    assert plan.plan_hash == before_hash
    assert plan.snapshot.output_html == before_html
    assert relations.notes["note"][0] is _by_role(plan, "note_body")[0]
