"""Installer event and cancellation acceptance against disposable managed roots."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from filelock import FileLock
from filelock import Timeout as FileLockTimeout
from packaging.version import Version
from test_publisher_bootstrap_scripts import publisher_bootstrap_fixture

from carlina.core.bootstrap_ownership import (
    BOOTSTRAP_BINDING_NAME,
    BOOTSTRAP_CLAIM_NAME,
    BootstrapIdentity,
    bind_bootstrap_generation,
    bootstrap_claim_bytes,
)
from carlina.core.installation_lifecycle import (
    InstallationCancelled,
    InstallationErrorCode,
    InstallationEvent,
    InstallationLifecycle,
    InstallationOperation,
    InstallationPhase,
    InstallationState,
)
from carlina.core.publisher_bootstrap import PublisherBootstrapPlan
from carlina.core.publisher_bootstrap_scripts import render_publisher_bootstrap
from carlina.core.publisher_installation import PublisherBootstrapFailure
from carlina.profile_binding import bind_runtime_profile
from carlina.runtime_profiles import RuntimeProfile
from iarf_assistant import bootstrap_manager, distribution_cli
from iarf_assistant.core import app_paths as app_paths_module
from iarf_assistant.core import managed_install
from iarf_assistant.core.app_paths import AppPaths, resolve_app_paths
from iarf_assistant.core.encryption import InstallationKeyStore
from iarf_assistant.core.managed_install import (
    ManagedCompatibilityError,
    ManagedInstallationLayout,
    ManagedInstallError,
    ManagedReleaseManager,
)
from iarf_assistant.core.release_downloads import ReleaseDownloadError
from iarf_assistant.core.release_manifest import (
    ReleaseExecutionContract,
    parse_release_index,
)
from iarf_assistant.core.runtime_ownership import runtime_leader_lock

pytestmark = [pytest.mark.fast, pytest.mark.contract]


def _marker(paths: AppPaths, value: str | None = None) -> str:
    with sqlite3.connect(paths.database_file) as connection:
        if value is not None:
            connection.execute("UPDATE test_marker SET value = ?", (value,))
        row = connection.execute("SELECT value FROM test_marker").fetchone()
    assert row is not None
    return str(row[0])


def _seed_target(
    layout: ManagedInstallationLayout, version: str, profile: RuntimeProfile
) -> None:
    python = (
        layout.version_dir(version)
        / "env"
        / ("python.exe" if os.name == "nt" else "bin/python")
    )
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_bytes(b"synthetic interpreter, never executed")
    pixi = (
        layout.version_dir(version)
        / "env/bin"
        / ("pixi.exe" if os.name == "nt" else "pixi")
    )
    pixi.parent.mkdir(parents=True, exist_ok=True)
    pixi.write_bytes(b"synthetic Pixi, never executed")
    pixi.chmod(0o700)
    if profile is RuntimeProfile.DOCUMENTS:
        site = (
            Path("Lib/site-packages")
            if os.name == "nt"
            else Path("lib/python3.14/site-packages")
        )
        descriptor = (
            layout.version_dir(version) / "env" / site / "carlina/distribution.json"
        )
        descriptor.parent.mkdir(parents=True, exist_ok=True)
        descriptor.write_text(
            json.dumps(
                {
                    "format": "carlina.application-distribution",
                    "schema_version": 1,
                    "application_profile": "documents",
                    "source_revision": "a" * 40,
                    "source_date_epoch": 1_791_050_400,
                    "addon_ids": ["doc_processor", "sentence_segmenter", "translator"],
                }
            ),
            encoding="utf-8",
        )


def _case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: RuntimeProfile
) -> tuple[ManagedReleaseManager, Path, Path]:
    monkeypatch.setenv("CARLINA_PROFILE", profile.value)
    for prefix in ("CARLINA", "IARF"):
        for channel in ("STABLE", "PREVIEW"):
            monkeypatch.delenv(f"{prefix}_RELEASE_INDEX_{channel}_URL", raising=False)
    monkeypatch.setattr(
        app_paths_module,
        "_platform_roots",
        lambda _profile=None: tuple(
            tmp_path / name for name in ("data", "config", "cache", "logs")
        ),
    )
    data_root = tmp_path / "owned-data"
    bind_runtime_profile(data_root, profile)
    paths = resolve_app_paths(
        tmp_path / "source",
        package_root=tmp_path / "source/iarf_assistant",
        environment={"CARLINA_HOME": str(data_root), "CARLINA_PROFILE": profile.value},
        allow_legacy=False,
    )
    paths.ensure_directories()
    paths.write_storage_manifest()
    InstallationKeyStore(paths.master_keys_file).initialize()
    with sqlite3.connect(paths.database_file) as connection:
        connection.executescript(
            "CREATE TABLE alembic_version (version_num TEXT NOT NULL);"
            "INSERT INTO alembic_version VALUES ('rev1');"
            "CREATE TABLE test_marker (value TEXT NOT NULL);"
            "INSERT INTO test_marker VALUES ('before');"
        )
    manager = ManagedReleaseManager(
        paths, program_root=tmp_path / "program", platform_tag="win-64"
    )
    manager.layout.ensure()
    _seed_target(manager.layout, "1.0.0", profile)
    manager.layout.activate("1.0.0")
    monkeypatch.setattr(
        managed_install.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(
            total=16 * 1024**3, used=8 * 1024**3, free=8 * 1024**3
        ),
    )
    artifact = tmp_path / "synthetic-runtime.zip"
    artifact.write_bytes(b"reviewed synthetic runtime")
    index = tmp_path / "index.json"

    def write_index() -> None:
        index.write_text(
            json.dumps(
                {
                    "format": "carlina.release-index",
                    "schema_version": 1,
                    "channel": "stable",
                    "application_profile": profile.value,
                    "generated_at": "2026-10-18T12:00:00Z",
                    "releases": [
                        {
                            "version": "1.1.0",
                            "published_at": "2026-10-18T12:00:00Z",
                            "addon_api_version": "1.0",
                            "database": {
                                "known_revisions": ["rev1", "rev2"],
                                "heads": ["rev2"],
                                "rollback": "restore_required",
                            },
                            "artifacts": [
                                {
                                    "platform": "win-64",
                                    "kind": "pixi-pack-bundle",
                                    "filename": artifact.name,
                                    "url": artifact.name,
                                    "sha256": hashlib.sha256(
                                        artifact.read_bytes()
                                    ).hexdigest(),
                                    "size": artifact.stat().st_size,
                                }
                            ],
                        }
                    ],
                    "signatures": [],
                }
            ),
            encoding="utf-8",
        )

    write_index()

    def unpack(_artifact: Path, destination: Path, *, kind: str | None = None) -> None:
        assert kind == "pixi-pack-bundle"
        _seed_target(manager.layout, destination.name, profile)

    def verify(
        _layout: ManagedInstallationLayout,
        current_paths: AppPaths,
        _version: str,
        *,
        initialize_data: bool,
        **_options: object,
    ) -> None:
        if initialize_data:
            _marker(current_paths, "after")

    monkeypatch.setattr(managed_install, "_unpack_runtime", unpack)
    monkeypatch.setattr(managed_install, "_verify_target_runtime", verify)
    return manager, index, artifact


def _assert_released(manager: ManagedReleaseManager) -> None:
    with FileLock(str(manager.layout.lock_file), timeout=0):
        lease = runtime_leader_lock(manager.paths)
        lease.acquire(timeout=0)
        lease.release()


def _record_released_lock(
    manager: ManagedReleaseManager, witnesses: list[bool]
) -> None:
    _assert_released(manager)
    witnesses.append(True)


def _prepare_rollback(
    manager: ManagedReleaseManager,
    profile: RuntimeProfile,
    *,
    rollback_mode: str = "compatible",
    backup: dict[str, object] | None = None,
    database_checkpoint: dict[str, object] | None = None,
) -> bytes:
    current, previous = "1.1.0", "1.0.0"
    _seed_target(manager.layout, current, profile)
    receipt: dict[str, object] = {
        "format": "carlina.release-receipt",
        "schema_version": 3,
        "version": current,
        "previous_version": previous,
        "application_profile": profile.value,
        "database": {"rollback": rollback_mode},
    }
    manager.layout.receipt_file(previous).write_text(
        json.dumps(
            {
                "format": "carlina.release-receipt",
                "schema_version": 3,
                "version": previous,
                "application_profile": profile.value,
                "database": {"rollback": "compatible"},
            }
        ),
        encoding="utf-8",
    )
    if backup is not None:
        receipt["backup"] = backup
    if database_checkpoint is not None:
        receipt["database_checkpoint"] = database_checkpoint
    path = manager.layout.receipt_file(current)
    path.write_text(json.dumps(receipt), encoding="utf-8")
    manager.layout.activate(current)
    return path.read_bytes()


def _execute(
    manager: ManagedReleaseManager,
    index: Path,
    artifact: Path,
    lifecycle: InstallationLifecycle,
    route: str,
) -> None:
    if route == "update":
        manager.update(
            index,
            requested_version="1.1.0",
            allow_unsigned_local=True,
            lifecycle=lifecycle,
        )
    else:
        _seed_target(manager.layout, "1.1.0", manager.application_profile)
        manager.adopt_installed_release(
            index,
            artifact,
            manager.layout.version_dir("1.1.0"),
            requested_version="1.1.0",
            allow_unsigned_local=True,
            lifecycle=lifecycle,
        )


def _adopt(
    manager: ManagedReleaseManager,
    index: Path,
    artifact: Path,
    *,
    lifecycle: InstallationLifecycle | None = None,
    expected_index_sha256: str | None = None,
    retry_transaction_id: str | None = None,
    resume_bootstrap_claim_id: str | None = None,
    version_dir: Path | None = None,
) -> managed_install.UpdateResult:
    selected_version_dir = version_dir or manager.layout.version_dir("1.1.0")
    resume_options = (
        {"resume_bootstrap_claim_id": resume_bootstrap_claim_id}
        if resume_bootstrap_claim_id is not None
        else {}
    )
    return manager.adopt_installed_release(
        index,
        artifact,
        selected_version_dir,
        requested_version="1.1.0",
        expected_index_sha256=expected_index_sha256,
        retry_transaction_id=retry_transaction_id,
        **resume_options,
        allow_unsigned_local=True,
        lifecycle=lifecycle,
    )


def _retain_adoption(
    manager: ManagedReleaseManager,
    index: Path,
    artifact: Path,
    *,
    profile: RuntimeProfile,
    phase: str = "failed",
    lifecycle: InstallationLifecycle | None = None,
) -> tuple[str, dict[str, object]]:
    _seed_target(manager.layout, "1.1.0", profile)
    result = _adopt(manager, index, artifact, lifecycle=lifecycle)
    journal = json.loads(manager.layout.journal_file.read_text(encoding="utf-8"))
    manager.layout.activate("1.0.0")
    _marker(manager.paths, "before")
    if phase == "failed":
        journal["automatic_data_restore"] = {"completed": True, "coverage": "full"}
    else:
        journal.pop("automatic_data_restore", None)
    manager._write_journal(journal, phase=phase)
    retained = json.loads(manager.layout.journal_file.read_text(encoding="utf-8"))
    assert retained["transaction_id"] == result.transaction_id
    return result.transaction_id, retained


def _adoption_retry_snapshot(manager: ManagedReleaseManager) -> dict[str, object]:
    journal_path = manager.layout.journal_file
    journal_info = journal_path.lstat()
    target_path = manager.layout.versions_dir / "1.1.0"
    target_files = (
        {
            str(path.relative_to(target_path)): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in target_path.rglob("*")
            if path.is_file() and not path.is_symlink()
        }
        if target_path.is_dir() and not target_path.is_symlink()
        else None
    )
    backups = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in manager.paths.backups_dir.iterdir()
        if path.is_file()
    }
    return {
        "journal": journal_path.read_bytes(),
        "journal_is_symlink": stat.S_ISLNK(journal_info.st_mode),
        "current": manager.layout.current_file.read_bytes(),
        "marker": _marker(manager.paths),
        "target_is_symlink": target_path.is_symlink(),
        "target_files": target_files,
        "receipt": (
            (target_path / "release-receipt.json").read_bytes()
            if (target_path / "release-receipt.json").is_file()
            else None
        ),
        "backups": backups,
        "history": sorted(path.name for path in manager.layout.history_dir.iterdir()),
    }


def _run_adoption_retry(
    manager: ManagedReleaseManager,
    index: Path,
    artifact: Path,
    transaction_id: str,
    *,
    expected_index_sha256: str | None = None,
    version_dir: Path | None = None,
) -> managed_install.UpdateResult:
    return _adopt(
        manager,
        index,
        artifact,
        expected_index_sha256=(
            expected_index_sha256
            if expected_index_sha256 is not None
            else hashlib.sha256(index.read_bytes()).hexdigest()
        ),
        retry_transaction_id=transaction_id,
        version_dir=version_dir,
    )


@pytest.mark.parametrize("profile", list(RuntimeProfile))
@pytest.mark.parametrize("route", ["update", "install"])
@pytest.mark.parametrize("exception_type", [OSError, KeyboardInterrupt, SystemExit])
def test_pointer_switch_exception_preserves_committed_data_and_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
    route: str,
    exception_type: type[BaseException],
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, profile)
    if route == "install":
        _seed_target(manager.layout, "1.1.0", profile)
    exception = exception_type("synthetic lost activation acknowledgement")
    terminal_events: list[InstallationEvent] = []

    def observe(event: InstallationEvent) -> None:
        if event.state is not InstallationState.RUNNING:
            _assert_released(manager)
            terminal_events.append(event)

    lifecycle = InstallationLifecycle(route, profile, event_sink=observe)
    monkeypatch.setattr(
        manager,
        "_restore_failed_update_data",
        lambda *_args, **_options: pytest.fail(
            "post-commit failure restored already committed data"
        ),
    )
    layout_type = type(manager.layout)
    original_activate = layout_type.activate

    def switch_then_fail(layout: ManagedInstallationLayout, version: str) -> None:
        original_activate(layout, version)
        raise exception

    monkeypatch.setattr(layout_type, "activate", switch_then_fail)
    with pytest.raises(exception_type) as raised:
        _execute(manager, index, artifact, lifecycle, route)
    assert raised.value is exception
    assert lifecycle.terminal
    assert len(terminal_events) == 1
    assert terminal_events[0].state is InstallationState.FAILED

    assert manager.layout.current_version() == "1.1.0"
    assert _marker(manager.paths) == "after"
    receipt_path = manager.layout.receipt_file("1.1.0")
    assert receipt_path.is_file()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    journal = json.loads(manager.layout.journal_file.read_text(encoding="utf-8"))
    assert receipt["transaction_id"] == journal["transaction_id"]
    assert Path(receipt["backup"]["path"]).is_file()
    assert journal["activation_commit_observed"] is True
    assert manager.layout.read_pending_update() is None


@pytest.mark.parametrize("profile", list(RuntimeProfile))
@pytest.mark.parametrize("route", ["update", "install"])
def test_pre_pointer_activation_failure_keeps_existing_restore_behavior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
    route: str,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, profile)
    if route == "install":
        _seed_target(manager.layout, "1.1.0", profile)
    restore_calls: list[bool] = []
    original_restore = manager._restore_failed_update_data

    def record_restore(*args: object, **kwargs: object) -> object:
        restore_calls.append(True)
        return original_restore(*args, **kwargs)

    monkeypatch.setattr(manager, "_restore_failed_update_data", record_restore)
    layout_type = type(manager.layout)
    original_activate = layout_type.activate

    def fail_before_switch(layout: ManagedInstallationLayout, version: str) -> None:
        if layout.root == manager.layout.root and version == "1.1.0":
            raise OSError("synthetic failure before pointer switch")
        original_activate(layout, version)

    monkeypatch.setattr(layout_type, "activate", fail_before_switch)
    with pytest.raises(OSError, match="synthetic failure before pointer switch"):
        _execute(manager, index, artifact, InstallationLifecycle(route, profile), route)

    assert restore_calls == [True]
    assert manager.layout.current_version() == "1.0.0"
    assert _marker(manager.paths) == "before"
    assert not manager.layout.receipt_file("1.1.0").exists()
    assert manager.layout.read_pending_update() is None
    _assert_released(manager)


@pytest.mark.parametrize("profile", list(RuntimeProfile))
def test_expected_index_hash_is_optional_for_first_adoption_and_roots_are_recorded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, profile)
    _seed_target(manager.layout, "1.1.0", profile)
    index_sha = hashlib.sha256(index.read_bytes()).hexdigest()

    result = _adopt(
        manager,
        index,
        artifact,
        expected_index_sha256=index_sha,
    )

    journal = json.loads(manager.layout.journal_file.read_text(encoding="utf-8"))
    receipt = manager.layout.read_receipt(result.active_version)
    expected_roots = {
        "program_root": str(manager.layout.root.resolve()),
        "data_root": str(manager.paths.data_root.resolve()),
    }
    assert {key: journal[key] for key in expected_roots} == expected_roots
    assert {key: receipt[key] for key in expected_roots} == expected_roots
    assert "retried_adoption_transaction_id" not in journal
    assert "retried_adoption_transaction_id" not in receipt


@pytest.mark.parametrize("profile", list(RuntimeProfile))
def test_committed_adoption_retry_reconciles_original_transaction_idempotently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, profile)
    _seed_target(manager.layout, "1.1.0", profile)
    initial_lifecycle = InstallationLifecycle("install", profile)
    original = _adopt(manager, index, artifact, lifecycle=initial_lifecycle)
    index_sha = hashlib.sha256(index.read_bytes()).hexdigest()
    original_journal = json.loads(
        manager.layout.journal_file.read_text(encoding="utf-8")
    )
    original_receipt = manager.layout.receipt_file("1.1.0").read_bytes()
    backup = Path(original_journal["backup"]["path"])
    backup_hash = hashlib.sha256(backup.read_bytes()).hexdigest()
    terminal_witnesses: list[bool] = []

    def forbidden(*_args: object, **_kwargs: object) -> object:
        pytest.fail("committed reconciliation entered recovery or a new adoption")

    monkeypatch.setattr(manager, "_recover_interrupted_database_switches", forbidden)
    monkeypatch.setattr(manager, "_recover_interrupted_rollback", forbidden)
    monkeypatch.setattr(manager, "_recover_interrupted_update", forbidden)
    monkeypatch.setattr(manager, "_archive_existing_journal", forbidden)
    monkeypatch.setattr(manager, "_create_managed_backup", forbidden)
    monkeypatch.setattr(manager, "_restore_failed_update_data", forbidden)

    for _ in range(2):
        lifecycle = InstallationLifecycle(
            "install",
            profile,
            event_sink=lambda event: (
                _record_released_lock(manager, terminal_witnesses)
                if event.state is not InstallationState.RUNNING
                else None
            ),
        )
        result = _adopt(
            manager,
            index,
            artifact,
            lifecycle=lifecycle,
            expected_index_sha256=index_sha,
            retry_transaction_id=original.transaction_id,
        )
        assert result.transaction_id == original.transaction_id
        assert result.active_version == "1.1.0"
        assert lifecycle.terminal
        assert result.backup_path == backup

    journal = json.loads(manager.layout.journal_file.read_text(encoding="utf-8"))
    assert journal["transaction_id"] == original.transaction_id
    assert journal["operation_id"] == initial_lifecycle.operation_id
    assert journal["last_reconciled_operation_id"] != initial_lifecycle.operation_id
    assert journal["phase"] == "completed"
    assert manager.layout.receipt_file("1.1.0").read_bytes() == original_receipt
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == backup_hash
    assert len(terminal_witnesses) == 2
    assert not tuple(manager.layout.history_dir.iterdir())


@pytest.mark.parametrize("profile", list(RuntimeProfile))
def test_clean_pre_target_adoption_failure_retries_with_new_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, profile)
    _seed_target(manager.layout, "1.1.0", profile)
    original_verify = managed_install._verify_unpacked_application_profile
    verify_calls = 0

    def fail_once(
        layout: ManagedInstallationLayout,
        version: str,
        expected_profile: RuntimeProfile,
    ) -> None:
        nonlocal verify_calls
        verify_calls += 1
        if verify_calls == 1:
            raise OSError("synthetic pre-target verification failure")
        original_verify(layout, version, expected_profile)

    monkeypatch.setattr(
        managed_install, "_verify_unpacked_application_profile", fail_once
    )
    original_lifecycle = InstallationLifecycle("install", profile)
    with pytest.raises(OSError, match="synthetic pre-target verification failure"):
        _adopt(manager, index, artifact, lifecycle=original_lifecycle)
    old_journal_bytes = manager.layout.journal_file.read_bytes()
    old_journal = json.loads(old_journal_bytes)
    assert old_journal["phase"] == "failed"
    assert not manager.layout.receipt_file("1.1.0").exists()
    index_sha = hashlib.sha256(index.read_bytes()).hexdigest()
    new_lifecycle = InstallationLifecycle("install", profile)

    result = _adopt(
        manager,
        index,
        artifact,
        lifecycle=new_lifecycle,
        expected_index_sha256=index_sha,
        retry_transaction_id=old_journal["transaction_id"],
    )

    assert result.transaction_id != old_journal["transaction_id"]
    assert manager.layout.current_version() == "1.1.0"
    assert _marker(manager.paths) == "after"
    new_journal = json.loads(manager.layout.journal_file.read_text(encoding="utf-8"))
    receipt = manager.layout.read_receipt("1.1.0")
    assert (
        new_journal["retried_adoption_transaction_id"] == old_journal["transaction_id"]
    )
    assert receipt["retried_adoption_transaction_id"] == old_journal["transaction_id"]
    assert new_journal["transaction_id"] == result.transaction_id
    assert new_journal["operation_id"] == new_lifecycle.operation_id
    assert new_journal["operation_id"] != original_lifecycle.operation_id
    archived = (
        manager.layout.history_dir / f"update-{old_journal['transaction_id']}.json"
    )
    assert (
        json.loads(archived.read_bytes())["transaction_id"]
        == old_journal["transaction_id"]
    )


@pytest.mark.parametrize("profile", list(RuntimeProfile))
def test_post_target_failed_restored_adoption_retries_without_losing_old_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, profile)
    _seed_target(manager.layout, "1.1.0", profile)
    verify_calls = 0

    def fail_initialization_once(
        _layout: ManagedInstallationLayout,
        paths: AppPaths,
        _version: str,
        *,
        initialize_data: bool,
        **_options: object,
    ) -> None:
        nonlocal verify_calls
        if not initialize_data:
            return
        verify_calls += 1
        if verify_calls == 1:
            _marker(paths, "uncommitted")
            raise OSError("synthetic post-target health failure")
        _marker(paths, "after")

    monkeypatch.setattr(
        managed_install, "_verify_target_runtime", fail_initialization_once
    )
    original_lifecycle = InstallationLifecycle("install", profile)
    with pytest.raises(OSError, match="synthetic post-target health failure"):
        _adopt(manager, index, artifact, lifecycle=original_lifecycle)
    old_journal = json.loads(manager.layout.journal_file.read_text(encoding="utf-8"))
    assert old_journal["phase"] == "failed"
    assert old_journal["automatic_data_restore"]["completed"] is True
    assert _marker(manager.paths) == "before"
    old_backup = Path(old_journal["backup"]["path"])
    old_backup_hash = hashlib.sha256(old_backup.read_bytes()).hexdigest()
    new_lifecycle = InstallationLifecycle("install", profile)

    result = _adopt(
        manager,
        index,
        artifact,
        lifecycle=new_lifecycle,
        expected_index_sha256=hashlib.sha256(index.read_bytes()).hexdigest(),
        retry_transaction_id=old_journal["transaction_id"],
    )

    assert result.transaction_id != old_journal["transaction_id"]
    assert manager.layout.current_version() == "1.1.0"
    assert _marker(manager.paths) == "after"
    assert hashlib.sha256(old_backup.read_bytes()).hexdigest() == old_backup_hash
    new_journal = json.loads(manager.layout.journal_file.read_text(encoding="utf-8"))
    assert (
        new_journal["retried_adoption_transaction_id"] == old_journal["transaction_id"]
    )
    assert new_journal["operation_id"] != original_lifecycle.operation_id


@pytest.mark.parametrize("profile", list(RuntimeProfile))
def test_activation_pending_adoption_retry_uses_existing_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, profile)
    original_lifecycle = InstallationLifecycle("install", profile)
    original_tx, _journal = _retain_adoption(
        manager,
        index,
        artifact,
        profile=profile,
        phase="activation_pending",
        lifecycle=original_lifecycle,
    )
    backup_path = Path(_journal["backup"]["path"])
    backup_hash = hashlib.sha256(backup_path.read_bytes()).hexdigest()

    result = _adopt(
        manager,
        index,
        artifact,
        lifecycle=InstallationLifecycle("install", profile),
        expected_index_sha256=hashlib.sha256(index.read_bytes()).hexdigest(),
        retry_transaction_id=original_tx,
    )

    assert result.transaction_id != original_tx
    assert result.previous_version == "1.0.0"
    assert manager.layout.current_version() == "1.1.0"
    assert _marker(manager.paths) == "after"
    assert hashlib.sha256(backup_path.read_bytes()).hexdigest() == backup_hash
    new_journal = json.loads(manager.layout.journal_file.read_text(encoding="utf-8"))
    assert new_journal["retried_adoption_transaction_id"] == original_tx
    assert new_journal["operation_id"] != original_lifecycle.operation_id
    archived = manager.layout.history_dir / f"update-{original_tx}.json"
    assert json.loads(archived.read_bytes())["transaction_id"] == original_tx


@pytest.mark.parametrize("lock_kind", ["program", "data"])
def test_retained_adoption_retry_respects_independent_subprocess_locks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lock_kind: str,
) -> None:
    profile = RuntimeProfile.DOCUMENTS
    manager, index, artifact = _case(tmp_path, monkeypatch, profile)
    transaction_id, _journal = _retain_adoption(
        manager,
        index,
        artifact,
        profile=profile,
    )
    if lock_kind == "program":
        lock_value = str(manager.layout.lock_file)
        script = (
            "import sys; from filelock import FileLock; "
            "lock = FileLock(sys.argv[1], timeout=0); lock.acquire(); "
            "print('ready', flush=True); sys.stdin.readline(); lock.release()"
        )
    else:
        lock_value = str(manager.paths.data_root)
        script = (
            "import sys; from pathlib import Path; from types import SimpleNamespace; "
            "from iarf_assistant.core.runtime_ownership import runtime_leader_lock; "
            "paths = SimpleNamespace(data_root=Path(sys.argv[1])); "
            "lock = runtime_leader_lock(paths, timeout=0); lock.acquire(); "
            "print('ready', flush=True); sys.stdin.readline(); lock.release()"
        )
    child = subprocess.Popen(
        [sys.executable, "-c", script, lock_value],
        cwd=Path(__file__).parents[1],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    assert child.stdin is not None
    try:
        assert child.stdout.readline().strip() == "ready"
        before = _adoption_retry_snapshot(manager)
        with pytest.raises(ManagedInstallError):
            _run_adoption_retry(manager, index, artifact, transaction_id)
        assert _adoption_retry_snapshot(manager) == before
    finally:
        child.stdin.write("release\n")
        child.stdin.flush()
        child.wait(timeout=10)
    assert child.returncode == 0


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("application_profile", "documents"),
        ("program_root", "/tmp/wrong-program-root"),
        ("data_root", "/tmp/wrong-data-root"),
        ("channel", "preview"),
        ("signer_key_id", "untrusted-signer"),
        ("index_sha256", "0" * 64),
        ("release_index_schema_version", 4),
        ("release_contract_sha256", "0" * 64),
    ],
)
def test_retained_adoption_journal_identity_conflicts_fail_before_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    replacement: object,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, RuntimeProfile.WORKBENCH)
    transaction_id, journal = _retain_adoption(
        manager,
        index,
        artifact,
        profile=RuntimeProfile.WORKBENCH,
    )
    journal[field] = replacement
    manager.layout.journal_file.write_text(json.dumps(journal), encoding="utf-8")
    before = _adoption_retry_snapshot(manager)

    with pytest.raises(ManagedInstallError):
        _run_adoption_retry(manager, index, artifact, transaction_id)

    assert _adoption_retry_snapshot(manager) == before


@pytest.mark.parametrize("field", ["program_root", "data_root"])
def test_legacy_unbound_adoption_journal_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, RuntimeProfile.DOCUMENTS)
    transaction_id, journal = _retain_adoption(
        manager,
        index,
        artifact,
        profile=RuntimeProfile.DOCUMENTS,
    )
    journal.pop(field)
    manager.layout.journal_file.write_text(json.dumps(journal), encoding="utf-8")
    before = _adoption_retry_snapshot(manager)

    with pytest.raises(ManagedInstallError):
        _run_adoption_retry(manager, index, artifact, transaction_id)

    assert _adoption_retry_snapshot(manager) == before


@pytest.mark.parametrize("field", ["sha256", "filename", "size", "adopted_version_dir"])
def test_retained_adoption_journal_artifact_conflict_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, RuntimeProfile.DOCUMENTS)
    transaction_id, journal = _retain_adoption(
        manager,
        index,
        artifact,
        profile=RuntimeProfile.DOCUMENTS,
    )
    selected = journal["artifact"]
    assert isinstance(selected, dict)
    if field == "adopted_version_dir":
        selected[field] = "/tmp/wrong-version"
    elif field == "sha256":
        selected[field] = "0" * 64
    elif field == "filename":
        selected[field] = "different.zip"
    else:
        selected[field] = int(selected[field]) + 1
    manager.layout.journal_file.write_text(json.dumps(journal), encoding="utf-8")
    before = _adoption_retry_snapshot(manager)

    with pytest.raises(ManagedInstallError):
        _run_adoption_retry(manager, index, artifact, transaction_id)

    assert _adoption_retry_snapshot(manager) == before


@pytest.mark.parametrize(
    "phase",
    [
        "runtime_probe_pending",
        "target_verification",
        "isolated_probe_pending",
        "unknown-phase",
        None,
    ],
)
def test_retained_adoption_rejects_inflight_or_unknown_phases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str | None,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, RuntimeProfile.WORKBENCH)
    transaction_id, journal = _retain_adoption(
        manager,
        index,
        artifact,
        profile=RuntimeProfile.WORKBENCH,
    )
    if phase is None:
        journal.pop("phase")
    else:
        journal["phase"] = phase
    manager.layout.journal_file.write_text(json.dumps(journal), encoding="utf-8")
    before = _adoption_retry_snapshot(manager)

    with pytest.raises(ManagedInstallError):
        _run_adoption_retry(manager, index, artifact, transaction_id)

    assert _adoption_retry_snapshot(manager) == before


def test_retained_adoption_unknown_activation_outcome_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, RuntimeProfile.DOCUMENTS)
    transaction_id, journal = _retain_adoption(
        manager,
        index,
        artifact,
        profile=RuntimeProfile.DOCUMENTS,
    )
    journal["activation_outcome_unknown"] = True
    manager.layout.journal_file.write_text(json.dumps(journal), encoding="utf-8")
    before = _adoption_retry_snapshot(manager)

    with pytest.raises(ManagedInstallError):
        _run_adoption_retry(manager, index, artifact, transaction_id)

    assert _adoption_retry_snapshot(manager) == before


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("transaction_id", "f" * 32),
        ("version", "1.2.0"),
        ("application_profile", "documents"),
        ("index_sha256", "0" * 64),
        ("program_root", "/tmp/wrong-program-root"),
        ("data_root", "/tmp/wrong-data-root"),
        ("release_contract_sha256", "0" * 64),
    ],
)
def test_retained_adoption_receipt_conflicts_fail_before_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    replacement: object,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, RuntimeProfile.WORKBENCH)
    transaction_id, _journal = _retain_adoption(
        manager,
        index,
        artifact,
        profile=RuntimeProfile.WORKBENCH,
    )
    receipt_path = manager.layout.receipt_file("1.1.0")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt[field] = replacement
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    before = _adoption_retry_snapshot(manager)

    with pytest.raises(ManagedInstallError):
        _run_adoption_retry(manager, index, artifact, transaction_id)

    assert _adoption_retry_snapshot(manager) == before


@pytest.mark.parametrize("field", ["version", "application_profile"])
def test_committed_retry_requires_receipt_version_and_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, RuntimeProfile.DOCUMENTS)
    _seed_target(manager.layout, "1.1.0", RuntimeProfile.DOCUMENTS)
    result = _adopt(manager, index, artifact)
    receipt_path = manager.layout.receipt_file("1.1.0")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt[field] = "1.2.0" if field == "version" else "workbench"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    before = _adoption_retry_snapshot(manager)

    with pytest.raises(ManagedInstallError):
        _run_adoption_retry(manager, index, artifact, result.transaction_id)

    assert _adoption_retry_snapshot(manager) == before


@pytest.mark.parametrize(
    "journal_kind", ["malformed", "duplicate", "oversized", "symlink"]
)
def test_retained_adoption_rejects_unsafe_or_malformed_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    journal_kind: str,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, RuntimeProfile.DOCUMENTS)
    transaction_id, journal = _retain_adoption(
        manager,
        index,
        artifact,
        profile=RuntimeProfile.DOCUMENTS,
    )
    journal_path = manager.layout.journal_file
    if journal_kind == "malformed":
        journal_path.write_bytes(b"{")
    elif journal_kind == "duplicate":
        journal_path.write_bytes(b'{"format":"one","format":"two"}')
    elif journal_kind == "oversized":
        journal_path.write_bytes(b" " * (1024 * 1024 + 1))
    else:
        retained_path = manager.layout.state_dir / "retained-journal.json"
        retained_path.write_text(json.dumps(journal), encoding="utf-8")
        journal_path.unlink()
        journal_path.symlink_to(retained_path)
    before = _adoption_retry_snapshot(manager)

    with pytest.raises(ManagedInstallError):
        _run_adoption_retry(manager, index, artifact, transaction_id)

    assert _adoption_retry_snapshot(manager) == before


@pytest.mark.parametrize("record", ["journal", "receipt"])
@pytest.mark.parametrize("field", ["previous_version", "signer_key_id"])
def test_committed_adoption_requires_present_nullable_identity_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record: str,
    field: str,
) -> None:
    profile = RuntimeProfile.DOCUMENTS
    manager, index, artifact = _case(tmp_path, monkeypatch, profile)
    manager.layout.current_file.unlink()
    _seed_target(manager.layout, "1.1.0", profile)
    original = _adopt(manager, index, artifact)
    assert original.first_install
    assert _run_adoption_retry(
        manager, index, artifact, original.transaction_id
    ).first_install
    path = (
        manager.layout.journal_file
        if record == "journal"
        else manager.layout.receipt_file("1.1.0")
    )
    payload = json.loads(path.read_bytes())
    assert payload.pop(field) is None
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = _adoption_retry_snapshot(manager)

    with pytest.raises(ManagedInstallError):
        _run_adoption_retry(manager, index, artifact, original.transaction_id)

    assert _adoption_retry_snapshot(manager) == before


@pytest.mark.parametrize("kind", ["malformed", "duplicate", "oversized", "symlink"])
def test_retained_adoption_receipt_has_the_same_bounded_record_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    profile = RuntimeProfile.DOCUMENTS
    manager, index, artifact = _case(tmp_path, monkeypatch, profile)
    transaction_id, _journal = _retain_adoption(
        manager, index, artifact, profile=profile
    )
    path = manager.layout.receipt_file("1.1.0")
    original = path.read_bytes()
    if kind == "malformed":
        path.write_bytes(b"{")
    elif kind == "duplicate":
        assert original.rstrip().endswith(b"}")
        path.write_bytes(
            original.rstrip()[:-1] + b', "format": "carlina.release-receipt"}'
        )
    elif kind == "oversized":
        path.write_bytes(original + b" " * (1024 * 1024 + 1))
    else:
        retained = manager.layout.state_dir / "retained-receipt.json"
        retained.write_bytes(original)
        path.unlink()
        path.symlink_to(retained)
    before = _adoption_retry_snapshot(manager)

    with pytest.raises(ManagedInstallError):
        _run_adoption_retry(manager, index, artifact, transaction_id)

    assert _adoption_retry_snapshot(manager) == before


@pytest.mark.parametrize("symlink_kind", ["versions", "target", "argument"])
def test_retained_adoption_rejects_generation_symlinks_before_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    symlink_kind: str,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, RuntimeProfile.WORKBENCH)
    transaction_id, _journal = _retain_adoption(
        manager,
        index,
        artifact,
        profile=RuntimeProfile.WORKBENCH,
    )
    version_dir: Path | None = None
    if symlink_kind == "versions":
        real_versions = manager.layout.root / "versions-real"
        manager.layout.versions_dir.rename(real_versions)
        manager.layout.versions_dir.symlink_to(real_versions, target_is_directory=True)
    elif symlink_kind == "target":
        target = manager.layout.versions_dir / "1.1.0"
        real_target = manager.layout.versions_dir / "1.1.0-real"
        target.rename(real_target)
        target.symlink_to(real_target, target_is_directory=True)
    else:
        version_dir = tmp_path / "target-alias"
        version_dir.symlink_to(
            manager.layout.version_dir("1.1.0"), target_is_directory=True
        )
    before = _adoption_retry_snapshot(manager)

    with pytest.raises(ManagedInstallError):
        _run_adoption_retry(
            manager,
            index,
            artifact,
            transaction_id,
            version_dir=version_dir,
        )

    assert _adoption_retry_snapshot(manager) == before


@pytest.mark.parametrize("fence", ["recovery", "rollback"])
def test_retained_adoption_rejects_unrelated_recovery_fences(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fence: str,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, RuntimeProfile.DOCUMENTS)
    transaction_id, _journal = _retain_adoption(
        manager,
        index,
        artifact,
        profile=RuntimeProfile.DOCUMENTS,
    )
    if fence == "recovery":
        payload = {
            "format": managed_install.RECOVERY_REQUIRED_FORMAT,
            "schema_version": managed_install.RECOVERY_REQUIRED_VERSION,
            "status": "required",
            "transaction_id": "a" * 32,
            "failed_target_version": "1.2.0",
        }
        manager.layout.recovery_required_file.write_text(
            json.dumps(payload), encoding="utf-8"
        )
    else:
        payload = {
            "format": managed_install.ROLLBACK_TRANSACTION_FORMAT,
            "schema_version": managed_install.ROLLBACK_TRANSACTION_VERSION,
            "phase": "prepared",
            "transaction_id": "b" * 32,
            "coverage": "full",
            "from_version": "1.0.0",
            "to_version": "1.2.0",
        }
        manager.layout.rollback_transaction_file.write_text(
            json.dumps(payload), encoding="utf-8"
        )
    before = _adoption_retry_snapshot(manager)

    with pytest.raises(ManagedInstallError):
        _run_adoption_retry(manager, index, artifact, transaction_id)

    assert _adoption_retry_snapshot(manager) == before


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("malformed", [False, True])
def test_retained_adoption_rejects_unrelated_database_switch_before_any_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    committed: bool,
    malformed: bool,
) -> None:
    profile = RuntimeProfile.DOCUMENTS
    manager, index, artifact = _case(tmp_path, monkeypatch, profile)
    transaction_id, _journal = _retain_adoption(
        manager, index, artifact, profile=profile
    )
    if committed:
        manager.layout.activate("1.1.0")
    switch = (
        manager.paths.state_dir
        / "database-checkpoint-recovery"
        / "checkpoint-restore-unrelated"
    )
    switch.mkdir(parents=True)
    witness = switch / "switch.json"
    witness.write_text(
        "{"
        if malformed
        else json.dumps(
            {
                "format": "carlina.database-checkpoint-switch",
                "schema_version": 1,
                "transaction_id": "unrelated",
                "created_at": "2026-10-24T00:00:00Z",
                "updated_at": "2026-10-24T00:00:00Z",
                "phase": "prepared",
                "destination_database": str(manager.paths.database_file),
                "original_database_sha256": hashlib.sha256(
                    manager.paths.database_file.read_bytes()
                ).hexdigest(),
                "checkpoint_database_sha256": "a" * 64,
                "original_sidecars": [],
                "failed_database_name": "failed.sqlite3",
            }
        ),
        encoding="utf-8",
    )
    witness_before = witness.read_bytes()
    before = _adoption_retry_snapshot(manager)
    database_before = hashlib.sha256(
        manager.paths.database_file.read_bytes()
    ).hexdigest()
    events: list[InstallationEvent] = []
    context = InstallationLifecycle("install", profile, event_sink=events.append)

    with pytest.raises(ManagedInstallError):
        _adopt(
            manager,
            index,
            artifact,
            lifecycle=context,
            expected_index_sha256=hashlib.sha256(index.read_bytes()).hexdigest(),
            retry_transaction_id=transaction_id,
        )

    assert events[-1].error_code is InstallationErrorCode.RECOVERY_REQUIRED
    assert not events[-1].cancellation_safe
    _assert_released(manager)
    assert witness.read_bytes() == witness_before
    assert _adoption_retry_snapshot(manager) == before
    assert (
        hashlib.sha256(manager.paths.database_file.read_bytes()).hexdigest()
        == database_before
    )


def test_committed_adoption_readback_tolerates_failed_diagnostic_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = RuntimeProfile.DOCUMENTS
    manager, index, artifact = _case(tmp_path, monkeypatch, profile)
    _seed_target(manager.layout, "1.1.0", profile)
    original = _adopt(manager, index, artifact)
    before = _adoption_retry_snapshot(manager)

    def fail_diagnostics(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic unavailable diagnostic write")

    monkeypatch.setattr(manager, "_write_journal", fail_diagnostics)
    events: list[InstallationEvent] = []
    context = InstallationLifecycle("install", profile, event_sink=events.append)
    result = _adopt(
        manager,
        index,
        artifact,
        lifecycle=context,
        expected_index_sha256=hashlib.sha256(index.read_bytes()).hexdigest(),
        retry_transaction_id=original.transaction_id,
    )

    assert result == original
    assert events[-1].state is InstallationState.SUCCEEDED
    assert context.terminal
    assert _adoption_retry_snapshot(manager) == before
    _assert_released(manager)


def test_retained_adoption_rejects_wrong_current_target_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, RuntimeProfile.WORKBENCH)
    transaction_id, _journal = _retain_adoption(
        manager,
        index,
        artifact,
        profile=RuntimeProfile.WORKBENCH,
    )
    _seed_target(manager.layout, "1.2.0", RuntimeProfile.WORKBENCH)
    manager.layout.activate("1.2.0")
    before = _adoption_retry_snapshot(manager)

    with pytest.raises(ManagedInstallError):
        _run_adoption_retry(manager, index, artifact, transaction_id)

    assert _adoption_retry_snapshot(manager) == before


@pytest.mark.parametrize(
    "failure",
    [
        "wrong_hash",
        "missing_hash",
        "missing_version",
        "wrong_tx",
        "invalid_hash",
        "invalid_tx",
        "artifact_drift",
    ],
)
def test_retry_input_failure_preserves_retained_adoption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, RuntimeProfile.DOCUMENTS)
    transaction_id, _journal = _retain_adoption(
        manager,
        index,
        artifact,
        profile=RuntimeProfile.DOCUMENTS,
    )
    expected_sha = hashlib.sha256(index.read_bytes()).hexdigest()
    if failure == "artifact_drift":
        artifact.write_bytes(b"different synthetic artifact")
    before = _adoption_retry_snapshot(manager)

    with pytest.raises((ManagedInstallError, managed_install.ReleaseManifestError)):
        if failure == "wrong_hash":
            _run_adoption_retry(
                manager, index, artifact, transaction_id, expected_index_sha256="0" * 64
            )
        elif failure == "missing_hash":
            _adopt(
                manager,
                index,
                artifact,
                retry_transaction_id=transaction_id,
            )
        elif failure == "missing_version":
            manager.adopt_installed_release(
                index,
                artifact,
                manager.layout.version_dir("1.1.0"),
                expected_index_sha256=expected_sha,
                retry_transaction_id=transaction_id,
                allow_unsigned_local=True,
            )
        elif failure == "wrong_tx":
            _run_adoption_retry(manager, index, artifact, "f" * 32)
        elif failure == "invalid_hash":
            _adopt(
                manager,
                index,
                artifact,
                expected_index_sha256="A" * 64,
                retry_transaction_id=transaction_id,
            )
        elif failure == "invalid_tx":
            _adopt(
                manager,
                index,
                artifact,
                expected_index_sha256=expected_sha,
                retry_transaction_id="g" * 32,
            )
        else:
            _run_adoption_retry(manager, index, artifact, transaction_id)

    assert _adoption_retry_snapshot(manager) == before
    assert expected_sha == hashlib.sha256(index.read_bytes()).hexdigest()


@pytest.mark.parametrize("profile", list(RuntimeProfile))
def test_retained_adoption_retry_accepts_same_artifact_at_a_new_temp_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, profile)
    transaction_id, _journal = _retain_adoption(
        manager,
        index,
        artifact,
        profile=profile,
    )
    alternate_artifact = tmp_path / "retry-source-copy.zip"
    alternate_artifact.write_bytes(artifact.read_bytes())

    result = _run_adoption_retry(
        manager,
        index,
        alternate_artifact,
        transaction_id,
    )

    assert result.transaction_id != transaction_id
    assert manager.layout.current_version() == "1.1.0"
    journal = json.loads(manager.layout.journal_file.read_text(encoding="utf-8"))
    assert journal["retried_adoption_transaction_id"] == transaction_id


@pytest.mark.parametrize("profile", list(RuntimeProfile))
@pytest.mark.parametrize("route", ["update", "install"])
def test_unreadable_activation_outcome_preserves_data_receipt_and_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
    route: str,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, profile)
    if route == "install":
        _seed_target(manager.layout, "1.1.0", profile)
    original_activate = type(manager.layout).activate

    def switch_then_corrupt_pointer(
        layout: ManagedInstallationLayout,
        version: str,
    ) -> None:
        original_activate(layout, version)
        layout.current_file.write_text("not-a-version\n", encoding="utf-8")
        raise OSError("synthetic unreadable activation acknowledgement")

    monkeypatch.setattr(type(manager.layout), "activate", switch_then_corrupt_pointer)
    terminal_events: list[InstallationEvent] = []

    def observe(event: InstallationEvent) -> None:
        if event.state is not InstallationState.RUNNING:
            _assert_released(manager)
            terminal_events.append(event)

    lifecycle = InstallationLifecycle(route, profile, event_sink=observe)
    with pytest.raises(
        OSError, match="synthetic unreadable activation acknowledgement"
    ):
        _execute(manager, index, artifact, lifecycle, route)

    journal = json.loads(manager.layout.journal_file.read_text(encoding="utf-8"))
    receipt_path = manager.layout.receipt_file("1.1.0")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert journal["activation_outcome_unknown"] is True
    assert journal["transaction_id"] == receipt["transaction_id"]
    assert manager.layout.current_file.read_text(encoding="utf-8") == "not-a-version\n"
    assert _marker(manager.paths) == "after"
    assert Path(receipt["backup"]["path"]).is_file()
    pending = manager.layout.read_pending_update()
    assert pending is not None and pending["activation_outcome_unknown"] is True
    assert lifecycle.terminal
    assert terminal_events[-1].error_code is InstallationErrorCode.RECOVERY_REQUIRED


def test_isolated_no_data_activation_uncertainty_fences_without_recovery_material(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = RuntimeProfile.DOCUMENTS
    manager, index_path, artifact = _case(tmp_path, monkeypatch, profile)
    _seed_target(manager.layout, "1.1.0", profile)
    parsed_index = parse_release_index(index_path.read_bytes())
    release = parsed_index.releases[0]
    execution = ReleaseExecutionContract(
        data_impact="none",
        migration_mode="none",
        bootstrap_mode="none",
        rollback_mode="compatible",
        automatic_recovery="none",
        probe_protocol="carlina.managed-release-probe",
        probe_schema_version=1,
        probe_isolation="test-isolation",
    )
    isolated_index = replace(
        parsed_index,
        schema_version=2,
        releases=(replace(release, execution=execution),),
    )
    index_sha = hashlib.sha256(index_path.read_bytes()).hexdigest()
    monkeypatch.setattr(
        manager,
        "_verified_index",
        lambda *_args, **_kwargs: (isolated_index, "test-signer", index_sha),
    )
    monkeypatch.setattr(
        manager,
        "_probe_capability",
        lambda *_args, **_kwargs: SimpleNamespace(available=True),
    )
    attestation = {
        "format": "carlina.managed-release-probe-attestation",
        "schema_version": 1,
        "status": "passed",
        "protocol": execution.probe_protocol,
        "probe_schema_version": execution.probe_schema_version,
        "isolation": execution.probe_isolation,
        "target_version": "1.1.0",
    }
    monkeypatch.setattr(
        managed_install,
        "run_managed_release_probe",
        lambda **_kwargs: SimpleNamespace(to_dict=lambda: attestation),
    )
    original_activate = type(manager.layout).activate

    def switch_then_corrupt_pointer(
        layout: ManagedInstallationLayout,
        version: str,
    ) -> None:
        original_activate(layout, version)
        layout.current_file.write_text("not-a-version\n", encoding="utf-8")
        raise OSError("synthetic isolated activation ambiguity")

    monkeypatch.setattr(type(manager.layout), "activate", switch_then_corrupt_pointer)
    terminal_events: list[InstallationEvent] = []
    lifecycle = InstallationLifecycle(
        "install",
        profile,
        event_sink=lambda event: (
            terminal_events.append(event)
            if event.state is not InstallationState.RUNNING
            else None
        ),
    )
    with pytest.raises(OSError, match="synthetic isolated activation ambiguity"):
        manager.adopt_installed_release(
            index_path,
            artifact,
            manager.layout.version_dir("1.1.0"),
            requested_version="1.1.0",
            allow_unsigned_local=True,
            lifecycle=lifecycle,
        )

    journal = json.loads(manager.layout.journal_file.read_text(encoding="utf-8"))
    receipt = manager.layout.read_receipt("1.1.0")
    assert journal["recovery_plan"]["selected_coverage"] == "none"
    assert journal["isolated_target_execution_started"] is True
    assert journal["activation_outcome_unknown"] is True
    assert receipt["transaction_id"] == journal["transaction_id"]
    assert receipt["backup"] is None
    pending = manager.layout.read_pending_update()
    assert pending is not None and pending["activation_outcome_unknown"] is True
    assert lifecycle.terminal
    assert terminal_events[-1].error_code is InstallationErrorCode.RECOVERY_REQUIRED
    _assert_released(manager)


@pytest.mark.parametrize("conflict", ["profile", "version"])
def test_unknown_activation_commit_shortcut_requires_matching_receipt_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    conflict: str,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, RuntimeProfile.DOCUMENTS)
    _seed_target(manager.layout, "1.1.0", RuntimeProfile.DOCUMENTS)
    result = _adopt(manager, index, artifact)
    journal = json.loads(manager.layout.journal_file.read_text(encoding="utf-8"))
    journal["activation_outcome_unknown"] = True
    manager._write_journal(journal, phase="failed")
    assert manager.layout.read_pending_update() is None

    receipt_path = manager.layout.receipt_file("1.1.0")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["application_profile" if conflict == "profile" else "version"] = (
        "workbench" if conflict == "profile" else "1.2.0"
    )
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    pending = manager.layout.read_pending_update()
    assert pending is not None
    assert pending["transaction_id"] == result.transaction_id


@pytest.mark.parametrize("profile", list(RuntimeProfile))
def test_retry_rejects_artifact_drift_before_archiving_old_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, profile)
    transaction_id, _journal = _retain_adoption(
        manager,
        index,
        artifact,
        profile=profile,
    )
    old_journal = manager.layout.journal_file.read_bytes()
    artifact.write_bytes(b"different authenticated artifact")

    with pytest.raises(ManagedInstallError, match="artifact"):
        _run_adoption_retry(manager, index, artifact, transaction_id)

    assert manager.layout.journal_file.read_bytes() == old_journal
    assert not tuple(manager.layout.history_dir.iterdir())
    assert manager.layout.current_version() == "1.0.0"
    assert manager.layout.receipt_file("1.1.0").is_file()


@pytest.mark.parametrize("profile", list(RuntimeProfile))
def test_retry_rejects_changed_index_before_archiving_old_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, profile)
    transaction_id, _journal = _retain_adoption(
        manager,
        index,
        artifact,
        profile=profile,
    )
    old_index_hash = hashlib.sha256(index.read_bytes()).hexdigest()
    old_journal = manager.layout.journal_file.read_bytes()
    index.write_bytes(index.read_bytes() + b"\n")

    with pytest.raises(managed_install.ReleaseManifestError):
        _run_adoption_retry(
            manager,
            index,
            artifact,
            transaction_id,
            expected_index_sha256=old_index_hash,
        )

    assert manager.layout.journal_file.read_bytes() == old_journal
    assert not tuple(manager.layout.history_dir.iterdir())
    assert manager.layout.current_version() == "1.0.0"


@pytest.mark.parametrize("profile", list(RuntimeProfile))
def test_download_cancel_cleans_before_terminal_and_exact_input_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, profile)
    artifact.write_bytes(b"x" * (4 * 1024**2 + 17))
    payload = json.loads(index.read_text(encoding="utf-8"))
    selected = payload["releases"][0]["artifacts"][0]
    selected["size"] = artifact.stat().st_size
    selected["sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    index.write_text(json.dumps(payload), encoding="utf-8")
    index_sha = hashlib.sha256(index.read_bytes()).hexdigest()
    cancel = threading.Event()
    events: list[InstallationEvent] = []
    terminal_witnesses: list[bool] = []

    def observe(event: InstallationEvent) -> None:
        events.append(event)
        if event.bytes_completed:
            cancel.set()
        if event.state is InstallationState.CANCELLED:
            assert not tuple(manager.layout.staging_dir.iterdir())
            assert manager.layout.current_version() == "1.0.0"
            assert _marker(manager.paths) == "before"
            _assert_released(manager)
            terminal_witnesses.append(True)

    lifecycle = InstallationLifecycle(
        "update", profile, event_sink=observe, cancel_requested=cancel.is_set
    )
    with pytest.raises(InstallationCancelled):
        _execute(manager, index, artifact, lifecycle, "update")

    assert events[-1].state is InstallationState.CANCELLED
    assert terminal_witnesses == [True]
    assert events[-1].retryable
    assert manager.layout.read_pending_update() is None
    assert manager.layout.read_recovery_requirement() is None
    assert not manager.layout.version_dir("1.1.0").exists()
    assert hashlib.sha256(index.read_bytes()).hexdigest() == index_sha
    journal = json.loads(manager.layout.journal_file.read_text(encoding="utf-8"))
    assert journal["phase"] == "failed"
    assert journal["operation_id"] == lifecycle.operation_id

    retried: list[InstallationEvent] = []
    _execute(
        manager,
        index,
        artifact,
        InstallationLifecycle("update", profile, event_sink=retried.append),
        "update",
    )
    assert retried[-1].state is InstallationState.SUCCEEDED
    assert manager.layout.current_version() == "1.1.0"
    assert _marker(manager.paths) == "after"
    assert manager.layout.read_receipt("1.1.0")["index_sha256"] == index_sha
    assert [event.sequence for event in retried] == list(range(1, len(retried) + 1))
    assert any(event.bytes_completed == artifact.stat().st_size for event in retried)


@pytest.mark.parametrize("profile", list(RuntimeProfile))
@pytest.mark.parametrize("route", ["update", "install"])
def test_late_cancel_finishes_activation_and_reports_after_locks_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
    route: str,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, profile)
    cancel = threading.Event()
    events: list[InstallationEvent] = []
    terminal_witnesses: list[bool] = []

    def verify(
        _layout: ManagedInstallationLayout,
        paths: AppPaths,
        _version: str,
        *,
        initialize_data: bool,
        **options: object,
    ) -> None:
        context = options["lifecycle"]
        assert isinstance(context, InstallationLifecycle)
        assert not context.cancellation_safe
        cancel.set()
        if initialize_data:
            _marker(paths, "after")

    def observe(event: InstallationEvent) -> None:
        events.append(event)
        if event.state is InstallationState.SUCCEEDED:
            _assert_released(manager)
            assert manager.layout.current_version() == "1.1.0"
            assert manager.layout.read_pending_update() is None
            terminal_witnesses.append(True)

    monkeypatch.setattr(managed_install, "_verify_target_runtime", verify)
    lifecycle = InstallationLifecycle(
        route, profile, event_sink=observe, cancel_requested=cancel.is_set
    )
    _execute(manager, index, artifact, lifecycle, route)
    assert cancel.is_set()
    assert events[-1].state is InstallationState.SUCCEEDED
    assert terminal_witnesses == [True]
    assert not any(event.state is InstallationState.CANCELLED for event in events)
    assert (
        manager.layout.read_receipt("1.1.0")["operation_id"] == lifecycle.operation_id
    )
    assert _marker(manager.paths) == "after"


@pytest.mark.parametrize("profile", list(RuntimeProfile))
@pytest.mark.parametrize("route", ["update", "install"])
@pytest.mark.parametrize("restore_fails", [False, True])
def test_target_failure_reports_restored_or_fenced_state_without_private_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
    route: str,
    *,
    restore_fails: bool,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, profile)
    events: list[InstallationEvent] = []
    terminal_witnesses: list[bool] = []

    def verify(
        _layout: ManagedInstallationLayout,
        paths: AppPaths,
        _version: str,
        *,
        initialize_data: bool,
        **_options: object,
    ) -> None:
        if initialize_data:
            _marker(paths, "migrated")
            raise ManagedInstallError(
                "synthetic private diagnostic must not enter events"
            )

    def fail_restore(*_args: object, **_options: object) -> None:
        raise ManagedInstallError("synthetic recovery failure")

    def observe(event: InstallationEvent) -> None:
        events.append(event)
        if event.state is InstallationState.FAILED:
            _assert_released(manager)
            assert _marker(manager.paths) == ("migrated" if restore_fails else "before")
            assert manager.layout.current_version() == "1.0.0"
            assert not manager.layout.receipt_file("1.1.0").exists()
            terminal_witnesses.append(True)

    monkeypatch.setattr(managed_install, "_verify_target_runtime", verify)
    if restore_fails:
        monkeypatch.setattr(manager, "_restore_failed_update_data", fail_restore)
    with pytest.raises(ManagedInstallError, match="private diagnostic"):
        _execute(
            manager,
            index,
            artifact,
            InstallationLifecycle(route, profile, event_sink=observe),
            route,
        )
    assert events[-1].state is InstallationState.FAILED
    assert terminal_witnesses == [True]
    assert events[-1].error_code is (
        InstallationErrorCode.RECOVERY_REQUIRED
        if restore_fails
        else InstallationErrorCode.OPERATION_FAILED
    )
    assert events[-1].cancellation_safe is not restore_fails
    assert (manager.layout.read_pending_update() is not None) is restore_fails
    assert "private diagnostic" not in "".join(event.json_line() for event in events)
    assert not events[-1].retryable


@pytest.mark.parametrize("profile", list(RuntimeProfile))
def test_cancel_after_unpack_retains_backup_and_prior_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, profile)
    cancel = threading.Event()
    events: list[InstallationEvent] = []

    def observe(event: InstallationEvent) -> None:
        events.append(event)
        if event.phase is InstallationPhase.UNPACKING and not event.cancellation_safe:
            cancel.set()

    with pytest.raises(InstallationCancelled):
        _execute(
            manager,
            index,
            artifact,
            InstallationLifecycle(
                "update", profile, event_sink=observe, cancel_requested=cancel.is_set
            ),
            "update",
        )
    assert events[-1].state is InstallationState.CANCELLED
    assert _marker(manager.paths) == "before"
    assert manager.layout.current_version() == "1.0.0"
    assert not manager.layout.version_dir("1.1.0").exists()
    journal = json.loads(manager.layout.journal_file.read_text(encoding="utf-8"))
    assert Path(journal["backup"]["path"]).is_file()
    assert manager.layout.read_pending_update() is None
    _assert_released(manager)


def test_competing_owner_and_low_space_fail_without_target_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, RuntimeProfile.DOCUMENTS)
    acquired, release = threading.Event(), threading.Event()

    def hold_lock() -> None:
        with FileLock(str(manager.layout.lock_file), timeout=0):
            acquired.set()
            assert release.wait(15)

    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert acquired.wait(15)
    events: list[InstallationEvent] = []
    try:
        with pytest.raises(ManagedInstallError, match="Another managed"):
            _execute(
                manager,
                index,
                artifact,
                InstallationLifecycle("update", "documents", event_sink=events.append),
                "update",
            )
    finally:
        release.set()
        thread.join(15)
    assert not thread.is_alive()
    assert events[-1].state is InstallationState.FAILED
    assert manager.layout.current_version() == "1.0.0"
    monkeypatch.setattr(
        managed_install.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=16, used=16, free=0),
    )
    with pytest.raises(ManagedCompatibilityError):
        _execute(
            manager,
            index,
            artifact,
            InstallationLifecycle("update", "documents", event_sink=events.append),
            "update",
        )
    assert events[-1].error_code is InstallationErrorCode.INCOMPATIBLE_RELEASE
    assert _marker(manager.paths) == "before"
    assert not manager.layout.version_dir("1.1.0").exists()


@pytest.mark.parametrize("profile", list(RuntimeProfile))
def test_adoption_keeps_constrained_migration_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, profile)
    payload = json.loads(index.read_text(encoding="utf-8"))
    payload["schema_version"] = 2
    payload["releases"][0]["execution"] = {
        "data_impact": "migration",
        "migration": {"mode": "constrained_data_only"},
        "bootstrap": {"mode": "none"},
        "rollback": {"mode": "restore_required"},
        "recovery": {"automatic_coverage": "database_only"},
        "probe": {
            "protocol": "carlina.managed-release-probe",
            "schema_version": 1,
            "isolation": "host_filesystem_and_network_denied_v1",
        },
    }
    verified = parse_release_index(payload)
    monkeypatch.setattr(
        manager,
        "_verified_index",
        lambda *_args, **_options: (verified, "synthetic-trusted-signer", "a" * 64),
    )
    events: list[InstallationEvent] = []
    with pytest.raises(ManagedCompatibilityError, match="coordinator"):
        _execute(
            manager,
            index,
            artifact,
            InstallationLifecycle("install", profile, event_sink=events.append),
            "install",
        )
    assert events[-1].error_code is InstallationErrorCode.INCOMPATIBLE_RELEASE
    assert _marker(manager.paths) == "before"
    assert manager.layout.current_version() == "1.0.0"
    assert manager.layout.read_pending_update() is None


def _arguments(arguments: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    distribution_cli.add_distribution_parsers(
        parser.add_subparsers(dest="command", required=True)
    )
    return parser.parse_args(arguments)


def test_cli_jsonl_cancel_and_source_failure_keep_stdout_machine_readable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manager, index, _artifact = _case(tmp_path, monkeypatch, RuntimeProfile.DOCUMENTS)
    monkeypatch.setattr(distribution_cli, "_manager", lambda _args, _paths: manager)
    cancel = tmp_path / "cancel-request"
    cancel.touch()
    args = _arguments(
        [
            "update",
            "--manifest",
            str(index),
            "--allow-unsigned-local",
            "--events-jsonl",
            "--operation-id",
            "a" * 32,
            "--cancel-file",
            str(cancel),
        ]
    )
    assert distribution_cli.handle_distribution_command(args, manager.paths) == 130
    captured = capsys.readouterr()
    events = [json.loads(line) for line in captured.out.splitlines()]
    assert events[-1]["state"] == "cancelled"
    assert {event["operation_id"] for event in events} == {"a" * 32}
    assert "safe boundary" in captured.err
    assert manager.layout.current_version() == "1.0.0"

    args = _arguments(["update", "--channel", "preview", "--events-jsonl"])
    assert distribution_cli.handle_distribution_command(args, manager.paths) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out)["error_code"] == "invalid_source"
    assert captured.err == "Compatibility check failed.\n"


@pytest.mark.parametrize(
    "arguments",
    [
        ["update", "--json", "--events-jsonl"],
        [
            "install",
            "--artifact",
            "a",
            "--adopt-version-dir",
            "b",
            "--json",
            "--events-jsonl",
        ],
    ],
)
def test_json_and_jsonl_are_mutually_exclusive(arguments: list[str]) -> None:
    with pytest.raises(SystemExit):
        _arguments(arguments)


@pytest.mark.parametrize(
    "controls",
    [
        ["--cancel-file", "request"],
        ["--operation-id", "a" * 32],
        ["--check", "--events-jsonl"],
    ],
)
def test_cli_event_control_validation_precedes_manager_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    controls: list[str],
) -> None:
    manager, _index, _artifact = _case(tmp_path, monkeypatch, RuntimeProfile.DOCUMENTS)
    monkeypatch.setattr(
        distribution_cli,
        "_manager",
        lambda *_args: pytest.fail("invalid controls reached manager"),
    )
    args = _arguments(["update", *controls])
    assert distribution_cli.handle_distribution_command(args, manager.paths) == 1
    captured = capsys.readouterr()
    assert not args.events_jsonl or not captured.out


def test_runtime_phase_events_precede_initialization_and_readiness_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = managed_install._verify_target_runtime
    manager, _index, _artifact = _case(tmp_path, monkeypatch, RuntimeProfile.DOCUMENTS)
    _seed_target(manager.layout, "1.1.0", RuntimeProfile.DOCUMENTS)
    # Exercise the real verifier with every child process replaced by a fixture.
    context = InstallationLifecycle("update", "documents")
    observed: list[tuple[list[str], InstallationPhase]] = []

    def run(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        observed.append((command, context.phase))
        if command[-1] == "--version":
            output = "pixi 0.81.0\n"
        elif "m.version('carlina')" in command[-1]:
            output = "1.1.0\n"
        elif "print(profile)" in command[-1]:
            output = "documents\n"
        else:
            output = "{}\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(
        managed_install, "_target_pixi_version", lambda *_args: Version("0.81.0")
    )
    monkeypatch.setattr(managed_install, "_run_checked", run)
    verifier(
        manager.layout,
        manager.paths,
        "1.1.0",
        initialize_data=True,
        application_profile=RuntimeProfile.DOCUMENTS,
        lifecycle=context,
    )
    assert any(
        "init" in command and phase is InstallationPhase.INITIALIZATION
        for command, phase in observed
    )
    assert any(
        "doctor" in command and phase is InstallationPhase.READINESS
        for command, phase in observed
    )
    assert not context.cancellation_safe


def test_download_failure_has_retryable_terminal_code_after_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, RuntimeProfile.DOCUMENTS)

    def fail_download(*_args: object, **_options: object) -> None:
        raise ReleaseDownloadError("synthetic offline download")

    monkeypatch.setattr(managed_install, "download_release_artifact", fail_download)
    events: list[InstallationEvent] = []
    with pytest.raises(ReleaseDownloadError):
        _execute(
            manager,
            index,
            artifact,
            InstallationLifecycle("update", "documents", event_sink=events.append),
            "update",
        )
    assert events[-1].error_code is InstallationErrorCode.DOWNLOAD_FAILED
    assert events[-1].retryable
    assert not tuple(manager.layout.staging_dir.iterdir())
    _assert_released(manager)


@pytest.mark.parametrize("route", ["update", "install"])
def test_broken_event_pipe_does_not_abort_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, RuntimeProfile.DOCUMENTS)

    def broken_pipe(_event: InstallationEvent) -> None:
        raise BrokenPipeError("synthetic closed wrapper")

    lifecycle = InstallationLifecycle(route, "documents", event_sink=broken_pipe)
    _execute(manager, index, artifact, lifecycle, route)
    assert lifecycle.terminal
    assert manager.layout.current_version() == "1.1.0"
    assert _marker(manager.paths) == "after"
    assert manager.layout.read_pending_update() is None


@pytest.mark.parametrize("profile", list(RuntimeProfile))
def test_adoption_cancel_after_backup_never_restores_untouched_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, profile)
    cancel = threading.Event()
    events: list[InstallationEvent] = []

    def observe(event: InstallationEvent) -> None:
        events.append(event)
        if event.phase is InstallationPhase.ENVIRONMENT and event.cancellation_safe:
            cancel.set()

    monkeypatch.setattr(
        manager,
        "_restore_failed_update_data",
        lambda *_args, **_options: pytest.fail(
            "pre-target cancellation restored untouched data"
        ),
    )
    lifecycle = InstallationLifecycle(
        "install", profile, event_sink=observe, cancel_requested=cancel.is_set
    )
    with pytest.raises(InstallationCancelled):
        _execute(manager, index, artifact, lifecycle, "install")
    assert events[-1].state is InstallationState.CANCELLED
    journal = json.loads(manager.layout.journal_file.read_text(encoding="utf-8"))
    assert Path(journal["backup"]["path"]).is_file()
    assert "automatic_data_restore" not in journal
    assert not journal.get("target_execution_started")
    assert _marker(manager.paths) == "before"
    assert manager.layout.current_version() == "1.0.0"
    assert manager.layout.read_pending_update() is None
    _assert_released(manager)


@pytest.mark.parametrize("profile", list(RuntimeProfile))
@pytest.mark.parametrize("route", ["update", "install"])
def test_prior_recovery_journal_survives_interruption_before_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
    route: str,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, profile)
    backup = manager._create_managed_backup(
        "1.0.0", key_store=InstallationKeyStore(manager.paths.master_keys_file)
    )
    pending = {
        "format": managed_install.UPDATE_JOURNAL_FORMAT,
        "schema_version": managed_install.UPDATE_JOURNAL_VERSION,
        "transaction_id": "b" * 32,
        "operation": "update",
        "previous_version": "1.0.0",
        "target_version": "1.0.5",
        "target_execution_started": True,
        "migration_started": True,
        "recovery_plan": {"selected_coverage": "full"},
        "backup": {
            "path": str(backup.path),
            "sha256": backup.sha256,
            "backup_id": backup.backup_id,
        },
    }
    manager._write_journal(pending, phase="target_verification")
    original_journal = manager.layout.journal_file.read_bytes()
    _marker(manager.paths, "uncommitted")
    events: list[InstallationEvent] = []

    def interrupt(event: InstallationEvent) -> None:
        events.append(event)
        if (
            event.phase is InstallationPhase.RECOVERY
            and event.state is InstallationState.RUNNING
        ):
            raise KeyboardInterrupt

    lifecycle = InstallationLifecycle(route, profile, event_sink=interrupt)
    with pytest.raises(KeyboardInterrupt):
        _execute(manager, index, artifact, lifecycle, route)
    assert manager.layout.journal_file.read_bytes() == original_journal
    retained = manager.layout.read_pending_update()
    assert retained is not None and retained["transaction_id"] == "b" * 32
    assert _marker(manager.paths) == "uncommitted"
    assert events[-1].error_code is InstallationErrorCode.RECOVERY_REQUIRED
    assert not events[-1].cancellation_safe
    _assert_released(manager)

    observed_prior_data: list[str] = []

    def verify(
        _layout: ManagedInstallationLayout,
        paths: AppPaths,
        _version: str,
        *,
        initialize_data: bool,
        **_options: object,
    ) -> None:
        if initialize_data:
            _marker(paths, "after")
        else:
            observed_prior_data.append(_marker(paths))

    monkeypatch.setattr(managed_install, "_verify_target_runtime", verify)
    _execute(manager, index, artifact, InstallationLifecycle(route, profile), route)
    assert observed_prior_data == ["before"]
    assert _marker(manager.paths) == "after"
    assert manager.layout.current_version() == "1.1.0"
    assert manager.layout.read_pending_update() is None


@pytest.mark.parametrize(
    "controls",
    [["--stop"], ["--systemd-unit", "synthetic.service"], ["--check", "--stop"]],
)
def test_event_mode_refuses_shutdown_controls_before_any_runtime_access(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    controls: list[str],
) -> None:
    args = _arguments(["update", "--events-jsonl", *controls])
    monkeypatch.setattr(
        bootstrap_manager,
        "_active_paths",
        lambda: pytest.fail("event validation accessed runtime paths"),
    )
    assert bootstrap_manager._dispatch_command(args, argparse.ArgumentParser()) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert captured.err


def test_dispatched_event_stream_has_no_console_result_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manager, index, _artifact = _case(tmp_path, monkeypatch, RuntimeProfile.DOCUMENTS)
    monkeypatch.setattr(bootstrap_manager, "_active_paths", lambda: manager.paths)
    monkeypatch.setattr(distribution_cli, "_manager", lambda _args, _paths: manager)
    args = _arguments(
        [
            "update",
            "--manifest",
            str(index),
            "--version",
            "1.1.0",
            "--allow-unsigned-local",
            "--events-jsonl",
        ]
    )
    assert bootstrap_manager._dispatch_command(args, argparse.ArgumentParser()) == 0
    captured = capsys.readouterr()
    payloads = [json.loads(line) for line in captured.out.splitlines()]
    assert payloads[-1]["state"] == "succeeded"
    assert not captured.err
    assert manager.layout.current_version() == "1.1.0"


@pytest.mark.parametrize("profile", list(RuntimeProfile))
def test_rollback_without_lifecycle_preserves_target_verifier_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
) -> None:
    manager, _index, _artifact = _case(tmp_path, monkeypatch, profile)
    _prepare_rollback(manager, profile)
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        managed_install,
        "_verify_target_runtime",
        lambda *_args, **kwargs: calls.append(kwargs),
    )

    result = manager.rollback()

    assert result.active_version == "1.0.0"
    assert calls == [
        {"initialize_data": False, "application_profile": profile},
        {"initialize_data": True, "application_profile": profile},
    ]
    history = list(manager.layout.history_dir.glob("rollback-*.json"))
    assert len(history) == 1
    assert "operation_id" not in json.loads(history[0].read_text(encoding="utf-8"))


@pytest.mark.parametrize("profile", list(RuntimeProfile))
def test_pointer_rollback_lifecycle_orders_events_and_reports_after_unlock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
) -> None:
    manager, _index, _artifact = _case(tmp_path, monkeypatch, profile)
    _prepare_rollback(manager, profile)
    events: list[InstallationEvent] = []
    lock_witnesses: list[bool] = []

    def observe(event: InstallationEvent) -> None:
        events.append(event)
        if event.state in {InstallationState.SUCCEEDED, InstallationState.FAILED}:
            _record_released_lock(manager, lock_witnesses)

    operation_id = "f" * 32
    lifecycle = InstallationLifecycle(
        "rollback", profile, operation_id=operation_id, event_sink=observe
    )
    result = manager.rollback(lifecycle=lifecycle)

    phases = [event.phase for event in events]
    assert phases.index(InstallationPhase.RECOVERY) < phases.index(
        InstallationPhase.VERIFICATION
    )
    assert phases.index(InstallationPhase.PREFLIGHT, 2) < phases.index(
        InstallationPhase.VERIFICATION
    )
    assert (
        phases.index(InstallationPhase.VERIFICATION)
        < phases.index(InstallationPhase.ENVIRONMENT)
        < phases.index(InstallationPhase.ACTIVATION)
        < phases.index(InstallationPhase.COMPLETE)
    )
    assert result.active_version == "1.0.0"
    assert manager.layout.current_version() == "1.0.0"
    assert _marker(manager.paths) == "after"
    assert lifecycle.terminal
    assert (
        len([event for event in events if event.state is not InstallationState.RUNNING])
        == 1
    )
    assert events[-1].state is InstallationState.SUCCEEDED
    assert {event.operation_id for event in events} == {operation_id}
    assert lock_witnesses == [True]
    history = list(manager.layout.history_dir.glob("rollback-*.json"))
    assert len(history) == 1
    assert json.loads(history[0].read_text(encoding="utf-8"))["operation_id"] == (
        operation_id
    )


@pytest.mark.parametrize("profile", list(RuntimeProfile))
def test_rollback_cancellation_after_reconciliation_preserves_roots_and_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
) -> None:
    manager, _index, _artifact = _case(tmp_path, monkeypatch, profile)
    current_receipt = _prepare_rollback(manager, profile)
    previous_receipt = manager.layout.receipt_file("1.0.0").read_bytes()
    cancel = threading.Event()
    events: list[InstallationEvent] = []
    lock_witnesses: list[bool] = []
    preflight_count = 0
    target_calls: list[bool] = []

    def observe(event: InstallationEvent) -> None:
        nonlocal preflight_count
        events.append(event)
        if (
            event.state is InstallationState.RUNNING
            and event.phase is InstallationPhase.PREFLIGHT
        ):
            preflight_count += 1
            if preflight_count == 2:
                cancel.set()
        if event.state is InstallationState.CANCELLED:
            _record_released_lock(manager, lock_witnesses)

    monkeypatch.setattr(
        managed_install,
        "_verify_target_runtime",
        lambda *_args, **options: target_calls.append("lifecycle" in options),
    )
    lifecycle = InstallationLifecycle(
        "rollback", profile, event_sink=observe, cancel_requested=cancel.is_set
    )
    with pytest.raises(InstallationCancelled):
        manager.rollback(lifecycle=lifecycle)

    assert events[-1].state is InstallationState.CANCELLED
    assert target_calls == []
    assert manager.layout.current_version() == "1.1.0"
    assert _marker(manager.paths) == "before"
    assert manager.layout.receipt_file("1.1.0").read_bytes() == current_receipt
    assert manager.layout.receipt_file("1.0.0").read_bytes() == previous_receipt
    assert not manager.layout.rollback_transaction_file.exists()
    assert lock_witnesses == [True]


@pytest.mark.parametrize("profile", list(RuntimeProfile))
def test_rollback_finishes_if_cancel_is_requested_after_target_probe_starts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
) -> None:
    manager, _index, _artifact = _case(tmp_path, monkeypatch, profile)
    _prepare_rollback(manager, profile)
    cancel = threading.Event()
    events: list[InstallationEvent] = []
    target_calls: list[bool] = []

    def verify(
        _layout: ManagedInstallationLayout,
        paths: AppPaths,
        _version: str,
        *,
        initialize_data: bool,
        **options: object,
    ) -> None:
        context = options["lifecycle"]
        assert isinstance(context, InstallationLifecycle)
        assert not context.cancellation_safe
        target_calls.append(initialize_data)
        cancel.set()
        if initialize_data:
            _marker(paths, "rolled-back")

    monkeypatch.setattr(managed_install, "_verify_target_runtime", verify)
    lifecycle = InstallationLifecycle(
        "rollback", profile, event_sink=events.append, cancel_requested=cancel.is_set
    )
    manager.rollback(lifecycle=lifecycle)

    assert cancel.is_set()
    assert target_calls == [False, True]
    assert events[-1].state is InstallationState.SUCCEEDED
    assert not any(event.state is InstallationState.CANCELLED for event in events)
    assert manager.layout.current_version() == "1.0.0"
    assert _marker(manager.paths) == "rolled-back"


@pytest.mark.parametrize("profile", list(RuntimeProfile))
def test_rollback_restores_receipt_bound_full_backup_with_operation_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
) -> None:
    manager, _index, _artifact = _case(tmp_path, monkeypatch, profile)
    backup = manager._create_managed_backup(
        None, key_store=InstallationKeyStore(manager.paths.master_keys_file)
    )
    _prepare_rollback(
        manager,
        profile,
        rollback_mode="restore_required",
        backup={
            "path": str(backup.path),
            "sha256": backup.sha256,
            "backup_id": backup.backup_id,
        },
    )
    _marker(manager.paths, "current-data")
    observed_restored_data: list[str] = []

    def verify(
        _layout: ManagedInstallationLayout,
        paths: AppPaths,
        _version: str,
        *,
        initialize_data: bool,
        **_options: object,
    ) -> None:
        if initialize_data:
            observed_restored_data.append(_marker(paths))

    monkeypatch.setattr(managed_install, "_verify_target_runtime", verify)
    operation_id = "a" * 32
    lifecycle = InstallationLifecycle("rollback", profile, operation_id=operation_id)
    result = manager.rollback(restore_data=True, lifecycle=lifecycle)

    assert result.data_restored
    assert result.active_version == "1.0.0"
    assert observed_restored_data == ["before"]
    assert _marker(manager.paths) == "before"
    transaction = json.loads(
        manager.layout.rollback_transaction_file.read_text(encoding="utf-8")
    )
    assert transaction["transaction_id"] == result.transaction_id
    assert transaction["operation_id"] == operation_id
    assert transaction["coverage"] == "full"
    assert transaction["phase"] == "completed"
    history = list(manager.layout.history_dir.glob("rollback-*.json"))
    assert json.loads(history[0].read_text(encoding="utf-8"))["operation_id"] == (
        operation_id
    )


@pytest.mark.parametrize("profile", list(RuntimeProfile))
@pytest.mark.parametrize("with_lifecycle", [False, True])
def test_rollback_restores_database_only_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
    with_lifecycle: bool,
) -> None:
    manager, _index, _artifact = _case(tmp_path, monkeypatch, profile)
    checkpoint = manager._create_database_checkpoint(
        transaction_id="c" * 32,
        previous_version="1.0.0",
        target_version="1.1.0",
        key_store=InstallationKeyStore(manager.paths.master_keys_file),
    )
    _prepare_rollback(
        manager,
        profile,
        rollback_mode="restore_required",
        database_checkpoint=checkpoint.to_dict(),
    )
    _marker(manager.paths, "current-data")
    observed_restored_data: list[str] = []

    def verify(
        _layout: ManagedInstallationLayout,
        paths: AppPaths,
        _version: str,
        *,
        initialize_data: bool,
        **_options: object,
    ) -> None:
        if initialize_data:
            observed_restored_data.append(_marker(paths))

    monkeypatch.setattr(managed_install, "_verify_target_runtime", verify)
    lifecycle = InstallationLifecycle("rollback", profile) if with_lifecycle else None
    result = manager.rollback(restore_data=True, lifecycle=lifecycle)

    assert result.data_restored
    assert result.active_version == "1.0.0"
    assert observed_restored_data == ["before"]
    assert _marker(manager.paths) == "before"
    transaction = json.loads(
        manager.layout.rollback_transaction_file.read_text(encoding="utf-8")
    )
    assert transaction["coverage"] == "database_only"
    assert transaction["phase"] == "completed"
    history = list(manager.layout.history_dir.glob("rollback-*.json"))
    history_record = json.loads(history[0].read_text(encoding="utf-8"))
    if lifecycle is None:
        assert "operation_id" not in transaction
        assert "operation_id" not in history_record
    else:
        assert transaction["operation_id"] == lifecycle.operation_id
        assert history_record["operation_id"] == lifecycle.operation_id


@pytest.mark.parametrize("profile", list(RuntimeProfile))
def test_failed_post_restore_verification_reactivates_pre_rollback_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
) -> None:
    manager, _index, _artifact = _case(tmp_path, monkeypatch, profile)
    backup = manager._create_managed_backup(
        None, key_store=InstallationKeyStore(manager.paths.master_keys_file)
    )
    current_receipt = _prepare_rollback(
        manager,
        profile,
        rollback_mode="restore_required",
        backup={
            "path": str(backup.path),
            "sha256": backup.sha256,
            "backup_id": backup.backup_id,
        },
    )
    _marker(manager.paths, "current-data")
    events: list[InstallationEvent] = []
    lock_witnesses: list[bool] = []

    def verify(
        _layout: ManagedInstallationLayout,
        _paths: AppPaths,
        _version: str,
        *,
        initialize_data: bool,
        **_options: object,
    ) -> None:
        if initialize_data:
            raise ManagedInstallError("synthetic rollback verification failure")

    def observe(event: InstallationEvent) -> None:
        events.append(event)
        if event.state is InstallationState.FAILED:
            _record_released_lock(manager, lock_witnesses)

    monkeypatch.setattr(managed_install, "_verify_target_runtime", verify)
    operation_id = "b" * 32
    lifecycle = InstallationLifecycle(
        "rollback", profile, operation_id=operation_id, event_sink=observe
    )
    with pytest.raises(ManagedInstallError, match="verification failure"):
        manager.rollback(restore_data=True, lifecycle=lifecycle)

    assert events[-1].state is InstallationState.FAILED
    assert events[-1].error_code is InstallationErrorCode.OPERATION_FAILED
    assert lock_witnesses == [True]
    assert manager.layout.current_version() == "1.1.0"
    assert manager.layout.receipt_file("1.1.0").read_bytes() == current_receipt
    assert _marker(manager.paths) == "current-data"
    transaction = json.loads(
        manager.layout.rollback_transaction_file.read_text(encoding="utf-8")
    )
    assert transaction["phase"] == "aborted"
    assert transaction["operation_id"] == operation_id
    failed_history = list(manager.layout.history_dir.glob("rollback-failed-*.json"))
    assert len(failed_history) == 1
    failed_record = json.loads(failed_history[0].read_text(encoding="utf-8"))
    assert failed_record["operation_id"] == operation_id


@pytest.mark.parametrize("profile", list(RuntimeProfile))
@pytest.mark.parametrize("unreadable", [False, True])
def test_unresolved_rollback_fence_reports_recovery_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
    unreadable: bool,
) -> None:
    manager, _index, _artifact = _case(tmp_path, monkeypatch, profile)
    _prepare_rollback(manager, profile)
    if unreadable:
        manager.layout.rollback_transaction_file.write_text("{", encoding="utf-8")
    else:
        manager.layout.rollback_transaction_file.write_text(
            json.dumps(
                {
                    "format": managed_install.ROLLBACK_TRANSACTION_FORMAT,
                    "schema_version": managed_install.ROLLBACK_TRANSACTION_VERSION,
                    "transaction_id": "d" * 32,
                    "phase": "prepared",
                    "from_version": "1.1.0",
                    "to_version": "1.0.0",
                    "coverage": "full",
                }
            ),
            encoding="utf-8",
        )
    events: list[InstallationEvent] = []
    lifecycle = InstallationLifecycle("rollback", profile, event_sink=events.append)

    with pytest.raises(ManagedInstallError):
        manager.rollback(lifecycle=lifecycle)

    assert events[-1].state is InstallationState.FAILED
    assert events[-1].error_code is InstallationErrorCode.RECOVERY_REQUIRED
    assert not events[-1].retryable
    assert not events[-1].cancellation_safe
    _assert_released(manager)


@pytest.mark.parametrize("profile", list(RuntimeProfile))
def test_broken_rollback_event_sink_does_not_abort_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
) -> None:
    manager, _index, _artifact = _case(tmp_path, monkeypatch, profile)
    _prepare_rollback(manager, profile)

    def broken_pipe(_event: InstallationEvent) -> None:
        raise BrokenPipeError("synthetic closed wrapper")

    lifecycle = InstallationLifecycle("rollback", profile, event_sink=broken_pipe)
    result = manager.rollback(lifecycle=lifecycle)

    assert result.active_version == "1.0.0"
    assert lifecycle.terminal
    assert manager.layout.current_version() == "1.0.0"


@pytest.mark.parametrize("profile", list(RuntimeProfile))
@pytest.mark.parametrize("coverage", ["full", "database_only"])
@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("reactivation_fails", [False, True])
def test_recovery_observer_interrupt_finishes_reactivation_or_keeps_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
    coverage: str,
    interrupt: type[BaseException],
    reactivation_fails: bool,
) -> None:
    manager, _index, _artifact = _case(tmp_path, monkeypatch, profile)
    keys = InstallationKeyStore(manager.paths.master_keys_file)
    if coverage == "full":
        backup = manager._create_managed_backup(None, key_store=keys)
        _prepare_rollback(
            manager,
            profile,
            rollback_mode="restore_required",
            backup={
                "path": str(backup.path),
                "sha256": backup.sha256,
                "backup_id": backup.backup_id,
            },
        )
    else:
        checkpoint = manager._create_database_checkpoint(
            transaction_id="e" * 32,
            previous_version="1.0.0",
            target_version="1.1.0",
            key_store=keys,
        )
        _prepare_rollback(
            manager,
            profile,
            rollback_mode="restore_required",
            database_checkpoint=checkpoint.to_dict(),
        )
    _marker(manager.paths, "current-data")
    verification_failed = threading.Event()
    events: list[InstallationEvent] = []
    lock_witnesses: list[bool] = []

    def verify(
        _layout: ManagedInstallationLayout,
        paths: AppPaths,
        _version: str,
        *,
        initialize_data: bool,
        **_options: object,
    ) -> None:
        if initialize_data:
            assert _marker(paths) == "before"
            verification_failed.set()
            raise ManagedInstallError("synthetic post-restore failure")

    def observe(event: InstallationEvent) -> None:
        events.append(event)
        if (
            verification_failed.is_set()
            and event.phase is InstallationPhase.RECOVERY
            and event.state is InstallationState.RUNNING
        ):
            raise interrupt
        if event.state is InstallationState.FAILED:
            _record_released_lock(manager, lock_witnesses)

    def fail_reactivation(*_args: object, **_options: object) -> None:
        raise OSError("synthetic unavailable safety root")

    monkeypatch.setattr(managed_install, "_verify_target_runtime", verify)
    lifecycle = InstallationLifecycle("rollback", profile, event_sink=observe)
    expected = ManagedInstallError if reactivation_fails else interrupt
    with monkeypatch.context() as recovery_patch:
        if reactivation_fails:
            if coverage == "full":
                recovery_patch.setattr(
                    manager, "_reactivate_full_restore", fail_reactivation
                )
            else:
                recovery_patch.setattr(
                    managed_install, "reactivate_checkpoint_safety", fail_reactivation
                )
        with pytest.raises(expected):
            manager.rollback(restore_data=True, lifecycle=lifecycle)
    assert verification_failed.is_set()
    assert lock_witnesses == [True]
    assert manager.layout.current_version() == "1.1.0"
    assert (
        len([event for event in events if event.state is not InstallationState.RUNNING])
        == 1
    )
    if reactivation_fails:
        assert events[-1].error_code is InstallationErrorCode.RECOVERY_REQUIRED
        assert not events[-1].retryable
        assert not events[-1].cancellation_safe
        assert manager.layout.read_pending_rollback() is not None
        assert _marker(manager.paths) == "before"
        manager._recover_interrupted_rollback()
    else:
        assert events[-1].error_code is InstallationErrorCode.OPERATION_FAILED
        assert events[-1].cancellation_safe
    assert manager.layout.read_pending_rollback() is None
    assert _marker(manager.paths) == "current-data"


@dataclass(frozen=True)
class _PublisherUpdateCase:
    manager: ManagedReleaseManager
    index: Path
    plan: PublisherBootstrapPlan
    archive: bytes
    calls: Path
    transfer_started: Path
    transfer_release: Path
    archive_capture: Path


def _publisher_update_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> _PublisherUpdateCase:
    real_unpack = managed_install._unpack_runtime
    manager, index, _ = _case(tmp_path, monkeypatch, RuntimeProfile.DOCUMENTS)
    monkeypatch.setattr(managed_install, "_unpack_runtime", real_unpack)
    manager.platform_tag = "linux-64"
    capture = tmp_path / "reconstructed-archive"
    descriptor = {
        "format": "carlina.application-distribution",
        "schema_version": 1,
        "application_profile": "documents",
        "source_revision": "a" * 40,
        "source_date_epoch": 1_791_050_400,
        "addon_ids": ["doc_processor", "sentence_segmenter", "translator"],
    }
    unpacker = (
        "#!" + sys.executable + "\n"
        "from pathlib import Path\nimport json, os, sys\n"
        "archive = Path(sys.argv[1]).read_bytes()\n"
        "Path(os.environ['CARLINA_TEST_PUBLISHER_CAPTURE']).write_bytes(archive)\n"
        "root = Path.cwd()\n"
        "python = root / 'env/bin/python'\npython.parent.mkdir(parents=True)\n"
        "python.write_bytes(b'synthetic interpreter, never executed')\n"
        "pixi = root / 'env/bin/pixi'\npixi.write_bytes(b'synthetic Pixi')\npixi.chmod(0o700)\n"
        "descriptor = root / 'env/lib/python3.14/site-packages/carlina/distribution.json'\n"
        "descriptor.parent.mkdir(parents=True)\n"
        f"descriptor.write_text({json.dumps(descriptor)!r})\n"
    ).encode()
    plan, skeleton, archive, downloads, _, _ = publisher_bootstrap_fixture(
        monkeypatch, unpacker_bytes=unpacker
    )
    plan = replace(plan, version="1.1.0")
    if outcome == "hash_failure":
        downloads[plan.downloads[0].url] = b"x" * plan.downloads[0].size
    elif outcome == "download_failure":
        del downloads[plan.downloads[0].url]
    artifact = tmp_path / "publisher-1.1.0.sh"
    artifact.write_bytes(
        b"#!/bin/sh\nprintf 'unknown private record\\n'\nexit 0\n"
        if outcome == "malformed"
        else render_publisher_bootstrap(plan, skeleton)
    )
    payload = json.loads(index.read_text())
    payload["schema_version"] = 3
    release = payload["releases"][0]
    release["execution"] = {
        "data_impact": "opaque",
        "migration": {"mode": "python_alembic"},
        "bootstrap": {"mode": "repair_and_reconciliation"},
        "rollback": {"mode": "restore_required"},
        "recovery": {"automatic_coverage": "full"},
        "probe": {
            "protocol": "carlina.managed-release-probe",
            "schema_version": 1,
            "isolation": "live_data_root",
        },
    }
    release["artifacts"] = [
        {
            "platform": "linux-64",
            "kind": "publisher-bootstrap-executable",
            "filename": artifact.name,
            "url": artifact.name,
            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            "size": artifact.stat().st_size,
        }
    ]
    index.write_text(json.dumps(payload))
    sources: dict[str, str] = {}
    for number, (url, data) in enumerate(downloads.items()):
        path = tmp_path / f"dependency-{number}"
        path.write_bytes(data)
        sources[url] = str(path)
    mapping = tmp_path / "publisher-downloads.json"
    mapping.write_text(json.dumps(sources))
    calls = tmp_path / "publisher-download-calls.jsonl"
    started, released = tmp_path / "transfer-started", tmp_path / "transfer-release"
    shim = tmp_path / "publisher-shim"
    shim.mkdir()
    curl = shim / "curl"
    curl.write_text(
        "#!" + sys.executable + "\n"
        "import json, os, signal, sys, time\nfrom pathlib import Path\n"
        "url = next(arg for arg in sys.argv[1:] if arg.startswith('https://'))\n"
        "with Path(os.environ['CARLINA_TEST_PUBLISHER_CALLS']).open('a') as log:\n"
        "    log.write(json.dumps(url) + '\\n')\n"
        "sources = json.loads(Path(os.environ['CARLINA_TEST_PUBLISHER_SOURCES']).read_text())\n"
        "if url not in sources: raise SystemExit(22)\n"
        "payload = Path(sources[url]).read_bytes()\n"
        "target = Path(sys.argv[sys.argv.index('-o') + 1]).open('wb') if '-o' in sys.argv else sys.stdout.buffer\n"
        "if os.environ['CARLINA_TEST_PUBLISHER_BLOCK'] == '1' and url == os.environ['CARLINA_TEST_PUBLISHER_FIRST']:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    boundary = len(payload) // 2\n"
        "    target.write(payload[:boundary]); target.flush()\n"
        "    Path(os.environ['CARLINA_TEST_PUBLISHER_STARTED']).touch()\n"
        "    while not Path(os.environ['CARLINA_TEST_PUBLISHER_RELEASE']).exists(): time.sleep(0.01)\n"
        "    target.write(payload[boundary:])\n"
        "else:\n"
        "    target.write(payload)\n"
        "target.flush()\n",
        encoding="utf-8",
    )
    curl.chmod(0o700)
    # Preserve fixture logging for tiny inputs. Actual prlimit/curl bounds are
    # covered independently by the real TLS transport cases.
    prlimit = shim / "prlimit"
    prlimit.write_text(
        '#!/bin/sh\nwhile [ "$1" != -- ]; do shift; done\nshift\nexec "$@"\n'
    )
    prlimit.chmod(0o700)
    values = {
        "PATH": str(shim) + os.pathsep + os.environ.get("PATH", ""),
        "CARLINA_TEST_PUBLISHER_SOURCES": str(mapping),
        "CARLINA_TEST_PUBLISHER_CALLS": str(calls),
        "CARLINA_TEST_PUBLISHER_BLOCK": "1"
        if outcome in {"cancel", "interrupt"}
        else "0",
        "CARLINA_TEST_PUBLISHER_FIRST": plan.downloads[0].url,
        "CARLINA_TEST_PUBLISHER_STARTED": str(started),
        "CARLINA_TEST_PUBLISHER_RELEASE": str(released),
        "CARLINA_TEST_PUBLISHER_CAPTURE": str(capture),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return _PublisherUpdateCase(
        manager, index, plan, archive, calls, started, released, capture
    )


def _assert_publisher_update_finalized(
    case: _PublisherUpdateCase, events: list[InstallationEvent], *, succeeded: bool
) -> None:
    manager = case.manager
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert len({event.operation_id for event in events}) == 1
    assert {event.application_profile for event in events} == {RuntimeProfile.DOCUMENTS}
    assert (
        len([event for event in events if event.state is not InstallationState.RUNNING])
        == 1
    )
    assert not list(manager.layout.staging_dir.glob("unpack-*"))
    assert manager.layout.current_version() == ("1.1.0" if succeeded else "1.0.0")
    assert _marker(manager.paths) == ("after" if succeeded else "before")
    journal = json.loads(manager.layout.journal_file.read_text())
    assert Path(journal["backup"]["path"]).is_file()
    assert manager.layout.read_pending_update() is None
    if not succeeded:
        assert not manager.layout.version_dir("1.1.0").exists()
        quarantine = Path(journal["failed_release_quarantine"])
        assert quarantine.is_dir()
        assert not list(quarantine.glob(".carlina-publisher.*"))
        assert "automatic_data_restore" not in journal
    _assert_released(manager)


@pytest.mark.skipif(
    os.name == "nt", reason="Real Linux publisher in a temporary managed root"
)
@pytest.mark.parametrize(
    "outcome", ["success", "download_failure", "hash_failure", "malformed"]
)
def test_applying_update_consumes_real_publisher_substeps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    case = _publisher_update_case(tmp_path, monkeypatch, outcome)
    events: list[InstallationEvent] = []
    terminal_witnesses: list[bool] = []
    key_digest = hashlib.sha256(
        case.manager.paths.master_keys_file.read_bytes()
    ).hexdigest()

    def observe(event: InstallationEvent) -> None:
        events.append(event)
        if event.state is not InstallationState.RUNNING:
            _assert_released(case.manager)
            assert not list(case.manager.layout.staging_dir.glob("unpack-*"))
            terminal_witnesses.append(True)

    context = InstallationLifecycle("update", "documents", event_sink=observe)
    if outcome == "success":
        case.manager.update(
            case.index,
            requested_version="1.1.0",
            allow_unsigned_local=True,
            lifecycle=context,
        )
        assert case.archive_capture.read_bytes() == case.archive
        assert events[-1].state is InstallationState.SUCCEEDED
    else:
        with pytest.raises(PublisherBootstrapFailure):
            case.manager.update(
                case.index,
                requested_version="1.1.0",
                allow_unsigned_local=True,
                lifecycle=context,
            )
        expected = {
            "download_failure": InstallationErrorCode.DOWNLOAD_FAILED,
            "hash_failure": InstallationErrorCode.VERIFICATION_FAILED,
            "malformed": InstallationErrorCode.INCOMPATIBLE_RELEASE,
        }[outcome]
        assert events[-1].error_code is expected
        assert events[-1].retryable is (
            expected is InstallationErrorCode.DOWNLOAD_FAILED
        )
    assert (
        key_digest
        == hashlib.sha256(case.manager.paths.master_keys_file.read_bytes()).hexdigest()
    )
    assert terminal_witnesses == [True]
    _assert_publisher_update_finalized(case, events, succeeded=outcome == "success")
    if outcome != "malformed":
        assert any(event.bytes_total == case.plan.downloads[0].size for event in events)


@pytest.mark.skipif(
    os.name == "nt", reason="Independent Linux publisher transfer barrier"
)
@pytest.mark.parametrize("interruption", ["cancel", "keyboard", "exit"])
def test_publisher_update_keeps_ownership_until_child_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    case = _publisher_update_case(
        tmp_path, monkeypatch, "cancel" if interruption == "cancel" else "interrupt"
    )
    manager = case.manager
    request = threading.Event()
    injected = False
    expected_interrupt = (
        KeyboardInterrupt("publisher observer interrupted")
        if interruption == "keyboard"
        else SystemExit(17)
    )
    events: list[InstallationEvent] = []
    terminal_witnesses: list[bool] = []
    controller_errors: list[BaseException] = []
    ownership_witnesses: list[bool] = []

    def observe(event: InstallationEvent) -> None:
        nonlocal injected
        events.append(event)
        if (
            interruption != "cancel"
            and not injected
            and event.phase is InstallationPhase.DOWNLOAD
            and event.cancellation_safe
            and event.bytes_completed is not None
            and event.bytes_completed > 0
            and event.bytes_completed < case.plan.downloads[0].size
            and event.bytes_total == case.plan.downloads[0].size
        ):
            injected = True
            raise expected_interrupt
        if event.state is not InstallationState.RUNNING:
            _assert_released(manager)
            assert not list(manager.layout.staging_dir.glob("unpack-*"))
            terminal_witnesses.append(True)

    context = InstallationLifecycle(
        "update", "documents", event_sink=observe, cancel_requested=request.is_set
    )

    def control_transfer() -> None:
        try:
            deadline = time.monotonic() + 15
            while not case.transfer_started.exists():
                assert time.monotonic() < deadline, (
                    "The publisher transfer did not start."
                )
                time.sleep(0.01)
            if interruption == "cancel":
                request.set()
            while not list(
                manager.layout.staging_dir.glob("unpack-*/publisher.cancel")
            ):
                assert time.monotonic() < deadline, (
                    "No cooperative stop reached the blocked child."
                )
                time.sleep(0.01)
            controls = list(
                manager.layout.staging_dir.glob("unpack-*/publisher.cancel")
            )
            assert len(controls) == 1
            assert controls[0].stat().st_size == 0
            assert controls[0].stat().st_mode & 0o777 == 0o600
            assert manager.layout.version_dir("1.1.0").is_dir()
            assert any(
                event.phase is InstallationPhase.DOWNLOAD and event.cancellation_safe
                for event in events
            )
            assert all(event.state is InstallationState.RUNNING for event in events)
            assert _marker(manager.paths) == "before"
            competing = FileLock(str(manager.layout.lock_file), timeout=0)
            try:
                competing.acquire(timeout=0)
            except FileLockTimeout:
                pass
            else:
                competing.release()
                raise AssertionError("The publisher lost its program-root lease.")
            data_lease = runtime_leader_lock(manager.paths)
            try:
                data_lease.acquire(timeout=0)
            except FileLockTimeout:
                pass
            else:
                data_lease.release()
                raise AssertionError("The publisher lost its data-root lease.")
            ownership_witnesses.append(True)
        except BaseException as error:
            controller_errors.append(error)
        finally:
            case.transfer_release.touch()

    controller = threading.Thread(target=control_transfer)
    controller.start()
    expected_type = (
        InstallationCancelled if interruption == "cancel" else type(expected_interrupt)
    )
    try:
        with pytest.raises(expected_type) as raised:
            manager.update(
                case.index,
                requested_version="1.1.0",
                allow_unsigned_local=True,
                lifecycle=context,
            )
        if interruption != "cancel":
            assert raised.value is expected_interrupt
    finally:
        case.transfer_release.touch()
        controller.join(timeout=20)
    assert not controller.is_alive()
    assert controller_errors == []
    assert ownership_witnesses == [True]
    assert terminal_witnesses == [True]
    _assert_publisher_update_finalized(case, events, succeeded=False)
    assert events[-1].state is (
        InstallationState.CANCELLED
        if interruption == "cancel"
        else InstallationState.FAILED
    )
    assert events[-1].error_code is (
        InstallationErrorCode.CANCELLED
        if interruption == "cancel"
        else InstallationErrorCode.OPERATION_FAILED
    )
    assert not case.archive_capture.exists()
    assert [json.loads(line) for line in case.calls.read_text().splitlines()] == [
        case.plan.downloads[0].url
    ]


@pytest.mark.parametrize(
    "profile", [RuntimeProfile.DOCUMENTS, RuntimeProfile.WORKBENCH]
)
def test_publisher_failure_cannot_override_an_unresolved_recovery_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: RuntimeProfile
) -> None:
    manager, _, _ = _case(tmp_path, monkeypatch, profile)
    fence = manager.layout.recovery_required_file
    fence.write_bytes(b"{")
    events: list[InstallationEvent] = []
    context = InstallationLifecycle("update", profile, event_sink=events.append)

    def fail() -> None:
        raise PublisherBootstrapFailure(InstallationErrorCode.DOWNLOAD_FAILED)

    with pytest.raises(PublisherBootstrapFailure):
        manager._run_installation_operation(InstallationOperation.UPDATE, context, fail)

    assert events[-1].error_code is InstallationErrorCode.RECOVERY_REQUIRED
    assert events[-1].retryable is False
    assert events[-1].cancellation_safe is False
    assert fence.read_bytes() == b"{"


def _claim_native_generation(
    manager: ManagedReleaseManager,
    index: Path,
    *,
    ready: bool = True,
    data_root: Path | None = None,
) -> tuple[str, str]:
    parsed = parse_release_index(index.read_bytes())
    release = parsed.releases[0]
    artifact = release.artifact_for(manager.platform_tag)
    index_sha = hashlib.sha256(index.read_bytes()).hexdigest()
    identity = BootstrapIdentity(
        manager.application_profile.value,
        release.version,
        parsed.channel,
        index_sha,
        artifact.platform,
        artifact.kind,
        artifact.filename,
        artifact.sha256,
        artifact.size,
    )
    target = manager.layout.version_dir(release.version)
    _seed_target(manager.layout, release.version, manager.application_profile)
    claim_id = "c" * 32
    (target / BOOTSTRAP_CLAIM_NAME).write_bytes(
        bootstrap_claim_bytes(identity, claim_id)
    )
    if ready:
        lease = manager._acquire_lock()
        try:
            bind_bootstrap_generation(
                target,
                identity,
                claim_id=claim_id,
                program_root=manager.layout.root,
                data_root=data_root or manager.paths.data_root,
            )
        finally:
            lease.release()
    return claim_id, index_sha


def _native_resume_path_state(path: Path) -> tuple[object, ...] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        return ("symlink", os.readlink(path))
    if stat.S_ISREG(info.st_mode):
        return ("file", path.read_bytes())
    return ("other", info.st_mode, info.st_size)


def _native_resume_snapshot(manager: ManagedReleaseManager) -> dict[str, object]:
    target = manager.layout.version_dir("1.1.0")
    return {
        "journal": _native_resume_path_state(manager.layout.journal_file),
        "history": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in manager.layout.history_dir.iterdir()
            if path.is_file()
        },
        "current": _native_resume_path_state(manager.layout.current_file),
        "marker": _marker(manager.paths),
        "keys": hashlib.sha256(manager.paths.master_keys_file.read_bytes()).hexdigest(),
        "target_files": {
            str(path.relative_to(target)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in target.rglob("*")
            if path.is_file() and not path.is_symlink()
        },
        "claim": _native_resume_path_state(target / BOOTSTRAP_CLAIM_NAME),
        "binding": _native_resume_path_state(target / BOOTSTRAP_BINDING_NAME),
        "receipt": _native_resume_path_state(target / "release-receipt.json"),
        "backups": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in manager.paths.backups_dir.iterdir()
            if path.is_file()
        },
        "recovery_fence": _native_resume_path_state(
            manager.layout.recovery_required_file
        ),
        "rollback_fence": _native_resume_path_state(
            manager.layout.rollback_transaction_file
        ),
    }


def _forbid_resume_recovery_and_archive(
    manager: ManagedReleaseManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> object:
        pytest.fail("A rejected bootstrap resume reached recovery or archival.")

    monkeypatch.setattr(manager, "_recover_interrupted_database_switches", forbidden)
    monkeypatch.setattr(manager, "_recover_interrupted_rollback", forbidden)
    monkeypatch.setattr(manager, "_recover_interrupted_update", forbidden)
    monkeypatch.setattr(manager, "_archive_existing_journal", forbidden)


@pytest.mark.parametrize("profile", list(RuntimeProfile))
def test_native_owned_adoption_records_and_reconciles_immutable_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, profile)
    claim_id, index_sha = _claim_native_generation(manager, index)
    binding = manager.layout.version_dir("1.1.0") / BOOTSTRAP_BINDING_NAME
    before = binding.read_bytes()
    original = _adopt(manager, index, artifact, expected_index_sha256=index_sha)
    receipt = manager.layout.read_receipt("1.1.0")
    journal = json.loads(manager.layout.journal_file.read_bytes())
    for record in (receipt, journal):
        assert record["bootstrap_claim_id"] == claim_id
        assert record["bootstrap_index_sha256"] == index_sha
    monkeypatch.setattr(
        managed_install,
        "_verify_target_runtime",
        lambda *_args, **_kwargs: pytest.fail(
            "A committed native adoption was executed again."
        ),
    )
    result = _adopt(
        manager,
        index,
        artifact,
        expected_index_sha256=index_sha,
        retry_transaction_id=original.transaction_id,
    )
    assert result == original
    assert binding.read_bytes() == before
    _assert_released(manager)


@pytest.mark.parametrize("profile", list(RuntimeProfile))
@pytest.mark.parametrize(
    "state", ["preparing", "other_data", "missing_hash", "artifact_drift"]
)
def test_native_claim_refuses_unsafe_adoption_before_journal_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
    state: str,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, profile)
    _claim_id, index_sha = _claim_native_generation(
        manager,
        index,
        ready=state != "preparing",
        data_root=tmp_path / "foreign data" if state == "other_data" else None,
    )
    prior = b"prior journal retained without replacement"
    manager.layout.journal_file.write_bytes(prior)
    target = manager.layout.version_dir("1.1.0")
    frame = (target / BOOTSTRAP_CLAIM_NAME).read_bytes()
    key_hash = hashlib.sha256(manager.paths.master_keys_file.read_bytes()).hexdigest()
    if state == "artifact_drift":
        artifact.write_bytes(b"changed artifact")

    def unexpected_recovery(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Invalid native ownership must fail before recovery.")

    monkeypatch.setattr(
        manager, "_recover_interrupted_database_switches", unexpected_recovery
    )
    with pytest.raises(ManagedInstallError):
        _adopt(
            manager,
            index,
            artifact,
            expected_index_sha256=None if state == "missing_hash" else index_sha,
        )
    assert manager.layout.journal_file.read_bytes() == prior
    assert (target / BOOTSTRAP_CLAIM_NAME).read_bytes() == frame
    assert manager.layout.current_version() == "1.0.0"
    assert _marker(manager.paths) == "before"
    assert not list(manager.layout.history_dir.iterdir())
    assert not (tmp_path / "foreign data").exists()
    assert (
        hashlib.sha256(manager.paths.master_keys_file.read_bytes()).hexdigest()
        == key_hash
    )
    _assert_released(manager)


@pytest.mark.parametrize("profile", list(RuntimeProfile))
def test_native_owned_failed_adoption_retries_without_rebinding_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, profile)
    claim_id, index_sha = _claim_native_generation(manager, index)
    binding = manager.layout.version_dir("1.1.0") / BOOTSTRAP_BINDING_NAME
    before = binding.read_bytes()
    original_verify = managed_install._verify_unpacked_application_profile
    with monkeypatch.context() as patch:

        def fail(*_args: object) -> None:
            raise OSError("synthetic before target initialization")

        patch.setattr(managed_install, "_verify_unpacked_application_profile", fail)
        with pytest.raises(OSError):
            _adopt(manager, index, artifact, expected_index_sha256=index_sha)
    journal = json.loads(manager.layout.journal_file.read_bytes())
    assert managed_install._verify_unpacked_application_profile is original_verify
    result = _adopt(
        manager,
        index,
        artifact,
        expected_index_sha256=index_sha,
        retry_transaction_id=journal["transaction_id"],
    )
    receipt = manager.layout.read_receipt("1.1.0")
    assert result.transaction_id != journal["transaction_id"]
    assert receipt["bootstrap_claim_id"] == claim_id
    assert receipt["bootstrap_index_sha256"] == index_sha
    assert binding.read_bytes() == before


@pytest.mark.parametrize("profile", list(RuntimeProfile))
def test_native_resume_without_a_journal_adopts_ready_generation_as_fresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, profile)
    claim_id, index_sha = _claim_native_generation(manager, index)
    binding = manager.layout.version_dir("1.1.0") / BOOTSTRAP_BINDING_NAME
    binding_bytes = binding.read_bytes()
    assert not manager.layout.journal_file.exists()
    assert not manager.layout.receipt_file("1.1.0").exists()

    result = _adopt(
        manager,
        index,
        artifact,
        expected_index_sha256=index_sha,
        resume_bootstrap_claim_id=claim_id,
    )

    journal = json.loads(manager.layout.journal_file.read_bytes())
    receipt = manager.layout.read_receipt("1.1.0")
    assert result.transaction_id == journal["transaction_id"]
    assert manager.layout.current_version() == "1.1.0"
    assert journal["bootstrap_claim_id"] == claim_id
    assert journal["bootstrap_index_sha256"] == index_sha
    assert receipt["bootstrap_claim_id"] == claim_id
    assert receipt["bootstrap_index_sha256"] == index_sha
    assert "retried_adoption_transaction_id" not in journal
    assert "retried_adoption_transaction_id" not in receipt
    assert binding.read_bytes() == binding_bytes
    assert _marker(manager.paths) == "after"
    _assert_released(manager)


@pytest.mark.parametrize("profile", list(RuntimeProfile))
def test_native_resume_of_committed_adoption_is_idempotent_and_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, profile)
    claim_id, index_sha = _claim_native_generation(manager, index)
    initial_lifecycle = InstallationLifecycle("install", profile)
    original = _adopt(
        manager,
        index,
        artifact,
        lifecycle=initial_lifecycle,
        expected_index_sha256=index_sha,
    )
    initial_journal = json.loads(manager.layout.journal_file.read_bytes())
    initial_receipt = manager.layout.receipt_file("1.1.0").read_bytes()
    binding = manager.layout.version_dir("1.1.0") / BOOTSTRAP_BINDING_NAME
    binding_bytes = binding.read_bytes()
    backup = Path(initial_journal["backup"]["path"])
    backup_hash = hashlib.sha256(backup.read_bytes()).hexdigest()
    terminal_witnesses: list[bool] = []
    operation_ids: list[str] = []

    def forbidden(*_args: object, **_kwargs: object) -> object:
        pytest.fail("Committed bootstrap resume entered a new adoption or recovery.")

    monkeypatch.setattr(manager, "_recover_interrupted_database_switches", forbidden)
    monkeypatch.setattr(manager, "_recover_interrupted_rollback", forbidden)
    monkeypatch.setattr(manager, "_recover_interrupted_update", forbidden)
    monkeypatch.setattr(manager, "_archive_existing_journal", forbidden)
    monkeypatch.setattr(manager, "_create_managed_backup", forbidden)
    monkeypatch.setattr(manager, "_restore_failed_update_data", forbidden)
    monkeypatch.setattr(managed_install, "_verify_target_runtime", forbidden)
    monkeypatch.setattr(
        managed_install, "_verify_unpacked_application_profile", forbidden
    )
    monkeypatch.setattr(ManagedInstallationLayout, "write_launchers", forbidden)

    for _ in range(2):
        lifecycle = InstallationLifecycle(
            "install",
            profile,
            event_sink=lambda event: (
                _record_released_lock(manager, terminal_witnesses)
                if event.state is not InstallationState.RUNNING
                else None
            ),
        )
        operation_ids.append(lifecycle.operation_id)
        result = _adopt(
            manager,
            index,
            artifact,
            lifecycle=lifecycle,
            expected_index_sha256=index_sha,
            resume_bootstrap_claim_id=claim_id,
        )
        assert result.transaction_id == original.transaction_id
        assert result.active_version == "1.1.0"
        assert lifecycle.terminal

    journal = json.loads(manager.layout.journal_file.read_bytes())
    assert journal["transaction_id"] == original.transaction_id
    assert journal["operation_id"] == initial_lifecycle.operation_id
    assert journal["last_reconciled_operation_id"] == operation_ids[-1]
    assert operation_ids[0] != operation_ids[1]
    assert journal["phase"] == "completed"
    assert manager.layout.receipt_file("1.1.0").read_bytes() == initial_receipt
    assert binding.read_bytes() == binding_bytes
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == backup_hash
    assert _marker(manager.paths) == "after"
    assert terminal_witnesses == [True, True]


@pytest.mark.parametrize("profile", list(RuntimeProfile))
def test_native_resume_retries_recognized_stopped_failure_with_new_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, profile)
    claim_id, index_sha = _claim_native_generation(manager, index)
    binding = manager.layout.version_dir("1.1.0") / BOOTSTRAP_BINDING_NAME
    binding_bytes = binding.read_bytes()
    original_verify = managed_install._verify_unpacked_application_profile
    verify_calls = 0

    def fail_before_target_once(
        layout: ManagedInstallationLayout,
        version: str,
        expected_profile: RuntimeProfile,
    ) -> None:
        nonlocal verify_calls
        verify_calls += 1
        if verify_calls == 1:
            raise OSError("synthetic native handoff failure before target execution")
        original_verify(layout, version, expected_profile)

    monkeypatch.setattr(
        managed_install,
        "_verify_unpacked_application_profile",
        fail_before_target_once,
    )
    with pytest.raises(OSError, match="synthetic native handoff failure"):
        _adopt(
            manager,
            index,
            artifact,
            expected_index_sha256=index_sha,
            resume_bootstrap_claim_id=claim_id,
        )
    original_journal = json.loads(manager.layout.journal_file.read_bytes())
    assert original_journal["phase"] == "failed"
    original_transaction_id = original_journal["transaction_id"]
    assert manager.layout.current_version() == "1.0.0"
    assert _marker(manager.paths) == "before"

    lifecycle = InstallationLifecycle("install", profile)
    result = _adopt(
        manager,
        index,
        artifact,
        lifecycle=lifecycle,
        expected_index_sha256=index_sha,
        resume_bootstrap_claim_id=claim_id,
    )

    journal = json.loads(manager.layout.journal_file.read_bytes())
    receipt = manager.layout.read_receipt("1.1.0")
    assert result.transaction_id != original_transaction_id
    assert journal["retried_adoption_transaction_id"] == original_transaction_id
    assert receipt["retried_adoption_transaction_id"] == original_transaction_id
    assert journal["transaction_id"] == result.transaction_id
    assert journal["operation_id"] == lifecycle.operation_id
    assert journal["bootstrap_claim_id"] == claim_id
    assert receipt["bootstrap_claim_id"] == claim_id
    assert binding.read_bytes() == binding_bytes
    archived = manager.layout.history_dir / f"update-{original_transaction_id}.json"
    assert (
        json.loads(archived.read_bytes())["transaction_id"] == original_transaction_id
    )
    assert manager.layout.current_version() == "1.1.0"
    assert _marker(manager.paths) == "after"
    _assert_released(manager)


@pytest.mark.parametrize(
    ("controls", "expected_error"),
    [
        (
            {"resume_bootstrap_claim_id": "not-a-claim"},
            "Native bootstrap resume claim must be 32 lowercase hexadecimal characters.",
        ),
        (
            {
                "resume_bootstrap_claim_id": "c" * 32,
                "expected_index_sha256": "a" * 64,
            },
            "Native bootstrap resume requires an explicit version and index hash.",
        ),
        (
            {
                "resume_bootstrap_claim_id": "c" * 32,
                "requested_version": "1.1.0",
            },
            "Native bootstrap resume requires an explicit version and index hash.",
        ),
        (
            {
                "resume_bootstrap_claim_id": "c" * 32,
                "retry_transaction_id": "d" * 32,
                "requested_version": "1.1.0",
                "expected_index_sha256": "a" * 64,
            },
            "Native bootstrap resume and explicit adoption retry cannot be combined.",
        ),
    ],
)
def test_native_resume_controls_are_rejected_before_index_or_locks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    controls: dict[str, str],
    expected_error: str,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, RuntimeProfile.WORKBENCH)

    def unexpected(*_args: object, **_kwargs: object) -> object:
        pytest.fail("Invalid bootstrap resume controls reached index or lock access.")

    monkeypatch.setattr(manager, "_verified_index", unexpected)
    monkeypatch.setattr(manager, "_acquire_lock", unexpected)
    with pytest.raises(ManagedInstallError, match=f"^{expected_error}$"):
        manager.adopt_installed_release(
            index,
            artifact,
            manager.layout.version_dir("1.1.0"),
            allow_unsigned_local=True,
            **controls,
        )
    assert not manager.layout.journal_file.exists()
    assert not tuple(manager.layout.history_dir.iterdir())
    _assert_released(manager)


@pytest.mark.parametrize(
    "state",
    [
        "unmarked",
        "wrong_nonce",
        "preparing",
        "wrong_binding_root",
        "wrong_journal_root",
        "unsafe_phase",
        "recovery_fence",
        "rollback_fence",
        "malformed_journal",
        "symlink_journal",
    ],
)
def test_native_resume_rejects_unready_or_unresolvable_state_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    profile = RuntimeProfile.DOCUMENTS
    manager, index, artifact = _case(tmp_path, monkeypatch, profile)
    resume_claim_id = "c" * 32
    index_sha = hashlib.sha256(index.read_bytes()).hexdigest()
    if state == "unmarked":
        _seed_target(manager.layout, "1.1.0", profile)
    elif state == "preparing":
        resume_claim_id, index_sha = _claim_native_generation(
            manager, index, ready=False
        )
    elif state == "wrong_binding_root":
        resume_claim_id, index_sha = _claim_native_generation(
            manager, index, data_root=tmp_path / "foreign data"
        )
    else:
        resume_claim_id, index_sha = _claim_native_generation(manager, index)

    if state in {"wrong_journal_root", "unsafe_phase"}:
        _adopt(manager, index, artifact, expected_index_sha256=index_sha)
        journal = json.loads(manager.layout.journal_file.read_bytes())
        if state == "wrong_journal_root":
            journal["data_root"] = str(tmp_path / "foreign data")
        else:
            manager.layout.activate("1.0.0")
            journal.pop("automatic_data_restore", None)
            journal["phase"] = "runtime_probe_pending"
        manager.layout.journal_file.write_text(json.dumps(journal), encoding="utf-8")
    elif state == "recovery_fence":
        manager.layout.recovery_required_file.write_text(
            json.dumps(
                {
                    "format": managed_install.RECOVERY_REQUIRED_FORMAT,
                    "schema_version": managed_install.RECOVERY_REQUIRED_VERSION,
                    "status": "required",
                    "transaction_id": "d" * 32,
                    "failed_target_version": "1.2.0",
                }
            ),
            encoding="utf-8",
        )
    elif state == "rollback_fence":
        manager.layout.rollback_transaction_file.write_text(
            json.dumps(
                {
                    "format": managed_install.ROLLBACK_TRANSACTION_FORMAT,
                    "schema_version": managed_install.ROLLBACK_TRANSACTION_VERSION,
                    "phase": "prepared",
                    "transaction_id": "d" * 32,
                    "coverage": "full",
                    "from_version": "1.1.0",
                    "to_version": "1.0.0",
                }
            ),
            encoding="utf-8",
        )
    elif state == "malformed_journal":
        manager.layout.journal_file.write_bytes(b"{")
    elif state == "symlink_journal":
        other_journal = tmp_path / "other-journal.json"
        other_journal.write_text("{}", encoding="utf-8")
        manager.layout.journal_file.symlink_to(other_journal)

    if state == "wrong_nonce":
        resume_claim_id = "e" * 32
    snapshot = _native_resume_snapshot(manager)
    _forbid_resume_recovery_and_archive(manager, monkeypatch)
    with pytest.raises(ManagedInstallError) as raised:
        _adopt(
            manager,
            index,
            artifact,
            expected_index_sha256=index_sha,
            resume_bootstrap_claim_id=resume_claim_id,
        )
    if state in {"unmarked", "wrong_nonce", "preparing", "wrong_binding_root"}:
        assert str(raised.value) == (
            "Native bootstrap resume does not match the ready generation."
        )
    assert _native_resume_snapshot(manager) == snapshot
    _assert_released(manager)


@pytest.mark.parametrize("profile", list(RuntimeProfile))
@pytest.mark.parametrize(
    "candidate_receipt", ["absent", "malformed", "foreign", "symlink"]
)
def test_native_resume_archives_only_a_proven_completed_unrelated_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
    candidate_receipt: str,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, profile)
    claim_id, index_sha = _claim_native_generation(manager, index)
    old_transaction_id = "d" * 32
    old_receipt = {
        "format": managed_install.RELEASE_RECEIPT_FORMAT,
        "schema_version": managed_install.RELEASE_RECEIPT_VERSION,
        "version": "1.0.0",
        "transaction_id": old_transaction_id,
        "application_profile": profile.value,
    }
    manager.layout.receipt_file("1.0.0").write_text(
        json.dumps(old_receipt), encoding="utf-8"
    )
    old_journal = {
        "format": managed_install.UPDATE_JOURNAL_FORMAT,
        "schema_version": managed_install.UPDATE_JOURNAL_VERSION,
        "operation": "update",
        "transaction_id": old_transaction_id,
        "phase": "completed",
        "application_profile": profile.value,
        "program_root": str(manager.layout.root.resolve()),
        "data_root": str(manager.paths.data_root.resolve()),
        "target_version": "1.0.0",
        "previous_version": None,
    }
    manager.layout.journal_file.write_text(json.dumps(old_journal), encoding="utf-8")
    old_journal_bytes = manager.layout.journal_file.read_bytes()
    if candidate_receipt != "absent":
        candidate = manager.layout.receipt_file("1.1.0")
        if candidate_receipt == "symlink":
            foreign = tmp_path / "foreign-receipt.json"
            foreign.write_bytes(b"{}")
            candidate.symlink_to(foreign)
        elif candidate_receipt == "malformed":
            candidate.write_bytes(b"{")
        else:
            candidate.write_text(json.dumps({**old_receipt, "version": "1.1.0"}))
        before = _native_resume_snapshot(manager)
        _forbid_resume_recovery_and_archive(manager, monkeypatch)
        with pytest.raises(ManagedInstallError):
            _adopt(
                manager,
                index,
                artifact,
                expected_index_sha256=index_sha,
                resume_bootstrap_claim_id=claim_id,
            )
        assert _native_resume_snapshot(manager) == before
        _assert_released(manager)
        return

    def forbidden_restore(*_args: object, **_kwargs: object) -> object:
        pytest.fail("A committed prior journal must not trigger data restoration.")

    monkeypatch.setattr(manager, "_restore_failed_update_data", forbidden_restore)
    result = _adopt(
        manager,
        index,
        artifact,
        expected_index_sha256=index_sha,
        resume_bootstrap_claim_id=claim_id,
    )

    new_journal = json.loads(manager.layout.journal_file.read_bytes())
    receipt = manager.layout.read_receipt("1.1.0")
    archived = manager.layout.history_dir / f"update-{old_transaction_id}.json"
    assert archived.read_bytes() == old_journal_bytes
    assert new_journal["transaction_id"] == result.transaction_id
    assert "retried_adoption_transaction_id" not in new_journal
    assert "retried_adoption_transaction_id" not in receipt
    assert new_journal["program_root"] == str(manager.layout.root.resolve())
    assert new_journal["data_root"] == str(manager.paths.data_root.resolve())
    assert receipt["program_root"] == str(manager.layout.root.resolve())
    assert receipt["data_root"] == str(manager.paths.data_root.resolve())
    assert receipt["bootstrap_claim_id"] == claim_id
    assert manager.layout.current_version() == "1.1.0"
    assert _marker(manager.paths) == "after"


@pytest.mark.parametrize(
    "state", ["failed", "unbound", "activation_unknown", "candidate_target"]
)
def test_native_resume_rejects_unrelated_failed_unbound_or_ambiguous_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    profile = RuntimeProfile.WORKBENCH
    manager, index, artifact = _case(tmp_path, monkeypatch, profile)
    claim_id, index_sha = _claim_native_generation(manager, index)
    transaction_id = "d" * 32
    journal = {
        "format": managed_install.UPDATE_JOURNAL_FORMAT,
        "schema_version": managed_install.UPDATE_JOURNAL_VERSION,
        "operation": "update",
        "transaction_id": transaction_id,
        "phase": "completed",
        "application_profile": profile.value,
        "program_root": str(manager.layout.root.resolve()),
        "data_root": str(manager.paths.data_root.resolve()),
        "target_version": "1.0.0",
        "previous_version": None,
    }
    manager.layout.receipt_file("1.0.0").write_text(
        json.dumps(
            {
                "format": managed_install.RELEASE_RECEIPT_FORMAT,
                "schema_version": managed_install.RELEASE_RECEIPT_VERSION,
                "version": "1.0.0",
                "transaction_id": transaction_id,
                "application_profile": profile.value,
            }
        ),
        encoding="utf-8",
    )
    if state == "failed":
        journal["phase"] = "failed"
    elif state == "unbound":
        journal.pop("program_root")
    elif state == "activation_unknown":
        journal["activation_outcome_unknown"] = True
    else:
        journal["target_version"] = "1.1.0"
    manager.layout.journal_file.write_text(json.dumps(journal), encoding="utf-8")
    snapshot = _native_resume_snapshot(manager)
    _forbid_resume_recovery_and_archive(manager, monkeypatch)

    with pytest.raises(ManagedInstallError):
        _adopt(
            manager,
            index,
            artifact,
            expected_index_sha256=index_sha,
            resume_bootstrap_claim_id=claim_id,
        )

    assert _native_resume_snapshot(manager) == snapshot
    _assert_released(manager)


@pytest.mark.parametrize("state", ["pointer", "receipt", "receipt_symlink"])
@pytest.mark.parametrize("profile", list(RuntimeProfile))
def test_native_resume_without_a_journal_rejects_ambiguous_commit_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    profile: RuntimeProfile,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, profile)
    claim_id, index_sha = _claim_native_generation(manager, index)
    receipt_path = manager.layout.receipt_file("1.1.0")
    if state == "pointer":
        manager.layout.activate("1.1.0")
    elif state == "receipt":
        receipt_path.write_text("{}", encoding="utf-8")
    else:
        other_receipt = tmp_path / "other-receipt.json"
        other_receipt.write_text("{}", encoding="utf-8")
        receipt_path.symlink_to(other_receipt)
    snapshot = _native_resume_snapshot(manager)
    _forbid_resume_recovery_and_archive(manager, monkeypatch)

    with pytest.raises(
        ManagedInstallError,
        match=r"^Native bootstrap resume cannot reconcile the retained transaction\.$",
    ):
        _adopt(
            manager,
            index,
            artifact,
            expected_index_sha256=index_sha,
            resume_bootstrap_claim_id=claim_id,
        )

    assert _native_resume_snapshot(manager) == snapshot
    _assert_released(manager)


@pytest.mark.parametrize("profile", list(RuntimeProfile))
@pytest.mark.parametrize("prior", ["missing", "completed_adopt", "completed_update"])
def test_native_resume_of_an_older_generation_preserves_newer_active_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
    prior: str,
) -> None:
    # Both operation kinds can own the completed prior installation journal.
    manager, index, artifact = _case(tmp_path, monkeypatch, profile)
    claim_id, index_sha = _claim_native_generation(manager, index)
    _seed_target(manager.layout, "1.2.0", profile)
    manager.layout.activate("1.2.0")
    if prior != "missing":
        transaction = "d" * 32
        manager.layout.receipt_file("1.2.0").write_text(
            json.dumps(
                {
                    "format": managed_install.RELEASE_RECEIPT_FORMAT,
                    "schema_version": managed_install.RELEASE_RECEIPT_VERSION,
                    "version": "1.2.0",
                    "transaction_id": transaction,
                    "application_profile": profile.value,
                }
            )
        )
        manager.layout.journal_file.write_text(
            json.dumps(
                {
                    "format": managed_install.UPDATE_JOURNAL_FORMAT,
                    "schema_version": managed_install.UPDATE_JOURNAL_VERSION,
                    "transaction_id": transaction,
                    "operation": "adopt" if prior == "completed_adopt" else "update",
                    "phase": "completed",
                    "target_version": "1.2.0",
                    "previous_version": "1.0.0",
                    "application_profile": profile.value,
                    "program_root": str(manager.layout.root.resolve()),
                    "data_root": str(manager.paths.data_root.resolve()),
                }
            )
        )
    before = _native_resume_snapshot(manager)
    _forbid_resume_recovery_and_archive(manager, monkeypatch)
    with pytest.raises(ManagedInstallError):
        _adopt(
            manager,
            index,
            artifact,
            expected_index_sha256=index_sha,
            resume_bootstrap_claim_id=claim_id,
        )
    assert _native_resume_snapshot(manager) == before
    _assert_released(manager)


@pytest.mark.parametrize("profile", list(RuntimeProfile))
def test_native_resume_commit_readback_uses_fresh_process_contexts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, profile)
    claim, index_sha = _claim_native_generation(manager, index)
    original = _adopt(manager, index, artifact, expected_index_sha256=index_sha)
    state = _native_resume_snapshot(manager)
    state.pop("journal")
    paths_file = tmp_path / "process-paths.json"
    paths_file.write_text(
        json.dumps(
            {
                name: str(value)
                if isinstance(value, Path)
                else [str(path) for path in value]
                if isinstance(value, tuple)
                else value
                for name, value in asdict(manager.paths).items()
            }
        )
    )
    bridge = tmp_path / "resume-readback.py"
    bridge.write_text("""import importlib, json, sys
from pathlib import Path
from carlina.core.installation_lifecycle import InstallationLifecycle
from carlina.runtime_profiles import RuntimeProfile
kernel = importlib.import_module(sys.argv[9])
paths_module = importlib.import_module(sys.argv[10])
raw = json.loads(Path(sys.argv[1]).read_bytes())
values = {name: value if name == 'mode' else tuple(Path(p) for p in value)
    if name == 'legacy_locations' else Path(value) for name, value in raw.items()}
paths = paths_module.AppPaths(**values)
manager = kernel.ManagedReleaseManager(paths, program_root=sys.argv[2], platform_tag='win-64')
def forbidden(*args, **kwargs):
    raise AssertionError('Committed readback executed a target or recovery action')
kernel._verify_unpacked_application_profile = forbidden
kernel._verify_target_runtime = forbidden
kernel.ManagedInstallationLayout.write_launchers = forbidden
for name in ('_create_managed_backup', '_restore_failed_update_data',
    '_recover_interrupted_database_switches', '_recover_interrupted_rollback',
    '_recover_interrupted_update', '_archive_existing_journal'):
    setattr(manager, name, forbidden)
context = InstallationLifecycle('install', RuntimeProfile(sys.argv[7]), operation_id=sys.argv[8])
result = manager.adopt_installed_release(sys.argv[3], sys.argv[4],
    manager.layout.version_dir('1.1.0'), requested_version='1.1.0',
    expected_index_sha256=sys.argv[6], resume_bootstrap_claim_id=sys.argv[5],
    allow_unsigned_local=True, lifecycle=context)
assert context.terminal
print(json.dumps({'transaction': result.transaction_id, 'active': result.active_version}))
""")
    operation_ids = ("e" * 32, "f" * 32)
    for operation_id in operation_ids:
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                str(bridge),
                str(paths_file),
                str(manager.layout.root),
                str(index),
                str(artifact),
                claim,
                index_sha,
                profile.value,
                operation_id,
                managed_install.__name__,
                app_paths_module.__name__,
            ],
            env={
                **os.environ,
                "CARLINA_HOME": str(manager.paths.data_root),
                "CARLINA_PROFILE": profile.value,
            },
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {
            "transaction": original.transaction_id,
            "active": "1.1.0",
        }
        journal = json.loads(manager.layout.journal_file.read_bytes())
        assert journal["transaction_id"] == original.transaction_id
        assert journal["last_reconciled_operation_id"] == operation_id
        after = _native_resume_snapshot(manager)
        after.pop("journal")
        assert after == state
        _assert_released(manager)


@pytest.mark.parametrize("profile", list(RuntimeProfile))
@pytest.mark.parametrize("operation", ["update", "rollback"])
def test_native_recovery_rejects_a_different_data_root_before_any_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
    operation: str,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, profile)
    _claim_id, index_sha = _claim_native_generation(manager, index)
    _adopt(manager, index, artifact, expected_index_sha256=index_sha)
    manager.layout.activate("1.0.0")
    # Reconstruct interruption before write_launchers commits installation metadata.
    # Older/bootstrap runtimes can have a pointer without a root-bound metadata file.
    manager.layout.metadata_file.unlink()
    journal = json.loads(manager.layout.journal_file.read_bytes())
    journal.pop("automatic_data_restore", None)
    manager._write_journal(journal, phase="target_verification")
    prior = manager.layout.journal_file.read_bytes()
    foreign_root = tmp_path / "cloned data with matching keys"
    shutil.copytree(manager.paths.data_root, foreign_root)
    foreign_paths = resolve_app_paths(
        tmp_path / "source",
        package_root=tmp_path / "source/iarf_assistant",
        environment={
            "CARLINA_HOME": str(foreign_root),
            "CARLINA_PROFILE": profile.value,
        },
        allow_legacy=False,
    )
    _marker(foreign_paths, "foreign must survive")
    foreign = ManagedReleaseManager(
        foreign_paths, program_root=manager.layout.root, platform_tag="win-64"
    )
    with pytest.raises(ManagedInstallError):
        if operation == "update":
            foreign.update(index, requested_version="1.1.0", allow_unsigned_local=True)
        else:
            foreign.rollback(restore_data=True)
    assert _marker(foreign_paths) == "foreign must survive"
    assert _marker(manager.paths) == "after"
    assert manager.layout.journal_file.read_bytes() == prior
    assert not list(manager.layout.history_dir.iterdir())
    _assert_released(foreign)


@pytest.mark.parametrize("profile", list(RuntimeProfile))
def test_update_reuse_preserves_native_founder_identity_across_new_index_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
) -> None:
    manager, index, artifact = _case(tmp_path, monkeypatch, profile)
    claim_id, index_sha = _claim_native_generation(manager, index)
    _adopt(manager, index, artifact, expected_index_sha256=index_sha)
    target = manager.layout.version_dir("1.1.0")
    binding = (target / BOOTSTRAP_BINDING_NAME).read_bytes()
    frame = (target / BOOTSTRAP_CLAIM_NAME).read_bytes()
    monkeypatch.setattr(
        managed_install,
        "_unpack_runtime",
        lambda *_args, **_kwargs: pytest.fail(
            "Reusing a native generation unpacked it again."
        ),
    )
    for _ in range(2):
        manager.layout.activate("1.0.0")
        _marker(manager.paths, "before")
        index.write_bytes(index.read_bytes() + b"\n")
        manager.update(index, requested_version="1.1.0", allow_unsigned_local=True)
        receipt = manager.layout.read_receipt("1.1.0")
        assert receipt["bootstrap_claim_id"] == claim_id
        assert receipt["bootstrap_index_sha256"] == index_sha
        assert receipt["index_sha256"] != index_sha
        assert (target / BOOTSTRAP_BINDING_NAME).read_bytes() == binding
        assert (target / BOOTSTRAP_CLAIM_NAME).read_bytes() == frame


@pytest.mark.parametrize("profile", list(RuntimeProfile))
def test_quarantine_refuses_a_reserved_native_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
) -> None:
    manager, index, _artifact = _case(tmp_path, monkeypatch, profile)
    _claim_native_generation(manager, index, ready=False)
    target = manager.layout.version_dir("1.1.0")
    frame = (target / BOOTSTRAP_CLAIM_NAME).read_bytes()
    with pytest.raises(ManagedInstallError):
        manager._quarantine_incomplete_release("1.1.0", transaction_id="d" * 32)
    assert (target / BOOTSTRAP_CLAIM_NAME).read_bytes() == frame
    assert manager.layout.current_version() == "1.0.0"


def test_quarantine_checks_lexical_link_before_resolving_active_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _index, _artifact = _case(tmp_path, monkeypatch, RuntimeProfile.WORKBENCH)
    active = manager.layout.version_dir("1.0.0")
    alias = manager.layout.versions_dir / "1.1.0"
    alias.symlink_to(active, target_is_directory=True)
    with pytest.raises(ManagedInstallError):
        manager._quarantine_incomplete_release("1.1.0", transaction_id="d" * 32)
    assert active.is_dir()
    assert alias.is_symlink()
    assert manager.layout.current_version() == "1.0.0"
