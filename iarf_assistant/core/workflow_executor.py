import logging
from datetime import datetime, timedelta, timezone

from carlina.core.runtime_policy import clamp_tool_call_iterations
from iarf_assistant.core.runtime_contracts import (
    build_tool_result_envelope,
    stable_json,
)
from iarf_assistant.core.runtime_data_store import externalize_runtime_payload
from iarf_assistant.core.runtime_state import (
    evaluate_runtime_checkpoints,
    set_workflow_run_status,
)
from iarf_assistant.core.task_queue import TaskQueue
from iarf_assistant.extensions import db
from iarf_assistant.models import (
    RuntimeApprovalRequest,
    RuntimeJob,
    Setting,
    WorkflowRun,
    WorkflowStep,
)


class WorkflowOrchestrator:
    """
    Background daemon intended to run via APScheduler.
    Polls the database for active WorkflowRuns, evaluates the DAG of steps based on dependencies,
    handles routing, parallel branching, and dispatches them as RuntimeJobs.
    """
    def __init__(self, app):
        self.app = app
        # Use a dedicated task queue for workflow orchestration
        self.queue = TaskQueue(app.instance_path, "core_workflow")

    def close(self):
        self.queue.close()

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback):
        self.close()
        return False

    def close_on_scheduler_event(self, _event=None):
        self.close()

    def poll_and_dispatch(self):
        with self.app.app_context():
            try:
                # 1. Resume runs that have resolved approvals
                self._resume_approved_runs()
                self._recover_stale_steps()
                evaluate_runtime_checkpoints()
                
                # 2. Check for runs that are in 'running' state
                runs = WorkflowRun.query.filter_by(status='running').all()
                for run in runs:
                    self._process_run(run)
            except Exception as e:
                logging.error(f"Error in WorkflowOrchestrator poll_and_dispatch: {e}")

    def _resume_approved_runs(self):
        # Find steps that are waiting for approval but the approval is now resolved
        waiting_steps = WorkflowStep.query.filter_by(status='waiting_for_approval').all()
        for step in waiting_steps:
            # Check associated approvals
            if not step.approval_ids:
                continue
            all_resolved = True
            any_rejected = False
            for approval_id in step.approval_ids:
                approval = db.session.get(RuntimeApprovalRequest, approval_id)
                if not approval or approval.status == 'pending':
                    all_resolved = False
                    break
                if approval.status in ('rejected', 'cancelled', 'expired'):
                    any_rejected = True
                    break
            
            if all_resolved:
                if any_rejected:
                    step.status = 'failed'
                    step.error = "Approval rejected or cancelled."
                else:
                    # Approvals granted, transition back to queued and dispatch
                    # Check if any approved requests were for turn budget increases
                    for approval_id in step.approval_ids:
                        approval = db.session.get(RuntimeApprovalRequest, approval_id)
                        if approval and approval.subject_type == 'budget_increase' and approval.status == 'approved':
                            preview = approval.preview_payload or {}
                            inc = preview.get("requested_increase", 0)
                            try:
                                requested_increase = int(inc)
                            except (ValueError, TypeError):
                                requested_increase = 0
                            
                            if requested_increase > 0:
                                max_tool_calls_setting = db.session.get(Setting, 'max_tool_calls')
                                default_limit = clamp_tool_call_iterations(
                                    max_tool_calls_setting.value
                                    if max_tool_calls_setting
                                    and max_tool_calls_setting.value
                                    else 30
                                )
                                
                                metadata = step.metadata_json or {}
                                current_limit = metadata.get("max_tool_calls")
                                if current_limit is None:
                                    current_limit = default_limit
                                else:
                                    current_limit = clamp_tool_call_iterations(
                                        current_limit,
                                        default=default_limit,
                                    )
                                
                                new_limit = clamp_tool_call_iterations(
                                    current_limit + requested_increase,
                                    default=current_limit,
                                )
                                step.metadata_json = {
                                    **metadata,
                                    "max_tool_calls": new_limit,
                                }
                                logging.info(
                                    f"Applied turn limit increase of +{requested_increase} to step {step.id} "
                                    f"(new max_tool_calls={new_limit})."
                                )
                    
                    step.status = 'queued'
                    run = db.session.get(WorkflowRun, step.workflow_run_id)
                    if run is None:
                        step.status = "failed"
                        step.error = "The workflow run no longer exists."
                        logging.error(
                            "Cannot resume workflow step %s because run %s is missing.",
                            step.id,
                            step.workflow_run_id,
                        )
                        continue
                    self._dispatch_step(run, step)
                db.session.commit()

    def _process_run(self, run: WorkflowRun):
        # Determine runnable steps based on a DAG.
        # A step is runnable if its status is 'planned' and all dependency steps are 'completed'.
        
        all_steps = (
            WorkflowStep.query
            .filter_by(workflow_run_id=run.id)
            .order_by(WorkflowStep.sequence.asc(), WorkflowStep.created_at.asc())
            .all()
        )
        steps_by_id = {step.id: step for step in all_steps}
        steps_by_key = {step.step_key: step for step in all_steps if step.step_key}
        
        # Check if the run has failed steps
        failed_steps = [s for s in all_steps if s.status == 'failed']
        fatal_failed_steps = [
            step for step in failed_steps if not self._continue_on_failure(step)
        ]
        if fatal_failed_steps:
            # Mark run as failed
            updated = set_workflow_run_status(run.id, 'failed')
            logging.info(f"WorkflowRun {run.id} finished with status: {updated.status}")
            return
            
        waiting_steps = [s for s in all_steps if s.status in ['waiting_for_approval', 'waiting_for_review']]
        running_steps = [s for s in all_steps if s.status in ['queued', 'running']]
        
        # Find 'planned' steps whose dependencies are met
        completed_step_ids = {s.id for s in all_steps if s.status == 'completed'}
        tolerated_terminal_step_ids = {
            step.id
            for step in all_steps
            if step.status in {"failed", "cancelled", "skipped"}
            and self._continue_on_failure(step)
        }
        runnable_steps = []
        
        for step in all_steps:
            if step.status == 'planned':
                if not self._retry_due(step):
                    continue
                dependency_ids = self._dependency_ids(step, steps_by_id=steps_by_id, steps_by_key=steps_by_key)
                missing_dependencies = [dependency_id for dependency_id in dependency_ids if dependency_id not in steps_by_id]
                if missing_dependencies:
                    step.status = 'failed'
                    step.error = f"Missing dependency step(s): {', '.join(missing_dependencies)}"
                    db.session.commit()
                    continue
                allowed_dependency_ids = set(completed_step_ids)
                if self._allow_failed_dependencies(step):
                    allowed_dependency_ids.update(tolerated_terminal_step_ids)
                if not dependency_ids or all(dependency_id in allowed_dependency_ids for dependency_id in dependency_ids):
                    self._pipe_dependency_outputs(step, [steps_by_id[dependency_id] for dependency_id in dependency_ids])
                    runnable_steps.append(step)

        failed_steps = [step for step in all_steps if step.status == "failed"]
        fatal_failed_steps = [
            step for step in failed_steps if not self._continue_on_failure(step)
        ]
        if fatal_failed_steps:
            updated = set_workflow_run_status(run.id, "failed")
            logging.info(
                "WorkflowRun %s finished with status %s after dependency validation.",
                run.id,
                updated.status,
            )
            return

        if runnable_steps:
            # Dispatch independent runnable steps up to the run-wide worker ceiling.
            available_slots = max(
                0,
                self._run_concurrency_limit(run) - len(running_steps),
            )
            for step in runnable_steps[:available_slots]:
                self._dispatch_step(run, step)
        elif not running_steps and not waiting_steps:
            # No steps are running, waiting, or runnable. If all are completed (or skipped), run is done.
            planned_steps = [s for s in all_steps if s.status == 'planned']
            if not planned_steps:
                if failed_steps:
                    metadata = (
                        dict(run.metadata_json)
                        if isinstance(run.metadata_json, dict)
                        else {}
                    )
                    metadata["completion_outcome"] = "completed_with_item_failures"
                    metadata["failed_item_count"] = len(failed_steps)
                    metadata["failed_step_ids"] = [step.id for step in failed_steps]
                    run.metadata_json = metadata
                updated = set_workflow_run_status(run.id, 'completed')
                logging.info(f"WorkflowRun {run.id} finished with status: {updated.status}")

    def _dispatch_step(self, run: WorkflowRun, step: WorkflowStep):
        # We assume model-role routing is handled by the worker fetching the model.
        # But we could theoretically resolve it here and assign it to the step metadata if we wanted strict orchestration.
        # Currently, model_router.py is used inside workflow_worker.py, which is correct for just-in-time routing.
        
        step.status = 'queued'
        metadata = step.metadata_json if isinstance(step.metadata_json, dict) else {}
        if metadata.get("last_stale_recovery_at"):
            step.error = None
        retry_state = metadata.get("retry_state") if isinstance(metadata.get("retry_state"), dict) else None
        if retry_state:
            updated_retry_state = dict(retry_state)
            updated_retry_state.pop("next_retry_at", None)
            updated_retry_state["last_dispatched_at"] = datetime.now(timezone.utc).isoformat()
            step.metadata_json = {**metadata, "retry_state": updated_retry_state}
        
        # Enqueue with retries
        job_id = self.queue.enqueue(
            task_name="execute_workflow_step",
            payload={"step_id": step.id, "run_id": run.id}
        )
        step.job_ids = (step.job_ids or []) + [job_id]
        
        run.current_step = step.id
        db.session.commit()
        logging.info(f"Dispatched step {step.id} as job {job_id} for run {run.id}")

    @staticmethod
    def _continue_on_failure(step: WorkflowStep) -> bool:
        metadata = step.metadata_json if isinstance(step.metadata_json, dict) else {}
        return metadata.get("continue_on_failure") is True

    @staticmethod
    def _allow_failed_dependencies(step: WorkflowStep) -> bool:
        metadata = step.metadata_json if isinstance(step.metadata_json, dict) else {}
        return metadata.get("allow_failed_dependencies") is True

    @staticmethod
    def _run_concurrency_limit(run: WorkflowRun) -> int:
        metadata = run.metadata_json if isinstance(run.metadata_json, dict) else {}
        raw_compiled = metadata.get("compiled_run")
        compiled = raw_compiled if isinstance(raw_compiled, dict) else {}
        raw_concurrency = compiled.get("concurrency")
        concurrency = raw_concurrency if isinstance(raw_concurrency, dict) else {}
        raw_limit = concurrency.get("max_workers") or metadata.get("max_concurrency") or 4
        try:
            return max(1, min(int(raw_limit), 32))
        except (TypeError, ValueError):
            return 4

    def _recover_stale_steps(self) -> None:
        """Reopen abandoned queued/running steps; a newer job id fences stale jobs."""
        try:
            setting = db.session.get(Setting, "workflow_step_stale_seconds")
            default_seconds = int(setting.value) if setting and setting.value else 900
        except (TypeError, ValueError):
            default_seconds = 900
        now = datetime.now(timezone.utc)
        changed = False
        candidates = WorkflowStep.query.filter(
            WorkflowStep.status.in_(["queued", "running"])
        ).all()
        for step in candidates:
            run = db.session.get(WorkflowRun, step.workflow_run_id)
            if not run or str(run.status or "").lower() != "running":
                continue
            run_metadata = run.metadata_json if isinstance(run.metadata_json, dict) else {}
            try:
                stale_seconds = max(
                    30,
                    min(
                        int(run_metadata.get("stale_step_seconds") or default_seconds),
                        86400,
                    ),
                )
            except (TypeError, ValueError):
                stale_seconds = 900
            timestamp = step.updated_at or step.started_at or step.created_at
            if timestamp is None:
                timestamp = datetime.min
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            age = now - timestamp.astimezone(timezone.utc)

            latest_job_id = str((step.job_ids or [])[-1]) if step.job_ids else None
            job = db.session.get(RuntimeJob, latest_job_id) if latest_job_id else None
            job_status = str(job.status or "").lower() if job else "missing"
            terminal_job = job_status in {"failed", "cancelled"}
            if not terminal_job and age < timedelta(seconds=stale_seconds):
                continue

            metadata = dict(step.metadata_json) if isinstance(step.metadata_json, dict) else {}
            recoveries = int(metadata.get("stale_recovery_count") or 0) + 1
            metadata.update({
                "stale_recovery_count": recoveries,
                "last_stale_recovery_at": now.isoformat(),
                "recovered_job_id": latest_job_id,
                "recovered_job_status": job_status,
            })
            step.metadata_json = metadata
            step.status = "planned"
            step.error = (
                f"Recovered abandoned workflow job {latest_job_id or '(missing)'} "
                f"from status {job_status}; redispatch pending."
            )
            step.updated_at = now.replace(tzinfo=None)
            changed = True
        if changed:
            db.session.commit()

    @staticmethod
    def _retry_due(step: WorkflowStep) -> bool:
        metadata = step.metadata_json if isinstance(step.metadata_json, dict) else {}
        raw_retry_state = metadata.get("retry_state")
        retry_state = raw_retry_state if isinstance(raw_retry_state, dict) else {}
        retry_at_raw = retry_state.get("next_retry_at")
        if not retry_at_raw:
            return True
        try:
            retry_at = datetime.fromisoformat(str(retry_at_raw).replace("Z", "+00:00"))
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) >= retry_at.astimezone(timezone.utc)
        except Exception:
            return True

    @staticmethod
    def _dependency_ids(
        step: WorkflowStep,
        *,
        steps_by_id: dict[str, WorkflowStep] | None = None,
        steps_by_key: dict[str, WorkflowStep] | None = None,
    ) -> list[str]:
        steps_by_id = steps_by_id or {}
        steps_by_key = steps_by_key or {}
        dependency_ids = []
        dependency_refs = [str(item) for item in (step.depends_on_step_ids or []) if item]
        if not dependency_refs and step.parent_step_id:
            dependency_refs = [step.parent_step_id]
        for dependency_ref in dependency_refs:
            dependency = steps_by_id.get(dependency_ref) or steps_by_key.get(dependency_ref)
            resolved_id = dependency.id if dependency else dependency_ref
            if resolved_id not in dependency_ids:
                dependency_ids.append(resolved_id)
        return dependency_ids

    def _pipe_dependency_outputs(self, step: WorkflowStep, dependencies: list[WorkflowStep]) -> None:
        if not dependencies:
            return
        try:
            inline_limit_setting = db.session.get(
                Setting,
                "runtime_dependency_inline_chars",
            )
            inline_limit = max(
                1000,
                int(inline_limit_setting.value if inline_limit_setting else 12000),
            )
        except (TypeError, ValueError):
            inline_limit = 12000

        dependency_outputs = []
        for dependency in dependencies:
            output = dependency.output_payload or {}

            def externalize(raw_content, result):
                return externalize_runtime_payload(
                    raw_content,
                    instance_path=self.app.instance_path,
                    namespace=f"workflow-{step.workflow_run_id}",
                    created_by_user_id=(
                        step.created_by_user_id
                        if step.created_by_user_id is not None
                        else dependency.created_by_user_id
                    ),
                    workflow_run_id=step.workflow_run_id,
                    workflow_step_id=dependency.id,
                    label=f"Dependency output: {dependency.display_title()}",
                    kind="runtime_dependency_output_payload",
                    lineage={
                        "workflow_run_id": step.workflow_run_id,
                        "source_step_id": dependency.id,
                        "downstream_step_id": step.id,
                    },
                    source_ids=dependency.source_ids,
                    evidence_ids=dependency.evidence_ids,
                    commit=False,
                )

            envelope = build_tool_result_envelope(
                tool_name="workflow_dependency_output",
                internal_tool_call_id=f"dependency:{dependency.id}",
                result=output,
                action="dependency",
                inline_limit_chars=inline_limit,
                externalize=externalize,
            ).to_dict()
            has_reference = bool(envelope.get("data_refs"))
            if envelope.get("truncated") and has_reference:
                downstream_output = {
                    "status": "result_externalized",
                    "summary": envelope.get("summary") or "",
                    "data_refs": envelope.get("data_refs") or [],
                    "artifact_refs": envelope.get("artifact_refs") or [],
                    "raw_chars": envelope.get("raw_chars") or len(stable_json(output)),
                    "estimated_raw_tokens": envelope.get("estimated_raw_tokens") or 0,
                }
            else:
                downstream_output = output
            dependency_outputs.append({
                "step_id": dependency.id,
                "step_key": dependency.step_key,
                "title": dependency.title,
                "status": dependency.status,
                "error": dependency.error,
                "output": downstream_output,
                "result_envelope": envelope,
            })

        base_input = step.input_payload if isinstance(step.input_payload, dict) else {}
        step.input_payload = {
            **base_input,
            "dependency_outputs": dependency_outputs,
        }


def poll_workflow_orchestrator_once(app):
    """Run one orchestration pass and always release its queue handles."""
    with WorkflowOrchestrator(app) as orchestrator:
        return orchestrator.poll_and_dispatch()
