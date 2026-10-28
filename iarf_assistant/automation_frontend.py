from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from flask import Blueprint, current_app, jsonify, request
from flask_login import current_user, login_required
from sqlalchemy import and_, or_

from iarf_assistant.core.frontend_contracts import set_frontend_api_version_headers
from iarf_assistant.core.workflow_templates import (
    TemplateDefinitionError,
    active_template_definition,
    compile_template_definition,
    read_template_storage,
)
from iarf_assistant.models import WorkflowTemplate

frontend_automation_blueprint = Blueprint(
    "frontend_automation_api",
    __name__,
)
MAX_AUTOMATION_TEMPLATES = 160
MAX_AUTOMATION_WORKFLOWS = 160


def _is_admin() -> bool:
    return bool(getattr(current_user, "is_admin", False))


def _visible_template_query():
    query = WorkflowTemplate.query
    if _is_admin():
        return query
    user_id = int(current_user.id)
    return query.filter(
        or_(
            WorkflowTemplate.owner_id == user_id,
            and_(
                WorkflowTemplate.owner_id.is_(None),
                WorkflowTemplate.is_active.is_(True),
            ),
        )
    )


def _visible_template(template_id: str) -> WorkflowTemplate | None:
    return _visible_template_query().filter(WorkflowTemplate.id == template_id).first()


def _template_scope(record: WorkflowTemplate) -> str:
    if record.owner_id is None:
        return "shared"
    if int(record.owner_id) == int(current_user.id):
        return "personal"
    return "managed"


def _template_payload(
    record: WorkflowTemplate,
    *,
    include_definition: bool = False,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": str(record.id),
        "title": str(record.title or "Untitled automation"),
        "description": str(record.description or ""),
        "owner_id": record.owner_id,
        "scope": _template_scope(record),
        "editable": bool(
            _is_admin()
            or (
                record.owner_id is not None
                and int(record.owner_id) == int(current_user.id)
            )
        ),
        "is_active": bool(record.is_active),
        "created_at": (
            record.created_at.isoformat() if record.created_at else None
        ),
        "updated_at": (
            record.updated_at.isoformat() if record.updated_at else None
        ),
    }
    try:
        storage = read_template_storage(record.definition_json)
        definition = active_template_definition(storage)
    except TemplateDefinitionError as exc:
        return {
            **base,
            "active_version": None,
            "version_count": 0,
            "step_count": 0,
            "definition_error": str(exc),
            **(
                {
                    "definition": {
                        "version": 1,
                        "name": str(
                            record.title or "Untitled automation"
                        ),
                        "description": str(record.description or ""),
                        "steps": [],
                    },
                    "versions": [],
                }
                if include_definition
                else {}
            ),
        }

    payload = {
        **base,
        "active_version": int(storage["active_version"]),
        "version_count": len(storage["versions"]),
        "step_count": len(definition.get("steps") or []),
        "definition_error": None,
    }
    if include_definition:
        payload["definition"] = definition
        payload["versions"] = [
            {
                "version": int(item["version"]),
                "name": str(
                    (item.get("definition") or {}).get("name")
                    or record.title
                ),
                "description": str(
                    (item.get("definition") or {}).get("description")
                    or ""
                ),
                "step_count": len(
                    (item.get("definition") or {}).get("steps") or []
                ),
                "created_at": item.get("created_at"),
                "created_by_user_id": item.get("created_by_user_id"),
                "change_summary": str(item.get("change_summary") or ""),
                "active": int(item["version"])
                == int(storage["active_version"]),
            }
            for item in storage["versions"]
        ]
    return payload


def _safe_frontend_href(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw or any(character in raw for character in ("\r", "\n", "\\")):
        return None
    if raw.startswith("/") and not raw.startswith("//"):
        return raw
    parsed = urlsplit(raw)
    if (
        parsed.scheme in {"http", "https"}
        and parsed.netloc
        and not parsed.username
        and not parsed.password
    ):
        return raw
    return None


def _named_record(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        text = str(value or "").strip()
        return {"id": text, "label": text, "type": ""} if text else None
    record_id = str(
        value.get("id")
        or value.get("key")
        or value.get("name")
        or value.get("label")
        or ""
    ).strip()
    label = str(
        value.get("label")
        or value.get("name")
        or value.get("title")
        or record_id
    ).strip()
    if not record_id and not label:
        return None
    return {
        "id": record_id or label,
        "label": label or record_id,
        "type": str(value.get("type") or value.get("kind") or ""),
    }


def _named_records(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    records = []
    for value in values[:80]:
        record = _named_record(value)
        if record:
            records.append(record)
    return records


def _link_record(value: Any, *, kind: str) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    link_id = str(value.get("id") or value.get("name") or kind).strip()
    label = str(
        value.get("label")
        or value.get("name")
        or value.get("title")
        or link_id
    ).strip()
    href = _safe_frontend_href(
        value.get("url") or value.get("href") or value.get("path")
    )
    if not link_id and not label:
        return None
    return {
        "id": link_id or label,
        "label": label or link_id,
        "href": href,
        "kind": kind,
    }


def _definition_from_workflow(workflow: dict[str, Any]) -> dict[str, Any] | None:
    metadata = workflow.get("metadata")
    if not isinstance(metadata, dict):
        return None
    for key in ("definition", "workflow_definition", "execution_definition"):
        candidate = metadata.get(key)
        if isinstance(candidate, dict):
            return candidate
    if isinstance(metadata.get("steps"), list):
        return {
            "version": metadata.get("version") or 1,
            "name": metadata.get("name") or workflow.get("name"),
            "description": (
                metadata.get("description") or workflow.get("description")
            ),
            "steps": metadata.get("steps"),
        }
    return None


def _workflow_payload(workflow: dict[str, Any]) -> dict[str, Any]:
    raw_status = (
        workflow.get("status")
        if isinstance(workflow.get("status"), dict)
        else {}
    )
    state = str(raw_status.get("state") or "unknown")
    definition = _definition_from_workflow(workflow)
    compiled = None
    definition_error = None
    if definition:
        try:
            compiled = compile_template_definition(
                definition,
                fallback_name=str(
                    workflow.get("name") or workflow.get("id") or "Workflow"
                ),
            )
        except TemplateDefinitionError as exc:
            definition_error = str(exc)

    steps = []
    if compiled:
        steps = [
            {
                "key": str(step["key"]),
                "title": str(step["title"]),
                "type": str(step["type"]),
                "depends_on": [str(value) for value in step["depends_on"]],
                "assigned_model_role": step.get("assigned_model_role"),
                "instructions": step.get("instructions"),
                "capability_ids": [
                    str(value) for value in step.get("capability_ids") or []
                ],
                "skill_ids": [
                    str(value) for value in step.get("skill_ids") or []
                ],
                "tool_ids": [
                    str(value) for value in step.get("tool_ids") or []
                ],
                "provider_requirements": [
                    str(value)
                    for value in step.get("provider_requirements") or []
                ],
            }
            for step in compiled.steps
        ]

    requirements = {
        key: [
            str(value)
            for value in workflow.get(source_key) or []
            if str(value).strip()
        ]
        for key, source_key in (
            ("addons", "required_addons"),
            ("tools", "required_tools"),
            ("tool_views", "required_tool_views"),
            ("helpers", "required_helpers"),
            ("providers", "required_providers"),
            ("services", "required_services"),
            ("workers", "required_workers"),
            ("auth_scopes", "auth_scopes"),
            ("resource_scopes", "resource_scopes"),
        )
    }

    links: list[dict[str, Any]] = []
    launch_url = _safe_frontend_href(workflow.get("launch_url"))
    if launch_url:
        links.append(
            {
                "id": "launch",
                "label": "Open workspace",
                "href": launch_url,
                "kind": "launch",
            }
        )
    for kind, key in (
        ("state", "state_links"),
        ("log", "log_links"),
        ("resume", "resume_links"),
    ):
        for value in workflow.get(key) or []:
            link = _link_record(value, kind=kind)
            if link and not any(
                existing["id"] == link["id"]
                and existing["kind"] == link["kind"]
                for existing in links
            ):
                links.append(link)

    return {
        "id": str(workflow.get("id") or ""),
        "name": str(workflow.get("name") or workflow.get("id") or "Workflow"),
        "description": str(workflow.get("description") or ""),
        "addon_id": str(workflow.get("addon_id") or ""),
        "status": {
            "state": state,
            "reasons": [
                str(value)
                for value in raw_status.get("reasons") or []
                if str(value).strip()
            ],
        },
        "available": state in {"active", "degraded"},
        "dry_run_supported": bool(workflow.get("dry_run_supported", True)),
        "execution_supported": compiled is not None,
        "definition_error": definition_error,
        "steps": steps,
        "requirements": requirements,
        "approval_gates": [
            str(value)
            for value in workflow.get("approval_gates") or []
            if str(value).strip()
        ],
        "expected_jobs": _named_records(workflow.get("expected_jobs")),
        "expected_artifacts": _named_records(
            workflow.get("expected_artifacts")
        ),
        "links": links,
        "launch_url": launch_url,
    }


def _requirement_group(
    group_id: str,
    values: Any,
) -> dict[str, Any]:
    items = []
    for value in values if isinstance(values, list) else []:
        if not isinstance(value, dict):
            continue
        state = str(value.get("state") or "unknown")
        items.append(
            {
                "id": str(value.get("id") or ""),
                "registered": bool(value.get("registered")),
                "state": state,
                "ready": bool(
                    value.get("registered")
                    and state in {"active", "degraded"}
                ),
                "reasons": [
                    str(reason)
                    for reason in value.get("reasons") or []
                    if str(reason).strip()
                ],
            }
        )
    return {
        "id": group_id,
        "label": group_id.replace("_", " ").title(),
        "total": len(items),
        "ready": sum(item["ready"] for item in items),
        "items": items,
    }


@frontend_automation_blueprint.after_request
def _frontend_automation_headers(response):
    set_frontend_api_version_headers(response)
    response.headers["Cache-Control"] = "private, no-cache"
    return response


@frontend_automation_blueprint.route(
    "/api/frontend/automations",
    methods=["GET"],
)
@login_required
def get_frontend_automations():
    templates = (
        _visible_template_query()
        .order_by(WorkflowTemplate.updated_at.desc())
        .limit(MAX_AUTOMATION_TEMPLATES)
        .all()
    )
    template_payloads = [
        _template_payload(record) for record in templates
    ]

    manager = getattr(current_app, "addon_manager", None)
    declared = manager.get_workflow_manifest() if manager else []
    workflow_payloads = [
        _workflow_payload(workflow)
        for workflow in declared[:MAX_AUTOMATION_WORKFLOWS]
        if isinstance(workflow, dict)
    ]
    workflow_payloads.sort(
        key=lambda item: (item["addon_id"], item["name"], item["id"])
    )

    return jsonify(
        {
            "templates": template_payloads,
            "workflows": workflow_payloads,
            "summary": {
                "templates": len(template_payloads),
                "personal_templates": sum(
                    item["scope"] == "personal"
                    for item in template_payloads
                ),
                "addon_workflows": len(workflow_payloads),
                "executable_workflows": sum(
                    item["execution_supported"]
                    for item in workflow_payloads
                ),
                "attention": sum(
                    bool(item["definition_error"])
                    or item["status"]["state"] == "degraded"
                    for item in workflow_payloads
                )
                + sum(
                    bool(item["definition_error"])
                    for item in template_payloads
                ),
            },
        }
    )


@frontend_automation_blueprint.route(
    "/api/frontend/automations/templates/<template_id>",
    methods=["GET"],
)
@login_required
def get_frontend_automation_template(template_id: str):
    record = _visible_template(template_id)
    if not record:
        return jsonify({"error": "Automation template not found."}), 404
    return jsonify(_template_payload(record, include_definition=True))


@frontend_automation_blueprint.route(
    "/api/frontend/automations/workflows/<path:workflow_id>/readiness",
    methods=["GET", "POST"],
)
@login_required
def get_frontend_workflow_readiness(workflow_id: str):
    manager = getattr(current_app, "addon_manager", None)
    if not manager:
        return jsonify({"error": "Addon manager not available."}), 503
    payload = request.get_json(silent=True) or {}
    try:
        result = manager.validate_workflow_dry_run(
            workflow_id,
            payload=payload,
        )
    except KeyError:
        return jsonify({"error": "Automation workflow not found."}), 404

    workflow = (
        result.get("workflow")
        if isinstance(result.get("workflow"), dict)
        else {}
    )
    requirements = (
        result.get("requirements")
        if isinstance(result.get("requirements"), dict)
        else {}
    )
    workflow_payload = _workflow_payload(workflow)
    return jsonify(
        {
            "workflow": workflow_payload,
            "available": bool(result.get("available")),
            "can_dry_run": bool(result.get("can_dry_run")),
            "execution_supported": bool(
                result.get("full_execution_supported")
            ),
            "message": str(result.get("message") or ""),
            "blocking_reasons": [
                str(value)
                for value in result.get("blocking_reasons") or []
                if str(value).strip()
            ],
            "step_count": int(
                (
                    result.get("execution_support")
                    if isinstance(result.get("execution_support"), dict)
                    else {}
                ).get("step_count")
                or 0
            ),
            "requirements": [
                _requirement_group(group_id, values)
                for group_id, values in requirements.items()
                if values
            ],
            "approval_gates": [
                str(value)
                for value in result.get("approval_gates") or []
                if str(value).strip()
            ],
            "expected_jobs": _named_records(result.get("expected_jobs")),
            "expected_artifacts": _named_records(
                result.get("expected_artifacts")
            ),
            "links": workflow_payload["links"],
        }
    )


def register_frontend_automation_blueprint(app) -> None:
    app.register_blueprint(frontend_automation_blueprint)


__all__ = [
    "MAX_AUTOMATION_TEMPLATES",
    "MAX_AUTOMATION_WORKFLOWS",
    "frontend_automation_blueprint",
    "register_frontend_automation_blueprint",
]
