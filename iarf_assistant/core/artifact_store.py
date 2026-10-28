from datetime import datetime
from typing import Any
from carlina.core.utc import utc_now_db
from iarf_assistant.extensions import db
from iarf_assistant.models import RuntimeArtifact, RuntimeArtifactVersion, WorkflowRun, WorkflowStep
import uuid
from sqlalchemy import func

def _json_object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}

def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if isinstance(value, tuple):
        return [str(item) for item in value if item]
    if isinstance(value, set):
        return [str(item) for item in sorted(value) if item]
    return [str(value)]

def _append_unique(existing: Any, values: Any) -> list[str]:
    result = _string_list(existing)
    seen = set(result)
    for value in _string_list(values):
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result

def _link_workflow_run_ids(workflow_run_id: str | None, **updates: Any) -> WorkflowRun | None:
    if not workflow_run_id:
        return None
    record = db.session.get(WorkflowRun, workflow_run_id)
    if not record:
        return None
    for attr, values in updates.items():
        if not hasattr(record, attr):
            continue
        setattr(record, attr, _append_unique(getattr(record, attr), values))
    record.updated_at = utc_now_db()
    return record

def _link_workflow_step_ids(step_id: str | None, **updates: Any) -> WorkflowStep | None:
    if not step_id:
        return None
    record = db.session.get(WorkflowStep, step_id)
    if not record:
        return None
    for attr, values in updates.items():
        if not hasattr(record, attr):
            continue
        setattr(record, attr, _append_unique(getattr(record, attr), values))
    record.updated_at = utc_now_db()
    return record

ARTIFACT_REVIEW_STATUSES = {"draft", "pending_review", "approved", "rejected", "changes_requested", "archived"}

def normalize_artifact_review_status(status: str | None) -> str:
    mapping = {
        "needs_review": "pending_review",
        "reviewing": "pending_review",
        "accepted": "approved",
        "declined": "rejected",
        "denied": "rejected",
        "change_requested": "changes_requested",
        "needs_changes": "changes_requested",
    }
    normalized = mapping.get(str(status or "draft").lower(), str(status or "draft").lower())
    if normalized not in ARTIFACT_REVIEW_STATUSES:
        raise ValueError(f"Invalid artifact review status: {status}")
    return normalized

class ArtifactStore:
    """
    Service for managing generated artifacts, exports, and versioning.
    """
    @classmethod
    def create_artifact(
        cls,
        *,
        artifact_type: str = "file",
        job_id: str | None = None,
        workflow_run_id: str | None = None,
        step_id: str | None = None,
        source_addon_id: str | None = None,
        created_by_user_id: int | None = None,
        uri: str | None = None,
        path: str | None = None,
        remote_reference: str | None = None,
        mime_type: str | None = None,
        content_hash: str | None = None,
        size_bytes: int | None = None,
        label: str | None = None,
        source_ids: Any = None,
        evidence_ids: Any = None,
        metadata: dict[str, Any] | None = None,
        change_summary: str | None = None,
        review_status: str = "draft",
        retention_policy: str = "default",
        download_permission: str = "owner",
        commit: bool = True,
    ) -> RuntimeArtifact:
        run = db.session.get(WorkflowRun, workflow_run_id) if workflow_run_id else None
        if workflow_run_id and not run:
            raise KeyError(f"Workflow run '{workflow_run_id}' not found.")
        if step_id:
            step = db.session.get(WorkflowStep, step_id)
            if not step:
                raise KeyError(f"Workflow step '{step_id}' not found.")
            if workflow_run_id and step.workflow_run_id != workflow_run_id:
                raise ValueError("Artifact step_id must belong to the same run.")
            workflow_run_id = workflow_run_id or step.workflow_run_id
            run = run or db.session.get(WorkflowRun, workflow_run_id)

        owner_id = created_by_user_id if created_by_user_id is not None else (run.created_by_user_id if run else None)
        now = utc_now_db()
        record = RuntimeArtifact(
            id=str(uuid.uuid4()),
            artifact_type=artifact_type,
            job_id=job_id,
            workflow_run_id=workflow_run_id,
            source_addon_id=source_addon_id,
            created_by_user_id=owner_id,
            uri=uri,
            path=path,
            remote_reference=remote_reference,
            mime_type=mime_type,
            content_hash=content_hash,
            size_bytes=size_bytes,
            label=label,
            metadata_json=_json_object(metadata),
            created_at=now,
            updated_at=now,
            review_status=normalize_artifact_review_status(review_status),
            retention_policy=retention_policy or "default",
            download_permission=download_permission or "owner",
        )
        db.session.add(record)
        version = cls._create_artifact_version_record(
            record,
            version_number=1,
            job_id=job_id,
            workflow_run_id=workflow_run_id,
            step_id=step_id,
            source_addon_id=source_addon_id,
            created_by_user_id=owner_id,
            artifact_type=artifact_type,
            uri=uri,
            path=path,
            remote_reference=remote_reference,
            mime_type=mime_type,
            content_hash=content_hash,
            size_bytes=size_bytes,
            label=label,
            source_ids=source_ids,
            evidence_ids=evidence_ids,
            metadata=metadata,
            change_summary=change_summary or "Initial artifact version.",
            created_at=now,
        )
        record.current_version_id = version.id
        _link_workflow_run_ids(workflow_run_id, artifact_ids=[record.id])
        _link_workflow_step_ids(step_id, artifact_ids=[record.id])
        if commit:
            db.session.commit()
        return record

    @classmethod
    def _create_artifact_version_record(
        cls,
        artifact: RuntimeArtifact,
        *,
        version_number: int,
        job_id: str | None = None,
        workflow_run_id: str | None = None,
        step_id: str | None = None,
        source_addon_id: str | None = None,
        created_by_user_id: int | None = None,
        artifact_type: str | None = None,
        uri: str | None = None,
        path: str | None = None,
        remote_reference: str | None = None,
        mime_type: str | None = None,
        content_hash: str | None = None,
        size_bytes: int | None = None,
        label: str | None = None,
        source_ids: Any = None,
        evidence_ids: Any = None,
        metadata: dict[str, Any] | None = None,
        change_summary: str | None = None,
        created_at: datetime | None = None,
    ) -> RuntimeArtifactVersion:
        record = RuntimeArtifactVersion(
            id=str(uuid.uuid4()),
            artifact_id=artifact.id,
            version_number=version_number,
            job_id=job_id,
            workflow_run_id=workflow_run_id,
            step_id=step_id,
            source_addon_id=source_addon_id,
            created_by_user_id=created_by_user_id,
            artifact_type=artifact_type or artifact.artifact_type,
            uri=uri,
            path=path,
            remote_reference=remote_reference,
            mime_type=mime_type,
            content_hash=content_hash,
            size_bytes=size_bytes,
            label=label,
            source_ids=_string_list(source_ids),
            evidence_ids=_string_list(evidence_ids),
            metadata_json=_json_object(metadata),
            change_summary=change_summary,
            created_at=created_at or utc_now_db(),
        )
        db.session.add(record)
        return record

    @classmethod
    def create_artifact_version(
        cls,
        artifact_id: str,
        *,
        job_id: str | None = None,
        workflow_run_id: str | None = None,
        step_id: str | None = None,
        source_addon_id: str | None = None,
        created_by_user_id: int | None = None,
        artifact_type: str | None = None,
        uri: str | None = None,
        path: str | None = None,
        remote_reference: str | None = None,
        mime_type: str | None = None,
        content_hash: str | None = None,
        size_bytes: int | None = None,
        label: str | None = None,
        source_ids: Any = None,
        evidence_ids: Any = None,
        metadata: dict[str, Any] | None = None,
        change_summary: str | None = None,
        make_current: bool = True,
        reset_review_status: bool = True,
        commit: bool = True,
    ) -> RuntimeArtifactVersion:
        artifact = db.session.get(RuntimeArtifact, artifact_id)
        if not artifact:
            raise KeyError(f"Runtime artifact '{artifact_id}' not found.")

        target_run_id = workflow_run_id or artifact.workflow_run_id
        run = db.session.get(WorkflowRun, target_run_id) if target_run_id else None
        if target_run_id and not run:
            raise KeyError(f"Workflow run '{target_run_id}' not found.")
        if artifact.workflow_run_id and target_run_id and artifact.workflow_run_id != target_run_id:
            raise ValueError("Artifact version workflow_run_id must belong to the artifact run.")
        if step_id:
            step = db.session.get(WorkflowStep, step_id)
            if not step:
                raise KeyError(f"Workflow step '{step_id}' not found.")
            if not target_run_id:
                target_run_id = step.workflow_run_id
                run = db.session.get(WorkflowRun, target_run_id)
            elif step.workflow_run_id != target_run_id:
                raise ValueError("Artifact version step_id must belong to the same run.")

        max_version = db.session.query(func.max(RuntimeArtifactVersion.version_number)).filter_by(artifact_id=artifact.id).scalar() or 0
        owner_id = created_by_user_id if created_by_user_id is not None else artifact.created_by_user_id
        version = cls._create_artifact_version_record(
            artifact,
            version_number=int(max_version) + 1,
            job_id=job_id if job_id is not None else artifact.job_id,
            workflow_run_id=target_run_id,
            step_id=step_id,
            source_addon_id=source_addon_id if source_addon_id is not None else artifact.source_addon_id,
            created_by_user_id=owner_id,
            artifact_type=artifact_type if artifact_type is not None else artifact.artifact_type,
            uri=uri if uri is not None else artifact.uri,
            path=path if path is not None else artifact.path,
            remote_reference=remote_reference if remote_reference is not None else artifact.remote_reference,
            mime_type=mime_type if mime_type is not None else artifact.mime_type,
            content_hash=content_hash if content_hash is not None else artifact.content_hash,
            size_bytes=size_bytes if size_bytes is not None else artifact.size_bytes,
            label=label if label is not None else artifact.label,
            source_ids=source_ids,
            evidence_ids=evidence_ids,
            metadata=metadata if metadata is not None else artifact.metadata_json,
            change_summary=change_summary,
        )

        if make_current:
            artifact.current_version_id = version.id
            artifact.job_id = version.job_id
            artifact.workflow_run_id = version.workflow_run_id
            artifact.source_addon_id = version.source_addon_id
            artifact.artifact_type = version.artifact_type
            artifact.uri = version.uri
            artifact.path = version.path
            artifact.remote_reference = version.remote_reference
            artifact.mime_type = version.mime_type
            artifact.content_hash = version.content_hash
            artifact.size_bytes = version.size_bytes
            artifact.label = version.label
            artifact.metadata_json = _json_object(version.metadata_json)
            artifact.updated_at = utc_now_db()
            if reset_review_status:
                artifact.review_status = "draft"
                artifact.reviewed_by_user_id = None
                artifact.reviewed_at = None
                artifact.review_note = None
            _link_workflow_run_ids(version.workflow_run_id, artifact_ids=[artifact.id])
            _link_workflow_step_ids(step_id, artifact_ids=[artifact.id])

        if commit:
            db.session.commit()
        return version

    @classmethod
    def update_artifact_review_status(
        cls,
        artifact_id: str,
        review_status: str,
        *,
        reviewed_by_user_id: int | None = None,
        review_note: str | None = None,
        commit: bool = True,
    ) -> RuntimeArtifact:
        artifact = db.session.get(RuntimeArtifact, artifact_id)
        if not artifact:
            raise KeyError(f"Runtime artifact '{artifact_id}' not found.")
        normalized = normalize_artifact_review_status(review_status)
        artifact.review_status = normalized
        artifact.review_note = review_note
        artifact.updated_at = utc_now_db()
        if normalized in {"approved", "rejected", "changes_requested", "archived"}:
            artifact.reviewed_by_user_id = reviewed_by_user_id
            artifact.reviewed_at = artifact.updated_at
        elif normalized in {"draft", "pending_review"}:
            artifact.reviewed_by_user_id = None
            artifact.reviewed_at = None
        if commit:
            db.session.commit()
        return artifact
