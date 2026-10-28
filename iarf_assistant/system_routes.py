import json
import logging
from datetime import date, timedelta

from docker.errors import DockerException
from flask import (
    Blueprint,
    Response,
    current_app,
    jsonify,
    request,
    stream_with_context,
)
from flask_login import current_user, login_required
from sqlalchemy import desc, func
from sqlalchemy.exc import SQLAlchemyError

from carlina.core.utc import utc_now
from carlina.runtime_restart import (
    _infer_restart_target_from_request as _infer_restart_target_from_request,
)
from carlina.runtime_restart import (
    restart_server as restart_server,
)
from carlina.runtime_restart import (
    restart_status as restart_status,
)
from iarf_assistant.core.logging_observability import redact_text
from iarf_assistant.core.persistence.query_metrics import query_metrics_snapshot
from iarf_assistant.extensions import db
from iarf_assistant.models import DataContext, LLMCost, User
from iarf_assistant.security import admin_required

_LOGGER = logging.getLogger(__name__)
system_blueprint = Blueprint("system_api", __name__)


system_blueprint.add_url_rule(
    "/api/system/restart", endpoint="restart_server", view_func=restart_server, methods=["POST"]
)
system_blueprint.add_url_rule(
    "/api/system/restart-status", endpoint="restart_status", view_func=restart_status
)


@system_blueprint.route("/api/statistics")
@login_required
@admin_required
def get_statistics():
    start_date_str = request.args.get("start_date")
    end_date_str = request.args.get("end_date")

    try:
        if not end_date_str:
            end_date = utc_now().date()
        else:
            end_date = date.fromisoformat(end_date_str)

        if not start_date_str:
            start_date = end_date - timedelta(days=6)
        else:
            start_date = date.fromisoformat(start_date_str)

        end_date_inclusive = end_date + timedelta(days=1)

        date_expression = func.date(LLMCost.timestamp)
        daily_source_stats = (
            db.session.query(
                date_expression.label("date"),
                LLMCost.source,
                func.sum(LLMCost.cost).label("total_cost"),
                func.sum(LLMCost.total_tokens).label("total_tokens"),
            )
            .filter(
                LLMCost.timestamp >= start_date,
                LLMCost.timestamp < end_date_inclusive,
            )
            .group_by(date_expression, LLMCost.source)
            .order_by(desc(date_expression), LLMCost.source)
            .all()
        )

        cost_expression = func.sum(LLMCost.cost)
        user_stats = (
            db.session.query(
                User.email,
                cost_expression.label("total_cost"),
                func.sum(LLMCost.total_tokens).label("total_tokens"),
            )
            .join(User, User.id == LLMCost.user_id)
            .filter(
                LLMCost.timestamp >= start_date,
                LLMCost.timestamp < end_date_inclusive,
            )
            .group_by(User.email)
            .order_by(desc(cost_expression))
            .all()
        )

        daily_results = [
            {
                "date": row.date,
                "source": row.source,
                "total_cost": row.total_cost,
                "total_tokens": row.total_tokens,
            }
            for row in daily_source_stats
        ]
        user_results = [
            {
                "email": row.email,
                "total_cost": row.total_cost,
                "total_tokens": row.total_tokens,
            }
            for row in user_stats
        ]

        return jsonify(
            {
                "daily_by_source": daily_results,
                "by_user": user_results,
                "database": query_metrics_snapshot(),
            }
        )
    except (SQLAlchemyError, ValueError) as e:
        _LOGGER.exception("Error fetching statistics.")
        return jsonify({"error": str(e)}), 500


@system_blueprint.route("/api/system/rebuild-rag", methods=["GET", "POST"])
@login_required
@admin_required
def rebuild_rag_api():
    """Drops and rebuilds the VSS tables, re-embedding all chunks."""

    def generate():
        yield f"data: {json.dumps({'message': 'Clearing vector tables...', 'progress': 0})}\n\n"
        try:
            current_app.rag_service.delete_all_embeddings()
            yield f"data: {json.dumps({'message': 'Vector schema updated. Gathering contexts...', 'progress': 5})}\n\n"

            all_contexts = DataContext.query.all()
            total = len(all_contexts)

            if total == 0:
                yield f"data: {json.dumps({'message': 'No data contexts to index. Complete.', 'progress': 100, 'done': True})}\n\n"
                return

            for i, context in enumerate(all_contexts):
                current_app.rag_service.generate_embeddings_for_context(context.id)
                progress = 5 + int((i / total) * 85)
                yield f"data: {json.dumps({'message': f'Indexing item {i + 1} of {total}...', 'progress': progress})}\n\n"

            yield f"data: {json.dumps({'message': 'Notifying addons to rebuild isolated vector stores...', 'progress': 95})}\n\n"
            current_app.addon_manager.notify_rag_rebuild()

            yield f"data: {json.dumps({'message': 'RAG index successfully rebuilt!', 'progress': 100, 'done': True})}\n\n"

        except Exception as e:
            _LOGGER.exception("Error during API RAG rebuild")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


def _docker_manager_or_error():
    manager = getattr(current_app, "addon_manager", None)
    if not manager:
        return None, (jsonify({"error": "Addon manager is not initialized."}), 503)

    manager._refresh_docker_status(log_failure=False)
    if not manager.docker_available or not manager.docker_client:
        return None, (jsonify({"error": "Docker is not available."}), 503)
    return manager, None


@system_blueprint.route("/api/system/docker/status", methods=["GET"])
@login_required
@admin_required
def docker_status():
    manager = getattr(current_app, "addon_manager", None)
    if not manager:
        return jsonify(
            {
                "available": False,
                "message": "Addon manager is not initialized.",
            }
        ), 503

    force_refresh = str(request.args.get("refresh") or "").lower() in {
        "1",
        "true",
        "yes",
    }
    manager._refresh_docker_status(
        log_failure=False,
        force=force_refresh,
    )
    return jsonify(
        {
            "available": manager.docker_available,
            "checked": manager.docker_status_checked,
            "message": manager.docker_status_message,
            "runtime_kind": getattr(manager, "_container_runtime_kind", "none"),
            "wsl_distro": getattr(manager, "_docker_wsl_distro", None),
            "log_links": current_app.log_registry.get_links_by_category("docker")
            if hasattr(current_app, "log_registry")
            else [],
        }
    )


@system_blueprint.route("/api/system/docker/containers", methods=["GET"])
@login_required
@admin_required
def docker_containers():
    manager, error = _docker_manager_or_error()
    if error:
        return error

    try:
        show_all = request.args.get("all", "false").lower() == "true"
        containers = manager.docker_client.containers.list(all=True)
        result = []

        for c in containers:
            identity = manager.identify_container_resource(c)
            if not show_all and identity is None:
                continue

            image_label = (
                c.image.tags[0]
                if c.image and c.image.tags
                else (c.image.id[:15] if c.image and c.image.id else "<unknown>")
            )
            log_links = []
            if hasattr(current_app, "log_registry") and identity is not None:
                addon_id = str(identity["addon_id"])
                service_name = str(identity["service_name"])
                source_id = f"docker.{addon_id}.{service_name}"
                try:
                    log_links.append(
                        current_app.log_registry.get_link_for_source(source_id)
                    )
                except KeyError:
                    try:
                        current_app.log_registry.register_docker_source(
                            source_id.replace("docker.", "", 1),
                            name=f"{addon_id} {service_name} Docker",
                            addon_id=addon_id,
                        )
                        log_links.append(
                            current_app.log_registry.get_link_for_source(source_id)
                        )
                    except KeyError, OSError, RuntimeError, TypeError, ValueError:
                        log_links = []
            result.append(
                {
                    "id": c.short_id,
                    "name": c.name,
                    "status": c.status,
                    "image": image_label,
                    "created": c.attrs.get("Created", ""),
                    "log_links": log_links,
                    "managed": identity is not None,
                    "legacy": bool(identity and identity.get("legacy")),
                    "addon_id": identity.get("addon_id") if identity else None,
                    "service_name": identity.get("service_name") if identity else None,
                }
            )

        result.sort(key=lambda item: item["name"])
        return jsonify(result)
    except (DockerException, OSError, RuntimeError, TypeError, ValueError) as e:
        return jsonify({"error": str(e)}), 500


@system_blueprint.route(
    "/api/system/docker/containers/<container_id>/details", methods=["GET"]
)
@login_required
@admin_required
def docker_container_details(container_id):
    manager, error = _docker_manager_or_error()
    if error:
        return error

    try:
        c = manager.docker_client.containers.get(container_id)
        logs = redact_text(
            c.logs(tail=600).decode("utf-8", errors="replace"), max_length=250_000
        )
        stats = {"memory": "N/A", "cpu": "N/A"}

        if c.status == "running":
            raw_stats = c.stats(stream=False)

            memory_stats = raw_stats.get("memory_stats", {}) or {}
            mem_usage = memory_stats.get("usage", 0) or 0
            mem_limit = memory_stats.get("limit", 0) or 0
            if mem_limit > 0:
                mem_percent = (mem_usage / mem_limit) * 100.0
                stats["memory"] = (
                    f"{mem_usage / (1024 * 1024):.1f} MB / {mem_limit / (1024 * 1024):.1f} MB ({mem_percent:.1f}%)"
                )

            cpu_stats = raw_stats.get("cpu_stats", {}) or {}
            precpu_stats = raw_stats.get("precpu_stats", {}) or {}
            cpu_delta = (cpu_stats.get("cpu_usage", {}) or {}).get("total_usage", 0) - (
                precpu_stats.get("cpu_usage", {}) or {}
            ).get("total_usage", 0)
            sys_delta = cpu_stats.get("system_cpu_usage", 0) - precpu_stats.get(
                "system_cpu_usage", 0
            )
            cpus = (
                cpu_stats.get("online_cpus")
                or len(
                    (cpu_stats.get("cpu_usage", {}) or {}).get("percpu_usage", []) or []
                )
                or 1
            )

            if sys_delta > 0 and cpu_delta > 0:
                cpu_percent = (cpu_delta / sys_delta) * cpus * 100.0
                stats["cpu"] = f"{cpu_percent:.2f}%"

        return jsonify({"logs": logs, "stats": stats})
    except (DockerException, RuntimeError, TypeError, ValueError) as e:
        return jsonify({"error": str(e)}), 500


@system_blueprint.route(
    "/api/system/docker/containers/<container_id>/<action>", methods=["POST"]
)
@login_required
@admin_required
def docker_container_action(container_id, action):
    manager, error = _docker_manager_or_error()
    if error:
        return error

    action_map = {
        "start": "started",
        "stop": "stopped",
        "restart": "restarted",
        "remove": "removed",
        "rebuild": "rebuilt",
    }
    if action not in action_map:
        return jsonify({"error": "Invalid action."}), 400

    try:
        c = manager.docker_client.containers.get(container_id)
        identity = manager.identify_container_resource(c)
        container_name = getattr(c, "name", container_id)
        if identity is None:
            return jsonify(
                {
                    "error": (
                        "Carlina refuses to modify a Docker container that is not "
                        "owned by this installation."
                    ),
                    "status": "ownership_conflict",
                }
            ), 409
        if hasattr(current_app, "log_registry"):
            try:
                current_app.log_registry.append_to_source(
                    "docker.addon_services",
                    f"Admin user {current_user.email} requested action '{action}' for Docker container '{container_name}'.",
                )
            except (KeyError, OSError, RuntimeError, ValueError) as log_error:
                _LOGGER.warning("Failed to write Docker action log: %s", log_error)

        message_override = None

        if action == "start":
            c.start()
        elif action == "stop":
            c.stop(timeout=10)
        elif action == "restart":
            c.restart(timeout=10)
        elif action == "remove":
            manager._stop_and_remove_container(str(identity["addon_id"]))
        elif action == "rebuild":
            addon_id = str(identity["addon_id"])

            manifest = manager._get_addon_manifest(addon_id) or {}
            docker_config = manifest.get("docker") or {}
            services = manager._get_docker_services(docker_config)
            if not services:
                raise ValueError(
                    f"Addon '{addon_id}' has no Docker services to rebuild."
                )

            was_running = c.status in {"running", "restarting", "created"}
            manager._append_docker_log(
                addon_id, None, "System Monitor requested Docker rebuild."
            )
            manager._stop_and_remove_container(
                addon_id, remove_image=True, manifest=manifest
            )
            manager._ensure_docker_image_prepared(
                addon_id, manifest=manifest, force_refresh=False
            )

            if was_running and addon_id in manager.loaded_addons:
                try:
                    manager.get_container_url(addon_id)
                    message_override = (
                        "Container image rebuilt and addon service stack restarted."
                    )
                except (
                    DockerException,
                    OSError,
                    RuntimeError,
                    ValueError,
                ) as boot_error:
                    message_override = (
                        "Container image rebuilt, but automatic stack restart failed. "
                        f"Error: {boot_error}"
                    )
            else:
                message_override = (
                    "Container image rebuilt. The stack will start on next addon use."
                )

        if hasattr(current_app, "log_registry"):
            try:
                current_app.log_registry.append_to_source(
                    "docker.addon_services",
                    f"Admin user {current_user.email} completed action '{action}' for Docker container '{container_name}'.",
                )
            except (KeyError, OSError, RuntimeError, ValueError) as log_error:
                _LOGGER.warning("Failed to write Docker action log: %s", log_error)

        return jsonify(
            {
                "success": True,
                "message": message_override
                or f"Container {action_map[action]} successfully.",
            }
        )
    except (DockerException, OSError, RuntimeError, TypeError, ValueError) as e:
        if hasattr(current_app, "log_registry"):
            try:
                current_app.log_registry.append_to_source(
                    "docker.addon_services",
                    f"Docker container action '{action}' failed for '{container_id}': {e}",
                )
            except (KeyError, OSError, RuntimeError, ValueError) as log_error:
                _LOGGER.debug(
                    "Could not append Docker action failure to its log source: %s",
                    log_error,
                )
        return jsonify({"error": str(e)}), 500


@system_blueprint.route("/api/system/docker/images", methods=["GET"])
@login_required
@admin_required
def docker_images():
    manager, error = _docker_manager_or_error()
    if error:
        return error

    try:
        show_all = request.args.get("all", "false").lower() == "true"
        images = manager.docker_client.images.list()
        result = []

        for img in images:
            tags = img.tags if img.tags else ["<none>:<none>"]
            identity = manager.identify_image_resource(img)
            if not show_all and identity is None:
                continue

            size_mb = (img.attrs.get("Size", 0) or 0) / (1024 * 1024)
            result.append(
                {
                    "id": img.short_id,
                    "tags": tags,
                    "size": f"{size_mb:.1f} MB",
                    "created": img.attrs.get("Created", ""),
                    "managed": identity is not None,
                    "addon_id": identity.get("addon_id") if identity else None,
                    "service_name": identity.get("service_name") if identity else None,
                }
            )

        result.sort(key=lambda item: item["tags"][0] if item["tags"] else "")
        return jsonify(result)
    except (DockerException, RuntimeError, TypeError, ValueError) as e:
        return jsonify({"error": str(e)}), 500


@system_blueprint.route("/api/system/docker/images/<image_id>", methods=["DELETE"])
@login_required
@admin_required
def docker_image_delete(image_id):
    manager, error = _docker_manager_or_error()
    if error:
        return error

    try:
        image = manager.docker_client.images.get(image_id)
        identity = manager.identify_image_resource(image)
        if identity is None:
            return jsonify(
                {
                    "error": (
                        "Carlina refuses to delete a Docker image that is not "
                        "owned by this installation."
                    ),
                    "status": "ownership_conflict",
                }
            ), 409
        manager.docker_client.images.remove(image_id, force=True)
        return jsonify({"success": True, "message": "Image deleted successfully."})
    except DockerException as e:
        return jsonify({"error": str(e)}), 500


@system_blueprint.route("/api/log-sources", methods=["GET"])
@login_required
@admin_required
def get_log_sources():
    """Returns all registered log sources and their current status."""
    category = request.args.get("category")
    return jsonify(
        {"sources": current_app.log_registry.list_sources(category=category)}
    )


def _read_registered_log_source(source_id):
    try:
        return jsonify(
            current_app.log_registry.read_chunk(
                source_id, end_byte=request.args.get("end_byte")
            )
        )
    except KeyError:
        return jsonify({"error": f"Log source '{source_id}' is not registered."}), 404
    except ValueError:
        return jsonify({"error": "end_byte must be an integer."}), 400
    except (OSError, RuntimeError) as e:
        _LOGGER.error("Error reading log source '%s': %s", source_id, e)
        return jsonify({"error": str(e)}), 500


@system_blueprint.route("/api/logs", methods=["GET"])
@login_required
@admin_required
def get_logs():
    """Returns chunked content for a registered source, defaulting to the legacy app log."""
    source_id = request.args.get("source") or "app.legacy"
    return _read_registered_log_source(source_id)


@system_blueprint.route("/api/logs/<path:source_id>", methods=["GET"])
@login_required
@admin_required
def get_log_source(source_id):
    """Returns chunked content for a single registered log source."""
    return _read_registered_log_source(source_id)


@system_blueprint.route("/api/logs/<path:source_id>/download", methods=["GET"])
@login_required
@admin_required
def download_log_source(source_id):
    """Downloads one registered log source after applying read-time redaction."""
    try:
        source = current_app.log_registry.get_source(source_id)
        if not source.downloadable:
            return jsonify(
                {"error": f"Log source '{source.id}' cannot be downloaded."}
            ), 403
        text = current_app.log_registry.read_all_text(source.id)
        filename = f"{source.id}.log"
        return Response(
            text,
            mimetype="text/plain; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
    except KeyError:
        return jsonify({"error": f"Log source '{source_id}' is not registered."}), 404
    except FileNotFoundError:
        return jsonify(
            {"error": f"Log source '{source_id}' has not been created yet."}
        ), 404
    except (OSError, RuntimeError) as e:
        _LOGGER.error("Error downloading log source '%s': %s", source_id, e)
        return jsonify({"error": str(e)}), 500


@system_blueprint.route("/api/logs", methods=["DELETE"])
@login_required
@admin_required
def clear_logs():
    """Clears one registered log source, defaulting to the legacy app log."""
    source_id = request.args.get("source") or "app.legacy"
    return _clear_registered_log_source(source_id)


@system_blueprint.route("/api/logs/<path:source_id>", methods=["DELETE"])
@login_required
@admin_required
def clear_log_source(source_id):
    """Clears one registered log source."""
    return _clear_registered_log_source(source_id)


def _clear_registered_log_source(source_id):
    try:
        current_app.log_registry.clear_source(
            source_id, actor=f"user {current_user.email}"
        )
        return jsonify(
            {"success": True, "message": f"Log source '{source_id}' cleared."}
        )
    except KeyError:
        return jsonify({"error": f"Log source '{source_id}' is not registered."}), 404
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403
    except (OSError, RuntimeError) as e:
        _LOGGER.error("Error clearing log source '%s': %s", source_id, e)
        return jsonify({"error": str(e)}), 500


def register_system_blueprint(app):
    app.register_blueprint(system_blueprint)
