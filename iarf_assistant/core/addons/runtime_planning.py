"""Compact, bounded runtime-planning projections."""

from __future__ import annotations

import copy
from typing import Any, Protocol

from carlina.core.runtime_policy import resolve_runtime_policy

from ..addon_contracts import ACTIVE, DEGRADED, AddonRegistries
from ..project_workspace_roots import public_project_workspace_roots
from .capability_matching import bounded_text
from .planning_inputs import normalize_planning_identifiers, planning_int
from .runtime_model_roles import RuntimePlanningContext

MAX_RUNTIME_CAPABILITY_IDS = 20
MAX_RUNTIME_CAPABILITY_ID_LENGTH = 128
MAX_RUNTIME_TASK_LENGTH = 4096


def _clamped_int(value: Any, *, default: int, maximum: int) -> int:
    return planning_int(value, default=default, maximum=maximum)


def _normalized_capability_ids(value: Any) -> tuple[list[str], bool, int]:
    normalized = normalize_planning_identifiers(
        value,
        max_items=MAX_RUNTIME_CAPABILITY_IDS,
        max_length=MAX_RUNTIME_CAPABILITY_ID_LENGTH,
    )
    return (
        [raw for raw, _canonical in normalized.items],
        normalized.truncated,
        normalized.ignored_count,
    )


class RuntimePlanningHost(Protocol):
    app: Any
    registries: AddonRegistries

    def get_capability_record(self, capability_id: str) -> Any: ...

    def search_capabilities(self, **_kwargs: Any) -> list[dict[str, Any]]: ...

    def _capability_card(self, capability: Any) -> dict[str, Any]: ...

    def _common_task_cards_for_capabilities(
        self,
        capabilities: list[Any],
        max_common_tasks: int,
    ) -> list[dict[str, Any]]: ...

    def _runtime_context_snapshot(self) -> RuntimePlanningContext: ...

    def _runtime_model_role_cards(
        self,
        context: RuntimePlanningContext | None = None,
    ) -> list[dict[str, Any]]: ...

    def _runtime_inventory_capabilities(
        self,
        requested_capability_ids: list[str],
        *,
        task: str,
        max_capabilities: int,
        include_inactive: bool,
    ) -> list[Any]: ...

    def _runtime_policy_profile_cards(
        self,
        runtime_policy: dict[str, Any] | None = None,
        *,
        context: RuntimePlanningContext | None = None,
    ) -> list[dict[str, Any]]: ...

    def _runtime_mode_profile_cards(self) -> list[dict[str, Any]]: ...

    def _runtime_profile_cards(
        self,
        *,
        context: RuntimePlanningContext | None = None,
    ) -> list[dict[str, Any]]: ...


RUNTIME_PROFILE_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "id": "direct_tool_calls",
        "availability": "available",
        "description": "The orchestrator can call selected loaded tools directly in the current chat turn.",
        "best_for": "small read-only or bounded sequential tasks",
        "limits": ["model turn budget", "context budget", "loaded tool scope"],
    },
    {
        "id": "durable_runs",
        "availability": "available",
        "description": "Database-backed workflow runs with steps, workers, approvals, and checkpoints.",
        "best_for": "long, parallel, resumable, or review-gated work",
        "primary_tool_name": "runs_compile",
        "tool_names": [
            "runs_compile",
            "runs_create",
            "runs_update_understanding",
            "runs_update_plan",
            "runs_add_step",
            "runs_create_checkpoint",
            "runs_start",
            "runs_get",
            "runs_list_steps",
            "runs_read_shared_memory",
            "runs_read_artifact",
            "runs_execute_tool_batch",
            "runs_code_session_create",
            "runs_code_session_get",
            "runs_code_session_execute",
            "runs_code_session_install_dependencies",
            "documents_inspect",
            "documents_preview_pages",
            "documents_extract_pages",
            "documents_split_pages",
            "runs_create_collection",
            "runs_get_collection",
            "runs_upsert_collection_items",
            "runs_partition_collection_items",
            "runs_claim_collection_items",
            "runs_annotate_collection_items",
        ],
        "limits": [
            "worker semantics still differ from chat until the shared loop lands"
        ],
    },
    {
        "id": "runtime_collection_ledger",
        "availability": "available",
        "description": (
            "Row-backed run collection and item ledger for source enumeration, deterministic identity, "
            "atomic lease fencing, worker-sized partition planning, item outcomes, retries, provenance, "
            "and compact coverage summaries."
        ),
        "best_for": "every-item workflows across WordPress posts, email threads, Drive files, local files, document chunks, and research sources",
        "tool_names": [
            "runs_create_collection",
            "runs_get_collection",
            "runs_upsert_collection_items",
            "runs_partition_collection_items",
            "runs_claim_collection_items",
            "runs_annotate_collection_items",
        ],
        "code_callable_tools": True,
        "storage": "rows_v1",
        "statuses": [
            "pending",
            "leased",
            "completed",
            "failed",
            "skipped",
        ],
        "adapters": [
            "generic",
            "wordpress.posts",
            "gdrive.files",
        ],
        "limits": [
            "adapters normalize identity and provenance but do not call connectors themselves",
            "semantic dedupe is not automatic",
            "expired leases are recoverable; stale completion tokens are rejected",
        ],
    },
    {
        "id": "code_in_loop",
        "availability": "available_for_scratch_python_transforms",
        "description": (
            "Generated scripts can use either one-shot scratch execution or a governed resumable session. "
            "The session SDK mediates selected host tools, bounded agent mapping, progress, and artifacts "
            "without exposing connector credentials."
        ),
        "best_for": "dedupe, batching, validation, chunking, deterministic transforms, and worker orchestration",
        "code_callable_tools": True,
        "tool_name": "runs_execute_python",
        "tool_names": [
            "runs_execute_python",
            "runs_execute_tool_batch",
            "runs_code_session_create",
            "runs_code_session_get",
            "runs_code_session_execute",
            "runs_code_session_install_dependencies",
        ],
        "runtime_profile_id": "python.scratch",
        "next_action": (
            "Use runs_execute_python for one-shot standard-library transforms. Use python.session "
            "when work needs maintained data/document dependencies, resumable state, artifacts, "
            "selected host tools, or bounded agent mapping."
        ),
    },
    {
        "id": "python.session",
        "availability": "available",
        "description": (
            "Governed resumable Python session backed by the application's locked Pixi environment. "
            "Declared resources are copied into a session workspace; generated code cannot read connector "
            "credentials, open network connections, spawn subprocesses, or access undeclared host files."
        ),
        "best_for": (
            "tabular/document transforms, authenticated-source normalization through host tools, "
            "bounded map work, deterministic retries, progress, and artifact production"
        ),
        "tool_name": "runs_code_session_execute",
        "tool_names": [
            "runs_code_session_create",
            "runs_code_session_get",
            "runs_code_session_execute",
            "runs_code_session_install_dependencies",
        ],
        "code_callable_tools": True,
        "environment_mechanism": "pixi",
        "base_dependencies": [
            "jsonschema",
            "numpy",
            "openpyxl",
            "pandas",
            "Pillow",
            "pypdf",
            "python-docx",
        ],
        "limits": [
            "one Python language runtime",
            "declared resources and session outputs only",
            "host actions remain scope, approval, and event guarded",
            "extra dependencies require an explicit approval-gated Pixi install",
            "scoped mode requires runtime_profile_ids to include python.session",
        ],
    },
    {
        "id": "python.scratch",
        "availability": "available",
        "description": (
            "Bounded Python subprocess for run-scoped JSON/text processing. The script receives JSON on stdin, "
            "runs with a sanitized environment, and can store results in run memory or artifacts."
        ),
        "best_for": "normalization, dedupe, validation, batch construction, JSON reduction, and report assembly",
        "tool_name": "runs_execute_python",
        "tool_names": ["runs_execute_python"],
        "code_callable_tools": False,
        "limits": [
            "timeout/output bounded",
            "no connector credentials or toolkit SDK",
            "not an OS sandbox",
            "scoped mode requires runtime_profile_ids to include python.scratch",
        ],
    },
    {
        "id": "code_callable_tool_batch",
        "availability": "available_for_code_callable_views_or_full_access_registered_tools",
        "description": (
            "Run-scoped batch executor for normalized connector/toolkit calls that target tool views "
            "explicitly marked code-callable, or selected registered tools in full_access runs. Calls are "
            "executed by the orchestrator with normal schema, approval, and resource guards; scripts do "
            "not receive credentials."
        ),
        "best_for": "executing deduped/enumerated read batches or approved side-effect batches produced by code",
        "tool_name": "runs_execute_tool_batch",
        "tool_names": ["runs_execute_tool_batch"],
        "code_callable_tools": True,
        "limits": [
            "tool_view_id calls require view.code_callable=true",
            "registered tool calls without tool_view_id require full_access runtime policy",
            "normal approvals and resource gates apply",
            "scoped mode requires selected tool_view_ids or tool_ids",
        ],
    },
    {
        "id": "project.workspace",
        "availability": "available_permission_gated",
        "description": (
            "Observable project execution beneath an explicit operator-configured root. "
            "Files use bounded inspect and structured edit operations; commands use argv arrays "
            "with supervised timeouts, compact previews, full log artifacts, and cleanup fencing."
        ),
        "best_for": "repository inspection, attributable source edits, tests, builds, formatters, and bounded dev processes",
        "tool_name": "runs_project_workspace_create",
        "tool_names": [
            "runs_project_workspace_create",
            "runs_project_workspace_get",
            "runs_project_workspace_inspect",
            "runs_project_workspace_edit",
            "runs_project_workspace_execute",
            "runs_project_workspace_process",
            "runs_project_workspace_close",
        ],
        "code_callable_tools": False,
        "configured_workspace_root_count": 0,
        "workspace_roots": [],
        "workspace_root_selection": "server_side_authorized_reference",
        "limits": [
            "scoped mode requires runtime_profile_ids to include project.workspace",
            "workspace roots must be explicitly configured and selected",
            "shell strings, shell interpreters, inline interpreter code, and credential-like environment values are denied",
            "network, dependency-changing, and destructive intent must be explicitly declared and policy guarded",
            "full access suppresses configured prompts only; root, event, artifact, and process cleanup controls remain active",
        ],
    },
    {
        "id": "document_processor",
        "availability": "unavailable",
        "description": (
            "Shared standalone document tools provide metadata, selected-page preview, direct extraction, "
            "and splitting. Document Processor is the advanced OCR/layout/conversion provider."
        ),
        "best_for": "PDF, DOCX, MSG, EPUB, and document preview/extraction workflows",
        "code_callable_tools": False,
        "tool_names": [
            "documents_inspect",
            "documents_preview_pages",
            "documents_extract_pages",
            "documents_split_pages",
        ],
    },
)

__all__ = [
    "MAX_RUNTIME_CAPABILITY_IDS",
    "MAX_RUNTIME_CAPABILITY_ID_LENGTH",
    "MAX_RUNTIME_TASK_LENGTH",
    "AddonRuntimePlanningMixin",
    "RuntimePlanningHost",
]


class AddonRuntimePlanningMixin:
    """Expose planning affordances without loading full tool schemas."""

    def get_runtime_inventory(
        self: RuntimePlanningHost,
        task: str = "",
        capability_ids=None,
        max_capabilities: int = 8,
        max_common_tasks: int = 20,
        include_inactive: bool = False,
        runtime_policy: dict | None = None,
    ) -> dict:
        """Returns compact planning affordances without loading full tool schemas."""
        task_text = bounded_text(task, MAX_RUNTIME_TASK_LENGTH)
        (
            requested_capability_ids,
            capability_ids_truncated,
            capability_ids_ignored,
        ) = _normalized_capability_ids(capability_ids)
        max_capabilities = _clamped_int(
            max_capabilities,
            default=8,
            maximum=20,
        )
        max_common_tasks = _clamped_int(
            max_common_tasks,
            default=20,
            maximum=50,
        )
        capabilities = self._runtime_inventory_capabilities(
            requested_capability_ids,
            task=task_text,
            max_capabilities=max_capabilities,
            include_inactive=include_inactive is True,
        )
        common_tasks = self._common_task_cards_for_capabilities(
            capabilities, max_common_tasks
        )
        context = self._runtime_context_snapshot()
        return {
            "inventory_version": 1,
            "task": task_text,
            "input_limits": {
                "max_capability_ids": MAX_RUNTIME_CAPABILITY_IDS,
                "max_capability_id_length": MAX_RUNTIME_CAPABILITY_ID_LENGTH,
                "max_task_text_length": MAX_RUNTIME_TASK_LENGTH,
            },
            "capability_ids_truncated": capability_ids_truncated,
            "capability_ids_ignored_count": capability_ids_ignored,
            "task_truncated": isinstance(task, str)
            and len(task) > MAX_RUNTIME_TASK_LENGTH,
            "planning_defaults": {
                "first_step": "Use common_tasks for the cheapest plausible path; call capabilities_describe only for shortlisted capabilities.",
                "inline_path": "Use capabilities_load_to_session for simple bounded work with one or a few tools.",
                "delegate_path": "Use delegate_task for cheap read-only subtasks when review can happen in the chief turn.",
                "run_path": (
                    "Use runs_compile when you already understand the durable run shape; use lower-level "
                    "runs_create/runs_add_step only for incremental construction or repair."
                ),
                "code_in_loop_path": "Use a run with code in the loop when results need dedupe, batching, validation, chunking, or deterministic transformation.",
            },
            "policy_profiles": [
                *self._runtime_policy_profile_cards(
                    runtime_policy=runtime_policy,
                    context=context,
                ),
            ],
            "model_roles": self._runtime_model_role_cards(context=context),
            "mode_profiles": self._runtime_mode_profile_cards(),
            "runtime_profiles": self._runtime_profile_cards(context=context),
            "capabilities": [
                self._capability_card(capability) for capability in capabilities
            ],
            "common_tasks": common_tasks,
        }

    def _runtime_inventory_capabilities(
        self: RuntimePlanningHost,
        requested_capability_ids: list[str],
        *,
        task: str,
        max_capabilities: int,
        include_inactive: bool,
    ) -> list[Any]:
        capabilities: list[Any] = []
        if requested_capability_ids:
            for capability_id in requested_capability_ids:
                capability = self.get_capability_record(capability_id)
                if not capability:
                    continue
                if not include_inactive and capability.status.state not in {
                    ACTIVE,
                    DEGRADED,
                }:
                    continue
                capabilities.append(capability)
        else:
            cards = self.search_capabilities(
                query=task,
                limit=max_capabilities * 3,
                include_inactive=include_inactive,
            )
            non_core_cards = [
                card for card in cards if card.get("capability_type") != "core"
            ]
            if non_core_cards:
                cards = non_core_cards
            for card in cards:
                capability = self.get_capability_record(card["id"])
                if capability:
                    capabilities.append(capability)

        return capabilities[:max_capabilities]

    def _runtime_policy_profile_cards(
        self: RuntimePlanningHost,
        runtime_policy: dict | None = None,
        *,
        context: RuntimePlanningContext | None = None,
    ) -> list[dict]:
        context = context or self._runtime_context_snapshot()
        settings = context.settings
        source = context.settings_source
        policy = resolve_runtime_policy(
            settings, runtime_policy if isinstance(runtime_policy, dict) else None
        )
        if isinstance(runtime_policy, dict):
            source = "active_turn_policy"
        disable_approvals = bool(policy["disable_tool_approvals"])
        max_tool_calls = int(policy["max_tool_calls"])
        extension_max = int(policy["chat_tool_budget_extension_max"])
        active_approval_profile = policy["approval_profile"]
        return [
            {
                "id": "review",
                "kind": "approval_profile",
                "active": active_approval_profile == "review",
                "description": "Ask before side effects, publishing, deletion, or high-cost expansion.",
                "side_effect_approval_required": not disable_approvals,
                "setting_key": "disable_tool_approvals",
                "setting_value": "false" if not disable_approvals else "true",
                "settings_source": source,
            },
            {
                "id": "full_access",
                "kind": "approval_profile",
                "active": active_approval_profile == "full_access",
                "description": (
                    "Proceed without routine approval prompts when approvals are disabled; still log actions, "
                    "validate schemas, and avoid irreversible operations unless the user requested them."
                ),
                "side_effect_approval_required": not disable_approvals,
                "setting_key": "disable_tool_approvals",
                "setting_value": "true" if disable_approvals else "false",
                "settings_source": source,
            },
            {
                "id": "chat_tool_budget",
                "kind": "turn_budget",
                "active": True,
                "policy_profile": policy["profile"],
                "max_tool_call_iterations": max_tool_calls,
                "warning_remaining_iterations": 2 if max_tool_calls >= 3 else None,
                "extension_tool_name": "request_tool_budget_extension",
                "extension_available": extension_max > 0,
                "extension_request_limit_per_turn": 1 if extension_max > 0 else 0,
                "added_tool_call_iterations_per_extension": extension_max,
                "extension_policy": (
                    f"At most one request_tool_budget_extension call per turn; one granted request adds "
                    f"up to {extension_max} tool-call iterations."
                    if extension_max > 0
                    else "Budget extensions are disabled for this turn."
                ),
                "settings_source": source,
                "guidance": (
                    "Use request_tool_budget_extension only after deciding more tool work is necessary; "
                    "otherwise provide the best answer from gathered evidence before the limit."
                ),
            },
        ]

    def _runtime_mode_profile_cards(self) -> list[dict]:
        return [
            {
                "id": "autonomous_discovery",
                "label": "Default autonomous mode",
                "discovery_allowed": True,
                "initial_tooling": "built-ins plus meta-discovery tools and durable-run tools",
                "model_decision_surface": [
                    "answer directly",
                    "load a small toolkit inline",
                    "delegate bounded subtasks",
                    "create a durable run",
                    "plan code-in-the-loop when a runtime profile supports it",
                ],
            },
            {
                "id": "scoped",
                "label": "Direct/scoped mode",
                "discovery_allowed": False,
                "initial_tooling": "only selected toolkits, skills, tool views, resources, built-ins, and durable-run tools",
                "scope_expansion_tool": "request_scope_expansion",
                "model_decision_surface": [
                    "use provided tools directly",
                    "delegate within the selected scope",
                    "create a durable run constrained to the selected scope",
                    "ask for scope expansion if the selected scope is insufficient",
                ],
            },
        ]

    def _runtime_profile_cards(
        self: RuntimePlanningHost,
        *,
        context: RuntimePlanningContext | None = None,
    ) -> list[dict[str, Any]]:
        doc_processor_available = False
        try:
            capability = self.get_capability_record("doc_processor.toolkit")
            doc_processor_available = bool(
                capability and capability.status.state in {ACTIVE, DEGRADED}
            )
        except AttributeError, KeyError:
            doc_processor_available = False

        context = context or self._runtime_context_snapshot()
        app = getattr(self, "app", None)
        configured_roots = (
            app.config.get("PROJECT_WORKSPACE_ROOTS")
            if app is not None and hasattr(app, "config")
            else []
        )
        root_cards = public_project_workspace_roots(
            configured_roots,
            user_id=context.user_id,
            is_admin=context.user_is_admin,
        )

        profiles = [copy.deepcopy(profile) for profile in RUNTIME_PROFILE_TEMPLATES]
        by_id = {str(profile["id"]): profile for profile in profiles}
        by_id["document_processor"]["availability"] = (
            "available" if doc_processor_available else "unavailable"
        )
        project_profile = by_id["project.workspace"]
        project_profile["configured_workspace_root_count"] = len(root_cards)
        project_profile["workspace_roots"] = root_cards
        if not root_cards:
            project_profile["availability"] = "unavailable_no_authorized_roots"
        return profiles
