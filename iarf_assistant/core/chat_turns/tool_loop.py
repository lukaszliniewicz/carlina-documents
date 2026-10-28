"""Bounded chat tool-loop and tool-result processing services."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable, Sequence

from iarf_assistant.chat_builtin_tools import (
    extract_delegate_coverage_items,
    extract_source_coverage_items_from_tool_result,
    upsert_source_coverage,
)
from iarf_assistant.chat_context_store import externalize_tool_result_message
from iarf_assistant.chat_execution_helpers import (
    build_tool_attachment_message,
    build_tool_result_message,
    sanitize_llm_message,
    split_tool_output_attachments,
    tool_result_limits_for_policy,
)
from iarf_assistant.core.agent_events import (
    source_coverage_updated_event,
    tool_call_context_event,
)
from iarf_assistant.core.agent_loop import message_tool_calls
from iarf_assistant.core.context_fabric import store_tool_result
from iarf_assistant.models import LLMModel


@dataclass(frozen=True, slots=True)
class ToolCallProcessing:
    """Effects produced while incorporating one completed tool call."""

    waiting_for_user: bool = False
    attachment_messages: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class ChatToolLoopResult:
    """Terminal state returned to the compatibility orchestrator."""

    response: Any | None
    waiting_for_user: bool
    finalization_attempted: bool
    iterations: int


@dataclass(slots=True)
class ChatToolLoopHooks:
    """Side-effect boundaries owned by the surrounding chat turn."""

    complete_iteration: Callable[[int], Any]
    execute_tool_calls: Callable[[Sequence[Any], int], Sequence[Any]]
    process_tool_result: Callable[[Any, Any, int], ToolCallProcessing]
    request_finalization: Callable[[str, int | None], Any]
    emit_assistant_output: Callable[..., None]
    emit_status: Callable[[str], None]
    record_tool_executions: Callable[[Sequence[Any]], None]
    add_budget_warning_tool: Callable[[], None]
    get_max_tool_calls: Callable[[], int]


class ChatToolLoop:
    """Run the model/tool iteration protocol without owning Flask state."""

    def __init__(
        self,
        *,
        messages: list[dict[str, Any]],
        new_messages: list[dict[str, Any]],
        loop_runner: Any,
        hooks: ChatToolLoopHooks,
        one_shot_code_intent: bool = False,
    ) -> None:
        self.messages = messages
        self.new_messages = new_messages
        self.loop_runner = loop_runner
        self.hooks = hooks
        self.one_shot_code_intent = bool(one_shot_code_intent)

    def _append_attachments(self, attachments: Sequence[dict[str, Any]]) -> None:
        self.messages.extend(attachments)
        self.new_messages.extend(attachments)

    def run(self) -> ChatToolLoopResult:
        response = None
        waiting_for_user = False
        finalization_attempted = False
        loop_index = 0
        while loop_index < self.hooks.get_max_tool_calls():
            self.loop_runner.max_tool_calls = self.hooks.get_max_tool_calls()
            budget = self.loop_runner.prepare_iteration(self.messages, loop_index)
            if budget.should_warn and not self.one_shot_code_intent:
                self.hooks.add_budget_warning_tool()
            self.hooks.emit_status("Evaluating next actions...")
            response = self.hooks.complete_iteration(loop_index)
            response_message = response.choices[0].message
            clean_message = sanitize_llm_message(response_message)
            self.messages.append(clean_message)
            tool_calls = message_tool_calls(response_message)
            if not tool_calls:
                break

            self.new_messages.append(clean_message)
            self.hooks.emit_assistant_output(
                clean_message,
                loop_index=loop_index,
            )
            executed = self.hooks.execute_tool_calls(tool_calls, loop_index)
            self.hooks.record_tool_executions(executed)
            attachments: list[dict[str, Any]] = []
            for tool_call, execution in zip(tool_calls, executed):
                outcome = self.hooks.process_tool_result(
                    tool_call,
                    execution,
                    loop_index,
                )
                waiting_for_user = waiting_for_user or outcome.waiting_for_user
                attachments.extend(outcome.attachment_messages)
            self._append_attachments(attachments)
            if waiting_for_user:
                break

            self.loop_runner.max_tool_calls = self.hooks.get_max_tool_calls()
            budget = self.loop_runner.after_tool_calls(
                loop_index,
                has_tool_calls=bool(tool_calls),
            )
            if budget.should_force_finalization:
                response = self.hooks.request_finalization(
                    "tool_call_limit",
                    loop_index,
                )
                finalization_attempted = True
                break
            loop_index += 1

        if (
            not waiting_for_user
            and response is not None
            and not finalization_attempted
            and self.loop_runner.needs_finalization(response.choices[0].message)
        ):
            response = self.hooks.request_finalization(
                "empty_assistant_response",
                None,
            )
            finalization_attempted = True
        return ChatToolLoopResult(
            response=response,
            waiting_for_user=waiting_for_user,
            finalization_attempted=finalization_attempted,
            iterations=loop_index + (1 if response is not None else 0),
        )


@dataclass(slots=True)
class ChatToolResultContext:
    """Mutable turn state and explicit callbacks used by result processing."""

    app: Any
    session_id: str
    user_id: int
    turn_id: str
    model_name: str
    runtime_policy: dict[str, Any]
    messages: list[dict[str, Any]]
    new_messages: list[dict[str, Any]]
    selected_capability_ids: list[str]
    one_shot_code_intent: bool
    ensure_context_result_tools: Callable[[], Any]
    clear_tools: Callable[[], None]
    emit_agent_event: Callable[[dict[str, Any]], None]
    prepare_direct_tools: Callable[..., dict[str, Any]]
    execute_run_tool: Callable[[str, dict[str, Any], int], Any]
    apply_budget_extension: Callable[..., dict[str, Any]]


class ChatToolResultProcessor:
    """Normalize, persist, and append completed tool results."""

    _CONTEXT_ACTIONS = frozenset(
        {
            "memory",
            "memory_fact",
            "context_read",
            "context_search",
            "context_manage",
            "source_coverage",
            "user_interaction",
            "runtime_monitor",
        }
    )

    def __init__(self, context: ChatToolResultContext) -> None:
        self.context = context
        self.one_shot_code_completed = False
        self._model_capabilities_loaded = False
        self._supports_pdf = False
        self._supports_vision = False

    @staticmethod
    def _arguments(tool_call: Any) -> dict[str, Any]:
        raw = getattr(tool_call.function, "arguments", None)
        if isinstance(raw, dict):
            return dict(raw)
        try:
            parsed = json.loads(raw or "{}")
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _waiting(result: Any) -> bool:
        return bool(
            isinstance(result, dict)
            and str(result.get("status") or "").lower()
            in {"waiting_for_user", "waiting_for_approval"}
            and result.get("interaction_id")
        )

    def _externalize_context_message(self, message: dict[str, Any], tool_name: str) -> None:
        try:
            externalize_tool_result_message(
                message,
                instance_path=self.context.app.instance_path,
                session_id=self.context.session_id,
                created_by_user_id=self.context.user_id,
                commit=False,
            )
        except Exception as exc:
            self.context.app.logger.warning(
                "Unable to externalize compacted tool result for %s: %s",
                tool_name,
                exc,
                exc_info=True,
            )

    def _apply_one_shot_completion(self, tool_name: str, result: Any) -> None:
        if not (
            self.context.one_shot_code_intent
            and tool_name == "runs_execute_python"
            and scratch_execution_succeeded(result)
        ):
            return
        self.one_shot_code_completed = True
        self.context.clear_tools()
        self.context.messages.append(
            {
                "role": "user",
                "content": (
                    "(System runtime note.) The requested one-shot Python "
                    "execution completed successfully. Answer the user now "
                    "from the execution result. Do not call another tool."
                ),
            }
        )

    def _emit_context_event(
        self,
        result: Any,
        message: dict[str, Any],
        *,
        tool_call_id: str,
        tool_name: str,
        loop_index: int,
    ) -> None:
        metadata = message.get("_context_optimization") or {}
        if metadata.get("compacted"):
            self.context.ensure_context_result_tools()
        if not should_emit_tool_context_event(result):
            return
        self.context.emit_agent_event(
            tool_call_context_event(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                context_optimization=message.get("_context_optimization"),
                loop_index=loop_index,
            )
        )

    def _record_source_coverage(
        self,
        tool_call: Any,
        result: Any,
        message: dict[str, Any],
        *,
        tool_call_id: str,
        loop_index: int,
    ) -> None:
        tool_name = tool_call.function.name
        try:
            items = extract_source_coverage_items_from_tool_result(
                tool_name,
                result,
                context_ref_id=(
                    message.get("_context_optimization") or {}
                ).get("ref_id"),
                tool_args=self._arguments(tool_call),
            )
        except Exception as exc:
            self.context.app.logger.warning(
                "Unable to extract automatic source coverage for %s: %s",
                tool_name,
                exc,
                exc_info=True,
            )
            return
        if not items:
            return
        coverage = upsert_source_coverage(
            {
                "items": items,
                "reason": f"Automatically recorded from {tool_name}.",
            },
            self.context.session_id,
        )
        self._emit_coverage_result(
            coverage,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            loop_index=loop_index,
        )

    def _emit_coverage_result(
        self,
        coverage: Any,
        *,
        tool_call_id: str,
        tool_name: str,
        loop_index: int,
    ) -> None:
        if not isinstance(coverage, dict) or coverage.get("status") != "ok":
            return
        self.context.emit_agent_event(
            source_coverage_updated_event(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                updated_count=coverage.get("updated_count") or 0,
                ledger_counts=coverage.get("ledger_counts") or {},
                loop_index=loop_index,
                automatic=True,
            )
        )

    def append_context_message(
        self,
        tool_call: Any,
        execution: Any,
        result: Any,
        loop_index: int,
        *,
        inline_limit_chars: int | None = None,
    ) -> dict[str, Any]:
        tool_name = tool_call.function.name
        limits = tool_result_limits_for_policy(self.context.runtime_policy)
        evidence, item = store_tool_result(
            session_id=self.context.session_id,
            user_id=self.context.user_id,
            turn_id=self.context.turn_id,
            tool_name=tool_name,
            tool_call_id=execution.tool_call_id,
            result=result,
            context_stewardship=limits["context_stewardship"],
        )
        message = build_tool_result_message(
            session_id=self.context.session_id,
            tool_call_id=execution.tool_call_id,
            provider_tool_call_id=execution.provider_tool_call_id,
            tool_name=tool_name,
            result=result,
            result_envelope=execution.result_envelope,
            inline_limit_chars=inline_limit_chars or limits["inline_limit_chars"],
            preview_chars=limits["preview_chars"],
            context_stewardship=limits["context_stewardship"],
        )
        message["_context_fabric"] = {
            "schema_version": 2,
            "turn_id": self.context.turn_id,
            "evidence_id": evidence.id,
            "context_item_id": item.id,
            "context_item_version": item.version,
            "content_hash": evidence.content_hash,
        }
        self._externalize_context_message(message, tool_name)
        self.context.messages.append(message)
        self.context.new_messages.append(message)
        self._apply_one_shot_completion(tool_name, result)
        self._emit_context_event(
            result,
            message,
            tool_call_id=execution.tool_call_id,
            tool_name=tool_name,
            loop_index=loop_index,
        )
        self._record_source_coverage(
            tool_call,
            result,
            message,
            tool_call_id=execution.tool_call_id,
            loop_index=loop_index,
        )
        return message

    def _prepare_described_capabilities(
        self,
        tool_call: Any,
        result: Any,
    ) -> Any:
        arguments = self._arguments(tool_call)
        if not arguments.get("prepare_tools"):
            return result
        prepared = self.context.prepare_direct_tools(
            arguments.get("capability_ids") or [],
            operation_groups=arguments.get("operation_groups") or [],
            max_tools=arguments.get("max_tools"),
        )
        if isinstance(result, dict):
            return {**result, "direct_tools_enabled": prepared}
        return {"result": result, "direct_tools_enabled": prepared}

    def _load_session_capabilities(self, result: dict[str, Any]) -> dict[str, Any]:
        loaded = result.get("capability_ids") or []
        for capability_id in loaded:
            if capability_id not in self.context.selected_capability_ids:
                self.context.selected_capability_ids.append(capability_id)
        prepared = self.context.prepare_direct_tools(
            loaded,
            operation_groups=["read"],
        )
        return {
            "result": "success",
            "message": "Capabilities loaded into session.",
            "direct_tools_enabled": prepared,
        }

    def _upgrade_session(self, result: dict[str, Any]) -> dict[str, Any]:
        reason = result.get("reason", "Session execution became too complex.")
        run = self.context.execute_run_tool(
            "runs_create",
            {
                "title": f"Upgraded Run: {reason[:40]}",
                "input": {
                    "reason": reason,
                    "transferred_from_session": True,
                },
                "understanding": {
                    "assessment": "Upgraded from inline session execution.",
                    "reason": reason,
                },
                "status": "draft",
            },
            self.context.user_id,
        )
        return {
            "result": "success",
            "message": (
                "Successfully upgraded session to a durable run. Please call "
                "runs_update_plan and runs_start."
            ),
            "run_data": run,
        }

    def _process_meta_capability(self, tool_call: Any, result: Any) -> Any:
        if tool_call.function.name == "capabilities_describe":
            return self._prepare_described_capabilities(tool_call, result)
        if not isinstance(result, dict):
            return result
        instruction = result.get("_instruction")
        if instruction == "load_to_session":
            return self._load_session_capabilities(result)
        if instruction == "upgrade_session":
            return self._upgrade_session(result)
        return result

    def _record_delegate_coverage(
        self,
        tool_call: Any,
        result: Any,
        message: dict[str, Any],
        execution: Any,
        loop_index: int,
    ) -> None:
        arguments = self._arguments(tool_call)
        try:
            items = extract_delegate_coverage_items(
                result,
                default_purpose=(
                    arguments.get("coverage_purpose")
                    or arguments.get("prompt")
                ),
                context_ref_id=(
                    message.get("_context_optimization") or {}
                ).get("ref_id"),
            )
        except Exception as exc:
            self.context.app.logger.warning(
                "Unable to extract delegate source coverage for %s: %s",
                tool_call.function.name,
                exc,
                exc_info=True,
            )
            return
        if not items:
            return
        coverage = upsert_source_coverage(
            {
                "items": items,
                "default_status": "inspected",
                "reason": "Automatically recorded from delegate_task result.",
            },
            self.context.session_id,
        )
        self._emit_coverage_result(
            coverage,
            tool_call_id=execution.tool_call_id,
            tool_name=tool_call.function.name,
            loop_index=loop_index,
        )

    def _process_delegate(
        self,
        tool_call: Any,
        execution: Any,
        result: Any,
        loop_index: int,
    ) -> None:
        message = self.append_context_message(
            tool_call,
            execution,
            result,
            loop_index,
        )
        self._record_delegate_coverage(
            tool_call,
            result,
            message,
            execution,
            loop_index,
        )

    def _load_model_capabilities(self) -> None:
        if self._model_capabilities_loaded:
            return
        record = LLMModel.query.filter_by(
            model_id=self.context.model_name
        ).first()
        self._supports_pdf = bool(record and record.supports_pdf)
        self._supports_vision = bool(record and record.supports_vision)
        self._model_capabilities_loaded = True

    def _can_view_data_uri(self, mime_type: str) -> bool:
        self._load_model_capabilities()
        if mime_type == "application/pdf":
            return self._supports_pdf
        return bool(mime_type.startswith("image/") and self._supports_vision)

    def _append_file_message(
        self,
        data_uri: str,
        mime_type: str,
        *,
        note: str,
    ) -> None:
        payload = (
            {"type": "file", "file": {"file_data": data_uri}}
            if mime_type == "application/pdf"
            else {"type": "image_url", "image_url": {"url": data_uri}}
        )
        message = {
            "role": "user",
            "content": [{"type": "text", "text": note}, payload],
        }
        self.context.messages.append(message)
        self.context.new_messages.append(message)

    @staticmethod
    def _attachment_messages(result: Any) -> tuple[Any, tuple[dict[str, Any], ...]]:
        clean_result, attachments = split_tool_output_attachments(result)
        attachment_message = build_tool_attachment_message(attachments)
        return (
            clean_result,
            (attachment_message,) if attachment_message else (),
        )

    def _process_view_attachment(
        self,
        tool_call: Any,
        execution: Any,
        result: Any,
        loop_index: int,
    ) -> tuple[dict[str, Any], ...]:
        if isinstance(result, str) and result.startswith("data:"):
            mime_type = result.split(";", 1)[0].split(":", 1)[1]
            if self._can_view_data_uri(mime_type):
                self.append_context_message(
                    tool_call,
                    execution,
                    "Content in next message.",
                    loop_index,
                )
                self._append_file_message(
                    result,
                    mime_type,
                    note="(System): Attached content:",
                )
            else:
                self.append_context_message(
                    tool_call,
                    execution,
                    {"error": "Unsupported format."},
                    loop_index,
                )
            return ()
        clean_result, attachments = self._attachment_messages(result)
        self.append_context_message(
            tool_call,
            execution,
            clean_result,
            loop_index,
        )
        return attachments

    def _process_default(
        self,
        tool_call: Any,
        execution: Any,
        result: Any,
        loop_index: int,
    ) -> tuple[dict[str, Any], ...]:
        is_pdf = bool(
            tool_call.function.name == "filesystem_view_pdf_file"
            and isinstance(result, str)
            and result.startswith("data:application/pdf")
        )
        if is_pdf and self._can_view_data_uri("application/pdf"):
            self.append_context_message(
                tool_call,
                execution,
                "Content below.",
                loop_index,
            )
            self._append_file_message(
                result,
                "application/pdf",
                note="Requested PDF:",
            )
            return ()
        clean_result, attachments = self._attachment_messages(result)
        self.append_context_message(
            tool_call,
            execution,
            clean_result,
            loop_index,
        )
        return attachments

    def process(
        self,
        tool_call: Any,
        execution: Any,
        loop_index: int,
    ) -> ToolCallProcessing:
        output = execution.output if isinstance(execution.output, dict) else {}
        action = str(output.get("action") or "")
        result = output.get("result")
        waiting = self._waiting(result)
        if action == "tool_budget_extension":
            result = self.context.apply_budget_extension(
                result,
                loop_index=loop_index,
            )
        if action == "meta_capability":
            result = self._process_meta_capability(tool_call, result)
        if action == "delegate":
            self._process_delegate(tool_call, execution, result, loop_index)
            return ToolCallProcessing(waiting_for_user=waiting)
        if action == "context_read":
            self.append_context_message(
                tool_call,
                execution,
                result,
                loop_index,
                inline_limit_chars=60_000,
            )
            return ToolCallProcessing(waiting_for_user=waiting)
        if action in self._CONTEXT_ACTIONS or action == "meta_capability":
            self.append_context_message(
                tool_call,
                execution,
                result,
                loop_index,
            )
            return ToolCallProcessing(waiting_for_user=waiting)
        if action == "view_attachment":
            attachments = self._process_view_attachment(
                tool_call,
                execution,
                result,
                loop_index,
            )
        else:
            attachments = self._process_default(
                tool_call,
                execution,
                result,
                loop_index,
            )
        return ToolCallProcessing(
            waiting_for_user=waiting,
            attachment_messages=attachments,
        )


def scratch_execution_succeeded(result: Any) -> bool:
    payload = result if isinstance(result, dict) else {}
    execution = (
        payload.get("execution")
        if isinstance(payload.get("execution"), dict)
        else {}
    )
    return bool(
        str(execution.get("status") or "").lower() == "completed"
        and execution.get("returncode") == 0
    )


def should_emit_tool_context_event(result: Any) -> bool:
    status = (
        str(result.get("status") or "").lower()
        if isinstance(result, dict)
        else ""
    )
    return status not in {"waiting_for_user", "waiting_for_approval"}


__all__ = [
    "ChatToolLoop",
    "ChatToolLoopHooks",
    "ChatToolLoopResult",
    "ChatToolResultContext",
    "ChatToolResultProcessor",
    "ToolCallProcessing",
    "scratch_execution_succeeded",
    "should_emit_tool_context_event",
]
