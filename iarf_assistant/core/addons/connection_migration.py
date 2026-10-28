"""Idempotent migration from legacy credentials/settings to Connections."""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from importlib import import_module
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from iarf_assistant.models import (
    Addon,
    AddonConnection,
    AddonDataMigrationEvent,
    AddonDataMigrationRun,
    AddonPreference,
    CapabilityBinding,
    ConnectionGrant,
    ConnectionSecret,
    CredentialDelegation,
    PluginCredential,
    User,
)

from .connections import capability_binding_key

MIGRATION_NAMESPACE = uuid.UUID("c6dc0f00-4bd3-4aac-aa5c-480cc4408f31")
LEGACY_SECRET_NAME = "credentials"
CONNECTION_MIGRATION_ID = "addon-connections-v1"
SENSITIVE_PREFERENCE_KEY = re.compile(
    r"(?:^|_)(?:api_?key|client_?secret|password|refresh_?token|secret|token)(?:$|_)",
    re.IGNORECASE,
)

# Canonical connector declarations for legacy provider names. Pandatask used
# to own duplicate WordPress credentials; those credentials now become normal
# WordPress site Connections so every WordPress toolkit shares one contract.
LEGACY_CONNECTOR_DECLARATIONS: dict[str, tuple[str, str]] = {
    "bluesky": ("bluesky", "bluesky.account"),
    "facebook": ("facebook", "facebook.account"),
    "gmail": ("gmail", "gmail.account"),
    "google_auth": ("google_auth", "google_auth.account"),
    "imap": ("imap", "imap.account"),
    "ing_ksiegowosc": ("ing_ksiegowosc", "ing_ksiegowosc.account"),
    "pandatask": ("wordpress", "wordpress.site"),
    "stripe": ("stripe", "stripe.account"),
    "twitter": ("twitter", "twitter.account"),
    "wordpress": ("wordpress", "wordpress.site"),
    "zoom": ("zoom", "zoom.account"),
}

# Consumer bindings that preserve the old implicit "active credential" behavior
# after credentials become explicit, user-scoped Connections.
LEGACY_PROVIDER_BINDINGS: dict[
    str,
    tuple[tuple[str, str, str], ...],
] = {
    "google_auth": (
        ("gmail", "google_oauth", "google_oauth"),
        ("googlecalendar", "google_oauth", "google_oauth"),
        ("googlecontacts", "google_oauth", "google_oauth"),
        ("googledrive", "google_oauth", "google_oauth"),
    ),
    "wordpress": (
        ("accounting", "wordpress_site", "wordpress"),
        ("buddypress", "wordpress_site", "wordpress"),
        ("community_tour", "wordpress_site", "wordpress"),
        ("facebook", "wordpress_media", "wordpress"),
        ("freelibrary", "wordpress_site", "wordpress"),
        ("iarfcalendar", "wordpress_site", "wordpress"),
        ("iarfpoll", "wordpress_site", "wordpress"),
        ("iarfrm", "wordpress_site", "wordpress"),
        ("news_pipeline", "publisher", "wordpress"),
        ("pandatask", "wordpress_site", "wordpress"),
        ("volunteer", "wordpress_site", "wordpress"),
    ),
}


@dataclass(slots=True)
class ConnectionMigrationReport:
    dry_run: bool
    connections_created: int = 0
    secrets_created: int = 0
    grants_created: int = 0
    preferences_created: int = 0
    email_accounts_linked: int = 0
    bindings_created: int = 0
    unchanged: int = 0
    warnings: list[str] = field(default_factory=list)
    migration_run_id: str | None = None
    resumed_from_run_id: str | None = None
    completed_checkpoint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _stable_id(kind: str, source_id: object) -> str:
    return str(uuid.uuid5(MIGRATION_NAMESPACE, f"{kind}:{source_id}"))


def _declaration_id(value: object, *, fallback: str) -> str:
    candidate = re.sub(r"[^a-z0-9_.:-]+", "-", str(value or "").strip().lower())
    candidate = candidate.strip("-._:")
    if not candidate or not candidate[0].isalpha():
        candidate = fallback
    return candidate[:100]


def _secret_payload(value: Any, report: ConnectionMigrationReport) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    report.warnings.append(
        "A legacy credential payload was not an object and was wrapped under 'value'."
    )
    return {"value": value}


def _normalized_legacy_secret_payload(
    provider_name: str,
    value: Any,
    report: ConnectionMigrationReport,
) -> dict[str, Any]:
    payload = _secret_payload(value, report)
    if provider_name == "pandatask":
        payload = dict(payload)
        if "base_url" not in payload and "url" in payload:
            payload["base_url"] = payload["url"]
        if "app_password" not in payload and "password" in payload:
            payload["app_password"] = payload["password"]
    return payload


def _preference_payload(
    value: Any,
    *,
    addon_id: str,
    report: ConnectionMigrationReport,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"value": value}
    preferences = {}
    removed = []
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        if SENSITIVE_PREFERENCE_KEY.search(key):
            removed.append(key)
            continue
        preferences[key] = raw_value
    if removed:
        report.warnings.append(
            f"Excluded {len(removed)} secret-like field(s) from normalized "
            f"preferences for add-on {addon_id!r}; legacy settings remain unchanged."
        )
    return preferences


def _credential_connection_id(credential_id: int) -> str:
    return _stable_id("plugin-credential", credential_id)


def _journal_available(session: Session) -> bool:
    inspector = inspect(session.get_bind())
    return inspector.has_table("addon_data_migration_run") and inspector.has_table(
        "addon_data_migration_event"
    )


def _start_journal(
    session: Session,
    report: ConnectionMigrationReport,
) -> AddonDataMigrationRun:
    previous = (
        session.query(AddonDataMigrationRun)
        .filter(
            AddonDataMigrationRun.migration_id == CONNECTION_MIGRATION_ID,
            AddonDataMigrationRun.status.in_(("running", "failed", "interrupted")),
        )
        .order_by(AddonDataMigrationRun.started_at.desc(), AddonDataMigrationRun.id)
        .first()
    )
    if previous is not None and previous.status == "running":
        previous.status = "interrupted"
        previous.completed_at = datetime.now(UTC)
        previous.error_type = "InterruptedProcess"
        previous.events.append(
            AddonDataMigrationEvent(
                sequence=len(previous.events) + 1,
                stage=previous.checkpoint or "start",
                status="interrupted",
                detail_json={},
            )
        )
        session.commit()
    resumed_from = previous.id if previous is not None else None
    run = AddonDataMigrationRun(
        migration_id=CONNECTION_MIGRATION_ID,
        status="running",
        checkpoint="start",
        resumed_from_run_id=resumed_from,
        report_json=report.to_dict(),
    )
    session.add(run)
    session.flush()
    report.migration_run_id = run.id
    report.resumed_from_run_id = resumed_from
    run.report_json = report.to_dict()
    run.events.append(
        AddonDataMigrationEvent(
            sequence=1,
            stage="start",
            status="started",
            detail_json={"resumed_from_run_id": resumed_from},
        )
    )
    session.commit()
    return run


def _checkpoint_journal(
    session: Session,
    run: AddonDataMigrationRun,
    report: ConnectionMigrationReport,
    *,
    stage: str,
) -> None:
    report.completed_checkpoint = stage
    run.checkpoint = stage
    run.report_json = report.to_dict()
    run.events.append(
        AddonDataMigrationEvent(
            sequence=len(run.events) + 1,
            stage=stage,
            status="completed",
            detail_json=report.to_dict(),
        )
    )
    session.commit()


def _complete_journal(
    session: Session,
    run: AddonDataMigrationRun,
    report: ConnectionMigrationReport,
) -> None:
    report.completed_checkpoint = "complete"
    run.status = "completed"
    run.checkpoint = "complete"
    run.report_json = report.to_dict()
    run.completed_at = datetime.now(UTC)
    run.events.append(
        AddonDataMigrationEvent(
            sequence=len(run.events) + 1,
            stage="complete",
            status="completed",
            detail_json=report.to_dict(),
        )
    )
    session.commit()


def _fail_journal(
    session: Session,
    run_id: str,
    report: ConnectionMigrationReport,
    exc: Exception,
) -> None:
    session.rollback()
    run = session.get(AddonDataMigrationRun, run_id)
    if run is None:
        return
    run.status = "failed"
    run.report_json = report.to_dict()
    run.error_type = exc.__class__.__name__
    run.completed_at = datetime.now(UTC)
    run.events.append(
        AddonDataMigrationEvent(
            sequence=len(run.events) + 1,
            stage=run.checkpoint or "unknown",
            status="failed",
            detail_json={"error_type": exc.__class__.__name__},
        )
    )
    session.commit()


def _migrate_credentials(
    session: Session,
    report: ConnectionMigrationReport,
) -> dict[int, str]:
    mapping: dict[int, str] = {}
    credentials = session.query(PluginCredential).order_by(PluginCredential.id).all()
    for credential in credentials:
        legacy_provider_id = _declaration_id(
            credential.provider_name,
            fallback="legacy-connector",
        )
        connector_addon_id, connector_definition_id = (
            LEGACY_CONNECTOR_DECLARATIONS.get(
                legacy_provider_id,
                (legacy_provider_id, f"{legacy_provider_id}.account"),
            )
        )
        connection_id = _credential_connection_id(credential.id)
        mapping[credential.id] = connection_id
        connection = session.get(AddonConnection, connection_id)
        if connection is None:
            connection = (
                session.query(AddonConnection)
                .filter(AddonConnection.legacy_credential_id == credential.id)
                .one_or_none()
            )
        if connection is None:
            report.connections_created += 1
            if not report.dry_run:
                connection = AddonConnection(
                    id=connection_id,
                    owner_user_id=credential.user_id,
                    connector_addon_id=connector_addon_id,
                    connector_definition_id=connector_definition_id,
                    label=credential.credential_name or credential.provider_name,
                    state="ready",
                    is_default=bool(credential.is_active),
                    config_json={},
                    metadata_json={
                        "migration": {
                            "source": "plugin_credential",
                            "source_id": credential.id,
                        }
                    },
                    legacy_credential_id=credential.id,
                    legacy_provider_name=credential.provider_name,
                    created_at=credential.created_at,
                )
                session.add(connection)
        else:
            mapping[credential.id] = connection.id
            report.unchanged += 1
            migration = (
                connection.metadata_json.get("migration", {})
                if isinstance(connection.metadata_json, Mapping)
                else {}
            )
            if (
                not report.dry_run
                and connection.legacy_credential_id == credential.id
                and migration.get("source") == "plugin_credential"
            ):
                connection.connector_addon_id = connector_addon_id
                connection.connector_definition_id = connector_definition_id

        existing_secret = session.get(
            ConnectionSecret,
            _stable_id("plugin-credential-secret", credential.id),
        )
        if existing_secret is None and not report.dry_run:
            existing_secret = (
                session.query(ConnectionSecret)
                .filter(
                    ConnectionSecret.connection_id == mapping[credential.id],
                    ConnectionSecret.name == LEGACY_SECRET_NAME,
                )
                .one_or_none()
            )
        if existing_secret is None:
            report.secrets_created += 1
            if not report.dry_run:
                session.add(
                    ConnectionSecret(
                        id=_stable_id("plugin-credential-secret", credential.id),
                        connection_id=mapping[credential.id],
                        name=LEGACY_SECRET_NAME,
                        secret_data=_normalized_legacy_secret_payload(
                            legacy_provider_id,
                            credential.credential_data,
                            report,
                        ),
                    )
                )
        else:
            report.unchanged += 1
    return mapping


def _migrate_delegations(
    session: Session,
    report: ConnectionMigrationReport,
    credential_connections: Mapping[int, str],
    *,
    provider_connections: Mapping[int, set[str]],
) -> None:
    delegations = (
        session.query(CredentialDelegation)
        .order_by(
            CredentialDelegation.delegatee_id,
            CredentialDelegation.provider_name,
        )
        .all()
    )
    for delegation in delegations:
        connection_ids: set[str] = set()
        if delegation.credential_id is not None:
            base_connection_id = credential_connections.get(delegation.credential_id)
            if base_connection_id:
                connection_ids.add(base_connection_id)
            connection_ids.update(
                provider_connections.get(delegation.credential_id, set())
            )
        if not connection_ids:
            connection = (
                session.query(AddonConnection)
                .filter(
                    AddonConnection.owner_user_id == delegation.delegator_id,
                    AddonConnection.legacy_provider_name == delegation.provider_name,
                )
                .order_by(AddonConnection.is_default.desc(), AddonConnection.created_at)
                .first()
            )
            if connection is not None:
                connection_ids.add(connection.id)
        if not connection_ids:
            report.warnings.append(
                "Could not map credential delegation "
                f"{delegation.delegatee_id}:{delegation.provider_name!r} "
                "to a Connection."
            )
            continue
        for connection_id in sorted(connection_ids):
            existing = (
                session.query(ConnectionGrant)
                .filter(
                    ConnectionGrant.connection_id == connection_id,
                    ConnectionGrant.grantee_user_id == delegation.delegatee_id,
                )
                .one_or_none()
            )
            if existing is not None:
                report.unchanged += 1
                continue
            report.grants_created += 1
            if not report.dry_run:
                session.add(
                    ConnectionGrant(
                        id=_stable_id(
                            "credential-delegation",
                            f"{delegation.delegatee_id}:{connection_id}",
                        ),
                        connection_id=connection_id,
                        grantee_user_id=delegation.delegatee_id,
                        granted_by_user_id=delegation.delegator_id,
                        operations_json=None,
                    )
                )


def _migrate_preferences(
    session: Session,
    report: ConnectionMigrationReport,
) -> None:
    for user in session.query(User).order_by(User.id):
        settings = user.addon_settings
        if not isinstance(settings, Mapping):
            continue
        for raw_addon_id, raw_preferences in sorted(
            settings.items(),
            key=lambda item: str(item[0]),
        ):
            addon_id = str(raw_addon_id).strip()
            if not addon_id:
                continue
            existing = session.get(AddonPreference, (user.id, addon_id))
            if existing is not None:
                report.unchanged += 1
                continue
            report.preferences_created += 1
            if report.dry_run:
                continue
            preferences = _preference_payload(
                raw_preferences,
                addon_id=addon_id,
                report=report,
            )
            session.add(
                AddonPreference(
                    user_id=user.id,
                    addon_id=addon_id,
                    preferences_json=preferences,
                    migrated_from_user_settings=True,
                )
            )


def _migrate_email_accounts(
    session: Session,
    report: ConnectionMigrationReport,
    credential_connections: Mapping[int, str],
) -> dict[int, set[str]]:
    provider_connections: dict[int, set[str]] = {}
    if not inspect(session.get_bind()).has_table("email_account"):
        return provider_connections
    EmailAccount = import_module("iarf_assistant.addons.email.models").EmailAccount

    columns = {
        column["name"]
        for column in inspect(session.get_bind()).get_columns("email_account")
    }
    if "connection_id" not in columns:
        report.warnings.append(
            "email_account.connection_id is not installed; run the Email "
            "add-on migration before data cutover."
        )
        return provider_connections

    for account in session.query(EmailAccount).order_by(EmailAccount.id):
        provider_id = _declaration_id(
            account.provider_id,
            fallback="email-connector",
        )
        needs_link = not bool(account.connection_id)
        existing_connection = (
            session.get(AddonConnection, account.connection_id)
            if account.connection_id
            else None
        )
        existing_config = (
            existing_connection.config_json
            if existing_connection is not None
            and isinstance(existing_connection.config_json, Mapping)
            else {}
        )
        auth_connection_id = str(
            existing_config.get("auth_connection_id") or ""
        ).strip() or (
            credential_connections.get(account.credential_id)
            if account.credential_id is not None
            else None
        )
        auth_connection = (
            session.get(AddonConnection, auth_connection_id)
            if auth_connection_id
            else None
        )
        if existing_connection is not None:
            connection_id = existing_connection.id
            report.unchanged += 1
        elif (
            auth_connection is not None
            and auth_connection.connector_addon_id == provider_id
        ):
            connection_id = auth_connection.id
        else:
            connection_id = _stable_id("email-account", account.id)
        if account.credential_id is not None:
            provider_connections.setdefault(account.credential_id, set()).add(
                connection_id
            )

        connection = existing_connection or session.get(AddonConnection, connection_id)
        if connection is None:
            report.connections_created += 1
            user = session.get(User, account.user_id)
            user_settings = user.addon_settings if user is not None else {}
            user_settings = user_settings or {}
            provider_settings = (
                user_settings.get(provider_id, {})
                if isinstance(user_settings, Mapping)
                else {}
            )
            provider_settings = (
                provider_settings
                if isinstance(provider_settings, Mapping)
                else {}
            )
            config = {
                key: value
                for key, value in provider_settings.items()
                if not SENSITIVE_PREFERENCE_KEY.search(str(key))
            }
            if auth_connection_id:
                config["auth_connection_id"] = auth_connection_id
            credential = (
                session.get(PluginCredential, account.credential_id)
                if account.credential_id is not None
                else None
            )
            secret_payload = (
                _secret_payload(credential.credential_data, report)
                if credential is not None
                else {
                    str(key): value
                    for key, value in provider_settings.items()
                    if SENSITIVE_PREFERENCE_KEY.search(str(key))
                }
            )
            if secret_payload:
                report.secrets_created += 1
            if not report.dry_run:
                connection = AddonConnection(
                    id=connection_id,
                    owner_user_id=account.user_id,
                    connector_addon_id=provider_id,
                    connector_definition_id=f"{provider_id}.account",
                    label=account.account_email,
                    state="ready" if secret_payload else "legacy_unverified",
                    is_default=bool(account.is_active),
                    config_json=config,
                    metadata_json={
                        "migration": {
                            "source": "email_account",
                            "source_id": account.id,
                            "auth_connection_id": auth_connection_id,
                        }
                    },
                    legacy_provider_name=account.provider_id,
                )
                session.add(connection)
                if secret_payload:
                    session.add(
                        ConnectionSecret(
                            id=_stable_id("email-account-secret", account.id),
                            connection_id=connection_id,
                            name=LEGACY_SECRET_NAME,
                            secret_data=secret_payload,
                        )
                    )
        if needs_link:
            report.email_accounts_linked += 1
        provider_capability_id = {
            "gmail": "gmail_account",
            "imap": "imap_account",
        }.get(provider_id, f"{provider_id}_account")
        binding_key = capability_binding_key(
            consumer_addon_id="email",
            requirement_id="mailboxes",
            provider_addon_id=provider_id,
            provider_capability_id=provider_capability_id,
            connection_id=connection_id,
            scope="user",
            scope_id=str(account.user_id),
        )
        binding = (
            session.query(CapabilityBinding.id)
            .filter(CapabilityBinding.binding_key == binding_key)
            .first()
        )
        if binding is None:
            report.bindings_created += 1
            if not report.dry_run:
                session.add(
                    CapabilityBinding(
                        binding_key=binding_key,
                        consumer_addon_id="email",
                        requirement_id="mailboxes",
                        provider_addon_id=provider_id,
                        provider_capability_id=provider_capability_id,
                        connection_id=connection_id,
                        scope="user",
                        scope_id=str(account.user_id),
                        priority=int(account.id),
                        status="enabled",
                        contract_version="1.6.1",
                        created_by_user_id=account.user_id,
                        metadata_json={"migration": "legacy_email_account"},
                    )
                )
        if provider_id == "gmail" and auth_connection_id:
            auth_binding_key = capability_binding_key(
                consumer_addon_id="gmail",
                requirement_id="google_oauth",
                provider_addon_id="google_auth",
                provider_capability_id="google_oauth",
                connection_id=auth_connection_id,
                scope="user",
                scope_id=str(account.user_id),
            )
            auth_binding = (
                session.query(CapabilityBinding.id)
                .filter(CapabilityBinding.binding_key == auth_binding_key)
                .first()
            )
            if auth_binding is None:
                report.bindings_created += 1
                if not report.dry_run:
                    session.add(
                        CapabilityBinding(
                            binding_key=auth_binding_key,
                            consumer_addon_id="gmail",
                            requirement_id="google_oauth",
                            provider_addon_id="google_auth",
                            provider_capability_id="google_oauth",
                            connection_id=auth_connection_id,
                            scope="user",
                            scope_id=str(account.user_id),
                            priority=int(account.id),
                            status="enabled",
                            contract_version="2.0.0",
                            created_by_user_id=account.user_id,
                            metadata_json={"migration": "legacy_email_account"},
                        )
                    )
        if not report.dry_run and needs_link:
            account.connection_id = connection_id
    return provider_connections


def _migrate_implicit_capability_bindings(
    session: Session,
    report: ConnectionMigrationReport,
) -> None:
    """Preserve the old per-user active-credential provider selection."""

    installed = {
        addon.id: addon
        for addon in session.query(Addon)
        .filter(Addon.id.in_(tuple(
            consumer_id
            for bindings in LEGACY_PROVIDER_BINDINGS.values()
            for consumer_id, _requirement_id, _provider_id in bindings
        )))
        .all()
    }
    if not installed:
        return

    connections = (
        session.query(AddonConnection)
        .filter(
            AddonConnection.connector_addon_id.in_(
                tuple(LEGACY_PROVIDER_BINDINGS)
            ),
            AddonConnection.state == "ready",
            AddonConnection.is_default.is_(True),
        )
        .order_by(AddonConnection.created_at, AddonConnection.id)
        .all()
    )
    for connection in connections:
        users = [(int(connection.owner_user_id), 0)]
        users.extend(
            (int(grant.grantee_user_id), -100)
            for grant in connection.grants
            if grant.revoked_at is None
        )
        for consumer_id, requirement_id, provider_capability_id in (
            LEGACY_PROVIDER_BINDINGS[connection.connector_addon_id]
        ):
            addon = installed.get(consumer_id)
            if addon is None:
                continue
            for user_id, priority in users:
                key = capability_binding_key(
                    consumer_addon_id=consumer_id,
                    requirement_id=requirement_id,
                    provider_addon_id=connection.connector_addon_id,
                    provider_capability_id=provider_capability_id,
                    connection_id=connection.id,
                    scope="user",
                    scope_id=str(user_id),
                )
                existing = (
                    session.query(CapabilityBinding.id)
                    .filter(CapabilityBinding.binding_key == key)
                    .first()
                )
                if existing is not None:
                    report.unchanged += 1
                    continue
                report.bindings_created += 1
                if report.dry_run:
                    continue
                session.add(
                    CapabilityBinding(
                        binding_key=key,
                        consumer_addon_id=consumer_id,
                        requirement_id=requirement_id,
                        provider_addon_id=connection.connector_addon_id,
                        provider_capability_id=provider_capability_id,
                        connection_id=connection.id,
                        scope="user",
                        scope_id=str(user_id),
                        priority=priority,
                        status="enabled",
                        contract_version=str(addon.version),
                        created_by_user_id=user_id,
                        metadata_json={
                            "migration": "legacy_implicit_provider_selection"
                        },
                    )
                )


def migrate_legacy_connection_data(
    session: Session,
    *,
    dry_run: bool = False,
    commit: bool = False,
    stage_hook: Callable[[str], None] | None = None,
) -> ConnectionMigrationReport:
    """Copy legacy data into the new shape without deleting or rewriting sources.

    The operation is deterministic and may be resumed. Existing new-shape rows
    win so a repeated compatibility migration never overwrites user changes.
    """

    report = ConnectionMigrationReport(dry_run=dry_run)
    journal = (
        _start_journal(session, report)
        if commit and not dry_run and _journal_available(session)
        else None
    )

    def checkpoint(stage: str) -> None:
        if journal is not None:
            _checkpoint_journal(session, journal, report, stage=stage)
        if stage_hook is not None:
            stage_hook(stage)

    try:
        credential_connections = _migrate_credentials(session, report)
        checkpoint("credentials")
        _migrate_preferences(session, report)
        checkpoint("preferences")
        provider_connections = _migrate_email_accounts(
            session,
            report,
            credential_connections,
        )
        checkpoint("email_accounts")
        _migrate_delegations(
            session,
            report,
            credential_connections,
            provider_connections=provider_connections,
        )
        checkpoint("delegations")
        _migrate_implicit_capability_bindings(session, report)
        checkpoint("capability_bindings")
        if not dry_run:
            session.flush()
            if journal is not None:
                _complete_journal(session, journal, report)
            elif commit:
                session.commit()
    except Exception as exc:
        if journal is not None:
            _fail_journal(session, journal.id, report, exc)
        raise
    return report


__all__ = [
    "CONNECTION_MIGRATION_ID",
    "LEGACY_SECRET_NAME",
    "MIGRATION_NAMESPACE",
    "ConnectionMigrationReport",
    "migrate_legacy_connection_data",
]
