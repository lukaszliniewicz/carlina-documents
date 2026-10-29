from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import struct
import subprocess
from pathlib import Path
from typing import Any, TypedDict

import pytest

from scripts import build_documents_windows_installer as builder

_VERSION = "0.2.0a2"
_INDEX_SHA256 = "a" * 64
_INSTALLER_HASH = "b" * 64


class InstallerOptions(TypedDict, total=False):
    identity: dict[str, object]
    identity_bytes: bytes
    identity_header: str
    duplicate_assignment: bool


def _git(root: Path, *arguments: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", os.fspath(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    return result.stdout.strip()


def _make_source_checkout(
    temporary_path: Path,
    *,
    project_version: str = _VERSION,
    workspace_version: str = _VERSION,
) -> tuple[Path, str, int]:
    root = temporary_path / "source"
    files = {
        "scripts/documents_appimage.py": b"# synthetic fixture bootstrap\n",
        "scripts/build_documents_windows_installer.py": b"# synthetic fixture builder\n",
        "scripts/build_documents_appimage.py": b"# synthetic fixture source helper\n",
        "packaging/windows/carlina-documents.ico": b"synthetic icon bytes\n",
        "packaging/windows/requirements-build.lock": b"pyinstaller==6.22.3\n",
        "LICENSE": b"Synthetic license notice.\n",
        "COPYING-AGPL-3.0.txt": b"Synthetic AGPL notice.\n",
        "THIRD_PARTY_NOTICES.md": b"Synthetic dependency notices.\n",
        "third_party/windows-bootstrap/NOTICE.json": b'{"notices":1}\n',
        "third_party/windows-bootstrap/notices/vendor/thirdparty.txt": b"Synthetic third-party terms.\n",
        "third_party/windows-bootstrap/notices/nested/second.txt": b"Synthetic nested terms.\n",
        "pyproject.toml": (
            f'[project]\nname = "fixture"\nversion = "{project_version}"\n'
        ).encode(),
        "pixi.toml": (
            f'[workspace]\nname = "fixture"\nversion = "{workspace_version}"\n'
        ).encode(),
    }
    for relative, contents in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)

    _git(root, "init", "--quiet")
    _git(root, "config", "user.name", "Windows Builder Fixture")
    _git(root, "config", "user.email", "fixture@example.invalid")
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "config", "commit.gpgsign", "false")
    environment = os.environ.copy()
    environment["GIT_AUTHOR_DATE"] = "2026-01-02T03:04:05+00:00"
    environment["GIT_COMMITTER_DATE"] = "2026-01-02T03:04:05+00:00"
    _git(root, "add", "--all", env=environment)
    _git(
        root,
        "commit",
        "--quiet",
        "-m",
        "synthetic committed Windows builder inputs",
        env=environment,
    )
    revision = _git(root, "rev-parse", "HEAD")
    epoch = int(_git(root, "show", "-s", "--format=%ct", revision))
    return root, revision, epoch


def _identity(**changes: object) -> dict[str, object]:
    identity: dict[str, object] = {
        "application_profile": "documents",
        "release_version": _VERSION,
        "channel": "preview",
        "index_sha256": _INDEX_SHA256,
        "artifact_platform": "win-64",
        "artifact_kind": "publisher-bootstrap-executable",
        "artifact_filename": "carlina-documents-bootstrap-win-64.exe",
        "artifact_sha256": "c" * 64,
        "artifact_size": 1234,
    }
    identity.update(changes)
    return identity


def _write_installer(
    temporary_path: Path,
    *,
    identity: dict[str, object] | None = None,
    identity_bytes: bytes | None = None,
    identity_header: str | None = None,
    duplicate_assignment: bool = False,
) -> tuple[Path, str]:
    payload = identity_bytes or json.dumps(
        identity or _identity(), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    identity_sha256 = identity_header or hashlib.sha256(payload).hexdigest()
    encoded = base64.b64encode(payload).decode("ascii")
    lines = [
        f"$version = '{_VERSION}'",
        f"$indexSha256 = '{_INDEX_SHA256}'",
        f"$bootstrapIdentity = '{identity_sha256}'",
        f"$bootstrapIdentityBase64 = '{encoded}'",
    ]
    if duplicate_assignment:
        lines.append(f"$version = '{_VERSION}'")
    contents = (
        "\n".join(lines) + "\nWrite-Output 'synthetic installer body'\n"
    ).encode("utf-8")
    path = temporary_path / "installer.ps1"
    path.write_bytes(contents)
    return path, hashlib.sha256(contents).hexdigest()


def _allow_windows_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(builder.sys, "platform", "win32")
    monkeypatch.setattr(builder.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(builder.platform, "python_implementation", lambda: "CPython")
    monkeypatch.setattr(builder.platform, "python_version", lambda: "3.14.0")
    monkeypatch.setattr(builder.sys, "version_info", (3, 14, 0, "final", 0))
    monkeypatch.setattr(builder.struct, "calcsize", lambda _format: 8)
    versions = {
        "pyinstaller": "6.22.3",
        "pyinstaller-hooks-contrib": "2026.8",
    }
    monkeypatch.setattr(
        builder.importlib.metadata,
        "version",
        lambda distribution: versions[distribution],
    )


def _minimal_x64_gui_pe() -> bytes:
    payload = bytearray(512)
    payload[:2] = b"MZ"
    pe_offset = 0x80
    struct.pack_into("<I", payload, 0x3C, pe_offset)
    payload[pe_offset : pe_offset + 4] = b"PE\0\0"
    struct.pack_into(
        "<HHIIIHH",
        payload,
        pe_offset + 4,
        0x8664,
        1,
        0,
        0,
        0,
        0xF0,
        0x0022,
    )
    optional_header = pe_offset + 24
    struct.pack_into("<H", payload, optional_header, 0x20B)
    struct.pack_into("<H", payload, optional_header + 68, 2)
    return bytes(payload)


def _option(command: list[str], name: str) -> str:
    return command[command.index(name) + 1]


class _FakeFreezerProcess:
    def __init__(
        self,
        command: list[str],
        *,
        payload: bytes,
        on_start: Any,
        return_code: int = 0,
    ) -> None:
        self.command = command
        self.payload = payload
        self.on_start = on_start
        self.return_code = return_code
        self.stdout = io.BytesIO(b"synthetic freezer stdout\n")
        self.stderr = io.BytesIO(b"")
        self.killed = False

    def wait(self, timeout: int | None = None) -> int:
        assert timeout == builder._PROCESS_TIMEOUT_SECONDS
        self.on_start(self.command)
        executable = Path(_option(self.command, "--distpath")) / (
            _option(self.command, "--name") + ".exe"
        )
        executable.write_bytes(self.payload)
        return self.return_code

    def kill(self) -> None:
        self.killed = True


def _fake_freezer(
    monkeypatch: pytest.MonkeyPatch,
    *,
    payload: bytes | None = None,
    on_start: Any = lambda _command: None,
    return_code: int = 0,
) -> list[list[str]]:
    calls: list[list[str]] = []
    real_popen = builder.subprocess.Popen

    def popen(command: list[str], **kwargs: Any) -> Any:
        if len(command) >= 3 and command[1:3] == ["-m", "PyInstaller"]:
            calls.append(command)
            assert kwargs["stdin"] == subprocess.DEVNULL
            assert kwargs["stdout"] == subprocess.PIPE
            assert kwargs["stderr"] == subprocess.PIPE
            return _FakeFreezerProcess(
                command,
                payload=payload if payload is not None else _minimal_x64_gui_pe(),
                on_start=on_start,
                return_code=return_code,
            )
        return real_popen(command, **kwargs)

    monkeypatch.setattr(builder.subprocess, "Popen", popen)
    return calls


def _build(
    root: Path,
    revision: str,
    installer: Path,
    installer_sha256: str,
    output_dir: Path,
) -> tuple[Path, Path, Path]:
    return builder.build_windows_installer(
        root=root,
        version=_VERSION,
        source_revision=revision,
        output_dir=output_dir,
        installer=installer,
        installer_sha256=installer_sha256,
        release_index_sha256=_INDEX_SHA256,
    )


def test_host_and_toolchain_gates_precede_freezer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        builder.subprocess, "Popen", lambda command, **_kwargs: calls.append(command)
    )
    with pytest.raises(
        builder.DocumentsWindowsInstallerBuildError, match="native Windows"
    ):
        builder.build_windows_installer(
            root=tmp_path / "missing",
            version=_VERSION,
            source_revision="0" * 40,
            output_dir=tmp_path / "out",
            installer=tmp_path / "missing.ps1",
            installer_sha256="0" * 64,
            release_index_sha256="0" * 64,
        )
    assert calls == []

    _allow_windows_builder(monkeypatch)
    monkeypatch.setattr(
        builder.importlib.metadata,
        "version",
        lambda distribution: "6.22.2" if distribution == "pyinstaller" else "2026.8",
    )
    with pytest.raises(builder.DocumentsWindowsInstallerBuildError, match=r"6\.22\.3"):
        builder.build_windows_installer(
            root=tmp_path / "missing",
            version=_VERSION,
            source_revision="0" * 40,
            output_dir=tmp_path / "out",
            installer=tmp_path / "missing.ps1",
            installer_sha256="0" * 64,
            release_index_sha256="0" * 64,
        )
    assert calls == []


@pytest.mark.parametrize(
    ("installer_options", "expected_message"),
    [
        ({"identity": _identity(application_profile="workbench")}, "metadata"),
        ({"identity": _identity(artifact_platform="linux-64")}, "metadata"),
        ({"identity_header": "0" * 64}, "metadata"),
        ({"identity": _identity(artifact_sha256="bad")}, "metadata"),
        ({"identity": _identity(release_version="0.2.0a1")}, "metadata"),
        ({"identity": _identity(index_sha256="d" * 64)}, "metadata"),
        ({"duplicate_assignment": True}, "metadata"),
    ],
)
def test_wrong_or_altered_installer_identity_is_rejected_before_freezer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    installer_options: InstallerOptions,
    expected_message: str,
) -> None:
    _allow_windows_builder(monkeypatch)
    root, revision, _epoch = _make_source_checkout(tmp_path)
    installer, installer_sha256 = _write_installer(tmp_path, **installer_options)
    calls = _fake_freezer(monkeypatch)

    with pytest.raises(
        builder.DocumentsWindowsInstallerBuildError, match=expected_message
    ):
        _build(root, revision, installer, installer_sha256, tmp_path / "out")
    assert calls == []
    assert not (tmp_path / "out").exists()


def test_duplicate_identity_keys_are_rejected_before_freezer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _allow_windows_builder(monkeypatch)
    root, revision, _epoch = _make_source_checkout(tmp_path)
    canonical = json.dumps(_identity(), sort_keys=True, separators=(",", ":")).encode()
    duplicate = canonical.replace(
        b'"application_profile":"documents"',
        b'"application_profile":"documents","application_profile":"documents"',
    )
    installer, installer_sha256 = _write_installer(tmp_path, identity_bytes=duplicate)
    calls = _fake_freezer(monkeypatch)

    with pytest.raises(builder.DocumentsWindowsInstallerBuildError, match="metadata"):
        _build(root, revision, installer, installer_sha256, tmp_path / "out")
    assert calls == []


def test_installer_hash_mismatch_is_rejected_before_freezer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _allow_windows_builder(monkeypatch)
    root, revision, _epoch = _make_source_checkout(tmp_path)
    installer, _installer_sha256 = _write_installer(tmp_path)
    calls = _fake_freezer(monkeypatch)

    with pytest.raises(builder.DocumentsWindowsInstallerBuildError, match="SHA-256"):
        _build(root, revision, installer, "f" * 64, tmp_path / "out")
    assert calls == []
    assert not (tmp_path / "out").exists()


def test_clean_source_identity_and_versions_are_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _allow_windows_builder(monkeypatch)
    root, revision, _epoch = _make_source_checkout(
        tmp_path / "mismatch", workspace_version="0.2.0a1"
    )
    installer, installer_sha256 = _write_installer(tmp_path)
    calls = _fake_freezer(monkeypatch)

    with pytest.raises(builder.DocumentsWindowsInstallerBuildError, match="version"):
        _build(root, revision, installer, installer_sha256, tmp_path / "mismatch-out")
    assert calls == []

    dirty_root, dirty_revision, _epoch = _make_source_checkout(tmp_path / "dirty")
    (dirty_root / "scripts/documents_appimage.py").write_bytes(
        b"changed tracked source"
    )
    with pytest.raises(
        builder.DocumentsWindowsInstallerBuildError, match="tracked changes"
    ):
        _build(
            dirty_root,
            dirty_revision,
            installer,
            installer_sha256,
            tmp_path / "dirty-out",
        )
    assert calls == []

    with pytest.raises(builder.DocumentsWindowsInstallerBuildError, match="HEAD"):
        _build(
            dirty_root,
            "0" * 40,
            installer,
            installer_sha256,
            tmp_path / "wrong-head-out",
        )
    assert calls == []


def test_freezer_command_outputs_sidecar_and_preserves_license_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _allow_windows_builder(monkeypatch)
    root, revision, epoch = _make_source_checkout(tmp_path)
    installer, installer_sha256 = _write_installer(tmp_path)
    observed: dict[str, Any] = {}

    def inspect_staging(command: list[str]) -> None:
        cwd = Path(_option(command, "--specpath")).parent
        stage = cwd / "stage"
        observed["cwd"] = cwd
        observed["bootstrap"] = (stage / "bootstrap.py").read_bytes()
        observed["installer"] = (stage / "installer.ps1").read_bytes()
        observed["launcher"] = json.loads((stage / "launcher.json").read_text("utf-8"))
        observed["notices"] = sorted(
            path.relative_to(stage / "licenses").as_posix()
            for path in (stage / "licenses").rglob("*")
            if path.is_file()
        )
        observed["version_resource"] = Path(
            _option(command, "--version-file")
        ).read_text("utf-8")

    calls = _fake_freezer(monkeypatch, on_start=inspect_staging)
    executable, sidecar_path, checksum_path = _build(
        root,
        revision,
        installer,
        installer_sha256,
        tmp_path / "artifacts",
    )
    assert len(calls) == 1
    command = calls[0]
    artifact = json.loads(sidecar_path.read_text("utf-8"))
    executable_bytes = executable.read_bytes()
    executable_sha256 = hashlib.sha256(executable_bytes).hexdigest()

    assert executable.name == f"Carlina_Documents-{_VERSION}-Setup-x86_64.exe"
    assert executable_bytes == _minimal_x64_gui_pe()
    assert artifact == {
        "kind": "windows-setup-launcher",
        "platform": "win-64",
        "profile": "documents",
        "version": _VERSION,
        "source_revision": revision,
        "source_date_epoch": epoch,
        "sha256": executable_sha256,
        "bytes": len(executable_bytes),
        "input_hashes": {
            "installer": installer_sha256,
            "release_index": _INDEX_SHA256,
            "icon": hashlib.sha256(b"synthetic icon bytes\n").hexdigest(),
            "requirements_build_lock": hashlib.sha256(
                b"pyinstaller==6.22.3\n"
            ).hexdigest(),
            "notice_catalog": hashlib.sha256(b'{"notices":1}\n').hexdigest(),
            "notice:third_party/windows-bootstrap/notices/vendor/thirdparty.txt": hashlib.sha256(
                b"Synthetic third-party terms.\n"
            ).hexdigest(),
            "notice:third_party/windows-bootstrap/notices/nested/second.txt": hashlib.sha256(
                b"Synthetic nested terms.\n"
            ).hexdigest(),
        },
        "build_tools": {
            "python": "3.14.0",
            "pyinstaller": "6.22.3",
            "pyinstaller_hooks_contrib": "2026.8",
        },
    }
    assert str(tmp_path) not in sidecar_path.read_text("utf-8")
    assert (
        checksum_path.read_text("ascii") == f"{executable_sha256}  {executable.name}\n"
    )
    assert observed["bootstrap"] == b"# synthetic fixture bootstrap\n"
    assert observed["installer"] == installer.read_bytes()
    assert observed["launcher"] == {
        "schema_version": 1,
        "product": "carlina-documents",
        "version": _VERSION,
        "source_revision": revision,
        "platform": "win-64",
        "installer_sha256": installer_sha256,
        "release_index_sha256": _INDEX_SHA256,
    }
    assert observed["notices"] == [
        "COPYING-AGPL-3.0.txt",
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "bootstrap/NOTICE.json",
        "bootstrap/notices/nested/second.txt",
        "bootstrap/notices/vendor/thirdparty.txt",
    ]
    assert "filevers=(0, 2, 0, 0)" in observed["version_resource"]
    assert "prodvers=(0, 2, 0, 0)" in observed["version_resource"]
    for expected in (
        "CompanyName",
        "Install Carlina Documents",
        "ProductName",
        _VERSION,
    ):
        assert expected in observed["version_resource"]

    for flag in ("--onefile", "--windowed", "--noupx", "--clean", "--noconfirm"):
        assert flag in command
    assert _option(command, "--name") == executable.stem
    assert _option(command, "--icon") == "stage/carlina-documents.ico"
    for option in ("--specpath", "--workpath", "--distpath"):
        assert Path(_option(command, option)).parent == observed["cwd"]
    assert "stage/carlina-documents.ico:." in [
        command[index + 1]
        for index, argument in enumerate(command[:-1])
        if argument == "--add-data"
    ]
    assert "stage/installer.ps1:." in [
        command[index + 1]
        for index, argument in enumerate(command[:-1])
        if argument == "--add-data"
    ]
    assert "stage/launcher.json:." in [
        command[index + 1]
        for index, argument in enumerate(command[:-1])
        if argument == "--add-data"
    ]
    assert "stage/licenses:licenses" in [
        command[index + 1]
        for index, argument in enumerate(command[:-1])
        if argument == "--add-data"
    ]
    assert [
        command[index + 1]
        for index, argument in enumerate(command[:-1])
        if argument == "--exclude-module"
    ] == ["carlina", "iarf_assistant"]
    assert "--uac-admin" not in command
    assert "--uac-uiaccess" not in command
    assert "--hidden-import" not in command
    assert "--collect-all" not in command


@pytest.mark.parametrize("output_suffix", ["", ".artifact.json", ".sha256"])
def test_existing_destinations_are_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output_suffix: str
) -> None:
    _allow_windows_builder(monkeypatch)
    root, revision, _epoch = _make_source_checkout(tmp_path)
    installer, installer_sha256 = _write_installer(tmp_path)
    output_dir = tmp_path / "artifacts"
    output_dir.mkdir()
    executable_name = f"Carlina_Documents-{_VERSION}-Setup-x86_64.exe"
    existing = output_dir / f"{executable_name}{output_suffix}"
    existing.write_bytes(b"preserve this collision")
    calls = _fake_freezer(monkeypatch)

    with pytest.raises(
        builder.DocumentsWindowsInstallerBuildError, match="already exists"
    ):
        _build(root, revision, installer, installer_sha256, output_dir)
    assert existing.read_bytes() == b"preserve this collision"
    assert calls == []
    assert sorted(path.name for path in output_dir.iterdir()) == [existing.name]


def test_invalid_pe_is_rejected_without_publishing_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _allow_windows_builder(monkeypatch)
    root, revision, _epoch = _make_source_checkout(tmp_path)
    installer, installer_sha256 = _write_installer(tmp_path)
    calls = _fake_freezer(monkeypatch, payload=b"NO" + b"\0" * 62)
    output_dir = tmp_path / "artifacts"

    with pytest.raises(
        builder.DocumentsWindowsInstallerBuildError, match="Windows executable"
    ):
        _build(root, revision, installer, installer_sha256, output_dir)
    assert len(calls) == 1
    assert list(output_dir.iterdir()) == []


def test_notice_count_limit_is_enforced_before_freezer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _allow_windows_builder(monkeypatch)
    root, revision, _epoch = _make_source_checkout(tmp_path)
    for index in range(builder._MAX_NOTICE_FILES + 1):
        path = root / builder._BOOTSTRAP_NOTICES_DIRECTORY / f"extra-{index:03}.txt"
        path.write_text("Synthetic notice.\n", encoding="utf-8")
    installer, installer_sha256 = _write_installer(tmp_path)
    calls = _fake_freezer(monkeypatch)

    with pytest.raises(builder.DocumentsWindowsInstallerBuildError, match="file-count"):
        _build(root, revision, installer, installer_sha256, tmp_path / "out")
    assert calls == []


def test_notice_byte_limit_is_enforced_before_freezer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _allow_windows_builder(monkeypatch)
    root, revision, _epoch = _make_source_checkout(tmp_path)
    oversized = root / builder._BOOTSTRAP_NOTICES_DIRECTORY / "oversized.txt"
    oversized.write_bytes(b"x" * (builder._MAX_NOTICE_BYTES + 1))
    installer, installer_sha256 = _write_installer(tmp_path)
    calls = _fake_freezer(monkeypatch)

    with pytest.raises(builder.DocumentsWindowsInstallerBuildError, match="byte limit"):
        _build(root, revision, installer, installer_sha256, tmp_path / "out")
    assert calls == []
