from __future__ import annotations

import json
import random

import pytest

from iarf_assistant.addons.doc_processor import review_history as history


@pytest.mark.parametrize(
    "before,after",
    [
        ("", "new"),
        ("old", ""),
        ("same", "same"),
        ("<p>alpha</p><p>beta</p>", "<p>beta</p><p>alpha</p>"),
        ("<h3>Title</h3><p>Body</p>", "<h2>Title</h2><p>Body!</p>"),
        ("abc" * 1000, "abd" * 1000),
        (
            "<p>\u00df \u05e9\u05dc\u05d5\u05dd</p>",
            "<p>SS \u05e9\u05dc\u05d5\u05dd!</p>",
        ),
    ],
)
def test_inverse_splices_restore_exact_bytes(before, after):
    assert (
        history.restore_splices(after, history.inverse_splices(before, after)) == before
    )


def test_deterministic_random_splices_restore_exact_snapshot():
    randomizer = random.Random(91013)
    for _ in range(200):
        before = "".join(
            randomizer.choices(["a", "b", "<p>", "</p>", "\n", "\u05e9"], k=150)
        )
        after = before
        for _ in range(5):
            start = randomizer.randrange(len(after) + 1)
            end = randomizer.randrange(start, len(after) + 1)
            after = after[:start] + "<p>replacement</p>" + after[end:]
        delta = history.inverse_splices(before, after)
        assert history.restore_splices(after, delta) == before


def test_distant_edits_in_large_book_do_not_store_the_book():
    before = "".join(
        f"<p>Unique synthetic paragraph {number:06d}.</p>" for number in range(50000)
    )
    after = before.replace("000010", "change-a").replace("049990", "change-b")
    delta = history.inverse_splices(before, after)
    assert len(json.dumps(delta)) < 2000
    assert history.restore_splices(after, delta) == before


def test_delta_and_preview_budgets_are_explicit():
    with pytest.raises(ValueError, match="split"):
        history.inverse_splices("a" * (history.MAX_DELTA_BYTES + 1), "b")
    delta = history.inverse_splices("a" * 20000, "b" * 20000)
    preview = history.preview_splices("b" * 20000, delta)
    assert preview["fragments"][0]["truncated"]
    assert (
        sum(len(item["before"]) + len(item["after"]) for item in preview["fragments"])
        <= history.MAX_PREVIEW_CHARS
    )
    with pytest.raises(ValueError, match="match"):
        history.restore_splices("tampered", delta)


def test_invalid_overlapping_delta_fails_closed():
    delta = history.inverse_splices("old", "new")
    with pytest.raises(ValueError):
        history.restore_splices("new", [*delta, *delta])
