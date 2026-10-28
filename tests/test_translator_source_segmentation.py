from __future__ import annotations

import pytest

# Keep the old package-name references consolidated for the exact rename ratchet.
# isort: off
from iarf_assistant.addons.translator import (
    block_contract as translator_block_contract,
    source_segmentation as translator_source_segmentation,
)
# isort: on

BlockContractError = translator_block_contract.BlockContractError
manifest_spans = translator_source_segmentation.manifest_spans
segment_projection = translator_source_segmentation.segment_projection


def test_segment_projection_assigns_whitespace_to_preceding_span() -> None:
    projection = "  First.\t  Second.  "
    ends = segment_projection(projection, lambda _text: [" First. ", " Second. "])
    assert ends == (11, len(projection))
    assert ((0, 11), (11, len(projection))) == manifest_spans(projection, ends)
    assert projection[: ends[0]] == "  First.\t  "


def test_segment_projection_accepts_exact_rtl_and_repeated_text() -> None:
    projection = "אבג.  אבג."
    ends = segment_projection(projection, lambda _text: ["אבג.", "אבג."])
    assert ends == (6, len(projection))
    assert manifest_spans(projection, ends) == ((0, 6), (6, len(projection)))


def test_whitespace_only_projection_is_one_span_without_callback() -> None:
    called = False

    def unexpected(_text: str) -> list[str]:
        nonlocal called
        called = True
        return []

    assert segment_projection("\u00a0\n", unexpected) == (2,)
    assert called is False
    assert manifest_spans("\u00a0\n", (2,)) == ((0, 2),)


@pytest.mark.parametrize(
    "result",
    (
        "not-a-sequence",
        ("",),
        ("Changed.",),
        ("First.", "Missing."),
        ("First.", "First."),
        tuple(f"First {index}." for index in range(4_097)),
    ),
)
def test_invalid_segmenter_results_fail_closed(result: object) -> None:
    projection = "First. Second."
    with pytest.raises(BlockContractError, match=r"^source-sentence-segmentation-"):
        segment_projection(projection, lambda _text, result=result: result)  # type: ignore[arg-type]


def test_manifest_rejects_whitespace_only_intermediate_span() -> None:
    with pytest.raises(BlockContractError, match=r"^source-sentence-segmentation-manifest$"):
        manifest_spans("First.  Second.", (6, 8, len("First.  Second.")))
