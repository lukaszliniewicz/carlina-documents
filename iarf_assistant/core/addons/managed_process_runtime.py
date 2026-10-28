"""Managed-process runtime backends.

Pixi is the single supported dependency environment for isolated Carlina
processes. This module owns binary discovery and command construction so add-on
code and the Flask-facing manager do not encode environment-manager details.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

MANAGED_PROCESS_RUNTIME_PIXI = "pixi"
_DEFAULT_PIXI_TIMEOUT_SECONDS = 1800.0
_PIXI_EXECUTABLE_ENVIRONMENT = "CARLINA_PIXI_EXECUTABLE"
_MANAGED_VERSION_ENVIRONMENT = "CARLINA_MANAGED_VERSION"
_PROGRAM_ROOT_ENVIRONMENT = "CARLINA_PROGRAM_ROOT"


def managed_process_creation_flags() -> int:
    """Return the platform flags for non-interactive managed processes.

    Carlina's workers and dependency tools are background processes. On
    Windows, launching a console executable without ``CREATE_NO_WINDOW`` can
    create a visible console for every Pixi or worker invocation.
    """

    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


class ManagedProcessRuntimeError(RuntimeError):
    """Raised when a managed-process runtime cannot satisfy an operation."""


@dataclass(frozen=True, slots=True)
class ManagedProcessRuntimeProbe:
    kind: str
    available: bool
    detail: str
    binary: str | None = None


class ManagedProcessRuntimeBackend(Protocol):
    """Common contract for a supervised dependency runtime."""

    kind: str

    def probe(self) -> ManagedProcessRuntimeProbe: ...

    def install(
        self,
        manifest_path: str | Path,
        *,
        locked: bool = False,
    ) -> subprocess.CompletedProcess[str]: ...

    def lock(
        self,
        manifest_path: str | Path,
    ) -> subprocess.CompletedProcess[str]: ...

    def command(
        self,
        manifest_path: str | Path,
        argv: Sequence[str],
    ) -> list[str]: ...

    def execute(
        self,
        manifest_path: str | Path,
        argv: Sequence[str],
        *,
        input_text: str | None = None,
        timeout_seconds: float | None = None,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]: ...

    def list_packages(
        self,
        manifest_path: str | Path,
    ) -> subprocess.CompletedProcess[str]: ...


class PixiManagedProcessRuntimeBackend:
    """Pixi implementation of the managed-process runtime contract."""

    kind = MANAGED_PROCESS_RUNTIME_PIXI

    def __init__(
        self,
        project_root: str | Path,
        *,
        binary: str | Path | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        configured = binary or os.environ.get(_PIXI_EXECUTABLE_ENVIRONMENT)
        self._configured_binary = (
            Path(configured).expanduser().resolve() if configured else None
        )
        self.timeout_seconds = self._resolve_timeout(timeout_seconds)

    @staticmethod
    def _resolve_timeout(value: float | None) -> float:
        raw = value
        if raw is None:
            raw = os.environ.get("CARLINA_PIXI_TIMEOUT_SECONDS")
        try:
            parsed = float(raw) if raw is not None else _DEFAULT_PIXI_TIMEOUT_SECONDS
        except TypeError, ValueError:
            parsed = _DEFAULT_PIXI_TIMEOUT_SECONDS
        return max(1.0, parsed)

    def resolve_binary(self) -> Path:
        """Resolve Pixi from the active Carlina runtime before any dev fallback.

        Managed launchers set ``CARLINA_PROGRAM_ROOT`` and
        ``CARLINA_MANAGED_VERSION``.  Their packed runtime carries Pixi beside
        its Python interpreter, so consulting ``PATH`` (or the wheel's
        site-packages directory) is both unreliable and can select a different
        toolchain.  Source checkouts retain their explicit ``.bin`` then PATH
        compatibility path.  ``CARLINA_PIXI_EXECUTABLE`` is the documented
        escape hatch for a deliberately provisioned launcher.
        """
        candidates: list[Path] = []
        if self._configured_binary is not None:
            candidates.append(self._configured_binary)
        binary_name = "pixi.exe" if platform.system() == "Windows" else "pixi"
        managed_binary = self._managed_runtime_binary(binary_name)
        if managed_binary is not None:
            candidates.append(managed_binary)
            for candidate in candidates:
                if candidate.is_file():
                    return candidate.resolve()
            raise ManagedProcessRuntimeError(
                "Managed Carlina could not find its bundled Pixi executable. "
                "Repair or reinstall the active managed release, or configure "
                f"{_PIXI_EXECUTABLE_ENVIRONMENT} in the managed launcher."
            )
        candidates.append(self.project_root / ".bin" / binary_name)
        discovered = shutil.which("pixi")
        if discovered:
            candidates.append(Path(discovered))
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
        raise ManagedProcessRuntimeError(
            "Pixi is required for managed add-on processes but was not found in "
            f"{self.project_root / '.bin'} or PATH. Run the Carlina setup/repair "
            "command."
        )

    @staticmethod
    def _managed_runtime_binary(binary_name: str) -> Path | None:
        """Return the active packed-runtime Pixi path, if launcher-bound.

        The values are intentionally treated as a contract, not arbitrary
        filesystem hints: an invalid version disables the managed candidate
        and yields the actionable managed diagnostic above.
        """

        program_root = str(os.environ.get(_PROGRAM_ROOT_ENVIRONMENT) or "").strip()
        version = str(os.environ.get(_MANAGED_VERSION_ENVIRONMENT) or "").strip()
        if not program_root or not version:
            return None
        if any(part in {"", ".", ".."} for part in Path(version).parts):
            return Path(program_root) / "missing-managed-pixi"
        root = Path(program_root).expanduser().resolve()
        return root / "versions" / version / "env" / "bin" / binary_name

    def probe(self) -> ManagedProcessRuntimeProbe:
        try:
            binary = self.resolve_binary()
        except ManagedProcessRuntimeError as exc:
            return ManagedProcessRuntimeProbe(
                kind=self.kind,
                available=False,
                detail=str(exc),
            )
        result = self._run([str(binary), "--version"], check=False)
        detail = (result.stdout or result.stderr or "Pixi is available.").strip()
        return ManagedProcessRuntimeProbe(
            kind=self.kind,
            available=result.returncode == 0,
            detail=detail,
            binary=str(binary),
        )

    @staticmethod
    def _manifest(path: str | Path) -> Path:
        manifest = Path(path).resolve()
        if not manifest.is_file():
            raise ManagedProcessRuntimeError(f"Pixi manifest not found: {manifest}")
        return manifest

    @staticmethod
    def environment(base: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(base or os.environ)
        # Pixi parses this environment variable as a boolean enum. Windows
        # tolerated "1" in older builds, while current Linux releases require
        # the canonical string form.
        env.setdefault("PIXI_NO_PROGRESS", "true")
        env.setdefault("UV_LINK_MODE", "copy")
        # Isolate the Pixi interpreter from the host. Worker processes export
        # PYTHONPATH pointing at the host 3.14 site-packages; inheriting it
        # breaks the Pixi 3.11 env (e.g. greenlet binary mismatch) so every
        # scraping_backend fetch fails fast. Pixi activation sets what it needs.
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        env["PYTHONNOUSERSITE"] = "1"
        return env

    def _run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        check: bool = True,
        input_text: str | None = None,
        timeout_seconds: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        effective_timeout = self.timeout_seconds
        if timeout_seconds is not None:
            try:
                effective_timeout = max(1.0, float(timeout_seconds))
            except (TypeError, ValueError) as exc:
                raise ManagedProcessRuntimeError(
                    "Managed-process timeout must be a number."
                ) from exc
        try:
            return subprocess.run(
                list(argv),
                cwd=str(cwd or self.project_root),
                env=self.environment(),
                input=input_text,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
                check=check,
                creationflags=managed_process_creation_flags(),
            )
        except subprocess.TimeoutExpired as exc:
            raise ManagedProcessRuntimeError(
                f"Pixi operation exceeded {effective_timeout:g} seconds."
            ) from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or str(exc)).strip()
            raise ManagedProcessRuntimeError(
                f"Pixi operation failed: {detail}"
            ) from exc
        except OSError as exc:
            raise ManagedProcessRuntimeError(f"Could not execute Pixi: {exc}") from exc

    def install(
        self,
        manifest_path: str | Path,
        *,
        locked: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        manifest = self._manifest(manifest_path)
        binary = self.resolve_binary()
        command = [str(binary), "install", "--manifest-path", str(manifest)]
        if locked:
            command.append("--locked")
        return self._run(
            command,
            cwd=manifest.parent,
        )

    def lock(
        self,
        manifest_path: str | Path,
    ) -> subprocess.CompletedProcess[str]:
        """Resolve and write the distributable lock without creating an env."""

        manifest = self._manifest(manifest_path)
        binary = self.resolve_binary()
        return self._run(
            [
                str(binary),
                "lock",
                "--manifest-path",
                str(manifest),
                "--no-install",
            ],
            cwd=manifest.parent,
        )

    def command(
        self,
        manifest_path: str | Path,
        argv: Sequence[str],
    ) -> list[str]:
        manifest = self._manifest(manifest_path)
        binary = self.resolve_binary()
        return [
            str(binary),
            "run",
            "--manifest-path",
            str(manifest),
            *[str(item) for item in argv],
        ]

    def execute(
        self,
        manifest_path: str | Path,
        argv: Sequence[str],
        *,
        input_text: str | None = None,
        timeout_seconds: float | None = None,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        manifest = self._manifest(manifest_path)
        return self._run(
            self.command(manifest, argv),
            cwd=manifest.parent,
            check=check,
            input_text=input_text,
            timeout_seconds=timeout_seconds,
        )

    def list_packages(
        self,
        manifest_path: str | Path,
    ) -> subprocess.CompletedProcess[str]:
        manifest = self._manifest(manifest_path)
        binary = self.resolve_binary()
        return self._run(
            [str(binary), "list", "--manifest-path", str(manifest)],
            cwd=manifest.parent,
        )


__all__ = [
    "MANAGED_PROCESS_RUNTIME_PIXI",
    "ManagedProcessRuntimeBackend",
    "ManagedProcessRuntimeError",
    "ManagedProcessRuntimeProbe",
    "PixiManagedProcessRuntimeBackend",
]
