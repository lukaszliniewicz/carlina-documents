"""Connection, secret, grant, and capability-binding domain services."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy.orm import Session

from iarf_assistant.models import (
    AddonConnection,
    CapabilityBinding,
    ConnectionGrant,
    ConnectionSecret,
)

from .contracts import CAPABILITY_SCOPES

DECLARATION_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]*$")
CONNECTION_STATES = frozenset(
    {"authorizing", "ready", "needs_attention", "disabled", "legacy_unverified"}
)
BINDING_STATUSES = frozenset({"enabled", "disabled", "broken"})


class ConnectionError(RuntimeError):
    """Base error for Connection domain operations."""


class ConnectionNotFound(ConnectionError):
    pass


class SecretNotFound(ConnectionError):
    pass


class SecretAccessDenied(ConnectionError):
    pass


class InvalidConnectionDeclaration(ConnectionError):
    pass


def _require_declaration_id(value: str, *, field: str) -> str:
    normalized = str(value or "").strip()
    if not DECLARATION_ID_PATTERN.fullmatch(normalized):
        raise InvalidConnectionDeclaration(
            f"{field} must be a stable lowercase identifier."
        )
    return normalized


def capability_binding_key(
    *,
    consumer_addon_id: str,
    requirement_id: str,
    provider_addon_id: str,
    provider_capability_id: str,
    connection_id: str | None,
    scope: str,
    scope_id: str,
) -> str:
    """Return a deterministic idempotency key for one scoped binding."""

    payload = {
        "connection_id": connection_id or "",
        "consumer_addon_id": consumer_addon_id,
        "provider_addon_id": provider_addon_id,
        "provider_capability_id": provider_capability_id,
        "requirement_id": requirement_id,
        "scope": scope,
        "scope_id": scope_id,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class ConnectionService:
    def __init__(self, session: Session):
        self.session = session

    def create(
        self,
        *,
        owner_user_id: int,
        connector_addon_id: str,
        connector_definition_id: str,
        label: str,
        state: str = "ready",
        is_default: bool = False,
        config: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> AddonConnection:
        connector_addon_id = _require_declaration_id(
            connector_addon_id,
            field="connector_addon_id",
        )
        connector_definition_id = _require_declaration_id(
            connector_definition_id,
            field="connector_definition_id",
        )
        if state not in CONNECTION_STATES:
            raise InvalidConnectionDeclaration(
                f"state must be one of: {', '.join(sorted(CONNECTION_STATES))}."
            )
        normalized_label = str(label or "").strip()
        if not normalized_label:
            raise InvalidConnectionDeclaration("label must not be empty.")
        connection = AddonConnection(
            owner_user_id=int(owner_user_id),
            connector_addon_id=connector_addon_id,
            connector_definition_id=connector_definition_id,
            label=normalized_label,
            state=state,
            is_default=bool(is_default),
            config_json=dict(config or {}),
            metadata_json=dict(metadata or {}),
        )
        self.session.add(connection)
        return connection

    def upsert_owned_account(
        self,
        *,
        owner_user_id: int,
        connector_addon_id: str,
        connector_definition_id: str,
        identity_field: str,
        identity_value: object,
        label: str,
        config: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        make_default: bool = True,
    ) -> tuple[AddonConnection, bool]:
        """Create or refresh one externally identified account Connection.

        OAuth callbacks generally learn a stable remote account identifier only
        after authorization.  Encrypted JSON columns cannot be filtered by a
        portable SQL expression, so the small owner-scoped candidate set is
        compared in Python.  Duplicate historical rows are retained for
        recovery but disabled and removed from active capability resolution.
        """

        owner_user_id = int(owner_user_id)
        connector_addon_id = _require_declaration_id(
            connector_addon_id,
            field="connector_addon_id",
        )
        connector_definition_id = _require_declaration_id(
            connector_definition_id,
            field="connector_definition_id",
        )
        identity_field = _require_declaration_id(
            identity_field,
            field="identity_field",
        )
        normalized_identity = str(identity_value or "").strip().casefold()
        if not normalized_identity:
            raise InvalidConnectionDeclaration("identity_value must not be empty.")

        candidates = (
            self.session.query(AddonConnection)
            .filter(
                AddonConnection.owner_user_id == owner_user_id,
                AddonConnection.connector_addon_id == connector_addon_id,
                AddonConnection.connector_definition_id == connector_definition_id,
            )
            .order_by(
                AddonConnection.is_default.desc(),
                AddonConnection.created_at.desc(),
                AddonConnection.id,
            )
            .all()
        )
        matches = [
            item
            for item in candidates
            if str(
                (item.config_json or {}).get(identity_field, "")
                if isinstance(item.config_json, Mapping)
                else ""
            )
            .strip()
            .casefold()
            == normalized_identity
        ]
        connection = matches[0] if matches else None
        created = connection is None
        resolved_config = dict(config or {})
        resolved_config[identity_field] = str(identity_value).strip()
        if connection is None:
            connection = self.create(
                owner_user_id=owner_user_id,
                connector_addon_id=connector_addon_id,
                connector_definition_id=connector_definition_id,
                label=label,
                is_default=make_default,
                config=resolved_config,
                metadata=metadata,
            )
            # The generated primary key is needed by the default-selection
            # update below. Without this flush, ``id != None`` becomes
            # ``IS NOT NULL`` and can accidentally update the new row too.
            self.session.flush([connection])
        else:
            connection.label = str(label or "").strip()
            if not connection.label:
                raise InvalidConnectionDeclaration("label must not be empty.")
            connection.state = "ready"
            connection.config_json = resolved_config
            connection.metadata_json = dict(metadata or {})
            connection.health_status = None
            connection.health_checked_at = None

        duplicate_ids = [item.id for item in matches[1:]]
        for duplicate in matches[1:]:
            duplicate.state = "disabled"
            duplicate.is_default = False
        if duplicate_ids:
            (
                self.session.query(CapabilityBinding)
                .filter(
                    CapabilityBinding.connection_id.in_(duplicate_ids),
                    CapabilityBinding.status == "enabled",
                )
                .update(
                    {
                        "status": "disabled",
                        "last_error": "Connection was superseded by a reconnect.",
                    },
                    synchronize_session=False,
                )
            )

        if make_default:
            (
                self.session.query(AddonConnection)
                .filter(
                    AddonConnection.owner_user_id == owner_user_id,
                    AddonConnection.connector_addon_id == connector_addon_id,
                    AddonConnection.connector_definition_id == connector_definition_id,
                    AddonConnection.id != connection.id,
                )
                .update({"is_default": False}, synchronize_session="fetch")
            )
            connection.is_default = True
        return connection, created

    def visible_to_user(
        self,
        user_id: int,
        *,
        connector_addon_id: str | None = None,
    ) -> tuple[AddonConnection, ...]:
        owned = self.session.query(AddonConnection.id).filter(
            AddonConnection.owner_user_id == int(user_id)
        )
        granted = self.session.query(ConnectionGrant.connection_id).filter(
            ConnectionGrant.grantee_user_id == int(user_id),
            ConnectionGrant.revoked_at.is_(None),
        )
        query = self.session.query(AddonConnection).filter(
            AddonConnection.id.in_(owned.union(granted))
        )
        if connector_addon_id:
            query = query.filter(
                AddonConnection.connector_addon_id == connector_addon_id
            )
        return tuple(query.order_by(AddonConnection.created_at, AddonConnection.id))

    def require_visible(
        self,
        connection_id: str,
        *,
        user_id: int,
    ) -> AddonConnection:
        """Return one owned or actively granted Connection.

        Callers resolving an explicit Connection ID must use the same access
        rule as list/discovery paths. Looking up the row directly first would
        expose another user's non-secret configuration before the Secret
        Broker gets a chance to enforce its policy.
        """

        connection = self.session.get(AddonConnection, str(connection_id))
        if connection is None:
            raise ConnectionNotFound(f"Unknown Connection {connection_id!r}.")
        if connection.owner_user_id == int(user_id):
            return connection
        grant = (
            self.session.query(ConnectionGrant.id)
            .filter(
                ConnectionGrant.connection_id == connection.id,
                ConnectionGrant.grantee_user_id == int(user_id),
                ConnectionGrant.revoked_at.is_(None),
            )
            .first()
        )
        if grant is None:
            raise SecretAccessDenied("The user cannot use this Connection.")
        return connection

    def owned(self, connection_id: str, *, owner_user_id: int) -> AddonConnection:
        """Return an owned Connection or reject cross-user mutation."""

        connection = self.session.get(AddonConnection, str(connection_id))
        if connection is None:
            raise ConnectionNotFound(f"Unknown Connection {connection_id!r}.")
        if connection.owner_user_id != int(owner_user_id):
            raise SecretAccessDenied("Only the Connection owner can change it.")
        return connection

    def make_default(
        self,
        connection_id: str,
        *,
        owner_user_id: int,
    ) -> AddonConnection:
        """Select one ready Connection as the owner's connector default."""

        connection = self.owned(connection_id, owner_user_id=owner_user_id)
        if connection.state != "ready":
            raise InvalidConnectionDeclaration(
                "Only a ready Connection can become the default."
            )
        self.session.query(AddonConnection).filter(
            AddonConnection.owner_user_id == int(owner_user_id),
            AddonConnection.connector_addon_id == connection.connector_addon_id,
            AddonConnection.connector_definition_id
            == connection.connector_definition_id,
            AddonConnection.id != connection.id,
        ).update({"is_default": False}, synchronize_session="fetch")
        connection.is_default = True
        return connection

    def delete_owned(
        self,
        connection_id: str,
        *,
        owner_user_id: int,
    ) -> AddonConnection:
        """Delete an owned Connection and make its saved bindings explicitly broken."""

        connection = self.owned(connection_id, owner_user_id=owner_user_id)
        bindings = (
            self.session.query(CapabilityBinding)
            .filter(CapabilityBinding.connection_id == connection.id)
            .all()
        )
        for binding in bindings:
            metadata = dict(binding.metadata_json or {})
            metadata["broken_connection_id"] = connection.id
            metadata["broken_reason"] = "connection_deleted"
            binding.metadata_json = metadata
            binding.last_error = "The selected Connection was deleted by its owner."
            binding.status = "broken"
            binding.connection_id = None
        self.session.delete(connection)
        return connection

    def bind(
        self,
        *,
        consumer_addon_id: str,
        requirement_id: str,
        provider_addon_id: str,
        provider_capability_id: str,
        contract_version: str,
        connection_id: str | None = None,
        scope: str = "installation",
        scope_id: str = "",
        priority: int = 0,
        status: str = "enabled",
        created_by_user_id: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> CapabilityBinding:
        consumer_addon_id = _require_declaration_id(
            consumer_addon_id,
            field="consumer_addon_id",
        )
        requirement_id = _require_declaration_id(
            requirement_id,
            field="requirement_id",
        )
        provider_addon_id = _require_declaration_id(
            provider_addon_id,
            field="provider_addon_id",
        )
        provider_capability_id = _require_declaration_id(
            provider_capability_id,
            field="provider_capability_id",
        )
        if scope not in CAPABILITY_SCOPES:
            raise InvalidConnectionDeclaration(
                f"scope must be one of: {', '.join(sorted(CAPABILITY_SCOPES))}."
            )
        normalized_scope_id = str(scope_id or "").strip()
        if scope != "installation" and not normalized_scope_id:
            raise InvalidConnectionDeclaration(
                f"scope_id is required for {scope!r} bindings."
            )
        if scope == "installation":
            normalized_scope_id = ""
        if status not in BINDING_STATUSES:
            raise InvalidConnectionDeclaration(
                f"status must be one of: {', '.join(sorted(BINDING_STATUSES))}."
            )
        if (
            connection_id is not None
            and self.session.get(AddonConnection, connection_id) is None
        ):
            raise ConnectionNotFound(f"Unknown Connection {connection_id!r}.")
        key = capability_binding_key(
            consumer_addon_id=consumer_addon_id,
            requirement_id=requirement_id,
            provider_addon_id=provider_addon_id,
            provider_capability_id=provider_capability_id,
            connection_id=connection_id,
            scope=scope,
            scope_id=normalized_scope_id,
        )
        binding = (
            self.session.query(CapabilityBinding)
            .filter(CapabilityBinding.binding_key == key)
            .one_or_none()
        )
        if binding is None:
            binding = CapabilityBinding(binding_key=key)
            self.session.add(binding)
        binding.consumer_addon_id = consumer_addon_id
        binding.requirement_id = requirement_id
        binding.provider_addon_id = provider_addon_id
        binding.provider_capability_id = provider_capability_id
        binding.connection_id = connection_id
        binding.scope = scope
        binding.scope_id = normalized_scope_id
        binding.priority = int(priority)
        binding.status = status
        binding.contract_version = str(contract_version)
        binding.created_by_user_id = created_by_user_id
        binding.metadata_json = dict(metadata or {})
        return binding


class SecretBroker:
    """The only supported application-layer gateway to Connection secrets."""

    def __init__(
        self,
        session: Session,
        *,
        actor_user_id: int,
        requester_addon_id: str,
    ):
        self.session = session
        self.actor_user_id = int(actor_user_id)
        self.requester_addon_id = _require_declaration_id(
            requester_addon_id,
            field="requester_addon_id",
        )

    def _connection(self, connection_id: str) -> AddonConnection:
        connection = self.session.get(AddonConnection, connection_id)
        if connection is None:
            raise ConnectionNotFound(f"Unknown Connection {connection_id!r}.")
        return connection

    def _assert_user_access(self, connection: AddonConnection) -> None:
        if connection.owner_user_id == self.actor_user_id:
            return
        grant = (
            self.session.query(ConnectionGrant.id)
            .filter(
                ConnectionGrant.connection_id == connection.id,
                ConnectionGrant.grantee_user_id == self.actor_user_id,
                ConnectionGrant.revoked_at.is_(None),
            )
            .first()
        )
        if grant is None:
            raise SecretAccessDenied("The user cannot use this Connection.")

    def _assert_addon_access(self, connection: AddonConnection) -> None:
        if connection.connector_addon_id == self.requester_addon_id:
            return
        binding = (
            self.session.query(CapabilityBinding.id)
            .filter(
                CapabilityBinding.connection_id == connection.id,
                CapabilityBinding.provider_addon_id == self.requester_addon_id,
                CapabilityBinding.status == "enabled",
            )
            .first()
        )
        if binding is None:
            raise SecretAccessDenied(
                "The requesting add-on has no active binding to this Connection."
            )

    def _authorize(self, connection: AddonConnection) -> None:
        self._assert_user_access(connection)
        self._assert_addon_access(connection)

    def names(self, connection_id: str) -> tuple[str, ...]:
        connection = self._connection(connection_id)
        self._authorize(connection)
        rows = (
            self.session.query(ConnectionSecret.name)
            .filter(ConnectionSecret.connection_id == connection.id)
            .order_by(ConnectionSecret.name)
            .all()
        )
        return tuple(row[0] for row in rows)

    def read(self, connection_id: str, name: str = "credentials") -> dict[str, Any]:
        connection = self._connection(connection_id)
        self._authorize(connection)
        secret = (
            self.session.query(ConnectionSecret)
            .filter(
                ConnectionSecret.connection_id == connection.id,
                ConnectionSecret.name == _require_declaration_id(name, field="name"),
            )
            .one_or_none()
        )
        if secret is None:
            raise SecretNotFound(
                f"Connection {connection.id!r} has no secret named {name!r}."
            )
        return copy.deepcopy(dict(secret.secret_data or {}))

    def store(
        self,
        connection_id: str,
        payload: Mapping[str, Any],
        *,
        name: str = "credentials",
    ) -> ConnectionSecret:
        connection = self._connection(connection_id)
        if connection.owner_user_id != self.actor_user_id:
            raise SecretAccessDenied("Only the Connection owner can change secrets.")
        self._assert_addon_access(connection)
        return self._store_authorized(connection, payload, name=name)

    def rotate(
        self,
        connection_id: str,
        payload: Mapping[str, Any],
        *,
        name: str = "credentials",
    ) -> ConnectionSecret:
        """Persist provider-issued replacement material during delegated use.

        A grantee may use a Connection and trigger an OAuth refresh, but cannot
        otherwise edit its credentials.  Rotation is therefore limited to the
        connector package that owns the Connection and still requires a valid
        owner or grant relationship for the acting user.
        """

        connection = self._connection(connection_id)
        self._authorize(connection)
        if connection.connector_addon_id != self.requester_addon_id:
            raise SecretAccessDenied(
                "Only the owning connector can rotate Connection secrets."
            )
        return self._store_authorized(connection, payload, name=name)

    def _store_authorized(
        self,
        connection: AddonConnection,
        payload: Mapping[str, Any],
        *,
        name: str,
    ) -> ConnectionSecret:
        normalized_name = _require_declaration_id(name, field="name")
        secret = (
            self.session.query(ConnectionSecret)
            .filter(
                ConnectionSecret.connection_id == connection.id,
                ConnectionSecret.name == normalized_name,
            )
            .one_or_none()
        )
        if secret is None:
            secret = ConnectionSecret(
                connection_id=connection.id,
                name=normalized_name,
                secret_data=dict(payload),
            )
            self.session.add(secret)
        else:
            secret.secret_data = dict(payload)
        return secret


def redact_secret_values(
    payload: Mapping[str, Any],
    *,
    visible_keys: Sequence[str] = (),
) -> dict[str, Any]:
    """Return safe diagnostic metadata without leaking secret values."""

    visible = set(visible_keys)
    return {
        str(key): copy.deepcopy(value) if key in visible else "***"
        for key, value in payload.items()
    }


__all__ = [
    "BINDING_STATUSES",
    "CONNECTION_STATES",
    "ConnectionError",
    "ConnectionNotFound",
    "ConnectionService",
    "InvalidConnectionDeclaration",
    "SecretAccessDenied",
    "SecretBroker",
    "SecretNotFound",
    "capability_binding_key",
    "redact_secret_values",
]
