"""Core route composition without eager imports of every application surface."""

from importlib import import_module
from importlib.util import find_spec

from carlina.runtime_profiles import RuntimeProfile


def register_application_blueprints(app, *, profile=RuntimeProfile.WORKBENCH):
    """Register the selected application's core HTTP boundary."""
    from iarf_assistant.addon_routes import register_addon_blueprint
    from iarf_assistant.auth import register_auth_blueprint
    from iarf_assistant.credential_routes import register_credential_blueprint
    from iarf_assistant.frontend_routes import register_frontend_blueprint
    from iarf_assistant.llm_routes import llm_blueprint
    from iarf_assistant.pages import (
        register_page_blueprint,
        settings_page,
        terms,
        uploaded_file,
    )
    from iarf_assistant.settings_frontend import (
        register_frontend_settings_blueprint,
    )
    from iarf_assistant.settings_routes import register_settings_blueprint

    if profile is RuntimeProfile.DOCUMENTS:
        from carlina.runtime_restart import register_restart_blueprint
        from iarf_assistant.mcp_access_routes import register_mcp_access_blueprint

        app.register_blueprint(llm_blueprint)
        register_auth_blueprint(app)
        register_frontend_blueprint(app)
        register_frontend_settings_blueprint(app)
        register_addon_blueprint(app)
        register_credential_blueprint(app)
        register_settings_blueprint(app)
        register_restart_blueprint(app)
        register_mcp_access_blueprint(app)
        _register_document_pages(app, settings_page, terms, uploaded_file)
        return

    from iarf_assistant.administration_frontend import (
        register_frontend_administration_blueprint,
    )
    from iarf_assistant.automation_frontend import (
        register_frontend_automation_blueprint,
    )
    from iarf_assistant.chat_frontend import register_frontend_chat_blueprint
    from iarf_assistant.chat_routes import register_chat_blueprint
    from iarf_assistant.context_frontend import register_frontend_context_blueprint
    from iarf_assistant.core.client_api.compass_session_routes import (
        register_compass_client_session_blueprints,
    )
    from iarf_assistant.discovery_routes import register_discovery_blueprint
    from iarf_assistant.knowledge.routes import blueprint as knowledge_blueprint
    from iarf_assistant.knowledge_frontend import (
        register_frontend_knowledge_blueprint,
    )
    from iarf_assistant.profile_routes import register_profile_blueprint
    from iarf_assistant.run_frontend import register_frontend_run_blueprint
    from iarf_assistant.runtime_operations_frontend import (
        register_frontend_runtime_operations_blueprint,
    )
    from iarf_assistant.runtime_routes import register_runtime_blueprint
    from iarf_assistant.system_routes import register_system_blueprint
    from iarf_assistant.upload_routes import register_upload_blueprint

    app.register_blueprint(knowledge_blueprint)
    app.register_blueprint(llm_blueprint)
    register_auth_blueprint(app)
    register_page_blueprint(app)
    register_discovery_blueprint(app)
    register_frontend_blueprint(app)
    register_frontend_chat_blueprint(app)
    register_frontend_context_blueprint(app)
    register_frontend_run_blueprint(app)
    register_frontend_runtime_operations_blueprint(app)
    register_frontend_settings_blueprint(app)
    register_frontend_administration_blueprint(app)
    register_frontend_knowledge_blueprint(app)
    register_frontend_automation_blueprint(app)
    register_runtime_blueprint(app)
    register_addon_blueprint(app)
    register_system_blueprint(app)
    register_credential_blueprint(app)
    register_settings_blueprint(app)
    register_upload_blueprint(app)
    register_profile_blueprint(app)
    register_chat_blueprint(app)
    register_compass_client_session_blueprints(app)
    compass_adapter = "iarf_assistant.core.client_api.compass_routes"
    if find_spec(compass_adapter) is not None:
        import_module(compass_adapter).register_compass_client_blueprint(app)


def _register_document_pages(app, settings_page, terms, uploaded_file):
    """Reuse shared views while registering the document application's pages."""
    from flask_login import login_required

    from iarf_assistant.pages import render_frontend_shell, render_frontend_unavailable

    @login_required
    def document_index():
        shell = render_frontend_shell(route_id="documents")
        if shell is not None:
            return shell
        return render_frontend_unavailable(route_id="documents")

    app.add_url_rule("/", endpoint="index", view_func=document_index)
    app.add_url_rule("/terms", endpoint="terms", view_func=terms)
    app.add_url_rule("/uploads/<path:filename>", endpoint="uploaded_file", view_func=uploaded_file)
    app.add_url_rule("/settings", endpoint="settings_page", view_func=settings_page)
    app.add_url_rule("/settings/<path:settings_path>", endpoint="settings_page", view_func=settings_page)
