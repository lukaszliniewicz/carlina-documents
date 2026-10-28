"""Canonical document-block translation-unit setting values."""

from __future__ import annotations

from .block_contract import BlockContractError

TRANSLATION_UNIT_MODE_BLOCK = "block"
TRANSLATION_UNIT_MODE_ALIGNED_SENTENCES = "aligned_sentences"
TRANSLATION_UNIT_MODES = frozenset(
    {
        TRANSLATION_UNIT_MODE_BLOCK,
        TRANSLATION_UNIT_MODE_ALIGNED_SENTENCES,
    }
)
DEFAULT_TRANSLATION_UNIT_MODE = TRANSLATION_UNIT_MODE_BLOCK


def normalize_translation_unit_mode(value: object = None) -> str:
    """Return the strict canonical translation-unit mode.

    Missing legacy settings retain the original block workflow.  Explicit
    values are intentionally not coerced so a malformed or future setting
    cannot silently select a different translation contract.
    """

    if value is None:
        return DEFAULT_TRANSLATION_UNIT_MODE
    if isinstance(value, str) and value in TRANSLATION_UNIT_MODES:
        return value
    raise BlockContractError("translation-unit-mode")


__all__ = [
    "DEFAULT_TRANSLATION_UNIT_MODE",
    "TRANSLATION_UNIT_MODES",
    "TRANSLATION_UNIT_MODE_ALIGNED_SENTENCES",
    "TRANSLATION_UNIT_MODE_BLOCK",
    "normalize_translation_unit_mode",
]
