import logging

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required
from sqlalchemy.orm.attributes import flag_modified

from iarf_assistant.chat_builtin_tools import (
    ensure_memory_tables_exist,
    get_default_memory_category_definitions,
    handle_manage_long_term_memory,
    handle_memorize_fact,
    is_missing_memory_tables_error,
    sanitize_memory_category_definitions,
)
from iarf_assistant.extensions import db
from iarf_assistant.models import User


profile_blueprint = Blueprint('profile_api', __name__)


def _ensure_profile_dict(user: User) -> dict:
    if user.addon_settings is None:
        user.addon_settings = {}
    profile_data = user.addon_settings.get('profile')
    if not isinstance(profile_data, dict):
        user.addon_settings['profile'] = {}
    return user.addon_settings['profile']


def _load_memory_category_definitions(user: User) -> list[dict[str, str]]:
    profile_data = _ensure_profile_dict(user)
    memory_settings = profile_data.get('memory')
    if not isinstance(memory_settings, dict):
        return get_default_memory_category_definitions()
    return sanitize_memory_category_definitions(memory_settings.get('category_definitions'))


def _run_memory_action_with_repair(action_fn):
    try:
        return action_fn()
    except Exception as exc:
        if is_missing_memory_tables_error(exc):
            ensure_memory_tables_exist()
            return action_fn()
        raise


@profile_blueprint.route('/api/me', methods=['GET'])
@login_required
def me():
    """Returns the currently logged-in user's data."""
    return jsonify(current_user.to_dict())


@profile_blueprint.route('/api/me', methods=['PUT'])
@login_required
def update_me():
    """Updates the currently logged-in user's profile data."""
    data = request.get_json(silent=True) or {}
    user = db.session.get(User, current_user.id)

    # Store profile fields in addon_settings under 'profile' key
    profile_data = _ensure_profile_dict(user)
    if 'preferences' in data:
        profile_data['preferences'] = data['preferences']
    if 'style_guidelines' in data:
        profile_data['style_guidelines'] = data['style_guidelines']
    if 'writing_samples' in data:
        profile_data['writing_samples'] = data['writing_samples']
    if 'style_reference' in data:
        profile_data['style_reference'] = data['style_reference']
        
    flag_modified(user, "addon_settings")
    db.session.commit()
    return jsonify({"success": True, "message": "Profile updated successfully."})


@profile_blueprint.route('/api/me/memory/preferences', methods=['GET'])
@login_required
def get_my_memory_preferences():
    user = db.session.get(User, current_user.id)
    categories = _load_memory_category_definitions(user)
    return jsonify(
        {
            "category_definitions": categories,
            "default_category": next(
                (entry["key"] for entry in categories if entry.get("key")),
                "other",
            ),
        }
    )


@profile_blueprint.route('/api/me/memory/preferences', methods=['PUT'])
@login_required
def update_my_memory_preferences():
    data = request.get_json(silent=True) or {}
    user = db.session.get(User, current_user.id)
    profile_data = _ensure_profile_dict(user)
    memory_settings = profile_data.get("memory")
    if not isinstance(memory_settings, dict):
        memory_settings = {}
        profile_data["memory"] = memory_settings

    categories = sanitize_memory_category_definitions(data.get("category_definitions"))
    memory_settings["category_definitions"] = categories

    flag_modified(user, "addon_settings")
    db.session.commit()

    return jsonify(
        {
            "success": True,
            "message": "Memory category preferences saved.",
            "category_definitions": categories,
        }
    )


@profile_blueprint.route('/api/me/memory', methods=['GET'])
@login_required
def get_my_persistent_memory():
    query = request.args.get("query")
    category = request.args.get("category")
    tier = request.args.get("tier")
    include_tier2 = request.args.get("include_tier2", "true")
    limit = request.args.get("limit", "80")

    try:
        listing = _run_memory_action_with_repair(
            lambda: handle_manage_long_term_memory(
                action="list",
                scope="user",
                target_uid=current_user.id,
                query=query,
                limit=limit,
                category=category,
                tier=tier,
                include_tier2=include_tier2,
            )
        )
        if listing.get("status") != "ok":
            return jsonify(listing), 400

        summary = _run_memory_action_with_repair(
            lambda: handle_manage_long_term_memory(
                action="summarize_categories",
                scope="user",
                target_uid=current_user.id,
                query=query,
                category=category,
                tier=tier,
                include_tier2=include_tier2,
            )
        )
        category_summary = summary.get("categories", []) if summary.get("status") == "ok" else []

        return jsonify(
            {
                **listing,
                "category_summary": category_summary,
            }
        )
    except Exception as exc:
        logging.exception("Error reading persistent memory preview.")
        return jsonify({"status": "error", "message": f"Could not load memory: {exc}"}), 500


@profile_blueprint.route('/api/me/memory', methods=['POST'])
@login_required
def create_my_persistent_memory():
    data = request.get_json(silent=True) or {}
    fact = data.get("fact")
    category = data.get("category")
    tier = data.get("tier")
    importance = data.get("importance")
    tier_locked = data.get("tier_locked")
    source = data.get("source", "user_profile")

    try:
        result = _run_memory_action_with_repair(
            lambda: handle_memorize_fact(
                scope="user",
                fact=fact,
                target_uid=current_user.id,
                category=category,
                tier=tier,
                importance=importance,
                tier_locked=tier_locked,
                source=source,
            )
        )
    except Exception as exc:
        logging.exception("Error creating persistent memory.")
        return jsonify({"status": "error", "message": f"Could not create memory: {exc}"}), 500

    if isinstance(result, str) and result.startswith("Error:"):
        return jsonify({"status": "error", "message": result}), 400
    if isinstance(result, str) and "Database error" in result:
        return jsonify({"status": "error", "message": result}), 500
    return jsonify({"status": "ok", "message": result})


@profile_blueprint.route('/api/me/memory/<int:memory_id>', methods=['PUT'])
@login_required
def update_my_persistent_memory(memory_id: int):
    data = request.get_json(silent=True) or {}
    fact = data.get("fact")
    category = data.get("category")
    tier = data.get("tier")
    importance = data.get("importance")
    tier_locked = data.get("tier_locked")

    try:
        result = _run_memory_action_with_repair(
            lambda: handle_manage_long_term_memory(
                action="update",
                scope="user",
                target_uid=current_user.id,
                memory_id=memory_id,
                fact=fact,
                category=category,
                tier=tier,
                importance=importance,
                tier_locked=tier_locked,
            )
        )
    except Exception as exc:
        logging.exception("Error updating persistent memory.")
        return jsonify({"status": "error", "message": f"Could not update memory: {exc}"}), 500

    if result.get("status") != "ok":
        return jsonify(result), 400
    return jsonify(result)


@profile_blueprint.route('/api/me/memory/<int:memory_id>', methods=['DELETE'])
@login_required
def delete_my_persistent_memory(memory_id: int):
    try:
        result = _run_memory_action_with_repair(
            lambda: handle_manage_long_term_memory(
                action="delete",
                scope="user",
                target_uid=current_user.id,
                memory_id=memory_id,
            )
        )
    except Exception as exc:
        logging.exception("Error deleting persistent memory.")
        return jsonify({"status": "error", "message": f"Could not delete memory: {exc}"}), 500

    if result.get("status") != "ok":
        return jsonify(result), 400
    return jsonify(result)


def register_profile_blueprint(app):
    app.register_blueprint(profile_blueprint)
    app.add_url_rule('/api/me', endpoint='me', build_only=True)
    app.add_url_rule('/api/me/memory/preferences', endpoint='get_my_memory_preferences', build_only=True)
    app.add_url_rule('/api/me/memory/preferences', endpoint='update_my_memory_preferences', build_only=True)
    app.add_url_rule('/api/me/memory', endpoint='get_my_persistent_memory', build_only=True)
    app.add_url_rule('/api/me/memory', endpoint='create_my_persistent_memory', build_only=True)
    app.add_url_rule('/api/me/memory/<int:memory_id>', endpoint='update_my_persistent_memory', build_only=True)
    app.add_url_rule('/api/me/memory/<int:memory_id>', endpoint='delete_my_persistent_memory', build_only=True)
