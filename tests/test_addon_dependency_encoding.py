from __future__ import annotations

import hashlib
import sys
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from iarf_assistant.core.addons import dependency_orchestration
from iarf_assistant.core.addons.contracts import (
    AddonSourceKind,
    DependencyLock,
    ResolvedAddonContract,
)
from iarf_assistant.core.addons.dependency_orchestration import (
    DependencyOrchestrationMixin,
)
from iarf_assistant.core.addons.environments import AddonEnvironmentPaths

ADDON_ID = "synthetic-addon"


class _DependencyManagerHarness(DependencyOrchestrationMixin):
    def __init__(
        self,
        addon_dir: Path,
        environment_paths: AddonEnvironmentPaths,
        locks_dir: Path,
    ) -> None:
        self._addon_dir = addon_dir
        self._environment_paths = environment_paths
        self._addon_packages: dict[str, Any] = {}
        self.app: Any = SimpleNamespace(
            extensions={"carlina_paths": SimpleNamespace(locks_dir=locks_dir)}
        )

    def get_addon_directory(self, addon_id: str) -> Path:
        assert addon_id == ADDON_ID
        return self._addon_dir

    def _addon_source_kind(self, addon_id: str) -> AddonSourceKind:
        assert addon_id == ADDON_ID
        return AddonSourceKind.BUILT_IN

    def _addon_environment_paths(
        self,
        addon_id: str,
        python_environment: Any = None,
    ) -> AddonEnvironmentPaths:
        assert addon_id == ADDON_ID
        return self._environment_paths

    def _ensure_addon_python_path(
        self,
        addon_id: str,
        python_environment: Any = None,
    ) -> None:
        assert addon_id == ADDON_ID


def _manager_for(
    tmp_path: Path,
) -> tuple[_DependencyManagerHarness, Path, AddonEnvironmentPaths]:
    addon_dir = tmp_path / "source" / ADDON_ID
    addon_dir.mkdir(parents=True)
    environment_root = tmp_path / "data" / "addons" / ADDON_ID / "environment"
    environment_paths = AddonEnvironmentPaths(
        root=environment_root,
        lib_dir=environment_root / "lib",
        pixi_project_dir=environment_root / "pixi",
        pixi_manifest=environment_root / "pixi" / "pixi.toml",
    )
    manager = _DependencyManagerHarness(
        addon_dir,
        environment_paths,
        tmp_path / "locks",
    )
    return manager, addon_dir, environment_paths


def _capture_check_call(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[list[str], str | None]]:
    calls: list[tuple[list[str], str | None]] = []

    def capture(command: Sequence[str], **kwargs: Any) -> int:
        cwd = kwargs.get("cwd")
        calls.append((list(command), str(cwd) if cwd is not None else None))
        return 0

    monkeypatch.setattr(dependency_orchestration.subprocess, "check_call", capture)
    return calls


def _select_uv(monkeypatch: pytest.MonkeyPatch, uv_path: str | None) -> None:
    monkeypatch.setattr(
        dependency_orchestration.shutil,
        "which",
        lambda executable: uv_path if executable == "uv" else None,
    )


@pytest.mark.parametrize(
    "uv_path",
    [None, pytest.param("/synthetic/bin/uv", id="uv-control")],
    ids=["python-fallback", None],
)
def test_install_dependencies_uses_utf8_python_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    uv_path: str | None,
) -> None:
    manager, addon_dir, environment_paths = _manager_for(tmp_path)
    requirements = addon_dir / "requirements.txt"
    requirements.write_text("synthetic-package==1.2.3\n", encoding="utf-8")
    _select_uv(monkeypatch, uv_path)
    calls = _capture_check_call(monkeypatch)

    manager.install_dependencies(ADDON_ID, prepare_docker=False)

    assert len(calls) == 1
    command, cwd = calls[0]
    assert cwd == str(environment_paths.root)
    if uv_path is None:
        assert command[:6] == [sys.executable, "-X", "utf8", "-m", "pip", "install"]
    else:
        assert command[:5] == [uv_path, "pip", "install", "--python", sys.executable]
        assert "-X" not in command
        assert "-m" not in command
    target_index = command.index("--target")
    assert command[target_index + 1] == str(environment_paths.lib_dir)
    assert command[-1] == f"-r{requirements}"


@pytest.mark.parametrize(
    "uv_path",
    [None, pytest.param("/synthetic/bin/uv", id="uv-control")],
    ids=["python-fallback", None],
)
def test_locked_dependency_environment_uses_utf8_python_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    uv_path: str | None,
) -> None:
    manager, addon_dir, environment_paths = _manager_for(tmp_path)
    lock_path = addon_dir / "requirements.lock"
    lock_bytes = ("synthetic-package==1.2.3 --hash=sha256:" + "a" * 64 + "\n").encode(
        "utf-8"
    )
    lock_path.write_bytes(lock_bytes)
    dependency_lock = DependencyLock(
        path="requirements.lock",
        sha256=hashlib.sha256(lock_bytes).hexdigest(),
        format="pip_requirements",
        require_hashes=True,
    )
    contract = cast(
        ResolvedAddonContract,
        SimpleNamespace(
            addon_id=ADDON_ID,
            version="1.2.3",
            manifest_schema_version=1,
            dependency_environment=None,
            python_version=None,
            dependency_lock=dependency_lock,
        ),
    )
    _select_uv(monkeypatch, uv_path)
    calls = _capture_check_call(monkeypatch)

    target = manager._install_locked_dependency_environment(ADDON_ID, contract)

    assert target == environment_paths.root
    assert (target / "environment.json").is_file()
    assert (target / "lib").is_dir()
    assert list(target.parent.glob(f".{dependency_lock.sha256}.tmp-*")) == []
    assert len(calls) == 1
    command, cwd = calls[0]
    assert cwd == str(addon_dir)
    if uv_path is None:
        assert command[:7] == [
            sys.executable,
            "-X",
            "utf8",
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
        ]
    else:
        assert command[:5] == [uv_path, "pip", "install", "--python", sys.executable]
        assert "-X" not in command
        assert "-m" not in command
    assert "--require-hashes" in command
    target_index = command.index("--target")
    staged_lib = Path(command[target_index + 1])
    assert staged_lib.name == "lib"
    assert staged_lib.parent.parent == target.parent
    lock_index = command.index("-r")
    assert command[lock_index + 1] == str(lock_path)
