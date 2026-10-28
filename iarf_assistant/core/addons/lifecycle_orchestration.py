"""Add-on activation orchestration and atomic runtime publication."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from carlina.core.managed_target_recovery import ManagedTargetRecoveryContext

from ...extensions import db
from ...models import Addon
from ..addon_base import AddonContext
from ..frontend_builds import STATUS_FAILED, STATUS_MISCONFIGURED
from .contracts import AddonSourceKind
from .lifecycle import (
    ACTIVATED as LIFECYCLE_ACTIVATED,
)
from .lifecycle import (
    MIGRATED as LIFECYCLE_MIGRATED,
)
from .lifecycle import (
    PREPARED as LIFECYCLE_PREPARED,
)
from .lifecycle import (
    VALIDATED as LIFECYCLE_VALIDATED,
)
from .schema_ownership import addon_has_migrations
from .startup_loading import builtin_migration_reconciliation_state

logger = logging.getLogger(__name__)

ProgressCallback = Callable[
    [str, float, str, dict[str, Any] | None],
    None,
]
CancellationCheck = Callable[[], None]


class AddonLifecycleHost(Protocol):
    """Manager state required by activation orchestration."""

    app: Any
    manifests: dict[str, dict[str, Any]]
    loaded_addons: dict[str, Any]
    _addon_packages: dict[str, Any]
    _builtin_reconciliation_required: dict[str, dict[str, str]]
    _dispatch_enabled_addons: set[str]
    lifecycle: Any

    @property
    def container_services_enabled(self) -> bool: ...

    def _lifecycle_mutation_lock(self) -> threading.RLock: ...

    def _require_lifecycle_owner(self, operation: str) -> None: ...

    def _set_addon_dispatch_enabled(self, addon_id: str, enabled: bool) -> bool: ...

    def _addon_source_kind(self, addon_id: str) -> AddonSourceKind: ...

    def _validate_builtin_version_transition(
        self,
        addon_id: str,
        stored_version: str,
        package_version: str,
    ) -> None: ...

    def install_dependencies(
        self,
        addon_id: str,
        *,
        prepare_docker: bool = True,
    ) -> None: ...

    def ensure_frontend_assets(self, addon_id: str) -> dict[str, Any]: ...

    def prepare_container_images(
        self,
        addon_id: str,
        *,
        manifest: dict[str, Any] | None = None,
        force_refresh: bool = False,
    ) -> None: ...

    def get_addon_directory(self, addon_id: str) -> Path: ...

    def _validate_addon_migration_contract(self, addon_id: str) -> None: ...

    def _current_addon_db_revision_from_contract(
        self,
        contract: Any,
    ) -> str | None: ...

    def upgrade_addon_db(
        self,
        addon_id: str,
        revision: str = "head",
        *,
        managed_recovery: ManagedTargetRecoveryContext | None = None,
    ) -> None: ...

    def _load_addon(self, addon_id: str, context: AddonContext) -> None: ...

    def get_container_url(self, addon_id: str) -> str: ...

    def _get_effective_stack_lifecycle(
        self,
        docker_config: dict[str, Any],
        services: dict[str, dict[str, Any]] | None = None,
    ) -> str: ...

    def _stop_and_remove_container(self, addon_id: str) -> None: ...

    def _unload_addon_runtime(self, addon_id: str) -> None: ...

    def rebuild_registries(self) -> None: ...


if TYPE_CHECKING:

    class _AddonLifecycleMixinBase(AddonLifecycleHost):
        pass

else:

    class _AddonLifecycleMixinBase:
        pass


@dataclass(frozen=True, slots=True)
class _ActivationPlan:
    addon_id: str
    record: Addon
    manifest: dict[str, Any]
    docker_config: dict[str, Any]
    package_version: str
    upgrade_from: str | None
    is_builtin: bool
    migration_reconciliation_required: bool
    reviewed_dependency_notices: list[str]


def _ignore_progress(
    _stage: str,
    _progress: float,
    _message: str,
    _metadata: dict[str, Any] | None = None,
) -> None:
    """Default progress sink used by synchronous lifecycle callers."""


def _ignore_cancellation() -> None:
    """Default cancellation boundary used by synchronous lifecycle callers."""


class AddonLifecycleOrchestrationMixin(_AddonLifecycleMixinBase):
    """Prepare and atomically expose validated add-on runtimes."""

    @staticmethod
    def _normalize_dependency_review(
        dependency_review: list[str] | None,
    ) -> list[str]:
        return sorted(
            {
                notice_id.strip()
                for notice_id in (dependency_review or [])[:50]
                if isinstance(notice_id, str)
                and notice_id.strip()
                and len(notice_id) <= 128
            }
        )

    def _activation_plan(
        self,
        addon_id: str,
        *,
        upgrade_from: str | None,
        dependency_review: list[str] | None,
    ) -> _ActivationPlan | None:
        reviewed_notices = self._normalize_dependency_review(dependency_review)
        addon_record = db.session.get(Addon, addon_id)
        if not addon_record:
            raise FileNotFoundError(f"Addon '{addon_id}' not found in database.")

        if addon_record.is_active and addon_id in self.loaded_addons:
            self._set_addon_dispatch_enabled(addon_id, True)
            logger.warning("Addon '%s' is already active.", addon_id)
            return None
        if addon_record.is_active:
            logger.warning(
                "Add-on '%s' is persisted as active but has no runtime; "
                "resuming activation.",
                addon_id,
            )
            self.lifecycle.transition(
                addon_record,
                LIFECYCLE_VALIDATED,
                event="activation_runtime_recovery",
                force=True,
            )

        if addon_id not in self.manifests:
            raise FileNotFoundError(
                f"Validated code for add-on '{addon_id}' is not available."
            )
        manifest = self.manifests[addon_id]["manifest"]
        package_version = str(manifest.get("version") or "0.0.0")
        is_builtin = self._addon_source_kind(addon_id) == AddonSourceKind.BUILT_IN
        effective_upgrade_from = self._builtin_upgrade_origin(
            addon_id,
            addon_record,
            package_version,
            upgrade_from,
            is_builtin=is_builtin,
        )
        migration_reconciliation_required = False
        if is_builtin:
            package = self._addon_packages.get(addon_id)
            if package is not None:
                *_, migration_reconciliation_required = (
                    builtin_migration_reconciliation_state(
                        self,
                        addon_record,
                        package,
                    )
                )
        return _ActivationPlan(
            addon_id=addon_id,
            record=addon_record,
            manifest=manifest,
            docker_config=manifest.get("docker") or {},
            package_version=package_version,
            upgrade_from=effective_upgrade_from,
            is_builtin=is_builtin,
            migration_reconciliation_required=migration_reconciliation_required,
            reviewed_dependency_notices=reviewed_notices,
        )

    def _builtin_upgrade_origin(
        self,
        addon_id: str,
        addon_record: Addon,
        package_version: str,
        upgrade_from: str | None,
        *,
        is_builtin: bool,
    ) -> str | None:
        stored_version = str(addon_record.version)
        if not is_builtin or stored_version == package_version:
            return upgrade_from
        self._validate_builtin_version_transition(
            addon_id,
            stored_version,
            package_version,
        )
        return stored_version if upgrade_from is None else upgrade_from

    def _validate_activation_state(
        self,
        plan: _ActivationPlan,
        report_progress: ProgressCallback,
        check_cancel: CancellationCheck,
    ) -> None:
        check_cancel()
        report_progress(
            "activation_preflight",
            32,
            "Checking the package lifecycle state.",
            None,
        )
        stale_migrated_package = plan.record.lifecycle_state == LIFECYCLE_MIGRATED and (
            plan.upgrade_from is not None or plan.migration_reconciliation_required
        )
        if (
            plan.record.lifecycle_state
            in {
                LIFECYCLE_VALIDATED,
                LIFECYCLE_PREPARED,
                LIFECYCLE_MIGRATED,
            }
            and not stale_migrated_package
        ):
            return
        self.lifecycle.transition(
            plan.record,
            LIFECYCLE_VALIDATED,
            event="activation_validated",
            force=True,
        )

    def _prepare_activation_dependencies(
        self,
        plan: _ActivationPlan,
        report_progress: ProgressCallback,
        check_cancel: CancellationCheck,
    ) -> None:
        if plan.record.lifecycle_state == LIFECYCLE_MIGRATED:
            return

        # Dependency setup and builds deliberately run outside a database write
        # transaction. Each completed boundary is journaled before exposure.
        check_cancel()
        report_progress(
            "dependency_environment",
            38,
            "Preparing locked Python dependency environments.",
            None,
        )
        self.install_dependencies(plan.addon_id, prepare_docker=False)
        report_progress(
            "frontend_assets",
            54,
            "Checking prebuilt frontend assets.",
            None,
        )
        frontend_status = self.ensure_frontend_assets(plan.addon_id)
        self._require_frontend_assets(frontend_status)
        self._prepare_activation_image(plan, report_progress, check_cancel)
        self.lifecycle.transition(
            plan.record,
            LIFECYCLE_PREPARED,
            event="activation_prepared",
            metadata={"frontend_status": frontend_status.get("status")},
        )
        self._migrate_activation_database(plan, report_progress, check_cancel)

    @staticmethod
    def _require_frontend_assets(frontend_status: dict[str, Any]) -> None:
        if frontend_status.get("status") not in {
            STATUS_FAILED,
            STATUS_MISCONFIGURED,
        }:
            return
        reasons = frontend_status.get("reasons") or ["Frontend preparation failed."]
        raise RuntimeError("; ".join(reasons))

    def _prepare_activation_image(
        self,
        plan: _ActivationPlan,
        report_progress: ProgressCallback,
        check_cancel: CancellationCheck,
    ) -> None:
        if not plan.docker_config:
            return
        if not self.container_services_enabled:
            if self._get_effective_stack_lifecycle(plan.docker_config) == "always_on":
                raise RuntimeError("This application does not include container services.")
            return
        check_cancel()
        report_progress(
            "container_image",
            62,
            "Preparing declared container images.",
            {"readiness": {"image": "preparing"}},
        )
        self.prepare_container_images(
            plan.addon_id,
            manifest=plan.manifest,
            force_refresh=False,
        )
        report_progress(
            "container_image",
            72,
            "Declared container images are ready.",
            {"readiness": {"image": "ready"}},
        )

    def _migrate_activation_database(
        self,
        plan: _ActivationPlan,
        report_progress: ProgressCallback,
        check_cancel: CancellationCheck,
    ) -> None:
        addon_dir = self.get_addon_directory(plan.addon_id)
        self._validate_addon_migration_contract(plan.addon_id)
        if not addon_has_migrations(addon_dir, plan.addon_id):
            return
        check_cancel()
        report_progress(
            "database_migration",
            75,
            "Applying the add-on database migration.",
            None,
        )
        self.upgrade_addon_db(plan.addon_id)
        self.lifecycle.transition(
            plan.record,
            LIFECYCLE_MIGRATED,
            event="activation_migrated",
        )

    def _load_activation_runtime(
        self,
        plan: _ActivationPlan,
        report_progress: ProgressCallback,
        check_cancel: CancellationCheck,
    ) -> Any:
        check_cancel()
        report_progress(
            "runtime_load",
            82,
            "Loading the validated add-on runtime.",
            None,
        )
        self._load_addon(plan.addon_id, AddonContext(self.app, self))
        instance = self.loaded_addons.get(plan.addon_id)
        if not instance:
            raise RuntimeError(
                f"Failed to load add-on '{plan.addon_id}' during activation."
            )
        return instance

    @staticmethod
    def _hook_progress(report_progress: ProgressCallback, *, upgrade: bool) -> None:
        report_progress(
            "lifecycle_hook",
            86,
            (
                "Running the package upgrade hook."
                if upgrade
                else "Running the package activation hook."
            ),
            None,
        )

    def _run_activation_hook(
        self,
        plan: _ActivationPlan,
        instance: Any,
        report_progress: ProgressCallback,
        check_cancel: CancellationCheck,
    ) -> None:
        if plan.upgrade_from is not None and hasattr(instance, "on_upgrade"):
            check_cancel()
            self._hook_progress(report_progress, upgrade=True)
            logger.info(
                "Running on_upgrade hook for addon '%s' from version %s...",
                plan.addon_id,
                plan.upgrade_from,
            )
            instance.on_upgrade(plan.upgrade_from)
            return
        if not hasattr(instance, "on_activate"):
            return
        check_cancel()
        self._hook_progress(report_progress, upgrade=False)
        logger.info("Running on_activate hook for addon '%s'...", plan.addon_id)
        instance.on_activate()

    @staticmethod
    def _readiness_metadata(
        *,
        image: str,
        service: str,
        model: str,
        first_job_warmup: str,
    ) -> dict[str, dict[str, str]]:
        return {
            "readiness": {
                "image": image,
                "service": service,
                "model": model,
                "first_job_warmup": first_job_warmup,
            }
        }

    @staticmethod
    def _reconcile_builtin_version(plan: _ActivationPlan) -> None:
        if plan.is_builtin and str(plan.record.version) != plan.package_version:
            plan.record.version = plan.package_version

    def _start_always_on_service(
        self,
        addon_id: str,
        report_progress: ProgressCallback,
    ) -> tuple[str, str]:
        report_progress(
            "service_readiness",
            90,
            "Starting the always-on service and waiting for health readiness.",
            self._readiness_metadata(
                image="ready",
                service="starting",
                model="checking",
                first_job_warmup="pending",
            ),
        )
        try:
            self.get_container_url(addon_id)
        except Exception:
            logger.exception(
                "Failed to start always-on Docker microservice for addon '%s' "
                "during activation. It will retry on first use.",
                addon_id,
            )
            report_progress(
                "service_readiness",
                94,
                "The image is ready, but the always-on service will retry "
                "on first use.",
                self._readiness_metadata(
                    image="ready",
                    service="degraded_retry_on_use",
                    model="unknown",
                    first_job_warmup="pending",
                ),
            )
            return "degraded_retry_on_use", "unknown"

        report_progress(
            "service_readiness",
            94,
            "The always-on service is healthy.",
            self._readiness_metadata(
                image="ready",
                service="ready",
                model="service_reported_ready",
                first_job_warmup="pending",
            ),
        )
        return "ready", "service_reported_ready"

    def _activation_service_readiness(
        self,
        addon_id: str,
        instance: Any,
        report_progress: ProgressCallback,
        check_cancel: CancellationCheck,
    ) -> tuple[dict[str, Any], str, str]:
        docker_config = instance.manifest.get("docker") or {}
        if not docker_config:
            return docker_config, "not_required", "not_required"
        if not self.container_services_enabled:
            if self._get_effective_stack_lifecycle(docker_config) == "always_on":
                raise RuntimeError("This application does not include container services.")
            return {}, "not_required", "not_required"
        if self._get_effective_stack_lifecycle(docker_config) != "always_on":
            return (
                docker_config,
                "deferred_until_first_use",
                "deferred_until_first_use",
            )

        logger.info(
            "Starting always-on Docker microservice for addon '%s' during "
            "activation...",
            addon_id,
        )
        # Cancellation is a lifecycle decision, not a degraded service state.
        # Keep this check outside the retry-on-use exception boundary.
        check_cancel()
        service_state, model_state = self._start_always_on_service(
            addon_id,
            report_progress,
        )
        return docker_config, service_state, model_state

    def _report_registry_commit(
        self,
        docker_config: dict[str, Any],
        service_state: str,
        model_state: str,
        report_progress: ProgressCallback,
        check_cancel: CancellationCheck,
    ) -> None:
        check_cancel()
        required = bool(docker_config)
        report_progress(
            "registry_commit",
            96,
            "Committing activation to the live capability registry.",
            self._readiness_metadata(
                image="ready" if required else "not_required",
                service=service_state,
                model=model_state,
                first_job_warmup="pending" if required else "not_required",
            ),
        )

    def _complete_activation(self, plan: _ActivationPlan, instance: Any) -> None:
        self.lifecycle.transition(
            plan.record,
            LIFECYCLE_ACTIVATED,
            event="activation_completed",
            metadata={
                "manifest_sha256": getattr(plan.record, "manifest_sha256", None),
                "contract_sha256": getattr(plan.record, "contract_sha256", None),
                "reviewed_dependency_notices": plan.reviewed_dependency_notices,
                "upgraded_from": plan.upgrade_from,
                "version": plan.package_version,
            },
        )
        if not self._set_addon_dispatch_enabled(plan.addon_id, True):
            raise RuntimeError(
                f"Cannot expose add-on '{plan.addon_id}': its runtime or "
                "validated manifest disappeared before publication."
            )
        logger.info("Addon '%s' activated successfully.", plan.addon_id)
        if hasattr(instance, "on_register_jobs"):
            instance.on_register_jobs(getattr(self.app, "scheduler", None))
        self._builtin_reconciliation_required.pop(plan.addon_id, None)

    def _fail_activation(self, plan: _ActivationPlan, error: Exception) -> None:
        db.session.rollback()
        self._set_addon_dispatch_enabled(plan.addon_id, False)
        try:
            self._stop_and_remove_container(plan.addon_id)
        except Exception:
            logger.exception(
                "Failed to clean up container resources after activation failure "
                "for '%s'.",
                plan.addon_id,
            )
        self.lifecycle.fail(
            plan.record,
            event="activation_failed",
            error=error,
        )
        self._unload_addon_runtime(plan.addon_id)
        logger.error(
            "Error during activation of addon '%s': %s",
            plan.addon_id,
            error,
            exc_info=(type(error), error, error.__traceback__),
        )

    def activate_addon(
        self,
        addon_id: str,
        *,
        upgrade_from: str | None = None,
        dependency_review: list[str] | None = None,
        operation_progress: ProgressCallback | None = None,
        cancellation_check: CancellationCheck | None = None,
    ) -> None:
        """Serialize the complete activation transaction in the owner process."""

        self._require_lifecycle_owner("activate add-ons")
        with self._lifecycle_mutation_lock():
            self._activate_addon_serialized(
                addon_id,
                upgrade_from=upgrade_from,
                dependency_review=dependency_review,
                operation_progress=operation_progress,
                cancellation_check=cancellation_check,
            )

    def _activate_addon_serialized(
        self,
        addon_id: str,
        *,
        upgrade_from: str | None = None,
        dependency_review: list[str] | None = None,
        operation_progress: ProgressCallback | None = None,
        cancellation_check: CancellationCheck | None = None,
    ) -> None:
        """Prepare, migrate, and atomically expose one validated add-on."""

        report_progress = operation_progress or _ignore_progress
        check_cancel = cancellation_check or _ignore_cancellation
        plan = self._activation_plan(
            addon_id,
            upgrade_from=upgrade_from,
            dependency_review=dependency_review,
        )
        if plan is None:
            return

        try:
            self._validate_activation_state(plan, report_progress, check_cancel)
            self._prepare_activation_dependencies(plan, report_progress, check_cancel)
            instance = self._load_activation_runtime(
                plan,
                report_progress,
                check_cancel,
            )
            self._run_activation_hook(
                plan,
                instance,
                report_progress,
                check_cancel,
            )
            self._reconcile_builtin_version(plan)
            docker_config, service_state, model_state = (
                self._activation_service_readiness(
                    addon_id,
                    instance,
                    report_progress,
                    check_cancel,
                )
            )
            self._report_registry_commit(
                docker_config,
                service_state,
                model_state,
                report_progress,
                check_cancel,
            )
            self._complete_activation(plan, instance)
        except Exception as exc:
            self._fail_activation(plan, exc)
            raise
        finally:
            self.rebuild_registries()


__all__ = [
    "AddonLifecycleHost",
    "AddonLifecycleOrchestrationMixin",
    "CancellationCheck",
    "ProgressCallback",
]
