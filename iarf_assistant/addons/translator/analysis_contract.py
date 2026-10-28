"""Strict DTO contracts for Phase-5A analysis evidence.

These records deliberately distinguish a typed artifact's schema from the
untrusted text that an analyst (human, model, or deterministic system) wrote.
Nothing here is a prompt or an instruction channel.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from iarf_assistant.core.encryption import keyed_lookup_candidates, keyed_lookup_digest

from .block_contract import BlockContractError, canonical_json, sha256_text

ANALYSIS_CENSUS_PROFILE = "semantic-census-v1"
ANALYSIS_CENSUS_VERSION = "1"
ARTIFACT_SCHEMA_VERSION = "context-artifact-v1"
ARTIFACT_KINDS = frozenset({"document_brief", "style_guide", "entity_register"})


def _hash(value: object, code: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise BlockContractError(code)
    return value


def private_witness(value: object, *, purpose: str) -> str:
    """Opaque fixed-width witness for source-bearing material.

    The versioned keyed digest is the cryptographic authority.  Hashing that
    already-opaque value only keeps the durable column width stable; it must
    never be replaced with a hash of the private value itself.
    """

    return sha256_text(keyed_lookup_digest(canonical_json(value), purpose=purpose))


def private_witness_candidates(value: object, *, purpose: str) -> tuple[str, ...]:
    return tuple(
        sha256_text(candidate)
        for candidate in keyed_lookup_candidates(canonical_json(value), purpose=purpose)
    )


def _safe_identifier(value: object, code: str, maximum: int = 128) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value.encode("utf-8")) > maximum
    ):
        raise BlockContractError(code)
    return value


def canonical_scope(value: object) -> str:
    import unicodedata

    if not isinstance(value, str):
        raise BlockContractError("glossary-concept-scope")
    normalized = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    if not normalized or len(normalized.encode("utf-8")) > 128:
        raise BlockContractError("glossary-concept-scope")
    return normalized


@dataclass(frozen=True, slots=True)
class ContextArtifactPayload:
    """Kind-specific but bounded structured artifact material."""

    kind: str
    language: str
    content: Mapping[str, Any]

    def __post_init__(self) -> None:
        if (
            self.kind not in ARTIFACT_KINDS
            or not isinstance(self.language, str)
            or not self.language.strip()
            or not isinstance(self.content, Mapping)
        ):
            raise BlockContractError("analysis-artifact-payload")
        required = {
            "document_brief": {"summary", "domains", "audience"},
            "style_guide": {"register", "guidance", "prohibitions"},
            "entity_register": {"entities"},
        }[self.kind]
        if set(self.content) != required:
            raise BlockContractError("analysis-artifact-schema")
        if len(canonical_json(dict(self.content)).encode("utf-8")) > 128 * 1024:
            raise BlockContractError("analysis-artifact-size")
        _validate_artifact_content(self.kind, self.content)
        # The dataclass is frozen only at the top level.  Rematerialize the
        # validated JSON tree so a caller mutating its original nested lists or
        # mappings cannot silently mutate this DTO after validation.
        try:
            isolated = json.loads(
                json.dumps(
                    dict(self.content),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            )
        except (RecursionError, TypeError, ValueError) as exc:
            raise BlockContractError("analysis-artifact-payload") from exc
        if not isinstance(isolated, dict):
            raise BlockContractError("analysis-artifact-payload")
        object.__setattr__(self, "content", isolated)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "kind": self.kind,
            "language": self.language,
            "content": dict(self.content),
        }

    @property
    def payload_hash(self) -> str:
        return private_witness(self.to_dict(), purpose="translator-artifact-payload-v1")


@dataclass(frozen=True, slots=True)
class GlossaryPayload:
    """Private terminology material; lookup data is derived, never exposed."""

    source_term: str
    target_term: str
    notes: str | None = None
    scope: str = "document"
    usage: dict[str, object] | None = None

    def __post_init__(self) -> None:
        for field in (self.source_term, self.target_term):
            _safe_identifier(field, "glossary-payload", 4096)
        object.__setattr__(self, "scope", canonical_scope(self.scope))
        if self.notes is not None and (
            not isinstance(self.notes, str)
            or len(self.notes.encode("utf-8")) > 32 * 1024
        ):
            raise BlockContractError("glossary-notes")

        if self.usage is not None:
            import copy

            from .glossary_contract import GlossaryUsageV1, validate_glossary_usage

            try:
                original_usage = copy.deepcopy(self.usage)
                usage = validate_glossary_usage(original_usage)
                for form in usage.source_forms:
                    _safe_identifier(form, "glossary-source-form", 4096)
                for form in getattr(usage, "accepted_target_forms", []):
                    _safe_identifier(form, "glossary-target-form", 4096)
            except ValueError as exc:
                raise BlockContractError("glossary-usage") from exc
            object.__setattr__(
                self,
                "usage",
                original_usage if isinstance(usage, GlossaryUsageV1) else usage.model_dump(),
            )

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "source_term": self.source_term,
            "target_term": self.target_term,
            "notes": self.notes,
            "scope": self.scope,
        }
        # Preserve historical payload bytes and hashes exactly. Only newly
        # authored, morphology-aware revisions carry the optional extension.
        if self.usage is not None:
            import copy
            result["usage"] = copy.deepcopy(self.usage)
        return result

    @property
    def payload_hash(self) -> str:
        return private_witness(self.to_dict(), purpose="translator-glossary-payload-v1")


def glossary_usage_view(payload: GlossaryPayload) -> dict[str, object] | None:
    """Safe reader/prompt metadata, excluding private request-retry witnesses."""
    if payload.usage is None:
        return None
    keys = ["sense", "match_policy", "source_forms", "observations", "provenance"]
    if "accepted_target_forms" in payload.usage:
        keys.append("accepted_target_forms")
    return {key: payload.usage[key] for key in keys}


def glossary_prompt_mapping(payload: GlossaryPayload) -> dict[str, object]:
    """Preserve old baseline bytes; add sense/inflection policy for new terms."""
    result: dict[str, object] = {"source": payload.source_term, "target": payload.target_term}
    if payload.usage is not None:
        result.update(sense=payload.usage["sense"], match_policy=payload.usage["match_policy"],
                      source_forms=payload.usage["source_forms"], notes=payload.notes)
        if "accepted_target_forms" in payload.usage:
            result["accepted_target_forms"] = payload.usage["accepted_target_forms"]
    return result


def normalized_term(
    term: str, *, profile: str = "unicode-casefold-v1", version: str = "1"
) -> str:
    if profile != "unicode-casefold-v1" or version != "1" or not isinstance(term, str):
        raise BlockContractError("glossary-normalization-profile")
    value = " ".join(term.casefold().split())
    if not value:
        raise BlockContractError("glossary-normalization-empty")
    return value


def glossary_lookup_digest(
    term: str, *, profile: str = "unicode-casefold-v1", version: str = "1"
) -> str:
    """Versioned installation-key lookup digest; retained keys support rotation."""

    normalized = normalized_term(term, profile=profile, version=version)
    return keyed_lookup_digest(
        normalized,
        purpose=f"translator-glossary:{profile}:{version}",
    )


def _bounded_string(value: object, code: str, maximum: int = 16_384) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > maximum:
        raise BlockContractError(code)
    return value


def _bounded_strings(value: object, code: str, *, maximum: int = 128) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise BlockContractError(code)
    return [_bounded_string(item, code) for item in value]


def _validate_artifact_content(kind: str, content: Mapping[str, Any]) -> None:
    """Reject loosely-shaped JSON before it can become durable analysis evidence."""

    if kind == "document_brief":
        _bounded_string(content["summary"], "analysis-artifact-schema")
        _bounded_strings(content["domains"], "analysis-artifact-schema", maximum=32)
        _bounded_string(content["audience"], "analysis-artifact-schema", 1024)
        return
    if kind == "style_guide":
        _bounded_string(content["register"], "analysis-artifact-schema", 256)
        _bounded_strings(content["guidance"], "analysis-artifact-schema", maximum=128)
        _bounded_strings(content["prohibitions"], "analysis-artifact-schema", maximum=128)
        return
    entities = content["entities"]
    if not isinstance(entities, list) or len(entities) > 2_048:
        raise BlockContractError("analysis-artifact-schema")
    for entity in entities:
        if not isinstance(entity, Mapping) or set(entity) != {"name", "type", "aliases"}:
            raise BlockContractError("analysis-artifact-schema")
        _bounded_string(entity["name"], "analysis-artifact-schema", 4096)
        _bounded_string(entity["type"], "analysis-artifact-schema", 256)
        _bounded_strings(entity["aliases"], "analysis-artifact-schema", maximum=64)


__all__ = [
    "ANALYSIS_CENSUS_PROFILE",
    "ANALYSIS_CENSUS_VERSION",
    "ARTIFACT_KINDS",
    "ARTIFACT_SCHEMA_VERSION",
    "ContextArtifactPayload",
    "GlossaryPayload",
    "canonical_scope",
    "glossary_lookup_digest",
    "normalized_term",
    "private_witness",
    "private_witness_candidates",
]
