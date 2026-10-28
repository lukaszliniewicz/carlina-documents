import os

from flask import Blueprint, current_app, jsonify, request
from flask_login import current_user, login_required

from iarf_assistant.core.settings_cache import (
    get_setting_value,
    get_settings_snapshot,
    invalidate_settings_cache,
)
from iarf_assistant.extensions import db
from iarf_assistant.models import Setting
from iarf_assistant.prompts import (
    CHIEF_ASSISTANT_DEFAULT_PROMPT,
    DIRECT_ASSISTANT_DEFAULT_PROMPT,
    JUNIOR_ASSISTANT_MANDATE_DEFAULT_PROMPT,
)
from iarf_assistant.security import admin_required

settings_blueprint = Blueprint('settings_api', __name__)


def resolve_embedding_profile(*args, **kwargs):
    from iarf_assistant.embedding_profiles import resolve_embedding_profile as resolve

    return resolve(*args, **kwargs)


def LLMService(*args, **kwargs):
    from iarf_assistant.llm_service import LLMService as service

    return service(*args, **kwargs)


@settings_blueprint.route('/api/core-prompts')
@login_required
@admin_required
def get_core_prompts():
    """Returns the default text for core assistant prompts."""
    return jsonify({
        'chief_assistant': CHIEF_ASSISTANT_DEFAULT_PROMPT,
        'junior_assistant_mandate': JUNIOR_ASSISTANT_MANDATE_DEFAULT_PROMPT,
        'direct_assistant_default': DIRECT_ASSISTANT_DEFAULT_PROMPT,
    })


@settings_blueprint.route('/api/settings', methods=['GET'])
@login_required
@admin_required
def get_settings():
    """Returns all application settings as a key-value map."""
    settings = get_settings_snapshot()
    settings['IARF_SECRET_KEY_IS_TEMPORARY'] = current_app.config.get('IARF_SECRET_KEY_IS_TEMPORARY', True)
    settings['IARF_SECRET_KEY_IS_FROM_ENV'] = bool(os.environ.get('FLASK_SECRET_KEY'))
    return jsonify(settings)


@settings_blueprint.route('/api/settings', methods=['PUT'])
@login_required
@admin_required
def update_settings():
    """Updates one or more application settings."""
    data = request.get_json(silent=True) or {}
    for key, value in data.items():
        setting = db.session.get(Setting, key)
        value_string = str(value) if value is not None else ""
        if setting:
            setting.value = value_string
        else:
            db.session.add(Setting(key=key, value=value_string))
    db.session.commit()
    invalidate_settings_cache()
    return jsonify({"success": True, "message": "Settings updated."})


@settings_blueprint.route('/api/settings/rag-preflight', methods=['POST'])
@login_required
@admin_required
def rag_preflight():
    """Validates the selected RAG embedding setup by running live embedding probes."""
    data = request.get_json(silent=True) or {}

    provider = str(data.get("provider") or "").strip().lower()
    model_name = str(data.get("model_name") or "").strip()
    model = str(data.get("model") or "").strip()

    if not model:
        if provider and model_name:
            model = f"{provider}/{model_name}"
        else:
            model = str(get_setting_value("rag_embedding_model", "") or "").strip()

    if not model:
        return jsonify({"error": "Embedding model is required for RAG preflight."}), 400

    dim_raw = data.get("dimensions")
    if dim_raw in (None, ""):
        dim_raw = get_setting_value("rag_embedding_dimensions", "768") or "768"

    try:
        dimensions = int(dim_raw)
    except (TypeError, ValueError):
        return jsonify({"error": "Embedding dimensions must be an integer."}), 400

    if dimensions < 1:
        return jsonify({"error": "Embedding dimensions must be greater than 0."}), 400

    task_type = str(data.get("task_type") or "search").strip().lower() or "search"
    probe_document = str(data.get("probe_document") or "RAG preflight document probe.").strip()
    probe_query = str(data.get("probe_query") or "RAG preflight query probe.").strip()

    profile = resolve_embedding_profile(
        model=model,
        task_type=task_type,
        user_id=int(current_user.id),
    )
    llm_service = LLMService(user_id=current_user.id)

    def _extract_vector(item):
        return item.get("embedding") if isinstance(item, dict) else getattr(item, "embedding")

    try:
        doc_input = profile.prepare_documents([probe_document], titles=["preflight"])
        doc_kwargs = profile.request_kwargs_for_documents(dimensions=dimensions)
        doc_response = llm_service.embedding(
            model=model,
            input=doc_input,
            embedding_profile=profile,
            **doc_kwargs,
        )

        query_input = [profile.prepare_query(probe_query)]
        query_kwargs = profile.request_kwargs_for_query(dimensions=dimensions)
        query_response = llm_service.embedding(
            model=model,
            input=query_input,
            embedding_profile=profile,
            **query_kwargs,
        )

        doc_vector = _extract_vector(doc_response.data[0])
        query_vector = _extract_vector(query_response.data[0])
        doc_dim = len(doc_vector)
        query_dim = len(query_vector)
        dimensions_match = doc_dim == dimensions and query_dim == dimensions

        return jsonify({
            "success": True,
            "model": model,
            "task_type": task_type,
            "requested_dimensions": dimensions,
            "document_dimensions": doc_dim,
            "query_dimensions": query_dim,
            "dimensions_match_requested": dimensions_match,
            "message": "Preflight completed successfully." if dimensions_match else (
                f"Model returned {doc_dim}/{query_dim} dimensions instead of requested {dimensions}."
            ),
        })
    except Exception as e:
        return jsonify({"error": f"RAG preflight failed: {str(e)}"}), 400


def register_settings_blueprint(app):
    app.register_blueprint(settings_blueprint)
    app.add_url_rule('/api/core-prompts', endpoint='get_core_prompts', build_only=True)
    app.add_url_rule('/api/settings', endpoint='get_settings', build_only=True)
    app.add_url_rule('/api/settings', endpoint='update_settings', build_only=True)
    app.add_url_rule('/api/settings/rag-preflight', endpoint='rag_preflight', build_only=True)
