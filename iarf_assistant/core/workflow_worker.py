import logging
import copy
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from jsonschema import Draft202012Validator, SchemaError
from iarf_assistant.extensions import db
from iarf_assistant.models import (
    RuntimeArtifact,
    WorkflowRun,
    WorkflowStep,
)
from iarf_assistant.core.model_router import ModelRouter
from iarf_assistant.llm_service import LLMService
from iarf_assistant.core.agent_loop import (
    build_finalization_request_message,
    tool_call_function_name,
)
from iarf_assistant.core.agent_events import (
    append_agent_event_to_metadata_attr,
)
from carlina.core.tool_risk import tool_is_read_only


__all__ = ["LLMService", "ModelRouter", "handle_execute_workflow_step"]

TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled"}
TERMINAL_STEP_STATUSES = {"completed", "failed", "cancelled", "skipped"}
NON_RETRYABLE_ERROR_MARKERS = (
    "output did not match the required json schema",
    "output_schema is invalid",
    "declared an output_schema but the model returned no content",
    "workflow step completed without required side effects",
    "is not allowed inside a workflow-step context",
    "access denied",
    "invalid workflow step retry policy",
)
TRANSIENT_ERROR_MARKERS = (
    "timeout",
    "timed out",
    "temporar",
    "rate limit",
    "too many requests",
    "connection",
    "network",
    "service unavailable",
    "unavailable",
    "overloaded",
    "429",
    "502",
    "503",
    "504",
    "try again",
)
STATEFUL_WORKER_TOOL_NAMES = {
    "update_shared_memory",
    "read_shared_memory",
    "runtime_read_data_ref",
    "request_budget_increase",
    "send_user_alert",
    "memorize_fact",
    "recall_fact",
    "manage_long_term_memory",
}


@dataclass
class StepRetryPolicy:
    enabled: bool = True
    max_attempts: int = 3
    backoff_seconds: float = 10.0
    backoff_multiplier: float = 2.0
    max_backoff_seconds: float = 120.0
    retry_on: str = "transient"


def _worker_tool_calls_parallel_safe(
    calls,
    *,
    tool_record_resolver,
    parallel_tool_call_limit: int,
) -> bool:
    if len(calls or []) < 2 or int(parallel_tool_call_limit or 0) <= 1:
        return False
    for call in calls:
        name = tool_call_function_name(call)
        if not name or name in STATEFUL_WORKER_TOOL_NAMES or name.startswith("runs_"):
            return False
        record = tool_record_resolver(name) if callable(tool_record_resolver) else None
        if record is None or not tool_is_read_only(record):
            return False
    return True


def handle_execute_workflow_step(payload: dict, task_queue, job_id: str):
    """Compatibility facade over the decomposed workflow-step executor."""
    from iarf_assistant.core.workflow_steps import execute_workflow_step

    return execute_workflow_step(payload, task_queue, job_id)


def _resolve_agent_profile(addon_manager: Any, assigned_model_role: str | None):
    if not assigned_model_role or not hasattr(addon_manager, 'registries'):
        return None
    return addon_manager.registries.agent_profiles.get(assigned_model_role)


def _retry_state_metadata(step: WorkflowStep) -> tuple[dict[str, Any], dict[str, Any]]:
    metadata = step.metadata_json if isinstance(step.metadata_json, dict) else {}
    retry_state = metadata.get("retry_state") if isinstance(metadata.get("retry_state"), dict) else {}
    if metadata is step.metadata_json:
        metadata = copy.deepcopy(metadata)
    return metadata, retry_state


def _retry_policy(step: WorkflowStep, settings: dict[str, Any] | None = None) -> StepRetryPolicy:
    settings = settings or {}
    metadata = step.metadata_json if isinstance(step.metadata_json, dict) else {}
    payload = metadata.get("retry_policy") if isinstance(metadata.get("retry_policy"), dict) else {}

    enabled = _to_bool(payload.get("enabled"), _to_bool(settings.get("workflow_step_retries_enabled"), True))
    max_attempts = _bounded_int(payload.get("max_attempts"), settings.get("workflow_step_max_attempts"), default=3, minimum=1, maximum=10)
    initial_backoff = _bounded_float(
        payload.get("initial_backoff_seconds", payload.get("backoff_seconds")),
        settings.get("workflow_step_retry_backoff_seconds"),
        default=10.0,
        minimum=0.0,
        maximum=3600.0,
    )
    backoff_multiplier = _bounded_float(
        payload.get("backoff_multiplier"),
        settings.get("workflow_step_retry_backoff_multiplier"),
        default=2.0,
        minimum=1.0,
        maximum=10.0,
    )
    max_backoff = _bounded_float(
        payload.get("max_backoff_seconds"),
        settings.get("workflow_step_retry_max_backoff_seconds"),
        default=120.0,
        minimum=0.0,
        maximum=86400.0,
    )
    retry_on = str(payload.get("retry_on") or settings.get("workflow_step_retry_on") or "transient").strip().lower()
    if retry_on not in {"transient", "all", "none"}:
        raise ValueError("Invalid workflow step retry policy: retry_on must be one of transient, all, none.")

    return StepRetryPolicy(
        enabled=enabled,
        max_attempts=max_attempts,
        backoff_seconds=initial_backoff,
        backoff_multiplier=backoff_multiplier,
        max_backoff_seconds=max_backoff,
        retry_on=retry_on,
    )


def _retry_delay_seconds(policy: StepRetryPolicy, attempt_number: int) -> float:
    base = max(0.0, float(policy.backoff_seconds))
    multiplier = max(1.0, float(policy.backoff_multiplier))
    exponent = max(0, int(attempt_number) - 1)
    delay = base * (multiplier ** exponent)
    capped = min(delay, max(0.0, float(policy.max_backoff_seconds)))
    return float(capped)


def _is_retryable_error(error: Exception, retry_on: str) -> bool:
    if retry_on == "none":
        return False
    if retry_on == "all":
        return True

    message = str(error or "").lower()
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    if any(marker in message for marker in NON_RETRYABLE_ERROR_MARKERS):
        return False
    return any(marker in message for marker in TRANSIENT_ERROR_MARKERS)


def _maybe_schedule_retry(
    step: WorkflowStep,
    *,
    error: Exception,
    job_id: str,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    try:
        policy = _retry_policy(step, settings)
    except Exception as policy_error:
        logging.warning("Workflow step %s has invalid retry policy: %s", step.id, policy_error)
        return None
    if not policy.enabled:
        return None
    if not _is_retryable_error(error, policy.retry_on):
        return None

    metadata, retry_state = _retry_state_metadata(step)
    attempts = _bounded_int((retry_state or {}).get("attempts"), None, default=0, minimum=0, maximum=10_000) + 1
    if attempts >= policy.max_attempts:
        retry_state = {
            **retry_state,
            "attempts": attempts,
            "max_attempts": policy.max_attempts,
            "last_error": str(error),
            "last_job_id": str(job_id),
            "exhausted": True,
        }
        metadata["retry_state"] = retry_state
        step.metadata_json = metadata
        return None

    delay_seconds = _retry_delay_seconds(policy, attempts)
    next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
    retry_state = {
        **retry_state,
        "attempts": attempts,
        "max_attempts": policy.max_attempts,
        "last_error": str(error),
        "last_job_id": str(job_id),
        "next_retry_at": next_retry_at.isoformat(),
        "delay_seconds": delay_seconds,
    }
    metadata["retry_state"] = retry_state
    step.metadata_json = metadata
    step.status = "planned"
    step.error = (
        f"{error} (retry scheduled in {int(round(delay_seconds))}s; "
        f"attempt {attempts}/{policy.max_attempts})"
    )
    db.session.commit()
    logging.warning(
        "Workflow step %s scheduled for retry %s/%s in %.1fs after error: %s",
        step.id,
        attempts,
        policy.max_attempts,
        delay_seconds,
        error,
    )
    return {
        "status": "retry_scheduled",
        "attempt": attempts,
        "max_attempts": policy.max_attempts,
        "delay_seconds": delay_seconds,
        "next_retry_at": next_retry_at.isoformat(),
        "reason": str(error),
    }


def _clear_retry_state(step: WorkflowStep) -> None:
    metadata = step.metadata_json if isinstance(step.metadata_json, dict) else {}
    if not isinstance(metadata, dict) or "retry_state" not in metadata:
        return
    updated = copy.deepcopy(metadata)
    updated.pop("retry_state", None)
    step.metadata_json = updated


def _to_bool(value: Any, default: bool) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _bounded_int(
    primary: Any,
    fallback: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = primary if primary is not None else fallback
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(minimum, min(parsed, maximum))


def _bounded_float(
    primary: Any,
    fallback: Any,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    value = primary if primary is not None else fallback
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = float(default)
    return max(minimum, min(parsed, maximum))


@dataclass
class StepExecutionRequirements:
    shared_memory_keys: list[str]
    artifact_filenames: list[str]
    requires_artifact: bool = False
    checkpoint_resolution_ids: list[str] = field(default_factory=list)

    def has_requirements(self) -> bool:
        return bool(self.shared_memory_keys or self.artifact_filenames or self.requires_artifact or self.checkpoint_resolution_ids)


def _collect_execution_requirements(step: WorkflowStep) -> StepExecutionRequirements:
    explicit = _explicit_execution_requirements(step.metadata_json)
    inferred = _infer_execution_requirements(step.instructions or "")
    return StepExecutionRequirements(
        shared_memory_keys=_unique([*explicit.shared_memory_keys, *inferred.shared_memory_keys]),
        artifact_filenames=_unique([*explicit.artifact_filenames, *inferred.artifact_filenames]),
        requires_artifact=bool(explicit.requires_artifact or inferred.requires_artifact),
        checkpoint_resolution_ids=_unique(explicit.checkpoint_resolution_ids),
    )


def _explicit_execution_requirements(metadata: dict[str, Any] | None) -> StepExecutionRequirements:
    payload = metadata if isinstance(metadata, dict) else {}
    requirements = payload.get("completion_requirements") or payload.get("execution_requirements")
    if not isinstance(requirements, dict):
        return StepExecutionRequirements(shared_memory_keys=[], artifact_filenames=[])

    memory_keys = [
        str(item).strip()
        for item in (
            requirements.get("shared_memory_keys")
            or requirements.get("required_memory_keys")
            or requirements.get("memory_keys")
            or []
        )
        if str(item).strip()
    ]
    artifact_filenames = []
    for item in (
        requirements.get("artifact_filenames")
        or requirements.get("required_artifact_filenames")
        or []
    ):
        filename = _sanitize_filename(item)
        if filename:
            artifact_filenames.append(filename)
    requires_artifact = bool(requirements.get("requires_artifact"))
    checkpoint_resolution_ids = _string_values(
        requirements.get("requires_checkpoint_resolution")
        or requirements.get("checkpoint_resolution_ids")
        or requirements.get("required_checkpoint_ids")
        or []
    )
    return StepExecutionRequirements(
        shared_memory_keys=_unique(memory_keys),
        artifact_filenames=_unique(artifact_filenames),
        requires_artifact=requires_artifact,
        checkpoint_resolution_ids=_unique(checkpoint_resolution_ids),
    )


def _infer_execution_requirements(instructions: str) -> StepExecutionRequirements:
    shared_memory_keys: list[str] = []
    artifact_filenames: list[str] = []
    requires_artifact = False
    artifact_requested = False
    sentences = _instruction_sentences(instructions)

    memory_write_patterns = [
        re.compile(
            r"(?:save|store|write|persist|set|update)[^\n.]{0,240}?shared memory[^\n.]{0,120}?\bkey\b\s*['\"]([^'\"]+)['\"]",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?:save|store|write|persist|set|update)[^\n.]{0,240}?shared memory\s*['\"]([^'\"]+)['\"]",
            re.IGNORECASE,
        ),
    ]
    artifact_negation_pattern = re.compile(r"\b(?:do not|don't|dont|without|no)\b[^\n.]{0,100}\bartifact\b", re.IGNORECASE)
    artifact_create_pattern = re.compile(r"\b(create|generate|produce|write|save|attach|add)\b", re.IGNORECASE)
    filename_pattern = re.compile(r"\bfilename\b\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
    quoted_file_pattern = re.compile(r"['\"]([^'\"]+\.[A-Za-z0-9]{1,12})['\"]")

    for sentence in sentences:
        lower_sentence = sentence.lower()
        if "shared memory" in lower_sentence:
            for pattern in memory_write_patterns:
                for match in pattern.finditer(sentence):
                    key = str(match.group(1) or "").strip()
                    if key:
                        shared_memory_keys.append(key)

        mentions_artifact = "artifact" in lower_sentence
        negated_artifact = bool(artifact_negation_pattern.search(sentence))
        if mentions_artifact and not negated_artifact and artifact_create_pattern.search(sentence):
            artifact_requested = True
            requires_artifact = True

        if not negated_artifact and (mentions_artifact or artifact_requested):
            for match in filename_pattern.finditer(sentence):
                filename = _sanitize_filename(match.group(1))
                if filename:
                    artifact_filenames.append(filename)
            for match in quoted_file_pattern.finditer(sentence):
                filename = _sanitize_filename(match.group(1))
                if filename:
                    artifact_filenames.append(filename)

    return StepExecutionRequirements(
        shared_memory_keys=_unique(shared_memory_keys),
        artifact_filenames=_unique(artifact_filenames),
        requires_artifact=requires_artifact or bool(artifact_filenames),
    )


def _instruction_sentences(instructions: str) -> list[str]:
    if not instructions:
        return []
    chunks = re.split(r"(?<=[.!?])\s+|\n+", str(instructions))
    return [chunk.strip() for chunk in chunks if chunk and chunk.strip()]


def _sanitize_filename(raw_value: Any) -> str:
    value = str(raw_value or "").strip().strip("'\"")
    if not value:
        return ""
    return Path(value).name


def _missing_execution_requirements(run: WorkflowRun, step: WorkflowStep, requirements: StepExecutionRequirements) -> list[str]:
    if not requirements.has_requirements():
        return []
    db.session.refresh(run)
    db.session.refresh(step)
    memory = _workflow_memory(run)
    artifacts = _step_artifacts(step)
    artifact_names = _artifact_filename_set(artifacts)
    resolved_checkpoint_ids = _resolved_runtime_checkpoint_ids(run)
    return _missing_execution_requirements_from_state(
        memory_keys_present={str(key) for key in memory.keys()},
        artifact_names=artifact_names,
        artifact_count=len(artifacts),
        resolved_checkpoint_ids=resolved_checkpoint_ids,
        requirements=requirements,
    )


def _missing_execution_requirements_from_state(
    *,
    memory_keys_present: set[str],
    artifact_names: set[str],
    artifact_count: int,
    requirements: StepExecutionRequirements,
    resolved_checkpoint_ids: set[str] | None = None,
) -> list[str]:
    missing = []
    resolved_checkpoint_ids = resolved_checkpoint_ids or set()
    for key in requirements.shared_memory_keys:
        if str(key) not in memory_keys_present:
            missing.append(f"shared memory key '{key}'")

    if requirements.requires_artifact and artifact_count < 1:
        missing.append("at least one artifact")

    for filename in requirements.artifact_filenames:
        if str(filename).lower() not in artifact_names:
            missing.append(f"artifact filename '{filename}'")
    for checkpoint_id in requirements.checkpoint_resolution_ids:
        if str(checkpoint_id) not in resolved_checkpoint_ids:
            missing.append(f"runtime checkpoint '{checkpoint_id}' resolved")
    return missing


def _resolved_runtime_checkpoint_ids(run: WorkflowRun) -> set[str]:
    metadata = run.metadata_json if isinstance(run.metadata_json, dict) else {}
    checkpoints = metadata.get("runtime_checkpoints") if isinstance(metadata.get("runtime_checkpoints"), list) else []
    return {
        str(checkpoint.get("id"))
        for checkpoint in checkpoints
        if isinstance(checkpoint, dict)
        and checkpoint.get("id")
        and str(checkpoint.get("status") or "").lower() == "resolved"
    }


def _step_artifacts(step: WorkflowStep) -> list[RuntimeArtifact]:
    artifacts = []
    for artifact_id in _unique([str(item) for item in (step.artifact_ids or []) if item]):
        artifact = db.session.get(RuntimeArtifact, artifact_id)
        if artifact:
            artifacts.append(artifact)
    return artifacts


def _artifact_filename_set(artifacts: list[RuntimeArtifact]) -> set[str]:
    names = set()
    for artifact in artifacts:
        if artifact.path:
            names.add(Path(str(artifact.path)).name.lower())
        if artifact.uri:
            raw_uri = str(artifact.uri)
            normalized_uri = raw_uri.split("://", 1)[-1] if "://" in raw_uri else raw_uri
            names.add(Path(normalized_uri).name.lower())
    return {name for name in names if name}


def _missing_requirements_prompt(missing_requirements: list[str]) -> str:
    checklist = ", ".join(missing_requirements)
    return (
        "Execution is incomplete. Before you return final JSON, you MUST satisfy these requirements via tool calls: "
        f"{checklist}. "
        "Call the necessary tools now, then return the final JSON output."
    )


def _workflow_finalization_request_message(
    step: WorkflowStep,
    *,
    reason: str,
    max_tool_calls: int | None = None,
) -> dict[str, str]:
    message = build_finalization_request_message(
        reason=reason,
        max_tool_calls=max_tool_calls,
    )
    additions = [
        "This is a workflow step finalization call. Tools are disabled. Return the step output now.",
    ]
    if step.output_schema:
        additions.append(
            "The step has an output_schema. Return only JSON matching this schema, with no Markdown fences:\n"
            + json.dumps(_strict_json_schema(step.output_schema), ensure_ascii=True, sort_keys=True)
        )
    message["content"] = message["content"] + "\n\n" + "\n\n".join(additions)
    return message


def _workflow_output_repair_request_message(
    step: WorkflowStep,
    *,
    invalid_text: str | None,
    error_message: str,
) -> dict[str, str]:
    schema_text = json.dumps(_strict_json_schema(step.output_schema or {}), ensure_ascii=True, sort_keys=True)
    previous_output = str(invalid_text or "")
    if len(previous_output) > 6000:
        previous_output = previous_output[:6000] + "... [truncated]"
    return {
        "role": "user",
        "content": (
            "(System): Your previous workflow-step final answer did not parse as valid JSON for the required "
            "output_schema. Tools are disabled. Return only corrected JSON now, with no Markdown fences, "
            "no explanation, and no extra text.\n\n"
            f"Parse/validation error: {error_message}\n\n"
            f"Required strict JSON schema:\n{schema_text}\n\n"
            f"Previous invalid output:\n{previous_output}"
        ),
    }


def _is_invalid_json_output_error(error: Exception) -> bool:
    return "output did not match the required json schema" in str(error or "").lower()


def _append_step_runtime_event(step: WorkflowStep, event_packet: dict[str, Any], *, limit: int = 100) -> None:
    append_agent_event_to_metadata_attr(step, event_packet, limit=limit)


def _build_worker_system_prompt(addon_manager: Any, step: WorkflowStep, profile: Any = None, run: WorkflowRun | None = None, settings: dict[str, Any] | None = None) -> str:
    sections = ["You are a constrained worker agent executing one workflow step. Complete only this step and return the requested output."]

    if profile:
        sections.append(
            "\n".join(
                part for part in [
                    f"Agent profile: {profile.name} ({profile.id})",
                    profile.description,
                    f"Model class: {profile.model_class}" if profile.model_class else "",
                ]
                if part
            )
        )
        profile_prompts = _render_prompt_records(addon_manager, profile.prompt_ids)
        if profile_prompts:
            sections.append("--- PROFILE PROMPTS ---\n" + "\n\n".join(profile_prompts))

    skill_ids = _resolve_skill_ids(addon_manager, [*(profile.skill_ids if profile else []), *(step.skill_ids or [])])
    skill_sections = []
    for skill_id in skill_ids:
        if hasattr(addon_manager, 'render_skill_instructions'):
            rendered = addon_manager.render_skill_instructions(skill_id)
            if rendered:
                skill_sections.append(rendered)
    if skill_sections:
        sections.append("--- SKILL INSTRUCTIONS ---\n" + "\n\n".join(skill_sections))

    if run:
        sections.append(
            "--- WORKFLOW WORKING MEMORY ---\n"
            + json.dumps(_workflow_memory(run), indent=2, ensure_ascii=False)
            + "\nUse `update_shared_memory` to save durable intermediate findings for this run. Use `read_shared_memory` if you need to inspect a key."
        )
        sections.append(
            "--- RUNTIME CONTEXT ---\n"
            f"Workflow run id: {run.id}\n"
            f"Workflow step id: {step.id}\n"
            "Large tool and dependency outputs are summarized with artifact-backed data refs. "
            "Call `runtime_read_data_ref` only when you need a targeted slice of one of those refs.\n"
            "If this step creates a report, draft, or other text output meant for the user, call `runs_create_text_artifact` with the content, label, and filename. "
            "The runtime will attach the current run and step ids if you omit them."
        )

    if step.instructions:
        sections.append(f"--- STEP INSTRUCTIONS ---\n{step.instructions}")
    if step.output_schema:
        sections.append("Return the final answer as JSON matching the step output schema. Do not wrap it in Markdown.")

    return "\n\n".join(section for section in sections if section)


def _render_prompt_records(addon_manager: Any, prompt_ids: list[str] | None) -> list[str]:
    if not prompt_ids or not hasattr(addon_manager, 'registries'):
        return []
    prompts_by_id = {prompt.id: prompt for prompt in getattr(addon_manager.registries, 'prompts', [])}
    rendered = []
    for prompt_id in prompt_ids:
        prompt = prompts_by_id.get(prompt_id)
        if prompt and prompt.content:
            rendered.append(f"Prompt: {prompt.name} ({prompt.id})\n{prompt.content}")
    return rendered


def _resolve_skill_ids(addon_manager: Any, raw_skill_ids: list[Any]) -> list[str]:
    skill_ids = []
    for raw_skill_id in _unique(raw_skill_ids):
        skill_id = str(raw_skill_id)
        if not hasattr(addon_manager, "get_skill_record"):
            skill_ids.append(skill_id)
            continue
        if addon_manager.get_skill_record(skill_id):
            skill_ids.append(skill_id)
            continue
        capability = addon_manager.get_capability_record(skill_id) if hasattr(addon_manager, "get_capability_record") else None
        if capability and capability.capability_type == "skill":
            for element_id in capability.element_ids:
                if hasattr(addon_manager, "get_skill_record") and addon_manager.get_skill_record(element_id):
                    skill_ids.append(element_id)
                    break
    return _unique(skill_ids)


def _read_shared_memory_tool_def() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "read_shared_memory",
            "description": "Read the workflow run's shared JSON working memory. Omit key to read the whole memory object.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Optional memory key to read."},
                },
            },
        },
    }


def _runtime_read_data_ref_tool_def() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "runtime_read_data_ref",
            "description": (
                "Read a bounded slice of a large tool or dependency result retained "
                "behind an artifact-backed runtime data reference."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "data_ref_id": {
                        "type": "string",
                        "description": "The artifact:... data reference from a result envelope.",
                    },
                    "start_offset": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 0,
                    },
                    "max_chars": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50000,
                        "default": 8000,
                    },
                },
                "required": ["data_ref_id"],
            },
        },
    }


def _update_shared_memory_tool_def() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "update_shared_memory",
            "description": "Update the workflow run's shared JSON working memory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["set", "delete", "clear"]},
                    "key": {"type": "string", "description": "Memory key to set/delete."},
                    "value": {"description": "Value to store for set action."},
                },
                "required": ["action"],
            },
        },
    }


def _request_budget_increase_tool_def() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "request_budget_increase",
            "description": "Request more tool call turns (budget) from the user/main model if you need more steps to complete the task.",
            "parameters": {
                "type": "object",
                "properties": {
                    "requested_increase": {
                        "type": "integer",
                        "minimum": 5,
                        "maximum": 50,
                        "description": "The number of additional tool call turns you need (e.g. 10, 20)."
                    },
                    "reason": {
                        "type": "string",
                        "description": "Detailed explanation of why you need more budget (e.g. 'Processing remaining 40 attachments in batches')."
                    }
                },
                "required": ["requested_increase", "reason"]
            }
        }
    }


def _workflow_memory(run: WorkflowRun) -> dict[str, Any]:
    state = run.state_json if isinstance(run.state_json, dict) else {}
    memory = state.get("working_memory") if isinstance(state, dict) else {}
    return memory if isinstance(memory, dict) else {}


def _update_shared_memory(run: WorkflowRun, arguments: dict[str, Any]) -> dict[str, Any]:
    action = arguments.get("action")
    key = arguments.get("key")
    value = arguments.get("value")
    db.session.refresh(run)
    state = dict(run.state_json) if isinstance(run.state_json, dict) else {}
    memory = dict(state.get("working_memory")) if isinstance(state.get("working_memory"), dict) else {}
    if action == "set":
        if not key:
            return {"error": "key is required for set."}
        memory[str(key)] = value
        message = f"Key '{key}' set successfully."
    elif action == "delete":
        if not key:
            return {"error": "key is required for delete."}
        memory.pop(str(key), None)
        message = f"Key '{key}' deleted successfully."
    elif action == "clear":
        memory = {}
        message = "Memory cleared successfully."
    else:
        return {"error": f"Unknown action '{action}'."}
    run.state_json = {**state, "working_memory": memory}
    db.session.commit()
    return {"status": "ok", "message": message, "working_memory": memory}


def _read_shared_memory(run: WorkflowRun, arguments: dict[str, Any]) -> dict[str, Any]:
    db.session.refresh(run)
    memory = _workflow_memory(run)
    key = arguments.get("key")
    if key:
        return {"key": key, "value": memory.get(str(key))}
    return {"working_memory": memory}


def _latest_step_job_id(step: WorkflowStep) -> str | None:
    job_ids = [str(item) for item in (step.job_ids or []) if item]
    if not job_ids:
        return None
    return job_ids[-1]


def _populate_run_tool_context(tool_name: str, arguments: dict[str, Any], *, run: WorkflowRun, step: WorkflowStep) -> dict[str, Any]:
    hydrated = dict(arguments or {})
    run_scoped_tools = {
        "runs_get",
        "runs_observe",
        "runs_list_steps",
        "runs_list_artifacts",
        "runs_read_artifact",
        "runs_read_shared_memory",
        "runs_create_collection",
        "runs_get_collection",
        "runs_upsert_collection_items",
        "runs_partition_collection_items",
        "runs_claim_collection_items",
        "runs_annotate_collection_items",
        "runs_update_understanding",
        "runs_update_plan",
        "runs_create_checkpoint",
        "runs_update_checkpoint",
        "runs_start",
        "runs_pause",
        "runs_resume",
        "runs_stop",
        "runs_add_step",
        "runs_add_source",
        "runs_add_evidence",
        "runs_add_artifact",
        "runs_create_text_artifact",
        "runs_quick_text_artifact",
        "documents_inspect",
        "documents_preview_pages",
        "documents_extract_pages",
        "documents_split_pages",
        "runs_code_session_create",
        "runs_code_session_get",
        "runs_code_session_execute",
        "runs_code_session_install_dependencies",
        "runs_project_workspace_create",
        "runs_project_workspace_get",
        "runs_project_workspace_inspect",
        "runs_project_workspace_edit",
        "runs_project_workspace_execute",
        "runs_project_workspace_process",
        "runs_project_workspace_close",
        "runs_execute_python",
        "runs_execute_tool_batch",
        "runs_add_artifact_version",
        "runs_update_artifact_review",
    }
    step_scoped_tools = {
        "runs_update_step",
        "runs_add_source",
        "runs_add_evidence",
        "runs_add_artifact",
        "runs_add_artifact_version",
        "runs_create_text_artifact",
        "runs_quick_text_artifact",
        "documents_inspect",
        "documents_preview_pages",
        "documents_extract_pages",
        "documents_split_pages",
        "runs_code_session_create",
        "runs_code_session_execute",
        "runs_code_session_install_dependencies",
        "runs_project_workspace_create",
        "runs_project_workspace_get",
        "runs_project_workspace_inspect",
        "runs_project_workspace_edit",
        "runs_project_workspace_execute",
        "runs_project_workspace_process",
        "runs_project_workspace_close",
        "runs_execute_python",
        "runs_execute_tool_batch",
        "runs_create_collection",
        "runs_get_collection",
        "runs_upsert_collection_items",
        "runs_partition_collection_items",
        "runs_claim_collection_items",
        "runs_annotate_collection_items",
    }
    if tool_name in run_scoped_tools:
        hydrated["run_id"] = run.id
    if tool_name in step_scoped_tools:
        hydrated["step_id"] = step.id
    return hydrated


def _response_format_for_step(step: WorkflowStep) -> dict[str, Any] | None:
    if not step.output_schema:
        return None
    schema = _strict_json_schema(step.output_schema)
    schema_name = re.sub(r"[^a-zA-Z0-9_]", "_", f"workflow_step_{step.step_key or step.id}")[:64]
    if not schema_name or not schema_name[0].isalpha():
        schema_name = f"step_{schema_name}"
    return {
        "type": "json_schema",
        "json_schema": {
            "name": schema_name,
            "schema": schema,
            "strict": True,
        },
    }


def _strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(schema)

    def visit(node: Any) -> None:
        if not isinstance(node, dict):
            return
        schema_type = node.get("type")
        if isinstance(schema_type, str):
            node["type"] = schema_type.strip().lower()
        elif isinstance(schema_type, list):
            node["type"] = [
                value.strip().lower() if isinstance(value, str) else value
                for value in schema_type
            ]
        if node.get("type") == "object" or "properties" in node:
            node.setdefault("type", "object")
            node.setdefault("additionalProperties", False)
            for child in (node.get("properties") or {}).values():
                visit(child)
        items = node.get("items")
        if isinstance(items, dict):
            visit(items)
        for key in ("anyOf", "allOf", "oneOf"):
            for child in node.get(key) or []:
                visit(child)

    visit(normalized)
    return normalized


def _output_payload_for_step(step: WorkflowStep, final_text: str | None) -> dict[str, Any]:
    if not step.output_schema:
        return {"text": final_text}
    if not final_text:
        raise ValueError("Workflow step declared an output_schema but the model returned no content.")
    try:
        parsed = json.loads(final_text)
    except json.JSONDecodeError as exc:
        raise ValueError("Workflow step output did not match the required JSON schema.") from exc
    schema = _strict_json_schema(step.output_schema)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError("Workflow step output_schema is invalid.") from exc
    validation_error = next(Draft202012Validator(schema).iter_errors(parsed), None)
    if validation_error is not None:
        path = "$"
        for segment in validation_error.absolute_path:
            path += (
                f"[{segment}]"
                if isinstance(segment, int)
                else f".{segment}"
            )
        expected = json.dumps(
            validation_error.validator_value,
            ensure_ascii=True,
            default=str,
        )
        if len(expected) > 300:
            expected = expected[:300] + "... [truncated]"
        raise ValueError(
            "Workflow step output did not match the required JSON schema "
            f"at {path}: failed '{validation_error.validator}' constraint "
            f"(expected {expected})."
        )
    if isinstance(parsed, dict):
        return parsed
    return {"result": parsed}


def _message_content(message: Any) -> str | None:
    if isinstance(message, dict):
        return message.get("content")
    return getattr(message, "content", None)


def _unique(values: list[Any]) -> list[Any]:
    result = []
    seen = set()
    for value in values:
        if not value or value in seen:
            continue
        result.append(value)
        seen.add(value)
    return result


def _string_values(value: Any) -> list[str]:
    if value is None:
        return []
    raw_values = value if isinstance(value, (list, tuple, set)) else [value]
    return [str(item).strip() for item in raw_values if str(item).strip()]


def _unique_tool_declarations(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        key = name or json.dumps(tool, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        result.append(tool)
    return result
