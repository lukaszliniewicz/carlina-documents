"""Side-effect-free worker registration and explicit worker startup."""

from __future__ import annotations

import os

from carlina.runtime_environment import mark_worker_environment
from iarf_assistant.core.app_paths import resolve_app_paths
from iarf_assistant.core.task_queue import WorkerNode


def build_worker_node(
    addon_id: str,
    *,
    idle_timeout: int = 60,
    mirror_runtime: bool = True,
    status_ttl_seconds: float | None = None,
) -> WorkerNode:
    """Create an import-safe handler registry for an add-on worker.

    The returned node does not create queue directories or open SQLite handles
    until ``start`` is called.
    """

    paths = resolve_app_paths()
    options = {}
    if mirror_runtime is not True:
        options["mirror_runtime"] = mirror_runtime
    if status_ttl_seconds is not None:
        options["status_ttl_seconds"] = status_ttl_seconds
    return WorkerNode(
        str(paths.instance_dir),
        addon_id,
        idle_timeout=idle_timeout,
        **options,
    )


def run_worker_node(worker: WorkerNode) -> None:
    """Create the Flask application explicitly and run one worker node."""

    mark_worker_environment(os.environ, addon_id=worker.addon_id)

    from iarf_assistant.app import create_app

    application = create_app()
    with application.app_context():
        worker.start()
