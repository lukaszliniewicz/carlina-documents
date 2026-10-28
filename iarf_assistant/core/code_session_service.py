from __future__ import annotations

import codecs
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Callable

from packaging.requirements import InvalidRequirement, Requirement

from carlina.runtime_environment import set_product_environment

MAX_SESSION_RESOURCES = 50
MAX_SESSION_RESOURCE_BYTES = 250 * 1024 * 1024
MAX_SESSION_CODE_CHARS = 200_000
MAX_HOST_ACTION_ROUNDS = 50
BASE_DEPENDENCIES = (
    "jsonschema",
    "numpy",
    "openpyxl",
    "pandas",
    "Pillow",
    "pypdf",
    "python-docx",
)
HOST_ACTION_PENDING_PREFIXES = (
    "CARLINA_HOST_ACTION_PENDING:",
    "IARF_HOST_ACTION_PENDING:",
)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the temporary basename short: real Windows instance paths can sit
    # close to the legacy MAX_PATH boundary before the execution id is added.
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


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_segment(value: Any, *, fallback: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip(".-")
    return text[:100] or fallback


def _bounded(value: str, limit: int) -> tuple[str, bool]:
    text = str(value or "")
    size = max(1, int(limit or 1))
    if len(text) <= size:
        return text, False
    marker = "\n...[truncated by governed code session]..."
    return text[: max(0, size - len(marker))] + marker, True


def _strip_host_action_pending_markers(value: Any) -> str:
    """Remove internal control-protocol lines from user-visible logs."""

    return "\n".join(
        line
        for line in str(value or "").splitlines()
        if not line.startswith(HOST_ACTION_PENDING_PREFIXES)
    )


def _run_bounded_process(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: float,
    max_output_chars: int,
) -> dict[str, Any]:
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        creationflags=creation_flags,
    )
    limit = max(1_000, int(max_output_chars or 1_000))
    marker = "\n...[truncated by governed code session]..."
    payload_limit = max(1, limit - len(marker))
    captured: dict[str, list[str]] = {"stdout": [], "stderr": []}
    counts = {"stdout": 0, "stderr": 0}
    truncated = {"stdout": False, "stderr": False}

    def drain(name: str, stream: Any) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            chunk = stream.read(8192)
            if not chunk:
                break
            text = decoder.decode(chunk)
            remaining = payload_limit - counts[name]
            if remaining > 0:
                captured[name].append(text[:remaining])
                counts[name] += min(len(text), remaining)
            if len(text) > remaining:
                truncated[name] = True
        tail = decoder.decode(b"", final=True)
        remaining = payload_limit - counts[name]
        if tail and remaining > 0:
            captured[name].append(tail[:remaining])
            counts[name] += min(len(tail), remaining)
        if tail and len(tail) > remaining:
            truncated[name] = True

    threads = [
        threading.Thread(
            target=drain,
            args=("stdout", process.stdout),
            daemon=True,
        ),
        threading.Thread(
            target=drain,
            args=("stderr", process.stderr),
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        returncode = process.wait(timeout=max(0.1, float(timeout_seconds)))
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        returncode = process.wait(timeout=10)
    finally:
        for thread in threads:
            thread.join(timeout=10)
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()
    return {
        "returncode": int(returncode),
        "timed_out": timed_out,
        "stdout": "".join(captured["stdout"]) + (
            marker if truncated["stdout"] else ""
        ),
        "stderr": "".join(captured["stderr"]) + (
            marker if truncated["stderr"] else ""
        ),
        "stdout_truncated": truncated["stdout"],
        "stderr_truncated": truncated["stderr"],
    }


def _base_dependency_versions() -> list[dict[str, Any]]:
    rows = []
    for name in BASE_DEPENDENCIES:
        try:
            version = importlib_metadata.version(name)
            rows.append({"name": name, "version": version, "available": True})
        except importlib_metadata.PackageNotFoundError:
            rows.append({"name": name, "version": None, "available": False})
    return rows


class CodeSessionService:
    """Filesystem-backed, resumable Python sessions with host-mediated actions."""

    def __init__(
        self,
        *,
        instance_path: str | Path,
        owner_user_id: int | None,
        run_id: str,
        step_id: str | None = None,
        resource_resolver: Callable[[Any], dict[str, Any]] | None = None,
        tool_executor: Callable[[list[dict[str, Any]], bool], Any] | None = None,
        agent_mapper: Callable[[dict[str, Any], str], Any] | None = None,
        artifact_registrar: Callable[..., Any] | None = None,
        event_emitter: Callable[[str, dict[str, Any]], Any] | None = None,
        python_executable: str | Path | None = None,
    ):
        self.instance_path = Path(instance_path).resolve()
        self.owner_user_id = owner_user_id
        self.run_id = str(run_id)
        self.step_id = str(step_id) if step_id else None
        self.resource_resolver = resource_resolver
        self.tool_executor = tool_executor
        self.agent_mapper = agent_mapper
        self.artifact_registrar = artifact_registrar
        self.event_emitter = event_emitter
        self.python_executable = Path(python_executable or sys.executable).resolve()
        run_segment = _safe_segment(self.run_id, fallback="run")
        if run_segment != self.run_id:
            raise ValueError("run_id contains unsupported path characters.")
        self.sessions_root = (
            self.instance_path
            / "artifacts"
            / "workflow_runs"
            / run_segment
            / "code_sessions"
        )

    def create(
        self,
        *,
        resources: Any = None,
        dependencies: Any = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        session_id = uuid.uuid4().hex[:24]
        root = self.sessions_root / session_id
        (root / "resources").mkdir(parents=True)
        (root / "outputs").mkdir()
        (root / "requests").mkdir()
        (root / "responses").mkdir()
        (root / "executions").mkdir()
        (root / "tmp").mkdir()
        shutil.copy2(Path(__file__).with_name("code_session_sdk.py"), root / "iarf_runtime.py")
        shutil.copy2(
            Path(__file__).with_name("code_session_bootstrap.py"),
            root / "bootstrap.py",
        )
        _write_json(root / "state.json", {})

        declared_resources = self._materialize_resources(root, resources)
        requested_dependencies = self._normalize_dependencies(dependencies)
        now = _utc_iso()
        manifest = {
            "version": 1,
            "session_id": session_id,
            "label": str(label or "Governed Python session")[:255],
            "owner_user_id": self.owner_user_id,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "status": "ready",
            "created_at": now,
            "updated_at": now,
            "environment": {
                "mechanism": "pixi",
                "profile_id": "python.session",
                "reuse": "application_locked_environment",
                "python_version": platform.python_version(),
                "base_dependencies": _base_dependency_versions(),
                "requested_dependencies": requested_dependencies,
                "dependency_status": (
                    "planned" if requested_dependencies else "base_environment"
                ),
                "dependency_changes": [],
            },
            "resources": declared_resources,
            "executions": [],
            "host_actions": {},
        }
        _write_json(root / "manifest.json", manifest)
        self._emit(
            "code_session.created",
            {
                "session_id": session_id,
                "resource_count": len(declared_resources),
                "requested_dependencies": requested_dependencies,
            },
        )
        return self._public_session(manifest)

    def get(self, session_id: Any) -> dict[str, Any]:
        _root, manifest = self._load_session(session_id)
        return self._public_session(manifest)

    def execute(
        self,
        session_id: Any,
        *,
        code: Any,
        input_value: Any = None,
        timeout_seconds: int = 60,
        max_output_chars: int = 50_000,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        root, manifest = self._load_session(session_id)
        source = str(code or "")
        if not source.strip():
            raise ValueError("code is required.")
        if len(source) > MAX_SESSION_CODE_CHARS:
            raise ValueError(
                f"code contains {len(source)} characters; limit is {MAX_SESSION_CODE_CHARS}."
            )
        timeout = max(1, min(int(timeout_seconds or 60), 300))
        output_limit = max(1_000, min(int(max_output_chars or 50_000), 500_000))
        normalized_key = str(idempotency_key or "").strip()
        execution_id = (
            hashlib.sha256(
                f"{manifest['session_id']}:{normalized_key}".encode("utf-8")
            ).hexdigest()[:24]
            if normalized_key
            else uuid.uuid4().hex[:24]
        )
        execution_dir = root / "executions" / execution_id
        execution_dir.mkdir(parents=True, exist_ok=True)
        code_path = execution_dir / "code.py"
        input_path = execution_dir / "input.json"
        result_path = execution_dir / "result.json"
        code_hash = _sha256_text(source)
        input_text = json.dumps(input_value, ensure_ascii=False, sort_keys=True, default=str)
        input_hash = _sha256_text(input_text)

        prior = next(
            (
                row
                for row in manifest.get("executions") or []
                if isinstance(row, dict) and row.get("id") == execution_id
            ),
            None,
        )
        if prior:
            if prior.get("code_sha256") != code_hash or prior.get("input_sha256") != input_hash:
                raise ValueError(
                    "idempotency_key was already used with different code or input."
                )
            cached = _read_json(result_path)
            if isinstance(cached, dict) and cached.get("status") in {
                "completed",
                "failed",
                "timeout",
            }:
                return {"session": self._public_session(manifest), "execution": cached}

        code_path.write_text(source, encoding="utf-8")
        _write_json(input_path, input_value)
        started_at = prior.get("started_at") if prior else _utc_iso()
        manifest["status"] = "running"
        manifest["updated_at"] = _utc_iso()
        if not prior:
            manifest.setdefault("executions", []).append({
                "id": execution_id,
                "status": "running",
                "started_at": started_at,
                "code_sha256": code_hash,
                "input_sha256": input_hash,
                "idempotency_key": normalized_key or None,
            })
        else:
            prior["status"] = "running"
        _write_json(root / "manifest.json", manifest)
        self._emit(
            "code_session.execution.started",
            {
                "session_id": manifest["session_id"],
                "execution_id": execution_id,
                "code_sha256": code_hash,
            },
        )

        python_path = self._session_python(root, manifest)
        environment = self._execution_environment(
            root,
            execution_id=execution_id,
            input_path=input_path,
            python_path=python_path,
        )
        command = [
            str(python_path),
            "-I",
            str(root / "bootstrap.py"),
            str(code_path),
        ]
        deadline = time.monotonic() + timeout
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        round_logs: list[dict[str, Any]] = []
        returncode = None
        timed_out = False
        host_action_count = 0

        for round_index in range(1, MAX_HOST_ACTION_ROUNDS + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            completed = _run_bounded_process(
                command,
                cwd=root,
                environment=environment,
                timeout_seconds=remaining,
                max_output_chars=output_limit,
            )
            returncode = int(completed["returncode"])
            if completed["stdout"]:
                stdout_parts.append(completed["stdout"])
            if completed["stderr"]:
                stderr_parts.append(completed["stderr"])
            round_logs.append({
                "round": round_index,
                "returncode": returncode,
                "timed_out": bool(completed["timed_out"]),
                "stdout": completed["stdout"],
                "stderr": completed["stderr"],
                "stdout_truncated": bool(completed["stdout_truncated"]),
                "stderr_truncated": bool(completed["stderr_truncated"]),
            })
            if completed["timed_out"]:
                timed_out = True
                break
            if returncode != 75:
                break

            request = self._pending_request(root, execution_id)
            if not request:
                stderr_parts.append(
                    "Session requested a host action but no pending request was found."
                )
                returncode = 1
                break
            host_action_count += 1
            self._resolve_host_action(root, manifest, request)
        else:
            stderr_parts.append(
                f"Session exceeded the {MAX_HOST_ACTION_ROUNDS} host-action round limit."
            )
            returncode = 1

        raw_stdout = self._redact_runtime_paths(
            stdout_parts[-1] if stdout_parts else "",
            session_root=root,
            python_path=python_path,
        )
        raw_stderr = self._redact_runtime_paths(
            stderr_parts[-1] if stderr_parts else "",
            session_root=root,
            python_path=python_path,
        )
        raw_stderr = _strip_host_action_pending_markers(raw_stderr)
        redacted_round_logs = [
            {
                **row,
                "stdout": self._redact_runtime_paths(
                    row.get("stdout") or "",
                    session_root=root,
                    python_path=python_path,
                ),
                "stderr": self._redact_runtime_paths(
                    _strip_host_action_pending_markers(row.get("stderr")),
                    session_root=root,
                    python_path=python_path,
                ),
            }
            for row in round_logs
        ]
        stdout, stdout_truncated = _bounded(raw_stdout, output_limit)
        stderr, stderr_truncated = _bounded(raw_stderr, output_limit)
        if round_logs:
            stdout_truncated = (
                stdout_truncated
                or bool(round_logs[-1].get("stdout_truncated"))
            )
            stderr_truncated = (
                stderr_truncated
                or bool(round_logs[-1].get("stderr_truncated"))
            )
        if timed_out:
            status = "timeout"
        elif returncode == 0:
            status = "completed"
        else:
            status = "failed"

        progress_events = self._progress_events(root, execution_id)
        for progress_event in progress_events:
            self._emit(
                "code_session.progress",
                {
                    "session_id": manifest["session_id"],
                    "execution_id": execution_id,
                    "status": "running",
                    "message": progress_event.get("message"),
                    "progress": progress_event.get("progress"),
                    "data": progress_event.get("data") or {},
                    "progress_event_id": progress_event.get("id"),
                },
            )
        outputs = self._output_inventory(root)
        finished_at = _utc_iso()
        result: dict[str, Any] = {
            "id": execution_id,
            "session_id": manifest["session_id"],
            "runtime_profile_id": "python.session",
            "status": status,
            "returncode": returncode,
            "timed_out": timed_out,
            "timeout_seconds": timeout,
            "started_at": started_at,
            "finished_at": finished_at,
            "code_sha256": code_hash,
            "input_sha256": input_hash,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "host_action_count": host_action_count,
            "progress_events": progress_events,
            "outputs": outputs,
            "environment": {
                "mechanism": "pixi",
                "profile_id": "python.session",
                "dependency_status": (
                    manifest.get("environment") or {}
                ).get("dependency_status"),
                "base_dependencies": (
                    manifest.get("environment") or {}
                ).get("base_dependencies") or [],
                "requested_dependencies": (
                    manifest.get("environment") or {}
                ).get("requested_dependencies") or [],
            },
            "limitations": [
                "declared session resources only",
                "writes limited to the session workspace",
                "network and subprocess creation denied in generated code",
                "connector credentials and authenticated clients remain in the host",
                "host tool and worker actions use deterministic request/response mediation",
            ],
        }
        bundle = {
            "version": 1,
            "session": self._public_session(manifest),
            "execution": result,
            "code": {
                "sha256": code_hash,
                "source": source,
            },
            "input": input_value,
            "resources": manifest.get("resources") or [],
            "dependency_changes": (
                manifest.get("environment") or {}
            ).get("dependency_changes") or [],
            "host_actions": self._host_action_inventory(root, execution_id),
            "round_logs": redacted_round_logs,
            "state": _read_json(root / "state.json", {}),
        }
        bundle_path = execution_dir / "execution_bundle.json"
        _write_json(bundle_path, bundle)
        if callable(self.artifact_registrar):
            artifact = self.artifact_registrar(
                path=bundle_path,
                label=f"Code session {manifest['session_id'][:8]} execution bundle",
                metadata={
                    "kind": "code_session_execution_bundle",
                    "session_id": manifest["session_id"],
                    "execution_id": execution_id,
                    "status": status,
                    "code_sha256": code_hash,
                },
                review_status="draft",
                action_id=f"bundle:{execution_id}",
            )
            if artifact:
                result["bundle_artifact"] = artifact
                result["artifact_ids"] = [
                    artifact.get("id")
                    if isinstance(artifact, dict)
                    else getattr(artifact, "id", None)
                ]
        _write_json(result_path, result)

        manifest = _read_json(root / "manifest.json", manifest)
        execution_row = next(
            row
            for row in manifest.get("executions") or []
            if row.get("id") == execution_id
        )
        execution_row.update({
            "status": status,
            "returncode": returncode,
            "timed_out": timed_out,
            "finished_at": finished_at,
            "host_action_count": host_action_count,
            "output_count": len(outputs),
            "bundle_artifact_id": (
                (result.get("bundle_artifact") or {}).get("id")
                if isinstance(result.get("bundle_artifact"), dict)
                else None
            ),
        })
        manifest["status"] = "ready" if status == "completed" else status
        manifest["updated_at"] = _utc_iso()
        _write_json(root / "manifest.json", manifest)
        self._emit(
            "code_session.execution.finished",
            {
                "session_id": manifest["session_id"],
                "execution_id": execution_id,
                "status": status,
                "returncode": returncode,
                "host_action_count": host_action_count,
                "output_count": len(outputs),
                "artifact_ids": result.get("artifact_ids") or [],
            },
        )
        return {"session": self._public_session(manifest), "execution": result}

    def install_dependencies(
        self,
        session_id: Any,
        *,
        timeout_seconds: int = 600,
    ) -> dict[str, Any]:
        root, manifest = self._load_session(session_id)
        environment = manifest.get("environment") or {}
        requested = environment.get("requested_dependencies") or []
        if not requested:
            return {
                "session": self._public_session(manifest),
                "dependency_install": {
                    "status": "not_needed",
                    "requested_dependencies": [],
                },
            }
        pixi = shutil.which("pixi")
        if not pixi:
            raise RuntimeError("Pixi is required for governed dependency installation.")

        pixi_manifest = root / "pixi.toml"
        pixi_manifest.write_text(
            self._pixi_manifest_text(requested),
            encoding="utf-8",
        )
        started_at = _utc_iso()
        completed = subprocess.run(
            [pixi, "install", "--manifest-path", str(pixi_manifest)],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(30, min(int(timeout_seconds or 600), 1200)),
            env=self._pixi_environment(),
            check=False,
        )
        stdout, stdout_truncated = _bounded(completed.stdout or "", 50_000)
        stderr, stderr_truncated = _bounded(completed.stderr or "", 50_000)
        status = "completed" if completed.returncode == 0 else "failed"
        change = {
            "id": uuid.uuid4().hex,
            "status": status,
            "mechanism": "pixi",
            "requested_dependencies": requested,
            "started_at": started_at,
            "finished_at": _utc_iso(),
            "returncode": int(completed.returncode),
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
        }
        environment["dependency_changes"] = [
            *(environment.get("dependency_changes") or []),
            change,
        ][-20:]
        environment["dependency_status"] = (
            "installed" if status == "completed" else "install_failed"
        )
        environment["reuse"] = (
            "session_locked_environment"
            if status == "completed"
            else environment.get("reuse")
        )
        manifest["environment"] = environment
        manifest["updated_at"] = _utc_iso()
        _write_json(root / "manifest.json", manifest)
        self._emit(
            "code_session.dependencies.changed",
            {
                "session_id": manifest["session_id"],
                "status": status,
                "requested_dependencies": requested,
                "returncode": int(completed.returncode),
            },
        )
        return {
            "session": self._public_session(manifest),
            "dependency_install": change,
        }

    def _materialize_resources(self, root: Path, resources: Any) -> list[dict[str, Any]]:
        rows = resources if isinstance(resources, list) else ([] if resources is None else [resources])
        if len(rows) > MAX_SESSION_RESOURCES:
            raise ValueError(
                f"A code session can declare at most {MAX_SESSION_RESOURCES} resources."
            )
        result: list[dict[str, Any]] = []
        names: set[str] = set()
        total_bytes = 0
        for index, raw in enumerate(rows, start=1):
            row = dict(raw) if isinstance(raw, dict) else {"file_ref": raw}
            if "data" in row:
                requested_name = row.get("name") or f"resource-{index}.json"
                name = _safe_segment(requested_name, fallback=f"resource-{index}.json")
                if not Path(name).suffix:
                    name += ".json"
                target = root / "resources" / name
                _write_json(target, row.get("data"))
                source = {
                    "reference": f"inline:{index}",
                    "label": row.get("label") or name,
                    "source_kind": "inline_json",
                }
            elif "content" in row:
                requested_name = row.get("name") or f"resource-{index}.txt"
                name = _safe_segment(requested_name, fallback=f"resource-{index}.txt")
                target = root / "resources" / name
                target.write_text(str(row.get("content") or ""), encoding="utf-8")
                source = {
                    "reference": f"inline:{index}",
                    "label": row.get("label") or name,
                    "source_kind": "inline_text",
                }
            else:
                reference = (
                    row.get("file_ref")
                    or row.get("reference")
                    or row.get("artifact_id")
                    or row.get("resource_reference_id")
                )
                if not reference:
                    raise ValueError(
                        "Each session resource needs data, content, or file_ref/reference."
                    )
                if not callable(self.resource_resolver):
                    raise RuntimeError("No session resource resolver is available.")
                source = self.resource_resolver(reference)
                source_path = Path(source["path"]).resolve()
                requested_name = (
                    row.get("name")
                    or source.get("label")
                    or source_path.name
                    or f"resource-{index}"
                )
                name = _safe_segment(requested_name, fallback=f"resource-{index}")
                if source_path.suffix and not Path(name).suffix:
                    name += source_path.suffix.lower()
                target = root / "resources" / name
                shutil.copy2(source_path, target)

            if name in names:
                raise ValueError(f"Duplicate session resource name '{name}'.")
            names.add(name)
            size = target.stat().st_size
            total_bytes += size
            if total_bytes > MAX_SESSION_RESOURCE_BYTES:
                raise ValueError(
                    "Declared session resources exceed the 250 MiB aggregate limit."
                )
            result.append({
                "name": name,
                "path": target.relative_to(root).as_posix(),
                "label": source.get("label") or name,
                "reference": source.get("reference"),
                "source_kind": source.get("source_kind"),
                "artifact_id": source.get("artifact_id"),
                "resource_reference_id": source.get("resource_reference_id"),
                "size_bytes": size,
                "sha256": _sha256_file(target),
                "read_only": True,
            })
        return result

    def _normalize_dependencies(self, dependencies: Any) -> list[dict[str, str]]:
        values = (
            dependencies
            if isinstance(dependencies, list)
            else ([] if dependencies in (None, "") else [dependencies])
        )
        if len(values) > 20:
            raise ValueError("A session can request at most 20 ephemeral dependencies.")
        result = []
        seen = set()
        for raw in values:
            try:
                requirement = Requirement(str(raw or "").strip())
            except InvalidRequirement as exc:
                raise ValueError(f"Invalid dependency requirement '{raw}'.") from exc
            if requirement.url:
                raise ValueError("Direct URL and local-path dependencies are not supported.")
            if requirement.marker:
                raise ValueError("Environment-marker dependency requests are not supported.")
            key = requirement.name.lower()
            if key in seen:
                raise ValueError(f"Duplicate dependency request '{requirement.name}'.")
            seen.add(key)
            result.append({
                "name": requirement.name,
                "extras": ",".join(sorted(requirement.extras)),
                "specifier": str(requirement.specifier) or "*",
                "requirement": str(requirement),
            })
        return result

    def _load_session(self, session_id: Any) -> tuple[Path, dict[str, Any]]:
        normalized = str(session_id or "").strip()
        if not re.fullmatch(r"[a-f0-9]{24,32}", normalized):
            raise ValueError("session_id is invalid.")
        root = (self.sessions_root / normalized).resolve()
        root.relative_to(self.sessions_root.resolve())
        manifest = _read_json(root / "manifest.json")
        if not isinstance(manifest, dict):
            raise KeyError(f"Code session '{normalized}' was not found.")
        if manifest.get("run_id") != self.run_id:
            raise PermissionError("Code session belongs to a different workflow run.")
        if (
            self.owner_user_id is not None
            and manifest.get("owner_user_id") not in {None, self.owner_user_id}
        ):
            raise PermissionError("Code session belongs to another user.")
        return root, manifest

    def _public_session(self, manifest: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": manifest.get("session_id"),
            "label": manifest.get("label"),
            "run_id": manifest.get("run_id"),
            "step_id": manifest.get("step_id"),
            "runtime_profile_id": "python.session",
            "status": manifest.get("status"),
            "created_at": manifest.get("created_at"),
            "updated_at": manifest.get("updated_at"),
            "environment": manifest.get("environment") or {},
            "resources": manifest.get("resources") or [],
            "executions": manifest.get("executions") or [],
            "capabilities": [
                "declared_resource_read",
                "session_output_write",
                "host_tool_call",
                "bounded_agent_map",
                "progress_events",
                "artifact_create",
                "durable_state",
            ],
        }

    def _session_python(self, root: Path, manifest: dict[str, Any]) -> Path:
        if (manifest.get("environment") or {}).get("dependency_status") == "installed":
            if os.name == "nt":
                candidate = root / ".pixi" / "envs" / "default" / "python.exe"
            else:
                candidate = root / ".pixi" / "envs" / "default" / "bin" / "python"
            if not candidate.is_file():
                raise RuntimeError("The installed session Pixi environment is unavailable.")
            return candidate.resolve()
        return self.python_executable

    def _execution_environment(
        self,
        root: Path,
        *,
        execution_id: str,
        input_path: Path,
        python_path: Path,
    ) -> dict[str, str]:
        prefix = (
            python_path.parent
            if os.name == "nt"
            else python_path.parent.parent
        ).resolve()
        temp_root = root / "tmp"
        environment = {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TEMP": str(temp_root),
            "TMP": str(temp_root),
            "TMPDIR": str(temp_root),
        }
        contract_values = {
            "CODE_RUNTIME": "python.session",
            "SESSION_ROOT": str(root),
            "EXECUTION_ID": execution_id,
            "EXECUTION_INPUT": str(input_path),
            "RUNTIME_READ_ROOTS": os.pathsep.join(
                dict.fromkeys(
                    str(item)
                    for item in (
                        prefix,
                        Path(sys.prefix).resolve(),
                        Path(sys.base_prefix).resolve(),
                    )
                )
            ),
        }
        for suffix, value in contract_values.items():
            set_product_environment(
                environment,
                suffix,
                value,
                include_legacy=True,
            )
        for key in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"):
            value = os.environ.get(key)
            if value:
                environment[key] = value
        return environment

    def _pending_request(
        self,
        root: Path,
        execution_id: str,
    ) -> dict[str, Any] | None:
        pending = []
        for path in sorted((root / "requests").glob("*.json")):
            request = _read_json(path)
            if not isinstance(request, dict) or request.get("execution_id") != execution_id:
                continue
            response = root / "responses" / f"{request.get('request_id')}.json"
            if not response.exists():
                pending.append(request)
        return pending[0] if pending else None

    def _resolve_host_action(
        self,
        root: Path,
        manifest: dict[str, Any],
        request: dict[str, Any],
    ) -> None:
        request_id = str(request.get("request_id") or "")
        action = str(request.get("action") or "")
        payload = request.get("payload") if isinstance(request.get("payload"), dict) else {}
        self._emit(
            "code_session.host_action.started",
            {
                "session_id": manifest["session_id"],
                "execution_id": request.get("execution_id"),
                "request_id": request_id,
                "action": action,
            },
        )
        try:
            if action == "tool.call":
                if not callable(self.tool_executor):
                    raise RuntimeError("Host tool execution is unavailable.")
                batch = self.tool_executor([payload], True)
                calls = batch.get("calls") if isinstance(batch, dict) else None
                result = calls[0] if isinstance(calls, list) and calls else batch
            elif action == "tool.call_many":
                if not callable(self.tool_executor):
                    raise RuntimeError("Host tool execution is unavailable.")
                calls = payload.get("calls")
                if not isinstance(calls, list) or not calls:
                    raise ValueError("tool.call_many requires a non-empty calls list.")
                result = self.tool_executor(
                    calls[:50],
                    payload.get("continue_on_error", True) is not False,
                )
            elif action == "agents.map":
                if not callable(self.agent_mapper):
                    raise RuntimeError("Agent mapping is unavailable.")
                result = self.agent_mapper(payload, request_id)
            elif action == "artifact.create":
                if not callable(self.artifact_registrar):
                    raise RuntimeError("Artifact creation is unavailable.")
                relative = Path(str(payload.get("output_path") or ""))
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("Artifact output_path must be session-relative.")
                path = (root / "outputs" / relative).resolve()
                path.relative_to((root / "outputs").resolve())
                if not path.is_file():
                    raise FileNotFoundError("Artifact output file is unavailable.")
                result = self.artifact_registrar(
                    path=path,
                    label=payload.get("label") or path.name,
                    metadata={
                        **(
                            payload.get("metadata")
                            if isinstance(payload.get("metadata"), dict)
                            else {}
                        ),
                        "kind": "code_session_output",
                        "session_id": manifest["session_id"],
                        "execution_id": request.get("execution_id"),
                    },
                    review_status=payload.get("review_status") or "draft",
                    action_id=request_id,
                )
            else:
                raise ValueError(f"Unsupported host action '{action}'.")
            response = {
                "request_id": request_id,
                "ok": True,
                "result": result,
                "resolved_at": _utc_iso(),
            }
            status = "completed"
            error = None
        except Exception as exc:
            response = {
                "request_id": request_id,
                "ok": False,
                "error": str(exc),
                "resolved_at": _utc_iso(),
            }
            status = "failed"
            error = str(exc)
        _write_json(root / "responses" / f"{request_id}.json", response)
        fresh_manifest = _read_json(root / "manifest.json", manifest)
        fresh_manifest.setdefault("host_actions", {})[request_id] = {
            "action": action,
            "execution_id": request.get("execution_id"),
            "ordinal": request.get("ordinal"),
            "status": status,
            "resolved_at": response["resolved_at"],
            "error": error,
        }
        fresh_manifest["updated_at"] = _utc_iso()
        _write_json(root / "manifest.json", fresh_manifest)
        self._emit(
            "code_session.host_action.finished",
            {
                "session_id": manifest["session_id"],
                "execution_id": request.get("execution_id"),
                "request_id": request_id,
                "action": action,
                "status": status,
                "error": error,
            },
        )

    def _progress_events(self, root: Path, execution_id: str) -> list[dict[str, Any]]:
        path = root / "progress.jsonl"
        if not path.is_file():
            return []
        events = {}
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(event, dict)
                and event.get("execution_id") == execution_id
                and event.get("id")
            ):
                events[event["id"]] = event
        return list(events.values())[-200:]

    def _output_inventory(self, root: Path) -> list[dict[str, Any]]:
        outputs = []
        output_root = root / "outputs"
        for path in sorted(output_root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            outputs.append({
                "path": path.relative_to(output_root).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
            })
        return outputs[:500]

    def _host_action_inventory(
        self,
        root: Path,
        execution_id: str,
    ) -> list[dict[str, Any]]:
        rows = []
        for path in sorted((root / "requests").glob("*.json")):
            request = _read_json(path)
            if not isinstance(request, dict) or request.get("execution_id") != execution_id:
                continue
            response = _read_json(
                root / "responses" / f"{request.get('request_id')}.json",
                {},
            )
            rows.append({
                "request_id": request.get("request_id"),
                "ordinal": request.get("ordinal"),
                "action": request.get("action"),
                "payload": request.get("payload"),
                "response": response,
            })
        return rows

    def _pixi_manifest_text(self, dependencies: list[dict[str, str]]) -> str:
        current_platform = {
            ("Windows", "AMD64"): "win-64",
            ("Linux", "x86_64"): "linux-64",
            ("Darwin", "arm64"): "osx-arm64",
            ("Darwin", "x86_64"): "osx-64",
        }.get((platform.system(), platform.machine()))
        if not current_platform:
            raise RuntimeError("The current platform is not supported by the Pixi session profile.")
        lines = [
            "[workspace]",
            'name = "iarf-code-session"',
            'version = "0.1.0"',
            'channels = ["conda-forge"]',
            f'platforms = ["{current_platform}"]',
            "",
            "[dependencies]",
            f'python = "{sys.version_info.major}.{sys.version_info.minor}.*"',
            'pip = "*"',
            "",
            "[pypi-dependencies]",
        ]
        for dependency in dependencies:
            extras = dependency.get("extras")
            key = dependency["name"] + (f"[{extras}]" if extras else "")
            lines.append(
                f"{json.dumps(key)} = {json.dumps(dependency.get('specifier') or '*')}"
            )
        return "\n".join(lines) + "\n"

    def _pixi_environment(self) -> dict[str, str]:
        environment = {
            "PIXI_NO_PROGRESS": "1",
        }
        for key in (
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "COMSPEC",
            "PATHEXT",
            "PATH",
        ):
            value = os.environ.get(key)
            if value:
                environment[key] = value
        return environment

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if callable(self.event_emitter):
            self.event_emitter(kind, payload)

    def _redact_runtime_paths(
        self,
        value: str,
        *,
        session_root: Path,
        python_path: Path,
    ) -> str:
        text = str(value or "")
        runtime_prefix = (
            python_path.parent
            if os.name == "nt"
            else python_path.parent.parent
        ).resolve()
        replacements = [
            (session_root, "<session>"),
            (runtime_prefix, "<pixi-runtime>"),
            (Path(sys.prefix).resolve(), "<pixi-runtime>"),
            (self.instance_path, "<instance>"),
        ]
        for path, replacement in sorted(
            replacements,
            key=lambda item: len(str(item[0])),
            reverse=True,
        ):
            for rendered in {str(path), str(path).replace("\\", "/")}:
                text = text.replace(rendered, replacement)
        return text
