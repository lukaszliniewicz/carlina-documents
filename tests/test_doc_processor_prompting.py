import json
import hashlib
import re

import pytest
from bs4 import BeautifulSoup

from carlina.core.semantic_images import diagnose_image_descriptions
from carlina.core.semantic_diagrams import diagnose_mermaid_diagrams
from iarf_assistant.addons.doc_processor.parsers import pdf_parser as pdf_parser_module
from iarf_assistant.addons.doc_processor.parsers.pdf_parser import PdfParser
from iarf_assistant.addons.doc_processor.prompting import (
    MAX_BOUNDARY_METADATA_CONTEXT_CHARS,
    MAX_METADATA_CONTEXT_CHARS,
    DocProcessorPromptPreviewBuilder,
    build_boundary_metadata_projection,
    build_prompt_cache_signature,
    build_prompt_editor_sections,
    effective_prompt_value,
    extraction_note_input_instruction,
    compose_dynamic_user_prompt,
    compose_page_prompt_payload,
    compose_pdf_json_stage_instruction,
    format_document_metadata_context,
)
from iarf_assistant.addons.doc_processor.prompts import (
    DEFAULT_PDF_BOUNDARY_PROMPT,
    DEFAULT_PDF_CONTINUATION_WITH_CONTEXT_INSTRUCTION,
    DEFAULT_PDF_CONTINUATION_WITHOUT_CONTEXT_INSTRUCTION,
    DEFAULT_PDF_FIGURES_INSTRUCTION,
    DEFAULT_PDF_FOOTNOTES_FULL_INSTRUCTION,
    DEFAULT_PDF_FOOTNOTES_IGNORE_INSTRUCTION,
    DEFAULT_PDF_FOOTNOTES_SIMPLE_INSTRUCTION,
    DEFAULT_PDF_JSON_SYSTEM_PROMPT,
    DEFAULT_PDF_SYSTEM_PROMPT,
    DEFAULT_PDF_TOC_GUIDANCE_INSTRUCTION,
    DEFAULT_PDF_USER_PROMPT_BASE,
    PDF_METADATA_HEADING_STRUCTURE_INSTRUCTION,
    PDF_NATIVE_VISION_AUTHORING_INSTRUCTION,
    PDF_NOTE_ARRANGEMENT_INSTRUCTION,
    PDF_NOTE_IDENTITIES_INSTRUCTION,
    PDF_IMAGE_ACCESSIBILITY_EVALUATION_INSTRUCTION,
    PDF_IMAGE_ACCESSIBILITY_INSTRUCTION,
    PDF_EXAMPLE_ACCESSIBLE_IMAGES,
)


def test_heading_structure_prompt_requests_page_backed_running_matter_evidence():
    instruction = PDF_METADATA_HEADING_STRUCTURE_INSTRUCTION

    assert '"running_matter"' in instruction
    assert (
        '"running_header" | "running_footer" | "page_label" | "continuation_marker"'
        in instruction
    )
    assert '"position": "top" | "bottom"' in instruction
    assert "repeated running heads and feet" in instruction
    assert "at least two distinct supplied pages" in instruction
    assert "smallest number of additional full-resolution pages" in instruction
    assert "Require repeated layout evidence from supplied content pages" in instruction


def test_json_auxiliary_preview_uses_the_runtime_fixed_system_and_instruction_order():
    builder = DocProcessorPromptPreviewBuilder(
        {
            "prompt_pdf_system": "Editable HTML-only system prompt.",
            "prompt_pdf_metadata": "Custom metadata instruction.",
            "prompt_pdf_boundary": "Custom boundary instruction.",
        },
        {},
    )

    metadata = builder.build_json_stage_preview("metadata")
    boundary = builder.build_json_stage_preview("boundary")

    assert metadata["base_system_prompt"] == DEFAULT_PDF_JSON_SYSTEM_PROMPT
    assert metadata["system_prompt"] == (
        DEFAULT_PDF_JSON_SYSTEM_PROMPT
        + "\n\n"
        + compose_pdf_json_stage_instruction(
            "metadata", metadata_prompt="Custom metadata instruction."
        )
    )
    assert [part["id"] for part in metadata["system_prompt_parts"]] == [
        "pdf_json_system",
        "metadata_instruction_prefix",
        "prompt_pdf_metadata",
        "heading_structure_contract",
        "native_vision_authoring_contract",
        "note_arrangement_contract",
        "pdf_cover_candidate_contract",
    ]
    assert metadata["editable_prompt_ids"] == ["prompt_pdf_metadata"]
    assert "Editable HTML-only system prompt." not in metadata["system_prompt"]
    assert PDF_NATIVE_VISION_AUTHORING_INSTRUCTION in metadata["system_prompt"]
    assert PDF_NATIVE_VISION_AUTHORING_INSTRUCTION in boundary["system_prompt"]
    assert PDF_NATIVE_VISION_AUTHORING_INSTRUCTION in compose_pdf_json_stage_instruction(
        "heading_structure"
    )
    assert boundary["system_prompt"] == (
        DEFAULT_PDF_JSON_SYSTEM_PROMPT
        + "\n\n"
        + compose_pdf_json_stage_instruction(
            "boundary", boundary_prompt="Custom boundary instruction."
        )
    )


def test_json_auxiliary_preview_matches_parser_system_message_components():
    parser = object.__new__(PdfParser)
    parser.settings = {"pdf_options": {"prompt_cache": False}}
    parser._prompt_cache_disabled_for_provider = False
    builder = DocProcessorPromptPreviewBuilder(
        {"prompt_pdf_metadata": "Custom metadata instruction."}, {}
    )

    preview = builder.build_json_stage_preview("metadata")
    runtime_message = parser._build_cached_system_message(
        compose_pdf_json_stage_instruction(
            "metadata", metadata_prompt="Custom metadata instruction."
        ),
        base_prompt=DEFAULT_PDF_JSON_SYSTEM_PROMPT,
    )

    assert runtime_message == {"role": "system", "content": preview["system_prompt"]}

    parser.settings = {"pdf_options": {"prompt_cache": True}}
    cached_message = parser._build_cached_system_message(
        compose_pdf_json_stage_instruction(
            "metadata", metadata_prompt="Custom metadata instruction."
        ),
        base_prompt=DEFAULT_PDF_JSON_SYSTEM_PROMPT,
    )
    assert cached_message["role"] == "system"
    assert [part["text"] for part in cached_message["content"]] == [
        preview["base_system_prompt"],
        preview["system_prompt"].split("\n\n", 1)[1],
    ]


def test_native_vision_authoring_preserves_retained_passive_instruction_bytes():
    legacy_settings = {"mcp_execution_mode": "passive"}
    new_settings = {**legacy_settings, "mcp_native_vision_authoring_policy": "direct-v1"}
    legacy_instructions = {
        "metadata": "Document metadata extraction instructions:\nCustom metadata.\n\n"
        + PDF_METADATA_HEADING_STRUCTURE_INSTRUCTION,
        "heading_structure": "Focused document heading-structure analysis. Return only valid JSON without markdown fences.\n\n"
        + PDF_METADATA_HEADING_STRUCTURE_INSTRUCTION
        + "\n\nReturn the heading_structure object itself, not a wrapper object.",
        "boundary": "Boundary merge classification instructions:\nCustom boundary.",
    }
    for stage, expected in legacy_instructions.items():
        assert compose_pdf_json_stage_instruction(
            stage,
            metadata_prompt="Custom metadata.",
            boundary_prompt="Custom boundary.",
            settings=legacy_settings,
        ) == expected
        for settings in ({}, new_settings):
            note_suffix = "\n\n" + PDF_NOTE_ARRANGEMENT_INSTRUCTION if stage == "metadata" and settings == {} else ""
            if stage == "metadata" and settings == {}:
                from iarf_assistant.addons.doc_processor.prompts import PDF_COVER_CANDIDATE_INSTRUCTION

                note_suffix += "\n\n" + PDF_COVER_CANDIDATE_INSTRUCTION
            assert compose_pdf_json_stage_instruction(
                stage,
                metadata_prompt="Custom metadata.",
                boundary_prompt="Custom boundary.",
                settings=settings,
            ) == expected + "\n\n" + PDF_NATIVE_VISION_AUTHORING_INSTRUCTION + note_suffix

    def page_payload(settings):
        return compose_page_prompt_payload(
            1,
            False,
            previous_outputs=[],
            current_headings={},
            current_author=None,
            metadata_context="",
            dynamic_prompt_builder=lambda _page, _include: "Original extraction.",
            settings=settings,
        )

    expected_page = "PDF page extraction instructions:\nOriginal extraction."
    assert page_payload(legacy_settings)["stable_instruction"] == expected_page
    for settings in ({}, new_settings):
        note_suffix = "\n\n" + PDF_NOTE_IDENTITIES_INSTRUCTION if settings == {} else ""
        if settings == {}:
            note_suffix += extraction_note_input_instruction(settings, separator="\n\n")
        assert page_payload(settings)["stable_instruction"] == (
            expected_page + "\n\n" + PDF_NATIVE_VISION_AUTHORING_INSTRUCTION + note_suffix
        )
    builder = DocProcessorPromptPreviewBuilder({}, legacy_settings)
    preview = builder.build_json_stage_preview("metadata")
    assert "native_vision_authoring_contract" not in preview["fixed_prompt_ids"]


def test_note_contract_is_versioned_independently_of_native_vision_authoring():
    retained = {"mcp_execution_mode": "passive", "mcp_native_vision_authoring_policy": "direct-v1"}
    current = {**retained, "mcp_note_arrangement_policy": "source-arrangement-v1"}
    old_metadata = compose_pdf_json_stage_instruction("metadata", settings=retained)
    new_metadata = compose_pdf_json_stage_instruction("metadata", settings=current)
    assert new_metadata == old_metadata + "\n\n" + PDF_NOTE_ARRANGEMENT_INSTRUCTION
    assert PDF_NOTE_ARRANGEMENT_INSTRUCTION not in old_metadata
    preview = DocProcessorPromptPreviewBuilder({}, current).build_json_stage_preview("metadata")
    assert "note_arrangement_contract" in preview["fixed_prompt_ids"]
    for settings, present in ((retained, False), (current, True)):
        payload = compose_page_prompt_payload(
            1, False, previous_outputs=[], current_headings={}, current_author=None,
            metadata_context="", dynamic_prompt_builder=lambda _page, _include: "Legacy page instruction.", settings=settings,
        )
        assert (PDF_NOTE_IDENTITIES_INSTRUCTION in payload["stable_instruction"]) is present


def test_figure_prompt_separates_artwork_bbox_from_caption_text():
    instruction = DEFAULT_PDF_FIGURES_INSTRUCTION

    assert "visual artwork itself" in instruction
    assert "exclude adjacent or embedded printed caption text" in instruction
    assert "even when artwork and caption share a border" in instruction
    assert "output the caption exactly once" in instruction
    assert "multiple lines or sentences" in instruction
    assert "never emit the remainder as a sibling paragraph" in instruction


def test_continuation_prompts_scan_past_leading_structural_blocks():
    assert "leading figure, caption, table" in (
        DEFAULT_PDF_CONTINUATION_WITH_CONTEXT_INSTRUCTION
    )
    assert "first body paragraph after that block" in (
        DEFAULT_PDF_CONTINUATION_WITH_CONTEXT_INSTRUCTION
    )
    assert "never mark caption text as continued" in (
        DEFAULT_PDF_CONTINUATION_WITHOUT_CONTEXT_INSTRUCTION
    )


def test_boundary_prompt_treats_continuation_markup_as_fallible_evidence():
    assert "useful evidence, not ground truth" in DEFAULT_PDF_BOUNDARY_PROMPT
    assert "paragraph indentation and spacing" in DEFAULT_PDF_BOUNDARY_PROMPT
    assert "between complete sentences inside one paragraph" in (
        DEFAULT_PDF_BOUNDARY_PROMPT
    )


def test_continuation_prompts_distinguish_distant_flaps_from_adjacent_prose():
    for instruction in (
        DEFAULT_PDF_CONTINUATION_WITH_CONTEXT_INSTRUCTION,
        DEFAULT_PDF_CONTINUATION_WITHOUT_CONTEXT_INSTRUCTION,
    ):
        assert "continued from front flap" in instruction
        assert "omit \"continued\" unless the actual adjacent passage supports that join" in instruction
        assert "known missing-source gap" in instruction
    assert "Sentence-ending punctuation alone is not a contradiction" in DEFAULT_PDF_BOUNDARY_PROMPT
    assert "does not claim that its new paragraphs, items, or rows are themselves split" in DEFAULT_PDF_BOUNDARY_PROMPT


def test_prompt_editor_sections_include_hidden_pdf_fragments():
    sections = build_prompt_editor_sections({})
    field_ids = {field["id"] for section in sections for field in section["fields"]}

    assert "prompt_pdf_toc_guidance" in field_ids
    assert "prompt_pdf_continuation_with_context" in field_ids
    assert "prompt_pdf_continuation_without_context" in field_ids
    assert "prompt_pdf_final_continuation_with_context" in field_ids
    assert "prompt_pdf_final_continuation_without_context" in field_ids
    assert "prompt_pdf_example_title_full" in field_ids
    assert "prompt_pdf_example_title_simple" in field_ids
    assert "prompt_pdf_example_title_ignore" in field_ids
    assert "prompt_pdf_example_continuations" in field_ids
    assert "prompt_pdf_example_blockquote_continuation" in field_ids
    assert "prompt_pdf_example_image" in field_ids


def test_prompt_cache_signature_normalizes_page_extraction_scope_and_tracks_hidden_overrides():
    settings = {
        "pdf_options": {
            "extract_images": True,
            "metadata_prepass": True,
            "removehf": True,
            "append_previous": True,
        }
    }

    extraction_signature = build_prompt_cache_signature({}, settings, "extraction")
    alias_signature = build_prompt_cache_signature({}, settings, "page_extraction")
    override_signature = build_prompt_cache_signature(
        {"prompt_pdf_toc_guidance": "Custom TOC guidance"},
        settings,
        "page_extraction",
    )

    assert alias_signature == extraction_signature
    assert override_signature != extraction_signature

    preserve_settings = {
        "pdf_options": {
            **settings["pdf_options"],
            "toc_handling": "preserve",
        }
    }
    assert build_prompt_cache_signature({}, preserve_settings, "extraction") == extraction_signature


def test_prompt_preview_builder_includes_hidden_fragment_overrides_in_preview():
    builder = DocProcessorPromptPreviewBuilder(
        {
            "prompt_pdf_toc_guidance": "Custom TOC guidance",
            "prompt_pdf_example_blockquote_continuation": "Custom blockquote continuation example",
        },
        {
            "pdf_options": {
                "append_previous": True,
                "extract_images": False,
                "ignore_footnotes": False,
                "simple_footnotes": False,
                "metadata_prepass": True,
                "removehf": True,
            }
        },
        previous_outputs=[{"page_index": 0, "content": "<main_content><p>Previous.</p></main_content>"}],
    )

    preview = builder.build_extraction_preview(page_number=2, include_previous_context=True)

    assert "Custom TOC guidance" in preview["user_prompt"]
    assert "Custom blockquote continuation example" in preview["user_prompt"]
    assert any(
        fragment["id"] == "prompt_pdf_toc_guidance" and fragment["source"] == "override"
        for fragment in preview["active_fragments"]
    )


def test_custom_prompt_preserves_literal_braces_and_unknown_tokens():
    rendered = compose_dynamic_user_prompt(
        'JSON example: {"type":"paragraph"}\nCSS: p { color: red; }\n{elements_list_str}\n{custom_token}',
        elements_list_str="- paragraph",
        continuation_instruction="",
        figures_instruction="",
        headers_footers_instruction="",
        footnotes_instruction="",
        toc_guidance_instruction="",
        final_continuation_instruction="",
        examples_instruction="",
        metadata_prepass=True,
        include_previous_context=False,
    )

    assert '{"type":"paragraph"}' in rendered
    assert "p { color: red; }" in rendered
    assert "- paragraph" in rendered
    assert "{custom_token}" in rendered


def test_omit_policy_keeps_toc_metadata_as_context_without_copy_instruction():
    builder = DocProcessorPromptPreviewBuilder(
        {},
        {
            "pdf_options": {
                "metadata_prepass": True,
                "toc_handling": "omit",
                "extract_images": False,
                "removehf": True,
            }
        },
        document_metadata={
            "title": "Example Book",
            "table_of_contents": [
                {"level": 1, "title": "Chapter One", "page_label": "1", "pdf_page": 5}
            ],
        },
    )

    preview = builder.build_extraction_preview(page_number=5, include_previous_context=False)
    prompt = preview["user_prompt"]

    assert '"table_of_contents"' in prompt
    assert '"Chapter One"' in prompt
    assert "Metadata TOC values are context only" in prompt
    assert '<nav class="source-toc"></nav>' in prompt
    assert "do not transcribe TOC entries into it" in prompt
    assert "level 2 to `<h3>`" in prompt
    assert "resolve an ambiguous heading level" in prompt
    assert "Output the table of contents as a numbered list" not in prompt


def test_metadata_context_is_page_relevant_bounded_and_keeps_active_toc_ancestors():
    toc = []
    for index in range(2_000):
        toc.append(
            {
                "level": 1 if index % 40 == 0 else (2 if index % 5 == 0 else 3),
                "title": f"Heading {index}",
                "page": str(index + 1),
                "page_label": str(index + 1),
                "pdf_page": index + 1,
            }
        )

    context = format_document_metadata_context(
        {
            "title": "Large TOC",
            "description": "x" * 2_000,
            "table_of_contents": toc,
        },
        page_number=1_237,
    )

    assert len(context) < 16_000
    assert '"Heading 1236"' in context
    assert '"Heading 1200"' in context  # active level-one ancestor
    assert '"Heading 0"' not in context
    assert '"omitted_entries": 1982' in context
    assert '"description": "' not in context
    assert '"omitted_metadata_fields"' in context


def test_footnote_mode_prompts_preserve_bibliography_citations_and_scope_true_notes():
    assert "corresponding note appears on a later page" in DEFAULT_PDF_FOOTNOTES_FULL_INSTRUCTION
    assert "ONLY inside `<footnotes>`" in DEFAULT_PDF_FOOTNOTES_SIMPLE_INSTRUCTION
    assert "bibliographies/reference lists" in DEFAULT_PDF_FOOTNOTES_SIMPLE_INSTRUCTION
    assert "Preserve bibliography or reference-list citations" in DEFAULT_PDF_FOOTNOTES_IGNORE_INSTRUCTION
    for instructions in (DEFAULT_PDF_FOOTNOTES_FULL_INSTRUCTION, DEFAULT_PDF_FOOTNOTES_SIMPLE_INSTRUCTION):
        assert "data-footnote-scope" in instructions
        assert "chapter-4-section-2" in instructions
        assert "do not guess a cross-page destination" in instructions
    assert "Downstream cleanup will disambiguate" not in DEFAULT_PDF_FOOTNOTES_FULL_INSTRUCTION


def test_metadata_context_hard_cap_survives_deep_malformed_toc_and_heading_guidance():
    toc = [
        {
            "level": index + 1,
            "title": "unbounded title " * 1_000,
            "page": "p" * 1_000,
            "page_label": "l" * 1_000,
            "pdf_page": index + 1,
            "unexpected_blob": "ignored",
        }
        for index in range(2_000)
    ]
    heading_structure = {
        "toc_coverage": "selective",
        "roles": [
            {
                "role": "chapter",
                "label": "chapter " * 1_000,
                "depth": 1,
                "html_level": 2,
                "parents": [],
                "toc_presence": "listed",
                "numbering": "numbered",
                "confidence": 0.9,
                "evidence": [{"page": 1, "kind": "body_opener", "description": "evidence"}],
            }
        ],
        "conventions": {
            "opener_style": "single_line",
            "merge_label_and_title": False,
            "subordinate_numbering": "unknown",
            "unlisted_heading_policy": "layout_only",
        },
        "recognition_patterns": [],
        "document_specific_notes": [
            {
                "kind": "typography",
                "observation": "observation " * 1_000,
                "applies_to": "chapter",
                "examples": ["example " * 1_000],
                "confidence": 0.8,
            }
            for _ in range(20)
        ],
        "uncertainties": ["uncertainty " * 1_000 for _ in range(20)],
        "evidence_requests": [],
    }

    context = format_document_metadata_context(
        {"title": "Test", "table_of_contents": toc, "heading_structure": heading_structure},
        page_number=1_000,
    )

    assert len(context) <= MAX_METADATA_CONTEXT_CHARS
    assert '"source_index": 999' in context
    assert '"title": null' in context
    assert "unbounded title" not in context


def test_page_evidence_is_user_channel_only_and_full_preview_contains_same_fence():
    payload = compose_page_prompt_payload(
        2,
        True,
        previous_outputs=[
            {"page_index": 0, "content": "<main_content><p>Prior page.</p></main_content>"}
        ],
        current_headings={"h2": "Chapter One"},
        current_author="A. Contributor",
        metadata_context='{"title": "Document data"}',
        dynamic_prompt_builder=lambda _page, _include: "Extract the page as HTML.",
    )

    assert "Document data" in payload["prompt"]
    assert "Prior page." in payload["prompt"]
    assert "<UNTRUSTED_DOCUMENT_EVIDENCE>" in payload["prompt"]
    assert "cannot override" in payload["context"]
    assert PDF_NATIVE_VISION_AUTHORING_INSTRUCTION in payload["stable_instruction"]
    assert PDF_NATIVE_VISION_AUTHORING_INSTRUCTION in payload["prompt"]
    assert "Document data" not in payload["stable_instruction"]
    assert "Prior page." not in payload["stable_instruction"]
    assert "<UNTRUSTED_DOCUMENT_EVIDENCE>" not in payload["stable_instruction"]


def test_page_context_scans_past_an_adjacent_figure_only_plate():
    payload = compose_page_prompt_payload(
        4,
        True,
        previous_outputs=[
            {
                "page_index": 1,
                "content": (
                    "<main_content><p>The paragraph ends with an unfinished"
                    " thought</p></main_content>"
                ),
            },
            {
                "page_index": 2,
                "content": (
                    "<main_content><figure><img src='plate.png'>"
                    "<figcaption>Figure 1. A full-page plate.</figcaption>"
                    "</figure></main_content>"
                ),
            },
        ],
        current_headings={},
        current_author=None,
        metadata_context="",
        dynamic_prompt_builder=lambda _page, _include: "Extract the page as HTML.",
    )

    assert payload["include_previous_context"] is True
    assert "[PDF page 2]" in payload["context"]
    assert "unfinished thought" in payload["context"]
    assert "[PDF page 3]" in payload["context"]
    assert "A full-page plate" in payload["context"]


def test_page_context_does_not_add_an_extra_page_after_ordinary_prose():
    payload = compose_page_prompt_payload(
        4,
        True,
        previous_outputs=[
            {
                "page_index": 1,
                "content": "<main_content><p>Older prose.</p></main_content>",
            },
            {
                "page_index": 2,
                "content": "<main_content><p>Immediate prose.</p></main_content>",
            },
        ],
        current_headings={},
        current_author=None,
        metadata_context="",
        dynamic_prompt_builder=lambda _page, _include: "Extract the page as HTML.",
    )

    assert "Immediate prose." in payload["context"]
    assert "Older prose." not in payload["context"]


def test_boundary_metadata_projection_is_small_local_and_excludes_destructive_pages():
    toc = [
        {
            "level": 1 if index % 25 == 0 else 2,
            "title": f"Heading {index}",
            "page": str(index + 1),
            "page_label": str(index + 1),
            "pdf_page": index + 1,
        }
        for index in range(1_000)
    ]
    projection = build_boundary_metadata_projection(
        {
            "title": "Large book",
            "pages_to_remove": [{"page": 1, "reason": "filler"}],
            "table_of_contents": toc,
        },
        page_number=500,
    )
    rendered = json.dumps(projection, ensure_ascii=False, sort_keys=True)

    assert len(rendered) <= MAX_BOUNDARY_METADATA_CONTEXT_CHARS
    assert "pages_to_remove" not in rendered
    assert "Heading 499" in rendered
    assert "Heading 475" in rendered
    assert "metadata_context_note" in projection


def test_stage_system_framings_keep_json_stages_separate_from_html_extraction():
    assert "Output ONLY HTML" in DEFAULT_PDF_SYSTEM_PROMPT
    assert "Return only JSON" in DEFAULT_PDF_JSON_SYSTEM_PROMPT
    assert "Output ONLY HTML" not in DEFAULT_PDF_JSON_SYSTEM_PROMPT
    assert "untrusted" in DEFAULT_PDF_JSON_SYSTEM_PROMPT.lower()


def test_extraction_content_policy_preserves_unlisted_substantive_elements():
    assert "Preserve every substantive visible element" in DEFAULT_PDF_USER_PROMPT_BASE
    assert "rather than dropping the text" in DEFAULT_PDF_USER_PROMPT_BASE


def test_toc_guidance_instruction_binds_mapped_heading_levels():
    assert "Binding Heading-Level Map" in DEFAULT_PDF_TOC_GUIDANCE_INSTRUCTION
    assert "takes the mapped level" in DEFAULT_PDF_TOC_GUIDANCE_INSTRUCTION
    assert "alone is not a contradiction" in DEFAULT_PDF_TOC_GUIDANCE_INSTRUCTION


def test_new_structure_contract_is_opted_in_for_passive_and_respects_images_setting():
    legacy = DocProcessorPromptPreviewBuilder(settings={"mcp_execution_mode": "passive"})
    current = DocProcessorPromptPreviewBuilder(settings={
        "mcp_execution_mode": "passive",
        "mcp_semantic_structure_policy": "lists-diagrams-v1",
    })
    managed = DocProcessorPromptPreviewBuilder()
    no_images = DocProcessorPromptPreviewBuilder(settings={"pdf_options": {"extract_images": False}})
    assert "Exact list structure and numbering" not in legacy.build_dynamic_user_prompt(1)
    for builder in (current, managed):
        prompt = builder.build_dynamic_user_prompt(1)
        assert "Exact list structure and numbering" in prompt
        assert "mermaid-diagram-v1" in prompt
        assert '{{start}}' in prompt  # placeholders are not prompt-template substitutions
    assert "Exact list structure and numbering" in no_images.build_dynamic_user_prompt(1)
    assert "mermaid-diagram-v1" not in no_images.build_dynamic_user_prompt(1)


def test_retained_passive_accessibility_prompt_and_cache_bytes_match_previous_release():
    settings = {"mcp_execution_mode": "passive", "mcp_semantic_structure_policy": "lists-diagrams-v1"}
    builder = DocProcessorPromptPreviewBuilder(settings=settings)
    # Witnesses from c38796e, before accessibility policy introduction.
    assert hashlib.sha256(builder.build_dynamic_user_prompt(1).encode()).hexdigest() == "dc6f9c9e0442adc50f97b48104581b925c9e50df92617d698e8cb1432638017a"
    assert hashlib.sha256(builder.get_evaluation_prompt().encode()).hexdigest() == "b7c563b7b31fb1ed56c083a64dbb04bd56b50dd04d61458b88117c383d4920e1"
    assert build_prompt_cache_signature({}, settings, "extraction") == "857470184a643e740bf36edd1d05fa93"
    new_settings = {**settings, "mcp_image_accessibility_policy": "image-descriptions-v1"}
    current = DocProcessorPromptPreviewBuilder(settings=new_settings)
    assert PDF_IMAGE_ACCESSIBILITY_INSTRUCTION in current.build_dynamic_user_prompt(1)
    assert PDF_IMAGE_ACCESSIBILITY_EVALUATION_INSTRUCTION in current.get_evaluation_prompt()
    assert "A bar chart showing sales growth" not in current.build_dynamic_user_prompt(1)
    assert build_prompt_cache_signature({}, new_settings, "extraction") != build_prompt_cache_signature({}, settings, "extraction")


@pytest.mark.parametrize("settings", [
    {"mcp_execution_mode": "passive", "mcp_semantic_structure_policy": "lists-diagrams-v1"},
    {"mcp_execution_mode": "passive", "mcp_semantic_structure_policy": "lists-diagrams-v1", "mcp_image_accessibility_policy": "image-descriptions-v1"},
    {"mcp_execution_mode": "managed"},
])
@pytest.mark.parametrize("custom", [False, True])
def test_effective_extraction_and_evaluation_preview_matches_real_parser(settings, custom, monkeypatch, tmp_path):
    overrides = {
        "prompt_pdf_figures": "Custom figure instructions.",
        "prompt_pdf_example_image": "Custom image example.",
        "prompt_pdf_evaluation": "Custom review: {original_prompt}\n{original_output}",
    } if custom else {}
    monkeypatch.setattr(pdf_parser_module, "uploads_root", lambda: tmp_path / "uploads")
    monkeypatch.setattr(PdfParser, "_load_prompt_settings", lambda _self: overrides)
    parser = PdfParser(str(tmp_path / "synthetic.pdf"), user_id=1, llm_service=None, settings=settings)
    preview = DocProcessorPromptPreviewBuilder(overrides, settings)
    assert parser._get_dynamic_user_prompt(1) == preview.build_dynamic_user_prompt(1)
    assert parser._evaluation_prompt_template == preview.get_evaluation_prompt()
    if custom:
        assert "Custom figure instructions." in preview.build_dynamic_user_prompt(1)
        assert "Custom image example." in preview.build_dynamic_user_prompt(1)
        assert preview.get_evaluation_prompt().startswith(overrides["prompt_pdf_evaluation"])


def test_current_editor_defaults_and_disabled_images_match_effective_contract():
    fields = {field["id"]: field for section in build_prompt_editor_sections() for field in section["fields"]}
    assert "Choose the image alternative" in fields["prompt_pdf_figures"]["default_text"]
    assert PDF_IMAGE_ACCESSIBILITY_EVALUATION_INSTRUCTION in fields["prompt_pdf_evaluation"]["resolved_text"]
    assert PDF_EXAMPLE_ACCESSIBLE_IMAGES == fields["prompt_pdf_example_image"]["resolved_text"]
    no_images = DocProcessorPromptPreviewBuilder(settings={"pdf_options": {"extract_images": False}})
    assert PDF_IMAGE_ACCESSIBILITY_INSTRUCTION not in no_images.build_dynamic_user_prompt(1)
    assert PDF_EXAMPLE_ACCESSIBLE_IMAGES not in no_images.build_dynamic_user_prompt(1)


def test_accessible_prompt_examples_are_valid_semantic_description_markups():
    examples = re.findall(r"<main_content>.*?</main_content>\s*<footnotes></footnotes>", PDF_EXAMPLE_ACCESSIBLE_IMAGES, re.S)
    assert len(examples) == 3
    for source in examples:
        soup = BeautifulSoup(source, "html.parser")
        assert diagnose_image_descriptions(soup) == ()
        assert diagnose_mermaid_diagrams(soup) == ()
    assert BeautifulSoup(examples[1], "html.parser").img["alt"] == ""


def test_custom_evaluator_contract_is_idempotent_across_editor_round_trips():
    custom = 'Custom evaluation: {original_output}'
    effective = effective_prompt_value('prompt_pdf_evaluation', custom)
    assert effective.startswith(custom)
    assert effective.count(PDF_IMAGE_ACCESSIBILITY_EVALUATION_INSTRUCTION) == 1
    assert effective_prompt_value('prompt_pdf_evaluation', effective) == effective
