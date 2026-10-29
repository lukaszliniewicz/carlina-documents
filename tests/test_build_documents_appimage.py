"""Synthetic acceptance checks for the exact Documents AppImage builder."""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import stat
import subprocess
import sys
import tarfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, Literal, cast

import pytest

from scripts import build_documents_appimage as builder

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or platform.machine().lower() not in {"x86_64", "amd64"},
    reason="The AppImage builder and fake-tool acceptance lane require Linux x86-64.",
)

_VERSION: Final = "1.2.3"
_RELEASE_INDEX_HASH: Final = "a" * 64
_ASSETS: Final = {
    "packaging/appimage/AppRun": b"synthetic opaque AppRun bytes\x00",
    "packaging/appimage/carlina-documents.desktop": b"synthetic opaque desktop bytes\x01",
    "scripts/documents_appimage.py": b"synthetic opaque bootstrap bytes\x02",
    "iarf_assistant/static/favicon.svg": b"synthetic opaque icon bytes\x03",
    "LICENSE": b"synthetic license bytes\x04",
    "COPYING-AGPL-3.0.txt": b"synthetic AGPL text bytes\x05",
    "THIRD_PARTY_NOTICES.md": b"synthetic notice bytes\x06",
    "scripts/build_documents_appimage.py": b"synthetic opaque builder bytes\x07",
    "third_party/appimage/NOTICE.json": (
        b'{"notices":[{"file":"notices/synthetic-license.txt"}]}\n'
    ),
    "third_party/appimage/notices/synthetic-license.txt": (
        b"synthetic opaque bootstrap license bytes\x09"
    ),
}


@dataclass(frozen=True)
class BuildInputs:
    python_archive: Path
    python_sha256: str
    appimagetool: Path
    appimagetool_sha256: str
    runtime: Path
    runtime_sha256: str
    installer: Path
    installer_sha256: str


@dataclass(frozen=True)
class TarEntry:
    name: str
    kind: Literal["directory", "file", "symlink", "hardlink"]
    value: bytes | str = b""
    mode: int = 0o644


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _installer_contents(
    *,
    profile: str = "documents",
    version: str = _VERSION,
    index_hash: str = _RELEASE_INDEX_HASH,
    platform_name: str = "linux-64",
    artifact_kind: str = "pixi-pack-executable",
) -> bytes:
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        f"export CARLINA_PROFILE={profile}\n"
        f"VERSION='{version}'\n"
        f'INDEX_SHA256="{index_hash}"\n'
        f"ARTIFACT_PLATFORM={platform_name}\n"
        f"ARTIFACT_KIND={artifact_kind}\n"
        "# opaque generated installer body\n"
        "exit 0\n"
    ).encode()


def _add_directory(archive: tarfile.TarFile, name: str) -> None:
    member = tarfile.TarInfo(name)
    member.type = tarfile.DIRTYPE
    member.mode = 0o755
    archive.addfile(member)


def _add_file(
    archive: tarfile.TarFile, name: str, contents: bytes, mode: int = 0o644
) -> None:
    member = tarfile.TarInfo(name)
    member.type = tarfile.REGTYPE
    member.mode = mode
    member.size = len(contents)
    archive.addfile(member, fileobj=io.BytesIO(contents))


def _add_extra(archive: tarfile.TarFile, entry: TarEntry) -> None:
    member = tarfile.TarInfo(entry.name)
    member.mode = entry.mode
    if entry.kind == "directory":
        member.type = tarfile.DIRTYPE
        archive.addfile(member)
    elif entry.kind == "file":
        payload = (
            entry.value if isinstance(entry.value, bytes) else entry.value.encode()
        )
        member.type = tarfile.REGTYPE
        member.size = len(payload)
        archive.addfile(member, fileobj=io.BytesIO(payload))
    elif entry.kind == "symlink":
        assert isinstance(entry.value, str)
        member.type = tarfile.SYMTYPE
        member.linkname = entry.value
        archive.addfile(member)
    else:
        assert isinstance(entry.value, str)
        member.type = tarfile.LNKTYPE
        member.linkname = entry.value
        archive.addfile(member)


def _write_python_archive(path: Path, extra: tuple[TarEntry, ...] = ()) -> None:
    with tarfile.open(path, mode="w:gz") as archive:
        _add_directory(archive, "python/")
        _add_directory(archive, "python/bin/")
        _add_file(archive, "python/bin/python3.14", b"synthetic python", 0o755)
        link = tarfile.TarInfo("python/bin/python3")
        link.type = tarfile.SYMTYPE
        link.linkname = "python3.14"
        archive.addfile(link)
        _add_directory(archive, "python/lib/")
        _add_directory(archive, "python/lib/python3.14/")
        _add_directory(archive, "python/lib/python3.14/encodings/")
        _add_file(
            archive,
            "python/lib/python3.14/encodings/__init__.py",
            b"synthetic standard library marker",
        )
        for entry in extra:
            _add_extra(archive, entry)


def _make_source_root(path: Path) -> tuple[Path, str]:
    root = path / "source"
    for relative, contents in _ASSETS.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "carlina"\nversion = "{_VERSION}"\n',
        encoding="utf-8",
    )
    (root / "pixi.toml").write_text(
        f'[workspace]\nname = "documents-test"\nversion = "{_VERSION}"\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "--quiet", os.fspath(root)], check=True)
    subprocess.run(["git", "-C", os.fspath(root), "add", "--all"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            os.fspath(root),
            "-c",
            "user.name=AppImage Test",
            "-c",
            "user.email=appimage-test@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "synthetic release source",
        ],
        check=True,
    )
    revision = subprocess.run(
        ["git", "-C", os.fspath(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return root, revision


def _fake_appimagetool_source() -> bytes:
    return b"""#!/usr/bin/env python3
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

arguments = sys.argv[1:]
stage = Path(arguments[-2])
output = Path(arguments[-1])
fixed = [
    "AppRun", "carlina-documents.desktop", "bootstrap.py",
    "carlina-documents.svg", ".DirIcon", "licenses/LICENSE",
    "licenses/COPYING-AGPL-3.0.txt", "licenses/THIRD_PARTY_NOTICES.md",
    "licenses/bootstrap/NOTICE.json",
    "licenses/bootstrap/notices/synthetic-license.txt",
    "installer.sh",
]
record = {
    "arguments": arguments,
    "environment": {
        name: os.environ.get(name)
        for name in ("ARCH", "SOURCE_DATE_EPOCH", "APPIMAGE_EXTRACT_AND_RUN")
    },
    "root_members": sorted(child.name for child in stage.iterdir()),
    "fixed_hashes": {
        name: hashlib.sha256((stage / name).read_bytes()).hexdigest()
        for name in fixed
    },
    "bootstrap_notice_bytes": {
        name: (stage / name).read_bytes().hex()
        for name in (
            "licenses/bootstrap/NOTICE.json",
            "licenses/bootstrap/notices/synthetic-license.txt",
        )
    },
    "catalogue_notice_bytes": {
        notice["file"]: (
            stage / "licenses" / "bootstrap" / notice["file"]
        ).read_bytes().hex()
        for notice in json.loads(
            (stage / "licenses/bootstrap/NOTICE.json").read_text(encoding="utf-8")
        )["notices"]
    },
    "flattened_notice_exists": (
        stage / "licenses/bootstrap/synthetic-license.txt"
    ).exists(),
    "fixed_modes": {
        name: stat.S_IMODE((stage / name).stat().st_mode)
        for name in (
            "AppRun",
            "installer.sh",
            "licenses/bootstrap/NOTICE.json",
            "licenses/bootstrap/notices/synthetic-license.txt",
        )
    },
    "launcher": json.loads((stage / "launcher.json").read_text(encoding="utf-8")),
    "python_link": os.readlink(stage / "python/bin/python3"),
}
with open(os.environ["CARLINA_TEST_TRACE"], "a", encoding="utf-8") as trace:
    trace.write(json.dumps(record) + "\\n")

if os.environ.get("CARLINA_TEST_FAIL") == "1":
    print("private fake builder diagnostic", file=sys.stderr)
    raise SystemExit(37)

if os.environ.get("CARLINA_TEST_BAD_ELF") == "1":
    payload = b"not an AppImage"
else:
    payload = b"\\x7fELF" + bytes((2, 1, 1, 0)) + b"AI\\x02" + b"synthetic image"
output.write_bytes(payload)
output.chmod(0o755)
"""


def _make_inputs(path: Path) -> BuildInputs:
    path.mkdir(parents=True, exist_ok=True)
    archive = path / "python-build-standalone.tar.gz"
    _write_python_archive(archive)
    appimagetool = path / "appimagetool"
    appimagetool.write_bytes(_fake_appimagetool_source())
    appimagetool.chmod(0o755)
    runtime = path / "runtime"
    runtime.write_bytes(b"synthetic pinned type-2 runtime")
    installer = path / "installer.sh"
    installer.write_bytes(_installer_contents())
    return BuildInputs(
        python_archive=archive,
        python_sha256=_sha256(archive),
        appimagetool=appimagetool,
        appimagetool_sha256=_sha256(appimagetool),
        runtime=runtime,
        runtime_sha256=_sha256(runtime),
        installer=installer,
        installer_sha256=_sha256(installer),
    )


def _invoke(
    root: Path,
    output_dir: Path,
    revision: str,
    inputs: BuildInputs,
    *,
    version: str = _VERSION,
    release_index_sha256: str = _RELEASE_INDEX_HASH,
) -> dict[str, object]:
    return builder.build_appimage(
        root=root,
        output_dir=output_dir,
        version=version,
        source_revision=revision,
        python_archive=inputs.python_archive,
        python_sha256=inputs.python_sha256,
        appimagetool=inputs.appimagetool,
        appimagetool_sha256=inputs.appimagetool_sha256,
        runtime=inputs.runtime,
        runtime_sha256=inputs.runtime_sha256,
        installer=inputs.installer,
        installer_sha256=inputs.installer_sha256,
        release_index_sha256=release_index_sha256,
    )


def _trace_records(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _assert_no_stage(output_dir: Path) -> None:
    assert not list(output_dir.glob(".carlina-documents-appimage-*"))


@pytest.fixture
def build_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, str, BuildInputs, Path]:
    root, revision = _make_source_root(tmp_path)
    inputs = _make_inputs(tmp_path / "inputs")
    trace = tmp_path / "tool-trace.jsonl"
    monkeypatch.setenv("CARLINA_TEST_TRACE", os.fspath(trace))
    return root, revision, inputs, trace


@pytest.mark.parametrize(
    "artifact_kind",
    ["pixi-pack-executable", "publisher-bootstrap-executable"],
)
def test_builds_exact_provenance_bound_appimage(
    tmp_path: Path,
    build_environment: tuple[Path, str, BuildInputs, Path],
    artifact_kind: str,
) -> None:
    root, revision, inputs, trace_path = build_environment
    inputs.installer.write_bytes(_installer_contents(artifact_kind=artifact_kind))
    inputs = replace(inputs, installer_sha256=_sha256(inputs.installer))
    output_dir = tmp_path / "output"

    metadata = _invoke(root, output_dir, revision, inputs)

    artifact = output_dir / f"Carlina_Documents-{_VERSION}-x86_64.AppImage"
    sidecar = Path(os.fspath(artifact) + ".artifact.json")
    assert artifact.is_file()
    assert stat.S_IMODE(artifact.stat().st_mode) & 0o111
    assert artifact.read_bytes()[8:11] == b"AI\x02"
    assert sidecar.is_file()
    assert json.loads(sidecar.read_text(encoding="utf-8")) == metadata
    assert metadata["sha256"] == _sha256(artifact)
    assert metadata["bytes"] == artifact.stat().st_size
    assert metadata["version"] == _VERSION
    assert metadata["profile"] == "documents"
    assert metadata["source_revision"] == revision
    assert metadata["kind"] == "appimage"
    assert metadata["input_hashes"] == {
        "python_archive": inputs.python_sha256,
        "appimagetool": inputs.appimagetool_sha256,
        "runtime": inputs.runtime_sha256,
        "installer": inputs.installer_sha256,
        "release_index": _RELEASE_INDEX_HASH,
    }

    epoch = int(
        subprocess.run(
            ["git", "-C", os.fspath(root), "show", "-s", "--format=%ct", revision],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    assert metadata["source_date_epoch"] == epoch

    records = _trace_records(trace_path)
    assert len(records) == 1
    record = records[0]
    arguments = cast(list[str], record["arguments"])
    assert arguments[:4] == [
        "--appimage-extract-and-run",
        "--no-appstream",
        "--comp",
        "zstd",
    ]
    assert arguments[4:6] == ["--runtime-file", os.fspath(inputs.runtime)]
    assert Path(arguments[-2]).name == "stage"
    assert Path(arguments[-1]).name == "candidate.AppImage"
    assert record["environment"] == {
        "ARCH": "x86_64",
        "SOURCE_DATE_EPOCH": str(epoch),
        "APPIMAGE_EXTRACT_AND_RUN": "1",
    }
    assert record["root_members"] == sorted(
        [
            ".DirIcon",
            "AppRun",
            "bootstrap.py",
            "carlina-documents.desktop",
            "carlina-documents.svg",
            "installer.sh",
            "launcher.json",
            "licenses",
            "python",
        ]
    )
    assert record["fixed_hashes"] == {
        "AppRun": hashlib.sha256(_ASSETS["packaging/appimage/AppRun"]).hexdigest(),
        "carlina-documents.desktop": hashlib.sha256(
            _ASSETS["packaging/appimage/carlina-documents.desktop"]
        ).hexdigest(),
        "bootstrap.py": hashlib.sha256(
            _ASSETS["scripts/documents_appimage.py"]
        ).hexdigest(),
        "carlina-documents.svg": hashlib.sha256(
            _ASSETS["iarf_assistant/static/favicon.svg"]
        ).hexdigest(),
        ".DirIcon": hashlib.sha256(
            _ASSETS["iarf_assistant/static/favicon.svg"]
        ).hexdigest(),
        "licenses/LICENSE": hashlib.sha256(_ASSETS["LICENSE"]).hexdigest(),
        "licenses/COPYING-AGPL-3.0.txt": hashlib.sha256(
            _ASSETS["COPYING-AGPL-3.0.txt"]
        ).hexdigest(),
        "licenses/THIRD_PARTY_NOTICES.md": hashlib.sha256(
            _ASSETS["THIRD_PARTY_NOTICES.md"]
        ).hexdigest(),
        "licenses/bootstrap/NOTICE.json": hashlib.sha256(
            _ASSETS["third_party/appimage/NOTICE.json"]
        ).hexdigest(),
        "licenses/bootstrap/notices/synthetic-license.txt": hashlib.sha256(
            _ASSETS["third_party/appimage/notices/synthetic-license.txt"]
        ).hexdigest(),
        "installer.sh": inputs.installer_sha256,
    }
    assert record["bootstrap_notice_bytes"] == {
        "licenses/bootstrap/NOTICE.json": _ASSETS[
            "third_party/appimage/NOTICE.json"
        ].hex(),
        "licenses/bootstrap/notices/synthetic-license.txt": _ASSETS[
            "third_party/appimage/notices/synthetic-license.txt"
        ].hex(),
    }
    assert record["catalogue_notice_bytes"] == {
        "notices/synthetic-license.txt": _ASSETS[
            "third_party/appimage/notices/synthetic-license.txt"
        ].hex()
    }
    assert record["flattened_notice_exists"] is False
    assert record["fixed_modes"] == {
        "AppRun": 0o755,
        "installer.sh": 0o700,
        "licenses/bootstrap/NOTICE.json": 0o644,
        "licenses/bootstrap/notices/synthetic-license.txt": 0o644,
    }
    assert record["python_link"] == "python3.14"
    launcher = record["launcher"]
    assert launcher == {
        "schema_version": 1,
        "product": "carlina-documents",
        "version": _VERSION,
        "source_revision": revision,
        "installer_sha256": inputs.installer_sha256,
        "release_index_sha256": _RELEASE_INDEX_HASH,
        "bootstrap_python_sha256": inputs.python_sha256,
    }
    _assert_no_stage(output_dir)


@pytest.mark.parametrize(
    "field",
    [
        "python_sha256",
        "appimagetool_sha256",
        "runtime_sha256",
        "installer_sha256",
    ],
)
def test_hash_mismatch_fails_before_staging_or_subprocess(
    tmp_path: Path,
    build_environment: tuple[Path, str, BuildInputs, Path],
    field: str,
) -> None:
    root, revision, inputs, trace_path = build_environment
    bad_inputs = replace(inputs, **{field: "0" * 64})
    output_dir = tmp_path / f"output-{field}"

    with pytest.raises(builder.DocumentsAppImageBuildError, match="SHA-256"):
        _invoke(root, output_dir, revision, bad_inputs)

    assert not output_dir.exists()
    assert not trace_path.exists()


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("profile", "workbench"),
        ("version", "9.9.9"),
        ("index_hash", "b" * 64),
        ("platform_name", "win-64"),
        ("artifact_kind", "standard-bundle"),
        pytest.param(
            "artifact_kind",
            "publisher-bootstrap-bundle",
            id="publisher-bootstrap-bundle",
        ),
        pytest.param("artifact_kind", "unknown-executable", id="unknown-kind"),
    ],
)
def test_rejects_wrong_generated_installer_identity_before_staging_or_tool(
    tmp_path: Path,
    build_environment: tuple[Path, str, BuildInputs, Path],
    field: Literal[
        "profile", "version", "index_hash", "platform_name", "artifact_kind"
    ],
    wrong_value: str,
) -> None:
    root, revision, inputs, trace_path = build_environment
    values = {
        "profile": "documents",
        "version": _VERSION,
        "index_hash": _RELEASE_INDEX_HASH,
        "platform_name": "linux-64",
        "artifact_kind": "pixi-pack-executable",
    }
    values[field] = wrong_value
    inputs.installer.write_bytes(
        _installer_contents(
            profile=values["profile"],
            version=values["version"],
            index_hash=values["index_hash"],
            platform_name=values["platform_name"],
            artifact_kind=values["artifact_kind"],
        )
    )
    wrong_inputs = replace(inputs, installer_sha256=_sha256(inputs.installer))
    output_dir = tmp_path / f"wrong-installer-{field}"

    with pytest.raises(builder.DocumentsAppImageBuildError, match="installer metadata"):
        _invoke(root, output_dir, revision, wrong_inputs)

    assert not trace_path.exists()
    assert not output_dir.exists()


@pytest.mark.parametrize("malformation", ["duplicate", "unclosed-quote"])
def test_rejects_malformed_or_duplicate_installer_metadata(
    tmp_path: Path,
    build_environment: tuple[Path, str, BuildInputs, Path],
    malformation: Literal["duplicate", "unclosed-quote"],
) -> None:
    root, revision, inputs, trace_path = build_environment
    contents = _installer_contents()
    if malformation == "duplicate":
        contents = contents.replace(
            b"INDEX_SHA256=",
            b"VERSION='1.2.3'\nINDEX_SHA256=",
            1,
        )
    else:
        contents = contents.replace(
            b"VERSION='1.2.3'",
            b"VERSION='1.2.3",
            1,
        )
    inputs.installer.write_bytes(contents)
    bad_inputs = replace(inputs, installer_sha256=_sha256(inputs.installer))
    output_dir = tmp_path / f"malformed-installer-{malformation}"

    with pytest.raises(builder.DocumentsAppImageBuildError, match="installer metadata"):
        _invoke(root, output_dir, revision, bad_inputs)

    assert not trace_path.exists()
    assert not output_dir.exists()


def test_accepts_uppercase_hex_hashes_and_records_canonical_hashes(
    tmp_path: Path,
    build_environment: tuple[Path, str, BuildInputs, Path],
) -> None:
    root, revision, inputs, _trace_path = build_environment
    uppercase_inputs = replace(
        inputs,
        python_sha256=inputs.python_sha256.upper(),
        appimagetool_sha256=inputs.appimagetool_sha256.upper(),
        runtime_sha256=inputs.runtime_sha256.upper(),
        installer_sha256=inputs.installer_sha256.upper(),
    )

    metadata = _invoke(
        root,
        tmp_path / "uppercase-hashes",
        revision,
        uppercase_inputs,
        release_index_sha256=_RELEASE_INDEX_HASH.upper(),
    )

    assert metadata["input_hashes"] == {
        "python_archive": inputs.python_sha256,
        "appimagetool": inputs.appimagetool_sha256,
        "runtime": inputs.runtime_sha256,
        "installer": inputs.installer_sha256,
        "release_index": _RELEASE_INDEX_HASH,
    }


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param(
            TarEntry("python/../../outside", "file", b"escape"), id="traversal"
        ),
        pytest.param(TarEntry("/python/outside", "file", b"absolute"), id="absolute"),
        pytest.param(TarEntry("other/file", "file", b"wrong-root"), id="wrong-root"),
        pytest.param(
            TarEntry("python/bin/escape", "symlink", "../../../../etc/passwd"),
            id="escaping-symlink",
        ),
        pytest.param(
            TarEntry("python/bin/hard", "hardlink", "python/bin/python3.14"),
            id="hardlink",
        ),
        pytest.param(
            TarEntry("python/bin/privileged", "file", b"x", 0o4755),
            id="setuid",
        ),
        pytest.param(
            TarEntry("python/bin/python3.14", "file", b"duplicate"),
            id="duplicate-path",
        ),
    ],
)
def test_rejects_unsafe_or_duplicate_archive_members(
    tmp_path: Path,
    build_environment: tuple[Path, str, BuildInputs, Path],
    extra: TarEntry,
) -> None:
    root, revision, inputs, trace_path = build_environment
    archive = inputs.python_archive
    _write_python_archive(archive, (extra,))
    unsafe_inputs = replace(inputs, python_sha256=_sha256(archive))
    output_dir = tmp_path / "unsafe-output"

    with pytest.raises(builder.DocumentsAppImageBuildError):
        _invoke(root, output_dir, revision, unsafe_inputs)

    assert not trace_path.exists()
    assert not list(output_dir.glob("Carlina_Documents-*"))
    _assert_no_stage(output_dir)


def test_rejects_writes_through_archive_symlinks(
    tmp_path: Path,
    build_environment: tuple[Path, str, BuildInputs, Path],
) -> None:
    root, revision, inputs, trace_path = build_environment
    archive = inputs.python_archive
    _write_python_archive(
        archive,
        (
            TarEntry("python/bin/link", "symlink", "python3.14"),
            TarEntry("python/bin/link/payload", "file", b"write through link"),
        ),
    )
    unsafe_inputs = replace(inputs, python_sha256=_sha256(archive))
    output_dir = tmp_path / "symlink-output"

    with pytest.raises(builder.DocumentsAppImageBuildError, match="symbolic link"):
        _invoke(root, output_dir, revision, unsafe_inputs)

    assert not trace_path.exists()
    _assert_no_stage(output_dir)


def test_rejects_noncanonical_versions_and_dirty_tracked_source(
    tmp_path: Path,
    build_environment: tuple[Path, str, BuildInputs, Path],
) -> None:
    root, revision, inputs, trace_path = build_environment
    with pytest.raises(builder.DocumentsAppImageBuildError, match="canonical"):
        _invoke(root, tmp_path / "noncanonical", revision, inputs, version="v1.2.3")
    assert not trace_path.exists()

    (root / "LICENSE").write_bytes(b"tracked modification")
    output_dir = tmp_path / "dirty-source"
    with pytest.raises(builder.DocumentsAppImageBuildError, match="tracked changes"):
        _invoke(root, output_dir, revision, inputs)
    assert not trace_path.exists()
    assert not output_dir.exists()


@pytest.mark.parametrize("drift", ["bytes", "mode"])
def test_rejects_payload_drift_even_when_git_status_is_hidden(
    tmp_path: Path,
    build_environment: tuple[Path, str, BuildInputs, Path],
    drift: Literal["bytes", "mode"],
) -> None:
    root, revision, inputs, trace_path = build_environment
    relative = "scripts/build_documents_appimage.py"
    source = root / relative
    subprocess.run(
        ["git", "-C", os.fspath(root), "update-index", "--assume-unchanged", relative],
        check=True,
    )
    if drift == "bytes":
        source.write_bytes(source.read_bytes() + b"uncommitted change")
    else:
        source.chmod(source.stat().st_mode | stat.S_IXUSR)

    assert not subprocess.run(
        ["git", "-C", os.fspath(root), "status", "--porcelain", "-uno"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    output_dir = tmp_path / f"payload-drift-{drift}"
    with pytest.raises(builder.DocumentsAppImageBuildError, match="requested revision"):
        _invoke(root, output_dir, revision, inputs)

    assert not trace_path.exists()
    assert not output_dir.exists()


def test_rejects_existing_untracked_ui_payload_as_unbound_to_source_revision(
    tmp_path: Path,
    build_environment: tuple[Path, str, BuildInputs, Path],
) -> None:
    root, _old_revision, inputs, trace_path = build_environment
    relative = "scripts/documents_appimage.py"
    subprocess.run(
        ["git", "-C", os.fspath(root), "rm", "--cached", "--quiet", relative],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            os.fspath(root),
            "-c",
            "user.name=AppImage Test",
            "-c",
            "user.email=appimage-test@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "remove UI payload from source revision",
        ],
        check=True,
    )
    revision = subprocess.run(
        ["git", "-C", os.fspath(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert (root / relative).is_file()
    assert not subprocess.run(
        ["git", "-C", os.fspath(root), "status", "--porcelain", "-uno"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    output_dir = tmp_path / "untracked-ui"
    with pytest.raises(builder.DocumentsAppImageBuildError, match="absent from"):
        _invoke(root, output_dir, revision, inputs)

    assert not trace_path.exists()
    assert not output_dir.exists()


def test_rejects_missing_bootstrap_notice_before_tool_or_output(
    tmp_path: Path,
    build_environment: tuple[Path, str, BuildInputs, Path],
) -> None:
    root, revision, inputs, trace_path = build_environment
    (root / "third_party/appimage/NOTICE.json").unlink()
    output_dir = tmp_path / "missing-bootstrap-notice"

    with pytest.raises(builder.DocumentsAppImageBuildError):
        _invoke(root, output_dir, revision, inputs)

    assert not trace_path.exists()
    assert not output_dir.exists()


@pytest.mark.parametrize(
    "relative",
    [
        "third_party/appimage/NOTICE.json",
        "third_party/appimage/notices/synthetic-license.txt",
    ],
)
def test_rejects_untracked_bootstrap_notice_before_tool_or_output(
    tmp_path: Path,
    build_environment: tuple[Path, str, BuildInputs, Path],
    relative: str,
) -> None:
    root, _old_revision, inputs, trace_path = build_environment
    subprocess.run(
        ["git", "-C", os.fspath(root), "rm", "--cached", "--quiet", relative],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            os.fspath(root),
            "-c",
            "user.name=AppImage Test",
            "-c",
            "user.email=appimage-test@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "remove bootstrap notice from source revision",
        ],
        check=True,
    )
    revision = subprocess.run(
        ["git", "-C", os.fspath(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert (root / relative).is_file()
    assert not subprocess.run(
        ["git", "-C", os.fspath(root), "status", "--porcelain", "-uno"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    output_dir = tmp_path / "untracked-bootstrap-notice"

    with pytest.raises(builder.DocumentsAppImageBuildError, match="absent from"):
        _invoke(root, output_dir, revision, inputs)

    assert not trace_path.exists()
    assert not output_dir.exists()


def test_rejects_symlink_bootstrap_notice_before_tool_or_output(
    tmp_path: Path,
    build_environment: tuple[Path, str, BuildInputs, Path],
) -> None:
    root, revision, inputs, trace_path = build_environment
    notice = root / "third_party/appimage/notices/synthetic-license.txt"
    notice.unlink()
    notice.symlink_to("../../../LICENSE")
    output_dir = tmp_path / "symlink-bootstrap-notice"

    with pytest.raises(builder.DocumentsAppImageBuildError, match="regular file"):
        _invoke(root, output_dir, revision, inputs)

    assert not trace_path.exists()
    assert not output_dir.exists()


@pytest.mark.parametrize("entry_kind", ["nontext", "nested"])
def test_rejects_non_txt_and_nested_bootstrap_notice_entries(
    tmp_path: Path,
    build_environment: tuple[Path, str, BuildInputs, Path],
    entry_kind: Literal["nontext", "nested"],
) -> None:
    root, revision, inputs, trace_path = build_environment
    notices = root / "third_party/appimage/notices"
    if entry_kind == "nontext":
        (notices / "unexpected.md").write_bytes(b"synthetic opaque entry")
    else:
        nested = notices / "nested"
        nested.mkdir()
        (nested / "license.txt").write_bytes(b"synthetic opaque nested entry")
    output_dir = tmp_path / f"invalid-notices-{entry_kind}"

    with pytest.raises(builder.DocumentsAppImageBuildError):
        _invoke(root, output_dir, revision, inputs)

    assert not trace_path.exists()
    assert not output_dir.exists()


def test_rejects_non_appimage_output_and_cleans_stage(
    tmp_path: Path,
    build_environment: tuple[Path, str, BuildInputs, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, revision, inputs, trace_path = build_environment
    monkeypatch.setenv("CARLINA_TEST_BAD_ELF", "1")
    output_dir = tmp_path / "bad-elf"

    with pytest.raises(builder.DocumentsAppImageBuildError, match="type-2 AppImage"):
        _invoke(root, output_dir, revision, inputs)

    assert len(_trace_records(trace_path)) == 1
    assert not list(output_dir.glob("Carlina_Documents-*"))
    _assert_no_stage(output_dir)


def test_subprocess_failure_uses_fixed_error_and_cleans_stage(
    tmp_path: Path,
    build_environment: tuple[Path, str, BuildInputs, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, revision, inputs, trace_path = build_environment
    monkeypatch.setenv("CARLINA_TEST_FAIL", "1")
    output_dir = tmp_path / "tool-failure"

    with pytest.raises(builder.DocumentsAppImageBuildError) as error:
        _invoke(root, output_dir, revision, inputs)

    assert "private fake builder diagnostic" not in str(error.value)
    assert len(_trace_records(trace_path)) == 1
    assert not list(output_dir.glob("Carlina_Documents-*"))
    _assert_no_stage(output_dir)


@pytest.mark.parametrize("existing", ["artifact", "sidecar"])
def test_existing_output_is_never_overwritten(
    tmp_path: Path,
    build_environment: tuple[Path, str, BuildInputs, Path],
    existing: Literal["artifact", "sidecar"],
) -> None:
    root, revision, inputs, trace_path = build_environment
    output_dir = tmp_path / "existing"
    output_dir.mkdir()
    artifact = output_dir / f"Carlina_Documents-{_VERSION}-x86_64.AppImage"
    occupied = (
        artifact
        if existing == "artifact"
        else Path(os.fspath(artifact) + ".artifact.json")
    )
    occupied.write_bytes(b"keep this exact file")

    with pytest.raises(builder.DocumentsAppImageBuildError, match="already exists"):
        _invoke(root, output_dir, revision, inputs)

    assert occupied.read_bytes() == b"keep this exact file"
    assert not trace_path.exists()
    _assert_no_stage(output_dir)
