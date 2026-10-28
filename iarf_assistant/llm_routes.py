import logging
import os
import re
from functools import wraps

import requests
from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required
from sqlalchemy import or_
from sqlalchemy.orm import contains_eager
from sqlalchemy.orm.attributes import flag_modified

from .core.litellm_runtime import litellm
from .core.model_capabilities import (
    invalidate_model_catalog_cache,
    parse_model_token_limit,
    resolve_model_capabilities,
    resolve_model_record,
)
from .core.model_readiness import resolve_model_readiness_records
from .extensions import db
from .llm_provider_utils import (
    OPENAI_COMPATIBLE_PROVIDER_KIND,
    normalize_openai_compatible_base_url,
    provider_is_openai_compatible,
)
from .models import ApiKey, LLMModel, LLMProvider, Setting, User
from .reasoning import (
    get_addon_reasoning_settings,
    model_reasoning_metadata,
    validate_reasoning_slot_updates,
)

llm_blueprint = Blueprint('llm_api', __name__, url_prefix='/api/llm')
logger = logging.getLogger(__name__)
USER_MODEL_TIER_KEYS = {
    "default_fast_model",
    "default_medium_model",
    "default_powerful_model",
}

_TOKEN_LIMIT_OVERRIDE_FIELDS = {
    "max_input_tokens_override": "max_input_tokens",
    "max_output_tokens_override": "max_output_tokens",
}
_TOKEN_LIMIT_CAPABILITY_KEYS = {
    "max_input_tokens",
    "context_window_tokens",
    "max_output_tokens",
}


def _validated_model_token_fields(data):
    """Validate reserved token fields before any model object is mutated."""
    if not isinstance(data, dict):
        return None, "A model settings object is required."

    parsed = {}
    for field_name in ("context_window", "max_output_tokens"):
        if field_name not in data:
            continue
        try:
            parsed[field_name] = parse_model_token_limit(
                data.get(field_name),
                field_name=field_name,
                allow_blank=True,
            )
        except ValueError as exc:
            return None, str(exc)

    capability_overrides = data.get("capability_overrides")
    if capability_overrides is not None and not isinstance(capability_overrides, dict):
        return None, "capability_overrides must be an object or null."
    for key in _TOKEN_LIMIT_CAPABILITY_KEYS:
        if not isinstance(capability_overrides, dict) or key not in capability_overrides:
            continue
        try:
            parsed[f"capability_overrides.{key}"] = parse_model_token_limit(
                capability_overrides.get(key),
                field_name=f"capability_overrides.{key}",
                allow_blank=True,
            )
        except ValueError as exc:
            return None, str(exc)

    for field_name in _TOKEN_LIMIT_OVERRIDE_FIELDS:
        if field_name not in data:
            continue
        try:
            parsed[field_name] = parse_model_token_limit(
                data.get(field_name),
                field_name=field_name,
                allow_blank=True,
            )
        except ValueError as exc:
            return None, str(exc)
    return parsed, None


def _merged_capability_overrides(
    existing,
    data,
    parsed_token_fields,
):
    """Merge explicit token choices without discarding other capabilities."""
    if "capability_overrides" in data and data.get("capability_overrides") is None:
        merged = {}
    else:
        merged = dict(existing) if isinstance(existing, dict) else {}
        supplied = data.get("capability_overrides")
        if isinstance(supplied, dict):
            merged.update(supplied)

            # Normalize and independently clear reserved values supplied via
            # the legacy object shape as well as the explicit request fields.
            for key in _TOKEN_LIMIT_CAPABILITY_KEYS:
                if key not in supplied:
                    continue
                merged.pop(key, None)
                if key == "max_input_tokens":
                    merged.pop("context_window_tokens", None)
                value = parsed_token_fields.get(f"capability_overrides.{key}")
                if value is not None:
                    merged[key] = value

    for field_name, capability_key in _TOKEN_LIMIT_OVERRIDE_FIELDS.items():
        if field_name not in data:
            continue
        merged.pop(capability_key, None)
        if capability_key == "max_input_tokens":
            merged.pop("context_window_tokens", None)
        value = parsed_token_fields.get(field_name)
        if value is not None:
            merged[capability_key] = value
    return merged or None


def _provider_is_visible(provider):
    return provider and (provider.user_id is None or provider.user_id == current_user.id)


def _provider_is_user_defined(provider):
    return provider_is_openai_compatible(provider) and provider.name not in {'lmstudio', 'vllm'}


def _can_manage_provider(provider):
    return _provider_is_user_defined(provider) and (provider.user_id == current_user.id or (provider.user_id is None and current_user.is_admin))


def _provider_to_response(provider):
    data = provider.to_dict()
    data['is_user_defined'] = _provider_is_user_defined(provider)
    data['can_manage'] = _can_manage_provider(provider)
    return data


def _slugify_endpoint_name(name):
    slug = re.sub(r'[^a-z0-9]+', '_', (name or '').strip().lower()).strip('_')
    return slug or 'endpoint'


def _unique_custom_provider_name(display_name, owner_user_id):
    scope_prefix = f"custom_u{owner_user_id}" if owner_user_id else "custom_global"
    base_name = f"{scope_prefix}_{_slugify_endpoint_name(display_name)}"
    candidate = base_name[:100]
    suffix = 2
    while LLMProvider.query.filter_by(name=candidate).first():
        suffix_text = f"_{suffix}"
        candidate = f"{base_name[:100 - len(suffix_text)]}{suffix_text}"
        suffix += 1
    return candidate

def admin_required(f):
    """Decorator to ensure user is an admin."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            return jsonify({"error": "Administrator access required."}), 403
        return f(*args, **kwargs)
    return decorated_function

def get_model_category(model_id):
    model_id_lower = model_id.lower()
    if any(x in model_id_lower for x in ['opus', 'ultra', 'o1', '405b', 'large', 'max']):
        return 'powerful'
    if any(x in model_id_lower for x in ['flash', 'mini', 'haiku', '8b', '3b', 'fast', 'light']):
        return 'fast'
    if any(x in model_id_lower for x in ['pro', 'sonnet', '70b', 'gpt-4o', 'gpt-4']):
        return 'medium'
    return 'other'


@llm_blueprint.route('/models', methods=['GET'])
@login_required
def get_active_models():
    """
    Provides a list of available models for addon dropdowns.
    This includes globally active models and the user's own custom models.
    """
    model_type = request.args.get('model_type', 'chat')
    if model_type == 'all':
        model_type = None
    ready_only = str(request.args.get('ready_only', '')).strip().lower() in {
        '1',
        'true',
        'yes',
        'on',
    }

    # Globally active models (admin-set)
    global_query = (
        LLMModel.query
        .join(LLMProvider, LLMModel.provider_id == LLMProvider.id)
        .options(contains_eager(LLMModel.provider))
        .filter(LLMModel.is_active == True, LLMModel.user_id.is_(None), LLMProvider.user_id.is_(None))
    )
    if model_type:
        global_query = global_query.filter(LLMModel.model_type == model_type)
    global_models = global_query.all()
    
    # User's own manually added models or active user-specific models
    user_models = []
    if current_user.is_authenticated:
        user_query = (
            LLMModel.query
            .join(LLMProvider, LLMModel.provider_id == LLMProvider.id)
            .options(contains_eager(LLMModel.provider))
            .filter(
                LLMModel.user_id == current_user.id,
                or_(
                    LLMProvider.user_id == current_user.id,
                    LLMProvider.user_id.is_(None),
                ),
                or_(LLMModel.is_active == True, LLMModel.manually_added == True)
            )
        )
        if model_type:
            user_query = user_query.filter(LLMModel.model_type == model_type)
        user_models = user_query.all()

    # Combine and remove duplicates, user models can override global ones if they have the same model_id
    model_map = {m.model_id: m for m in global_models}
    for m in user_models:
        model_map[m.model_id] = m
    
    sorted_models = sorted(model_map.values(), key=lambda m: m.display_name)
    
    fast_preset = db.session.get(Setting, 'default_fast_model')
    medium_preset = db.session.get(Setting, 'default_medium_model')
    powerful_preset = db.session.get(Setting, 'default_powerful_model')
    
    fast_id = fast_preset.value if fast_preset else None
    medium_id = medium_preset.value if medium_preset else None
    powerful_id = powerful_preset.value if powerful_preset else None

    readiness_by_id = resolve_model_readiness_records(
        sorted_models,
        user_id=current_user.id,
        required_model_type=model_type,
    )
    payloads = []
    for model in sorted_models:
        readiness = readiness_by_id[model.model_id]
        if ready_only and not readiness.ready:
            continue
        payloads.append(
            {
                'id': model.model_id,
                'name': model.display_name,
                'provider': (
                    (model.provider.display_name or model.provider.name)
                    if model.provider
                    else 'Unknown'
                ),
                'category': get_model_category(model.model_id),
                'is_preset_fast': model.model_id == fast_id,
                'is_preset_medium': model.model_id == medium_id,
                'is_preset_powerful': model.model_id == powerful_id,
                'supports_vision': model.supports_vision,
                'supports_pdf': model.supports_pdf,
                'supports_audio_in': model.supports_audio_in,
                **model_reasoning_metadata(model),
                **readiness.to_dict(),
            }
        )
    return jsonify(payloads)

@llm_blueprint.route('/models/capabilities')
@login_required
def get_model_capabilities():
    model_id = request.args.get('model_id')
    if not model_id:
        return jsonify({"error": "model_id parameter is required"}), 400

    model = resolve_model_record(model_id, user_id=current_user.id)
    profile = resolve_model_capabilities(
        model_id,
        user_id=current_user.id,
        model_record=model,
    )
    reasoning = (
        model_reasoning_metadata(model)
        if model is not None
        else {
            "supports_reasoning": profile.supports_reasoning,
            "supported_reasoning_levels": [],
        }
    )
    return jsonify({
        **profile.to_dict(),
        "supports_audio_in": profile.supports_audio_input,
        **reasoning,
    })

# --- Endpoints for Settings Page ---

@llm_blueprint.route('/providers', methods=['GET'])
@login_required
def get_providers():
    providers = (
        LLMProvider.query
        .filter(or_(LLMProvider.user_id.is_(None), LLMProvider.user_id == current_user.id))
        .order_by(LLMProvider.name)
        .all()
    )
    return jsonify([_provider_to_response(provider) for provider in providers])


@llm_blueprint.route('/openai-compatible-providers', methods=['POST'])
@login_required
def create_openai_compatible_provider():
    data = request.json or {}
    display_name = (data.get('display_name') or data.get('name') or '').strip()
    api_base = (data.get('api_base') or '').strip()
    api_key = (data.get('api_key') or '').strip() or 'not-required'
    api_version = (data.get('api_version') or '').strip() or None
    is_global = bool(data.get('is_global')) and current_user.is_admin

    if not display_name or not api_base:
        return jsonify({"error": "display_name and api_base are required."}), 400

    owner_user_id = None if is_global else current_user.id
    provider = LLMProvider(
        user_id=owner_user_id,
        name=_unique_custom_provider_name(display_name, owner_user_id),
        display_name=display_name,
        provider_kind=OPENAI_COMPATIBLE_PROVIDER_KIND,
        discovery_type=OPENAI_COMPATIBLE_PROVIDER_KIND,
        supports_custom_models=True,
        requires_api_base=True,
    )
    db.session.add(provider)
    db.session.flush()

    key = ApiKey(
        user_id=owner_user_id,
        provider_id=provider.id,
        name=(data.get('key_name') or 'Default').strip() or 'Default',
        api_key=api_key,
        api_base=api_base,
        api_version=api_version,
        is_active=True,
    )
    db.session.add(key)
    db.session.commit()

    return jsonify({"provider": _provider_to_response(provider), "key": key.to_dict()}), 201


@llm_blueprint.route('/providers/<int:provider_id>', methods=['DELETE'])
@login_required
def delete_openai_compatible_provider(provider_id):
    provider = db.session.get(LLMProvider, provider_id)
    if not provider or not _provider_is_visible(provider):
        return jsonify({"error": "Provider not found."}), 404
    if not _can_manage_provider(provider):
        return jsonify({"error": "Only user-defined OpenAI-compatible endpoints can be deleted."}), 403

    ApiKey.query.filter_by(provider_id=provider.id).delete(synchronize_session=False)
    LLMModel.query.filter_by(provider_id=provider.id).delete(synchronize_session=False)
    db.session.delete(provider)
    db.session.commit()
    return jsonify({"success": True})

@llm_blueprint.route('/providers/<int:provider_id>/keys', methods=['GET'])
@login_required
def get_api_keys_for_provider(provider_id):
    provider = db.session.get(LLMProvider, provider_id)
    if not provider or not _provider_is_visible(provider):
        return jsonify({"error": "Provider not found."}), 404

    user_keys = ApiKey.query.filter_by(user_id=current_user.id, provider_id=provider_id).all()
    global_keys = ApiKey.query.filter_by(user_id=None, provider_id=provider_id).all()
    
    all_keys = user_keys + global_keys
    return jsonify([k.to_dict() for k in all_keys])

@llm_blueprint.route('/keys', methods=['POST'])
@login_required
def create_api_key():
    data = request.json
    provider_id = data.get('provider_id')
    name = data.get('name')
    api_key = data.get('api_key')
    api_base = data.get('api_base')
    api_version = data.get('api_version')
    is_global = data.get('is_global', False)

    provider = db.session.get(LLMProvider, provider_id)
    if not provider or not _provider_is_visible(provider):
        return jsonify({"error": "Provider not found."}), 404

    if not all([provider_id, name, api_key]):
        return jsonify({"error": "provider_id, name, and api_key are required."}), 400

    user_id = None
    if provider.user_id is not None:
        if provider.user_id != current_user.id:
            return jsonify({"error": "Forbidden"}), 403
        user_id = current_user.id
    elif not (is_global and current_user.is_admin):
        user_id = current_user.id
    
    existing_key = ApiKey.query.filter_by(user_id=user_id, provider_id=provider_id, name=name).first()
    if existing_key:
        return jsonify({"error": f"A key named '{name}' already exists for this provider."}), 409

    new_key = ApiKey(
        user_id=user_id,
        provider_id=provider_id,
        name=name,
        api_key=api_key,
        api_base=api_base,
        api_version=api_version
    )
    db.session.add(new_key)
    db.session.commit()
    return jsonify(new_key.to_dict()), 201

@llm_blueprint.route('/keys/<int:key_id>', methods=['PUT'])
@login_required
def update_api_key(key_id):
    key = db.session.get(ApiKey, key_id)
    if not key:
        return jsonify({"error": "API key not found."}), 404

    data = request.json
    
    # Check permissions
    if key.user_id != current_user.id and not current_user.is_admin:
        return jsonify({"error": "Forbidden"}), 403

    # Update basic fields
    if 'name' in data and data['name']:
        key.name = data['name'].strip()
    if 'api_key' in data and data['api_key']:
        key.api_key = data['api_key'].strip()
    if 'api_base' in data:
        key.api_base = data['api_base'].strip() if data['api_base'] else None
    if 'api_version' in data:
        key.api_version = data['api_version'].strip() if data['api_version'] else None

    # Handle scope change
    if 'is_global' in data:
        if not current_user.is_admin:
             return jsonify({"error": "Only admins can change key scope."}), 403
        
        is_global = data['is_global']
        new_user_id = None if is_global else current_user.id
        
        if new_user_id != key.user_id:
            # Check for name collision in new scope
            existing = ApiKey.query.filter_by(
                user_id=new_user_id, 
                provider_id=key.provider_id, 
                name=key.name
            ).first()
            if existing:
                return jsonify({"error": f"A key named '{key.name}' already exists in the target scope."}), 409
            
            key.user_id = new_user_id
            key.is_active = False # Deactivate when moving scopes to avoid confusion

    db.session.commit()
    return jsonify(key.to_dict())

@llm_blueprint.route('/keys/<int:key_id>', methods=['DELETE'])
@login_required
def delete_api_key(key_id):
    key_to_delete = db.session.get(ApiKey, key_id)
    if not key_to_delete:
        return jsonify({"error": "API key not found."}), 404
    
    if key_to_delete.user_id != current_user.id and not current_user.is_admin:
        return jsonify({"error": "Forbidden"}), 403

    db.session.delete(key_to_delete)
    db.session.commit()
    return jsonify({"success": True, "message": "API key deleted."})

@llm_blueprint.route('/keys/<int:key_id>/set-active', methods=['POST'])
@login_required
def set_active_api_key(key_id):
    key_to_activate = db.session.get(ApiKey, key_id)
    if not key_to_activate:
        return jsonify({"error": "API key not found."}), 404
    
    is_global_key = key_to_activate.user_id is None
    if is_global_key and not current_user.is_admin:
         return jsonify({"error": "Only admins can set global keys as active."}), 403
    if not is_global_key and key_to_activate.user_id != current_user.id:
        return jsonify({"error": "You can only activate your own keys."}), 403

    user_id_filter = key_to_activate.user_id
    
    # Find all keys for the provider in the same scope (user or global)
    keys_in_scope = ApiKey.query.filter_by(
        user_id=user_id_filter,
        provider_id=key_to_activate.provider_id
    ).all()

    # Set the target key as active and all others as inactive.
    for key in keys_in_scope:
        key.is_active = (key.id == key_id)

    db.session.commit()
    return jsonify({"success": True, "message": "Active key updated."})

@llm_blueprint.route('/managed-models', methods=['GET'])
@login_required
def get_managed_models():
    """
    Gets all models for a provider viewable by the current user.
    Admins see all global and all user-specific models.
    Regular users see all global models and only their own models.
    """
    from sqlalchemy import or_
    provider_id = request.args.get('provider_id', type=int)
    if not provider_id: return jsonify({"error": "provider_id is required"}), 400
    
    provider = db.session.get(LLMProvider, provider_id)
    if not provider or not _provider_is_visible(provider):
        return jsonify({"error": "Provider not found"}), 404

    model_type = request.args.get('model_type')
    if model_type == 'all':
        model_type = None

    # Special handling for local/custom OpenAI-compatible endpoints
    if provider_is_openai_compatible(provider):
        try:
            # An active key can be global or user-specific. User-specific takes precedence for connection.
            active_key = ApiKey.query.filter_by(
                provider_id=provider.id, 
                user_id=current_user.id, 
                is_active=True
            ).first()

            if not active_key:
                active_key = ApiKey.query.filter_by(
                    provider_id=provider.id, 
                    user_id=None, # Global key
                    is_active=True
                ).first()

            if not active_key or not active_key.api_base:
                return jsonify({
                    "error": f"No active API key with an API Base URL is configured for {provider.name}. Please add one in the provider settings."
                }), 400

            base_url = normalize_openai_compatible_base_url(provider.name, active_key.api_base, provider.provider_kind).rstrip('/')
            models_url = f"{base_url}/models"
            
            headers = {}
            if active_key.api_key and active_key.api_key != 'not-required':
                headers['Authorization'] = f"Bearer {active_key.api_key}"

            response = requests.get(models_url, headers=headers, timeout=5)
            response.raise_for_status()
            provider_models_data = response.json()

            # Get existing models from DB to check for active status
            existing_db_models_q = LLMModel.query.filter(LLMModel.provider_id == provider.id)
            if not current_user.is_admin:
                existing_db_models_q = existing_db_models_q.filter(or_(LLMModel.user_id == current_user.id, LLMModel.user_id == None))
            existing_db_models = {m.model_id: m for m in existing_db_models_q.all()}

            # Format the response to match what the frontend expects
            formatted_models = []
            server_model_ids = set()
            
            prefix = provider.name.lower()
            
            for model_info in provider_models_data.get('data', []):
                model_id_str = model_info.get('id')
                if not model_id_str:
                    continue

                full_model_id = f"{prefix}/{model_id_str}"
                server_model_ids.add(full_model_id)
                db_model = existing_db_models.get(full_model_id)

                # Determine model type
                resolved_type = 'chat'
                if db_model:
                    resolved_type = db_model.model_type
                elif 'embed' in model_id_str.lower() or 'embed' in full_model_id.lower():
                    resolved_type = 'embedding'

                # Filter by model type if specified
                if model_type and resolved_type != model_type:
                    continue

                formatted_models.append({
                    'db_id': db_model.id if db_model else None,
                    'id': full_model_id,
                    'name': model_id_str.split('/')[-1], # User-friendly name
                    'context_window': 'N/A',
                    'supports_vision': db_model.supports_vision if db_model else False,
                    'supports_pdf': db_model.supports_pdf if db_model else False,
                    'supports_reasoning': db_model.supports_reasoning if db_model else False,
                    'supports_audio_in': db_model.supports_audio_in if db_model else False,
                    'input_cost': db_model.input_cost_per_million_tokens if db_model else None,
                    'input_cached_cost': db_model.input_cost_per_million_cached_tokens if db_model else None,
                    'output_cost': db_model.output_cost_per_million_tokens if db_model else None,
                    'is_active': db_model.is_active if db_model else False,
                    'manually_added': db_model.manually_added if db_model else False,
                    'user_id': db_model.user_id if db_model else provider.user_id,
                    'model_type': resolved_type,
                    'default_dimensions': db_model.default_dimensions if db_model else None,
                    'supports_mrl': db_model.supports_mrl if db_model else False,
                    'prefix_style': db_model.prefix_style if db_model else 'none',
                    'custom_query_prefix': db_model.custom_query_prefix if db_model else None,
                    'custom_passage_prefix': db_model.custom_passage_prefix if db_model else None
                })
            
            # Add any DB models that weren't returned by the server (maybe server is down or model unloaded)
            for db_model in existing_db_models.values():
                if db_model.model_id not in server_model_ids:
                    if model_type and db_model.model_type != model_type:
                        continue
                    formatted_models.append(db_model.to_dict())

            return jsonify(formatted_models)

        except Exception as e:
            logging.error(f"Could not connect to {provider.name} server: {e}")
            return jsonify({
                    "error": f"Failed to fetch models from {provider.display_name or provider.name}. Ensure the server is running and the API Base URL is correct. Error: {str(e)}"
                }), 500

    # Original logic for all other providers
    query = LLMModel.query.filter_by(provider_id=provider.id)
    if model_type:
        query = query.filter_by(model_type=model_type)
    
    if not current_user.is_admin:
        # Regular user sees global models + their own
        query = query.filter(or_(LLMModel.user_id == None, LLMModel.user_id == current_user.id))
    
    models = query.order_by(LLMModel.display_name).all()
    return jsonify([m.to_dict() for m in models])

@llm_blueprint.route('/sync-models', methods=['POST'])
@login_required
@admin_required
def sync_models_from_litellm():
    provider_id = request.json.get('provider_id')
    provider = db.session.get(LLMProvider, provider_id)
    if not provider or not _provider_is_visible(provider):
        return jsonify({"error": "Provider not found."}), 404

    if provider_is_openai_compatible(provider):
        # These models are ephemeral and fetched on demand. This endpoint
        # for other providers syncs to DB, but for these we just
        # return a success message. The frontend will then refresh the model list.
        return jsonify({"success": True, "message": f"{provider.display_name or provider.name} model list refreshed."})

    if provider.discovery_type != 'automatic':
        return jsonify({"error": "This provider does not support automatic discovery."}), 400

    api_key_env_var = None
    try:
        # This logic for setting env vars is preserved as litellm.get_valid_models relies on it.
        active_key = ApiKey.query.filter_by(provider_id=provider.id, is_active=True).first()

        provider_name_lower = provider.name.lower()
        
        env_key_map = { 'openai': 'OPENAI_API_KEY', 'gemini': 'GEMINI_API_KEY', 'anthropic': 'ANTHROPIC_API_KEY', 'vertex_ai': None, 'xiaomi_mimo': 'XIAOMI_MIMO_API_KEY' }
        api_key_env_var = env_key_map.get(provider_name_lower)
        if api_key_env_var:
            if not active_key:
                return jsonify({"error": f"An active API key is required to sync with {provider.name}."}), 400
            os.environ[api_key_env_var] = active_key.api_key

        # --- THIS IS THE CORRECTED LOGIC ---
        # 1. Discover model IDs for the specific provider. This is a synchronous call.
        model_id_list = litellm.get_valid_models(custom_llm_provider=provider_name_lower)
        # --- END OF CORRECTION ---

        if not isinstance(model_id_list, list):
            raise Exception(f"Failed to get model list from provider. Response: {model_id_list}")

        existing_models = {m.model_id: m for m in LLMModel.query.filter_by(provider_id=provider.id).all()}
        added_count, updated_count = 0, 0

        # 2. For each discovered model ID, get its detailed information.
        for model_id in model_id_list:
            model_id_for_db = model_id
            
            detailed_info = litellm.get_model_info(model_id)

            if not detailed_info:
                logging.warning(f"Could not get detailed info for model '{model_id}', skipping.")
                continue

            # LiteLLM's legacy ``max_tokens`` is ambiguous across providers:
            # it can describe output capacity, not the model's context window.
            # Only explicit input/context fields may populate the snapshot.
            context_window = None
            for input_key in ('max_input_tokens', 'max_context_tokens'):
                if detailed_info.get(input_key) in (None, ''):
                    continue
                try:
                    context_window = parse_model_token_limit(
                        detailed_info.get(input_key),
                        field_name=input_key,
                    )
                except ValueError:
                    logger.warning(
                        "Ignoring invalid %s metadata for %s.",
                        input_key,
                        model_id,
                    )
                if context_window is not None:
                    break
            max_output_tokens = (
                detailed_info.get('max_output_tokens')
                or detailed_info.get('output_token_limit')
                or detailed_info.get('max_tokens')
            )
            try:
                max_output_tokens = parse_model_token_limit(
                    max_output_tokens,
                    field_name='max_output_tokens',
                    allow_blank=True,
                )
            except ValueError:
                logger.warning("Ignoring invalid output limit metadata for %s.", model_id)
                max_output_tokens = None
            supports_reasoning = detailed_info.get('supports_reasoning_effort', False) or detailed_info.get('supports_thinking', False)
            try:
                vision_support = litellm.supports_vision(model_id)
            except Exception as e:
                logging.warning(f"Could not determine vision support for {model_id}: {e}")
                vision_support = False
            
            supports_vision = vision_support
            
            input_cost = detailed_info.get('input_cost_per_million_tokens')
            if detailed_info.get('input_cost_per_token') is not None:
                input_cost = detailed_info.get('input_cost_per_token') * 1_000_000
            
            output_cost = detailed_info.get('output_cost_per_million_tokens')
            if detailed_info.get('output_cost_per_token') is not None:
                output_cost = detailed_info.get('output_cost_per_token') * 1_000_000

            if model_id_for_db in existing_models:
                model = existing_models[model_id_for_db]
                model.display_name = detailed_info.get('display_name', model_id_for_db)
                model.context_window = context_window
                model.max_output_tokens = max_output_tokens
                model.supports_reasoning = supports_reasoning
                # model.supports_vision = supports_vision # Preserve manual overrides
                # Only update costs from LiteLLM if no custom price has been pinned by the admin.
                # This prevents sync from silently overwriting manually-set custom prices.
                if model.input_cost_per_million_tokens is None:
                    model.input_cost_per_million_tokens = input_cost
                if model.output_cost_per_million_tokens is None:
                    model.output_cost_per_million_tokens = output_cost
                updated_count += 1
            else:
                inferred_model_type = 'chat'
                if 'embed' in model_id_for_db.lower():
                    inferred_model_type = 'embedding'

                new_model = LLMModel(
                    provider_id=provider.id,
                    model_id=model_id_for_db,
                    display_name=detailed_info.get('display_name', model_id_for_db),
                    context_window=context_window,
                    max_output_tokens=max_output_tokens,
                    supports_vision=supports_vision,
                    supports_reasoning=supports_reasoning,
                    input_cost_per_million_tokens=input_cost,
                    output_cost_per_million_tokens=output_cost,
                    is_active=False,
                    manually_added=False,
                    model_type=inferred_model_type,
                    default_dimensions=detailed_info.get('dimensions') or None,
                    supports_mrl=('text-embedding-3' in model_id_for_db or 'gemini-embedding-2' in model_id_for_db),
                    prefix_style='gemini_v2' if 'gemini-embedding-2' in model_id_for_db else ('gemini_v1' if 'gemini-embedding-001' in model_id_for_db else 'none')
                )
                db.session.add(new_model)
                added_count += 1
        
        db.session.commit()
        invalidate_model_catalog_cache()
        return jsonify({"success": True, "message": f"Sync complete. Found and processed {len(model_id_list)} models for {provider.name}. Added: {added_count}, Updated: {updated_count}."})
    except Exception as e:
        db.session.rollback()
        logging.exception("Error syncing models from LiteLLM.")
        return jsonify({"error": str(e)}), 500
    finally:
        if api_key_env_var and api_key_env_var in os.environ: del os.environ[api_key_env_var]
        if 'OPENAI_API_BASE' in os.environ: del os.environ['OPENAI_API_BASE']
        if 'OPENAI_API_KEY' in os.environ: del os.environ['OPENAI_API_KEY']

@llm_blueprint.route('/models/<int:model_db_id>', methods=['PUT'])
@login_required
def update_manual_model(model_db_id):
    model = db.session.get(LLMModel, model_db_id)
    if not model:
        return jsonify({"error": "Model not found."}), 404
        
    is_own_model = model.user_id == current_user.id
    if not current_user.is_admin and not is_own_model:
        return jsonify({"error": "You do not have permission to edit this model."}), 403

    data = request.get_json(silent=True) or {}
    parsed_token_fields, token_error = _validated_model_token_fields(data)
    if token_error:
        return jsonify({"error": token_error}), 400
    provider = model.provider

    merged_overrides = _merged_capability_overrides(
        model.capability_overrides,
        data,
        parsed_token_fields,
    )

    if 'model_string' in data and data['model_string']:
        if not model.manually_added:
            return jsonify({"error": "Cannot modify model string of standard preset models."}), 400
        prefix = provider.name.lower()
        new_model_id = f"{prefix}/{data['model_string']}"
        # Check collision if changing the model string
        if new_model_id != model.model_id:
            if LLMModel.query.filter_by(model_id=new_model_id, user_id=model.user_id).first():
                return jsonify({"error": "A model with this ID already exists."}), 409
        model.model_id = new_model_id
        
    if 'display_name' in data and data['display_name']:
        model.display_name = data['display_name']
        
    if 'input_cost' in data:
        model.input_cost_per_million_tokens = data['input_cost'] or None
        
    if 'input_cached_cost' in data:
        model.input_cost_per_million_cached_tokens = data['input_cached_cost'] or None
        
    if 'output_cost' in data:
        model.output_cost_per_million_tokens = data['output_cost'] or None

    if 'context_window' in parsed_token_fields:
        model.context_window = parsed_token_fields['context_window']
    if 'max_output_tokens' in parsed_token_fields:
        model.max_output_tokens = parsed_token_fields['max_output_tokens']
    if (
        'capability_overrides' in data
        or any(field in data for field in _TOKEN_LIMIT_OVERRIDE_FIELDS)
    ):
        model.capability_overrides = merged_overrides
        
    if 'supports_reasoning' in data:
        model.supports_reasoning = data['supports_reasoning']
    if 'supports_vision' in data:
        model.supports_vision = bool(data['supports_vision'])
    if 'supports_pdf' in data:
        model.supports_pdf = bool(data['supports_pdf'])
    if 'supports_audio_in' in data:
        model.supports_audio_in = data['supports_audio_in']
    if 'supported_reasoning_levels' in data:
        model.supported_reasoning_levels = data['supported_reasoning_levels']
    if 'model_type' in data:
        model.model_type = data['model_type']
    if 'default_dimensions' in data:
        val = data['default_dimensions']
        model.default_dimensions = int(val) if val not in (None, "") else None
    if 'supports_mrl' in data:
        model.supports_mrl = bool(data['supports_mrl'])
    if 'prefix_style' in data:
        model.prefix_style = data['prefix_style']
    if 'custom_query_prefix' in data:
        model.custom_query_prefix = data['custom_query_prefix'] or None
    if 'custom_passage_prefix' in data:
        model.custom_passage_prefix = data['custom_passage_prefix'] or None

    db.session.commit()
    invalidate_model_catalog_cache()
    return jsonify(model.to_dict())

@llm_blueprint.route('/models', methods=['POST'])
@login_required
def add_manual_model():
    data = request.get_json(silent=True) or {}
    parsed_token_fields, token_error = _validated_model_token_fields(data)
    if token_error:
        return jsonify({"error": token_error}), 400
    provider = db.session.get(LLMProvider, data.get('provider_id'))
    if not provider or not _provider_is_visible(provider):
        return jsonify({"error": "Provider not found."}), 404
    if not provider.supports_custom_models:
        return jsonify({"error": "This provider does not support manual model addition."}), 400

    # Private endpoint models stay private to the endpoint owner.
    user_id = provider.user_id if provider.user_id is not None else (current_user.id if not current_user.is_admin else None)

    prefix = provider.name.lower()
    model_id = f"{prefix}/{data['model_string']}"
    
    if LLMModel.query.filter_by(model_id=model_id, user_id=user_id).first():
        return jsonify({"error": "A model with this ID already exists for this user/scope."}), 409

    new_model = LLMModel(
        user_id=user_id,
        provider_id=provider.id,
        model_id=model_id,
        display_name=data['display_name'],
        context_window=parsed_token_fields.get('context_window'),
        max_output_tokens=parsed_token_fields.get('max_output_tokens'),
        capability_overrides=_merged_capability_overrides(
            None,
            data,
            parsed_token_fields,
        ),
        supports_vision=data.get('supports_vision', False),
        supports_pdf=data.get('supports_pdf', False),
        supports_reasoning=data.get('supports_reasoning', False),
        supports_audio_in=data.get('supports_audio_in', False),
        supported_reasoning_levels=data.get('supported_reasoning_levels'),
        input_cost_per_million_tokens=data.get('input_cost') or None,
        input_cost_per_million_cached_tokens=data.get('input_cached_cost') or None,
        output_cost_per_million_tokens=data.get('output_cost') or None,
        is_active=False, # User models are implicitly active for them, global models need activation.
        manually_added=True,
        model_type=data.get('model_type', 'chat'),
        default_dimensions=int(data['default_dimensions']) if data.get('default_dimensions') not in (None, "") else None,
        supports_mrl=bool(data.get('supports_mrl', False)),
        prefix_style=data.get('prefix_style', 'none'),
        custom_query_prefix=data.get('custom_query_prefix') or None,
        custom_passage_prefix=data.get('custom_passage_prefix') or None
    )
    db.session.add(new_model)
    db.session.commit()
    invalidate_model_catalog_cache()
    return jsonify(new_model.to_dict()), 201

@llm_blueprint.route('/models/<int:model_db_id>', methods=['DELETE'])
@login_required
def delete_model(model_db_id):
    model = db.session.get(LLMModel, model_db_id)
    if not model:
        return jsonify({"error": "Model not found."}), 404
    
    # Check permissions
    is_own_model = model.user_id == current_user.id
    if not current_user.is_admin and not is_own_model:
        return jsonify({"error": "You do not have permission to delete this model."}), 403

    if not model.manually_added:
        return jsonify({"error": "Only manually added models can be deleted."}), 400
    
    db.session.delete(model)
    db.session.commit()
    invalidate_model_catalog_cache()
    return jsonify({"success": True, "message": "Model deleted."})

@llm_blueprint.route('/models/<int:model_db_id>/toggle-active', methods=['POST'])
@login_required
def toggle_model_active(model_db_id):
    model = db.session.get(LLMModel, model_db_id)
    if not model:
        return jsonify({"error": "Model not found."}), 404
    
    # Permission check:
    if model.user_id is None:
        if not current_user.is_admin:
            return jsonify({"error": "Administrator access required."}), 403
    else:
        if model.user_id != current_user.id and not current_user.is_admin:
            return jsonify({"error": "Forbidden"}), 403
    
    model.is_active = not model.is_active
    db.session.commit()
    invalidate_model_catalog_cache()
    return jsonify(model.to_dict())

@llm_blueprint.route('/models/<int:model_db_id>/toggle-pdf', methods=['POST'])
@login_required
@admin_required
def toggle_model_pdf_support(model_db_id):
    model = db.session.get(LLMModel, model_db_id)
    if not model:
        return jsonify({"error": "Model not found."}), 404
    
    model.supports_pdf = not model.supports_pdf
    db.session.commit()
    invalidate_model_catalog_cache()
    return jsonify(model.to_dict())

@llm_blueprint.route('/models/<int:model_db_id>/toggle-vision', methods=['POST'])
@login_required
@admin_required
def toggle_model_vision_support(model_db_id):
    model = db.session.get(LLMModel, model_db_id)
    if not model:
        return jsonify({"error": "Model not found."}), 404
    
    model.supports_vision = not model.supports_vision
    db.session.commit()
    invalidate_model_catalog_cache()
    return jsonify(model.to_dict())

@llm_blueprint.route('/models/<int:model_db_id>/toggle-reasoning', methods=['POST'])
@login_required
@admin_required
def toggle_model_reasoning_support(model_db_id):
    model = db.session.get(LLMModel, model_db_id)
    if not model:
        return jsonify({"error": "Model not found."}), 404
    
    model.supports_reasoning = not model.supports_reasoning
    db.session.commit()
    invalidate_model_catalog_cache()
    return jsonify(model.to_dict())

@llm_blueprint.route('/models/<int:model_db_id>/toggle-audio', methods=['POST'])
@login_required
@admin_required
def toggle_model_audio_support(model_db_id):
    model = db.session.get(LLMModel, model_db_id)
    if not model:
        return jsonify({"error": "Model not found."}), 404
    model.supports_audio_in = not model.supports_audio_in
    db.session.commit()
    invalidate_model_catalog_cache()
    return jsonify(model.to_dict())

@llm_blueprint.route('/activate-discovered-model', methods=['POST'])
@login_required
def activate_discovered_model():
    data = request.json
    provider_id = data.get('provider_id')
    model_id = data.get('model_id')
    display_name = data.get('display_name')
    is_active = data.get('is_active', False)

    if not all([provider_id, model_id, display_name]):
        return jsonify({"error": "provider_id, model_id, and display_name are required."}), 400

    provider = db.session.get(LLMProvider, provider_id)
    if not provider or not _provider_is_visible(provider):
        return jsonify({"error": "Provider not found."}), 404

    # Permission check:
    if provider.user_id is None:
        if not current_user.is_admin:
            return jsonify({"error": "Administrator access required."}), 403
    else:
        if provider.user_id != current_user.id and not current_user.is_admin:
            return jsonify({"error": "Forbidden"}), 403

    model_owner_id = provider.user_id

    # Find or create the model record.
    model = LLMModel.query.filter_by(model_id=model_id, user_id=model_owner_id).first()

    if not model:
        # It's a new model discovered from the provider
        model = LLMModel(
            provider_id=provider_id,
            model_id=model_id,
            display_name=display_name,
            user_id=model_owner_id,  # Match provider's scope
            manually_added=False, # It was discovered, not manually entered
            is_active=False # Will be set below
        )
        db.session.add(model)
        # Flush to get an ID if we need it, though we don't here.
        db.session.flush()

    # Now handle activation logic
    model.is_active = is_active
    db.session.commit()
    invalidate_model_catalog_cache()

    return jsonify(model.to_dict())

@llm_blueprint.route('/user-settings', methods=['GET'])
@login_required
def get_user_llm_settings():
    """Gets all user-specific LLM settings, such as thinking configs."""
    user_addon_settings = current_user.addon_settings or {}
    user_settings = user_addon_settings.get('llm_settings', {})
    return jsonify(user_settings)

@llm_blueprint.route('/user-settings', methods=['PUT'])
@login_required
def update_user_llm_settings():
    """Updates user-specific LLM settings."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Settings must be a JSON object."}), 400

    for key in USER_MODEL_TIER_KEYS.intersection(data):
        value = data.get(key)
        if value in {None, ""}:
            continue
        if not isinstance(value, str) or len(value.strip()) > 255:
            return jsonify({"error": f"{key} must be a valid model identifier."}), 400
        model = resolve_model_record(value.strip(), user_id=int(current_user.id))
        is_routable = bool(
            model
            and model.model_type == "chat"
            and (
                bool(model.is_active)
                if model.user_id is None
                else bool(model.is_active or model.manually_added)
            )
        )
        if not is_routable:
            return jsonify(
                {
                    "error": (
                        f"{key} must reference an accessible chat model that "
                        "is active or is your own manually added model."
                    )
                }
            ), 400
    
    # Need to fetch user from session to make sure JSON modification is tracked.
    user = db.session.get(User, current_user.id)
    
    # Create a copy to ensure SQLAlchemy detects the modification of the JSON field.
    current_settings = (user.addon_settings or {}).copy()

    if 'llm_settings' not in current_settings:
        current_settings['llm_settings'] = {}
        
    # Merge new settings with existing ones
    for key, value in data.items():
        current_settings['llm_settings'][key] = (
            value.strip()
            if key in USER_MODEL_TIER_KEYS and isinstance(value, str)
            else value
        )
        
    user.addon_settings = current_settings
    flag_modified(user, "addon_settings")
    
    try:
        db.session.commit()
        return jsonify({"success": True, "message": "Settings updated."})
    except Exception as e:
        db.session.rollback()
        logging.exception("Error updating user LLM settings.")
        return jsonify({"error": str(e)}), 500


@llm_blueprint.route('/addon-settings/<string:addon_id>/reasoning', methods=['GET', 'PUT'])
@login_required
def addon_reasoning_settings(addon_id):
    """Read or update user-scoped reasoning settings for one add-on.

    Individual slot updates merge into the add-on's settings so independent
    controls on the same page cannot overwrite each other.
    """
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,99}', addon_id):
        return jsonify({"error": "Invalid addon id."}), 400

    user = db.session.get(User, current_user.id)
    if not user:
        return jsonify({"error": "User not found."}), 404

    if request.method == 'GET':
        return jsonify(get_addon_reasoning_settings(user, addon_id))

    data = request.get_json(silent=True) or {}
    try:
        slot_updates = validate_reasoning_slot_updates(user, data.get('slots'))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    current_settings = dict(user.addon_settings or {})
    addon_settings = dict(current_settings.get(addon_id) or {})
    reasoning_settings = dict(addon_settings.get('reasoning') or {})
    current_slots = dict(reasoning_settings.get('slots') or {})
    current_slots.update(slot_updates)
    reasoning_settings['slots'] = current_slots
    addon_settings['reasoning'] = reasoning_settings
    current_settings[addon_id] = addon_settings
    user.addon_settings = current_settings
    flag_modified(user, 'addon_settings')

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logging.exception("Failed to save add-on reasoning settings.")
        return jsonify({"error": "Unable to save add-on reasoning settings."}), 500

    return jsonify(get_addon_reasoning_settings(user, addon_id))
