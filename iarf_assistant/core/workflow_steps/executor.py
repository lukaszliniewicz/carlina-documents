"""Decomposed workflow-step executor behind the legacy worker handler."""

from __future__ import annotations

import copy
import inspect
import logging
import uuid
from typing import Any

from flask import current_app

from carlina.core.runtime_policy import (
    MAX_TOOL_CALL_ITERATIONS,
    apply_runtime_policy_to_settings,
    clamp_tool_call_iterations,
)
from carlina.core.utc import utc_now_db
from iarf_assistant.chat_builtin_tools import (
    MANAGE_LONG_TERM_MEMORY_TOOL_DEF,
    MEMORIZE_FACT_TOOL_DEF,
    RECALL_FACT_TOOL_DEF,
    SEND_USER_ALERT_TOOL_DEF,
    handle_manage_long_term_memory,
    handle_memorize_fact,
    handle_recall_fact,
    handle_send_user_alert,
)
from iarf_assistant.chat_execution_helpers import (
    sanitize_llm_message,
    sanitize_messages_for_llm_context,
)
from iarf_assistant.core import workflow_worker as worker
from iarf_assistant.core.agent_events import (
    assistant_message_event,
    output_repair_event,
)
from iarf_assistant.core.agent_loop import message_content_text, message_tool_calls
from iarf_assistant.core.execution_kernel import (
    AgentEventSink,
    AgentLoopRunner,
    ContextAssembler,
    ToolAuthorityContext,
    ToolExecutor,
)
from iarf_assistant.core.run_orchestrator import (
    RUN_TOOL_NAMES,
    get_run_tool_declarations,
)
from iarf_assistant.core.runtime_contracts import tool_message_from_envelope
from iarf_assistant.core.runtime_data_store import (
    externalize_runtime_payload,
    read_runtime_data_ref,
)
from iarf_assistant.core.runtime_state import set_workflow_step_status
from iarf_assistant.core.settings_cache import get_settings_snapshot
from iarf_assistant.core.task_queue import JobCancelledError
from iarf_assistant.extensions import db
from iarf_assistant.models import RuntimeJob, User, WorkflowRun, WorkflowStep


def _load_step_and_run(payload: dict[str, Any]) -> tuple[WorkflowStep, WorkflowRun]:
    step_id = payload.get("step_id")
    run_id = payload.get("run_id")
    step = db.session.get(WorkflowStep, step_id)
    if step is None:
        raise ValueError(f"Step {step_id} not found")
    run = db.session.get(WorkflowRun, run_id)
    if run is None:
        raise ValueError(f"Run {run_id} not found")
    return step, run


def _preflight_result(
    step: WorkflowStep,
    run: WorkflowRun,
    job_id: str,
) -> dict[str, Any] | None:
    latest_job_id = worker._latest_step_job_id(step)
    if latest_job_id and str(job_id) != latest_job_id:
        logging.info(
            "Skipping stale workflow job %s for step %s (latest=%s).",
            job_id,
            step.id,
            latest_job_id,
        )
        return {
            "status": "skipped",
            "reason": "stale_job",
            "latest_job_id": latest_job_id,
        }
    run_status = str(run.status or "").lower()
    step_status = str(step.status or "").lower()
    if run_status == "paused":
        if step_status in {"planned", "ready", "queued", "running"}:
            step.status = "paused"
            db.session.commit()
        return {"status": "paused", "reason": "run_paused"}
    if run_status in worker.TERMINAL_RUN_STATUSES:
        if run_status == "cancelled" and step_status not in worker.TERMINAL_STEP_STATUSES:
            step.status = "cancelled"
            db.session.commit()
        return {"status": "skipped", "reason": f"run_{run_status}"}
    if step_status in worker.TERMINAL_STEP_STATUSES:
        return {"status": "skipped", "reason": f"step_{step_status}"}
    return None


def execute_workflow_step(payload: dict[str, Any], task_queue: Any, job_id: str):
    """Load, fence, execute, and finalize one workflow step."""

    app = current_app._get_current_object()
    step, run = _load_step_and_run(payload)
    preflight = _preflight_result(step, run, job_id)
    if preflight is not None:
        return preflight
    step.status = "running"
    step.started_at = step.started_at or utc_now_db()
    db.session.commit()
    settings: dict[str, Any] = {}
    try:
        executor = WorkflowStepExecution(
            app=app,
            addon_manager=app.addon_manager,
            step=step,
            run=run,
            user_id=step.created_by_user_id or run.created_by_user_id,
            task_queue=task_queue,
            job_id=job_id,
        )
        settings = executor.prepare()
        return executor.execute()
    except JobCancelledError as exc:
        set_workflow_step_status(step.id, "cancelled", error=str(exc))
        logging.info("Workflow step %s cancelled.", step.id)
        raise
    except Exception as exc:
        retry = worker._maybe_schedule_retry(
            step,
            error=exc,
            job_id=job_id,
            settings=settings,
        )
        if retry:
            return retry
        set_workflow_step_status(step.id, "failed", error=str(exc))
        logging.exception("Workflow step %s failed.", step.id)
        raise


class WorkflowStepExecution:
    """Stateful executor split into bounded setup, loop, tool, and output stages."""

    def __init__(
        self,
        *,
        app: Any,
        addon_manager: Any,
        step: WorkflowStep,
        run: WorkflowRun,
        user_id: int | None,
        task_queue: Any,
        job_id: str,
    ):
        self.app = app
        self.addon_manager = addon_manager
        self.step = step
        self.run = run
        self.user_id = user_id
        self.task_queue = task_queue
        self.job_id = str(job_id)
        self.settings: dict[str, Any] = {}
        self.runtime_policy: dict[str, Any] = {}
        self.messages: list[dict[str, Any]] = []
        self.response_message: Any = None
        self.finalization_attempted = False
        self.output_repair_attempted = False

    def prepare(self) -> dict[str, Any]:
        self._prepare_model()
        self.config = self._build_config()
        self.tools = self._build_tools()
        self.messages = self._initial_messages()
        self.execution_requirements = worker._collect_execution_requirements(self.step)
        self.max_tool_calls = self._max_tool_calls()
        self.loop_runner = AgentLoopRunner(
            max_tool_calls=self.max_tool_calls,
            event_sink=self.event_sink,
            warning_at_remaining=2,
            minimum_warning_budget=2,
            budget_increase_tool_name="request_budget_increase",
        )
        self.available_tool_names = self._available_tool_names()
        self.shared_executor = self._tool_executor(parallel=False)
        self.parallel_executor = self._tool_executor(parallel=True)
        self.parallel_tool_call_limit = self._parallel_tool_call_limit()
        self.response_format = worker._response_format_for_step(self.step)
        return self.settings

    def execute(self) -> dict[str, Any]:
        paused = self._run_agent_loop()
        if paused:
            return paused
        return self._finalize_output()

    def _prepare_model(self) -> None:
        profile = worker._resolve_agent_profile(
            self.addon_manager,
            self.step.assigned_model_role,
        )
        model_role = (
            profile.model_class
            if profile and profile.model_class
            else self.step.assigned_model_role
        )
        self.target_model = worker.ModelRouter.resolve_model(
            model_role,
            user_id=self.user_id,
        )
        self.settings = get_settings_snapshot()
        step_metadata = self._metadata(self.step)
        run_metadata = self._metadata(self.run)
        self.settings, self.runtime_policy = apply_runtime_policy_to_settings(
            self.settings,
            step_metadata.get("runtime_policy") or run_metadata.get("runtime_policy"),
        )
        self.system_prompt = worker._build_worker_system_prompt(
            self.addon_manager,
            self.step,
            profile,
            run=self.run,
            settings=self.settings,
        )
        self.profile = profile
        self.turn_id = str(uuid.uuid4())
        self.runtime_job_id = self._persisted_runtime_job_id()
        self.llm_service = worker.LLMService(
            user_id=self.user_id,
            cost_attribution=self._cost_attribution(),
        )
        self.event_sink = AgentEventSink(
            persist=lambda packet: worker._append_step_runtime_event(self.step, packet),
            runtime_context=self._runtime_context(),
        )
        self.context_assembler = ContextAssembler(
            "worker",
            message_sanitizer=sanitize_messages_for_llm_context,
        )

    @staticmethod
    def _metadata(record: Any) -> dict[str, Any]:
        value = record.metadata_json
        return value if isinstance(value, dict) else {}

    def _persisted_runtime_job_id(self) -> str | None:
        if self.job_id and db.session.get(RuntimeJob, self.job_id):
            return self.job_id
        return None

    def _cost_attribution(self) -> dict[str, Any]:
        return {
            "chat_session_id": self.run.chat_session_id,
            "workflow_run_id": self.run.id,
            "workflow_step_id": self.step.id,
            "runtime_job_id": self.runtime_job_id,
            "turn_id": self.turn_id,
        }

    def _runtime_context(self) -> dict[str, Any]:
        return {
            "role": "worker",
            "workflow_run_id": self.run.id,
            "workflow_step_id": self.step.id,
            "runtime_job_id": self.runtime_job_id,
            "turn_id": self.turn_id,
        }

    def _build_tools(self) -> list[dict[str, Any]]:
        tools = [
            *get_run_tool_declarations(),
            worker._update_shared_memory_tool_def(),
            worker._read_shared_memory_tool_def(),
            worker._runtime_read_data_ref_tool_def(),
            worker._request_budget_increase_tool_def(),
            MEMORIZE_FACT_TOOL_DEF,
            RECALL_FACT_TOOL_DEF,
            MANAGE_LONG_TERM_MEMORY_TOOL_DEF,
            SEND_USER_ALERT_TOOL_DEF,
        ]
        if self.step.capability_ids and hasattr(
            self.addon_manager, "resolve_toolkit_plugin_ids"
        ):
            prefixes = self.addon_manager.resolve_toolkit_plugin_ids(
                self.step.capability_ids
            )
            if hasattr(self.addon_manager, "get_tool_declarations_for_plugin_ids"):
                tools.extend(
                    self.addon_manager.get_tool_declarations_for_plugin_ids(
                        prefixes,
                        self.config,
                    )
                )
        skill_ids = worker._resolve_skill_ids(
            self.addon_manager,
            [
                *(self.profile.skill_ids if self.profile else []),
                *(self.step.skill_ids or []),
            ],
        )
        for skill_id in skill_ids:
            if hasattr(self.addon_manager, "get_skill_tool_declarations"):
                tools.extend(self.addon_manager.get_skill_tool_declarations(skill_id))
        tool_ids = worker._unique(
            [
                *(self.profile.tool_ids if self.profile else []),
                *(self.step.tool_ids or []),
            ]
        )
        if tool_ids and hasattr(self.addon_manager, "registries"):
            tools.extend(
                self.addon_manager.registries.tools.generate_tool_declarations(tool_ids)
            )
        return worker._unique_tool_declarations(tools)

    def _build_config(self) -> dict[str, Any]:
        config = {
            "current_user_id": self.user_id,
            "db_session": db.session,
            "current_model": self.target_model,
            "app_root_path": self.app.root_path,
            "runtime_policy": self.runtime_policy,
        }
        user = db.session.get(User, self.user_id) if self.user_id else None
        for addon_id, addon_settings in (user.addon_settings or {}).items() if user else ():
            for key, value in addon_settings.items():
                config[f"{addon_id}_{key}"] = value
        return config

    def _initial_messages(self) -> list[dict[str, Any]]:
        metadata = self._metadata(self.step)
        if "messages" in metadata:
            messages = list(metadata["messages"])
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "The workflow step has been resumed. Any pending approvals "
                        "have been resolved (either approved or handled). Please "
                        "proceed with the execution."
                    ),
                }
            )
            return messages
        return [
            {"role": "system", "content": self.system_prompt},
            {
                "role": "user",
                "content": (
                    f"Execute the following step:\nTitle: {self.step.title}\n"
                    f"Input: {worker.json.dumps(self.step.input_payload or {})}"
                ),
            },
        ]

    def _max_tool_calls(self) -> int:
        raw = self.settings.get("max_tool_calls", "30")
        step_metadata = self._metadata(self.step)
        run_metadata = self._metadata(self.run)
        override = step_metadata.get("max_tool_calls")
        if override is None:
            override = run_metadata.get("max_tool_calls")
        return clamp_tool_call_iterations(
            override if override is not None else raw,
        )

    def _available_tool_names(self) -> set[str]:
        return {
            declaration.get("function", {}).get("name")
            for declaration in self.tools
            if isinstance(declaration, dict)
            and isinstance(declaration.get("function"), dict)
            and declaration["function"].get("name")
        }

    def _current_messages(self) -> list[dict[str, Any]]:
        assembly = self.context_assembler.assemble(
            self.messages,
            metadata=self._runtime_context(),
        )
        metadata = copy.deepcopy(self._metadata(self.step))
        metadata["execution_kernel"] = {
            "context_assembly": assembly.to_dict(),
            "result_envelope_version": 1,
        }
        self.step.metadata_json = metadata
        return list(assembly.messages)

    def _tool_record(self, name: str):
        if not hasattr(self.addon_manager, "get_tool_record"):
            return None
        return self.addon_manager.get_tool_record(name)

    def _validate_tool_arguments(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        if not hasattr(self.addon_manager, "validate_registered_tool_call"):
            return dict(arguments or {})
        return self.addon_manager.validate_registered_tool_call(name, arguments)

    def _tool_availability(self, name: str, _arguments: dict[str, Any]):
        if name in self.available_tool_names:
            return None
        return {
            "status": "blocked",
            "policy": "tool_not_declared_for_worker",
            "error": f"Tool '{name}' is not declared for this workflow worker.",
        }

    def _dispatch_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        _prepared: Any,
    ) -> dict[str, Any]:
        db.session.commit()
        active_run = db.session.get(WorkflowRun, self.run.id)
        active_step = db.session.get(WorkflowStep, self.step.id)
        active_user = db.session.get(User, self.user_id) if self.user_id else None
        if active_run is None or active_step is None:
            raise RuntimeError("Workflow run or step disappeared during tool execution.")
        builtin = self._dispatch_builtin(name, arguments, active_run, active_step)
        if builtin is not None:
            return builtin
        if name in RUN_TOOL_NAMES:
            return self._dispatch_run_tool(
                name,
                arguments,
                active_run,
                active_step,
                active_user,
            )
        result = self.addon_manager._execute_registered_tool_raw(
            name,
            arguments,
            self.config,
        )
        if inspect.isgenerator(result):
            result = next(
                (
                    row
                    for row in result
                    if isinstance(row, dict) and row.get("type") in {"finish", "error"}
                ),
                {"status": "success", "message": "Streaming operation completed."},
            )
        return {"action": "standard", "result": result}

    def _dispatch_builtin(
        self,
        name: str,
        arguments: dict[str, Any],
        active_run: WorkflowRun,
        active_step: WorkflowStep,
    ) -> dict[str, Any] | None:
        handlers = {
            "update_shared_memory": lambda: self._memory_result(
                worker._update_shared_memory(active_run, arguments)
            ),
            "read_shared_memory": lambda: self._memory_result(
                worker._read_shared_memory(active_run, arguments)
            ),
            "runtime_read_data_ref": lambda: self._read_data_ref(
                arguments,
                active_run,
            ),
            "request_budget_increase": lambda: self._request_budget_increase(
                arguments,
                active_run,
                active_step,
            ),
            "send_user_alert": lambda: self._send_user_alert(arguments),
            "memorize_fact": lambda: self._memorize_fact(arguments),
            "recall_fact": lambda: self._recall_fact(arguments),
            "manage_long_term_memory": lambda: self._manage_memory(arguments),
        }
        handler = handlers.get(name)
        return handler() if handler else None

    @staticmethod
    def _memory_result(result: Any) -> dict[str, Any]:
        return {"action": "memory", "result": result}

    def _read_data_ref(
        self,
        arguments: dict[str, Any],
        active_run: WorkflowRun,
    ) -> dict[str, Any]:
        return {
            "action": "context_read",
            "result": read_runtime_data_ref(
                arguments.get("data_ref_id"),
                instance_path=self.app.instance_path,
                owner_user_id=self.user_id,
                workflow_run_id=active_run.id,
                start_offset=arguments.get("start_offset", 0),
                max_chars=arguments.get("max_chars", 8_000),
            ),
        }

    def _request_budget_increase(
        self,
        arguments: dict[str, Any],
        active_run: WorkflowRun,
        active_step: WorkflowStep,
    ) -> dict[str, Any]:
        from iarf_assistant.core.runtime_state import create_approval_request

        requested = clamp_tool_call_iterations(
            arguments.get("requested_increase", 20),
            default=20,
        )
        remaining_headroom = MAX_TOOL_CALL_ITERATIONS - clamp_tool_call_iterations(
            self.max_tool_calls,
        )
        if remaining_headroom < 1:
            return {
                "action": "budget_increase",
                "result": {
                    "status": "blocked",
                    "policy": "tool_budget_ceiling_reached",
                    "message": "The server tool-call ceiling has already been reached.",
                    "maximum_tool_calls": MAX_TOOL_CALL_ITERATIONS,
                },
            }
        approved_request = min(requested, remaining_headroom)

        approval = create_approval_request(
            requester_user_id=self.user_id,
            subject_type="budget_increase",
            subject_id=active_step.id,
            preview={
                "requested_increase": approved_request,
                "maximum_tool_calls": MAX_TOOL_CALL_ITERATIONS,
                "reason": arguments.get("reason", "No reason provided"),
            },
            workflow_run_id=active_run.id,
            commit=False,
        )
        return {
            "action": "budget_increase",
            "result": {
                "status": "waiting_for_approval",
                "approval_id": approval.id,
                "message": (
                    "The requested workflow tool budget increase is waiting for "
                    "approval."
                ),
            },
        }

    def _send_user_alert(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "action": "user_alert",
            "result": handle_send_user_alert(
                arguments.get("title"),
                arguments.get("message"),
                arguments.get("action_url"),
                self.user_id,
            ),
        }

    def _memorize_fact(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "action": "memory_fact",
            "result": handle_memorize_fact(
                arguments.get("scope"),
                arguments.get("fact"),
                self.user_id,
                category=arguments.get("category"),
                tier=arguments.get("tier"),
                importance=arguments.get("importance"),
                tier_locked=arguments.get("tier_locked"),
                source="workflow_model",
            ),
        }

    def _recall_fact(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "action": "memory_fact",
            "result": handle_recall_fact(
                arguments.get("query"),
                self.user_id,
                scope=arguments.get("scope"),
                category=arguments.get("category"),
                include_tier2=arguments.get("include_tier2"),
                max_results=arguments.get("max_results"),
            ),
        }

    def _manage_memory(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "action": "memory_fact",
            "result": handle_manage_long_term_memory(
                arguments.get("action"),
                arguments.get("scope"),
                self.user_id,
                memory_id=arguments.get("memory_id"),
                fact=arguments.get("fact"),
                query=arguments.get("query"),
                limit=arguments.get("limit"),
                category=arguments.get("category"),
                tier=arguments.get("tier"),
                include_tier2=arguments.get("include_tier2"),
                importance=arguments.get("importance"),
                tier_locked=arguments.get("tier_locked"),
            ),
        }

    def _dispatch_run_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        active_run: WorkflowRun,
        active_step: WorkflowStep,
        active_user: User | None,
    ) -> dict[str, Any]:
        from iarf_assistant.core.run_orchestrator import execute_run_tool

        run_arguments = worker._populate_run_tool_context(
            name,
            arguments,
            run=active_run,
            step=active_step,
        )
        return {
            "action": "runtime_run",
            "result": execute_run_tool(
                name,
                run_arguments,
                user_id=self.user_id,
                is_admin=active_user.is_admin if active_user else False,
                addon_manager=self.addon_manager,
                tool_config=self.config,
                chat_session_id=active_run.chat_session_id,
                enforced_run_id=active_run.id,
                enforced_step_id=active_step.id,
            ),
        }

    def _externalize_result(self, prepared: Any, raw: str, result: Any):
        return externalize_runtime_payload(
            raw,
            instance_path=self.app.instance_path,
            namespace=f"workflow-{self.run.id}",
            created_by_user_id=self.user_id,
            workflow_run_id=self.run.id,
            workflow_step_id=self.step.id,
            runtime_job_id=self.runtime_job_id,
            label=f"Workflow tool result: {prepared.tool_name}",
            kind="runtime_tool_result_payload",
            lineage={
                **self._runtime_context(),
                "tool_call_id": prepared.tool_call_id,
                "tool_name": prepared.tool_name,
            },
            source_ids=result.get("source_ids") if isinstance(result, dict) else None,
            evidence_ids=result.get("evidence_ids")
            if isinstance(result, dict)
            else None,
            commit=False,
        )

    def _tool_timeout(self) -> float | None:
        try:
            timeout = float(self.settings.get("runtime_tool_timeout_seconds") or 300)
        except (TypeError, ValueError):
            timeout = 300.0
        return max(1.0, timeout) if timeout > 0 else None

    def _authority(self):
        return ToolAuthorityContext(
            requester_user_id=self.user_id,
            runtime_policy=self.runtime_policy,
            audit_log_source_id="jobs.runtime",
            log_source_ids=("jobs.runtime",),
            workflow_run_id=self.run.id,
            workflow_step_id=self.step.id,
            runtime_job_id=self.runtime_job_id,
        )

    def _tool_executor(self, *, parallel: bool):
        kwargs = {
            "dispatcher": self._dispatch_tool,
            "event_sink": self.event_sink,
            "authority": self._authority(),
            "tool_record_resolver": self._tool_record,
            "argument_validator": self._validate_tool_arguments,
            "availability_checker": self._tool_availability,
            "result_externalizer": self._externalize_result,
            "timeout_seconds": self._tool_timeout(),
        }
        if parallel:
            kwargs["execution_context_factory"] = self.app.app_context
        else:
            kwargs["inline_execution"] = True
        return ToolExecutor(**kwargs)

    def _parallel_tool_call_limit(self) -> int:
        try:
            raw = self._metadata(self.step).get("parallel_tool_calls") or 4
            return max(1, min(int(raw), 10))
        except (TypeError, ValueError):
            return 4

    def _request_finalization(
        self,
        reason: str,
        loop_index: int | None = None,
    ) -> Any:
        self.finalization_attempted = True
        request_message = self.loop_runner.begin_finalization(
            self.messages,
            reason=reason,
            loop_index=loop_index,
        )
        request_message.update(
            worker._workflow_finalization_request_message(
                self.step,
                reason=reason,
                max_tool_calls=(
                    self.max_tool_calls if reason == "tool_call_limit" else None
                ),
            )
        )
        current_messages = self._current_messages()
        db.session.commit()
        response = self.llm_service.completion(
            model=self.target_model,
            messages=current_messages,
            tools=None,
            response_format=self.response_format,
            source="workflow_step_finalization",
        )
        self.response_message = response.choices[0].message
        clean = sanitize_llm_message(self.response_message)
        self.messages.append(clean)
        self.event_sink.emit(assistant_message_event(clean, loop_index=loop_index))
        content = message_content_text(clean)
        self.loop_runner.finish_finalization(
            reason=reason,
            content=content,
            loop_index=loop_index,
        )
        return self.response_message

    def _request_output_repair(
        self,
        *,
        invalid_text: str | None,
        error: Exception,
        loop_index: int | None = None,
    ) -> Any:
        self.output_repair_attempted = True
        self.messages.append(
            worker._workflow_output_repair_request_message(
                self.step,
                invalid_text=invalid_text,
                error_message=str(error),
            )
        )
        self.event_sink.emit(
            output_repair_event(
                status="running",
                reason="invalid_json",
                loop_index=loop_index,
                content_preview=invalid_text or "",
                error=str(error),
            )
        )
        current_messages = self._current_messages()
        db.session.commit()
        response = self.llm_service.completion(
            model=self.target_model,
            messages=current_messages,
            tools=None,
            response_format=self.response_format,
            source="workflow_step_output_repair",
        )
        self.response_message = response.choices[0].message
        clean = sanitize_llm_message(self.response_message)
        self.messages.append(clean)
        self.event_sink.emit(assistant_message_event(clean, loop_index=loop_index))
        repaired = message_content_text(clean)
        self.event_sink.emit(
            output_repair_event(
                status="completed" if repaired.strip() else "error",
                reason="invalid_json",
                loop_index=loop_index,
                content_preview=repaired,
                error="" if repaired.strip() else "Repair response was empty.",
            )
        )
        return self.response_message

    def _run_agent_loop(self) -> dict[str, Any] | None:
        for turn_index in range(self.max_tool_calls):
            self.loop_runner.prepare_iteration(self.messages, turn_index)
            if self.task_queue and self.task_queue.is_cancel_requested(self.job_id):
                raise JobCancelledError("Workflow step job was cancelled.")
            current_messages = self._current_messages()
            db.session.commit()
            response = self.llm_service.completion(
                model=self.target_model,
                messages=current_messages,
                tools=self.tools or None,
                response_format=self.response_format,
                source="workflow_step",
            )
            self.response_message = response.choices[0].message
            clean = sanitize_llm_message(self.response_message)
            self.messages.append(clean)
            self.event_sink.emit(
                assistant_message_event(clean, loop_index=turn_index)
            )
            tool_calls = message_tool_calls(self.response_message)
            if not tool_calls:
                if self._request_missing_requirements():
                    continue
                break
            paused = self._execute_tool_calls(tool_calls, turn_index)
            if paused:
                return paused
            budget = self.loop_runner.after_tool_calls(
                turn_index,
                has_tool_calls=bool(tool_calls),
            )
            if budget.should_force_finalization:
                self._request_finalization("tool_call_limit", loop_index=turn_index)
                break
        return None

    def _request_missing_requirements(self) -> bool:
        missing = worker._missing_execution_requirements(
            self.run,
            self.step,
            self.execution_requirements,
        )
        if not missing:
            return False
        self.messages.append(
            {"role": "system", "content": worker._missing_requirements_prompt(missing)}
        )
        return True

    def _execute_tool_calls(
        self,
        tool_calls: list[Any],
        turn_index: int,
    ) -> dict[str, Any] | None:
        if worker._worker_tool_calls_parallel_safe(
            tool_calls,
            tool_record_resolver=self._tool_record,
            parallel_tool_call_limit=self.parallel_tool_call_limit,
        ):
            executed_calls = self.parallel_executor.execute_calls(
                tool_calls,
                loop_index=turn_index,
                max_workers=min(self.parallel_tool_call_limit, len(tool_calls)),
            )
            for executed in executed_calls:
                paused = self._record_tool_result(executed)
                if paused:
                    return paused
            return None
        for tool_call in tool_calls:
            executed = self.shared_executor.execute_call(
                tool_call,
                loop_index=turn_index,
            )
            paused = self._record_tool_result(executed)
            if paused:
                return paused
        return None

    def _record_tool_result(self, executed: Any) -> dict[str, Any] | None:
        self.messages.append(
            tool_message_from_envelope(
                executed.result_envelope or {},
                result=executed.result,
                provider_tool_call_id=executed.provider_tool_call_id,
            )
        )
        status = (
            str(executed.result.get("status") or "").lower()
            if isinstance(executed.result, dict)
            else ""
        )
        if status != "waiting_for_approval":
            return None
        approval_id = executed.result.get("approval_id")
        if approval_id and approval_id not in (self.step.approval_ids or []):
            self.step.approval_ids = [*(self.step.approval_ids or []), approval_id]
        self.step.status = "waiting_for_approval"
        self.step.metadata_json = {
            **self._metadata(self.step),
            "messages": self.messages,
        }
        db.session.commit()
        return {
            "status": "paused",
            "reason": "waiting_for_approval",
            "approval_id": approval_id,
        }

    def _finalize_output(self) -> dict[str, Any]:
        if self.response_message is None:
            raise ValueError("Workflow step execution produced no model response.")
        if not self.finalization_attempted and self.loop_runner.needs_finalization(
            self.response_message
        ):
            self._request_finalization("empty_assistant_response")
        final_text = worker._message_content(self.response_message)
        try:
            output = worker._output_payload_for_step(self.step, final_text)
        except ValueError as error:
            output = self._repair_invalid_output(final_text, error)
        missing = worker._missing_execution_requirements(
            self.run,
            self.step,
            self.execution_requirements,
        )
        if missing:
            raise ValueError(
                "Workflow step completed without required side effects: "
                + ", ".join(missing)
            )
        worker._clear_retry_state(self.step)
        set_workflow_step_status(
            self.step.id,
            "completed",
            output_payload=output,
        )
        return {"status": "completed", "result": output}

    def _repair_invalid_output(
        self,
        invalid_text: str,
        error: ValueError,
    ) -> dict[str, Any]:
        if not (
            self.step.output_schema
            and not self.output_repair_attempted
            and worker._is_invalid_json_output_error(error)
        ):
            raise error
        self._request_output_repair(invalid_text=invalid_text, error=error)
        repaired_text = worker._message_content(self.response_message)
        try:
            return worker._output_payload_for_step(self.step, repaired_text)
        except ValueError as repair_error:
            self.event_sink.emit(
                output_repair_event(
                    status="error",
                    reason="invalid_json",
                    content_preview=repaired_text or "",
                    error=str(repair_error),
                )
            )
            raise


__all__ = ["WorkflowStepExecution", "execute_workflow_step"]
