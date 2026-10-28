from __future__ import annotations

import hashlib

from bs4 import BeautifulSoup

from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION
from iarf_assistant.addons.doc_processor.parsers.pdf_utils import (
    concatenate_html_pages,
    normalize_footnote_links,
)
from iarf_assistant.addons.doc_processor.parsers.semantic_validation import (
    validate_normalized_page_output,
)
from iarf_assistant.addons.doc_processor.semantic_document import (
    canonicalize_document_html,
    validate_semantic_export,
)
from iarf_assistant.addons.translator.block_contract import (
    AttestedDocumentSnapshot,
    SourceProviderAttestation,
)
from iarf_assistant.addons.translator.block_planner import (
    plan_document_blocks,
    validate_canonical_plan,
)


def test_repeated_footnote_marker_survives_doc_processor_to_translator_plan():
    page = (
        "<main_content><p>First "
        '<span class="footnote_marker" id="fm1">1</span> again '
        '<span class="footnote_marker" id="fm1">1</span>.</p></main_content>'
        '<footnotes><div class="footnote" id="f1">Note one.</div></footnotes>'
    )

    assert validate_normalized_page_output(page).valid
    concatenated = concatenate_html_pages([page])
    concatenated_soup = BeautifulSoup(concatenated, "html.parser")
    references = concatenated_soup.find_all("a", class_="footnote-ref")
    assert len(references) == 2
    assert len({reference.get("href") for reference in references}) == 1
    target_id = str(references[0]["href"])[1:]
    target = concatenated_soup.find(id=target_id)
    assert target is not None
    assert target.get_text(" ", strip=True).endswith("Note one.")

    semantic = canonicalize_document_html(concatenated, user_id=1)
    assert semantic.errors == ()
    validate_semantic_export(
        BeautifulSoup(semantic.html, "html.parser"), export_name="synthetic"
    )

    digest = hashlib.sha256(semantic.html.encode("utf-8")).hexdigest()
    snapshot = AttestedDocumentSnapshot(
        id="doc-footnote-compat",
        document_id="doc-footnote-compat",
        output_html=semantic.html,
        content_hash=digest,
        semantic_content_hash=digest,
        metadata={},
        provenance={},
        semantic_contract_version=SEMANTIC_HTML_CONTRACT_VERSION,
    )
    attestation = SourceProviderAttestation(
        provider_addon_id="doc-processor",
        provider_capability_id="document.processing",
        owner_id="owner-1",
        resource_id=snapshot.document_id,
    )

    plan = plan_document_blocks(
        snapshot,
        project_id="project-footnote-compat",
        source_attestation=attestation,
        source_language="en",
    )
    validate_canonical_plan(plan)

    planned_soup = BeautifulSoup(plan.snapshot.output_html, "html.parser")
    planned_references = planned_soup.find_all("a", class_="footnote-ref")
    assert [reference.get("href") for reference in planned_references] == [
        f"#{target_id}",
        f"#{target_id}",
    ]
    assert len(planned_soup.find_all(id=target_id)) == 1


def test_symbolic_marker_ids_resolve_distinct_notes_and_preserve_repeated_identity():
    page = (
        '<main_content><p><span class="footnote_marker" id="fm1">*</span> '
        '<span class="footnote_marker" id="fm2">*</span> again '
        '<span class="footnote_marker" id="fm1">*</span></p></main_content>'
        '<footnotes><div class="footnote" id="f1">First note.</div>'
        '<div class="footnote" id="f2">Second note.</div></footnotes>'
    )

    soup = BeautifulSoup(concatenate_html_pages([page]), "html.parser")
    refs = soup.find_all("a", class_="footnote-ref")

    assert len(refs) == 3
    assert refs[0]["href"] != refs[1]["href"]
    assert refs[0]["href"] == refs[2]["href"]
    assert len(soup.find_all(class_="footnote")) == 2
    assert len({note["id"] for note in soup.find_all(class_="footnote")}) == 2


def test_anonymous_symbolic_markers_stay_unlinked_when_scope_is_ambiguous():
    main = BeautifulSoup(
        '<main><p><span class="footnote_marker">*</span> '
        '<span class="footnote_marker">*</span></p></main>',
        "html.parser",
    )
    footnotes = BeautifulSoup(
        '<div class="footnote" id="f1">First note.</div>'
        '<div class="footnote" id="f2">Second note.</div>',
        "html.parser",
    )

    normalize_footnote_links(main, footnotes)

    assert not main.find_all("a", class_="footnote-ref")
    assert [marker.get_text(strip=True) for marker in main.find_all("sup")] == [
        "*",
        "*",
    ]
    notes = footnotes.find_all(class_="footnote")
    assert len(notes) == 2
    assert len({note["id"] for note in notes}) == 2


def test_single_symbolic_marker_per_page_resolves_to_that_page_note():
    pages = [
        '<main_content><p>First <span class="footnote_marker">*</span>.</p>'
        "</main_content>"
        '<footnotes><div class="footnote" id="f1">First note.</div></footnotes>',
        '<main_content><p>Second <span class="footnote_marker">*</span>.</p>'
        "</main_content>"
        '<footnotes><div class="footnote" id="f1">Second note.</div></footnotes>',
    ]

    soup = BeautifulSoup(concatenate_html_pages(pages), "html.parser")
    refs = soup.find_all("a", class_="footnote-ref")
    targets = [soup.find(id=ref["href"].removeprefix("#")) for ref in refs]

    assert len(refs) == 2
    assert refs[0]["href"] != refs[1]["href"]
    assert "First note." in targets[0].get_text(" ", strip=True)
    assert "Second note." in targets[1].get_text(" ", strip=True)


def test_explicit_local_target_href_wins_for_symbolic_marker():
    page = (
        '<main_content><p><span class="footnote_marker" href="#f2">*</span>'
        "</p></main_content>"
        '<footnotes><div class="footnote" id="f1">First note.</div>'
        '<div class="footnote" id="f2">Second note.</div></footnotes>'
    )

    soup = BeautifulSoup(concatenate_html_pages([page]), "html.parser")
    reference = soup.find("a", class_="footnote-ref")
    assert reference is not None
    target = soup.find(id=reference["href"].removeprefix("#"))
    assert target is not None
    assert "Second note." in target.get_text(" ", strip=True)


def test_identical_note_bodies_keep_distinct_explicit_target_ids():
    page = (
        '<main_content><p><span class="footnote_marker" id="fm1">*</span> '
        '<span class="footnote_marker" id="fm2">*</span></p></main_content>'
        '<footnotes><div class="footnote" id="f1">Same note.</div>'
        '<div class="footnote" id="f2">Same note.</div></footnotes>'
    )

    soup = BeautifulSoup(concatenate_html_pages([page]), "html.parser")
    refs = soup.find_all("a", class_="footnote-ref")
    notes = soup.find_all(class_="footnote")

    assert len(refs) == 2
    assert refs[0]["href"] != refs[1]["href"]
    assert len(notes) == 2
    assert len({note["id"] for note in notes}) == 2


def test_explicit_href_reaches_globally_unique_note_on_a_later_page():
    main = BeautifulSoup(
        '<main><span class="footnote_marker" href="#later-note" '
        'data-docproc-source-page="1">*</span></main>',
        "html.parser",
    )
    footnotes = BeautifulSoup(
        '<div class="footnote" id="later-note" data-docproc-source-page="10">Later note.</div>',
        "html.parser",
    )

    normalize_footnote_links(main, footnotes)

    reference = main.find("a", class_="footnote-ref")
    assert reference is not None
    target = footnotes.find(id=reference["href"].removeprefix("#"))
    assert target is not None
    assert "Later note." in target.get_text(" ", strip=True)


def test_explicit_href_uses_page_to_disambiguate_repeated_note_ids():
    main = BeautifulSoup(
        '<main><span class="footnote_marker" href="#f1" '
        'data-docproc-source-page="1">*</span></main>',
        "html.parser",
    )
    footnotes = BeautifulSoup(
        '<div class="footnote" id="f1" data-docproc-source-page="1">First note.</div>'
        '<div class="footnote" id="f1" data-docproc-source-page="10">Later note.</div>',
        "html.parser",
    )

    normalize_footnote_links(main, footnotes)

    reference = main.find("a", class_="footnote-ref")
    assert reference is not None
    targets = footnotes.find_all(id=reference["href"].removeprefix("#"))
    assert len(targets) == 1
    assert "First note." in targets[0].get_text(" ", strip=True)


def test_missing_conventional_target_id_stays_unlinked_despite_label_match():
    main = BeautifulSoup(
        '<main><span class="footnote_marker" id="fm1">*</span></main>',
        "html.parser",
    )
    footnotes = BeautifulSoup(
        '<div class="footnote" id="f2">Only other note.</div>',
        "html.parser",
    )

    normalize_footnote_links(main, footnotes)

    assert not main.find("a", class_="footnote-ref")
    assert main.find("sup").get_text(strip=True) == "*"


def test_ambiguous_conventional_target_id_stays_unlinked_despite_label_match():
    main = BeautifulSoup(
        '<main><span class="footnote_marker" id="fm1">1</span></main>',
        "html.parser",
    )
    footnotes = BeautifulSoup(
        '<div class="footnote" id="f1">First copy.</div>'
        '<div class="footnote" id="f1">Second copy.</div>'
        '<div class="footnote" id="f2">Other note.</div>',
        "html.parser",
    )

    normalize_footnote_links(main, footnotes)

    assert not main.find("a", class_="footnote-ref")
    assert main.find("sup").get_text(strip=True) == "1"
