from __future__ import annotations

import hashlib
import re
import base64
import io
import zipfile
from dataclasses import replace
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup
from flask import Flask
from PIL import Image

from carlina.core.semantic_diagrams import materialize_mermaid
from iarf_assistant.addons.doc_processor.epub_packager import EPUBPackager
from iarf_assistant.addons.doc_processor.epub_validation import validate_epub_structure

from iarf_assistant.addons.translator import (
    block_rendering,
    compact_markup,
    target_rendering,
    target_validation,
)
from iarf_assistant.addons.translator.block_contract import (
    AttestedDocumentSnapshot,
    BlockContractError,
    SourceProviderAttestation,
)
from iarf_assistant.addons.translator.block_planner import (
    plan_document_blocks as _plan_document_blocks,
)

render_source_identity = block_rendering.render_source_identity


def _plan():
    html = '<html><body><p>A <em>nested &amp; text</em>.<br/>Next.</p><img alt="Accessible" src="x"></body></html>'
    digest = hashlib.sha256(html.encode()).hexdigest()
    snapshot = AttestedDocumentSnapshot(
        id="doc-1",
        document_id="doc-1",
        output_html=html,
        content_hash=digest,
        semantic_content_hash=digest,
        metadata={},
        provenance={},
    )
    return plan_document_blocks(
        snapshot, project_id="project-1", source_language="en"
    ), html


def _attestation(snapshot: AttestedDocumentSnapshot) -> SourceProviderAttestation:
    return SourceProviderAttestation(
        provider_addon_id="doc-processor",
        provider_capability_id="document.processing",
        owner_id="owner-1",
        resource_id=snapshot.document_id,
    )


def plan_document_blocks(snapshot: AttestedDocumentSnapshot, **kwargs):
    return _plan_document_blocks(
        snapshot,
        source_attestation=kwargs.pop("source_attestation", _attestation(snapshot)),
        **kwargs,
    )


def test_identity_renderer_is_exact_and_validates_protected_inline() -> None:
    plan, html = _plan()
    assert render_source_identity(plan) == html
    assert any(block.protected_inline for block in plan.blocks)
    assert plan == type(plan).from_dict(plan.to_dict())


def test_identity_renderer_fails_closed_for_source_and_slot_tampering() -> None:
    plan, html = _plan()
    with pytest.raises(BlockContractError):
        render_source_identity(plan, html.replace("Next", "Changed"))
    first = plan.blocks[0]
    object.__setattr__(first.slots[0], "source_hash", "0" * 64)
    with pytest.raises(BlockContractError):
        render_source_identity(plan)


def _compact_plan(source: str):
    snapshot = AttestedDocumentSnapshot(
        id="doc-1",
        document_id="doc-1",
        output_html=source,
        content_hash=hashlib.sha256(source.encode()).hexdigest(),
        semantic_content_hash=hashlib.sha256(source.encode()).hexdigest(),
        metadata={},
        provenance={},
    )
    return plan_document_blocks(
        snapshot,
        project_id="project-1",
        source_attestation=SourceProviderAttestation(
            provider_addon_id="doc-provider",
            provider_capability_id="semantic-document",
            owner_id="owner-1",
            resource_id="doc-1",
        ),
        source_language="en",
    )


def _normalize(
    plan,
    block,
    source_block,
    *,
    content=None,
    fields=None,
    implicit_outer_wrappers=False,
):
    return compact_markup.normalize_compact_result(
        plan=plan,
        plan_id="plan-1",
        block=block,
        expected_number=source_block["n"],
        value={
            "n": source_block["n"],
            "content": source_block["content"] if content is None else content,
            "fields": source_block["fields"] if fields is None else fields,
        },
        implicit_outer_wrappers=implicit_outer_wrappers,
    )


def test_image_alts_and_structured_descriptions_translate_and_survive_epub(tmp_path):
    image = io.BytesIO()
    Image.new("RGB", (8, 8), (40, 80, 120)).save(image, format="PNG")
    data_url = "data:image/png;base64," + base64.b64encode(image.getvalue()).decode()
    source = (
        '<html lang="en"><body><h1>Readiness</h1>'
        '<figure class="mermaid-diagram-v1" id="diagram1" lang="en">'
        '<pre class="mermaid-template"><code>flowchart TD\nn1{"{{ready}}"}\nn2["{{send}}"]\n'
        'n1 --&gt;|"{{yes}}"| n2\nn2 --&gt;|"{{no}}"| n1</code></pre>'
        '<div class="mermaid-labels"><p id="diagram1--ready">Ready?</p><p id="diagram1--send">Send</p>'
        '<p id="diagram1--yes">Yes</p><p id="diagram1--no">No</p></div>'
        '<figcaption>Figure 1: Readiness.</figcaption>'
        '<p class="image-alt-v1">Readiness decision. Description follows.</p>'
        '<div class="image-description-v1" id="diagram1-description"><p>The No connection returns to the decision.</p></div></figure>'
        f'<figure><img id="plot" src="{data_url}" alt="Sales chart. Values follow.">'
        '<figcaption>Figure 2: Sales.</figcaption><p class="image-description-link"><a href="#plot-description">Chart description</a></p>'
        '<div class="image-description-v1" id="plot-description"><p>Sales increased.</p>'
        '<table><caption>Sales values</caption><thead><tr><th scope="col">Year</th><th scope="col">Sales</th></tr></thead>'
        '<tbody><tr><th scope="row">2023</th><td>60</td></tr></tbody></table></div></figure></body></html>'
    )
    translations = {
        "Readiness decision. Description follows.": "Decyzja o gotowości. Opis poniżej.",
        "The No connection returns to the decision.": "Połączenie Nie wraca do decyzji.",
        "Sales chart. Values follow.": "Wykres sprzedaży. Wartości poniżej.",
        "Chart description": "Opis wykresu", "Sales increased.": "Sprzedaż wzrosła.",
        "Sales values": "Wartości sprzedaży", "Ready?": "Gotowe?", "Send": "Wyślij",
        "Yes": "Tak", "No": "Nie", "Readiness": "Gotowość", "Figure": "Rysunek",
        "Sales": "Sprzedaż", "Year": "Rok",
    }

    def translated(text):
        for old, new in translations.items():
            text = text.replace(old, new)
        return text

    plan = _compact_plan(source)
    assert render_source_identity(plan) == source
    assert any(block.role == "accessibility_text" for block in plan.blocks)
    candidates = {}
    for number, block in enumerate(plan.blocks, 1):
        wire = compact_markup.compact_source_block(block, number=number)
        candidate = _normalize(
            plan, block, wire, content=translated(wire["content"]),
            fields=[{**field, "value": translated(field["value"])} for field in wire["fields"]],
        )
        assert target_validation.validate_target_candidate(plan, "plan-1", block, candidate).valid
        candidates[block.id] = candidate
    target = target_rendering.render_target_document(plan, "plan-1", candidates, target_language="pl")
    assert target_rendering.validate_target_semantic_document(target).valid
    soup = BeautifulSoup(target, "html.parser")
    assert soup.select_one('img#plot')["alt"] == "Wykres sprzedaży. Wartości poniżej."
    assert soup.select_one('.image-alt-v1').get_text() == "Decyzja o gotowości. Opis poniżej."
    assert soup.find(id="plot-description").get_text(" ", strip=True).startswith("Sprzedaż wzrosła.")
    assert soup.select_one('.image-description-link a')["href"] == '#plot-description'
    source_code = BeautifulSoup(source, "html.parser").select_one('.mermaid-template code').get_text()
    assert soup.select_one('.mermaid-template code').get_text() == source_code
    assert 'n1 -->|"Tak"| n2' in materialize_mermaid(soup.select_one('figure.mermaid-diagram-v1'))
    assert render_source_identity(plan) == source

    document = SimpleNamespace(
        id="11111111-1111-1111-1111-111111111111", filename="synthetic.html",
        file_path=str(tmp_path / 'synthetic.html'), output_html=target,
        output_markdown=None, metadata_record=None,
        settings={"document_metadata": {"title": "Gotowość", "language": "pl"}},
    )
    output = tmp_path / 'translated-descriptions.epub'
    with Flask(__name__).app_context():
        packager = EPUBPackager(document, str(output), options={}, allowed_resource_roots=[tmp_path])
        packager.build()
        report = packager.quality_report()
    assert validate_epub_structure(str(output)) == (True, None)
    assert document.output_html == target
    assert report["checks"]["manual"] == 'not_reviewed'
    assert sum(issue["code"] == "image-description-review" for issue in report["issues"]) == 2
    with zipfile.ZipFile(output) as archive:
        content = "".join(archive.read(name).decode() for name in archive.namelist() if name.startswith('EPUB/content-') and name.endswith('.xhtml'))
        markup = BeautifulSoup(content, 'xml')
        assert markup.find(id='plot').get('alt') == "Wykres sprzedaży. Wartości poniżej."
        assert "Decyzja o gotowości. Opis poniżej." in content
        assert "Sprzedaż wzrosła." in content
        assert markup.select_one('.diagram-description-v1')["lang"] == 'pl'
        assert markup.select_one('.diagram-connections caption').get_text() == 'Połączenia schematu'
        assert [row.get_text('|', strip=True) for row in markup.select('.diagram-connections tbody tr')] == ['1|2|Skierowane|Tak', '2|1|Skierowane|Nie']
        assert markup.get_text().count('Rysunek 1: Gotowość.') == 1
        assert 'conformsTo' not in archive.read('EPUB/package.opf').decode()


def test_compact_source_is_natural_markup_without_durable_lineage() -> None:
    plan = _compact_plan(
        "<html><body><p>The <em>proposal</em> cites "
        '<a href="#note" role="doc-noteref">note <sup>1</sup></a>, uses '
        "<code>&lt;value&gt;</code>.<br>Next.</p></body></html>"
    )
    source = compact_markup.compact_source_block(plan.blocks[0], number=1)

    assert set(source) == {"n", "role", "content", "fields"}
    assert "The <em>proposal</em>" in source["content"]
    assert '<a r="' in source["content"]
    assert "<lit " in source["content"]
    assert "<code>" in source["content"]
    assert "<br/>" in source["content"]
    assert "href" not in source["content"]
    assert "token_id" not in source["content"]
    assert "source_plan" not in source["content"]


def test_compact_source_round_trip_restores_exact_source_owned_semantics() -> None:
    plan = _compact_plan(
        '<html><body><p class="body">See <a href="#target">this '
        '<em class="book-title">book</em></a>.</p></body></html>'
    )
    block = plan.blocks[0]
    source = compact_markup.compact_source_block(block, number=1)
    candidate = _normalize(plan, block, source)

    assert target_validation.validate_target_candidate(
        plan, "plan-1", block, candidate
    ).valid
    rendered = target_rendering.render_target_block_fragment(
        plan,
        "plan-1",
        block,
        candidate,
        target_language="pl",
    )
    assert 'class="body"' in rendered
    assert 'href="#target"' in rendered
    assert 'class="book-title"' in rendered
    assert " r=" not in rendered
    assert "<lit" not in rendered


def test_implicit_outer_wrappers_are_host_owned_and_restored() -> None:
    plan = _compact_plan(
        '<html><body><p><a href="#note"><strong>Whole block.</strong></a></p>'
        '<aside id="note" role="doc-footnote"><p>Note.</p></aside></body></html>'
    )
    block = plan.blocks[0]
    source = compact_markup.compact_source_block(
        block,
        number=1,
        implicit_outer_wrappers=True,
    )

    assert source["content"] == "Whole block."
    candidate = _normalize(
        plan,
        block,
        source,
        content="Cały blok.",
        implicit_outer_wrappers=True,
    )
    with pytest.raises(BlockContractError, match="compact-markup-tag"):
        _normalize(
            plan,
            block,
            source,
            content="<strong>Cały blok.</strong>",
            implicit_outer_wrappers=True,
        )
    assert target_validation.validate_target_candidate(
        plan, "plan-1", block, candidate
    ).valid
    editor = compact_markup.compact_candidate_projection(block, candidate)
    assert '<a r="1"><strong>Cały blok.</strong></a>' == editor["content"]
    rendered = target_rendering.render_target_block_fragment(
        plan,
        "plan-1",
        block,
        candidate,
        target_language="pl",
    )
    assert '<a href="#note"' in rendered
    assert ">Cały blok.</strong></a>" in rendered


def test_implicit_outer_wrappers_do_not_hide_internal_formatting() -> None:
    plan = _compact_plan(
        "<html><body><p>Before <em>inside</em> after.</p></body></html>"
    )
    block = plan.blocks[0]
    source = compact_markup.compact_source_block(
        block,
        number=1,
        implicit_outer_wrappers=True,
    )

    assert source["content"] == "Before <em>inside</em> after."
    with pytest.raises(BlockContractError, match="compact-markup-cardinality"):
        _normalize(
            plan,
            block,
            source,
            content="Przed środkiem i po nim.",
            implicit_outer_wrappers=True,
        )


def test_v3_replay_still_requires_complete_outer_wrappers() -> None:
    plan = _compact_plan("<html><body><p><em>Whole block.</em></p></body></html>")
    block = plan.blocks[0]
    source = compact_markup.compact_source_block(block, number=1)

    assert source["content"] == "<em>Whole block.</em>"
    with pytest.raises(BlockContractError, match="compact-markup-cardinality"):
        _normalize(plan, block, source, content="Cały blok.")


def test_compact_result_allows_natural_sentence_merge_inside_one_block() -> None:
    plan = _compact_plan("<html><body><p>One <em>part</em>. Two.</p></body></html>")
    block = plan.blocks[0]
    source = compact_markup.compact_source_block(block, number=1)
    candidate = _normalize(
        plan,
        block,
        source,
        content="Jedno zdanie z <em>wyróżnieniem</em>.",
    )

    assert target_validation.validate_target_candidate(
        plan, "plan-1", block, candidate
    ).valid
    assert len(block.anchors) >= 2


def test_compact_result_accepts_common_html_named_entities_as_text() -> None:
    plan = _compact_plan("<html><body><p>One space.</p></body></html>")
    block = plan.blocks[0]
    source = compact_markup.compact_source_block(block, number=1)

    candidate = _normalize(plan, block, source, content="Jedna&nbsp;spacja.")

    assert candidate.parts[0].text == "Jedna\u00a0spacja."
    assert target_validation.validate_target_candidate(
        plan, "plan-1", block, candidate
    ).valid


def test_compact_result_decodes_model_overescaped_unicode_entities() -> None:
    plan = _compact_plan("<html><body><p>Quoted text.</p></body></html>")
    block = plan.blocks[0]
    source = compact_markup.compact_source_block(block, number=1)

    candidate = _normalize(
        plan,
        block,
        source,
        content=("&amp;lsquo;Dopóki&amp;rsquo; &amp;Uuml;bel &amp;ndash; &amp;sect; 5"),
    )

    assert candidate.parts[0].text == "‘Dopóki’ Übel – § 5"
    editor = compact_markup.compact_candidate_projection(block, candidate)
    assert editor["content"] == "‘Dopóki’ Übel – § 5"
    rendered = target_rendering.render_target_block_fragment(
        plan,
        "plan-1",
        block,
        candidate,
        target_language="pl",
    )
    assert "‘Dopóki’ Übel – § 5" in rendered
    assert "&amp;lsquo;" not in rendered


def test_compact_result_decodes_entities_in_translated_fields() -> None:
    plan = _compact_plan(
        '<html><body><img src="portrait.jpg" alt="Portrait"></body></html>'
    )
    block = next(item for item in plan.blocks if item.role == "accessibility_text")
    source = compact_markup.compact_source_block(block, number=1)

    candidate = _normalize(
        plan,
        block,
        source,
        fields=[{"name": "alt", "value": "B&ouml;hme &amp; Hegel"}],
    )

    assert candidate.lexical_attributes[0].value == "Böhme & Hegel"


def test_compact_result_preserves_source_literal_entity_notation() -> None:
    plan = _compact_plan(
        "<html><body><p>Write &amp;copy; for the copyright entity.</p>"
        '<img src="sample.png" alt="The literal &amp;copy; spelling">'
        "</body></html>"
    )
    paragraph = next(block for block in plan.blocks if block.role == "paragraph")
    paragraph_source = compact_markup.compact_source_block(paragraph, number=1)
    paragraph_candidate = _normalize(
        plan,
        paragraph,
        paragraph_source,
        content="Wpisz &amp;copy; jako encję praw autorskich.",
    )
    assert paragraph_candidate.parts[0].text == (
        "Wpisz &copy; jako encję praw autorskich."
    )

    accessibility = next(
        block for block in plan.blocks if block.role == "accessibility_text"
    )
    accessibility_source = compact_markup.compact_source_block(accessibility, number=2)
    accessibility_candidate = _normalize(
        plan,
        accessibility,
        accessibility_source,
        fields=[{"name": "alt", "value": "Dosłowny zapis &copy;"}],
    )
    assert accessibility_candidate.lexical_attributes[0].value == (
        "Dosłowny zapis &copy;"
    )


def test_source_entities_cross_the_compact_boundary_as_semantic_text() -> None:
    plan = _compact_plan(
        "<html><body><p>&uuml;bert&auml;gige Menschen &ndash; B&ouml;hme</p>"
        '<img src="portrait.png" alt="B&ouml;hme &amp; Hegel"></body></html>'
    )
    paragraph = next(block for block in plan.blocks if block.role == "paragraph")
    source = compact_markup.compact_source_block(paragraph, number=1)

    assert source["content"] == "übertägige Menschen – Böhme"
    candidate = _normalize(plan, paragraph, source)
    assert candidate.parts[0].text == "übertägige Menschen – Böhme"
    rendered = target_rendering.render_target_block_fragment(
        plan,
        "plan-1",
        paragraph,
        candidate,
        target_language="pl",
    )
    assert "übertägige Menschen – Böhme" in rendered
    assert "&amp;uuml;" not in rendered
    assert "&uuml;" not in rendered

    accessibility = next(
        block for block in plan.blocks if block.role == "accessibility_text"
    )
    accessibility_source = compact_markup.compact_source_block(accessibility, number=2)
    assert accessibility_source["fields"] == [{"name": "alt", "value": "Böhme & Hegel"}]
    accessibility_candidate = _normalize(plan, accessibility, accessibility_source)
    assert accessibility_candidate.lexical_attributes[0].value == "Böhme & Hegel"


@pytest.mark.parametrize(
    "replacement, code",
    [
        (lambda content: content.replace("<em>", ""), "compact-markup-xml"),
        (
            lambda content: content.replace("<em>", '<em class="invented">'),
            "compact-markup-attribute",
        ),
        (lambda content: content.replace("<em>", "<strong>"), "compact-markup-xml"),
        (lambda content: "<!DOCTYPE x>" + content, "compact-markup-active"),
    ],
)
def test_compact_result_rejects_markup_drift(replacement, code) -> None:
    plan = _compact_plan("<html><body><p>Text <em>inside</em>.</p></body></html>")
    block = plan.blocks[0]
    source = compact_markup.compact_source_block(block, number=1)

    with pytest.raises(BlockContractError, match=code):
        _normalize(plan, block, source, content=replacement(source["content"]))


@pytest.mark.parametrize("encoded", ["&#13;", "&#x0D;", "&#13;&#10;"])
def test_compact_literal_carriage_returns_survive_xml_round_trip(encoded):
    source_html = f"<html><body><p>A{encoded}B</p></body></html>"
    plan = _compact_plan(source_html)
    block = plan.blocks[0]
    source = compact_markup.compact_source_block(block, number=1)
    assert "&#13;" in source["content"]
    candidate = _normalize(plan, block, source)
    assert target_validation.validate_target_candidate(plan, "plan-1", block, candidate).valid
    target = target_rendering.render_target_document(
        plan, "plan-1", {block.id: candidate}, target_language="pl",
    )
    assert target_rendering.validate_target_semantic_document(target).valid
    assert render_source_identity(plan) == source_html


def test_compact_result_rejects_changed_or_missing_literal() -> None:
    plan = _compact_plan(
        "<html><body><p>Use <code>&lt;value&gt;</code>.</p></body></html>"
    )
    block = plan.blocks[0]
    source = compact_markup.compact_source_block(block, number=1)
    changed = source["content"].replace("value", "other", 1)
    missing = re.sub(r'<lit r="\d+">.*?</lit>', "", source["content"], count=1)

    with pytest.raises(BlockContractError, match="compact-literal-value"):
        _normalize(plan, block, source, content=changed)
    with pytest.raises(BlockContractError, match="compact-literal-cardinality"):
        _normalize(plan, block, source, content=missing)


def test_compact_fields_translate_accessibility_text_without_fake_prose() -> None:
    plan = _compact_plan(
        '<html><body><figure><img src="cover.jpg" alt="A red house">'
        "<figcaption>Cover.</figcaption></figure></body></html>"
    )
    block = next(item for item in plan.blocks if item.role == "accessibility_text")
    source = compact_markup.compact_source_block(block, number=1)
    assert source["content"] == ""
    assert source["fields"] == [{"name": "alt", "value": "A red house"}]

    candidate = _normalize(
        plan,
        block,
        source,
        fields=[{"name": "alt", "value": "Czerwony dom"}],
    )
    assert target_validation.validate_target_candidate(
        plan, "plan-1", block, candidate
    ).valid


def test_continuous_editor_projection_round_trips_to_durable_candidate() -> None:
    plan = _compact_plan(
        "<html><body><p>Text <strong>inside</strong>.</p></body></html>"
    )
    block = plan.blocks[0]
    source = compact_markup.compact_source_block(block, number=1)
    candidate = _normalize(
        plan,
        block,
        source,
        content="Tekst <strong>wewnątrz</strong>.",
    )
    editor = compact_markup.compact_candidate_projection(block, candidate)

    assert editor["contract_version"] == compact_markup.COMPACT_EDITOR_CONTRACT_VERSION
    restored = compact_markup.normalize_editor_candidate(
        plan=plan,
        plan_id="plan-1",
        block=block,
        value=editor,
    )
    assert restored.to_dict() == candidate.to_dict()


def test_provider_compact_result_cannot_invent_target_only_formatting() -> None:
    plan = _compact_plan("<html><body><p>Plain source text.</p></body></html>")
    block = plan.blocks[0]
    source = compact_markup.compact_source_block(block, number=1)
    with pytest.raises(BlockContractError, match="compact-markup-tag"):
        _normalize(
            plan,
            block,
            source,
            content="Zwykły <strong>tekst</strong> docelowy.",
        )


@pytest.mark.parametrize("tag", ["b", "em", "i", "strong", "sub", "sup", "u"])
def test_owner_editor_can_add_target_only_inline_formatting(tag: str) -> None:
    plan = _compact_plan("<html><body><p>Plain source text.</p></body></html>")
    block = plan.blocks[0]
    candidate = compact_markup.normalize_editor_candidate(
        plan=plan,
        plan_id="plan-1",
        block=block,
        value={
            "contract_version": compact_markup.COMPACT_EDITOR_CONTRACT_VERSION,
            "content": f"Zwykły <{tag}>tekst</{tag}> docelowy.",
            "fields": [],
        },
    )

    assert target_validation.validate_target_candidate(
        plan, "plan-1", block, candidate
    ).valid
    assert [part.to_dict() for part in candidate.parts] == [
        {"kind": "text", "text": "Zwykły "},
        {"kind": "format", "tag": tag, "boundary": "open"},
        {"kind": "text", "text": "tekst"},
        {"kind": "format", "tag": tag, "boundary": "close"},
        {"kind": "text", "text": " docelowy."},
    ]
    rendered = target_rendering.render_target_block_fragment(
        plan,
        "plan-1",
        block,
        candidate,
        target_language="pl",
    )
    assert f"Zwykły <{tag}>tekst</{tag}> docelowy." in rendered
    assert (
        compact_markup.compact_candidate_projection(block, candidate)["content"]
        == f"Zwykły <{tag}>tekst</{tag}> docelowy."
    )


def test_all_supported_semantic_block_roles_round_trip_through_compact_markup() -> None:
    plan = _compact_plan(
        """<html><body><h1>Title</h1><p>Paragraph.</p><blockquote>Quote.</blockquote>
        <ol><li>Item.</li></ol><dl><dt>Term</dt><dd>Definition.</dd></dl>
        <table><caption>Table.</caption><tr><th>Head</th><td>Cell</td></tr></table>
        <figure><img src="x.jpg" alt="Image"><figcaption>Figure.</figcaption></figure>
        <aside role="doc-footnote"><p>Note.</p></aside></body></html>"""
    )

    assert plan.blocks
    for number, block in enumerate(plan.blocks, 1):
        source = compact_markup.compact_source_block(block, number=number)
        candidate = _normalize(plan, block, source)
        assert target_validation.validate_target_candidate(
            plan, "plan-1", block, candidate
        ).valid
        implicit_source = compact_markup.compact_source_block(
            block,
            number=number,
            implicit_outer_wrappers=True,
        )
        implicit_candidate = _normalize(
            plan,
            block,
            implicit_source,
            implicit_outer_wrappers=True,
        )
        assert target_validation.validate_target_candidate(
            plan, "plan-1", block, implicit_candidate
        ).valid


def test_identity_renderer_rejects_stored_value_and_wire_tampering() -> None:
    plan, _html = _plan()
    first = plan.blocks[0]
    object.__setattr__(first.slots[0], "source_value", "Changed")
    with pytest.raises(BlockContractError):
        render_source_identity(plan)


@pytest.mark.parametrize(
    "html",
    [
        "<html><body><div><em>Hello</em> world.</div></body></html>",
        "<html><body><div>Hello <em>world</em></div></body></html>",
        "<html><body><div><strong><em>Hello</em></strong> <code>x</code> world.</div></body></html>",
    ],
)
def test_direct_inline_runs_identity_round_trip(html: str) -> None:
    digest = hashlib.sha256(html.encode()).hexdigest()
    snapshot = AttestedDocumentSnapshot(
        id="doc-1",
        document_id="doc-1",
        output_html=html,
        content_hash=digest,
        semantic_content_hash=digest,
        metadata={},
        provenance={},
    )
    plan = plan_document_blocks(snapshot, project_id="project-1", source_language="en")
    assert render_source_identity(plan) == html


@pytest.mark.parametrize(
    "html",
    [
        '<html><body><aside role="doc-footnote"><p>Note <em>emphasis</em> end.</p></aside></body></html>',
        '<html><body><aside role="doc-footnote"><p>Note <a href="#n">link</a> end.</p></aside></body></html>',
    ],
)
def test_note_children_inherit_without_inline_phantom_blocks(html: str) -> None:
    digest = hashlib.sha256(html.encode()).hexdigest()
    snapshot = AttestedDocumentSnapshot(
        id="doc-1",
        document_id="doc-1",
        output_html=html,
        content_hash=digest,
        semantic_content_hash=digest,
        metadata={},
        provenance={},
    )
    plan = plan_document_blocks(snapshot, project_id="project-1", source_language="en")
    assert len(plan.blocks) == 1
    assert plan.blocks[0].role == "note_body"
    assert plan.blocks[0].projection in {"Note emphasis end.", "Note link end."}
    assert {token.kind for token in plan.blocks[0].protected_inline} == {
        "open",
        "close",
    }
    assert render_source_identity(plan) == html


def test_identity_rejects_rehashed_coverage_bijection_tamper() -> None:
    from iarf_assistant.addons.translator.block_contract import CoverageWitness

    plan, _html = _plan()
    index = next(
        index
        for index, entry in enumerate(plan.coverage.entries)
        if entry.disposition == "translatable"
    )
    entries = list(plan.coverage.entries)
    entries[index] = replace(entries[index], block_id=None)
    object.__setattr__(plan, "coverage", CoverageWitness.create(tuple(entries)))
    object.__setattr__(plan, "plan_hash", plan._expected_hash())
    with pytest.raises(BlockContractError, match="plan-canonical-rebuild"):
        render_source_identity(plan)


def test_identity_rejects_rehashed_block_and_inline_census_tampering() -> None:
    from iarf_assistant.addons.translator.block_contract import CoverageWitness

    plan, _html = _plan()
    removed = plan.blocks[-1]
    object.__setattr__(plan, "blocks", plan.blocks[:-1])
    object.__setattr__(
        plan,
        "coverage",
        CoverageWitness.create(
            tuple(
                entry for entry in plan.coverage.entries if entry.block_id != removed.id
            )
        ),
    )
    object.__setattr__(plan, "plan_hash", plan._expected_hash())
    with pytest.raises(BlockContractError, match="plan-canonical-rebuild"):
        render_source_identity(plan)

    plan, _html = _plan()
    first = plan.blocks[0]
    token = first.protected_inline[0]
    object.__setattr__(
        first,
        "protected_inline",
        tuple(item for item in first.protected_inline if item.id != token.id),
    )
    object.__setattr__(
        first, "wire", tuple(item for item in first.wire if item.ref_id != token.id)
    )
    object.__setattr__(plan, "plan_hash", plan._expected_hash())
    with pytest.raises(BlockContractError, match="plan-canonical-rebuild"):
        render_source_identity(plan)
