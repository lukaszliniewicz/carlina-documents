from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


SCOPE_SCHEMA_VERSION = 1
AUTONOMOUS_DISCOVERY = "autonomous_discovery"
SCOPED = "scoped"


def string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    result = []
    seen = set()
    for item in value or []:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def normalize_scope_mode(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"direct", "scoped", "scope", "provisioned", "provisioned_mode", "scoped_tools"}:
        return SCOPED
    return AUTONOMOUS_DISCOVERY


def legacy_assistant_mode(value: Any) -> str:
    return "direct" if normalize_scope_mode(value) == SCOPED else "default"


@dataclass(frozen=True)
class ScopeCheck:
    allowed: bool
    capability_ids: list[str] = field(default_factory=list)
    skill_ids: list[str] = field(default_factory=list)
    workflow_ids: list[str] = field(default_factory=list)
    tool_ids: list[str] = field(default_factory=list)
    tool_view_ids: list[str] = field(default_factory=list)
    resource_reference_ids: list[str] = field(default_factory=list)
    blocked_capability_ids: list[str] = field(default_factory=list)
    blocked_skill_ids: list[str] = field(default_factory=list)
    blocked_workflow_ids: list[str] = field(default_factory=list)
    blocked_tool_ids: list[str] = field(default_factory=list)
    blocked_tool_view_ids: list[str] = field(default_factory=list)
    blocked_resource_reference_ids: list[str] = field(default_factory=list)

    def to_error(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": "blocked",
            "error": "Execution scope does not allow one or more requested capabilities, tools, skills, workflows, or resources.",
        }
        for key in (
            "blocked_capability_ids",
            "blocked_skill_ids",
            "blocked_workflow_ids",
            "blocked_tool_ids",
            "blocked_tool_view_ids",
            "blocked_resource_reference_ids",
        ):
            values = getattr(self, key)
            if values:
                payload[key] = values
        return payload


@dataclass(frozen=True)
class ExecutionScope:
    mode: str = AUTONOMOUS_DISCOVERY
    capability_ids: list[str] = field(default_factory=list)
    skill_ids: list[str] = field(default_factory=list)
    workflow_ids: list[str] = field(default_factory=list)
    tool_ids: list[str] = field(default_factory=list)
    tool_view_ids: list[str] = field(default_factory=list)
    resource_reference_ids: list[str] = field(default_factory=list)
    runtime_profile_ids: list[str] = field(default_factory=list)
    model_profile_ids: list[str] = field(default_factory=list)
    allow_discovery: bool = True
    allow_scope_expansion_request: bool = True

    def __post_init__(self):
        normalized_mode = normalize_scope_mode(self.mode)
        object.__setattr__(self, "mode", normalized_mode)
        for field_name in (
            "capability_ids",
            "skill_ids",
            "workflow_ids",
            "tool_ids",
            "tool_view_ids",
            "resource_reference_ids",
            "runtime_profile_ids",
            "model_profile_ids",
        ):
            object.__setattr__(self, field_name, string_list(getattr(self, field_name)))
        if normalized_mode == SCOPED:
            object.__setattr__(self, "allow_discovery", False)

    @property
    def is_scoped(self) -> bool:
        return self.mode == SCOPED

    @property
    def is_autonomous(self) -> bool:
        return self.mode == AUTONOMOUS_DISCOVERY

    @classmethod
    def autonomous(cls, *, capability_ids: Any = None) -> "ExecutionScope":
        return cls(
            mode=AUTONOMOUS_DISCOVERY,
            capability_ids=string_list(capability_ids),
            allow_discovery=True,
            allow_scope_expansion_request=True,
        )

    @classmethod
    def from_dict(cls, value: Any) -> "ExecutionScope":
        if isinstance(value, ExecutionScope):
            return value
        payload = value if isinstance(value, dict) else {}
        mode = payload.get("mode") or payload.get("assistant_mode")
        if payload.get("allow_discovery") is False:
            mode = mode or SCOPED
        return cls(
            mode=mode,
            capability_ids=payload.get("capability_ids"),
            skill_ids=payload.get("skill_ids"),
            workflow_ids=payload.get("workflow_ids"),
            tool_ids=payload.get("tool_ids"),
            tool_view_ids=payload.get("tool_view_ids"),
            resource_reference_ids=payload.get("resource_reference_ids"),
            runtime_profile_ids=payload.get("runtime_profile_ids"),
            model_profile_ids=payload.get("model_profile_ids"),
            allow_discovery=payload.get("allow_discovery", normalize_scope_mode(mode) != SCOPED),
            allow_scope_expansion_request=payload.get("allow_scope_expansion_request", True) is not False,
        )

    @classmethod
    def from_assistant_selection(
        cls,
        selection: dict[str, Any] | None,
        *,
        active_capability_ids: Any = None,
    ) -> "ExecutionScope":
        payload = selection if isinstance(selection, dict) else {}
        mode = normalize_scope_mode(payload.get("mode"))
        capability_ids = string_list(payload.get("capability_ids"))
        if mode == AUTONOMOUS_DISCOVERY:
            capability_ids = merge_unique(capability_ids, active_capability_ids)
        return cls(
            mode=mode,
            capability_ids=capability_ids,
            skill_ids=payload.get("skill_ids"),
            workflow_ids=payload.get("workflow_ids"),
            tool_ids=payload.get("tool_ids"),
            tool_view_ids=payload.get("tool_view_ids"),
            resource_reference_ids=payload.get("resource_reference_ids"),
            runtime_profile_ids=payload.get("runtime_profile_ids"),
            model_profile_ids=payload.get("model_profile_ids"),
            allow_discovery=payload.get("allow_discovery", mode == AUTONOMOUS_DISCOVERY),
            allow_scope_expansion_request=payload.get("allow_scope_expansion_request", True) is not False,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCOPE_SCHEMA_VERSION,
            "mode": self.mode,
            "assistant_mode": legacy_assistant_mode(self.mode),
            "capability_ids": list(self.capability_ids),
            "skill_ids": list(self.skill_ids),
            "workflow_ids": list(self.workflow_ids),
            "tool_ids": list(self.tool_ids),
            "tool_view_ids": list(self.tool_view_ids),
            "resource_reference_ids": list(self.resource_reference_ids),
            "runtime_profile_ids": list(self.runtime_profile_ids),
            "model_profile_ids": list(self.model_profile_ids),
            "allow_discovery": bool(self.allow_discovery),
            "allow_scope_expansion_request": bool(self.allow_scope_expansion_request),
        }

    def child_metadata(self) -> dict[str, Any]:
        return {"execution_scope": self.to_dict()}

    def with_capabilities(self, capability_ids: Any) -> "ExecutionScope":
        return ExecutionScope(
            mode=self.mode,
            capability_ids=merge_unique(self.capability_ids, capability_ids),
            skill_ids=self.skill_ids,
            workflow_ids=self.workflow_ids,
            tool_ids=self.tool_ids,
            tool_view_ids=self.tool_view_ids,
            resource_reference_ids=self.resource_reference_ids,
            runtime_profile_ids=self.runtime_profile_ids,
            model_profile_ids=self.model_profile_ids,
            allow_discovery=self.allow_discovery,
            allow_scope_expansion_request=self.allow_scope_expansion_request,
        )

    def check_child_request(
        self,
        *,
        capability_ids: Any = None,
        skill_ids: Any = None,
        workflow_ids: Any = None,
        tool_ids: Any = None,
        tool_view_ids: Any = None,
        resource_reference_ids: Any = None,
    ) -> ScopeCheck:
        requested_capabilities = string_list(capability_ids)
        requested_skills = string_list(skill_ids)
        requested_workflows = string_list(workflow_ids)
        requested_tools = string_list(tool_ids)
        requested_tool_views = string_list(tool_view_ids)
        requested_resources = string_list(resource_reference_ids)

        if self.is_autonomous:
            return ScopeCheck(
                allowed=True,
                capability_ids=requested_capabilities,
                skill_ids=requested_skills,
                workflow_ids=requested_workflows,
                tool_ids=requested_tools,
                tool_view_ids=requested_tool_views,
                resource_reference_ids=requested_resources,
            )

        permitted_capabilities, blocked_capabilities = split_by_scope(requested_capabilities, self.capability_ids)
        permitted_skills, blocked_skills = split_by_scope(requested_skills, self.skill_ids)
        permitted_workflows, blocked_workflows = split_by_scope(requested_workflows, self.workflow_ids)
        permitted_tools, blocked_tools = split_by_scope(requested_tools, self.tool_ids)
        permitted_tool_views, blocked_tool_views = split_by_scope(requested_tool_views, self.tool_view_ids)
        permitted_resources, blocked_resources = split_by_scope(requested_resources, self.resource_reference_ids)
        blocked = any((
            blocked_capabilities,
            blocked_skills,
            blocked_workflows,
            blocked_tools,
            blocked_tool_views,
            blocked_resources,
        ))
        return ScopeCheck(
            allowed=not blocked,
            capability_ids=permitted_capabilities,
            skill_ids=permitted_skills,
            workflow_ids=permitted_workflows,
            tool_ids=permitted_tools,
            tool_view_ids=permitted_tool_views,
            resource_reference_ids=permitted_resources,
            blocked_capability_ids=blocked_capabilities,
            blocked_skill_ids=blocked_skills,
            blocked_workflow_ids=blocked_workflows,
            blocked_tool_ids=blocked_tools,
            blocked_tool_view_ids=blocked_tool_views,
            blocked_resource_reference_ids=blocked_resources,
        )


def merge_unique(first: Any, second: Any) -> list[str]:
    result = string_list(first)
    seen = set(result)
    for item in string_list(second):
        if item not in seen:
            result.append(item)
            seen.add(item)
    return result


def split_by_scope(requested: Any, allowed: Any) -> tuple[list[str], list[str]]:
    allowed_set = set(string_list(allowed))
    permitted = []
    blocked = []
    for item in string_list(requested):
        if item in allowed_set:
            permitted.append(item)
        else:
            blocked.append(item)
    return permitted, blocked

