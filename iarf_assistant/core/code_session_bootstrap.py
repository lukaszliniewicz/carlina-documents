"""Session-local bootstrap for governed Python execution."""

from __future__ import annotations

import os
import runpy
import sys
import traceback
from pathlib import Path

DENIED_EVENTS = {
    "os.exec",
    "os.execve",
    "os.posix_spawn",
    "os.spawn",
    "os.system",
    "socket.__new__",
    "socket.bind",
    "socket.connect",
    "socket.getaddrinfo",
    "subprocess.Popen",
    "urllib.Request",
    "webbrowser.open",
}
TRUSTED_NATIVE_PACKAGES = {
    "docx",
    "fitz",
    "jsonschema",
    "numpy",
    "openpyxl",
    "pandas",
    "PIL",
}
HOST_ACTION_PENDING_PREFIX = "CARLINA_HOST_ACTION_PENDING:"


def _product_environment_value(suffix: str) -> str | None:
    """Read a session contract value without importing the host package."""

    canonical_name = f"CARLINA_{suffix}"
    if canonical_name in os.environ:
        return os.environ[canonical_name]
    return os.environ.get(f"IARF_{suffix}")


def _resolved_path(value, *, cwd: Path) -> Path | None:
    if isinstance(value, int) or value is None:
        return None
    try:
        path = Path(os.fsdecode(value))
    except (TypeError, ValueError):
        return None
    if not path.is_absolute():
        path = cwd / path
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def _is_within(path: Path, roots: tuple[Path, ...]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _install_audit_hook(session_root: Path) -> None:
    read_roots = [session_root]
    for value in (_product_environment_value("RUNTIME_READ_ROOTS") or "").split(
        os.pathsep
    ):
        if value:
            read_roots.append(Path(value).resolve())
    read_roots_tuple = tuple(dict.fromkeys(read_roots))
    write_roots_tuple = (session_root,)

    def audit(event: str, args: tuple) -> None:
        if event in DENIED_EVENTS or event.startswith(("socket.", "subprocess.")):
            raise PermissionError(f"Code-session policy denied '{event}'.")

        if event == "ctypes.dlopen":
            native_path = _resolved_path(args[0] if args else None, cwd=session_root)
            trusted_package_frame = False
            frame = sys._getframe(1)
            while frame is not None:
                filename = str(frame.f_code.co_filename).replace("\\", "/")
                if "/site-packages/" in filename:
                    package = filename.split("/site-packages/", 1)[1].split("/", 1)[0]
                    if package in TRUSTED_NATIVE_PACKAGES:
                        trusted_package_frame = True
                        break
                frame = frame.f_back
            if not trusted_package_frame:
                raise PermissionError(
                    "Code-session policy denied direct native-library loading."
                )
            raw_name = str(args[0] if args else "")
            if (
                native_path is not None
                and ("/" in raw_name or "\\" in raw_name)
                and not _is_within(native_path, read_roots_tuple)
            ):
                raise PermissionError(
                    "Code-session policy denied a native library outside the runtime."
                )
            return

        if event == "open" and args:
            path = _resolved_path(args[0], cwd=session_root)
            if path is None:
                return
            mode = str(args[1] if len(args) > 1 else "r")
            numeric_flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
            write_flags = (
                os.O_WRONLY
                | os.O_RDWR
                | os.O_CREAT
                | os.O_TRUNC
                | os.O_APPEND
            )
            writing = (
                any(flag in mode for flag in ("w", "a", "x", "+"))
                or bool(numeric_flags & write_flags)
            )
            allowed_roots = write_roots_tuple if writing else read_roots_tuple
            if not _is_within(path, allowed_roots):
                raise PermissionError(
                    f"Code-session policy denied file access outside declared roots: {path}"
                )
            return

        if event in {
            "os.chdir",
            "os.chmod",
            "os.link",
            "os.listdir",
            "os.mkdir",
            "os.remove",
            "os.rename",
            "os.rmdir",
            "os.scandir",
            "os.symlink",
            "os.truncate",
            "os.unlink",
            "os.utime",
        } and args:
            path = _resolved_path(args[0], cwd=session_root)
            if path is None:
                return
            writing = event in {
                "os.chmod",
                "os.link",
                "os.mkdir",
                "os.remove",
                "os.rename",
                "os.rmdir",
                "os.symlink",
                "os.truncate",
                "os.unlink",
                "os.utime",
            }
            allowed_roots = write_roots_tuple if writing else read_roots_tuple
            if not _is_within(path, allowed_roots):
                raise PermissionError(
                    f"Code-session policy denied '{event}' outside declared roots."
                )
            if event in {"os.link", "os.rename", "os.symlink"} and len(args) > 1:
                destination = _resolved_path(args[1], cwd=session_root)
                if destination is not None and not _is_within(
                    destination,
                    write_roots_tuple,
                ):
                    raise PermissionError(
                        f"Code-session policy denied '{event}' to an undeclared destination."
                    )

    sys.addaudithook(audit)


def main() -> int:
    if len(sys.argv) != 2:
        print("Expected a single session script path.", file=sys.stderr)
        return 2
    session_root_value = _product_environment_value("SESSION_ROOT")
    if not session_root_value:
        print("CARLINA_SESSION_ROOT is unavailable.", file=sys.stderr)
        return 2
    session_root = Path(session_root_value).resolve()
    script_path = Path(sys.argv[1]).resolve()
    script_path.relative_to(session_root)
    os.chdir(session_root)
    sys.dont_write_bytecode = True
    _install_audit_hook(session_root)
    sys.path.insert(0, str(session_root))
    sys.argv = [str(script_path)]

    try:
        runpy.run_path(str(script_path), run_name="__main__")
        return 0
    except SystemExit as exc:
        return int(exc.code or 0) if isinstance(exc.code, (int, type(None))) else 1
    except Exception as exc:
        runtime_module = sys.modules.get("iarf_runtime")
        pending_type = getattr(runtime_module, "HostActionPending", None)
        if pending_type and isinstance(exc, pending_type):
            print(f"{HOST_ACTION_PENDING_PREFIX}{exc.request_id}", file=sys.stderr)
            return 75
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
