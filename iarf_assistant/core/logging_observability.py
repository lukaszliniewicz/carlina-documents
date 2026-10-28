import logging
import os
import re
import shutil
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from carlina.core.utc import utc_now

DEFAULT_LOG_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 5
DEFAULT_LOG_CHUNK_BYTES = 50 * 1024
DEFAULT_LOG_MESSAGE_LIMIT = 20_000
DEFAULT_ROTATION_RETRY_ATTEMPTS = 5
DEFAULT_ROTATION_RETRY_DELAY_SECONDS = 0.05
_LOCKED_FILE_WINERRORS = {5, 32}
_LOCKED_FILE_ERRNOS = {13, 16}

_LOG_CONTEXT: ContextVar[dict[str, str]] = ContextVar("iarf_log_context", default={})

_SECRET_KEYS = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "api_key",
    "apikey",
    "access_key",
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "cookie",
    "session",
    "client_secret",
    "encryption_key",
    "fernet_key",
    "flask_secret_key",
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(\b(?:"
    + "|".join(re.escape(key) for key in _SECRET_KEYS)
    + r")\b[\"']?\s*[:=]\s*)([\"'][^\"']*[\"']|[^,\s}\]]+)"
)
_BEARER_RE = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]{12,}")
_BASIC_AUTH_RE = re.compile(r"(?i)(\bBasic\s+)[A-Za-z0-9+/=-]{12,}")
_COOKIE_RE = re.compile(r"(?i)(\bcookie\s*[:=]\s*)([^\r\n]+)")
_DATA_BASE64_RE = re.compile(r"(?i)(data:[^;\s]+;base64,)([A-Za-z0-9+/=]{80,})")
_LONG_BASE64_RE = re.compile(
    r"(?<![A-Za-z0-9+/=])([A-Za-z0-9+/]{160,}={0,2})(?![A-Za-z0-9+/=])"
)
_SHARED_CAPABILITY_PATH_RE = re.compile(r"(/(?:api/)?shared/)[^/?\s\]]+")
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _is_lock_conflict(exc: OSError) -> bool:
    winerror = getattr(exc, "winerror", None)
    errno = getattr(exc, "errno", None)
    return winerror in _LOCKED_FILE_WINERRORS or errno in _LOCKED_FILE_ERRNOS


def _retry_locked_file_operation(
    operation: Callable[[], Any],
    *,
    action: str,
    attempts: int = DEFAULT_ROTATION_RETRY_ATTEMPTS,
    delay_seconds: float = DEFAULT_ROTATION_RETRY_DELAY_SECONDS,
    logger: logging.Logger | None = None,
):
    retries = max(1, int(attempts))
    for attempt in range(1, retries + 1):
        try:
            return operation()
        except OSError as exc:
            should_retry = _is_lock_conflict(exc) and attempt < retries
            if logger is not None:
                import sys

                if should_retry:
                    sys.stderr.write(
                        f"Retrying {action} after lock conflict (attempt {attempt}/{retries}): {exc}\n"
                    )
                else:
                    sys.stderr.write(
                        f"Failed to complete {action} after {attempt} attempt(s): {exc}\n"
                    )
            if not should_retry:
                raise
            time.sleep(max(0.0, float(delay_seconds)) * (2 ** (attempt - 1)))


class WindowsSafeRotatingFileHandler(RotatingFileHandler):
    """Rotating handler resilient to transient Windows file-lock conflicts."""

    def __init__(
        self,
        *args,
        rotation_retry_attempts: int = DEFAULT_ROTATION_RETRY_ATTEMPTS,
        rotation_retry_delay_seconds: float = DEFAULT_ROTATION_RETRY_DELAY_SECONDS,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.rotation_retry_attempts = max(1, int(rotation_retry_attempts))
        self.rotation_retry_delay_seconds = max(
            0.0, float(rotation_retry_delay_seconds)
        )

    def _retry(self, operation: Callable[[], Any], action: str):
        return _retry_locked_file_operation(
            operation,
            action=action,
            attempts=self.rotation_retry_attempts,
            delay_seconds=self.rotation_retry_delay_seconds,
            logger=logging.getLogger(__name__),
        )

    def rotate(self, source, dest):
        if not source or not os.path.exists(source):
            return

        if dest and os.path.exists(dest):
            self._retry(
                lambda: os.remove(dest),
                action=f"delete rotated log '{os.path.basename(dest)}'",
            )

        try:
            self._retry(
                lambda: os.replace(source, dest),
                action=f"rename '{os.path.basename(source)}' -> '{os.path.basename(dest)}'",
            )
            return
        except OSError as rename_exc:
            import sys

            sys.stderr.write(
                f"Rename-based rollover failed for '{source}'; trying copy-truncate fallback: {rename_exc}\n"
            )

        self._retry(
            lambda: shutil.copy2(source, dest),
            action=f"copy '{os.path.basename(source)}' -> '{os.path.basename(dest)}'",
        )
        self._retry(
            lambda: open(source, "w", encoding=self.encoding or "utf-8").close(),
            action=f"truncate '{os.path.basename(source)}'",
        )


def redact_text(value: Any, max_length: int | None = DEFAULT_LOG_MESSAGE_LIMIT) -> str:
    """Redacts common secret and oversized payload patterns from log text."""
    if value is None:
        return ""
    text = str(value)
    text = _COOKIE_RE.sub(r"\1[REDACTED]", text)
    text = _BEARER_RE.sub(r"\1[REDACTED]", text)
    text = _BASIC_AUTH_RE.sub(r"\1[REDACTED]", text)
    text = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}[REDACTED]", text)
    text = _SHARED_CAPABILITY_PATH_RE.sub(r"\1[REDACTED]", text)
    text = _DATA_BASE64_RE.sub(
        lambda match: f"{match.group(1)}[BASE64_REDACTED length={len(match.group(2))}]",
        text,
    )
    text = _LONG_BASE64_RE.sub(
        lambda match: f"[BASE64_REDACTED length={len(match.group(1))}]", text
    )
    if max_length is not None and len(text) > max_length:
        omitted = len(text) - max_length
        text = f"{text[:max_length]}...[TRUNCATED {omitted} chars]"
    return text


def sanitize_log_id(value: str) -> str:
    cleaned = _SAFE_ID_RE.sub("_", str(value or "").strip()).strip("._-")
    return cleaned or "unknown"


def push_log_context(**fields: Any):
    context = dict(_LOG_CONTEXT.get({}))
    for key, value in fields.items():
        if value is None:
            continue
        clean_key = sanitize_log_id(key)
        clean_value = (
            redact_text(value, max_length=300).replace("\n", " ").replace("\r", " ")
        )
        context[clean_key] = clean_value
    return _LOG_CONTEXT.set(context)


def reset_log_context(token) -> None:
    _LOG_CONTEXT.reset(token)


@contextmanager
def logging_context(**fields: Any):
    token = push_log_context(**fields)
    try:
        yield
    finally:
        reset_log_context(token)


def _format_log_context() -> str:
    context = _LOG_CONTEXT.get({})
    if not context:
        return ""
    preferred_order = [
        "request_id",
        "job_id",
        "build_id",
        "addon_id",
        "source_id",
        "operation",
        "method",
        "path",
        "user_id",
    ]
    keys = [key for key in preferred_order if key in context]
    keys.extend(sorted(key for key in context if key not in keys))
    fields = " ".join(f"{key}={context[key]}" for key in keys)
    return f" [{fields}]"


class LogContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.iarf_context = _format_log_context()
        return True


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record), max_length=None)


@dataclass
class LogSource:
    id: str
    name: str
    category: str
    path: Path
    description: str = ""
    source_type: str = "file"
    optional: bool = True
    clearable: bool = True
    downloadable: bool = True
    max_bytes: int = DEFAULT_LOG_MAX_BYTES
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT
    metadata: dict[str, Any] = field(default_factory=dict)


class LogSourceRegistry:
    """Allowlisted registry for local log files exposed through the admin UI."""

    def __init__(
        self,
        instance_path: str | Path,
        *,
        logs_dir: str | Path | None = None,
    ):
        self.instance_path = Path(instance_path).resolve()
        self.logs_dir = (
            Path(logs_dir).resolve()
            if logs_dir is not None
            else self.instance_path / "logs"
        )
        self.allowlisted_roots = tuple(
            dict.fromkeys((self.instance_path, self.logs_dir))
        )
        self.sources: dict[str, LogSource] = {}

    def register_default_sources(self) -> None:
        for subdir in ("app", "workers", "addons", "builds", "docker", "jobs"):
            (self.logs_dir / subdir).mkdir(parents=True, exist_ok=True)

        self.register_file_source(
            "app.legacy",
            "Current Application Log",
            "app",
            self.instance_path / "iarf_assistant.log",
            description="Compatibility source for the existing application log file.",
            optional=False,
        )
        self.register_file_source(
            "app.current",
            "Canonical Application Log",
            "app",
            self.logs_dir / "app" / "iarf_assistant.log",
            description="Canonical application log path under the configured log root.",
        )
        self.register_worker_source("core_knowledge", name="Core Knowledge Worker")
        self.register_file_source(
            "addons.install",
            "Addon Install Log",
            "addon",
            self.logs_dir / "addons" / "install.log",
            description="Shared addon installation and upgrade log.",
        )
        self.register_file_source(
            "addons.build",
            "Addon Build Log",
            "build",
            self.logs_dir / "addons" / "build.log",
            description="Shared addon build log.",
        )
        self.register_file_source(
            "build.tailwind",
            "Tailwind Build Log",
            "build",
            self.logs_dir / "builds" / "tailwind.log",
            description="Canonical Tailwind build log path.",
        )
        self.register_file_source(
            "docker.addon_services",
            "Docker Service Log",
            "docker",
            self.logs_dir / "docker" / "addon_services.log",
            description="Canonical Docker service lifecycle log path.",
        )
        self.register_file_source(
            "jobs.runtime",
            "Runtime Jobs Log",
            "jobs",
            self.logs_dir / "jobs" / "runtime.log",
            description="Canonical shared runtime jobs log path.",
        )

    def register_addon_manager_sources(self, manager: Any) -> None:
        for addon_id in sorted(getattr(manager, "manifests", {}).keys()):
            self.register_addon_sources(addon_id)

        registries = getattr(manager, "registries", None)
        if not registries:
            return

        for worker in getattr(
            getattr(registries, "workers", None), "records", {}
        ).values():
            self.register_worker_source(
                worker.id, name=worker.name, addon_id=worker.addon_id
            )
        for service in getattr(
            getattr(registries, "services", None), "records", {}
        ).values():
            if service.runtime == "docker":
                self.register_docker_source(
                    service.id, name=service.name, addon_id=service.addon_id
                )
        for job in getattr(
            getattr(registries, "scheduled_jobs", None), "records", {}
        ).values():
            self.register_job_source(job.id, name=job.name, addon_id=job.addon_id)

    def register_addon_sources(self, addon_id: str) -> None:
        safe_addon_id = sanitize_log_id(addon_id)
        self.register_file_source(
            f"addon.{safe_addon_id}.install",
            f"{addon_id} Install",
            "addon",
            self.logs_dir / "addons" / safe_addon_id / "install.log",
            description=f"Installation and upgrade log for addon '{addon_id}'.",
            metadata={"addon_id": addon_id},
        )
        self.register_file_source(
            f"addon.{safe_addon_id}.build",
            f"{addon_id} Build",
            "build",
            self.logs_dir / "addons" / safe_addon_id / "build.log",
            description=f"Build log for addon '{addon_id}'.",
            metadata={"addon_id": addon_id},
        )

    def register_worker_source(
        self, worker_id: str, name: str | None = None, addon_id: str | None = None
    ) -> LogSource:
        safe_worker_id = sanitize_log_id(worker_id)
        return self.register_file_source(
            f"worker.{safe_worker_id}",
            name or f"{worker_id} Worker",
            "worker",
            self.logs_dir / "workers" / f"{safe_worker_id}.log",
            description=f"Worker process log for '{worker_id}'.",
            metadata={"worker_id": worker_id, "addon_id": addon_id}
            if addon_id
            else {"worker_id": worker_id},
        )

    def register_docker_source(
        self, service_id: str, name: str | None = None, addon_id: str | None = None
    ) -> LogSource:
        safe_service_id = sanitize_log_id(service_id)
        return self.register_file_source(
            f"docker.{safe_service_id}",
            name or f"{service_id} Docker Service",
            "docker",
            self.logs_dir / "docker" / f"{safe_service_id}.log",
            description=f"Docker lifecycle log for service '{service_id}'.",
            metadata={"service_id": service_id, "addon_id": addon_id}
            if addon_id
            else {"service_id": service_id},
        )

    def register_job_source(
        self, job_id: str, name: str | None = None, addon_id: str | None = None
    ) -> LogSource:
        safe_job_id = sanitize_log_id(job_id)
        return self.register_file_source(
            f"job.{safe_job_id}",
            name or f"{job_id} Job",
            "jobs",
            self.logs_dir / "jobs" / f"{safe_job_id}.log",
            description=f"Scheduled or runtime job log for '{job_id}'.",
            metadata={"job_id": job_id, "addon_id": addon_id}
            if addon_id
            else {"job_id": job_id},
        )

    def register_file_source(
        self,
        source_id: str,
        name: str,
        category: str,
        path: str | Path,
        description: str = "",
        optional: bool = True,
        clearable: bool = True,
        downloadable: bool = True,
        max_bytes: int = DEFAULT_LOG_MAX_BYTES,
        backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
        metadata: dict[str, Any] | None = None,
    ) -> LogSource:
        source = LogSource(
            id=sanitize_log_id(source_id),
            name=name,
            category=category,
            path=self._validate_allowlisted_path(path),
            description=description,
            optional=optional,
            clearable=clearable,
            downloadable=downloadable,
            max_bytes=max_bytes,
            backup_count=backup_count,
            metadata=metadata or {},
        )
        source.path.parent.mkdir(parents=True, exist_ok=True)
        self.sources[source.id] = source
        return source

    def _validate_allowlisted_path(self, path: str | Path) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.instance_path / candidate
        resolved = candidate.resolve()
        if any(
            resolved == root or root in resolved.parents
            for root in self.allowlisted_roots
        ):
            return resolved
        raise ValueError(
            f"Log source path is outside Carlina's allowlisted roots: {resolved}"
        )

    def get_source(self, source_id: str) -> LogSource:
        safe_source_id = sanitize_log_id(source_id)
        if safe_source_id not in self.sources:
            raise KeyError(source_id)
        return self.sources[safe_source_id]

    def list_sources(self, category: str | None = None) -> list[dict[str, Any]]:
        sources = self.sources.values()
        if category:
            sources = [source for source in sources if source.category == category]
        return [
            self.source_to_dict(source)
            for source in sorted(
                sources, key=lambda item: (item.category, item.name.lower())
            )
        ]

    def source_to_dict(self, source: LogSource) -> dict[str, Any]:
        exists = source.path.exists()
        warning = None
        size = 0
        modified_at = None
        readable = False
        writable = False
        if exists:
            try:
                stat = source.path.stat()
                size = stat.st_size
                modified_at = datetime.fromtimestamp(stat.st_mtime).isoformat()
                readable = os.access(source.path, os.R_OK)
                writable = os.access(source.path, os.W_OK)
                if not readable:
                    warning = "Log file exists but is not readable."
            except OSError as exc:
                warning = f"Could not inspect log file: {exc}"
        else:
            writable = os.access(source.path.parent, os.W_OK)
            warning = "Log file has not been created yet."
            if not source.optional:
                warning = "Required log file does not exist yet."

        return {
            "id": source.id,
            "name": source.name,
            "category": source.category,
            "description": source.description,
            "path": str(source.path),
            "exists": exists,
            "readable": readable,
            "writable": writable,
            "size": size,
            "modified_at": modified_at,
            "warning": warning,
            "clearable": source.clearable,
            "downloadable": source.downloadable,
            "max_bytes": source.max_bytes,
            "backup_count": source.backup_count,
            "metadata": dict(source.metadata),
            "url": f"/logs?source={quote(source.id)}",
            "api_url": f"/api/logs/{quote(source.id)}",
        }

    def get_links_for_addon(self, addon_id: str) -> list[dict[str, str]]:
        links = []
        for source in self.sources.values():
            if source.metadata.get("addon_id") != addon_id:
                continue
            links.append(self.source_to_link(source))
        return sorted(links, key=lambda item: (item["category"], item["name"].lower()))

    def get_links_by_category(self, category: str) -> list[dict[str, str]]:
        links = []
        for source in self.sources.values():
            if source.category != category:
                continue
            links.append(self.source_to_link(source))
        return sorted(links, key=lambda item: item["name"].lower())

    def get_link_for_source(self, source_id: str) -> dict[str, str]:
        return self.source_to_link(self.get_source(source_id))

    def source_to_link(self, source: LogSource) -> dict[str, str]:
        return {
            "id": source.id,
            "name": source.name,
            "category": source.category,
            "url": f"/logs?source={quote(source.id)}",
            "api_url": f"/api/logs/{quote(source.id)}",
        }

    def read_chunk(
        self,
        source_id: str,
        end_byte: int | str | None = None,
        chunk_size: int = DEFAULT_LOG_CHUNK_BYTES,
    ) -> dict[str, Any]:
        source = self.get_source(source_id)
        source_info = self.source_to_dict(source)
        if not source.path.exists():
            return {
                "text": "",
                "next_cursor": 0,
                "source": source_info,
                "warning": source_info["warning"],
            }

        file_size = source.path.stat().st_size
        if end_byte in (None, ""):
            cursor = file_size
        else:
            cursor = max(0, min(file_size, int(end_byte)))
        if cursor <= 0:
            return {
                "text": "",
                "next_cursor": 0,
                "source": source_info,
                "warning": source_info["warning"],
            }

        start_byte = max(0, cursor - chunk_size)
        bytes_to_read = cursor - start_byte
        with open(source.path, "rb") as handle:
            handle.seek(start_byte)
            chunk = handle.read(bytes_to_read)

        text = chunk.decode("utf-8", errors="replace")
        if start_byte > 0:
            first_newline = text.find("\n")
            if first_newline != -1:
                text = text[first_newline + 1 :]
                start_byte += first_newline + 1

        return {
            "text": redact_text(text, max_length=250_000),
            "next_cursor": start_byte,
            "source": source_info,
            "warning": source_info["warning"],
        }

    def read_all_text(self, source_id: str) -> str:
        source = self.get_source(source_id)
        if not source.path.exists():
            raise FileNotFoundError(str(source.path))
        with open(source.path, "r", encoding="utf-8", errors="replace") as handle:
            return redact_text(handle.read(), max_length=None)

    def read_tail_lines(self, source_id: str, lines: int = 100) -> str:
        source = self.get_source(source_id)
        if not source.path.exists():
            raise FileNotFoundError(str(source.path))
        with open(source.path, "r", encoding="utf-8", errors="replace") as handle:
            all_lines = handle.readlines()
        return redact_text("".join(all_lines[-max(1, lines) :]), max_length=250_000)

    def clear_source(self, source_id: str, actor: str | None = None) -> None:
        source = self.get_source(source_id)
        if not source.clearable:
            raise PermissionError(f"Log source '{source.id}' cannot be cleared.")
        source.path.parent.mkdir(parents=True, exist_ok=True)
        actor_label = redact_text(actor or "unknown", max_length=300)
        with open(source.path, "w", encoding="utf-8") as handle:
            handle.write(
                f"[{utc_now().strftime('%Y-%m-%d %H:%M:%S')}] Log source '{source.id}' cleared by {actor_label}.\n"
            )

    def append_to_source(self, source_id: str, text: str) -> None:
        source = self.get_source(source_id)
        source.path.parent.mkdir(parents=True, exist_ok=True)
        self.rotate_if_needed(source)
        timestamp = utc_now().strftime("%Y-%m-%d %H:%M:%S")
        with open(source.path, "a", encoding="utf-8") as handle:
            handle.write(f"[{timestamp}] {redact_text(text, max_length=250_000)}\n")

    def open_for_append(self, source_id: str):
        source = self.get_source(source_id)
        source.path.parent.mkdir(parents=True, exist_ok=True)
        self.rotate_if_needed(source)
        return open(source.path, "a", encoding="utf-8", errors="replace")

    def rotate_if_needed(self, source: LogSource) -> None:
        if (
            source.max_bytes <= 0
            or not source.path.exists()
            or source.path.stat().st_size <= source.max_bytes
        ):
            return
        if source.backup_count <= 0:
            source.path.write_text("", encoding="utf-8")
            return
        logger = logging.getLogger(__name__)

        def _safe_delete(path: Path) -> None:
            if not path.exists():
                return
            _retry_locked_file_operation(
                lambda: path.unlink(),
                action=f"delete rotated log '{path.name}'",
                logger=logger,
            )

        def _safe_replace(source_path: Path, target_path: Path) -> None:
            _retry_locked_file_operation(
                lambda: os.replace(str(source_path), str(target_path)),
                action=f"rename '{source_path.name}' -> '{target_path.name}'",
                logger=logger,
            )

        oldest = source.path.with_name(f"{source.path.name}.{source.backup_count}")
        try:
            _safe_delete(oldest)
            for index in range(source.backup_count - 1, 0, -1):
                current = source.path.with_name(f"{source.path.name}.{index}")
                if current.exists():
                    _safe_replace(
                        current,
                        source.path.with_name(f"{source.path.name}.{index + 1}"),
                    )
            _safe_replace(source.path, source.path.with_name(f"{source.path.name}.1"))
        except OSError as exc:
            logger.warning(
                "Rotation failed for '%s'; keeping current log file in place: %s",
                source.path,
                exc,
            )


def _make_rotating_handler(source: LogSource) -> RotatingFileHandler:
    source.path.parent.mkdir(parents=True, exist_ok=True)
    handler = WindowsSafeRotatingFileHandler(
        source.path,
        maxBytes=source.max_bytes,
        backupCount=source.backup_count,
        encoding="utf-8",
    )
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        RedactingFormatter("%(asctime)s - %(levelname)s - %(message)s%(iarf_context)s")
    )
    handler.addFilter(LogContextFilter())
    handler._iarf_managed_handler = True
    handler._iarf_source_id = source.id
    return handler


def configure_application_logging(app: Any, registry: LogSourceRegistry) -> None:
    """Installs managed rotating file handlers for legacy and canonical app logs."""
    target_sources = [
        registry.get_source("app.legacy"),
        registry.get_source("app.current"),
    ]
    target_paths = {source.path for source in target_sources}
    root_logger = logging.getLogger()

    for handler in list(root_logger.handlers):
        handler_path = (
            Path(getattr(handler, "baseFilename", "")).resolve()
            if getattr(handler, "baseFilename", None)
            else None
        )
        if (
            getattr(handler, "_iarf_managed_handler", False)
            or handler_path in target_paths
        ):
            root_logger.removeHandler(handler)
            handler.close()

    for source in target_sources:
        root_logger.addHandler(_make_rotating_handler(source))

    root_logger.setLevel(logging.INFO)
    app.logger.setLevel(logging.INFO)
    app.logger.propagate = True
