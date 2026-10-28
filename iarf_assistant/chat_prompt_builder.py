import json
import re
from dataclasses import dataclass
from iarf_assistant.core.execution_scope import ExecutionScope
from carlina.core.utc import utc_now
from iarf_assistant.chat_builtin_tools import (
    DELEGATE_TASK_TOOL_DEF,
    LIST_KNOWLEDGE_COLLECTIONS_TOOL_DEF,
    MANAGE_LONG_TERM_MEMORY_TOOL_DEF,
    CONTEXT_MANAGE_RESULTS_TOOL_DEF,
    CONTEXT_READ_RESULT_TOOL_DEF,
    CONTEXT_SEARCH_RESULT_TOOL_DEF,
    KNOWLEDGE_QUERY_TOOL_DEF,
    RAG_SEARCH_TOOL_DEF,
    REQUEST_SELF_NUDGE_TOOL_DEF,
    REQUEST_USER_CHOICE_TOOL_DEF,
    REQUEST_SCOPE_EXPANSION_TOOL_DEF,
    SOURCE_COVERAGE_GET_TOOL_DEF,
    SOURCE_COVERAGE_UPSERT_TOOL_DEF,
    VIEW_ATTACHMENT_TOOL_DEF,
    WORKING_MEMORY_TOOL_DEF,
    MEMORIZE_FACT_TOOL_DEF,
    RECALL_FACT_TOOL_DEF,
    sanitize_memory_category_definitions,
)
from iarf_assistant.chat_execution_helpers import (
    DEFAULT_CONTEXT_PRESSURE_INCLUDED_TOKENS,
    DEFAULT_CONTEXT_PRESSURE_RAW_TOKENS,
    estimate_text_tokens,
)
from iarf_assistant.core.run_orchestrator import format_run_protocol_for_prompt
from carlina.core.runtime_policy import resolve_runtime_policy
from iarf_assistant.prompts import CHIEF_ASSISTANT_DEFAULT_PROMPT, DIRECT_ASSISTANT_DEFAULT_PROMPT


@dataclass
class ChatInvocation:
    user_content: str
    skill_id: str = None
    skill_instructions: str = ""
    workflow_context: str = ""
    run_context: str = ""
    error: str = None


@dataclass
class ChatPromptBuildResult:
    messages: list
    tools: list
    routing_metadata: dict | None = None
    error: str | None = None


@dataclass
class PromptTimeCapabilitySuggestions:
    prompt_text: str
    tool_declarations: list
    preloaded_capability_ids: list
    preloaded_tool_names: list
    source_classes: list
    suggested_capability_ids: list


INITIAL_SUGGESTION_LIMIT = 3
INITIAL_PRELOADED_READ_TOOL_LIMIT = 6
INITIAL_PRELOADED_SOURCE_LIMIT = 2
DEFAULT_AUTONOMOUS_SCHEMA_BUDGET_TOKENS = 4800
DEFAULT_AUTONOMOUS_RUN_SCHEMA_BUDGET_TOKENS = 12000
DEFAULT_HISTORY_BUDGET_TOKENS = 24000

_PROMPT_TOKEN_RE = re.compile(r"[a-z0-9]+")
_READ_OPERATION_TOKENS = {
    "browse",
    "fetch",
    "find",
    "get",
    "inspect",
    "list",
    "open",
    "preview",
    "query",
    "read",
    "retrieve",
    "search",
    "show",
    "summarize",
}
_WRITE_OPERATION_TOKENS = {
    "add",
    "archive",
    "create",
    "delete",
    "draft",
    "edit",
    "move",
    "publish",
    "remove",
    "reply",
    "send",
    "trash",
    "update",
    "upload",
    "write",
}
_BROAD_TASK_TOKENS = {
    "all",
    "batch",
    "broad",
    "classify",
    "corpus",
    "dedupe",
    "delegate",
    "delegated",
    "delegation",
    "every",
    "many",
    "research",
    "triage",
    "worker",
    "workers",
}
_MEMORY_INTENT_TOKENS = {"forget", "memorize", "memory", "recall", "remember"}
_CONTEXT_INTENT_TOKENS = {"compact", "context", "retained", "retention", "tokens"}
_RUN_INTENT_TOKENS = {"background", "durable", "run", "workflow"}
_DOCUMENT_INTENT_TOKENS = {
    "doc",
    "document",
    "documents",
    "extract",
    "ocr",
    "page",
    "pages",
    "pdf",
    "preview",
    "split",
}
_CODE_INTENT_TOKENS = {
    "code",
    "csv",
    "dataframe",
    "dedupe",
    "jsonl",
    "normalize",
    "python",
    "script",
    "spreadsheet",
    "tabular",
    "transform",
}
_PROJECT_STRONG_INTENT_TOKENS = {
    "codebase",
    "command",
    "commands",
    "git",
    "pytest",
    "repo",
    "repository",
    "shell",
    "terminal",
}
_PROJECT_ACTION_TOKENS = {
    "build",
    "compile",
    "edit",
    "fix",
    "format",
    "implement",
    "implementation",
    "lint",
    "refactor",
    "test",
    "tests",
}
_PROJECT_CONTEXT_TOKENS = {
    "app",
    "application",
    "codebase",
    "project",
    "repo",
    "repository",
    "source",
}
_CODE_SESSION_INTENT_TOKENS = {
    "artifact",
    "artifacts",
    "dataframe",
    "dependencies",
    "dependency",
    "install",
    "package",
    "packages",
    "resume",
    "resumable",
    "session",
    "spreadsheet",
    "state",
}
_DOCUMENT_TOOL_NAMES = {
    "documents_inspect",
    "documents_preview_pages",
    "documents_extract_pages",
    "documents_split_pages",
}
_CODE_SESSION_TOOL_NAMES = {
    "runs_code_session_create",
    "runs_code_session_get",
    "runs_code_session_execute",
}
_CODE_DEPENDENCY_TOOL_NAMES = {
    "runs_code_session_install_dependencies",
}
_PROJECT_WORKSPACE_TOOL_NAMES = {
    "runs_project_workspace_create",
    "runs_project_workspace_get",
    "runs_project_workspace_inspect",
    "runs_project_workspace_edit",
    "runs_project_workspace_execute",
    "runs_project_workspace_process",
    "runs_project_workspace_close",
}
_RUN_OBSERVATION_TOKENS = {
    "approve",
    "artifact",
    "artifacts",
    "cost",
    "failure",
    "failures",
    "inspect",
    "monitor",
    "observe",
    "pause",
    "progress",
    "result",
    "results",
    "resume",
    "review",
    "start",
    "status",
    "stop",
}
_RUN_REPAIR_TOKENS = {
    "adjust",
    "change",
    "edit",
    "fix",
    "repair",
    "replace",
    "retry",
    "update",
}
_WAKE_INTENT_TOKENS = {
    "later",
    "monitor",
    "nudge",
    "remind",
    "stall",
    "timer",
    "wait",
    "wake",
}
_RUN_OBSERVATION_TOOL_NAMES = {
    "runs_list",
    "runs_get",
    "runs_observe",
    "runs_list_steps",
    "runs_list_artifacts",
    "runs_read_artifact",
    "runs_read_shared_memory",
    "runs_start",
    "runs_pause",
    "runs_resume",
    "runs_stop",
    "runs_update_checkpoint",
    "runs_update_artifact_review",
}
_RUN_REPAIR_TOOL_NAMES = {
    "runs_update_understanding",
    "runs_update_plan",
    "runs_add_step",
    "runs_update_step",
    "runs_add_source",
    "runs_add_evidence",
    "runs_add_artifact",
    "runs_create_text_artifact",
    "runs_create_checkpoint",
    "runs_create_collection",
    "runs_get_collection",
    "runs_upsert_collection_items",
    "runs_partition_collection_items",
    "runs_claim_collection_items",
    "runs_annotate_collection_items",
}
_NEGATED_OPERATION_CLAUSE_RE = re.compile(
    r"\b(?:do\s+not|don['’]?t|never|without|avoid(?:ing)?)\b[^.!?;\n]*",
    re.IGNORECASE,
)


def _clean_prompt_text(value):
    return str(value or "").strip()


def _tool_declaration_key(tool):
    if not isinstance(tool, dict):
        return None
    function = tool.get('function') if isinstance(tool.get('function'), dict) else {}
    name = function.get('name')
    if not name:
        return None
    return str(name)


def _dedupe_tool_declarations(tools):
    deduped = []
    seen = set()
    for tool in tools or []:
        key = _tool_declaration_key(tool)
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        deduped.append(tool)
    return deduped


def _prompt_tokens(value) -> set[str]:
    return set(_PROMPT_TOKEN_RE.findall(str(value or "").lower()))


def _tool_schema_tokens(tool) -> int:
    try:
        payload = json.dumps(tool, ensure_ascii=True, sort_keys=True, default=str)
    except Exception:
        payload = str(tool or "")
    return estimate_text_tokens(payload)


def _tool_declarations_with_names(tool_declarations, names) -> list[dict]:
    wanted = {str(name) for name in names or [] if name}
    return [
        declaration
        for declaration in tool_declarations or []
        if _tool_declaration_key(declaration) in wanted
    ]


def _fit_tool_declarations_to_budget(
    tool_declarations,
    *,
    max_schema_tokens: int,
) -> tuple[list[dict], list[str], int]:
    selected = []
    omitted = []
    used_tokens = 0
    for declaration in _dedupe_tool_declarations(tool_declarations):
        name = _tool_declaration_key(declaration) or "(unnamed)"
        schema_tokens = _tool_schema_tokens(declaration)
        if selected and used_tokens + schema_tokens > max_schema_tokens:
            omitted.append(name)
            continue
        if not selected and schema_tokens > max_schema_tokens:
            omitted.append(name)
            continue
        selected.append(declaration)
        used_tokens += schema_tokens
    return selected, omitted, used_tokens


def _history_has_retained_results(history_messages) -> bool:
    for message in history_messages or []:
        if not isinstance(message, dict):
            continue
        meta = message.get("_context_optimization")
        if isinstance(meta, dict) and meta.get("ref_id"):
            return True
    return False


def _history_has_run_context(history_messages) -> bool:
    for message in history_messages or []:
        if not isinstance(message, dict):
            continue
        for tool_call in message.get("tool_calls") or []:
            function = tool_call.get("function") if isinstance(tool_call, dict) else {}
            name = function.get("name") if isinstance(function, dict) else None
            if str(name or "").startswith("runs_"):
                return True
        if message.get("role") == "tool" and str(message.get("name") or "").startswith("runs_"):
            return True
        event = message.get("event") if isinstance(message.get("event"), dict) else {}
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if str(payload.get("tool_name") or "").startswith("runs_"):
            return True
    return False


def _history_has_code_session_context(history_messages) -> bool:
    for message in history_messages or []:
        if not isinstance(message, dict):
            continue
        for tool_call in message.get("tool_calls") or []:
            function = tool_call.get("function") if isinstance(tool_call, dict) else {}
            name = function.get("name") if isinstance(function, dict) else None
            if str(name or "").startswith("runs_code_session_"):
                return True
        if (
            message.get("role") == "tool"
            and str(message.get("name") or "").startswith("runs_code_session_")
        ):
            return True
        event = message.get("event") if isinstance(message.get("event"), dict) else {}
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if str(payload.get("tool_name") or "").startswith("runs_code_session_"):
            return True
    return False


def _history_has_project_workspace_context(history_messages) -> bool:
    for message in reversed(list(history_messages or [])[-24:]):
        if not isinstance(message, dict):
            continue
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            for call in tool_calls:
                function = call.get("function") if isinstance(call, dict) else {}
                name = function.get("name") if isinstance(function, dict) else None
                if str(name or "").startswith("runs_project_workspace_"):
                    return True
        if (
            message.get("role") == "tool"
            and str(message.get("name") or "").startswith(
                "runs_project_workspace_"
            )
        ):
            return True
        event = message.get("event") if isinstance(message.get("event"), dict) else {}
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if str(payload.get("tool_name") or "").startswith(
            "runs_project_workspace_"
        ):
            return True
        runtime_context = (
            payload.get("runtime_context")
            if isinstance(payload.get("runtime_context"), dict)
            else {}
        )
        if runtime_context.get("runtime_profile_id") == "project.workspace":
            return True
    return False


def _run_tool_declarations_for_turn(
    runtime_run_tool_defs,
    *,
    route_flags: dict,
    invoked_run_context: str | None,
    history_messages,
    execution_scope: ExecutionScope | None = None,
) -> list[dict]:
    scope = execution_scope or ExecutionScope.autonomous()
    tokens = set(route_flags.get("affirmative_tokens") or [])
    has_run_context = bool(invoked_run_context) or _history_has_run_context(history_messages)
    names: set[str] = set()
    python_session_allowed = (
        scope.is_autonomous or "python.session" in scope.runtime_profile_ids
    )
    python_scratch_allowed = (
        scope.is_autonomous or "python.scratch" in scope.runtime_profile_ids
    )
    project_workspace_allowed = (
        scope.is_autonomous or "project.workspace" in scope.runtime_profile_ids
    )
    code_session_requested = bool(tokens & _CODE_SESSION_INTENT_TOKENS)
    if route_flags.get("run_intent") or route_flags.get("broad_task") or invoked_run_context:
        names.add("runs_compile")
    if route_flags.get("has_attachments") or route_flags.get("document_intent"):
        names.update(_DOCUMENT_TOOL_NAMES)
    if (
        route_flags.get("code_intent")
        and not route_flags.get("project_intent")
        and python_session_allowed
        and (code_session_requested or not python_scratch_allowed)
    ) or (
        _history_has_code_session_context(history_messages)
        and python_session_allowed
    ):
        names.update(_CODE_SESSION_TOOL_NAMES)
    if (
        route_flags.get("code_intent")
        and not route_flags.get("project_intent")
        and python_session_allowed
        and code_session_requested
        and tokens
        & {
            "dependency",
            "dependencies",
            "install",
            "package",
            "packages",
        }
    ):
        names.update(_CODE_DEPENDENCY_TOOL_NAMES)
    if project_workspace_allowed and (
        route_flags.get("project_intent")
        or _history_has_project_workspace_context(history_messages)
    ):
        names.update(_PROJECT_WORKSPACE_TOOL_NAMES)
    if (
        python_scratch_allowed
        and route_flags.get("code_intent")
        and not route_flags.get("project_intent")
        and not code_session_requested
    ):
        names.add("runs_execute_python")
    if (route_flags.get("run_intent") or has_run_context) and tokens & _RUN_OBSERVATION_TOKENS:
        names.update(_RUN_OBSERVATION_TOOL_NAMES)
    if (route_flags.get("run_intent") or has_run_context) and tokens & _RUN_REPAIR_TOKENS:
        names.update(_RUN_OBSERVATION_TOOL_NAMES)
        names.update(_RUN_REPAIR_TOOL_NAMES)
    return _tool_declarations_with_names(runtime_run_tool_defs, names)


def _budget_history_messages(history_messages, max_tokens: int | None) -> tuple[list[dict], dict]:
    """Keep a recent, protocol-safe history tail within the configured budget."""
    history = list(history_messages or [])
    if not history:
        return [], {"budget_tokens": max_tokens, "estimated_tokens": 0, "omitted_messages": 0}

    if max_tokens is None:
        estimated_tokens = sum(
            estimate_text_tokens(json.dumps(message, ensure_ascii=True, default=str))
            for message in history
        )
        return history, {
            "budget_tokens": None,
            "estimated_tokens": estimated_tokens,
            "omitted_messages": 0,
            "policy": "preserve_until_model_admission",
        }

    try:
        budget = max(0, int(max_tokens))
    except (TypeError, ValueError):
        budget = DEFAULT_HISTORY_BUDGET_TOKENS
    if budget == 0:
        return [], {"budget_tokens": 0, "estimated_tokens": 0, "omitted_messages": len(history)}

    message_tokens = [
        estimate_text_tokens(json.dumps(message, ensure_ascii=True, default=str))
        for message in history
    ]
    start = len(history)
    used = 0
    while start > 0:
        next_tokens = message_tokens[start - 1]
        if start < len(history) and used + next_tokens > budget:
            break
        used += next_tokens
        start -= 1

    # Never start a provider history on an orphan tool result. Bring back the
    # assistant tool-call message that immediately precedes the retained tail.
    while start > 0 and isinstance(history[start], dict) and history[start].get("role") == "tool":
        start -= 1
        used += message_tokens[start]
        if isinstance(history[start], dict) and history[start].get("role") == "assistant":
            break

    selected = history[start:]
    first_user = next(
        (
            message
            for message in history[:start]
            if isinstance(message, dict) and message.get("role") == "user"
        ),
        None,
    )
    if first_user is not None:
        selected = [first_user, *selected]
        used += estimate_text_tokens(json.dumps(first_user, ensure_ascii=True, default=str))

    return selected, {
        "budget_tokens": budget,
        "estimated_tokens": used,
        "omitted_messages": max(0, len(history) - len(selected)),
    }


def _task_route_flags(task_text: str, *, files_from_client=None, source_count=0) -> dict:
    tokens = _prompt_tokens(task_text)
    affirmative_text = _NEGATED_OPERATION_CLAUSE_RE.sub(" ", str(task_text or ""))
    affirmative_tokens = _prompt_tokens(affirmative_text)
    write_intent = bool(affirmative_tokens & _WRITE_OPERATION_TOKENS)
    read_intent = bool(affirmative_tokens & _READ_OPERATION_TOKENS) or not write_intent
    broad = bool(affirmative_tokens & _BROAD_TASK_TOKENS)
    project_intent = bool(
        affirmative_tokens & _PROJECT_STRONG_INTENT_TOKENS
    ) or bool(
        affirmative_tokens & _PROJECT_ACTION_TOKENS
        and affirmative_tokens & _PROJECT_CONTEXT_TOKENS
    )
    code_intent = bool(affirmative_tokens & _CODE_INTENT_TOKENS)
    one_shot_code_intent = bool(
        code_intent
        and not project_intent
        and not broad
        and not files_from_client
        and not affirmative_tokens & _CODE_SESSION_INTENT_TOKENS
    )
    return {
        "tokens": sorted(tokens),
        "affirmative_tokens": sorted(affirmative_tokens),
        "read_intent": read_intent,
        "write_intent": write_intent,
        "broad_task": broad,
        "parallel_sources": source_count > 1 or "parallel" in affirmative_tokens,
        "memory_intent": bool(affirmative_tokens & _MEMORY_INTENT_TOKENS),
        "context_intent": bool(affirmative_tokens & _CONTEXT_INTENT_TOKENS),
        "run_intent": bool(affirmative_tokens & _RUN_INTENT_TOKENS),
        "document_intent": bool(affirmative_tokens & _DOCUMENT_INTENT_TOKENS),
        "code_intent": code_intent,
        "one_shot_code_intent": one_shot_code_intent,
        "project_intent": project_intent,
        "has_attachments": bool(files_from_client),
    }


def _message_text(message) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if text:
                    parts.append(str(text))
        return "\n".join(parts)
    return ""


def _truncate_one_line(value, limit=180):
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


def _format_initial_model_payload_contract(mode_label: str) -> str:
    is_scoped = "scoped" in str(mode_label or "").lower() or "direct" in str(mode_label or "").lower()
    if is_scoped:
        mode_line = (
            "- `mode`: direct/scoped. The selected toolkits, skills, tool views, and resources are the "
            "complete allowed scope for this turn."
        )
        suggestion_line = (
            "- `suggested_capabilities`: selected definitions or declared tool views only; do not treat "
            "automatic suggestions as permission to expand scope."
        )
        discovery_line = (
            "- `discovery`: autonomous expansion is disabled; request in-chat scope expansion if more is needed."
        )
    else:
        mode_line = (
            "- `mode`: autonomous. Automatic suggestions are advisory hints, not boundaries; compact discovery "
            "remains available for gaps."
        )
        suggestion_line = (
            "- `suggested_capabilities`: use at most 1-3 high-confidence source/toolkit groups from prompt routing."
        )
        discovery_line = (
            "- `discovery`: search, describe, and load progressively only when suggestions are missing, "
            "ambiguous, unavailable, risky, or insufficient."
        )

    return "\n".join([
        "This first model request is intentionally compact. The current user task is the final user message, not this system block.",
        f"Current mode: {mode_label}.",
        "- `task`: final user message plus compact attachment/resource refs and available read/preview actions.",
        mode_line,
        "- `execution_options`: direct answer; inline tools; bounded delegation; durable run through `runs_compile`; code-in-the-loop; permission-gated project workspace.",
        suggestion_line,
        "- `memory`: session memory is temporary working state; long-term user/org memory is for durable facts and preferences.",
        "- `context`: bulky tool results may become summaries or refs; routine compaction is handled by the runtime.",
        discovery_line,
        "Choose the lightest correct path. Use durable runs only when batching, provenance, checkpoints, artifacts, side effects, or long-running work warrant them.",
    ])


def format_memory_access_for_prompt(chat_session) -> str:
    text = (
        "Session memory is a chat-scoped JSON scratchpad available through `update_session_memory`; "
        "use it for task state, coverage notes, compact conclusions, and resumable decisions. "
        "Long-term user/org memory is available through `memorize_fact`, `recall_fact`, and "
        "`manage_long_term_memory`; use it only for stable reusable facts, preferences, policies, "
        "or durable project context."
    )
    working_memory = getattr(chat_session, "working_memory", None)
    if working_memory:
        text += f"\nCurrent session memory:\n{working_memory}\n"
    return text


def _search_capability_suggestions(addon_manager, query: str, limit: int = INITIAL_SUGGESTION_LIMIT) -> list[dict]:
    if not hasattr(addon_manager, "search_capabilities"):
        return []
    try:
        return addon_manager.search_capabilities(query=query, limit=limit) or []
    except TypeError:
        try:
            return addon_manager.search_capabilities(query, limit=limit) or []
        except Exception:
            return []
    except Exception:
        return []


def _tool_names_from_declarations(tool_declarations) -> list[str]:
    names = []
    for declaration in tool_declarations or []:
        name = _tool_declaration_key(declaration)
        if name and name not in names:
            names.append(name)
    return names


def _prompt_time_source_coverage(addon_manager, task_text: str) -> dict:
    if not task_text.strip() or not hasattr(addon_manager, "get_source_coverage"):
        return {}
    try:
        return addon_manager.get_source_coverage(
            task=task_text,
            max_tools_per_source=INITIAL_PRELOADED_READ_TOOL_LIMIT,
            include_tool_cards=False,
        ) or {}
    except Exception:
        return {}


def _prompt_time_coverage_rows(coverage: dict) -> list[dict]:
    return [
        row for row in (coverage.get("source_classes") or [])
        if isinstance(row, dict)
        and row.get("source_class")
        and row.get("status")
    ]


def _prompt_time_read_preload(
    addon_manager,
    coverage: dict,
    config: dict | None = None,
    task_text: str = "",
) -> tuple[list[dict], list[str], list[str]]:
    if not hasattr(addon_manager, "get_tool_declarations_for_capability_ids"):
        return [], [], []

    rows = [
        row for row in _prompt_time_coverage_rows(coverage)
        if row.get("status") == "available"
        and row.get("coverage_sufficient_to_plan")
        and row.get("matched_read_toolkit_ids")
    ]
    canonical_sources = list(dict.fromkeys(
        str(row.get("source_class") or "")
        for row in rows
        if row.get("source_class")
    ))
    if not canonical_sources or len(canonical_sources) > INITIAL_PRELOADED_SOURCE_LIMIT:
        return [], [], []

    config = config if isinstance(config, dict) else {}
    max_tools = _int_from_prompt_config(
        {},
        config,
        ("first_payload_preloaded_tool_limit", "chat_first_payload_preloaded_tool_limit"),
        INITIAL_PRELOADED_READ_TOOL_LIMIT,
    )
    max_tools = max(1, min(max_tools, INITIAL_PRELOADED_READ_TOOL_LIMIT))
    schema_budget = _int_from_prompt_config(
        {},
        config,
        ("first_payload_preload_schema_tokens", "chat_first_payload_preload_schema_tokens"),
        3400 if len(canonical_sources) == 1 else 4400,
    )
    task_tokens = _prompt_tokens(task_text)
    selected_declarations = []
    selected_capability_ids = []
    used_schema_tokens = 0

    def declaration_rank(declaration):
        name = (_tool_declaration_key(declaration) or "").lower()
        name_tokens = _prompt_tokens(name)
        score = len(task_tokens & name_tokens) * 8
        if task_tokens & {"search", "find", "query"} and name_tokens & {"search", "find", "query"}:
            score += 20
        if task_tokens & {"read", "fetch", "get", "open", "summarize"} and name_tokens & {
            "read", "fetch", "get", "message", "messages", "thread", "content", "file"
        }:
            score += 15
        if name_tokens & {"search", "list"}:
            score += 6
        if name_tokens & {"read", "get", "fetch"}:
            score += 4
        return (-score, name)

    remaining_sources = len(canonical_sources)
    for row in rows[:INITIAL_PRELOADED_SOURCE_LIMIT]:
        capability_ids = [
            str(item)
            for item in (row.get("matched_read_toolkit_ids") or [])
            if item
        ][:1]
        allowed_names = {
            str(item)
            for item in (row.get("common_function_names") or [])
            if item
        }
        if not capability_ids or not allowed_names:
            remaining_sources -= 1
            continue

        try:
            declarations = addon_manager.get_tool_declarations_for_capability_ids(
                capability_ids,
                config,
                include_side_effect_tools=False,
            )
        except TypeError:
            try:
                declarations = addon_manager.get_tool_declarations_for_capability_ids(capability_ids, config)
            except Exception:
                remaining_sources -= 1
                continue
        except Exception:
            remaining_sources -= 1
            continue

        candidates = [
            declaration
            for declaration in declarations or []
            if _tool_declaration_key(declaration) in allowed_names
        ]
        candidates.sort(key=declaration_rank)
        remaining_slots = max_tools - len(selected_declarations)
        source_limit = max(1, remaining_slots // max(1, remaining_sources))
        source_selected = 0
        for declaration in candidates:
            if len(selected_declarations) >= max_tools or source_selected >= source_limit:
                break
            schema_tokens = _tool_schema_tokens(declaration)
            if used_schema_tokens + schema_tokens > schema_budget:
                continue
            selected_declarations.append(declaration)
            used_schema_tokens += schema_tokens
            source_selected += 1
        if source_selected:
            selected_capability_ids.extend(capability_ids)
        remaining_sources -= 1

    return (
        selected_declarations,
        list(dict.fromkeys(selected_capability_ids)),
        _tool_names_from_declarations(selected_declarations),
    )


def _format_prompt_time_source_coverage_for_prompt(coverage: dict, *, preloaded_capability_ids=None) -> list[str]:
    rows = _prompt_time_coverage_rows(coverage)
    if not rows:
        return []
    unique_rows = []
    seen_sources = set()
    for row in rows:
        source_class = str(row.get("source_class") or "")
        if not source_class or source_class in seen_sources:
            continue
        seen_sources.add(source_class)
        unique_rows.append(row)
    if not unique_rows:
        return []

    preloaded_set = set(str(item) for item in preloaded_capability_ids or [])
    lines = ["Likely source routes:"]
    for row in unique_rows[:INITIAL_SUGGESTION_LIMIT]:
        source_class = str(row.get("source_class") or "")
        status = str(row.get("status") or "unknown")
        read_ids = [str(item) for item in (row.get("matched_read_toolkit_ids") or []) if item][:2]
        describe_ids = [str(item) for item in (row.get("recommended_describe_capability_ids") or []) if item][:2]
        function_names = [str(item) for item in (row.get("common_function_names") or []) if item][:3]
        detail = f"- source=`{source_class}`; status={status}"
        if read_ids:
            detail += "; capability=" + ", ".join(f"`{item}`" for item in read_ids)
        if describe_ids:
            detail += "; describe=" + ", ".join(f"`{item}`" for item in describe_ids)
        if function_names:
            detail += "; common=" + ", ".join(f"`{item}`" for item in function_names)
        if any(item in preloaded_set for item in read_ids):
            detail += "; callable=true"
        lines.append(detail)
    if len(unique_rows) > 1:
        lines.append(
            "Use callable source tools in parallel. Resolve only missing or ambiguous capabilities."
        )
    return lines


def _capability_status_for_prompt(card: dict, next_action: str) -> str:
    status = card.get("status")
    state = ""
    if isinstance(status, dict):
        state = str(status.get("state") or "")
    elif status:
        state = str(status)
    state = state.lower()
    if state and state not in {"active", "degraded"}:
        return "unavailable"
    if next_action == "use_declared_tool":
        return "callable"
    if next_action in {"load_view", "load_to_session"}:
        return "available_to_load"
    if state in {"active", "degraded"}:
        return "available_to_load"
    return "unknown"


def _source_type_for_capability_card(card: dict) -> str:
    for key in ("source_type", "source_class"):
        value = str(card.get(key) or "").strip()
        if value:
            return value
    text = " ".join(str(card.get(key) or "").lower() for key in ("id", "label", "name", "group", "source_addon_id"))
    if any(token in text for token in ("gmail", "email", "mail")):
        return "email"
    if any(token in text for token in ("gdrive", "google drive", "googledrive", "docs", "sheets", "slides")):
        return "drive"
    if any(token in text for token in ("wordpress", "wp", "taxonomy", "post")):
        return "app"
    if any(token in text for token in ("local", "filesystem", "folder", "file")):
        return "local"
    if any(token in text for token in ("web", "scrape", "browser")):
        return "web"
    if any(token in text for token in ("document", "pdf", "docx")):
        return "document"
    capability_type = str(card.get("capability_type") or "").strip().lower()
    return capability_type or "other"


def _risk_for_capability_card(card: dict) -> str:
    raw = str(card.get("risk") or card.get("risk_class") or "").strip().lower()
    if raw in {"read", "read_only", "readonly"}:
        return "read_only"
    if raw in {"write", "writes_internal", "internal_write"}:
        return "writes_internal"
    if raw in {"external_write", "writes_external", "publish"}:
        return "writes_external"
    if raw in {"high", "delete", "destructive"}:
        return "destructive"
    return "unknown"


def _common_calls_for_capability_card(card: dict) -> list[str]:
    for key in ("common_calls", "common_function_names", "function_names"):
        value = card.get(key)
        if isinstance(value, list):
            return [str(item) for item in value if item][:6]
    return []


def _normalize_next_action_for_prompt(next_action: str) -> str:
    normalized = str(next_action or "").strip().lower()
    if normalized in {"use_declared_tool", "call"}:
        return "use_declared_tool"
    if normalized in {"describe", "capabilities_describe"}:
        return "call_capabilities_describe"
    if normalized in {"load", "load_view", "load_to_session"}:
        return "call_capabilities_describe"
    if normalized in {"permission", "request_permission"}:
        return "request_permission"
    if normalized in {"ask", "ask_user"}:
        return "ask_user"
    if normalized in {"unavailable", "ignore"}:
        return "ignore_unless_needed"
    return normalized or "call_capabilities_describe"


def _format_capability_suggestion_card(card: dict) -> str:
    capability_id = str(card.get("id") or card.get("capability_id") or "").strip()
    name = str(card.get("label") or card.get("name") or capability_id).strip()
    raw_next_action = card.get("next_action") or "describe"
    next_action = _normalize_next_action_for_prompt(str(raw_next_action))
    status = _capability_status_for_prompt(card, str(raw_next_action))
    common_calls = _common_calls_for_capability_card(card)
    calls_text = ", ".join(f"`{name}`" for name in common_calls) if common_calls else "none declared"
    why = (
        str(card.get("why") or card.get("match_reason") or card.get("description") or "")
        .strip()
    )
    why = _truncate_one_line(why, 130)
    return (
        f"- id=`{capability_id}`; name={name}; source_type={_source_type_for_capability_card(card)}; "
        f"confidence={card.get('confidence') or 'high'}; status={status}; risk={_risk_for_capability_card(card)}; "
        f"common_calls={calls_text}; next_action={next_action}; why={why}"
    )


def build_automatic_capability_suggestions_for_prompt(
    addon_manager,
    user_message_for_llm,
    *,
    selected_capability_ids=None,
    limit: int = INITIAL_SUGGESTION_LIMIT,
    config: dict | None = None,
) -> PromptTimeCapabilitySuggestions:
    task_text = _message_text(user_message_for_llm)
    routing_task_text = _NEGATED_OPERATION_CLAUSE_RE.sub(" ", task_text).strip()
    selected_ids = [str(item) for item in selected_capability_ids or [] if item]
    source_coverage = _prompt_time_source_coverage(addon_manager, routing_task_text)
    coverage_rows = _prompt_time_coverage_rows(source_coverage)
    coverage_has_route = any(
        row.get("coverage_sufficient_to_plan")
        and (
            row.get("matched_read_toolkit_ids")
            or row.get("matched_write_toolkit_ids")
            or row.get("recommended_describe_capability_ids")
        )
        for row in coverage_rows
    )
    suggestions = []
    if not coverage_has_route:
        suggestions = [
            card
            for card in _search_capability_suggestions(
                addon_manager,
                routing_task_text,
                limit=max(limit * 2, 5),
            )
            if isinstance(card, dict) and str(card.get("confidence") or "").lower() == "high"
        ][:limit]
    preloaded_tools, preloaded_capability_ids, preloaded_tool_names = _prompt_time_read_preload(
        addon_manager,
        source_coverage,
        config=config,
        task_text=routing_task_text,
    )
    lines = []
    if preloaded_tool_names:
        lines.append(
            "Callable read tools: "
            + ", ".join(f"`{name}`" for name in preloaded_tool_names)
            + ". Use them directly; resolve capabilities only for gaps."
        )
    lines.extend(_format_prompt_time_source_coverage_for_prompt(
        source_coverage,
        preloaded_capability_ids=preloaded_capability_ids,
    ))
    if selected_ids:
        lines.append(
            "Session-loaded capabilities: "
            + ", ".join(selected_ids[:8])
            + ("..." if len(selected_ids) > 8 else "")
            + "."
        )
    if not suggestions:
        return PromptTimeCapabilitySuggestions(
            prompt_text="\n".join(lines),
            tool_declarations=preloaded_tools,
            preloaded_capability_ids=preloaded_capability_ids,
            preloaded_tool_names=preloaded_tool_names,
            source_classes=[
                str(row.get("source_class"))
                for row in _prompt_time_coverage_rows(source_coverage)
                if row.get("source_class")
            ][:INITIAL_SUGGESTION_LIMIT],
            suggested_capability_ids=[],
        )

    lines.append("High-confidence capability routes:")
    for card in suggestions[:limit]:
        lines.append(_format_capability_suggestion_card(card))
    return PromptTimeCapabilitySuggestions(
        prompt_text="\n".join(lines),
        tool_declarations=preloaded_tools,
        preloaded_capability_ids=preloaded_capability_ids,
        preloaded_tool_names=preloaded_tool_names,
        source_classes=[
            str(row.get("source_class"))
            for row in _prompt_time_coverage_rows(source_coverage)
            if row.get("source_class")
        ][:INITIAL_SUGGESTION_LIMIT],
        suggested_capability_ids=[
            str(card.get("id") or card.get("capability_id"))
            for card in suggestions[:limit]
            if card.get("id") or card.get("capability_id")
        ],
    )


def format_automatic_capability_suggestions_for_prompt(
    addon_manager,
    user_message_for_llm,
    *,
    selected_capability_ids=None,
    limit: int = INITIAL_SUGGESTION_LIMIT,
) -> str:
    return build_automatic_capability_suggestions_for_prompt(
        addon_manager,
        user_message_for_llm,
        selected_capability_ids=selected_capability_ids,
        limit=limit,
    ).prompt_text


def format_active_runtime_policy_for_prompt(settings, execution_scope: ExecutionScope | None = None):
    scope = execution_scope or ExecutionScope.autonomous()
    policy = resolve_runtime_policy(settings)
    approval_profile = policy["approval_profile"]
    max_tool_calls = policy["max_tool_calls"]
    extension_max = policy["chat_tool_budget_extension_max"]
    discovery_text = (
        "allowed; use source coverage/runtime inventory before opening full schemas"
        if scope.allow_discovery
        else "disabled; use only selected scope and request expansion if needed"
    )
    extension_text = (
        f"available once per turn via request_tool_budget_extension, up to +{extension_max} iterations"
        if extension_max > 0
        else "disabled for this turn"
    )
    approval_text = (
        "routine side-effect approval prompts are disabled; still validate, log, and avoid irreversible actions unless requested"
        if approval_profile == "full_access"
        else "ask before side effects, publishing, deletion, or high-cost expansion"
    )
    return (
        f"mode={scope.mode}; discovery={discovery_text}; "
        f"policy_profile={policy['profile']}; "
        f"approval_profile={approval_profile} ({approval_text}); "
        f"context_stewardship={policy.get('context_stewardship', 'assisted')}; "
        f"max_tool_calls={max_tool_calls}; budget_extension={extension_text}; "
        "code_in_loop=only use if an available runtime profile explicitly supports it."
    )


def _int_from_prompt_config(settings, config, keys, default: int) -> int:
    settings = settings if isinstance(settings, dict) else {}
    config = config if isinstance(config, dict) else {}
    for key in keys:
        raw_value = config.get(key)
        if raw_value is None:
            raw_value = settings.get(key)
        if raw_value is None:
            continue
        try:
            return max(0, int(str(raw_value).strip()))
        except (TypeError, ValueError):
            continue
    return int(default)


def _history_has_context_pressure(history_messages=None, settings=None, config=None) -> bool:
    history_messages = history_messages or []
    policy = resolve_runtime_policy(settings)
    stewardship = str(policy.get("context_stewardship") or "assisted").strip().lower()
    if stewardship in {"off", "disabled"}:
        return False

    min_included = _int_from_prompt_config(
        settings,
        config,
        (
            "context_prompt_pressure_min_included_tokens",
            "context_pressure_min_included_tokens",
            "context_pressure_included_tokens",
        ),
        DEFAULT_CONTEXT_PRESSURE_INCLUDED_TOKENS,
    )
    min_raw = _int_from_prompt_config(
        settings,
        config,
        (
            "context_prompt_pressure_min_raw_tokens",
            "context_pressure_min_raw_tokens",
            "context_pressure_raw_tokens",
        ),
        DEFAULT_CONTEXT_PRESSURE_RAW_TOKENS,
    )

    def safe_int(value):
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    total_included_tokens = 0
    has_large_raw_ref = False
    has_large_inline = False
    for message in history_messages:
        if not isinstance(message, dict):
            continue
        meta = message.get("_context_optimization")
        if not isinstance(meta, dict):
            continue
        raw_tokens = safe_int(meta.get("estimated_raw_tokens"))
        included_tokens = safe_int(meta.get("estimated_included_tokens"))
        retention_state = str(meta.get("retention_state") or "")
        total_included_tokens += included_tokens
        has_large_raw_ref = has_large_raw_ref or raw_tokens >= min_raw
        has_large_inline = has_large_inline or (
            included_tokens >= min_included
            or (
                retention_state in {"inline_keep", "inline_this_turn", "pin_for_chief"}
                and raw_tokens >= max(1, min_raw // 2)
            )
        )

    return total_included_tokens >= min_included or has_large_raw_ref or has_large_inline


def _context_stewardship_detail_requested(settings=None, config=None, history_messages=None) -> bool:
    settings = settings if isinstance(settings, dict) else {}
    config = config if isinstance(config, dict) else {}
    raw_value = (
        config.get("context_stewardship_prompt")
        or config.get("context_prompt_detail")
        or settings.get("context_stewardship_prompt")
        or settings.get("prompt_context_stewardship_detail")
        or ""
    )
    if str(raw_value).strip().lower() in {"full", "detailed", "always", "true", "1", "yes"}:
        return True
    return _history_has_context_pressure(history_messages, settings=settings, config=config)


def format_context_stewardship_for_prompt(*, detailed: bool = False):
    if not detailed:
        return (
            "Routine tool-result compaction is handled by the runtime only through explicit, reversible "
            "admission or terminal-housekeeping decisions. Evidence is durable while the prompt is a "
            "model-specific view. Use complete results when they "
            "help the current turn. At the terminal response, preserve important conclusions and evidence IDs "
            "in session memory, then explicitly disposition eligible results. Archived evidence remains "
            "searchable and restorable; manual mode forbids silent removal or summarization."
        )
    return (
        "Treat context as a reversible view over durable evidence. A complete tool result or source can be "
        "used directly when it fits the selected model. When it does not fit, use the explicit overflow "
        "manifest to choose full coverage, exact ranges, structured filters, or another model; do not silently "
        "drop evidence. Assisted mode performs housekeeping in the terminal response: write the answer, an "
        "evidence-linked memory patch, and explicit context dispositions together. Manual mode keeps every "
        "item active unless the user authorizes a change. Aggressive mode may prefer references or summaries, "
        "but still retains raw evidence. Use archive search/read to recover earlier evidence rather than "
        "re-running expensive reads.\n\n"
        "For broad research or corpus work, treat source coverage separately from context retention. "
        "Use `source_coverage_upsert` to log discovered, queued, inspected, skipped, or failed sources "
        "with purpose, evidence refs, worker/code owner, confidence, and concise findings. Use "
        "`source_coverage_get` before duplicate searches/reads, before delegating a batch, and before "
        "final synthesis so you can identify what is already covered and what remains. "
        "This is lightweight chat-scoped coverage; create a durable run collection when the task needs "
        "resumable batching, item claiming, partitioning, artifacts, or long-running work.\n\n"
        "Routing rubric: do simple bounded work directly. Delegate broad repetitive extraction, "
        "classification, or triage over many emails/documents/pages/images to fast or medium workers "
        "when quality permits. Create a durable run when the task needs source ledgers, batching, "
        "fan-out/fan-in, resumability, artifacts, checkpoints, or many items. Read key primary "
        "documents yourself when they anchor the final answer, require nuanced judgment, or worker "
        "findings conflict."
    )


def build_user_instructions_context(assistant_instructions=None, addon_settings=None):
    """
    Builds the user preference block injected into prompts by combining the
    legacy `assistant_instructions` field with profile settings.
    """
    profile = {}
    if isinstance(addon_settings, dict):
        profile_data = addon_settings.get("profile")
        if isinstance(profile_data, dict):
            profile = profile_data

    sections = []
    seen = set()

    def append_section(label, raw_value):
        text = _clean_prompt_text(raw_value)
        if not text:
            return
        dedupe_key = " ".join(text.split()).lower()
        if dedupe_key in seen:
            return
        seen.add(dedupe_key)
        sections.append(f"{label}:\n{text}")

    append_section("Assistant instructions", assistant_instructions)
    append_section("General preferences", profile.get("preferences"))
    has_style_reference = isinstance(profile, dict) and "style_reference" in profile
    if has_style_reference:
        append_section("Style preferences and writing samples", profile.get("style_reference"))
    else:
        append_section("Style guidelines", profile.get("style_guidelines"))
        append_section("Writing samples", profile.get("writing_samples"))

    memory_settings = profile.get("memory") if isinstance(profile.get("memory"), dict) else {}
    raw_categories = memory_settings.get("category_definitions")
    if raw_categories is not None:
        category_defs = sanitize_memory_category_definitions(raw_categories)
        category_text = ", ".join([f"{entry['label']} ({entry['key']})" for entry in category_defs])
        append_section("Preferred memory categories", category_text)

    return "\n\n".join(sections)


def resolve_chat_invocation(prompt, addon_manager):
    """Resolves slash commands that alter the LLM-facing user task."""
    prompt_text = str(prompt or "").strip()
    prompt_parts = prompt_text.split(maxsplit=2)
    if not prompt_parts:
        return ChatInvocation(user_content=prompt)

    command = prompt_parts[0].lower()
    if command in {'/skill', '/sk'}:
        if len(prompt_parts) < 2:
            return ChatInvocation(user_content=prompt, error=f"Use {command} <skill_id> <task>.")
        skill_id = prompt_parts[1]
        skill_instructions = addon_manager.render_skill_instructions(skill_id)
        if not skill_instructions:
            return ChatInvocation(user_content=prompt, error=f"Skill '{skill_id}' is unavailable.")
        return ChatInvocation(
            user_content=prompt_parts[2] if len(prompt_parts) > 2 else f"Use the {skill_id} skill.",
            skill_id=skill_id,
            skill_instructions=skill_instructions,
        )

    if command == '/workflow':
        if len(prompt_parts) < 2:
            return ChatInvocation(user_content=prompt, error="Use /workflow <workflow_id> [dry-run notes].")
        workflow_id = prompt_parts[1]
        workflow_context = addon_manager.render_workflow_discovery(workflow_id)
        if not workflow_context:
            return ChatInvocation(user_content=prompt, error=f"Workflow '{workflow_id}' is unavailable.")
        return ChatInvocation(
            user_content=prompt_parts[2] if len(prompt_parts) > 2 else f"Dry-run validate workflow {workflow_id}.",
            workflow_context=workflow_context,
        )

    if command == '/run':
        if len(prompt_parts) < 2:
            return ChatInvocation(user_content=prompt, error="Use /run <task>.")
        return ChatInvocation(
            user_content=prompt_text[len('/run'):].strip(),
            run_context=(
                "The user explicitly requested a durable ad hoc run. Create an ad hoc run before "
                "execution and persist understanding, plan, steps, sources, and evidence as applicable."
            ),
        )

    return ChatInvocation(user_content=prompt)


def build_chat_messages_and_tools(
    *,
    history_messages,
    assistant_mode,
    addon_manager,
    settings,
    user_instructions,
    chat_session,
    user_message_for_llm,
    files_from_client,
    selected_capability_ids,
    enable_delegation,
    invoked_skill_id,
    invoked_skill_instructions,
    invoked_workflow_context,
    invoked_run_context,
    has_vector_collections,
    meta_capability_tool_defs,
    runtime_run_tool_defs,
    config,
    now=None,
):
    """Builds the system prompt, message list, and tool declarations for a chat turn."""
    if assistant_mode == 'default':
        return _build_default_mode_prompt(
            history_messages=history_messages,
            addon_manager=addon_manager,
            settings=settings,
            user_instructions=user_instructions,
            chat_session=chat_session,
            user_message_for_llm=user_message_for_llm,
            files_from_client=files_from_client,
            selected_capability_ids=selected_capability_ids,
            invoked_skill_id=invoked_skill_id,
            invoked_skill_instructions=invoked_skill_instructions,
            invoked_workflow_context=invoked_workflow_context,
            invoked_run_context=invoked_run_context,
            has_vector_collections=has_vector_collections,
            enable_delegation=enable_delegation,
            meta_capability_tool_defs=meta_capability_tool_defs,
            runtime_run_tool_defs=runtime_run_tool_defs,
            config=config,
            now=now,
        )

    return _build_direct_mode_prompt(
        history_messages=history_messages,
        addon_manager=addon_manager,
        settings=settings,
        user_instructions=user_instructions,
        chat_session=chat_session,
        user_message_for_llm=user_message_for_llm,
        files_from_client=files_from_client,
        selected_capability_ids=selected_capability_ids,
        enable_delegation=enable_delegation,
        invoked_skill_id=invoked_skill_id,
        invoked_skill_instructions=invoked_skill_instructions,
        invoked_workflow_context=invoked_workflow_context,
        invoked_run_context=invoked_run_context,
        has_vector_collections=has_vector_collections,
        meta_capability_tool_defs=meta_capability_tool_defs,
        runtime_run_tool_defs=runtime_run_tool_defs,
        config=config,
        now=now,
    )


def _base_prompt_sections(
    addon_manager,
    invoked_skill_instructions,
    invoked_workflow_context,
    invoked_run_context,
    *,
    include_capability_discovery=True,
    include_available_skills=True,
    scoped_run_protocol=False,
):
    sections = []
    if include_capability_discovery:
        sections.append(f"--- CAPABILITY DISCOVERY ---\n{addon_manager.format_capability_protocol_for_prompt()}")
    run_protocol = format_run_protocol_for_prompt()
    if scoped_run_protocol:
        run_protocol = run_protocol.replace(
            "1. Discover capabilities/guidance using the available capabilities tools.",
            "1. Use only the capabilities, skills, resources, and tool schemas already selected for this scoped turn.",
        )
    sections.append(f"--- RUN PLANNING ---\n{run_protocol}")
    if include_available_skills:
        available_skills = addon_manager.format_available_skills_for_prompt()
        if available_skills:
            sections.append(f"--- AVAILABLE SKILLS ---\n{available_skills}")
    if invoked_skill_instructions:
        sections.append(f"--- INVOKED SKILL ---\n{invoked_skill_instructions}")
    if invoked_workflow_context:
        sections.append(f"--- INVOKED WORKFLOW ---\n{invoked_workflow_context}")
    if invoked_run_context:
        sections.append(f"--- INVOKED RUN ---\n{invoked_run_context}")
    return sections


def _finish_messages(history_messages, system_prompt, user_message_for_llm, now):
    turn_time = now or utc_now()
    messages = list(history_messages)
    messages.insert(0, {"role": "system", "content": f"Time: {turn_time.strftime('%Y-%m-%d %H:%M:%S UTC')}.\n\n{system_prompt}"})
    messages.append(user_message_for_llm)
    return messages


def _compact_runtime_core(
    *,
    execution_scope: ExecutionScope,
    settings,
    route_flags: dict,
) -> str:
    policy = resolve_runtime_policy(settings)
    if execution_scope.is_scoped:
        mode_line = (
            "Mode: Scoped. Declared capabilities, tools, views, skills, and resources are the complete "
            "boundary; request scope expansion instead of discovering outside it."
        )
    else:
        mode_line = (
            "Mode: Autonomous. Prompt-routed tools are suggestions, not a boundary; use compact capability "
            "resolution only when declared tools are insufficient."
        )
    approval = str(policy.get("approval_profile") or "review")
    context_mode = str(policy.get("context_stewardship") or "assisted")
    route_hint = (
        "Independent source reads may run in parallel."
        if route_flags.get("parallel_sources")
        else "Prefer a direct answer or the smallest useful inline tool sequence."
    )
    if context_mode == "manual":
        context_line = (
            "Context: preserve every conversation and evidence item until the user explicitly authorizes a "
            "change; on overflow, report the exact manifest and propose changes without applying them."
        )
    elif context_mode == "aggressive":
        context_line = (
            "Context: raw evidence remains durable; prefer smaller reversible materializations and complete "
            "terminal housekeeping, while respecting pins and evidence links."
        )
    else:
        context_line = (
            "Context: the runtime retains bulky results as durable evidence. Use complete evidence when useful, "
            "then finish the turn with evidence-linked memory and explicit reversible dispositions; archived "
            "evidence remains searchable."
        )
    return "\n".join([
        mode_line,
        (
            f"Policy: approval={approval}; context={context_mode}; "
            f"tool_iterations={policy.get('max_tool_calls')}. Full access changes approvals, never capability scope."
        ),
        "Memory: session memory is temporary task state; durable memory is only for stable reusable facts.",
        context_line,
        (
            "Knowledge: do not default to vector-only RAG. Use structured filters for dates and fields, BM25 "
            "lexical search for quoted phrases, semantic search for ordinary paraphrases/concepts, and weighted "
            "hybrid retrieval for identifiers, corrections, or chronology. Use complete source reads when useful "
            "and admitted by the selected model, and coverage/programmatic processing for exhaustive work. "
            "Request only useful result fields, but never discard canonical provenance."
        ),
        (
            "Strategy: choose the lightest sufficient path—inline for bounded work, delegate repetitive bounded reads, "
            "and use `runs_compile` only for durable, resumable, reviewable, or genuinely broad work. "
            + route_hint
        ),
        "Continue after tool results with the next useful call or a final answer; report errors and empty results.",
    ])


def assemble_first_payload_context(
    *,
    base_prompt: str,
    execution_scope: ExecutionScope,
    settings,
    user_instructions: str,
    chat_session,
    history_messages,
    user_message_for_llm,
    tool_declarations,
    route_flags: dict,
    routing_metadata: dict,
    config,
    extra_sections=None,
    schema_budget_tokens: int | None,
    strict_schema_budget: bool,
    now=None,
) -> ChatPromptBuildResult:
    """Assemble the single provider-facing first-payload contract for all chat modes."""
    config = config if isinstance(config, dict) else {}
    policy = resolve_runtime_policy(settings)
    stewardship = str(policy.get("context_stewardship") or "assisted").strip().lower()
    history_budget = None
    if stewardship != "manual":
        for history_key in (
            "chat_history_budget_tokens",
            "first_payload_history_budget_tokens",
        ):
            raw_history_budget = config.get(history_key)
            if raw_history_budget is None:
                raw_history_budget = settings.get(history_key)
            if raw_history_budget in (None, ""):
                continue
            try:
                history_budget = max(0, int(str(raw_history_budget).strip()))
            except (TypeError, ValueError):
                history_budget = None
            break
    budgeted_history, history_metadata = _budget_history_messages(history_messages, history_budget)

    deduped_tools = _dedupe_tool_declarations(tool_declarations)
    full_schema_tokens = sum(_tool_schema_tokens(tool) for tool in deduped_tools)
    if schema_budget_tokens is None:
        fitted_tools = deduped_tools
        omitted_names = []
        used_schema_tokens = full_schema_tokens
    else:
        fitted_tools, omitted_names, used_schema_tokens = _fit_tool_declarations_to_budget(
            deduped_tools,
            max_schema_tokens=max(1, int(schema_budget_tokens)),
        )
    if strict_schema_budget and omitted_names:
        return ChatPromptBuildResult(
            messages=[],
            tools=[],
            routing_metadata={
                **routing_metadata,
                "history": history_metadata,
                "schema_budget_tokens": schema_budget_tokens,
                "schema_tokens_requested": full_schema_tokens,
                "omitted_tool_names": omitted_names,
            },
            error=(
                "The selected Scoped capability definitions exceed the first-turn schema budget "
                f"({full_schema_tokens} estimated tokens requested; {schema_budget_tokens} allowed). "
                "Narrow the selected capabilities or choose smaller tool views; the runtime did not silently omit them."
            ),
        )

    sections = [_clean_prompt_text(base_prompt), "--- RUNTIME CORE ---\n" + _compact_runtime_core(
        execution_scope=execution_scope,
        settings=settings,
        route_flags=route_flags,
    )]
    if omitted_names:
        sections.append(
            "--- SCHEMA BUDGET ---\n"
            "These optional declarations were omitted from this turn and are not callable: "
            + ", ".join(f"`{name}`" for name in omitted_names[:12])
            + (", ..." if len(omitted_names) > 12 else "")
            + ". Use a declared capability gateway to resolve a narrower operation group if needed."
        )
    if settings.get("org_functions_enabled") == "true" and settings.get("org_profile"):
        sections.append("--- ORGANIZATION PROFILE ---\n" + str(settings.get("org_profile")))
    if user_instructions:
        sections.append("--- USER PREFERENCES ---\n" + user_instructions)

    working_memory = getattr(chat_session, "working_memory", None)
    if working_memory:
        sections.append("--- SESSION MEMORY ---\n" + str(working_memory))
    for section in extra_sections or []:
        clean_section = _clean_prompt_text(section)
        if clean_section:
            sections.append(clean_section)

    system_prompt = "\n\n".join(section for section in sections if section)
    messages = _finish_messages(
        budgeted_history,
        system_prompt,
        dict(user_message_for_llm),
        now,
    )
    declared_tool_names = _tool_names_from_declarations(fitted_tools)
    effective_routing_metadata = {
        **routing_metadata,
        "history": history_metadata,
        "schema_budget_tokens": schema_budget_tokens,
        "schema_tokens_requested": full_schema_tokens,
        "schema_tokens_declared": used_schema_tokens,
        "omitted_tool_names": omitted_names,
        "declared_tool_names": declared_tool_names,
    }
    if "preloaded_tool_names" in routing_metadata:
        declared_name_set = set(declared_tool_names)
        effective_routing_metadata["effective_preloaded_tool_names"] = [
            name
            for name in routing_metadata.get("preloaded_tool_names") or []
            if name in declared_name_set
        ]

    return ChatPromptBuildResult(
        messages=messages,
        tools=fitted_tools,
        routing_metadata=effective_routing_metadata,
    )


def _build_default_mode_prompt(
    *,
    history_messages,
    addon_manager,
    settings,
    user_instructions,
    chat_session,
    user_message_for_llm,
    files_from_client,
    selected_capability_ids,
    invoked_skill_id,
    invoked_skill_instructions,
    invoked_workflow_context,
    invoked_run_context,
    has_vector_collections,
    enable_delegation,
    meta_capability_tool_defs,
    runtime_run_tool_defs,
    config,
    now,
):
    automatic_suggestions = build_automatic_capability_suggestions_for_prompt(
        addon_manager,
        user_message_for_llm,
        selected_capability_ids=selected_capability_ids,
        config=config,
    )
    user_message = dict(user_message_for_llm)
    if files_from_client:
        content = str(user_message.get('content') or '')
        user_message['content'] = (
            f"{content}\n\n"
            f"(Attached resource refs: {json.dumps(files_from_client, ensure_ascii=True)}. "
            "Use the declared attachment reader when content is needed.)"
        )

    source_count = len(automatic_suggestions.source_classes)
    route_flags = _task_route_flags(
        _message_text(user_message),
        files_from_client=files_from_client,
        source_count=source_count,
    )
    execution_scope = ExecutionScope.autonomous(capability_ids=selected_capability_ids)
    route_tokens = set(route_flags.get("affirmative_tokens") or [])
    retained_results = _history_has_retained_results(history_messages)
    context_pressure = _history_has_context_pressure(history_messages, settings=settings, config=config)

    tools = [
        REQUEST_USER_CHOICE_TOOL_DEF,
        *_tool_declarations_with_names(
            meta_capability_tool_defs,
            {
                "capabilities_search",
                "capabilities_describe",
                *(
                    {"capabilities_runtime_inventory"}
                    if route_flags["project_intent"]
                    or route_flags["document_intent"]
                    or route_flags["run_intent"]
                    else set()
                ),
            },
        ),
        *automatic_suggestions.tool_declarations,
    ]

    if retained_results:
        tools.extend([CONTEXT_READ_RESULT_TOOL_DEF, CONTEXT_SEARCH_RESULT_TOOL_DEF])
    if retained_results and (context_pressure or route_flags["context_intent"]):
        tools.append(CONTEXT_MANAGE_RESULTS_TOOL_DEF)
    if route_flags["has_attachments"]:
        tools.append(VIEW_ATTACHMENT_TOOL_DEF)
    if route_flags["memory_intent"]:
        if route_tokens & {"remember", "memorize"}:
            tools.append(MEMORIZE_FACT_TOOL_DEF)
        if route_tokens & {"recall", "remember", "memory"}:
            tools.append(RECALL_FACT_TOOL_DEF)
        if route_tokens & {"forget", "memory"}:
            tools.append(MANAGE_LONG_TERM_MEMORY_TOOL_DEF)
    if route_flags["broad_task"]:
        tools.append(WORKING_MEMORY_TOOL_DEF)
        if enable_delegation:
            tools.append(DELEGATE_TASK_TOOL_DEF)
    tools.extend(_run_tool_declarations_for_turn(
        runtime_run_tool_defs,
        route_flags=route_flags,
        invoked_run_context=invoked_run_context,
        history_messages=history_messages,
        execution_scope=execution_scope,
    ))
    if route_tokens & _WAKE_INTENT_TOKENS:
        tools.append(REQUEST_SELF_NUDGE_TOOL_DEF)
    if route_flags["broad_task"] and route_tokens & {"coverage", "ledger", "sources"}:
        tools.extend([SOURCE_COVERAGE_GET_TOOL_DEF, SOURCE_COVERAGE_UPSERT_TOOL_DEF])
    if has_vector_collections:
        tools.extend([
            KNOWLEDGE_QUERY_TOOL_DEF,
            LIST_KNOWLEDGE_COLLECTIONS_TOOL_DEF,
        ])
        if "rag" in route_tokens:
            tools.append(RAG_SEARCH_TOOL_DEF)

    if selected_capability_ids and hasattr(addon_manager, 'get_tool_declarations_for_capability_ids'):
        tools.extend(
            addon_manager.get_tool_declarations_for_capability_ids(
                selected_capability_ids,
                config,
                include_side_effect_tools=route_flags["write_intent"],
            )
        )
    if invoked_skill_id:
        tools.extend(addon_manager.get_skill_tool_declarations(invoked_skill_id))

    if source_count == 0:
        default_schema_budget = 1800
    elif source_count == 1:
        default_schema_budget = 3600
    else:
        default_schema_budget = DEFAULT_AUTONOMOUS_SCHEMA_BUDGET_TOKENS
    if (
        route_flags["has_attachments"]
        or route_flags["document_intent"]
        or route_flags["code_intent"]
    ):
        default_schema_budget = max(default_schema_budget, 7000)
    has_run_context = bool(invoked_run_context) or _history_has_run_context(
        history_messages
    )
    route_tokens = set(route_flags.get("affirmative_tokens") or [])
    if (
        (route_flags["run_intent"] or has_run_context)
        and route_tokens & _RUN_REPAIR_TOKENS
    ):
        default_schema_budget = max(
            default_schema_budget,
            DEFAULT_AUTONOMOUS_RUN_SCHEMA_BUDGET_TOKENS,
        )
    elif (
        route_flags["run_intent"]
        or route_flags["broad_task"]
        or invoked_run_context
        or (
            has_run_context
            and route_tokens & _RUN_OBSERVATION_TOKENS
        )
    ):
        default_schema_budget = max(
            default_schema_budget,
            DEFAULT_AUTONOMOUS_SCHEMA_BUDGET_TOKENS,
        )
    schema_budget = _int_from_prompt_config(
        settings,
        config,
        ("first_payload_schema_budget_tokens", "chat_first_payload_schema_budget_tokens"),
        default_schema_budget,
    )

    base_prompt = settings.get('prompt_chief_assistant') or CHIEF_ASSISTANT_DEFAULT_PROMPT
    try:
        base_prompt = base_prompt.format(plugin_list=automatic_suggestions.prompt_text)
    except (KeyError, ValueError):
        base_prompt = str(base_prompt).replace("{plugin_list}", automatic_suggestions.prompt_text)

    extra_sections = []
    if invoked_skill_instructions:
        extra_sections.append("--- INVOKED SKILL ---\n" + invoked_skill_instructions)
    if invoked_workflow_context:
        extra_sections.append("--- INVOKED WORKFLOW ---\n" + invoked_workflow_context)
    if invoked_run_context:
        extra_sections.append("--- INVOKED RUN ---\n" + invoked_run_context)
    if route_flags["one_shot_code_intent"]:
        extra_sections.append(
            "--- ONE-SHOT CODE EXECUTION ---\n"
            "Use `runs_execute_python` directly with no run_id; it creates its own "
            "lightweight auditable run. Do not search the capability inventory, create "
            "a code session, or open a project workspace. After one successful execution, "
            "answer from its stdout and stop calling tools."
        )

    return assemble_first_payload_context(
        base_prompt=base_prompt,
        execution_scope=execution_scope,
        settings=settings,
        user_instructions=user_instructions,
        chat_session=chat_session,
        history_messages=history_messages,
        user_message_for_llm=user_message,
        tool_declarations=tools,
        route_flags=route_flags,
        routing_metadata={
            "mode": execution_scope.mode,
            "source_classes": automatic_suggestions.source_classes,
            "suggested_capability_ids": automatic_suggestions.suggested_capability_ids,
            "preloaded_capability_ids": automatic_suggestions.preloaded_capability_ids,
            "preloaded_tool_names": automatic_suggestions.preloaded_tool_names,
            "route_flags": route_flags,
        },
        config=config,
        extra_sections=extra_sections,
        schema_budget_tokens=schema_budget,
        strict_schema_budget=False,
        now=now,
    )


def _build_direct_mode_prompt(
    *,
    history_messages,
    addon_manager,
    settings,
    user_instructions,
    chat_session,
    user_message_for_llm,
    files_from_client,
    selected_capability_ids,
    enable_delegation,
    invoked_skill_id,
    invoked_skill_instructions,
    invoked_workflow_context,
    invoked_run_context,
    has_vector_collections,
    meta_capability_tool_defs,
    runtime_run_tool_defs,
    config,
    now,
):
    execution_scope = ExecutionScope.from_dict(config.get("execution_scope") if isinstance(config, dict) else None)
    plugin_prefixes = _resolve_toolkit_plugin_prefixes(addon_manager, selected_capability_ids)
    task_text = _message_text(user_message_for_llm)
    route_flags = _task_route_flags(
        task_text,
        files_from_client=files_from_client,
        source_count=len(execution_scope.capability_ids),
    )
    route_tokens = set(route_flags.get("affirmative_tokens") or [])
    retained_results = _history_has_retained_results(history_messages)
    context_pressure = _history_has_context_pressure(history_messages, settings=settings, config=config)
    extra_sections = [
        (
            "--- ACTIVE EXECUTION SCOPE ---\n"
            + json.dumps(execution_scope.to_dict(), ensure_ascii=True, sort_keys=True)
        )
    ]
    if execution_scope.allow_scope_expansion_request:
        extra_sections.append(
            "If required capability scope is missing, call `request_scope_expansion` with exact IDs and a concrete reason."
        )
    else:
        extra_sections.append(
            "If required scope is missing, state the gap and ask the user to change the selected scope."
        )

    selected_resource_references = config.get("selected_resource_references") if isinstance(config, dict) else None
    if selected_resource_references:
        extra_sections.append(
            "--- SELECTED RESOURCE GRANTS ---\n"
            + json.dumps(selected_resource_references, ensure_ascii=True, sort_keys=True)
        )

    if invoked_skill_instructions:
        extra_sections.append("--- INVOKED SKILL ---\n" + invoked_skill_instructions)
    if invoked_workflow_context:
        extra_sections.append("--- INVOKED WORKFLOW ---\n" + invoked_workflow_context)
    if invoked_run_context:
        extra_sections.append("--- INVOKED RUN ---\n" + invoked_run_context)
    if route_flags["one_shot_code_intent"]:
        extra_sections.append(
            "--- ONE-SHOT CODE EXECUTION ---\n"
            "Use `runs_execute_python` directly with no run_id. After one successful "
            "execution, answer from its stdout and stop calling tools."
        )

    if plugin_prefixes:
        manifest = addon_manager.get_plugin_manifest()
        plugin_prompts = [
            settings.get(f"prompt_{prefix}") or next((item.get('plugin_prompt', '') for item in manifest if item['prefix'] == prefix), '')
            for prefix in plugin_prefixes
        ]
        if plugin_prompts:
            extra_sections.append(
                "--- ACTIVE CAPABILITY INSTRUCTIONS ---\n"
                + "\n\n".join(filter(None, plugin_prompts))
            )

    selected_skill_instructions = []
    for skill_id in execution_scope.skill_ids:
        if hasattr(addon_manager, "render_skill_instructions"):
            rendered = addon_manager.render_skill_instructions(skill_id)
            if rendered:
                selected_skill_instructions.append(rendered)
    if selected_skill_instructions:
        extra_sections.append(
            "--- SELECTED SKILL INSTRUCTIONS ---\n"
            + "\n\n".join(selected_skill_instructions)
        )

    if hasattr(addon_manager, 'get_tool_declarations_for_plugin_ids'):
        tools = [
            REQUEST_USER_CHOICE_TOOL_DEF,
            *addon_manager.get_tool_declarations_for_plugin_ids(plugin_prefixes, config),
        ]
    else:
        plugin_tools, _ = addon_manager.get_tools_and_mappings(plugin_prefixes, config)
        tools = [REQUEST_USER_CHOICE_TOOL_DEF, *plugin_tools]
    for skill_id in execution_scope.skill_ids:
        if hasattr(addon_manager, 'get_skill_tool_declarations'):
            tools.extend(addon_manager.get_skill_tool_declarations(skill_id))
    if execution_scope.tool_ids and hasattr(addon_manager, 'registries'):
        tools.extend(addon_manager.registries.tools.generate_tool_declarations(execution_scope.tool_ids))
    if execution_scope.tool_view_ids and hasattr(addon_manager, 'registries'):
        for view_id in execution_scope.tool_view_ids:
            declaration = addon_manager.registries.tools.declaration_for_view(view_id)
            if declaration:
                tools.append(declaration)
    if invoked_skill_id:
        tools.extend(addon_manager.get_skill_tool_declarations(invoked_skill_id))

    if retained_results:
        tools.extend([CONTEXT_READ_RESULT_TOOL_DEF, CONTEXT_SEARCH_RESULT_TOOL_DEF])
    if retained_results and (context_pressure or route_flags["context_intent"]):
        tools.append(CONTEXT_MANAGE_RESULTS_TOOL_DEF)
    if route_flags["has_attachments"]:
        tools.append(VIEW_ATTACHMENT_TOOL_DEF)
    if route_flags["memory_intent"]:
        if route_tokens & {"remember", "memorize"}:
            tools.append(MEMORIZE_FACT_TOOL_DEF)
        if route_tokens & {"recall", "remember", "memory"}:
            tools.append(RECALL_FACT_TOOL_DEF)
        if route_tokens & {"forget", "memory"}:
            tools.append(MANAGE_LONG_TERM_MEMORY_TOOL_DEF)
    if route_flags["broad_task"]:
        tools.append(WORKING_MEMORY_TOOL_DEF)
        if enable_delegation:
            tools.append(DELEGATE_TASK_TOOL_DEF)
    tools.extend(_run_tool_declarations_for_turn(
        runtime_run_tool_defs,
        route_flags=route_flags,
        invoked_run_context=invoked_run_context,
        history_messages=history_messages,
        execution_scope=execution_scope,
    ))
    if route_tokens & _WAKE_INTENT_TOKENS:
        tools.append(REQUEST_SELF_NUDGE_TOOL_DEF)
    if route_flags["broad_task"] and route_tokens & {"coverage", "ledger", "sources"}:
        tools.extend([SOURCE_COVERAGE_GET_TOOL_DEF, SOURCE_COVERAGE_UPSERT_TOOL_DEF])
    if execution_scope.allow_scope_expansion_request:
        tools.append(REQUEST_SCOPE_EXPANSION_TOOL_DEF)
    if has_vector_collections:
        tools.extend([
            KNOWLEDGE_QUERY_TOOL_DEF,
            LIST_KNOWLEDGE_COLLECTIONS_TOOL_DEF,
        ])
        if "rag" in route_tokens:
            tools.append(RAG_SEARCH_TOOL_DEF)

    return assemble_first_payload_context(
        base_prompt=settings.get('prompt_direct_assistant_default') or DIRECT_ASSISTANT_DEFAULT_PROMPT,
        execution_scope=execution_scope,
        settings=settings,
        user_instructions=user_instructions,
        chat_session=chat_session,
        history_messages=history_messages,
        user_message_for_llm=dict(user_message_for_llm),
        tool_declarations=tools,
        route_flags=route_flags,
        routing_metadata={
            "mode": execution_scope.mode,
            "source_classes": [],
            "selected_capability_ids": list(execution_scope.capability_ids),
            "route_flags": route_flags,
        },
        config=config,
        extra_sections=extra_sections,
        # A user-selected scope is authoritative. Declare every selected tool
        # instead of silently omitting capabilities or failing at an arbitrary
        # schema-size threshold. Provider context limits remain the only bound.
        schema_budget_tokens=None,
        strict_schema_budget=False,
        now=now,
    )


def _resolve_toolkit_plugin_prefixes(addon_manager, capability_ids):
    """Resolves canonical toolkit capability IDs to legacy plugin prefixes for runtime tool lookup."""
    if hasattr(addon_manager, 'resolve_toolkit_plugin_ids'):
        return addon_manager.resolve_toolkit_plugin_ids(capability_ids)
    return list(capability_ids or [])
