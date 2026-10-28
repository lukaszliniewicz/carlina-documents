from __future__ import annotations

import builtins
import hashlib
import importlib
import json
import sys
import warnings
import zipfile
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from scripts import measure_documents_closure as collector
from scripts.documents_dependency_inventory import LockedPackage, LockInventory


def test_source_metrics_hashes_sorted_member_bytes_and_groups_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "synthetic"
    root.mkdir()
    members = (
        "iarf_assistant/addons/doc_processor/worker.py",
        "carlina/__init__.py",
        "iarf_assistant/app.py",
    )
    payloads = {
        members[0]: b"addon",
        members[1]: b"host package",
        members[2]: b"assistant host",
    }
    for member, payload in payloads.items():
        path = root / member
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    monkeypatch.setattr(
        collector, "documents_backend_members", lambda _root: tuple(reversed(members))
    )

    result = collector.source_metrics(root)
    assert result == collector.source_metrics(root)
    assert result["member_count"] == 3
    assert result["bytes"] == sum(map(len, payloads.values()))
    groups = result["groups"]
    assert isinstance(groups, list)
    by_name = {group["name"]: group for group in groups}
    assert by_name["carlina"]["modules"] == ["carlina"]
    assert by_name["carlina"]["members"] == ["carlina/__init__.py"]
    assert by_name["iarf_assistant.host"]["modules"] == ["iarf_assistant.app"]
    assert by_name["iarf_assistant.host"]["members"] == ["iarf_assistant/app.py"]
    assert by_name["iarf_assistant.addons.doc_processor"]["modules"] == [
        "iarf_assistant.addons.doc_processor.worker"
    ]
    assert by_name["iarf_assistant.addons.doc_processor"]["members"] == [
        "iarf_assistant/addons/doc_processor/worker.py"
    ]
    assert all(group["members"] == sorted(group["members"]) for group in groups)
    assert str(root) not in json.dumps(result)
    digest = result["sha256"]
    assert isinstance(digest, str)

    (root / members[1]).write_bytes(b"changed")
    assert collector.source_metrics(root)["sha256"] != digest


def test_wheel_metrics_counts_groups_metadata_and_ignores_nested_archive_bytes(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "synthetic.whl"
    nested_payload = b"source archive contents must never be emitted"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("pkg/", b"")
        archive.writestr("pkg/__init__.py", b"package")
        archive.writestr("other/worker.py", b"worker")
        archive.writestr("carlina/application-source.zip", nested_payload)
        archive.writestr("pkg-1.0.dist-info/METADATA", b"Name: pkg\n")
        archive.writestr("pkg-1.0.dist-info/RECORD", b"record")

    result = collector.wheel_metrics(wheel)
    assert result["file_sha256"] == hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert result["file_bytes"] == wheel.stat().st_size
    assert result["file_count"] == 5
    assert result["metadata_count"] == 1
    assert result["record_count"] == 1
    groups = result["groups"]
    assert isinstance(groups, list)
    assert [group["name"] for group in groups] == [
        "carlina",
        "other",
        "pkg",
        "pkg-1.0.dist-info",
    ]
    assert nested_payload.decode() not in json.dumps(result)


@pytest.mark.parametrize(
    "member",
    ["../escape.py", "/absolute.py", "pkg/../escape.py", "pkg\\escape.py"],
)
def test_wheel_metrics_rejects_unsafe_member_names(tmp_path: Path, member: str) -> None:
    wheel = tmp_path / "unsafe.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(member, b"synthetic")
    with pytest.raises(
        ValueError, match=r"^Documents closure inventory input is invalid\.$"
    ):
        collector.wheel_metrics(wheel)


def test_wheel_metrics_rejects_duplicate_member_names(tmp_path: Path) -> None:
    wheel = tmp_path / "duplicate.whl"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("pkg/module.py", b"first")
            archive.writestr("pkg/module.py", b"second")
    with pytest.raises(
        ValueError, match=r"^Documents closure inventory input is invalid\.$"
    ):
        collector.wheel_metrics(wheel)


def test_directory_metrics_counts_but_does_not_follow_external_symlinks(
    tmp_path: Path,
) -> None:
    root = tmp_path / "tree"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "inside.txt").write_bytes(b"small")
    (outside / "payload.bin").write_bytes(b"x" * (1024 * 1024))
    try:
        (root / "external-file").symlink_to(outside / "payload.bin")
        (root / "external-directory").symlink_to(outside, target_is_directory=True)
    except NotImplementedError, OSError:
        pytest.skip("symlink creation is unavailable")

    result = collector.directory_metrics(root)
    assert result["file_count"] == 1
    assert result["directory_count"] == 1
    assert result["symlink_count"] == 2
    assert result["logical_bytes"] < (outside / "payload.bin").stat().st_size
    assert result["allocated_bytes"] is None or result["allocated_bytes"] >= 0


def test_locked_dependencies_follow_manifest_contexts_without_locator_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "source"
    manifests = {
        "doc_processor": [
            {
                "id": "host",
                "environment": "in_process_vendor",
                "lock_format": "pip_requirements",
                "lockfile": "requirements-host.lock",
            },
            {
                "id": "document_processing",
                "environment": "managed_process_pixi",
                "lock_format": "pixi",
                "lockfile": "pixi.lock",
            },
        ],
        "sentence_segmenter": [
            {
                "id": "native_segmentation",
                "environment": "managed_process_pixi",
                "lock_format": "pixi",
                "lockfile": "pixi.lock",
            }
        ],
        "translator": [
            {
                "id": "host",
                "environment": "in_process_vendor",
                "lock_format": "pip_requirements",
                "lockfile": "requirements.lock",
            }
        ],
    }
    for addon, environments in manifests.items():
        addon_root = root / "iarf_assistant" / "addons" / addon
        addon_root.mkdir(parents=True)
        (addon_root / "manifest.json").write_text(
            json.dumps({"dependencies": {"python": {"environments": environments}}}),
            encoding="utf-8",
        )
        for environment in environments:
            (addon_root / environment["lockfile"]).write_text(
                "synthetic", encoding="utf-8"
            )
    (root / "pixi.lock").write_text("synthetic", encoding="utf-8")

    package = LockedPackage(
        ecosystem="pypi",
        name="synthetic-package",
        version="1.2.3",
        locator="https://example.invalid/package?token=private",
        sha256=("a" * 64,),
        license_expression=None,
        build=None,
        subdir=None,
    )
    lock = LockInventory(lock_sha256="b" * 64, packages=(package,))
    pixi_calls: list[tuple[str, str, str]] = []
    requirements_calls: list[str] = []

    def fake_pixi(path: Path, *, environment: str, platform: str) -> LockInventory:
        pixi_calls.append((path.relative_to(root).as_posix(), environment, platform))
        return lock

    def fake_requirements(path: Path) -> LockInventory:
        requirements_calls.append(path.relative_to(root).as_posix())
        return lock

    monkeypatch.setattr(collector, "load_pixi_lock", fake_pixi)
    monkeypatch.setattr(collector, "load_requirements_lock", fake_requirements)

    result = collector.locked_dependencies(root, "linux-64")
    contexts = result["contexts"]
    assert isinstance(contexts, list)
    assert {context["context"] for context in contexts} == {
        "documents",
        "doc_processor:host",
        "doc_processor:document_processing",
        "sentence_segmenter:native_segmentation",
        "translator:host",
    }
    assert pixi_calls == [
        ("pixi.lock", "documents", "linux-64"),
        (
            "iarf_assistant/addons/doc_processor/pixi.lock",
            "default",
            "linux-64",
        ),
        (
            "iarf_assistant/addons/sentence_segmenter/pixi.lock",
            "default",
            "linux-64",
        ),
    ]
    assert set(requirements_calls) == {
        "iarf_assistant/addons/doc_processor/requirements-host.lock",
        "iarf_assistant/addons/translator/requirements.lock",
    }
    assert "token=private" not in json.dumps(result)
    assert set(contexts[0]["packages"][0]) == {"name", "version", "ecosystem"}


def test_runtime_snapshot_records_only_module_names_job_ids_and_presence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        sys.modules, "carlina.synthetic_module", ModuleType("carlina.synthetic_module")
    )
    app = SimpleNamespace(
        extensions={"carlina.compass_client_token_limiter": object()},
        scheduler=SimpleNamespace(
            get_jobs=lambda: [SimpleNamespace(id="z-job"), SimpleNamespace(id="a-job")]
        ),
        config={"SECRET_KEY": "never emitted"},
    )

    result = collector.runtime_snapshot(app)
    assert "carlina.synthetic_module" in result["host_modules"]
    assert result["measurement_scope"] == "current process including collector imports"
    assert result["scheduler_job_ids"] == ["a-job", "z-job"]
    assert result["active_thread_count"] >= 1
    assert result["extensions"] == {
        "carlina.compass_client_auth_service": False,
        "carlina.compass_client_principal_resolver": False,
        "carlina.compass_client_token_limiter": True,
    }
    assert "never emitted" not in json.dumps(result)


def test_cli_creates_new_json_and_rejects_output_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(collector, "source_metrics", lambda _root: {"member_count": 0})
    monkeypatch.setattr(
        collector, "locked_dependencies", lambda _root, _platform: {"contexts": []}
    )
    output = tmp_path / "closure.json"
    args = ["--root", str(tmp_path), "--output", str(output)]

    assert collector.main(args) == 0
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["schema_version"] == 1
    assert result["shipped_wheel"] is None
    assert result["imported_runtime"] is None
    assert result["storage"]["trees"] is None
    assert result["storage"]["peak_install_bytes"] is None
    assert "not measured" in result["storage"]["peak_install_bytes_note"]
    assert "not reclaimed-space" in result["storage"]["allocated_bytes_note"]

    output.write_text("keep", encoding="utf-8")
    assert collector.main(args) != 0
    assert output.read_text(encoding="utf-8") == "keep"
    assert str(tmp_path) not in capsys.readouterr().err


def test_cli_input_errors_are_content_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail_source(_root: Path) -> dict[str, object]:
        raise ValueError(f"private path: {tmp_path}")

    monkeypatch.setattr(collector, "source_metrics", fail_source)
    output = tmp_path / "closure.json"

    assert collector.main(["--root", str(tmp_path), "--output", str(output)]) != 0
    error = capsys.readouterr().err
    assert error == "Documents closure inventory input is invalid.\n"
    assert str(tmp_path) not in error


def test_collector_import_does_not_import_host_or_product_application_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__

    def guarded_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "carlina" or name.startswith(
            ("carlina.app", "iarf_assistant.app", "iarf_assistant.addons.")
        ):
            raise AssertionError("collector attempted an application import")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    importlib.reload(collector)


def test_runtime_input_rejects_unrelated_or_secret_json(tmp_path: Path) -> None:
    runtime_file = tmp_path / "unrelated.json"
    runtime_file.write_text('{"token": "must not be copied"}', encoding="utf-8")
    with pytest.raises(ValueError, match="Documents closure inventory input"):
        collector._runtime_json(runtime_file)

    snapshot = collector.runtime_snapshot(
        SimpleNamespace(extensions={}, scheduler=None)
    )
    runtime_file.write_text(json.dumps(snapshot), encoding="utf-8")
    assert collector._runtime_json(runtime_file) == snapshot
    snapshot["extensions"] = {"token": "must not be copied"}
    runtime_file.write_text(json.dumps(snapshot), encoding="utf-8")
    with pytest.raises(ValueError, match="Documents closure inventory input"):
        collector._runtime_json(runtime_file)
