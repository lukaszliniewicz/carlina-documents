"""Owner-scoped, bounded reads of completed document-block exports."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import stat
from pathlib import Path
from typing import Any

from flask import current_app

from carlina.core.windows_artifacts import (
    WindowsArtifactDirectory,
    WindowsArtifactError,
)

from .models import TranslationOperation, TranslationProject

EXPORT_FORMATS = frozenset({"epub", "docx", "html", "pdf"})
MAX_EXPORT_CHUNK_BYTES = 65_536
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_SAFE_FILENAME_RE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,254}\Z", re.ASCII)
_SAFE_OPERATION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z", re.ASCII)
_ARTIFACT_FIELDS = frozenset(
    {
        "format",
        "media_type",
        "byte_length",
        "sha256",
        "validation_profile",
        "validation_profile_version",
    }
)
_LEGACY_DUPLICATE = {
    "duplicate": True,
    "message": "Duplicate translator delivery ignored.",
}


def _is_legacy_duplicate(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == set(_LEGACY_DUPLICATE)
        and value.get("duplicate") is True
        and value.get("message") == _LEGACY_DUPLICATE["message"]
    )


class McpExportError(ValueError):
    """A stable, path-free error suitable for an MCP tool boundary."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = message


def _invalid(code: str, message: str) -> McpExportError:
    return McpExportError(code, message)


def validate_read_options(
    operation_id: str,
    include_content: bool,
    offset: int,
    limit: int,
    expected_sha256: str | None,
) -> None:
    if (
        not isinstance(operation_id, str)
        or not operation_id.strip()
        or operation_id != Path(operation_id).name
        or operation_id in {".", ".."}
    ):
        raise _invalid(
            "invalid_operation_id", "A document export operation ID is required."
        )
    if type(include_content) is not bool:
        raise _invalid("invalid_include_content", "include_content must be a boolean.")
    if type(offset) is not int or offset < 0:
        raise _invalid("invalid_offset", "offset must be a nonnegative integer.")
    if type(limit) is not int or not 1 <= limit <= MAX_EXPORT_CHUNK_BYTES:
        raise _invalid("invalid_limit", "limit must be an integer from 1 to 65536.")
    if expected_sha256 is not None and (
        not isinstance(expected_sha256, str)
        or _SHA256_RE.fullmatch(expected_sha256) is None
    ):
        raise _invalid(
            "invalid_expected_sha256", "expected_sha256 must be a SHA-256 hex digest."
        )


def _base_payload(
    project: TranslationProject, operation: TranslationOperation
) -> dict[str, Any]:
    state = operation.state
    payload: dict[str, Any] = {
        "contract": "translator-mcp-export-v1",
        "project_id": project.id,
        "operation_id": operation.id,
        "queue_job_id": operation.queue_job_id,
        "state": state,
        "status": state,
        "ready": False,
    }
    if state in {"queued", "running", "cancel_requested", "completed"}:
        payload["retry"] = "poll export_get with the same operation_id"
    if state in {"failed", "partial", "cancelled"}:
        code = operation.error_code
        payload["error_code"] = (
            code
            if isinstance(code, str) and _SAFE_CODE_RE.fullmatch(code)
            else "export_failed"
        )
        payload["message"] = "The document export did not complete."
    checkpoint = operation.checkpoint_json
    manifest = (operation.result_json or {}).get("review_manifest")
    if manifest is None and isinstance(checkpoint, dict):
        manifest = checkpoint.get("review_manifest")
    if isinstance(manifest, dict):
        payload["review"] = {
            "requirement": manifest.get("requirement", "none"),
            "manifest_sha256": manifest.get("sha256"),
            "summary": (manifest.get("coverage_manifest") or {}).get("summary"),
        }
    return payload


def export_job_projection(
    project: TranslationProject, operation: TranslationOperation
) -> dict[str, Any]:
    """Return the exact export operation without leaking private checkpoints."""

    payload = _base_payload(project, operation)
    request = operation.checkpoint_json
    if (
        isinstance(request, dict)
        and isinstance(request.get("format"), str)
        and request["format"] in EXPORT_FORMATS
    ):
        payload["format"] = request["format"]
    if operation.state == "completed":
        export_format, _media_type, _byte_length, artifact_sha256 = _artifact(
            operation
        )
        quality = _validated_quality(
            operation, export_format=export_format, artifact_sha256=artifact_sha256
        )
        if quality is not None:
            payload["quality"] = quality
    return payload


def _artifact(operation: TranslationOperation) -> tuple[str, str, int, str]:
    result = operation.result_json
    if not isinstance(result, dict) or not isinstance(result.get("artifact"), dict):
        raise _invalid("artifact_invalid", "The export artifact record is invalid.")
    artifact = result["artifact"]
    export_format = result.get("format")
    checkpoint = operation.checkpoint_json
    media_type = artifact.get("media_type")
    byte_length = artifact.get("byte_length")
    sha256 = artifact.get("sha256")
    artifact_fields = frozenset(artifact)
    if (
        artifact_fields not in {_ARTIFACT_FIELDS, _ARTIFACT_FIELDS | {"quality"}}
        or
        not isinstance(export_format, str)
        or export_format not in EXPORT_FORMATS
        or not isinstance(checkpoint, dict)
        or checkpoint.get("format") != export_format
        or artifact.get("format") != export_format
        or not isinstance(media_type, str)
        or not media_type
        or len(media_type) > 128
        or any(ord(char) < 32 or ord(char) > 126 for char in media_type)
        or (export_format == "pdf" and media_type != "application/pdf")
        or type(byte_length) is not int
        or byte_length < 0
        or not isinstance(sha256, str)
        or _SHA256_RE.fullmatch(sha256) is None
    ):
        raise _invalid("artifact_invalid", "The export artifact record is invalid.")
    return export_format, media_type, byte_length, sha256


def _validated_quality(
    operation: TranslationOperation, *, export_format: str, artifact_sha256: str
) -> dict[str, Any] | None:
    """Expose a report only when it matches its persisted request and artifact."""

    result = operation.result_json
    artifact = result.get("artifact") if isinstance(result, dict) else None
    checkpoint = operation.checkpoint_json
    if not isinstance(artifact, dict) or not isinstance(checkpoint, dict):
        raise _invalid("artifact_invalid", "The export artifact record is invalid.")

    from carlina.core.epub_export import (
        EpubExportContractError,
        epub_value_sha256,
        normalize_epub_options,
        normalize_epub_quality,
    )

    try:
        options = normalize_epub_options(checkpoint["epub_options"])
    except KeyError:
        options = {}
    except EpubExportContractError:
        raise _invalid("artifact_invalid", "The export artifact record is invalid.") from None

    if "quality" not in artifact:
        if options:
            raise _invalid("artifact_invalid", "The export artifact record is invalid.")
        return None
    if export_format != "epub" or not options:
        raise _invalid("artifact_invalid", "The export artifact record is invalid.")
    input_sha256 = checkpoint.get("semantic_html_sha256")
    if not isinstance(input_sha256, str) or _SHA256_RE.fullmatch(input_sha256) is None:
        raise _invalid("artifact_invalid", "The export artifact record is invalid.")
    try:
        quality = normalize_epub_quality(artifact["quality"])
    except EpubExportContractError:
        raise _invalid("artifact_invalid", "The export artifact record is invalid.") from None
    if (
        quality["input_sha256"] != input_sha256
        or quality["options_sha256"] != epub_value_sha256(options)
        or quality["artifact_sha256"] != artifact_sha256
    ):
        raise _invalid("artifact_invalid", "The export artifact record is invalid.")
    return quality


def _delivery_path(
    delivery: dict[str, Any], operation_id: str, export_format: str
) -> Path:
    raw_path = delivery.get("output_path")
    name = delivery.get("download_name")
    if not isinstance(raw_path, str) or not isinstance(name, str):
        raise _invalid("artifact_invalid", "The export delivery record is invalid.")
    path = Path(raw_path)
    expected_dir = Path(current_app.instance_path) / "translator_exports" / operation_id
    if (
        not path.is_absolute()
        or raw_path != str(path)
        or str(path.parent) != str(expected_dir)
        or path.name != name
        or _SAFE_FILENAME_RE.fullmatch(name) is None
        or not name.endswith(f".{export_format}")
    ):
        raise _invalid(
            "artifact_invalid", "The export filename or location is invalid."
        )
    return path


def _validate_delivery(
    delivery: Any,
    project: TranslationProject,
    operation: TranslationOperation,
    export_format: str,
    media_type: str,
) -> dict[str, Any]:
    if (
        not isinstance(delivery, dict)
        or delivery.get("kind") != "file_download"
        or type(delivery.get("user_id")) is not int
        or delivery["user_id"] != project.user_id
        or delivery.get("project_id") != project.id
        or delivery.get("format") != export_format
        or delivery.get("mimetype") != media_type
    ):
        raise _invalid("artifact_invalid", "The export delivery record is invalid.")
    _delivery_path(delivery, operation.id, export_format)
    return dict(delivery)


def _file_snapshot(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _verify_file_witness(fd: int, byte_length: int, sha256: str) -> None:
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size != byte_length:
            raise _invalid("artifact_invalid", "The export file is invalid.")
        digest = hashlib.sha256()
        while chunk := os.read(fd, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(fd)
        if (
            _file_snapshot(before) != _file_snapshot(after)
            or digest.hexdigest() != sha256
        ):
            raise _invalid(
                "artifact_invalid", "The export file does not match its record."
            )
    except OSError:
        raise _invalid(
            "artifact_unavailable", "The export file is unavailable."
        ) from None


def _recover_legacy_delivery(
    project: TranslationProject,
    operation: TranslationOperation,
    export_format: str,
    media_type: str,
    byte_length: int,
    sha256: str,
) -> dict[str, Any]:
    """Find one exact-operation legacy file and verify its immutable bytes."""

    root = Path(current_app.instance_path) / "translator_exports"
    directory_path = root / operation.id
    directory: WindowsArtifactDirectory | None = None
    directory_fds: list[int] = []
    try:
        if os.name == "nt":
            directory = WindowsArtifactDirectory.open_existing(
                Path(current_app.instance_path), "translator_exports", operation.id
            )
            names = os.listdir(directory.path)
        else:
            if (
                not hasattr(os, "O_DIRECTORY")
                or not hasattr(os, "O_NOFOLLOW")
                or os.open not in os.supports_dir_fd
            ):
                raise _invalid(
                    "artifact_access_unsupported",
                    "Secure export file access is unavailable on this platform.",
                )
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            directory_fds.append(os.open(root, flags))
            directory_fds.append(os.open(operation.id, flags, dir_fd=directory_fds[-1]))
            names = os.listdir(directory_fds[-1])
        candidates = [
            name
            for name in names
            if not name.startswith(".") and name.endswith(f".{export_format}")
        ]
        if len(candidates) != 1 or _SAFE_FILENAME_RE.fullmatch(candidates[0]) is None:
            raise _invalid(
                "artifact_invalid", "The export file cannot be identified safely."
            )
        name = candidates[0]
        if directory is not None:
            fd = directory.open_file(name)
        else:
            fd = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
                dir_fd=directory_fds[-1],
            )
        try:
            _verify_file_witness(fd, byte_length, sha256)
        finally:
            os.close(fd)
    except WindowsArtifactError as exc:
        if exc.code == "unsupported":
            raise _invalid(
                "artifact_access_unsupported",
                "Secure export file access is unavailable on this platform.",
            ) from None
        code = "artifact_invalid" if exc.code == "invalid" else "artifact_unavailable"
        raise _invalid(code, "The export file is invalid or unavailable.") from None
    except (OSError, ValueError) as exc:
        if isinstance(exc, McpExportError):
            raise
        raise _invalid(
            "artifact_unavailable", "The export file is unavailable."
        ) from None
    finally:
        if directory is not None:
            directory.close()
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)
    delivery = {
        "kind": "file_download",
        "output_path": str(directory_path / name),
        "download_name": name,
        "mimetype": media_type,
        "user_id": project.user_id,
        "project_id": project.id,
        "format": export_format,
    }
    return _validate_delivery(delivery, project, operation, export_format, media_type)


def completed_export_delivery(
    *,
    project: TranslationProject,
    operation: TranslationOperation,
    queue_result: Any = None,
    recover_legacy: bool = False,
) -> dict[str, Any] | None:
    """Return a completed exact-bound delivery, or None for unpublished legacy status.

    A present durable delivery is authoritative, including when malformed.
    Legacy disk recovery is available to an exact duplicate queue result, a
    missing terminal queue record, or the same-delivery terminal worker replay.
    """

    if (
        operation.project_id != project.id
        or operation.owner_user_id != project.user_id
        or operation.kind != "document_block_export"
        or operation.state != "completed"
        or not isinstance(operation.id, str)
        or _SAFE_OPERATION_ID_RE.fullmatch(operation.id) is None
        or not isinstance(operation.queue_job_id, str)
        or not operation.queue_job_id.strip()
        or len(operation.queue_job_id) > 128
    ):
        raise _invalid("export_not_found", "Document export operation not found.")
    export_format, media_type, byte_length, sha256 = _artifact(operation)
    result = operation.result_json
    if "delivery" in result:
        delivery = _validate_delivery(
            result["delivery"], project, operation, export_format, media_type
        )
        if recover_legacy:
            path = _delivery_path(delivery, operation.id, export_format)
            fd = _open_artifact(path, operation.id, export_format)
            try:
                _verify_file_witness(fd, byte_length, sha256)
            finally:
                os.close(fd)
        return delivery
    if isinstance(queue_result, dict) and queue_result.get("kind") == "file_download":
        return _validate_delivery(
            queue_result, project, operation, export_format, media_type
        )
    if (recover_legacy and queue_result is None) or _is_legacy_duplicate(queue_result):
        return _recover_legacy_delivery(
            project, operation, export_format, media_type, byte_length, sha256
        )
    if queue_result is not None:
        raise _invalid("artifact_invalid", "The export delivery record is invalid.")
    return None


def _open_artifact(path: Path, operation_id: str, export_format: str) -> int:
    """Open the exact operation child through no-follow directory descriptors."""

    root = Path(current_app.instance_path) / "translator_exports"
    expected_dir = root / operation_id
    if (
        not path.is_absolute()
        or path.parent != expected_dir
        or path.name in {"", ".", ".."}
        or path.suffix != f".{export_format}"
        or any(part in {".", ".."} for part in path.parts)
    ):
        raise _invalid("artifact_invalid", "The export file location is invalid.")
    if os.name == "nt":
        try:
            directory = WindowsArtifactDirectory.open_existing(
                Path(current_app.instance_path), "translator_exports", operation_id
            )
            try:
                return directory.open_file(path.name)
            finally:
                directory.close()
        except WindowsArtifactError as exc:
            if exc.code == "invalid":
                raise _invalid(
                    "artifact_invalid", "The export file is invalid."
                ) from None
            if exc.code == "unsupported":
                raise _invalid(
                    "artifact_access_unsupported",
                    "Secure export file access is unavailable on this platform.",
                ) from None
            raise _invalid(
                "artifact_unavailable", "The export file is unavailable."
            ) from None
        except OSError, ValueError:
            raise _invalid(
                "artifact_unavailable", "The export file is unavailable."
            ) from None
    if (
        not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or os.open not in os.supports_dir_fd
    ):
        raise _invalid(
            "artifact_access_unsupported",
            "Secure export file access is unavailable on this platform.",
        )
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    fds: list[int] = []
    try:
        fds.append(os.open(root, directory_flags))
        fds.append(os.open(operation_id, directory_flags, dir_fd=fds[-1]))
        opened = os.open(path.name, file_flags, dir_fd=fds[-1])
        if not stat.S_ISREG(os.fstat(opened).st_mode):
            os.close(opened)
            raise _invalid("artifact_invalid", "The export file is invalid.")
        return opened
    except (OSError, ValueError) as exc:
        if isinstance(exc, McpExportError):
            raise
        raise _invalid(
            "artifact_unavailable", "The export file is unavailable."
        ) from None
    finally:
        for fd in reversed(fds):
            os.close(fd)


def read_export(
    *,
    project: TranslationProject,
    operation: TranslationOperation,
    queue: Any,
    operation_id: str,
    include_content: bool = False,
    offset: int = 0,
    limit: int = MAX_EXPORT_CHUNK_BYTES,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """Read bounded bytes only after exact operation, queue, and artifact checks."""

    validate_read_options(operation_id, include_content, offset, limit, expected_sha256)
    if (
        operation.id != operation_id
        or operation.project_id != project.id
        or operation.owner_user_id != project.user_id
        or operation.kind != "document_block_export"
    ):
        raise _invalid("export_not_found", "Document export operation not found.")
    payload = _base_payload(project, operation)
    if operation.state != "completed":
        return payload
    export_format, media_type, byte_length, sha256 = _artifact(operation)
    quality = _validated_quality(
        operation, export_format=export_format, artifact_sha256=sha256
    )
    if expected_sha256 is not None and expected_sha256 != sha256:
        raise _invalid(
            "checksum_mismatch", "The export checksum does not match expected_sha256."
        )
    result = completed_export_delivery(project=project, operation=operation)
    recovered_legacy = False
    if result is None:
        try:
            status = queue.get_status(operation.queue_job_id)
        except Exception:
            raise _invalid(
                "export_queue_unavailable",
                "The export delivery state is temporarily unavailable.",
            ) from None
        if isinstance(status, dict) and status.get("status") == "unknown":
            if "result" in status:
                raise _invalid(
                    "artifact_invalid", "The export delivery record is invalid."
                )
            result = completed_export_delivery(
                project=project, operation=operation, recover_legacy=True
            )
            recovered_legacy = True
        else:
            if (
                not isinstance(status, dict)
                or status.get("status") != "completed"
                or not isinstance(status.get("result"), dict)
            ):
                return payload
            queue_result = status["result"]
            result = completed_export_delivery(
                project=project, operation=operation, queue_result=queue_result
            )
            recovered_legacy = _is_legacy_duplicate(queue_result)
        if result is None:
            return payload
    path = _delivery_path(result, operation.id, export_format)
    fd = _open_artifact(path, operation.id, export_format)
    try:
        before = os.fstat(fd)
        if recovered_legacy:
            # Discovery and the final reader open are separate. Authenticate
            # this exact descriptor before returning metadata or any chunk.
            _verify_file_witness(fd, byte_length, sha256)
        if before.st_size != byte_length:
            raise _invalid(
                "artifact_invalid", "The export file size does not match its record."
            )
        if offset > byte_length:
            raise _invalid("invalid_offset", "offset exceeds the export file size.")
        if include_content:
            os.lseek(fd, offset, os.SEEK_SET)
            chunk = os.read(fd, min(limit, byte_length - offset))
            if len(chunk) != min(limit, byte_length - offset):
                raise _invalid(
                    "artifact_invalid", "The export file changed during the read."
                )
        after = os.fstat(fd)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise _invalid(
                "artifact_invalid", "The export file changed during the read."
            )
    except OSError:
        raise _invalid(
            "artifact_unavailable", "The export file is unavailable."
        ) from None
    finally:
        os.close(fd)
    payload.update(
        {
            "ready": True,
            "retry": None,
            "filename": path.name,
            "media_type": media_type,
            "format": export_format,
            "byte_length": byte_length,
            "sha256": sha256,
        }
    )
    if quality is not None:
        payload["quality"] = quality
    if include_content:
        next_offset = offset + len(chunk)
        payload.update(
            {
                "content_base64": base64.b64encode(chunk).decode("ascii"),
                "offset": offset,
                "next_offset": next_offset,
                "eof": next_offset == byte_length,
                "chunk_sha256": hashlib.sha256(chunk).hexdigest(),
            }
        )
    return payload


__all__ = [
    "McpExportError",
    "completed_export_delivery",
    "export_job_projection",
    "read_export",
    "validate_read_options",
]
