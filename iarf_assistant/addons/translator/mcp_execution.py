"""Translator-owned coordinators for MCP project and execution admission."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Literal

from ...extensions import db

if TYPE_CHECKING:
    from ...core.executor_profile import ExecutorProfile
    from ...models import McpExecution
    from .api_client import TranslatorClient


def start_mcp_execution(
    *,
    owner_user_id: int,
    project_id: str,
    mode: str,
    client_factory: Callable[[], TranslatorClient],
    execution_payload: Callable[[McpExecution], dict[str, Any]],
    executor_profile: ExecutorProfile | None = None,
    research_dossier_revision_id: str | None = None,
    context_mode: Literal["self_contained", "retained"] | None = None,
) -> dict[str, Any]:
    from ...core.mcp_jobs import (
        bind_execution_queue_job,
        create_execution,
        transition_execution,
    )

    request: dict[str, Any] = {"project_id": project_id, "mode": mode}
    if executor_profile is not None:
        request["executor_profile"] = executor_profile.to_dict()
    if research_dossier_revision_id is not None:
        request["research_dossier_revision_id"] = research_dossier_revision_id
    if context_mode is not None:
        request["context_mode"] = context_mode
    execution = create_execution(
        owner_user_id=owner_user_id,
        addon_id="translator",
        resource_kind="project",
        resource_id=project_id,
        mode=mode,
        request_json=request,
    )
    # Commit before client construction or startup. SQLite permits one WAL writer;
    # Translator must be able to persist its own operation and lease records.
    db.session.commit()
    try:
        client = client_factory()
        client.set_mcp_execution(
            project_id,
            execution.id,
            mode=mode,
            executor_profile=executor_profile,
        )
        started = client.start_block_translation(
            project_id,
            executor_profile=executor_profile,
            research_dossier_revision_id=research_dossier_revision_id,
            context_mode=context_mode,
        )
        queue_job_id = str(started.get("job_id") or "")
        if not queue_job_id:
            raise RuntimeError("Translator did not return a durable queue job.")
        execution = bind_execution_queue_job(execution.id, queue_job_id)
        execution = transition_execution(execution.id, "queued")
        db.session.commit()
    except BaseException:
        db.session.rollback()
        transition_execution(execution.id, "failed")
        db.session.commit()
        raise
    return {
        "execution": execution_payload(execution),
        "project": client.mcp_job_projection(project_id, execution_id=execution.id),
    }


def create_mcp_project(
    *,
    owner_user_id: int,
    document_id: str,
    name: str,
    source_lang: str,
    target_lang: str,
    mode: str,
    idempotency_key: str | None,
    settings: Mapping[str, Any],
    client_factory: Callable[[], TranslatorClient],
    execution_payload: Callable[[McpExecution], dict[str, Any]],
) -> dict[str, Any]:
    options = dict(settings)
    options["execution_mode"] = mode
    if str(mode or "").strip().lower() == "passive":
        # Passive block creation has no model/research phase.
        options.setdefault("enable_pre_analysis", False)
    created = client_factory().create_from_document(
        document_id,
        name=name,
        source_lang=source_lang,
        target_lang=target_lang,
        idempotency_key=idempotency_key,
        defer_source_planning=True,
        **options,
    )
    payload = {
        "project_id": created["id"],
        "project": created,
        "status": created.get("status"),
        "job_id": created.get("job_id"),
        "operation": created.get("latest_operation") or created.get("operation"),
    }
    if created.get("job_id"):
        from ...core.mcp_jobs import create_execution

        ingestion = create_execution(
            owner_user_id=owner_user_id,
            addon_id="translator",
            resource_kind="project",
            resource_id=str(created["id"]),
            queue_job_id=str(created["job_id"]),
            mode=str(mode or "managed").strip().lower(),
            request_json={"kind": "source_ingestion"},
            deduplicate_queue=True,
        )
        payload["ingestion_execution"] = execution_payload(ingestion)
    return payload
