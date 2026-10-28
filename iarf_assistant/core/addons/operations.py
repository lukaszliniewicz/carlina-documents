"""Durable, observable add-on installation and activation operations."""

from __future__ import annotations

import logging
import os
import shutil
import threading
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any

from packaging.version import InvalidVersion, Version
from werkzeug.local import LocalProxy

from iarf_assistant.core.logging_observability import redact_text
from iarf_assistant.core.runtime_state import upsert_runtime_job
from carlina.core.utc import utc_iso, utc_now, utc_now_db
from iarf_assistant.extensions import db
from iarf_assistant.models import Addon, RuntimeJob

from .admission import evaluate_addon_admission
from .catalog import AddonCatalogError
from .catalog_service import AddonCatalogService, AddonCatalogSourceError
from .installation import (
    AddonDependencyReviewRequired,
    AddonInstallationCoordinator,
)
from .installer import AddonPackageInstaller, StagedAddonPackage

ADDON_OPERATION_SOURCE_TYPE = "addon_lifecycle"
ADDON_OPERATION_VERSION = 1
ADDON_OPERATION_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
ADDON_OPERATION_ACTIVE_STATUSES = frozenset(
    {"queued", "running", "waiting_for_approval", "paused"}
)
ADDON_OPERATION_INSTALL_UPLOAD = "install_upload"
ADDON_OPERATION_INSTALL_CATALOG = "install_catalog"
ADDON_OPERATION_ACTIVATE = "activate"
ADDON_OPERATION_TYPES = frozenset(
    {
        ADDON_OPERATION_INSTALL_UPLOAD,
        ADDON_OPERATION_INSTALL_CATALOG,
        ADDON_OPERATION_ACTIVATE,
    }
)
DEFAULT_UPLOAD_RETRY_RETENTION_DAYS = 7
MAX_UPLOAD_RETRY_RETENTION_DAYS = 90
INPUT_CLEANUP_INTERVAL_SECONDS = 60 * 60
PROGRESS_PERSIST_INTERVAL_SECONDS = 0.5

logger = logging.getLogger(__name__)


class AddonOperationError(RuntimeError):
    """A lifecycle operation could not be accepted or completed."""


class AddonOperationConflict(AddonOperationError):
    def __init__(self, addon_id: str, operation_id: str):
        super().__init__(
            f"Add-on '{addon_id}' already has lifecycle operation "
            f"'{operation_id}' in progress."
        )
        self.addon_id = addon_id
        self.operation_id = operation_id


class AddonOperationAdmissionError(AddonOperationError):
    def __init__(self, report: dict[str, Any]):
        reasons = report.get("blocking_reasons") or ["Resource admission failed."]
        super().__init__(" ".join(str(reason) for reason in reasons))
        self.report = report


class AddonOperationCancelled(AddonOperationError):
    """Cooperative cancellation observed at a safe lifecycle boundary."""


def configured_addon_catalog_service(app) -> AddonCatalogService | None:
    source = str(app.config.get("ADDON_CATALOG_URL") or "").strip()
    if not source:
        return None
    trust_store = app.config.get("ADDON_CATALOG_TRUST_STORE")
    if not trust_store:
        raise AddonCatalogSourceError("Add-on catalog trust store is not configured.")
    return AddonCatalogService(
        source=source,
        trust_store=trust_store,
        channel=str(app.config.get("ADDON_CATALOG_CHANNEL") or "stable"),
        allow_unsigned_local=bool(
            app.config.get("ADDON_CATALOG_ALLOW_UNSIGNED_LOCAL", False)
        ),
    )


def _normalized_notice_ids(values: Iterable[str] | None) -> list[str]:
    return sorted(
        {
            str(value).strip()
            for value in (values or ())
            if str(value).strip() and len(str(value).strip()) <= 128
        }
    )


def _operation_metadata(record: RuntimeJob) -> dict[str, Any]:
    return dict(record.metadata_json) if isinstance(record.metadata_json, dict) else {}


def _parse_utc_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _operation_retryable(record: RuntimeJob) -> bool:
    if record.status not in {"failed", "cancelled"}:
        return False
    metadata = _operation_metadata(record)
    operation = str(metadata.get("operation") or record.task_name or "")
    if operation != ADDON_OPERATION_INSTALL_UPLOAD:
        return True
    if metadata.get("input_retained") is False:
        return False
    retry_expires_at = _parse_utc_iso(metadata.get("retry_expires_at"))
    return retry_expires_at is None or retry_expires_at > utc_now()


def serialize_addon_operation(record: RuntimeJob) -> dict[str, Any]:
    metadata = _operation_metadata(record)
    operation = str(metadata.get("operation") or record.task_name or "")
    return {
        "id": record.id,
        "job_id": record.id,
        "addon_id": record.source_id or record.source_addon_id,
        "operation": operation,
        "status": record.status,
        "stage": metadata.get("stage") or "queued",
        "progress": record.progress,
        "message": record.message,
        "error": record.error,
        "bytes": {
            "current": metadata.get("bytes_current"),
            "total": metadata.get("bytes_total"),
        },
        "last_activity_at": metadata.get("last_activity_at"),
        "queued_at": record.queued_at.isoformat() if record.queued_at else None,
        "started_at": record.started_at.isoformat() if record.started_at else None,
        "completed_at": (
            record.completed_at.isoformat() if record.completed_at else None
        ),
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
        "attempt": int(metadata.get("attempt") or 1),
        "recovered_count": int(metadata.get("recovered_count") or 0),
        "cancel_requested": metadata.get("cancel_requested") is True,
        "cancellable": record.status in {"queued", "running"}
        and metadata.get("cancel_requested") is not True,
        "retryable": _operation_retryable(record),
        "retry_expires_at": metadata.get("retry_expires_at"),
        "input_retained": metadata.get("input_retained"),
        "admission": metadata.get("admission"),
        "readiness": metadata.get("readiness") or {},
        "result": record.result_payload,
        "log_source_ids": list(record.log_source_ids or []),
        "status_url": f"/api/addons/operations/{record.id}",
        "cancel_url": f"/api/addons/operations/{record.id}/cancel",
        "retry_url": f"/api/addons/operations/{record.id}/retry",
    }


class AddonOperationService:
    """Persist and execute one serial stream of add-on lifecycle work."""

    def __init__(self, app) -> None:
        self.app = app._get_current_object() if isinstance(app, LocalProxy) else app
        self.paths = self.app.extensions.get("carlina_paths")
        if self.paths is None:
            raise RuntimeError("Carlina storage paths are not initialized.")
        self._poll_lock = threading.Lock()
        self._create_lock = threading.Lock()
        self._recovery_complete = False
        self._last_input_cleanup_at = 0.0

    def _upload_retry_retention_days(self) -> int:
        raw_value = self.app.config.get(
            "ADDON_OPERATION_UPLOAD_RETRY_RETENTION_DAYS",
            DEFAULT_UPLOAD_RETRY_RETENTION_DAYS,
        )
        try:
            value = int(raw_value)
        except TypeError, ValueError:
            value = DEFAULT_UPLOAD_RETRY_RETENTION_DAYS
        return max(1, min(value, MAX_UPLOAD_RETRY_RETENTION_DAYS))

    def _upload_retry_metadata(self) -> dict[str, Any]:
        expires_at = utc_now() + timedelta(days=self._upload_retry_retention_days())
        return {
            "input_retained": True,
            "retry_expires_at": utc_iso(expires_at),
        }

    def _manager(self):
        manager = getattr(self.app, "addon_manager", None)
        if manager is None:
            raise RuntimeError("Addon manager is not initialized.")
        return manager

    def _register_logs(self, operation_id: str, addon_id: str) -> list[str]:
        source_ids = [
            "addons.install",
            f"addon.{addon_id}.install",
            f"job.{operation_id}",
        ]
        registry = getattr(self.app, "log_registry", None)
        if registry is not None:
            registry.register_addon_sources(addon_id)
            registry.register_job_source(
                operation_id,
                name=f"{addon_id} lifecycle operation",
                addon_id=addon_id,
            )
        return source_ids

    def _append_log(self, record: RuntimeJob, message: str) -> None:
        registry = getattr(self.app, "log_registry", None)
        if registry is None:
            return
        safe_message = redact_text(str(message))
        line = f"operation_id={record.id} addon_id={record.source_id} {safe_message}"
        for source_id in record.log_source_ids or ():
            try:
                registry.append_to_source(source_id, line)
            except Exception:
                logger.warning(
                    "Could not append add-on operation log to %s.",
                    source_id,
                    exc_info=True,
                )

    def _active_operation(self, addon_id: str) -> RuntimeJob | None:
        return (
            RuntimeJob.query.filter(
                RuntimeJob.source_type == ADDON_OPERATION_SOURCE_TYPE,
                RuntimeJob.source_id == addon_id,
                RuntimeJob.status.in_(ADDON_OPERATION_ACTIVE_STATUSES),
            )
            .order_by(RuntimeJob.queued_at.asc())
            .first()
        )

    def _create(
        self,
        *,
        operation_id: str,
        addon_id: str,
        operation: str,
        owner_user_id: int,
        input_payload: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> RuntimeJob:
        if operation not in ADDON_OPERATION_TYPES:
            raise ValueError(f"Unsupported add-on operation: {operation}")
        with self._create_lock:
            active = self._active_operation(addon_id)
            if active is not None:
                raise AddonOperationConflict(addon_id, active.id)
            log_source_ids = self._register_logs(operation_id, addon_id)
            record = upsert_runtime_job(
                job_id=operation_id,
                source_type=ADDON_OPERATION_SOURCE_TYPE,
                source_id=addon_id,
                source_addon_id=addon_id,
                owner_user_id=int(owner_user_id),
                task_name=operation,
                status="queued",
                progress=0,
                message="Waiting for the lifecycle worker.",
                input_payload=input_payload,
                metadata={
                    "operation_version": ADDON_OPERATION_VERSION,
                    "operation": operation,
                    "stage": "queued",
                    "attempt": 1,
                    "last_activity_at": utc_iso(),
                    **(metadata or {}),
                },
                log_source_ids=log_source_ids,
            )
        self._append_log(record, f"Queued {operation} operation.")
        return record

    def enqueue_upload(
        self,
        staged: StagedAddonPackage,
        *,
        owner_user_id: int,
        operator_approved: bool,
        reviewed_notice_ids: Iterable[str] = (),
        admission: dict[str, Any] | None = None,
    ) -> RuntimeJob:
        operation_id = f"addon-op-{uuid.uuid4().hex}"
        operation_root = (
            Path(self.paths.staging_dir) / "addon-operations" / operation_id
        ).resolve()
        allowed_root = (Path(self.paths.staging_dir) / "addon-operations").resolve()
        operation_root.relative_to(allowed_root)
        operation_root.mkdir(parents=True, exist_ok=False)
        archive_path = operation_root / "package.zip"
        try:
            os.replace(staged.archive_path, archive_path)
            AddonPackageInstaller.cleanup_stage(staged)
            return self._create(
                operation_id=operation_id,
                addon_id=staged.contract.addon_id,
                operation=ADDON_OPERATION_INSTALL_UPLOAD,
                owner_user_id=owner_user_id,
                input_payload={
                    "archive_path": str(archive_path),
                    "original_filename": staged.original_filename,
                    "archive_sha256": staged.archive_sha256,
                    "archive_size": int(staged.provenance.get("archive_size") or 0),
                    "manifest_sha256": staged.contract.manifest_sha256,
                    "contract_sha256": staged.contract.contract_sha256,
                    "operator_approved": bool(operator_approved),
                    "dependency_review": _normalized_notice_ids(reviewed_notice_ids),
                },
                metadata={
                    **self._upload_retry_metadata(),
                    **({"admission": admission} if admission else {}),
                },
            )
        except Exception:
            shutil.rmtree(operation_root, ignore_errors=True)
            raise

    def enqueue_catalog(
        self,
        *,
        addon_id: str,
        version: str,
        catalog_sha256: str,
        archive_sha256: str,
        archive_size: int,
        owner_user_id: int,
        operator_approved: bool,
        reviewed_notice_ids: Iterable[str] = (),
        admission: dict[str, Any] | None = None,
    ) -> RuntimeJob:
        return self._create(
            operation_id=f"addon-op-{uuid.uuid4().hex}",
            addon_id=addon_id,
            operation=ADDON_OPERATION_INSTALL_CATALOG,
            owner_user_id=owner_user_id,
            input_payload={
                "version": version,
                "catalog_sha256": catalog_sha256,
                "archive_sha256": archive_sha256,
                "archive_size": int(archive_size),
                "operator_approved": bool(operator_approved),
                "dependency_review": _normalized_notice_ids(reviewed_notice_ids),
            },
            metadata={"admission": admission} if admission else None,
        )

    def enqueue_activation(
        self,
        *,
        addon_id: str,
        owner_user_id: int,
        reviewed_notice_ids: Iterable[str] = (),
        admission: dict[str, Any] | None = None,
    ) -> RuntimeJob:
        return self._create(
            operation_id=f"addon-op-{uuid.uuid4().hex}",
            addon_id=addon_id,
            operation=ADDON_OPERATION_ACTIVATE,
            owner_user_id=owner_user_id,
            input_payload={
                "dependency_review": _normalized_notice_ids(reviewed_notice_ids)
            },
            metadata={"admission": admission} if admission else None,
        )

    def list(self, *, limit: int = 50, addon_id: str | None = None) -> list[RuntimeJob]:
        query = RuntimeJob.query.filter_by(source_type=ADDON_OPERATION_SOURCE_TYPE)
        if addon_id:
            query = query.filter(RuntimeJob.source_id == addon_id)
        records = (
            query.order_by(RuntimeJob.updated_at.desc(), RuntimeJob.id.desc())
            .limit(max(1, min(int(limit), 200)))
            .all()
        )
        for record in records:
            self._register_logs(
                record.id, str(record.source_id or record.source_addon_id)
            )
        return records

    def get(self, operation_id: str) -> RuntimeJob | None:
        record = db.session.get(RuntimeJob, operation_id)
        if record is None or record.source_type != ADDON_OPERATION_SOURCE_TYPE:
            return None
        self._register_logs(
            record.id,
            str(record.source_id or record.source_addon_id),
        )
        return record

    def request_cancel(self, operation_id: str, *, requested_by: int) -> RuntimeJob:
        record = self.get(operation_id)
        if record is None:
            raise KeyError(operation_id)
        if record.status in ADDON_OPERATION_TERMINAL_STATUSES:
            return record
        metadata = _operation_metadata(record)
        metadata.update(
            {
                "cancel_requested": True,
                "cancel_requested_at": utc_iso(),
                "cancel_requested_by": int(requested_by),
                "last_activity_at": utc_iso(),
            }
        )
        record.metadata_json = metadata
        record.message = (
            "Cancelled before execution."
            if record.status == "queued"
            else "Cancellation requested; waiting for a safe lifecycle boundary."
        )
        if record.status == "queued":
            record.status = "cancelled"
            record.completed_at = utc_now_db()
        record.updated_at = utc_now_db()
        db.session.commit()
        self._append_log(record, record.message)
        return record

    def retry(self, operation_id: str, *, requested_by: int) -> RuntimeJob:
        original = self.get(operation_id)
        if original is None:
            raise KeyError(operation_id)
        if original.status not in {"failed", "cancelled"}:
            raise AddonOperationError(
                "Only failed or cancelled operations can be retried."
            )
        if not _operation_retryable(original):
            raise AddonOperationError(
                "The retained upload has expired; choose the ZIP again."
            )
        payload = dict(original.input_payload or {})
        operation = str(
            _operation_metadata(original).get("operation") or original.task_name
        )
        if operation == ADDON_OPERATION_INSTALL_UPLOAD:
            archive_path = self._validated_upload_archive_path(
                payload.get("archive_path")
            )
            if not archive_path.is_file():
                raise AddonOperationError(
                    "The retained upload is no longer available; choose the ZIP again."
                )
        attempt = int(_operation_metadata(original).get("attempt") or 1) + 1
        original_metadata = _operation_metadata(original)
        retry_metadata = {
            "attempt": attempt,
            "retry_of": original.id,
            "admission": original_metadata.get("admission"),
        }
        if operation == ADDON_OPERATION_INSTALL_UPLOAD:
            retry_metadata.update(
                {
                    "input_retained": original_metadata.get("input_retained", True),
                    "retry_expires_at": original_metadata.get("retry_expires_at"),
                }
            )
        return self._create(
            operation_id=f"addon-op-{uuid.uuid4().hex}",
            addon_id=str(original.source_id or original.source_addon_id),
            operation=operation,
            owner_user_id=requested_by,
            input_payload=payload,
            metadata=retry_metadata,
        )

    def cleanup_expired_inputs(self, *, force: bool = False) -> int:
        """Delete only expired upload archives retained for lifecycle retry."""

        now_monotonic = monotonic()
        if (
            not force
            and now_monotonic - self._last_input_cleanup_at
            < INPUT_CLEANUP_INTERVAL_SECONDS
        ):
            return 0
        self._last_input_cleanup_at = now_monotonic
        removed = 0
        changed = 0
        records = RuntimeJob.query.filter(
            RuntimeJob.source_type == ADDON_OPERATION_SOURCE_TYPE,
            RuntimeJob.task_name == ADDON_OPERATION_INSTALL_UPLOAD,
            RuntimeJob.status.in_(ADDON_OPERATION_TERMINAL_STATUSES),
        ).all()
        for record in records:
            metadata = _operation_metadata(record)
            if metadata.get("input_retained") is False:
                continue
            expires_at = _parse_utc_iso(metadata.get("retry_expires_at"))
            if expires_at is None or expires_at > utc_now():
                continue
            payload = dict(record.input_payload or {})
            try:
                archive_path = self._validated_upload_archive_path(
                    payload.get("archive_path")
                )
                operation_root = archive_path.parent
                if operation_root.exists():
                    shutil.rmtree(operation_root)
                    removed += 1
            except OSError, ValueError:
                logger.warning(
                    "Could not remove expired retained input for add-on operation %s.",
                    record.id,
                    exc_info=True,
                )
                continue
            metadata.update(
                {
                    "input_retained": False,
                    "retry_input_removed_at": utc_iso(),
                    "last_activity_at": utc_iso(),
                }
            )
            record.metadata_json = metadata
            record.updated_at = utc_now_db()
            self._append_log(record, "Expired retained upload input was removed.")
            changed += 1
        if changed:
            db.session.commit()
        return removed

    def recover_interrupted(self) -> int:
        if self._recovery_complete:
            return 0
        records = RuntimeJob.query.filter(
            RuntimeJob.source_type == ADDON_OPERATION_SOURCE_TYPE,
            RuntimeJob.status == "running",
        ).all()
        for record in records:
            metadata = _operation_metadata(record)
            metadata.update(
                {
                    "stage": "queued",
                    "recovered_count": int(metadata.get("recovered_count") or 0) + 1,
                    "recovered_at": utc_iso(),
                    "last_activity_at": utc_iso(),
                }
            )
            record.metadata_json = metadata
            record.status = "queued"
            record.message = "Recovered after an interrupted application process."
            record.error = None
            record.started_at = None
            record.completed_at = None
            record.updated_at = utc_now_db()
            self._append_log(record, record.message)
        if records:
            db.session.commit()
        self._recovery_complete = True
        return len(records)

    def poll_once(self) -> RuntimeJob | None:
        if not self._poll_lock.acquire(blocking=False):
            return None
        try:
            with self.app.app_context():
                self.recover_interrupted()
                self.cleanup_expired_inputs()
                record = (
                    RuntimeJob.query.filter(
                        RuntimeJob.source_type == ADDON_OPERATION_SOURCE_TYPE,
                        RuntimeJob.status == "queued",
                    )
                    .order_by(RuntimeJob.queued_at.asc(), RuntimeJob.id.asc())
                    .first()
                )
                if record is None:
                    return None
                claimed = RuntimeJob.query.filter_by(
                    id=record.id,
                    source_type=ADDON_OPERATION_SOURCE_TYPE,
                    status="queued",
                ).update(
                    {
                        RuntimeJob.status: "running",
                        RuntimeJob.started_at: utc_now_db(),
                        RuntimeJob.updated_at: utc_now_db(),
                    },
                    synchronize_session=False,
                )
                db.session.commit()
                if claimed != 1:
                    return None
                record = db.session.get(RuntimeJob, record.id)
                self._execute(record)
                return record
        finally:
            self._poll_lock.release()

    def _update(
        self,
        record: RuntimeJob,
        *,
        stage: str,
        progress: float,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        existing = _operation_metadata(record)
        prior_stage = existing.get("stage")
        existing.update(metadata or {})
        existing.update({"stage": stage, "last_activity_at": utc_iso()})
        record.status = "running"
        record.progress = max(0.0, min(float(progress), 100.0))
        record.message = redact_text(message)
        record.metadata_json = existing
        record.updated_at = utc_now_db()
        db.session.commit()
        if stage != prior_stage or message:
            self._append_log(
                record, f"stage={stage} progress={record.progress:g} {record.message}"
            )

    def _check_cancel(self, record: RuntimeJob) -> None:
        db.session.refresh(record)
        if _operation_metadata(record).get("cancel_requested") is True:
            raise AddonOperationCancelled(
                "The add-on operation was cancelled at a safe lifecycle boundary."
            )

    def _progress_callback(self, record: RuntimeJob):
        last_persisted_at = 0.0
        last_stage = ""
        last_progress = -1.0

        def callback(
            stage: str,
            progress: float,
            message: str,
            metadata: dict[str, Any] | None = None,
        ) -> None:
            nonlocal last_persisted_at, last_stage, last_progress
            self._check_cancel(record)
            now = monotonic()
            numeric_progress = max(0.0, min(float(progress), 100.0))
            if (
                stage == last_stage
                and numeric_progress < 100
                and numeric_progress - last_progress < 1
                and now - last_persisted_at < PROGRESS_PERSIST_INTERVAL_SECONDS
            ):
                return
            self._update(
                record,
                stage=stage,
                progress=progress,
                message=message,
                metadata=metadata,
            )
            last_persisted_at = now
            last_stage = stage
            last_progress = numeric_progress

        return callback

    def _execute(self, record: RuntimeJob) -> None:
        operation = str(
            _operation_metadata(record).get("operation") or record.task_name
        )
        self._append_log(record, f"Started {operation} operation.")
        try:
            self._check_cancel(record)
            if operation == ADDON_OPERATION_INSTALL_UPLOAD:
                result = self._execute_upload(record)
            elif operation == ADDON_OPERATION_INSTALL_CATALOG:
                result = self._execute_catalog(record)
            elif operation == ADDON_OPERATION_ACTIVATE:
                result = self._execute_activation(record)
            else:
                raise AddonOperationError(f"Unsupported operation: {operation}")
            metadata = _operation_metadata(record)
            metadata.update(
                {
                    "stage": "completed",
                    "last_activity_at": utc_iso(),
                    "cancel_requested": False,
                }
            )
            record.status = "completed"
            record.progress = 100
            record.message = str(result.get("message") or "Operation completed.")
            record.error = None
            record.result_payload = result
            record.metadata_json = metadata
            record.completed_at = utc_now_db()
            record.updated_at = utc_now_db()
            db.session.commit()
            self._append_log(record, record.message)
        except AddonOperationCancelled as exc:
            db.session.rollback()
            record = db.session.get(RuntimeJob, record.id)
            metadata = _operation_metadata(record)
            metadata.update({"stage": "cancelled", "last_activity_at": utc_iso()})
            record.status = "cancelled"
            record.message = str(exc)
            record.error = None
            record.metadata_json = metadata
            record.completed_at = utc_now_db()
            record.updated_at = utc_now_db()
            db.session.commit()
            self._append_log(record, record.message)
        except Exception as exc:
            db.session.rollback()
            record = db.session.get(RuntimeJob, record.id)
            message = redact_text(str(exc) or exc.__class__.__name__)
            metadata = _operation_metadata(record)
            metadata.update({"stage": "failed", "last_activity_at": utc_iso()})
            if isinstance(exc, AddonOperationAdmissionError):
                metadata["admission"] = exc.report
            if isinstance(exc, AddonDependencyReviewRequired):
                metadata["dependency_review"] = {
                    "missing_notice_ids": list(exc.missing_ids),
                    "dependency_notices": exc.notices,
                }
            record.status = "failed"
            record.message = "Add-on lifecycle operation failed."
            record.error = message
            record.metadata_json = metadata
            record.completed_at = utc_now_db()
            record.updated_at = utc_now_db()
            db.session.commit()
            self._append_log(record, f"Failed: {message}")
            logger.exception(
                "Add-on lifecycle operation %s failed.",
                record.id,
            )

    def _validated_upload_archive_path(self, value: Any) -> Path:
        root = (Path(self.paths.staging_dir) / "addon-operations").resolve()
        path = Path(str(value or "")).resolve()
        path.relative_to(root)
        if path.name != "package.zip" or path.parent.parent != root:
            raise ValueError(
                "Retained upload path does not match the operation layout."
            )
        return path

    def _transfer_progress_callback(
        self,
        record: RuntimeJob,
        *,
        stage: str,
        progress_start: float,
        progress_span: float,
        message: str,
    ):
        last_persisted_at = 0.0

        def callback(current: int, total: int | None) -> None:
            nonlocal last_persisted_at
            self._check_cancel(record)
            now = monotonic()
            completed = total is not None and current >= total
            if (
                not completed
                and now - last_persisted_at < PROGRESS_PERSIST_INTERVAL_SECONDS
            ):
                return
            ratio = current / total if total else 0
            self._update(
                record,
                stage=stage,
                progress=progress_start + min(progress_span, ratio * progress_span),
                message=message,
                metadata={"bytes_current": current, "bytes_total": total},
            )
            last_persisted_at = now

        return callback

    def _admit(
        self,
        record: RuntimeJob,
        *,
        contract: Any,
        archive_size: int | None,
    ) -> dict[str, Any]:
        report = evaluate_addon_admission(
            paths=self.paths,
            contract=contract,
            archive_size=archive_size,
            manager=self._manager(),
        )
        self._update(
            record,
            stage="admission",
            progress=8,
            message=(
                "Host resource checks passed."
                if report["allowed"]
                else "Host resource checks blocked the operation."
            ),
            metadata={"admission": report},
        )
        if not report["allowed"]:
            raise AddonOperationAdmissionError(report)
        return report

    def _coordinator(
        self,
        record: RuntimeJob,
        installer: AddonPackageInstaller,
    ) -> AddonInstallationCoordinator:
        def append_log(_addon_id: str, message: str) -> None:
            self._append_log(record, message)

        return AddonInstallationCoordinator(
            session=db.session,
            manager=self._manager(),
            installer=installer,
            actor_user_id=record.owner_user_id,
            log=append_log,
            progress=self._progress_callback(record),
            cancellation_check=lambda: self._check_cancel(record),
        )

    def _execute_upload(self, record: RuntimeJob) -> dict[str, Any]:
        payload = dict(record.input_payload or {})
        archive_path = self._validated_upload_archive_path(payload.get("archive_path"))
        if not archive_path.is_file():
            raise AddonOperationError("The retained upload package is missing.")
        installer = AddonPackageInstaller(self.paths)
        staged = None
        try:
            self._update(
                record,
                stage="package_validation",
                progress=2,
                message="Validating the retained package.",
                metadata={
                    "bytes_current": 0,
                    "bytes_total": int(payload.get("archive_size") or 0) or None,
                },
            )

            copy_progress = self._transfer_progress_callback(
                record,
                stage="package_validation",
                progress_start=2,
                progress_span=5,
                message="Copying and hashing the retained package.",
            )

            with archive_path.open("rb") as stream:
                staged = installer.stage(
                    stream,
                    original_filename=str(
                        payload.get("original_filename") or "addon.zip"
                    ),
                    operator_approved=payload.get("operator_approved") is True,
                    expected_archive_sha256=str(payload.get("archive_sha256") or ""),
                    expected_archive_size=int(payload.get("archive_size") or 0) or None,
                    expected_manifest_sha256=str(payload.get("manifest_sha256") or ""),
                    expected_contract_sha256=str(payload.get("contract_sha256") or ""),
                    progress=copy_progress,
                    cancellation_check=lambda: self._check_cancel(record),
                )
            self._admit(
                record,
                contract=staged.contract,
                archive_size=int(payload.get("archive_size") or 0) or None,
            )
            coordinator = self._coordinator(record, installer)
            outcome = coordinator.install(
                staged,
                reviewed_notice_ids=payload.get("dependency_review") or (),
            )
            if outcome.requires_restart:
                from iarf_assistant.core.restart_state import require_restart

                require_restart(
                    self.paths, reason=f"addon:{outcome.addon_id}:installation"
                )
            shutil.rmtree(archive_path.parent, ignore_errors=True)
            metadata = _operation_metadata(record)
            metadata.update(
                {
                    "input_retained": archive_path.exists(),
                    "retry_input_removed_at": (
                        None if archive_path.exists() else utc_iso()
                    ),
                }
            )
            record.metadata_json = metadata
            return outcome.to_dict()
        finally:
            if staged is not None:
                installer.cleanup_stage(staged)

    def _catalog_release(self, payload: dict[str, Any]):
        service = configured_addon_catalog_service(self.app)
        if service is None:
            raise AddonCatalogSourceError("No add-on catalog source is configured.")
        loaded = service.load()
        if loaded.source_sha256 != str(payload.get("catalog_sha256") or ""):
            raise AddonCatalogSourceError(
                "The signed catalog changed after review; refresh and review it again."
            )
        addon_id = str(payload.get("addon_id") or "")
        package = loaded.index.package(addon_id)
        release = package.select_release(
            requested_version=str(payload.get("version") or ""),
            stable=loaded.index.channel == "stable",
        )
        if release is None:
            raise AddonCatalogError("No compatible package release was found.")
        if release.archive.sha256 != str(payload.get("archive_sha256") or ""):
            raise AddonCatalogSourceError(
                "The signed package digest changed after review."
            )
        return service, loaded, release

    def _execute_catalog(self, record: RuntimeJob) -> dict[str, Any]:
        payload = {**dict(record.input_payload or {}), "addon_id": record.source_id}
        self._update(
            record,
            stage="catalog_verification",
            progress=2,
            message="Revalidating the signed catalog generation.",
        )
        service, loaded, release = self._catalog_release(payload)
        self._admit(
            record,
            contract=dict(release.contract),
            archive_size=release.archive.size,
        )
        existing = db.session.get(Addon, record.source_id)
        manager = self._manager()
        if existing is not None and record.source_id in (manager.manifests or {}):
            try:
                if Version(release.version) <= Version(existing.version):
                    raise AddonOperationError(
                        f"Version {existing.version} is already installed; catalog installs do not perform downgrades."
                    )
            except InvalidVersion as exc:
                raise AddonOperationError(
                    "The installed package version is invalid."
                ) from exc

        installer = AddonPackageInstaller(self.paths)
        staged = None

        download_progress = self._transfer_progress_callback(
            record,
            stage="package_download",
            progress_start=10,
            progress_span=10,
            message="Downloading and verifying the signed package.",
        )

        try:
            staged = service.stage_release(
                loaded,
                addon_id=str(record.source_id),
                release=release,
                installer=installer,
                operator_approved=payload.get("operator_approved") is True,
                progress=download_progress,
                cancellation_check=lambda: self._check_cancel(record),
            )
            coordinator = self._coordinator(record, installer)
            outcome = coordinator.install(
                staged,
                reviewed_notice_ids=payload.get("dependency_review") or (),
            )
            if outcome.requires_restart:
                from iarf_assistant.core.restart_state import require_restart

                require_restart(
                    self.paths, reason=f"addon:{outcome.addon_id}:installation"
                )
            return {
                **outcome.to_dict(),
                "catalog_sha256": loaded.source_sha256,
                "catalog_signer_key_id": loaded.signer_key_id,
            }
        finally:
            if staged is not None:
                installer.cleanup_stage(staged)

    def _execute_activation(self, record: RuntimeJob) -> dict[str, Any]:
        manager = self._manager()
        addon_id = str(record.source_id or "")
        package = (manager._addon_packages or {}).get(addon_id)
        contract = getattr(package, "contract", None)
        if contract is None:
            raise FileNotFoundError(
                f"Validated code for add-on '{addon_id}' is not available."
            )
        self._admit(record, contract=contract, archive_size=None)
        payload = dict(record.input_payload or {})
        manager.activate_addon(
            addon_id,
            dependency_review=payload.get("dependency_review") or [],
            operation_progress=self._progress_callback(record),
            cancellation_check=lambda: self._check_cancel(record),
        )
        from iarf_assistant.core.restart_state import require_restart

        require_restart(self.paths, reason=f"addon:{addon_id}:activation")
        return {
            "success": True,
            "message": f"Addon '{addon_id}' activated.",
            "addon_id": addon_id,
            "requires_restart": True,
        }


def get_addon_operation_service(app) -> AddonOperationService:
    service = app.extensions.get("carlina_addon_operations")
    if service is None:
        service = AddonOperationService(app)
        app.extensions["carlina_addon_operations"] = service
    return service


def register_addon_operation_orchestrator(app) -> AddonOperationService | None:
    """Attach the durable poller to the single managed-runtime scheduler."""

    scheduler = getattr(app, "scheduler", None)
    if scheduler is None:
        return None
    service = get_addon_operation_service(app)
    scheduler.add_job(
        func=service.poll_once,
        trigger="interval",
        seconds=1,
        id="core_addon_lifecycle_operations",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    return service


__all__ = [
    "ADDON_OPERATION_SOURCE_TYPE",
    "AddonOperationAdmissionError",
    "AddonOperationConflict",
    "AddonOperationError",
    "AddonOperationService",
    "configured_addon_catalog_service",
    "get_addon_operation_service",
    "register_addon_operation_orchestrator",
    "serialize_addon_operation",
]
