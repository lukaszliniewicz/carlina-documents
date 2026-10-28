from flask import Blueprint, current_app, jsonify, redirect, request
from flask_login import login_required
from werkzeug.exceptions import BadRequest

from iarf_assistant.core.addons.source_coverage import (
    MAX_SOURCE_CLASSES,
    MAX_SOURCE_TEXT_LENGTH,
    MAX_TASK_TEXT_LENGTH,
)

discovery_blueprint = Blueprint("discovery_api", __name__)


def _addon_manager():
    return getattr(current_app, "addon_manager", None)


@discovery_blueprint.route("/status")
@login_required
def status():
    """Returns the server readiness and discoverable addon registry surfaces."""
    if hasattr(current_app, "addon_manager_error"):
        return jsonify(
            {
                "status": "error",
                "message": f"Server failed to initialize: {current_app.addon_manager_error}",
            }
        ), 500

    manager = _addon_manager()
    if manager:
        return jsonify(
            {
                "status": "ready",
                "plugins": manager.get_plugin_manifest(),
                "automations": manager.get_automation_manifest(),
                "capabilities": manager.get_capability_manifest(),
                "skills": manager.get_skill_manifest(),
                "workflows": manager.get_workflow_manifest(),
                "ui_components": manager.get_ui_component_manifest(),
                "ui_contributions": manager.get_ui_contribution_manifest(),
            }
        )

    return jsonify(
        {"status": "error", "message": "Server is not ready. Check logs."}
    ), 500


@discovery_blueprint.route("/plugins", methods=["GET"])
@login_required
def list_plugins():
    """Returns a compatibility manifest of available plugin/toolkit records."""
    manager = _addon_manager()
    if manager:
        return jsonify(manager.get_plugin_manifest())
    return jsonify({"error": "Addon manager not available."}), 500


@discovery_blueprint.route("/api/slash-commands", methods=["GET"])
@login_required
def get_slash_commands():
    """Returns a manifest of all available slash commands."""
    manager = _addon_manager()
    if manager:
        return jsonify(manager.get_slash_commands())
    return jsonify([])


@discovery_blueprint.route("/api/capabilities", methods=["GET"])
@login_required
def get_capabilities():
    """Returns user-facing capabilities derived from active addons and compatibility records."""
    manager = _addon_manager()
    if manager:
        return jsonify(manager.get_capability_manifest())
    return jsonify([])


@discovery_blueprint.route("/api/capabilities/search", methods=["GET", "POST"])
@login_required
def search_capabilities():
    """Returns compact capability cards for UI/chat discovery."""
    manager = _addon_manager()
    if not manager:
        return jsonify([])
    data = request.get_json(silent=True) or {}
    query = request.args.get("q") or data.get("query") or ""
    groups = data.get("groups") or request.args.getlist("group")
    capability_types = data.get("capability_types") or request.args.getlist("type")
    limit = data.get("limit") or request.args.get("limit") or 20
    return jsonify(
        manager.search_capabilities(
            query=query, groups=groups, capability_types=capability_types, limit=limit
        )
    )


@discovery_blueprint.route("/api/capabilities/describe", methods=["POST"])
@login_required
def describe_capabilities():
    """Returns operational details for selected capabilities."""
    manager = _addon_manager()
    if not manager:
        return jsonify([])
    data = request.get_json(silent=True) or {}
    return jsonify(
        manager.describe_capabilities(
            data.get("capability_ids") or data.get("ids") or []
        )
    )


@discovery_blueprint.route("/api/capabilities/guidance", methods=["POST"])
@login_required
def get_capability_guidance():
    """Returns retrievable guidance for selected capabilities."""
    manager = _addon_manager()
    if not manager:
        return jsonify([])
    data = request.get_json(silent=True) or {}
    return jsonify(
        manager.get_guidance(
            data.get("capability_ids") or data.get("ids") or [], task=data.get("task")
        )
    )


@discovery_blueprint.route("/api/capabilities/source-coverage", methods=["GET", "POST"])
@login_required
def get_capability_source_coverage():
    """Returns compact source-class coverage rows for multi-repository planning."""
    manager = _addon_manager()
    if not manager:
        return jsonify({"source_classes": []})
    raw_data = None
    if request.method == "POST" and request.content_length:
        if not request.is_json:
            return jsonify({"error": "Request body must be JSON."}), 400
        try:
            raw_data = request.get_json(silent=False)
        except BadRequest:
            return jsonify({"error": "Request body contains malformed JSON."}), 400
    if raw_data is not None and not isinstance(raw_data, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400
    data = raw_data or {}
    allowed_fields = {
        "include_tool_cards",
        "max_tools_per_source",
        "source_classes",
        "task",
    }
    unknown_fields = sorted(set(data) - allowed_fields)
    if unknown_fields:
        return jsonify(
            {"error": f"Unknown request fields: {', '.join(unknown_fields)}."}
        ), 400

    source_classes = (
        data.get("source_classes")
        if "source_classes" in data
        else request.args.getlist("source")
    )
    if source_classes is None:
        source_classes = []
    if not isinstance(source_classes, list) or any(
        not isinstance(value, str) for value in source_classes
    ):
        return jsonify({"error": "source_classes must be an array of strings."}), 400
    if len(source_classes) > MAX_SOURCE_CLASSES or any(
        len(value) > MAX_SOURCE_TEXT_LENGTH for value in source_classes
    ):
        return jsonify(
            {
                "error": (
                    f"At most {MAX_SOURCE_CLASSES} source classes of "
                    f"{MAX_SOURCE_TEXT_LENGTH} characters are allowed."
                )
            }
        ), 400

    task = request.args.get("task") or data.get("task") or ""
    if not isinstance(task, str) or len(task) > MAX_TASK_TEXT_LENGTH:
        return jsonify(
            {
                "error": f"task must be a string of at most {MAX_TASK_TEXT_LENGTH} characters."
            }
        ), 400

    raw_max_tools = (
        data.get("max_tools_per_source")
        if "max_tools_per_source" in data
        else request.args.get("max_tools_per_source", 4)
    )
    if isinstance(raw_max_tools, bool):
        return jsonify({"error": "max_tools_per_source must be an integer."}), 400
    try:
        max_tools = int(raw_max_tools)
    except (TypeError, ValueError):
        return jsonify({"error": "max_tools_per_source must be an integer."}), 400
    if not 1 <= max_tools <= 20:
        return jsonify({"error": "max_tools_per_source must be between 1 and 20."}), 400

    include_tool_cards = data.get("include_tool_cards")
    if include_tool_cards is not None and not isinstance(include_tool_cards, bool):
        return jsonify({"error": "include_tool_cards must be a boolean."}), 400
    if include_tool_cards is None:
        query_value = str(request.args.get("include_tool_cards") or "").lower()
        if query_value not in {"", "0", "1", "false", "no", "true", "yes"}:
            return jsonify({"error": "include_tool_cards must be a boolean."}), 400
        include_tool_cards = query_value in {"1", "true", "yes"}
    return jsonify(
        manager.get_source_coverage(
            source_classes=source_classes,
            task=task,
            max_tools_per_source=max_tools,
            include_tool_cards=bool(include_tool_cards),
        )
    )


@discovery_blueprint.route("/api/skills", methods=["GET"])
@login_required
def get_skills():
    """Returns discoverable skills from addon and personal skill registries."""
    manager = _addon_manager()
    if manager:
        return jsonify(manager.get_skill_manifest())
    return jsonify([])


@discovery_blueprint.route("/api/skills/<path:skill_id>", methods=["GET"])
@login_required
def get_skill_detail(skill_id):
    """Returns a skill plus its narrowed/open tool schemas and UI component discovery metadata."""
    manager = _addon_manager()
    if not manager:
        return jsonify({"error": "Addon manager not available."}), 500
    try:
        return jsonify(manager.describe_skill(skill_id))
    except KeyError:
        return jsonify({"error": f"Skill '{skill_id}' not found."}), 404
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 409


@discovery_blueprint.route("/api/workflows", methods=["GET"])
@login_required
def get_workflows():
    """Returns discoverable workflow metadata without claiming execution support."""
    manager = _addon_manager()
    if manager:
        return jsonify(manager.get_workflow_manifest())
    return jsonify([])


@discovery_blueprint.route("/api/tools", methods=["GET"])
@login_required
def get_tools():
    """Returns compact registered tool metadata for scoped selection."""
    manager = _addon_manager()
    if manager:
        return jsonify(manager.get_tool_manifest())
    return jsonify([])


@discovery_blueprint.route("/api/tool-views", methods=["GET"])
@login_required
def get_tool_views():
    """Returns compact tool-view metadata for scoped selection."""
    manager = _addon_manager()
    if manager:
        return jsonify(manager.get_tool_view_manifest())
    return jsonify([])


@discovery_blueprint.route("/api/ui-contributions", methods=["GET"])
@login_required
def get_ui_contributions():
    """Returns discoverable user-facing UI contributions."""
    manager = _addon_manager()
    if manager:
        return jsonify(manager.get_ui_contribution_manifest())
    return jsonify([])


@discovery_blueprint.route("/api/ui-components", methods=["GET"])
@login_required
def get_ui_components():
    """Returns discoverable UI component records declared by addons."""
    manager = _addon_manager()
    if manager:
        return jsonify(manager.get_ui_component_manifest())
    return jsonify([])


@discovery_blueprint.route(
    "/api/workflows/<path:workflow_id>/dry-run", methods=["GET", "POST"]
)
@login_required
def dry_run_workflow(workflow_id):
    """Validates workflow dependencies without creating a stateful workflow run."""
    manager = _addon_manager()
    if not manager:
        return jsonify({"error": "Addon manager not available."}), 500
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(manager.validate_workflow_dry_run(workflow_id, payload=payload))
    except KeyError:
        return jsonify({"error": f"Workflow '{workflow_id}' not found."}), 404


@discovery_blueprint.route(
    "/api/workflows/<path:workflow_id>/launch", methods=["GET", "POST"]
)
@login_required
def launch_workflow(workflow_id):
    """Returns or redirects to a lightweight workflow handoff URL when declared."""
    manager = _addon_manager()
    if not manager:
        return jsonify({"error": "Addon manager not available."}), 500
    try:
        launch = manager.get_workflow_launch(workflow_id)
    except KeyError:
        return jsonify({"error": f"Workflow '{workflow_id}' not found."}), 404
    if not launch["launch_supported"]:
        return jsonify(launch), 409
    if request.method == "GET" and request.args.get("redirect") == "1":
        return redirect(launch["launch_url"])
    return jsonify(launch)


def register_discovery_blueprint(app):
    app.register_blueprint(discovery_blueprint)
