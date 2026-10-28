import logging
import math
import re
import threading
import time
import traceback
import uuid
from pathlib import Path

from flask import has_request_context
from flask_login import current_user

from .logging_observability import LogSourceRegistry
from .runtime_state import mirror_task_queue_status
from .safe_sqlite_store import JsonSQLiteStore, SafeSQLiteStoreError

logger = logging.getLogger(__name__)
_MAX_RUNTIME_OWNER_ID = 2**63 - 1
_MAX_RUNTIME_OWNER_DIGITS = len(str(_MAX_RUNTIME_OWNER_ID))


def _normalize_runtime_owner_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        owner_user_id = value
    elif isinstance(value, str):
        if (
            not value
            or len(value) > _MAX_RUNTIME_OWNER_DIGITS
            or not value.isascii()
            or not value.isdecimal()
        ):
            return None
        owner_user_id = int(value, 10)
    else:
        return None

    if 0 < owner_user_id <= _MAX_RUNTIME_OWNER_ID:
        return owner_user_id
    return None


def _request_actor_id() -> int | None:
    try:
        if has_request_context() and current_user.is_authenticated:
            return _normalize_runtime_owner_id(current_user.get_id())
    except Exception:
        return None
    return None


def _admission_status_user_id(payload: dict) -> tuple[bool, object]:
    if "user_id" in payload:
        user_id = payload.get("user_id")
        return user_id not in (None, ""), user_id

    user_id = _request_actor_id()
    return user_id is not None, user_id


def _validate_status_ttl(value: float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(  # noqa: TRY004 - this public option requires content-free ValueError.
            "status_ttl_seconds must be a finite value in range."
        )
    try:
        seconds = float(value)
    except (OverflowError, TypeError, ValueError):
        raise ValueError(
            "status_ttl_seconds must be a finite value in range."
        ) from None
    if not math.isfinite(seconds) or seconds <= 0 or seconds > 24 * 60 * 60:
        raise ValueError("status_ttl_seconds must be a finite value in range.")
    return seconds


class JobCancelledError(RuntimeError):
    """Raised by workers/handlers when a queued job is cancelled by user request."""


class JobRetryLaterError(RuntimeError):
    """Keep a leased queue delivery for a bounded later retry."""

    def __init__(self, message: str, *, delay_seconds: float):
        delay = float(delay_seconds)
        if not math.isfinite(delay) or delay <= 0 or delay > 24 * 60 * 60:
            raise ValueError(
                "Retry delay must be finite, positive, and at most 24 hours."
            )
        self.delay_seconds = delay
        super().__init__(message)


class ExternalWorkPending(BaseException):
    """Suspend one delivery until an external worker returns model output.

    This deliberately derives directly from :class:`BaseException`. Product
    pipelines commonly translate ordinary ``Exception`` instances into a
    durable failure state. External work is control flow, not a provider
    failure, so it must cross those boundaries untouched and be handled only
    by the queue runner.
    """

    def __init__(
        self,
        message: str,
        *,
        delay_seconds: float,
        work_item_id: str,
    ) -> None:
        delay = float(delay_seconds)
        if not math.isfinite(delay) or delay <= 0 or delay > 24 * 60 * 60:
            raise ValueError(
                "Retry delay must be finite, positive, and at most 24 hours."
            )
        normalized_work_item_id = str(work_item_id or "").strip()
        if not normalized_work_item_id or len(normalized_work_item_id) > 128:
            raise ValueError("A bounded external work item id is required.")
        self.delay_seconds = delay
        self.work_item_id = normalized_work_item_id
        super().__init__(message)


class TaskQueue:
    """
    A robust, SQLite-backed task queue for communicating between the
    Flask web server and isolated background worker processes.
    """

    def __init__(
        self,
        instance_path: str,
        addon_id: str,
        *,
        mirror_runtime: bool = True,
        status_ttl_seconds: float | None = None,
    ):
        if not isinstance(addon_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", addon_id
        ):
            raise ValueError("addon_id must be a bounded package identifier.")
        if type(mirror_runtime) is not bool:
            raise ValueError("mirror_runtime must be a boolean.")
        normalized_status_ttl = _validate_status_ttl(status_ttl_seconds)
        self.addon_id = addon_id
        self.mirror_runtime = mirror_runtime
        self.status_ttl_seconds = normalized_status_ttl
        self.log_registry = None

        # Store queue data in the instance folder so it survives restarts
        self.queue_dir = Path(instance_path) / "task_queues" / addon_id
        self.queue_dir.mkdir(parents=True, exist_ok=True)

        # Queue v2 never opens legacy DiskCache databases. Those files may
        # contain pickle records; importing them after an update would restore
        # the code-execution primitive this JSON-only store is designed to remove.
        self.store_path = self.queue_dir / "queue-v2.sqlite3"
        self.cache = JsonSQLiteStore(self.store_path, timeout=5)
        self.queue = self.cache.queue("pending_jobs")
        self._closed = False

        try:
            self.log_registry = LogSourceRegistry(instance_path)
            self.log_registry.register_default_sources()
            self.log_registry.register_job_source(
                addon_id, name=f"{addon_id} Jobs", addon_id=addon_id
            )
        except Exception as e:
            logging.warning(
                f"Failed to initialize task queue log sources for '{addon_id}': {e}"
            )

    def _append_job_log(self, job_id: str, task_name: str | None, message: str):
        if not self.log_registry:
            return

        fields = [f"job_id={job_id}"]
        if task_name:
            fields.append(f"task={task_name}")
        log_line = f"{' '.join(fields)} {message}"
        for source_id in ("jobs.runtime", f"job.{self.addon_id}"):
            try:
                self.log_registry.append_to_source(source_id, log_line)
            except Exception as e:
                logging.warning(
                    f"Failed to append job lifecycle log to '{source_id}': {e}"
                )

    def close(self):
        """Close SQLite handles held by the queue and pending-job view."""
        if self._closed:
            return
        self._closed = True
        seen = set()
        for cache_obj in (getattr(self.queue, "cache", None), self.cache):
            # Some compatible cache objects are falsey when empty but still
            # own open SQLite handles that must be closed.
            if cache_obj is None or id(cache_obj) in seen:
                continue
            seen.add(id(cache_obj))
            try:
                cache_obj.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback):
        self.close()
        return False

    def enqueue(self, task_name: str, payload: dict) -> str:
        """Pushes a task to the queue and returns a unique Job ID."""
        if not isinstance(task_name, str) or not task_name.strip():
            raise ValueError("task_name must be a non-empty string.")
        if not isinstance(payload, dict):
            raise ValueError("Task payloads must be JSON-compatible dictionaries.")
        self.cache.validate(payload)
        self._expire_statuses()
        job_id = str(uuid.uuid4())
        job_data = {"job_id": job_id, "task_name": task_name, "payload": payload}

        # Initialize status
        initial_status = {
            "status": "queued",
            "progress": 0,
            "message": "Waiting for worker...",
            "task_name": task_name,
        }
        include_user_id, user_id = _admission_status_user_id(payload)
        if include_user_id:
            initial_status["user_id"] = user_id
        self.set_status(
            job_id,
            initial_status,
            input_payload=payload,
        )

        # Push to the deque
        self.queue.append(job_data)
        return job_id

    def enqueue_once(
        self, task_name: str, payload: dict, *, job_id: str,
        status_metadata: dict | None = None,
        max_queue_items: int | None = None,
    ) -> str:
        """Durably enqueue one caller-keyed job, returning its stable id.

        A repeated id with the same task and JSON payload is an idempotent
        no-op, including after acknowledgement. The receipt, initial queue
        status and delivery are committed together; the SQLAlchemy runtime
        mirror is a best-effort projection performed only after that commit.
        Product identity metadata is stored in that same initial transaction;
        callers cannot replace queue lifecycle or owner fields. An optional
        item limit is applied by the atomic SQLite queue admission.
        """

        if not isinstance(payload, dict):
            raise TypeError("Task payloads must be JSON-compatible dictionaries.")
        if status_metadata is not None and (
            not isinstance(status_metadata, dict)
            or {"status", "progress", "message", "task_name", "user_id", "result", "error"}
            & status_metadata.keys()
        ):
            raise ValueError("Initial queue metadata must contain only product identity fields.")

        initial_status = {
            "status": "queued",
            "progress": 0,
            "message": "Waiting for worker...",
            "task_name": task_name,
        }
        include_user_id, user_id = _admission_status_user_id(payload)
        if include_user_id:
            initial_status["user_id"] = user_id

        initial_status.update(status_metadata or {})

        self._expire_statuses()
        status_expiry: dict[str, float] = {}
        if self.status_ttl_seconds is not None:
            status_expiry["status_expire_seconds"] = self.status_ttl_seconds
        created = self.queue.enqueue_once(
            job_id=job_id,
            task_name=task_name,
            payload=payload,
            status_data=initial_status,
            max_queue_items=max_queue_items,
            **status_expiry,
        )
        if created:
            try:
                self._append_job_log(
                    job_id,
                    task_name,
                    "status=queued Waiting for worker...",
                )
            except Exception:  # noqa: BLE001 - lifecycle logging is best-effort.
                logger.debug(
                    "Skipped initial queue log for task queue '%s'.", self.addon_id
                )
            if self.mirror_runtime:
                try:
                    mirror_task_queue_status(
                        addon_id=self.addon_id,
                        job_id=job_id,
                        task_name=task_name,
                        status_data=initial_status,
                        input_payload=payload,
                        owner_user_id=_normalize_runtime_owner_id(
                            initial_status.get("user_id")
                        ),
                    )
                except Exception:  # noqa: BLE001 - the durable enqueue already committed.
                    logger.debug(
                        "Skipped initial runtime status mirror for task queue '%s'.",
                        self.addon_id,
                    )
        return job_id

    def set_status(
        self, job_id: str, status_data: dict, input_payload: dict | None = None
    ):
        """Updates the status/progress of a job. Safe for concurrent workers."""
        status_payload = status_data if isinstance(status_data, dict) else {}
        expiry: dict[str, float] = {}
        if self.status_ttl_seconds is not None:
            expiry["expire"] = self.status_ttl_seconds
        previous_status_data, merged_status = self.cache.merge_dict(
            f"job_status:{job_id}", status_payload, **expiry
        )
        task_name = merged_status.get("task_name") or previous_status_data.get(
            "task_name"
        )
        if task_name and "task_name" not in status_payload:
            merged_status["task_name"] = task_name

        new_status = merged_status.get("status")
        previous_status = previous_status_data.get("status")
        if new_status and new_status != previous_status:
            message = (
                merged_status.get("message")
                or merged_status.get("error")
                or "Status changed."
            )
            self._append_job_log(job_id, task_name, f"status={new_status} {message}")

        if self.mirror_runtime:
            mirror_task_queue_status(
                addon_id=self.addon_id,
                job_id=job_id,
                task_name=task_name,
                status_data=merged_status,
                input_payload=input_payload,
                owner_user_id=_normalize_runtime_owner_id(
                    merged_status.get("user_id")
                ),
            )

    def _expire_statuses(self) -> None:
        if self.status_ttl_seconds is not None:
            self.cache.expire()

    def request_cancel(
        self, job_id: str, requested_by: int | None = None, reason: str | None = None
    ) -> dict:
        current = self.get_status(job_id)
        if not isinstance(current, dict):
            current = {"status": "unknown", "message": "Job not found."}

        current_status = str(current.get("status") or "").lower()
        terminal = {"completed", "failed", "cancelled", "unknown"}
        if current_status in terminal:
            return current

        message = str(reason or "Cancellation requested.")
        update = {
            "cancel_requested": True,
            "cancel_requested_at": int(time.time()),
            "message": message,
        }
        if requested_by not in (None, ""):
            update["cancel_requested_by"] = int(requested_by)

        if current_status == "queued":
            update["status"] = "cancelled"
            update["progress"] = int(current.get("progress") or 0)
        else:
            update["status"] = "cancelling"

        self.set_status(job_id, update)
        return self.get_status(job_id)

    def is_cancel_requested(self, job_id: str) -> bool:
        status = self.get_status(job_id)
        if not isinstance(status, dict):
            return False
        if bool(status.get("cancel_requested")):
            return True
        return str(status.get("status") or "").lower() == "cancelling"

    def get_status(self, job_id: str) -> dict:
        """Reads the current status of a job."""
        fallback = {"status": "unknown", "message": "Job not found."}
        try:
            value = self.cache.get(f"job_status:{job_id}", fallback)
        except SafeSQLiteStoreError as exc:
            logging.warning("Discarded corrupt status for job %s: %s", job_id, exc)
            return fallback
        return value if isinstance(value, dict) else fallback

    def increment_counter(self, counter_name: str, amount: int = 1):
        """Thread/Process-safe counter for batch progress tracking."""
        try:
            self.cache.increment(f"counter:{counter_name}", amount, default=0)
        except Exception as e:
            logging.error(f"Failed to increment counter {counter_name}: {e}")

    def set_counter(self, counter_name: str, value: int):
        """Sets a counter to a specific absolute value. Useful for initialization."""
        try:
            self.cache.set(f"counter:{counter_name}", value)
        except Exception as e:
            logging.error(f"Failed to set counter {counter_name}: {e}")

    def delete_counter(self, counter_name: str):
        """Cleans up a counter to free memory."""
        try:
            self.cache.delete(f"counter:{counter_name}")
        except Exception as e:
            logging.error(f"Failed to delete counter {counter_name}: {e}")

    def get_counter(self, counter_name: str) -> int:
        try:
            value = self.cache.get(f"counter:{counter_name}", 0)
        except SafeSQLiteStoreError as exc:
            logging.warning("Discarded corrupt counter %s: %s", counter_name, exc)
            return 0
        return value if isinstance(value, int) and not isinstance(value, bool) else 0


class WorkerNode:
    """
    A consumer wrapper that polls the TaskQueue and executes registered handlers.
    Abstracts away the while-loop, idle timeouts, and exception handling from the AI.
    """

    def __init__(
        self,
        instance_path: str,
        addon_id: str,
        idle_timeout: int = 60,
        *,
        mirror_runtime: bool = True,
        status_ttl_seconds: float | None = None,
    ):
        # Opening a TaskQueue creates SQLite files and handles. Keep
        # handler modules safe to import by deferring that work until the worker
        # is actually started (or a caller explicitly asks for the queue).
        if type(mirror_runtime) is not bool:
            raise ValueError("mirror_runtime must be a boolean.")
        normalized_status_ttl = _validate_status_ttl(status_ttl_seconds)
        self.instance_path = str(instance_path)
        self.addon_id = addon_id
        self.task_queue = None
        self.handlers = {}
        self.idle_timeout = idle_timeout
        self.mirror_runtime = mirror_runtime
        self.status_ttl_seconds = normalized_status_ttl

    def _get_task_queue(self):
        if self.task_queue is None:
            options = {}
            if self.mirror_runtime is not True:
                options["mirror_runtime"] = False
            if self.status_ttl_seconds is not None:
                options["status_ttl_seconds"] = self.status_ttl_seconds
            self.task_queue = TaskQueue(self.instance_path, self.addon_id, **options)
        return self.task_queue

    def handler(self, task_name: str):
        """Decorator to register a function to handle a specific task_name."""

        def decorator(func):
            self.handlers[task_name] = func
            return func

        return decorator

    def start(self):
        """Starts the blocking polling loop."""
        self._get_task_queue()
        print(
            f"WorkerNode started. Listening for tasks. Idle timeout: {self.idle_timeout}s"
        )
        try:
            self._run_loop()
        finally:
            self.close()

    def close(self):
        """Release queue handles held by this worker."""
        if self.task_queue is not None:
            self.task_queue.close()

    def _run_loop(self):
        task_queue = self._get_task_queue()
        idle_time = 0
        observed_claim_expiries: dict[tuple[int, float], float] = {}
        healthy_claims: set[tuple[int, float]] = set()
        locally_deferred_claims: set[str] = set()

        while True:
            claim_token = None
            try:
                if hasattr(task_queue.queue, "claim"):
                    claim_token, job = task_queue.queue.claim(lease_seconds=300)
                else:
                    # Compatibility for test/third-party queue adapters. Core
                    # production queues always use leased claim/ack delivery.
                    job = task_queue.queue.popleft()
                idle_time = 0  # Reset idle timer on successful pop

            except IndexError:
                # Queue is empty
                sleep_seconds = 0.5
                next_available_delay = getattr(
                    task_queue.queue, "next_available_delay", None
                )
                delayed_work = False
                if callable(next_available_delay):
                    try:
                        delay = next_available_delay()
                        if delay is not None:
                            delay = float(delay)
                            if math.isfinite(delay) and delay >= 0:
                                delayed_work = True
                                sleep_seconds = min(max(delay, 0.05), 0.5)
                    except Exception:  # noqa: BLE001
                        # Scheduling observation is advisory. Preserve the
                        # normal idle path if an adapter cannot provide it.
                        pass
                time.sleep(sleep_seconds)
                waiting_for_stale_claim = False
                try:
                    active_claims = getattr(task_queue.queue, "active_claims", None)
                    claims = active_claims() if callable(active_claims) else ()
                    current_identities: set[tuple[int, float]] = set()
                    for entry_id, observed_token, claimed_at, expires_at in claims:
                        identity = (entry_id, claimed_at)
                        current_identities.add(identity)
                        if observed_token in locally_deferred_claims:
                            healthy_claims.add(identity)
                        first_expiry = observed_claim_expiries.setdefault(
                            identity, expires_at
                        )
                        # A lease extended after it was first observed belongs
                        # to a healthy worker. A replacement need not shadow it.
                        if expires_at != first_expiry:
                            healthy_claims.add(identity)
                        if identity not in healthy_claims:
                            waiting_for_stale_claim = True
                    absent_claims = set(observed_claim_expiries) - current_identities
                    expired_stale_claim = any(
                        identity not in healthy_claims for identity in absent_claims
                    )
                    for identity in absent_claims:
                        observed_claim_expiries.pop(identity, None)
                        healthy_claims.discard(identity)
                except Exception:  # noqa: BLE001
                    # Claim observation is only a shutdown/recovery hint.
                    # Preserve normal idle behavior if an adapter lacks it or
                    # the read fails.
                    waiting_for_stale_claim = False
                    expired_stale_claim = False
                if waiting_for_stale_claim or expired_stale_claim:
                    continue
                if delayed_work:
                    idle_time = 0
                    continue
                idle_time += 0.5
                if self.idle_timeout and idle_time >= self.idle_timeout:
                    print(
                        f"Worker idle for {self.idle_timeout} seconds. Shutting down gracefully."
                    )
                    break
                continue
            except Exception as e:
                print(f"Error accessing queue: {e}")
                time.sleep(1)
                continue

            stop_heartbeat = threading.Event()
            heartbeat = None
            if claim_token is not None:
                heartbeat = threading.Thread(
                    target=self._renew_queue_claim,
                    args=(task_queue.queue, claim_token, stop_heartbeat),
                    daemon=True,
                    name=f"{self.addon_id}-queue-claim",
                )
                heartbeat.start()
            handled = False
            retry_delay = None
            try:
                self._process_job(task_queue, job)
                handled = True
            except (JobRetryLaterError, ExternalWorkPending) as exc:
                retry_delay = exc.delay_seconds
                job_id = job.get("job_id") if isinstance(job, dict) else None
                if isinstance(job_id, str) and job_id:
                    waiting_for_external = isinstance(exc, ExternalWorkPending)
                    task_queue.set_status(
                        job_id,
                        {
                            "status": "queued",
                            "message": str(exc) or "Job deferred for a safe retry.",
                            "retry_after_seconds": retry_delay,
                            **(
                                {
                                    "waiting_for_external_work": True,
                                    "external_work_item_id": exc.work_item_id,
                                }
                                if waiting_for_external
                                else {}
                            ),
                        },
                    )
            finally:
                stop_heartbeat.set()
                if heartbeat is not None:
                    heartbeat.join(timeout=2)
                if claim_token is not None:
                    try:
                        if handled:
                            acknowledged = task_queue.queue.ack(claim_token)
                            if not acknowledged:
                                logging.warning(
                                    "Queue claim expired before acknowledgement for add-on %s",
                                    self.addon_id,
                                )
                        elif retry_delay is not None:
                            defer = getattr(task_queue.queue, "defer", None)
                            if callable(defer):
                                deferred = defer(claim_token, retry_delay)
                            else:
                                # Compatibility adapters keep the historical
                                # lease-extension behavior until they expose
                                # atomic durable deferral.
                                deferred = task_queue.queue.renew_claim(
                                    claim_token,
                                    lease_seconds=retry_delay,
                                )
                                if deferred:
                                    locally_deferred_claims.add(claim_token)
                            if not deferred:
                                task_queue.queue.release(claim_token)
                        else:
                            task_queue.queue.release(claim_token)
                    except Exception:
                        logging.exception(
                            "Unable to finalize queue claim for add-on %s",
                            self.addon_id,
                        )
                elif retry_delay is not None and hasattr(task_queue.queue, "append"):
                    # Compatibility adapters lack leased claims. Put the
                    # envelope back rather than silently losing a retry.
                    task_queue.queue.append(job)
                    time.sleep(min(retry_delay, 5))

    @staticmethod
    def _renew_queue_claim(queue, claim_token: str, stop_event: threading.Event):
        while not stop_event.wait(60):
            try:
                if not queue.renew_claim(claim_token, lease_seconds=300):
                    return
            except Exception:
                logging.exception("Unable to renew a worker queue claim")
                return

    def _process_job(self, task_queue, job) -> None:
        if not isinstance(job, dict):
            print("Discarded malformed queue entry: expected a JSON object.")
            return

        job_id = job.get("job_id")
        task_name = job.get("task_name")
        payload = job.get("payload", {})
        if (
            not isinstance(job_id, str)
            or not job_id
            or not isinstance(task_name, str)
            or not task_name
            or not isinstance(payload, dict)
        ):
            print("Discarded malformed queue entry: invalid job envelope.")
            return

        previous_status = task_queue.get_status(job_id)
        if (
            isinstance(previous_status, dict)
            and previous_status.get("status") == "completed"
            and previous_status.get("task_name") == task_name
        ):
            return

        cancel_requested = task_queue.is_cancel_requested(job_id)
        translator_cancel_ack = (
            cancel_requested
            and getattr(self, "addon_id", None) == "translator"
            and task_name == "translate_document_blocks"
            and task_name in self.handlers
            and callable(getattr(self.handlers[task_name], "external_wait_guard", None))
        )
        if cancel_requested and not translator_cancel_ack:
            task_queue.set_status(
                job_id,
                {
                    "status": "cancelled",
                    "message": "Job cancelled before worker execution.",
                },
            )
            return

        if task_name not in self.handlers:
            msg = f"No handler registered for task: {task_name}"
            print(msg)
            task_queue.set_status(job_id, {"status": "failed", "error": msg})
            return

        # Sequential Translator jobs have nothing to do until the exact
        # suspended packet returns. Preserve ordinary product fences on
        # resumption and never park concurrent Document Processor jobs here.
        if (
            getattr(self, "addon_id", None) == "translator"
            and task_name == "translate_document_blocks"
            and not translator_cancel_ack
        ):
            previous = task_queue.get_status(job_id)
            if (
                isinstance(previous, dict)
                and previous.get("waiting_for_external_work") is True
                and isinstance(previous.get("external_work_item_id"), str)
            ):
                from .mcp_completion import PASSIVE_RETRY_SECONDS
                from .mcp_jobs import passive_work_is_pending

                work_item_id = previous["external_work_item_id"]
                from sqlalchemy.exc import OperationalError
                from ..database_utils import is_database_locked

                guard = getattr(self.handlers[task_name], "external_wait_guard", None)
                try:
                    can_park = callable(guard) and passive_work_is_pending(
                        work_item_id=work_item_id,
                        queue_job_id=job_id,
                        addon_id=self.addon_id,
                        owner_user_id=payload.get("user_id"),
                        resource_kind="project",
                        resource_id=payload.get("project_id"),
                    ) and guard(payload, task_queue, job_id)
                except OperationalError as exc:
                    if not is_database_locked(exc):
                        raise
                    raise JobRetryLaterError(
                        "Database busy while checking external work readiness.",
                        delay_seconds=PASSIVE_RETRY_SECONDS,
                    ) from exc
                if can_park:
                    raise ExternalWorkPending(
                        "Waiting for an MCP worker to return model output.",
                        delay_seconds=PASSIVE_RETRY_SECONDS,
                        work_item_id=work_item_id,
                    )

        print(f"Processing job {job_id} ({task_name})...")
        task_queue.set_status(
            job_id,
            {
                "status": "processing",
                "message": "Task started...",
                "waiting_for_external_work": False,
                "external_work_item_id": None,
                "retry_after_seconds": None,
            },
        )

        try:
            result = self.handlers[task_name](payload, task_queue, job_id)
            if task_queue.is_cancel_requested(job_id):
                task_queue.set_status(
                    job_id,
                    {
                        "status": "cancelled",
                        "result": result,
                        "message": "Job cancelled.",
                    },
                )
            else:
                task_queue.set_status(
                    job_id,
                    {"status": "completed", "result": result, "progress": 100},
                )
            print(f"Job {job_id} completed successfully.")
        except JobRetryLaterError:
            raise
        except Exception as exc:
            if isinstance(exc, JobCancelledError):
                task_queue.set_status(
                    job_id,
                    {
                        "status": "cancelled",
                        "message": str(exc) or "Job cancelled.",
                    },
                )
                print(f"Job {job_id} cancelled.")
                return
            error_trace = traceback.format_exc()
            print(f"Job {job_id} failed: {exc}\n{error_trace}")
            task_queue.set_status(
                job_id,
                {"status": "failed", "error": str(exc), "traceback": error_trace},
            )
