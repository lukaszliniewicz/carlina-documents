"""Versioned, source-safe inputs for preliminary Translator research."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .block_contract import (
    BlockContractError,
    TranslationBlock,
    TranslationSourcePlan,
    canonical_json,
    sha256_text,
)
from .compact_markup import compact_source_context_block

RESEARCH_INPUT_PLANNER_CONTRACT_VERSION = "document-block-research-input-planner-v3"
LEGACY_RESEARCH_INPUT_PLANNER_CONTRACT_VERSION = (
    "document-block-research-reduced-brief-v1"
)
SUPPORTED_RESEARCH_INPUT_PLANNER_CONTRACTS = frozenset(
    {RESEARCH_INPUT_PLANNER_CONTRACT_VERSION}
)
RESEARCH_SOURCE_SCOPE_VERSION = "document-research-source-scope-v1"
# The preliminary source may use a larger message than ordinary translation
# windows, but it remains bounded below the provider adapter's complete-packet
# ceiling so later witnessed tool turns retain some transport headroom.
MAX_RESEARCH_INPUT_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ResearchSourceScope:
    """Exact ordered complete blocks shown to one preliminary research run."""

    source_plan_hash: str
    mode: str
    block_ids: tuple[str, ...]
    total_block_count: int
    omitted_block_count: int
    source_projection_hash: str
    source_bytes: int
    contract_version: str = RESEARCH_SOURCE_SCOPE_VERSION

    def __post_init__(self) -> None:
        if (
            self.contract_version != RESEARCH_SOURCE_SCOPE_VERSION
            or self.mode not in {"full_document", "complete_block_prefix"}
            or len(self.source_plan_hash) != 64
            or len(self.source_projection_hash) != 64
            or not self.block_ids
            or self.total_block_count < len(self.block_ids)
            or self.omitted_block_count != self.total_block_count - len(self.block_ids)
            or self.source_bytes < 1
        ):
            raise BlockContractError("research-source-scope")
        if (self.mode == "full_document") != (self.omitted_block_count == 0):
            raise BlockContractError("research-source-scope")

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "source_plan_hash": self.source_plan_hash,
            "mode": self.mode,
            "block_ids": list(self.block_ids),
            "total_block_count": self.total_block_count,
            "omitted_block_count": self.omitted_block_count,
            "source_projection_hash": self.source_projection_hash,
            "source_bytes": self.source_bytes,
        }


def _source_block(block: TranslationBlock) -> dict[str, object]:
    compact = compact_source_context_block(block)
    return {
        "block_id": block.id,
        "role": compact["role"],
        "content": compact["content"],
        "fields": compact["fields"],
    }


def build_research_source_material(
    plan: TranslationSourcePlan,
    blocks: Sequence[TranslationBlock],
) -> dict[str, object]:
    """Render one exact complete-block prefix without slicing private text."""

    if not isinstance(plan, TranslationSourcePlan) or not plan.blocks:
        raise BlockContractError("research-source-plan")
    chosen = tuple(blocks)
    if not chosen or len(chosen) > len(plan.blocks):
        raise BlockContractError("research-source-blocks")
    expected = plan.blocks[: len(chosen)]
    if chosen != expected:
        raise BlockContractError("research-source-order")
    source_blocks = [_source_block(block) for block in chosen]
    source_json = canonical_json(source_blocks)
    source_bytes = len(source_json.encode("utf-8"))
    scope = ResearchSourceScope(
        source_plan_hash=plan.plan_hash,
        mode=(
            "full_document"
            if len(chosen) == len(plan.blocks)
            else "complete_block_prefix"
        ),
        block_ids=tuple(block.id for block in chosen),
        total_block_count=len(plan.blocks),
        omitted_block_count=len(plan.blocks) - len(chosen),
        source_projection_hash=sha256_text(source_json),
        source_bytes=source_bytes,
    )
    snapshot = plan.snapshot.to_dict()
    document: dict[str, Any] = {
        "source_plan_hash": plan.plan_hash,
        "source_content_hash": plan.snapshot.content_hash,
        "source_language": plan.source_language,
        "filename": plan.snapshot.filename,
        "metadata": snapshot["metadata"],
        "scope": scope.to_dict(),
        "source_blocks": source_blocks,
    }
    encoded = canonical_json(document).encode("utf-8")
    if len(encoded) > MAX_RESEARCH_INPUT_BYTES:
        raise BlockContractError("research-source-size")
    return document


def validate_research_packet_source(
    plan: TranslationSourcePlan,
    packet: Mapping[str, Any],
) -> dict[str, object]:
    """Verify the exact v3 source and admission evidence in an initial packet."""

    if not isinstance(plan, TranslationSourcePlan):
        raise BlockContractError("research-source-plan")
    messages = packet.get("messages")
    packet_input = packet.get("input")
    if (
        not isinstance(messages, list)
        or len(messages) != 2
        or not all(isinstance(item, dict) for item in messages)
        or not isinstance(packet_input, Mapping)
        or packet_input.get("phase") != "block-research-v2"
    ):
        raise BlockContractError("research-source-packet")
    scope_data = packet_input.get("source_scope")
    admission = packet_input.get("provider_admission")
    if not isinstance(scope_data, Mapping) or set(scope_data) != {
        "contract_version",
        "source_plan_hash",
        "mode",
        "block_ids",
        "total_block_count",
        "omitted_block_count",
        "source_projection_hash",
        "source_bytes",
    }:
        raise BlockContractError("research-source-scope")
    block_ids = scope_data.get("block_ids")
    if not isinstance(block_ids, list) or not all(
        isinstance(item, str) and item for item in block_ids
    ):
        raise BlockContractError("research-source-scope")
    try:
        scope = ResearchSourceScope(
            contract_version=scope_data["contract_version"],
            source_plan_hash=scope_data["source_plan_hash"],
            mode=scope_data["mode"],
            block_ids=tuple(block_ids),
            total_block_count=scope_data["total_block_count"],
            omitted_block_count=scope_data["omitted_block_count"],
            source_projection_hash=scope_data["source_projection_hash"],
            source_bytes=scope_data["source_bytes"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise BlockContractError("research-source-scope") from exc
    if scope.to_dict() != dict(scope_data):
        raise BlockContractError("research-source-scope")
    count = len(scope.block_ids)
    if tuple(block.id for block in plan.blocks[:count]) != scope.block_ids:
        raise BlockContractError("research-source-order")
    expected = build_research_source_material(plan, plan.blocks[:count])
    if expected["scope"] != scope.to_dict():
        raise BlockContractError("research-source-scope")
    user_message = messages[1]
    if user_message.get("role") != "user" or not isinstance(
        user_message.get("content"), str
    ):
        raise BlockContractError("research-source-packet")
    try:
        user_payload = json.loads(user_message["content"])
    except (TypeError, ValueError, RecursionError) as exc:
        raise BlockContractError("research-source-packet") from exc
    if (
        not isinstance(user_payload, dict)
        or user_payload.get("source_document") != expected
        or user_payload.get("semantic_brief") != packet_input.get("semantic_brief")
        or user_payload.get("research_question")
        != packet_input.get("research_question")
    ):
        raise BlockContractError("research-source-packet")
    if (
        not isinstance(admission, Mapping)
        or admission.get("admitted") is not True
        or admission.get("overflow_tokens") != 0
        or admission.get("reason") is not None
        or not isinstance(admission.get("requested_output_tokens"), int)
        or isinstance(admission.get("requested_output_tokens"), bool)
        or admission["requested_output_tokens"] < 1
    ):
        raise BlockContractError("research-source-admission")
    return {
        "source_scope": scope.to_dict(),
        "source_document_hash": sha256_text(canonical_json(expected)),
        "initial_packet_hash": sha256_text(canonical_json(dict(packet))),
        "effective_output_tokens": admission["requested_output_tokens"],
    }


__all__ = [
    "LEGACY_RESEARCH_INPUT_PLANNER_CONTRACT_VERSION",
    "MAX_RESEARCH_INPUT_BYTES",
    "RESEARCH_INPUT_PLANNER_CONTRACT_VERSION",
    "RESEARCH_SOURCE_SCOPE_VERSION",
    "SUPPORTED_RESEARCH_INPUT_PLANNER_CONTRACTS",
    "ResearchSourceScope",
    "build_research_source_material",
    "validate_research_packet_source",
]
