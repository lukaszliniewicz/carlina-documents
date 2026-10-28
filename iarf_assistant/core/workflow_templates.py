from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


class TemplateDefinitionError(ValueError):
    """Raised when a workflow template definition is invalid."""


@dataclass
class CompiledTemplate:
    definition: dict[str, Any]
    plan: dict[str, Any]
    steps: list[dict[str, Any]]


def compile_template_definition(
    definition: dict[str, Any],
    *,
    fallback_name: str | None = None,
) -> CompiledTemplate:
    normalized = normalize_template_definition(definition, fallback_name=fallback_name)
    step_cards = []
    for index, step in enumerate(normalized["steps"], start=1):
        step_cards.append(
            {
                "key": step["key"],
                "title": step["title"],
                "type": step["type"],
                "depends_on": list(step["depends_on"]),
                "sequence": step.get("sequence") if step.get("sequence") is not None else index,
                "assigned_model_role": step.get("assigned_model_role"),
                "capability_ids": list(step.get("capability_ids") or []),
                "skill_ids": list(step.get("skill_ids") or []),
                "tool_ids": list(step.get("tool_ids") or []),
            }
        )
    plan = {
        "template": {
            "version": normalized["version"],
            "name": normalized["name"],
            "description": normalized.get("description", ""),
        },
        "steps": step_cards,
    }
    return CompiledTemplate(definition=normalized, plan=plan, steps=normalized["steps"])


def normalize_template_definition(
    definition: dict[str, Any],
    *,
    fallback_name: str | None = None,
) -> dict[str, Any]:
    if not isinstance(definition, dict):
        raise TemplateDefinitionError("Template definition must be an object.")

    raw_steps = definition.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise TemplateDefinitionError("Template definition requires a non-empty steps array.")

    raw_name = definition.get("name") or definition.get("title") or fallback_name or "Workflow Template"
    name = _normalize_text(raw_name, "name")
    description = str(definition.get("description") or "").strip()
    version = _normalize_int(definition.get("version"), default=1, minimum=1, field_name="version")

    normalized_steps: list[dict[str, Any]] = []
    keys_seen: set[str] = set()
    for index, raw_step in enumerate(raw_steps, start=1):
        if not isinstance(raw_step, dict):
            raise TemplateDefinitionError(f"Step #{index} must be an object.")
        raw_key = raw_step.get("key") or raw_step.get("id") or f"step_{index}"
        key = _normalize_key(raw_key, field_name=f"steps[{index}].key")
        if key in keys_seen:
            raise TemplateDefinitionError(f"Duplicate step key '{key}'.")
        keys_seen.add(key)

        step_type = _normalize_text(raw_step.get("type") or raw_step.get("step_type") or "task", f"steps[{index}].type")
        title = _normalize_text(raw_step.get("title") or raw_step.get("name") or key.replace("_", " "), f"steps[{index}].title")

        depends_on = []
        for dep in _as_string_list(raw_step.get("depends_on") or raw_step.get("depends_on_step_ids") or raw_step.get("requires")):
            dep_key = _normalize_key(dep, field_name=f"steps[{index}].depends_on")
            if dep_key == key:
                raise TemplateDefinitionError(f"Step '{key}' cannot depend on itself.")
            if dep_key not in depends_on:
                depends_on.append(dep_key)

        metadata = _json_obj(raw_step.get("metadata"))
        completion_requirements = raw_step.get("completion_requirements")
        if completion_requirements is not None:
            if not isinstance(completion_requirements, dict):
                raise TemplateDefinitionError(
                    f"Step '{key}' completion_requirements must be an object when provided."
                )
            metadata = {**metadata, "completion_requirements": copy.deepcopy(completion_requirements)}

        retry_policy = raw_step.get("retry_policy")
        if retry_policy is not None:
            if not isinstance(retry_policy, dict):
                raise TemplateDefinitionError(f"Step '{key}' retry_policy must be an object when provided.")
            metadata = {**metadata, "retry_policy": copy.deepcopy(retry_policy)}

        normalized_steps.append(
            {
                "key": key,
                "type": step_type,
                "title": title,
                "sequence": _optional_int(raw_step.get("sequence"), minimum=1, field_name=f"steps[{index}].sequence"),
                "depends_on": depends_on,
                "assigned_model_role": _optional_string(raw_step.get("assigned_model_role") or raw_step.get("model_role")),
                "instructions": _optional_string(raw_step.get("instructions")),
                "capability_ids": _as_string_list(raw_step.get("capability_ids")),
                "skill_ids": _as_string_list(raw_step.get("skill_ids")),
                "tool_ids": _as_string_list(raw_step.get("tool_ids")),
                "provider_requirements": _as_string_list(raw_step.get("provider_requirements")),
                "input_schema": _json_obj(raw_step.get("input_schema")),
                "output_schema": _json_obj(raw_step.get("output_schema")),
                "input": _json_obj(raw_step.get("input") or raw_step.get("input_payload")),
                "metadata": metadata,
            }
        )

    known_keys = {step["key"] for step in normalized_steps}
    for step in normalized_steps:
        for dep in step["depends_on"]:
            if dep not in known_keys:
                raise TemplateDefinitionError(
                    f"Step '{step['key']}' depends on unknown step '{dep}'."
                )
    _validate_acyclic_dependencies(normalized_steps)

    return {
        "version": version,
        "name": name,
        "description": description,
        "steps": normalized_steps,
    }


def executable_definition_from_addon_workflow(workflow_record: Any) -> dict[str, Any] | None:
    metadata = getattr(workflow_record, "metadata", None)
    if not isinstance(metadata, dict):
        return None

    for key in ("definition", "workflow_definition", "execution_definition"):
        candidate = metadata.get(key)
        if isinstance(candidate, dict):
            return copy.deepcopy(candidate)

    if isinstance(metadata.get("steps"), list):
        candidate = {
            "version": metadata.get("version") or 1,
            "name": metadata.get("name") or getattr(workflow_record, "name", "Workflow Template"),
            "description": metadata.get("description") or getattr(workflow_record, "description", ""),
            "steps": metadata.get("steps"),
        }
        return copy.deepcopy(candidate)
    return None


def make_template_storage(
    definition: dict[str, Any],
    *,
    created_by_user_id: int | None,
    change_summary: str | None = None,
) -> dict[str, Any]:
    compiled = compile_template_definition(definition)
    timestamp = datetime.now(timezone.utc).isoformat()
    entry = {
        "version": 1,
        "definition": compiled.definition,
        "created_at": timestamp,
        "created_by_user_id": created_by_user_id,
        "change_summary": (str(change_summary).strip() or "Initial version"),
    }
    return {
        "schema_version": 1,
        "active_version": 1,
        "versions": [entry],
    }


def read_template_storage(definition_json: Any) -> dict[str, Any]:
    payload = definition_json if isinstance(definition_json, dict) else {}
    versions = payload.get("versions") if isinstance(payload.get("versions"), list) else None
    active_version = payload.get("active_version")
    if versions is None:
        # Backward compatibility: plain definition object stored directly.
        compiled = compile_template_definition(payload)
        now = datetime.now(timezone.utc).isoformat()
        return {
            "schema_version": 1,
            "active_version": 1,
            "versions": [
                {
                    "version": 1,
                    "definition": compiled.definition,
                    "created_at": now,
                    "created_by_user_id": None,
                    "change_summary": "Imported legacy definition",
                }
            ],
        }

    normalized_versions = []
    seen = set()
    for index, item in enumerate(versions, start=1):
        if not isinstance(item, dict):
            continue
        version = _normalize_int(item.get("version"), default=index, minimum=1, field_name=f"versions[{index}].version")
        if version in seen:
            continue
        seen.add(version)
        definition = item.get("definition")
        if not isinstance(definition, dict):
            continue
        compiled = compile_template_definition(definition)
        normalized_versions.append(
            {
                "version": version,
                "definition": compiled.definition,
                "created_at": str(item.get("created_at") or datetime.now(timezone.utc).isoformat()),
                "created_by_user_id": item.get("created_by_user_id"),
                "change_summary": str(item.get("change_summary") or "").strip() or None,
            }
        )
    if not normalized_versions:
        raise TemplateDefinitionError("Workflow template has no valid definition versions.")

    normalized_versions.sort(key=lambda item: int(item["version"]))
    version_map = {int(item["version"]): item for item in normalized_versions}
    active = _normalize_int(active_version, default=normalized_versions[-1]["version"], minimum=1, field_name="active_version")
    if active not in version_map:
        active = normalized_versions[-1]["version"]
    return {
        "schema_version": 1,
        "active_version": active,
        "versions": normalized_versions,
    }


def append_template_version(
    storage: dict[str, Any],
    definition: dict[str, Any],
    *,
    created_by_user_id: int | None,
    change_summary: str | None = None,
) -> dict[str, Any]:
    normalized = read_template_storage(storage)
    compiled = compile_template_definition(definition)
    next_version = max(int(item["version"]) for item in normalized["versions"]) + 1
    updated = copy.deepcopy(normalized)
    updated["versions"].append(
        {
            "version": next_version,
            "definition": compiled.definition,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "created_by_user_id": created_by_user_id,
            "change_summary": str(change_summary or "").strip() or f"Version {next_version}",
        }
    )
    updated["active_version"] = next_version
    return updated


def set_active_template_version(storage: dict[str, Any], version: int) -> dict[str, Any]:
    normalized = read_template_storage(storage)
    candidate = _normalize_int(version, default=-1, minimum=1, field_name="active_version")
    if candidate not in {int(item["version"]) for item in normalized["versions"]}:
        raise TemplateDefinitionError(f"Template version {candidate} does not exist.")
    updated = copy.deepcopy(normalized)
    updated["active_version"] = candidate
    return updated


def active_template_definition(storage: dict[str, Any]) -> dict[str, Any]:
    normalized = read_template_storage(storage)
    active = int(normalized["active_version"])
    for item in normalized["versions"]:
        if int(item["version"]) == active:
            return copy.deepcopy(item["definition"])
    raise TemplateDefinitionError("Active template version is missing.")


def _normalize_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise TemplateDefinitionError(f"{field_name} is required.")
    return text


def _normalize_key(value: Any, *, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise TemplateDefinitionError(f"{field_name} is required.")
    normalized = "".join(char if (char.isalnum() or char in {"_", "-", "."}) else "_" for char in text.lower())
    normalized = "_".join(part for part in normalized.replace("-", "_").split("_") if part)
    if not normalized:
        raise TemplateDefinitionError(f"{field_name} is invalid.")
    return normalized[:128]


def _normalize_int(value: Any, *, default: int, minimum: int, field_name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if parsed < minimum:
        raise TemplateDefinitionError(f"{field_name} must be >= {minimum}.")
    return parsed


def _optional_int(value: Any, *, minimum: int, field_name: str) -> int | None:
    if value is None:
        return None
    return _normalize_int(value, default=minimum, minimum=minimum, field_name=field_name)


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (tuple, set)):
        value = list(value)
    if not isinstance(value, list):
        value = [value]
    normalized = []
    seen = set()
    for item in value:
        text = str(item or "").strip()
        if not text:
            continue
        if text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized


def _json_obj(value: Any) -> dict[str, Any]:
    return copy.deepcopy(value) if isinstance(value, dict) else {}


def _validate_acyclic_dependencies(steps: list[dict[str, Any]]) -> None:
    """Reject dependency cycles before they can create permanently blocked runs."""

    dependencies = {
        str(step["key"]): [str(value) for value in step["depends_on"]]
        for step in steps
    }
    complete: set[str] = set()
    visiting: set[str] = set()
    path: list[str] = []

    def visit(step_key: str) -> None:
        if step_key in complete:
            return
        if step_key in visiting:
            cycle_start = path.index(step_key)
            cycle = [*path[cycle_start:], step_key]
            raise TemplateDefinitionError(
                "Workflow dependency cycle detected: "
                + " -> ".join(cycle)
                + "."
            )

        visiting.add(step_key)
        path.append(step_key)
        for dependency_key in dependencies.get(step_key, []):
            visit(dependency_key)
        path.pop()
        visiting.remove(step_key)
        complete.add(step_key)

    for key in dependencies:
        visit(key)
