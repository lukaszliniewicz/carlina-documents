from flask import Blueprint, current_app, jsonify, redirect, request, url_for
from flask_login import current_user, login_required

from iarf_assistant.extensions import db
from iarf_assistant.models import CredentialDelegation, PluginCredential, User


credential_blueprint = Blueprint('credential_api', __name__)


@credential_blueprint.route('/api/credential-delegations', methods=['GET'])
@login_required
def get_delegations():
    """Gets all delegations created by the current user."""
    delegations = CredentialDelegation.query.filter_by(delegator_id=current_user.id).all()
    return jsonify([delegation.to_dict() for delegation in delegations])


@credential_blueprint.route('/api/credential-delegations', methods=['POST'])
@login_required
def create_delegation():
    """Creates new credential delegations from the current user."""
    data = request.get_json(silent=True) or {}
    delegatee_ids = data.get('delegatee_ids')
    credentials = data.get('credentials')

    if not delegatee_ids and data.get('delegatee_id'):
        delegatee_ids = [data.get('delegatee_id')]

    if not credentials and data.get('provider_name'):
        credentials = [{
            'provider_name': data.get('provider_name'),
            'credential_id': data.get('credential_id'),
        }]

    if not delegatee_ids or not credentials:
        return jsonify({"error": "Missing delegatee_ids or credentials."}), 400

    created_count = 0
    updated_count = 0
    manager = current_app.addon_manager

    for delegatee_id in delegatee_ids:
        if not db.session.get(User, delegatee_id):
            continue

        if int(delegatee_id) == current_user.id:
            continue

        for credential_info in credentials:
            provider_name = credential_info.get('provider_name')
            credential_id = credential_info.get('credential_id')

            if not provider_name:
                continue

            if credential_id:
                credential = db.session.get(PluginCredential, credential_id)
                if not credential or credential.user_id != current_user.id:
                    continue
                if credential.provider_name != provider_name:
                    continue
            else:
                addon = manager.loaded_addons.get(provider_name)
                if not addon or not addon.manifest.get("delegatable_credentials"):
                    continue

            existing = db.session.get(CredentialDelegation, (delegatee_id, provider_name))

            if existing:
                existing.delegator_id = current_user.id
                existing.credential_id = credential_id
                updated_count += 1
            else:
                db.session.add(CredentialDelegation(
                    delegator_id=current_user.id,
                    delegatee_id=delegatee_id,
                    provider_name=provider_name,
                    credential_id=credential_id,
                ))
                created_count += 1

    db.session.commit()
    return jsonify({
        "success": True,
        "message": f"Processed delegations. Created: {created_count}, Updated: {updated_count}.",
    }), 201


@credential_blueprint.route('/api/credential-delegations', methods=['DELETE'])
@login_required
def delete_delegation():
    """Deletes a credential delegation created by the current user."""
    data = request.get_json(silent=True) or {}
    delegatee_id = data.get('delegatee_id')
    provider_name = data.get('provider_name')

    if not delegatee_id or not provider_name:
        return jsonify({"error": "delegatee_id and provider_name are required."}), 400

    delegation = CredentialDelegation.query.filter_by(
        delegator_id=current_user.id,
        delegatee_id=delegatee_id,
        provider_name=provider_name,
    ).first()

    if not delegation:
        return jsonify({"error": "Delegation not found."}), 404

    db.session.delete(delegation)
    db.session.commit()
    return jsonify({"success": True, "message": "Delegation removed."})


@credential_blueprint.route('/api/credentials/<provider_name>', methods=['GET'])
@login_required
def get_credentials_for_provider(provider_name):
    """Gets all available credentials, including owned and delegated records."""
    own_credentials = PluginCredential.query.filter_by(
        user_id=current_user.id,
        provider_name=provider_name,
    ).order_by(PluginCredential.created_at.desc()).all()

    credential_list = []
    for credential in own_credentials:
        credential_dict = credential.to_dict()
        credential_dict['source'] = 'own'
        credential_dict['display_label'] = credential.credential_name
        credential_list.append(credential_dict)

    delegations = CredentialDelegation.query.filter_by(
        delegatee_id=current_user.id,
        provider_name=provider_name,
    ).all()

    for delegation in delegations:
        if delegation.credential:
            credential_dict = delegation.credential.to_dict()
            credential_dict['source'] = 'delegated'
            credential_dict['delegator_email'] = delegation.delegator.email
            credential_dict['display_label'] = f"{delegation.credential.credential_name} (Shared by {delegation.delegator.email})"
            credential_list.append(credential_dict)
        else:
            credential_list.append({
                'id': f"delegated_provider_{delegation.delegator_id}",
                'user_id': delegation.delegator_id,
                'provider_name': provider_name,
                'credential_name': f"Shared {provider_name} Access",
                'display_label': f"Shared {provider_name} Access (from {delegation.delegator.email})",
                'is_active': True,
                'source': 'delegated',
                'created_at': None,
            })

    addon = current_app.addon_manager.loaded_addons.get(provider_name)
    if addon and addon.manifest.get("delegatable_credentials"):
        if not addon.manifest.get("custom_settings_ui", False):
            schema = addon.manifest.get('settings_schema', [])
            can_delegate_provider = False

            user_fields = [field['id'] for field in schema if field.get('scope') == 'user']
            if user_fields:
                user_settings = (current_user.addon_settings or {}).get(provider_name, {})
                if user_settings and any(user_settings.get(field) for field in user_fields):
                    can_delegate_provider = True

            if not can_delegate_provider and addon.is_configured():
                can_delegate_provider = True

            if can_delegate_provider and not any(item.get('source') == 'system' for item in credential_list):
                credential_list.append({
                    'id': None,
                    'user_id': current_user.id,
                    'provider_name': provider_name,
                    'credential_name': f"{addon.manifest.get('name')} Account",
                    'display_label': f"{addon.manifest.get('name')} Account",
                    'is_active': True,
                    'source': 'system',
                    'created_at': None,
                })

    return jsonify(credential_list)


@credential_blueprint.route('/api/credentials/<int:credential_id>/set-active', methods=['POST'])
@login_required
def set_active_credential(credential_id):
    """Sets a credential as active, deactivating others for the same provider."""
    credential = db.session.get(PluginCredential, credential_id)
    if not credential or credential.user_id != current_user.id:
        return jsonify({"error": "Credential not found or permission denied."}), 404

    PluginCredential.query.filter(
        PluginCredential.user_id == current_user.id,
        PluginCredential.provider_name == credential.provider_name,
        PluginCredential.id != credential_id,
    ).update({'is_active': False})

    credential.is_active = True
    db.session.commit()
    return jsonify({"success": True, "message": "Active credential updated."})


@credential_blueprint.route('/api/credentials/<int:credential_id>', methods=['DELETE'])
@login_required
def delete_credential(credential_id):
    """Deletes a credential."""
    credential = db.session.get(PluginCredential, credential_id)
    if not credential or credential.user_id != current_user.id:
        return jsonify({"error": "Credential not found or permission denied."}), 404

    db.session.delete(credential)
    db.session.commit()
    return jsonify({"success": True, "message": "Credential deleted."})


@credential_blueprint.route('/twitter-callback')
@login_required
def twitter_callback_redirect():
    """Redirects legacy /twitter-callback to the namespaced addon URL."""
    return redirect(url_for('twitter.oauth_callback', **request.args), code=301)


def register_credential_blueprint(app):
    app.register_blueprint(credential_blueprint)
    app.add_url_rule('/api/credential-delegations', endpoint='get_delegations', build_only=True)
    app.add_url_rule('/api/credential-delegations', endpoint='create_delegation', build_only=True)
    app.add_url_rule('/api/credential-delegations', endpoint='delete_delegation', build_only=True)
    app.add_url_rule('/api/credentials/<provider_name>', endpoint='get_credentials_for_provider', build_only=True)
    app.add_url_rule('/api/credentials/<int:credential_id>/set-active', endpoint='set_active_credential', build_only=True)
    app.add_url_rule('/api/credentials/<int:credential_id>', endpoint='delete_credential', build_only=True)
    app.add_url_rule('/twitter-callback', endpoint='twitter_callback_redirect', build_only=True)
