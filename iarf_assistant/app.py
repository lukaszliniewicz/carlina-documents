import os
import sys
import uuid
from contextlib import suppress
from pathlib import Path

from flask import Flask, g, request
from flask_login import current_user

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Reconfigure stdout and stderr to default to UTF-8 to avoid encoding crashes on Windows console
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    with suppress(AttributeError, OSError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    with suppress(AttributeError, OSError, ValueError):
        sys.stderr.reconfigure(encoding="utf-8")

from carlina.core.application_distribution import register_distribution_endpoints
from carlina.product_identity import product_environment_value
from carlina.profile_binding import bind_runtime_profile
from carlina.runtime_composition import resolve_runtime_composition
from carlina.runtime_profiles import RuntimeProfile
from iarf_assistant.blueprints import register_application_blueprints
from iarf_assistant.cli import register_cli
from iarf_assistant.core.addons.capability_runtime import (
    configure_capability_error_handling,
)
from iarf_assistant.core.app_paths import load_runtime_environment
from iarf_assistant.core.encryption import configure_encryption, load_session_key
from iarf_assistant.core.health import register_health_endpoints
from iarf_assistant.core.http_delivery import (
    apply_static_asset_cache_headers,
    configure_http_delivery,
)
from iarf_assistant.core.logging_observability import (
    LogSourceRegistry,
    configure_application_logging,
    push_log_context,
    reset_log_context,
)
from iarf_assistant.core.login_security import configure_login_security
from iarf_assistant.core.migration_runtime import configure_migration_cli
from iarf_assistant.core.persistence import (
    configure_query_metrics,
    sqlite_busy_timeout_ms,
)
from iarf_assistant.core.restore_service import load_pending_restore
from iarf_assistant.core.runtime_ownership import (
    lifecycle_maintenance_cli_requested,
    resolve_runtime_role,
)
from iarf_assistant.core.settings_cache import configure_settings_cache_invalidation
from iarf_assistant.core.startup_readiness import get_startup_readiness
from iarf_assistant.core.web_security import (
    configure_web_security,
    normalized_request_id,
)
from iarf_assistant.extensions import db, login_manager
from iarf_assistant.models import User
from iarf_assistant.startup import (
    activate_scheduler,
    initialize_addon_manager,
    register_scheduler,
    start_deferred_runtime_services,
)
from iarf_assistant.template_context import register_template_context_processors

SQLITE_BUSY_TIMEOUT_MS = sqlite_busy_timeout_ms()


def _google_configuration(
    *, load_source_config: bool = True
) -> tuple[str | None, str | None, str | None]:
    """Load optional ignored source configuration after dotenv resolution."""

    if not load_source_config:
        return (
            os.environ.get("GOOGLE_API_KEY"),
            os.environ.get("GOOGLE_CLIENT_ID"),
            os.environ.get("GOOGLE_CLIENT_SECRET"),
        )
    try:
        from iarf_assistant.config import (
            GOOGLE_API_KEY,
            GOOGLE_CLIENT_ID,
            GOOGLE_CLIENT_SECRET,
        )
    except ModuleNotFoundError as exc:
        if exc.name != "iarf_assistant.config":
            raise
        return (
            os.environ.get("GOOGLE_API_KEY"),
            os.environ.get("GOOGLE_CLIENT_ID"),
            os.environ.get("GOOGLE_CLIENT_SECRET"),
        )
    return GOOGLE_API_KEY, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def _create_app(*, profile: RuntimeProfile | str | None = None) -> Flask:
    """Creates and configures the Flask application."""
    package_root = Path(__file__).resolve().parent
    default_program_root = package_root.parent
    configured_program_root = product_environment_value(
        "PROGRAM_ROOT",
        default=str(default_program_root),
    )
    project_root = Path(configured_program_root or default_program_root)
    paths = load_runtime_environment(
        project_root,
        package_root=package_root,
        profile=profile,
    )
    pending_restore = load_pending_restore(paths)
    if pending_restore is not None:
        raise RuntimeError(
            "A verified restore is pending. Start through 'carlina start' or run "
            "'carlina restore --apply-pending' while the service is stopped."
        )
    composition = resolve_runtime_composition(profile)
    profile_binding = bind_runtime_profile(paths.data_root, composition.profile)
    app = Flask(__name__, instance_path=str(paths.instance_dir))
    app.extensions["carlina_paths"] = paths
    app.extensions["carlina_runtime_composition"] = composition
    app.extensions["carlina_profile_binding"] = profile_binding
    app.config.update(paths.as_config())
    app.config["CARLINA_PROFILE"] = composition.profile.value
    app.config["CARLINA_CONTENT_POLICY"] = profile_binding.content_policy.value
    if composition.profile is RuntimeProfile.DOCUMENTS:
        app.config.update(
            SESSION_COOKIE_NAME="carlina_documents_session",
            REMEMBER_COOKIE_NAME="carlina_documents_remember",
        )
    startup_readiness = get_startup_readiness(app)
    runtime_role = resolve_runtime_role()
    app.extensions["carlina_runtime_role"] = runtime_role

    app.log_registry = LogSourceRegistry(
        app.instance_path,
        logs_dir=paths.log_dir,
    )
    app.log_registry.register_default_sources()
    configure_application_logging(app, app.log_registry)

    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + str(paths.database_file)
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SQLITE_BUSY_TIMEOUT_MS"] = SQLITE_BUSY_TIMEOUT_MS
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "connect_args": {
            "timeout": SQLITE_BUSY_TIMEOUT_MS / 1000,
        },
    }
    google_api_key, google_client_id, google_client_secret = _google_configuration(
        load_source_config=composition.assistant_services
    )
    app.config["GOOGLE_API_KEY"] = google_api_key
    app.config["GOOGLE_CLIENT_ID"] = google_client_id
    app.config["GOOGLE_CLIENT_SECRET"] = google_client_secret
    app.config["ADDON_CATALOG_URL"] = product_environment_value(
        "ADDON_CATALOG_URL",
        default="",
    )
    app.config["ADDON_CATALOG_CHANNEL"] = product_environment_value(
        "ADDON_CATALOG_CHANNEL",
        default="stable",
    )
    app.config["ADDON_CATALOG_TRUST_STORE"] = product_environment_value(
        "ADDON_CATALOG_TRUST_STORE",
        default=str(package_root / "core" / "addons" / "catalog_trust.json"),
    )
    # Unsigned catalogs are a focused-test/local-development escape hatch and
    # cannot be enabled through ambient process configuration.
    app.config["ADDON_CATALOG_ALLOW_UNSIGNED_LOCAL"] = False
    configured_workspace_roots = product_environment_value(
        "PROJECT_WORKSPACE_ROOTS",
        default="",
    )
    app.config["PROJECT_WORKSPACE_ROOTS"] = (
        [
            value.strip()
            for value in configured_workspace_roots.split(os.pathsep)
            if value.strip()
        ]
        if configured_workspace_roots
        else [
            {
                "id": "carlina-source",
                "label": "Carlina source",
                "path": str(Path(app.root_path).resolve().parent),
                "access": "read_write",
                "admin_only": True,
            }
        ]
    )
    configured_workspace_executables = product_environment_value(
        "PROJECT_WORKSPACE_EXECUTABLES",
        default="",
    )
    if configured_workspace_executables:
        app.config["PROJECT_WORKSPACE_EXECUTABLES"] = [
            value.strip()
            for value in configured_workspace_executables.split(",")
            if value.strip()
        ]

    configure_encryption(app, paths, initialize=False)
    secret_key = os.environ.get("FLASK_SECRET_KEY") or load_session_key(
        paths.session_key_file
    )
    app.secret_key = secret_key
    app.config["CARLINA_SECRET_KEY_IS_TEMPORARY"] = False
    app.config["IARF_SECRET_KEY_IS_TEMPORARY"] = False

    configure_web_security(app)
    configure_login_security(app, paths)
    db.init_app(app)
    if composition.assistant_services:
        from iarf_assistant.core.client_api.device_auth import (
            configure_compass_client_auth,
        )

        configure_compass_client_auth(app)
    configure_capability_error_handling(app)
    configure_settings_cache_invalidation()
    configure_query_metrics(app)
    configure_http_delivery(app)
    register_health_endpoints(app)
    register_distribution_endpoints(app, composition)

    register_template_context_processors(app)

    if composition.assistant_services:
        from iarf_assistant.rag_service import RAGService

        app.rag_service = RAGService()
        app.rag_service.init_app(app)
    configure_migration_cli(app, db)
    login_manager.init_app(app)
    login_manager.login_view = "login"

    @app.before_request
    def attach_request_log_context():
        request_id = (
            normalized_request_id(request.headers.get("X-Request-ID"))
            or uuid.uuid4().hex[:12]
        )
        g.request_id = request_id
        context = {
            "request_id": request_id,
            "method": request.method,
            "path": request.path,
        }
        if current_user.is_authenticated:
            context["user_id"] = current_user.get_id()
        g.log_context_token = push_log_context(**context)

    @app.after_request
    def add_request_id_header(response):
        if getattr(g, "request_id", None):
            response.headers["X-Request-ID"] = g.request_id
        return apply_static_asset_cache_headers(
            response,
            request.path,
        )

    @app.teardown_request
    def clear_request_log_context(_exc=None):
        token = getattr(g, "log_context_token", None)
        if token is not None:
            try:
                reset_log_context(token)
            except RuntimeError as exc:
                if "has already been used once" not in str(exc):
                    raise
            finally:
                g.log_context_token = None

    with startup_readiness.phase("scheduler"):
        register_scheduler(
            app,
            owns_services=runtime_role.owns_services,
            assistant_services=composition.assistant_services,
        )
    with startup_readiness.phase("addon_manager"):
        initialize_addon_manager(
            app,
            manage_lifecycle=runtime_role.owns_services,
            allow_lifecycle_maintenance=(
                runtime_role.allows_lifecycle_maintenance
                or lifecycle_maintenance_cli_requested()
            ),
            allowed_addon_ids=composition.addon_ids,
            container_services=composition.container_services,
        )
    with startup_readiness.phase("blueprints"):
        register_application_blueprints(app, profile=composition.profile)
    with startup_readiness.phase("cli"):
        register_cli(app, assistant_services=composition.assistant_services)
    with startup_readiness.phase("scheduler_activation"):
        if not composition.assistant_services:
            if app.scheduler is None and not hasattr(app, "addon_manager_error"):
                from sqlalchemy.orm import configure_mappers

                with app.app_context():
                    configure_mappers()
            else:
                activate_scheduler(app, register_addon_jobs=True)
        else:
            activate_scheduler(app)
    with startup_readiness.phase("chat_recovery"):
        if runtime_role.owns_services and composition.assistant_services:
            from iarf_assistant.core.context_fabric import recover_abandoned_chat_turns

            with app.app_context():
                try:
                    recovery = recover_abandoned_chat_turns(commit=True)
                    if recovery["interrupted_turns"]:
                        app.logger.warning(
                            "Recovered %s interrupted chat turn(s) from a previous process.",
                            recovery["interrupted_turns"],
                        )
                except Exception:
                    db.session.rollback()
                    app.logger.warning(
                        "Deferred abandoned chat-turn recovery because storage is not ready.",
                        exc_info=True,
                    )
    if runtime_role.owns_services and composition.assistant_services:
        start_deferred_runtime_services(app)
    else:
        startup_readiness.mark_ready()

    # A requested handoff is completed only after the successor has finished
    # its startup path. A pending lifecycle change remains visible until an
    # administrator explicitly asks for this hand-off.
    if startup_readiness.snapshot()["state"] in {"ready", "degraded"}:
        from iarf_assistant.core.restart_state import confirm_healthy_successor

        confirm_healthy_successor(paths)

    return app


def create_app():
    """Compatibility shim for imports that still use iarf_assistant.app."""
    from iarf_assistant import create_app as package_create_app

    return package_create_app()
