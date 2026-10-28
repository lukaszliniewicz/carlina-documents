"""Small durable hand-off marker for restart-required lifecycle changes."""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from filelock import FileLock

_LOCK_TIMEOUT_SECONDS = 15
_MAX_MARKER_BYTES = 64 * 1024
_MAX_REASON_COUNT = 64
_MAX_REASON_LENGTH = 256
_MAX_HANDOFF_ID_LENGTH = 128
_MARKER_FORMAT = "carlina.restart-required"
_MARKER_VERSION = 2
_PENDING_STATUSES = {"pending", "restart_requested", "unknown"}


def _path(paths) -> Path:
    return Path(paths.state_dir) / "restart-required.json"


def _lock_path(paths) -> Path:
    """Return the bounded sibling lock shared by every marker transition."""

    marker = _path(paths)
    return marker.with_name(f".{marker.name}.lock")


def _prepare_lock_path(lock_path: Path) -> None:
    """Create a non-empty lock sentinel for FileLock on every supported OS."""

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with lock_path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
    except PermissionError:
        # A held Windows FileLock can deny a second write-open. Its subsequent
        # acquisition remains authoritative, so do not weaken the lock here.
        pass


@contextmanager
def _restart_lock(paths) -> Iterator[None]:
    """Serialize marker reads and transitions across app processes."""

    lock_path = _lock_path(paths)
    _prepare_lock_path(lock_path)
    with FileLock(str(lock_path), timeout=_LOCK_TIMEOUT_SECONDS):
        yield


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(16)}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_parent(path)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_parent(path: Path) -> None:
    """Persist a rename/unlink on POSIX; directory syncing is best-effort on Windows."""

    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path.parent, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _bounded_reasons(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        str(item)[:_MAX_REASON_LENGTH]
        for item in value[:_MAX_REASON_COUNT]
        if str(item)
    ]


def _handoff_id(value: object) -> str | None:
    candidate = str(value or "")
    if not 24 <= len(candidate) <= _MAX_HANDOFF_ID_LENGTH:
        return None
    if not all(
        character.isascii() and (character.isalnum() or character in "-_")
        for character in candidate
    ):
        return None
    return candidate


def _load_restart_state_unlocked(paths) -> dict | None:
    """Read the durable state while its shared lifecycle lock is already held."""

    marker = _path(paths)
    try:
        if marker.stat().st_size > _MAX_MARKER_BYTES:
            return {"status": "unknown", "reasons": ["lifecycle"]}
        value = json.loads(marker.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except OSError, json.JSONDecodeError:
        return {"status": "unknown", "reasons": ["lifecycle"]}
    if not isinstance(value, dict) or value.get("format") != _MARKER_FORMAT:
        return {"status": "unknown", "reasons": ["lifecycle"]}
    status = str(value.get("status") or "pending")
    if status not in {"pending", "restart_requested", "completed"}:
        return {"status": "unknown", "reasons": ["lifecycle"]}
    completed_handoff_id = _handoff_id(value.get("completed_handoff_id"))
    handoff_id = _handoff_id(value.get("handoff_id"))
    if status == "completed" and completed_handoff_id is None:
        return {"status": "unknown", "reasons": ["lifecycle"]}
    return {
        "status": status,
        "reasons": _bounded_reasons(value.get("reasons")),
        "handoff_id": handoff_id,
        "completed_handoff_id": completed_handoff_id,
    }


def _public_state(state: dict) -> dict:
    status = str(state.get("status") or "unknown")
    return {
        "pending": status in _PENDING_STATUSES,
        "status": status,
        "reasons": list(state.get("reasons") or ()),
        "completed_handoff_id": state.get("completed_handoff_id"),
    }


def _write_state(
    paths,
    *,
    status: str,
    reasons: list[str],
    handoff_id: str | None = None,
    completed_handoff_id: str | None = None,
) -> None:
    payload = {
        "format": _MARKER_FORMAT,
        "version": _MARKER_VERSION,
        "status": status,
        "reasons": reasons,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    if handoff_id is not None:
        payload["handoff_id"] = handoff_id
    if completed_handoff_id is not None:
        payload["completed_handoff_id"] = completed_handoff_id
    _atomic_json(_path(paths), payload)


def pending_restart(paths) -> dict | None:
    """Return bounded public handoff state, treating corrupt state as pending."""

    with _restart_lock(paths):
        state = _load_restart_state_unlocked(paths)
        return _public_state(state) if state is not None else None


def require_restart(paths, *, reason: str) -> None:
    with _restart_lock(paths):
        existing = _load_restart_state_unlocked(paths) or {}
        reasons: set[str] = set(existing.get("reasons") or ())
        if reason:
            reasons.add(str(reason)[:_MAX_REASON_LENGTH])
        # Any lifecycle transition invalidates an active request. Retaining the
        # last completed handoff lets its initiating browser reload and then
        # discover this new pending change.
        _write_state(
            paths,
            status="pending",
            reasons=sorted(reasons)[:_MAX_REASON_COUNT],
            completed_handoff_id=existing.get("completed_handoff_id"),
        )


def request_restart(paths) -> str:
    """Durably request a restart and return its cryptographically random ID."""

    with _restart_lock(paths):
        existing = _load_restart_state_unlocked(paths) or {}
        handoff_id = secrets.token_urlsafe(32)
        _write_state(
            paths,
            status="restart_requested",
            reasons=list(existing.get("reasons") or ()),
            handoff_id=handoff_id,
            completed_handoff_id=existing.get("completed_handoff_id"),
        )
        return handoff_id


def confirm_healthy_successor(paths) -> bool:
    """Mark the currently requested handoff complete after successor startup."""

    with _restart_lock(paths):
        existing = _load_restart_state_unlocked(paths)
        handoff_id = existing.get("handoff_id") if existing is not None else None
        if (
            existing is None
            or existing.get("status") != "restart_requested"
            or handoff_id is None
        ):
            return False
        _write_state(
            paths,
            status="completed",
            reasons=[],
            completed_handoff_id=handoff_id,
        )
        return True
