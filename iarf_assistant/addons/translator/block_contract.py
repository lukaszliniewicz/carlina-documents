"""Strict, source-safe contracts for the experimental block translation engine.

This module deliberately contains no persistence or provider code.  The
records are the immutable boundary between a completed Doc Processor snapshot,
the deterministic source planner, and a future durable workflow.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any

from carlina.core.semantic_export import (
    SemanticExportContractError,
    SemanticExportResource,
)
from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION

LEGACY_ENGINE_CONTRACT = "segment-v1"
DOCUMENT_BLOCK_ENGINE_CONTRACT = "document-block-v1"
DOCUMENT_SNAPSHOT_CONTRACT_VERSION = "document-snapshot-v1"
SOURCE_PLAN_CONTRACT_VERSION = "translation-source-plan-v1"
PROTECTED_INLINE_CONTRACT_VERSION = "protected-inline-v1"
SENTENCE_ANCHOR_PROFILE_VERSION = "sentence-anchor-v1"
LEGACY_BLOCK_PLANNER_VERSION = "document-block-planner-v1"
BLOCK_PLANNER_V2 = "document-block-planner-v2"
BLOCK_PLANNER_VERSION = "document-block-planner-v3"
CORRECTED_BLOCK_PLANNER_VERSION = "document-block-correction-v1"
SUPPORTED_BLOCK_PLANNER_VERSIONS = frozenset(
    {
        LEGACY_BLOCK_PLANNER_VERSION,
        BLOCK_PLANNER_V2,
        BLOCK_PLANNER_VERSION,
        CORRECTED_BLOCK_PLANNER_VERSION,
    }
)

MAX_SNAPSHOT_HTML_BYTES = 20 * 1024 * 1024
MAX_METADATA_DEPTH = 4
MAX_METADATA_ITEMS = 128
MAX_METADATA_SCALAR_BYTES = 4_096
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
_BLOCK_ROLES = frozenset(
    {
        "heading",
        "paragraph",
        "direct_prose",
        "list_item",
        "blockquote",
        "definition_term",
        "definition_body",
        "table_caption",
        "table_header",
        "table_cell",
        "figure_caption",
        "note_body",
        "source_toc_entry",
        "document_title",
        "accessibility_text",
    }
)
_DISPOSITIONS = frozenset(
    {"translatable", "literal-preserved", "structural-only", "excluded-with-reason"}
)
_TOKEN_KINDS = frozenset({"open", "close", "void"})
_PROTECTED_INLINE_TAGS = frozenset(
    {
        "a",
        "abbr",
        "b",
        "br",
        "cite",
        "code",
        "del",
        "em",
        "i",
        "ins",
        "kbd",
        "mark",
        "q",
        "s",
        "samp",
        "small",
        "span",
        "strong",
        "sub",
        "sup",
        "time",
        "u",
        "var",
        "wbr",
    }
)
_TOKEN_POLICIES = frozenset({"fixed", "movable", "source-only"})
_COVERAGE_KINDS = frozenset({"text", "entity", "attribute", "break"})


class BlockContractError(ValueError):
    """A bounded, non-content-bearing block-contract failure."""

    def __init__(self, code: str):
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,95}", code):
            code = "block-contract-invalid"
        self.code = code
        super().__init__(code)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    """Return the sole JSON encoding used for hashes and durable DTOs."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _frozen_value(value: Any, *, depth: int = 0) -> Any:
    if depth > MAX_METADATA_DEPTH:
        raise BlockContractError("metadata-depth")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_METADATA_SCALAR_BYTES:
            raise BlockContractError("metadata-scalar-too-large")
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise BlockContractError("metadata-nonfinite")
        return value
    if isinstance(value, Mapping):
        if len(value) > MAX_METADATA_ITEMS:
            raise BlockContractError("metadata-too-many-items")
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 128:
                raise BlockContractError("metadata-key")
            result[key] = _frozen_value(item, depth=depth + 1)
        return MappingProxyType(result)
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_METADATA_ITEMS:
            raise BlockContractError("metadata-too-many-items")
        return tuple(_frozen_value(item, depth=depth + 1) for item in value)
    raise BlockContractError("metadata-type")


def _plain_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_value(item) for item in value]
    return value


MAX_SOURCE_SENTENCE_PROJECTIONS = 20_000
MAX_SOURCE_SENTENCE_BOUNDARIES = 100_000
MAX_SOURCE_SENTENCE_SEGMENTS = 4096


def normalize_source_sentence_manifest(value: object) -> dict[str, Any]:
    """Validate the document-sized boundary input without retaining prose."""

    code = "source-sentence-segmentation-manifest"
    if (
        not isinstance(value, Mapping)
        or set(value) != {"contract_version", "projections"}
        or value.get("contract_version") != "source-sentence-manifest-v1"
    ):
        raise BlockContractError(code)
    projections = value["projections"]
    if (
        not isinstance(projections, Mapping)
        or len(projections) > MAX_SOURCE_SENTENCE_PROJECTIONS
    ):
        raise BlockContractError(code)
    normalized: dict[str, list[int]] = {}
    total = 0
    for projection_hash, offsets in projections.items():
        if (
            not isinstance(projection_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", projection_hash) is None
            or not isinstance(offsets, (list, tuple))
            or not 1 <= len(offsets) <= MAX_SOURCE_SENTENCE_SEGMENTS
        ):
            raise BlockContractError(code)
        previous = 0
        for end in offsets:
            if isinstance(end, bool) or not isinstance(end, int) or end <= previous:
                raise BlockContractError(code)
            previous = end
        total += len(offsets)
        if total > MAX_SOURCE_SENTENCE_BOUNDARIES:
            raise BlockContractError(code)
        normalized[projection_hash] = list(offsets)
    return {
        "contract_version": "source-sentence-manifest-v1",
        "projections": normalized,
    }


def _frozen_planner_settings(
    value: Mapping[str, Any], *, depth: int = 0
) -> Mapping[str, Any]:
    """Freeze bounded settings plus the separately bounded inference input.

    Sentence boundaries are retained planner inputs, not generic metadata.
    Their document-sized inventory has its own limits; all other metadata
    keeps the existing small-object budget, including inside correction plans.
    """

    if depth > MAX_METADATA_DEPTH or len(value) > MAX_METADATA_ITEMS:
        raise BlockContractError("plan-settings")
    ordinary = dict(value)
    special: dict[str, Any] = {}
    if "source_sentence_manifest" in ordinary:
        manifest = normalize_source_sentence_manifest(
            ordinary.pop("source_sentence_manifest")
        )
        special["source_sentence_manifest"] = MappingProxyType(
            {
                "contract_version": manifest["contract_version"],
                "projections": MappingProxyType(
                    {
                        key: tuple(offsets)
                        for key, offsets in manifest["projections"].items()
                    }
                ),
            }
        )
    if "base_planner_settings" in ordinary:
        base = ordinary.pop("base_planner_settings")
        if not isinstance(base, Mapping):
            raise BlockContractError("plan-settings")
        special["base_planner_settings"] = _frozen_planner_settings(
            base, depth=depth + 1
        )
    return MappingProxyType({**dict(_frozen_value(ordinary, depth=depth)), **special})


def _mapping(value: Any, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BlockContractError(code)
    return value


def _exact_keys(value: Mapping[str, Any], keys: set[str], code: str) -> None:
    if set(value) != keys:
        raise BlockContractError(code)


def _string(
    value: Any, code: str, *, allow_blank: bool = False, maximum: int = 4096
) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > maximum:
        raise BlockContractError(code)
    if not allow_blank and not value.strip():
        raise BlockContractError(code)
    return value


def _hash(value: Any, code: str = "hash") -> str:
    value = _string(value, code, maximum=64)
    if not _HASH_RE.fullmatch(value):
        raise BlockContractError(code)
    return value


def _integer(value: Any, code: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise BlockContractError(code)
    return value


@dataclass(frozen=True, slots=True)
class AttestedDocumentSnapshot:
    """The only upstream DTO a block plan is allowed to accept."""

    id: str
    document_id: str
    output_html: str
    content_hash: str
    semantic_content_hash: str
    metadata: Mapping[str, Any]
    provenance: Mapping[str, Any]
    resources: tuple[SemanticExportResource, ...] = ()
    filename: str | None = None
    captured_at: str | None = None
    source_updated_at: str | None = None
    snapshot_contract_version: str = DOCUMENT_SNAPSHOT_CONTRACT_VERSION
    semantic_contract_version: str = SEMANTIC_HTML_CONTRACT_VERSION
    semantic_contract_mode: str = "completed"
    status: str = "completed"

    def __post_init__(self) -> None:
        if self.snapshot_contract_version != DOCUMENT_SNAPSHOT_CONTRACT_VERSION:
            raise BlockContractError("snapshot-contract-version")
        if self.semantic_contract_version != SEMANTIC_HTML_CONTRACT_VERSION:
            raise BlockContractError("semantic-contract-version")
        if self.semantic_contract_mode != "completed":
            raise BlockContractError("semantic-contract-mode")
        if _string(self.status, "snapshot-status", maximum=32).lower() != "completed":
            raise BlockContractError("snapshot-status")
        for value in (self.id, self.document_id):
            _string(value, "snapshot-identifier", maximum=256)
            if not _IDENTIFIER_RE.fullmatch(value):
                raise BlockContractError("snapshot-identifier")
        if self.id != self.document_id:
            raise BlockContractError("snapshot-id-mismatch")
        if not isinstance(self.output_html, str):
            raise BlockContractError("snapshot-html")
        encoded = self.output_html.encode("utf-8")
        if not self.output_html.strip() or len(encoded) > MAX_SNAPSHOT_HTML_BYTES:
            raise BlockContractError("snapshot-html-size")
        expected = sha256_text(self.output_html)
        if _hash(self.content_hash, "snapshot-content-hash") != expected:
            raise BlockContractError("snapshot-content-hash")
        if _hash(self.semantic_content_hash, "snapshot-semantic-hash") != expected:
            raise BlockContractError("snapshot-semantic-hash")
        object.__setattr__(self, "metadata", _frozen_value(self.metadata))
        object.__setattr__(self, "provenance", _frozen_value(self.provenance))
        if isinstance(self.resources, list):
            object.__setattr__(self, "resources", tuple(self.resources))
        if not isinstance(self.resources, tuple) or not all(
            isinstance(item, SemanticExportResource) for item in self.resources
        ):
            raise BlockContractError("snapshot-resources")
        if len({item.ref for item in self.resources}) != len(self.resources):
            raise BlockContractError("snapshot-resources")
        if self.filename is not None:
            _string(self.filename, "snapshot-filename", maximum=1024)
        for value in (self.captured_at, self.source_updated_at):
            if value is not None:
                timestamp = _string(value, "snapshot-timestamp", maximum=128)
                try:
                    if "T" not in timestamp:
                        raise ValueError
                    datetime.fromisoformat(timestamp)
                except ValueError as exc:
                    raise BlockContractError("snapshot-timestamp") from exc

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AttestedDocumentSnapshot:
        value = _mapping(value, "snapshot-dto")
        allowed = {
            "id",
            "document_id",
            "output_html",
            "content_hash",
            "semantic_content_hash",
            "metadata",
            "provenance",
            "filename",
            "captured_at",
            "source_updated_at",
            "snapshot_contract_version",
            "semantic_contract_version",
            "semantic_contract_mode",
            "status",
            "resources",
        }
        required = allowed - {
            "filename",
            "captured_at",
            "source_updated_at",
            "resources",
        }
        if not required.issubset(value) or set(value) - allowed:
            raise BlockContractError("snapshot-dto-keys")
        document_id = _string(
            value.get("document_id"), "snapshot-document-id", maximum=256
        )
        identifier = _string(value.get("id", document_id), "snapshot-id", maximum=256)
        try:
            resources = tuple(
                SemanticExportResource.from_dict(item)
                for item in value.get("resources", [])
                if isinstance(item, Mapping)
            )
        except SemanticExportContractError as exc:
            raise BlockContractError("snapshot-resources") from exc
        if len(resources) != len(value.get("resources", [])):
            raise BlockContractError("snapshot-resources")
        return cls(
            id=identifier,
            document_id=document_id,
            output_html=_string(
                value.get("output_html"),
                "snapshot-html",
                allow_blank=True,
                maximum=MAX_SNAPSHOT_HTML_BYTES,
            ),
            content_hash=_string(
                value.get("content_hash"), "snapshot-content-hash", maximum=64
            ),
            semantic_content_hash=_string(
                value.get("semantic_content_hash"), "snapshot-semantic-hash", maximum=64
            ),
            metadata=_mapping(value.get("metadata"), "snapshot-metadata"),
            provenance=_mapping(value.get("provenance"), "snapshot-provenance"),
            filename=value.get("filename"),
            captured_at=value.get("captured_at"),
            source_updated_at=value.get("source_updated_at"),
            snapshot_contract_version=value.get("snapshot_contract_version"),
            semantic_contract_version=value.get("semantic_contract_version"),
            semantic_contract_mode=value.get("semantic_contract_mode"),
            status=value.get("status"),
            resources=resources,
        )

    from_mapping = from_dict

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "document_id": self.document_id,
            "snapshot_contract_version": self.snapshot_contract_version,
            "status": self.status,
            "output_html": self.output_html,
            "content_hash": self.content_hash,
            "semantic_contract_version": self.semantic_contract_version,
            "semantic_contract_mode": self.semantic_contract_mode,
            "semantic_content_hash": self.semantic_content_hash,
            "filename": self.filename,
            "captured_at": self.captured_at,
            "source_updated_at": self.source_updated_at,
            "metadata": _plain_value(self.metadata),
            "provenance": _plain_value(self.provenance),
            "resources": [item.to_dict() for item in self.resources],
        }


@dataclass(frozen=True, slots=True)
class SourceProviderAttestation:
    """Pinned provider capability provenance, deliberately free of source text."""

    provider_addon_id: str
    provider_capability_id: str
    owner_id: str
    resource_id: str
    version: int = 1
    capability: str = "document.processing"
    resource_type: str = "document"

    def __post_init__(self) -> None:
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version != 1
            or self.capability != "document.processing"
            or self.resource_type != "document"
        ):
            raise BlockContractError("source-attestation-contract")
        for field_name, code in (
            ("provider_addon_id", "source-attestation-addon"),
            ("provider_capability_id", "source-attestation-capability"),
            ("owner_id", "source-attestation-owner"),
            ("resource_id", "source-attestation-resource"),
        ):
            value = getattr(self, field_name)
            _string(value, code, maximum=256)
            object.__setattr__(self, field_name, value.strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "capability": self.capability,
            "provider_addon_id": self.provider_addon_id,
            "provider_capability_id": self.provider_capability_id,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "owner_id": self.owner_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SourceProviderAttestation:
        value = _mapping(value, "source-attestation-dto")
        _exact_keys(
            value,
            {
                "version",
                "capability",
                "provider_addon_id",
                "provider_capability_id",
                "resource_type",
                "resource_id",
                "owner_id",
            },
            "source-attestation-dto-keys",
        )
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class SourceSlot:
    id: str
    locator: str
    kind: str
    disposition: str
    policy_code: str
    raw_start: int
    raw_end: int
    projection_start: int | None
    projection_end: int | None
    source_value: str
    source_hash: str

    def __post_init__(self) -> None:
        _string(self.id, "slot-id", maximum=128)
        _string(self.locator, "slot-locator", maximum=2048)
        if (
            self.kind
            not in {
                "text",
                "entity",
                "attribute",
                "break",
                "boundary",
            }
            or self.disposition not in _DISPOSITIONS
        ):
            raise BlockContractError("slot-kind")
        _string(self.policy_code, "slot-policy", maximum=96)
        start, end = (
            _integer(self.raw_start, "slot-span"),
            _integer(self.raw_end, "slot-span"),
        )
        if self.kind == "boundary":
            if (
                end != start
                or self.disposition != "translatable"
                or self.policy_code != "owner-merge-boundary"
                or self.source_value
            ):
                raise BlockContractError("slot-boundary")
        elif end <= start:
            raise BlockContractError("slot-span")
        if (self.projection_start is None) != (self.projection_end is None):
            raise BlockContractError("slot-projection")
        if self.projection_start is not None and _integer(
            self.projection_start, "slot-projection"
        ) > _integer(self.projection_end, "slot-projection"):
            raise BlockContractError("slot-projection")
        if _hash(self.source_hash, "slot-source-hash") != sha256_text(
            _string(
                self.source_value,
                "slot-source-value",
                allow_blank=True,
                maximum=MAX_SNAPSHOT_HTML_BYTES,
            )
        ):
            raise BlockContractError("slot-source-hash")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "locator": self.locator,
            "kind": self.kind,
            "disposition": self.disposition,
            "policy_code": self.policy_code,
            "raw_start": self.raw_start,
            "raw_end": self.raw_end,
            "projection_start": self.projection_start,
            "projection_end": self.projection_end,
            "source_value": self.source_value,
            "source_hash": self.source_hash,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SourceSlot:
        value = _mapping(value, "slot-dto")
        _exact_keys(
            value,
            {
                "id",
                "locator",
                "kind",
                "disposition",
                "policy_code",
                "raw_start",
                "raw_end",
                "projection_start",
                "projection_end",
                "source_value",
                "source_hash",
            },
            "slot-dto-keys",
        )
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class ProtectedInlineToken:
    id: str
    kind: str
    tag: str
    locator: str
    movement_policy: str
    raw_start: int
    raw_end: int
    element_id: str
    source_value: str
    source_hash: str
    contract_version: str = PROTECTED_INLINE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        _string(self.id, "inline-id", maximum=128)
        tag = _string(self.tag, "inline-tag", maximum=64).lower()
        _string(self.locator, "inline-locator", maximum=2048)
        _string(self.movement_policy, "inline-policy", maximum=96)
        _string(self.element_id, "inline-element-id", maximum=128)
        if (
            self.kind not in _TOKEN_KINDS
            or tag not in _PROTECTED_INLINE_TAGS
            or self.movement_policy not in _TOKEN_POLICIES
            or self.contract_version != PROTECTED_INLINE_CONTRACT_VERSION
        ):
            raise BlockContractError("inline-contract")
        if (self.kind == "void") != (tag in {"br", "wbr"}):
            raise BlockContractError("inline-kind-tag")
        start, end = (
            _integer(self.raw_start, "inline-span"),
            _integer(self.raw_end, "inline-span"),
        )
        if end <= start:
            raise BlockContractError("inline-span")
        if _hash(self.source_hash, "inline-source-hash") != sha256_text(
            _string(
                self.source_value,
                "inline-source-value",
                allow_blank=True,
                maximum=MAX_SNAPSHOT_HTML_BYTES,
            )
        ):
            raise BlockContractError("inline-source-hash")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "tag": self.tag,
            "locator": self.locator,
            "movement_policy": self.movement_policy,
            "raw_start": self.raw_start,
            "raw_end": self.raw_end,
            "element_id": self.element_id,
            "source_value": self.source_value,
            "source_hash": self.source_hash,
            "contract_version": self.contract_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProtectedInlineToken:
        value = _mapping(value, "inline-dto")
        _exact_keys(
            value,
            {
                "id",
                "kind",
                "tag",
                "locator",
                "movement_policy",
                "raw_start",
                "raw_end",
                "element_id",
                "source_value",
                "source_hash",
                "contract_version",
            },
            "inline-dto-keys",
        )
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class SourceAnchor:
    id: str
    order: int
    start: int
    end: int
    source_hash: str
    splitter_profile: str = "fallback"
    splitter_version: str = SENTENCE_ANCHOR_PROFILE_VERSION

    def __post_init__(self) -> None:
        _string(self.id, "anchor-id", maximum=128)
        _integer(self.order, "anchor-order", minimum=1)
        if _integer(self.start, "anchor-span") >= _integer(self.end, "anchor-span"):
            raise BlockContractError("anchor-span")
        _hash(self.source_hash, "anchor-source-hash")
        _string(self.splitter_profile, "anchor-profile", maximum=64)
        _string(self.splitter_version, "anchor-version", maximum=128)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "order": self.order,
            "start": self.start,
            "end": self.end,
            "source_hash": self.source_hash,
            "splitter_profile": self.splitter_profile,
            "splitter_version": self.splitter_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SourceAnchor:
        value = _mapping(value, "anchor-dto")
        _exact_keys(
            value,
            {
                "id",
                "order",
                "start",
                "end",
                "source_hash",
                "splitter_profile",
                "splitter_version",
            },
            "anchor-dto-keys",
        )
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class CoverageEntry:
    locator: str
    kind: str
    disposition: str
    policy_code: str
    raw_start: int
    raw_end: int
    source_hash: str
    block_id: str | None = None

    def __post_init__(self) -> None:
        _string(self.locator, "coverage-locator", maximum=2048)
        if _string(self.kind, "coverage-kind", maximum=64) not in _COVERAGE_KINDS:
            raise BlockContractError("coverage-kind")
        if self.disposition not in _DISPOSITIONS:
            raise BlockContractError("coverage-disposition")
        _string(self.policy_code, "coverage-policy", maximum=96)
        if _integer(self.raw_start, "coverage-span") >= _integer(
            self.raw_end, "coverage-span"
        ):
            raise BlockContractError("coverage-span")
        _hash(self.source_hash, "coverage-source-hash")
        if self.block_id is not None:
            _string(self.block_id, "coverage-block-id", maximum=128)

    def to_dict(self) -> dict[str, Any]:
        return {
            "locator": self.locator,
            "kind": self.kind,
            "disposition": self.disposition,
            "policy_code": self.policy_code,
            "raw_start": self.raw_start,
            "raw_end": self.raw_end,
            "source_hash": self.source_hash,
            "block_id": self.block_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CoverageEntry:
        value = _mapping(value, "coverage-dto")
        _exact_keys(
            value,
            {
                "locator",
                "kind",
                "disposition",
                "policy_code",
                "raw_start",
                "raw_end",
                "source_hash",
                "block_id",
            },
            "coverage-dto-keys",
        )
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class CoverageWitness:
    entries: tuple[CoverageEntry, ...]
    content_hash: str

    def __post_init__(self) -> None:
        if not self.entries:
            raise BlockContractError("coverage-empty")
        if len(self.entries) > 200_000:
            raise BlockContractError("coverage-too-large")
        expected = sha256_text(
            canonical_json([entry.to_dict() for entry in self.entries])
        )
        if _hash(self.content_hash, "coverage-hash") != expected:
            raise BlockContractError("coverage-hash")

    @classmethod
    def create(cls, entries: tuple[CoverageEntry, ...]) -> CoverageWitness:
        return cls(
            entries=entries,
            content_hash=sha256_text(
                canonical_json([entry.to_dict() for entry in entries])
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "entries": [entry.to_dict() for entry in self.entries],
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CoverageWitness:
        value = _mapping(value, "coverage-witness-dto")
        _exact_keys(value, {"entries", "content_hash"}, "coverage-witness-keys")
        if not isinstance(value["entries"], list):
            raise BlockContractError("coverage-entries")
        return cls(
            entries=tuple(CoverageEntry.from_dict(item) for item in value["entries"]),
            content_hash=value["content_hash"],
        )


@dataclass(frozen=True, slots=True)
class BlockWirePart:
    """A structured source-order reference; never provider-writable markup."""

    kind: str
    ref_id: str

    def __post_init__(self) -> None:
        if self.kind not in {"slot", "token"}:
            raise BlockContractError("wire-kind")
        _string(self.ref_id, "wire-reference", maximum=128)

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "ref_id": self.ref_id}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> BlockWirePart:
        value = _mapping(value, "wire-dto")
        _exact_keys(value, {"kind", "ref_id"}, "wire-dto-keys")
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class TranslationBlock:
    id: str
    order: int
    role: str
    locator: str
    source_start: int
    source_end: int
    projection: str
    projection_hash: str
    slots: tuple[SourceSlot, ...]
    protected_inline: tuple[ProtectedInlineToken, ...]
    wire: tuple[BlockWirePart, ...]
    anchors: tuple[SourceAnchor, ...]

    def __post_init__(self) -> None:
        _string(self.id, "block-id", maximum=128)
        _integer(self.order, "block-order", minimum=1)
        if self.role not in _BLOCK_ROLES:
            raise BlockContractError("block-role")
        _string(self.locator, "block-locator", maximum=2048)
        if _integer(self.source_start, "block-span") >= _integer(
            self.source_end, "block-span"
        ):
            raise BlockContractError("block-span")
        if not isinstance(self.projection, str):
            raise BlockContractError("block-projection")
        if _hash(self.projection_hash, "block-projection-hash") != sha256_text(
            self.projection
        ):
            raise BlockContractError("block-projection-hash")
        if len({slot.id for slot in self.slots}) != len(self.slots):
            raise BlockContractError("block-duplicate-slot")
        if not self.slots or not any(
            slot.disposition == "translatable" for slot in self.slots
        ):
            raise BlockContractError("block-no-writable-slot")
        if any(
            slot.disposition == "excluded-with-reason"
            or slot.projection_start is None
            or slot.projection_end is None
            for slot in self.slots
        ):
            raise BlockContractError("block-slot-projection")
        if len({token.id for token in self.protected_inline}) != len(
            self.protected_inline
        ):
            raise BlockContractError("block-duplicate-inline")
        slot_ids = {slot.id for slot in self.slots}
        token_ids = {token.id for token in self.protected_inline}
        expected = {
            ("slot", slot.id) for slot in self.slots if slot.kind != "break"
        } | {("token", token.id) for token in self.protected_inline}
        actual = [(part.kind, part.ref_id) for part in self.wire]
        if len(actual) != len(set(actual)) or set(actual) != expected:
            raise BlockContractError("block-wire-cardinality")
        if any(
            (part.kind == "slot" and part.ref_id not in slot_ids)
            or (part.kind == "token" and part.ref_id not in token_ids)
            for part in self.wire
        ):
            raise BlockContractError("block-wire-reference")
        if [anchor.order for anchor in self.anchors] != list(
            range(1, len(self.anchors) + 1)
        ):
            raise BlockContractError("block-anchor-order")
        if bool(self.projection) != bool(self.anchors):
            raise BlockContractError("block-anchor-empty")
        if self.anchors and (
            self.anchors[0].start != 0 or self.anchors[-1].end != len(self.projection)
        ):
            raise BlockContractError("block-anchor-coverage")
        if any(
            anchor.source_hash
            != sha256_text(self.projection[anchor.start : anchor.end])
            for anchor in self.anchors
        ):
            raise BlockContractError("block-anchor-hash")
        if any(
            left.end != right.start
            for left, right in zip(self.anchors, self.anchors[1:])
        ):
            raise BlockContractError("block-anchor-coverage")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "order": self.order,
            "role": self.role,
            "locator": self.locator,
            "source_start": self.source_start,
            "source_end": self.source_end,
            "projection": self.projection,
            "projection_hash": self.projection_hash,
            "slots": [slot.to_dict() for slot in self.slots],
            "protected_inline": [token.to_dict() for token in self.protected_inline],
            "wire": [part.to_dict() for part in self.wire],
            "anchors": [anchor.to_dict() for anchor in self.anchors],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> TranslationBlock:
        value = _mapping(value, "block-dto")
        _exact_keys(
            value,
            {
                "id",
                "order",
                "role",
                "locator",
                "source_start",
                "source_end",
                "projection",
                "projection_hash",
                "slots",
                "protected_inline",
                "wire",
                "anchors",
            },
            "block-dto-keys",
        )
        if not all(
            isinstance(value[name], list)
            for name in ("slots", "protected_inline", "wire", "anchors")
        ):
            raise BlockContractError("block-children")
        copy = dict(value)
        copy["slots"] = tuple(SourceSlot.from_dict(item) for item in value["slots"])
        copy["protected_inline"] = tuple(
            ProtectedInlineToken.from_dict(item) for item in value["protected_inline"]
        )
        copy["wire"] = tuple(BlockWirePart.from_dict(item) for item in value["wire"])
        copy["anchors"] = tuple(
            SourceAnchor.from_dict(item) for item in value["anchors"]
        )
        return cls(**copy)


@dataclass(frozen=True, slots=True)
class TranslationSourcePlan:
    snapshot: AttestedDocumentSnapshot
    source_attestation: SourceProviderAttestation
    project_id: str
    planner_version: str
    source_language: str
    language_profile: str
    planner_settings: Mapping[str, Any]
    settings_fingerprint: str
    namespace: str
    blocks: tuple[TranslationBlock, ...]
    coverage: CoverageWitness
    plan_hash: str
    contract_version: str = SOURCE_PLAN_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != SOURCE_PLAN_CONTRACT_VERSION:
            raise BlockContractError("plan-contract-version")
        if (
            not isinstance(self.planner_version, str)
            or self.planner_version not in SUPPORTED_BLOCK_PLANNER_VERSIONS
        ):
            raise BlockContractError("plan-planner-version")
        _string(self.project_id, "plan-project-id", maximum=256)
        _string(self.source_language, "plan-source-language", maximum=64)
        _string(self.language_profile, "plan-language-profile", maximum=128)
        if not isinstance(self.source_attestation, SourceProviderAttestation):
            raise BlockContractError("source-attestation-type")
        if not isinstance(self.planner_settings, Mapping):
            raise BlockContractError("plan-settings")
        object.__setattr__(
            self, "planner_settings", _frozen_planner_settings(self.planner_settings)
        )
        if _hash(self.settings_fingerprint, "plan-settings-fingerprint") != sha256_text(
            canonical_json(_plain_value(self.planner_settings))
        ):
            raise BlockContractError("plan-settings-fingerprint")
        if self.source_attestation.resource_id != self.snapshot.document_id:
            raise BlockContractError("source-attestation-resource")
        _string(self.namespace, "plan-namespace", maximum=128)
        if [block.order for block in self.blocks] != list(
            range(1, len(self.blocks) + 1)
        ):
            raise BlockContractError("plan-block-order")
        if len({block.id for block in self.blocks}) != len(self.blocks):
            raise BlockContractError("plan-duplicate-block")
        if _hash(self.plan_hash, "plan-hash") != self._expected_hash():
            raise BlockContractError("plan-hash")

    def _hash_payload(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "snapshot_identity": {
                "id": self.snapshot.id,
                "document_id": self.snapshot.document_id,
                "snapshot_contract_version": self.snapshot.snapshot_contract_version,
                "semantic_contract_version": self.snapshot.semantic_contract_version,
                "semantic_contract_mode": self.snapshot.semantic_contract_mode,
                "content_hash": self.snapshot.content_hash,
                "semantic_content_hash": self.snapshot.semantic_content_hash,
                **(
                    {"resources": [item.to_dict() for item in self.snapshot.resources]}
                    if self.snapshot.resources
                    else {}
                ),
            },
            "project_id": self.project_id,
            "source_attestation": self.source_attestation.to_dict(),
            "planner_version": self.planner_version,
            "source_language": self.source_language,
            "language_profile": self.language_profile,
            "planner_settings": _plain_value(self.planner_settings),
            "settings_fingerprint": self.settings_fingerprint,
            "namespace": self.namespace,
            "blocks": [block.to_dict() for block in self.blocks],
            "coverage": self.coverage.to_dict(),
        }

    def _expected_hash(self) -> str:
        return sha256_text(canonical_json(self._hash_payload()))

    @classmethod
    def create(cls, **kwargs: Any) -> TranslationSourcePlan:
        provisional = cls.__new__(cls)
        for key, value in kwargs.items():
            object.__setattr__(provisional, key, value)
        object.__setattr__(
            provisional,
            "contract_version",
            kwargs.get("contract_version", SOURCE_PLAN_CONTRACT_VERSION),
        )
        expected = provisional._expected_hash()
        return cls(**kwargs, plan_hash=expected)

    def to_dict(self) -> dict[str, Any]:
        result = {
            "contract_version": self.contract_version,
            "snapshot": self.snapshot.to_dict(),
            "source_attestation": self.source_attestation.to_dict(),
            "project_id": self.project_id,
            "planner_version": self.planner_version,
            "source_language": self.source_language,
            "language_profile": self.language_profile,
            "planner_settings": _plain_value(self.planner_settings),
            "settings_fingerprint": self.settings_fingerprint,
            "namespace": self.namespace,
            "blocks": [block.to_dict() for block in self.blocks],
            "coverage": self.coverage.to_dict(),
        }
        result["plan_hash"] = self.plan_hash
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> TranslationSourcePlan:
        value = _mapping(value, "plan-dto")
        _exact_keys(
            value,
            {
                "contract_version",
                "snapshot",
                "source_attestation",
                "project_id",
                "planner_version",
                "source_language",
                "language_profile",
                "planner_settings",
                "settings_fingerprint",
                "namespace",
                "blocks",
                "coverage",
                "plan_hash",
            },
            "plan-dto-keys",
        )
        if not isinstance(value["blocks"], list):
            raise BlockContractError("plan-blocks")
        return cls(
            snapshot=AttestedDocumentSnapshot.from_dict(value["snapshot"]),
            source_attestation=SourceProviderAttestation.from_dict(
                value["source_attestation"]
            ),
            project_id=value["project_id"],
            planner_version=value["planner_version"],
            source_language=value["source_language"],
            language_profile=value["language_profile"],
            planner_settings=_mapping(value["planner_settings"], "plan-settings"),
            settings_fingerprint=value["settings_fingerprint"],
            namespace=value["namespace"],
            blocks=tuple(TranslationBlock.from_dict(item) for item in value["blocks"]),
            coverage=CoverageWitness.from_dict(value["coverage"]),
            plan_hash=value["plan_hash"],
            contract_version=value["contract_version"],
        )
