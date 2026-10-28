from pathlib import Path

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from flask_login import current_user, login_required

from carlina.product_identity import PRODUCT_IDENTITY
from iarf_assistant.core.frontend_contracts import (
    resolve_frontend_feature_flags_for_user,
    set_frontend_implementation_headers,
)
from iarf_assistant.core.frontend_orchestration import (
    get_runtime_core_frontend as inspect_core_frontend,
)
from iarf_assistant.core.upload_paths import uploads_root
from iarf_assistant.extensions import db
from iarf_assistant.markdown import md_parser
from iarf_assistant.models import Setting
from iarf_assistant.security import admin_required

pages_blueprint = Blueprint('pages', __name__)


def _frontend_shell_assets(required_flag: str | None = None):
    flags = resolve_frontend_feature_flags_for_user(
        user=current_user,
        manager=getattr(current_app, "addon_manager", None),
        composition=current_app.extensions.get("carlina_runtime_composition"),
    )
    if not flags.get("frontend.shell"):
        return None
    if (
        required_flag
        and required_flag.startswith("frontend.addon.")
        and not flags.get(required_flag)
    ):
        return None

    project_root = Path(current_app.root_path).resolve().parent
    status = inspect_core_frontend(project_root)
    if status.get("status") != "built":
        return None
    assets = status.get("assets") or {}
    if not assets.get("entry_url"):
        return None
    return assets


def render_frontend_shell(
    *,
    route_id: str,
    required_flag: str | None = None,
):
    assets = _frontend_shell_assets(required_flag)
    if assets is None:
        # First-party addon document routes historically treated ``None`` as a
        # signal to render their Jinja workspace. Those workspaces have all
        # moved to compiled applications; silently falling back now creates a
        # second, stale UI with different behavior. Keep ``None`` for core
        # callers that explicitly choose their own unavailable response, while
        # addon routes fail closed into the shared retirement notice.
        if required_flag and required_flag.startswith("frontend.addon."):
            return render_frontend_unavailable(route_id=route_id)
        return None
    response = make_response(
        render_template(
            "frontend_shell.html",
            route_id=route_id,
            frontend_entry_url=assets["entry_url"],
            frontend_css_urls=assets.get("css_urls") or [],
            product=PRODUCT_IDENTITY.public_dict(),
        )
    )
    set_frontend_implementation_headers(response, "svelte")
    response.headers["Cache-Control"] = "private, no-cache"
    return response


# Transitional alias for route modules and tests written before the shell
# renderer became part of the addon-facing document-route contract.
_render_frontend_shell = render_frontend_shell


def render_frontend_unavailable(*, route_id: str):
    response = make_response(
        render_template(
            "frontend_unavailable.html",
            route_id=route_id,
            product=PRODUCT_IDENTITY.public_dict(),
        ),
        503,
    )
    set_frontend_implementation_headers(response, "unavailable")
    response.headers["Cache-Control"] = "private, no-cache"
    return response


@pages_blueprint.route('/terms')
def terms():
    """Renders the terms of service and privacy policy page."""
    site_terms_md_setting = db.session.get(Setting, 'site_terms_md')
    terms_html = ""
    if site_terms_md_setting and site_terms_md_setting.value:
        terms_html = md_parser(site_terms_md_setting.value)
    else:
        flash("No terms of service are configured for this site.", 'error')
        return redirect(url_for('index'))

    return render_template('terms.html', terms_content=terms_html, active_page='terms')


@pages_blueprint.route('/r/<code>')
@login_required
def redirect_short_url(code):
    """Redirects a short URL to its original long URL via addons."""
    long_url = current_app.addon_manager.resolve_short_url(code, current_user)
    if long_url:
        return redirect(long_url)
    flash("Short URL not found or you don't have permission to access it.", 'error')
    return redirect(url_for('index'))


@pages_blueprint.route('/uploads/<path:filename>')
@login_required
def uploaded_file(filename):
    """Serve an owned upload without exposing another user's files."""
    uploads_dir = uploads_root().resolve()
    requested = (uploads_dir / filename).resolve()
    try:
        requested.relative_to(uploads_dir)
    except ValueError:
        abort(404)
    if not current_user.is_admin:
        personal_root = (uploads_dir / str(current_user.id)).resolve()
        try:
            requested.relative_to(personal_root)
        except ValueError:
            # Do not disclose whether another user's path exists.
            abort(404)
    if not requested.is_file():
        abort(404)
    return send_from_directory(requested.parent, requested.name)


@pages_blueprint.route('/')
@login_required
def index():
    """Renders the main chat page."""
    shell = _render_frontend_shell(
        route_id="chat",
        required_flag="frontend.chat",
    )
    if shell is not None:
        return shell
    return render_frontend_unavailable(route_id="chat")


@pages_blueprint.route('/automations')
@login_required
def automations():
    """Renders the Svelte automation studio."""
    shell = _render_frontend_shell(route_id="automations")
    if shell is not None:
        return shell
    return render_frontend_unavailable(route_id="automations")


@pages_blueprint.route('/workspaces')
@login_required
def workspaces_page():
    """Renders the Svelte workspace directory."""
    shell = _render_frontend_shell(route_id="workspaces")
    if shell is not None:
        return shell
    return render_frontend_unavailable(route_id="workspaces")


@pages_blueprint.route('/settings')
@pages_blueprint.route('/settings/<path:settings_path>')
@login_required
def settings_page(settings_path=None):
    """Renders the settings page."""
    del settings_path
    shell = _render_frontend_shell(
        route_id="settings",
        required_flag="frontend.settings_profile",
    )
    if shell is not None:
        return shell
    return render_frontend_unavailable(route_id="settings")


@pages_blueprint.route('/approvals')
@login_required
def approvals_page():
    """Renders the semantic approval queue."""
    shell = _render_frontend_shell(
        route_id="runtime-operations",
        required_flag="frontend.run_studio",
    )
    if shell is not None:
        return shell
    return render_frontend_unavailable(route_id="approvals")


@pages_blueprint.route('/workflow-runs')
@login_required
def workflow_runs_page():
    """Redirects the former runtime directory into the focused Svelte views."""
    assets = _frontend_shell_assets("frontend.run_studio")
    if assets is None:
        return render_frontend_unavailable(route_id="run-studio")
    if request.args.get("tab") == "approvals":
        target = url_for("pages.approvals_page")
    elif request.args.get("run_id"):
        target = url_for(
            "pages.run_studio_page",
            run_path=str(request.args["run_id"]),
        )
    else:
        target = url_for("pages.run_studio_page")
    response = redirect(target)
    set_frontend_implementation_headers(response, "svelte")
    response.headers["Cache-Control"] = "private, no-cache"
    return response


@pages_blueprint.route('/runs')
@pages_blueprint.route('/runs/<path:run_path>')
@login_required
def run_studio_page(run_path=None):
    """Renders Run Studio through the Svelte shell."""
    shell = _render_frontend_shell(
        route_id="run-studio",
        required_flag="frontend.run_studio",
    )
    if shell is not None:
        return shell
    return render_frontend_unavailable(route_id="run-studio")


@pages_blueprint.route('/runtime/processes')
@login_required
@admin_required
def runtime_processes_page():
    """Renders semantic runtime administration."""
    shell = _render_frontend_shell(
        route_id="runtime-operations",
        required_flag="frontend.run_studio",
    )
    if shell is not None:
        return shell
    return render_frontend_unavailable(route_id="runtime-operations")


@pages_blueprint.route('/logs')
@login_required
@admin_required
def logs_page():
    """Renders the registered-source logs page."""
    shell = _render_frontend_shell(
        route_id="logs",
        required_flag="frontend.logs",
    )
    if shell is not None:
        return shell
    return render_frontend_unavailable(route_id="logs")


def register_page_blueprint(app):
    app.register_blueprint(pages_blueprint)
    app.add_url_rule('/terms', endpoint='terms', build_only=True)
    app.add_url_rule('/r/<code>', endpoint='redirect_short_url', build_only=True)
    app.add_url_rule('/uploads/<path:filename>', endpoint='uploaded_file', build_only=True)
    app.add_url_rule('/', endpoint='index', build_only=True)
    app.add_url_rule('/automations', endpoint='automations', build_only=True)
    app.add_url_rule('/workspaces', endpoint='workspaces_page', build_only=True)
    app.add_url_rule('/settings', endpoint='settings_page', build_only=True)
    app.add_url_rule('/approvals', endpoint='approvals_page', build_only=True)
    app.add_url_rule('/workflow-runs', endpoint='workflow_runs_page', build_only=True)
    app.add_url_rule('/runs', endpoint='run_studio_page', build_only=True)
    app.add_url_rule('/runtime/processes', endpoint='runtime_processes_page', build_only=True)
    app.add_url_rule('/logs', endpoint='logs_page', build_only=True)
