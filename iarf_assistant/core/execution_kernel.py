from __future__ import annotations

import contextlib
import copy
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, ContextManager

from carlina.core.runtime_policy import clamp_tool_call_iterations
from iarf_assistant.core.agent_events import (
    finalization_event,
    tool_budget_exhausted_event,
    tool_budget_warning_event,
    tool_call_finished_event,
    tool_call_started_event,
)
from iarf_assistant.core.agent_loop import (
    ExecutedToolCall,
    PreparedToolCall,
    build_finalization_request_message,
    build_tool_budget_warning_message,
    execute_ordered_tool_calls_parallel,
    loop_budget_status,
    normalize_tool_execution_output,
    prepare_tool_call,
    should_request_finalization,
)
from iarf_assistant.core.runtime_contracts import (
    ToolResultEnvelope,
    build_tool_result_envelope,
    estimate_tokens,
)
from iarf_assistant.core.runtime_state import guard_tool_execution

ROLE_CONTEXT_DEFAULTS = {
    "chief": {"history_tokens": None, "ref_preview_chars": 2400},
    "worker": {"history_tokens": 24000, "ref_preview_chars": 1600},
    "run": {"history_tokens": 32000, "ref_preview_chars": 1800},
    "code": {"history_tokens": 16000, "ref_preview_chars": 1200},
}


@dataclass(frozen=True)
class ContextAssembly:
    role: str
    messages: tuple[dict[str, Any], ...]
    estimated_tokens: int
    omitted_message_count: int = 0
    omitted_reasons: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "message_count": len(self.messages),
            "estimated_tokens": self.estimated_tokens,
            "omitted_message_count": self.omitted_message_count,
            "omitted_reasons": list(self.omitted_reasons),
            "metadata": copy.deepcopy(self.metadata),
        }


class ContextAssembler:
    """Build bounded provider views while preserving role-specific policy."""

    def __init__(
        self,
        role: str,
        *,
        max_history_tokens: int | None = None,
        message_sanitizer: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
    ):
        normalized_role = str(role or "chief").strip().lower()
        if normalized_role not in ROLE_CONTEXT_DEFAULTS:
            raise ValueError(f"Unsupported context role: {role}")
        defaults = ROLE_CONTEXT_DEFAULTS[normalized_role]
        self.role = normalized_role
        self.max_history_tokens = (
            max_history_tokens
            if max_history_tokens is not None
            else defaults["history_tokens"]
        )
        self.message_sanitizer = message_sanitizer

    def assemble(
        self,
        messages: list[dict[str, Any]],
        *,
        supplemental_messages: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ContextAssembly:
        primary_rows = [
            copy.deepcopy(message)
            for message in messages or []
            if isinstance(message, dict)
        ]
        supplemental_rows = [
            copy.deepcopy(message)
            for message in supplemental_messages or []
            if isinstance(message, dict)
        ]
        if callable(self.message_sanitizer):
            # Sanitize each origin independently so a sanitizer that filters
            # rows cannot erase the primary/supplemental boundary used to
            # protect the chief's actual latest request.
            primary_rows = self.message_sanitizer(primary_rows)
            supplemental_rows = self.message_sanitizer(supplemental_rows)
        rows = [*primary_rows, *supplemental_rows]

        omitted = 0
        reasons: list[str] = []
        limit = self.max_history_tokens
        if limit is not None and limit > 0:
            rows, omitted = self._bounded_messages(
                rows,
                limit,
                protect_latest_user=self.role == "chief",
                primary_message_count=len(primary_rows),
            )
            if omitted:
                reasons.append("role_history_budget")

        return ContextAssembly(
            role=self.role,
            messages=tuple(rows),
            estimated_tokens=sum(estimate_tokens(message) for message in rows),
            omitted_message_count=omitted,
            omitted_reasons=tuple(reasons),
            metadata=copy.deepcopy(metadata) if isinstance(metadata, dict) else {},
        )

    @staticmethod
    def _bounded_messages(
        messages: list[dict[str, Any]],
        max_tokens: int,
        *,
        protect_latest_user: bool = False,
        primary_message_count: int | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        if sum(estimate_tokens(message) for message in messages) <= max_tokens:
            return messages, 0

        system_index = next(
            (
                index
                for index, message in enumerate(messages)
                if message.get("role") == "system"
            ),
            None,
        )
        latest_user_boundary = (
            max(0, min(len(messages), int(primary_message_count)))
            if primary_message_count is not None
            else len(messages)
        )
        user_indexes = [
            index
            for index, message in enumerate(messages[:latest_user_boundary])
            if message.get("role") == "user"
        ]
        task_index = (
            user_indexes[-1]
            if protect_latest_user and user_indexes
            else user_indexes[0] if user_indexes else None
        )
        protected_indexes = {
            index
            for index in (system_index, task_index)
            if index is not None
        }
        protected_rows = [
            (index, message)
            for index, message in enumerate(messages)
            if index in protected_indexes
        ]
        remaining = [
            (index, message)
            for index, message in enumerate(messages)
            if index not in protected_indexes
        ]
        groups: list[list[tuple[int, dict[str, Any]]]] = []
        index = 0
        while index < len(remaining):
            message_index, message = remaining[index]
            group = [(message_index, message)]
            if message.get("role") == "assistant" and message.get("tool_calls"):
                index += 1
                while (
                    index < len(remaining)
                    and remaining[index][1].get("role") == "tool"
                ):
                    group.append(remaining[index])
                    index += 1
                groups.append(group)
                continue
            groups.append(group)
            index += 1

        kept_reversed: list[list[tuple[int, dict[str, Any]]]] = []
        used = sum(estimate_tokens(message) for _, message in protected_rows)
        for group in reversed(groups):
            cost = sum(estimate_tokens(message) for _, message in group)
            if used + cost > max_tokens:
                # Never force one oversized historical group into a bounded
                # projection. The protected task/current request already keeps
                # the model call actionable; raw omitted rows remain durable.
                if kept_reversed:
                    break
                continue
            kept_reversed.append(group)
            used += cost
        kept_rows = [
            *protected_rows,
            *[row for group in reversed(kept_reversed) for row in group],
        ]
        kept = [message for _, message in sorted(kept_rows, key=lambda row: row[0])]
        return kept, max(0, len(messages) - len(kept))


class AgentEventSink:
    """One event vocabulary with optional stream and persistence destinations."""

    def __init__(
        self,
        *,
        persist: Callable[[dict[str, Any]], Any] | None = None,
        stream: Callable[[dict[str, Any]], Any] | None = None,
        runtime_context: dict[str, Any] | None = None,
    ):
        self.persist = persist
        self.stream = stream
        self.runtime_context = copy.deepcopy(runtime_context) if isinstance(runtime_context, dict) else {}

    def emit(self, event: dict[str, Any]) -> dict[str, Any]:
        packet = copy.deepcopy(event) if isinstance(event, dict) else {}
        nested_event = packet.get("event")
        event_body: dict[str, Any] = (
            nested_event
            if packet.get("type") == "agent_event" and isinstance(nested_event, dict)
            else packet
        )
        raw_payload = event_body.get("payload")
        payload: dict[str, Any] = (
            raw_payload if isinstance(raw_payload, dict) else {}
        )
        if self.runtime_context:
            raw_runtime_context = payload.get("runtime_context")
            event_body["payload"] = {
                **payload,
                "runtime_context": {
                    **self.runtime_context,
                    **(
                        raw_runtime_context
                        if isinstance(raw_runtime_context, dict)
                        else {}
                    ),
                },
            }
        if callable(self.persist):
            self.persist(packet)
        if callable(self.stream):
            self.stream(packet)
        return packet


class AgentLoopRunner:
    """Shared loop/finalization policy without replacing path-specific orchestration."""

    def __init__(
        self,
        *,
        max_tool_calls: int,
        event_sink: AgentEventSink | None = None,
        warning_at_remaining: int = 2,
        minimum_warning_budget: int = 3,
        budget_increase_tool_name: str | None = None,
    ):
        self.max_tool_calls = clamp_tool_call_iterations(max_tool_calls, default=1)
        self.event_sink = event_sink or AgentEventSink()
        self.warning_at_remaining = max(1, int(warning_at_remaining or 1))
        self.minimum_warning_budget = max(1, int(minimum_warning_budget or 1))
        self.budget_increase_tool_name = budget_increase_tool_name

    def prepare_iteration(
        self,
        messages: list[dict[str, Any]],
        loop_index: int,
    ):
        status = loop_budget_status(
            loop_index,
            self.max_tool_calls,
            warning_at_remaining=self.warning_at_remaining,
            minimum_warning_budget=self.minimum_warning_budget,
        )
        if status.should_warn:
            messages.append(build_tool_budget_warning_message(
                used_tool_call_iterations=status.used_tool_call_iterations,
                max_tool_calls=self.max_tool_calls,
                remaining_tool_call_iterations=status.remaining_tool_call_iterations,
                budget_increase_tool_name=self.budget_increase_tool_name,
            ))
            self.event_sink.emit(tool_budget_warning_event(
                max_tool_calls=self.max_tool_calls,
                remaining_tool_calls=status.remaining_tool_call_iterations,
                loop_index=loop_index,
            ))
        return status

    def after_tool_calls(self, loop_index: int, *, has_tool_calls: bool):
        status = loop_budget_status(
            loop_index,
            self.max_tool_calls,
            has_tool_calls=has_tool_calls,
            warning_at_remaining=self.warning_at_remaining,
            minimum_warning_budget=self.minimum_warning_budget,
        )
        if status.should_force_finalization:
            self.event_sink.emit(tool_budget_exhausted_event(
                max_tool_calls=self.max_tool_calls,
                loop_index=loop_index,
            ))
        return status

    def needs_finalization(self, message: Any) -> bool:
        return should_request_finalization(message)

    def begin_finalization(
        self,
        messages: list[dict[str, Any]],
        *,
        reason: str,
        loop_index: int | None = None,
    ) -> dict[str, str]:
        request = build_finalization_request_message(
            reason=reason,
            max_tool_calls=self.max_tool_calls if reason == "tool_call_limit" else None,
        )
        messages.append(request)
        self.event_sink.emit(finalization_event(
            status="running",
            reason=reason,
            loop_index=loop_index,
        ))
        return request

    def finish_finalization(
        self,
        *,
        reason: str,
        content: str,
        loop_index: int | None = None,
    ) -> None:
        self.event_sink.emit(finalization_event(
            status="completed" if str(content or "").strip() else "error",
            reason=reason,
            loop_index=loop_index,
            content_preview=content,
        ))


@dataclass(frozen=True)
class ToolAuthorityContext:
    requester_user_id: int | None = None
    runtime_policy: dict[str, Any] = field(default_factory=dict)
    audit_log_source_id: str = "jobs.runtime"
    log_source_ids: tuple[str, ...] = ("jobs.runtime",)
    workflow_run_id: str | None = None
    workflow_step_id: str | None = None
    runtime_job_id: str | None = None


class ToolExecutor:
    """Shared validation, authority, error, envelope, and event path."""

    def __init__(
        self,
        *,
        dispatcher: Callable[[str, dict[str, Any], PreparedToolCall], Any],
        event_sink: AgentEventSink | None = None,
        authority: ToolAuthorityContext | None = None,
        tool_record_resolver: Callable[[str], Any] | None = None,
        argument_validator: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
        availability_checker: Callable[[str, dict[str, Any]], dict[str, Any] | None] | None = None,
        execution_context_factory: Callable[[], ContextManager[Any]] | None = None,
        result_externalizer: Callable[[PreparedToolCall, str, Any], Any] | None = None,
        inline_result_limit_chars: int = 12000,
        preview_chars: int = 2400,
        timeout_seconds: float | None = None,
        timeout_resolver: Callable[[PreparedToolCall], float | None] | None = None,
        inline_execution: bool = False,
    ):
        self.dispatcher = dispatcher
        self.event_sink = event_sink or AgentEventSink()
        self.authority = authority or ToolAuthorityContext()
        self.tool_record_resolver = tool_record_resolver
        self.argument_validator = argument_validator
        self.availability_checker = availability_checker
        self.execution_context_factory = execution_context_factory
        self.result_externalizer = result_externalizer
        self.inline_result_limit_chars = inline_result_limit_chars
        self.preview_chars = preview_chars
        self.timeout_seconds = float(timeout_seconds) if timeout_seconds else None
        self.timeout_resolver = timeout_resolver
        self.inline_execution = bool(inline_execution)

    def _execution_context(self):
        if callable(self.execution_context_factory):
            return self.execution_context_factory()
        return contextlib.nullcontext()

    def _execute_prepared(self, prepared: PreparedToolCall) -> dict[str, Any]:
        with self._execution_context():
            arguments = dict(prepared.arguments or {})
            if callable(self.availability_checker):
                blocked = self.availability_checker(prepared.tool_name, arguments)
                if blocked:
                    return {"action": "blocked", "result": blocked}

            tool_record = (
                self.tool_record_resolver(prepared.tool_name)
                if callable(self.tool_record_resolver)
                else None
            )
            if tool_record is not None:
                approval_id = arguments.pop("_approval_id", None)
                resource_reference_ids = arguments.pop("_resource_reference_ids", None)
                if callable(self.argument_validator):
                    try:
                        arguments = self.argument_validator(prepared.tool_name, arguments)
                    except (KeyError, RuntimeError, TypeError, ValueError) as exc:
                        return {
                            "action": "blocked",
                            "result": {
                                "status": "invalid_tool_call",
                                "error": str(exc),
                            },
                        }
                guard = guard_tool_execution(
                    tool_record,
                    arguments=arguments,
                    requester_user_id=self.authority.requester_user_id,
                    approval_id=approval_id,
                    resource_reference_ids=resource_reference_ids,
                    audit_log_source_id=self.authority.audit_log_source_id,
                    log_source_ids=list(self.authority.log_source_ids),
                    runtime_policy=self.authority.runtime_policy,
                    workflow_run_id=self.authority.workflow_run_id,
                )
                if not guard.allowed:
                    return {"action": "blocked", "result": guard.to_tool_response()}

            try:
                return self.dispatcher(prepared.tool_name, arguments, prepared)
            except TimeoutError as exc:
                return {
                    "action": "error",
                    "result": {
                        "status": "tool_timeout",
                        "error": str(exc) or "Tool execution timed out.",
                    },
                }
            except Exception as exc:
                return {
                    "action": "error",
                    "result": {
                        "status": "tool_execution_failed",
                        "error": str(exc),
                    },
                }

    def _envelope_for(self, executed: ExecutedToolCall) -> ToolResultEnvelope:
        def externalize(raw: str, result: Any):
            if not callable(self.result_externalizer):
                return None
            return self.result_externalizer(executed.prepared, raw, result)

        envelope = build_tool_result_envelope(
            tool_name=executed.tool_name,
            internal_tool_call_id=executed.tool_call_id,
            provider_tool_call_id=executed.provider_tool_call_id,
            result=executed.result,
            action=executed.action,
            duration_ms=executed.duration_ms,
            error=executed.error,
            inline_limit_chars=self.inline_result_limit_chars,
            preview_chars=self.preview_chars,
            externalize=externalize,
        )
        configured_timeout = self.timeout_seconds
        if callable(self.timeout_resolver):
            try:
                configured_timeout = self.timeout_resolver(executed.prepared)
            except Exception:
                configured_timeout = self.timeout_seconds
        if (
            configured_timeout is not None
            and executed.duration_ms > int(float(configured_timeout) * 1000)
        ):
            envelope = replace(
                envelope,
                warnings=(
                    *envelope.warnings,
                    (
                        f"The configured tool timeout was {float(configured_timeout):g} "
                        "seconds. Cancellation cannot be guaranteed for connectors "
                        "or operations with side effects."
                    ),
                ),
            )
        return envelope

    def _started(
        self,
        prepared: PreparedToolCall,
        *,
        loop_index: int | None,
        callback: Callable[[PreparedToolCall], Any] | None = None,
    ) -> None:
        self.event_sink.emit(tool_call_started_event(
            tool_call_id=prepared.tool_call_id,
            provider_tool_call_id=prepared.provider_tool_call_id,
            tool_name=prepared.tool_name,
            arguments=prepared.arguments,
            loop_index=loop_index,
        ))
        if callable(callback):
            callback(prepared)

    def _finished(
        self,
        executed: ExecutedToolCall,
        *,
        loop_index: int | None,
        callback: Callable[[ExecutedToolCall], Any] | None = None,
    ) -> ExecutedToolCall:
        envelope = self._envelope_for(executed)
        enveloped = replace(executed, result_envelope=envelope.to_dict())
        self.event_sink.emit(tool_call_finished_event(
            tool_call_id=enveloped.tool_call_id,
            provider_tool_call_id=enveloped.provider_tool_call_id,
            tool_name=enveloped.tool_name,
            action=enveloped.action,
            result=enveloped.result,
            result_envelope=enveloped.result_envelope,
            loop_index=loop_index,
            duration_ms=enveloped.duration_ms,
        ))
        if callable(callback):
            callback(enveloped)
        return enveloped

    def _execute_call_inline(
        self,
        tool_call: Any,
        *,
        loop_index: int | None = None,
        argument_transform: Callable[[str, dict[str, Any], Any], dict[str, Any]] | None = None,
    ) -> ExecutedToolCall:
        prepared = prepare_tool_call(
            tool_call,
            0,
            argument_transform=argument_transform,
        )
        self._started(prepared, loop_index=loop_index)
        started_at = time.monotonic()
        error = None
        if prepared.argument_error:
            output = {
                "action": "error",
                "result": {
                    "status": "invalid_tool_arguments",
                    "error": prepared.argument_error,
                },
            }
        else:
            try:
                output = normalize_tool_execution_output(
                    self._execute_prepared(prepared)
                )
            except Exception as exc:
                error = str(exc)
                output = {
                    "action": "error",
                    "result": {
                        "status": "tool_execution_failed",
                        "error": error,
                    },
                }
        executed = ExecutedToolCall(
            prepared=prepared,
            output=output,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            error=error,
        )
        return self._finished(executed, loop_index=loop_index)

    def execute_calls(
        self,
        tool_calls: list[Any],
        *,
        loop_index: int | None = None,
        argument_transform: Callable[[str, dict[str, Any], Any], dict[str, Any]] | None = None,
        max_workers: int = 10,
        on_started: Callable[[PreparedToolCall], Any] | None = None,
        on_finished: Callable[[ExecutedToolCall], Any] | None = None,
    ) -> list[ExecutedToolCall]:
        enveloped_by_index: dict[int, ExecutedToolCall] = {}

        def started(prepared: PreparedToolCall):
            self._started(
                prepared,
                loop_index=loop_index,
                callback=on_started,
            )

        def finished(executed: ExecutedToolCall):
            enveloped = self._finished(
                executed,
                loop_index=loop_index,
                callback=on_finished,
            )
            enveloped_by_index[executed.prepared.index] = enveloped

        raw_rows = execute_ordered_tool_calls_parallel(
            tool_calls,
            execute_tool=self._execute_prepared,
            argument_transform=argument_transform,
            on_started=started,
            on_finished=finished,
            max_workers=max_workers,
            timeout_seconds=self.timeout_seconds,
            timeout_resolver=self.timeout_resolver,
        )
        return [
            enveloped_by_index.get(row.prepared.index, row)
            for row in raw_rows
        ]

    def execute_call(
        self,
        tool_call: Any,
        *,
        loop_index: int | None = None,
        argument_transform: Callable[[str, dict[str, Any], Any], dict[str, Any]] | None = None,
    ) -> ExecutedToolCall:
        if self.inline_execution:
            return self._execute_call_inline(
                tool_call,
                loop_index=loop_index,
                argument_transform=argument_transform,
            )
        return self.execute_calls(
            [tool_call],
            loop_index=loop_index,
            argument_transform=argument_transform,
            max_workers=1,
        )[0]
