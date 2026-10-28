import json

import pytest
from jsonschema import Draft202012Validator

from iarf_assistant.addons.doc_processor.page_batch_response import (
    PageBatchValidationError,
    ParsedPageBatchPage,
    batch_validation_reason,
    page_batch_response_schema,
    parse_page_batch_response,
    parse_page_batch_response_details,
)


def test_batch_validation_advice_does_not_echo_model_supplied_identifiers():
    secret = "private-identifier-do-not-repeat"
    reason = batch_validation_reason("semantic_contract", (f"duplicate id={secret!r}",))
    error = PageBatchValidationError({
        "batch_id": "pages-017-020", "attempt": 3, "page": 18,
        "stage": "semantic_contract", "reason": reason,
    })
    assert "Duplicate HTML IDs" in reason
    assert "page 18" in str(error)
    assert secret not in str(error)
    assert secret not in json.dumps(error.feedback)


def test_page_batch_validation_error_tolerates_partial_feedback():
    error = PageBatchValidationError({"reason": "validation failed"})

    assert error.feedback == {"reason": "validation failed"}
    assert "PDF batch unknown" in str(error)
    assert "page unknown" in str(error)
    assert "validation failed" in str(error)


def _response(*pages: tuple[int, str]) -> str:
    return json.dumps(
        {"pages": [{"page_number": number, "html": html} for number, html in pages]}
    )


def test_parse_page_batch_response_accepts_two_pages_and_preserves_html():
    first = " <main_content><p>First</p></main_content><footnotes></footnotes> "
    second = "<main_content><p>Second</p></main_content><footnotes>n</footnotes>"

    assert parse_page_batch_response(_response((2, first), (5, second)), [2, 5]) == {
        2: first,
        5: second,
    }


def test_parse_page_batch_response_accepts_one_page():
    html = "<main_content><p>Only page</p></main_content><footnotes></footnotes>"

    assert parse_page_batch_response(_response((7, html)), [7]) == {7: html}


@pytest.mark.parametrize(
    "page_numbers",
    [
        [],
        [0],
        [1, 1],
        [2, 1],
        [1, True],
        [1, "2"],
        None,
        "12",
    ],
)
def test_page_batch_response_rejects_malformed_expected_page_numbers(page_numbers):
    with pytest.raises(ValueError, match="page_numbers"):
        page_batch_response_schema(page_numbers)


@pytest.mark.parametrize(
    "content",
    [
        "[]",
        "{}",
        _response(),
        _response((1, "<p>one</p>")),
        _response((1, "<p>one</p>"), (3, "<p>three</p>")),
        _response((2, "<p>two</p>"), (1, "<p>one</p>")),
        _response((1, "<p>one</p>"), (1, "<p>duplicate</p>")),
        json.dumps({"pages": [{"page_number": 1, "html": "<p>one</p>", "extra": 1}]}),
        json.dumps({"pages": [{"page_number": 1}]}),
        json.dumps({"pages": [[1, "<p>one</p>"]]}),
        json.dumps({"pages": {"page_number": 1, "html": "<p>one</p>"}}),
        "{malformed",
    ],
)
def test_parse_page_batch_response_rejects_invalid_protocol_shapes(content):
    with pytest.raises(ValueError):
        parse_page_batch_response(content, [1, 2])


@pytest.mark.parametrize(
    "item",
    [
        {"page_number": True, "html": "<p>one</p>"},
        {"page_number": "1", "html": "<p>one</p>"},
        {"page_number": 1, "html": 1},
        {"page_number": 1, "html": None},
        {"page_number": 1, "html": "   \n\t"},
    ],
)
def test_parse_page_batch_response_rejects_invalid_field_types_and_blank_html(item):
    with pytest.raises(ValueError):
        parse_page_batch_response(json.dumps({"pages": [item]}), [1])


def test_parse_page_batch_response_rejects_duplicate_json_fields_without_echoing_content():
    content = '{"pages":[{"page_number":1,"html":"secret","html":"<p>x</p>"}]}'

    with pytest.raises(ValueError) as error:
        parse_page_batch_response(content, [1])

    assert "secret" not in str(error.value)


def test_page_batch_response_schema_validates_shape_and_ids_but_parser_enforces_order():
    schema = page_batch_response_schema([2, 5])
    validator = Draft202012Validator(schema)
    valid = {
        "pages": [
            {"page_number": 2, "html": "<p>two</p>"},
            {"page_number": 5, "html": "<p>five</p>"},
        ]
    }
    reordered = {
        "pages": [
            {"page_number": 5, "html": "<p>five</p>"},
            {"page_number": 2, "html": "<p>two</p>"},
        ]
    }
    unknown_field = {
        "pages": [
            {"page_number": 2, "html": "<p>two</p>", "x": 1},
            {"page_number": 5, "html": "<p>five</p>"},
        ]
    }

    validator.validate(valid)
    assert validator.is_valid(reordered)
    assert not validator.is_valid(unknown_field)
    assert not validator.is_valid({"pages": [{"page_number": 2, "html": "<p>two</p>"}]})
    with pytest.raises(ValueError, match="expected order"):
        parse_page_batch_response(json.dumps(reordered), [2, 5])


def test_legacy_page_batch_schema_remains_exact_and_rejects_blank_fields():
    schema = page_batch_response_schema([3, 4])
    validator = Draft202012Validator(schema)
    expected_item = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "page_number": {"type": "integer", "enum": [3, 4]},
            "html": {"type": "string", "minLength": 1},
        },
        "required": ["page_number", "html"],
    }
    assert schema["properties"]["pages"]["items"] == expected_item
    assert not validator.is_valid(
        {"pages": [{"page_number": 3, "html": "<p>x</p>", "blank": True}]}
    )


def test_v2_page_batch_schema_requires_boolean_blank_for_strict_outputs():
    schema = page_batch_response_schema([3, 4], allow_blank=True)
    validator = Draft202012Validator(schema)
    item_schema = schema["properties"]["pages"]["items"]

    assert item_schema["required"] == ["page_number", "html", "blank"]
    assert item_schema["properties"]["blank"]["type"] == "boolean"
    valid = {
        "pages": [
            {"page_number": 3, "html": "<p>text</p>", "blank": False},
            {
                "page_number": 4,
                "html": "<main_content></main_content><footnotes></footnotes>",
                "blank": True,
            },
        ]
    }
    validator.validate(valid)
    missing = json.loads(json.dumps(valid))
    del missing["pages"][0]["blank"]
    assert not validator.is_valid(missing)
    malformed = json.loads(json.dumps(valid))
    malformed["pages"][0]["blank"] = "yes"
    assert not validator.is_valid(malformed)


def test_legacy_page_batch_parser_never_accepts_new_blank_field():
    content = json.dumps(
        {"pages": [{"page_number": 1, "html": "<p>a</p>", "blank": False}]}
    )
    with pytest.raises(ValueError):
        parse_page_batch_response(content, [1])


@pytest.mark.parametrize("blank", ["yes", 1, None])
def test_v2_page_batch_parser_rejects_non_boolean_blank(blank):
    content = json.dumps(
        {"pages": [{"page_number": 1, "html": "<p>a</p>", "blank": blank}]}
    )
    with pytest.raises(ValueError, match="blank must be a boolean"):
        parse_page_batch_response_details(content, [1], allow_blank=True)


def test_v2_page_batch_parser_requires_and_preserves_blank_declaration():
    empty = "<main_content></main_content><footnotes></footnotes>"
    content = json.dumps(
        {
            "pages": [
                {"page_number": 1, "html": "<p>text</p>", "blank": False},
                {"page_number": 2, "html": empty, "blank": True},
            ]
        }
    )

    assert parse_page_batch_response_details(
        content, [1, 2], allow_blank=True
    ) == {
        1: ParsedPageBatchPage("<p>text</p>", blank=False),
        2: ParsedPageBatchPage(empty, blank=True),
    }


def test_v2_parser_reports_missing_blank_without_echoing_response():
    content = json.dumps(
        {"pages": [{"page_number": 1, "html": "<p>private</p>"}]}
    )
    with pytest.raises(ValueError) as error:
        parse_page_batch_response_details(content, [1], allow_blank=True)
    assert "private" not in str(error.value)
    assert error.value.code == "page-blank-required"


def _issue_report():
    return {
        "key": "name-1",
        "kind": "name_check",
        "excerpt": "Łódź",
        "reason": "The final character is faint in the scan.",
        "suggested_readings": ["Łódź"],
        "references": [],
    }


def test_v3_page_batch_schema_and_parser_require_separate_issue_arrays():
    schema = page_batch_response_schema(
        [3], allow_blank=True, allow_issues=True
    )
    validator = Draft202012Validator(schema)
    valid = {
        "pages": [
            {
                "page_number": 3,
                "html": "<main_content><p>Łódź</p></main_content><footnotes></footnotes>",
                "blank": False,
                "issues": [_issue_report()],
            }
        ]
    }

    validator.validate(valid)
    parsed = parse_page_batch_response_details(
        json.dumps(valid, ensure_ascii=False),
        [3],
        allow_blank=True,
        allow_issues=True,
    )
    assert parsed[3].issues == (_issue_report(),)
    empty_issue_response = json.dumps(
        {
            "pages": [
                {
                    "page_number": 3,
                    "html": "<main_content></main_content><footnotes></footnotes>",
                    "blank": False,
                    "issues": [],
                }
            ]
        }
    )
    assert parse_page_batch_response_details(
        empty_issue_response,
        [3],
        allow_blank=True,
        allow_issues=True,
    )[3].issues == ()


@pytest.mark.parametrize(
    "issues",
    [
        None,
        [{**_issue_report(), "extra": "secret"}],
        [{**_issue_report(), "kind": "guess"}],
        [{**_issue_report(), "key": "bad key"}],
    ],
)
def test_v3_page_batch_rejects_invalid_issue_arrays_without_echoing_content(issues):
    content = json.dumps(
        {
            "pages": [
                {
                    "page_number": 3,
                    "html": "<main_content></main_content><footnotes></footnotes>",
                    "blank": False,
                    "issues": issues,
                }
            ]
        },
        ensure_ascii=False,
    )

    with pytest.raises(ValueError) as error:
        parse_page_batch_response_details(
            content, [3], allow_blank=True, allow_issues=True
        )

    assert error.value.code == "page-issues-invalid"
    assert "secret" not in str(error.value)


def test_v3_blank_page_requires_an_empty_issue_array():
    content = json.dumps(
        {
            "pages": [
                {
                    "page_number": 3,
                    "html": "<main_content></main_content><footnotes></footnotes>",
                    "blank": True,
                    "issues": [_issue_report()],
                }
            ]
        },
        ensure_ascii=False,
    )

    with pytest.raises(ValueError, match="blank pages") as error:
        parse_page_batch_response_details(
            content, [3], allow_blank=True, allow_issues=True
        )
    assert error.value.code == "page-blank-issues"


def test_issue_retry_advice_is_gated_and_legacy_advice_remains_unchanged():
    legacy = batch_validation_reason("page_identity", ())
    v2 = batch_validation_reason("page_identity", (), allow_blank=True)
    v3 = batch_validation_reason(
        "page_identity", (), allow_blank=True, allow_issues=True
    )

    assert legacy.endswith("page_number and html only.")
    assert v2.endswith("page_number, html, and boolean blank only.")
    assert "issues array" in v3


def test_batch_validation_reason_maps_invalid_blank_declaration():
    reason = batch_validation_reason(
        "source_grounding",
        ("blank declaration requires empty main_content and footnotes roots",),
    )

    assert "empty main_content and footnotes roots" in reason
