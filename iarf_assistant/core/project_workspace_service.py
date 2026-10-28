from __future__ import annotations

import atexit
import codecs
import difflib
import functools
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from carlina.runtime_environment import (
    canonical_environment_name,
    legacy_environment_name,
    set_product_environment,
)

PROJECT_WORKSPACE_PROFILE_ID = "project.workspace"
PROJECT_WORKSPACE_ROOT_SUFFIX = "PROJECT_WORKSPACE_ROOT"
PROJECT_WORKSPACE_ROOT_ENVIRONMENT_NAMES = frozenset(
    {
        canonical_environment_name(PROJECT_WORKSPACE_ROOT_SUFFIX),
        legacy_environment_name(PROJECT_WORKSPACE_ROOT_SUFFIX),
    }
)
MAX_EDIT_CHARS = 2_000_000
MAX_READ_CHARS = 500_000
MAX_ACTIONS = 500
MAX_COMMAND_ARGS = 100
MAX_COMMAND_ARG_CHARS = 8_000
MAX_COMMAND_TIMEOUT_SECONDS = 3_600
DEFAULT_EXECUTABLES = {
    "cargo",
    "cargo.exe",
    "dotnet",
    "dotnet.exe",
    "git",
    "git.exe",
    "go",
    "go.exe",
    "gradle",
    "gradle.bat",
    "java",
    "java.exe",
    "mvn",
    "mvn.cmd",
    "node",
    "node.exe",
    "npm",
    "npm.cmd",
    "npx",
    "npx.cmd",
    "pixi",
    "pixi.exe",
    "pnpm",
    "pnpm.cmd",
    "py",
    "py.exe",
    "pytest",
    "pytest.exe",
    "python",
    "python.exe",
    "ruff",
    "ruff.exe",
    "yarn",
    "yarn.cmd",
}
FORBIDDEN_SHELLS = {
    "bash",
    "bash.exe",
    "cmd",
    "cmd.exe",
    "command.com",
    "dash",
    "fish",
    "powershell",
    "powershell.exe",
    "pwsh",
    "pwsh.exe",
    "sh",
    "zsh",
}
SENSITIVE_ENV_FRAGMENTS = {
    "AUTH",
    "COOKIE",
    "CREDENTIAL",
    "KEY",
    "PASSWORD",
    "SECRET",
    "SESSION",
    "TOKEN",
}
SAFE_INHERITED_ENV = {
    "ALLUSERSPROFILE",
    "CI",
    "COLORTERM",
    "COMMONPROGRAMFILES",
    "COMMONPROGRAMFILES(X86)",
    "COMMONPROGRAMW6432",
    "COMPUTERNAME",
    "LANG",
    "LC_ALL",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "PATH",
    "PATHEXT",
    "PROCESSOR_ARCHITECTURE",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMW6432",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TERM",
    "TMP",
    "TMPDIR",
    "WINDIR",
}


_PROCESS_LOCK = threading.RLock()
_SUPERVISORS: dict[str, subprocess.Popen] = {}
_WORKSPACE_LOCKS_GUARD = threading.RLock()
_WORKSPACE_LOCKS: dict[str, threading.RLock] = {}


def _close_supervisors() -> None:
    with _PROCESS_LOCK:
        supervisors = list(_SUPERVISORS.values())
        _SUPERVISORS.clear()
    for supervisor in supervisors:
        try:
            if supervisor.stdin:
                supervisor.stdin.close()
        except OSError:
            pass


atexit.register(_close_supervisors)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00",
        "Z",
    )


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{uuid.uuid4().hex[:8]}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_text_preview(
    path: Path,
    limit: int,
) -> tuple[str, bool, int, str]:
    """Read a bounded text prefix while hashing the complete file."""
    size = max(1_000, min(int(limit or 100_000), MAX_READ_CHARS))
    digest = hashlib.sha256()
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    content_parts: list[str] = []
    content_chars = 0
    total_bytes = 0
    prefix_bytes_remaining = 8_192
    truncated = False

    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            total_bytes += len(chunk)
            digest.update(chunk)
            if prefix_bytes_remaining:
                prefix = chunk[:prefix_bytes_remaining]
                if b"\x00" in prefix:
                    raise ValueError(
                        "Binary files cannot be read through the text inspector."
                    )
                prefix_bytes_remaining -= len(prefix)
            if truncated:
                continue
            decoded = decoder.decode(chunk, final=False)
            remaining = size + 1 - content_chars
            if remaining <= 0:
                truncated = True
                continue
            content_parts.append(decoded[:remaining])
            content_chars += min(len(decoded), remaining)
            if len(decoded) > remaining or content_chars > size:
                truncated = True

    if not truncated:
        decoded = decoder.decode(b"", final=True)
        remaining = size + 1 - content_chars
        content_parts.append(decoded[:remaining])
        content_chars += min(len(decoded), remaining)
        if len(decoded) > remaining or content_chars > size:
            truncated = True

    content = "".join(content_parts)
    if len(content) > size:
        content = content[:size]
        truncated = True
    return content, truncated, total_bytes, digest.hexdigest()


def _safe_segment(value: Any, *, fallback: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip(".-")
    return text[:100] or fallback


def _is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(path), str(root))) == str(root)
    except ValueError:
        return False


def _preview_file(path: Path, limit: int) -> tuple[str, bool, int]:
    if not path.is_file():
        return "", False, 0
    total_bytes = path.stat().st_size
    size = max(1_000, min(int(limit or 20_000), 200_000))
    if total_bytes <= size * 4:
        text = path.read_bytes().decode("utf-8", errors="replace")
        if len(text) <= size:
            return text, False, total_bytes
    marker = "\n...[terminal output compacted; full log is an artifact]...\n"
    half = max(1, (size - len(marker)) // 2)
    byte_window = max(4_096, half * 4)
    with path.open("rb") as handle:
        head = handle.read(byte_window)
        handle.seek(max(0, total_bytes - byte_window))
        tail = handle.read(byte_window)
    head_text = head.decode("utf-8", errors="replace")
    tail_text = tail.decode("utf-8", errors="replace")
    return (
        head_text[:half] + marker + tail_text[-half:],
        True,
        total_bytes,
    )


def _which_from_explicit_path(command: str) -> Path | None:
    """Resolve a bare command from absolute PATH entries without cwd precedence."""
    name = Path(str(command or "")).name
    if not name:
        return None
    candidates = [name]
    if os.name == "nt":
        extensions = [
            value
            for value in (
                os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD"
            ).split(os.pathsep)
            if value
        ]
        if not any(name.lower().endswith(value.lower()) for value in extensions):
            candidates = [f"{name}{value}" for value in extensions]
    for raw_directory in (os.environ.get("PATH") or "").split(os.pathsep):
        directory_text = raw_directory.strip().strip('"')
        if not directory_text:
            continue
        directory = Path(directory_text).expanduser()
        if not directory.is_absolute():
            continue
        for candidate_name in candidates:
            candidate = (directory / candidate_name).resolve(strict=False)
            if candidate.is_file() and (
                os.name == "nt" or os.access(candidate, os.X_OK)
            ):
                return candidate
    return None


def _serialized_workspace_call(method):
    @functools.wraps(method)
    def wrapped(self, workspace_id, *args, **kwargs):
        key = f"{self.run_id}:{str(workspace_id or '').strip()}"
        with _WORKSPACE_LOCKS_GUARD:
            lock = _WORKSPACE_LOCKS.setdefault(key, threading.RLock())
        with lock:
            return method(self, workspace_id, *args, **kwargs)

    return wrapped


class ProjectWorkspaceService:
    """Root-bound project inspection, structured editing, and supervised commands."""

    def __init__(
        self,
        *,
        instance_path: str | Path,
        owner_user_id: int | None,
        run_id: str,
        step_id: str | None = None,
        allowed_roots: Any,
        artifact_registrar: Callable[..., Any] | None = None,
        event_emitter: Callable[[str, dict[str, Any]], Any] | None = None,
        python_executable: str | Path | None = None,
        executable_allowlist: Any = None,
    ):
        self.instance_path = Path(instance_path).resolve()
        self.owner_user_id = owner_user_id
        self.run_id = str(run_id)
        self.step_id = str(step_id) if step_id else None
        self.allowed_roots = [
            Path(value).expanduser().resolve()
            for value in (allowed_roots or [])
            if str(value or "").strip()
        ]
        self.artifact_registrar = artifact_registrar
        self.event_emitter = event_emitter
        self.python_executable = Path(python_executable or sys.executable).resolve()
        self.executable_allowlist = {
            str(value).strip().lower()
            for value in (executable_allowlist or DEFAULT_EXECUTABLES)
            if str(value or "").strip()
        }
        run_segment = _safe_segment(self.run_id, fallback="run")
        if run_segment != self.run_id:
            raise ValueError("run_id contains unsupported path characters.")
        self.workspaces_root = (
            self.instance_path
            / "artifacts"
            / "workflow_runs"
            / run_segment
            / "project_workspaces"
        )

    def create(
        self,
        *,
        workspace_root: Any,
        label: str | None = None,
        access: str = "read_write",
    ) -> dict[str, Any]:
        root = Path(str(workspace_root or "")).expanduser()
        if not root.is_absolute():
            raise ValueError("workspace_root must be an absolute path.")
        root = root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("workspace_root must identify an existing directory.")
        if not self.allowed_roots:
            raise PermissionError("No project workspace roots are configured.")
        if not any(_is_within(root, allowed) for allowed in self.allowed_roots):
            raise PermissionError(
                "workspace_root is outside the configured project workspace roots."
            )
        normalized_access = str(access or "read_write").strip().lower()
        if normalized_access not in {"read_only", "read_write"}:
            raise ValueError("access must be read_only or read_write.")

        workspace_id = uuid.uuid4().hex[:24]
        storage_root = self.workspaces_root / workspace_id
        (storage_root / "actions").mkdir(parents=True)
        now = _utc_iso()
        manifest = {
            "version": 1,
            "workspace_id": workspace_id,
            "runtime_profile_id": PROJECT_WORKSPACE_PROFILE_ID,
            "label": str(label or root.name or "Project workspace")[:255],
            "owner_user_id": self.owner_user_id,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "workspace_root": str(root),
            "access": normalized_access,
            "status": "ready",
            "created_at": now,
            "updated_at": now,
            "actions": [],
        }
        _write_json(storage_root / "manifest.json", manifest)
        self._emit(
            "project_workspace.created",
            {
                "workspace_id": workspace_id,
                "status": "ready",
                "access": normalized_access,
                "workspace_root": str(root),
            },
        )
        return self._public_workspace(manifest)

    @_serialized_workspace_call
    def get(self, workspace_id: Any) -> dict[str, Any]:
        storage_root, manifest = self._load_workspace(workspace_id)
        changed = self._refresh_all_processes(storage_root, manifest)
        if changed:
            self._save_manifest(storage_root, manifest)
        return self._public_workspace(manifest)

    @_serialized_workspace_call
    def inspect(
        self,
        workspace_id: Any,
        *,
        operation: str,
        path: Any = "",
        pattern: str = "*",
        max_entries: int = 200,
        max_depth: int = 4,
        max_chars: int = 100_000,
        include_hidden: bool = False,
    ) -> dict[str, Any]:
        storage_root, manifest = self._load_workspace(workspace_id)
        self._require_open(manifest)
        root = Path(manifest["workspace_root"]).resolve(strict=True)
        target, relative = self._resolve_path(root, path, allow_root=True)
        action_id = uuid.uuid4().hex[:24]
        normalized = str(operation or "").strip().lower()
        if normalized not in {"list", "read", "stat"}:
            raise ValueError("operation must be list, read, or stat.")
        started_at = _utc_iso()

        if normalized == "read":
            if not target.is_file():
                raise ValueError("The requested path is not a file.")
            limit = max(1_000, min(int(max_chars or 100_000), MAX_READ_CHARS))
            text, truncated, size_bytes, sha256 = _read_text_preview(
                target,
                limit,
            )
            payload = {
                "path": relative,
                "kind": "file",
                "size_bytes": size_bytes,
                "sha256": sha256,
                "content": text,
                "content_truncated": truncated,
            }
        elif normalized == "stat":
            if not target.exists():
                raise FileNotFoundError(relative or ".")
            payload = self._path_metadata(root, target)
        else:
            if not target.is_dir():
                raise ValueError("The requested path is not a directory.")
            limit = max(1, min(int(max_entries or 200), 2_000))
            depth_limit = max(0, min(int(max_depth or 4), 20))
            rows = []
            for candidate in target.rglob(str(pattern or "*")):
                resolved = candidate.resolve(strict=False)
                if not _is_within(resolved, root):
                    continue
                candidate_relative = candidate.relative_to(root)
                if len(candidate_relative.parts) - len(Path(relative).parts) > depth_limit:
                    continue
                if not include_hidden and any(
                    part.startswith(".") for part in candidate_relative.parts
                ):
                    continue
                rows.append(self._path_metadata(root, candidate))
                if len(rows) >= limit:
                    break
            payload = {
                "path": relative,
                "kind": "directory",
                "pattern": str(pattern or "*"),
                "entries": rows,
                "entry_count": len(rows),
                "entries_truncated": len(rows) >= limit,
            }

        action = {
            "id": action_id,
            "kind": f"inspect.{normalized}",
            "status": "completed",
            "path": relative,
            "started_at": started_at,
            "finished_at": _utc_iso(),
            "summary": {
                key: value
                for key, value in payload.items()
                if key not in {"content", "entries"}
            },
        }
        self._append_action(manifest, action)
        self._save_manifest(storage_root, manifest)
        self._emit(
            "project_workspace.inspection.finished",
            {
                "workspace_id": manifest["workspace_id"],
                "action_id": action_id,
                "status": "completed",
                "operation": normalized,
                "path": relative,
                "entry_count": payload.get("entry_count"),
                "content_truncated": payload.get("content_truncated"),
            },
        )
        return {
            "workspace": self._public_workspace(manifest),
            "inspection": {
                "id": action_id,
                "operation": normalized,
                **payload,
            },
        }

    @_serialized_workspace_call
    def edit(
        self,
        workspace_id: Any,
        *,
        path: Any,
        content: Any = None,
        replacements: Any = None,
        expected_sha256: str | None = None,
        create: bool = False,
    ) -> dict[str, Any]:
        storage_root, manifest = self._load_workspace(workspace_id)
        self._require_open(manifest)
        if manifest.get("access") != "read_write":
            raise PermissionError("This project workspace is read-only.")
        root = Path(manifest["workspace_root"]).resolve(strict=True)
        target, relative = self._resolve_path(root, path, allow_root=False)
        if target.exists() and not target.is_file():
            raise ValueError("The edit target is not a file.")
        existed = target.is_file()
        if not existed and not create:
            raise FileNotFoundError(relative)
        before = (
            target.read_bytes().decode("utf-8", errors="strict")
            if existed
            else ""
        )
        before_bytes = before.encode("utf-8")
        before_hash = _sha256_bytes(before_bytes)
        expected = str(expected_sha256 or "").strip().lower()
        if expected and expected != before_hash:
            raise ValueError(
                "expected_sha256 does not match the current file; refresh before editing."
            )

        if content is not None and replacements:
            raise ValueError("Provide content or replacements, not both.")
        if content is not None:
            after = str(content)
        else:
            rows = replacements if isinstance(replacements, list) else []
            if not rows:
                raise ValueError("content or at least one replacement is required.")
            if len(rows) > 100:
                raise ValueError("At most 100 replacements are allowed per edit.")
            after = before
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("Each replacement must be an object.")
                old = str(row.get("old") or "")
                new = str(row.get("new") or "")
                expected_occurrences = int(row.get("expected_occurrences") or 1)
                occurrences = after.count(old)
                if not old or occurrences != expected_occurrences:
                    raise ValueError(
                        "Replacement precondition failed: expected "
                        f"{expected_occurrences} occurrence(s), found {occurrences}."
                    )
                after = after.replace(old, new, expected_occurrences)
        if len(after) > MAX_EDIT_CHARS:
            raise ValueError(
                f"Edited content exceeds the {MAX_EDIT_CHARS} character limit."
            )

        action_id = uuid.uuid4().hex[:24]
        action_root = storage_root / "actions" / action_id
        action_root.mkdir(parents=True)
        diff_text = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{relative}",
                tofile=f"b/{relative}",
            )
        )
        diff_path = action_root / "change.diff"
        diff_path.write_text(diff_text, encoding="utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.tmp")
        temporary.write_bytes(after.encode("utf-8"))
        temporary.replace(target)
        after_hash = _sha256_file(target)
        diff_preview = diff_text
        diff_truncated = False
        if len(diff_preview) > 20_000:
            diff_preview = diff_preview[:20_000] + "\n...[diff preview truncated]..."
            diff_truncated = True
        artifact_ids = []
        if callable(self.artifact_registrar):
            artifact = self.artifact_registrar(
                path=diff_path,
                label=f"Project edit: {relative}",
                metadata={
                    "kind": "project_workspace_diff",
                    "workspace_id": manifest["workspace_id"],
                    "action_id": action_id,
                    "path": relative,
                    "before_sha256": before_hash,
                    "after_sha256": after_hash,
                },
                review_status="draft",
                action_id=f"{action_id}:diff",
            )
            artifact_id = (
                artifact.get("id")
                if isinstance(artifact, dict)
                else getattr(artifact, "id", None)
            )
            if artifact_id:
                artifact_ids.append(artifact_id)
        action = {
            "id": action_id,
            "kind": "file.edit",
            "status": "completed",
            "path": relative,
            "created": not existed,
            "before_sha256": before_hash,
            "after_sha256": after_hash,
            "diff_path": str(diff_path),
            "artifact_ids": artifact_ids,
            "started_at": _utc_iso(),
            "finished_at": _utc_iso(),
        }
        self._append_action(manifest, action)
        self._save_manifest(storage_root, manifest)
        self._emit(
            "project_workspace.edit.finished",
            {
                "workspace_id": manifest["workspace_id"],
                "action_id": action_id,
                "status": "completed",
                "path": relative,
                "created": not existed,
                "before_sha256": before_hash,
                "after_sha256": after_hash,
                "artifact_ids": artifact_ids,
            },
        )
        return {
            "workspace": self._public_workspace(manifest),
            "edit": {
                **action,
                "diff": diff_preview,
                "diff_truncated": diff_truncated,
            },
        }

    @_serialized_workspace_call
    def execute(
        self,
        workspace_id: Any,
        *,
        argv: Any,
        cwd: Any = "",
        environment: Any = None,
        timeout_seconds: int = 300,
        background: bool = False,
        max_output_chars: int = 20_000,
        allow_network: bool = False,
        allow_dependency_changes: bool = False,
        allow_destructive: bool = False,
    ) -> dict[str, Any]:
        storage_root, manifest = self._load_workspace(workspace_id)
        self._require_open(manifest)
        if manifest.get("access") != "read_write":
            raise PermissionError(
                "Project commands require a read-write workspace because tools "
                "and tests may create caches or build outputs."
            )
        root = Path(manifest["workspace_root"]).resolve(strict=True)
        working_directory, relative_cwd = self._resolve_path(
            root,
            cwd,
            allow_root=True,
        )
        if not working_directory.is_dir():
            raise ValueError("cwd must identify a directory inside the workspace.")
        command = self._validate_command(root, working_directory, argv)
        classification = self.classify_command(command)
        missing_declarations = []
        if classification["network"] and not allow_network:
            missing_declarations.append("allow_network")
        if classification["dependency_change"] and not allow_dependency_changes:
            missing_declarations.append("allow_dependency_changes")
        if classification["destructive"] and not allow_destructive:
            missing_declarations.append("allow_destructive")
        if missing_declarations:
            raise PermissionError(
                "Command intent requires explicit declaration(s): "
                + ", ".join(missing_declarations)
                + "."
            )
        timeout = max(
            1,
            min(
                int(timeout_seconds or 300),
                MAX_COMMAND_TIMEOUT_SECONDS,
            ),
        )
        output_limit = max(1_000, min(int(max_output_chars or 20_000), 200_000))
        action_id = uuid.uuid4().hex[:24]
        action_root = storage_root / "actions" / action_id
        action_root.mkdir(parents=True)
        stdout_path = action_root / "stdout.log"
        stderr_path = action_root / "stderr.log"
        control_path = action_root / "control.stop"
        result_path = action_root / "process_result.json"
        spec_path = action_root / "process_spec.json"
        sanitized_environment, inherited_keys, override_keys = self._environment(
            root,
            environment,
            allow_network=allow_network,
            runtime_home=action_root / "home",
        )
        spec = {
            "version": 1,
            "argv": command,
            "cwd": str(working_directory),
            "environment": sanitized_environment,
            "timeout_seconds": timeout,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "control_path": str(control_path),
        }
        _write_json(spec_path, spec)
        supervisor = subprocess.Popen(
            [
                str(self.python_executable),
                "-I",
                str(Path(__file__).with_name("project_process_supervisor.py")),
                str(spec_path),
                str(result_path),
            ],
            cwd=str(action_root),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if os.name == "nt"
                else 0
            ),
        )
        process_key = self._process_key(manifest["workspace_id"], action_id)
        with _PROCESS_LOCK:
            _SUPERVISORS[process_key] = supervisor
        threading.Thread(
            target=self._reap_supervisor,
            args=(process_key, supervisor),
            daemon=True,
        ).start()
        action = {
            "id": action_id,
            "kind": "command.execute",
            "runtime_profile_id": PROJECT_WORKSPACE_PROFILE_ID,
            "status": "running",
            "argv": command,
            "cwd": relative_cwd,
            "background": bool(background),
            "timeout_seconds": timeout,
            "max_output_chars": output_limit,
            "allow_network": bool(allow_network),
            "allow_dependency_changes": bool(allow_dependency_changes),
            "allow_destructive": bool(allow_destructive),
            "classification": classification,
            "environment": {
                "inherited_keys": inherited_keys,
                "override_keys": override_keys,
                "credentials_removed": True,
            },
            "supervisor_pid": supervisor.pid,
            "started_at": _utc_iso(),
            "result_path": str(result_path),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "control_path": str(control_path),
            "completion_event_emitted": False,
            "artifacts_registered": False,
        }
        self._append_action(manifest, action)
        manifest["status"] = "running"
        self._save_manifest(storage_root, manifest)
        self._emit(
            "project_workspace.command.started",
            {
                "workspace_id": manifest["workspace_id"],
                "action_id": action_id,
                "status": "running",
                "argv": command,
                "cwd": relative_cwd,
                "background": bool(background),
                "timeout_seconds": timeout,
                "classification": classification,
            },
        )

        if not background:
            try:
                supervisor.wait(timeout=timeout + 15)
            except subprocess.TimeoutExpired:
                control_path.write_text("supervisor_timeout", encoding="utf-8")
                supervisor.wait(timeout=10)
            finally:
                try:
                    if supervisor.stdin:
                        supervisor.stdin.close()
                except OSError:
                    pass
            with _PROCESS_LOCK:
                _SUPERVISORS.pop(process_key, None)
            self._refresh_process(storage_root, manifest, action)
            self._save_manifest(storage_root, manifest)
        return {
            "workspace": self._public_workspace(manifest),
            "process": self._public_process(action),
        }

    @_serialized_workspace_call
    def process(
        self,
        workspace_id: Any,
        *,
        operation: str = "get",
        process_id: Any = None,
    ) -> dict[str, Any]:
        storage_root, manifest = self._load_workspace(workspace_id)
        normalized = str(operation or "get").strip().lower()
        if normalized not in {"get", "list", "stop"}:
            raise ValueError("operation must be get, list, or stop.")
        command_actions = [
            row
            for row in manifest.get("actions") or []
            if isinstance(row, dict) and row.get("kind") == "command.execute"
        ]
        if normalized == "list":
            for action in command_actions:
                self._refresh_process(storage_root, manifest, action)
            self._save_manifest(storage_root, manifest)
            return {
                "workspace": self._public_workspace(manifest),
                "processes": [
                    self._public_process(action) for action in command_actions[-100:]
                ],
            }
        action = next(
            (
                row
                for row in command_actions
                if row.get("id") == str(process_id or "")
            ),
            None,
        )
        if action is None:
            raise KeyError(f"Project process '{process_id}' not found.")
        if normalized == "stop" and action.get("status") in {
            "planned",
            "running",
            "stopping",
        }:
            Path(action["control_path"]).write_text(
                "stop_requested",
                encoding="utf-8",
            )
            action["status"] = "stopping"
            action["stop_requested_at"] = _utc_iso()
            self._emit(
                "project_workspace.command.stop_requested",
                {
                    "workspace_id": manifest["workspace_id"],
                    "action_id": action["id"],
                    "status": "stopping",
                },
            )
            for _ in range(30):
                if Path(action["result_path"]).is_file():
                    break
                time.sleep(0.1)
        self._refresh_process(storage_root, manifest, action)
        self._save_manifest(storage_root, manifest)
        return {
            "workspace": self._public_workspace(manifest),
            "process": self._public_process(action),
        }

    @_serialized_workspace_call
    def close(self, workspace_id: Any) -> dict[str, Any]:
        storage_root, manifest = self._load_workspace(workspace_id)
        stopped = []
        for action in manifest.get("actions") or []:
            if (
                isinstance(action, dict)
                and action.get("kind") == "command.execute"
                and action.get("status") in {"planned", "running", "stopping"}
            ):
                Path(action["control_path"]).write_text(
                    "workspace_closed",
                    encoding="utf-8",
                )
                action["status"] = "stopping"
                stopped.append(action["id"])
        deadline = time.monotonic() + 5
        while stopped and time.monotonic() < deadline:
            remaining = []
            for action in manifest.get("actions") or []:
                if action.get("id") not in stopped:
                    continue
                self._refresh_process(storage_root, manifest, action)
                if action.get("status") in {"running", "stopping"}:
                    remaining.append(action["id"])
            stopped = remaining
            if stopped:
                time.sleep(0.1)
        manifest["status"] = "closed" if not stopped else "closing"
        manifest["closed_at"] = _utc_iso()
        self._save_manifest(storage_root, manifest)
        self._emit(
            "project_workspace.closed",
            {
                "workspace_id": manifest["workspace_id"],
                "status": manifest["status"],
                "remaining_process_ids": stopped,
            },
        )
        return self._public_workspace(manifest)

    @classmethod
    def classify_command(cls, argv: list[str]) -> dict[str, bool]:
        lowered = [str(value).strip().lower() for value in argv]
        executable = Path(lowered[0]).name if lowered else ""
        tail = lowered[1:]
        dependency_change = False
        network = False
        destructive = False

        if executable.startswith("python") or executable in {"py", "py.exe"}:
            if len(tail) >= 2 and tail[0] == "-m" and tail[1] in {
                "pip",
                "pip3",
            }:
                dependency_change = bool(
                    set(tail[2:])
                    & {"install", "uninstall", "download", "wheel"}
                )
        if executable in {"pip", "pip.exe", "pip3", "pip3.exe", "uv", "uv.exe"}:
            dependency_change = bool(
                set(tail)
                & {"add", "install", "remove", "sync", "uninstall", "update"}
            )
        if executable in {
            "npm",
            "npm.cmd",
            "npx",
            "npx.cmd",
            "pnpm",
            "pnpm.cmd",
            "yarn",
            "yarn.cmd",
        }:
            dependency_change = bool(
                set(tail)
                & {"add", "ci", "install", "remove", "uninstall", "update", "upgrade"}
            )
            network = dependency_change or bool(
                set(tail) & {"login", "publish", "whoami"}
            )
        if executable in {"pixi", "pixi.exe"}:
            dependency_change = bool(
                set(tail) & {"add", "install", "remove", "update", "upgrade"}
            )
        if executable in {"cargo", "cargo.exe"}:
            dependency_change = bool(set(tail) & {"add", "install", "update"})
        if executable in {"curl", "curl.exe", "wget", "wget.exe", "ssh", "scp"}:
            network = True
        if executable in {"git", "git.exe"}:
            verb = cls._git_subcommand(tail)
            network = verb in {"clone", "fetch", "pull", "push", "ls-remote"}
            destructive = verb in {
                "checkout",
                "clean",
                "reset",
                "restore",
                "switch",
            }
        network = network or dependency_change
        return {
            "network": bool(network),
            "dependency_change": bool(dependency_change),
            "destructive": bool(destructive),
        }

    @staticmethod
    def _git_subcommand(arguments: list[str]) -> str:
        options_with_values = {
            "-c",
            "--attr-source",
            "--config-env",
            "--git-dir",
            "--namespace",
            "--super-prefix",
            "--work-tree",
        }
        index = 0
        while index < len(arguments):
            value = arguments[index]
            if value == "--":
                index += 1
                return arguments[index] if index < len(arguments) else ""
            if not value.startswith("-"):
                return value
            option = value.split("=", 1)[0]
            if option in options_with_values and "=" not in value:
                index += 2
            else:
                index += 1
        return ""

    def _validate_command(
        self,
        root: Path,
        cwd: Path,
        argv: Any,
    ) -> list[str]:
        if not isinstance(argv, list) or not argv:
            raise ValueError("argv must be a non-empty array of command arguments.")
        if len(argv) > MAX_COMMAND_ARGS:
            raise ValueError(f"argv may contain at most {MAX_COMMAND_ARGS} arguments.")
        command = [str(value) for value in argv]
        if any(
            not value or len(value) > MAX_COMMAND_ARG_CHARS for value in command
        ):
            raise ValueError("Command arguments must be non-empty and bounded.")
        executable_name = Path(command[0]).name.lower()
        if executable_name in FORBIDDEN_SHELLS:
            raise PermissionError(
                "Shell interpreters are not exposed; use an argv-based project command."
            )
        raw_executable = Path(command[0]).expanduser()
        has_explicit_path = (
            raw_executable.is_absolute()
            or raw_executable.parent != Path(".")
            or "/" in command[0]
            or "\\" in command[0]
        )
        if has_explicit_path:
            executable_path = (
                raw_executable.resolve(strict=False)
                if raw_executable.is_absolute()
                else (cwd / raw_executable).resolve(strict=False)
            )
        else:
            local_executable = (cwd / raw_executable).resolve(strict=False)
            discovered = _which_from_explicit_path(command[0])
            executable_path = (
                discovered if discovered is not None else local_executable
            )
        trusted_external = {
            self.python_executable,
        }
        discovered = _which_from_explicit_path(executable_name)
        if discovered is not None:
            trusted_external.add(discovered)
        if not _is_within(executable_path, root):
            if (
                executable_name not in self.executable_allowlist
                or executable_path not in trusted_external
            ):
                raise PermissionError(
                    f"Executable '{executable_name}' is not a trusted project runtime executable."
                )
        elif not executable_path.is_file():
            raise ValueError(f"Project executable '{command[0]}' does not exist.")
        command[0] = str(executable_path)
        if (
            executable_name.startswith("python")
            or executable_name in {"py", "py.exe"}
        ) and any(value in {"-c", "-"} for value in command[1:]):
            raise PermissionError(
                "Inline interpreter code is not allowed in the project profile; "
                "execute a workspace file or a declared module."
            )
        if executable_name in {"node", "node.exe"} and any(
            value in {"-e", "--eval", "-p", "--print"} for value in command[1:]
        ):
            raise PermissionError(
                "Inline interpreter code is not allowed in the project profile."
            )
        for value in command[1:]:
            candidate_text = value.split("=", 1)[-1] if "=" in value else value
            candidate = Path(candidate_text).expanduser()
            if candidate.is_absolute() and not _is_within(
                candidate.resolve(strict=False),
                root,
            ):
                raise PermissionError(
                    "Command arguments may not reference absolute paths outside "
                    "the project workspace."
                )
            if (
                (
                    candidate_text == ".."
                    or candidate_text.startswith(("..\\", "../"))
                )
                and not _is_within((cwd / candidate).resolve(strict=False), root)
            ):
                raise PermissionError(
                    "Command arguments may not traverse outside the project workspace."
                )
        return command

    def _environment(
        self,
        root: Path,
        overrides: Any,
        *,
        allow_network: bool,
        runtime_home: Path,
    ) -> tuple[dict[str, str], list[str], list[str]]:
        runtime_home.mkdir(parents=True, exist_ok=True)
        runtime_config = runtime_home / "config"
        runtime_cache = runtime_home / "cache"
        runtime_appdata = runtime_home / "AppData" / "Roaming"
        runtime_local_appdata = runtime_home / "AppData" / "Local"
        for directory in (
            runtime_config,
            runtime_cache,
            runtime_appdata,
            runtime_local_appdata,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        inherited = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in SAFE_INHERITED_ENV
            and not any(fragment in key.upper() for fragment in SENSITIVE_ENV_FRAGMENTS)
        }
        set_product_environment(
            inherited,
            PROJECT_WORKSPACE_ROOT_SUFFIX,
            root,
            include_legacy=True,
        )
        inherited["PYTHONUNBUFFERED"] = "1"
        inherited["PYTHONNOUSERSITE"] = "1"
        inherited["GIT_TERMINAL_PROMPT"] = "0"
        inherited["HOME"] = str(runtime_home)
        inherited["USERPROFILE"] = str(runtime_home)
        inherited["APPDATA"] = str(runtime_appdata)
        inherited["LOCALAPPDATA"] = str(runtime_local_appdata)
        inherited["XDG_CONFIG_HOME"] = str(runtime_config)
        inherited["XDG_CACHE_HOME"] = str(runtime_cache)
        if not allow_network:
            inherited.update(
                {
                    "PIP_NO_INDEX": "1",
                    "UV_OFFLINE": "1",
                    "NPM_CONFIG_OFFLINE": "true",
                    "YARN_ENABLE_NETWORK": "0",
                    "CARGO_NET_OFFLINE": "true",
                }
            )
        override_rows = overrides if isinstance(overrides, dict) else {}
        override_keys = []
        for raw_key, raw_value in override_rows.items():
            key = str(raw_key or "").strip()
            upper = key.upper()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", key):
                raise ValueError(f"Invalid environment key '{key}'.")
            if any(fragment in upper for fragment in SENSITIVE_ENV_FRAGMENTS):
                raise PermissionError(
                    f"Credential-like environment key '{key}' is not allowed."
                )
            if upper in PROJECT_WORKSPACE_ROOT_ENVIRONMENT_NAMES:
                raise PermissionError(
                    f"Environment key '{key}' is reserved by the project runtime."
                )
            if upper in {
                "APPDATA",
                "HOME",
                "LD_PRELOAD",
                "LOCALAPPDATA",
                "PATH",
                "PATHEXT",
                "PYTHONSTARTUP",
                "PYTHONHOME",
                "PYTHONPATH",
                "NODE_OPTIONS",
                "GIT_SSH_COMMAND",
                "SYSTEMROOT",
                "USERPROFILE",
                "WINDIR",
                "XDG_CONFIG_HOME",
            }:
                raise PermissionError(
                    f"Environment key '{key}' can bypass the project runtime boundary."
                )
            value = str(raw_value)
            if len(value) > 8_000:
                raise ValueError(f"Environment value '{key}' is too large.")
            inherited[key] = value
            override_keys.append(key)
        inherited_keys = sorted(
            key
            for key in inherited
            if key not in set(override_keys)
            and not key.startswith(("CARLINA_", "IARF_"))
        )
        return inherited, inherited_keys, sorted(override_keys)

    def _resolve_path(
        self,
        root: Path,
        value: Any,
        *,
        allow_root: bool,
    ) -> tuple[Path, str]:
        raw = str(value or "").strip()
        relative = Path(raw.replace("\\", "/")) if raw else Path(".")
        if relative.is_absolute():
            raise PermissionError("Project paths must be relative to the workspace root.")
        target = (root / relative).resolve(strict=False)
        if not _is_within(target, root):
            raise PermissionError("Project path escapes the workspace root.")
        normalized = target.relative_to(root).as_posix()
        if normalized == ".":
            normalized = ""
        if not allow_root and not normalized:
            raise PermissionError("The workspace root itself is not a valid file target.")
        return target, normalized

    def _path_metadata(self, root: Path, path: Path) -> dict[str, Any]:
        resolved = path.resolve(strict=False)
        if not _is_within(resolved, root):
            raise PermissionError("Project path escapes the workspace root.")
        stat = path.stat()
        return {
            "path": path.relative_to(root).as_posix(),
            "kind": "directory" if path.is_dir() else "file",
            "size_bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(
                stat.st_mtime,
                timezone.utc,
            ).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "sha256": _sha256_file(path) if path.is_file() else None,
        }

    def _refresh_all_processes(
        self,
        storage_root: Path,
        manifest: dict[str, Any],
    ) -> bool:
        changed = False
        for action in manifest.get("actions") or []:
            if (
                isinstance(action, dict)
                and action.get("kind") == "command.execute"
                and action.get("status") in {"planned", "running", "stopping"}
            ):
                changed = self._refresh_process(
                    storage_root,
                    manifest,
                    action,
                ) or changed
        return changed

    def _refresh_process(
        self,
        storage_root: Path,
        manifest: dict[str, Any],
        action: dict[str, Any],
    ) -> bool:
        result_path = Path(action["result_path"])
        if not result_path.is_file():
            process_key = self._process_key(
                manifest["workspace_id"],
                action["id"],
            )
            with _PROCESS_LOCK:
                supervisor = _SUPERVISORS.get(process_key)
            if supervisor is not None and supervisor.poll() is not None:
                action["status"] = "failed"
                action["error"] = (
                    "Project command supervisor exited without a result record."
                )
                action["finished_at"] = _utc_iso()
                return True
            return False
        result = _read_json(result_path, {})
        stdout, stdout_truncated, stdout_bytes = _preview_file(
            Path(action["stdout_path"]),
            action.get("max_output_chars") or 20_000,
        )
        stderr, stderr_truncated, stderr_bytes = _preview_file(
            Path(action["stderr_path"]),
            action.get("max_output_chars") or 20_000,
        )
        action.update(
            {
                "status": result.get("status") or "failed",
                "termination_reason": result.get("termination_reason"),
                "returncode": result.get("returncode"),
                "timed_out": bool(result.get("timed_out")),
                "pid": result.get("pid"),
                "finished_at": result.get("finished_at") or _utc_iso(),
                "duration_ms": result.get("duration_ms"),
                "error": result.get("error"),
                "stdout_preview": stdout,
                "stderr_preview": stderr,
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
                "stdout_bytes": stdout_bytes,
                "stderr_bytes": stderr_bytes,
            }
        )
        process_key = self._process_key(manifest["workspace_id"], action["id"])
        with _PROCESS_LOCK:
            supervisor = _SUPERVISORS.pop(process_key, None)
        if supervisor is not None:
            try:
                if supervisor.stdin:
                    supervisor.stdin.close()
            except OSError:
                pass
            try:
                supervisor.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if not action.get("artifacts_registered"):
            action["artifact_ids"] = self._register_process_artifacts(
                storage_root,
                manifest,
                action,
                result,
            )
            action["artifacts_registered"] = True
        if not action.get("completion_event_emitted"):
            self._emit(
                "project_workspace.command.finished",
                {
                    "workspace_id": manifest["workspace_id"],
                    "action_id": action["id"],
                    "status": action["status"],
                    "returncode": action.get("returncode"),
                    "timed_out": action.get("timed_out"),
                    "duration_ms": action.get("duration_ms"),
                    "stdout_preview": stdout,
                    "stderr_preview": stderr,
                    "stdout_truncated": stdout_truncated,
                    "stderr_truncated": stderr_truncated,
                    "artifact_ids": action.get("artifact_ids") or [],
                },
            )
            action["completion_event_emitted"] = True
        running = any(
            row.get("kind") == "command.execute"
            and row.get("status") in {"planned", "running", "stopping"}
            for row in manifest.get("actions") or []
            if isinstance(row, dict)
        )
        manifest["status"] = "running" if running else "ready"
        return True

    def _register_process_artifacts(
        self,
        storage_root: Path,
        manifest: dict[str, Any],
        action: dict[str, Any],
        result: dict[str, Any],
    ) -> list[str]:
        bundle_path = storage_root / "actions" / action["id"] / "execution_bundle.json"
        bundle = {
            "version": 1,
            "runtime_profile_id": PROJECT_WORKSPACE_PROFILE_ID,
            "workspace_id": manifest["workspace_id"],
            "workspace_root": manifest["workspace_root"],
            "action_id": action["id"],
            "argv": action["argv"],
            "cwd": action["cwd"],
            "policy": {
                "allow_network": action.get("allow_network"),
                "allow_dependency_changes": action.get(
                    "allow_dependency_changes"
                ),
                "allow_destructive": action.get("allow_destructive"),
                "classification": action.get("classification") or {},
            },
            "environment": action.get("environment") or {},
            "result": result,
            "output": {
                "stdout_bytes": action.get("stdout_bytes"),
                "stderr_bytes": action.get("stderr_bytes"),
                "stdout_sha256": (
                    _sha256_file(Path(action["stdout_path"]))
                    if Path(action["stdout_path"]).is_file()
                    else None
                ),
                "stderr_sha256": (
                    _sha256_file(Path(action["stderr_path"]))
                    if Path(action["stderr_path"]).is_file()
                    else None
                ),
            },
        }
        _write_json(bundle_path, bundle)
        if not callable(self.artifact_registrar):
            return []
        artifacts = [
            (
                bundle_path,
                f"Project command bundle: {action['id'][:8]}",
                "project_workspace_command_bundle",
                "bundle",
            )
        ]
        if Path(action["stdout_path"]).is_file():
            artifacts.append(
                (
                    Path(action["stdout_path"]),
                    f"Project command stdout: {action['id'][:8]}",
                    "project_workspace_stdout",
                    "stdout",
                )
            )
        if Path(action["stderr_path"]).is_file():
            artifacts.append(
                (
                    Path(action["stderr_path"]),
                    f"Project command stderr: {action['id'][:8]}",
                    "project_workspace_stderr",
                    "stderr",
                )
            )
        artifact_ids = []
        for path, label, kind, suffix in artifacts:
            artifact = self.artifact_registrar(
                path=path,
                label=label,
                metadata={
                    "kind": kind,
                    "workspace_id": manifest["workspace_id"],
                    "action_id": action["id"],
                    "status": action.get("status"),
                },
                review_status="draft",
                action_id=f"{action['id']}:{suffix}",
            )
            artifact_id = (
                artifact.get("id")
                if isinstance(artifact, dict)
                else getattr(artifact, "id", None)
            )
            if artifact_id:
                artifact_ids.append(artifact_id)
        return artifact_ids

    def _append_action(
        self,
        manifest: dict[str, Any],
        action: dict[str, Any],
    ) -> None:
        actions = [
            *(manifest.get("actions") or []),
            action,
        ]
        if len(actions) <= MAX_ACTIONS:
            manifest["actions"] = actions
            return

        protected_indexes = {
            index
            for index, row in enumerate(actions)
            if (
                isinstance(row, dict)
                and row.get("kind") == "command.execute"
                and row.get("status") in {"planned", "running", "stopping"}
            )
        }
        protected_indexes.add(len(actions) - 1)
        history_slots = max(0, MAX_ACTIONS - len(protected_indexes))
        history_indexes = [
            index
            for index in range(len(actions))
            if index not in protected_indexes
        ]
        retained_history = (
            history_indexes[-history_slots:]
            if history_slots
            else []
        )
        keep_indexes = protected_indexes | set(retained_history)
        manifest["actions"] = [
            row
            for index, row in enumerate(actions)
            if index in keep_indexes
        ]

    @staticmethod
    def _reap_supervisor(
        process_key: str,
        supervisor: subprocess.Popen,
    ) -> None:
        try:
            supervisor.wait()
        finally:
            with _PROCESS_LOCK:
                current = _SUPERVISORS.get(process_key)
                if current is supervisor:
                    _SUPERVISORS.pop(process_key, None)
            try:
                if supervisor.stdin:
                    supervisor.stdin.close()
            except OSError:
                pass

    def _save_manifest(
        self,
        storage_root: Path,
        manifest: dict[str, Any],
    ) -> None:
        manifest["updated_at"] = _utc_iso()
        _write_json(storage_root / "manifest.json", manifest)

    def _load_workspace(
        self,
        workspace_id: Any,
    ) -> tuple[Path, dict[str, Any]]:
        text = str(workspace_id or "").strip()
        if not re.fullmatch(r"[a-f0-9]{24}", text):
            raise ValueError("workspace_id is invalid.")
        storage_root = self.workspaces_root / text
        manifest = _read_json(storage_root / "manifest.json")
        if not isinstance(manifest, dict):
            raise KeyError(f"Project workspace '{text}' not found.")
        if manifest.get("run_id") != self.run_id:
            raise PermissionError("Project workspace belongs to a different run.")
        root = Path(manifest["workspace_root"]).resolve(strict=True)
        if not any(_is_within(root, allowed) for allowed in self.allowed_roots):
            raise PermissionError(
                "Project workspace root is no longer configured as allowed."
            )
        return storage_root, manifest

    def _require_open(self, manifest: dict[str, Any]) -> None:
        if manifest.get("status") in {"closed", "closing"}:
            raise PermissionError("Project workspace is closed.")

    def _public_workspace(self, manifest: dict[str, Any]) -> dict[str, Any]:
        actions = [
            self._public_process(row)
            if row.get("kind") == "command.execute"
            else {
                key: value
                for key, value in row.items()
                if not key.endswith("_path")
            }
            for row in (manifest.get("actions") or [])[-100:]
            if isinstance(row, dict)
        ]
        return {
            "id": manifest["workspace_id"],
            "runtime_profile_id": PROJECT_WORKSPACE_PROFILE_ID,
            "label": manifest.get("label"),
            "run_id": manifest.get("run_id"),
            "step_id": manifest.get("step_id"),
            "workspace_root": manifest.get("workspace_root"),
            "access": manifest.get("access"),
            "status": manifest.get("status"),
            "created_at": manifest.get("created_at"),
            "updated_at": manifest.get("updated_at"),
            "closed_at": manifest.get("closed_at"),
            "actions": actions,
            "action_count": len(manifest.get("actions") or []),
            "limitations": [
                "all direct file operations are resolved beneath the declared workspace root",
                "commands use argv arrays; shell interpreters and inline interpreter code are denied",
                "credential-like environment values are never inherited or accepted",
                "network, dependency, and destructive command intent must be explicitly declared",
                "background commands are supervised, timeout-bounded, stoppable, and parent-disconnect fenced",
            ],
        }

    def _public_process(self, action: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "id",
            "kind",
            "runtime_profile_id",
            "status",
            "argv",
            "cwd",
            "background",
            "timeout_seconds",
            "allow_network",
            "allow_dependency_changes",
            "allow_destructive",
            "classification",
            "environment",
            "supervisor_pid",
            "pid",
            "started_at",
            "finished_at",
            "stop_requested_at",
            "duration_ms",
            "termination_reason",
            "returncode",
            "timed_out",
            "error",
            "stdout_preview",
            "stderr_preview",
            "stdout_truncated",
            "stderr_truncated",
            "stdout_bytes",
            "stderr_bytes",
            "artifact_ids",
        }
        return {key: value for key, value in action.items() if key in allowed}

    def _process_key(self, workspace_id: str, action_id: str) -> str:
        return f"{self.run_id}:{workspace_id}:{action_id}"

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if callable(self.event_emitter):
            self.event_emitter(kind, payload)
