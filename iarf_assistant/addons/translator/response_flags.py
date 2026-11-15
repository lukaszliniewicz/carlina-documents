"""Doubts a translating model attaches to its own window result.

When a frozen operation enables the response-flags contract, the provider
schema gives every result item a ``flags`` array: usually empty, otherwise
assumptions, source defects, unverified facts, deliberate departures and
passages to revisit. A result that omits the array has no flags; one that
supplies it must supply a valid one. Flags are
review records about the translation. They never enter the target text, and
they are stored as translator flags (origin ``translation``) only when the
window that raised them is activated.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from .block_contract import BlockContractError
from .models import REVIEW_FLAG_KINDS

RESPONSE_FLAGS_CONTRACT_VERSION = "translator-response-flags-v1"
MAX_RESPONSE_FLAGS_PER_BLOCK = 2
MAX_RESPONSE_FLAG_BODY_CHARS = 600
MAX_RESPONSE_FLAG_EXCERPT_CHARS = 200

RESPONSE_FLAGS_INSTRUCTION = (
    " Each result also has `flags`, which is normally empty. Add a flag only for a doubt "
    "a reviewer must settle: `assumption` (a reading the text does not settle; name the "
    "alternative you rejected), `source_defect` (a misprint, gap or garbled passage), "
    "`unverified` (a name, date, quotation or fact you could not check), `departure` (a "
    "deliberate change of form or content) or `revisit` (a heading, term or passage to "
    "reconsider once more of the text is translated). State the doubt and what would "
    "settle it in one or two sentences of `body`; `excerpt` quotes a few words that "
    "locate it, or is empty. Never put doubts, alternatives or queries in `content` "
    "or in notes."
)


@dataclass(frozen=True, slots=True)
class ResponseFlag:
    """One validated flag from one result of a window response."""

    block_id: str
    number: int
    index: int
    kind: str
    body: str
    excerpt: str

    @property
    def idempotency_suffix(self) -> str:
        return f"{self.number}:{self.index}"


def response_flags_schema() -> dict[str, object]:
    """Return the per-result ``flags`` property for strict provider schemas."""

    return {
        "type": "array",
        "maxItems": MAX_RESPONSE_FLAGS_PER_BLOCK,
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "body", "excerpt"],
            "properties": {
                "kind": {"type": "string", "enum": list(REVIEW_FLAG_KINDS)},
                "body": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_RESPONSE_FLAG_BODY_CHARS,
                },
                "excerpt": {
                    "type": "string",
                    "maxLength": MAX_RESPONSE_FLAG_EXCERPT_CHARS,
                },
            },
        },
    }


def _text(value: object, maximum: int, *, required: bool) -> str:
    if not isinstance(value, str):
        raise BlockContractError("window-response-flags")
    text = value.strip()
    if (required and not text) or len(text) > maximum or "\x00" in text:
        raise BlockContractError("window-response-flags")
    return text


def block_response_flags(
    value: object, *, block_id: str, number: int
) -> tuple[ResponseFlag, ...]:
    """Validate the ``flags`` array of one result item."""

    if not isinstance(value, list):
        raise BlockContractError("window-response-flags")
    items = cast(list[object], value)
    if len(items) > MAX_RESPONSE_FLAGS_PER_BLOCK:
        raise BlockContractError("window-response-flags")
    flags: list[ResponseFlag] = []
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise BlockContractError("window-response-flags")
        mapping = cast(Mapping[str, object], item)
        if set(mapping) != {"kind", "body", "excerpt"}:
            raise BlockContractError("window-response-flags")
        kind = mapping["kind"]
        if not isinstance(kind, str) or kind not in REVIEW_FLAG_KINDS:
            raise BlockContractError("window-response-flags")
        flags.append(
            ResponseFlag(
                block_id=block_id,
                number=number,
                index=index,
                kind=kind,
                body=_text(
                    mapping["body"], MAX_RESPONSE_FLAG_BODY_CHARS, required=True
                ),
                excerpt=_text(
                    mapping["excerpt"], MAX_RESPONSE_FLAG_EXCERPT_CHARS, required=False
                ),
            )
        )
    return tuple(flags)


def window_response_flags(
    results: Sequence[object], block_ids: Sequence[str]
) -> tuple[ResponseFlag, ...]:
    """Validate and collect the flags of every result in window order.

    A result without ``flags`` contributes none: strict providers always send
    the array, and a passive worker that leaves it out loses nothing else.
    """

    if len(results) != len(block_ids):
        raise BlockContractError("window-response-cardinality")
    flags: list[ResponseFlag] = []
    for number, (block_id, item) in enumerate(zip(block_ids, results, strict=True), 1):
        if not isinstance(item, Mapping):
            raise BlockContractError("window-response-flags")
        mapping = cast(Mapping[str, object], item)
        if "flags" not in mapping:
            continue
        flags.extend(
            block_response_flags(mapping["flags"], block_id=block_id, number=number)
        )
    return tuple(flags)


__all__ = [
    "MAX_RESPONSE_FLAGS_PER_BLOCK",
    "MAX_RESPONSE_FLAG_BODY_CHARS",
    "MAX_RESPONSE_FLAG_EXCERPT_CHARS",
    "RESPONSE_FLAGS_CONTRACT_VERSION",
    "RESPONSE_FLAGS_INSTRUCTION",
    "ResponseFlag",
    "block_response_flags",
    "response_flags_schema",
    "window_response_flags",
]
