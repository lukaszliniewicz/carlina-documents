import urllib.parse
from typing import Any
from carlina.core.utc import utc_now_db
from iarf_assistant.extensions import db
from iarf_assistant.models import RuntimeSourceRecord, RuntimeEvidenceRecord, WorkflowRun, WorkflowStep, RuntimeArtifact
import uuid

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

class SourceLedger:
    """
    Service for managing provenance records: sources, evidence, canonical URLs, and duplicate detection.
    """
    @staticmethod
    def canonicalize_uri(uri: str) -> str:
        if not uri:
            return ""
        parsed = urllib.parse.urlparse(uri)
        if parsed.scheme in ("http", "https"):
            # Strip fragments and trailing slashes for basic canonicalization
            return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
        return uri.strip()

    @classmethod
    def record_source(
        cls,
        *,
        workflow_run_id: str,
        source_type: str,
        uri: str,
        step_id: str | None = None,
        provider: str | None = None,
        external_id: str | None = None,
        title: str | None = None,
        summary: str | None = None,
        content_hash: str | None = None,
        access_scope: str = "private",
        created_by_user_id: int | None = None,
        metadata: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> RuntimeSourceRecord:
        run = db.session.get(WorkflowRun, workflow_run_id)
        if not run:
            raise KeyError(f"Workflow run '{workflow_run_id}' not found.")
        if step_id:
            step = db.session.get(WorkflowStep, step_id)
            if not step:
                raise KeyError(f"Workflow step '{step_id}' not found.")
            if step.workflow_run_id != workflow_run_id:
                raise ValueError("Source step_id must belong to the same run.")

        canonical_uri = cls.canonicalize_uri(uri)
        
        # Duplicate detection within the same workflow run
        existing = RuntimeSourceRecord.query.filter_by(
            workflow_run_id=workflow_run_id, 
            canonical_uri=canonical_uri
        ).first()
        
        if existing:
            existing.accessed_at = utc_now_db()
            if commit:
                db.session.commit()
            return existing

        owner_id = created_by_user_id if created_by_user_id is not None else run.created_by_user_id
        record = RuntimeSourceRecord(
            id=str(uuid.uuid4()),
            workflow_run_id=workflow_run_id,
            step_id=step_id,
            source_type=str(source_type),
            uri=str(uri),
            canonical_uri=canonical_uri,
            provider=provider,
            external_id=external_id,
            title=title,
            summary=summary,
            content_hash=content_hash,
            access_scope=access_scope or "private",
            created_by_user_id=owner_id,
            metadata_json=_json_object(metadata),
            accessed_at=utc_now_db()
        )
        db.session.add(record)
        _link_workflow_run_ids(workflow_run_id, source_ids=[record.id])
        _link_workflow_step_ids(step_id, source_ids=[record.id])
        if commit:
            db.session.commit()
        return record

    @classmethod
    def record_evidence(
        cls,
        *,
        workflow_run_id: str,
        source_id: str,
        step_id: str | None = None,
        artifact_id: str | None = None,
        claim: str | None = None,
        quote: str | None = None,
        citation: dict[str, Any] | None = None,
        relevance_score: float | None = None,
        novelty_score: float | None = None,
        used_in_artifact: bool = False,
        created_by_user_id: int | None = None,
        metadata: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> RuntimeEvidenceRecord:
        run = db.session.get(WorkflowRun, workflow_run_id)
        if not run:
            raise KeyError(f"Workflow run '{workflow_run_id}' not found.")
        source = db.session.get(RuntimeSourceRecord, source_id)
        if not source:
            raise KeyError(f"Runtime source '{source_id}' not found.")
        if source.workflow_run_id != workflow_run_id:
            raise ValueError("Evidence source_id must belong to the same run.")
        if step_id:
            step = db.session.get(WorkflowStep, step_id)
            if not step:
                raise KeyError(f"Workflow step '{step_id}' not found.")
            if step.workflow_run_id != workflow_run_id:
                raise ValueError("Evidence step_id must belong to the same run.")
        if artifact_id:
            artifact = db.session.get(RuntimeArtifact, artifact_id)
            if not artifact:
                raise KeyError(f"Runtime artifact '{artifact_id}' not found.")
            if artifact.workflow_run_id and artifact.workflow_run_id != workflow_run_id:
                raise ValueError("Evidence artifact_id must belong to the same run.")

        owner_id = created_by_user_id if created_by_user_id is not None else run.created_by_user_id
        record = RuntimeEvidenceRecord(
            id=str(uuid.uuid4()),
            workflow_run_id=workflow_run_id,
            step_id=step_id,
            source_id=source_id,
            artifact_id=artifact_id,
            claim=claim,
            quote=quote,
            citation=_json_object(citation),
            relevance_score=relevance_score,
            novelty_score=novelty_score,
            used_in_artifact=bool(used_in_artifact),
            created_by_user_id=owner_id,
            metadata_json=_json_object(metadata),
        )
        db.session.add(record)
        _link_workflow_run_ids(workflow_run_id, evidence_ids=[record.id])
        _link_workflow_step_ids(step_id, evidence_ids=[record.id])
        if artifact_id:
            _link_workflow_run_ids(workflow_run_id, artifact_ids=[artifact_id])
            _link_workflow_step_ids(step_id, artifact_ids=[artifact_id])
        if commit:
            db.session.commit()
        return record
