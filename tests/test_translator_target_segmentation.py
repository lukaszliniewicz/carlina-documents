"""Pure Phase-4 target projection split-profile acceptance tests."""

from __future__ import annotations

from hashlib import sha256

import pytest

from iarf_assistant.addons.translator.target_segmentation import (
    is_atomic_target_sentence,
    split_target_projection,
    target_split_profile,
)


@pytest.mark.parametrize(
    ("language", "text", "expected_profile", "count"),
    [
        ("en", "Dr. Smith stayed.  Next.", "target-english", 2),
        ("pl", "Np. to działa. Dalej!", "target-polish", 2),
        ("zh", "第一句。第二句！", "target-cjk", 2),
        ("ar", "هذه جملة؟ وهذه أخرى.", "target-arabic", 2),
        ("xx", "A. B?", "target-unknown", 2),
    ],
)
def test_target_profiles_are_deterministic_exact_partitions(language, text, expected_profile, count):
    assert target_split_profile(language)[0] == expected_profile
    first = split_target_projection(text, language)
    second = split_target_projection(text, language)
    assert first == second and len(first) == count
    assert "".join(text[span.start:span.end] for span in first) == text
    assert [span.order for span in first] == list(range(1, len(first) + 1))
    assert [span.start for span in first] == [0, *[span.end for span in first[:-1]]]


def test_adjacent_punctuation_and_whitespace_remain_in_the_exact_projection():
    text = "One?!  Two...\nThree"
    spans = split_target_projection(text, "en")
    assert "".join(text[span.start:span.end] for span in spans) == text
    assert spans[0].end == text.index("Two")


@pytest.mark.parametrize(
    ("language", "text", "first"),
    [
        ("en", 'He said "Done." Next.', 'He said "Done." '),
        ("pl", "To jest (gotowe!). Dalej.", "To jest (gotowe!). "),
        ("zh", "他说“好了。”接着。", "他说“好了。”"),
        ("ar", "قال: «تم؟» ثم تابع.", "قال: «تم؟» "),
        ("en", "Value 3.14 remains. Next.", "Value 3.14 remains. "),
    ],
)
def test_closing_punctuation_and_decimals_remain_with_the_preceding_sentence(language, text, first):
    spans = split_target_projection(text, language)
    assert text[spans[0].start:spans[0].end] == first
    assert "".join(text[span.start:span.end] for span in spans) == text


@pytest.mark.parametrize(
    ("language", "text"),
    [
        (
            "en",
            'The angels ask, “What has God wrought?”—what new teaching there is on high.',
        ),
        ("en", '“Stop!” said Mary.'),
        ("en", '“Really?”, he asked.'),
        ("pl", '„Naprawdę?” —zapytał.'),
    ],
)
def test_relaxed_atomicity_accepts_clear_quoted_continuations(language, text):
    assert is_atomic_target_sentence(text, language)


@pytest.mark.parametrize(
    ("language", "text"),
    [
        ("en", '“Done.” Next.'),
        ("en", "One. Two."),
        ("en", "Done!) said."),
        ("de", '“Done!” sagte er.'),
        ("en", "   \t"),
    ],
)
def test_relaxed_atomicity_rejects_ambiguous_or_empty_projection(language, text):
    assert not is_atomic_target_sentence(text, language)


def test_relaxed_atomicity_does_not_change_versioned_target_splitter():
    text = 'The angels ask, “What has God wrought?”—what new teaching there is on high.'
    first = 'The angels ask, “What has God wrought?”'
    second = '—what new teaching there is on high.'
    spans = split_target_projection(text, "en")

    assert target_split_profile("en")[1] == "target-splitter-v1"
    assert [(span.start, span.end) for span in spans] == [(0, 39), (39, 75)]
    assert [text[span.start:span.end] for span in spans] == [first, second]
    assert [span.text_hash for span in spans] == [
        sha256(first.encode("utf-8")).hexdigest(),
        sha256(second.encode("utf-8")).hexdigest(),
    ]
