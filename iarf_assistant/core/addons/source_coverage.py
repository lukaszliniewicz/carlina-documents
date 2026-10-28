"""Bounded, registry-backed source coverage projections."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from ..addon_contracts import ACTIVE, DEGRADED, AddonRegistries
from .capability_matching import (
    SOURCE_CLASS_ALIASES,
    SOURCE_CLASS_CANONICAL,
    SOURCE_CLASS_STRONG_ALIASES,
    bounded_text,
    canonical_source_class,
    contains_source_alias,
    contains_strong_source_alias,
    infer_source_classes,
    matches_source_alias,
    normalized_capability_text,
    source_alias_terms,
)
from .planning_inputs import normalize_planning_identifiers, planning_int

MAX_SOURCE_CLASSES = 20
MAX_SOURCE_TEXT_LENGTH = 128
MAX_TASK_TEXT_LENGTH = 4096
MAX_DETAILED_SOURCE_ROWS = 1
MAX_MATCHES_PER_SOURCE = 50
MAX_TOOL_CANDIDATES_PER_SOURCE = 100


@dataclass(frozen=True, slots=True)
class IndexedSourceRecord:
    record: Any
    match_text: str


@dataclass(frozen=True, slots=True)
class SourceCoverageIndex:
    providers: tuple[IndexedSourceRecord, ...]
    helpers: tuple[IndexedSourceRecord, ...]
    toolkits: tuple[IndexedSourceRecord, ...]
    include_inactive: bool


@dataclass(slots=True)
class ToolkitCoverageMatches:
    read_tools: list[dict[str, Any]] = field(default_factory=list)
    write_tools: list[dict[str, Any]] = field(default_factory=list)
    tool_views: list[dict[str, Any]] = field(default_factory=list)
    read_toolkit_ids: set[str] = field(default_factory=set)
    write_toolkit_ids: set[str] = field(default_factory=set)
    read_function_names: list[str] = field(default_factory=list)
    write_function_names: list[str] = field(default_factory=list)
    view_function_names: list[str] = field(default_factory=list)
    omitted_tool_candidates: int = 0


@dataclass(frozen=True, slots=True)
class SourceCoverageProjection:
    raw_source: str
    canonical_source: str
    aliases: tuple[str, ...]
    status: str
    provider_cards: list[dict[str, Any]]
    helper_cards: list[dict[str, Any]]
    tool_matches: ToolkitCoverageMatches
    read_toolkit_ids: list[str]
    write_toolkit_ids: list[str]
    recommended_ids: list[str]
    function_names: list[str]
    omitted_function_count: int
    provider_matches_omitted: int
    helper_matches_omitted: int
    next_actions: list[str]


class SourceCoverageHost(Protocol):
    registries: AddonRegistries

    def _runtime_policy_profile_cards(
        self,
        runtime_policy: dict[str, Any] | None = None,
        *,
        context: Any = None,
    ) -> list[dict[str, Any]]: ...

    def _runtime_mode_profile_cards(self) -> list[dict[str, Any]]: ...

    def _runtime_profile_cards(
        self,
        *,
        context: Any = None,
    ) -> list[dict[str, Any]]: ...

    def _runtime_context_snapshot(self) -> Any: ...

    def _tool_affordance_card(
        self,
        tool: Any,
        capability: Any,
    ) -> dict[str, Any]: ...


class AddonSourceCoverageMixin:
    """Build compact planning coverage without importing add-on code."""

    SOURCE_CLASS_ALIASES = SOURCE_CLASS_ALIASES
    SOURCE_CLASS_CANONICAL = SOURCE_CLASS_CANONICAL
    SOURCE_CLASS_STRONG_ALIASES = SOURCE_CLASS_STRONG_ALIASES

    def get_source_coverage(
        self,
        source_classes=None,
        task: str = "",
        max_tools_per_source: int = 6,
        include_tool_cards: bool = False,
        include_inactive: bool = False,
        runtime_policy: dict | None = None,
    ) -> dict:
        """Return compact per-source coverage rows for multi-repository planning."""
        task_text = bounded_text(task, MAX_TASK_TEXT_LENGTH)
        max_tools_per_source = planning_int(
            max_tools_per_source,
            default=6,
            maximum=20,
        )
        normalized = normalize_planning_identifiers(
            source_classes,
            max_items=MAX_SOURCE_CLASSES,
            max_length=MAX_SOURCE_TEXT_LENGTH,
            canonicalize=self._canonical_source_class,
        )
        normalized_requests = list(normalized.items)
        if not normalized_requests and task_text:
            inferred = self._infer_source_classes_from_text(task_text)[
                :MAX_SOURCE_CLASSES
            ]
            normalized_requests = [
                (source_class, self._canonical_source_class(source_class))
                for source_class in inferred
            ]

        detail_suppressed = bool(
            include_tool_cards is True
            and len(normalized_requests) > MAX_DETAILED_SOURCE_ROWS
        )
        include_details = bool(include_tool_cards is True and not detail_suppressed)
        index = self._source_coverage_index(include_inactive=include_inactive is True)
        rows = []
        for raw_source, canonical in normalized_requests:
            rows.append(
                self._source_coverage_row(
                    raw_source,
                    canonical,
                    max_tools_per_source=max_tools_per_source,
                    include_tool_cards=include_details,
                    index=index,
                )
            )

        host = cast(SourceCoverageHost, self)
        context = host._runtime_context_snapshot()
        return {
            "coverage_version": 1,
            "task": task_text,
            "source_classes": rows,
            "input_limits": {
                "max_source_classes": MAX_SOURCE_CLASSES,
                "max_source_text_length": MAX_SOURCE_TEXT_LENGTH,
                "max_task_text_length": MAX_TASK_TEXT_LENGTH,
                "max_detailed_source_rows": MAX_DETAILED_SOURCE_ROWS,
            },
            "source_classes_truncated": normalized.truncated,
            "source_classes_ignored_count": normalized.ignored_count,
            "task_truncated": isinstance(task, str)
            and len(task) > MAX_TASK_TEXT_LENGTH,
            "detail_suppressed": detail_suppressed,
            "policy_profiles": host._runtime_policy_profile_cards(
                runtime_policy=runtime_policy,
                context=context,
            ),
            "mode_profiles": host._runtime_mode_profile_cards(),
            "runtime_profiles": host._runtime_profile_cards(context=context),
            "planner_next_step": (
                "If coverage_sufficient_to_plan is true for the requested rows, stop broad discovery and "
                "synthesize the plan now. For durable, parallel, resumable, checkpointed, or reviewable work, "
                "call runs_compile next when the run shape is understood. Call capabilities_describe with the "
                "row's recommended_describe_capability_ids only when exact schemas, write details, or scriptable "
                "tool details are required before acting. Do not call capabilities_search to re-check covered sources."
            ),
            "rules": {
                "multi_source_first_step": (
                    "Use this coverage table before broad capability search for tasks that name multiple "
                    "repositories of data."
                ),
                "coverage_stop_rule": (
                    "If each requested source class has a coverage row and the available rows include "
                    "common_function_names plus recommended capability IDs, stop discovery and synthesize "
                    "the plan unless a specific full schema is required."
                ),
                "avoid_broad_search_after_coverage": (
                    "Do not call capabilities_search merely to confirm source classes already covered here. "
                    "Use recommended_describe_capability_ids for targeted detail, or mark explicit gaps."
                ),
                "gap_discipline": (
                    "Do not declare a requested source unavailable unless its coverage row has no provider, "
                    "UI picker, read toolkit, write toolkit, or tool view route."
                ),
                "tool_name_provenance": (
                    "Exact function names may be cited from source_coverage.common_task cards, "
                    "capabilities_describe, or loaded schemas. Treat other names as proposals/gaps."
                ),
                "code_runtime_discipline": (
                    "Code-in-the-loop can be a strategy, but execution is only available when a runtime "
                    "profile explicitly says it is available."
                ),
                "budget_discipline": (
                    "When tool-call budget is low, either answer from gathered evidence or use the policy-listed "
                    "budget extension tool once if more tool work is necessary."
                ),
            },
        }

    def _infer_source_classes_from_text(self, text: str) -> list[str]:
        return infer_source_classes(text)

    def _canonical_source_class(self, raw_source: str) -> str:
        return canonical_source_class(raw_source)

    def _source_alias_terms(
        self, source_class: str, aliases: list[str] | None = None
    ) -> list[str]:
        return source_alias_terms(source_class, aliases)

    def _source_match_text(self, *parts) -> str:
        return normalized_capability_text(*parts)

    def _contains_source_alias(self, text: str, alias: str) -> bool:
        return contains_source_alias(text, alias)

    def _contains_strong_source_alias(
        self, text: str, source_class: str, alias: str
    ) -> bool:
        return contains_strong_source_alias(text, source_class, alias)

    def _bounded_text(self, text: str | None, limit: int = 220) -> str:
        return bounded_text(text, limit)

    def _matches_source_alias(
        self, text: str, source_class: str, aliases: list[str]
    ) -> bool:
        return matches_source_alias(text, source_class, aliases)

    def _source_coverage_index(
        self,
        *,
        include_inactive: bool,
    ) -> SourceCoverageIndex:
        def usable(record: Any) -> bool:
            return bool(include_inactive or record.status.state in {ACTIVE, DEGRADED})

        host = cast(SourceCoverageHost, self)
        providers = tuple(
            IndexedSourceRecord(
                record=provider,
                match_text=self._source_match_text(
                    provider.id,
                    provider.addon_id,
                    provider.provider_id,
                    provider.domain,
                    provider.name,
                    " ".join(provider.operations or []),
                    " ".join(provider.capabilities or []),
                ),
            )
            for provider in host.registries.providers.list()
            if usable(provider)
        )
        helpers = tuple(
            IndexedSourceRecord(
                record=helper,
                match_text=self._source_match_text(
                    helper.id,
                    helper.addon_id,
                    helper.name,
                    " ".join(helper.resources or []),
                    " ".join(helper.aliases or []),
                ),
            )
            for helper in host.registries.helpers.list()
            if usable(helper)
        )
        toolkits = tuple(
            IndexedSourceRecord(
                record=capability,
                match_text=self._source_match_text(
                    capability.id,
                    capability.addon_id,
                    capability.name,
                    capability.source_addon_id,
                ),
            )
            for capability in host.registries.capabilities.list()
            if capability.capability_type == "toolkit" and usable(capability)
        )
        return SourceCoverageIndex(
            providers=providers,
            helpers=helpers,
            toolkits=toolkits,
            include_inactive=include_inactive,
        )

    def _matching_provider_cards(
        self,
        index: SourceCoverageIndex,
        source_class: str,
        aliases: list[str] | tuple[str, ...],
    ) -> tuple[list[dict[str, Any]], int]:
        cards: list[dict[str, Any]] = []
        omitted = 0
        for indexed in index.providers:
            if not self._matches_source_alias(
                indexed.match_text,
                source_class,
                list(aliases),
            ):
                continue
            if len(cards) >= MAX_MATCHES_PER_SOURCE:
                omitted += 1
                continue
            provider = indexed.record
            cards.append(
                {
                    "id": provider.id,
                    "addon_id": provider.addon_id,
                    "name": provider.name,
                    "domain": provider.domain,
                    "kind": provider.kind,
                    "runtime": provider.runtime,
                    "operations": list(provider.operations or [])[:10],
                    "capabilities": list(provider.capabilities or [])[:10],
                    "status": provider.status.state,
                }
            )
        return cards, omitted

    def _matching_helper_cards(
        self,
        index: SourceCoverageIndex,
        source_class: str,
        aliases: list[str] | tuple[str, ...],
    ) -> tuple[list[dict[str, Any]], int]:
        cards: list[dict[str, Any]] = []
        omitted = 0
        for indexed in index.helpers:
            if not self._matches_source_alias(
                indexed.match_text,
                source_class,
                list(aliases),
            ):
                continue
            if len(cards) >= MAX_MATCHES_PER_SOURCE:
                omitted += 1
                continue
            helper = indexed.record
            cards.append(
                {
                    "id": helper.id,
                    "addon_id": helper.addon_id,
                    "name": helper.name,
                    "description": self._bounded_text(helper.description, 180),
                    "resources": list(helper.resources or [])[:8],
                    "status": helper.status.state,
                }
            )
        return cards, omitted

    def _matching_toolkit_cards(
        self,
        index: SourceCoverageIndex,
        source_class: str,
        aliases: list[str] | tuple[str, ...],
        *,
        max_tools_per_source: int,
    ) -> ToolkitCoverageMatches:
        matches = ToolkitCoverageMatches()
        host = cast(SourceCoverageHost, self)
        candidate_count = 0
        budget_exhausted = False
        for indexed in index.toolkits:
            if not self._matches_source_alias(
                indexed.match_text,
                source_class,
                list(aliases),
            ):
                continue
            capability = indexed.record
            for element_id in capability.element_ids:
                candidate_count += 1
                if candidate_count > MAX_TOOL_CANDIDATES_PER_SOURCE:
                    matches.omitted_tool_candidates += 1
                    budget_exhausted = True
                    break
                tool = host.registries.tools.resolve(element_id)
                if not tool or (
                    not index.include_inactive
                    and tool.status.state not in {ACTIVE, DEGRADED}
                ):
                    continue
                card = self._source_coverage_tool_card(tool, capability)
                self._record_toolkit_match(
                    matches,
                    card,
                    tool_id=tool.id,
                    capability_id=capability.id,
                    max_tools_per_source=max_tools_per_source,
                )
            if budget_exhausted:
                break
        return matches

    @staticmethod
    def _record_toolkit_match(
        matches: ToolkitCoverageMatches,
        card: dict[str, Any],
        *,
        tool_id: str,
        capability_id: str,
        max_tools_per_source: int,
    ) -> None:
        is_read = card["risk_class"] == "read"
        function_name = str(card.get("function_name") or "")
        toolkit_ids = matches.read_toolkit_ids if is_read else matches.write_toolkit_ids
        function_names = (
            matches.read_function_names if is_read else matches.write_function_names
        )
        cards = matches.read_tools if is_read else matches.write_tools
        toolkit_ids.add(str(card["capability_id"]))
        if function_name:
            function_names.append(function_name)
        if len(cards) < max_tools_per_source:
            cards.append(card)

        for view_card in card.get("tool_views") or []:
            if len(matches.tool_views) >= max_tools_per_source:
                break
            if function_name:
                matches.view_function_names.append(function_name)
            matches.tool_views.append(
                {
                    "id": view_card["id"],
                    "tool_id": tool_id,
                    "capability_id": capability_id,
                    "function_name": function_name,
                    "name": view_card.get("name"),
                    "allowed_parameters": view_card.get("allowed_parameters") or [],
                    "code_callable": bool(view_card.get("code_callable")),
                    "code_callable_status": (
                        view_card.get("code_callable_status") or "model_tool_view_only"
                    ),
                    "batch_policy": view_card.get("batch_policy") or "serial",
                    "idempotency": view_card.get("idempotency") or "unknown",
                    "limits": view_card.get("limits") or {},
                }
            )

    @staticmethod
    def _coverage_status(
        provider_cards: list[dict[str, Any]],
        helper_cards: list[dict[str, Any]],
        tool_matches: ToolkitCoverageMatches,
    ) -> str:
        if tool_matches.read_tools or tool_matches.write_tools:
            return "available"
        if helper_cards:
            return "ui_only"
        if provider_cards:
            return "provider_only"
        return "unavailable"

    @staticmethod
    def _coverage_gap(status: str) -> str | None:
        gaps = {
            "unavailable": (
                "No provider, UI picker, read toolkit, write toolkit, or "
                "tool view matched this source class."
            ),
            "provider_only": (
                "A provider exists, but no model-callable toolkit matched "
                "this source class."
            ),
            "ui_only": (
                "A UI/resource picker exists, but no model-callable toolkit "
                "matched this source class."
            ),
        }
        return gaps.get(status)

    def _source_coverage_row(
        self,
        raw_source: str,
        canonical_source: str,
        *,
        max_tools_per_source: int,
        include_tool_cards: bool,
        index: SourceCoverageIndex,
    ) -> dict[str, Any]:
        aliases = self.SOURCE_CLASS_ALIASES.get(
            canonical_source,
            (canonical_source,),
        )
        match_aliases = self.SOURCE_CLASS_STRONG_ALIASES.get(
            canonical_source,
            aliases,
        )
        provider_cards, providers_omitted = self._matching_provider_cards(
            index,
            canonical_source,
            match_aliases,
        )
        helper_cards, helpers_omitted = self._matching_helper_cards(
            index,
            canonical_source,
            match_aliases,
        )
        tool_matches = self._matching_toolkit_cards(
            index,
            canonical_source,
            match_aliases,
            max_tools_per_source=max_tools_per_source,
        )
        read_toolkit_ids = sorted(tool_matches.read_toolkit_ids)
        write_toolkit_ids = sorted(tool_matches.write_toolkit_ids)
        function_names = list(
            dict.fromkeys(
                [
                    *tool_matches.read_function_names,
                    *tool_matches.view_function_names,
                    *tool_matches.write_function_names,
                ]
            )
        )
        omitted_function_count = 0
        if not include_tool_cards and len(function_names) > max_tools_per_source:
            omitted_function_count = len(function_names) - max_tools_per_source
            function_names = function_names[:max_tools_per_source]

        recommended_ids = list(dict.fromkeys([*read_toolkit_ids, *write_toolkit_ids]))[
            :MAX_MATCHES_PER_SOURCE
        ]
        status = self._coverage_status(
            provider_cards,
            helper_cards,
            tool_matches,
        )
        next_actions = self._source_coverage_next_actions(
            status=status,
            read_toolkit_ids=read_toolkit_ids,
            write_toolkit_ids=write_toolkit_ids,
            provider_cards=provider_cards,
            ui_picker_cards=helper_cards,
            tool_views=tool_matches.tool_views,
        )
        projection = SourceCoverageProjection(
            raw_source=raw_source,
            canonical_source=canonical_source,
            aliases=tuple(aliases),
            status=status,
            provider_cards=provider_cards,
            helper_cards=helper_cards,
            tool_matches=tool_matches,
            read_toolkit_ids=read_toolkit_ids,
            write_toolkit_ids=write_toolkit_ids,
            recommended_ids=recommended_ids,
            function_names=function_names,
            omitted_function_count=omitted_function_count,
            provider_matches_omitted=providers_omitted,
            helper_matches_omitted=helpers_omitted,
            next_actions=next_actions,
        )
        row = self._source_coverage_summary_row(
            projection,
            include_tool_cards=include_tool_cards,
            max_tools_per_source=max_tools_per_source,
        )
        alias_limit = 20 if include_tool_cards else 5
        if include_tool_cards or status != "available":
            row["aliases_considered"] = self._source_alias_terms(
                canonical_source,
                list(aliases),
            )[:alias_limit]
        if include_tool_cards:
            row.update(
                {
                    "providers": provider_cards[:8],
                    "ui_pickers": helper_cards[:8],
                    "matched_scriptable_tool_views": tool_matches.tool_views,
                    "read_tools": tool_matches.read_tools,
                    "write_tools": tool_matches.write_tools,
                }
            )
        gap = self._coverage_gap(status)
        if gap:
            row["gap"] = gap
        return row

    def _source_coverage_summary_row(
        self,
        projection: SourceCoverageProjection,
        *,
        include_tool_cards: bool,
        max_tools_per_source: int,
    ) -> dict[str, Any]:
        tools = projection.tool_matches
        has_tools = bool(tools.read_tools or tools.write_tools)
        return {
            "requested_source_class": projection.raw_source,
            "source_class": projection.canonical_source,
            "status": projection.status,
            "matched_provider_ids": [card["id"] for card in projection.provider_cards],
            "matched_ui_picker_ids": [card["id"] for card in projection.helper_cards],
            "matched_read_toolkit_ids": projection.read_toolkit_ids,
            "matched_write_toolkit_ids": projection.write_toolkit_ids,
            "recommended_describe_capability_ids": projection.recommended_ids,
            "matched_tool_view_ids": [card["id"] for card in tools.tool_views],
            "scriptable_tool_views_code_callable": (
                any(card.get("code_callable") for card in tools.tool_views)
                if tools.tool_views
                else None
            ),
            "common_function_names": projection.function_names,
            "common_function_names_omitted_count": (projection.omitted_function_count),
            "coverage_sufficient_to_plan": bool(
                projection.status == "available"
                and (projection.function_names or projection.recommended_ids)
            ),
            "credential_state": self._source_credential_state(
                projection.provider_cards
            ),
            "access_state": "unknown_user_scoped_not_evaluated",
            "permission_state": self._source_permission_state(
                projection.helper_cards,
                tools.read_tools,
                tools.write_tools,
            ),
            "describe_required_before_full_schema_or_script": bool(
                tools.read_tools or tools.write_tools or tools.tool_views
            ),
            "may_name_common_functions_from_coverage": has_tools,
            "function_name_provenance": (
                "source_coverage.common_tasks" if has_tools else None
            ),
            "next_actions": projection.next_actions,
            "detail_omitted": not include_tool_cards,
            "tool_card_sample_limit": max_tools_per_source,
            "omitted_match_counts": {
                "providers": projection.provider_matches_omitted,
                "ui_pickers": projection.helper_matches_omitted,
                "tool_candidates": tools.omitted_tool_candidates,
            },
        }

    def _source_coverage_tool_card(self, tool, capability) -> dict:
        host = cast(SourceCoverageHost, self)
        card = host._tool_affordance_card(tool, capability)
        tool_views = []
        for view_card in card["tool_views"]:
            tool_views.append(
                {
                    "id": view_card["id"],
                    "name": view_card.get("name"),
                    "allowed_parameters": list(
                        view_card.get("allowed_parameters") or []
                    )[:12],
                    "code_callable": bool(view_card.get("code_callable")),
                    "code_callable_status": view_card.get("code_callable_status")
                    or "model_tool_view_only",
                    "batch_policy": view_card.get("batch_policy") or "serial",
                    "idempotency": view_card.get("idempotency") or "unknown",
                }
            )
        return {
            "capability_id": card["capability_id"],
            "tool_id": card["tool_id"],
            "function_name": card["function_name"],
            "function_name_source": card["function_name_source"],
            "provenance": "source_coverage.common_tasks",
            "tool_name_provenance": "source_coverage.common_tasks",
            "may_name_function": True,
            "operation": card["operation"],
            "risk_class": card["risk_class"],
            "approval_required": card["approval_required"],
            "requires_approval_or_review": card["requires_approval_or_review"],
            "required_args": card["required_args"],
            "optional_args": card["optional_args"][:6],
            "argument_names": card["argument_names"][:12],
            "schema_state": "summary_only",
            "supports_batch_or_paging": card["supports_batch_or_paging"],
            "batch_policy": card["batch_policy"],
            "callable_from_code": card["callable_from_code"],
            "code_callable_status": card["code_callable_status"],
            "tool_views": tool_views,
        }

    def _source_credential_state(self, provider_cards: list[dict]) -> str:
        if not provider_cards:
            return "unknown_no_provider_match"
        if any(card.get("status") == DEGRADED for card in provider_cards):
            return "degraded_or_needs_configuration"
        return "provider_registered_credentials_not_verified"

    def _source_permission_state(
        self,
        ui_picker_cards: list[dict],
        read_tools: list[dict],
        write_tools: list[dict],
    ) -> str:
        if any(card.get("approval_required") for card in write_tools):
            return "write_or_side_effect_approval_required"
        if ui_picker_cards:
            return "resource_picker_available_if_user_grant_needed"
        if read_tools or write_tools:
            return "toolkit_available_subject_to_tool_policy"
        return "unknown"

    def _source_coverage_next_actions(
        self,
        *,
        status: str,
        read_toolkit_ids: list[str],
        write_toolkit_ids: list[str],
        provider_cards: list[dict],
        ui_picker_cards: list[dict],
        tool_views: list[dict],
    ) -> list[str]:
        actions = []
        toolkit_ids = list(dict.fromkeys([*read_toolkit_ids, *write_toolkit_ids]))
        if toolkit_ids:
            actions.append(
                "Coverage is sufficient for high-level planning; do not call capabilities_search for this source class."
            )
            actions.append(
                "Call capabilities_describe for "
                + ", ".join(toolkit_ids[:4])
                + " before loading full schemas, planning writes, or scripting exact tool calls."
            )
            actions.append(
                "Use capabilities_load_to_session for simple bounded inline work with these toolkit IDs."
            )
            actions.append(
                "Use runs_compile for broad, parallel, resumable, or checkpointed work across many items; "
                "use runs_create/runs_add_step for incremental construction or repair."
            )
        if tool_views:
            actions.append(
                "Tool views are available for narrowed model tool use; scratch Python can transform tool results, "
                "but scripts may call only views explicitly marked code-callable by a future connector SDK."
            )
        if provider_cards and not toolkit_ids:
            actions.append(
                "Provider capability exists; look for or add a model-callable toolkit/adapter before planning autonomous reads."
            )
        if ui_picker_cards and not toolkit_ids:
            actions.append(
                "Use the UI/resource picker to get a user grant, or ask the user to add a callable toolkit."
            )
        if status == "unavailable":
            actions.append(
                "Ask the user to install, enable, or grant a capability for this source class before claiming it is searchable."
            )
        return actions


__all__ = [
    "MAX_DETAILED_SOURCE_ROWS",
    "MAX_SOURCE_CLASSES",
    "MAX_SOURCE_TEXT_LENGTH",
    "MAX_TASK_TEXT_LENGTH",
    "AddonSourceCoverageMixin",
    "SourceCoverageHost",
]
