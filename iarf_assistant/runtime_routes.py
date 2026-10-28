import shutil
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request, send_from_directory
from flask_login import current_user, login_required
from sqlalchemy import false, func, or_

from iarf_assistant.core.runtime_state import (
    APPROVAL_STATUSES,
    ARTIFACT_REVIEW_STATUSES,
    JOB_STATUSES,
    RUN_KINDS,
    WORKFLOW_RUN_STATUSES,
    WORKFLOW_STEP_STATUSES,
    attach_runtime_log_link_ids,
    create_artifact,
    create_artifact_version,
    create_evidence_record,
    create_resource_reference,
    create_source_record,
    create_workflow_run,
    create_workflow_step,
    evaluate_runtime_checkpoints,
    normalize_workflow_status,
    normalize_workflow_step_status,
    pause_workflow_run,
    read_text_artifact_content,
    resume_workflow_run,
    resolve_approval_request,
    resolve_artifact_path,
    set_workflow_run_status,
    set_workflow_step_status,
    scope_expansion_payload_from_approval,
    update_artifact_review_status,
    update_workflow_run_planning,
    visible_to_user,
)
from iarf_assistant.core.runs.projections import load_run_projection, serialize_workflow_run
from carlina.core.utc import utc_iso, utc_now_db
from iarf_assistant.core.workflow_templates import (
    TemplateDefinitionError,
    active_template_definition,
    append_template_version,
    compile_template_definition,
    executable_definition_from_addon_workflow,
    make_template_storage,
    read_template_storage,
    set_active_template_version,
)
from iarf_assistant.extensions import db
from iarf_assistant.html_sanitizer import sanitize_html_fragment
from iarf_assistant.markdown import md_parser
from iarf_assistant.models import (
    ChatSession,
    RuntimeApprovalRequest,
    RuntimeArtifact,
    RuntimeArtifactVersion,
    RuntimeCollection,
    RuntimeCollectionItem,
    RuntimeEvidenceRecord,
    RuntimeJob,
    RuntimeResourceReference,
    RuntimeSourceRecord,
    WorkflowTemplate,
    WorkflowRun,
    WorkflowStep,
)


runtime_blueprint = Blueprint('runtime_api', __name__)


def _runtime_query_for_user(model):
    query = model.query
    if current_user.is_admin:
        return query

    user_id = int(current_user.id)
    filters = []
    for attr in ('created_by_user_id', 'owner_user_id', 'requester_user_id', 'approver_user_id'):
        if hasattr(model, attr):
            filters.append(getattr(model, attr) == user_id)
    if hasattr(model, 'workflow_run_id'):
        visible_run_ids = db.session.query(WorkflowRun.id).filter(WorkflowRun.created_by_user_id == user_id)
        filters.append(getattr(model, 'workflow_run_id').in_(visible_run_ids))
    if not filters:
        return query.filter(false())
    return query.filter(or_(*filters))


def _runtime_record_or_404(model, record_id):
    record = db.session.get(model, record_id)
    if not record:
        return None, (jsonify({"error": "Runtime record not found."}), 404)
    if not visible_to_user(record, current_user.id, current_user.is_admin):
        return None, (jsonify({"error": "Access denied."}), 403)
    return record, None


def _with_runtime_log_links(payload):
    log_source_ids = payload.get('log_source_ids') or []
    links = attach_runtime_log_link_ids(log_source_ids)
    if links:
        payload['log_links'] = links
    return payload


def _collect_runtime_artifact_paths(records):
    paths = set()
    for record in records:
        try:
            path = resolve_artifact_path(record, instance_path=current_app.instance_path)
        except ValueError:
            current_app.logger.warning("Skipping unsafe artifact path for record %s.", getattr(record, "id", "<unknown>"))
            continue
        if path and path.is_file():
            paths.add(path)
    return sorted(paths, key=lambda item: len(str(item)), reverse=True)


def _cleanup_runtime_artifact_files(*, run_id, artifact_paths):
    for path in artifact_paths:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            current_app.logger.warning("Failed to remove artifact file '%s': %s", path, exc)

    root = Path(current_app.instance_path).resolve()
    run_artifact_dir = (root / "artifacts" / "workflow_runs" / str(run_id)).resolve()
    try:
        run_artifact_dir.relative_to(root)
    except ValueError:
        current_app.logger.warning("Skipping removal of out-of-root run artifact directory '%s'.", run_artifact_dir)
        return

    if run_artifact_dir.exists():
        try:
            shutil.rmtree(run_artifact_dir, ignore_errors=False)
        except OSError as exc:
            current_app.logger.warning("Failed to remove run artifact directory '%s': %s", run_artifact_dir, exc)


def _count_runtime_rows_for_run(model, run_id: str) -> int:
    """
    Count rows by run id without relying on Query.count().

    Query.count() builds a subquery that selects all mapped columns, which can fail
    during transitional schemas (for example, after model fields are added before a
    migration is applied). Counting a concrete id column is safer and faster.
    """
    run_column = getattr(model, "workflow_run_id", None)
    id_column = getattr(model, "id", None)
    if run_column is None or id_column is None:
        return 0
    count = db.session.query(func.count(id_column)).filter(run_column == run_id).scalar()
    return int(count or 0)


TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled"}


def _trigger_workflow_orchestrator() -> None:
    try:
        from iarf_assistant.core.workflow_executor import poll_workflow_orchestrator_once

        poll_workflow_orchestrator_once(current_app)
    except Exception as exc:
        current_app.logger.error(f"Failed to trigger orchestrator: {exc}")


def _resolve_chat_session_for_current_user(chat_session_id: str | None):
    if not chat_session_id:
        return None, None
    chat_session = db.session.get(ChatSession, chat_session_id)
    if not chat_session:
        return None, (jsonify({"error": "Chat session not found."}), 404)
    if not current_user.is_admin and int(chat_session.user_id) != int(current_user.id):
        return None, (jsonify({"error": "Access denied."}), 403)
    return chat_session, None


def _workflow_template_query_for_user(*, include_inactive: bool = False):
    query = WorkflowTemplate.query
    if current_user.is_admin:
        if not include_inactive:
            query = query.filter(WorkflowTemplate.is_active.is_(True))
        return query

    query = query.filter(
        or_(
            WorkflowTemplate.owner_id == int(current_user.id),
            WorkflowTemplate.owner_id.is_(None),
        )
    )
    if not include_inactive:
        query = query.filter(
            or_(
                WorkflowTemplate.owner_id == int(current_user.id),
                WorkflowTemplate.is_active.is_(True),
            )
        )
    return query


def _workflow_template_or_404(template_id: str, *, require_write: bool = False):
    record = db.session.get(WorkflowTemplate, template_id)
    if not record:
        return None, (jsonify({"error": "Workflow template not found."}), 404)

    if current_user.is_admin:
        return record, None

    user_id = int(current_user.id)
    is_owner = record.owner_id is not None and int(record.owner_id) == user_id
    can_read = is_owner or (record.owner_id is None and bool(record.is_active))
    if not can_read:
        return None, (jsonify({"error": "Access denied."}), 403)
    if require_write and not is_owner:
        return None, (jsonify({"error": "Access denied."}), 403)
    return record, None


def _workflow_template_payload(record: WorkflowTemplate) -> dict:
    base = {
        "id": record.id,
        "title": record.title,
        "description": record.description,
        "owner_id": record.owner_id,
        "is_active": bool(record.is_active),
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
    }
    try:
        storage = read_template_storage(record.definition_json)
    except TemplateDefinitionError as exc:
        return {
            **base,
            "schema_version": 1,
            "active_version": None,
            "versions": [],
            "definition": {},
            "definition_error": str(exc),
        }

    active_version = int(storage["active_version"])
    versions = []
    for item in storage["versions"]:
        definition = item.get("definition") if isinstance(item.get("definition"), dict) else {}
        versions.append(
            {
                "version": int(item["version"]),
                "name": definition.get("name"),
                "description": definition.get("description"),
                "step_count": len(definition.get("steps") or []),
                "created_at": item.get("created_at"),
                "created_by_user_id": item.get("created_by_user_id"),
                "change_summary": item.get("change_summary"),
            }
        )
    versions.sort(key=lambda row: row["version"])
    return {
        **base,
        "schema_version": int(storage.get("schema_version") or 1),
        "active_version": active_version,
        "versions": versions,
        "definition": active_template_definition(storage),
    }


def _materialize_compiled_template_steps(run: WorkflowRun, compiled, *, owner_user_id: int) -> list[WorkflowStep]:
    created_steps = []
    for index, step in enumerate(compiled.steps, start=1):
        created_steps.append(
            create_workflow_step(
                workflow_run_id=run.id,
                step_type=step["type"],
                title=step["title"],
                step_key=step["key"],
                status="planned",
                sequence=step.get("sequence") if step.get("sequence") is not None else index,
                depends_on_step_ids=step.get("depends_on"),
                assigned_model_role=step.get("assigned_model_role"),
                created_by_user_id=owner_user_id,
                instructions=step.get("instructions"),
                capability_ids=step.get("capability_ids"),
                skill_ids=step.get("skill_ids"),
                tool_ids=step.get("tool_ids"),
                provider_requirements=step.get("provider_requirements"),
                input_schema=step.get("input_schema"),
                output_schema=step.get("output_schema"),
                input_payload=step.get("input"),
                metadata=step.get("metadata"),
                commit=False,
            )
        )
    db.session.commit()
    return created_steps


def _build_template_run(record: WorkflowRun, *, start: bool) -> dict:
    payload = _with_runtime_log_links(record.to_dict())
    if start and str(record.status).lower() not in TERMINAL_RUN_STATUSES:
        if str(record.status).lower() != "running":
            updated = set_workflow_run_status(record.id, "running")
            payload = _with_runtime_log_links(updated.to_dict())
        _trigger_workflow_orchestrator()
    return payload


def _create_runtime_workflow_run_from_data(data: dict):
    status = normalize_workflow_status(data.get('status') or 'ready')
    run_kind = (data.get('kind') or ('template' if (data.get('workflow_id') or data.get('template_id')) else 'ad_hoc')).lower()
    if run_kind not in RUN_KINDS:
        return jsonify({"error": "Invalid run kind."}), 400
    workflow_id = data.get('workflow_id')
    template_id = data.get('template_id')
    if workflow_id and template_id:
        return jsonify({"error": "Provide either workflow_id or template_id, not both."}), 400
    if run_kind == 'template' and not (workflow_id or template_id):
        return jsonify({"error": "workflow_id or template_id is required."}), 400

    workflow = None
    if workflow_id and hasattr(current_app, 'addon_manager'):
        workflow = current_app.addon_manager.get_workflow_record(workflow_id)
        if not workflow:
            return jsonify({"error": f"Workflow '{workflow_id}' not found."}), 404

    template_record = None
    if template_id:
        template_record, error = _workflow_template_or_404(template_id)
        if error:
            return error
        if not template_record.is_active:
            return jsonify({"error": "Workflow template is inactive."}), 409

    compiled_template = None
    template_runtime = None
    if template_record:
        try:
            storage = read_template_storage(template_record.definition_json)
            definition = active_template_definition(storage)
            compiled_template = compile_template_definition(definition, fallback_name=template_record.title)
            template_runtime = {
                "type": "user_template",
                "template_id": template_record.id,
                "active_version": int(storage["active_version"]),
            }
        except TemplateDefinitionError as exc:
            return jsonify({"error": f"Template definition is invalid: {exc}"}), 400
    elif workflow:
        definition = executable_definition_from_addon_workflow(workflow)
        if definition:
            try:
                compiled_template = compile_template_definition(definition, fallback_name=workflow.name)
                template_runtime = {
                    "type": "addon_workflow",
                    "workflow_id": workflow.id,
                }
            except TemplateDefinitionError as exc:
                return jsonify({"error": f"Addon workflow definition is invalid: {exc}"}), 400

    log_source_ids = data.get('log_source_ids')
    if workflow and not log_source_ids:
        log_source_ids = [link.get('id') for link in workflow.log_links if isinstance(link, dict) and link.get('id')]

    chat_session_id = data.get('chat_session_id') or data.get('session_id')
    if chat_session_id:
        chat_session, error = _resolve_chat_session_for_current_user(chat_session_id)
        if error:
            return error
        chat_session_id = chat_session.id

    metadata = data.get('metadata') if isinstance(data.get('metadata'), dict) else {}
    plan_payload = data.get('plan')
    if compiled_template:
        user_plan = plan_payload if isinstance(plan_payload, dict) else {}
        plan_payload = {**compiled_template.plan, **user_plan}
        template_metadata = {
            **(template_runtime or {}),
            "compiled_at": utc_iso(),
            "step_count": len(compiled_template.steps),
            "definition_version": int(compiled_template.definition.get("version") or 1),
        }
        metadata = {**metadata, "template_runtime": template_metadata}

    run_workflow_id = workflow_id
    if template_record and not run_workflow_id:
        run_workflow_id = f"user_template:{template_record.id}"

    record = create_workflow_run(
        title=data.get('title') or data.get('name') or (workflow.name if workflow else (template_record.title if template_record else None)),
        kind=run_kind,
        workflow_id=run_workflow_id,
        source_addon_id=data.get('source_addon_id') or (workflow.addon_id if workflow else ("user_template" if template_record else None)),
        status=status,
        input_payload=data.get('input'),
        understanding=data.get('understanding'),
        plan=plan_payload,
        state=data.get('state'),
        current_step=data.get('current_step'),
        created_by_user_id=current_user.id,
        chat_session_id=chat_session_id,
        approval_ids=data.get('approval_ids'),
        resource_reference_ids=data.get('resource_reference_ids'),
        job_ids=data.get('job_ids'),
        step_ids=data.get('step_ids'),
        source_ids=data.get('source_ids'),
        evidence_ids=data.get('evidence_ids'),
        artifact_ids=data.get('artifact_ids'),
        log_source_ids=log_source_ids,
        resume_url=data.get('resume_url'),
        cancel_url=data.get('cancel_url'),
        metadata=metadata,
    )

    if compiled_template:
        try:
            _materialize_compiled_template_steps(record, compiled_template, owner_user_id=int(current_user.id))
        except (TemplateDefinitionError, ValueError, KeyError) as exc:
            db.session.rollback()
            return jsonify({"error": f"Failed to materialize template steps: {exc}"}), 400

    start_requested = bool(data.get("start") or data.get("auto_start"))
    payload = _build_template_run(
        db.session.get(WorkflowRun, record.id),
        start=bool(compiled_template) and (start_requested or status == "running"),
    )
    return jsonify(payload), 201


def _render_artifact_preview_html(artifact: RuntimeArtifact, content_text: str) -> tuple[str | None, str | None]:
    mime_type = (artifact.mime_type or "").lower()
    suffix = ""
    try:
        path = resolve_artifact_path(artifact, instance_path=current_app.instance_path)
        if path:
            suffix = path.suffix.lower()
    except ValueError:
        suffix = ""

    html_mimes = {"text/html", "application/xhtml+xml"}
    markdown_mimes = {"text/markdown", "text/x-markdown", "application/markdown"}
    markdown_suffixes = {".md", ".markdown"}
    html_suffixes = {".html", ".htm"}

    if mime_type in html_mimes or suffix in html_suffixes:
        safe_html = sanitize_html_fragment(content_text or "")
        return (safe_html or None, "html")

    if mime_type in markdown_mimes or suffix in markdown_suffixes:
        rendered_html = md_parser(content_text or "")
        safe_html = sanitize_html_fragment(rendered_html)
        return (safe_html or None, "markdown")

    return None, None


@runtime_blueprint.route('/api/runtime/approvals', methods=['GET'])
@login_required
def list_runtime_approvals():
    """Returns approval requests visible to the current user."""
    status = request.args.get('status')
    query = _runtime_query_for_user(RuntimeApprovalRequest)
    if status:
        normalized = status.lower()
        if normalized not in APPROVAL_STATUSES:
            return jsonify({"error": "Invalid approval status."}), 400
        query = query.filter(RuntimeApprovalRequest.status == normalized)
    rows = query.order_by(RuntimeApprovalRequest.created_at.desc()).limit(200).all()
    return jsonify([_with_runtime_log_links(row.to_dict()) for row in rows])


@runtime_blueprint.route('/api/runtime/approvals/<approval_id>', methods=['GET'])
@login_required
def get_runtime_approval(approval_id):
    record, error = _runtime_record_or_404(RuntimeApprovalRequest, approval_id)
    if error:
        return error
    return jsonify(_with_runtime_log_links(record.to_dict()))


@runtime_blueprint.route('/api/runtime/approvals/<approval_id>', methods=['POST'])
@login_required
def update_runtime_approval(approval_id):
    record, error = _runtime_record_or_404(RuntimeApprovalRequest, approval_id)
    if error:
        return error

    data = request.get_json(silent=True) or {}
    status = (data.get('status') or data.get('action') or '').lower()
    if status not in {'approved', 'rejected', 'cancelled'}:
        return jsonify({"error": "Status must be approved, rejected, or cancelled."}), 400

    is_approver = record.approver_user_id and int(record.approver_user_id) == int(current_user.id)
    is_requester = record.requester_user_id and int(record.requester_user_id) == int(current_user.id)
    can_resolve_unassigned = record.approver_user_id is None and is_requester
    if status in {'approved', 'rejected'} and not (current_user.is_admin or is_approver or can_resolve_unassigned):
        return jsonify({"error": "Only an admin or assigned approver can resolve this request."}), 403
    if status == 'cancelled' and not (current_user.is_admin or is_requester):
        return jsonify({"error": "Only an admin or requester can cancel this request."}), 403

    try:
        updated = resolve_approval_request(
            approval_id,
            status,
            approver_user_id=current_user.id,
            note=data.get('note'),
        )
        
        from flask import current_app
        from iarf_assistant.core.workflow_executor import poll_workflow_orchestrator_once
        try:
            poll_workflow_orchestrator_once(current_app)
        except Exception as e:
            from flask import current_app
            current_app.logger.error(f"Failed to trigger orchestrator after approval: {e}")

        payload = _with_runtime_log_links(updated.to_dict())
        scope_expansion = scope_expansion_payload_from_approval(updated)
        if scope_expansion:
            payload["scope_expansion"] = scope_expansion
        return jsonify(payload)
    except (KeyError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 409


@runtime_blueprint.route('/api/runtime/resource-references', methods=['GET'])
@login_required
def list_runtime_resource_references():
    query = _runtime_query_for_user(RuntimeResourceReference)
    resource_type = request.args.get('type')
    if resource_type:
        query = query.filter(RuntimeResourceReference.resource_type == resource_type)
    rows = query.order_by(RuntimeResourceReference.created_at.desc()).limit(200).all()
    return jsonify([row.to_dict() for row in rows])


@runtime_blueprint.route('/api/runtime/resource-references', methods=['POST'])
@login_required
def create_runtime_resource_reference():
    data = request.get_json(silent=True) or {}
    resource_type = data.get('resource_type') or data.get('type')
    resource_uri = data.get('resource_uri') or data.get('uri') or data.get('path')
    if not resource_type or not resource_uri:
        return jsonify({"error": "resource_type and resource_uri are required."}), 400
    record = create_resource_reference(
        owner_user_id=current_user.id,
        resource_type=resource_type,
        resource_uri=resource_uri,
        provider=data.get('provider'),
        label=data.get('label'),
        permissions=data.get('permissions'),
        scopes=data.get('scopes'),
        metadata=data.get('metadata'),
        source_addon_id=data.get('source_addon_id'),
        workflow_run_id=data.get('workflow_run_id'),
    )
    return jsonify(record.to_dict()), 201


@runtime_blueprint.route('/api/runtime/resource-references/<reference_id>', methods=['GET'])
@login_required
def get_runtime_resource_reference(reference_id):
    record, error = _runtime_record_or_404(RuntimeResourceReference, reference_id)
    if error:
        return error
    return jsonify(record.to_dict())


@runtime_blueprint.route('/api/runtime/jobs', methods=['GET'])
@login_required
def list_runtime_jobs():
    status = request.args.get('status')
    query = _runtime_query_for_user(RuntimeJob)
    if status:
        normalized = status.lower()
        if normalized not in JOB_STATUSES:
            return jsonify({"error": "Invalid job status."}), 400
        query = query.filter(RuntimeJob.status == normalized)
    rows = query.order_by(RuntimeJob.updated_at.desc()).limit(200).all()
    return jsonify([_with_runtime_log_links(row.to_dict()) for row in rows])


@runtime_blueprint.route('/api/runtime/jobs/<job_id>', methods=['GET'])
@login_required
def get_runtime_job(job_id):
    record, error = _runtime_record_or_404(RuntimeJob, job_id)
    if error:
        return error
    return jsonify(_with_runtime_log_links(record.to_dict()))


@runtime_blueprint.route('/api/runtime/artifacts', methods=['GET'])
@runtime_blueprint.route('/api/runtime/runs/<run_id>/artifacts', methods=['GET'])
@login_required
def list_runtime_artifacts(run_id=None):
    query = _runtime_query_for_user(RuntimeArtifact)
    if run_id:
        run, error = _runtime_record_or_404(WorkflowRun, run_id)
        if error:
            return error
        query = query.filter(RuntimeArtifact.workflow_run_id == run.id)
    chat_session_id = request.args.get('session_id') or request.args.get('chat_session_id')
    if chat_session_id:
        chat_session, error = _resolve_chat_session_for_current_user(chat_session_id)
        if error:
            return error
        session_run_ids = db.session.query(WorkflowRun.id).filter(WorkflowRun.chat_session_id == chat_session.id)
        query = query.filter(RuntimeArtifact.workflow_run_id.in_(session_run_ids))
    artifact_type = request.args.get('type')
    if artifact_type:
        query = query.filter(RuntimeArtifact.artifact_type == artifact_type)
    include_context_results = str(request.args.get('include_context_results') or '').lower() in {"1", "true", "yes"}
    if not include_context_results:
        query = query.filter(RuntimeArtifact.retention_policy != 'chat_context_result')
    review_status = request.args.get('review_status') or request.args.get('status')
    if review_status:
        normalized_status = review_status.lower()
        if normalized_status not in ARTIFACT_REVIEW_STATUSES:
            return jsonify({"error": "Invalid artifact review status."}), 400
        query = query.filter(RuntimeArtifact.review_status == normalized_status)
    workflow_run_id = request.args.get('workflow_run_id')
    if workflow_run_id:
        query = query.filter(RuntimeArtifact.workflow_run_id == workflow_run_id)
    rows = query.order_by(RuntimeArtifact.created_at.desc()).limit(200).all()
    return jsonify([row.to_dict() for row in rows])


@runtime_blueprint.route('/api/runtime/runs/<run_id>/artifacts', methods=['POST'])
@login_required
def create_runtime_artifact(run_id):
    run, error = _runtime_record_or_404(WorkflowRun, run_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    artifact_type = data.get('artifact_type') or data.get('type') or 'file'
    try:
        record = create_artifact(
            artifact_type=artifact_type,
            job_id=data.get('job_id'),
            workflow_run_id=run.id,
            step_id=data.get('step_id'),
            source_addon_id=data.get('source_addon_id') or run.source_addon_id,
            created_by_user_id=current_user.id,
            uri=data.get('uri'),
            path=data.get('path'),
            remote_reference=data.get('remote_reference'),
            mime_type=data.get('mime_type'),
            content_hash=data.get('content_hash'),
            size_bytes=data.get('size_bytes'),
            label=data.get('label'),
            source_ids=data.get('source_ids'),
            evidence_ids=data.get('evidence_ids'),
            metadata=data.get('metadata'),
            change_summary=data.get('change_summary'),
            review_status=data.get('review_status') or 'draft',
            retention_policy=data.get('retention_policy') or 'default',
            download_permission=data.get('download_permission') or 'owner',
        )
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(record.to_dict()), 201


@runtime_blueprint.route('/api/runtime/artifacts/<artifact_id>', methods=['GET'])
@login_required
def get_runtime_artifact(artifact_id):
    record, error = _runtime_record_or_404(RuntimeArtifact, artifact_id)
    if error:
        return error
    return jsonify(record.to_dict())


@runtime_blueprint.route('/api/runtime/artifacts/<artifact_id>/versions', methods=['GET'])
@login_required
def list_runtime_artifact_versions(artifact_id):
    artifact, error = _runtime_record_or_404(RuntimeArtifact, artifact_id)
    if error:
        return error
    rows = RuntimeArtifactVersion.query.filter_by(artifact_id=artifact.id).order_by(RuntimeArtifactVersion.version_number.desc()).all()
    return jsonify([row.to_dict() for row in rows])


@runtime_blueprint.route('/api/runtime/artifacts/<artifact_id>/versions', methods=['POST'])
@login_required
def create_runtime_artifact_version(artifact_id):
    artifact, error = _runtime_record_or_404(RuntimeArtifact, artifact_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    try:
        version = create_artifact_version(
            artifact.id,
            job_id=data.get('job_id'),
            workflow_run_id=data.get('workflow_run_id') or artifact.workflow_run_id,
            step_id=data.get('step_id'),
            source_addon_id=data.get('source_addon_id'),
            created_by_user_id=current_user.id,
            artifact_type=data.get('artifact_type') or data.get('type'),
            uri=data.get('uri'),
            path=data.get('path'),
            remote_reference=data.get('remote_reference'),
            mime_type=data.get('mime_type'),
            content_hash=data.get('content_hash'),
            size_bytes=data.get('size_bytes'),
            label=data.get('label'),
            source_ids=data.get('source_ids'),
            evidence_ids=data.get('evidence_ids'),
            metadata=data.get('metadata'),
            change_summary=data.get('change_summary'),
            make_current=data.get('make_current') is not False,
            reset_review_status=data.get('reset_review_status') is not False,
        )
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"version": version.to_dict(), "artifact": db.session.get(RuntimeArtifact, artifact.id).to_dict()}), 201


@runtime_blueprint.route('/api/runtime/artifacts/<artifact_id>/versions/<version_id>', methods=['GET'])
@login_required
def get_runtime_artifact_version(artifact_id, version_id):
    artifact, error = _runtime_record_or_404(RuntimeArtifact, artifact_id)
    if error:
        return error
    version = db.session.get(RuntimeArtifactVersion, version_id)
    if not version or version.artifact_id != artifact.id:
        return jsonify({"error": "Runtime artifact version not found."}), 404
    return jsonify(version.to_dict())


@runtime_blueprint.route('/api/runtime/artifacts/<artifact_id>/review', methods=['POST', 'PATCH'])
@login_required
def update_runtime_artifact_review(artifact_id):
    record, error = _runtime_record_or_404(RuntimeArtifact, artifact_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    review_status = data.get('review_status') or data.get('status')
    if not review_status:
        return jsonify({"error": "review_status is required."}), 400
    try:
        updated = update_artifact_review_status(
            record.id,
            review_status,
            reviewed_by_user_id=current_user.id,
            review_note=data.get('review_note') or data.get('note'),
        )
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(updated.to_dict())


@runtime_blueprint.route('/api/runtime/artifacts/<artifact_id>/download', methods=['GET'])
@login_required
def download_runtime_artifact(artifact_id):
    record, error = _runtime_record_or_404(RuntimeArtifact, artifact_id)
    if error:
        return error
    if record.download_permission == 'none':
        return jsonify({"error": "Artifact is not downloadable."}), 403
    if record.download_permission == 'admin' and not current_user.is_admin:
        return jsonify({"error": "Administrator access required."}), 403
    try:
        path = resolve_artifact_path(record, instance_path=current_app.instance_path)
    except ValueError:
        return jsonify({"error": "Artifact path is not safe."}), 400
    if not path or not path.exists() or not path.is_file():
        return jsonify({"error": "Artifact file is not available."}), 404
    return send_from_directory(path.parent, path.name, as_attachment=True, download_name=record.label or path.name)


@runtime_blueprint.route('/api/runtime/artifacts/<artifact_id>/content', methods=['GET'])
@login_required
def get_runtime_artifact_content(artifact_id):
    record, error = _runtime_record_or_404(RuntimeArtifact, artifact_id)
    if error:
        return error
    max_chars = request.args.get('max_chars', '20000')
    try:
        max_chars_int = int(max_chars)
    except ValueError:
        return jsonify({"error": "max_chars must be an integer."}), 400
    content = read_text_artifact_content(record, instance_path=current_app.instance_path, max_chars=max_chars_int)
    if content.get("available"):
        rendered_html, render_mode = _render_artifact_preview_html(record, content.get("content") or "")
        if rendered_html:
            content["rendered_html"] = rendered_html
        if render_mode:
            content["render_mode"] = render_mode
    return jsonify({"artifact": record.to_dict(), "content": content})


@runtime_blueprint.route('/api/runtime/runs', methods=['GET'])
@runtime_blueprint.route('/api/runtime/workflow-runs', methods=['GET'])
@login_required
def list_runtime_workflow_runs():
    status = request.args.get('status')
    kind = request.args.get('kind')
    chat_session_id = request.args.get('session_id') or request.args.get('chat_session_id')
    query = _runtime_query_for_user(WorkflowRun)
    if chat_session_id:
        chat_session, error = _resolve_chat_session_for_current_user(chat_session_id)
        if error:
            return error
        query = query.filter(WorkflowRun.chat_session_id == chat_session.id)
    if status:
        normalized = normalize_workflow_status(status)
        if normalized not in WORKFLOW_RUN_STATUSES:
            return jsonify({"error": "Invalid workflow run status."}), 400
        query = query.filter(WorkflowRun.status == normalized)
    if kind:
        normalized_kind = kind.lower()
        if normalized_kind not in RUN_KINDS:
            return jsonify({"error": "Invalid run kind."}), 400
        query = query.filter(WorkflowRun.kind == normalized_kind)
    rows = query.order_by(WorkflowRun.updated_at.desc()).limit(200).all()
    for row in rows:
        evaluate_runtime_checkpoints(row.id)
    return jsonify([_with_runtime_log_links(row.to_dict()) for row in rows])


@runtime_blueprint.route('/api/runtime/workflow-templates', methods=['GET'])
@login_required
def list_runtime_workflow_templates():
    include_inactive = str(request.args.get("include_inactive") or "").lower() in {"1", "true", "yes"}
    rows = (
        _workflow_template_query_for_user(include_inactive=include_inactive)
        .order_by(WorkflowTemplate.updated_at.desc())
        .limit(200)
        .all()
    )
    return jsonify([_workflow_template_payload(row) for row in rows])


@runtime_blueprint.route('/api/runtime/workflow-templates', methods=['POST'])
@login_required
def create_runtime_workflow_template():
    data = request.get_json(silent=True) or {}
    title = str(data.get("title") or data.get("name") or "").strip()
    if not title:
        return jsonify({"error": "title is required."}), 400
    definition = data.get("definition")
    if not isinstance(definition, dict):
        return jsonify({"error": "definition must be an object."}), 400
    try:
        storage = make_template_storage(
            definition,
            created_by_user_id=int(current_user.id),
            change_summary=data.get("change_summary"),
        )
    except TemplateDefinitionError as exc:
        return jsonify({"error": str(exc)}), 400

    record = WorkflowTemplate(
        title=title,
        description=data.get("description"),
        definition_json=storage,
        owner_id=int(current_user.id),
        is_active=bool(data.get("is_active", True)),
    )
    db.session.add(record)
    db.session.commit()
    return jsonify(_workflow_template_payload(record)), 201


@runtime_blueprint.route('/api/runtime/workflow-templates/<template_id>', methods=['GET'])
@login_required
def get_runtime_workflow_template(template_id):
    record, error = _workflow_template_or_404(template_id)
    if error:
        return error
    return jsonify(_workflow_template_payload(record))


@runtime_blueprint.route('/api/runtime/workflow-templates/<template_id>', methods=['PUT', 'POST', 'PATCH'])
@login_required
def update_runtime_workflow_template(template_id):
    record, error = _workflow_template_or_404(template_id, require_write=True)
    if error:
        return error
    data = request.get_json(silent=True) or {}

    if data.get("title") is not None:
        title = str(data.get("title") or "").strip()
        if not title:
            return jsonify({"error": "title cannot be empty."}), 400
        record.title = title
    if data.get("description") is not None:
        record.description = data.get("description")
    if data.get("is_active") is not None:
        record.is_active = bool(data.get("is_active"))

    try:
        storage = read_template_storage(record.definition_json)
    except TemplateDefinitionError as exc:
        return jsonify({"error": str(exc)}), 400
    if data.get("definition") is not None:
        if not isinstance(data.get("definition"), dict):
            return jsonify({"error": "definition must be an object."}), 400
        try:
            storage = append_template_version(
                storage,
                data.get("definition"),
                created_by_user_id=int(current_user.id),
                change_summary=data.get("change_summary"),
            )
        except TemplateDefinitionError as exc:
            return jsonify({"error": str(exc)}), 400
    if data.get("active_version") is not None:
        try:
            storage = set_active_template_version(storage, int(data.get("active_version")))
        except (TemplateDefinitionError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

    record.definition_json = storage
    db.session.commit()
    return jsonify(_workflow_template_payload(record))


@runtime_blueprint.route('/api/runtime/workflow-templates/<template_id>', methods=['DELETE'])
@login_required
def delete_runtime_workflow_template(template_id):
    record, error = _workflow_template_or_404(template_id, require_write=True)
    if error:
        return error
    db.session.delete(record)
    db.session.commit()
    return jsonify({"success": True, "id": template_id})


@runtime_blueprint.route('/api/runtime/workflow-templates/<template_id>/versions', methods=['GET'])
@login_required
def list_runtime_workflow_template_versions(template_id):
    record, error = _workflow_template_or_404(template_id)
    if error:
        return error
    payload = _workflow_template_payload(record)
    return jsonify(
        {
            "template_id": payload["id"],
            "active_version": payload["active_version"],
            "versions": payload["versions"],
        }
    )


@runtime_blueprint.route('/api/runtime/workflow-templates/<template_id>/runs', methods=['POST'])
@login_required
def create_runtime_run_from_workflow_template(template_id):
    data = request.get_json(silent=True) or {}
    data = {**data, "template_id": template_id, "kind": "template"}
    return _create_runtime_workflow_run_from_data(data)


@runtime_blueprint.route('/api/runtime/runs', methods=['POST'])
@runtime_blueprint.route('/api/runtime/workflow-runs', methods=['POST'])
@login_required
def create_runtime_workflow_run():
    data = request.get_json(silent=True) or {}
    return _create_runtime_workflow_run_from_data(data)


@runtime_blueprint.route('/api/runtime/runs/<run_id>', methods=['GET'])
@runtime_blueprint.route('/api/runtime/workflow-runs/<run_id>', methods=['GET'])
@login_required
def get_runtime_workflow_run(run_id):
    record, error = _runtime_record_or_404(WorkflowRun, run_id)
    if error:
        return error
    evaluate_runtime_checkpoints(record.id)
    record = db.session.get(WorkflowRun, record.id)
    projection = load_run_projection(record)
    return jsonify(_with_runtime_log_links(serialize_workflow_run(record, projection)))

@runtime_blueprint.route('/api/runtime/runs/<run_id>', methods=['DELETE'])
@runtime_blueprint.route('/api/runtime/workflow-runs/<run_id>', methods=['DELETE'])
@login_required
def delete_runtime_workflow_run(run_id):
    record, error = _runtime_record_or_404(WorkflowRun, run_id)
    if error:
        return error

    artifact_query = RuntimeArtifact.query.filter_by(workflow_run_id=record.id)
    artifact_rows = artifact_query.all()
    artifact_ids = [row.id for row in artifact_rows]
    artifact_version_filters = [RuntimeArtifactVersion.workflow_run_id == record.id]
    if artifact_ids:
        artifact_version_filters.append(RuntimeArtifactVersion.artifact_id.in_(artifact_ids))
    artifact_version_query = RuntimeArtifactVersion.query.filter(or_(*artifact_version_filters))
    artifact_version_rows = artifact_version_query.all()
    artifact_paths = _collect_runtime_artifact_paths([*artifact_rows, *artifact_version_rows])
    step_query = WorkflowStep.query.filter_by(workflow_run_id=record.id)
    source_query = RuntimeSourceRecord.query.filter_by(workflow_run_id=record.id)
    evidence_query = RuntimeEvidenceRecord.query.filter_by(workflow_run_id=record.id)
    approval_query = RuntimeApprovalRequest.query.filter_by(workflow_run_id=record.id)
    resource_query = RuntimeResourceReference.query.filter_by(workflow_run_id=record.id)
    job_query = RuntimeJob.query.filter_by(workflow_run_id=record.id)
    collection_rows = RuntimeCollection.query.filter_by(
        workflow_run_id=record.id
    ).all()
    collection_ids = [row.id for row in collection_rows]
    collection_item_query = RuntimeCollectionItem.query.filter(
        RuntimeCollectionItem.collection_id.in_(collection_ids)
    ) if collection_ids else RuntimeCollectionItem.query.filter(false())
    collection_item_rows = collection_item_query.all()
    collection_query = RuntimeCollection.query.filter_by(workflow_run_id=record.id)

    deleted_counts = {
        "steps": _count_runtime_rows_for_run(WorkflowStep, record.id),
        "sources": _count_runtime_rows_for_run(RuntimeSourceRecord, record.id),
        "evidence": _count_runtime_rows_for_run(RuntimeEvidenceRecord, record.id),
        "artifacts": len(artifact_rows),
        "artifact_versions": len(artifact_version_rows),
        "approvals": _count_runtime_rows_for_run(RuntimeApprovalRequest, record.id),
        "resource_references": _count_runtime_rows_for_run(RuntimeResourceReference, record.id),
        "jobs": _count_runtime_rows_for_run(RuntimeJob, record.id),
        "collections": len(collection_rows),
        "collection_items": len(collection_item_rows),
        "files": len(artifact_paths),
    }

    try:
        evidence_query.delete(synchronize_session=False)
        artifact_version_query.delete(synchronize_session=False)
        artifact_query.delete(synchronize_session=False)
        source_query.delete(synchronize_session=False)
        approval_query.delete(synchronize_session=False)
        resource_query.delete(synchronize_session=False)
        job_query.delete(synchronize_session=False)
        collection_item_query.delete(synchronize_session=False)
        collection_query.delete(synchronize_session=False)
        step_query.delete(synchronize_session=False)
        db.session.delete(record)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("Failed to delete workflow run '%s'.", run_id)
        return jsonify({"error": f"Failed to delete workflow run: {exc}"}), 500

    _cleanup_runtime_artifact_files(run_id=run_id, artifact_paths=artifact_paths)
    return jsonify({
        "success": True,
        "message": "Run deleted successfully.",
        "deleted": deleted_counts,
    })

@runtime_blueprint.route('/api/runtime/runs/<run_id>', methods=['PUT', 'POST', 'PATCH'])
@runtime_blueprint.route('/api/runtime/workflow-runs/<run_id>', methods=['PUT', 'POST', 'PATCH'])
@login_required
def update_runtime_workflow_run(run_id):
    record, error = _runtime_record_or_404(WorkflowRun, run_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    status = normalize_workflow_status(data.get('status')) if data.get('status') else None
    if status and status.lower() not in WORKFLOW_RUN_STATUSES:
        return jsonify({"error": "Invalid workflow run status."}), 400
    try:
        updated = update_workflow_run_planning(
            record.id,
            title=data.get('title') or data.get('name'),
            understanding=data.get('understanding'),
            plan=data.get('plan'),
            state=data.get('state'),
            status=status,
            current_step=data.get('current_step'),
            metadata=data.get('metadata'),
            merge=data.get('replace') is not True,
        )
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify(_with_runtime_log_links(updated.to_dict()))


@runtime_blueprint.route('/api/runtime/runs/<run_id>/start', methods=['POST'])
@runtime_blueprint.route('/api/runtime/workflow-runs/<run_id>/start', methods=['POST'])
@login_required
def start_runtime_workflow_run(run_id):
    record, error = _runtime_record_or_404(WorkflowRun, run_id)
    if error:
        return error
    if record.status in TERMINAL_RUN_STATUSES:
        return jsonify({"error": f"Workflow run is already {record.status}."}), 409
    resume_summary = None
    if str(record.status).lower() == 'paused':
        updated, resume_summary = resume_workflow_run(run_id)
    else:
        updated = set_workflow_run_status(run_id, 'running')
    _trigger_workflow_orchestrator()
    payload = _with_runtime_log_links(updated.to_dict())
    if resume_summary:
        payload['resume_summary'] = resume_summary
    return jsonify(payload)


@runtime_blueprint.route('/api/runtime/runs/<run_id>/cancel', methods=['POST'])
@runtime_blueprint.route('/api/runtime/workflow-runs/<run_id>/cancel', methods=['POST'])
@login_required
def cancel_runtime_workflow_run(run_id):
    record, error = _runtime_record_or_404(WorkflowRun, run_id)
    if error:
        return error
    if record.status in TERMINAL_RUN_STATUSES:
        return jsonify({"error": f"Workflow run is already {record.status}."}), 409
    updated = set_workflow_run_status(run_id, 'cancelled')
    return jsonify(_with_runtime_log_links(updated.to_dict()))


@runtime_blueprint.route('/api/runtime/runs/<run_id>/pause', methods=['POST'])
@runtime_blueprint.route('/api/runtime/workflow-runs/<run_id>/pause', methods=['POST'])
@runtime_blueprint.route('/api/runtime/runs/<run_id>/stop', methods=['POST'])
@runtime_blueprint.route('/api/runtime/workflow-runs/<run_id>/stop', methods=['POST'])
@login_required
def pause_runtime_workflow_run(run_id):
    record, error = _runtime_record_or_404(WorkflowRun, run_id)
    if error:
        return error
    if record.status in TERMINAL_RUN_STATUSES:
        return jsonify({"error": f"Workflow run is already {record.status}."}), 409
    if str(record.status).lower() == 'paused':
        payload = _with_runtime_log_links(record.to_dict())
        payload['pause_summary'] = {"paused_steps": 0, "running_steps": 0, "waiting_steps": 0}
        return jsonify(payload)
    updated, pause_summary = pause_workflow_run(run_id)
    payload = _with_runtime_log_links(updated.to_dict())
    payload['pause_summary'] = pause_summary
    payload['paused_at'] = utc_iso()
    return jsonify(payload)


@runtime_blueprint.route('/api/runtime/runs/<run_id>/resume', methods=['POST'])
@runtime_blueprint.route('/api/runtime/workflow-runs/<run_id>/resume', methods=['POST'])
@login_required
def resume_runtime_workflow_run(run_id):
    record, error = _runtime_record_or_404(WorkflowRun, run_id)
    if error:
        return error
    if record.status in TERMINAL_RUN_STATUSES:
        return jsonify({"error": f"Workflow run is already {record.status}."}), 409
    if str(record.status).lower() != 'paused':
        return jsonify({"error": f"Workflow run is {record.status}. Only paused runs can be resumed."}), 409
    updated, resume_summary = resume_workflow_run(run_id)
    _trigger_workflow_orchestrator()
    payload = _with_runtime_log_links(updated.to_dict())
    payload['resume_summary'] = resume_summary
    payload['resumed_at'] = utc_iso()
    return jsonify(payload)


@runtime_blueprint.route('/api/runtime/steps', methods=['GET'])
@runtime_blueprint.route('/api/runtime/runs/<run_id>/steps', methods=['GET'])
@login_required
def list_runtime_workflow_steps(run_id=None):
    status = request.args.get('status')
    query = _runtime_query_for_user(WorkflowStep)
    if run_id:
        run, error = _runtime_record_or_404(WorkflowRun, run_id)
        if error:
            return error
        query = query.filter(WorkflowStep.workflow_run_id == run.id)
    if status:
        normalized = normalize_workflow_step_status(status)
        if normalized not in WORKFLOW_STEP_STATUSES:
            return jsonify({"error": "Invalid workflow step status."}), 400
        query = query.filter(WorkflowStep.status == normalized)
    rows = query.order_by(WorkflowStep.created_at.desc()).limit(500).all()
    return jsonify([_with_runtime_log_links(row.to_dict()) for row in rows])


@runtime_blueprint.route('/api/runtime/runs/<run_id>/steps', methods=['POST'])
@login_required
def create_runtime_workflow_step(run_id):
    run, error = _runtime_record_or_404(WorkflowRun, run_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    step_type = data.get('step_type') or data.get('type')
    if not step_type:
        return jsonify({"error": "step_type is required."}), 400
    status = normalize_workflow_step_status(data.get('status') or 'planned')
    if status.lower() not in WORKFLOW_STEP_STATUSES:
        return jsonify({"error": "Invalid workflow step status."}), 400
    try:
        record = create_workflow_step(
            workflow_run_id=run.id,
            step_type=step_type,
            title=data.get('title'),
            step_key=data.get('step_key') or data.get('key'),
            status=status,
            sequence=data.get('sequence'),
            parent_step_id=data.get('parent_step_id'),
            depends_on_step_ids=data.get('depends_on_step_ids'),
            assigned_model_role=data.get('assigned_model_role') or data.get('model_role'),
            created_by_user_id=current_user.id,
            instructions=data.get('instructions'),
            capability_ids=data.get('capability_ids'),
            skill_ids=data.get('skill_ids'),
            tool_ids=data.get('tool_ids'),
            provider_requirements=data.get('provider_requirements'),
            resource_reference_ids=data.get('resource_reference_ids'),
            approval_ids=data.get('approval_ids'),
            job_ids=data.get('job_ids'),
            artifact_ids=data.get('artifact_ids'),
            source_ids=data.get('source_ids'),
            evidence_ids=data.get('evidence_ids'),
            input_schema=data.get('input_schema'),
            output_schema=data.get('output_schema'),
            input_payload=data.get('input'),
            output_payload=data.get('output'),
            error=data.get('error'),
            metadata=data.get('metadata'),
            log_source_ids=data.get('log_source_ids'),
            make_current=bool(data.get('make_current')),
        )
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(_with_runtime_log_links(record.to_dict())), 201


@runtime_blueprint.route('/api/runtime/steps/<step_id>', methods=['GET'])
@login_required
def get_runtime_workflow_step(step_id):
    record, error = _runtime_record_or_404(WorkflowStep, step_id)
    if error:
        return error
    return jsonify(_with_runtime_log_links(record.to_dict()))


@runtime_blueprint.route('/api/runtime/steps/<step_id>', methods=['POST', 'PATCH'])
@login_required
def update_runtime_workflow_step(step_id):
    record, error = _runtime_record_or_404(WorkflowStep, step_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    status = normalize_workflow_step_status(data.get('status')) if data.get('status') else None
    if not status:
        return jsonify({"error": "status is required."}), 400
    if status.lower() not in WORKFLOW_STEP_STATUSES:
        return jsonify({"error": "Invalid workflow step status."}), 400
    updated = set_workflow_step_status(
        record.id,
        status,
        output_payload=data.get('output'),
        error=data.get('error'),
        make_current=bool(data.get('make_current')),
    )
    return jsonify(_with_runtime_log_links(updated.to_dict()))


def _collect_downstream_steps(run_id: str, start_step: WorkflowStep) -> list[WorkflowStep]:
    all_steps = WorkflowStep.query.filter_by(workflow_run_id=run_id).all()
    steps_by_id = {s.id: s for s in all_steps}
    steps_by_key = {s.step_key: s for s in all_steps if s.step_key}
    
    from iarf_assistant.core.workflow_executor import WorkflowOrchestrator
    
    # Build adjacency list: node -> dependents
    adj = {s.id: [] for s in all_steps}
    for s in all_steps:
        dep_ids = WorkflowOrchestrator._dependency_ids(s, steps_by_id=steps_by_id, steps_by_key=steps_by_key)
        for dep_id in dep_ids:
            if dep_id in adj:
                adj[dep_id].append(s.id)
                
    # BFS from start_step.id
    visited = {start_step.id}
    queue = [start_step.id]
    result = []
    
    head = 0
    while head < len(queue):
        curr_id = queue[head]
        head += 1
        for neighbor_id in adj.get(curr_id, []):
            if neighbor_id not in visited:
                visited.add(neighbor_id)
                result.append(steps_by_id[neighbor_id])
                queue.append(neighbor_id)
                
    return result


@runtime_blueprint.route('/api/runtime/steps/<step_id>/rerun', methods=['POST'])
@login_required
def rerun_runtime_workflow_step(step_id):
    step, error = _runtime_record_or_404(WorkflowStep, step_id)
    if error:
        return error
    run = db.session.get(WorkflowRun, step.workflow_run_id)
    if not run:
        return jsonify({"error": "Workflow run not found."}), 404
        
    steps_to_reset = _collect_downstream_steps(run.id, step)
    
    from iarf_assistant.core.task_queue import TaskQueue
    reset_ids = []
    with TaskQueue(current_app.instance_path, "core_workflow") as queue:
        for s in [step] + steps_to_reset:
            job_ids = s.job_ids or []
            if job_ids:
                latest_job_id = job_ids[-1]
                if latest_job_id:
                    queue.request_cancel(latest_job_id, reason="Rerun triggered upstream.")

            # Reset step state to planned
            s.status = "planned"
            s.started_at = None
            s.completed_at = None
            s.error = None
            s.output_payload = None

            # Clear retry state
            metadata = dict(s.metadata_json or {})
            if "retry_state" in metadata:
                metadata.pop("retry_state", None)
            s.metadata_json = metadata
            reset_ids.append(s.id)
        
    # Resume run if it was terminal
    if run.status in ("completed", "failed", "cancelled"):
        run.status = "running"
        run.updated_at = utc_now_db()
        
    db.session.commit()
    _trigger_workflow_orchestrator()
    
    return jsonify({
        "success": True,
        "run_id": run.id,
        "reset_step_ids": reset_ids
    }), 200


@runtime_blueprint.route('/api/runtime/sources', methods=['GET'])
@runtime_blueprint.route('/api/runtime/runs/<run_id>/sources', methods=['GET'])
@login_required
def list_runtime_sources(run_id=None):
    query = _runtime_query_for_user(RuntimeSourceRecord)
    if run_id:
        run, error = _runtime_record_or_404(WorkflowRun, run_id)
        if error:
            return error
        query = query.filter(RuntimeSourceRecord.workflow_run_id == run.id)
    source_type = request.args.get('type')
    if source_type:
        query = query.filter(RuntimeSourceRecord.source_type == source_type)
    rows = query.order_by(RuntimeSourceRecord.created_at.desc()).limit(500).all()
    return jsonify([row.to_dict() for row in rows])


@runtime_blueprint.route('/api/runtime/runs/<run_id>/sources', methods=['POST'])
@login_required
def create_runtime_source(run_id):
    run, error = _runtime_record_or_404(WorkflowRun, run_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    source_type = data.get('source_type') or data.get('type')
    uri = data.get('uri') or data.get('resource_uri') or data.get('url')
    if not source_type or not uri:
        return jsonify({"error": "source_type and uri are required."}), 400
    try:
        record = create_source_record(
            workflow_run_id=run.id,
            source_type=source_type,
            uri=uri,
            step_id=data.get('step_id'),
            provider=data.get('provider'),
            external_id=data.get('external_id'),
            title=data.get('title'),
            summary=data.get('summary'),
            content_hash=data.get('content_hash'),
            access_scope=data.get('access_scope') or 'private',
            created_by_user_id=current_user.id,
            metadata=data.get('metadata'),
        )
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(record.to_dict()), 201


@runtime_blueprint.route('/api/runtime/sources/<source_id>', methods=['GET'])
@login_required
def get_runtime_source(source_id):
    record, error = _runtime_record_or_404(RuntimeSourceRecord, source_id)
    if error:
        return error
    return jsonify(record.to_dict())


@runtime_blueprint.route('/api/runtime/evidence', methods=['GET'])
@runtime_blueprint.route('/api/runtime/runs/<run_id>/evidence', methods=['GET'])
@login_required
def list_runtime_evidence(run_id=None):
    query = _runtime_query_for_user(RuntimeEvidenceRecord)
    if run_id:
        run, error = _runtime_record_or_404(WorkflowRun, run_id)
        if error:
            return error
        query = query.filter(RuntimeEvidenceRecord.workflow_run_id == run.id)
    source_id = request.args.get('source_id')
    if source_id:
        query = query.filter(RuntimeEvidenceRecord.source_id == source_id)
    rows = query.order_by(RuntimeEvidenceRecord.created_at.desc()).limit(500).all()
    return jsonify([row.to_dict() for row in rows])


@runtime_blueprint.route('/api/runtime/runs/<run_id>/evidence', methods=['POST'])
@login_required
def create_runtime_evidence(run_id):
    run, error = _runtime_record_or_404(WorkflowRun, run_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    source_id = data.get('source_id')
    if not source_id:
        return jsonify({"error": "source_id is required."}), 400
    source, source_error = _runtime_record_or_404(RuntimeSourceRecord, source_id)
    if source_error:
        return source_error
    if source.workflow_run_id != run.id:
        return jsonify({"error": "source_id does not belong to this run."}), 400
    try:
        record = create_evidence_record(
            workflow_run_id=run.id,
            source_id=source.id,
            step_id=data.get('step_id'),
            artifact_id=data.get('artifact_id'),
            claim=data.get('claim'),
            quote=data.get('quote'),
            citation=data.get('citation'),
            relevance_score=data.get('relevance_score'),
            novelty_score=data.get('novelty_score'),
            used_in_artifact=bool(data.get('used_in_artifact')),
            created_by_user_id=current_user.id,
            metadata=data.get('metadata'),
        )
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(record.to_dict()), 201


@runtime_blueprint.route('/api/runtime/evidence/<evidence_id>', methods=['GET'])
@login_required
def get_runtime_evidence(evidence_id):
    record, error = _runtime_record_or_404(RuntimeEvidenceRecord, evidence_id)
    if error:
        return error
    return jsonify(record.to_dict())


def register_runtime_blueprint(app):
    app.register_blueprint(runtime_blueprint)
