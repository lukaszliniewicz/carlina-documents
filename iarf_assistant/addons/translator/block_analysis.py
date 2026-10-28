"""Quality-first map/reduce analysis for semantic document blocks.

The block workflow treats analysis as evidence about a pinned source plan, not
as a replacement for that plan.  This module therefore keeps the surface
deliberately small and strict:

* scopes contain ordered, whole block IDs and a conservative, explicitly
  derived heading context;
* a map response must be JSON and every finding must link back to blocks in
  its scope; and
* reduction emits the existing context-artifact payloads without adding model
  fields to those payloads.  Glossary material remains a proposal until a
  separate human-approved glossary workflow accepts it.

Provider calls and crash recovery use the generic analysis-attempt ledger.  No
analysis state is inferred from a completed-chunks counter or kept in a
module-level cache.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, cast

from iarf_assistant.extensions import db

from .analysis_attempts import (
    ANALYSIS_CONTRACT_VERSION_V1,
    rehydrate_analysis_attempt,
)
from .analysis_contract import ContextArtifactPayload
from .analysis_execution import (
    execute_analysis_attempt,
    prepare_and_commit_analysis_attempt,
    reconcile_dispatched_analysis_attempt,
    resume_analysis_attempt,
)
from .analysis_persistence import (
    persist_context_artifact_revision,
    rehydrate_context_artifact_revision,
)
from .block_contract import (
    BlockContractError,
    TranslationBlock,
    TranslationSourcePlan,
    canonical_json,
    sha256_text,
)
from .block_persistence import rehydrate_source_plan
from .block_provider_adapter import PROVIDER_DEFAULT_OUTPUT_POLICY, call_analysis
from .compact_markup import compact_source_context_block
from .glossary_persistence import persist_analysis_glossary_proposals
from .models import (
    TranslationAnalysisAttempt,
    TranslationContextArtifactRevision,
)

ANALYSIS_MAP_SCHEMA_VERSION = "analysis-map-v1"
ANALYSIS_REDUCE_SCHEMA_VERSION = "analysis-reduce-v1"
ANALYSIS_PROFILE_ID = "semantic-block-analysis-map-v1"
ANALYSIS_PROFILE_VERSION = "3"
HEADING_PATH_SOURCE = "derived-ordered-heading-boundaries-v1"
ANALYSIS_KIND = "document_brief"
ANALYSIS_REDUCE_KIND = "terminology_consolidation"
# A defensive envelope, not a planning budget.  Real admission is governed by
# complete-block byte bounds and the selected model's measured token capacity.
MAX_SCOPE_BLOCKS = 65_536
MAX_SCOPE_BYTES = 2 * 1024 * 1024
MAX_MAP_BYTES = 512 * 1024
MAX_REDUCE_BYTES = 2 * 1024 * 1024
MAX_REDUCE_PACKET_BYTES = 2 * 1024 * 1024
MAX_HEADING_PATH = 32
MAX_SOURCE_BLOCKS = 128
MAX_LIST_ITEMS = 2_048
_RETRYABLE_TERMINAL_ANALYSIS_STATES = frozenset(
    {
        "provider_failed",
        "provider_outcome_uncertain",
        "fence_failed",
        "cancelled",
        "failed",
    }
)


@dataclass(frozen=True, slots=True)
class AnalysisAttemptDiscovery:
    """One exact, witnessed analysis attempt selected without time heuristics.

    ``executable_under_current_generation`` is deliberately true only for a
    current-generation prepared request. A completed response is immutable
    evidence and can be reused after a later safe-boundary operation-version
    update; a merely response-received attempt still needs its original live
    fence to finalize. ``retry_lineage_attempt_id`` is populated only for a
    terminal state accepted by generic attempt persistence. A ``dispatched``
    attempt is never resumable: callers must reconcile it to an explicit
    uncertain terminal state before deciding whether to make a fresh retry
    attempt.
    """

    attempt: TranslationAnalysisAttempt | None
    retry_lineage_attempt_id: str | None
    executable_under_current_generation: bool
    resumable: bool
    reconciliation_required: bool


# This schema is deliberately kept beside the DTO validator.  It is used only
# when the frozen capability profile proves native JSON Schema support; a
# strict-text provider receives no response_format at all.
ANALYSIS_MAP_JSON_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "source_plan_hash",
        "part_index",
        "block_ids",
        "summary",
        "style",
        "entities",
        "terminology_candidates",
        "unresolved",
    ],
    "properties": {
        "schema_version": {"type": "string", "const": ANALYSIS_MAP_SCHEMA_VERSION},
        "source_plan_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "part_index": {"type": "integer", "minimum": 1},
        "block_ids": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string"},
        },
        "summary": {
            "type": "object",
            "additionalProperties": False,
            "required": ["text", "domains", "audience", "source_block_ids"],
            "properties": {
                "text": {"type": "string"},
                "domains": {"type": "array", "items": {"type": "string"}},
                "audience": {"type": "string"},
                "source_block_ids": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                },
            },
        },
        "style": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "register",
                "guidance",
                "prohibitions",
                "source_block_ids",
            ],
            "properties": {
                "register": {"type": "string"},
                "guidance": {"type": "array", "items": {"type": "string"}},
                "prohibitions": {"type": "array", "items": {"type": "string"}},
                "source_block_ids": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                },
            },
        },
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "type", "aliases", "source_block_ids"],
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "source_block_ids": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string"},
                    },
                },
            },
        },
        "terminology_candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "source_term",
                    "variants",
                    "proposed_target",
                    "definition",
                    "source_block_ids",
                    "confidence",
                ],
                "properties": {
                    "source_term": {"type": "string"},
                    "variants": {"type": "array", "items": {"type": "string"}},
                    "proposed_target": {"type": ["string", "null"]},
                    "definition": {"type": "string"},
                    "source_block_ids": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string"},
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        },
        "unresolved": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["question", "reason", "impact", "source_block_ids"],
                "properties": {
                    "question": {"type": "string"},
                    "reason": {"type": "string"},
                    "impact": {"type": "string"},
                    "source_block_ids": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string"},
                    },
                },
            },
        },
    },
}

ANALYSIS_REDUCE_JSON_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "source_plan_hash",
        "document_brief",
        "style_guide",
        "entity_register",
        "glossary_proposals",
        "unresolved",
        "scope_hashes",
    ],
    "properties": {
        "schema_version": {"type": "string", "const": ANALYSIS_REDUCE_SCHEMA_VERSION},
        "source_plan_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "document_brief": {
            "type": "object",
            "additionalProperties": False,
            "required": ["summary", "domains", "audience"],
            "properties": {
                "summary": {"type": "string"},
                "domains": {"type": "array", "items": {"type": "string"}},
                "audience": {"type": "string"},
            },
        },
        "style_guide": {
            "type": "object",
            "additionalProperties": False,
            "required": ["register", "guidance", "prohibitions"],
            "properties": {
                "register": {"type": "string"},
                "guidance": {"type": "array", "items": {"type": "string"}},
                "prohibitions": {"type": "array", "items": {"type": "string"}},
            },
        },
        "entity_register": {
            "type": "object",
            "additionalProperties": False,
            "required": ["entities"],
            "properties": {
                "entities": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["name", "type", "aliases"],
                        "properties": {
                            "name": {"type": "string"},
                            "type": {"type": "string"},
                            "aliases": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                }
            },
        },
        "glossary_proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "source_term",
                    "target_term",
                    "notes",
                    "scope",
                    "source_block_ids",
                    "confidence",
                    "status",
                ],
                "properties": {
                    "source_term": {"type": "string"},
                    "target_term": {"type": ["string", "null"]},
                    "notes": {"type": "string"},
                    "scope": {"type": "string", "const": "document"},
                    "source_block_ids": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string"},
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "status": {"type": "string", "const": "proposed"},
                },
            },
        },
        "unresolved": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["question", "reason", "impact", "source_block_ids"],
                "properties": {
                    "question": {"type": "string"},
                    "reason": {"type": "string"},
                    "impact": {"type": "string"},
                    "source_block_ids": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string"},
                    },
                },
            },
        },
        "scope_hashes": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        },
    },
}


def _json(value: object, code: str, *, maximum: int) -> Any:
    """Canonical JSON round-trip with no coercion, NaN, or unbounded input."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        restored = json.loads(encoded)
    except (RecursionError, TypeError, ValueError) as exc:
        raise BlockContractError(code) from exc
    if len(encoded) > maximum or restored != value:
        raise BlockContractError(code)
    return restored


def _mapping(
    value: object, code: str, *, maximum: int = MAX_MAP_BYTES
) -> dict[str, Any]:
    material = _json(value, code, maximum=maximum)
    if not isinstance(material, dict):
        raise BlockContractError(code)
    return cast(dict[str, Any], material)


def _string(
    value: object,
    code: str,
    *,
    allow_blank: bool = False,
    maximum: int = 16_384,
) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > maximum:
        raise BlockContractError(code)
    if not allow_blank and not value.strip():
        raise BlockContractError(code)
    return value


def _hash(value: object, code: str) -> str:
    result = _string(value, code, maximum=64)
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise BlockContractError(code)
    return result


def _positive_int(value: object, code: str, *, maximum: int = 1_000_000) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 1
        or value > maximum
    ):
        raise BlockContractError(code)
    return value


def _provider_output_limit(
    packet: Mapping[str, Any], *, budget_code: str = "analysis-budget"
) -> int | None:
    """Replay legacy caps while current attempts use the provider default."""

    budget = _mapping(packet.get("budget"), "analysis-budget")
    reserve = _positive_int(
        budget.get("output_tokens"), budget_code, maximum=1_000_000
    )
    profile = _mapping(packet.get("profile"), "analysis-profile")
    version = profile.get("profile_version")
    policy = profile.get("provider_output_policy")
    if version == "1" and policy is None:
        return reserve
    if version in {"2", ANALYSIS_PROFILE_VERSION} and policy == PROVIDER_DEFAULT_OUTPUT_POLICY:
        return None
    raise BlockContractError("analysis-provider-output-policy")


def _strings(value: object, code: str, *, maximum: int = 128) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise BlockContractError(code)
    return [_string(item, code, maximum=16_384) for item in value]


@dataclass(frozen=True, slots=True)
class HeadingPathEntry:
    """One heading included as context, never a claimed semantic level.

    The block planner currently persists heading blocks but not heading depth.
    Keeping ``derived`` and ``source`` explicit prevents an analyst from
    mistaking this ordered context for a repaired or inferred outline.
    """

    block_id: str
    text: str
    derived: bool = True
    source: str = HEADING_PATH_SOURCE

    def __post_init__(self) -> None:
        _string(self.block_id, "analysis-heading-id", maximum=128)
        _string(self.text, "analysis-heading-text", allow_blank=True, maximum=16_384)
        if self.derived is not True or self.source != HEADING_PATH_SOURCE:
            raise BlockContractError("analysis-heading-provenance")

    def to_dict(self) -> dict[str, object]:
        return {
            "block_id": self.block_id,
            "text": self.text,
            "derived": True,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> HeadingPathEntry:
        if set(value) != {"block_id", "text", "derived", "source"}:
            raise BlockContractError("analysis-heading-keys")
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class AnalysisScope:
    """A deterministic map unit containing only complete source blocks."""

    source_plan_hash: str
    part_index: int
    block_ids: tuple[str, ...]
    heading_path: tuple[HeadingPathEntry, ...] = ()
    source_bytes: int = 0
    heading_path_source: str = HEADING_PATH_SOURCE

    def __post_init__(self) -> None:
        _hash(self.source_plan_hash, "analysis-scope-plan-hash")
        _positive_int(self.part_index, "analysis-scope-part-index")
        if not isinstance(self.block_ids, tuple) or not self.block_ids:
            raise BlockContractError("analysis-scope-blocks")
        if len(self.block_ids) > MAX_SCOPE_BLOCKS:
            raise BlockContractError("analysis-scope-blocks")
        for block_id in self.block_ids:
            _string(block_id, "analysis-scope-block-id", maximum=128)
        if len(set(self.block_ids)) != len(self.block_ids):
            raise BlockContractError("analysis-scope-blocks")
        if (
            not isinstance(self.heading_path, tuple)
            or len(self.heading_path) > MAX_HEADING_PATH
        ):
            raise BlockContractError("analysis-scope-heading-path")
        if not all(isinstance(item, HeadingPathEntry) for item in self.heading_path):
            raise BlockContractError("analysis-scope-heading-path")
        if self.heading_path_source != HEADING_PATH_SOURCE:
            raise BlockContractError("analysis-scope-heading-path")
        if (
            not isinstance(self.source_bytes, int)
            or isinstance(self.source_bytes, bool)
            or self.source_bytes < 1
            or self.source_bytes > MAX_SCOPE_BYTES
        ):
            raise BlockContractError("analysis-scope-size")

    @property
    def scope_hash(self) -> str:
        return sha256_text(canonical_json(self.to_dict()))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": ANALYSIS_MAP_SCHEMA_VERSION,
            "source_plan_hash": self.source_plan_hash,
            "part_index": self.part_index,
            "block_ids": list(self.block_ids),
            "heading_path": [item.to_dict() for item in self.heading_path],
            "heading_path_source": self.heading_path_source,
            "source_bytes": self.source_bytes,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AnalysisScope:
        material = _mapping(value, "analysis-scope", maximum=MAX_MAP_BYTES)
        if set(material) != {
            "schema_version",
            "source_plan_hash",
            "part_index",
            "block_ids",
            "heading_path",
            "heading_path_source",
            "source_bytes",
        }:
            raise BlockContractError("analysis-scope-keys")
        if material["schema_version"] != ANALYSIS_MAP_SCHEMA_VERSION:
            raise BlockContractError("analysis-scope-version")
        block_ids = material["block_ids"]
        heading_path = material["heading_path"]
        if not isinstance(block_ids, list) or not isinstance(heading_path, list):
            raise BlockContractError("analysis-scope-children")
        return cls(
            source_plan_hash=material["source_plan_hash"],
            part_index=material["part_index"],
            block_ids=tuple(block_ids),
            heading_path=tuple(
                HeadingPathEntry.from_dict(item) for item in heading_path
            ),
            source_bytes=material["source_bytes"],
            heading_path_source=material["heading_path_source"],
        )


def _heading_entry(block: TranslationBlock) -> HeadingPathEntry:
    return HeadingPathEntry(block_id=block.id, text=block.projection)


def _analysis_source_block(block: TranslationBlock) -> dict[str, object]:
    """Project one block into language-first markup plus its durable citation ID."""

    compact = compact_source_context_block(block)
    return {
        "block_id": block.id,
        "role": compact["role"],
        "content": compact["content"],
        "fields": compact["fields"],
    }


def _block_size(block: TranslationBlock) -> int:
    # This is an admission estimate over the same language-first projection the
    # provider receives.  Exact request-token admission remains final.
    return len(canonical_json(_analysis_source_block(block)).encode("utf-8"))


def _scope_size(
    blocks: Sequence[TranslationBlock], path: Sequence[HeadingPathEntry]
) -> int:
    return len(
        canonical_json(
            {
                "blocks": [block.id for block in blocks],
                "heading_path": [item.to_dict() for item in path],
            }
        ).encode("utf-8")
    ) + sum(_block_size(block) for block in blocks)


def build_analysis_scopes(
    plan: TranslationSourcePlan,
    *,
    max_input_bytes: int = 512 * 1024,
    context_bytes: int = 16 * 1024,
) -> tuple[AnalysisScope, ...]:
    """Build source-ordered scopes with heading boundaries and block-only splits.

    The planner does not infer heading levels.  A heading starts a new
    boundary and the path contains the most recent ordered heading context;
    every entry is marked ``derived`` so it cannot be treated as source
    metadata.  A block that cannot fit alone fails with the stable
    ``analysis-oversized-block`` code.
    """

    if not isinstance(plan, TranslationSourcePlan):
        raise BlockContractError("analysis-plan")
    if (
        not isinstance(max_input_bytes, int)
        or isinstance(max_input_bytes, bool)
        or max_input_bytes < 1
        or max_input_bytes > MAX_SCOPE_BYTES
        or not isinstance(context_bytes, int)
        or isinstance(context_bytes, bool)
        or context_bytes < 0
        or context_bytes >= max_input_bytes
    ):
        raise BlockContractError("analysis-scope-budget")
    blocks = tuple(plan.blocks)
    if not blocks:
        raise BlockContractError("analysis-no-blocks")

    groups: list[list[TranslationBlock]] = []
    current: list[TranslationBlock] = []
    for block in blocks:
        if block.role == "heading" and current:
            groups.append(current)
            current = []
        current.append(block)
    if current:
        groups.append(current)

    raw_parts: list[tuple[list[TranslationBlock], tuple[HeadingPathEntry, ...]]] = []
    heading_history: list[HeadingPathEntry] = []
    for group in groups:
        group_heading_history = list(heading_history)
        pending: list[TranslationBlock] = []
        pending_path: tuple[HeadingPathEntry, ...] = tuple(group_heading_history)
        for block in group:
            if block.role == "heading":
                group_heading_history.append(_heading_entry(block))
            candidate = [*pending, block]
            path = tuple(group_heading_history[-MAX_HEADING_PATH:])
            if _scope_size(candidate, path) + context_bytes <= max_input_bytes:
                if not pending:
                    pending_path = path
                pending = candidate
                continue
            if not pending:
                raise BlockContractError("analysis-oversized-block")
            raw_parts.append((pending, pending_path))
            pending = [block]
            pending_path = path
            if _scope_size(pending, pending_path) + context_bytes > max_input_bytes:
                raise BlockContractError("analysis-oversized-block")
        if pending:
            # A heading boundary is a quality boundary.  Do not infer a level
            # for it, but retain it as the current ordered context for later
            # parts of the same section.
            raw_parts.append((pending, pending_path))
        heading_history = group_heading_history[-MAX_HEADING_PATH:]

    scopes: list[AnalysisScope] = []
    for index, (part, path) in enumerate(raw_parts, 1):
        scopes.append(
            AnalysisScope(
                source_plan_hash=plan.plan_hash,
                part_index=index,
                block_ids=tuple(block.id for block in part),
                heading_path=path,
                source_bytes=_scope_size(part, path),
            )
        )
    return tuple(scopes)


def whole_document_analysis_scope(plan: TranslationSourcePlan) -> AnalysisScope:
    """Build one complete source scope when the deterministic guards permit it."""

    if not isinstance(plan, TranslationSourcePlan) or not plan.blocks:
        raise BlockContractError("analysis-plan")
    return analysis_scope_from_blocks(plan, plan.blocks, part_index=1)


def analysis_scope_from_blocks(
    plan: TranslationSourcePlan,
    blocks: Sequence[TranslationBlock],
    *,
    part_index: int,
    heading_path: Sequence[HeadingPathEntry] = (),
) -> AnalysisScope:
    """Create one witnessed scope from an ordered complete-block slice."""

    if not isinstance(plan, TranslationSourcePlan):
        raise BlockContractError("analysis-plan")
    chosen = tuple(blocks)
    path = tuple(heading_path)
    if not chosen or len(chosen) > MAX_SCOPE_BLOCKS:
        raise BlockContractError("analysis-scope-blocks")
    plan_ids = {block.id for block in plan.blocks}
    if any(block.id not in plan_ids for block in chosen):
        raise BlockContractError("analysis-scope-block")
    orders = [block.order for block in chosen]
    if orders != sorted(orders) or any(
        right != left + 1 for left, right in pairwise(orders)
    ):
        raise BlockContractError("analysis-scope-order")
    source_bytes = _scope_size(chosen, path)
    if source_bytes > MAX_SCOPE_BYTES:
        raise BlockContractError("analysis-scope-size")
    return AnalysisScope(
        source_plan_hash=plan.plan_hash,
        part_index=part_index,
        block_ids=tuple(block.id for block in chosen),
        heading_path=path,
        source_bytes=source_bytes,
    )


def source_blocks_for_scope(
    plan: TranslationSourcePlan, scope: AnalysisScope
) -> list[dict[str, object]]:
    """Materialize the exact, ordered source projection admitted to a scope."""

    if scope.source_plan_hash != plan.plan_hash:
        raise BlockContractError("analysis-scope-plan")
    by_id = {block.id: block for block in plan.blocks}
    if any(block_id not in by_id for block_id in scope.block_ids):
        raise BlockContractError("analysis-scope-block")
    blocks = [by_id[block_id] for block_id in scope.block_ids]
    orders = [block.order for block in blocks]
    if orders != sorted(orders) or any(
        right != left + 1 for left, right in pairwise(orders)
    ):
        raise BlockContractError("analysis-scope-order")
    heading_by_id = {
        block.id: block for block in plan.blocks if block.role == "heading"
    }
    heading_orders = [
        heading_by_id[item.block_id].order
        if item.block_id in heading_by_id
        and item.text == heading_by_id[item.block_id].projection
        else -1
        for item in scope.heading_path
    ]
    if any(order < 1 for order in heading_orders) or heading_orders != sorted(
        heading_orders
    ):
        raise BlockContractError("analysis-scope-heading-path")
    if heading_orders and heading_orders[-1] > blocks[0].order:
        raise BlockContractError("analysis-scope-heading-path")
    result: list[dict[str, object]] = []
    for block in blocks:
        result.append(_analysis_source_block(block))
    return cast(
        list[dict[str, object]],
        _json(result, "analysis-source-blocks", maximum=MAX_SCOPE_BYTES),
    )


def build_analysis_packet(
    plan: TranslationSourcePlan,
    scope: AnalysisScope,
    *,
    phase: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build the encrypted generic-attempt packet for one map scope."""

    source_blocks = source_blocks_for_scope(plan, scope)
    phase_value = _mapping(phase or {}, "analysis-phase", maximum=MAX_MAP_BYTES)
    transport, response_format = _map_transport(phase)
    prompt = (
        "You are the semantic document-analysis mapper. Analyze only the pinned "
        "source blocks supplied below. Return one JSON object matching "
        "analysis-map-v1 exactly. Keep every observation source-linked by block "
        "ID; distinguish evidence from inference, never invent facts, and put "
        "uncertainty in unresolved. Do not translate, rewrite, split, merge, or "
        "emit source markup. Each source block uses natural inline XML only to "
        "preserve meaningful formatting and references; attend to its language, "
        "not the markup scaffolding. Heading context is derived context, not a claimed "
        "heading level. In summary, audience is one concise non-empty string, "
        "never an array or object. Do not call tools."
    )
    if transport == "strict_text_json":
        prompt += (
            " Output strict JSON only, with no Markdown fences, commentary, or "
            "additional keys."
        )
    else:
        prompt += " The native response schema is enforced; still emit no commentary."
    user_payload: dict[str, object] = {
        "scope": scope.to_dict(),
        "source_blocks": source_blocks,
    }
    if transport == "strict_text_json":
        # Text-only models cannot infer a private contract name. Give them the
        # same closed schema native-capability models receive, plus the exact
        # lineage values they must echo. This material is part of the sealed
        # request and therefore participates in token admission and replay.
        user_payload["response_contract"] = {
            "json_schema": ANALYSIS_MAP_JSON_SCHEMA,
            "exact_values": {
                "schema_version": ANALYSIS_MAP_SCHEMA_VERSION,
                "source_plan_hash": scope.source_plan_hash,
                "part_index": scope.part_index,
                "block_ids": list(scope.block_ids),
            },
        }
    packet = {
        "messages": [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": json.dumps(
                    user_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        ],
        "input": {
            "phase": phase_value,
            "scope": scope.to_dict(),
            "source_plan_hash": plan.plan_hash,
            "source_blocks": source_blocks,
            "output_schema": ANALYSIS_MAP_SCHEMA_VERSION,
            "census_profile": "semantic-census-v1",
            "transport": transport,
            "response_format": response_format,
        },
    }
    return cast(
        dict[str, object], _json(packet, "analysis-packet", maximum=MAX_SCOPE_BYTES)
    )


def _source_ids(value: object, scope: AnalysisScope, code: str) -> list[str]:
    ids = _strings(value, code, maximum=len(scope.block_ids))
    if not ids or any(item not in scope.block_ids for item in ids):
        raise BlockContractError(code)
    if len(set(ids)) != len(ids):
        raise BlockContractError(code)
    return ids


def _confidence(value: object) -> float:
    if not isinstance(value, (float, int)) or isinstance(value, bool):
        raise BlockContractError("analysis-map-confidence")
    confidence = float(value)
    if confidence < 0 or confidence > 1:
        raise BlockContractError("analysis-map-confidence")
    return confidence


@dataclass(frozen=True, slots=True)
class AnalysisMapResult:
    source_plan_hash: str
    part_index: int
    block_ids: tuple[str, ...]
    summary: dict[str, object]
    style: dict[str, object]
    entities: tuple[dict[str, object], ...]
    terminology_candidates: tuple[dict[str, object], ...]
    unresolved: tuple[dict[str, object], ...]

    def __post_init__(self) -> None:
        _hash(self.source_plan_hash, "analysis-map-plan-hash")
        _positive_int(self.part_index, "analysis-map-part-index")
        if not isinstance(self.block_ids, tuple) or not self.block_ids:
            raise BlockContractError("analysis-map-blocks")
        for item in self.block_ids:
            _string(item, "analysis-map-block-id", maximum=128)
        if len(set(self.block_ids)) != len(self.block_ids):
            raise BlockContractError("analysis-map-blocks")
        if not isinstance(self.summary, dict) or not isinstance(self.style, dict):
            raise BlockContractError("analysis-map-content")
        _validate_summary(self.summary, self)
        _validate_style(self.style, self)
        if len(self.entities) > MAX_LIST_ITEMS:
            raise BlockContractError("analysis-map-entities")
        if len(self.terminology_candidates) > MAX_LIST_ITEMS:
            raise BlockContractError("analysis-map-terms")
        if len(self.unresolved) > MAX_LIST_ITEMS:
            raise BlockContractError("analysis-map-unresolved")
        for item in self.entities:
            _validate_entity(item, self)
        for item in self.terminology_candidates:
            _validate_term(item, self)
        for item in self.unresolved:
            _validate_unresolved(item, self)
        _json(self.to_dict(), "analysis-map-size", maximum=MAX_MAP_BYTES)

    @property
    def scope_hash(self) -> str:
        return sha256_text(
            canonical_json(
                {
                    "source_plan_hash": self.source_plan_hash,
                    "part_index": self.part_index,
                    "block_ids": list(self.block_ids),
                }
            )
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": ANALYSIS_MAP_SCHEMA_VERSION,
            "source_plan_hash": self.source_plan_hash,
            "part_index": self.part_index,
            "block_ids": list(self.block_ids),
            "summary": dict(self.summary),
            "style": dict(self.style),
            "entities": [dict(item) for item in self.entities],
            "terminology_candidates": [
                dict(item) for item in self.terminology_candidates
            ],
            "unresolved": [dict(item) for item in self.unresolved],
        }

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], scope: AnalysisScope
    ) -> AnalysisMapResult:
        material = _mapping(value, "analysis-map", maximum=MAX_MAP_BYTES)
        if set(material) != {
            "schema_version",
            "source_plan_hash",
            "part_index",
            "block_ids",
            "summary",
            "style",
            "entities",
            "terminology_candidates",
            "unresolved",
        }:
            raise BlockContractError("analysis-map-keys")
        if material["schema_version"] != ANALYSIS_MAP_SCHEMA_VERSION:
            raise BlockContractError("analysis-map-version")
        block_ids = material["block_ids"]
        entities = material["entities"]
        terms = material["terminology_candidates"]
        unresolved = material["unresolved"]
        if not all(
            isinstance(item, list) for item in (block_ids, entities, terms, unresolved)
        ):
            raise BlockContractError("analysis-map-children")
        if (
            material["source_plan_hash"] != scope.source_plan_hash
            or material["part_index"] != scope.part_index
        ):
            raise BlockContractError("analysis-map-scope")
        if block_ids != list(scope.block_ids):
            raise BlockContractError("analysis-map-scope")
        summary = dict(_mapping(material["summary"], "analysis-map-summary"))
        summary["audience"] = _canonical_audience(summary.get("audience"))
        return cls(
            source_plan_hash=material["source_plan_hash"],
            part_index=material["part_index"],
            block_ids=tuple(block_ids),
            summary=summary,
            style=dict(_mapping(material["style"], "analysis-map-style")),
            entities=tuple(_mapping(item, "analysis-map-entity") for item in entities),
            terminology_candidates=tuple(
                _mapping(item, "analysis-map-term") for item in terms
            ),
            unresolved=tuple(
                _mapping(item, "analysis-map-unresolved") for item in unresolved
            ),
        )

    @classmethod
    def from_response(cls, response: object, scope: AnalysisScope) -> AnalysisMapResult:
        if isinstance(response, Mapping) and "schema_version" in response:
            return cls.from_dict(response, scope)
        if isinstance(response, Mapping) and set(response) == {"role", "content"}:
            content = response["content"]
        elif isinstance(response, Mapping) and set(response) == {
            "role",
            "content",
            "tool_calls",
        }:
            raise BlockContractError("analysis-map-tool-call")
        else:
            content = response
        if not isinstance(content, str):
            raise BlockContractError("analysis-map-content")
        try:
            parsed = json.loads(content)
        except (TypeError, ValueError, RecursionError) as exc:
            raise BlockContractError("analysis-map-json") from exc
        if not isinstance(parsed, Mapping):
            raise BlockContractError("analysis-map-json")
        return cls.from_dict(parsed, scope)


def _canonical_audience(value: object) -> str:
    """Losslessly canonicalize a common text-model audience-list variation."""

    if isinstance(value, str):
        return _string(value, "analysis-map-summary", maximum=4_096)
    if not isinstance(value, list):
        raise BlockContractError("analysis-map-summary")
    items = _strings(value, "analysis-map-summary", maximum=32)
    if not items:
        raise BlockContractError("analysis-map-summary")
    audience = "; ".join(dict.fromkeys(item.strip() for item in items))
    return _string(audience, "analysis-map-summary", maximum=4_096)


def _validate_summary(value: Mapping[str, object], result: AnalysisMapResult) -> None:
    if set(value) != {"text", "domains", "audience", "source_block_ids"}:
        raise BlockContractError("analysis-map-summary")
    _string(value["text"], "analysis-map-summary", maximum=64 * 1024)
    _strings(value["domains"], "analysis-map-summary", maximum=32)
    _string(value["audience"], "analysis-map-summary", maximum=4_096)
    _source_ids(
        value["source_block_ids"],
        result_scope_from_result(result),
        "analysis-map-summary",
    )


def result_scope_from_result(result: AnalysisMapResult) -> AnalysisScope:
    """Create the minimal scope used to validate source links in a map DTO."""

    # Map DTOs intentionally retain no heading text.  A zero source-byte value
    # is not passed through AnalysisScope.__post_init__; this helper only
    # validates links, so use the smallest valid witness size.
    return AnalysisScope(
        source_plan_hash=result.source_plan_hash,
        part_index=result.part_index,
        block_ids=result.block_ids,
        source_bytes=1,
    )


def _validate_style(value: Mapping[str, object], result: AnalysisMapResult) -> None:
    if set(value) != {"register", "guidance", "prohibitions", "source_block_ids"}:
        raise BlockContractError("analysis-map-style")
    _string(value["register"], "analysis-map-style", maximum=1_024)
    _strings(value["guidance"], "analysis-map-style", maximum=256)
    _strings(value["prohibitions"], "analysis-map-style", maximum=256)
    _source_ids(
        value["source_block_ids"],
        result_scope_from_result(result),
        "analysis-map-style",
    )


def _validate_entity(value: Mapping[str, object], result: AnalysisMapResult) -> None:
    if set(value) != {"name", "type", "aliases", "source_block_ids"}:
        raise BlockContractError("analysis-map-entity")
    _string(value["name"], "analysis-map-entity", maximum=4_096)
    _string(value["type"], "analysis-map-entity", maximum=1_024)
    _strings(value["aliases"], "analysis-map-entity", maximum=64)
    _source_ids(
        value["source_block_ids"],
        result_scope_from_result(result),
        "analysis-map-entity",
    )


def _validate_term(value: Mapping[str, object], result: AnalysisMapResult) -> None:
    if set(value) != {
        "source_term",
        "variants",
        "proposed_target",
        "definition",
        "source_block_ids",
        "confidence",
    }:
        raise BlockContractError("analysis-map-term")
    _string(value["source_term"], "analysis-map-term", maximum=4_096)
    _strings(value["variants"], "analysis-map-term", maximum=64)
    proposed = value["proposed_target"]
    if proposed is not None:
        _string(proposed, "analysis-map-term", maximum=4_096)
    _string(value["definition"], "analysis-map-term", allow_blank=True, maximum=16_384)
    _source_ids(
        value["source_block_ids"], result_scope_from_result(result), "analysis-map-term"
    )
    _confidence(value["confidence"])


def _validate_unresolved(
    value: Mapping[str, object], result: AnalysisMapResult
) -> None:
    if set(value) != {"question", "reason", "impact", "source_block_ids"}:
        raise BlockContractError("analysis-map-unresolved")
    _string(value["question"], "analysis-map-unresolved", maximum=8_192)
    _string(value["reason"], "analysis-map-unresolved", maximum=8_192)
    _string(value["impact"], "analysis-map-unresolved", maximum=1_024)
    _source_ids(
        value["source_block_ids"],
        result_scope_from_result(result),
        "analysis-map-unresolved",
    )


def parse_analysis_map_result(
    response: object, scope: AnalysisScope
) -> AnalysisMapResult:
    return AnalysisMapResult.from_response(response, scope)


def _ordered_map_results(
    results: Sequence[AnalysisMapResult],
) -> tuple[AnalysisMapResult, ...]:
    if not results:
        raise BlockContractError("analysis-reduce-empty")
    ordered = tuple(results)
    if any(not isinstance(item, AnalysisMapResult) for item in ordered):
        raise BlockContractError("analysis-reduce-map-result")
    if [item.part_index for item in ordered] != list(range(1, len(ordered) + 1)):
        raise BlockContractError("analysis-reduce-order")
    source_plan_hash = ordered[0].source_plan_hash
    if any(item.source_plan_hash != source_plan_hash for item in ordered):
        raise BlockContractError("analysis-reduce-plan")
    seen_blocks: set[str] = set()
    for item in ordered:
        if seen_blocks.intersection(item.block_ids):
            raise BlockContractError("analysis-reduce-block-overlap")
        seen_blocks.update(item.block_ids)
    return ordered


def _mapped_block_ids(results: Sequence[AnalysisMapResult]) -> tuple[str, ...]:
    ordered = _ordered_map_results(results)
    seen: set[str] = set()
    block_ids: list[str] = []
    for result in ordered:
        for block_id in result.block_ids:
            if block_id not in seen:
                seen.add(block_id)
                block_ids.append(block_id)
    if not block_ids:
        raise BlockContractError("analysis-reduce-blocks")
    return tuple(block_ids)


def _structured_transport(
    phase: Mapping[str, object] | None,
    *,
    schema_name: str,
    schema: Mapping[str, object],
) -> tuple[str, dict[str, object] | None]:
    """Choose transport solely from the frozen capability profile.

    JSON mode is deliberately not treated as native schema support.  Models
    with absent, unknown, or non-``json_schema`` capabilities receive strict
    text instructions and ``response_format=None``.
    """

    phase_data = _mapping(phase or {}, "analysis-phase")
    model = phase_data.get("model")
    capability = model.get("capability_profile") if isinstance(model, Mapping) else None
    capability_data = (
        _mapping(capability, "analysis-structured-capability")
        if capability is not None
        else {}
    )
    native = (
        capability_data.get("supports_structured_output") is True
        and capability_data.get("structured_output_mode") == "json_schema"
    )
    if not native:
        return "strict_text_json", None
    return (
        "native_json_schema",
        {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": dict(schema),
            },
        },
    )


def _map_transport(
    phase: Mapping[str, object] | None,
) -> tuple[str, dict[str, object] | None]:
    return _structured_transport(
        phase,
        schema_name="translator_analysis_map_v1",
        schema=ANALYSIS_MAP_JSON_SCHEMA,
    )


def _reduce_transport(
    phase: Mapping[str, object] | None,
) -> tuple[str, dict[str, object] | None]:
    return _structured_transport(
        phase,
        schema_name="translator_analysis_reduce_v1",
        schema=ANALYSIS_REDUCE_JSON_SCHEMA,
    )


def build_reduce_packet(
    results: Sequence[AnalysisMapResult],
    *,
    phase: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build the exact consolidation request from ordered map evidence."""

    ordered = _ordered_map_results(results)
    source_plan_hash = ordered[0].source_plan_hash
    scope_hashes = [item.scope_hash for item in ordered]
    mapped_block_ids = list(_mapped_block_ids(ordered))
    transport, response_format = _reduce_transport(phase)
    prompt = (
        "You are the quality-first semantic document-analysis consolidator. "
        "Use only the ordered witnessed map results supplied below. Return one "
        "JSON object matching analysis-reduce-v1 exactly: the three existing "
        "artifact payload shapes plus glossary_proposals, unresolved, and the "
        "exact source_plan_hash and ordered scope_hashes. Preserve every source "
        "block link in terminology proposals and unresolved findings; never "
        "invent a source block ID, source fact, target term, or certainty. "
        "Glossary entries must remain status=proposed and must never be treated "
        "as locked or approved."
    )
    if transport == "strict_text_json":
        prompt += (
            " Output strict JSON only, with no Markdown fences, commentary, or "
            "additional keys."
        )
    else:
        prompt += " The native response schema is enforced; still emit no commentary."
    map_payload = [item.to_dict() for item in ordered]
    user_payload: dict[str, object] = {
        "source_plan_hash": source_plan_hash,
        "scope_hashes": scope_hashes,
        "mapped_block_ids": mapped_block_ids,
        "map_results": map_payload,
    }
    if transport == "strict_text_json":
        user_payload["response_contract"] = {
            "json_schema": ANALYSIS_REDUCE_JSON_SCHEMA,
            "exact_values": {
                "schema_version": ANALYSIS_REDUCE_SCHEMA_VERSION,
                "source_plan_hash": source_plan_hash,
                "scope_hashes": scope_hashes,
            },
        }
    packet = {
        "messages": [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": json.dumps(
                    user_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        ],
        "input": {
            "phase": _mapping(phase or {}, "analysis-phase"),
            "source_plan_hash": source_plan_hash,
            "scope_hashes": scope_hashes,
            "mapped_block_ids": mapped_block_ids,
            "map_results": map_payload,
            "output_schema": ANALYSIS_REDUCE_SCHEMA_VERSION,
            "transport": transport,
            "response_format": response_format,
        },
    }
    return cast(
        dict[str, object],
        _json(packet, "analysis-reduce-packet", maximum=MAX_REDUCE_PACKET_BYTES),
    )


def prepare_block_analysis_attempt(
    *,
    operation: Any,
    source_plan: Any,
    scope: AnalysisScope,
    phase: Mapping[str, object],
    settings: Mapping[str, object],
    budget: Mapping[str, object],
    analysis_kind: str = ANALYSIS_KIND,
    retry_of_attempt_id: str | None = None,
) -> Any:
    """Persist one exact map request before any provider I/O."""

    if not isinstance(phase, Mapping) or not isinstance(phase.get("model"), Mapping):
        raise BlockContractError("analysis-phase")
    plan = (
        source_plan
        if isinstance(source_plan, TranslationSourcePlan)
        else rehydrate_source_plan(source_plan)
    )
    packet = build_analysis_packet(plan, scope, phase=phase)
    return prepare_and_commit_analysis_attempt(
        operation=operation,
        source_plan=source_plan,
        analysis_kind=analysis_kind,
        contract_version=ANALYSIS_CONTRACT_VERSION_V1,
        packet=packet,
        model=cast(Mapping[str, Any], phase["model"]),
        profile={
            "profile_id": ANALYSIS_PROFILE_ID,
            "profile_version": ANALYSIS_PROFILE_VERSION,
            "provider_output_policy": PROVIDER_DEFAULT_OUTPUT_POLICY,
            "phase": dict(phase),
        },
        settings=settings,
        budget=budget,
        tool_declarations=[],
        retry_of_attempt_id=retry_of_attempt_id,
    )


def execute_block_analysis_attempt(
    attempt: Any,
    *,
    provider_service: Any,
) -> AnalysisMapResult:
    """Execute one map attempt through the shared no-retry lifecycle."""

    # The one-shot runner transitions the attempt in independent sessions.
    # Retain the already-verified immutable request packet here instead of
    # re-reading the caller's intentionally stale ORM identity afterwards.
    packet = rehydrate_analysis_attempt(attempt)
    input_data = _mapping(packet.get("input"), "analysis-packet-input")
    scope = AnalysisScope.from_dict(_mapping(input_data.get("scope"), "analysis-scope"))

    def completion(provider_input: Mapping[str, Any]) -> dict[str, object]:
        provider_input_data = _mapping(
            provider_input.get("input"), "analysis-packet-input"
        )
        provider_scope = AnalysisScope.from_dict(
            _mapping(provider_input_data.get("scope"), "analysis-scope")
        )
        if provider_scope != scope:
            raise BlockContractError("analysis-scope")
        phase = _mapping(provider_input_data.get("phase"), "analysis-phase")
        transport, response_format = _map_transport(phase)
        if (
            provider_input_data.get("transport") != transport
            or provider_input_data.get("response_format") != response_format
        ):
            raise BlockContractError("analysis-map-transport")
        provider_packet = {
            "messages": provider_input.get("messages"),
            "tools": None,
            "response_format": response_format,
            "max_completion_tokens": _provider_output_limit(provider_input),
        }
        response = call_analysis(
            provider_service,
            phase=phase,
            packet=provider_packet,
            action_id=getattr(attempt, "id", "analysis-map"),
        )
        return parse_analysis_map_result(response, scope).to_dict()

    result = execute_analysis_attempt(attempt, completion_call=completion)
    if not isinstance(result, Mapping):
        raise BlockContractError("analysis-map-result")
    return AnalysisMapResult.from_dict(result, scope)


def _reduce_input(
    packet: Mapping[str, Any],
) -> tuple[
    Mapping[str, Any],
    tuple[AnalysisMapResult, ...],
    str,
    tuple[str, ...],
    tuple[str, ...],
]:
    input_data = _mapping(packet.get("input"), "analysis-reduce-packet-input")
    phase = _mapping(input_data.get("phase"), "analysis-phase")
    source_plan_hash = _hash(input_data.get("source_plan_hash"), "analysis-reduce-plan")
    scope_values = input_data.get("scope_hashes")
    block_values = input_data.get("mapped_block_ids")
    map_values = input_data.get("map_results")
    if (
        not isinstance(scope_values, list)
        or not isinstance(block_values, list)
        or not isinstance(map_values, list)
    ):
        raise BlockContractError("analysis-reduce-packet-input")
    scopes = tuple(_hash(value, "analysis-reduce-scope-hash") for value in scope_values)
    mapped_blocks = tuple(
        _strings(block_values, "analysis-reduce-blocks", maximum=MAX_SCOPE_BLOCKS)
    )
    parsed: list[AnalysisMapResult] = []
    for raw in map_values:
        data = _mapping(raw, "analysis-reduce-map-result")
        block_ids = data.get("block_ids")
        if not isinstance(block_ids, list):
            raise BlockContractError("analysis-reduce-map-result")
        scope = AnalysisScope(
            source_plan_hash=source_plan_hash,
            part_index=_positive_int(
                data.get("part_index"), "analysis-reduce-part-index"
            ),
            block_ids=tuple(block_ids),
            source_bytes=1,
        )
        parsed.append(AnalysisMapResult.from_dict(data, scope))
    ordered = _ordered_map_results(parsed)
    if (
        ordered[0].source_plan_hash != source_plan_hash
        or tuple(item.scope_hash for item in ordered) != scopes
        or _mapped_block_ids(ordered) != mapped_blocks
    ):
        raise BlockContractError("analysis-reduce-packet-lineage")
    transport, response_format = _reduce_transport(phase)
    if (
        input_data.get("transport") != transport
        or input_data.get("response_format") != response_format
    ):
        raise BlockContractError("analysis-reduce-transport")
    return phase, ordered, source_plan_hash, scopes, mapped_blocks


def _reduce_provider_packet(packet: Mapping[str, Any]) -> dict[str, object]:
    phase, _results, _plan_hash, _scope_hashes, _mapped_blocks = _reduce_input(packet)
    transport, response_format = _reduce_transport(phase)
    if transport == "strict_text_json" and response_format is not None:
        raise BlockContractError("analysis-reduce-transport")
    return {
        "messages": packet.get("messages"),
        "tools": None,
        "response_format": response_format,
        "max_completion_tokens": _provider_output_limit(
            packet, budget_code="analysis-reduce-output-budget"
        ),
    }


def prepare_reduce_analysis_attempt(
    *,
    operation: Any,
    source_plan: Any,
    map_results: Sequence[AnalysisMapResult],
    phase: Mapping[str, object],
    settings: Mapping[str, object],
    budget: Mapping[str, object],
    retry_of_attempt_id: str | None = None,
) -> Any:
    """Reserve and commit one provider-backed consolidation request."""

    if not isinstance(phase, Mapping) or not isinstance(phase.get("model"), Mapping):
        raise BlockContractError("analysis-phase")
    budget_data = _mapping(budget, "analysis-reduce-budget")
    _positive_int(
        budget_data.get("output_tokens"),
        "analysis-reduce-output-budget",
        maximum=1_000_000,
    )
    packet = build_reduce_packet(map_results, phase=phase)
    transport, response_format = _reduce_transport(phase)
    return prepare_and_commit_analysis_attempt(
        operation=operation,
        source_plan=source_plan,
        analysis_kind=ANALYSIS_REDUCE_KIND,
        contract_version=ANALYSIS_CONTRACT_VERSION_V1,
        packet=packet,
        model=cast(Mapping[str, Any], phase["model"]),
        profile={
            "profile_id": ANALYSIS_REDUCE_KIND,
            "profile_version": ANALYSIS_PROFILE_VERSION,
            "provider_output_policy": PROVIDER_DEFAULT_OUTPUT_POLICY,
            "transport": transport,
            "response_format": response_format,
            "phase": dict(phase),
        },
        settings=settings,
        budget=budget_data,
        tool_declarations=[],
        retry_of_attempt_id=retry_of_attempt_id,
    )


def execute_reduce_analysis_attempt(
    attempt: Any,
    *,
    provider_service: Any,
) -> AnalysisReduceResult:
    """Execute one no-tool consolidation call through the shared ledger."""

    # As with map execution, the durable state transitions happen in
    # independent sessions. Keep this immutable packet rather than consulting
    # the caller's stale attempt instance after completion.
    packet = rehydrate_analysis_attempt(attempt)
    _phase, _results, source_plan_hash, scope_hashes, mapped_blocks = _reduce_input(
        packet
    )

    def completion(provider_input: Mapping[str, Any]) -> dict[str, object]:
        phase, _mapped, provider_plan_hash, provider_scope_hashes, provider_blocks = (
            _reduce_input(provider_input)
        )
        if (
            provider_plan_hash != source_plan_hash
            or provider_scope_hashes != scope_hashes
            or provider_blocks != mapped_blocks
        ):
            raise BlockContractError("analysis-reduce-packet-lineage")
        response = call_analysis(
            provider_service,
            phase=phase,
            packet=_reduce_provider_packet(provider_input),
            action_id=getattr(attempt, "id", "analysis-reduce"),
        )
        return AnalysisReduceResult.from_response(
            response,
            source_plan_hash=source_plan_hash,
            scope_hashes=scope_hashes,
            mapped_block_ids=mapped_blocks,
        ).to_dict()

    result = execute_analysis_attempt(attempt, completion_call=completion)
    if not isinstance(result, Mapping):
        raise BlockContractError("analysis-reduce-result")
    return AnalysisReduceResult.from_dict(
        result,
        source_plan_hash=source_plan_hash,
        scope_hashes=scope_hashes,
        mapped_block_ids=mapped_blocks,
    )


def resume_reduce_analysis_attempt(
    attempt: Any, source_plan: Any
) -> AnalysisReduceResult:
    packet = rehydrate_analysis_attempt(attempt, source_plan)
    _phase, _results, source_plan_hash, scope_hashes, mapped_blocks = _reduce_input(
        packet
    )
    result = resume_analysis_attempt(attempt)
    if not isinstance(result, Mapping):
        raise BlockContractError("analysis-reduce-result")
    return AnalysisReduceResult.from_dict(
        result,
        source_plan_hash=source_plan_hash,
        scope_hashes=scope_hashes,
        mapped_block_ids=mapped_blocks,
    )


def reconcile_reduce_analysis_attempt(attempt: Any) -> None:
    reconcile_dispatched_analysis_attempt(attempt)


def resume_block_analysis_attempt(attempt: Any, source_plan: Any) -> AnalysisMapResult:
    packet = rehydrate_analysis_attempt(attempt, source_plan)
    scope = AnalysisScope.from_dict(
        _mapping(packet["input"]["scope"], "analysis-scope")
    )
    result = resume_analysis_attempt(attempt)
    if not isinstance(result, Mapping):
        raise BlockContractError("analysis-map-result")
    return AnalysisMapResult.from_dict(result, scope)


def reconcile_block_analysis_attempt(attempt: Any) -> None:
    reconcile_dispatched_analysis_attempt(attempt)


def _operation_identity(operation: Any) -> tuple[str, int]:
    operation_id = getattr(operation, "id", None)
    operation_version = getattr(operation, "version", None)
    if (
        not isinstance(operation_id, str)
        or not operation_id
        or not isinstance(operation_version, int)
        or isinstance(operation_version, bool)
        or operation_version < 1
    ):
        raise BlockContractError("analysis-discovery-operation")
    return operation_id, operation_version


def _attempt_lineage_tip(
    attempts: Sequence[TranslationAnalysisAttempt],
) -> TranslationAnalysisAttempt:
    """Return the sole exact-request lineage tip, or fail closed.

    Attempt timestamps are deliberately absent from this decision.  A rebase
    whose parent has different immutable request material is a root within
    this exact-request set; a retry parent has identical material and links
    the two rows.  More than one such root or tip is ambiguous evidence.
    """

    if not attempts:
        raise BlockContractError("analysis-discovery-empty")
    ids = {attempt.id for attempt in attempts}
    parents = {
        attempt.retry_of_attempt_id or attempt.rebase_of_attempt_id
        for attempt in attempts
        if (attempt.retry_of_attempt_id or attempt.rebase_of_attempt_id) in ids
    }
    tips = [attempt for attempt in attempts if attempt.id not in parents]
    roots = [
        attempt
        for attempt in attempts
        if (attempt.retry_of_attempt_id or attempt.rebase_of_attempt_id) not in ids
    ]
    if len(tips) != 1 or len(roots) != 1:
        raise BlockContractError("analysis-discovery-ambiguous")
    return tips[0]


def _matching_analysis_attempts(
    *,
    operation: Any,
    source_plan: Any,
    analysis_kind: str,
    packet: Mapping[str, object],
    model: Mapping[str, object],
    profile: Mapping[str, object],
    settings: Mapping[str, object],
    budget: Mapping[str, object],
    session: Any | None = None,
) -> AnalysisAttemptDiscovery:
    """Select a sealed request whose full immutable material is exact.

    Every candidate in the scoped operation is rehydrated before it can be
    ignored.  That keeps a corrupted row from being silently bypassed during
    restart recovery, while unrelated *valid* rows remain harmless.
    """

    operation_id, operation_version = _operation_identity(operation)
    plan = rehydrate_source_plan(source_plan)
    plan_id = getattr(source_plan, "id", None)
    if not isinstance(plan_id, str) or not plan_id:
        raise BlockContractError("analysis-discovery-source-plan")
    active_session = db.session if session is None else session
    candidates = list(
        active_session.query(TranslationAnalysisAttempt)
        .filter_by(operation_id=operation_id, source_plan_id=plan_id)
        .all()
    )
    expected_packet = {
        "messages": _json(
            packet.get("messages"),
            "analysis-discovery-packet",
            maximum=MAX_REDUCE_PACKET_BYTES,
        ),
        "input": _json(
            packet.get("input"),
            "analysis-discovery-packet",
            maximum=MAX_REDUCE_PACKET_BYTES,
        ),
    }
    expected_model = _mapping(model, "analysis-discovery-model")
    expected_profile = _mapping(profile, "analysis-discovery-profile")
    expected_settings = _mapping(settings, "analysis-discovery-settings")
    expected_budget = _mapping(budget, "analysis-discovery-budget")
    matching: list[TranslationAnalysisAttempt] = []
    for attempt in candidates:
        sealed = rehydrate_analysis_attempt(
            attempt, source_plan, session=active_session
        )
        if (
            attempt.analysis_kind == analysis_kind
            and attempt.contract_version == ANALYSIS_CONTRACT_VERSION_V1
            and {"messages": sealed["messages"], "input": sealed["input"]}
            == expected_packet
            and sealed["model"] == expected_model
            and sealed["profile"] == expected_profile
            and sealed["settings"] == expected_settings
            and sealed["budget"] == expected_budget
            and sealed["tool_declarations"] == []
            and attempt.source_plan_hash == plan.plan_hash
        ):
            matching.append(attempt)
    if not matching:
        return AnalysisAttemptDiscovery(None, None, False, False, False)
    selected = _attempt_lineage_tip(matching)
    current_generation = selected.operation_version == operation_version
    state = selected.dispatch_state
    return AnalysisAttemptDiscovery(
        attempt=selected,
        retry_lineage_attempt_id=(
            selected.id if state in _RETRYABLE_TERMINAL_ANALYSIS_STATES else None
        ),
        executable_under_current_generation=current_generation and state == "prepared",
        resumable=state == "completed"
        or (current_generation and state == "response_received"),
        reconciliation_required=state == "dispatched",
    )


def discover_exact_map_attempt(
    *,
    operation: Any,
    source_plan: Any,
    scope: AnalysisScope,
    phase: Mapping[str, object],
    settings: Mapping[str, object],
    budget: Mapping[str, object],
    packet: Mapping[str, object] | None = None,
    session: Any | None = None,
) -> AnalysisAttemptDiscovery:
    """Discover one exact map attempt for a scope, without replay guessing."""

    if not isinstance(phase, Mapping) or not isinstance(phase.get("model"), Mapping):
        raise BlockContractError("analysis-phase")
    plan = rehydrate_source_plan(source_plan)
    if scope.source_plan_hash != plan.plan_hash:
        raise BlockContractError("analysis-scope-plan")
    generated_packet = build_analysis_packet(plan, scope, phase=phase)
    if packet is not None and dict(packet) != generated_packet:
        raise BlockContractError("analysis-map-packet")
    expected_packet = generated_packet
    return _matching_analysis_attempts(
        operation=operation,
        source_plan=source_plan,
        analysis_kind=ANALYSIS_KIND,
        packet=expected_packet,
        model=cast(Mapping[str, object], phase["model"]),
        profile={
            "profile_id": ANALYSIS_PROFILE_ID,
            "profile_version": ANALYSIS_PROFILE_VERSION,
            "provider_output_policy": PROVIDER_DEFAULT_OUTPUT_POLICY,
            "phase": dict(phase),
        },
        settings=settings,
        budget=budget,
        session=session,
    )


def discover_exact_reduce_attempt(
    *,
    operation: Any,
    source_plan: Any,
    map_results: Sequence[AnalysisMapResult],
    phase: Mapping[str, object],
    settings: Mapping[str, object],
    budget: Mapping[str, object],
    packet: Mapping[str, object] | None = None,
    session: Any | None = None,
) -> AnalysisAttemptDiscovery:
    """Discover one exact provider-backed reduction request and its state."""

    if not isinstance(phase, Mapping) or not isinstance(phase.get("model"), Mapping):
        raise BlockContractError("analysis-phase")
    plan = rehydrate_source_plan(source_plan)
    ordered = _ordered_map_results(map_results)
    if ordered[0].source_plan_hash != plan.plan_hash:
        raise BlockContractError("analysis-reduce-plan")
    generated_packet = build_reduce_packet(ordered, phase=phase)
    if packet is not None and dict(packet) != generated_packet:
        raise BlockContractError("analysis-reduce-packet")
    expected_packet = generated_packet
    transport, response_format = _reduce_transport(phase)
    return _matching_analysis_attempts(
        operation=operation,
        source_plan=source_plan,
        analysis_kind=ANALYSIS_REDUCE_KIND,
        packet=expected_packet,
        model=cast(Mapping[str, object], phase["model"]),
        profile={
            "profile_id": ANALYSIS_REDUCE_KIND,
            "profile_version": ANALYSIS_PROFILE_VERSION,
            "provider_output_policy": PROVIDER_DEFAULT_OUTPUT_POLICY,
            "transport": transport,
            "response_format": response_format,
            "phase": dict(phase),
        },
        settings=settings,
        budget=budget,
        session=session,
    )


def discover_analysis_scopes(
    attempts: Iterable[Any], source_plan: Any
) -> tuple[AnalysisScope, ...]:
    """Recover scopes from sealed request packets, never progress counters."""

    scopes: dict[int, AnalysisScope] = {}
    expected_hash = getattr(source_plan, "plan_hash", None)
    for attempt in attempts:
        if getattr(attempt, "analysis_kind", None) != ANALYSIS_KIND:
            continue
        packet = rehydrate_analysis_attempt(attempt, source_plan)
        input_data = _mapping(packet.get("input"), "analysis-packet-input")
        scope = AnalysisScope.from_dict(
            _mapping(input_data.get("scope"), "analysis-scope")
        )
        if expected_hash is not None and scope.source_plan_hash != expected_hash:
            raise BlockContractError("analysis-scope-plan")
        if scope.part_index in scopes and scopes[scope.part_index] != scope:
            raise BlockContractError("analysis-scope-duplicate")
        scopes[scope.part_index] = scope
    return tuple(scopes[index] for index in sorted(scopes))


@dataclass(frozen=True, slots=True)
class AnalysisReduceResult:
    source_plan_hash: str
    document_brief: dict[str, object]
    style_guide: dict[str, object]
    entity_register: dict[str, object]
    glossary_proposals: tuple[dict[str, object], ...]
    unresolved: tuple[dict[str, object], ...]
    scope_hashes: tuple[str, ...]

    def __post_init__(self) -> None:
        _hash(self.source_plan_hash, "analysis-reduce-plan-hash")
        if set(self.document_brief) != {"summary", "domains", "audience"}:
            raise BlockContractError("analysis-reduce-document-brief")
        if set(self.style_guide) != {"register", "guidance", "prohibitions"}:
            raise BlockContractError("analysis-reduce-style-guide")
        if set(self.entity_register) != {"entities"}:
            raise BlockContractError("analysis-reduce-entity-register")
        _string(
            self.document_brief["summary"],
            "analysis-reduce-document-brief",
            maximum=MAX_REDUCE_BYTES,
        )
        _strings(
            self.document_brief["domains"],
            "analysis-reduce-document-brief",
            maximum=256,
        )
        _string(
            self.document_brief["audience"],
            "analysis-reduce-document-brief",
            maximum=4_096,
        )
        _string(
            self.style_guide["register"], "analysis-reduce-style-guide", maximum=2_048
        )
        _strings(
            self.style_guide["guidance"], "analysis-reduce-style-guide", maximum=512
        )
        _strings(
            self.style_guide["prohibitions"], "analysis-reduce-style-guide", maximum=512
        )
        entities = self.entity_register["entities"]
        if not isinstance(entities, list):
            raise BlockContractError("analysis-reduce-entity-register")
        for entity in entities:
            if not isinstance(entity, Mapping) or set(entity) != {
                "name",
                "type",
                "aliases",
            }:
                raise BlockContractError("analysis-reduce-entity-register")
            _string(entity["name"], "analysis-reduce-entity-register", maximum=4_096)
            _string(entity["type"], "analysis-reduce-entity-register", maximum=1_024)
            _strings(entity["aliases"], "analysis-reduce-entity-register", maximum=64)
        for proposal in self.glossary_proposals:
            _validate_proposal(proposal)
        for unresolved in self.unresolved:
            if set(unresolved) != {"question", "reason", "impact", "source_block_ids"}:
                raise BlockContractError("analysis-reduce-unresolved")
            _strings(
                unresolved["source_block_ids"],
                "analysis-reduce-unresolved",
                maximum=MAX_SCOPE_BLOCKS,
            )
        for item in self.scope_hashes:
            _hash(item, "analysis-reduce-scope-hash")
        _json(self.to_dict(), "analysis-reduce-size", maximum=MAX_REDUCE_BYTES)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": ANALYSIS_REDUCE_SCHEMA_VERSION,
            "source_plan_hash": self.source_plan_hash,
            "document_brief": dict(self.document_brief),
            "style_guide": dict(self.style_guide),
            "entity_register": dict(self.entity_register),
            "glossary_proposals": [dict(item) for item in self.glossary_proposals],
            "unresolved": [dict(item) for item in self.unresolved],
            "scope_hashes": list(self.scope_hashes),
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        source_plan_hash: str,
        scope_hashes: Sequence[str],
        mapped_block_ids: Sequence[str],
    ) -> AnalysisReduceResult:
        """Validate one provider reduction against its witnessed map inputs."""

        material = _mapping(value, "analysis-reduce", maximum=MAX_REDUCE_BYTES)
        expected_keys = {
            "schema_version",
            "source_plan_hash",
            "document_brief",
            "style_guide",
            "entity_register",
            "glossary_proposals",
            "unresolved",
            "scope_hashes",
        }
        if set(material) != expected_keys:
            raise BlockContractError("analysis-reduce-keys")
        if material["schema_version"] != ANALYSIS_REDUCE_SCHEMA_VERSION:
            raise BlockContractError("analysis-reduce-version")
        if material["source_plan_hash"] != source_plan_hash:
            raise BlockContractError("analysis-reduce-plan")
        expected_scopes = list(scope_hashes)
        actual_scopes = material["scope_hashes"]
        if not isinstance(actual_scopes, list) or actual_scopes != expected_scopes:
            raise BlockContractError("analysis-reduce-scopes")
        allowed_blocks = set(
            _strings(
                list(mapped_block_ids),
                "analysis-reduce-blocks",
                maximum=MAX_SCOPE_BLOCKS,
            )
        )
        if not allowed_blocks:
            raise BlockContractError("analysis-reduce-blocks")
        proposals = material["glossary_proposals"]
        unresolved = material["unresolved"]
        if not isinstance(proposals, list) or not isinstance(unresolved, list):
            raise BlockContractError("analysis-reduce-children")
        proposal_values = tuple(
            _mapping(item, "analysis-glossary-proposal") for item in proposals
        )
        unresolved_values = tuple(
            _mapping(item, "analysis-reduce-unresolved") for item in unresolved
        )
        for item in proposal_values:
            _validate_proposal(item)
            _require_source_links(
                item["source_block_ids"], allowed_blocks, "analysis-glossary-proposal"
            )
        for item in unresolved_values:
            if set(item) != {"question", "reason", "impact", "source_block_ids"}:
                raise BlockContractError("analysis-reduce-unresolved")
            _require_source_links(
                item["source_block_ids"], allowed_blocks, "analysis-reduce-unresolved"
            )
        return cls(
            source_plan_hash=source_plan_hash,
            document_brief=dict(
                _mapping(material["document_brief"], "analysis-reduce-document-brief")
            ),
            style_guide=dict(
                _mapping(material["style_guide"], "analysis-reduce-style-guide")
            ),
            entity_register=dict(
                _mapping(material["entity_register"], "analysis-reduce-entity-register")
            ),
            glossary_proposals=proposal_values,
            unresolved=unresolved_values,
            scope_hashes=tuple(actual_scopes),
        )

    @classmethod
    def from_response(
        cls,
        response: object,
        *,
        source_plan_hash: str,
        scope_hashes: Sequence[str],
        mapped_block_ids: Sequence[str],
    ) -> AnalysisReduceResult:
        if isinstance(response, Mapping) and "schema_version" in response:
            parsed: object = response
        elif isinstance(response, Mapping) and set(response) == {"role", "content"}:
            parsed = response["content"]
        elif isinstance(response, Mapping) and set(response) == {
            "role",
            "content",
            "tool_calls",
        }:
            raise BlockContractError("analysis-reduce-tool-call")
        else:
            parsed = response
        if isinstance(parsed, str):
            try:
                parsed = json.loads(parsed)
            except (RecursionError, TypeError, ValueError) as exc:
                raise BlockContractError("analysis-reduce-json") from exc
        if not isinstance(parsed, Mapping):
            raise BlockContractError("analysis-reduce-json")
        return cls.from_dict(
            parsed,
            source_plan_hash=source_plan_hash,
            scope_hashes=scope_hashes,
            mapped_block_ids=mapped_block_ids,
        )


def _dedupe_strings(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.casefold().strip()
        if key and key not in seen:
            result.append(value)
            seen.add(key)
    return result


def _validate_proposal(value: Mapping[str, object]) -> None:
    if set(value) != {
        "source_term",
        "target_term",
        "notes",
        "scope",
        "source_block_ids",
        "confidence",
        "status",
    }:
        raise BlockContractError("analysis-glossary-proposal")
    _string(value["source_term"], "analysis-glossary-proposal", maximum=4_096)
    target = value["target_term"]
    if target is not None:
        _string(target, "analysis-glossary-proposal", maximum=4_096)
    _string(
        value["notes"], "analysis-glossary-proposal", allow_blank=True, maximum=16_384
    )
    if value["scope"] != "document" or value["status"] != "proposed":
        raise BlockContractError("analysis-glossary-proposal")
    _strings(
        value["source_block_ids"],
        "analysis-glossary-proposal",
        maximum=MAX_SCOPE_BLOCKS,
    )
    _confidence(value["confidence"])


def _require_source_links(value: object, allowed: set[str], code: str) -> list[str]:
    links = _strings(value, code, maximum=MAX_SCOPE_BLOCKS)
    if (
        not links
        or len(set(links)) != len(links)
        or any(item not in allowed for item in links)
    ):
        raise BlockContractError(code)
    return links


def parse_analysis_reduce_result(
    response: object,
    *,
    source_plan_hash: str,
    scope_hashes: Sequence[str],
    mapped_block_ids: Sequence[str],
) -> AnalysisReduceResult:
    return AnalysisReduceResult.from_response(
        response,
        source_plan_hash=source_plan_hash,
        scope_hashes=scope_hashes,
        mapped_block_ids=mapped_block_ids,
    )


def reduce_analysis_results(
    results: Sequence[AnalysisMapResult],
    *,
    source_plan_hash: str | None = None,
) -> AnalysisReduceResult:
    """Deterministic fallback/fixture reducer for already-witnessed map DTOs.

    Production quality runs should use :func:`prepare_reduce_analysis_attempt`
    and its shared provider lifecycle.  This helper remains useful for
    deterministic fixtures, recovery tooling, and an explicitly opted-in
    fallback when no reducer call is available.
    """

    if not results:
        raise BlockContractError("analysis-reduce-empty")
    ordered = tuple(sorted(results, key=lambda item: item.part_index))
    if [item.part_index for item in ordered] != list(range(1, len(ordered) + 1)):
        raise BlockContractError("analysis-reduce-order")
    plan_hash = ordered[0].source_plan_hash
    if source_plan_hash is not None and source_plan_hash != plan_hash:
        raise BlockContractError("analysis-reduce-plan")
    if any(item.source_plan_hash != plan_hash for item in ordered):
        raise BlockContractError("analysis-reduce-plan")

    summary = "\n\n".join(cast(str, item.summary["text"]) for item in ordered)
    domains = _dedupe_strings(
        domain
        for item in ordered
        for domain in cast(list[str], item.summary["domains"])
    )
    audience = next(
        (
            cast(str, item.summary["audience"])
            for item in ordered
            if cast(str, item.summary["audience"]).strip()
        ),
        "unspecified",
    )
    guidance = _dedupe_strings(
        text for item in ordered for text in cast(list[str], item.style["guidance"])
    )
    prohibitions = _dedupe_strings(
        text for item in ordered for text in cast(list[str], item.style["prohibitions"])
    )
    register = next(
        (
            cast(str, item.style["register"])
            for item in ordered
            if cast(str, item.style["register"]).strip()
        ),
        "unspecified",
    )

    entities_by_name: dict[str, dict[str, object]] = {}
    for item in ordered:
        for entity in item.entities:
            key = cast(str, entity["name"]).casefold().strip()
            existing = entities_by_name.get(key)
            if existing is None:
                entities_by_name[key] = {
                    "name": entity["name"],
                    "type": entity["type"],
                    "aliases": list(cast(list[str], entity["aliases"])),
                }
            else:
                existing["aliases"] = _dedupe_strings(
                    [
                        *cast(list[str], existing["aliases"]),
                        *cast(list[str], entity["aliases"]),
                    ]
                )

    proposals_by_term: dict[str, dict[str, object]] = {}
    for item in ordered:
        for term in item.terminology_candidates:
            key = cast(str, term["source_term"]).casefold().strip()
            existing = proposals_by_term.get(key)
            source_ids = cast(list[str], term["source_block_ids"])
            proposed_target = term["proposed_target"]
            notes = cast(str, term["definition"])
            if existing is None:
                proposals_by_term[key] = {
                    "source_term": term["source_term"],
                    "target_term": proposed_target,
                    "notes": notes,
                    "scope": "document",
                    "source_block_ids": list(source_ids),
                    "confidence": term["confidence"],
                    "status": "proposed",
                }
            else:
                existing["source_block_ids"] = _dedupe_strings(
                    [
                        *cast(list[str], existing["source_block_ids"]),
                        *source_ids,
                    ]
                )
                if existing["target_term"] is None and proposed_target is not None:
                    existing["target_term"] = proposed_target
                if notes and not cast(str, existing["notes"]):
                    existing["notes"] = notes
                existing["confidence"] = max(
                    cast(float, existing["confidence"]), cast(float, term["confidence"])
                )

    unresolved: list[dict[str, object]] = []
    for item in ordered:
        unresolved.extend(dict(value) for value in item.unresolved)
    return AnalysisReduceResult(
        source_plan_hash=plan_hash,
        document_brief={"summary": summary, "domains": domains, "audience": audience},
        style_guide={
            "register": register,
            "guidance": guidance,
            "prohibitions": prohibitions,
        },
        entity_register={"entities": list(entities_by_name.values())},
        glossary_proposals=tuple(proposals_by_term.values()),
        unresolved=tuple(unresolved),
        scope_hashes=tuple(item.scope_hash for item in ordered),
    )


def _artifact_lineage_matches(evidence: object, expected: Mapping[str, object]) -> bool:
    if not isinstance(evidence, Mapping):
        return False
    return all(evidence.get(key) == expected.get(key) for key in expected)


def _reuse_reduced_analysis_artifacts(
    source_plan: Any,
    *,
    expected: Mapping[str, Mapping[str, object]],
    lineage: Mapping[str, object],
) -> dict[str, Any] | None:
    """Return one verified exact artifact trio or reject incomplete lineage.

    Artifact revisions are append-only, so a restart must prove that the
    previous call either wrote the whole exact trio or wrote nothing relevant.
    Selecting a latest revision would make a partial crash look successful and
    would silently append duplicate model artefacts on the next run.
    """

    source_plan_id = getattr(source_plan, "id", None)
    if not isinstance(source_plan_id, str) or not source_plan_id:
        raise BlockContractError("analysis-artifact-source-plan")
    exact: dict[str, list[Any]] = {kind: [] for kind in expected}
    relevant_found = False
    rows = TranslationContextArtifactRevision.query.filter_by(
        source_plan_id=source_plan_id
    ).all()
    for revision in rows:
        # Rehydration proves the encrypted output/evidence and parent chain
        # before we inspect it for recovery decisions.
        payload = rehydrate_context_artifact_revision(revision, source_plan)
        evidence = revision.evidence_json
        if not _artifact_lineage_matches(evidence, lineage):
            continue
        relevant_found = True
        kind = payload.kind
        expected_value = expected.get(kind)
        if expected_value is None:
            raise BlockContractError("analysis-artifact-reuse-drift")
        if (
            revision.analysis_state != "model"
            or revision.profile_id != ANALYSIS_REDUCE_SCHEMA_VERSION
            or revision.profile_version != "1"
            or payload.to_dict() != expected_value["payload"]
            or revision.model_json != expected_value["model"]
            or revision.prompt_json != expected_value["prompt"]
            or revision.settings_json != expected_value["settings"]
            or revision.input_json != expected_value["input_evidence"]
            or revision.evidence_json != expected_value["evidence"]
        ):
            raise BlockContractError("analysis-artifact-reuse-drift")
        exact[kind].append(revision)
    if not relevant_found:
        return None
    if any(len(rows_for_kind) > 1 for rows_for_kind in exact.values()):
        raise BlockContractError("analysis-artifact-reuse-ambiguous")
    if any(len(rows_for_kind) != 1 for rows_for_kind in exact.values()):
        raise BlockContractError("analysis-artifact-reuse-partial")
    return {kind: rows_for_kind[0] for kind, rows_for_kind in exact.items()}


def persist_reduced_analysis_artifacts(
    source_plan: Any,
    reduced: AnalysisReduceResult,
    *,
    language: str,
    attempts: Sequence[Any],
    reduce_attempt: Any | None = None,
    deterministic_fallback: bool = False,
    phase: Mapping[str, object] | None = None,
    settings: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Persist exactly the three typed artifacts, with map lineage evidence.

    ``attempts`` must contain one completed map analysis-attempt row per scope
    in order.  A provider-backed reduce attempt is required by default; the
    deterministic reducer is accepted only with the explicit
    ``deterministic_fallback=True`` switch.  Actionable terminology is also
    persisted as model-authored proposals.  It remains advisory until a
    separate owner interaction creates a human lock and active pointer.
    """

    _string(language, "analysis-artifact-language", maximum=64)
    if not isinstance(reduced, AnalysisReduceResult):
        raise BlockContractError("analysis-reduce-result")
    if len(attempts) != len(reduced.scope_hashes):
        raise BlockContractError("analysis-reduce-attempts")
    if reduce_attempt is None and deterministic_fallback is not True:
        raise BlockContractError("analysis-reduce-attempt-required")
    if reduce_attempt is not None and deterministic_fallback is True:
        raise BlockContractError("analysis-reduce-mode")
    lineage: list[dict[str, object]] = []
    for scope_hash, attempt in zip(reduced.scope_hashes, attempts):
        attempt_hash = getattr(attempt, "attempt_record_hash", None)
        attempt_id = getattr(attempt, "id", None)
        request_hash = getattr(attempt, "request_hash", None)
        if isinstance(attempt, Mapping):
            attempt_hash = attempt.get("attempt_record_hash")
            attempt_id = attempt.get("id")
            request_hash = attempt.get("request_hash")
        lineage.append(
            {
                "scope_hash": _hash(scope_hash, "analysis-reduce-scope-hash"),
                "attempt_id": _string(
                    attempt_id, "analysis-reduce-attempt-id", maximum=128
                ),
                "attempt_hash": _hash(attempt_hash, "analysis-reduce-attempt-hash"),
                "request_hash": _hash(request_hash, "analysis-reduce-request-hash"),
            }
        )
    reduce_lineage: dict[str, object] | None = None
    if reduce_attempt is not None:
        reduce_id = getattr(reduce_attempt, "id", None)
        reduce_record_hash = getattr(reduce_attempt, "attempt_record_hash", None)
        reduce_request_hash = getattr(reduce_attempt, "request_hash", None)
        reduce_kind = getattr(reduce_attempt, "analysis_kind", ANALYSIS_REDUCE_KIND)
        if isinstance(reduce_attempt, Mapping):
            reduce_id = reduce_attempt.get("id")
            reduce_record_hash = reduce_attempt.get("attempt_record_hash")
            reduce_request_hash = reduce_attempt.get("request_hash")
            reduce_kind = reduce_attempt.get("analysis_kind", ANALYSIS_REDUCE_KIND)
        if reduce_kind != ANALYSIS_REDUCE_KIND:
            raise BlockContractError("analysis-reduce-attempt-kind")
        reduce_lineage = {
            "attempt_id": _string(reduce_id, "analysis-reduce-attempt-id", maximum=128),
            "attempt_record_hash": _hash(
                reduce_record_hash, "analysis-reduce-attempt-hash"
            ),
            "request_hash": _hash(reduce_request_hash, "analysis-reduce-request-hash"),
        }
    lineage_value = {
        "source_plan_hash": reduced.source_plan_hash,
        "scope_attempts": lineage,
        "reduction_mode": (
            "provider" if reduce_lineage is not None else "deterministic_fallback"
        ),
        "reduce_attempt": reduce_lineage,
    }
    model = {}
    if phase is not None and isinstance(phase.get("model"), Mapping):
        model = dict(cast(Mapping[str, object], phase["model"]))
    prompt = {
        "profile_id": ANALYSIS_PROFILE_ID,
        "profile_version": ANALYSIS_PROFILE_VERSION,
        "reduce_schema_version": ANALYSIS_REDUCE_SCHEMA_VERSION,
    }
    frozen_settings = dict(settings or {})
    artifact_values: dict[str, Mapping[str, object]] = {}
    for kind, key in (
        ("document_brief", "document_brief"),
        ("style_guide", "style_guide"),
        ("entity_register", "entity_register"),
    ):
        payload = ContextArtifactPayload(
            kind=kind,
            language=language,
            content=cast(dict[str, object], getattr(reduced, key)),
        )
        artifact_values[kind] = {
            "payload": payload.to_dict(),
            "model": model,
            "prompt": prompt,
            "settings": frozen_settings,
            "input_evidence": {
                "source_plan_hash": reduced.source_plan_hash,
                "scope_hashes": list(reduced.scope_hashes),
            },
            "evidence": {
                **lineage_value,
                "unresolved": [dict(value) for value in reduced.unresolved],
                "glossary_proposals": [
                    dict(value) for value in reduced.glossary_proposals
                ],
            },
        }
    reusable = _reuse_reduced_analysis_artifacts(
        source_plan,
        expected=artifact_values,
        lineage=lineage_value,
    )
    if reusable is not None:
        persist_analysis_glossary_proposals(
            source_plan,
            reduced.glossary_proposals,
        )
        return reusable

    revisions: dict[str, Any] = {}
    with db.session.begin_nested():
        for kind, key in (
            ("document_brief", "document_brief"),
            ("style_guide", "style_guide"),
            ("entity_register", "entity_register"),
        ):
            payload = ContextArtifactPayload(
                kind=kind,
                language=language,
                content=cast(dict[str, object], getattr(reduced, key)),
            )
            expected = artifact_values[kind]
            revisions[kind] = persist_context_artifact_revision(
                source_plan,
                kind=kind,
                payload=payload,
                analysis_state="model",
                profile_id=ANALYSIS_REDUCE_SCHEMA_VERSION,
                profile_version="1",
                model=cast(Mapping[str, object], expected["model"]),
                prompt=cast(Mapping[str, object], expected["prompt"]),
                settings=cast(Mapping[str, object], expected["settings"]),
                input_evidence=cast(Mapping[str, object], expected["input_evidence"]),
                evidence=cast(Mapping[str, object], expected["evidence"]),
            )
        persist_analysis_glossary_proposals(
            source_plan,
            reduced.glossary_proposals,
        )
    return revisions


__all__ = [
    "ANALYSIS_KIND",
    "ANALYSIS_MAP_SCHEMA_VERSION",
    "ANALYSIS_PROFILE_ID",
    "ANALYSIS_PROFILE_VERSION",
    "ANALYSIS_REDUCE_JSON_SCHEMA",
    "ANALYSIS_REDUCE_KIND",
    "ANALYSIS_REDUCE_SCHEMA_VERSION",
    "AnalysisAttemptDiscovery",
    "AnalysisMapResult",
    "AnalysisReduceResult",
    "AnalysisScope",
    "HeadingPathEntry",
    "analysis_scope_from_blocks",
    "build_analysis_packet",
    "build_analysis_scopes",
    "build_reduce_packet",
    "discover_analysis_scopes",
    "discover_exact_map_attempt",
    "discover_exact_reduce_attempt",
    "execute_block_analysis_attempt",
    "execute_reduce_analysis_attempt",
    "parse_analysis_map_result",
    "parse_analysis_reduce_result",
    "persist_reduced_analysis_artifacts",
    "prepare_block_analysis_attempt",
    "prepare_reduce_analysis_attempt",
    "reconcile_block_analysis_attempt",
    "reconcile_reduce_analysis_attempt",
    "reduce_analysis_results",
    "resume_block_analysis_attempt",
    "resume_reduce_analysis_attempt",
    "source_blocks_for_scope",
    "whole_document_analysis_scope",
]
