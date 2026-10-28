"""Persistence adapter for capability resolution and setup diagnostics."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from iarf_assistant.models import (
    Addon,
    AddonConnection,
    CapabilityBinding,
    ConnectionGrant,
)

from .capabilities import (
    BindingSelection,
    CapabilityCatalog,
    CapabilityResolution,
    CapabilityResolutionReport,
    CapabilityResolver,
)
from .contracts import ResolvedAddonContract


@dataclass(frozen=True, slots=True)
class PersistedCapabilityResolution:
    """Resolution result plus installed-but-inactive provider diagnostics."""

    report: CapabilityResolutionReport
    inactive_compatible: Mapping[str, tuple[dict[str, Any], ...]]

    def to_dict(self) -> dict[str, Any]:
        payload = self.report.to_dict()
        for resolution in payload["resolutions"]:
            requirement_id = resolution["requirement"]["id"]
            resolution["inactive_compatible"] = list(
                self.inactive_compatible.get(requirement_id, ())
            )
        payload["inactive_provider_count"] = sum(
            len(items) for items in self.inactive_compatible.values()
        )
        return payload


class CapabilityUseError(RuntimeError):
    """Structured first-use failure that setup-aware callers can surface."""

    def __init__(
        self,
        *,
        consumer_addon_id: str,
        resolution: CapabilityResolution,
        message: str | None = None,
    ):
        self.consumer_addon_id = consumer_addon_id
        self.resolution = resolution
        super().__init__(message or resolution.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "capability_setup_required",
            "error": str(self),
            "consumer_addon_id": self.consumer_addon_id,
            "requirement_id": self.resolution.requirement.id,
            "capability": self.resolution.requirement.capability_id,
            "resolution": self.resolution.to_dict(),
        }


def resolved_contracts_from_manager(manager: Any) -> dict[str, ResolvedAddonContract]:
    """Read the manager's immutable contracts without exposing package internals."""

    packages = getattr(manager, "_addon_packages", {}) or {}
    return {
        str(addon_id): package.contract
        for addon_id, package in packages.items()
        if getattr(package, "contract", None) is not None
    }


def _visible_connection_ids(session: Session, user_id: int) -> set[str]:
    owned = session.query(AddonConnection.id).filter(
        AddonConnection.owner_user_id == int(user_id),
        AddonConnection.state == "ready",
    )
    granted = (
        session.query(ConnectionGrant.connection_id)
        .join(
            AddonConnection,
            AddonConnection.id == ConnectionGrant.connection_id,
        )
        .filter(
            ConnectionGrant.grantee_user_id == int(user_id),
            ConnectionGrant.revoked_at.is_(None),
            AddonConnection.state == "ready",
        )
    )
    return {str(row[0]) for row in owned.union(granted).all()}


def _binding_map(
    session: Session,
    *,
    consumer_addon_id: str,
    user_id: int,
    visible_connection_ids: set[str],
) -> dict[str, tuple[BindingSelection, ...]]:
    rows = (
        session.query(CapabilityBinding)
        .filter(
            CapabilityBinding.consumer_addon_id == consumer_addon_id,
            CapabilityBinding.status.in_(("enabled", "broken")),
            or_(
                CapabilityBinding.scope == "installation",
                (
                    (CapabilityBinding.scope == "user")
                    & (CapabilityBinding.scope_id == str(int(user_id)))
                ),
            ),
        )
        .order_by(
            CapabilityBinding.requirement_id,
            CapabilityBinding.priority,
            CapabilityBinding.id,
        )
        .all()
    )
    grouped: dict[str, list[BindingSelection]] = {}
    for row in rows:
        status = row.status
        if row.connection_id and row.connection_id not in visible_connection_ids:
            status = "broken"
        grouped.setdefault(row.requirement_id, []).append(
            BindingSelection(
                provider_addon_id=row.provider_addon_id,
                provider_capability_id=row.provider_capability_id,
                connection_id=row.connection_id,
                status=status,
                priority=row.priority,
            )
        )
    return {key: tuple(value) for key, value in grouped.items()}


def resolve_persisted_capabilities(
    session: Session,
    *,
    contracts: Mapping[str, ResolvedAddonContract],
    consumer_addon_id: str,
    user_id: int,
) -> PersistedCapabilityResolution:
    """Resolve one installed contract against active providers and saved bindings."""

    consumer = contracts.get(consumer_addon_id)
    if consumer is None:
        raise LookupError(f"Add-on {consumer_addon_id!r} has no resolved contract.")

    active_ids = {
        str(row[0])
        for row in session.query(Addon.id).filter(Addon.is_active.is_(True)).all()
    }
    # Include the consumer while evaluating activation so a package may satisfy
    # one of its own requirements after activation.
    active_ids.add(consumer_addon_id)
    active_contracts = [
        contract for addon_id, contract in contracts.items() if addon_id in active_ids
    ]
    visible_ids = _visible_connection_ids(session, user_id)
    bindings = _binding_map(
        session,
        consumer_addon_id=consumer_addon_id,
        user_id=user_id,
        visible_connection_ids=visible_ids,
    )
    report = CapabilityResolver(CapabilityCatalog(active_contracts)).resolve_contract(
        consumer,
        bindings=bindings,
    )

    active_catalog = CapabilityCatalog(active_contracts)
    all_catalog = CapabilityCatalog(contracts.values())
    inactive: dict[str, tuple[dict[str, Any], ...]] = {}
    for requirement in consumer.capability_requirements:
        active_keys = {
            (match.candidate.addon_id, match.candidate.provider.id)
            for match in active_catalog.matches(requirement)
            if match.compatible
        }
        inactive[requirement.id] = tuple(
            match.candidate.to_dict()
            for match in all_catalog.matches(requirement)
            if match.compatible
            and (match.candidate.addon_id, match.candidate.provider.id)
            not in active_keys
        )
    return PersistedCapabilityResolution(
        report=report,
        inactive_compatible=inactive,
    )


def require_persisted_capability(
    session: Session,
    *,
    contracts: Mapping[str, ResolvedAddonContract],
    consumer_addon_id: str,
    requirement_id: str,
    user_id: int,
) -> CapabilityResolution:
    """Resolve one capability at its use boundary or raise a setup state."""

    persisted = resolve_persisted_capabilities(
        session,
        contracts=contracts,
        consumer_addon_id=consumer_addon_id,
        user_id=user_id,
    )
    resolution = next(
        (
            item
            for item in persisted.report.resolutions
            if item.requirement.id == requirement_id
        ),
        None,
    )
    if resolution is None:
        raise LookupError(
            f"Add-on {consumer_addon_id!r} has no capability requirement "
            f"{requirement_id!r}."
        )
    if not resolution.resolved:
        raise CapabilityUseError(
            consumer_addon_id=consumer_addon_id,
            resolution=resolution,
        )
    return resolution


def journal_capability_setup_required(
    session: Session,
    *,
    error: CapabilityUseError,
    actor_user_id: int | None,
    source: str = "first_use",
    commit: bool = True,
) -> bool:
    """Record a structured setup failure in the add-on lifecycle journal."""

    addon = session.get(Addon, error.consumer_addon_id)
    if addon is None:
        return False
    from .lifecycle import AddonLifecycleService

    AddonLifecycleService(session).record_event(
        addon,
        event="capability_setup_required",
        success=False,
        detail=str(error),
        metadata={
            "source": str(source),
            "requirement_id": error.resolution.requirement.id,
            "capability": error.resolution.requirement.capability_id,
            "resolution_status": error.resolution.status.value,
            "provider_choices": [
                {
                    "addon_id": item.addon_id,
                    "provider_id": item.provider.id,
                }
                for item in error.resolution.compatible
            ],
        },
        actor_user_id=actor_user_id,
        commit=commit,
    )
    return True


def configure_capability_error_handling(app: Any) -> None:
    """Expose first-use setup failures consistently across every add-on API."""

    from flask import jsonify
    from flask_login import current_user

    @app.errorhandler(CapabilityUseError)
    def _handle_capability_use_error(error: CapabilityUseError):
        from iarf_assistant.extensions import db

        try:
            journal_capability_setup_required(
                db.session,
                error=error,
                actor_user_id=(
                    int(current_user.id)
                    if current_user.is_authenticated
                    else None
                ),
            )
        except Exception:
            db.session.rollback()
            app.logger.exception("Could not journal capability setup failure.")
        return jsonify(error.to_dict()), 409


__all__ = [
    "CapabilityUseError",
    "PersistedCapabilityResolution",
    "configure_capability_error_handling",
    "journal_capability_setup_required",
    "require_persisted_capability",
    "resolve_persisted_capabilities",
    "resolved_contracts_from_manager",
]
