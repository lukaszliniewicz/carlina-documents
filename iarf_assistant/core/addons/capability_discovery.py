"""Capability registry discovery, ranking, and guidance retrieval."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol, cast

from ..addon_contracts import ACTIVE, DEGRADED, AddonRegistries, CapabilityRecord
from .capability_matching import (
    SOURCE_CLASS_ALIASES,
    SOURCE_CLASS_STRONG_ALIASES,
    capability_search_tokens,
    contains_strong_source_alias,
    infer_source_classes,
    matches_source_alias,
    normalized_capability_text,
)

CAPABILITY_SEARCH_DEFAULT_LIMIT = 5
CAPABILITY_SEARCH_MAX_LIMIT = 5


class CapabilityDiscoveryHost(Protocol):
    """Manager surface required by capability discovery."""

    registries: AddonRegistries

    def _capability_card(self, capability: CapabilityRecord) -> dict[str, Any]: ...

    def _slim_capability_description(
        self,
        capability: CapabilityRecord,
    ) -> dict[str, Any]: ...


class CapabilityDiscoveryMixin:
    """Expose compact capability discovery without importing add-on code."""

    def get_capability_manifest(
        self,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        """Returns derived user-facing capabilities from addon registries."""
        host = cast(CapabilityDiscoveryHost, self)
        capabilities = host.registries.capabilities.as_dicts()
        if include_inactive:
            return capabilities
        return [
            capability
            for capability in capabilities
            if capability.get("status", {}).get("state") in {"active", "degraded"}
        ]

    def get_capability_record(self, capability_id: str) -> CapabilityRecord | None:
        host = cast(CapabilityDiscoveryHost, self)
        return host.registries.capabilities.get(capability_id)

    def search_capabilities(
        self,
        query: str = "",
        groups: list[str] | None = None,
        capability_types: list[str] | None = None,
        limit: int = 20,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        """Returns ranked compact capability cards for chat/UI discovery.

        Matching is token based. This deliberately avoids arbitrary substring
        matches (for example, ``art`` matching ``artifact``) while still
        expanding explicit source aliases such as ``gmail`` -> ``email``.
        """
        query_text = self._normalized_capability_search_text(query)
        query_tokens = self._capability_search_tokens(query_text)
        if "drive" in query_tokens and not contains_strong_source_alias(
            query_text, "gdrive", "drive"
        ):
            query_tokens = [token for token in query_tokens if token != "drive"]
        expanded_query_tokens = self._expanded_capability_query_tokens(
            query_text, query_tokens
        )
        requested_source_classes = infer_source_classes(query_text)
        group_filter = {str(item).lower() for item in groups or [] if item}
        type_filter = {str(item).lower() for item in capability_types or [] if item}
        scored_cards = []
        try:
            max_results = max(1, min(int(limit or 20), 100))
        except TypeError, ValueError:
            max_results = 20

        # Name and id are most diagnostic; description is useful but cannot
        # outweigh a direct source/toolkit match on its own.
        field_weights = [
            ("name", 10),
            ("id", 8),
            ("description", 4),
            ("group", 2),
            ("capability_type", 2),
            ("addon_id", 1),
        ]

        host = cast(CapabilityDiscoveryHost, self)
        for capability in host.registries.capabilities.list():
            if not include_inactive and capability.status.state not in {
                ACTIVE,
                DEGRADED,
            }:
                continue
            if group_filter and capability.group.lower() not in group_filter:
                continue
            if type_filter and capability.capability_type.lower() not in type_filter:
                continue

            score = 0
            match_reasons = []
            if requested_source_classes:
                capability_source_text = normalized_capability_text(
                    getattr(capability, "id", ""),
                    getattr(capability, "name", ""),
                    getattr(capability, "description", ""),
                    getattr(capability, "source_addon_id", ""),
                )
                matched_sources = [
                    source_class
                    for source_class in requested_source_classes
                    if matches_source_alias(
                        capability_source_text,
                        source_class,
                        SOURCE_CLASS_ALIASES.get(source_class, ()),
                    )
                ]
                if not matched_sources:
                    continue
                score += 60 * len(matched_sources)
                match_reasons.append(
                    "explicit source matched " + ", ".join(matched_sources)
                )
            if query_text:
                for field_name, weight in field_weights:
                    field_val = self._normalized_capability_search_text(
                        getattr(capability, field_name, "") or ""
                    )
                    if not field_val:
                        continue
                    field_tokens = set(
                        self._capability_search_tokens(
                            field_val, remove_stopwords=False
                        )
                    )
                    direct_overlap = sorted(set(query_tokens) & field_tokens)
                    alias_overlap = sorted(
                        (set(expanded_query_tokens) - set(query_tokens)) & field_tokens
                    )
                    if field_val == query_text:
                        score += weight * 12
                        match_reasons.append(f"exact {field_name} match")
                    if direct_overlap:
                        score += weight * 2 * len(direct_overlap)
                        match_reasons.append(
                            f"{field_name} matched " + ", ".join(direct_overlap[:3])
                        )
                    if alias_overlap:
                        score += weight * len(alias_overlap)
                        match_reasons.append(
                            f"{field_name} alias matched "
                            + ", ".join(alias_overlap[:3])
                        )
                if score == 0:
                    continue

            card = host._capability_card(capability)
            confidence = self._capability_match_confidence(
                score, has_query=bool(query_text)
            )
            card.update(
                {
                    "match_score": score,
                    "confidence": confidence,
                    "match_reasons": list(dict.fromkeys(match_reasons))[:3],
                    "why": (
                        "; ".join(list(dict.fromkeys(match_reasons))[:2])
                        if match_reasons
                        else "Available capability; no query ranking was requested."
                    ),
                }
            )
            scored_cards.append(
                (score, str(card.get("name") or card.get("id") or ""), card)
            )

        scored_cards.sort(key=lambda item: (-item[0], item[1].lower()))
        return [card for _score, _name, card in scored_cards[:max_results]]

    @staticmethod
    def _normalized_capability_search_text(value: Any) -> str:
        return normalized_capability_text(value)

    def _capability_search_tokens(
        self, value: Any, *, remove_stopwords: bool = True
    ) -> list[str]:
        return capability_search_tokens(value, remove_stopwords=remove_stopwords)

    def _expanded_capability_query_tokens(
        self, query_text: str, query_tokens: list[str]
    ) -> list[str]:
        expanded = list(query_tokens)
        for source_class, aliases in SOURCE_CLASS_STRONG_ALIASES.items():
            if not any(
                contains_strong_source_alias(query_text, source_class, alias)
                for alias in aliases
            ):
                continue
            expanded.extend(
                self._capability_search_tokens(source_class.replace("_", " "))
            )
            for alias in aliases:
                expanded.extend(self._capability_search_tokens(alias))
        return list(dict.fromkeys(expanded))

    @staticmethod
    def _capability_match_confidence(score: int, *, has_query: bool) -> str:
        if not has_query:
            return "low"
        if score >= 24:
            return "high"
        if score >= 10:
            return "medium"
        return "low"

    @staticmethod
    def _capability_search_limit(value: Any) -> tuple[int, bool]:
        try:
            requested = (
                int(value) if value is not None else CAPABILITY_SEARCH_DEFAULT_LIMIT
            )
        except TypeError, ValueError:
            requested = CAPABILITY_SEARCH_DEFAULT_LIMIT
        clamped = max(1, min(requested, CAPABILITY_SEARCH_MAX_LIMIT))
        return clamped, requested != clamped

    def search_capabilities_for_model(
        self,
        query: str = "",
        groups: list[str] | None = None,
        capability_types: list[str] | None = None,
        limit: int | None = None,
        include_inactive: bool = False,
    ) -> dict[str, Any]:
        """Returns compact capability cards with a chat-safe payload budget."""
        max_results, limit_was_clamped = self._capability_search_limit(limit)
        cards = self.search_capabilities(
            query=query,
            groups=groups,
            capability_types=capability_types,
            limit=max_results + 1,
            include_inactive=include_inactive,
        )
        truncated = len(cards) > max_results
        return {
            "query": query or "",
            "capabilities": cards[:max_results],
            "returned_count": min(len(cards), max_results),
            "has_more": truncated,
            "payload_budget": {
                "default_results": CAPABILITY_SEARCH_DEFAULT_LIMIT,
                "max_results": CAPABILITY_SEARCH_MAX_LIMIT,
                "returned_results": min(len(cards), max_results),
                "card_shape": "compact_capability_card",
                "full_schemas_included": False,
                "full_schemas_available_via": [
                    "capabilities_describe(prepare_tools=true)"
                ],
            },
            "truncated": truncated,
            "limit_was_clamped": limit_was_clamped,
            "recommended_next_actions": [
                "Use declared tools directly when already available.",
                "Call capabilities_describe only for shortlisted capability IDs and the narrowest operation group.",
            ],
            "next_step": (
                "Use a declared tool directly if available; otherwise call capabilities_describe for one or "
                "two shortlisted capabilities and set prepare_tools=true with the narrowest operation_groups."
            ),
        }

    def describe_capabilities(
        self,
        capability_ids: str | Iterable[str] | None,
        include_inactive: bool = False,
        *,
        operation_groups: Iterable[str] | None = None,
        max_tools: int | None = None,
    ) -> list[dict[str, Any]]:
        """Returns model-optimized operational descriptions for selected capabilities."""
        if isinstance(capability_ids, str):
            capability_ids = [capability_ids]
        requested_groups = {
            str(group or "").strip().lower()
            for group in (operation_groups or [])
            if str(group or "").strip()
        }
        valid_groups = {
            "read",
            "search",
            "list",
            "preview",
            "write",
            "create",
            "update",
            "publish",
            "delete",
            "all",
        }
        requested_groups &= valid_groups
        tool_limit = None
        if max_tools is not None:
            try:
                tool_limit = max(1, min(int(max_tools), 12))
            except TypeError, ValueError:
                tool_limit = 6

        def operation_matches(tool_card: dict[str, Any]) -> bool:
            if not requested_groups or "all" in requested_groups:
                return True
            operation = str(tool_card.get("operation") or "call").lower()
            risk = str(tool_card.get("risk_class") or "unknown").lower()
            read_operations = {"read", "search", "list", "preview", "export", "call"}
            write_operations = {"create", "update", "publish", "delete"}
            if (
                "read" in requested_groups
                and risk == "read"
                and operation in read_operations
            ):
                return True
            if "write" in requested_groups and (
                operation in write_operations or risk != "read"
            ):
                return True
            if "preview" in requested_groups and operation == "read":
                return True
            return operation in requested_groups

        descriptions = []
        for capability_id in capability_ids or []:
            capability = self.get_capability_record(str(capability_id))
            if not capability:
                descriptions.append(
                    {
                        "id": capability_id,
                        "registered": False,
                        "error": "Capability is not registered.",
                    }
                )
                continue
            if not include_inactive and capability.status.state not in {
                ACTIVE,
                DEGRADED,
            }:
                descriptions.append(
                    {
                        "id": capability.id,
                        "registered": True,
                        "status": capability.status.state,
                        "error": f"Capability is {capability.status.state}.",
                    }
                )
                continue
            host = cast(CapabilityDiscoveryHost, self)
            description = {
                "registered": True,
                **host._slim_capability_description(capability),
            }
            tools = description.get("tools")
            if isinstance(tools, list):
                filtered_tools = [tool for tool in tools if operation_matches(tool)]
                omitted_count = max(0, len(tools) - len(filtered_tools))
                if tool_limit is not None and len(filtered_tools) > tool_limit:
                    omitted_count += len(filtered_tools) - tool_limit
                    filtered_tools = filtered_tools[:tool_limit]
                description["tools"] = filtered_tools
                description["tool_selection"] = {
                    "operation_groups": sorted(requested_groups),
                    "returned_count": len(filtered_tools),
                    "omitted_count": omitted_count,
                    "max_tools": tool_limit,
                    "schema_state": "summary_only",
                    "prepare_tools_for_full_schemas": True,
                }
            descriptions.append(description)
        return descriptions

    def get_guidance(
        self,
        capability_ids: str | Iterable[str] | None = None,
        guidance_ids: str | Iterable[str] | None = None,
        task: str = "",
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        """Returns full guidance content after a capability has been selected."""
        if isinstance(capability_ids, str):
            capability_ids = [capability_ids]
        if isinstance(guidance_ids, str):
            guidance_ids = [guidance_ids]

        selected_ids = []
        for capability_id in capability_ids or []:
            capability = self.get_capability_record(str(capability_id))
            if not capability:
                continue
            selected_ids.extend(capability.guidance_ids)
        selected_ids.extend(guidance_ids or [])
        selected_ids = list(dict.fromkeys(str(item) for item in selected_ids if item))

        if not selected_ids:
            return []

        task_text = str(task or "").strip().lower()
        host = cast(CapabilityDiscoveryHost, self)
        guidance_by_id = {
            record.id: record
            for record in getattr(host.registries, "guidance", [])
        }
        results = []
        for guidance_id in selected_ids:
            record = guidance_by_id.get(guidance_id)
            if not record:
                continue
            if not include_inactive and record.status.state not in {ACTIVE, DEGRADED}:
                continue
            if task_text:
                haystack = (
                    f"{record.title} {record.guidance_type} "
                    f"{record.summary} {record.content}"
                ).lower()
                task_tokens = [
                    token
                    for token in task_text.replace("_", " ").replace(".", " ").split()
                    if len(token) >= 3
                ]
                if task_tokens and not any(token in haystack for token in task_tokens):
                    continue
            results.append(
                {
                    "id": record.id,
                    "title": record.title,
                    "type": record.guidance_type,
                    "applies_to": list(record.applies_to),
                    "summary": record.summary,
                    "content": record.content,
                    "status": record.status.state,
                }
            )
        return results


__all__ = [
    "CAPABILITY_SEARCH_DEFAULT_LIMIT",
    "CAPABILITY_SEARCH_MAX_LIMIT",
    "CapabilityDiscoveryHost",
    "CapabilityDiscoveryMixin",
]
