import base64
import io
import json
import logging
import tempfile
import threading
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pymupdf as fitz
import pytest
from bs4 import BeautifulSoup

from iarf_assistant.addons.doc_processor.parsers import pdf_parser as pdf_parser_module
from iarf_assistant.addons.doc_processor.parsers.pdf_parser import (
    METADATA_JSON_SCHEMA,
    PUBLICATION_TYPE_VALUES,
    PdfParser,
    StructuredResponseError,
)
from iarf_assistant.addons.doc_processor.passive_preflight import (
    PASSIVE_PAGE_BATCH_V2_SCHEMA,
)
from iarf_assistant.addons.doc_processor.prompts import (
    DEFAULT_PDF_EVALUATOR_SYSTEM_PROMPT,
    DEFAULT_PDF_JSON_SYSTEM_PROMPT,
    DEFAULT_PDF_USER_PROMPT_BASE,
    PDF_NATIVE_VISION_AUTHORING_INSTRUCTION,
)


class _FakeDoc:
    def __init__(self, mapping):
        self._mapping = mapping

    def get_page_numbers(self, label, only_one=False):
        return self._mapping.get(label, [])


def _make_parser_with_settings(settings=None):
    parser = object.__new__(PdfParser)
    parser.settings = settings or {}
    return parser


def test_cover_suggestion_requires_an_observed_physical_pdf_page():
    parser = _make_parser_with_settings()
    candidate = {"page": 3, "kind": "title_page", "evidence": "Visible title and author."}
    assert parser._normalize_document_metadata({"title": "Book", "cover_candidate": candidate}, source_pages=[1, 3])["cover_candidate"] == candidate
    assert "cover_candidate" not in parser._normalize_document_metadata({"title": "Book", "cover_candidate": candidate}, source_pages=[1, 2])
    assert "cover_candidate" not in parser._normalize_document_metadata({"title": "Book", "cover_candidate": candidate}, doc=[None, None])


def _batch_call_parser(settings, content, *, source_text=""):
    parser = _make_parser_with_settings(settings)
    parser.metrics = {}
    parser.metrics_lock = threading.Lock()
    parser.extraction_issues_by_page = {}
    parser._image_file_to_data_url = MagicMock(
        return_value="data:image/png;base64,AA=="
    )
    parser._build_cached_system_message = MagicMock(
        side_effect=lambda instruction, **_kwargs: {
            "role": "system",
            "content": instruction,
        }
    )
    parser._record_model_use = MagicMock()
    parser._record_response_metrics = MagicMock()
    parser._completion_kwargs = MagicMock(return_value={})
    parser._thinking_config = MagicMock(return_value=None)
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content), finish_reason="stop"
            )
        ]
    )
    parser._completion_with_structured_json = MagicMock(return_value=response)
    output = parser._call_page_batch_model(
        {0: ("page.png", source_text)},
        {"stable_instruction": "existing extraction instructions", "context": ""},
        "test-model",
        object(),
    )
    return parser, output


def _expected_legacy_page_batch_instruction():
    return (
        "existing extraction instructions"
        + "\n\nMULTIPAGE RESPONSE CONTRACT (overrides top-level HTML instructions):\n"
        "Return ONLY a JSON object with a pages array, one result for EVERY labelled "
        "PDF page, in supplied order. Each result has only page_number (integer) and "
        "html (string containing both <main_content> and <footnotes> roots). Apply "
        "the extraction rules separately to each page. Do not combine pages, move "
        "text between pages, omit blank pages, or copy the boundary evidence. Use "
        "empty roots for a blank page and the specified source-toc marker for a "
        "TOC-only page. For continuation analysis, preceding supplied pages in this "
        "batch are available; preserve their page boundaries in the response. "
        "Use bounding boxes relative to each individual page image."
    )


def test_page_batch_retries_list_join_conflict_with_safe_page_feedback():
    from iarf_assistant.addons.doc_processor.page_batch_response import PageBatchValidationError

    parser, _output = _batch_call_parser({}, json.dumps({"pages": [{
        "page_number": 1,
        "html": "<main_content><p>Setup.</p></main_content><footnotes></footnotes>",
    }]}))
    content = json.dumps({"pages": [
        {"page_number": 1, "html": '<main_content><ol start="4" type="1"><li>Rule.</li></ol></main_content><footnotes></footnotes>'},
        {"page_number": 2, "html": '<main_content><ol start="1" type="a" class="continued"><li>PRIVATE-BODY</li></ol></main_content><footnotes></footnotes>'},
    ]})
    parser._completion_with_structured_json.return_value.choices[0].message.content = content
    with pytest.raises(PageBatchValidationError) as failed:
        parser._call_page_batch_model(
            {0: ("one.png", ""), 1: ("two.png", "")},
            {"stable_instruction": "extract", "context": ""},
            "test-model", object(), batch_id="pages-1-2",
        )
    assert failed.value.feedback["stage"] == "list_boundary"
    assert failed.value.feedback["reason"] == "list-continuation-style-conflict"
    assert failed.value.feedback["page"] == 2
    assert failed.value.feedback["previous_page"] == 1
    assert "PRIVATE-BODY" not in str(failed.value.feedback)
    assert parser._completion_with_structured_json.call_count == 4


@pytest.mark.parametrize(
    ("size", "crop_region", "expected_size"),
    [
        ((3000, 1500), None, (2000, 1000)),
        ((1500, 3000), None, (1000, 2000)),
        ((120, 80), None, (120, 80)),
        ((2001, 1000), None, (2000, 1000)),
        ((3000, 2500), "top", (2000, 467)),
    ],
)
def test_png_image_transport_caps_dimensions_without_upscale_or_distortion(
    tmp_path, size, crop_region, expected_size
):
    from PIL import Image

    source = tmp_path / "source.png"
    Image.new("RGB", size, color="white").save(source, format="PNG")
    parser = _make_parser_with_settings({"mcp_image_transport": "png-2000-v1"})

    data_url = parser._image_file_to_data_url(
        source, crop_region=crop_region, max_width=None
    )

    assert data_url.startswith("data:image/png;base64,")
    with Image.open(
        io.BytesIO(base64.b64decode(data_url.split(",", 1)[1]))
    ) as rendered:
        assert rendered.size == expected_size
        assert max(rendered.size) <= 2000
        if crop_region is None:
            assert rendered.size[0] / rendered.size[1] == pytest.approx(
                size[0] / size[1], rel=0.001
            )


def test_png_image_transport_passes_fitting_png_bytes_through_unchanged(tmp_path):
    from PIL import Image

    source = tmp_path / "fitting.png"
    Image.new("P", (80, 40), color=3).save(source, format="PNG")
    original = source.read_bytes()
    parser = _make_parser_with_settings({"mcp_image_transport": "png-2000-v1"})

    data_url = parser._image_file_to_data_url(source, max_width=None)

    assert data_url == "data:image/png;base64," + base64.b64encode(original).decode(
        "ascii"
    )


def test_legacy_image_transport_preserves_jpeg_quality_algorithm_exactly(tmp_path):
    from PIL import Image

    source = tmp_path / "legacy.png"
    Image.new("RGB", (1536, 2048), color=(37, 91, 149)).save(source, format="PNG")
    parser = _make_parser_with_settings()
    expected_buffer = io.BytesIO()
    with Image.open(source) as image:
        if image.mode in ("RGBA", "P"):
            image = image.convert("RGB")
        image.save(expected_buffer, format="JPEG", quality=88)

    actual = parser._image_file_to_data_url(source, max_width=None)

    assert actual == "data:image/jpeg;base64," + base64.b64encode(
        expected_buffer.getvalue()
    ).decode("utf-8")


def test_png_image_transport_writes_heading_discovery_contact_sheet_as_png(tmp_path):
    document = fitz.open()
    page = document.new_page(width=240, height=320)
    page.insert_text((20, 35), "Synthetic heading")
    parser = _make_parser_with_settings({"mcp_image_transport": "png-2000-v1"})
    parser.output_folder = tmp_path
    parser._copy_debug_image = MagicMock()
    temporary_paths = []

    try:
        content = parser._render_heading_contact_sheets(
            document, [0], {}, None, temporary_paths
        )
    finally:
        document.close()

    assert temporary_paths and temporary_paths[0].endswith(".png")
    assert Path(temporary_paths[0]).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_thread_submission_propagates_an_independent_context_copy():
    marker = ContextVar("pdf_test_marker", default="missing")
    token = marker.set("passive-execution")
    entered = threading.Event()
    release = threading.Event()

    def model_callable():
        entered.set()
        release.wait(timeout=5)
        return marker.get()

    try:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = pdf_parser_module._submit_with_current_context(
                executor, model_callable
            )
            assert entered.wait(timeout=5)
            second = pdf_parser_module._submit_with_current_context(
                executor, model_callable
            )
            release.set()
            assert [first.result(), second.result()] == [
                "passive-execution",
                "passive-execution",
            ]
    finally:
        release.set()
        marker.reset(token)


def test_passive_metrics_do_not_consult_provider_cost_catalog():
    parser = _make_parser_with_settings()
    parser.metrics_lock = threading.Lock()
    parser.metrics = {
        "cost": 0.0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_prompt_tokens": 0,
    }
    response = SimpleNamespace(usage=None)
    service = MagicMock()
    with patch(
        "iarf_assistant.core.mcp_completion.passive_execution_active",
        return_value=True,
    ):
        parser._record_response_metrics(response, service, model="passive-role")
    service.calculate_response_cost.assert_not_called()
    assert parser.metrics["cost"] == 0.0


@pytest.mark.parametrize("operation", ("process", "resume_processing"))
def test_passive_pdf_operations_use_neutral_roles_without_model_router(
    tmp_path, operation
):
    source_path = tmp_path / "source.pdf"
    source_doc = fitz.open()
    page = source_doc.new_page(width=200, height=200)
    page.insert_text((24, 40), "Synthetic passive PDF")
    source_doc.save(source_path)
    source_doc.close()

    state = (
        {
            "html_parts": ["<main_content><p>Existing page</p></main_content>"],
            "png_paths": [None],
            "boundary_flags": {},
        }
        if operation == "resume_processing"
        else None
    )
    with (
        patch.object(
            pdf_parser_module, "uploads_root", return_value=tmp_path / "uploads"
        ),
        patch.object(PdfParser, "_load_prompt_settings", return_value={}),
    ):
        parser = PdfParser(
            str(source_path),
            user_id=1,
            llm_service=None,
            settings={
                "parsing_model": "configured/provider-model",
                "metadata_model": "configured/metadata-model",
                "pdf_options": {"metadata_prepass": False},
            },
            state=state,
        )

    with (
        patch(
            "iarf_assistant.core.mcp_completion.passive_execution_context",
            return_value=SimpleNamespace(),
        ),
        patch(
            "iarf_assistant.core.model_router.ModelRouter.get_model_for_tier",
            side_effect=AssertionError("passive PDF must not query ModelRouter"),
        ),
        patch.object(
            pdf_parser_module,
            "_passive_document_model",
            wraps=pdf_parser_module._passive_document_model,
        ) as resolve_passive_model,
    ):
        status, _output = getattr(parser, operation)()

    assert status == "COMPLETED"
    expected_calls = [call("parsing")]
    if operation == "process":
        expected_calls.append(call("metadata"))
    assert resolve_passive_model.call_args_list == expected_calls


def test_resume_rerenders_a_missing_review_png_without_model_calls(tmp_path):
    uploads_dir = tmp_path / "uploads"
    source_path = tmp_path / "source.pdf"
    source_doc = fitz.open()
    page = source_doc.new_page(width=200, height=200)
    page.draw_rect(fitz.Rect(30, 30, 170, 170), color=(0, 0, 0), fill=(1, 1, 1))
    source_doc.save(source_path)
    source_doc.close()

    state = {
        "phase": "PAUSED_IMAGE_REVIEW",
        "included_pages": [0],
        "html_parts": [
            "<main_content><figure><img alt='Diagram' "
            "src='[100, 100, 900, 900]'/></figure></main_content>"
        ],
        "png_paths": [str(tmp_path / "deleted-review-page.png")],
        "boundary_flags": {},
    }
    with (
        patch.object(pdf_parser_module, "uploads_root", return_value=uploads_dir),
        patch.object(PdfParser, "_load_prompt_settings", return_value={}),
    ):
        parser = PdfParser(
            str(source_path),
            user_id=1,
            llm_service=None,
            settings={"pdf_options": {"page_range": "1"}},
            state=state,
        )

    status, output = parser.resume_processing()

    assert status == "COMPLETED"
    image = BeautifulSoup(output, "html.parser").find("img")
    assert image is not None
    assert image["src"].startswith("/uploads/1/doc_processor_docs/extracted_")
    assert list((uploads_dir / "1" / "doc_processor_docs").glob("extracted_*.png"))
    assert not list((uploads_dir / "1" / "doc_processor_docs").glob("docproc_*.png"))


@pytest.mark.parametrize("operation", ["process", "resume_processing"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("stored_in_state", [False, True])
def test_reentry_ignores_saved_boundary_correction_when_disabled(
    tmp_path, operation, enabled, stored_in_state
):
    source_path = tmp_path / "source.pdf"
    with fitz.open() as source:
        for text in ("Complete notice.", "Independent passage."):
            page = source.new_page(width=200, height=200)
            page.insert_text((24, 40), text)
        source.save(source_path)
    flags = {0: {"main_content": "merge", "footnotes": "separate"}}
    state = {
        "html_parts": [
            "<main_content><p>Complete notice.</p></main_content>",
            "<main_content><p>Independent passage.</p></main_content>",
        ],
        "png_paths": [None, None],
        "boundary_flags": flags if stored_in_state else {},
    }
    with (
        patch.object(pdf_parser_module, "uploads_root", return_value=tmp_path / "uploads"),
        patch.object(PdfParser, "_load_prompt_settings", return_value={}),
    ):
        parser = PdfParser(
            str(source_path), user_id=1, llm_service=None, state=state,
            settings={
                "parsing_model": "synthetic/model",
                "boundary_decisions": flags,
                "pdf_options": {"metadata_prepass": False, "llm_boundary_merge": enabled},
            },
        )
    with patch.object(parser, "_call_boundary_model_with_context") as dispatch:
        status, output = getattr(parser, operation)()
    assert status == "COMPLETED"
    paragraphs = BeautifulSoup(output, "html.parser").find_all("p")
    retained_merge = enabled and (stored_in_state or operation == "resume_processing")
    assert len(paragraphs) == (1 if retained_merge else 2)
    assert " ".join(p.get_text(" ", strip=True) for p in paragraphs) == "Complete notice. Independent passage."
    dispatch.assert_not_called()


def test_crop_guard_expands_an_edge_that_slices_substantive_native_text():
    parser = _make_parser_with_settings()
    parser.metrics = {"crop_guard_expansions": []}
    parser.metrics_lock = threading.Lock()
    page = SimpleNamespace(
        number=1,
        rect=fitz.Rect(0, 0, 260, 412),
        get_text=lambda _kind: [
            (5.17, 38.55, 176.55, 48.12, "The author of this powerful book"),
            (5.46, 51.11, 176.4, 61.36, "of a new way of life and purpose"),
        ],
    )

    clip = parser._get_clip_rect(page, [0.0577, 0.0279, 0.952, 0.9709])

    assert clip is not None
    assert clip.x0 == pytest.approx(3.87)
    assert clip.y0 == pytest.approx(11.4948)
    assert clip.x1 == pytest.approx(247.52)
    assert parser.metrics["crop_guard_expansions"] == [
        {
            "page": 2,
            "requested": [0.0577, 0.0279, 0.952, 0.9709],
            "expanded": [0.0149, 0.0279, 0.952, 0.9709],
            "crossing_text_blocks": 2,
        }
    ]


def test_crop_guard_ignores_short_margin_artifacts():
    parser = _make_parser_with_settings()
    parser.metrics = {"crop_guard_expansions": []}
    parser.metrics_lock = threading.Lock()
    page = SimpleNamespace(
        number=15,
        rect=fitz.Rect(0, 0, 260, 412),
        get_text=lambda _kind: [(5, 386, 20, 394, "15")],
    )

    clip = parser._get_clip_rect(page, [0.0577, 0.0279, 0.952, 0.9709])

    assert clip is not None
    assert list(clip) == pytest.approx([15.002, 11.4948, 247.52, 400.0108])
    assert parser.metrics["crop_guard_expansions"] == []


def test_boundary_heuristic_reviews_continuation_after_completed_sentence():
    parser = _make_parser_with_settings()
    previous = BeautifulSoup("<p>The first paragraph is complete.</p>", "html.parser").p
    following = BeautifulSoup(
        '<p class="continued">The next paragraph starts here.</p>',
        "html.parser",
    ).p

    decision, uncertain, reason = parser._boundary_heuristic_decision(
        previous, following, "main_content"
    )

    assert decision == "auto"
    assert uncertain is True
    assert reason == "continuation after sentence punctuation needs visual confirmation"


def test_boundary_heuristic_keeps_unambiguous_continuation_without_review():
    parser = _make_parser_with_settings()
    previous = BeautifulSoup("<p>The sentence continues</p>", "html.parser").p
    following = BeautifulSoup(
        '<p class="continued">on the following page.</p>',
        "html.parser",
    ).p

    decision, uncertain, reason = parser._boundary_heuristic_decision(
        previous, following, "main_content"
    )

    assert decision == "merge"
    assert uncertain is False
    assert reason == "continuation class supplied by extraction model"


def test_boundary_review_does_not_infer_a_forward_join_from_a_backward_marker():
    parser = _make_parser_with_settings()
    previous = BeautifulSoup('<p class="continued">The old paragraph ends.</p>', "html.parser").p
    following = BeautifulSoup("<p>A new paragraph begins.</p>", "html.parser").p
    decision, uncertain, _ = parser._boundary_heuristic_decision(previous, following, "main_content")
    assert decision == "separate"
    assert uncertain is False


@pytest.mark.parametrize("options", [{}, {"llm_boundary_merge": False}])
def test_boundary_correction_is_opt_in_and_does_not_dispatch_when_disabled(options):
    parser = _make_parser_with_settings({"pdf_options": options})
    with patch.object(parser, "_call_boundary_model_with_context") as dispatch:
        assert parser._build_boundary_flags(["<p>Uncertain</p>", "<p>Boundary</p>"], [], "unused") == {}
    dispatch.assert_not_called()


def test_inconclusive_boundary_correction_preserves_continuation_markup():
    parser = _make_parser_with_settings()
    fallback = {"main_content": "auto", "footnotes": "auto"}
    for response in ({}, {"main_content": {"action": "separate", "confidence": 0.2}}):
        assert parser._sanitize_boundary_model_decision(response, fallback, {"main_content"}) == fallback


def test_boundary_heuristic_keeps_structural_blocks_separate_when_mislabeled():
    parser = _make_parser_with_settings()
    previous = BeautifulSoup("<p>Chapter opening.</p>", "html.parser").p
    following = BeautifulSoup(
        '<h2 class="continued">A New Chapter</h2>',
        "html.parser",
    ).h2

    decision, uncertain, reason = parser._boundary_heuristic_decision(
        previous, following, "main_content"
    )

    assert decision == "separate"
    assert uncertain is False
    assert reason == "next page starts a new structural block"


def test_debug_call_keeps_safe_service_trace_alongside_parser_prompt():
    parser = _make_parser_with_settings()
    parser.store_debug_artifacts = True
    parser.metrics_lock = threading.Lock()
    parser.debug_calls = {
        "metadata_prepass": [],
        "metadata_heading_structure": [],
        "page_extraction": {},
        "evaluation": {},
        "boundary_merge": [],
    }
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(role="assistant", content="<main_content />")
            )
        ]
    )
    provider_trace = {
        "schema_version": 2,
        "messages": [{"role": "user", "content": "not duplicated here"}],
        "provider_calls": [
            {
                "litellm_model": "openai/mimo-v2.5",
                "parameters": {"reasoning_effort": "high"},
                "outcome": {"status": "completed"},
            }
        ],
    }

    parser._record_debug_call(
        "page_extraction",
        1,
        "custom_u1_opencode_go/mimo-v2.5",
        [{"role": "user", "content": "parser-level prompt"}],
        response,
        provider_trace,
    )

    record = parser.debug_calls["page_extraction"][1][0]
    assert record["messages"][0]["content"] == "parser-level prompt"
    assert record["service_trace"]["schema_version"] == 2
    assert record["service_trace"]["provider_calls"][0]["parameters"] == {
        "reasoning_effort": "high"
    }
    assert "messages" not in record["service_trace"]


def test_pdf_parser_keeps_cross_user_source_out_of_managed_artifact_root():
    with tempfile.TemporaryDirectory() as temp_dir:
        uploads_root = Path(temp_dir) / "uploads"
        foreign_source = (
            uploads_root / "8" / "doc_processor_docs" / "foreign-doc" / "source.pdf"
        )
        foreign_source.parent.mkdir(parents=True)
        foreign_source.write_bytes(b"pdf")

        with (
            patch.object(pdf_parser_module, "uploads_root", return_value=uploads_root),
            patch.object(PdfParser, "_load_prompt_settings", return_value={}),
        ):
            parser = PdfParser(
                str(foreign_source),
                7,
                None,
                settings={"pdf_options": {"store_debug_artifacts": True}},
            )

        owner_root = (uploads_root / "7" / "doc_processor_docs").resolve()
        assert parser.output_folder == owner_root
        assert parser.debug_dir == owner_root / "debug_pipeline"
        assert parser.web_path_prefix == "/uploads/7/doc_processor_docs"


def test_pdf_parser_resume_preserves_existing_debug_call_history():
    with tempfile.TemporaryDirectory() as temp_dir:
        uploads_root = Path(temp_dir) / "uploads"
        document_root = uploads_root / "7" / "doc_processor_docs" / "document-id"
        source = document_root / "source.pdf"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"pdf")
        debug_dir = document_root / "debug_pipeline"
        debug_dir.mkdir()
        (debug_dir / "debug_info.json").write_text(
            json.dumps(
                {
                    "debug_calls": {
                        "metadata_prepass": [{"stage": "metadata"}],
                        "metadata_heading_structure": [],
                        "page_extraction": {
                            "1": [{"stage": "page"}],
                            "not-a-page": [{"stage": "ignored"}],
                        },
                        "evaluation": {},
                        "boundary_merge": [{"stage": "boundary"}],
                    }
                }
            ),
            encoding="utf-8",
        )
        settings = {"pdf_options": {"store_debug_artifacts": True}}

        with (
            patch.object(pdf_parser_module, "uploads_root", return_value=uploads_root),
            patch.object(PdfParser, "_load_prompt_settings", return_value={}),
        ):
            resumed = PdfParser(
                str(source),
                7,
                None,
                settings=settings,
                state={"html_parts": ["<main_content />"]},
            )
            fresh = PdfParser(str(source), 7, None, settings=settings)

        assert resumed.debug_calls["metadata_prepass"] == [{"stage": "metadata"}]
        assert resumed.debug_calls["page_extraction"] == {1: [{"stage": "page"}]}
        assert resumed.debug_calls["boundary_merge"] == [{"stage": "boundary"}]
        assert fresh.debug_calls == PdfParser._empty_debug_calls()


def test_normalize_document_metadata_scopes_pages_and_normalizes_fields():
    parser = _make_parser_with_settings()
    metadata = {
        "isbn": " 978-0-1234-5678-9 ",
        "issn": " 2049-3630 ",
        "contributors": "invalid-shape",
        "table_of_contents": [
            {
                "level": "2",
                "title": "Preface",
                "page": "iv",
                "page_label": "",
                "pdf_page": None,
            },
            {
                "level": "bad",
                "title": "Chapter 1",
                "page": "12",
                "page_label": "12",
                "pdf_page": "0",
            },
        ],
        "pages_to_remove": [
            {"page": 1, "reason": " blank "},
            {"page": 25, "reason": "outside prepass"},
            {"page": "2"},
            3,
        ],
        "extraction_notes": [" keep this ", None, 42],
    }
    doc = _FakeDoc({"iv": [3], "12": [11]})

    normalized = parser._normalize_document_metadata(
        metadata, doc=doc, source_pages=[1, 2]
    )

    assert normalized["isbn"] == "978-0-1234-5678-9"
    assert normalized["issn"] == "2049-3630"
    assert normalized["contributors"] == {
        "authors": [],
        "editors": [],
        "translators": [],
        "other": [],
    }
    assert normalized["source_pages"] == [1, 2]
    assert normalized["extraction_notes"] == ["keep this", "42"]
    assert normalized["pages_to_remove"] == [
        {"page": 1, "reason": "blank"},
        {"page": 2, "reason": "unspecified"},
    ]
    assert normalized["table_of_contents"] == [
        {
            "level": 2,
            "title": "Preface",
            "page": "iv",
            "page_label": "iv",
            "pdf_page": 4,
        },
        {
            "level": 1,
            "title": "Chapter 1",
            "page": "12",
            "page_label": "12",
            "pdf_page": 12,
        },
    ]


def test_normalize_document_metadata_preserves_page_and_page_label_distinction():
    parser = _make_parser_with_settings()
    metadata = {
        "table_of_contents": [
            {
                "level": 1,
                "title": "Main Section",
                "page": "xv",
                "page_label": "15",
                "pdf_page": None,
            }
        ],
    }
    doc = _FakeDoc({"15": [14], "xv": [99]})

    normalized = parser._normalize_document_metadata(
        metadata, doc=doc, source_pages=[1]
    )
    toc_entry = normalized["table_of_contents"][0]

    assert toc_entry["page"] == "xv"
    assert toc_entry["page_label"] == "15"
    assert toc_entry["pdf_page"] == 15


def test_normalize_pdf_options_defaults_metadata_delete_to_false():
    parser = _make_parser_with_settings({"pdf_options": {}})
    normalized = parser._normalize_pdf_options()
    assert normalized["metadata_delete_flagged_pages"] is False


def test_normalize_pdf_options_migrates_legacy_metadata_exclude_setting():
    parser = _make_parser_with_settings(
        {"pdf_options": {"metadata_exclude_flagged_pages": True}}
    )
    normalized = parser._normalize_pdf_options()

    assert normalized["metadata_delete_flagged_pages"] is True
    assert "metadata_exclude_flagged_pages" not in normalized


def test_page_side_map_preserves_physical_parity_after_exclusion():
    left_box = [0.05, 0.05, 0.9, 0.95]
    right_box = [0.1, 0.05, 0.95, 0.95]
    parser = _make_parser_with_settings(
        {
            "crop_boxes": {
                "mode": "left_right",
                "first_page_side": "right",
                "left": left_box,
                "right": right_box,
            }
        }
    )

    page_side_map = parser._build_page_side_map(
        included_pages=[0, 2, 3],
        parity_pages=[0, 1, 2, 3],
    )

    assert page_side_map == {0: "right", 2: "right", 3: "left"}
    assert parser._get_page_crop_box(2, page_side_map, None) == right_box
    assert parser._get_page_crop_box(3, page_side_map, None) == left_box


def test_page_index_list_accepts_internal_sets():
    parser = _make_parser_with_settings()

    assert parser._parse_page_index_list({2, 0, "4"}) == [0, 2, 4]


def test_metadata_flagged_pages_are_only_deleted_without_crop_review():
    parser = _make_parser_with_settings()

    assert parser._should_delete_metadata_flagged_pages(
        {"metadata_delete_flagged_pages": True, "skip_cropping": True},
    )
    assert parser._should_delete_metadata_flagged_pages(
        {"metadata_delete_flagged_pages": True, "skip_cropping": False},
        auto_mode=True,
    )
    assert not parser._should_delete_metadata_flagged_pages(
        {"metadata_delete_flagged_pages": True, "skip_cropping": False},
    )


def test_metadata_flagged_page_indexes_respects_source_pages():
    parser = _make_parser_with_settings()
    parser.document_metadata = {
        "source_pages": [1, 2],
        "pages_to_remove": [
            {"page": 1, "safe_to_remove": True, "confidence": 0.99},
            {"page": 2, "safe_to_remove": True, "confidence": 0.5},
            {"page": 4, "safe_to_remove": True, "confidence": 1.0},
        ],
    }

    flagged = parser._metadata_flagged_page_indexes(total_pages=10)
    assert flagged == {0}


def test_metadata_flagged_page_indexes_requires_explicit_high_confidence_safety_decision():
    parser = _make_parser_with_settings()
    parser.document_metadata = {
        "pages_to_remove": [
            {"page": 1, "reason": "legacy suggestion"},
            {"page": 2, "safe_to_remove": False, "confidence": 1.0},
            {"page": 3, "safe_to_remove": True, "confidence": 0.97},
            {"page": 4, "safe_to_remove": True, "confidence": 0.98},
        ]
    }

    assert parser._metadata_flagged_page_indexes(total_pages=10) == {3}


def test_pdf_parser_uses_metadata_model_from_settings():
    from unittest.mock import MagicMock, patch

    parser = _make_parser_with_settings(
        {
            "parsing_model": "gemini/gemini-2.5-pro",
            "metadata_model": "gemini/gemini-2.5-flash",
        }
    )

    import threading

    parser.llm_service = MagicMock()
    parser.metrics_lock = threading.Lock()
    parser._previous_outputs = []
    parser.metrics = {"failed_pages": []}
    parser.used_models = {}
    parser.current_headings = {"h1": None, "h2": None, "h3": None}
    parser.current_author = None

    with (
        patch.object(
            parser,
            "_normalize_pdf_options",
            return_value={"append_previous": False, "page_range": "3-3"},
        ) as mock_opts,
        patch.object(
            parser, "_get_excluded_pages", return_value=set()
        ) as mock_excluded,
        patch.object(parser, "_extract_document_metadata") as mock_extract,
        patch.object(
            parser, "_render_page_to_png", return_value="dummy_path.png"
        ) as mock_render,
        patch.object(
            parser, "_build_prompt_with_context", return_value="dummy_prompt"
        ) as mock_prompt_context,
        patch.object(
            parser, "_process_rendered_page", return_value="<p>test</p>"
        ) as mock_proc,
        patch.object(parser, "_build_boundary_flags", return_value={}) as mock_boundary,
        patch.object(
            parser, "_fallback_page_html", return_value="<p>test</p>"
        ) as mock_fallback,
    ):
        fake_doc = MagicMock()
        fake_doc.__len__.return_value = 4
        fake_page = MagicMock()
        fake_page.rect.width = 1000
        fake_page.rect.height = 1000
        fake_doc.__getitem__.return_value = fake_page

        with patch.object(pdf_parser_module.fitz, "open", return_value=fake_doc):
            parser.absolute_path = MagicMock()
            parser.absolute_path.exists.return_value = True
            parser.absolute_path.__str__.return_value = "dummy.pdf"
            parser.state = None

            parser.process(auto_mode=True)

            mock_extract.assert_called_once()
            args, kwargs = mock_extract.call_args
            assert args[1] == [0, 1, 2, 3]
            assert args[4] == "gemini/gemini-2.5-flash"


def test_page_extraction_metrics_use_requested_custom_model_for_pricing():
    """Preserve custom pricing when the provider returns a normalized model id."""
    parser = object.__new__(PdfParser)
    active_llm_service = MagicMock()
    response = SimpleNamespace(
        # OpenAI-compatible endpoints commonly omit the app's custom provider prefix.
        model="gpt-5.6-luna-2026-07-09",
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="<p>Extracted page</p>"),
                finish_reason="stop",
            )
        ],
    )
    active_llm_service.completion.return_value = response

    parser.llm_service = active_llm_service
    parser.settings = {}
    parser._system_prompt_cache = "You are a document parser."
    parser._copy_debug_image = MagicMock()
    parser._completion_kwargs = MagicMock(return_value={})
    parser._record_model_use = MagicMock()
    parser._record_response_metrics = MagicMock()
    parser._record_debug_call = MagicMock()
    parser.metrics_lock = threading.Lock()
    parser.metrics = {"semantic_validation_rejections": []}

    image = MagicMock()
    image.mode = "RGB"
    image.save.side_effect = lambda buffer, **_kwargs: buffer.write(b"jpeg")
    image_context = MagicMock()
    image_context.__enter__.return_value = image

    requested_model = "custom_u1_mama/gpt-5.6-luna"
    with patch("PIL.Image.open", return_value=image_context):
        parser._call_page_model(
            "page.png",
            "Extract this page.",
            requested_model,
            page_number=1,
            llm_service=active_llm_service,
        )

    parser._record_response_metrics.assert_called_once_with(
        response,
        active_llm_service,
        model=requested_model,
    )


def test_png_image_transport_page_model_uses_shared_png_delivery_helper():
    parser = object.__new__(PdfParser)
    parser.settings = {"mcp_image_transport": "png-2000-v1"}
    parser.llm_service = MagicMock()
    parser._system_prompt_cache = "You are a document parser."
    parser._copy_debug_image = MagicMock()
    parser._completion_kwargs = MagicMock(return_value={})
    parser._record_model_use = MagicMock()
    parser._record_response_metrics = MagicMock()
    parser._record_debug_call = MagicMock()
    parser._image_file_to_data_url = MagicMock(
        return_value="data:image/png;base64,png"
    )
    parser.metrics_lock = threading.Lock()
    parser.metrics = {"semantic_validation_rejections": []}
    parser._completion_with_provider_fallback = MagicMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="<main_content><p>Text</p></main_content>"
                        "<footnotes></footnotes>"
                    ),
                    finish_reason="stop",
                )
            ]
        )
    )

    result = parser._call_page_model(
        "page.png",
        "Extract this page.",
        "custom/test-model",
        page_number=1,
        llm_service=parser.llm_service,
    )

    assert "<main_content>" in result
    parser._image_file_to_data_url.assert_called_once_with("page.png", max_width=None)
    image_block = parser._completion_with_provider_fallback.call_args.kwargs[
        "messages"
    ][1]["content"][1]
    assert image_block["image_url"]["url"] == "data:image/png;base64,png"


def test_page_extraction_retries_unrelated_model_output_with_page_only_instruction():
    parser = object.__new__(PdfParser)
    parser.llm_service = MagicMock()
    parser.settings = {"pdf_options": {}}
    parser._system_prompt_cache = "Document parser instructions."
    parser._copy_debug_image = MagicMock()
    parser._completion_kwargs = MagicMock(return_value={})
    parser._record_model_use = MagicMock()
    parser._record_response_metrics = MagicMock()
    parser._record_debug_call = MagicMock()
    parser.metrics_lock = threading.Lock()
    parser.metrics = {"semantic_validation_rejections": []}

    source = " ".join(
        "absolute spirit develops through concept judgment syllogism logic "
        "freedom consciousness dialectic substance subject philosophy reason"
        for _ in range(5)
    )
    unrelated = (
        "<main_content><p>"
        + " ".join(
            "wireless beacon indoor positioning sensor calibration antenna "
            "network protocol signal measurement localization algorithm survey"
            for _ in range(4)
        )
        + "</p></main_content><footnotes></footnotes>"
    )
    faithful = f"<main_content><p>{source}</p></main_content><footnotes></footnotes>"

    def response(content):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=content),
                    finish_reason="stop",
                )
            ]
        )

    parser._completion_with_provider_fallback = MagicMock(
        side_effect=[response(unrelated), response(faithful)]
    )
    image = MagicMock()
    image.mode = "RGB"
    image.save.side_effect = lambda buffer, **_kwargs: buffer.write(b"jpeg")
    image_context = MagicMock()
    image_context.__enter__.return_value = image

    with patch("PIL.Image.open", return_value=image_context):
        result = parser._call_page_model(
            "page.png",
            "Extract this page.",
            "custom/test-model",
            page_number=12,
            llm_service=parser.llm_service,
            source_text=source,
        )

    assert "absolute spirit" in result
    assert parser._completion_with_provider_fallback.call_count == 2
    retry_messages = parser._completion_with_provider_fallback.call_args_list[1].kwargs[
        "messages"
    ]
    retry_text = retry_messages[1]["content"][0]["text"]
    assert "RETRY:" in retry_text
    assert "only the currently supplied page image" in retry_text
    assert parser._record_debug_call.call_count == 2
    assert parser.metrics["semantic_validation_rejections"] == [
        {
            "page": 12,
            "stage": "page_source_grounding",
            "reason": "source grounding mismatch (bag=0.000, sequence=0.000)",
        }
    ]


def test_page_extraction_accepts_explicit_toc_only_marker_without_retry():
    parser = object.__new__(PdfParser)
    parser.llm_service = MagicMock()
    parser.settings = {"pdf_options": {}}
    parser._system_prompt_cache = "Document parser instructions."
    parser._copy_debug_image = MagicMock()
    parser._completion_kwargs = MagicMock(return_value={})
    parser._record_model_use = MagicMock()
    parser._record_response_metrics = MagicMock()
    parser._record_debug_call = MagicMock()
    parser.metrics_lock = threading.Lock()
    parser.metrics = {"semantic_validation_rejections": []}
    marker = (
        '<main_content><nav class="source-toc"></nav></main_content>'
        "<footnotes></footnotes>"
    )
    parser._completion_with_provider_fallback = MagicMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=marker),
                    finish_reason="stop",
                )
            ]
        )
    )
    image = MagicMock()
    image.mode = "RGB"
    image.save.side_effect = lambda buffer, **_kwargs: buffer.write(b"jpeg")
    image_context = MagicMock()
    image_context.__enter__.return_value = image

    with patch("PIL.Image.open", return_value=image_context):
        result = parser._call_page_model(
            "page.png",
            "Extract this page.",
            "custom/test-model",
            page_number=11,
            llm_service=parser.llm_service,
            source_text="printed table of contents entry " * 60,
        )

    assert BeautifulSoup(result, "html.parser").find("nav") is None
    assert parser._completion_with_provider_fallback.call_count == 1
    assert parser.metrics["semantic_validation_rejections"] == []


def test_page_extraction_gives_up_after_three_invalid_outputs_without_leaking_source_text():
    parser = object.__new__(PdfParser)
    parser.llm_service = MagicMock()
    parser.settings = {"pdf_options": {}}
    parser._system_prompt_cache = "Document parser instructions."
    parser._copy_debug_image = MagicMock()
    parser._completion_kwargs = MagicMock(return_value={})
    parser._record_model_use = MagicMock()
    parser._record_response_metrics = MagicMock()
    parser._record_debug_call = MagicMock()
    parser.metrics_lock = threading.Lock()
    parser.metrics = {"semantic_validation_rejections": []}
    source = "private-source-sentinel " * 60
    unrelated = (
        "<main_content><p>"
        + "wireless localization calibration survey protocol network " * 20
        + "</p></main_content><footnotes></footnotes>"
    )
    invalid_response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=unrelated),
                finish_reason="stop",
            )
        ]
    )
    parser._completion_with_provider_fallback = MagicMock(
        side_effect=[invalid_response, invalid_response, invalid_response]
    )
    image = MagicMock()
    image.mode = "RGB"
    image.save.side_effect = lambda buffer, **_kwargs: buffer.write(b"jpeg")
    image_context = MagicMock()
    image_context.__enter__.return_value = image

    with (
        patch("PIL.Image.open", return_value=image_context),
        pytest.raises(RuntimeError) as caught,
    ):
        parser._call_page_model(
            "page.png",
            "Extract this page.",
            "custom/test-model",
            page_number=12,
            llm_service=parser.llm_service,
            source_text=source,
        )

    assert parser._completion_with_provider_fallback.call_count == 3
    assert "stage=source_grounding" in str(caught.value)
    assert "private-source-sentinel" not in str(caught.value)
    assert len(parser.metrics["semantic_validation_rejections"]) == 3


def test_page_extraction_does_not_log_or_propagate_raw_provider_error_text(caplog):
    sentinel = "private-provider-payload-sentinel"
    parser = object.__new__(PdfParser)
    parser.llm_service = MagicMock()
    parser.settings = {"pdf_options": {}}
    parser._system_prompt_cache = "Document parser instructions."
    parser._copy_debug_image = MagicMock()
    parser._completion_kwargs = MagicMock(return_value={})
    parser._record_model_use = MagicMock()
    parser._record_response_metrics = MagicMock()
    parser._record_debug_call = MagicMock()
    parser._completion_with_provider_fallback = MagicMock(
        side_effect=RuntimeError(sentinel)
    )

    image = MagicMock()
    image.mode = "RGB"
    image.save.side_effect = lambda buffer, **_kwargs: buffer.write(b"jpeg")
    image_context = MagicMock()
    image_context.__enter__.return_value = image

    with (
        patch("PIL.Image.open", return_value=image_context),
        caplog.at_level(
            logging.WARNING,
            logger="iarf_assistant.addons.doc_processor.parsers.pdf_parser",
        ),
        pytest.raises(RuntimeError) as caught,
    ):
        parser._call_page_model(
            "page.png",
            "Private source prompt.",
            "gemini/test-model",
            page_number=3,
            llm_service=parser.llm_service,
        )

    assert sentinel not in caplog.text
    assert sentinel not in str(caught.value)
    assert caught.value.__cause__ is None


def test_evaluation_failure_log_is_source_safe(caplog):
    sentinel = "private-evaluator-provider-payload"
    parser = object.__new__(PdfParser)
    parser.llm_service = MagicMock()
    parser.settings = {"pdf_options": {"toc_handling": "preserve"}}
    parser._evaluation_prompt_template = "{original_prompt}\n{original_output}"
    parser._completion_kwargs = MagicMock(return_value={})
    parser._record_model_use = MagicMock()
    parser._record_evaluation_decision = MagicMock()
    parser._completion_with_provider_fallback = MagicMock(
        side_effect=RuntimeError(sentinel)
    )

    with caplog.at_level(
        logging.WARNING,
        logger="iarf_assistant.addons.doc_processor.parsers.pdf_parser",
    ):
        result = parser._evaluate_output(
            "Private original prompt.",
            "<main_content><p>Private text.</p></main_content><footnotes></footnotes>",
            "gemini/test-model",
            page_number=3,
        )

    assert "Private text" in result
    assert sentinel not in caplog.text


@pytest.mark.parametrize(
    "execution_settings,has_authoring_rule",
    [
        ({}, True),
        ({"mcp_execution_mode": "passive"}, False),
        (
            {"mcp_execution_mode": "passive", "mcp_native_vision_authoring_policy": "direct-v1"},
            True,
        ),
    ],
)
def test_evaluator_uses_dedicated_system_boundary_and_keeps_refined_template_as_user(
    execution_settings, has_authoring_rule
):
    parser = object.__new__(PdfParser)
    parser.llm_service = MagicMock()
    parser.settings = {
        **execution_settings,
        "pdf_options": {"prompt_cache": False, "toc_handling": "omit"},
    }
    parser._system_prompt_cache = "configured extraction HTML system"
    parser._evaluation_prompt_template = (
        "Review this page.\n{original_prompt}\n{original_output}"
    )
    parser._completion_kwargs = MagicMock(return_value={})
    parser._record_model_use = MagicMock()
    parser._record_evaluation_decision = MagicMock()
    parser._completion_with_provider_fallback = MagicMock(
        side_effect=RuntimeError("provider failure")
    )

    result = parser._evaluate_output(
        "document-derived prior HTML",
        "<main_content><p>document text</p></main_content><footnotes></footnotes>",
        "test-model",
        page_number=1,
    )

    assert "document text" in result
    messages = parser._completion_with_provider_fallback.call_args.kwargs["messages"]
    assert messages[0]["role"] == "system"
    assert DEFAULT_PDF_EVALUATOR_SYSTEM_PROMPT in messages[0]["content"]
    assert (
        PDF_NATIVE_VISION_AUTHORING_INSTRUCTION in messages[0]["content"]
    ) is has_authoring_rule
    if not has_authoring_rule:
        assert messages[0]["content"] == DEFAULT_PDF_EVALUATOR_SYSTEM_PROMPT
    assert "configured extraction HTML system" not in messages[0]["content"]
    user_text = "\n".join(
        block.get("text", "")
        for block in messages[1]["content"]
        if block["type"] == "text"
    )
    assert "Review this page." in user_text
    assert "document-derived prior HTML" in user_text


def test_boundary_evidence_uses_bounded_untrusted_projection():
    parser = object.__new__(PdfParser)
    parser.document_metadata = {
        "title": "Boundary book",
        "pages_to_remove": [{"page": 1, "reason": "filler"}],
        "table_of_contents": [
            {
                "level": 1 if index % 20 == 0 else 2,
                "title": f"Chapter {index}",
                "page": str(index + 1),
                "page_label": str(index + 1),
                "pdf_page": index + 1,
            }
            for index in range(1_000)
        ],
    }

    evidence = parser._build_boundary_evidence(
        499,
        500,
        "<main_content><p>previous text</p></main_content><footnotes></footnotes>",
        "<main_content><p>next text</p></main_content><footnotes></footnotes>",
        {"main_content": "auto", "footnotes": "separate"},
    )

    assert evidence.index("<UNTRUSTED_DOCUMENT_EVIDENCE>") < evidence.index(
        '"boundary"'
    )
    assert "pages_to_remove" not in evidence
    assert '"Chapter 499"' in evidence
    assert '"Chapter 480"' in evidence
    assert "previous text" in evidence and "next text" in evidence


def test_boundary_evidence_retains_the_previous_tail_and_next_head():
    parser = _make_parser_with_settings()
    parser.document_metadata = {}
    previous = "PREVIOUS_START " + "middle " * 350 + " PREVIOUS_END"
    following = "NEXT_START " + "middle " * 350 + " NEXT_END"
    evidence = parser._build_boundary_evidence(
        0, 1,
        f"<main_content><p>{previous}</p></main_content>",
        f"<main_content><p>{following}</p></main_content>",
        {"main_content": "auto"},
    )
    assert "PREVIOUS_END" in evidence and "PREVIOUS_START" not in evidence
    assert "NEXT_START" in evidence and "NEXT_END" not in evidence


def test_json_system_override_never_inherits_extraction_html_contract():
    parser = object.__new__(PdfParser)
    parser.settings = {"pdf_options": {"prompt_cache": False}}
    parser._system_prompt_cache = "Output ONLY HTML and use <main_content>."

    metadata_message = parser._build_cached_system_message(
        "Metadata schema instructions",
        base_prompt=DEFAULT_PDF_JSON_SYSTEM_PROMPT,
    )

    assert metadata_message["role"] == "system"
    assert "Return only JSON" in metadata_message["content"]
    assert "Output ONLY HTML" not in metadata_message["content"]
    assert "<main_content>" not in metadata_message["content"]


def test_pdf_parser_reports_native_extraction_progress():
    from unittest.mock import MagicMock, patch

    parser = _make_parser_with_settings(
        {"pdf_options": {"metadata_prepass": False, "append_previous": False}}
    )
    parser.llm_service = MagicMock()
    parser.metrics_lock = __import__("threading").Lock()
    parser._previous_outputs = []
    parser.metrics = {"failed_pages": []}
    parser.current_headings = {"h1": None, "h2": None, "h3": None}
    parser.current_author = None
    parser.document_metadata = None
    parser.state = None
    parser.absolute_path = MagicMock()
    parser.absolute_path.exists.return_value = True
    parser.absolute_path.name = "dummy.pdf"
    progress_updates = []

    fake_doc = MagicMock()
    fake_doc.__len__.return_value = 2
    fake_doc.__getitem__.return_value = MagicMock()

    with (
        patch.object(pdf_parser_module.fitz, "open", return_value=fake_doc),
        patch.object(parser, "_get_excluded_pages", return_value=set()),
        patch.object(parser, "_extract_document_metadata"),
        patch.object(parser, "_render_page_to_png", return_value="dummy_path.png"),
        patch.object(parser, "_build_prompt_with_context", return_value="dummy_prompt"),
        patch.object(parser, "_process_rendered_page", return_value="<p>test</p>"),
        patch.object(parser, "_build_boundary_flags", return_value={}),
    ):
        status, _html = parser.process(
            auto_mode=True,
            progress_callback=lambda pct, msg: progress_updates.append((pct, msg)),
        )

    assert status == "COMPLETED"
    assert progress_updates[0][0] == 2
    assert any(
        msg.startswith("Extracting page 1 of 2") for _pct, msg in progress_updates
    )
    assert any(pct >= 50 and "Extracted page" in msg for pct, msg in progress_updates)
    assert progress_updates[-1][0] == 99


def test_pdf_parser_pauses_after_completed_page_and_records_actual_models():
    from unittest.mock import MagicMock, patch

    parser = _make_parser_with_settings(
        {
            "parsing_model": "lmstudio/example-model",
            "pdf_options": {
                "metadata_prepass": False,
                "append_previous": False,
                "extract_images": False,
            },
        }
    )
    parser.llm_service = MagicMock()
    parser.metrics_lock = __import__("threading").Lock()
    parser._previous_outputs = []
    parser.metrics = {"failed_pages": []}
    parser.used_models = {}
    parser.current_headings = {"h1": None, "h2": None, "h3": None}
    parser.current_author = None
    parser.document_metadata = None
    parser.state = None
    parser.absolute_path = MagicMock()
    parser.absolute_path.exists.return_value = True
    parser.absolute_path.name = "dummy.pdf"
    control_actions = iter([None, None, "pause"])

    fake_doc = MagicMock()
    fake_doc.__len__.return_value = 2
    fake_doc.__getitem__.return_value = MagicMock()

    with (
        patch.object(pdf_parser_module.fitz, "open", return_value=fake_doc),
        patch.object(parser, "_get_excluded_pages", return_value=set()),
        patch.object(parser, "_render_page_to_png", return_value="dummy_path.png"),
        patch.object(parser, "_build_prompt_with_context", return_value="dummy_prompt"),
        patch.object(
            parser,
            "_process_rendered_page",
            return_value="<main_content><p>Page one</p></main_content>",
        ),
    ):
        status, state = parser.process(
            auto_mode=False,
            control_callback=lambda: next(control_actions),
        )

    parser._record_model_use("extraction", "lmstudio/example-model")
    parser._record_model_use("extraction", "lmstudio/example-model")

    assert status == "PAUSED_USER"
    assert state["html_parts"][0] == "<main_content><p>Page one</p></main_content>"
    assert state["html_parts"][1] is None
    assert parser.settings["models_used"] == {"extraction": ["lmstudio/example-model"]}

    parser.state = state
    parser._previous_outputs = []
    parser.current_headings = {"h1": None, "h2": None, "h3": None}
    parser.current_author = None
    with (
        patch.object(pdf_parser_module.fitz, "open", return_value=fake_doc),
        patch.object(parser, "_get_excluded_pages", return_value=set()),
        patch.object(parser, "_render_page_to_png", return_value="dummy_path.png"),
        patch.object(parser, "_build_prompt_with_context", return_value="dummy_prompt"),
        patch.object(
            parser,
            "_process_rendered_page",
            return_value="<main_content><p>Page two</p></main_content>",
        ) as process_page,
        patch.object(parser, "_build_boundary_flags", return_value={}),
    ):
        resumed_status, _resumed_html = parser.process(auto_mode=True)

    assert resumed_status == "COMPLETED"
    # The normal parser path retains page one and asks the model only for the
    # missing page; it is not the image-review-only resume worker.
    process_page.assert_called_once()


def test_normalize_pdf_options_defaults_metadata_end_pages_to_zero():
    parser = _make_parser_with_settings({"pdf_options": {}})
    normalized = parser._normalize_pdf_options()
    assert normalized["metadata_end_pages"] == 0


def test_extract_document_metadata_page_selection():
    from unittest.mock import MagicMock, patch

    parser = _make_parser_with_settings(
        {
            "pdf_options": {
                "metadata_prepass": True,
                "metadata_pages": 3,
                "metadata_end_pages": 2,
                "prompt_cache": False,
            }
        }
    )
    parser.llm_service = MagicMock()
    parser.document_metadata = None
    parser.absolute_path = MagicMock()
    parser.absolute_path.name = "dummy.pdf"
    parser._metadata_prompt = "metadata prompt"

    with (
        patch.object(parser, "_get_page_crop_box", return_value=None),
        patch.object(
            parser, "_render_page_to_png", return_value="dummy_path.png"
        ) as mock_render,
        patch.object(
            parser,
            "_image_file_to_data_url",
            return_value="data:image/png;base64,dummy",
        ),
        patch.object(
            parser, "_normalize_document_metadata", return_value={"mock": "metadata"}
        ),
        patch.object(parser, "_safe_json_loads", return_value={"mock": "metadata"}),
        patch.object(
            parser,
            "_build_cached_system_message",
            return_value={"role": "system", "content": "prompt"},
        ),
        patch.object(parser, "_record_response_metrics"),
        patch.object(
            parser,
            "_extract_heading_structure",
            side_effect=lambda doc, metadata, *args, **kwargs: metadata,
        ),
        patch("os.path.exists", return_value=True),
        patch("os.remove"),
    ):
        mock_completion = MagicMock()
        mock_completion.choices = [MagicMock()]
        mock_completion.choices[0].message.content = '{"mock": "metadata"}'
        parser.llm_service.completion.return_value = mock_completion

        doc = MagicMock()
        included_pages = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]
        page_side_map = {}
        default_crop_box = None

        res = parser._extract_document_metadata(
            doc, included_pages, page_side_map, default_crop_box, "mock-model"
        )

        retrieved_indexes = [call.args[0] for call in doc.__getitem__.call_args_list]
        assert retrieved_indexes == [0, 1, 2, 8, 9]
        assert (
            parser.llm_service.completion.call_args.kwargs["provider_max_attempts"] == 3
        )


def test_extract_document_metadata_page_selection_overlap():
    from unittest.mock import MagicMock, patch

    parser = _make_parser_with_settings(
        {
            "pdf_options": {
                "metadata_prepass": True,
                "metadata_pages": 3,
                "metadata_end_pages": 2,
            }
        }
    )
    parser.llm_service = MagicMock()
    parser.document_metadata = None
    parser.absolute_path = MagicMock()
    parser.absolute_path.name = "dummy.pdf"

    with (
        patch.object(parser, "_get_page_crop_box", return_value=None),
        patch.object(
            parser, "_render_page_to_png", return_value="dummy_path.png"
        ) as mock_render,
        patch.object(
            parser,
            "_image_file_to_data_url",
            return_value="data:image/png;base64,dummy",
        ),
        patch.object(
            parser, "_normalize_document_metadata", return_value={"mock": "metadata"}
        ),
        patch.object(parser, "_safe_json_loads", return_value={"mock": "metadata"}),
        patch("os.path.exists", return_value=True),
        patch("os.remove"),
    ):
        mock_completion = MagicMock()
        mock_completion.choices = [MagicMock()]
        mock_completion.choices[0].message.content = '{"mock": "metadata"}'
        parser.llm_service.completion.return_value = mock_completion

        doc = MagicMock()
        included_pages = [0, 1, 2, 3]
        page_side_map = {}
        default_crop_box = None

        res = parser._extract_document_metadata(
            doc, included_pages, page_side_map, default_crop_box, "mock-model"
        )

        retrieved_indexes = [call.args[0] for call in doc.__getitem__.call_args_list]
        assert retrieved_indexes == [0, 1, 2, 3]


def test_metadata_toc_page_request_only_honors_partial_visible_toc():
    complete = {
        "table_of_contents_status": "complete",
        "toc_page_request": {"from_start": 5, "from_end": 4},
    }
    not_visible = {
        "table_of_contents_status": "not_visible",
        "toc_page_request": {"from_start": 5, "from_end": 4},
    }
    partial = {
        "table_of_contents_status": "partial",
        "toc_page_request": {"from_start": 999, "from_end": 4},
    }

    assert PdfParser._get_metadata_toc_page_request(
        complete, end_window_available=True
    ) == (0, 0)
    assert PdfParser._get_metadata_toc_page_request(
        not_visible, end_window_available=True
    ) == (0, 0)
    assert PdfParser._get_metadata_toc_page_request(
        partial, end_window_available=False
    ) == (20, 0)
    assert PdfParser._get_metadata_toc_page_request(
        partial, end_window_available=True
    ) == (20, 4)


def test_select_heading_structure_evidence_pages_adds_representative_chapter_pages():
    metadata = {
        "table_of_contents": [
            {"level": 1, "title": "SECTION ONE", "pdf_page": 5},
            {"level": 2, "title": "1. First Chapter", "pdf_page": 10},
            {"level": 2, "title": "2. Middle Chapter", "pdf_page": 40},
            {"level": 2, "title": "3. Last Chapter", "pdf_page": 70},
        ],
        "heading_structure": {
            "roles": [{"role": "chapter"}, {"role": "within_chapter"}],
        },
    }

    selected = PdfParser._select_heading_structure_evidence_pages(
        metadata,
        list(range(100)),
        list(range(20)),
    )

    assert selected[:3] == [0, 1, 2]
    assert selected[3:] == [9, 12, 16, 22, 33, 50, 69, 72, 82]


def test_select_heading_structure_evidence_pages_does_not_require_a_toc():
    selected = PdfParser._select_heading_structure_evidence_pages(
        {},
        list(range(100)),
        [0, 1, 2],
    )

    assert selected == [0, 1, 2, 16, 33, 50, 66, 82, 97, 98, 99]


class _TextLayerPage:
    def __init__(self, text, *, full_page_image=False):
        self._text = text
        self._full_page_image = full_page_image
        self.rect = fitz.Rect(0, 0, 100, 100)

    def get_text(self, mode):
        if mode == "text":
            return self._text
        return {"blocks": []}

    def get_image_info(self):
        return [{"bbox": (0, 0, 100, 100)}] if self._full_page_image else []


def test_assess_document_text_layer_distinguishes_native_ocr_and_image_only():
    native = [_TextLayerPage("native text " * 20) for _ in range(7)]
    ocr = [_TextLayerPage("ocr text " * 20, full_page_image=True) for _ in range(7)]
    image_only = [_TextLayerPage("", full_page_image=True) for _ in range(7)]

    assert (
        PdfParser._assess_document_text_layer(native, list(range(7)))["mode"]
        == "native_text"
    )
    assert (
        PdfParser._assess_document_text_layer(ocr, list(range(7)))["mode"]
        == "ocr_overlay"
    )
    assert (
        PdfParser._assess_document_text_layer(image_only, list(range(7)))["mode"]
        == "image_only"
    )


def test_heading_evidence_requests_are_bounded_and_scoped_to_included_pages():
    requested = PdfParser._get_heading_evidence_request_pages(
        {
            "evidence_requests": [
                {"pages": [1, 3, 7, 500], "kind": "body_opener", "reason": "Confirm"},
                {
                    "pages": [4, 5, 6, 7],
                    "kind": "interior_heading",
                    "reason": "Check depth",
                },
            ]
        },
        included_pages=list(range(10)),
        supplied_pages={0, 2},
    )

    assert requested == [6, 3, 4, 5]


def test_focused_heading_pass_requests_and_validates_body_evidence():
    parser = _make_parser_with_settings({"pdf_options": {"prompt_cache": False}})
    parser.llm_service = MagicMock()
    parser._completion_kwargs = MagicMock(return_value={})

    toc_only = {
        "toc_coverage": "selective",
        "roles": [
            {
                "role": "article",
                "label": "papers",
                "depth": 1,
                "html_level": 2,
                "parents": [],
                "toc_presence": "listed",
                "numbering": "unnumbered",
                "confidence": 0.9,
                "evidence": [
                    {
                        "page": 1,
                        "kind": "toc_entry",
                        "description": "Listed in contents",
                    }
                ],
            }
        ],
        "conventions": {
            "opener_style": "single_line",
            "merge_label_and_title": False,
            "subordinate_numbering": "unknown",
            "unlisted_heading_policy": "layout_only",
        },
        "recognition_patterns": [],
        "document_specific_notes": [],
        "uncertainties": ["Body opener not yet supplied."],
        "evidence_requests": [
            {"pages": [24], "kind": "body_opener", "reason": "Confirm paper opener"}
        ],
    }
    confirmed = {
        **toc_only,
        "roles": [
            {
                **toc_only["roles"][0],
                "evidence": [
                    {
                        "page": 1,
                        "kind": "toc_entry",
                        "description": "Listed in contents",
                    },
                    {
                        "page": 24,
                        "kind": "body_opener",
                        "description": "Full paper opener",
                    },
                    {
                        "page": 77,
                        "kind": "body_opener",
                        "description": "Unsupplied hallucination",
                    },
                ],
            }
        ],
        "uncertainties": [],
        "evidence_requests": [],
    }

    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = "{}"
    parser._completion_with_structured_json = MagicMock(
        side_effect=[response, response]
    )

    with (
        patch.object(
            parser, "_assess_document_text_layer", return_value={"mode": "image_only"}
        ),
        patch.object(
            parser, "_build_native_heading_candidate_catalog", return_value=[]
        ),
        patch.object(parser, "_render_heading_contact_sheets", return_value=[]),
        patch.object(parser, "_get_page_crop_box", return_value=None),
        patch.object(parser, "_render_page_to_png", return_value="dummy.png"),
        patch.object(
            parser,
            "_image_file_to_data_url",
            return_value="data:image/png;base64,dummy",
        ),
        patch.object(parser, "_copy_debug_image"),
        patch.object(parser, "_safe_json_loads", side_effect=[toc_only, confirmed]),
        patch.object(parser, "_record_model_use"),
        patch.object(parser, "_record_response_metrics"),
        patch.object(parser, "_record_debug_call"),
        patch.object(
            parser,
            "_build_cached_system_message",
            return_value={"role": "system", "content": "prompt"},
        ),
    ):
        result = parser._extract_heading_structure(
            MagicMock(__len__=lambda self: 100),
            {
                "table_of_contents": [{"level": 1, "title": "A Paper"}],
                "source_pages": [1],
            },
            list(range(100)),
            [0],
            {},
            [],
            {},
            None,
            "mock-model",
        )

    structure = result["heading_structure"]
    assert parser._completion_with_structured_json.call_count == 2
    assert structure["validation"]["status"] == "extractor_ready"
    assert [item["page"] for item in structure["roles"][0]["evidence"]] == [1, 24]
    assert 24 in result["source_pages"]


def test_extract_document_metadata_expands_start_window_for_partial_toc():
    parser = _make_parser_with_settings(
        {
            "pdf_options": {
                "metadata_prepass": True,
                "metadata_pages": 2,
                "metadata_end_pages": 0,
                "prompt_cache": False,
            }
        }
    )
    parser.llm_service = MagicMock()
    parser.document_metadata = None
    parser.absolute_path = MagicMock()
    parser.absolute_path.name = "dummy.pdf"
    parser._metadata_prompt = "metadata prompt"

    first_metadata = {
        "table_of_contents": [{"level": 1, "title": "Part One"}],
        "table_of_contents_status": "partial",
        "toc_page_request": {"from_start": 3, "from_end": 8},
    }
    expanded_metadata = {
        "table_of_contents": [
            {"level": 1, "title": "Part One"},
            {"level": 2, "title": "Chapter One"},
        ],
        "table_of_contents_status": "complete",
        "toc_page_request": {"from_start": 0, "from_end": 0},
    }

    def normalize(metadata, doc=None, source_pages=None):
        return {
            "table_of_contents": metadata["table_of_contents"],
            "source_pages": source_pages,
        }

    mock_completion = MagicMock()
    mock_completion.choices = [MagicMock()]
    mock_completion.choices[0].message.content = "{}"
    parser.llm_service.completion.return_value = mock_completion

    with (
        patch.object(parser, "_get_page_crop_box", return_value=None),
        patch.object(parser, "_render_page_to_png", return_value="dummy_path.png"),
        patch.object(
            parser,
            "_image_file_to_data_url",
            return_value="data:image/png;base64,dummy",
        ),
        patch.object(parser, "_normalize_document_metadata", side_effect=normalize),
        patch.object(
            parser, "_safe_json_loads", side_effect=[first_metadata, expanded_metadata]
        ),
        patch.object(parser, "_record_model_use"),
        patch.object(parser, "_record_response_metrics"),
        patch.object(parser, "_record_debug_call"),
        patch.object(
            parser,
            "_build_cached_system_message",
            return_value={"role": "system", "content": "prompt"},
        ),
        patch.object(
            parser,
            "_extract_heading_structure",
            side_effect=lambda doc, metadata, *args, **kwargs: metadata,
        ),
        patch("os.path.exists", return_value=True),
        patch("os.remove"),
    ):
        doc = MagicMock()
        result = parser._extract_document_metadata(
            doc,
            list(range(10)),
            {},
            None,
            "mock-model",
        )

    retrieved_indexes = [call.args[0] for call in doc.__getitem__.call_args_list]
    assert retrieved_indexes == [0, 1, 2, 3, 4]
    assert parser.llm_service.completion.call_count == 2
    assert result["source_pages"] == [1, 2, 3, 4, 5]
    assert result["table_of_contents"][-1]["title"] == "Chapter One"

    follow_up_content = parser.llm_service.completion.call_args_list[1].kwargs[
        "messages"
    ][1]["content"]
    follow_up_text = "\n".join(
        block.get("text", "") for block in follow_up_content if block["type"] == "text"
    )
    assert "PDF page 5" in follow_up_text
    assert "No end window is supplied" in follow_up_text


def test_extract_document_metadata_expands_configured_end_window_for_partial_toc():
    parser = _make_parser_with_settings(
        {
            "pdf_options": {
                "metadata_prepass": True,
                "metadata_pages": 1,
                "metadata_end_pages": 2,
                "prompt_cache": False,
            }
        }
    )
    parser.llm_service = MagicMock()
    parser.document_metadata = None
    parser.absolute_path = MagicMock()
    parser.absolute_path.name = "dummy.pdf"
    parser._metadata_prompt = "metadata prompt"

    partial_metadata = {
        "table_of_contents": [{"level": 2, "title": "Appendix B"}],
        "table_of_contents_status": "partial",
        "toc_page_request": {"from_start": 0, "from_end": 3},
    }
    complete_metadata = {
        **partial_metadata,
        "table_of_contents_status": "complete",
        "toc_page_request": {"from_start": 0, "from_end": 0},
    }

    mock_completion = MagicMock()
    mock_completion.choices = [MagicMock()]
    mock_completion.choices[0].message.content = "{}"
    parser.llm_service.completion.return_value = mock_completion

    with (
        patch.object(parser, "_get_page_crop_box", return_value=None),
        patch.object(parser, "_render_page_to_png", return_value="dummy_path.png"),
        patch.object(
            parser,
            "_image_file_to_data_url",
            return_value="data:image/png;base64,dummy",
        ),
        patch.object(
            parser,
            "_normalize_document_metadata",
            side_effect=lambda metadata, doc=None, source_pages=None: {
                "source_pages": source_pages
            },
        ),
        patch.object(
            parser,
            "_safe_json_loads",
            side_effect=[partial_metadata, complete_metadata],
        ),
        patch.object(parser, "_record_model_use"),
        patch.object(parser, "_record_response_metrics"),
        patch.object(parser, "_record_debug_call"),
        patch.object(
            parser,
            "_build_cached_system_message",
            return_value={"role": "system", "content": "prompt"},
        ),
        patch.object(
            parser,
            "_extract_heading_structure",
            side_effect=lambda doc, metadata, *args, **kwargs: metadata,
        ),
        patch("os.path.exists", return_value=True),
        patch("os.remove"),
    ):
        doc = MagicMock()
        result = parser._extract_document_metadata(
            doc,
            list(range(10)),
            {},
            None,
            "mock-model",
        )

    retrieved_indexes = [call.args[0] for call in doc.__getitem__.call_args_list]
    assert retrieved_indexes == [0, 8, 9, 5, 6, 7]
    assert parser.llm_service.completion.call_count == 2
    assert result["source_pages"] == [1, 6, 7, 8, 9, 10]


def test_safe_json_loads_strips_markdown_code_block_fences():
    parser = _make_parser_with_settings()

    # 1. Standard markdown block with json
    content = '```json\n{\n  "key": "value"\n}\n```'
    result = parser._safe_json_loads(content)
    assert result == {"key": "value"}

    # 2. Conversational prefix and suffix
    content_convo = (
        'Here is the result:\n```json\n{\n  "key": "value2"\n}\n```\nHope this helps!'
    )
    result_convo = parser._safe_json_loads(content_convo)
    assert result_convo == {"key": "value2"}

    # 3. Simple block fence
    content_simple = '```\n{\n  "key": "value3"\n}\n```'
    result_simple = parser._safe_json_loads(content_simple)
    assert result_simple == {"key": "value3"}


def _response_with_content(content="{}", finish_reason="STOP", choices=True):
    response = SimpleNamespace()
    response.choices = (
        [
            SimpleNamespace(
                message=SimpleNamespace(content=content), finish_reason=finish_reason
            )
        ]
        if choices
        else []
    )
    return response


def _response_parser():
    parser = object.__new__(PdfParser)
    parser.settings = {}
    parser.metrics = {"model_stage_diagnostics": []}
    parser.metrics_lock = threading.Lock()
    return parser


def test_metadata_schema_matches_the_controlled_publication_type_prompt_enum():
    assert METADATA_JSON_SCHEMA["properties"]["publication_type"]["enum"] == list(
        PUBLICATION_TYPE_VALUES
    )
    assert PUBLICATION_TYPE_VALUES == (
        "book",
        "journal_article",
        "article",
        "chapter",
        "report",
        "thesis",
        "newsletter",
        "magazine",
        "proceedings",
        "pamphlet",
        "letter",
        "unknown",
    )
    safe_remove_schema = METADATA_JSON_SCHEMA["properties"]["pages_to_remove"]["items"][
        "properties"
    ]["safe_to_remove"]
    assert safe_remove_schema == {"type": "boolean", "enum": [True]}


def test_metadata_prepass_reuses_the_enabled_schema_for_admission_and_completion():
    parser = _make_parser_with_settings(
        {
            "mcp_execution_mode": "passive",
            "mcp_metadata_policy": "budgeted-v1",
            "mcp_note_arrangement_policy": "source-arrangement-v1",
            "pdf_options": {"metadata_pages": 1, "prompt_cache": False},
        }
    )
    parser.llm_service = MagicMock()
    parser.document_metadata = None
    parser.absolute_path = SimpleNamespace(name="synthetic.pdf")
    parser._metadata_prompt = "Read the supplied metadata page."
    parser._uses_budgeted_metadata = MagicMock(return_value=True)
    parser._metadata_admission = MagicMock(
        return_value=SimpleNamespace(admitted=True)
    )
    parser._record_preparation = MagicMock()
    parser._record_model_use = MagicMock()
    parser._record_response_metrics = MagicMock()
    parser._record_debug_call = MagicMock()
    parser._copy_debug_image = MagicMock()
    parser._report_progress = MagicMock()
    parser._build_cached_system_message = MagicMock(
        return_value={"role": "system", "content": "metadata contract"}
    )
    parser._get_page_crop_box = MagicMock(return_value=None)
    parser._render_page_to_png = MagicMock(return_value="synthetic-page.png")
    parser._image_file_to_data_url = MagicMock(
        return_value="data:image/png;base64,c3ludGhldGlj"
    )
    parser._completion_with_structured_json = MagicMock(return_value=object())
    parser._structured_response_content = MagicMock(return_value='{"title":"Source"}')
    parser._safe_json_loads = MagicMock(return_value={"title": "Source"})
    parser._normalize_document_metadata = MagicMock(return_value={"title": "Source"})
    parser._extract_heading_structure = MagicMock(
        side_effect=lambda _doc, metadata, *_args, **_kwargs: metadata
    )
    parser._completion_kwargs = MagicMock(return_value={})
    parser._thinking_config = MagicMock(return_value=None)

    with patch("os.path.exists", return_value=False):
        parser._extract_document_metadata(MagicMock(), [0], {}, None, "synthetic-model")

    admitted_schema = parser._metadata_admission.call_args.args[2]
    completed_schema = parser._completion_with_structured_json.call_args.kwargs[
        "schema"
    ]
    assert admitted_schema is completed_schema
    assert "note_arrangement" in admitted_schema["required"]
    assert "note_arrangement" in admitted_schema["properties"]
    assert "note_arrangement" not in METADATA_JSON_SCHEMA["properties"]


@pytest.mark.parametrize(
    "finish_reason",
    [
        "MAX_TOKENS",
        "RECITATION",
        "SAFETY",
        "PROHIBITED_CONTENT",
        "SPII",
        "BLOCKLIST",
        "content_filter",
        "FinishReason.MAX_TOKENS",
    ],
)
def test_structured_response_accessor_rejects_provider_terminal_reasons(finish_reason):
    parser = _response_parser()

    with pytest.raises(StructuredResponseError) as error:
        parser._structured_response_content(
            _response_with_content(finish_reason=finish_reason), "metadata"
        )

    assert error.value.category.startswith("terminal_finish_reason_")


def test_structured_response_accessor_requires_choices_and_nonempty_content():
    parser = _response_parser()

    with pytest.raises(StructuredResponseError, match="missing_choices"):
        parser._structured_response_content(
            _response_with_content(choices=False), "metadata"
        )
    with pytest.raises(StructuredResponseError, match="empty_content"):
        parser._structured_response_content(
            _response_with_content(content=""), "metadata"
        )
    assert (
        parser._structured_response_content(
            _response_with_content("{}", "STOP"), "metadata"
        )
        == "{}"
    )


def test_prompt_cache_key_isolated_by_model_and_stable_for_same_scope():
    parser = _make_parser_with_settings({"pdf_options": {"prompt_cache": True}})
    parser.absolute_path = Path("/tmp/docproc-cache-test.pdf")
    parser._prompt_settings = {}
    parser._prompt_cache_disabled_for_provider = False

    first = parser._completion_kwargs("metadata", "vertex_ai/gemini-3.7-flash")
    same = parser._completion_kwargs("metadata", "vertex_ai/gemini-3.7-flash")
    other = parser._completion_kwargs("metadata", "vertex_ai/gemini-3.7-pro")

    assert first == same
    assert first["prompt_cache_key"] != other["prompt_cache_key"]


def _configure_metadata_failure_parser(profile):
    parser = _make_parser_with_settings(
        {
            "processing_profile": profile,
            "pdf_options": {
                "metadata_prepass": True,
                "metadata_pages": 1,
                "prompt_cache": False,
            },
        }
    )
    parser.llm_service = MagicMock()
    parser.document_metadata = None
    parser.absolute_path = Path("/tmp/docproc-metadata-failure.pdf")
    parser.metrics = {"model_stage_diagnostics": []}
    parser.metrics_lock = threading.Lock()
    parser._metadata_prompt = "metadata prompt"
    parser._cache_enabled = lambda: False
    parser._build_cached_system_message = MagicMock(
        return_value={"role": "system", "content": "prompt"}
    )
    parser._completion_with_structured_json = MagicMock(
        side_effect=StructuredResponseError("metadata_prepass", "empty_content")
    )
    return parser


@pytest.mark.parametrize(
    "profile,should_raise", [("custom", False), ("book_quality", True)]
)
def test_metadata_failure_is_visible_and_book_quality_fails_closed(
    profile, should_raise
):
    parser = _configure_metadata_failure_parser(profile)

    with (
        patch.object(parser, "_get_page_crop_box", return_value=None),
        patch.object(parser, "_render_page_to_png", return_value="dummy.png"),
        patch.object(
            parser,
            "_image_file_to_data_url",
            return_value="data:image/png;base64,dummy",
        ),
        patch.object(parser, "_copy_debug_image"),
        patch("os.path.exists", return_value=True),
        patch("os.remove"),
    ):
        if should_raise:
            with pytest.raises(RuntimeError, match="book-quality profile"):
                parser._extract_document_metadata(
                    MagicMock(), [0], {}, None, "mock-model"
                )
        else:
            assert (
                parser._extract_document_metadata(
                    MagicMock(), [0], {}, None, "mock-model"
                )
                == {}
            )

    assert parser.metrics["model_stage_diagnostics"] == [
        {
            "stage": "metadata_prepass",
            "category": "empty_content",
            "error_type": "StructuredResponseError",
        }
    ]
    assert (
        parser.settings["model_stage_diagnostics"]
        == parser.metrics["model_stage_diagnostics"]
    )


def test_metadata_failure_persists_safe_provider_trace():
    with tempfile.TemporaryDirectory() as temp_dir:
        parser = _configure_metadata_failure_parser("book_quality")
        parser.store_debug_artifacts = True
        parser.debug_dir = Path(temp_dir)
        parser.debug_calls = PdfParser._empty_debug_calls()
        parser.used_models = {}

        def fail_with_provider_trace(*_args, **kwargs):
            kwargs["debug_capture_sink"].update(
                {
                    "schema_version": 2,
                    "provider_calls": [
                        {
                            "litellm_model": "openai/mimo-v2.5",
                            "parameters": {"reasoning_effort": "high"},
                            "outcome": {
                                "status": "failed",
                                "error_type": "ServiceUnavailableError",
                                "status_code": 503,
                            },
                        }
                    ],
                    "credentials": "must-not-be-persisted",
                }
            )
            raise StructuredResponseError("metadata_prepass", "provider_failure")

        parser._completion_with_structured_json.side_effect = fail_with_provider_trace

        with (
            patch.object(parser, "_get_page_crop_box", return_value=None),
            patch.object(parser, "_render_page_to_png", return_value="dummy.png"),
            patch.object(
                parser,
                "_image_file_to_data_url",
                return_value="data:image/png;base64,dummy",
            ),
            patch.object(parser, "_copy_debug_image"),
            patch("os.path.exists", return_value=True),
            patch("os.remove"),
        ):
            with pytest.raises(RuntimeError, match="book-quality profile"):
                parser._extract_document_metadata(
                    MagicMock(), [0], {}, None, "mock-model"
                )

        debug_payload = json.loads(
            (Path(temp_dir) / "debug_info.json").read_text(encoding="utf-8")
        )
        record = debug_payload["debug_calls"]["metadata_prepass"][0]
        assert record["response"]["choices"] == []
        assert record["service_trace"]["provider_calls"][0]["outcome"] == {
            "status": "failed",
            "error_type": "ServiceUnavailableError",
            "status_code": 503,
        }
        assert "credentials" not in record["service_trace"]
        assert "must-not-be-persisted" not in json.dumps(debug_payload)
        assert (
            parser._completion_with_structured_json.call_args.kwargs[
                "provider_max_attempts"
            ]
            == 3
        )


def test_structured_completion_retries_without_cache_on_vertex_cache_permission_error():
    parser = _make_parser_with_settings()
    llm_service = MagicMock()
    expected = object()
    llm_service.completion.side_effect = [
        RuntimeError("403 Permission 'aiplatform.cachedContents.create' denied"),
        expected,
    ]

    result = parser._completion_with_structured_json(
        llm_service,
        schema_name="example",
        schema={"type": "object"},
        model="vertex_ai/gemini-3.5-flash",
        messages=[
            {
                "role": "system",
                "content": [
                    {
                        "type": "text",
                        "text": "instructions",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            }
        ],
        prompt_cache_key="cache-key",
    )

    assert result is expected
    assert parser._prompt_cache_disabled_for_provider is True
    assert llm_service.completion.call_count == 2
    assert (
        llm_service.completion.call_args_list[0].kwargs["prompt_cache_key"]
        == "cache-key"
    )
    assert (
        llm_service.completion.call_args_list[0].kwargs["redact_provider_error_logs"]
        is True
    )
    assert "prompt_cache_key" not in llm_service.completion.call_args_list[1].kwargs
    assert "cache_control" not in json.dumps(
        llm_service.completion.call_args_list[1].kwargs["messages"]
    )
    assert (
        llm_service.completion.call_args_list[1].kwargs["response_format"]["type"]
        == "json_schema"
    )


def test_structured_completion_retries_without_cache_on_zero_storage_quota():
    parser = _make_parser_with_settings()
    llm_service = MagicMock()
    expected = object()
    llm_service.completion.side_effect = [
        RuntimeError(
            "TotalCachedContentStorageTokensPerModelFreeTier limit exceeded; "
            "RESOURCE_EXHAUSTED"
        ),
        expected,
    ]

    result = parser._completion_with_provider_fallback(
        llm_service,
        model="gemini/gemini-3.5-flash",
        messages=[
            {
                "role": "system",
                "content": [
                    {
                        "type": "text",
                        "text": "instructions",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            }
        ],
        prompt_cache_key="cache-key",
    )

    assert result is expected
    assert parser._prompt_cache_disabled_for_provider is True
    assert llm_service.completion.call_count == 2
    second = llm_service.completion.call_args_list[1].kwargs
    assert "prompt_cache_key" not in second
    assert "cache_control" not in json.dumps(second["messages"])


def test_ordinary_completion_uses_same_cache_permission_fallback():
    parser = _make_parser_with_settings()
    llm_service = MagicMock()
    expected = object()
    llm_service.completion.side_effect = [
        RuntimeError("403 Permission 'aiplatform.cachedContents.create' denied"),
        expected,
    ]

    result = parser._completion_with_provider_fallback(
        llm_service,
        model="vertex_ai/gemini-3.7-flash",
        messages=[
            {
                "role": "system",
                "content": [
                    {
                        "type": "text",
                        "text": "instructions",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            }
        ],
        prompt_cache_key="cache-key",
    )

    assert result is expected
    assert parser._prompt_cache_disabled_for_provider is True
    assert llm_service.completion.call_count == 2
    second = llm_service.completion.call_args_list[1].kwargs
    assert "prompt_cache_key" not in second
    assert "cache_control" not in json.dumps(second["messages"])
    assert second["redact_provider_error_logs"] is True


def test_build_page_prompt_payload_does_not_use_non_adjacent_context_from_skipped_pages():
    parser = _make_parser_with_settings()
    parser._cache_enabled = lambda: False
    parser._get_dynamic_user_prompt = lambda page_number, include_previous_context: (
        "base_prompt"
    )
    parser._format_document_metadata_context = lambda page_number=None: (
        "metadata_context"
    )
    parser.current_headings = {}
    parser.current_author = None

    # Add page 1 (index 0) output
    parser._previous_outputs = [{"page_index": 0, "content": "This is page 1 content."}]

    # We are parsing page 3 (index 2), page 2 (index 1) was skipped/deleted
    payload = parser._build_page_prompt_payload(
        page_number=3, include_previous_context=True
    )

    # Page 2 was skipped/deleted, so page 1 must not be presented as if it
    # directly preceded page 3.  The no-context continuation prompt is used.
    assert "This is page 1 content." not in payload["context"]
    assert "This is page 1 content." not in payload["prompt"]
    assert payload["include_previous_context"] is False


def test_pdf_parser_metadata_model_defaults_to_default_model():
    from unittest.mock import MagicMock, patch

    parser = _make_parser_with_settings(
        {
            "parsing_model": "local/custom-extraction-model",
            # "metadata_model" is not set
        }
    )

    import threading

    parser.llm_service = MagicMock()
    parser.metrics_lock = threading.Lock()
    parser._previous_outputs = []
    parser.metrics = {"failed_pages": []}
    parser.current_headings = {"h1": None, "h2": None, "h3": None}
    parser.current_author = None

    with (
        patch.object(
            parser, "_normalize_pdf_options", return_value={"append_previous": False}
        ) as mock_opts,
        patch.object(
            parser, "_get_excluded_pages", return_value=set()
        ) as mock_excluded,
        patch.object(parser, "_extract_document_metadata") as mock_extract,
        patch.object(
            parser, "_render_page_to_png", return_value="dummy_path.png"
        ) as mock_render,
        patch.object(
            parser, "_build_prompt_with_context", return_value="dummy_prompt"
        ) as mock_prompt_context,
        patch.object(
            parser, "_process_rendered_page", return_value="<p>test</p>"
        ) as mock_proc,
        patch.object(parser, "_build_boundary_flags", return_value={}) as mock_boundary,
        patch.object(
            parser, "_fallback_page_html", return_value="<p>test</p>"
        ) as mock_fallback,
        patch(
            "iarf_assistant.core.model_router.ModelRouter.get_model_for_tier",
            return_value="gemini/gemini-2.5-pro-default",
        ) as mock_router,
    ):
        fake_doc = MagicMock()
        fake_doc.__len__.return_value = 1
        fake_page = MagicMock()
        fake_page.rect.width = 1000
        fake_page.rect.height = 1000
        fake_doc.__getitem__.return_value = fake_page

        with (
            patch.object(pdf_parser_module.fitz, "open", return_value=fake_doc),
            patch("flask.has_app_context", return_value=True),
        ):
            parser.absolute_path = MagicMock()
            parser.absolute_path.exists.return_value = True
            parser.absolute_path.__str__.return_value = "dummy.pdf"
            parser.state = None

            parser.process(auto_mode=True)

            mock_extract.assert_called_once()
            args, kwargs = mock_extract.call_args
            assert args[4] == "gemini/gemini-2.5-pro-default"


def test_build_elements_list_str_excludes_toc_by_default_independent_of_prepass():
    parser = _make_parser_with_settings({"pdf_options": {"metadata_prepass": True}})
    elements_str = parser._build_elements_list_str(
        ignore_footnotes=True, extract_images=False, removehf=True
    )
    assert "table-of-contents" not in elements_str


def test_parser_resume_normalizes_legacy_preserved_toc_to_omit():
    parser = _make_parser_with_settings({"pdf_options": {"toc_handling": "preserve"}})

    options = parser._normalize_pdf_options()

    assert options["toc_handling"] == "omit"
    assert parser.settings["pdf_options"]["toc_handling"] == "omit"


def test_pdf_prompt_omits_printed_toc_and_preserves_mixed_page_content():
    assert "Do not copy a printed table of contents" in DEFAULT_PDF_USER_PROMPT_BASE
    assert "omit ONLY the table-of-contents region" in DEFAULT_PDF_USER_PROMPT_BASE
    assert (
        "preserve every title, contributor, publication detail"
        in DEFAULT_PDF_USER_PROMPT_BASE
    )


def test_get_dynamic_user_prompt_omits_only_toc_region_on_mixed_pages():
    parser = _make_parser_with_settings(
        {
            "pdf_options": {
                "metadata_prepass": True,
                "toc_handling": "omit",
            }
        }
    )
    parser._system_prompt_cache = "system_prompt"
    parser._user_prompt_base_cache = (
        "### Table of Contents\n"
        'Preserve a visible source table of contents as an explicit semantic block with class "source-toc"...\n'
        "Do not confuse section titles with the table of contents!\n"
        "{elements_list_str} {continuation_instruction} {figures_instruction} {headers_footers_instruction} {footnotes_instruction} {toc_guidance_instruction} {final_continuation_instruction} {examples_instruction}\n"
        "### Section Titles\n"
        "Section titles denote chapters..."
    )
    parser._get_continuation_instruction = lambda include_previous_context: (
        "continuation"
    )
    parser._get_toc_guidance_instruction = lambda: "toc_guidance"
    parser._get_headers_footers_instruction = lambda removehf: "headers_footers"
    parser._get_footnotes_instruction = lambda ignore_footnotes, simple_footnotes: (
        "footnotes"
    )
    parser._get_examples_instruction = (
        lambda extract_images, ignore_footnotes, simple_footnotes: "examples"
    )
    parser._get_final_continuation_instruction = lambda include_previous_context: (
        "final_continuation"
    )
    parser._figures_instruction = "figures"

    prompt = parser._get_dynamic_user_prompt(
        page_number=1, include_previous_context=True
    )

    assert "omit ONLY the table-of-contents region" in prompt
    assert "Preserve all non-TOC material on mixed pages" in prompt
    assert 'class "source-toc"' not in prompt


def test_get_dynamic_user_prompt_ignores_legacy_preserve_setting():
    parser = _make_parser_with_settings(
        {
            "pdf_options": {
                "metadata_prepass": False,
                "toc_handling": "preserve",
            }
        }
    )
    parser._user_prompt_base_cache = (
        "### Table of Contents\n"
        'Preserve a visible source table of contents as an explicit semantic block with class "source-toc".\n'
        "{elements_list_str} {continuation_instruction} {figures_instruction} {headers_footers_instruction} "
        "{footnotes_instruction} {toc_guidance_instruction} {final_continuation_instruction} {examples_instruction}\n"
        "### Section Titles\nSection titles denote chapters..."
    )
    parser._figures_instruction = "figures"
    parser._get_continuation_instruction = lambda include_previous_context: (
        "continuation"
    )
    parser._get_toc_guidance_instruction = lambda: "toc_guidance"
    parser._get_headers_footers_instruction = lambda removehf: "headers_footers"
    parser._get_footnotes_instruction = lambda ignore_footnotes, simple_footnotes: (
        "footnotes"
    )
    parser._get_examples_instruction = (
        lambda extract_images, ignore_footnotes, simple_footnotes: "examples"
    )
    parser._get_final_continuation_instruction = lambda include_previous_context: (
        "final_continuation"
    )

    prompt = parser._get_dynamic_user_prompt(
        page_number=1, include_previous_context=False
    )

    assert "omit ONLY the table-of-contents region" in prompt
    assert 'class "source-toc"' not in prompt
    assert "table-of-contents" not in parser._build_elements_list_str(
        ignore_footnotes=True,
        extract_images=False,
        removehf=True,
    )


def test_legacy_passive_batch_replay_keeps_schema_and_instruction_bytes():
    page_html = "<main_content><p>Faithful text.</p></main_content><footnotes></footnotes>"
    content = json.dumps(
        {"pages": [{"page_number": 1, "html": page_html}]}
    )
    parser, output = _batch_call_parser(
        {"mcp_page_batch_protocol": "carlina.pdf-page-batch.v1"}, content
    )

    call_kwargs = parser._completion_with_structured_json.call_args.kwargs
    assert "<p>Faithful text.</p>" in output[0]
    assert call_kwargs["schema_name"] == "document_page_batch"
    assert "blank" not in call_kwargs["schema"]["properties"]["pages"]["items"]["properties"]
    assert call_kwargs["messages"][0]["content"] == _expected_legacy_page_batch_instruction()


def test_new_passive_batch_uses_required_blank_flag_and_accepts_noisy_ocr_empty_page():
    empty_html = (
        "<main_content><!-- page has no visible content --></main_content>"
        "<footnotes> \n </footnotes>"
    )
    content = json.dumps(
        {"pages": [{"page_number": 1, "html": empty_html, "blank": True}]}
    )
    settings = {
        "mcp_page_batch_protocol": "carlina.pdf-page-batch.v1",
        "mcp_page_blank_policy": "agent-declared-v1",
    }
    parser, output = _batch_call_parser(
        settings, content, source_text="noisy-ocr-layer " * 80
    )

    call_kwargs = parser._completion_with_structured_json.call_args.kwargs
    item_schema = call_kwargs["schema"]["properties"]["pages"]["items"]
    expected_instruction = _expected_legacy_page_batch_instruction().replace(
        "Each result has only page_number (integer) and "
        "html (string containing both <main_content> and <footnotes> roots). Apply",
        "Each result has only page_number (integer), "
        "html (string containing both <main_content> and <footnotes> roots), "
        "and blank (boolean). Apply",
    ) + (
        "\n\nEvery page must include the boolean blank field. Set blank=false for pages "
        "with visible content and for intentionally omitted TOC content. Set blank=true "
        "only after inspecting the supplied image and finding no substantive visible "
        "content. Return empty main_content and footnotes roots for that page. This is "
        "your explicit visual judgment, not an automatic image classification. Use the "
        "source-TOC omission marker for intentionally omitted TOC content instead."
    )
    assert output[0].startswith("<main_content>")
    assert call_kwargs["schema_name"] == PASSIVE_PAGE_BATCH_V2_SCHEMA
    assert item_schema["required"] == ["page_number", "html", "blank"]
    assert call_kwargs["messages"][0]["content"] == expected_instruction


def test_new_passive_issue_batch_uses_v3_schema_and_keeps_issues_out_of_html():
    v3_instruction = pdf_parser_module._page_batch_instruction({}, allow_blank=True, allow_issues=True)
    assert "blank (boolean), and issues (array). Apply" in v3_instruction
    assert "and blank (boolean). Apply" not in v3_instruction
    page_html = "<main_content><p>Łódź</p></main_content><footnotes></footnotes>"
    issue = {
        "key": "name-1",
        "kind": "name_check",
        "excerpt": "Łódź",
        "reason": "The final character is faint in the scan.",
        "suggested_readings": ["Łódź"],
        "references": [],
    }
    content = json.dumps(
        {
            "pages": [
                {
                    "page_number": 1,
                    "html": page_html,
                    "blank": False,
                    "issues": [issue],
                }
            ]
        },
        ensure_ascii=False,
    )
    settings = {
        "mcp_page_batch_protocol": "carlina.pdf-page-batch.v1",
        "mcp_page_blank_policy": "agent-declared-v1",
        "mcp_extraction_issues_policy": "issues-v1",
    }
    with patch.object(
        pdf_parser_module, "_passive_document_model", return_value="carlina.passive.document"
    ):
        parser, output = _batch_call_parser(settings, content)

    call_kwargs = parser._completion_with_structured_json.call_args.kwargs
    item = call_kwargs["schema"]["properties"]["pages"]["items"]
    assert "<p>Łódź</p>" in output[0]
    assert "issues" not in output[0]
    assert parser.extraction_issues_by_page == {1: [issue]}
    assert call_kwargs["schema_name"] == "document_page_batch_v3"
    assert item["required"] == ["page_number", "html", "blank", "issues"]
    assert "issues-v1" in call_kwargs["messages"][0]["content"]


def test_concurrent_issue_batch_callbacks_do_not_overwrite_other_pages():
    parser = object.__new__(PdfParser)
    parser.metrics_lock = threading.Lock()
    parser.extraction_issues_by_page = {}
    barrier = threading.Barrier(3)
    failure = []

    def record(page_number):
        try:
            barrier.wait(timeout=5)
            parser._replace_extraction_issue_pages(
                {page_number: SimpleNamespace(issues=[])}
            )
        except BaseException as exc:  # noqa: BLE001 - collect thread failures for the assertion.
            failure.append(exc)

    threads = [threading.Thread(target=record, args=(page,)) for page in (2, 5)]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=5)

    assert not failure
    assert all(not thread.is_alive() for thread in threads)
    assert parser.extraction_issues_by_page == {2: [], 5: []}


def test_replayed_issue_page_replaces_prior_reports_without_duplicates():
    parser = object.__new__(PdfParser)
    parser.metrics_lock = threading.Lock()
    parser.extraction_issues_by_page = {}
    first = {"key": "name-1", "excerpt": "First reading"}
    replacement = {"key": "name-1", "excerpt": "Second reading"}

    parser._replace_extraction_issue_pages(
        {4: SimpleNamespace(issues=[first])}
    )
    parser._replace_extraction_issue_pages(
        {4: SimpleNamespace(issues=[replacement])}
    )

    assert parser.extraction_issues_by_page == {4: [replacement]}


def test_new_nonblank_page_still_runs_source_grounding():
    parser = _make_parser_with_settings(
        {
            "mcp_page_batch_protocol": "carlina.pdf-page-batch.v1",
            "mcp_page_blank_policy": "agent-declared-v1",
        }
    )
    parser.metrics = {}
    parser.metrics_lock = threading.Lock()
    source = " ".join(f"sourceword{index}" for index in range(80))
    unrelated = (
        "<main_content><p>completely unrelated wording</p></main_content>"
        "<footnotes></footnotes>"
    )

    with pytest.raises(ValueError) as error:
        parser._validate_page_model_content(
            unrelated,
            "stop",
            1,
            source,
            allow_blank=True,
            declared_blank=False,
        )

    assert error.value.stage == "source_grounding"
