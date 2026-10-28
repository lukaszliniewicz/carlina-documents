import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Protocol, cast

from carlina.product_identity import product_environment_value
from carlina.runtime_environment import is_worker_process, mark_worker_environment
from iarf_assistant.addon_dispatch import AddonRouteManager, register_addon_blueprints
from iarf_assistant.core.addons.managed_process_runtime import (
    managed_process_creation_flags,
)
from iarf_assistant.core.addons.manager_selection import create_addon_manager
from iarf_assistant.core.app_paths import (
    load_runtime_environment,
    worker_ownership_xoption,
)
from iarf_assistant.core.runtime_ownership import resolve_runtime_role
from iarf_assistant.core.startup_readiness import get_startup_readiness


class _ContainerReaper(Protocol):
    def reap_idle_containers(self) -> None: ...


def init_scheduler(app):
    from iarf_assistant.scheduler import init_scheduler as initialize

    return initialize(app)


def WorkflowOrchestrator(*args, **kwargs):
    from iarf_assistant.core.workflow_executor import (
        WorkflowOrchestrator as orchestrator,
    )

    return orchestrator(*args, **kwargs)


def run_scheduled_artifact_retention(*args, **kwargs):
    from iarf_assistant.core.artifact_retention import (
        run_scheduled_artifact_retention as run,
    )

    return run(*args, **kwargs)


def run_scheduled_collection_lookup_backfill(*args, **kwargs):
    from iarf_assistant.core.collection_service import (
        run_scheduled_collection_lookup_backfill as run,
    )

    return run(*args, **kwargs)


def memory_pruner_enabled(*args, **kwargs):
    from iarf_assistant.maintenance import memory_pruner_enabled as enabled

    return enabled(*args, **kwargs)


def trigger_memory_pruner_workflow(*args, **kwargs):
    from iarf_assistant.maintenance import trigger_memory_pruner_workflow as trigger

    return trigger(*args, **kwargs)


def register_addon_operation_orchestrator(*args, **kwargs):
    from iarf_assistant.core.addons.operations import (
        register_addon_operation_orchestrator as register,
    )

    return register(*args, **kwargs)


def register_scheduler(
    app, *, owns_services: bool | None = None, assistant_services: bool = True
):
    """Initializes the scheduler in web processes only."""
    should_own = (
        resolve_runtime_role().owns_services
        if owns_services is None
        else bool(owns_services)
    )
    if should_own and not is_worker_process():
        app.scheduler = init_scheduler(app)

        app.scheduler.add_job(
            func=run_scheduled_artifact_retention,
            args=[app],
            trigger="cron",
            hour=3,
            minute=15,
            id="core_runtime_artifact_retention",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        if not assistant_services:
            return

        # Register Workflow Orchestrator
        orchestrator = WorkflowOrchestrator(app)
        app.scheduler.add_job(
            func=orchestrator.poll_and_dispatch,
            trigger='interval',
            seconds=5,
            id='core_workflow_orchestrator',
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        if hasattr(app.scheduler, "add_listener"):
            from apscheduler.events import EVENT_SCHEDULER_SHUTDOWN

            app.scheduler.add_listener(
                orchestrator.close_on_scheduler_event,
                EVENT_SCHEDULER_SHUTDOWN,
            )
        app.workflow_orchestrator = orchestrator

        app.scheduler.add_job(
            func=run_scheduled_collection_lookup_backfill,
            args=[app],
            trigger="cron",
            hour=3,
            minute=30,
            id="core_collection_lookup_backfill",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        if memory_pruner_enabled(app):
            # Register Nightly Memory Pruner
            app.scheduler.add_job(
                func=trigger_memory_pruner_workflow,
                args=[app],
                trigger='cron',
                hour=2, # Run at 2 AM
                minute=0,
                id='system_memory_pruner',
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
        else:
            logging.info("Nightly memory pruner scheduler is disabled (set ENABLE_MEMORY_PRUNER=true to enable).")
    else:
        app.scheduler = None


def initialize_addon_manager(
    app,
    *,
    manage_lifecycle: bool | None = None,
    allow_lifecycle_maintenance: bool | None = None,
    allowed_addon_ids: frozenset[str] | None = None,
    container_services: bool = True,
):
    """Discovers active addons and registers their HTTP routes."""
    logging.info("Initializing Addon Manager...")
    started_at = time.perf_counter()
    metrics: dict[str, object] = {
        "manager_construct_ms": 0.0,
        "discover_and_load_ms": 0.0,
        "log_sources_ms": 0.0,
        "blueprint_registration_ms": 0.0,
        "operation_orchestrator_ms": 0.0,
        "scheduler_registration_ms": 0.0,
    }
    addon_manager = None
    try:
        worker_process = is_worker_process()
        owns_lifecycle = (
            resolve_runtime_role().owns_services
            if manage_lifecycle is None
            else bool(manage_lifecycle)
        )
        allows_maintenance = (
            resolve_runtime_role().allows_lifecycle_maintenance
            if allow_lifecycle_maintenance is None
            else bool(allow_lifecycle_maintenance)
        )
        phase_started_at = time.perf_counter()
        addon_manager = create_addon_manager(
            app,
            manage_lifecycle=owns_lifecycle,
            allow_lifecycle_maintenance=allows_maintenance,
            allowed_addon_ids=allowed_addon_ids,
            container_services=container_services,
        )
        metrics["manager_construct_ms"] = round(
            (time.perf_counter() - phase_started_at) * 1000,
            3,
        )
        phase_started_at = time.perf_counter()
        addon_manager.discover_and_load_addons()
        metrics["discover_and_load_ms"] = round(
            (time.perf_counter() - phase_started_at) * 1000,
            3,
        )
        app.addon_manager = addon_manager
        phase_started_at = time.perf_counter()
        app.log_registry.register_addon_manager_sources(addon_manager)
        metrics["log_sources_ms"] = round(
            (time.perf_counter() - phase_started_at) * 1000,
            3,
        )

        if worker_process:
            logging.info("Worker process initialized Addon Manager without route, migration, or scheduler startup work.")
            return

        phase_started_at = time.perf_counter()
        register_addon_blueprints(
            app,
            cast(AddonRouteManager, addon_manager),
        )
        metrics["blueprint_registration_ms"] = round(
            (time.perf_counter() - phase_started_at) * 1000,
            3,
        )

        if owns_lifecycle:
            phase_started_at = time.perf_counter()
            register_addon_operation_orchestrator(app)
            metrics["operation_orchestrator_ms"] = round(
                (time.perf_counter() - phase_started_at) * 1000,
                3,
            )

        if app.scheduler and container_services:
            phase_started_at = time.perf_counter()
            app.scheduler.add_job(
                func=cast(_ContainerReaper, addon_manager).reap_idle_containers,
                trigger='interval',
                seconds=60,
                id='addon_container_reaper',
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            metrics["scheduler_registration_ms"] = round(
                (time.perf_counter() - phase_started_at) * 1000,
                3,
            )

    except Exception as exc:
        logging.exception("Failed to initialize Addon Manager on startup.")
        app.addon_manager_error = str(exc)
        get_startup_readiness(app).add_error("addon_manager", exc)
    finally:
        if addon_manager is not None:
            metrics["lifecycle"] = addon_manager.get_startup_metrics()
        metrics["total_ms"] = round(
            (time.perf_counter() - started_at) * 1000,
            3,
        )
        get_startup_readiness(app).set_detail("addon_manager", metrics)
        logging.info("Addon Manager startup metrics=%s", metrics)


def activate_scheduler(app, *, register_addon_jobs: bool = False):
    """Release scheduled queries only after synchronous model imports finish."""
    if app.scheduler is None or hasattr(app, "addon_manager_error"):
        return

    from sqlalchemy.orm import configure_mappers

    try:
        with app.app_context():
            configure_mappers()
            if register_addon_jobs:
                app.addon_manager.register_scheduled_jobs()
        app.scheduler.resume()
    except Exception as exc:
        # Keep the scheduler paused if the registry is incomplete. A background
        # query must not become the first observer of partially loaded models.
        get_startup_readiness(app).add_error("scheduler_activation", exc)
        logging.exception("Failed to activate scheduler after model registration.")


def _worker_ownership_root(app) -> Path:
    """Resolve the data-root boundary for worker argv markers.

    Real Flask apps always publish ``carlina_paths``. The resolver fallback
    keeps lightweight synthetic callers from depending on an ``instance_path``
    attribute while still honoring the configured portable-data environment.
    """

    paths = (getattr(app, "extensions", None) or {}).get("carlina_paths")
    if paths is not None:
        return Path(paths.data_root)
    package_root = Path(getattr(app, "root_path", Path(__file__).resolve().parent))
    return load_runtime_environment(
        package_root.parent,
        package_root=package_root,
    ).data_root


def start_core_knowledge_worker(app):
    """Starts the core knowledge worker unless this process is itself a worker."""
    if is_worker_process():
        return

    knowledge_worker_path = Path(app.root_path) / "knowledge" / "worker.py"
    if not knowledge_worker_path.exists():
        return

    core_worker_running = False
    if hasattr(app, "addon_manager"):
        core_worker_running = app.addon_manager.get_worker_state("core_knowledge").get("running", 0) > 0
    if core_worker_running:
        logging.info("Core knowledge worker already running; skipping spawn.")
        return

    app.log_registry.register_worker_source("core_knowledge", name="Core Knowledge Worker")
    knowledge_worker_log = app.log_registry.open_for_append("worker.core_knowledge")
    worker_environment = os.environ.copy()
    mark_worker_environment(worker_environment, addon_id="doc_processor")
    ownership_root = _worker_ownership_root(app)
    knowledge_worker = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-X",
            worker_ownership_xoption(ownership_root),
            "-u",
            str(knowledge_worker_path),
        ],
        stdout=knowledge_worker_log,
        stderr=subprocess.STDOUT,
        cwd=str(knowledge_worker_path.parent),
        env=worker_environment,
        creationflags=managed_process_creation_flags(),
    )
    if hasattr(app, "addon_manager"):
        app.addon_manager.track_worker_process("core_knowledge", knowledge_worker, knowledge_worker_log)
    logging.info("Spawned core_knowledge background worker.")


def start_core_workflow_worker(app):
    """Starts the core workflow execution worker unless this process is itself a worker."""
    if is_worker_process():
        return

    worker_path = Path(app.root_path) / "core" / "worker_process.py"
    if not worker_path.exists():
        return

    core_worker_running = False
    if hasattr(app, "addon_manager"):
        core_worker_running = app.addon_manager.get_worker_state("core_workflow").get("running", 0) > 0
    if core_worker_running:
        logging.info("Core workflow worker already running; skipping spawn.")
        return

    app.log_registry.register_worker_source("core_workflow", name="Core Workflow Worker")
    worker_log = app.log_registry.open_for_append("worker.core_workflow")
    worker_environment = os.environ.copy()
    mark_worker_environment(worker_environment)
    ownership_root = _worker_ownership_root(app)
    worker_proc = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-X",
            worker_ownership_xoption(ownership_root),
            "-u",
            str(worker_path),
        ],
        stdout=worker_log,
        stderr=subprocess.STDOUT,
        cwd=str(worker_path.parent),
        env=worker_environment,
        creationflags=managed_process_creation_flags(),
    )
    if hasattr(app, "addon_manager"):
        app.addon_manager.track_worker_process("core_workflow", worker_proc, worker_log)
    logging.info("Spawned core_workflow background worker.")


def _deferred_startup_delay_seconds() -> float:
    raw_value = str(
        product_environment_value(
            "DEFERRED_STARTUP_DELAY_SECONDS",
            default="0.25",
        )
        or ""
    ).strip()
    try:
        return max(0.0, float(raw_value))
    except ValueError:
        return 0.25


def _run_deferred_phase(app, name, callback) -> None:
    readiness = get_startup_readiness(app)
    try:
        with readiness.phase(name):
            callback()
    except Exception as exc:
        readiness.add_error(name, exc)
        logging.exception("Deferred startup phase '%s' failed.", name)


def run_deferred_runtime_services(app) -> None:
    """Runs nonessential startup work without delaying HTTP readiness."""
    readiness = get_startup_readiness(app)
    manager = getattr(app, "addon_manager", None)

    with app.app_context():
        if manager is not None:
            # Package upgrades are explicit, journaled Add-on API lifecycle
            # transactions. Ordinary startup must never import or mutate every
            # discovered package merely because its manifest version changed.
            _run_deferred_phase(
                app,
                "always_on_services",
                manager.start_always_on_containers,
            )
            if getattr(app, "scheduler", None):
                _run_deferred_phase(
                    app,
                    "addon_jobs",
                    manager.register_scheduled_jobs,
                )

        _run_deferred_phase(
            app,
            "core_knowledge_worker",
            lambda: start_core_knowledge_worker(app),
        )
        _run_deferred_phase(
            app,
            "core_workflow_worker",
            lambda: start_core_workflow_worker(app),
        )
    readiness.mark_ready()
    snapshot = readiness.snapshot()
    logging.info(
        "Deferred runtime startup completed with state=%s timings_ms=%s",
        snapshot["state"],
        snapshot["timings_ms"],
    )


def start_deferred_runtime_services(app):
    """Starts the deferred runtime supervisor at most once per app."""
    readiness = get_startup_readiness(app)
    if (
        is_worker_process()
        or not resolve_runtime_role().owns_services
    ):
        readiness.mark_ready()
        return None

    config_default = app.config.get(
        "CARLINA_START_DEFERRED_SERVICES",
        app.config.get("IARF_START_DEFERRED_SERVICES", "true"),
    )
    configured_value = product_environment_value(
        "START_DEFERRED_SERVICES",
        default=str(config_default),
    )
    enabled = str(configured_value).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    if not enabled:
        readiness.mark_ready()
        return None

    lock = app.extensions.setdefault(
        "iarf_deferred_startup_lock",
        threading.RLock(),
    )
    with lock:
        existing = app.extensions.get("iarf_deferred_startup_thread")
        if existing is not None:
            return existing

        def runner() -> None:
            delay = _deferred_startup_delay_seconds()
            if delay:
                time.sleep(delay)
            run_deferred_runtime_services(app)

        thread = threading.Thread(
            target=runner,
            name="iarf-deferred-startup",
            daemon=True,
        )
        app.extensions["iarf_deferred_startup_thread"] = thread
        thread.start()
        return thread
