"""Shared transactional coordinator for upload and signed-catalog installs."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from iarf_assistant.core.logging_observability import logging_context
from iarf_assistant.models import Addon

from .installer import AddonInstallError, AddonPackageInstaller, StagedAddonPackage
from .migration_contracts import stored_migration_upgrade_issues

_LOGGER = logging.getLogger(__name__)


class AddonDependencyReviewRequired(AddonInstallError):
    def __init__(self, addon_id: str, notices: list[dict], missing_ids: set[str]):
        super().__init__(
            "Review and acknowledge the package's declared dependency licenses "
            "before installation."
        )
        self.addon_id = addon_id
        self.notices = notices
        self.missing_ids = tuple(sorted(missing_ids))


@dataclass(frozen=True, slots=True)
class AddonInstallOutcome:
    addon_id: str
    name: str
    version: str
    previous_version: str | None
    upgraded: bool
    requires_restart: bool
    trust_tier: str
    permissions: tuple[dict[str, str], ...]
    archive_sha256: str
    signature_status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": True,
            "message": (
                f"Add-on '{self.name}' upgraded to v{self.version}."
                if self.upgraded
                else f"Add-on '{self.name}' installed and activated."
            ),
            "addon_id": self.addon_id,
            "requires_restart": self.requires_restart,
            "trust_tier": self.trust_tier,
            "permissions": list(self.permissions),
            "archive_sha256": self.archive_sha256,
            "signature_status": self.signature_status,
        }


class AddonInstallationCoordinator:
    """Publish one validated generation and restore the prior marker on failure."""

    def __init__(
        self,
        *,
        session,
        manager,
        installer: AddonPackageInstaller,
        actor_user_id: int | None = None,
        log: Callable[[str, str], None] | None = None,
        progress: Callable[[str, float, str, dict[str, Any] | None], None]
        | None = None,
        cancellation_check: Callable[[], None] | None = None,
    ) -> None:
        self.session = session
        self.manager = manager
        self.installer = installer
        self.actor_user_id = actor_user_id
        self.log = log or (lambda _addon_id, _message: None)
        self.progress = progress or (
            lambda _stage, _progress, _message, _metadata=None: None
        )
        self.cancellation_check = cancellation_check or (lambda: None)

    def _dependency_notices(self, staged: StagedAddonPackage) -> list[dict]:
        manifest = staged.contract.to_dict(include_manifest=True)["manifest"]
        notices = self.manager._normalize_dependency_notices(manifest)
        return notices if isinstance(notices, list) else []

    @staticmethod
    def _migration_descriptor(record: Addon) -> dict[str, Any] | None:
        payload = (
            record.resolved_contract_json
            if isinstance(record.resolved_contract_json, dict)
            else {}
        )
        descriptor = payload.get("database_migration")
        return descriptor if isinstance(descriptor, dict) else None

    def _validate_migration_update(
        self,
        record: Addon | None,
        staged: StagedAddonPackage,
    ) -> None:
        if record is None:
            return
        target = staged.contract.migration_bundle
        previous = self._migration_descriptor(record)
        issues = list(stored_migration_upgrade_issues(previous, target))
        recorded_revision = str(record.alembic_revision or "").strip() or None
        if previous is None and recorded_revision is not None:
            if target is None:
                issues.append(
                    "the target package removes a migration contract with "
                    "recorded database state"
                )
            elif recorded_revision not in target.revision_ids:
                issues.append(
                    f"recorded revision {recorded_revision!r} is absent from "
                    "the target migration graph"
                )
        if issues:
            raise AddonInstallError(
                f"Unsafe database migration update for '{record.id}': "
                + "; ".join(dict.fromkeys(issues))
                + "."
            )

    @staticmethod
    def _required_notice_ids(notices: list[dict]) -> set[str]:
        return {
            str(notice["id"])
            for notice in notices[:50]
            if isinstance(notice, dict)
            and isinstance(notice.get("id"), str)
            and notice.get("review_required", True)
        }

    def install(
        self,
        staged: StagedAddonPackage,
        *,
        reviewed_notice_ids: Iterable[str] = (),
        retain_published_on_failure: bool = False,
    ) -> AddonInstallOutcome:
        """Serialize package publication, activation, and rollback as one mutation.

        ``retain_published_on_failure`` is reserved for recovery paths that have
        deliberately transferred package ownership before publication.  Those
        callers cannot safely fall back to the prior owner if activation fails.
        Ordinary installs and upgrades keep the default rollback behavior.
        """

        self.manager._require_lifecycle_owner("install or upgrade add-on packages")
        with self.manager._lifecycle_mutation_lock():
            return self._install_serialized(
                staged,
                reviewed_notice_ids=reviewed_notice_ids,
                retain_published_on_failure=retain_published_on_failure,
            )

    def _install_serialized(
        self,
        staged: StagedAddonPackage,
        *,
        reviewed_notice_ids: Iterable[str] = (),
        retain_published_on_failure: bool = False,
    ) -> AddonInstallOutcome:
        addon_id = staged.contract.addon_id
        selection_check = getattr(self.manager, "_require_selected_addon", None)
        if callable(selection_check):
            selection_check(addon_id)
        else:
            allowed_addon_ids = getattr(self.manager, "allowed_addon_ids", None)
            if allowed_addon_ids is None:
                allowed_addon_ids = getattr(
                    self.manager,
                    "_allowed_addon_ids",
                    None,
                )
            if allowed_addon_ids is not None and addon_id not in allowed_addon_ids:
                raise ValueError("Add-on is not selected in this runtime profile.")
        manifest = staged.contract.to_dict(include_manifest=True)["manifest"]
        notices = self._dependency_notices(staged)
        required_notice_ids = self._required_notice_ids(notices)
        reviewed_ids = {
            str(value).strip() for value in reviewed_notice_ids if str(value).strip()
        }
        missing_notice_ids = required_notice_ids - reviewed_ids
        if missing_notice_ids:
            raise AddonDependencyReviewRequired(
                addon_id,
                notices,
                missing_notice_ids,
            )

        self.cancellation_check()
        self.progress(
            "package_verified",
            22,
            "Package contract and dependency review are valid.",
            None,
        )
        self.log(
            addon_id,
            f"Validated package SHA-256: {staged.archive_sha256}",
        )
        addon_record = self.session.get(Addon, addon_id)
        if addon_record and addon_record.source_kind != "installed":
            raise AddonInstallError(
                f"Installed package '{addon_id}' cannot replace a built-in "
                "Carlina add-on."
            )
        self._validate_migration_update(addon_record, staged)
        upgraded = addon_record is not None
        was_active = bool(addon_record and addon_record.is_active)
        previous_version = addon_record.version if addon_record else None
        target_migration = staged.contract.migration_bundle
        pre_install_revision = (
            self.manager._current_addon_db_revision_from_contract(target_migration)
            if target_migration is not None
            else None
        )
        published = None
        try:
            if addon_record is None:
                addon_record = Addon()
                addon_record.id = addon_id
                addon_record.version = staged.contract.version
                addon_record.is_active = False
                addon_record.source_kind = "installed"
                addon_record.trust_tier = staged.contract.trust_tier.value
                addon_record.lifecycle_state = "discovered"
                addon_record.manifest_sha256 = staged.contract.manifest_sha256
                addon_record.contract_sha256 = staged.contract.contract_sha256
                addon_record.resolved_contract_json = staged.contract.to_dict()
                addon_record.provenance_json = staged.provenance
                self.session.add(addon_record)
                self.session.commit()
                self.manager.lifecycle.transition(
                    addon_record,
                    "staged",
                    event="package_staged",
                    metadata={"archive_sha256": staged.archive_sha256},
                    actor_user_id=self.actor_user_id,
                )
                self.manager.lifecycle.transition(
                    addon_record,
                    "verified",
                    event="package_verified",
                    actor_user_id=self.actor_user_id,
                )
                self.manager.lifecycle.transition(
                    addon_record,
                    "validated",
                    event="contract_compiled",
                    metadata={"contract_sha256": staged.contract.contract_sha256},
                    actor_user_id=self.actor_user_id,
                )
            else:
                self.manager.lifecycle.record_event(
                    addon_record,
                    event="upgrade_package_validated",
                    metadata={
                        "from_version": previous_version,
                        "to_version": staged.contract.version,
                        "archive_sha256": staged.archive_sha256,
                        "contract_sha256": staged.contract.contract_sha256,
                    },
                    actor_user_id=self.actor_user_id,
                )

            self.cancellation_check()
            self.progress(
                "package_publish",
                28,
                "Publishing the validated package generation.",
                None,
            )
            if was_active:
                self.manager.deactivate_addon(addon_id)
            published = self.installer.publish(staged)
            self.manager._sync_addons_from_filesystem()
            addon_record = self.session.get(Addon, addon_id)
            if addon_record is None:
                raise AddonInstallError(
                    f"Published add-on '{addon_id}' disappeared during registry sync."
                )
            addon_record.version = staged.contract.version
            addon_record.installed_at = addon_record.installed_at or datetime.now(UTC)
            self.session.commit()
            self.manager.lifecycle.record_event(
                addon_record,
                event="package_published",
                metadata={
                    "version": staged.contract.version,
                    "archive_sha256": staged.archive_sha256,
                    "previous_version": published.previous_version,
                },
                actor_user_id=self.actor_user_id,
            )

            with logging_context(
                addon_id=addon_id,
                operation="addon_upgrade" if upgraded else "addon_activate",
            ):
                self.manager.activate_addon(
                    addon_id,
                    upgrade_from=previous_version if upgraded else None,
                    dependency_review=sorted(reviewed_ids & required_notice_ids),
                    operation_progress=self.progress,
                    cancellation_check=self.cancellation_check,
                )
            self.progress(
                "activation_complete",
                98,
                "Package activation completed.",
                None,
            )
            self.log(addon_id, "Install completed.")
            return AddonInstallOutcome(
                addon_id=addon_id,
                name=str(manifest["name"]),
                version=staged.contract.version,
                previous_version=previous_version,
                upgraded=upgraded,
                requires_restart=True,
                trust_tier=staged.contract.trust_tier.value,
                permissions=tuple(
                    {"id": item.id, "reason": item.reason}
                    for item in staged.contract.permissions
                ),
                archive_sha256=staged.archive_sha256,
                signature_status=str(staged.provenance["signature_status"]),
            )
        except Exception as exc:
            self.session.rollback()
            self.log(addon_id, f"Install failed: {exc}")
            if published is not None:
                if self._migration_advanced(
                    staged,
                    previous_revision=pre_install_revision,
                ):
                    self._retain_migrated_generation(
                        published,
                        error=exc,
                        previous_version=previous_version,
                        previous_revision=pre_install_revision,
                    )
                elif retain_published_on_failure:
                    self._retain_published_generation(
                        published,
                        error=exc,
                        previous_version=previous_version,
                    )
                else:
                    self._rollback_published(
                        published,
                        previous_version=previous_version,
                        was_active=was_active,
                    )
            else:
                failed_record = self.session.get(Addon, addon_id)
                if failed_record is not None:
                    try:
                        self.manager.lifecycle.fail(
                            failed_record,
                            event="package_install_failed",
                            error=exc,
                            metadata={
                                "attempted_version": staged.contract.version,
                                "archive_sha256": staged.archive_sha256,
                            },
                        )
                    except Exception:
                        self.session.rollback()
                        _LOGGER.exception(
                            "Failed to journal add-on install failure for '%s'.",
                            addon_id,
                        )
            raise

    def _migration_advanced(
        self,
        staged: StagedAddonPackage,
        *,
        previous_revision: str | None,
    ) -> bool:
        target = staged.contract.migration_bundle
        if target is None:
            return False
        try:
            current = self.manager._current_addon_db_revision_from_contract(target)
        except Exception:
            _LOGGER.exception(
                "Could not prove the migration state after failed activation; "
                "retaining the selected package generation."
            )
            return True
        return current != previous_revision

    def _retain_migrated_generation(
        self,
        published,
        *,
        error: Exception,
        previous_version: str | None,
        previous_revision: str | None,
    ) -> None:
        """Keep code paired with a schema that advanced before activation failed."""

        record = self.session.get(Addon, published.addon_id)
        if record is None:
            raise AddonInstallError(
                f"Migrated add-on '{published.addon_id}' disappeared after a "
                "failed activation; its selected package was retained."
            ) from error
        record.version = published.version
        record.is_active = False
        self.session.commit()
        self.manager.lifecycle.record_event(
            record,
            event="package_generation_retained_after_migration",
            success=False,
            detail=str(error),
            metadata={
                "previous_version": previous_version,
                "selected_version": published.version,
                "previous_revision": previous_revision,
                "physical_revision": record.alembic_revision,
                "retry_safe": True,
            },
            actor_user_id=self.actor_user_id,
        )
        self.log(
            published.addon_id,
            "Activation failed after the database advanced; the matching "
            "package generation remains selected and disabled for a safe retry.",
        )

    def _retain_published_generation(
        self,
        published,
        *,
        error: Exception,
        previous_version: str | None,
    ) -> None:
        """Keep a selected generation when ownership cannot safely roll back."""

        record = self.session.get(Addon, published.addon_id)
        if record is None:
            raise AddonInstallError(
                f"Retained add-on '{published.addon_id}' disappeared after a "
                "failed activation."
            ) from error
        record.source_kind = "installed"
        record.version = published.version
        record.is_active = False
        self.session.commit()
        self.manager.lifecycle.record_event(
            record,
            event="package_generation_retained_after_failure",
            success=False,
            detail=str(error),
            metadata={
                "previous_version": previous_version,
                "selected_version": published.version,
                "retry_safe": True,
                "retention_policy": "retain_published_on_failure",
            },
            actor_user_id=self.actor_user_id,
        )
        self.log(
            published.addon_id,
            "Activation failed after ownership transfer; the selected package "
            "generation remains installed and disabled for a safe retry.",
        )

    def _rollback_published(
        self,
        published,
        *,
        previous_version: str | None,
        was_active: bool,
    ) -> None:
        _LOGGER.info("Rolling back add-on version selection...")
        try:
            addon_record = self.session.get(Addon, published.addon_id)
            if addon_record is not None:
                self.manager.lifecycle.transition(
                    addon_record,
                    "rollback_pending",
                    event="rollback_started",
                    metadata={
                        "failed_version": published.version,
                        "previous_version": previous_version,
                    },
                    actor_user_id=self.actor_user_id,
                    force=True,
                )
            self.installer.rollback(published)
            self.manager._sync_addons_from_filesystem()
            addon_record = self.session.get(Addon, published.addon_id)
            if addon_record:
                if previous_version:
                    addon_record.version = previous_version
                self.manager.lifecycle.transition(
                    addon_record,
                    "rolled_back",
                    event="rollback_completed",
                    metadata={
                        "failed_version": published.version,
                        "restored_version": previous_version,
                    },
                    actor_user_id=self.actor_user_id,
                    force=True,
                )
            if was_active and previous_version:
                self.manager.activate_addon(published.addon_id)
        except Exception:
            self.session.rollback()
            _LOGGER.exception("Failed to reactivate the previous add-on version.")


__all__ = [
    "AddonDependencyReviewRequired",
    "AddonInstallOutcome",
    "AddonInstallationCoordinator",
]
