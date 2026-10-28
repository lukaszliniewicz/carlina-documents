import json
from copy import deepcopy

from iarf_assistant.addons.doc_processor.api_client import _bounded_metadata
from iarf_assistant.addons.doc_processor.metadata_service import (
    normalize_document_metadata_payload,
)
from iarf_assistant.addons.doc_processor.note_arrangement import (
    NOTE_ARRANGEMENT_SCHEMA,
    metadata_schema_with_note_arrangement,
    normalize_note_arrangement,
    note_arrangement_enabled,
)
from iarf_assistant.addons.doc_processor.parsers.pdf_parser import (
    METADATA_JSON_SCHEMA,
    PdfParser,
)


def test_note_arrangement_normalization_is_closed_and_bounded():
    normalized = normalize_note_arrangement(
        {
            "placement": "margin-notes",
            "numbering": "per-footnote",
            "scope_heading_level": True,
            "source_pages": [4, 0, -1, 2, True, 3.0, "5", *range(10, 50), 4],
            "evidence": f" \n{'é' * 900}\t ",
            "private_setting": "must be dropped",
        }
    )

    assert normalized == {
        "placement": "unknown",
        "numbering": "unknown",
        "scope_heading_level": None,
        "source_pages": [2, 4, *range(10, 40)],
        "evidence": "é" * 800,
    }
    assert normalize_note_arrangement(None) == {}
    assert normalize_note_arrangement([]) == {}
    assert normalize_note_arrangement({}) == {}
    assert normalize_note_arrangement({"private_setting": "ignored"}) == {
        "placement": "unknown",
        "numbering": "unknown",
        "scope_heading_level": None,
        "source_pages": [],
        "evidence": "",
    }
    assert (
        normalize_note_arrangement({"scope_heading_level": 6})["scope_heading_level"]
        == 6
    )
    assert (
        normalize_note_arrangement({"scope_heading_level": 7})["scope_heading_level"]
        is None
    )
    assert normalize_note_arrangement({"evidence": 42})["evidence"] == ""


def test_document_metadata_keeps_only_present_normalized_arrangement():
    assert "note_arrangement" not in normalize_document_metadata_payload(
        {"title": "Source title"}
    )
    assert normalize_document_metadata_payload({"note_arrangement": {}}) == {
        "note_arrangement": {}
    }
    assert normalize_document_metadata_payload(
        {"note_arrangement": {"placement": "chapter_endnotes", "extra": "x"}}
    )["note_arrangement"] == {
        "placement": "chapter_endnotes",
        "numbering": "unknown",
        "scope_heading_level": None,
        "source_pages": [],
        "evidence": "",
    }
    assert "note_arrangement" not in normalize_document_metadata_payload(
        {"note_arrangement": "not an object"}
    )


def test_note_arrangement_schema_is_opt_in_for_passive_and_default_for_managed():
    original_schema = deepcopy(METADATA_JSON_SCHEMA)

    assert note_arrangement_enabled() is True
    assert note_arrangement_enabled({}) is True
    assert note_arrangement_enabled({"mcp_execution_mode": "managed"}) is True
    assert note_arrangement_enabled({"mcp_execution_mode": "passive"}) is False
    assert (
        note_arrangement_enabled(
            {
                "mcp_execution_mode": "passive",
                "mcp_note_arrangement_policy": "source-arrangement-v1",
            }
        )
        is True
    )

    legacy = metadata_schema_with_note_arrangement(
        METADATA_JSON_SCHEMA, {"mcp_execution_mode": "passive"}
    )
    assert legacy is METADATA_JSON_SCHEMA
    assert legacy == original_schema

    selected = metadata_schema_with_note_arrangement(METADATA_JSON_SCHEMA)
    assert selected is not METADATA_JSON_SCHEMA
    assert selected["properties"]["note_arrangement"] == NOTE_ARRANGEMENT_SCHEMA
    assert selected["required"][-1] == "note_arrangement"
    assert NOTE_ARRANGEMENT_SCHEMA["additionalProperties"] is False
    assert NOTE_ARRANGEMENT_SCHEMA["required"] == [
        "placement",
        "numbering",
        "scope_heading_level",
        "source_pages",
        "evidence",
    ]
    properties = NOTE_ARRANGEMENT_SCHEMA["properties"]
    assert properties["placement"]["enum"] == [
        "page_footnotes",
        "chapter_endnotes",
        "document_endnotes",
        "mixed",
        "none",
        "unknown",
    ]
    assert properties["numbering"]["enum"] == [
        "document",
        "chapter",
        "section",
        "page",
        "mixed",
        "unknown",
    ]
    assert properties["scope_heading_level"] == {
        "type": ["integer", "null"],
        "minimum": 1,
        "maximum": 6,
    }
    assert properties["source_pages"] == {
        "type": "array",
        "items": {"type": "integer", "minimum": 1},
        "maxItems": 32,
    }
    assert properties["evidence"] == {"type": "string", "maxLength": 800}
    assert original_schema == METADATA_JSON_SCHEMA


def test_pdf_metadata_arrangement_pages_are_limited_to_supplied_evidence():
    parser = object.__new__(PdfParser)
    assert (
        parser._normalize_document_metadata({"note_arrangement": {}}, source_pages=[1])[
            "note_arrangement"
        ]
        == {}
    )

    normalized = parser._normalize_document_metadata(
        {
            "note_arrangement": {
                "placement": "page_footnotes",
                "numbering": "page",
                "scope_heading_level": 2,
                "source_pages": [1, 3, 4, 9],
                "evidence": "Observed page-based notes.",
            }
        },
        source_pages=[1, 4],
    )

    assert normalized["note_arrangement"]["source_pages"] == [1, 4]


def test_source_dto_allows_only_bounded_note_arrangement_metadata():
    metadata = _bounded_metadata(
        {
            "title": "😀" * 600,
            "subtitle": "🟩" * 300,
            "publication_type": "publication " * 20,
            "publisher": "🙂" * 300,
            "publication_date": "2026 " * 20,
            "journal_or_series": "©" * 300,
            "volume": "volume " * 10,
            "issue": "issue " * 10,
            "isbn": "9" * 100,
            "issn": "8" * 100,
            "contributors": {
                "authors": [f"😀Author {index}" for index in range(20)],
                "editors": [f"🟩Editor {index}" for index in range(20)],
                "translators": [f"🙂Translator {index}" for index in range(20)],
                "other": [
                    {"role": "illustrator", "name": f"©Artist {index}"}
                    for index in range(20)
                ],
            },
            "identifiers": [
                {
                    "scheme": "S" * 40,
                    "label": "👩" * 70,
                    "value": f"{index}" + "1" * 200,
                }
                for index in range(20)
            ],
            "note_arrangement": {
                "placement": "document_endnotes",
                "numbering": "document",
                "scope_heading_level": 2,
                "source_pages": [*range(1, 80)],
                "evidence": "é" * 5000,
            },
            "private_setting": "must not cross the DTO boundary",
        }
    )

    assert metadata["note_arrangement"]["source_pages"] == list(range(1, 33))
    assert metadata["note_arrangement"]["evidence"] == "é" * 800
    assert "private_setting" not in metadata
    assert len(json.dumps(metadata, ensure_ascii=False).encode("utf-8")) <= 8 * 1024

    for pages in (
        [10**700 + page for page in range(32)],
        [10**5000],
    ):
        bounded = _bounded_metadata({"note_arrangement": {"source_pages": pages}})
        assert len(json.dumps(bounded, ensure_ascii=False).encode("utf-8")) <= 8 * 1024
