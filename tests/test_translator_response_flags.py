"""Validation of the reviewer flags a translating model returns per result."""

from __future__ import annotations

import pytest

from iarf_assistant.addons.translator.block_contract import BlockContractError
from iarf_assistant.addons.translator.models import REVIEW_FLAG_KINDS
from iarf_assistant.addons.translator.response_flags import (
    MAX_RESPONSE_FLAG_BODY_CHARS,
    MAX_RESPONSE_FLAG_EXCERPT_CHARS,
    ResponseFlag,
    block_response_flags,
    response_flags_schema,
    window_response_flags,
)


def test_block_flags_are_stripped_numbered_and_keep_an_empty_excerpt() -> None:
    flags = block_response_flags(
        [
            {
                "kind": "assumption",
                "body": "  Reads 'he' as the abbot.  ",
                "excerpt": "",
            },
            {"kind": "unverified", "body": "Date of the synod.", "excerpt": " 1563 "},
        ],
        block_id="b-7",
        number=3,
    )

    assert flags == (
        ResponseFlag("b-7", 3, 0, "assumption", "Reads 'he' as the abbot.", ""),
        ResponseFlag("b-7", 3, 1, "unverified", "Date of the synod.", "1563"),
    )
    assert flags[1].idempotency_suffix == "3:1"
    assert block_response_flags([], block_id="b-7", number=3) == ()


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        [{"kind": "opinion", "body": "x", "excerpt": ""}],
        [{"kind": "revisit", "body": "   ", "excerpt": ""}],
        [{"kind": "revisit", "body": "x"}],
        [{"kind": "revisit", "body": "x", "excerpt": "", "confidence": 0.4}],
        [
            {
                "kind": "revisit",
                "body": "x" * (MAX_RESPONSE_FLAG_BODY_CHARS + 1),
                "excerpt": "",
            }
        ],
        [
            {
                "kind": "revisit",
                "body": "x",
                "excerpt": "y" * (MAX_RESPONSE_FLAG_EXCERPT_CHARS + 1),
            }
        ],
        [{"kind": "revisit", "body": "a\x00b", "excerpt": ""}],
        [{"kind": "revisit", "body": "x", "excerpt": ""}] * 3,
    ],
)
def test_malformed_block_flags_reject_the_result(value: object) -> None:
    with pytest.raises(BlockContractError, match="window-response-flags"):
        block_response_flags(value, block_id="b-1", number=1)


def test_window_flags_follow_result_order_and_treat_a_missing_array_as_none() -> None:
    results: list[object] = [
        {"n": 1, "flags": []},
        {
            "n": 2,
            "flags": [{"kind": "source_defect", "body": "Garbled.", "excerpt": ""}],
        },
    ]

    assert window_response_flags(results, ("b-1", "b-2")) == (
        ResponseFlag("b-2", 2, 0, "source_defect", "Garbled.", ""),
    )
    with pytest.raises(BlockContractError, match="window-response-cardinality"):
        window_response_flags(results, ("b-1",))
    assert window_response_flags([{"n": 1}, {"n": 2}], ("b-1", "b-2")) == ()
    with pytest.raises(BlockContractError, match="window-response-flags"):
        window_response_flags(["not a result", results[1]], ("b-1", "b-2"))


def test_schema_offers_exactly_the_review_flag_kinds() -> None:
    schema = response_flags_schema()
    items = schema["items"]
    assert isinstance(items, dict)
    assert items["required"] == ["kind", "body", "excerpt"]
    assert items["properties"]["kind"]["enum"] == list(REVIEW_FLAG_KINDS)
    assert schema["maxItems"] == 2
