"""Tests for the pure Translator target-output parser boundary."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from iarf_assistant.addons.translator import segment_output, segmenter, worker


def test_segment_output_file_import_closure_is_independent_of_host_runtime():
    module_path = (
        Path(__file__).resolve().parents[1]
        / "iarf_assistant"
        / "addons"
        / "translator"
        / "segment_output.py"
    )
    script = """
import importlib.util
import sys

module_name = "carlina_pure_segment_output_probe"
spec = importlib.util.spec_from_file_location(module_name, sys.argv[1])
if spec is None or spec.loader is None:
    raise SystemExit("could not load segment_output.py")
module = importlib.util.module_from_spec(spec)
sys.modules[module_name] = module
spec.loader.exec_module(module)
forbidden_prefixes = (
    "flask",
    "sqlalchemy",
    "iarf_assistant.addons.translator.models",
    "iarf_assistant.addons.translator.worker",
    "iarf_assistant.addons.translator.segmenter",
    "iarf_assistant.addons.translator.source_providers",
    "iarf_assistant.addons.translator.block_provider_adapter",
)
provider_modules = tuple(
    name
    for name in sys.modules
    if name.startswith(("carlina.", "iarf_assistant."))
    and "provider" in name.casefold()
)
loaded = sorted(
    name
    for name in sys.modules
    if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden_prefixes)
    or name in provider_modules
)
if loaded:
    raise SystemExit("forbidden modules loaded: " + ", ".join(loaded))
"""

    completed = subprocess.run(
        [sys.executable, "-c", script, str(module_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_worker_aliases_and_segmenter_policy_reexports_are_shared():
    assert worker._validate_target_output is segment_output.validate_target_output
    assert worker._parse_target_output is segment_output.parse_target_output
    assert worker.TranslationOutputError.__module__ == worker.__name__
    assert worker.TRANSLATABLE_INLINE_TAGS is segment_output.TRANSLATABLE_INLINE_TAGS
    assert (
        worker.TRANSLATABLE_INLINE_GLOBAL_ATTRIBUTES
        is segment_output.TRANSLATABLE_INLINE_GLOBAL_ATTRIBUTES
    )
    assert (
        worker.TRANSLATABLE_INLINE_TAG_ATTRIBUTES
        is segment_output.TRANSLATABLE_INLINE_TAG_ATTRIBUTES
    )
    assert worker.MAX_TARGET_TEXT_LENGTH == segment_output.MAX_TARGET_TEXT_LENGTH
    assert worker.MAX_TARGET_NOTE_LENGTH == segment_output.MAX_TARGET_NOTE_LENGTH
    assert (
        worker.MAX_TRANSLATOR_NOTE_ANCHOR_LENGTH
        == segment_output.MAX_TRANSLATOR_NOTE_ANCHOR_LENGTH
    )
    assert (
        worker.MAX_TRANSLATOR_NOTE_BODY_LENGTH
        == segment_output.MAX_TRANSLATOR_NOTE_BODY_LENGTH
    )
    assert (
        worker.MAX_TRANSLATOR_NOTES_PER_BATCH
        == segment_output.MAX_TRANSLATOR_NOTES_PER_BATCH
    )

    assert segmenter.TRANSLATABLE_INLINE_TAGS is segment_output.TRANSLATABLE_INLINE_TAGS
    assert (
        segmenter.TRANSLATABLE_INLINE_GLOBAL_ATTRIBUTES
        is segment_output.TRANSLATABLE_INLINE_GLOBAL_ATTRIBUTES
    )
    assert (
        segmenter.TRANSLATABLE_INLINE_TAG_ATTRIBUTES
        is segment_output.TRANSLATABLE_INLINE_TAG_ATTRIBUTES
    )


def test_public_validator_preserves_partial_missing_order_and_inline_text_slots():
    translated = (
        'A longer translated lead     <a href="#note">translated note</a>'
        "          and a longer translated end."
    )
    content = '<target id="second"><text>' + translated + "</text></target>"

    parsed, missing, errors = segment_output.validate_target_output(
        content,
        ["first", "second", "third"],
        source_markup_by_id={
            "second": 'lead <a href="#note">note</a> end.',
        },
    )
    parsed_with_missing, parser_missing = segment_output.parse_target_output(
        content,
        ["first", "second", "third"],
        source_markup_by_id={
            "second": 'lead <a href="#note">note</a> end.',
        },
    )

    assert parsed == {
        "second": {"text": translated, "note": None, "needs_review": False}
    }
    assert parsed_with_missing == parsed
    assert missing == parser_missing == ["first", "third"]
    assert errors == []


def test_public_validator_keeps_translator_note_anchor_matching_case_sensitive():
    content = (
        '<target id="a"><text>Han Festival was observed.</text>'
        "<translator_note><anchor>han festival</anchor>"
        "<body>Context.</body></translator_note></target>"
    )

    parsed, missing, errors = segment_output.validate_target_output(
        content,
        ["a"],
        translator_notes_enabled=True,
    )

    assert parsed == {}
    assert missing == ["a"]
    assert errors == ["translator_note anchor does not occur exactly once for a"]


def test_public_validator_preserves_output_text_note_and_translator_note_caps():
    oversized_text = (
        '<target id="a"><text>'
        + ("x" * (segment_output.MAX_TARGET_TEXT_LENGTH + 1))
        + "</text></target>"
    )
    oversized_note = (
        '<target id="a"><text>Text.</text><note>'
        + ("x" * (segment_output.MAX_TARGET_NOTE_LENGTH + 1))
        + "</note></target>"
    )
    oversized_anchor = (
        '<target id="a"><text>'
        + ("x" * (segment_output.MAX_TRANSLATOR_NOTE_ANCHOR_LENGTH + 1))
        + "</text><translator_note><anchor>"
        + ("x" * (segment_output.MAX_TRANSLATOR_NOTE_ANCHOR_LENGTH + 1))
        + "</anchor><body>Context.</body></translator_note></target>"
    )
    oversized_body = (
        '<target id="a"><text>Anchor.</text><translator_note>'
        "<anchor>Anchor.</anchor><body>"
        + ("x" * (segment_output.MAX_TRANSLATOR_NOTE_BODY_LENGTH + 1))
        + "</body></translator_note></target>"
    )

    for content, notes_enabled in (
        (oversized_text, False),
        (oversized_note, False),
        (oversized_anchor, True),
        (oversized_body, True),
    ):
        parsed, missing, errors = segment_output.validate_target_output(
            content,
            ["a"],
            translator_notes_enabled=notes_enabled,
        )
        assert parsed == {}
        assert missing == ["a"]
        assert errors
