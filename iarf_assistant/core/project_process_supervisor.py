from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00",
        "Z",
    )


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{uuid.uuid4().hex[:8]}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)


def _terminate_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    spec_path = Path(sys.argv[1]).resolve()
    result_path = Path(sys.argv[2]).resolve()
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    stdout_path = Path(spec["stdout_path"]).resolve()
    stderr_path = Path(spec["stderr_path"]).resolve()
    control_path = Path(spec["control_path"]).resolve()
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)

    parent_disconnected = threading.Event()

    def watch_parent() -> None:
        try:
            descriptor = sys.stdin.fileno()
            while os.read(descriptor, 8192):
                pass
        finally:
            parent_disconnected.set()

    threading.Thread(target=watch_parent, daemon=True).start()
    started_at = _utc_iso()
    started_monotonic = time.monotonic()
    child = None
    status = "failed"
    reason = "launch_failed"
    returncode = None
    error = None
    try:
        creation_flags = 0
        popen_kwargs = {}
        if os.name == "nt":
            creation_flags = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        else:
            popen_kwargs["start_new_session"] = True
        with stdout_path.open("wb") as stdout_handle, stderr_path.open(
            "wb"
        ) as stderr_handle:
            child = subprocess.Popen(
                [str(value) for value in spec["argv"]],
                cwd=str(spec["cwd"]),
                env={
                    str(key): str(value)
                    for key, value in (spec.get("environment") or {}).items()
                },
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                shell=False,
                creationflags=creation_flags,
                **popen_kwargs,
            )
            while True:
                returncode = child.poll()
                if returncode is not None:
                    status = "completed" if returncode == 0 else "failed"
                    reason = "process_exited"
                    break
                if parent_disconnected.is_set():
                    status = "cancelled"
                    reason = "parent_disconnected"
                    _terminate_tree(child)
                    returncode = child.poll()
                    break
                if control_path.is_file():
                    status = "cancelled"
                    reason = "stop_requested"
                    _terminate_tree(child)
                    returncode = child.poll()
                    break
                if time.monotonic() - started_monotonic >= float(
                    spec["timeout_seconds"]
                ):
                    status = "timeout"
                    reason = "timeout"
                    _terminate_tree(child)
                    returncode = child.poll()
                    break
                time.sleep(0.1)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        if child is not None:
            _terminate_tree(child)
            returncode = child.poll()
    finally:
        if child is not None and child.poll() is None:
            _terminate_tree(child)
            returncode = child.poll()
        _write_json(
            result_path,
            {
                "version": 1,
                "status": status,
                "termination_reason": reason,
                "returncode": returncode,
                "timed_out": status == "timeout",
                "pid": child.pid if child is not None else None,
                "started_at": started_at,
                "finished_at": _utc_iso(),
                "duration_ms": max(
                    0,
                    int((time.monotonic() - started_monotonic) * 1000),
                ),
                "error": error,
            },
        )
    return 0 if status in {"completed", "failed", "timeout", "cancelled"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
