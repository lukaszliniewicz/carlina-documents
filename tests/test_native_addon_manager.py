from __future__ import annotations

import atexit as system_atexit
import builtins
import inspect
import logging
import sys
import time as system_time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from flask import Flask

from iarf_assistant.core import addon_manager_core
from iarf_assistant.core.addon_manager_core import AddonManagerCore
from iarf_assistant.core.addons.container_lifecycle_support import (
    ContainerLifecycleSupportMixin,
)
from iarf_assistant.core.addons.manager_selection import create_addon_manager

if TYPE_CHECKING:
    from iarf_assistant.core.addon_manager import AddonManager
    from iarf_assistant.core.addons.native_manager import NativeAddonManager

DISABLED_MESSAGE = "This application does not include container services."


@pytest.fixture
def manager_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Flask:
    app_root = tmp_path / "app"
    instance_root = tmp_path / "instance"
    app_root.mkdir()
    instance_root.mkdir()
    app = Flask(
        "native-manager-test",
        root_path=str(app_root),
        instance_path=str(instance_root),
    )
    monkeypatch.setattr(addon_manager_core.atexit, "register", lambda *_args: None)
    monkeypatch.setattr(
        addon_manager_core,
        "get_cached_frontend_workspace_status",
        lambda _root: None,
    )
    return app


def _attach_installation(app: Flask, installation_id: str, root: Path) -> None:
    app.extensions["carlina_paths"] = SimpleNamespace(
        installation_id=installation_id,
        data_root=root,
        installed_addons_dir=root / "addons" / "installed",
    )


def _workbench_manager(app: Flask, monkeypatch: pytest.MonkeyPatch) -> AddonManager:
    from iarf_assistant.core import addon_manager as facade

    monkeypatch.setattr(
        facade,
        "get_cached_frontend_workspace_status",
        lambda _root: None,
    )
    return cast("AddonManager", create_addon_manager(app, container_services=True))


def _native_manager(app: Flask) -> NativeAddonManager:
    return cast(
        "NativeAddonManager", create_addon_manager(app, container_services=False)
    )


def test_native_selection_does_not_import_or_probe_container_backend(
    manager_app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked = {
        "iarf_assistant.core.addons.container_runtime",
        "iarf_assistant.core.addons.container_orchestration",
    }
    imported_before = {name for name in blocked if name in sys.modules}
    original_import = builtins.__import__

    def guarded_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name in blocked:
            raise AssertionError("Native manager imported a container backend.")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    sys.modules.pop("iarf_assistant.core.addons.native_manager", None)

    manager = _native_manager(manager_app)

    assert type(manager).__name__ == "NativeAddonManager"
    assert {name for name in blocked if name in sys.modules} == imported_before
    assert manager.docker_status_checked is False
    assert manager.container_runtime_available(force=True) is False
    assert manager.docker_status_checked is False


def test_factory_selects_explicit_workbench_and_native_classes(
    manager_app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    from iarf_assistant.core.addon_manager import AddonManager
    from iarf_assistant.core.addons.container_orchestration import (
        ContainerOrchestrationMixin,
    )
    from iarf_assistant.core.addons.native_manager import NativeAddonManager

    workbench = _workbench_manager(manager_app, monkeypatch)
    native = _native_manager(manager_app)

    assert type(workbench) is AddonManager
    assert type(native) is NativeAddonManager
    assert issubclass(AddonManager, AddonManagerCore)
    assert issubclass(AddonManager, ContainerOrchestrationMixin)
    assert issubclass(NativeAddonManager, AddonManagerCore)
    assert issubclass(NativeAddonManager, ContainerLifecycleSupportMixin)
    assert not issubclass(NativeAddonManager, ContainerOrchestrationMixin)
    assert ContainerOrchestrationMixin in AddonManager.__bases__
    assert ContainerLifecycleSupportMixin not in AddonManagerCore.__bases__
    manager_parameters = inspect.signature(AddonManager).parameters
    assert tuple(manager_parameters) == (
        "app",
        "manage_lifecycle",
        "allow_lifecycle_maintenance",
        "allowed_addon_ids",
        "container_services",
    )
    assert manager_parameters["manage_lifecycle"].default is True
    assert manager_parameters["allow_lifecycle_maintenance"].default is False
    assert manager_parameters["allowed_addon_ids"].default is None
    assert manager_parameters["container_services"].default is True
    assert (
        inspect.signature(NativeAddonManager).parameters["container_services"].default
        is False
    )
    assert (
        inspect.signature(create_addon_manager).parameters["container_services"].default
        is True
    )


def test_workbench_facade_preserves_legacy_exports_and_cache_monkeypatch(
    manager_app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from iarf_assistant.core import addon_manager as facade

    roots: list[Path] = []
    status = {"addon_id": "doc_processor", "status": "built"}

    def frontend_status(root: Path) -> dict[str, list[dict[str, str]]]:
        roots.append(root)
        return {"addons": [status]}

    monkeypatch.setattr(facade, "get_cached_frontend_workspace_status", frontend_status)
    manager = create_addon_manager(manager_app, container_services=True)

    assert facade.time is system_time
    assert facade.atexit is system_atexit
    assert facade.get_cached_frontend_workspace_status is frontend_status
    assert facade._compact_scope_alias("Doc-Processor") == "docprocessor"
    assert manager._frontend_build_status_cache == {"doc_processor": status}
    assert roots == [Path(manager_app.root_path).resolve().parent]


@pytest.mark.parametrize("container_services", [1, "false", None])
def test_selection_rejects_non_boolean_before_accessing_app(
    container_services: object,
) -> None:
    class UnreadableApp:
        @property
        def root_path(self) -> str:
            raise AssertionError("Invalid selection accessed the app.")

    with pytest.raises(TypeError, match="selection must be a boolean"):
        create_addon_manager(
            cast(Flask, UnreadableApp()),
            container_services=cast(bool, container_services),
        )


@pytest.mark.parametrize("container_services", [1, True])
def test_native_manager_rejects_unsafe_selection_before_app_access(
    container_services: object,
) -> None:
    from iarf_assistant.core.addons.native_manager import NativeAddonManager

    class UnreadableApp:
        @property
        def root_path(self) -> str:
            raise AssertionError("Invalid selection accessed the app.")

    expected_error = ValueError if container_services is True else TypeError
    native_factory = cast(Callable[..., object], NativeAddonManager)
    with pytest.raises(expected_error):
        native_factory(
            cast(Flask, UnreadableApp()),
            container_services=cast(bool, container_services),
        )


def test_native_status_and_container_fields_are_explicitly_disabled(
    manager_app: Flask,
) -> None:
    manager = _native_manager(manager_app)

    assert manager.container_services_enabled is False
    assert manager._active_containers == {}
    assert manager._container_last_accessed_ts == {}
    assert manager.docker_client is None
    assert manager.docker_available is False
    assert manager.docker_status_message == DISABLED_MESSAGE
    assert manager._docker_backend == "none"
    assert manager._container_runtime_kind == "none"
    assert manager._docker_wsl_distro is None
    assert manager.docker_status_checked is False
    for backend_state in (
        "_container_lock",
        "_addon_container_locks",
        "_docker_status_checked_at",
        "_docker_status_lock",
        "_docker_status_ttl_seconds",
    ):
        assert not hasattr(manager, backend_state)
    assert manager.get_container_runtime_status() == {
        "available": False,
        "message": DISABLED_MESSAGE,
    }


@pytest.mark.parametrize(
    ("docker_config", "expected_lifecycle", "expected_services"),
    [
        ({"lifecycle": "always_on"}, "always_on", {"service"}),
        (
            {
                "lifecycle": "on_demand",
                "services": {
                    "required": {"lifecycle": "on_demand"},
                    "optional": {"lifecycle": "always_on"},
                    "ignored": "malformed",
                },
            },
            "always_on",
            {"required", "optional"},
        ),
        (
            {
                "lifecycle": "malformed",
                "services": {"service": {"lifecycle": "also-malformed"}},
            },
            "on_demand",
            {"service"},
        ),
        (
            {
                "lifecycle": "always_on",
                "services": {"service": {"lifecycle": "malformed"}},
            },
            "always_on",
            {"service"},
        ),
    ],
)
def test_native_and_workbench_share_nested_lifecycle_normalization(
    manager_app: Flask,
    monkeypatch: pytest.MonkeyPatch,
    docker_config: dict[str, Any],
    expected_lifecycle: str,
    expected_services: set[str],
) -> None:
    native = _native_manager(manager_app)
    workbench = _workbench_manager(manager_app, monkeypatch)

    native_services = native._get_docker_services(docker_config)
    workbench_services = workbench._get_docker_services(docker_config)
    assert set(native_services) == expected_services
    assert native_services == workbench_services
    assert (
        native._get_effective_stack_lifecycle(docker_config, native_services)
        == expected_lifecycle
    )
    assert (
        workbench._get_effective_stack_lifecycle(docker_config, workbench_services)
        == expected_lifecycle
    )


def test_native_marker_cleanup_is_installation_scoped_and_matches_workbench(
    manager_app: Flask,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iarf_assistant.core.addons import container_lifecycle_support

    marker_root = tmp_path / "markers"
    marker_root.mkdir()
    monkeypatch.setattr(
        container_lifecycle_support.tempfile,
        "gettempdir",
        lambda: str(marker_root),
    )
    _attach_installation(manager_app, "installation-a", tmp_path / "data-a")
    native = _native_manager(manager_app)
    workbench = _workbench_manager(manager_app, monkeypatch)

    other_app = Flask(
        "other-installation",
        root_path=manager_app.root_path,
        instance_path=manager_app.instance_path,
    )
    _attach_installation(other_app, "installation-b", tmp_path / "data-b")
    other = _native_manager(other_app)

    native_marker = native._container_access_marker_path("translator")
    workbench_marker = workbench._container_access_marker_path("translator")
    other_marker = other._container_access_marker_path("translator")
    assert native_marker == workbench_marker
    assert native_marker != other_marker

    native_marker.touch()
    other_marker.touch()
    native._active_containers["translator"] = object()
    native._container_last_accessed_ts["translator"] = 5.0
    native.reset_container("translator")

    assert not native_marker.exists()
    assert other_marker.exists()
    assert native._active_containers == {}
    assert native._container_last_accessed_ts == {}


def test_stale_marker_behavior_is_shared_by_both_compositions(
    manager_app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = _native_manager(manager_app)
    workbench = _workbench_manager(manager_app, monkeypatch)

    native.mark_docker_image_stale("translator", "v1")
    assert native._is_docker_image_stale("translator") is True
    assert workbench._is_docker_image_stale("translator") is True
    workbench._clear_docker_image_stale("translator")
    assert native._is_docker_image_stale("translator") is False


def test_native_marker_cleanup_warns_without_emitting_oserror_path(
    manager_app: Flask,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from iarf_assistant.core.addons import container_lifecycle_support

    marker_root = tmp_path / "markers"
    marker_root.mkdir()
    monkeypatch.setattr(
        container_lifecycle_support.tempfile,
        "gettempdir",
        lambda: str(marker_root),
    )
    _attach_installation(manager_app, "marker-installation", tmp_path / "data")
    manager = _native_manager(manager_app)
    marker = manager._container_access_marker_path("translator")
    original_unlink = Path.unlink

    def fail_marker_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == marker:
            raise PermissionError("synthetic marker failure")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_marker_unlink)
    manager._active_containers["translator"] = object()
    manager._container_last_accessed_ts["translator"] = 10.0

    with caplog.at_level(logging.WARNING):
        manager._stop_and_remove_container("translator")

    assert manager._active_containers == {}
    assert manager._container_last_accessed_ts == {}
    assert "PermissionError" in caplog.text
    assert str(marker) not in caplog.text
    assert "synthetic marker failure" not in caplog.text


def test_native_container_operations_fail_closed_without_backend_calls(
    manager_app: Flask,
) -> None:
    manager = _native_manager(manager_app)
    operations = (
        lambda: manager.get_container_url("translator"),
        lambda: manager.container_use("translator"),
        lambda: manager.touch_container_access("translator"),
        lambda: manager.prepare_container_images("translator"),
        lambda: manager._ensure_docker_image_prepared("translator"),
        lambda: manager.remove_container_image("image:tag"),
        lambda: manager.manage_container_service("translator", action="start"),
    )

    for operation in operations:
        with pytest.raises(RuntimeError, match=DISABLED_MESSAGE):
            operation()

    assert manager.container_runtime_available(force=True) is False
    assert manager._refresh_docker_status(force=True) is False
    assert manager.container_image_exists("image:tag") is False
    assert manager._docker_image_exists("image:tag") is False
    manager.reap_idle_containers()
    manager.start_always_on_containers()


def test_shutdown_runs_managed_cleanup_then_container_bookkeeping_cleanup(
    manager_app: Flask,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from unittest.mock import Mock

    from iarf_assistant.core.addons import container_lifecycle_support

    marker_root = tmp_path / "markers"
    marker_root.mkdir()
    monkeypatch.setattr(
        container_lifecycle_support.tempfile,
        "gettempdir",
        lambda: str(marker_root),
    )
    _attach_installation(manager_app, "shutdown-installation", tmp_path / "data")
    manager = _native_manager(manager_app)
    managed_cleanup = Mock()
    manager.cleanup_managed_processes = managed_cleanup
    access_marker = manager._container_access_marker_path("translator")
    access_marker.touch()
    manager._active_containers["translator"] = object()
    manager._container_last_accessed_ts["orphan"] = 10.0
    orphan_marker = manager._container_access_marker_path("orphan")
    orphan_marker.touch()

    manager.cleanup_workers()

    managed_cleanup.assert_called_once_with()
    assert manager._active_containers == {}
    assert manager._container_last_accessed_ts == {}
    assert not access_marker.exists()
    assert not orphan_marker.exists()
