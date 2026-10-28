"""Document Processor MCP execution startup coordination."""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, cast

from ...extensions import db

if TYPE_CHECKING:
    from ...core.executor_profile import ExecutorProfile
    from ...models import McpExecution
    from .mcp_service import DocProcessorMcpService


def start_mcp_execution(
    *,
    owner_user_id: int,
    document_id: str,
    mode: str,
    service_factory: Callable[[], DocProcessorMcpService],
    execution_payload: Callable[[McpExecution], dict[str, Any]],
    executor_profile: ExecutorProfile | None = None,
    batch_plan: Mapping[str, Any] | None = None,
    restart_from_scratch: bool = False,
) -> dict[str, Any]:
    from ...core.mcp_jobs import (
        bind_execution_queue_job,
        create_execution,
        transition_execution,
    )

    request: dict[str, Any] = {"document_id": document_id, "mode": mode}
    if executor_profile is not None:
        request["executor_profile"] = executor_profile.to_dict()
    if batch_plan is not None:
        request["batch_plan"] = copy.deepcopy(dict(batch_plan))
    restart_options = {"restart_from_scratch": True} if restart_from_scratch else {}
    service = service_factory()
    if mode == "passive" and batch_plan is None:
        document = cast(object, service.get_document(document_id))
        source = (
            cast(Mapping[object, object], document).get("source")
            if isinstance(document, Mapping)
            else None
        )
        extension = (
            str(cast(Mapping[object, object], source).get("extension") or "").lower()
            if isinstance(source, Mapping)
            else ""
        )
        if extension == ".pdf":
            from .mcp_service import DocProcessorMcpServiceError

            raise DocProcessorMcpServiceError(
                "Passive PDF execution requires a valid orchestrator-authored "
                "batch_plan derived from source.page_count.",
                code="missing_batch_plan",
            )
    if mode == "passive" and executor_profile is not None:
        service.preflight_passive_document(
            document_id, executor_profile, **restart_options
        )
    execution = create_execution(
        owner_user_id=owner_user_id,
        addon_id="doc_processor",
        resource_kind="document",
        resource_id=document_id,
        mode=mode,
        request_json=request,
    )
    try:
        started = service.start_document(
            document_id,
            execution_id=execution.id,
            mode=mode,
            executor_profile=executor_profile,
            batch_plan=batch_plan,
            **restart_options,
        )
        queue_job_id = started.get("queue_job_id")
        if queue_job_id:
            execution = bind_execution_queue_job(execution.id, str(queue_job_id))
        execution = transition_execution(
            execution.id,
            "waiting_review" if started.get("state") == "waiting_review" else "queued",
        )
        db.session.commit()
    except BaseException:
        db.session.rollback()
        transition_execution(execution.id, "failed")
        db.session.commit()
        raise
    result: dict[str, Any] = {"execution": execution_payload(execution), **started}
    if batch_plan is not None:
        result["batch_plan"] = copy.deepcopy(dict(batch_plan))
    document_payload: object = result.get("document")
    source = (
        cast(Mapping[object, object], document_payload).get("source")
        if isinstance(document_payload, Mapping)
        else None
    )
    if (
        mode == "passive"
        and isinstance(source, Mapping)
        and cast(Mapping[object, object], source).get("extension") == ".pdf"
    ):
        wait_seconds = 5
        next_work_claim: dict[str, Any] = {
            "tool": "carlina_work_claim",
            "execution_id": execution.id,
            "arguments_template": {
                "worker_id": "CALLER_STABLE_WORKER_ID",
                "execution_id": execution.id,
                "wait_seconds": wait_seconds,
            },
            "required_caller_arguments": ["worker_id"],
            "wait_seconds": wait_seconds,
        }
        result["orchestration"] = {
            "execution_id": execution.id,
            "initial_crop_review_required": False,
            "preparation_state": (
                cast(Mapping[object, object], document_payload).get("status")
                if isinstance(document_payload, Mapping)
                else started.get("state")
            ),
            "work_ready": False,
            "work_may_not_be_ready_until_local_preparation_completes": True,
            "next_work_claim": next_work_claim,
        }
    return result
