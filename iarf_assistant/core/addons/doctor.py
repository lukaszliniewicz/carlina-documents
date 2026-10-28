"""User-scoped diagnostics for add-on capabilities, Connections, and runtimes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal

from sqlalchemy import or_
from sqlalchemy.orm import Session

from iarf_assistant.models import (
    Addon,
    AddonConnection,
    AddonDataMigrationRun,
    CapabilityBinding,
)

from .capability_runtime import resolve_persisted_capabilities
from .connections import ConnectionService
from .contracts import ResolvedAddonContract
from .migration_inventory import inspect_addon_migration_inventory

AddonDoctorStatus = Literal["ok", "info", "warning", "error"]


@dataclass(frozen=True, slots=True)
class AddonDoctorCheck:
    id: str
    title: str
    status: AddonDoctorStatus
    summary: str
    addon_id: str | None = None
    requirement_id: str | None = None
    connection_id: str | None = None
    details: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class AddonDoctorReport:
    checks: tuple[AddonDoctorCheck, ...]
    runtime: Mapping[str, Any]

    @property
    def errors(self) -> tuple[AddonDoctorCheck, ...]:
        return tuple(item for item in self.checks if item.status == "error")

    @property
    def warnings(self) -> tuple[AddonDoctorCheck, ...]:
        return tuple(item for item in self.checks if item.status == "warning")

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": {
                "errors": len(self.errors),
                "warnings": len(self.warnings),
                "ok": sum(item.status == "ok" for item in self.checks),
                "info": sum(item.status == "info" for item in self.checks),
            },
            "checks": [asdict(item) for item in self.checks],
            "runtime": dict(self.runtime),
        }


def _capability_checks(
    session: Session,
    *,
    contracts: Mapping[str, ResolvedAddonContract],
    active_ids: set[str],
    user_id: int,
) -> list[AddonDoctorCheck]:
    checks: list[AddonDoctorCheck] = []
    for addon_id, contract in sorted(contracts.items()):
        if not contract.capability_requirements:
            continue
        persisted = resolve_persisted_capabilities(
            session,
            contracts=contracts,
            consumer_addon_id=addon_id,
            user_id=user_id,
        )
        for resolution in persisted.report.resolutions:
            active = addon_id in active_ids
            if resolution.resolved:
                status: AddonDoctorStatus = "ok"
            elif resolution.status.value == "broken_binding":
                status = "error" if active else "warning"
            elif resolution.blocks_activation and active:
                status = "error"
            elif resolution.requirement.activation == "on_first_use":
                status = "info"
            else:
                status = "warning"
            checks.append(
                AddonDoctorCheck(
                    id=f"capability.{addon_id}.{resolution.requirement.id}",
                    title=f"{contract.name}: {resolution.requirement.capability_id}",
                    status=status,
                    summary=resolution.message,
                    addon_id=addon_id,
                    requirement_id=resolution.requirement.id,
                    details={
                        "active": active,
                        "resolution_status": resolution.status.value,
                        "activation": resolution.requirement.activation,
                        "scope": resolution.requirement.scope,
                        "selected_providers": [
                            item.addon_id for item in resolution.selected
                        ],
                        "connection_ids": list(resolution.connection_ids),
                        "inactive_compatible": len(
                            persisted.inactive_compatible.get(
                                resolution.requirement.id,
                                (),
                            )
                        ),
                    },
                )
            )
    return checks


def _connection_checks(
    session: Session,
    *,
    user_id: int,
) -> list[AddonDoctorCheck]:
    connections = ConnectionService(session).visible_to_user(user_id)
    if not connections:
        return [
            AddonDoctorCheck(
                id="connections.none",
                title="Connections",
                status="info",
                summary="No external account Connections are configured for this user.",
            )
        ]
    checks: list[AddonDoctorCheck] = []
    for connection in connections:
        if connection.state == "ready":
            status: AddonDoctorStatus = "ok"
            summary = "Connection is ready for capability binding."
        elif connection.state == "disabled":
            status = "info"
            summary = "Connection is disabled and excluded from resolution."
        elif connection.state == "needs_attention":
            status = "warning"
            summary = "Connection needs attention before it can be used."
        else:
            status = "warning"
            summary = "Legacy Connection has not been verified yet."
        checks.append(
            AddonDoctorCheck(
                id=f"connection.{connection.id}",
                title=f"{connection.connector_addon_id}: {connection.label}",
                status=status,
                summary=summary,
                addon_id=connection.connector_addon_id,
                connection_id=connection.id,
                details={
                    "state": connection.state,
                    "access": (
                        "owner" if connection.owner_user_id == user_id else "delegated"
                    ),
                    "health_status": connection.health_status,
                },
            )
        )
    return checks


def _binding_checks(
    session: Session,
    *,
    contracts: Mapping[str, ResolvedAddonContract],
    user_id: int,
) -> list[AddonDoctorCheck]:
    rows = (
        session.query(CapabilityBinding)
        .filter(
            CapabilityBinding.status.in_(("enabled", "broken")),
            or_(
                CapabilityBinding.scope == "installation",
                (
                    (CapabilityBinding.scope == "user")
                    & (CapabilityBinding.scope_id == str(user_id))
                ),
            ),
        )
        .order_by(CapabilityBinding.consumer_addon_id, CapabilityBinding.id)
        .all()
    )
    checks: list[AddonDoctorCheck] = []
    for binding in rows:
        missing: list[str] = []
        if binding.consumer_addon_id not in contracts:
            missing.append("consumer contract")
        if binding.provider_addon_id not in contracts:
            missing.append("provider contract")
        connection = (
            session.get(AddonConnection, binding.connection_id)
            if binding.connection_id
            else None
        )
        if binding.connection_id and connection is None:
            missing.append("Connection")
        if missing or binding.status == "broken":
            summary = binding.last_error
            if not summary:
                summary = (
                    f"Binding is missing: {', '.join(missing)}."
                    if missing
                    else "Binding is marked broken."
                )
            checks.append(
                AddonDoctorCheck(
                    id=f"binding.{binding.id}",
                    title=(
                        f"{binding.consumer_addon_id} → {binding.provider_addon_id}"
                    ),
                    status="error",
                    summary=summary,
                    addon_id=binding.consumer_addon_id,
                    requirement_id=binding.requirement_id,
                    connection_id=binding.connection_id,
                    details={"binding_status": binding.status, "missing": missing},
                )
            )
    return checks


def _migration_check(session: Session) -> AddonDoctorCheck:
    latest = (
        session.query(AddonDataMigrationRun)
        .order_by(AddonDataMigrationRun.started_at.desc(), AddonDataMigrationRun.id)
        .first()
    )
    if latest is None:
        return AddonDoctorCheck(
            id="migration.connections",
            title="Connection data migration",
            status="info",
            summary="No normalized Connection migration run is recorded.",
        )
    status: AddonDoctorStatus
    if latest.status == "completed":
        status = "ok"
    elif latest.status == "failed":
        status = "error"
    else:
        status = "warning"
    return AddonDoctorCheck(
        id="migration.connections",
        title="Connection data migration",
        status=status,
        summary=(
            f"Latest migration run is {latest.status} at checkpoint "
            f"{latest.checkpoint or 'not started'}."
        ),
        details={
            "run_id": latest.id,
            "status": latest.status,
            "checkpoint": latest.checkpoint,
            "error_type": latest.error_type,
        },
    )


def _migration_ledger_checks(
    session: Session,
    *,
    contracts: Mapping[str, ResolvedAddonContract],
) -> list[AddonDoctorCheck]:
    status_by_classification: dict[str, AddonDoctorStatus] = {
        "known_head": "ok",
        "known_behind": "warning",
        "mirror_mismatch": "warning",
        "uninitialized": "info",
        "legacy_adoption_required": "error",
        "unknown_quarantined": "error",
        "orphaned_empty": "warning",
        "orphaned_unknown": "warning",
    }
    checks: list[AddonDoctorCheck] = []
    for item in inspect_addon_migration_inventory(session, contracts):
        addon_id = item.addon_id
        name = (
            contracts[addon_id].name
            if addon_id is not None and addon_id in contracts
            else None
        )
        title = (
            f"{name}: migration ledger"
            if name
            else (
                f"{addon_id}: retained migration ledger"
                if addon_id is not None
                else f"Orphan migration table: {item.version_table}"
            )
        )
        checks.append(
            AddonDoctorCheck(
                id=f"migration.ledger.{addon_id or item.version_table}",
                title=title,
                status=status_by_classification[item.classification],
                summary=item.detail,
                addon_id=addon_id,
                details={
                    "classification": item.classification,
                    "version_table": item.version_table,
                    "physical_revision": item.physical_revision,
                    "recorded_revision": item.recorded_revision,
                    "head_revision": item.head_revision,
                    "revision_count": item.revision_count,
                    "mirror_mismatch": item.mirror_mismatch,
                },
            )
        )
    return checks


def run_addon_doctor(
    session: Session,
    *,
    contracts: Mapping[str, ResolvedAddonContract],
    user_id: int,
    manager: Any | None = None,
) -> AddonDoctorReport:
    """Build a deterministic, secret-free report for one user's add-on view."""

    user_id = int(user_id)
    active_ids = {
        str(row[0])
        for row in session.query(Addon.id).filter(Addon.is_active.is_(True)).all()
    }
    checks = _capability_checks(
        session,
        contracts=contracts,
        active_ids=active_ids,
        user_id=user_id,
    )
    checks.extend(_connection_checks(session, user_id=user_id))
    checks.extend(_binding_checks(session, contracts=contracts, user_id=user_id))
    checks.extend(_migration_ledger_checks(session, contracts=contracts))
    checks.append(_migration_check(session))
    runtime = {
        "container_kind": str(
            getattr(manager, "_container_runtime_kind", "not_probed")
        ),
        "container_available": bool(getattr(manager, "docker_available", False)),
        "container_detail": str(
            getattr(manager, "docker_status_message", "Container runtime not probed.")
        ),
        "wsl_distribution": getattr(manager, "wsl_distro", None),
    }
    return AddonDoctorReport(checks=tuple(checks), runtime=runtime)


__all__ = [
    "AddonDoctorCheck",
    "AddonDoctorReport",
    "AddonDoctorStatus",
    "run_addon_doctor",
]
