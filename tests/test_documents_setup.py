from __future__ import annotations

import json
import multiprocessing
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from carlina.documents_setup import (
    DocumentsSetupError,
    begin_documents_setup,
    complete_documents_setup,
    inspect_documents_setup,
)
from carlina.runtime_profiles import RuntimeProfile
from iarf_assistant.core.runtime_ownership import runtime_leader_lock

_ADDON_IDS = ("sentence_segmenter", "doc_processor", "translator")
_REQUIRED = {
    "sentence_segmenter": [],
    "doc_processor": ["extract-msg-gpl", "pymupdf-agpl"],
    "translator": [],
}


@dataclass(frozen=True)
class _Paths:
    data_root: Path
    state_dir: Path


class _NoTouchPaths:
    @property
    def data_root(self) -> Path:
        raise AssertionError("paths must not be inspected")

    @property
    def state_dir(self) -> Path:
        raise AssertionError("paths must not be inspected")


class _Manager:
    def __init__(self) -> None:
        self.loaded_addons: dict[str, object] = {}
        self.notices: dict[str, object] = {
            "sentence_segmenter": [],
            "doc_processor": [
                {"id": "pymupdf-agpl", "review_required": True},
                {"id": "extract-msg-gpl", "review_required": True},
            ],
            "translator": [],
        }
        self.events: list[tuple[object, ...]] = []
        self.fail_addon: str | None = None

    def get_addon_dependency_notices(self, addon_id: str) -> object:
        self.events.append(("notices", addon_id))
        return self.notices[addon_id]

    @contextmanager
    def lifecycle_maintenance_authority(self) -> Iterator[None]:
        self.events.append(("authority", "enter"))
        try:
            yield
        finally:
            self.events.append(("authority", "exit"))

    def activate_addon(
        self, addon_id: str, *, dependency_review: list[str] | None = None
    ) -> None:
        self.events.append(("activate", addon_id, dependency_review))
        if addon_id == self.fail_addon:
            raise RuntimeError("private activation detail")
        self.loaded_addons[addon_id] = object()


class _UntouchableManager:
    @property
    def loaded_addons(self) -> Mapping[str, object]:
        raise AssertionError("manager must not be inspected")

    def get_addon_dependency_notices(self, addon_id: str) -> object:
        del addon_id
        raise AssertionError("manager must not be inspected")

    def lifecycle_maintenance_authority(self) -> Any:
        raise AssertionError("manager must not be inspected")

    def activate_addon(
        self, addon_id: str, *, dependency_review: list[str] | None = None
    ) -> None:
        del addon_id, dependency_review
        raise AssertionError("manager must not be inspected")


def _paths(tmp_path: Path) -> _Paths:
    data_root = tmp_path / "documents-data"
    data_root.mkdir()
    return _Paths(data_root=data_root, state_dir=data_root / "state")


def _marker(paths: _Paths) -> Path:
    return paths.state_dir / "bootstrap" / "documents-products.json"


def _begin(paths: _Paths) -> bool:
    return begin_documents_setup(
        paths,
        profile=RuntimeProfile.DOCUMENTS,
        has_existing_accounts=False,
    )


def _hold_setup_lock(
    data_root: str,
    state_dir: str,
    ready: Any,
    release: Any,
) -> None:
    paths = _Paths(Path(data_root), Path(state_dir))
    with runtime_leader_lock(paths, timeout=0):
        ready.set()
        release.wait(15)


def test_workbench_and_existing_accounts_return_before_path_access(
    tmp_path: Path,
) -> None:
    assert (
        begin_documents_setup(
            _NoTouchPaths(),
            profile=RuntimeProfile.WORKBENCH,
            has_existing_accounts=False,
        )
        is False
    )
    assert (
        begin_documents_setup(
            _NoTouchPaths(),
            profile=RuntimeProfile.DOCUMENTS,
            has_existing_accounts=True,
        )
        is False
    )
    assert not complete_documents_setup(
        _NoTouchPaths(),
        profile=RuntimeProfile.WORKBENCH,
        has_administrator=True,
        manager=_UntouchableManager(),
    )
    assert inspect_documents_setup(_paths(tmp_path)) == "absent"
    assert not (tmp_path / "documents-data" / "state").exists()


def test_begin_writes_pending_atomically_with_private_mode(tmp_path: Path) -> None:
    paths = _paths(tmp_path)

    assert _begin(paths) is True
    assert _begin(paths) is False
    assert inspect_documents_setup(paths) == "pending"

    marker = _marker(paths)
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload == {
        "format": "carlina.documents-setup",
        "schema_version": 1,
        "state": "pending",
        "addon_ids": list(_ADDON_IDS),
    }
    if os.name != "nt":
        assert marker.stat().st_mode & 0o777 == 0o600
    assert list(marker.parent.glob("*.tmp")) == []


def test_no_administrator_does_not_activate_pending_setup(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    assert _begin(paths) is True

    assert (
        complete_documents_setup(
            paths,
            profile=RuntimeProfile.DOCUMENTS,
            has_administrator=False,
            manager=_UntouchableManager(),  # type: ignore[arg-type]
        )
        is False
    )
    assert inspect_documents_setup(paths) == "pending"


def test_admin_with_absent_marker_leaves_products_untouched(tmp_path: Path) -> None:
    paths = _paths(tmp_path)

    assert (
        complete_documents_setup(
            paths,
            profile=RuntimeProfile.DOCUMENTS,
            has_administrator=True,
            manager=_UntouchableManager(),  # type: ignore[arg-type]
        )
        is False
    )
    assert inspect_documents_setup(paths) == "absent"
    assert list(tmp_path.iterdir()) == [paths.data_root]


def test_completion_validates_all_notices_then_activates_in_fixed_order(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    assert _begin(paths) is True
    manager = _Manager()
    callbacks: list[str] = []

    def on_product(addon_id: str) -> None:
        callbacks.append(addon_id)
        manager.events.append(("callback", addon_id))

    assert (
        complete_documents_setup(
            paths,
            profile=RuntimeProfile.DOCUMENTS,
            has_administrator=True,
            manager=manager,
            on_product=on_product,
        )
        is True
    )

    assert manager.events[:3] == [("notices", addon_id) for addon_id in _ADDON_IDS]
    assert callbacks == list(_ADDON_IDS)
    assert manager.events[3:] == [
        ("authority", "enter"),
        ("callback", "sentence_segmenter"),
        ("activate", "sentence_segmenter", []),
        ("callback", "doc_processor"),
        ("activate", "doc_processor", _REQUIRED["doc_processor"]),
        ("callback", "translator"),
        ("activate", "translator", []),
        ("authority", "exit"),
    ]
    assert set(manager.loaded_addons) == set(_ADDON_IDS)

    payload = json.loads(_marker(paths).read_text(encoding="utf-8"))
    assert payload == {
        "format": "carlina.documents-setup",
        "schema_version": 1,
        "state": "complete",
        "addon_ids": list(_ADDON_IDS),
        "notice_review_source": "bundled_documents_distribution",
        "reviewed_dependency_notices": _REQUIRED,
    }
    assert inspect_documents_setup(paths) == "complete"


def test_completed_setup_does_not_reenable_disabled_products(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    assert _begin(paths) is True
    first_manager = _Manager()
    assert (
        complete_documents_setup(
            paths,
            profile=RuntimeProfile.DOCUMENTS,
            has_administrator=True,
            manager=first_manager,
        )
        is True
    )
    second_manager = _UntouchableManager()

    def unexpected_lock(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("completed setup must not acquire mutation authority")

    monkeypatch.setattr(
        "carlina.documents_setup._runtime_lock_factory", unexpected_lock
    )

    assert (
        complete_documents_setup(
            paths,
            profile=RuntimeProfile.DOCUMENTS,
            has_administrator=True,
            manager=second_manager,  # type: ignore[arg-type]
        )
        is False
    )
    assert inspect_documents_setup(paths) == "complete"


def test_bundled_manifests_match_the_reviewed_dependency_notices() -> None:
    root = Path(__file__).resolve().parents[1]
    for addon_id in _ADDON_IDS:
        manifest = json.loads(
            (root / "iarf_assistant/addons" / addon_id / "manifest.json").read_text()
        )
        required = sorted(
            notice["id"]
            for notice in manifest.get("dependency_notices", [])
            if notice.get("review_required", True)
        )
        assert required == _REQUIRED[addon_id]


def test_activation_failure_keeps_pending_and_fresh_manager_can_retry(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    assert _begin(paths) is True
    first_manager = _Manager()
    first_manager.fail_addon = "doc_processor"

    with pytest.raises(DocumentsSetupError) as error:
        complete_documents_setup(
            paths,
            profile=RuntimeProfile.DOCUMENTS,
            has_administrator=True,
            manager=first_manager,
        )
    assert error.value.code == "documents_setup_activation_failed"
    assert error.value.addon_id == "doc_processor"
    assert "private activation detail" not in str(error.value)
    assert inspect_documents_setup(paths) == "pending"

    retry_manager = _Manager()
    assert (
        complete_documents_setup(
            paths,
            profile=RuntimeProfile.DOCUMENTS,
            has_administrator=True,
            manager=retry_manager,
        )
        is True
    )
    assert [
        event[1] for event in retry_manager.events if event[0] == "activate"
    ] == list(_ADDON_IDS)
    assert inspect_documents_setup(paths) == "complete"


def test_unknown_required_notice_fails_before_any_activation(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    assert _begin(paths) is True
    manager = _Manager()
    manager.notices["translator"] = [{"id": "unknown-license", "review_required": True}]

    with pytest.raises(DocumentsSetupError) as error:
        complete_documents_setup(
            paths,
            profile=RuntimeProfile.DOCUMENTS,
            has_administrator=True,
            manager=manager,
        )
    assert error.value.code == "documents_setup_unreviewed_notices"
    assert not any(event[0] == "activate" for event in manager.events)
    assert inspect_documents_setup(paths) == "pending"


def test_malformed_notice_return_fails_closed_before_activation(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    assert _begin(paths) is True
    manager = _Manager()
    manager.notices["translator"] = [{"id": "missing-flag"}]

    with pytest.raises(DocumentsSetupError) as error:
        complete_documents_setup(
            paths,
            profile=RuntimeProfile.DOCUMENTS,
            has_administrator=True,
            manager=manager,
        )
    assert error.value.code == "documents_setup_unreviewed_notices"
    assert not any(event[0] == "activate" for event in manager.events)


@pytest.mark.parametrize(
    "contents",
    [
        b"{",
        b"[" * 2_000 + b"]" * 2_000,
        b'{"format":"carlina.documents-setup","format":"other"}',
        json.dumps(
            {
                "format": "carlina.documents-setup",
                "schema_version": 1,
                "state": "pending",
                "addon_ids": list(_ADDON_IDS),
                "unexpected": True,
            }
        ).encode(),
        json.dumps(
            {
                "format": "carlina.documents-setup",
                "schema_version": 1,
                "state": "complete",
                "addon_ids": list(_ADDON_IDS),
                "notice_review_source": "bundled_documents_distribution",
                "reviewed_dependency_notices": {
                    **_REQUIRED,
                    "translator": ["unexpected"],
                },
            }
        ).encode(),
    ],
)
def test_invalid_marker_fails_closed(tmp_path: Path, contents: bytes) -> None:
    paths = _paths(tmp_path)
    marker = _marker(paths)
    marker.parent.mkdir(parents=True)
    marker.write_bytes(contents)

    with pytest.raises(DocumentsSetupError) as error:
        inspect_documents_setup(paths)
    assert error.value.code == "documents_setup_state_invalid"


def test_oversized_marker_fails_closed(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    marker = _marker(paths)
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b" " * (16 * 1024 + 1))

    with pytest.raises(DocumentsSetupError) as error:
        inspect_documents_setup(paths)
    assert error.value.code == "documents_setup_state_invalid"


def test_symlink_marker_and_bootstrap_directory_fail_closed(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    bootstrap = _marker(paths).parent
    bootstrap.mkdir(parents=True)
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    marker = _marker(paths)
    try:
        marker.symlink_to(target)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlinks unavailable: {type(error).__name__}")

    with pytest.raises(DocumentsSetupError):
        inspect_documents_setup(paths)

    marker.unlink()
    outside_dir = tmp_path / "outside-bootstrap"
    outside_dir.mkdir()
    bootstrap.rmdir()
    bootstrap.symlink_to(outside_dir, target_is_directory=True)
    with pytest.raises(DocumentsSetupError):
        inspect_documents_setup(paths)


def test_callback_failure_leaves_pending_with_fixed_target_error(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    assert _begin(paths) is True
    manager = _Manager()

    def fail_callback(addon_id: str) -> None:
        if addon_id == "doc_processor":
            raise RuntimeError("private callback detail")

    with pytest.raises(DocumentsSetupError) as error:
        complete_documents_setup(
            paths,
            profile=RuntimeProfile.DOCUMENTS,
            has_administrator=True,
            manager=manager,
            on_product=fail_callback,
        )
    assert error.value.code == "documents_setup_activation_failed"
    assert error.value.addon_id == "doc_processor"
    assert "private callback detail" not in str(error.value)
    assert inspect_documents_setup(paths) == "pending"


def test_independent_process_runtime_lock_blocks_activation(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    assert _begin(paths) is True
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_setup_lock,
        args=(str(paths.data_root), str(paths.state_dir), ready, release),
    )
    process.start()
    try:
        assert ready.wait(10)
        manager = _Manager()
        with pytest.raises(DocumentsSetupError) as error:
            complete_documents_setup(
                paths,
                profile=RuntimeProfile.DOCUMENTS,
                has_administrator=True,
                manager=manager,
            )
        assert error.value.code == "documents_setup_busy"
        assert manager.events == []
        assert inspect_documents_setup(paths) == "pending"
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)
    assert process.exitcode == 0
