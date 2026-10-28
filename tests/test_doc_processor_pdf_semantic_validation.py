from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from iarf_assistant.addons.doc_processor.parsers.pdf_parser import (
    MAX_EVALUATION_DECISIONS,
    MAX_SEMANTIC_VALIDATION_REJECTIONS,
    PdfParser,
    _RetryablePageModelOutputError,
)
from iarf_assistant.addons.doc_processor.parsers.semantic_validation import (
    EXTRACTION_NOTE_INPUT_POLICY_SETTING,
    EXTRACTION_NOTE_INPUT_POLICY_VERSION,
    extraction_note_input_contract,
    validate_evaluator_replacement,
    validate_extraction_grounding,
    validate_extraction_note_input,
    validate_model_response_content,
    validate_normalized_page_output,
)
from iarf_assistant.addons.doc_processor.prompting import (
    build_prompt_cache_signature,
    compose_page_prompt_payload,
)


def _page(main: str, footnotes: str = "") -> str:
    return f"<main_content>{main}</main_content><footnotes>{footnotes}</footnotes>"


@pytest.mark.parametrize("artwork", [
    '<figure><svg xmlns="http://www.w3.org/2000/svg"><text x="1" y="2">Source label</text></svg></figure>',
    '<figure><drawing:svg xmlns:drawing="http://www.w3.org/2000/svg"><drawing:text>Source label</drawing:text></drawing:svg></figure>',
    '<figure><img alt="Diagram" src="data:image/svg+xml;base64,PHN2Zy8+" /></figure>',
    '<figure><img alt="Diagram" src="DATA:IMAGE/SVG+XML,%3Csvg%2F%3E" /></figure>',
])
def test_svg_extraction_is_rejected_before_canonicalization_can_discard_artwork(artwork):
    result = validate_normalized_page_output(_page(artwork))
    assert not result.valid
    assert "SVG extraction input is not retained" in result.diagnostics[0]


def test_svg_mentioned_as_literal_code_is_not_treated_as_an_image():
    assert validate_normalized_page_output(_page('<pre><code>&lt;svg&gt;sample&lt;/svg&gt;</code></pre>')).valid


@pytest.mark.parametrize(
    "content",
    [
        _page(
            '<p>Marker <span class="footnote_marker">'
            '<a href="#later-page-note">7</a></span>; x<sup>2</sup> '
            '<a href="https://example.org">ordinary</a>.</p>'
        ),
        _page(
            "<p>Main text.</p>",
            '<div class="footnote" id="f1"><p>Note cites '
            '<span class="footnote_marker"><a href="#other-note">1</a>'
            "</span>.</p></div>",
        ),
    ],
)
def test_extraction_note_input_accepts_linked_marker_shapes_and_unresolved_targets(
    content,
):
    assert validate_extraction_note_input(content).valid


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (
            _page('<div class="footnote_marker">7</div>'),
            "footnote_marker must be a span",
        ),
        (
            _page(
                "<p>Marker.</p>",
                '<span class="footnote" id="f1">Note body.</span>',
            ),
            "footnote body must be a div",
        ),
        (
            _page(
                '<div class="footnote" id="outside">Bad root</div>'
                '<p><span class="footnote_marker">7</span></p>',
                '<div class="footnote" id="f1">Note</div>',
            ),
            "footnote body must be a div",
        ),
        (
            _page(
                '<p><a href="#f1">7</a></p>',
                '<div class="footnote" id="f1">Note body.</div>',
            ),
            "direct links to a known note body",
        ),
        (
            _page(
                '<p><sup><a class="footnote-ref" href="#f1">7</a></sup></p>',
                '<div class="footnote" id="f1">Note body.</div>',
            ),
            "a.footnote-ref is output-only",
        ),
        (
            _page('<p><span role="doc-noteref">7</span></p>'),
            "doc-noteref/doc-endnote",
        ),
        (
            _page('<aside role="doc-endnote">Note body.</aside>'),
            "doc-noteref/doc-endnote",
        ),
        (
            _page('<p><a epub:type="noteref" href="#f1">7</a></p>'),
            "epub noteref/endnote",
        ),
        (
            _page('<div epub:type="endnote">Note body.</div>'),
            "epub noteref/endnote",
        ),
    ],
)
def test_extraction_note_input_rejects_wrong_reserved_shapes(content, expected):
    result = validate_extraction_note_input(content)

    assert not result.valid
    assert result.diagnostics[0].startswith("footnote input shape:")
    assert expected in result.diagnostics[0]


@pytest.mark.parametrize("allow_blank", [False, True])
def test_parser_applies_strict_note_input_to_managed_page_responses(allow_blank):
    parser = object.__new__(PdfParser)
    parser.settings = {"mcp_execution_mode": "managed"}
    parser.metrics_lock = threading.Lock()
    parser.metrics = {}
    content = _page(
        '<p><sup><a class="footnote-ref" href="#f1">1</a></sup></p>',
        '<div class="footnote" id="f1">Note body.</div>',
    )

    with pytest.raises(_RetryablePageModelOutputError) as error:
        parser._validate_page_model_content(
            content,
            "stop",
            1,
            None,
            allow_blank=allow_blank,
            declared_blank=False,
        )

    assert error.value.stage == "semantic_contract"
    assert error.value.diagnostics[0].startswith("footnote input shape:")


def test_parser_keeps_retained_passive_canonical_note_input_compatible():
    parser = object.__new__(PdfParser)
    parser.settings = {"mcp_execution_mode": "passive"}
    parser.metrics_lock = threading.Lock()
    parser.metrics = {}
    content = _page(
        '<p><sup><a class="footnote-ref" href="#f1">1</a></sup></p>',
        '<div class="footnote" id="f1">Note body.</div>',
    )

    normalized = parser._validate_page_model_content(content, "stop", 1, None)

    assert 'class="footnote-ref"' in normalized


def test_extraction_note_contract_prompt_cache_and_historical_prompt_gating():
    contract = extraction_note_input_contract()
    legacy_settings = {"mcp_execution_mode": "passive", "pdf_options": {}}
    strict_passive = {
        **legacy_settings,
        EXTRACTION_NOTE_INPUT_POLICY_SETTING: EXTRACTION_NOTE_INPUT_POLICY_VERSION,
    }

    def stable_instruction(settings):
        return compose_page_prompt_payload(
            1,
            False,
            previous_outputs=[],
            current_headings={},
            current_author=None,
            metadata_context="",
            dynamic_prompt_builder=lambda _page, _include: "Original extraction.",
            settings=settings,
        )["stable_instruction"]

    assert "span.footnote_marker" not in stable_instruction(legacy_settings)
    assert stable_instruction(strict_passive).endswith(contract["instructions"])
    managed = stable_instruction({"mcp_execution_mode": "managed"})
    assert managed.endswith(contract["instructions"])
    assert (
        build_prompt_cache_signature({}, legacy_settings, "extraction")
        == "857470184a643e740bf36edd1d05fa93"
    )
    assert build_prompt_cache_signature({}, strict_passive, "extraction") != (
        build_prompt_cache_signature({}, legacy_settings, "extraction")
    )


@pytest.mark.parametrize("content,code", [
    ('<ol><ul><li>Bad nesting</li></ul></ol>', "list_invalid_child"),
    ('<ol>Unwrapped item<li>Item</li></ol>', "list_invalid_child"),
    ('<li>Orphan</li>', "list_item_without_list"),
    ('<ol start="iv"><li>Item</li></ol>', "list_invalid_start"),
    ('<ol type="roman"><li>Item</li></ol>', "list_invalid_type"),
    ('<ul><li value="4">Bullet</li></ul>', "list_invalid_value"),
])
def test_page_rejects_malformed_list_evidence(content, code):
    result = validate_normalized_page_output(_page(content))
    assert not result.valid
    assert code in result.diagnostics


def test_page_accepts_explicit_nested_ordinals_and_intentional_jumps():
    assert validate_normalized_page_output(_page(
        '<ol start="4" type="I"><li value="4">Fourth'
        '<ul><li>Detail</li></ul></li><li value="7">Seventh</li></ol>'
    )).valid


def test_semantic_validation_accepts_later_page_footnote_marker_and_safe_local_reference():
    marker_on_later_note_page = _page(
        '<p>Text<span class="footnote_marker" id="fm7">7</span></p>'
    )
    linked_note = _page(
        '<p>Text<sup><a class="footnote-ref" href="#f7">7</a></sup></p>',
        '<div class="footnote" id="f7">A note.</div>',
    )

    assert validate_normalized_page_output(marker_on_later_note_page).valid
    assert validate_normalized_page_output(linked_note).valid


def test_semantic_validation_rejects_malicious_or_broken_page_topology():
    duplicate_ids = _page('<p id="repeat">One</p><p id="repeat">Two</p>')
    broken_footnote_ref = _page('<p><a class="footnote-ref" href="#missing">1</a></p>')

    assert not validate_normalized_page_output("<p>unrooted output</p>").valid
    assert not validate_normalized_page_output(duplicate_ids).valid
    assert not validate_normalized_page_output(broken_footnote_ref).valid
    assert not validate_model_response_content("", "stop").valid
    assert not validate_model_response_content("Blocked by safety policy", "stop").valid


@pytest.mark.parametrize("marker_class", ["footnote_marker", "inline-footnote"])
def test_semantic_validation_accepts_repeated_same_page_footnote_markers(marker_class):
    repeated_markers = _page(
        f'<p>First <span class="{marker_class}" id="fm7">7</span> '
        f'again <span class="{marker_class}" id="fm7">7</span>.</p>'
    )

    assert validate_normalized_page_output(repeated_markers).valid


def test_semantic_validation_rejects_mixed_marker_and_non_marker_duplicate():
    mixed = _page(
        '<p>Marker <span class="footnote_marker" id="fm7">7</span> '
        '<span id="fm7">not a marker</span>.</p>'
    )

    result = validate_normalized_page_output(mixed)

    assert not result.valid
    assert result.diagnostics == ("duplicate id='fm7'",)


def test_semantic_validation_rejects_duplicate_note_body_ids():
    duplicate_note_bodies = _page(
        '<p>Marker <span class="footnote_marker" id="fm7">7</span>.</p>',
        '<div class="footnote" id="f7">First note.</div>'
        '<div class="footnote" id="f7">Duplicate note.</div>',
    )

    result = validate_normalized_page_output(duplicate_note_bodies)

    assert not result.valid
    assert result.diagnostics == ("duplicate id='f7'",)


@pytest.mark.parametrize(
    "duplicate_markup",
    [
        '<p id="repeat">One</p><p id="repeat">Two</p>',
        (
            '<span class="inline-footnote" id="repeat">One</span>'
            '<div class="inline-footnote" id="repeat">Two</div>'
        ),
    ],
)
def test_semantic_validation_rejects_unrelated_duplicate_ids(duplicate_markup):
    result = validate_normalized_page_output(_page(duplicate_markup))

    assert not result.valid
    assert result.diagnostics == ("duplicate id='repeat'",)


def test_semantic_validation_requires_inert_structured_source_toc_entries():
    valid = _page(
        '<nav class="source-toc"><h2 class="source-toc-title">Contents</h2>'
        '<div class="toc-entry toc-depth-2">'
        '<span class="toc-entry-title">Private title</span> '
        '<span class="toc-page-label">xii</span></div></nav>'
    )
    linked = _page(
        '<nav class="source-toc"><div class="toc-entry toc-depth-1">'
        '<span class="toc-entry-title"><a href="#chapter">Private</a></span>'
        "</div></nav>"
    )
    malformed = _page(
        '<nav class="source-toc"><div class="toc-entry">Private</div></nav>'
    )

    assert validate_normalized_page_output(valid).valid
    assert (
        "source_toc_contains_link"
        in validate_normalized_page_output(linked).diagnostics
    )
    result = validate_normalized_page_output(malformed)
    assert not result.valid
    assert "source_toc_invalid_depth" in result.diagnostics
    assert "source_toc_missing_title" in result.diagnostics


@pytest.mark.parametrize(
    "finish_reason",
    [
        "MAX_TOKENS",
        "length",
        "FinishReason.MAX_TOKENS",
        "FinishReason.RECITATION",
        "google.genai.types.FinishReason.SAFETY",
        "PROHIBITED_CONTENT",
        "spii",
        "BLOCKLIST",
        "content-filter",
        "content_filter",
        "provider: blocked",
    ],
)
def test_semantic_validation_rejects_provider_terminal_failure_reasons(finish_reason):
    result = validate_model_response_content(
        _page("<p>Otherwise valid-looking HTML.</p>"), finish_reason
    )

    assert not result.valid
    assert "terminal failure" in result.diagnostics[0]


def test_semantic_validation_accepts_stop_finish_reason():
    assert validate_model_response_content(_page("<p>Complete HTML.</p>"), "STOP").valid


def test_extraction_grounding_accepts_faithful_reflow_and_dehyphenation():
    source = """
    The science of logic examines the forms through which thought determines
    itself. This determina-\n tion is not imposed from outside, because the
    concept develops through its own movement. A printed page may divide words
    at the margin, vary whitespace, and retain running matter which extraction
    is expected to remove. The visible philosophical argument nevertheless
    remains stable through those presentational repairs and ordinary optical
    character recognition corrections.
    """
    extracted = _page(
        "<p>The science of logic examines the forms through which thought "
        "determines itself. This determination is not imposed from outside, "
        "because the concept develops through its own movement.</p>"
        "<p>A printed page may divide words at the margin, vary whitespace, "
        "and retain running matter which extraction is expected to remove. "
        "The visible philosophical argument nevertheless remains stable "
        "through those presentational repairs and ordinary optical character "
        "recognition corrections.</p>"
    )

    assert validate_extraction_grounding(source, extracted).valid


def test_extraction_grounding_rejects_unrelated_complete_page():
    source = " ".join(
        "absolute spirit develops through concept judgment syllogism logic "
        "freedom consciousness dialectic substance subject philosophy reason"
        for _ in range(5)
    )
    unrelated = _page(
        "<p>"
        + " ".join(
            "wireless beacon indoor positioning sensor calibration antenna "
            "network protocol signal measurement localization algorithm survey"
            for _ in range(4)
        )
        + "</p>"
    )

    result = validate_extraction_grounding(source, unrelated)

    assert not result.valid
    assert result.diagnostics[0].startswith("source grounding mismatch")


def test_extraction_grounding_rejects_a_grounded_but_truncated_prefix():
    source_tokens = [f"sourceword{index}" for index in range(300)]
    source = " ".join(source_tokens)
    truncated = _page("<p>" + " ".join(source_tokens[:190]) + "</p>")

    result = validate_extraction_grounding(source, truncated)

    assert not result.valid
    assert result.diagnostics[0].startswith("source extraction appears incomplete")


def test_extraction_grounding_allows_expected_large_source_omissions():
    source_tokens = [f"sourceword{index}" for index in range(300)]
    source = " ".join(source_tokens)
    partial = _page("<p>" + " ".join(source_tokens[:190]) + "</p>")

    assert validate_extraction_grounding(
        source,
        partial,
        allow_expected_source_omissions=True,
    ).valid


def test_extraction_grounding_requires_explicit_policy_for_empty_output():
    source = "sourceword " * 60
    empty = _page("")

    rejected = validate_extraction_grounding(source, empty)

    assert not rejected.valid
    assert validate_extraction_grounding(
        source,
        empty,
        allow_empty_output=True,
    ).valid


def test_extraction_grounding_defers_to_vision_for_sparse_text_layer():
    source = "Illustrated plate with no usable embedded text."
    vision_output = _page(
        "<p>A complete transcription reconstructed from the supplied page image "
        "rather than from a sparse or absent PDF text layer.</p>"
    )

    assert validate_extraction_grounding(source, vision_output).valid


def test_evaluator_validation_allows_small_ocr_correction_but_rejects_catastrophic_loss_and_reorder():
    original = _page(
        "".join(
            f"<p>Paragraph {index} carries enough stable visible text.</p>"
            for index in range(8)
        )
    )
    small_correction = _page(
        "".join(
            f"<p>Paragraph {index} carries enough stable visible text{'!' if index == 3 else '.'}</p>"
            for index in range(8)
        )
    )
    text_loss = _page("<p>Paragraph 0 carries enough stable visible text.</p>")
    reordered = _page(
        "".join(
            f"<p>Paragraph {index} carries enough stable visible text.</p>"
            for index in (7, 6, 5, 4, 3, 2, 1, 0)
        )
    )

    assert validate_evaluator_replacement(original, small_correction).valid
    assert not validate_evaluator_replacement(original, text_loss).valid
    assert not validate_evaluator_replacement(original, reordered).valid


def test_evaluator_validation_rejects_footnote_body_or_topology_loss_hidden_by_long_body():
    main = "".join(
        f"<p>Long retained paragraph {index} with stable visible source text.</p>"
        for index in range(12)
    )
    original = _page(
        main + '<p>Marker <span class="footnote_marker" id="fm7">7</span>.</p>',
        '<div class="footnote" id="f7">A long source note that must remain intact.</div>',
    )

    removed = _page(main)
    emptied = _page(
        main + '<p>Marker <span class="footnote_marker" id="fm7">7</span>.</p>',
        '<div class="footnote" id="f7"></div>',
    )

    assert not validate_evaluator_replacement(original, removed).valid
    assert not validate_evaluator_replacement(original, emptied).valid


@pytest.mark.parametrize(
    "protected_markup",
    [
        '<figure><img src="figure.png"><figcaption>Caption</figcaption></figure>',
        "<table><tr><td>Cell</td></tr></table>",
        "<ul><li>List item</li></ul>",
        "<blockquote>Quoted source text.</blockquote>",
        '<nav class="source-toc"><div class="toc-entry toc-depth-1">'
        '<span class="toc-entry-title">Chapter</span></div></nav>',
    ],
)
def test_evaluator_validation_rejects_protected_structure_loss(protected_markup):
    long_body = "".join(
        f"<p>Retained paragraph {index} with enough stable source text.</p>"
        for index in range(12)
    )

    result = validate_evaluator_replacement(
        _page(long_body + protected_markup),
        _page(long_body),
    )

    assert not result.valid


def test_evaluator_validation_rejects_template_token_loss():
    long_body = "".join(
        f"<p>Retained paragraph {index} with enough stable source text.</p>"
        for index in range(12)
    )

    result = validate_evaluator_replacement(
        _page(long_body + "<p>{{chapter_title}}</p>"),
        _page(long_body),
    )

    assert not result.valid


def test_evaluator_rejection_keeps_original_output_and_records_diagnostic():
    parser = object.__new__(PdfParser)
    parser.llm_service = MagicMock()
    parser.settings = {"pdf_options": {"toc_handling": "omit"}}
    parser._evaluation_prompt_template = "{original_prompt}\n{original_output}"
    parser._completion_kwargs = MagicMock(return_value={})
    parser._record_model_use = MagicMock()
    parser._record_response_metrics = MagicMock()
    parser._record_debug_call = MagicMock()
    parser.metrics_lock = threading.Lock()
    parser.metrics = {"semantic_validation_rejections": []}

    original = _page(
        "".join(
            f"<p>Long paragraph {index} with enough source text.</p>"
            for index in range(12)
        )
    )
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=_page("<p>Lost.</p>")),
                finish_reason="stop",
            )
        ]
    )
    parser.llm_service.completion.return_value = response

    result = parser._evaluate_output(
        "original prompt", original, "test-model", page_number=4
    )

    assert result == original
    assert parser.metrics["semantic_validation_rejections"] == [
        {"page": 4, "stage": "evaluator", "reason": "catastrophic visible-text loss"}
    ]
    assert parser.metrics["evaluation_attempts"] == 1
    assert parser.metrics["evaluation_rejected"] == 1
    assert parser.metrics["evaluation_decisions"] == [
        {
            "page": 4,
            "status": "rejected",
            "visible_text_changed": True,
            "visible_text_chars_delta": -500,
            "tag_count_deltas": {"p": -11},
            "footnote_ref_delta": 0,
            "footnote_body_delta": 0,
        }
    ]


def test_evaluation_metrics_are_source_free_and_keep_a_bounded_tail():
    parser = object.__new__(PdfParser)
    parser.metrics_lock = threading.Lock()
    parser.metrics = {}
    original = _page("<h2>Private heading</h2><p>Private body.</p>")
    candidate = _page("<h3>Corrected private heading</h3><p>Private body.</p>")

    for page in range(MAX_EVALUATION_DECISIONS + 1):
        parser._record_evaluation_decision(page, "accepted_change", original, candidate)

    assert parser.metrics["evaluation_attempts"] == MAX_EVALUATION_DECISIONS + 1
    assert parser.metrics["evaluation_accepted_changes"] == (
        MAX_EVALUATION_DECISIONS + 1
    )
    assert len(parser.metrics["evaluation_decisions"]) == MAX_EVALUATION_DECISIONS
    assert parser.metrics["evaluation_decisions"][0]["page"] == 1
    assert "Private" not in repr(parser.metrics["evaluation_decisions"])


def test_semantic_validation_rejection_history_has_a_fixed_tail():
    parser = object.__new__(PdfParser)
    parser.metrics_lock = threading.Lock()
    parser.metrics = {
        "semantic_validation_rejections": [
            {"page": page, "stage": "evaluator", "reason": "old"}
            for page in range(MAX_SEMANTIC_VALIDATION_REJECTIONS)
        ]
    }

    parser._record_semantic_validation_rejection(999, "evaluator", ("new",))

    rejections = parser.metrics["semantic_validation_rejections"]
    assert len(rejections) == MAX_SEMANTIC_VALIDATION_REJECTIONS
    assert rejections[0]["page"] == 1
    assert rejections[-1] == {"page": 999, "stage": "evaluator", "reason": "new"}
