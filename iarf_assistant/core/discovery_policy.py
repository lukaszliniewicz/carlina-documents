import threading
from typing import Any

from iarf_assistant.core.execution_scope import ExecutionScope, string_list


SOURCE_CLASS_QUERY_ALIASES = {
    "email": ["email", "mail", "gmail", "imap", "smtp", "thread", "inbox"],
    "gdrive": ["gdrive", "google drive", "drive", "google docs", "docs", "sheets", "slides"],
    "local_files": ["local files", "local folder", "folder", "filesystem", "file system", "path"],
    "web": ["web", "website", "url", "search", "scrape", "page"],
    "wordpress": ["wordpress", "wp", "post", "posts", "taxonomy"],
    "documents": ["document", "documents", "pdf", "docx", "epub"],
    "images": ["image", "images", "photo", "photos", "picture", "pictures"],
}


def _source_alias_matches_query(query: str, aliases) -> bool:
    query_text = " ".join(str(query or "").lower().replace("_", " ").split())
    query_compact = query_text.replace(" ", "")
    if not query_text:
        return True
    for alias in aliases or []:
        alias_text = " ".join(str(alias or "").lower().replace("_", " ").split())
        if not alias_text:
            continue
        if alias_text in query_text or alias_text.replace(" ", "") in query_compact:
            return True
    return False


def source_coverage_is_sufficient(result: dict | None) -> bool:
    if not isinstance(result, dict):
        return False
    rows = result.get("source_classes")
    if not isinstance(rows, list) or not rows:
        return False
    return all(isinstance(row, dict) and row.get("coverage_sufficient_to_plan") for row in rows)


def source_coverage_blocks_broad_search(coverage_result: dict | None, search_arguments: dict | None) -> bool:
    if not source_coverage_is_sufficient(coverage_result):
        return False
    query = (search_arguments or {}).get("query", "")
    rows = coverage_result.get("source_classes") or []
    if not str(query or "").strip():
        return True
    for row in rows:
        if not isinstance(row, dict):
            continue
        aliases = [
            row.get("requested_source_class"),
            row.get("source_class"),
            *(row.get("aliases_considered") or []),
            *SOURCE_CLASS_QUERY_ALIASES.get(str(row.get("source_class") or ""), []),
        ]
        if _source_alias_matches_query(query, aliases):
            return True
    return False


def redundant_source_coverage_search_response(coverage_result: dict, search_arguments: dict | None) -> dict:
    rows = [row for row in coverage_result.get("source_classes", []) if isinstance(row, dict)]
    recommended_ids = []
    next_actions = []
    run_tool_name = None
    for profile in coverage_result.get("runtime_profiles") or []:
        if isinstance(profile, dict) and profile.get("id") == "durable_runs":
            run_tool_name = profile.get("primary_tool_name") or "runs_compile"
            break
    for row in rows:
        for capability_id in row.get("recommended_describe_capability_ids") or []:
            if capability_id not in recommended_ids:
                recommended_ids.append(capability_id)
        for action in row.get("next_actions") or []:
            if action not in next_actions:
                next_actions.append(action)
    return {
        "status": "blocked",
        "policy": "source_coverage_stop_rule",
        "error": "Redundant broad capability search blocked after sufficient source coverage.",
        "message": (
            "Source coverage already has enough information for the covered source classes. "
            "For durable, parallel, resumable, checkpointed, or reviewable work, call "
            f"{run_tool_name or 'runs_compile'} now if the run shape is understood. Otherwise synthesize the "
            "plan now, or call capabilities_describe with the recommended capability IDs only if exact schemas, "
            "write behavior, or script details are required before acting."
        ),
        "blocked_tool": "capabilities_search",
        "blocked_arguments": dict(search_arguments or {}),
        "source_classes": [row.get("source_class") for row in rows if row.get("source_class")],
        "recommended_run_tool_name": run_tool_name or "runs_compile",
        "recommended_describe_capability_ids": recommended_ids,
        "recommended_next_actions": next_actions[:12],
    }


class DiscoveryRoutingGuard:
    """Turn-local guard for progressive discovery rules that should not stay prompt-only."""

    def __init__(self):
        self._lock = threading.Lock()
        self._source_coverage_result = None

    def guard_meta_tool_call(self, tool_name: str, arguments: dict | None) -> dict | None:
        with self._lock:
            if tool_name == "capabilities_search" and source_coverage_blocks_broad_search(
                self._source_coverage_result,
                arguments,
            ):
                return redundant_source_coverage_search_response(self._source_coverage_result, arguments)
        return None

    def record_meta_tool_result(self, tool_name: str, result: dict | None) -> None:
        if tool_name != "capabilities_source_coverage" or not source_coverage_is_sufficient(result):
            return
        with self._lock:
            self._source_coverage_result = result


def scoped_meta_capability_block_response(
    tool_name: str,
    arguments: dict | None,
    execution_scope: Any,
) -> dict | None:
    scope = ExecutionScope.from_dict(execution_scope)
    if not scope.is_scoped:
        return None

    args = arguments if isinstance(arguments, dict) else {}
    if tool_name == "capabilities_describe":
        requested_capability_ids = string_list(args.get("capability_ids"))
        check = scope.check_child_request(capability_ids=requested_capability_ids)
        if check.allowed:
            return None
        error = check.to_error()
        error.update({
            "policy": "scoped_capability_discovery",
            "blocked_tool": tool_name,
            "error": (
                "Scoped Tools mode can only describe capabilities already selected for this turn. "
                "Ask for scope expansion if more capabilities are required."
            ),
            "current_scope": scope.to_dict(),
            "allowed_capability_ids": list(scope.capability_ids),
        })
        return error

    if tool_name == "capabilities_load_to_session":
        requested_capability_ids = string_list(args.get("capability_ids"))
        check = scope.check_child_request(capability_ids=requested_capability_ids)
        if check.allowed:
            return {
                "status": "noop",
                "policy": "scoped_capability_discovery",
                "message": (
                    "The requested capabilities are already inside the selected Scoped Tools turn. "
                    "Use the provided tool schemas directly; do not load additional capabilities."
                ),
                "capability_ids": check.capability_ids,
                "current_scope": scope.to_dict(),
            }
        error = check.to_error()
        error.update({
            "policy": "scoped_capability_discovery",
            "blocked_tool": tool_name,
            "error": (
                "Scoped Tools mode cannot load capabilities outside the user's selected scope. "
                "Use request_scope_expansion with exact missing ids instead."
            ),
            "current_scope": scope.to_dict(),
            "allowed_capability_ids": list(scope.capability_ids),
        })
        return error

    return {
        "status": "blocked",
        "policy": "scoped_capability_discovery_disabled",
        "blocked_tool": tool_name,
        "blocked_arguments": dict(args),
        "error": (
            "Scoped Tools mode disables autonomous capability discovery and broad source coverage. "
            "Use only the selected tools, skills, resources, and run scope. If something is missing, "
            "call request_scope_expansion or ask the user to switch to Autonomous mode."
        ),
        "current_scope": scope.to_dict(),
        "allowed_capability_ids": list(scope.capability_ids),
        "allowed_skill_ids": list(scope.skill_ids),
        "allowed_tool_ids": list(scope.tool_ids),
        "allowed_tool_view_ids": list(scope.tool_view_ids),
        "allowed_resource_reference_ids": list(scope.resource_reference_ids),
    }


def execute_meta_capability_with_policy(
    addon_manager: Any,
    guard: DiscoveryRoutingGuard | None,
    tool_name: str,
    arguments: dict | None,
    *,
    execution_scope: Any = None,
) -> dict:
    scoped_block = scoped_meta_capability_block_response(tool_name, arguments, execution_scope)
    if scoped_block:
        return scoped_block
    if guard is not None:
        blocked = guard.guard_meta_tool_call(tool_name, arguments)
        if blocked:
            return blocked
    result = addon_manager.execute_meta_capability_tool(tool_name, arguments or {})
    if guard is not None:
        guard.record_meta_tool_result(tool_name, result)
    return result
