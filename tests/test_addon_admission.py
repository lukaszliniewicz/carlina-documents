"""Admission follows the application's available runtime services."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from iarf_assistant.core.addons import admission
from iarf_assistant.core.addons.contracts import AddonSourceKind, compile_addon_contract


def _paths(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        staging_dir=tmp_path / "staging",
        addon_environments_dir=tmp_path / "environments",
    )


@pytest.fixture(autouse=True)
def _available_host_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        admission.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=64 * 1024**3),
    )
    monkeypatch.setattr(
        admission,
        "_available_memory_bytes",
        lambda: 16 * 1024**3,
    )


@pytest.mark.parametrize("mapping_contract", [False, True])
def test_documents_admits_native_segmenter_without_container_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mapping_contract: bool,
) -> None:
    assert admission.__file__ is not None
    addon_dir = (
        Path(admission.__file__).resolve().parents[2] / "addons/sentence_segmenter"
    )
    manifest = json.loads((addon_dir / "manifest.json").read_text())
    compiled = compile_addon_contract(
        manifest, addon_dir=addon_dir, source_kind=AddonSourceKind.BUILT_IN
    )
    contract = (
        {
            "manifest": manifest,
            "runtime_units": [
                {
                    "id": unit.id,
                    "mode": unit.mode.value,
                    "lifecycle": unit.lifecycle,
                    "requirements": dict(unit.requirements),
                }
                for unit in compiled.runtime_units
            ],
        }
        if mapping_contract
        else compiled
    )
    manager = SimpleNamespace(
        container_services_enabled=False,
        container_runtime_available=Mock(side_effect=AssertionError("Docker probe")),
    )
    gpu_probe = Mock(side_effect=AssertionError("Container GPU probe"))
    monkeypatch.setattr(admission, "_gpu_probe", gpu_probe)

    report = admission.evaluate_addon_admission(
        paths=_paths(tmp_path), contract=contract, manager=manager
    )

    assert report["allowed"], report["blocking_reasons"]
    assert report["container_runtime"]["required"] is False
    assert report["gpu"]["requirement"] == "none"
    manager.container_runtime_available.assert_not_called()
    gpu_probe.assert_not_called()


@pytest.mark.parametrize("lifecycle", ["always_on", "host_managed", "unknown", None])
def test_disabled_container_services_keep_required_or_unspecified_units_blocked(
    tmp_path: Path, lifecycle: str | None
) -> None:
    manager = SimpleNamespace(
        container_services_enabled=False,
        container_runtime_available=Mock(return_value=True),
    )
    report = admission.evaluate_addon_admission(
        paths=_paths(tmp_path),
        contract={"runtime_units": [{"mode": "container", "lifecycle": lifecycle}]},
        manager=manager,
    )

    assert report["allowed"] is False
    assert report["container_runtime"]["required"] is True
    assert (
        "This application does not include container services."
        in report["blocking_reasons"]
    )
    manager.container_runtime_available.assert_not_called()


@pytest.mark.parametrize("runtime_available", [False, True])
@pytest.mark.parametrize("explicit_services_flag", [False, True])
def test_workbench_preserves_on_demand_container_admission(
    tmp_path: Path, runtime_available: bool, explicit_services_flag: bool
) -> None:
    manager = SimpleNamespace(
        container_runtime_available=Mock(return_value=runtime_available)
    )
    if explicit_services_flag:
        manager.container_services_enabled = True
    report = admission.evaluate_addon_admission(
        paths=_paths(tmp_path),
        contract={"runtime_units": [{"mode": "container", "lifecycle": "on_demand"}]},
        manager=manager,
    )

    assert report["allowed"] is runtime_available
    assert report["container_runtime"]["required"] is True
    manager.container_runtime_available.assert_called_once_with(
        log_failure=False, force=True
    )


def test_native_profile_preserves_declared_disk_admission(tmp_path: Path) -> None:
    report = admission.evaluate_addon_admission(
        paths=_paths(tmp_path),
        contract={
            "runtime_units": [{"mode": "container", "lifecycle": "on_demand"}],
            "installation": {"required_disk_bytes": 128 * 1024**3},
        },
        manager=SimpleNamespace(container_services_enabled=False),
    )

    assert report["allowed"] is False
    assert report["container_runtime"]["required"] is False
    assert any(check["status"] == "blocked" for check in report["disk"])


def test_on_demand_container_stays_blocked_without_manager(tmp_path: Path) -> None:
    report = admission.evaluate_addon_admission(
        paths=_paths(tmp_path),
        contract={"runtime_units": [{"mode": "container", "lifecycle": "on_demand"}]},
    )

    assert report["allowed"] is False
    assert report["container_runtime"]["required"] is True
