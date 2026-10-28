"""Deterministic assembly helpers for the document-block translation worker.

The worker owns transactions, leasing, provider calls, and finalisation.  This
module owns the smaller but equally important question of *what exactly is a
window request?*  Keeping that answer here means planning, packet admission,
retry discovery, and recovery can share one source-safe implementation.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, cast

from carlina.core.semantic_html import SAFE_LANGUAGE_RE
from iarf_assistant.core.model_capabilities import ModelCapabilityProfile
from iarf_assistant.extensions import db

from .block_contract import (
    BlockContractError,
    TranslationBlock,
    TranslationSourcePlan,
    canonical_json,
    sha256_text,
)
from .block_notes import normalize_annotation_profile
from .block_workflow_config import DOCUMENT_STRUCTURE_CONTEXT_VERSION
from .compact_markup import compact_candidate_projection, compact_source_block
from .models import TranslationSourcePlan as SourcePlanRecord
from .models import (
    TranslationTargetAlignmentActive,
    TranslationTargetBlockActive,
    TranslationTargetBlockRevision,
    TranslationWindowAttempt,
)
from .note_relations import reuse_note_relations
from .source_indexes import reuse_source_indexes, source_index
from .target_contract import literal_reference_slots, structural_fingerprint
from .target_persistence import rehydrate_target_revision, verify_active_pair
from .window_contract import (
    COMPACT_WRITABLE_SOURCE_CONTRACT_VERSION,
    LEGACY_WRITABLE_SOURCE_CONTRACT_VERSION,
    LOCATED_SOURCE_WRITABLE_CONTRACT_VERSION,
    NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
    PARTS_WRITABLE_SOURCE_CONTRACT_VERSION,
    SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION,
    SINGLE_COPY_WRITABLE_SOURCE_CONTRACT_VERSION,
    STUDY_NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
    WINDOW_PROMPT_CONTRACT_VERSION,
    WRITABLE_SOURCE_CONTRACT_VERSION,
    ContextSlot,
    ProviderPacket,
    TranslationWindow,
    context_dependency_manifest,
    render_admitted_packet,
)
from .window_execution import ModelExecutionProfile, admit_packet
from .window_persistence import (
    VerifiedWindowEvidence,
    predecessor_snapshot,
    rehydrate_window_attempt,
)

WINDOW_PROFILE_ID = "document-block-window"
WINDOW_PROFILE_VERSION = "5"
DEFAULT_TRANSLATION_BATCH_INPUT_TOKENS = 16_000
MIN_TRANSLATION_BATCH_INPUT_TOKENS = 2_048
MAX_TRANSLATION_BATCH_INPUT_TOKENS = 1_000_000
DEFAULT_TRANSLATION_BATCH_MAX_BLOCKS = 4
MIN_TRANSLATION_BATCH_MAX_BLOCKS = 1
MAX_TRANSLATION_BATCH_MAX_BLOCKS = 256
SETTINGS_EVIDENCE_CONTRACT_VERSION = "translation-block-window-settings-v1"
LEGACY_CONTINUITY_CONTRACT_VERSION = "translation-block-continuity-v1"
CONTINUITY_CONTRACT_VERSION = "translation-block-continuity-v2"
EXACT_PRECEDENT_CONTRACT_VERSION = "translation-exact-precedent-v1"
STRUCTURE_CONTEXT_CONTRACT_VERSION = DOCUMENT_STRUCTURE_CONTEXT_VERSION
_STRUCTURE_TEXT_LIMIT = 512
_STRUCTURE_TABLE_LIMIT = 4
_STRUCTURE_CHILD_LIMIT = 16
_STRUCTURE_ROW_LIMIT = 4
_STRUCTURE_CONTEXT_MAX_BYTES = 256 * 1024
_MAX_WINDOW_BLOCKS = 65_536
_MAX_EXACT_PRECEDENT_VARIANTS = 4
_MAX_EXACT_PRECEDENT_OCCURRENCES = 16
_SUPPORTED_WINDOW_PROFILE_VERSIONS = frozenset(
    {"1", "2", "3", "4", WINDOW_PROFILE_VERSION}
)
_OUTPUT_GROWTH_NUMERATOR = 5
_OUTPUT_GROWTH_DENOMINATOR = 4
_OUTPUT_HEADROOM_NUMERATOR = 3
_OUTPUT_HEADROOM_DENOMINATOR = 4
_REGISTER_VALUES = frozenset({"formal", "neutral", "informal"})
_PROTECTED_SLOT_TYPES = frozenset(
    {"control_contract", "project_policy", "writable_source"}
)
_RETRYABLE_TERMINAL_WINDOW_STATES = frozenset(
    {"provider_failed", "normalization_failed", "fence_failed"}
)


@dataclass(frozen=True, slots=True)
class WindowProfile:
    """Frozen capacity policy used for both planning and provider admission."""

    profile_id: str
    profile_version: str
    capacity_version: str
    max_blocks: int
    max_input_tokens: int
    output_reserve_tokens: int
    target_input_tokens: int | None = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "capacity_version": self.capacity_version,
            "max_blocks": self.max_blocks,
            "max_input_tokens": self.max_input_tokens,
            "output_reserve_tokens": self.output_reserve_tokens,
        }
        if self.profile_version != "1":
            if self.target_input_tokens is None:
                raise BlockContractError("block-translation-window-input-target")
            result["target_input_tokens"] = self.target_input_tokens
        return result


@dataclass(frozen=True, slots=True)
class WindowMaterial:
    """The exact request evidence for one window, before it is persisted."""

    window: TranslationWindow
    slots: tuple[ContextSlot, ...]
    packet: ProviderPacket
    admission: Mapping[str, Any]
    settings: Mapping[str, Any]
    model_profile: Mapping[str, Any]
    predecessor: Mapping[str, Any] | None


@dataclass(frozen=True, slots=True)
class PlannedWindows:
    """Ordered no-I/O planning output; continuity is deliberately absent."""

    profile: WindowProfile
    materials: tuple[WindowMaterial, ...]

    @property
    def windows(self) -> tuple[TranslationWindow, ...]:
        return tuple(material.window for material in self.materials)


@dataclass(frozen=True, slots=True)
class AttemptDiscovery:
    """Exact durable attempt selection without timestamp/counter guessing.

    ``executable_under_current_generation`` is true for a prepared request
    whose operation generation and dispatch fence are current, or for a
    provider-neutral external work item that is durably pending under the same
    queue delivery. A stale prepared request is safe retry lineage because it
    has not crossed the provider-dispatch boundary. ``resumable`` means that a
    durable response or staged revision set can be advanced without another
    provider request; response/staging recovery still requires the attempt's
    original live fence, while staged revisions are immutable and can be reused
    across later operation generations. ``reconciliation_required`` is reserved
    for dispatched requests whose provider outcome is not durably known.
    """

    attempt: TranslationWindowAttempt | None
    retry_lineage_attempt_id: str | None
    executable_under_current_generation: bool
    resumable: bool
    reconciliation_required: bool


def _profile_capability(profile: ModelExecutionProfile) -> tuple[dict[str, Any], str]:
    if not isinstance(profile, ModelExecutionProfile):
        raise BlockContractError("block-translation-model-profile")
    raw = profile.capability_profile.get("capability_profile")
    if not isinstance(raw, Mapping):
        raise BlockContractError("block-translation-model-profile")
    try:
        encoded = canonical_json(dict(raw)).encode("utf-8")
        normalized = cast(dict[str, Any], json.loads(encoded))
        capability = ModelCapabilityProfile(**normalized)
    except (TypeError, ValueError, RecursionError) as exc:
        raise BlockContractError("block-translation-model-profile") from exc
    if (
        capability.model_id != profile.model_id
        or profile.transport not in {"native_json_schema", "strict_text_json"}
        or not isinstance(profile.fingerprint, str)
        or len(profile.fingerprint) != 64
    ):
        raise BlockContractError("block-translation-model-profile")
    for value in (
        capability.max_input_tokens,
        capability.max_output_tokens,
        capability.input_safety_margin_tokens,
        capability.usable_input_tokens,
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise BlockContractError("block-translation-model-profile")
    if (
        capability.max_input_tokens < 1
        or capability.max_output_tokens < 1
        or capability.usable_input_tokens < 1
        or capability.usable_input_tokens > capability.max_input_tokens
        or capability.input_safety_margin_tokens
        != capability.max_input_tokens - capability.usable_input_tokens
    ):
        raise BlockContractError("block-translation-model-profile")
    digest = hashlib.sha256(encoded).hexdigest()
    witnessed = profile.capability_profile.get("capability_profile_hash")
    if witnessed is not None and witnessed != digest:
        raise BlockContractError("block-translation-model-profile")
    return normalized, digest


def derive_window_profile(
    profile: ModelExecutionProfile,
    *,
    maximum_blocks: int = _MAX_WINDOW_BLOCKS,
    profile_version: str = WINDOW_PROFILE_VERSION,
    target_input_tokens: int | None = None,
) -> WindowProfile:
    """Derive provider capacity and the independent translation-batch target."""

    if (
        not isinstance(maximum_blocks, int)
        or isinstance(maximum_blocks, bool)
        or not 1 <= maximum_blocks <= _MAX_WINDOW_BLOCKS
    ):
        raise BlockContractError("block-translation-window-blocks")
    if profile_version not in _SUPPORTED_WINDOW_PROFILE_VERSIONS:
        raise BlockContractError("block-translation-window-profile-version")
    if target_input_tokens is not None and (
        not isinstance(target_input_tokens, int)
        or isinstance(target_input_tokens, bool)
        or not 1 <= target_input_tokens <= MAX_TRANSLATION_BATCH_INPUT_TOKENS
    ):
        raise BlockContractError("block-translation-window-input-target")
    if profile_version == "1" and target_input_tokens is not None:
        raise BlockContractError("block-translation-window-input-target")
    _raw, capability_hash = _profile_capability(profile)
    capability = ModelCapabilityProfile(**_raw)
    usable = capability.usable_input_tokens
    reserve = min(capability.max_output_tokens, usable - 1)
    if usable < 2 or reserve < 1:
        raise BlockContractError("block-translation-window-capacity")
    return WindowProfile(
        profile_id=WINDOW_PROFILE_ID,
        profile_version=profile_version,
        capacity_version=capability_hash,
        max_blocks=maximum_blocks,
        max_input_tokens=usable,
        output_reserve_tokens=reserve,
        target_input_tokens=(
            None
            if profile_version == "1"
            else min(
                target_input_tokens or DEFAULT_TRANSLATION_BATCH_INPUT_TOKENS,
                usable,
            )
        ),
    )


def _require_window_against_plan(
    plan: TranslationSourcePlan, window: TranslationWindow
) -> tuple[TranslationBlock, ...]:
    blocks = source_index(
        plan, "block-map-v1",
        lambda: MappingProxyType({block.id: block for block in plan.blocks}),
    )
    try:
        chosen = tuple(blocks[block_id] for block_id in window.block_ids)
    except KeyError as exc:
        raise BlockContractError("block-translation-window-owner") from exc
    from .note_relations import window_blocks_are_linked

    if not window_blocks_are_linked(plan, window.block_ids):
        raise BlockContractError("block-translation-window-adjacency")
    expected = tuple(structural_fingerprint(plan, block) for block in chosen)
    if expected != window.block_fingerprints:
        raise BlockContractError("block-translation-window-fingerprint")
    return chosen


def _frozen_target_language(config: Mapping[str, Any]) -> str:
    language = config.get("target_language")
    if not isinstance(language, str) or not SAFE_LANGUAGE_RE.fullmatch(language):
        raise BlockContractError("block-translation-target-language")
    return language


def _translation_context_spec(
    config: Mapping[str, Any],
) -> tuple[str | None, str | None]:
    """Return the frozen context contract and v2 continuity mode, if present."""

    marker = config.get("translation_context")
    if marker == {"contract_version": "translation-context-v1"}:
        return "v1", None
    if isinstance(marker, Mapping) and marker.get("contract_version") == "translation-context-v2":
        if (
            set(marker) != {"contract_version", "continuity_mode"}
            or marker.get("continuity_mode") not in {"self_contained", "retained"}
        ):
            raise BlockContractError("block-translation-context-contract")
        return "v2", cast(str, marker["continuity_mode"])
    return None, None


def _frozen_translation_model(
    config: Mapping[str, Any], profile: ModelExecutionProfile
) -> None:
    translation = config.get("translation")
    if not isinstance(translation, Mapping) or not isinstance(
        translation.get("model"), Mapping
    ):
        raise BlockContractError("block-translation-config")
    model = cast(Mapping[str, Any], translation["model"])
    raw, capability_hash = _profile_capability(profile)
    if (
        model.get("model_id") != profile.model_id
        or model.get("capability_profile_hash") != capability_hash
        or model.get("capability_profile") != raw
    ):
        raise BlockContractError("block-translation-model-drift")


def _slot(
    slot_type: str,
    content: str,
    provenance: Mapping[str, Any],
    artifact_id: str,
    artifact_version: str,
    *,
    required: bool = False,
    dependency: Mapping[str, Any] | None = None,
) -> ContextSlot:
    return ContextSlot(
        slot_type,
        content,
        dict(provenance),
        artifact_id,
        artifact_version,
        sha256_text(content),
        dict(dependency or {}),
        required,
    )


def build_required_slots(
    *,
    plan: TranslationSourcePlan,
    source_plan_id: str,
    window: TranslationWindow,
    frozen_config: Mapping[str, Any],
    register: str = "neutral",
    pin_owner_policy: bool = True,
) -> tuple[ContextSlot, ...]:
    """Build source-safe required slots, retaining owner policy as data only."""

    if not isinstance(source_plan_id, str) or not source_plan_id:
        raise BlockContractError("block-translation-source-plan")
    selected = _require_window_against_plan(plan, window)
    language = _frozen_target_language(frozen_config)
    if register not in _REGISTER_VALUES:
        raise BlockContractError("block-translation-register")
    source = frozen_config.get("source_plan")
    if not isinstance(source, Mapping) or source != {
        "id": source_plan_id,
        "plan_hash": plan.plan_hash,
        "content_hash": plan.snapshot.content_hash,
    }:
        raise BlockContractError("block-translation-source-plan")
    control_content = canonical_json(
        {"contract_version": WINDOW_PROMPT_CONTRACT_VERSION}
    )
    policy = {"target_language": language, "register": register}
    translation_context_version, continuity_mode = _translation_context_spec(
        frozen_config
    )
    translation_context_enabled = translation_context_version in {"v1", "v2"}
    if (
        "foreign_language_policy" in frozen_config
        and not translation_context_enabled
    ):
        raise BlockContractError("block-translation-foreign-language-policy-context")
    if translation_context_enabled:
        foreign_language_policy = frozen_config.get(
            "foreign_language_policy", "preserve-passages-v1"
        )
        if "foreign_language_policy" in frozen_config and (
            foreign_language_policy != "preserve-passages-v2"
        ):
            raise BlockContractError("block-translation-foreign-language-policy")
        policy.update(
            {
                "source_language": frozen_config["source_language"],
                "foreign_language_policy": foreign_language_policy,
            }
        )
    if translation_context_version == "v2":
        policy["context_mode"] = cast(str, continuity_mode)
        if continuity_mode == "retained":
            policy["continuity_lookup"] = {
                "source_plan_id": source_plan_id,
                "before_block_id": selected[0].id,
            }
    policy_content = canonical_json(policy)
    writable_contract_version = frozen_config.get(
        "writable_source_contract_version",
        LEGACY_WRITABLE_SOURCE_CONTRACT_VERSION,
    )
    if writable_contract_version in {
        COMPACT_WRITABLE_SOURCE_CONTRACT_VERSION,
        WRITABLE_SOURCE_CONTRACT_VERSION,
        NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
        STUDY_NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
        SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION,
        SINGLE_COPY_WRITABLE_SOURCE_CONTRACT_VERSION,
        LOCATED_SOURCE_WRITABLE_CONTRACT_VERSION,
    }:
        sentence_aligned = (
            writable_contract_version
            in {
                SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION,
                SINGLE_COPY_WRITABLE_SOURCE_CONTRACT_VERSION,
                LOCATED_SOURCE_WRITABLE_CONTRACT_VERSION,
            }
        )
        annotation_profile = normalize_annotation_profile(
            frozen_config.get("annotation_profile"),
            frozen_config.get("enable_translator_notes"),
        )
        writable_blocks = [
            compact_source_block(
                block,
                number=index,
                implicit_outer_wrappers=(
                    writable_contract_version
                    in {
                        WRITABLE_SOURCE_CONTRACT_VERSION,
                        NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
                        STUDY_NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
                        SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION,
                        SINGLE_COPY_WRITABLE_SOURCE_CONTRACT_VERSION,
                        LOCATED_SOURCE_WRITABLE_CONTRACT_VERSION,
                    }
                ),
                include_sentences=(
                    sentence_aligned
                    and writable_contract_version != LOCATED_SOURCE_WRITABLE_CONTRACT_VERSION
                ),
                locate_sentences=(
                    writable_contract_version == LOCATED_SOURCE_WRITABLE_CONTRACT_VERSION
                ),
            )
            for index, block in enumerate(selected, 1)
        ]
    elif writable_contract_version == PARTS_WRITABLE_SOURCE_CONTRACT_VERSION:
        writable_blocks = [
            {
                "role": block.role,
                "candidate_template": _source_candidate_template(
                    plan=plan,
                    source_plan_id=source_plan_id,
                    block=block,
                    structural_fingerprint_value=fingerprint,
                ),
            }
            for block, fingerprint in zip(
                selected, window.block_fingerprints, strict=True
            )
        ]
    elif writable_contract_version == LEGACY_WRITABLE_SOURCE_CONTRACT_VERSION:
        writable_blocks = [
            {
                "block": block.to_dict(),
                "structural_fingerprint": fingerprint,
            }
            for block, fingerprint in zip(
                selected, window.block_fingerprints, strict=True
            )
        ]
    else:
        raise BlockContractError("block-translation-writable-source-contract")
    writable_payload: dict[str, object] = {
        "contract_version": writable_contract_version,
        "blocks": writable_blocks,
    }
    if writable_contract_version in {
        SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION,
        SINGLE_COPY_WRITABLE_SOURCE_CONTRACT_VERSION,
        LOCATED_SOURCE_WRITABLE_CONTRACT_VERSION,
    }:
        writable_payload = {
            "contract_version": writable_contract_version,
            "annotation_profile": annotation_profile,
            "blocks": writable_blocks,
        }
    if writable_contract_version not in {
        COMPACT_WRITABLE_SOURCE_CONTRACT_VERSION,
        WRITABLE_SOURCE_CONTRACT_VERSION,
        NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
        STUDY_NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
        SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION,
        SINGLE_COPY_WRITABLE_SOURCE_CONTRACT_VERSION,
        LOCATED_SOURCE_WRITABLE_CONTRACT_VERSION,
    }:
        writable_payload.update(
            {
                "source_plan": {"id": source_plan_id, "plan_hash": plan.plan_hash},
                "window": {
                    "order": window.order,
                    "block_ids": list(window.block_ids),
                    "block_fingerprints": list(window.block_fingerprints),
                },
            }
        )
    writable_content = canonical_json(writable_payload)
    slots: list[ContextSlot] = [
        _slot(
            "control_contract",
            control_content,
            {"contract_version": WINDOW_PROMPT_CONTRACT_VERSION},
            "translation-block-window-contract",
            WINDOW_PROMPT_CONTRACT_VERSION,
            required=True,
        ),
        _slot(
            "project_policy",
            policy_content,
            {"policy": policy},
            f"translation-project-policy:{source_plan_id}",
            plan.plan_hash,
            required=True,
        ),
    ]
    owner_policy = frozen_config.get("owner_policy")
    if owner_policy is not None:
        if not isinstance(owner_policy, str) or not owner_policy.strip():
            raise BlockContractError("block-translation-owner-policy")
        owner_content = canonical_json(
            {
                "contract_version": "translation-owner-policy-reference-v1",
                "owner_policy": owner_policy,
            }
        )
        slots.append(
            _slot(
                "document_metadata",
                owner_content,
                {"source": "frozen-owner-policy", "trust": "untrusted_reference"},
                f"translation-owner-policy:{source_plan_id}",
                plan.plan_hash,
                required=pin_owner_policy,
            )
        )
    slots.append(
        _slot(
            "writable_source",
            writable_content,
            {"contract_version": writable_contract_version},
            f"translation-window-source:{source_plan_id}:{window.order}",
            plan.plan_hash,
            required=True,
        )
    )
    return tuple(slots)


def _source_candidate_template(
    *,
    plan: TranslationSourcePlan,
    source_plan_id: str,
    block: TranslationBlock,
    structural_fingerprint_value: str | None = None,
    include_structural_references: bool = True,
) -> dict[str, object]:
    """Expose only the exact target shape and model-writable source strings.

    The canonical source block remains private validation evidence.  Giving a
    provider the full planner DTO also exposes structural-only whitespace and
    host-owned attributes, which medium models can mistake for target literal
    references.  A valid candidate template makes the task smaller and removes
    that ambiguous internal vocabulary from the provider boundary.
    """

    slot_by_id = {slot.id: slot for slot in block.slots}
    literal_reference_ids = {
        slot.id
        for slot in literal_reference_slots(block)
        if include_structural_references or slot.disposition == "literal-preserved"
    }
    parts: list[dict[str, str]] = []
    lexical_attributes: list[dict[str, str]] = []
    for wire_part in block.wire:
        if wire_part.kind == "token":
            parts.append({"kind": "token", "token_id": wire_part.ref_id})
            continue
        slot = slot_by_id[wire_part.ref_id]
        if slot.kind == "attribute":
            if slot.disposition == "translatable":
                lexical_attributes.append(
                    {"slot_id": slot.id, "value": slot.source_value}
                )
            continue
        if slot.id in literal_reference_ids:
            parts.append({"kind": "literal", "literal_slot_id": slot.id})
        elif slot.disposition == "translatable":
            parts.append({"kind": "text", "text": slot.source_value})
    return {
        "contract_version": "target-block-result-v1",
        "source_plan_id": source_plan_id,
        "source_plan_hash": plan.plan_hash,
        "block_id": block.id,
        "structural_fingerprint": structural_fingerprint_value
        or structural_fingerprint(plan, block),
        "parts": parts,
        "lexical_attributes": lexical_attributes,
    }


def _context_block(block: TranslationBlock) -> dict[str, object]:
    text = block.projection
    return {
        "order": block.order,
        "role": block.role,
        "text": text[:_STRUCTURE_TEXT_LIMIT],
        "truncated": len(text) > _STRUCTURE_TEXT_LIMIT,
    }


def _heading_level(block: TranslationBlock) -> int | None:
    if block.role == "document_title":
        return 0
    if block.role != "heading":
        return None
    matches = re.findall(r"(?:^|/)h([1-6])\[\d+\](?:/|$)", block.locator)
    return int(matches[-1]) if matches else None


def _structural_parent(locator: str, tag: str) -> str | None:
    match = re.search(rf"^(.*?/{tag}\[\d+\])(?:/|$)", f"/{locator}")
    return match.group(1).lstrip("/") if match else None


def _row_identity(locator: str) -> tuple[str, int] | None:
    match = re.search(r"^(.*?)/tr\[(\d+)\](?:/|$)", locator)
    return (match.group(1), int(match.group(2))) if match else None


def _source_structure_index(plan: TranslationSourcePlan):
    headings_by_order = {}
    active_headings: dict[int, TranslationBlock] = {}
    tables: dict[str, list[TranslationBlock]] = {}
    for block in plan.blocks:
        level = _heading_level(block)
        if level is not None and block.role != "source_toc_entry":
            if level == 0:
                active_headings = {0: block}
            else:
                active_headings = {
                    key: value for key, value in active_headings.items() if key < level
                }
                active_headings[level] = block
        headings_by_order[block.order] = tuple(
            active_headings[key] for key in sorted(active_headings)
        )
        table = _structural_parent(block.locator, "table")
        if table is not None:
            tables.setdefault(table, []).append(block)
    return (
        MappingProxyType(headings_by_order),
        MappingProxyType({key: tuple(blocks) for key, blocks in tables.items()}),
    )


def structural_context_slot(
    plan: TranslationSourcePlan, window: TranslationWindow
) -> ContextSlot:
    """Build bounded, source-derived section and table context for one window.

    The complete writable blocks remain in ``writable_source``.  This optional
    slot supplies only read-only relationships that would otherwise be lost
    when a valid semantic plan is divided into provider windows.
    """

    selected = _require_window_against_plan(plan, window)
    headings_by_order, table_index = source_index(
        plan, "section-and-table-index-v1", lambda: _source_structure_index(plan),
    )
    first_order = min(block.order for block in selected)
    section_paths: list[dict[str, object]] = [{
        "block_order": first_order,
        "headings": [_context_block(block) for block in headings_by_order[first_order]],
    }]
    table_keys = {
        key for block in selected
        if (key := _structural_parent(block.locator, "table")) is not None
    }
    all_table_keys = list(table_index)
    table_contexts: list[dict[str, object]] = []
    for key in all_table_keys:
        if key not in table_keys or len(table_contexts) >= _STRUCTURE_TABLE_LIMIT:
            continue
        related = table_index[key]
        selected_rows = {
            row_identity
            for block in selected
            if _structural_parent(block.locator, "table") == key
            and (row_identity := _row_identity(block.locator)) is not None
        }
        boundary_rows: set[tuple[str, int]] = set()
        for row_parent in {parent for parent, _row in selected_rows}:
            indexes = sorted(
                row for parent, row in selected_rows if parent == row_parent
            )
            if not indexes:
                continue
            for selected_row in (indexes[0], indexes[-1]):
                boundary_rows.update(
                    (row_parent, row)
                    for row in (selected_row - 1, selected_row, selected_row + 1)
                    if row > 0
                )
        nearby_rows = sorted(boundary_rows)[:_STRUCTURE_ROW_LIMIT]
        captions = [
            _context_block(block) for block in related if block.role == "table_caption"
        ][:_STRUCTURE_CHILD_LIMIT]
        headers = [
            _context_block(block) for block in related if block.role == "table_header"
        ][:_STRUCTURE_CHILD_LIMIT]
        rows: list[dict[str, object]] = []
        for row_parent, row in nearby_rows:
            cells = [
                _context_block(block)
                for block in related
                if _row_identity(block.locator) == (row_parent, row)
            ][:_STRUCTURE_CHILD_LIMIT]
            if cells:
                rows.append({"row": row, "blocks": cells})
        table_contexts.append(
            {
                "table": all_table_keys.index(key) + 1,
                "captions": captions,
                "headers": headers,
                "nearby_rows": rows,
            }
        )

    payload = {
        "contract_version": STRUCTURE_CONTEXT_CONTRACT_VERSION,
        "section_paths": section_paths,
        "tables": table_contexts,
    }
    content = canonical_json(payload)
    while (
        len(content.encode("utf-8")) > _STRUCTURE_CONTEXT_MAX_BYTES and table_contexts
    ):
        table_contexts.pop()
        content = canonical_json(payload)
    if len(content.encode("utf-8")) > _STRUCTURE_CONTEXT_MAX_BYTES:
        raise BlockContractError("block-translation-structure-context-size")
    return _slot(
        "document_structure_context",
        content,
        {"contract_version": STRUCTURE_CONTEXT_CONTRACT_VERSION},
        f"translation-window-structure:{plan.plan_hash}:{window.order}",
        STRUCTURE_CONTEXT_CONTRACT_VERSION,
    )


def continuity_slot(
    source_plan: SourcePlanRecord,
    *,
    plan: TranslationSourcePlan,
    window: TranslationWindow,
    compact: bool = True,
) -> ContextSlot | None:
    """Return only the verified immediately preceding target candidate."""

    selected = _require_window_against_plan(plan, window)
    first = selected[0]
    if first.order == 1:
        return None
    predecessor_block = plan.blocks[first.order - 2]
    pointer = db.session.get(
        TranslationTargetBlockActive,
        {"source_plan_id": source_plan.id, "block_id": predecessor_block.id},
        populate_existing=True,
    )
    alignment = db.session.get(
        TranslationTargetAlignmentActive,
        {"source_plan_id": source_plan.id, "block_id": predecessor_block.id},
        populate_existing=True,
    )
    if pointer is None and alignment is None:
        return None
    if pointer is None or alignment is None:
        raise BlockContractError("block-translation-continuity-pair")
    if (
        pointer.revision_id != alignment.revision_id
        or pointer.version != alignment.version
        or pointer.block_id != predecessor_block.id
        or pointer.source_plan_id != source_plan.id
    ):
        raise BlockContractError("block-translation-continuity-pair")
    verify_active_pair(pointer, alignment)
    revision = db.session.get(
        TranslationTargetBlockRevision, pointer.revision_id, populate_existing=True
    )
    if (
        revision is None
        or revision.source_plan_id != source_plan.id
        or revision.block_id != predecessor_block.id
    ):
        raise BlockContractError("block-translation-continuity-revision")
    candidate = rehydrate_target_revision(
        revision,
        source_plan,
        verified_plan=plan,
    )
    if compact:
        projected = compact_candidate_projection(predecessor_block, candidate)
        contract_version = CONTINUITY_CONTRACT_VERSION
        content = canonical_json(
            {
                "contract_version": contract_version,
                "source_role": predecessor_block.role,
                "source_text": predecessor_block.projection,
                "target": {
                    "content": projected["content"],
                    "fields": projected["fields"],
                },
            }
        )
        artifact_id = f"translation-window-continuity:{window.order}"
        artifact_version = contract_version
    else:
        contract_version = LEGACY_CONTINUITY_CONTRACT_VERSION
        content = canonical_json(
            {
                "contract_version": contract_version,
                "predecessor_block_id": predecessor_block.id,
                "target_candidate": candidate.to_dict(),
            }
        )
        artifact_id = revision.id
        artifact_version = str(pointer.version)
    return _slot(
        "continuity",
        content,
        {"contract_version": contract_version},
        artifact_id,
        artifact_version,
        dependency={
            "predecessor_block_id": predecessor_block.id,
            "revision_id": revision.id,
            "revision_record_hash": revision.revision_record_hash,
            "active_pointer_version": pointer.version,
        },
    )


def _exact_source_key(value: str) -> str:
    """Normalize representation noise without weakening exact-text identity."""

    return " ".join(unicodedata.normalize("NFC", value).split())


def _source_exact_index(plan: TranslationSourcePlan):
    keys: dict[str, str] = {}
    blocks: dict[str, list[TranslationBlock]] = {}
    for block in plan.blocks:
        key = _exact_source_key(block.projection)
        keys[block.id] = key
        if key:
            blocks.setdefault(key, []).append(block)
    return (
        MappingProxyType(keys),
        MappingProxyType({key: tuple(items) for key, items in blocks.items()}),
    )


def exact_precedent_slot(
    source_plan: SourcePlanRecord,
    *,
    plan: TranslationSourcePlan,
    window: TranslationWindow,
) -> ContextSlot | None:
    """Expose active targets for exactly repeated source text.

    Exact repetition is useful even when vector memory is disabled or empty.
    The evidence remains advisory: the model sees established wording and may
    still choose a contextually justified variant for the current occurrence.
    """

    if source_plan.plan_hash != plan.plan_hash:
        raise BlockContractError("block-translation-source-plan")
    selected = _require_window_against_plan(plan, window)
    selected_ids = {block.id for block in selected}
    source_keys, blocks_by_key = source_index(
        plan, "exact-source-index-v1", lambda: _source_exact_index(plan),
    )
    selected_keys = {block.id: source_keys[block.id] for block in selected}
    selected_numbers_by_key: dict[str, list[int]] = {}
    for number, block in enumerate(selected, 1):
        key = selected_keys[block.id]
        if key:
            selected_numbers_by_key.setdefault(key, []).append(number)
    repeated = {
        block.id: tuple(
            candidate
            for candidate in blocks_by_key.get(selected_keys[block.id], ())
            if candidate.id not in selected_ids
        )
        for block in selected
    }
    candidate_ids = {
        candidate.id for candidates in repeated.values() for candidate in candidates
    }
    pointers = (
        {
            pointer.block_id: pointer
            for pointer in (
                TranslationTargetBlockActive.query.populate_existing()
                .filter(
                    TranslationTargetBlockActive.source_plan_id == source_plan.id,
                    TranslationTargetBlockActive.block_id.in_(candidate_ids),
                )
                .all()
            )
        }
        if candidate_ids
        else {}
    )
    alignments = (
        {
            alignment.block_id: alignment
            for alignment in (
                TranslationTargetAlignmentActive.query.populate_existing()
                .filter(
                    TranslationTargetAlignmentActive.source_plan_id == source_plan.id,
                    TranslationTargetAlignmentActive.block_id.in_(candidate_ids),
                )
                .all()
            )
        }
        if candidate_ids
        else {}
    )
    revision_ids = {pointer.revision_id for pointer in pointers.values()}
    revisions = (
        {
            revision.id: revision
            for revision in (
                TranslationTargetBlockRevision.query.populate_existing()
                .filter(TranslationTargetBlockRevision.id.in_(revision_ids))
                .all()
            )
        }
        if revision_ids
        else {}
    )

    dependency_rows: dict[str, dict[str, object]] = {}
    block_rows: list[dict[str, object]] = []
    for number, block in enumerate(selected, 1):
        available: list[tuple[Any, Any, Any, dict[str, object]]] = []
        for candidate_block in repeated[block.id]:
            pointer = pointers.get(candidate_block.id)
            alignment = alignments.get(candidate_block.id)
            if pointer is None and alignment is None:
                continue
            if pointer is None or alignment is None:
                raise BlockContractError("block-translation-exact-precedent-pair")
            if (
                pointer.revision_id != alignment.revision_id
                or pointer.version != alignment.version
                or pointer.block_id != candidate_block.id
                or pointer.source_plan_id != source_plan.id
            ):
                raise BlockContractError("block-translation-exact-precedent-pair")
            verify_active_pair(pointer, alignment)
            revision = revisions.get(pointer.revision_id)
            if (
                revision is None
                or revision.source_plan_id != source_plan.id
                or revision.block_id != candidate_block.id
            ):
                raise BlockContractError("block-translation-exact-precedent-revision")
            projected = compact_candidate_projection(
                candidate_block,
                rehydrate_target_revision(
                    revision,
                    source_plan,
                    verified_plan=plan,
                ),
            )
            target = {
                "content": projected["content"],
                "fields": projected["fields"],
            }
            available.append((candidate_block, pointer, revision, target))

        available.sort(
            key=lambda item: (
                0 if item[2].author_type == "human" else 1,
                0 if item[0].order < block.order else 1,
                abs(item[0].order - block.order),
                item[0].order,
            )
        )
        grouped: dict[str, dict[str, object]] = {}
        for candidate_block, pointer, revision, target in available:
            target_key = canonical_json(target)
            if (
                target_key not in grouped
                and len(grouped) >= _MAX_EXACT_PRECEDENT_VARIANTS
            ):
                continue
            group = grouped.setdefault(
                target_key,
                {"target": target, "occurrences": []},
            )
            occurrences = cast(list[dict[str, object]], group["occurrences"])
            if len(occurrences) >= _MAX_EXACT_PRECEDENT_OCCURRENCES:
                continue
            occurrences.append(
                {
                    "source_order": candidate_block.order,
                    "source_role": candidate_block.role,
                    "origin": revision.author_type,
                }
            )
            dependency_rows[candidate_block.id] = {
                "block_id": candidate_block.id,
                "revision_id": revision.id,
                "revision_record_hash": revision.revision_record_hash,
                "active_pointer_version": pointer.version,
            }

        same_window = [
            other
            for other in selected_numbers_by_key.get(selected_keys[block.id], [])
            if other != number
        ]
        if not grouped and not same_window:
            continue
        block_rows.append(
            {
                "n": number,
                "source_role": block.role,
                "source_text": block.projection,
                "same_window_ns": same_window,
                "precedents": list(grouped.values()),
            }
        )

    if not block_rows:
        return None
    content = canonical_json(
        {
            "contract_version": EXACT_PRECEDENT_CONTRACT_VERSION,
            "guidance": (
                "Prefer a human-edited precedent, then the nearest preceding wording. "
                "same_window_ns identifies other writable occurrences of the same source; "
                "normally render those consistently. A deliberate contextual variant is "
                "allowed."
            ),
            "blocks": block_rows,
        }
    )
    dependency: dict[str, object] = {}
    if dependency_rows:
        dependency = {
            "source_plan_id": source_plan.id,
            "active_targets": list(dependency_rows.values()),
        }
    return _slot(
        "exact_precedent",
        content,
        {
            "contract_version": EXACT_PRECEDENT_CONTRACT_VERSION,
            "normalization": "unicode-nfc-whitespace-v1",
        },
        f"translation-window-exact-precedent:{window.order}",
        EXACT_PRECEDENT_CONTRACT_VERSION,
        dependency=dependency,
    )


def settings_evidence(workflow_config: Any, profile: WindowProfile) -> dict[str, Any]:
    """Return stable settings evidence without copying secret configuration text."""

    def read(name: str) -> Any:
        return (
            workflow_config.get(name)
            if isinstance(workflow_config, Mapping)
            else getattr(workflow_config, name, None)
        )

    config_id = read("id")
    config_hash = read("config_hash")
    record_hash = read("record_hash") or read("config_receipt")
    if (
        not isinstance(config_id, str)
        or not config_id
        or not isinstance(config_hash, str)
        or len(config_hash) != 64
        or not isinstance(record_hash, str)
        or len(record_hash) != 64
    ):
        raise BlockContractError("block-translation-workflow-config")
    return {
        "contract_version": SETTINGS_EVIDENCE_CONTRACT_VERSION,
        "workflow_config": {
            "id": config_id,
            "record_hash": record_hash,
            "config_hash": config_hash,
        },
        "window_profile": profile.to_dict(),
    }


def _optional_slots(
    slots: Sequence[ContextSlot], *, source: str
) -> tuple[ContextSlot, ...]:
    result = tuple(slots)
    if any(not isinstance(slot, ContextSlot) for slot in result):
        raise BlockContractError("block-translation-context-slot")
    if any(slot.slot_type in _PROTECTED_SLOT_TYPES for slot in result):
        raise BlockContractError("block-translation-context-slot")
    allowed = {
        "baseline": {
            "document_metadata",
            "document_style_brief",
            "structural_context",
            "glossary_constraints",
            "research_evidence",
            "terminology_guidance",
        },
        "memory": {"translation_memory"},
    }
    if source not in allowed or any(
        slot.slot_type not in allowed[source] for slot in result
    ):
        raise BlockContractError("block-translation-context-slot")
    # A repeated full slot changes prompt evidence unexpectedly and is never
    # useful; unique artifact identity still permits distinct same-kind facts.
    identities = [
        (slot.slot_type, slot.artifact_id, slot.artifact_version) for slot in result
    ]
    if len(identities) != len(set(identities)):
        raise BlockContractError("block-translation-context-slot")
    return result


def baseline_slots_for_window_profile(
    slots: Sequence[ContextSlot], profile: WindowProfile
) -> tuple[ContextSlot, ...]:
    """Freeze selected baseline evidence as non-discardable after profile v1.

    Version 1 is retained solely to reconstruct existing durable attempts. New
    operations use the current profile, where choosing analysis, research,
    terminology, or glossary evidence means source packing must make room for
    it instead of silently deleting it during admission.
    """

    baseline = _optional_slots(slots, source="baseline")
    if profile.profile_version == "1":
        return baseline
    if profile.profile_version not in {"2", "3", "4", WINDOW_PROFILE_VERSION}:
        raise BlockContractError("block-translation-window-profile-version")
    return tuple(_pin_selected_context(slot, profile) for slot in baseline)


def _pin_selected_context(slot: ContextSlot, profile: WindowProfile) -> ContextSlot:
    """Keep deliberately selected evidence; shrink writable source instead."""

    if profile.profile_version == "1" or slot.required:
        return slot
    if profile.profile_version not in {"2", "3", "4", WINDOW_PROFILE_VERSION}:
        raise BlockContractError("block-translation-window-profile-version")
    return replace(slot, required=True)


def _output_skeleton(
    *,
    plan: TranslationSourcePlan,
    source_plan_id: str,
    blocks: Sequence[TranslationBlock],
    writable_source_contract_version: str = WRITABLE_SOURCE_CONTRACT_VERSION,
) -> str:
    """Render a conservative valid-shape projection for output admission."""

    if writable_source_contract_version not in {
        LEGACY_WRITABLE_SOURCE_CONTRACT_VERSION,
        PARTS_WRITABLE_SOURCE_CONTRACT_VERSION,
        COMPACT_WRITABLE_SOURCE_CONTRACT_VERSION,
        WRITABLE_SOURCE_CONTRACT_VERSION,
        NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
        STUDY_NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
        SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION,
        SINGLE_COPY_WRITABLE_SOURCE_CONTRACT_VERSION,
        LOCATED_SOURCE_WRITABLE_CONTRACT_VERSION,
    }:
        raise BlockContractError("block-translation-writable-source-contract")

    if writable_source_contract_version in {
        COMPACT_WRITABLE_SOURCE_CONTRACT_VERSION,
        WRITABLE_SOURCE_CONTRACT_VERSION,
        NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
        STUDY_NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
        SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION,
        SINGLE_COPY_WRITABLE_SOURCE_CONTRACT_VERSION,
        LOCATED_SOURCE_WRITABLE_CONTRACT_VERSION,
    }:
        results = [
            {
                "n": index,
                "content": compact["content"],
                "fields": compact["fields"],
            }
            for index, block in enumerate(blocks, 1)
            for compact in (
                compact_source_block(
                    block,
                    number=index,
                    implicit_outer_wrappers=(
                        writable_source_contract_version
                        in {
                            WRITABLE_SOURCE_CONTRACT_VERSION,
                            NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
                            STUDY_NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
                            SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION,
                            SINGLE_COPY_WRITABLE_SOURCE_CONTRACT_VERSION,
                            LOCATED_SOURCE_WRITABLE_CONTRACT_VERSION,
                        }
                    ),
                    include_sentences=(
                        writable_source_contract_version
                        in {
                            SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION,
                            SINGLE_COPY_WRITABLE_SOURCE_CONTRACT_VERSION,
                        }
                    ),
                    locate_sentences=(
                        writable_source_contract_version
                        == LOCATED_SOURCE_WRITABLE_CONTRACT_VERSION
                    ),
                ),
            )
        ]
        if writable_source_contract_version in {
            NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
            STUDY_NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
        }:
            for result in results:
                result["notes"] = []
        if (
            writable_source_contract_version
            in {
                SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION,
                SINGLE_COPY_WRITABLE_SOURCE_CONTRACT_VERSION,
                LOCATED_SOURCE_WRITABLE_CONTRACT_VERSION,
            }
        ):
            for result, block in zip(results, blocks, strict=True):
                result["notes"] = []
                result["alignment"] = {
                    "target_sentences": [
                        block.projection[anchor.start : anchor.end]
                        for anchor in block.anchors
                    ],
                    "groups": [
                        {
                            "source": [anchor.order],
                            "target": [anchor.order],
                        }
                        for anchor in block.anchors
                    ],
                }
                if writable_source_contract_version in {
                    SINGLE_COPY_WRITABLE_SOURCE_CONTRACT_VERSION,
                    LOCATED_SOURCE_WRITABLE_CONTRACT_VERSION,
                }:
                    del result["alignment"]["target_sentences"]
                    # This is a size projection, not a translation candidate.
                    # Charge marker bytes once without a second prose copy.
                    markers = "<sb/>" * len(block.anchors)
                    if not result["content"] and result["fields"]:
                        for field in result["fields"]:
                            field["value"] = html.escape(field["value"], quote=False) + markers
                    else:
                        result["content"] += markers
    else:
        results = [
            _source_candidate_template(
                plan=plan,
                source_plan_id=source_plan_id,
                block=block,
                include_structural_references=(
                    writable_source_contract_version
                    == PARTS_WRITABLE_SOURCE_CONTRACT_VERSION
                ),
            )
            for block in blocks
        ]
    return canonical_json({"results": results})


def estimate_window_output_tokens(
    *,
    plan: TranslationSourcePlan,
    source_plan_id: str,
    blocks: Sequence[TranslationBlock],
    model_id: str,
    token_counter: Callable[..., Any] | None = None,
    writable_source_contract_version: str = WRITABLE_SOURCE_CONTRACT_VERSION,
) -> int:
    """Estimate target JSON size with translation-expansion headroom."""

    skeleton = _output_skeleton(
        plan=plan,
        source_plan_id=source_plan_id,
        blocks=blocks,
        writable_source_contract_version=writable_source_contract_version,
    )
    counted: int | None = None
    if callable(token_counter):
        try:
            counted = max(1, int(token_counter(model=model_id, text=skeleton)))
        except Exception:  # noqa: BLE001 - injected tokenizer is a provider boundary.
            counted = None
    if counted is None:
        counted = max(1, math.ceil(len(skeleton) / 4))
    return math.ceil(counted * _OUTPUT_GROWTH_NUMERATOR / _OUTPUT_GROWTH_DENOMINATOR)


def _window_output_reserve_tokens(
    *,
    plan: TranslationSourcePlan,
    source_plan_id: str,
    blocks: Sequence[TranslationBlock],
    execution_profile: ModelExecutionProfile,
    window_profile: WindowProfile,
    token_counter: Callable[..., Any] | None,
    writable_source_contract_version: str,
) -> tuple[int, int]:
    """Return the visible-output estimate and context-admission reserve.

    V3 and V4 reserve enough capacity for estimated target JSON plus explicit
    headroom instead of treating the model's advertised output maximum as a
    sensible per-window size. V4 deliberately leaves the provider generation
    limit unset; this value remains durable solely so input plus a plausible
    answer can be admitted safely. V1 through V3 retain their historical
    evidence and provider projection for exact reconstruction.
    """

    if window_profile.profile_version in {"1", "2"}:
        return (
            window_profile.output_reserve_tokens,
            window_profile.output_reserve_tokens,
        )
    estimated = estimate_window_output_tokens(
        plan=plan,
        source_plan_id=source_plan_id,
        blocks=blocks,
        model_id=execution_profile.model_id,
        token_counter=token_counter,
        writable_source_contract_version=writable_source_contract_version,
    )
    requested = math.ceil(
        estimated * _OUTPUT_HEADROOM_DENOMINATOR / _OUTPUT_HEADROOM_NUMERATOR
    )
    return estimated, min(window_profile.output_reserve_tokens, max(1, requested))


def _window_output_fits(
    *,
    plan: TranslationSourcePlan,
    source_plan_id: str,
    blocks: Sequence[TranslationBlock],
    execution_profile: ModelExecutionProfile,
    window_profile: WindowProfile,
    token_counter: Callable[..., Any] | None,
    writable_source_contract_version: str,
) -> bool:
    if window_profile.profile_version == "1":
        return True
    estimated = estimate_window_output_tokens(
        plan=plan,
        source_plan_id=source_plan_id,
        blocks=blocks,
        model_id=execution_profile.model_id,
        token_counter=token_counter,
        writable_source_contract_version=writable_source_contract_version,
    )
    usable = math.floor(
        window_profile.output_reserve_tokens
        * _OUTPUT_HEADROOM_NUMERATOR
        / _OUTPUT_HEADROOM_DENOMINATOR
    )
    return estimated <= max(1, usable)


@reuse_note_relations
@reuse_source_indexes
def build_window_material(
    *,
    plan: TranslationSourcePlan,
    source_plan_id: str,
    window: TranslationWindow,
    frozen_config: Mapping[str, Any],
    workflow_config: Any,
    execution_profile: ModelExecutionProfile,
    window_profile: WindowProfile | None = None,
    register: str = "neutral",
    baseline_slots: Sequence[ContextSlot] = (),
    contextual_memory: ContextSlot | None = None,
    live_glossary: ContextSlot | None = None,
    source_plan_record: SourcePlanRecord | None = None,
    include_continuity: bool = True,
    token_counter: Callable[..., Any] | None = None,
) -> WindowMaterial:
    """Render and admit one exact packet.  It never persists or calls a model."""

    if not isinstance(frozen_config, Mapping):
        raise BlockContractError("block-translation-config")
    if source_plan_record is not None and (
        source_plan_record.id != source_plan_id
        or source_plan_record.plan_hash != plan.plan_hash
    ):
        raise BlockContractError("block-translation-source-plan")
    profile = window_profile or derive_window_profile(execution_profile)
    derived = derive_window_profile(
        execution_profile,
        maximum_blocks=profile.max_blocks,
        profile_version=profile.profile_version,
        target_input_tokens=profile.target_input_tokens,
    )
    if profile != derived:
        raise BlockContractError("block-translation-window-profile")
    _frozen_translation_model(frozen_config, execution_profile)
    required = build_required_slots(
        plan=plan,
        source_plan_id=source_plan_id,
        window=window,
        frozen_config=frozen_config,
        register=register,
        pin_owner_policy=profile.profile_version != "1",
    )
    baseline = baseline_slots_for_window_profile(baseline_slots, profile)
    translation_context_version, continuity_mode = _translation_context_spec(
        frozen_config
    )
    structure_context = frozen_config.get("document_structure_context")
    optional: list[ContextSlot] = list(baseline)
    if live_glossary is not None:
        if live_glossary.slot_type != "terminology_guidance":
            raise BlockContractError("glossary-window-slot")
        optional.append(live_glossary)
    if translation_context_version in {"v1", "v2"}:
        from .linked_note_context import linked_notes_slot

        linked = linked_notes_slot(
            plan, window, source_plan_id=source_plan_id, source_plan=source_plan_record,
        )
        if linked is not None:
            optional.append(linked)
    if structure_context == {"contract_version": STRUCTURE_CONTEXT_CONTRACT_VERSION}:
        optional.insert(
            0,
            _pin_selected_context(structural_context_slot(plan, window), profile),
        )
    if contextual_memory is not None:
        if (
            not isinstance(contextual_memory, ContextSlot)
            or contextual_memory.slot_type != "translation_memory"
        ):
            raise BlockContractError("block-translation-memory-slot")
        memory_slots = _optional_slots((contextual_memory,), source="memory")
        optional.extend(
            memory_slots
            if translation_context_version == "v2"
            else tuple(_pin_selected_context(slot, profile) for slot in memory_slots)
        )
    # Like immediate continuity, exact precedent is dynamic evidence and is
    # intentionally omitted from provider-free fit planning. Final assembly
    # supplies the source-plan record and pins any resulting slot.
    if (
        profile.profile_version == WINDOW_PROFILE_VERSION
        and source_plan_record is not None
    ):
        if translation_context_version == "v2":
            from .sentence_precedent import sentence_precedent_v2_slot

            sentence_precedent = sentence_precedent_v2_slot(
                source_plan_record,
                plan=plan,
                window=window,
            )
            if sentence_precedent is not None:
                optional.append(sentence_precedent)
        else:
            precedent = exact_precedent_slot(
                source_plan_record,
                plan=plan,
                window=window,
            )
            if precedent is not None:
                optional.append(_pin_selected_context(precedent, profile))
            if translation_context_version == "v1":
                from .sentence_precedent import sentence_precedent_slot

                sentence_precedent = sentence_precedent_slot(
                    source_plan_record, plan=plan, window=window
                )
                if sentence_precedent is not None:
                    optional.append(sentence_precedent)
    if include_continuity:
        if source_plan_record is None:
            raise BlockContractError("block-translation-continuity-source-plan")
        if translation_context_version == "v2":
            from .segment_context import segment_continuity_slot

            slot = segment_continuity_slot(
                source_plan_record,
                plan=plan,
                window=window,
                mode=cast(str, continuity_mode),
            )
        else:
            slot = continuity_slot(
                source_plan_record,
                plan=plan,
                window=window,
                compact=profile.profile_version == WINDOW_PROFILE_VERSION,
            )
        if slot is not None:
            optional.append(
                slot
                if translation_context_version == "v2"
                else _pin_selected_context(slot, profile)
            )
    writable = required[-1]
    slots = (*required[:-1], *optional, writable)
    language = _frozen_target_language(frozen_config)
    blocks = _require_window_against_plan(plan, window)
    writable_source_contract_version = str(
        frozen_config.get(
            "writable_source_contract_version",
            LEGACY_WRITABLE_SOURCE_CONTRACT_VERSION,
        )
    )
    estimated_output_tokens, output_reserve_tokens = _window_output_reserve_tokens(
        plan=plan,
        source_plan_id=source_plan_id,
        blocks=blocks,
        execution_profile=execution_profile,
        window_profile=profile,
        token_counter=token_counter,
        writable_source_contract_version=writable_source_contract_version,
    )

    def admit_for_window(item: ProviderPacket) -> Mapping[str, Any]:
        evidence = admit_packet(
            item,
            execution_profile,
            token_counter=token_counter,
        )
        target = profile.target_input_tokens
        if profile.profile_version == "1" or target is None:
            return evidence
        measurement = evidence.get("measurement")
        measured = (
            measurement.get("total_input_tokens")
            if isinstance(measurement, Mapping)
            else None
        )
        if (
            evidence.get("admitted") is True
            and isinstance(measured, int)
            and not isinstance(measured, bool)
            and measured > target
        ):
            rejected = dict(evidence)
            rejected["admitted"] = False
            rejected["overflow_tokens"] = measured - target
            rejected["reason"] = "translation_batch_input_target_exceeded"
            return rejected
        return evidence

    packet, admission = render_admitted_packet(
        slots=slots,
        target_language=language,
        requested_output_tokens=output_reserve_tokens,
        native_json_schema=execution_profile.transport == "native_json_schema",
        admit=admit_for_window,
    )
    if not bool(admission.get("admitted")):
        raise BlockContractError("block-translation-window-admission")
    if profile.target_input_tokens is not None:
        admission = {
            **admission,
            "translation_batch_input_target_tokens": profile.target_input_tokens,
        }
        if profile.profile_version in {"3", "4", WINDOW_PROFILE_VERSION}:
            admission = {
                **admission,
                "translation_batch_estimated_output_tokens": estimated_output_tokens,
                (
                    "translation_batch_output_reserve_tokens"
                    if profile.profile_version in {"4", WINDOW_PROFILE_VERSION}
                    else "translation_batch_requested_output_tokens"
                ): output_reserve_tokens,
            }
    predecessor = (
        predecessor_snapshot(source_plan_record, window)
        if source_plan_record is not None
        else None
    )
    return WindowMaterial(
        window=window,
        slots=packet.slots,
        packet=packet,
        admission=admission,
        settings=settings_evidence(workflow_config, profile),
        model_profile=execution_profile.to_dict(),
        predecessor=predecessor,
    )


def window_fits_callback(
    *,
    plan: TranslationSourcePlan,
    source_plan_id: str,
    window_order: int,
    frozen_config: Mapping[str, Any],
    workflow_config: Any,
    execution_profile: ModelExecutionProfile,
    window_profile: WindowProfile,
    register: str = "neutral",
    baseline_slots: Sequence[ContextSlot] = (),
    contextual_memory_for: Callable[[tuple[TranslationBlock, ...]], ContextSlot | None]
    | None = None,
    live_glossary_for: Callable[[TranslationWindow], ContextSlot | None]
    | None = None,
    source_plan_record: SourcePlanRecord | None = None,
    token_counter: Callable[..., Any] | None = None,
) -> Callable[[tuple[TranslationBlock, ...]], bool]:
    """Return a ``plan_windows`` compatible, provider-free exact-admission test.

    ``plan_windows`` cannot vary its callback by emitted order.  For planning a
    complete document use :func:`plan_ordered_window_materials`, which binds the
    real order before admission.  This helper is intended for a known order or
    focused retry planning only.

    When ``live_glossary_for`` is supplied, the candidate window's relevant
    terminology snapshot joins the fit estimation, so packing accounts for
    glossary growth instead of failing later at assembly.  Without it the
    legacy behavior is preserved exactly. A source-plan record enables known
    continuity and precedent during a retry of the next unissued window.
    """

    def fits(blocks: tuple[TranslationBlock, ...]) -> bool:
        if not blocks:
            return False
        window = TranslationWindow(
            window_order,
            tuple(block.id for block in blocks),
            tuple(structural_fingerprint(plan, block) for block in blocks),
        )
        memory = (
            contextual_memory_for(blocks) if contextual_memory_for is not None else None
        )
        try:
            if live_glossary_for is not None:
                # Cheap pre-check without live terminology first: a required
                # glossary slot can only grow the packet, so a scope that
                # already overflows cannot fit with it.  This avoids a
                # terminology read per failing candidate during binary search;
                # the glossary query below runs only for scopes that fit
                # without it.  The with-glossary build remains decisive.
                build_window_material(
                    plan=plan,
                    source_plan_id=source_plan_id,
                    window=window,
                    frozen_config=frozen_config,
                    workflow_config=workflow_config,
                    execution_profile=execution_profile,
                    window_profile=window_profile,
                    register=register,
                    baseline_slots=baseline_slots,
                    contextual_memory=memory,
                    source_plan_record=source_plan_record,
                    include_continuity=source_plan_record is not None,
                    token_counter=token_counter,
                )
            live_glossary = (
                live_glossary_for(window) if live_glossary_for is not None else None
            )
            build_window_material(
                plan=plan,
                source_plan_id=source_plan_id,
                window=window,
                frozen_config=frozen_config,
                workflow_config=workflow_config,
                execution_profile=execution_profile,
                window_profile=window_profile,
                register=register,
                baseline_slots=baseline_slots,
                contextual_memory=memory,
                live_glossary=live_glossary,
                source_plan_record=source_plan_record,
                include_continuity=source_plan_record is not None,
                token_counter=token_counter,
            )
        except BlockContractError as exc:
            if exc.code == "window-oversized-block":
                return False
            if (
                exc.code
                in {"block-translation-window-admission", "glossary-window-budget"}
                and live_glossary_for is not None
            ):
                return False
            raise
        return _window_output_fits(
            plan=plan,
            source_plan_id=source_plan_id,
            blocks=blocks,
            execution_profile=execution_profile,
            window_profile=window_profile,
            token_counter=token_counter,
            writable_source_contract_version=str(
                frozen_config.get(
                    "writable_source_contract_version",
                    LEGACY_WRITABLE_SOURCE_CONTRACT_VERSION,
                )
            ),
        )

    return fits


@reuse_note_relations
@reuse_source_indexes
def plan_ordered_window_materials(
    *,
    plan: TranslationSourcePlan,
    source_plan_id: str,
    frozen_config: Mapping[str, Any],
    workflow_config: Any,
    execution_profile: ModelExecutionProfile,
    window_profile: WindowProfile | None = None,
    register: str = "neutral",
    baseline_slots: Sequence[ContextSlot] = (),
    contextual_memory_for: Callable[[tuple[TranslationBlock, ...]], ContextSlot | None]
    | None = None,
    live_glossary_for: Callable[[TranslationWindow], ContextSlot | None]
    | None = None,
    source_plan_record: SourcePlanRecord | None = None,
    start_order: int = 1,
    token_counter: Callable[..., Any] | None = None,
    excluded_block_ids: Sequence[str] = (),
    max_windows: int | None = None,
) -> PlannedWindows:
    """Plan whole adjacent blocks with their actual packet order and admission.

    Continuity is normally omitted: it becomes knowable only after the preceding
    window has atomically activated. Retrying the next unissued window may supply
    its source-plan record to include known continuity and precedent in admission.
    Source, trusted policy and selected required context are never dropped.

    When ``live_glossary_for`` is supplied, each candidate window's relevant
    terminology snapshot joins both the fit estimation and the returned
    materials, so a growing live glossary shrinks packing instead of failing
    later at assembly.  Without it the legacy behavior is preserved exactly.
    ``start_order`` numbers the first planned window; tail repacking passes the
    next unissued order so issued prefixes keep their recorded orders.  A
    singleton that cannot fit even alone still raises
    ``window-oversized-block``: the budget is too small for one complete block
    plus required context, and the caller must grow it with the batch-settings
    update instead of dropping evidence.  When ``max_windows`` is set, at most
    that many leading windows are planned; the returned prefix is identical to
    the first ``max_windows`` windows of the unbounded plan for the same
    inputs, so a caller can plan the next unissued window on demand.
    """

    if (
        not isinstance(start_order, int)
        or isinstance(start_order, bool)
        or start_order < 1
    ):
        raise BlockContractError("block-translation-window-order")
    if max_windows is not None and (
        not isinstance(max_windows, int)
        or isinstance(max_windows, bool)
        or max_windows < 1
    ):
        raise BlockContractError("block-translation-max-windows")
    profile = window_profile or derive_window_profile(execution_profile)
    excluded = frozenset(excluded_block_ids)
    plan_block_ids = {block.id for block in plan.blocks}
    if (
        len(excluded) != len(tuple(excluded_block_ids))
        or any(not isinstance(block_id, str) for block_id in excluded)
        or not excluded.issubset(plan_block_ids)
    ):
        raise BlockContractError("block-translation-excluded-blocks")
    materials: list[WindowMaterial] = []
    translation_context_version, _continuity_mode = _translation_context_spec(
        frozen_config
    )
    linked_context = translation_context_version in {"v1", "v2"}
    if linked_context:
        from .note_relations import build_note_relations

        relations = build_note_relations(plan)
        linked_note_ids = {block.id for bodies in relations.notes.values() for block in bodies}
    else:
        relations = None
        linked_note_ids = set()
    assigned = set(excluded)
    index = 0
    while len(assigned) < len(plan.blocks) and (
        max_windows is None or len(materials) < max_windows
    ):
        if linked_context:
            # Notes already bundled with an earlier reference are skipped; notes
            # before their first reference wait for that passage when in scope.
            index = next((position for position, block in enumerate(plan.blocks) if block.id not in assigned and block.id not in linked_note_ids), -1)
            if index < 0:
                index = next(position for position, block in enumerate(plan.blocks) if block.id not in assigned)
        while index < len(plan.blocks) and plan.blocks[index].id in assigned:
            index += 1
        if index >= len(plan.blocks):
            break
        run_end = index
        body_run = plan.blocks[index].id not in linked_note_ids
        while run_end < len(plan.blocks) and plan.blocks[run_end].id not in assigned and not (linked_context and body_run and plan.blocks[run_end].id in linked_note_ids):
            run_end += 1
        order = start_order + len(materials)
        singleton = (plan.blocks[index],)
        fits = window_fits_callback(
            plan=plan,
            source_plan_id=source_plan_id,
            window_order=order,
            frozen_config=frozen_config,
            workflow_config=workflow_config,
            execution_profile=execution_profile,
            window_profile=profile,
            register=register,
            baseline_slots=baseline_slots,
            contextual_memory_for=contextual_memory_for,
            live_glossary_for=live_glossary_for,
            source_plan_record=source_plan_record,
            token_counter=token_counter,
        )
        if not fits(singleton):
            raise BlockContractError("window-oversized-block")

        deferred_targets: set[str] = set()

        def augmented(
            base: tuple[TranslationBlock, ...],
            *,
            _relations=relations,
            _deferred_targets=deferred_targets,
            _assigned=assigned,
            _plan=plan,
        ) -> tuple[TranslationBlock, ...]:
            if _relations is None:
                return base
            base_ids = {block.id for block in base}
            extra_ids = {
                note.id
                for block in base
                for target_id in _relations.references.get(block.id, ())
                if target_id not in _deferred_targets
                for note in _relations.notes[target_id]
                if note.id not in _assigned and note.id not in base_ids
            }
            return base + tuple(
                block for block in _plan.blocks if block.id in extra_ids
            )

        if relations is not None:
            # A note that cannot fit with even this one passage remains a
            # separate writable block. Its bounded relationship context remains
            # available in both packets, without changing the translation scope.
            targets = relations.references.get(singleton[0].id, ())
            deferred_targets.update(targets)
            for target_id in targets:
                deferred_targets.remove(target_id)
                proposed = augmented(singleton)
                if len(proposed) > profile.max_blocks or not fits(proposed):
                    deferred_targets.add(target_id)

        def prefix_fits(
            end: int,
            *,
            _index=index,
            _plan=plan,
            _augmented=augmented,
            _profile=profile,
            _fits=fits,
        ) -> bool:
            proposed = _augmented(tuple(_plan.blocks[_index:end]))
            return len(proposed) <= _profile.max_blocks and _fits(proposed)

        limit = min(run_end, index + profile.max_blocks)
        if limit == index + 1 or prefix_fits(limit):
            end = limit
        else:
            # Required source grows monotonically; optional evidence is reduced
            # before admission fails.  Binary search therefore finds the
            # largest exact complete-block packet without making block count a
            # practical planning budget.
            low = index + 2
            high = limit - 1
            end = index + 1
            while low <= high:
                candidate_end = (low + high) // 2
                if prefix_fits(candidate_end):
                    end = candidate_end
                    low = candidate_end + 1
                else:
                    high = candidate_end - 1
        chosen = augmented(tuple(plan.blocks[index:end]))
        window = TranslationWindow(
            order,
            tuple(block.id for block in chosen),
            tuple(structural_fingerprint(plan, block) for block in chosen),
        )
        memory = (
            contextual_memory_for(chosen) if contextual_memory_for is not None else None
        )
        live_glossary = (
            live_glossary_for(window) if live_glossary_for is not None else None
        )
        materials.append(
            build_window_material(
                plan=plan,
                source_plan_id=source_plan_id,
                window=window,
                frozen_config=frozen_config,
                workflow_config=workflow_config,
                execution_profile=execution_profile,
                window_profile=profile,
                register=register,
                baseline_slots=baseline_slots,
                contextual_memory=memory,
                live_glossary=live_glossary,
                source_plan_record=source_plan_record,
                include_continuity=source_plan_record is not None,
                token_counter=token_counter,
            )
        )
        assigned.update(block.id for block in chosen)
        index = end
    return PlannedWindows(profile=profile, materials=tuple(materials))


@reuse_source_indexes
def plan_unissued_tail_materials(
    *,
    plan: TranslationSourcePlan,
    source_plan_id: str,
    frozen_config: Mapping[str, Any],
    workflow_config: Any,
    execution_profile: ModelExecutionProfile,
    window_profile: WindowProfile,
    register: str = "neutral",
    baseline_slots: Sequence[ContextSlot] = (),
    contextual_memory_for: Callable[[tuple[TranslationBlock, ...]], ContextSlot | None]
    | None = None,
    live_glossary_for: Callable[[TranslationWindow], ContextSlot | None]
    | None = None,
    source_plan_record: SourcePlanRecord | None = None,
    start_order: int,
    fixed_block_ids: Sequence[str],
    token_counter: Callable[..., Any] | None = None,
    max_windows: int | None = None,
) -> tuple[WindowMaterial, ...]:
    """Repack only the not-yet-issued tail with glossary-aware fit.

    ``fixed_block_ids`` carries every immutable scope: the frozen translation
    exclusions, all durably issued attempt blocks, and the already-processed
    prefix of the current run.  Those blocks keep their recorded orders,
    packets, and snapshots; only the remainder is repartitioned, numbered from
    ``start_order``.  No durable contract changes: the caller rebuilds each
    authoritative packet with full context before dispatch.  A singleton that
    cannot fit even alone raises ``window-oversized-block``; the budget must
    then grow through the batch-settings update, and nothing is silently
    discarded.  When ``max_windows`` is set, only that many leading
    repacked windows are returned; the prefix still matches the unbounded
    repack for the same inputs.
    """

    if (
        not isinstance(start_order, int)
        or isinstance(start_order, bool)
        or start_order < 1
    ):
        raise BlockContractError("block-translation-window-order")
    if max_windows is not None and (
        not isinstance(max_windows, int)
        or isinstance(max_windows, bool)
        or max_windows < 1
    ):
        raise BlockContractError("block-translation-max-windows")
    replanned = plan_ordered_window_materials(
        plan=plan,
        source_plan_id=source_plan_id,
        frozen_config=frozen_config,
        workflow_config=workflow_config,
        execution_profile=execution_profile,
        window_profile=window_profile,
        register=register,
        baseline_slots=baseline_slots,
        contextual_memory_for=contextual_memory_for,
        live_glossary_for=live_glossary_for,
        source_plan_record=source_plan_record,
        start_order=start_order,
        token_counter=token_counter,
        excluded_block_ids=tuple(dict.fromkeys(fixed_block_ids)),
        max_windows=max_windows,
    )
    return replanned.materials


def _attempt_matches(
    attempt: TranslationWindowAttempt,
    *,
    source_plan: SourcePlanRecord,
    window: TranslationWindow,
    material: WindowMaterial,
    expected_predecessor: Mapping[str, Any] | None,
    verified_evidence: VerifiedWindowEvidence | None,
) -> bool:
    try:
        rehydrate_window_attempt(
            attempt, source_plan, verified_evidence=verified_evidence
        )
    except BlockContractError:
        return False
    packet = material.packet.to_dict()
    manifest = list(context_dependency_manifest(material.packet.slots))
    return (
        attempt.window_order == window.order
        and tuple(attempt.block_ids_json) == window.block_ids
        and tuple(attempt.block_fingerprints_json) == window.block_fingerprints
        and attempt.packet_json == packet
        and attempt.settings_json == dict(material.settings)
        and attempt.model_profile_json == dict(material.model_profile)
        and attempt.admission_json == dict(material.admission)
        and attempt.context_manifest_json == manifest
        and (
            expected_predecessor is None
            or attempt.predecessor_json == dict(expected_predecessor)
        )
    )


def discover_exact_attempt(
    *,
    operation: Any,
    source_plan: SourcePlanRecord,
    material: WindowMaterial,
    expected_predecessor: Mapping[str, Any] | None = None,
    session: Any | None = None,
    verified_evidence: VerifiedWindowEvidence | None = None,
) -> AttemptDiscovery:
    """Find an exact durable attempt, never selecting by creation time/count.

    A prepared request fenced to an older operation generation is returned only
    as retry lineage.  It is explicitly not safe to execute.
    """

    active_session = session or db.session
    operation_id = getattr(operation, "id", None)
    operation_version = getattr(operation, "version", None)
    if (
        not isinstance(operation_id, str)
        or not operation_id
        or not isinstance(operation_version, int)
        or isinstance(operation_version, bool)
        or operation_version < 1
    ):
        raise BlockContractError("block-translation-operation")
    candidates = list(
        active_session.query(TranslationWindowAttempt)
        # Window order is part of exact identity. Unrelated windows cannot
        # match and must not be decrypted/rehydrated merely to reject them.
        .filter_by(
            operation_id=operation_id, source_plan_id=source_plan.id,
            window_order=material.window.order,
        )
        .all()
    )
    matching = [
        item
        for item in candidates
        if _attempt_matches(
            item,
            source_plan=source_plan,
            window=material.window,
            material=material,
            expected_predecessor=expected_predecessor,
            verified_evidence=verified_evidence,
        )
    ]
    if not matching:
        return AttemptDiscovery(None, None, False, False, False)
    ids = {attempt.id for attempt in matching}
    children = {
        attempt.retry_of_attempt_id or attempt.rebase_of_attempt_id
        for attempt in matching
        if (attempt.retry_of_attempt_id or attempt.rebase_of_attempt_id) in ids
    }
    tips = [attempt for attempt in matching if attempt.id not in children]
    if len(tips) != 1:
        raise BlockContractError("block-translation-attempt-ambiguous")
    selected = tips[0]
    # A remaining root must be the sole lineage origin.  Two roots without a
    # relationship are not made deterministic by their timestamps.
    roots = [
        attempt
        for attempt in matching
        if (attempt.retry_of_attempt_id or attempt.rebase_of_attempt_id) is None
    ]
    if len(roots) != 1:
        raise BlockContractError("block-translation-attempt-ambiguous")
    current = selected.operation_version == operation_version
    state = selected.dispatch_state
    retry_lineage = (
        selected.id
        if state in _RETRYABLE_TERMINAL_WINDOW_STATES
        or (state == "prepared" and not current)
        else None
    )
    return AttemptDiscovery(
        selected,
        retry_lineage,
        (current and state == "prepared")
        or (
            state == "external_pending"
            and operation_version >= selected.operation_version
        ),
        state == "staged"
        or (current and state in {"response_received", "staging_failed"}),
        state == "dispatched",
    )


__all__ = [
    "COMPACT_WRITABLE_SOURCE_CONTRACT_VERSION",
    "CONTINUITY_CONTRACT_VERSION",
    "EXACT_PRECEDENT_CONTRACT_VERSION",
    "LEGACY_CONTINUITY_CONTRACT_VERSION",
    "LOCATED_SOURCE_WRITABLE_CONTRACT_VERSION",
    "NOTES_WRITABLE_SOURCE_CONTRACT_VERSION",
    "SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION",
    "SETTINGS_EVIDENCE_CONTRACT_VERSION",
    "SINGLE_COPY_WRITABLE_SOURCE_CONTRACT_VERSION",
    "STRUCTURE_CONTEXT_CONTRACT_VERSION",
    "STUDY_NOTES_WRITABLE_SOURCE_CONTRACT_VERSION",
    "WINDOW_PROFILE_ID",
    "WINDOW_PROFILE_VERSION",
    "WRITABLE_SOURCE_CONTRACT_VERSION",
    "AttemptDiscovery",
    "PlannedWindows",
    "WindowMaterial",
    "WindowProfile",
    "baseline_slots_for_window_profile",
    "build_required_slots",
    "build_window_material",
    "continuity_slot",
    "derive_window_profile",
    "discover_exact_attempt",
    "estimate_window_output_tokens",
    "exact_precedent_slot",
    "plan_ordered_window_materials",
    "plan_unissued_tail_materials",
    "settings_evidence",
    "structural_context_slot",
    "window_fits_callback",
]
