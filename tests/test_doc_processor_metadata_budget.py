"""Budget selection and optional-stage failures use real admission accounting."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from iarf_assistant.addons.doc_processor.heading_structure import (
    format_heading_structure_guidance,
    normalize_heading_structure,
)
from iarf_assistant.addons.doc_processor.parsers.pdf_parser import (
    MetadataPreparationError,
    PdfParser,
)
from iarf_assistant.core.executor_profile import normalize_executor_profile
from iarf_assistant.core.model_capabilities import admit_provider_payload
from iarf_assistant.core.task_queue import ExternalWorkPending, JobCancelledError


def _response(value):
    return {
        "choices": [
            {"message": {"content": json.dumps(value)}, "finish_reason": "stop"}
        ]
    }


def _heading(requests=()):
    return {
        "roles": [
            {
                "role": "chapter",
                "depth": 1,
                "confidence": 0.9,
                "evidence": [
                    {"page": 1, "kind": "body_opener", "description": "Heading"}
                ],
            }
        ],
        "evidence_requests": [{"pages": list(requests)}] if requests else [],
    }


@pytest.fixture
def prepared_parser(monkeypatch):
    parser = object.__new__(PdfParser)
    parser.settings = {
        "mcp_metadata_policy": "budgeted-v1",
        "pdf_options": {
            "metadata_pages": 10,
            "metadata_prepass": True,
            "prompt_cache": False,
        },
    }
    parser.document_metadata = {}
    parser.metrics = {}
    parser.absolute_path = SimpleNamespace(name="synthetic.pdf")
    parser._metadata_prompt = "Read the supplied metadata pages."
    for name in (
        "_record_model_use",
        "_record_response_metrics",
        "_record_debug_call",
        "_copy_debug_image",
        "_write_debug_artifacts",
        "_report_progress",
    ):
        monkeypatch.setattr(parser, name, MagicMock())
    monkeypatch.setattr(parser, "_uses_passive_page_batches", lambda: True)
    monkeypatch.setattr(parser, "_get_page_crop_box", lambda *args: None)
    monkeypatch.setattr(
        parser, "_render_page_to_png", lambda *args: "absent-test-image.png"
    )
    monkeypatch.setattr(
        parser,
        "_image_file_to_data_url",
        lambda *args: "data:image/png;base64,c3ludGhldGlj",
    )
    monkeypatch.setattr(
        parser,
        "_build_cached_system_message",
        lambda *args, **kwargs: {
            "role": "system",
            "content": "Extraction contract " * 100,
        },
    )
    monkeypatch.setattr(
        parser, "_assess_document_text_layer", lambda *args: {"mode": "image_only"}
    )
    monkeypatch.setattr(parser, "_render_heading_contact_sheets", lambda *args: [])
    monkeypatch.setattr(
        parser,
        "_normalize_document_metadata",
        lambda value, **kwargs: {
            "title": value["title"],
            "source_pages": kwargs["source_pages"],
        },
    )
    return parser


def _account(parser, input_tokens):
    profile = normalize_executor_profile(
        {
            "schema_version": "carlina.external-executor.v1",
            "max_input_tokens": input_tokens,
            "max_output_tokens": 16000,
            "input_modalities": ["text", "image"],
            "supports_tools": True,
            "supports_structured_output": True,
            "supports_reasoning": True,
            "user_confirmed": True,
        }
    ).capability_profile("document.metadata")
    previews = []

    def preview(**kwargs):
        result = admit_provider_payload(
            profile=profile,
            messages=kwargs["messages"],
            response_format=kwargs["response_format"],
        )
        previews.append(result)
        return result

    parser.llm_service = SimpleNamespace(preview_passive_admission=preview)
    return previews


def _metadata(parser):
    return parser._extract_document_metadata(
        [None] * 390,
        list(range(390)),
        {},
        None,
        "document.metadata",
    )


@pytest.mark.parametrize("limit", [128000, 256000])
def test_toc_expansion_fits_full_packet_without_dropping_initial_window(
    prepared_parser, monkeypatch, limit
):
    parser = prepared_parser
    previews = _account(parser, limit)
    windows = []

    def complete(service, **kwargs):
        assert previews[-1].admitted
        windows.append(
            sum(
                part["type"] == "image_url" for part in kwargs["messages"][1]["content"]
            )
        )
        return _response(
            {
                "title": "Retained title",
                "table_of_contents_status": "partial",
                "toc_page_request": {"from_start": 20, "from_end": 0},
            }
        )

    monkeypatch.setattr(parser, "_completion_with_structured_json", complete)
    monkeypatch.setattr(
        parser, "_extract_heading_structure", lambda doc, metadata, *args: metadata
    )
    result = _metadata(parser)

    assert windows[0] == 10
    assert len(windows) == 2
    assert 10 < windows[1] <= 30
    assert result["title"] == "Retained title"
    assert result["source_pages"][:10] == list(range(1, 11))
    assert parser.settings["mcp_metadata_complete"] is True
    assert result["heading_structure"]["validation"]["status"] == "provisional"
    if limit == 128000:
        assert any(not item.admitted for item in previews)
    else:
        assert windows[1] == 30


@pytest.mark.parametrize("limit", [131072, 256000])
def test_heading_followups_are_admitted_after_budgeted_evidence_selection(
    prepared_parser, monkeypatch, limit
):
    parser = prepared_parser
    previews = _account(parser, limit)
    monkeypatch.setattr(
        parser,
        "_select_heading_structure_evidence_pages",
        lambda *args: [0, 100, 200, 389],
    )
    monkeypatch.setattr(
        parser, "_assess_document_text_layer", lambda *args: {"mode": "native_text"}
    )
    monkeypatch.setattr(
        parser,
        "_build_native_heading_candidate_catalog",
        lambda *args: [{"text": "locator " * 5000}],
    )
    contact = [
        {"type": "text", "text": "Contact sheet"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,eA==", "detail": "high"},
        },
    ]
    monkeypatch.setattr(
        parser, "_render_heading_contact_sheets", lambda *args: contact * 8
    )
    answers = iter([_heading(range(10, 16)), _heading(range(20, 26)), _heading()])
    sent = []

    def complete(service, **kwargs):
        assert previews[-1].admitted
        sent.append(previews[-1])
        return _response(next(answers))

    monkeypatch.setattr(parser, "_completion_with_structured_json", complete)
    result = parser._extract_heading_structure(
        [None] * 390,
        {"title": "Book", "source_pages": [1]},
        list(range(390)),
        [0],
        {},
        [],
        {},
        None,
        "document.metadata",
    )
    assert len(sent) == 3
    assert all(item.measurement.total_input_tokens <= limit for item in sent)
    if limit == 131072:
        assert any(not item.admitted for item in previews)
        assert result["heading_structure"]["refinement_status"] == "partial"
    assert result["title"] == "Book"
    assert result["heading_structure"]["roles"][0]["role"] == "chapter"


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("private detail"),
        ValueError("private detail"),
        PermissionError("private detail"),
        JobCancelledError("cancelled"),
    ],
)
def test_only_recognized_optional_failures_degrade(
    prepared_parser, monkeypatch, failure
):
    parser = prepared_parser
    _account(parser, 256000)
    answers = iter([_response({"title": "Accepted title"}), failure])

    def complete(*args, **kwargs):
        value = next(answers)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(parser, "_completion_with_structured_json", complete)
    if isinstance(failure, TimeoutError):
        result = _metadata(parser)
        assert result["title"] == "Accepted title"
        assert result["heading_structure"]["validation"]["status"] == "provisional"
        assert "private detail" not in json.dumps(parser.metrics)
    elif isinstance(failure, JobCancelledError):
        with pytest.raises(JobCancelledError):
            _metadata(parser)
    else:
        with pytest.raises(MetadataPreparationError) as caught:
            _metadata(parser)
        assert "private detail" not in str(caught.value)
        assert "private detail" not in json.dumps(caught.value.preparation_diagnostics)
        assert not parser.settings.get("mcp_metadata_complete")
        stages = {item["stage"]: item for item in caught.value.preparation_diagnostics}
        assert stages["metadata_initial"]["status"] == "admitted"
        assert stages["heading_initial"]["status"] == "failed"


def test_failed_heading_followup_preserves_last_validated_structure(
    prepared_parser, monkeypatch
):
    parser = prepared_parser
    _account(parser, 256000)
    monkeypatch.setattr(
        parser,
        "_completion_with_structured_json",
        MagicMock(
            side_effect=[
                _response(_heading([25])),
                _response({}),
            ]
        ),
    )
    result = parser._extract_heading_structure(
        [None] * 390,
        {"title": "Book", "source_pages": [1]},
        list(range(390)),
        [0],
        {},
        [],
        {},
        None,
        "document.metadata",
    )
    structure = result["heading_structure"]
    assert structure["roles"][0]["role"] == "chapter"
    assert structure["refinement_status"] == "partial"
    assert 25 not in result["source_pages"]
    assert (
        normalize_heading_structure(structure)["validation"]["status"] == "provisional"
    )
    assert "provisional" in format_heading_structure_guidance(structure)


def test_initial_overflow_is_fatal_and_never_calls_completion(
    prepared_parser, monkeypatch
):
    parser = prepared_parser
    parser.settings["pdf_options"]["metadata_pages"] = 20
    _account(parser, 128000)
    complete = MagicMock()
    monkeypatch.setattr(parser, "_completion_with_structured_json", complete)
    with pytest.raises(MetadataPreparationError) as caught:
        _metadata(parser)
    complete.assert_not_called()
    detail = caught.value.preparation_diagnostics[-1]
    assert detail["stage"] == "metadata_initial"
    assert detail["requested_input_tokens"] > detail["admitted_input_tokens"]


def test_suspended_heading_preparation_does_not_treat_metadata_as_complete(
    prepared_parser, monkeypatch
):
    parser = prepared_parser
    _account(parser, 256000)
    monkeypatch.setattr(
        parser,
        "_completion_with_structured_json",
        MagicMock(
            side_effect=[
                _response({"title": "Accepted title"}),
                ExternalWorkPending(
                    "pending", work_item_id="test-work", delay_seconds=5
                ),
            ]
        ),
    )
    with pytest.raises(ExternalWorkPending):
        _metadata(parser)
    assert parser.document_metadata["title"] == "Accepted title"
    assert not parser.settings.get("mcp_metadata_complete")
    resumed = MagicMock(
        side_effect=[_response({"title": "Accepted title"}), _response(_heading())]
    )
    monkeypatch.setattr(parser, "_completion_with_structured_json", resumed)
    result = _metadata(parser)
    assert resumed.call_count == 2
    assert result["heading_structure"]["roles"]
    assert parser.settings["mcp_metadata_complete"] is True
