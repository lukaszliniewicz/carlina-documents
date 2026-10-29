"""Exact-staging tests for the isolated Documents package builder."""

from __future__ import annotations

import glob
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath
from typing import Final, TypedDict

import pytest

from scripts import build_documents_release as release
from scripts.documents_source_inventory import documents_backend_members

_PROJECT_METADATA: Final = b"""[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"

[project]
name = "carlina"
version = "1.2.3"

[tool.setuptools.package-data]
iarf_assistant = ["templates/**"]
"""

_EXPECTED_DOCUMENTS_RELEASE_CONTROLS: Final = (
    "scripts/build_documents_release.py",
    "scripts/build_documents_appimage.py",
    "scripts/documents_appimage.py",
    "scripts/documents_source_inventory.py",
    "scripts/check_documents_closure.py",
    "scripts/check_markdown_links.py",
    "scripts/measure_documents_closure.py",
    "scripts/addon_publication_policy.py",
    "scripts/build_managed_pack.py",
    "scripts/prepare_publisher_bootstrap.py",
    "scripts/build_publisher_bootstrap.py",
    "scripts/build_release_index.py",
    "scripts/render_managed_installers.py",
    "scripts/managed_pixi_smoke.py",
    "scripts/verify_release_artifacts.py",
    "scripts/build_clean_release.py",
    "scripts/package_application_source.py",
    "scripts/package_application_companions.py",
)

_OPAQUE_FILES: Final = {
    "frontend/apps/core/src/home.svelte": b"synthetic opaque frontend bytes\x00\x01",
    "frontend/packages/sdk/src/contracts.ts": b"synthetic package bytes\x02",
    "frontend/contracts/bootstrap.json": b"synthetic contract bytes\x03",
    "iarf_assistant/static/dist/app.js": b"synthetic static bytes\x04",
    "iarf_assistant/templates/documents.html": b"synthetic template bytes\x05",
    "iarf_assistant/addons/doc_processor/static/dist/worker.js": b"synthetic addon bytes\x06",
    "iarf_assistant/addons/translator/frontend/src/app.ts": b"synthetic addon frontend bytes\x07",
    "docs/documents-application.md": b"[Recovery contract](update-and-recovery.md#native-ready-generation-reruns)\n",
    "docs/epub-export.md": b"synthetic EPUB publishing contract\n",
    "docs/addon-api-v1.md": b"synthetic addon API contract\n",
    "docs/architecture.md": b"synthetic shared architecture\n",
    "docs/engineering-policy.md": b"synthetic engineering policy\n",
    "docs/extraction-efficiency.md": b"synthetic extraction contract\n",
    "docs/mcp-integration-architecture.md": b"synthetic MCP contract\n",
    "docs/translator.md": b"synthetic translator contract\n",
    "docs/update-and-recovery.md": b"[Architecture](architecture.md)\n",
    "docs/adr/0011-root-bound-ordinary-content-policy.md": b"synthetic approved ADR\n",
    "docs/adr/0012-documents-agpl-distribution.md": b"synthetic distribution ADR\n",
    "COPYING-AGPL-3.0.txt": b"synthetic dependency license\n",
    "third_party/frontend/NOTICE.json": b'{"synthetic":true}\n',
    "third_party/frontend/notices/synthetic--1.0--LICENSE.txt": b"synthetic upstream notice\n",
    "third_party/models/NOTICE.json": b'{"synthetic":true}\n',
    "third_party/appimage/NOTICE.json": b'{"synthetic":true}\n',
    "third_party/appimage/notices/synthetic-license.txt": b"synthetic original notice\n",
    "packaging/appimage/AppRun": b"synthetic opaque AppRun\n",
    "packaging/appimage/carlina-documents.desktop": b"synthetic opaque desktop entry\n",
    "scripts/build_documents_appimage.py": b"synthetic AppImage release control\n",
    "scripts/documents_appimage.py": b"synthetic opaque setup adapter\n",
    "third_party/models/segment-any-text-sat-12l-sm.md": b"synthetic SaT model card\n",
    "third_party/models/FacebookAI-xlm-roberta-base.md": b"synthetic XLM-R model card\n",
    "policy/release-exceptions.json": b'{"schema_version":1,"exceptions":[]}\n',
    "policy/documents-closure.json": b'{"synthetic":"exported-budget"}\n',
    "eslint.config.mjs": b"synthetic root config\n",
    "frontend/tsconfig.base.json": b"synthetic TS config\n",
    "scripts/run_frontend_workspaces.mjs": b"synthetic workspace runner\n",
    "scripts/build_tailwind.py": b"synthetic pinned core CSS builder\n",
    "scripts/documents_dependency_inventory.py": b"synthetic lock inventory reader\n",
    "scripts/generate_documents_inventory.py": b"synthetic release inventory generator\n",
    "scripts/documents_conda_notices.py": b"synthetic bounded Conda notice reader\n",
    "scripts/collect_documents_notices.py": b"synthetic exact archive notice collector\n",
    "scripts/assemble_documents_notices.py": b"synthetic portable notice assembler\n",
    "scripts/assemble_documents_sources.py": b"synthetic source companion assembler\n",
    "scripts/package_application_source.py": b"synthetic public source attachment tool\n",
    "scripts/package_application_companions.py": b"synthetic metadata companion wheel tool\n",
    "scripts/build_clean_release.py": b"synthetic wheel normalization tool\n",
    "scripts/build_documents_release.py": b"synthetic Documents release builder\n",
    "scripts/documents_source_inventory.py": b"synthetic Documents source selector\n",
    "scripts/measure_documents_closure.py": b"synthetic closure collector\n",
    "scripts/check_markdown_links.py": b"synthetic source link checker\n",
    "scripts/check_documents_closure.py": (
        b"import sys\nfrom pathlib import Path\n"
        b"stage = Path(__file__).resolve().parents[1]\n"
        b"assert stage.name == 'stage'\n"
        b"assert (stage / 'policy/documents-closure.json').read_bytes() == "
        b'b\'{"synthetic":"exported-budget"}\\n\'\n'
        b"assert Path(sys.argv[sys.argv.index('--root') + 1]).name == 'export'\n"
        b"assert sys.argv[sys.argv.index('--platform') + 1] in {'linux-64', 'win-64'}\n"
        b"assert Path(sys.argv[sys.argv.index('--wheel') + 1]).is_file()\n"
    ),
    "scripts/addon_publication_policy.py": b"synthetic add-on publication policy\n",
    "scripts/build_managed_pack.py": b"synthetic managed pack builder\n",
    "scripts/prepare_publisher_bootstrap.py": b"synthetic publisher bootstrap preparation\n",
    "scripts/build_publisher_bootstrap.py": b"synthetic publisher bootstrap renderer\n",
    "scripts/build_release_index.py": b"synthetic release index builder\n",
    "scripts/render_managed_installers.py": b"synthetic installer renderer\n",
    "scripts/managed_pixi_smoke.py": b"synthetic managed Pixi smoke control\n",
    "scripts/verify_release_artifacts.py": b"synthetic release artifact verifier\n",
}

_ORIGINAL_CORE_FRONTEND_FILES: Final = {
    "iarf_assistant/static/frontend/legacy-core.js": b"synthetic old core asset\x08",
    "iarf_assistant/static/frontend/.frontend-build-stamp.json": b'{"synthetic":"old"}\n',
}
_GENERATED_CORE_FRONTEND_FILES: Final = {
    "iarf_assistant/static/frontend/index.html": b"synthetic generated core entry\x09",
    "iarf_assistant/static/frontend/assets/app.js": b"synthetic generated core bundle\x0a",
    "iarf_assistant/static/frontend/.frontend-build-stamp.json": b'{"synthetic":"new"}\n',
}
_UNEXPECTED_GENERATED_SIBLING: Final = "iarf_assistant/static/unexpected-generated.js"
_SYNTHETIC_NODE_MODULE: Final = (
    "iarf_assistant/static/frontend/node_modules/synthetic-package/index.js"
)


def test_frontend_notices_keep_original_bytes_and_locked_package_pins() -> None:
    root = Path(__file__).resolve().parents[1]
    notice_root = root / "third_party/frontend"
    catalogue = json.loads((notice_root / "NOTICE.json").read_bytes())
    lock_bytes = (root / "package-lock.json").read_bytes()
    lock = json.loads(lock_bytes)
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = metadata["project"]["license-files"]
    covered = {
        p.relative_to(root).as_posix()
        for pattern in patterns
        for p in root.glob(pattern)
    }

    assert catalogue["format"] == "carlina.frontend-original-notices"
    assert catalogue["schema_version"] == 1
    assert catalogue["package_lock_sha256"] == hashlib.sha256(lock_bytes).hexdigest()
    assert "third_party/frontend/NOTICE.json" in covered
    identities = set()
    notice_files = set()
    for component in catalogue["components"]:
        identity = (component["name"], component["version"])
        assert identity not in identities
        identities.add(identity)
        origin = component["origin"]
        if origin["kind"] == "npm_archive":
            pinned = lock["packages"]["node_modules/" + component["name"]]
            assert pinned["version"] == component["version"]
            assert pinned["resolved"] == origin["url"]
            assert pinned["integrity"] == origin["integrity"]
        else:
            assert origin["kind"] in {"upstream_tag", "upstream_commit"}
            assert origin["url"].startswith("https://")
        assert component["notices"]
        for notice in component["notices"]:
            path = PurePosixPath(notice["path"])
            assert len(path.parts) == 2 and path.parts[0] == "notices"
            assert ".." not in path.parts
            actual = notice_root.joinpath(*path.parts)
            assert actual.is_file() and not actual.is_symlink()
            payload = actual.read_bytes()
            assert len(payload) == notice["bytes"]
            assert hashlib.sha256(payload).hexdigest() == notice["sha256"]
            assert actual.relative_to(root).as_posix() in covered
            notice_files.add(path.as_posix())
    assert notice_files == {
        f"notices/{p.name}" for p in (notice_root / "notices").iterdir()
    }


class _BuildInvocation(TypedDict):
    command: tuple[str, ...]
    environment: dict[str, str]
    staged_files: frozenset[str]
    staged_payloads: dict[str, bytes]
    manifest_members: frozenset[str]
    manifest_lines: frozenset[str]


class _CoreRebuildInvocation(TypedDict):
    source_date_epoch: int
    descriptor: dict[str, object]
    generated_members: frozenset[str]
    unexpected_sibling: str
    node_modules_created: bool
    node_modules_removed: bool


def _run_git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    return result.stdout.strip()


def _write(root: Path, relative: str, payload: bytes) -> None:
    target = root.joinpath(*PurePosixPath(relative).parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)


def _make_git_project(root: Path) -> tuple[str, int]:
    root.mkdir(parents=True)
    _run_git(root, "init", "-q")
    _run_git(root, "config", "user.name", "Documents Builder Test")
    _run_git(root, "config", "user.email", "documents-builder@example.invalid")
    files: dict[str, bytes] = {
        "LICENSE": b"synthetic license\n",
        "THIRD_PARTY_NOTICES.md": b"synthetic notices\n",
        "README.md": b"synthetic project readme\n",
        "MANIFEST.in": b"recursive-include . *\n",
        "pyproject.toml": _PROJECT_METADATA,
        "pixi.toml": b"synthetic pixi metadata\n",
        "pixi.lock": b"synthetic pixi lock\n",
        "package.json": b"synthetic package json\n",
        "package-lock.json": b"synthetic package lock\n",
        "carlina/__init__.py": b'"""Synthetic Carlina package."""\n',
        "carlina/documents.py": b"DOCUMENTS = True\n",
        "carlina/managed_launcher.py": b"MANAGED_LAUNCHER = True\n",
        "carlina/included.py": b"INCLUDED = True\n",
        "carlina/distribution.py": b"SOURCE_DISTRIBUTION_MODULE = True\n",
        "iarf_assistant/__init__.py": b'"""Synthetic host package."""\n',
        "iarf_assistant/app.py": b"APP = True\n",
        "iarf_assistant/models.py": b"COMPATIBLE_MODELS = True\n",
        "iarf_assistant/host_module.py": b"HOST_MODULE = True\n",
        "iarf_assistant/nested/not_selected.py": b"NESTED_TOP_LEVEL_MODULE = True\n",
        "iarf_assistant/core/__init__.py": b"CORE = True\n",
        "iarf_assistant/core/core_module.py": b"CORE_MODULE = True\n",
        "iarf_assistant/core/settings.json": b'{"core":true}\n',
        "iarf_assistant/core/addons/catalog.json": b'{"catalog":true}\n',
        "iarf_assistant/core/client_api/__init__.py": b"CAPTURE_API = True\n",
        "iarf_assistant/core/client_api/device_auth.py": b"CAPTURE_AUTH = True\n",
        "iarf_assistant/core/client_api/contracts/compass/v1/schema.yaml": b"schema: 1\n",
        "iarf_assistant/core/client_api/contracts/compass/v1/schema.json": b"{}\n",
        "iarf_assistant/core/alembic_templates/script.py.mako": b"template\n",
        "iarf_assistant/core/alembic_templates/env.ini": b"retained migration template\n",
        "iarf_assistant/core/alembic_templates/ignore.txt": b"excluded extension\n",
        "iarf_assistant/database/engine.py": b"ENGINE = True\n",
        "iarf_assistant/addon_api/api.py": b"API = True\n",
        "iarf_assistant/knowledge/index.py": b"INDEX = True\n",
        "iarf_assistant/migrations/alembic.ini": b"[alembic]\n",
        "iarf_assistant/migrations/env.py": b"ENV = True\n",
        "iarf_assistant/migrations/script.py.mako": b"script\n",
        "iarf_assistant/migrations/versions/001_core.py": b"MIGRATION = True\n",
        "iarf_assistant/migrations/versions/20260812_compass_client_auth.py": (
            b"COMPATIBLE_CAPTURE_MIGRATION = True\n"
        ),
        "iarf_assistant/core/cache/ignored.py": b"CACHE = True\n",
        "iarf_assistant/core/.env": b"DO NOT STAGE\n",
        "iarf_assistant/core/session.key": b"DO NOT STAGE\n",
        "iarf_assistant/core/local.sqlite3": b"DO NOT STAGE\n",
        "iarf_assistant/core/generated.pyc": b"DO NOT STAGE\n",
        "iarf_assistant/addons/stripe/manifest.json": b"private unrelated addon\n",
        "iarf_assistant/addons/stripe/private.py": b"PRIVATE = True\n",
        "iarf_assistant/addons/stripe/static/private.js": b"PRIVATE OPAQUE ADDON\n",
        "tests/test_private.py": b"TEST = True\n",
        "docs/private.md": b"DO NOT STAGE\n",
        "scripts/unapproved_release_helper.py": b"DO NOT STAGE\n",
        "setup.py": b"ROOT_SETUP = True\n",
        ".env": b"DO NOT STAGE\n",
        "private.db": b"DO NOT STAGE\n",
        "frontend/apps/core/dist/generated.js": b"DO NOT STAGE\n",
        "frontend/apps/core/node_modules/private.js": b"DO NOT STAGE\n",
        "frontend/contracts/.env.production": b"DO NOT STAGE\n",
    }
    files.update(_ORIGINAL_CORE_FRONTEND_FILES)
    for addon_id in ("doc_processor", "sentence_segmenter", "translator"):
        addon = f"iarf_assistant/addons/{addon_id}"
        files.update(
            {
                f"{addon}/manifest.json": f'{{"id":"{addon_id}"}}\n'.encode(),
                f"{addon}/__init__.py": b"ADDON = True\n",
                f"{addon}/worker.py": b"WORKER = True\n",
                f"{addon}/requirements.txt": b"addon-dependency\n",
                f"{addon}/requirements.lock": b"locked-addon-dependency\n",
                f"{addon}/conda-linux-64.lock": b"locked-conda-dependency\n",
                f"{addon}/pixi.toml": b"[workspace]\n",
                f"{addon}/pixi.lock": b"synthetic addon lock\n",
                f"{addon}/Dockerfile": b"FROM scratch\n",
                f"{addon}/.dockerignore": b"private\n",
                f"{addon}/docker-compose.yml": b"services: {}\n",
                f"{addon}/docker-requirements.txt": b"worker-dependency\n",
                f"{addon}/entrypoint.sh": b"#!/bin/sh\n",
                f"{addon}/migrations/versions/001_addon.py": b"ADDON_MIGRATION = True\n",
                f"{addon}/skills/native-worker.md": b"Native worker resource\n",
                f"{addon}/container/worker.py": b"CONTAINER_WORKER = True\n",
                f"{addon}/container/requirements.txt": b"worker-dep\n",
                f"{addon}/container/start.sh": b"#!/bin/sh\n",
                f"{addon}/build/generated.py": b"DO NOT STAGE\n",
                f"{addon}/frontend/dist/private.js": b"DO NOT STAGE\n",
            }
        )
    files.update(_OPAQUE_FILES)
    files["iarf_assistant/addons/doc_processor/requirements space.txt"] = (
        b"space path\n"
    )
    for relative, payload in files.items():
        _write(root, relative, payload)

    _run_git(root, "add", "--all")
    _run_git(
        root,
        "-c",
        "user.name=Documents Builder Test",
        "-c",
        "user.email=documents-builder@example.invalid",
        "commit",
        "-qm",
        "synthetic documents source",
    )
    revision = _run_git(root, "rev-parse", "HEAD")
    epoch = int(_run_git(root, "show", "-s", "--format=%ct", revision))
    return revision, epoch


def _fake_build_run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    rebuild_invocations: list[_CoreRebuildInvocation] | None = None,
    patch_core_rebuild: bool = True,
) -> list[_BuildInvocation]:
    original_run = release._release_run
    observed: list[_BuildInvocation] = []
    observed_rebuilds = rebuild_invocations if rebuild_invocations is not None else []

    def rebuild_core_frontend(stage_root: Path, *, source_date_epoch: int) -> None:
        descriptor_path = stage_root / "carlina" / "distribution.json"
        assert descriptor_path.is_file()
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        assert descriptor["application_profile"] == "documents"
        assert descriptor["source_date_epoch"] == source_date_epoch

        core_root = stage_root / "iarf_assistant" / "static" / "frontend"
        assert all(
            stage_root.joinpath(*PurePosixPath(relative).parts).read_bytes() == payload
            for relative, payload in _ORIGINAL_CORE_FRONTEND_FILES.items()
        )
        shutil.rmtree(core_root)
        core_root.mkdir(parents=True)
        for relative, payload in _GENERATED_CORE_FRONTEND_FILES.items():
            _write(stage_root, relative, payload)

        node_modules = core_root / "node_modules"
        fake_dependency = stage_root.joinpath(
            *PurePosixPath(_SYNTHETIC_NODE_MODULE).parts
        )
        fake_dependency.parent.mkdir(parents=True)
        fake_dependency.write_bytes(b"synthetic temporary dependency\x0b")
        node_modules_created = fake_dependency.is_file()
        shutil.rmtree(node_modules)
        node_modules_removed = not node_modules.exists()

        _write(
            stage_root,
            _UNEXPECTED_GENERATED_SIBLING,
            b"synthetic generated sibling outside core root\x0c",
        )
        observed_rebuilds.append(
            _CoreRebuildInvocation(
                source_date_epoch=source_date_epoch,
                descriptor=descriptor,
                generated_members=frozenset(_GENERATED_CORE_FRONTEND_FILES),
                unexpected_sibling=_UNEXPECTED_GENERATED_SIBLING,
                node_modules_created=node_modules_created,
                node_modules_removed=node_modules_removed,
            )
        )

    if patch_core_rebuild:
        monkeypatch.setattr(
            release, "_rebuild_staged_core_frontend", rebuild_core_frontend
        )

    def run(
        command: list[str],
        *,
        cwd: Path,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if command[1:3] != ["-m", "build"]:
            return original_run(command, cwd=cwd, environment=environment)

        assert environment is not None
        assert environment["SOURCE_DATE_EPOCH"].isdigit()
        assert environment["PYTHONHASHSEED"] == "0"
        assert "--outdir" in command
        output = Path(command[command.index("--outdir") + 1])
        output.mkdir(parents=True)

        manifest = (cwd / "MANIFEST.in").read_text(encoding="utf-8")
        manifest_lines = frozenset(manifest.splitlines())
        patterns = [line.removeprefix("include ") for line in manifest_lines]
        assert patterns
        matched: set[str] = set()
        for pattern in patterns:
            matched.update(
                Path(match).relative_to(cwd).as_posix()
                for match in glob.glob(os.fspath(cwd / pattern))
                if Path(match).is_file()
            )
        staged_files = {
            path.relative_to(cwd).as_posix()
            for path in cwd.rglob("*")
            if path.is_file()
        }
        assert staged_files - matched <= frozenset({_UNEXPECTED_GENERATED_SIBLING})
        observed.append(
            _BuildInvocation(
                command=tuple(command),
                environment=dict(environment),
                staged_files=frozenset(staged_files),
                staged_payloads={
                    relative: (cwd / relative).read_bytes() for relative in staged_files
                },
                manifest_members=frozenset(matched),
                manifest_lines=manifest_lines,
            )
        )

        wheel_path = output / "carlina-1.2.3-py3-none-any.whl"
        record = "carlina-1.2.3.dist-info/RECORD"
        with zipfile.ZipFile(wheel_path, "w") as wheel:
            wheel.writestr(
                "carlina/distribution.json",
                (cwd / "carlina/distribution.json").read_bytes(),
            )
            wheel.writestr(
                "carlina-1.2.3.dist-info/METADATA",
                b"Metadata-Version: 2.1\nName: carlina\nVersion: 1.2.3\n",
            )
            wheel.writestr(record, b"")

        if "--wheel" not in command:
            sdist_path = output / "carlina-1.2.3.tar.gz"
            with tarfile.open(sdist_path, "w:gz") as sdist:
                package_metadata = (
                    b"Metadata-Version: 2.1\nName: carlina\nVersion: 1.2.3\n"
                )
                package_info = tarfile.TarInfo("carlina-1.2.3/PKG-INFO")
                package_info.size = len(package_metadata)
                sdist.addfile(package_info, io.BytesIO(package_metadata))
                for relative in sorted(matched):
                    source = cwd / relative
                    info = tarfile.TarInfo(f"carlina-1.2.3/{relative}")
                    info.size = source.stat().st_size
                    sdist.addfile(info, source.open("rb"))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(release, "_release_run", run)
    return observed


def _read_source_archive(payload: bytes) -> dict[str, bytes]:
    members: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            source = archive.extractfile(member)
            assert source is not None
            members[member.name] = source.read()
    return members


def _assert_rebuilt_core_frontend_is_packaged(paths: tuple[Path, ...]) -> None:
    wheel_path = next(path for path in paths if path.suffix == ".whl")
    source_path = next(path for path in paths if path.name.endswith(".tar.gz"))
    source_bytes = source_path.read_bytes()
    source_prefix = "carlina-1.2.3/"
    source_members = _read_source_archive(source_bytes)
    package_members = {
        name.removeprefix(source_prefix): payload
        for name, payload in source_members.items()
        if name.startswith(source_prefix)
    }

    for relative, payload in _OPAQUE_FILES.items():
        assert package_members[relative] == payload
    for relative, payload in _GENERATED_CORE_FRONTEND_FILES.items():
        assert package_members[relative] == payload
    for relative in set(_ORIGINAL_CORE_FRONTEND_FILES) - set(
        _GENERATED_CORE_FRONTEND_FILES
    ):
        assert relative not in package_members
    assert _UNEXPECTED_GENERATED_SIBLING not in package_members
    assert not any(
        "node_modules" in PurePosixPath(relative).parts for relative in package_members
    )

    manifest = package_members["MANIFEST.in"].decode("utf-8").splitlines()
    manifest_lines = set(manifest)
    for relative in _GENERATED_CORE_FRONTEND_FILES:
        assert f"include {release._manifest_pattern(relative)}" in manifest_lines
    for relative in set(_ORIGINAL_CORE_FRONTEND_FILES) - set(
        _GENERATED_CORE_FRONTEND_FILES
    ):
        assert f"include {release._manifest_pattern(relative)}" not in manifest_lines
    assert (
        f"include {release._manifest_pattern(_UNEXPECTED_GENERATED_SIBLING)}"
        not in manifest_lines
    )
    assert not any("node_modules" in line for line in manifest_lines)

    with zipfile.ZipFile(wheel_path) as wheel:
        embedded_source = wheel.read("carlina/application-source.tar.gz")
    assert embedded_source == source_bytes
    embedded_members = _read_source_archive(embedded_source)
    assert embedded_members == source_members


def _assert_normalized_artifacts(paths: tuple[Path, ...], epoch: int) -> None:
    wheel = next(path for path in paths if path.suffix == ".whl")
    with zipfile.ZipFile(wheel) as archive:
        assert "carlina/distribution.json" in archive.namelist()
        timestamps = {info.date_time for info in archive.infolist()}
        assert len(timestamps) == 1
        manifest = json.loads(archive.read("carlina/application-source.json"))
        bundled_source = archive.read("carlina/application-source.tar.gz")
        assert manifest["sha256"] == hashlib.sha256(bundled_source).hexdigest()
        assert manifest["byte_length"] == len(bundled_source)

    sdist = next(path for path in paths if path.name.endswith(".tar.gz"))
    assert bundled_source == sdist.read_bytes()
    with tarfile.open(sdist, "r:gz") as archive:
        members = archive.getmembers()
        assert members
        assert {member.mtime for member in members} == {epoch}


def test_core_frontend_rebuild_orders_steps_and_normalizes_stamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from iarf_assistant.core import frontend_orchestration

    stage_root = tmp_path / "stage"
    stage_root.mkdir()
    node_modules = stage_root / "node_modules"
    stamp_path = (
        stage_root
        / "iarf_assistant"
        / "static"
        / "frontend"
        / ".frontend-build-stamp.json"
    )
    stamp_path.parent.mkdir(parents=True)
    original_stamp = {
        "built_at": "not-normalized",
        "opaque_metadata": {"profile": "synthetic", "sequence": [2, "kept"]},
    }
    calls: list[str] = []

    def install_dependencies(root: Path, *, force: bool) -> dict[str, object]:
        assert root == stage_root
        assert force
        calls.append("install")
        node_modules.mkdir()
        (node_modules / "synthetic-install.marker").write_bytes(b"synthetic")
        return {"success": True}

    def build_core(root: Path, *, force: bool) -> dict[str, object]:
        assert root == stage_root
        assert force
        assert calls == ["install"]
        assert node_modules.is_dir()
        calls.append("build")
        stamp_path.write_text(json.dumps(original_stamp), encoding="utf-8")
        return {"success": True}

    monkeypatch.setattr(
        frontend_orchestration,
        "install_frontend_dependencies",
        install_dependencies,
    )
    monkeypatch.setattr(frontend_orchestration, "run_core_frontend_build", build_core)

    release._rebuild_staged_core_frontend(stage_root, source_date_epoch=1_700_000_000)

    assert calls == ["install", "build"]
    assert json.loads(stamp_path.read_text(encoding="utf-8")) == {
        **original_stamp,
        "built_at": "2023-11-14T22:13:20Z",
    }
    assert not node_modules.exists()


@pytest.mark.parametrize("failure", ("install", "build", "invalid_stamp"))
def test_core_frontend_rebuild_cleans_node_modules_after_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from iarf_assistant.core import frontend_orchestration

    stage_root = tmp_path / "stage"
    stage_root.mkdir()
    node_modules = stage_root / "node_modules"
    stamp_path = (
        stage_root
        / "iarf_assistant"
        / "static"
        / "frontend"
        / ".frontend-build-stamp.json"
    )
    calls: list[str] = []

    def install_dependencies(root: Path, *, force: bool) -> dict[str, object]:
        assert root == stage_root
        assert force
        calls.append("install")
        node_modules.mkdir()
        (node_modules / "synthetic-install.marker").write_bytes(b"synthetic")
        return {"success": failure != "install"}

    def build_core(root: Path, *, force: bool) -> dict[str, object]:
        assert root == stage_root
        assert force
        assert calls == ["install"]
        calls.append("build")
        stamp_path.parent.mkdir(parents=True)
        stamp_path.write_text(
            "[]" if failure == "invalid_stamp" else "{}", encoding="utf-8"
        )
        return {"success": failure != "build"}

    monkeypatch.setattr(
        frontend_orchestration,
        "install_frontend_dependencies",
        install_dependencies,
    )
    monkeypatch.setattr(frontend_orchestration, "run_core_frontend_build", build_core)

    with pytest.raises(release.DocumentsReleaseBuildError):
        release._rebuild_staged_core_frontend(
            stage_root, source_date_epoch=1_700_000_000
        )

    assert calls == (["install"] if failure == "install" else ["install", "build"])
    assert stage_root.is_dir()
    assert not node_modules.exists()
    assert not node_modules.is_symlink()


def test_core_frontend_rebuild_unlinks_root_node_modules_symlink_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from iarf_assistant.core import frontend_orchestration

    stage_root = tmp_path / "stage"
    stage_root.mkdir()
    outside = tmp_path / "outside-node-modules"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_bytes(b"outside target stays intact")
    node_modules = stage_root / "node_modules"
    node_modules.symlink_to(outside, target_is_directory=True)
    stamp_path = (
        stage_root
        / "iarf_assistant"
        / "static"
        / "frontend"
        / ".frontend-build-stamp.json"
    )

    def install_dependencies(root: Path, *, force: bool) -> dict[str, object]:
        assert root == stage_root
        assert force
        assert node_modules.is_symlink()
        return {"success": True}

    def build_core(root: Path, *, force: bool) -> dict[str, object]:
        assert root == stage_root
        assert force
        stamp_path.parent.mkdir(parents=True)
        stamp_path.write_text('{"opaque":true}', encoding="utf-8")
        return {"success": True}

    monkeypatch.setattr(
        frontend_orchestration,
        "install_frontend_dependencies",
        install_dependencies,
    )
    monkeypatch.setattr(frontend_orchestration, "run_core_frontend_build", build_core)

    release._rebuild_staged_core_frontend(stage_root, source_date_epoch=1_700_000_000)

    assert not node_modules.is_symlink()
    assert not node_modules.exists()
    assert sentinel.read_bytes() == b"outside target stays intact"


def test_checkout_only_document_link_blocks_build_before_frontend_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    _make_git_project(source)
    _write(source, "docs/checkout-only.md", b"Not selected for Documents.\n")
    _write(
        source,
        "docs/documents-application.md",
        b"[checkout-only guide](checkout-only.md)\n",
    )
    _run_git(source, "add", "docs")
    _run_git(source, "commit", "-qm", "Add a link outside the source selection")
    rebuilds: list[_CoreRebuildInvocation] = []
    builds = _fake_build_run(monkeypatch, rebuild_invocations=rebuilds)
    destination = tmp_path / "output"

    with pytest.raises(
        release.DocumentsReleaseBuildError, match="broken local Markdown"
    ):
        release.build_documents_release(destination, root=source)

    assert rebuilds == []
    assert builds == []
    assert not destination.exists()


def test_build_stages_exact_documents_closure_and_normalizes_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    revision, epoch = _make_git_project(source)
    rebuild_observed: list[_CoreRebuildInvocation] = []
    observed = _fake_build_run(monkeypatch, rebuild_invocations=rebuild_observed)

    first = release.build_documents_release(tmp_path / "out-one", root=source)
    second = release.build_documents_release(tmp_path / "out-two", root=source)

    assert len(observed) == 2
    for item in observed:
        selected = documents_backend_members(
            source,
            extra_members=release._discover_extra_members(source),
        )
        staged_files = item["staged_files"]
        staged_payloads = item["staged_payloads"]
        assert set(_ORIGINAL_CORE_FRONTEND_FILES) <= set(selected)
        updated_selected = (set(selected) - set(_ORIGINAL_CORE_FRONTEND_FILES)) | set(
            _GENERATED_CORE_FRONTEND_FILES
        )
        assert "MANIFEST.in" not in selected
        expected_manifest_members = frozenset(
            {*updated_selected, "carlina/distribution.json", "MANIFEST.in"}
        )
        assert item["manifest_members"] == expected_manifest_members
        assert item["manifest_lines"] == frozenset(
            f"include {release._manifest_pattern(relative)}"
            for relative in expected_manifest_members
        )
        assert expected_manifest_members <= staged_files
        assert staged_files - expected_manifest_members <= frozenset(
            {_UNEXPECTED_GENERATED_SIBLING}
        )
        assert set(_EXPECTED_DOCUMENTS_RELEASE_CONTROLS) <= staged_files
        assert not any(
            name.startswith("iarf_assistant/core/client_api/") for name in staged_files
        )
        assert (
            staged_payloads["iarf_assistant/models.py"] == b"COMPATIBLE_MODELS = True\n"
        )
        assert (
            staged_payloads[
                "iarf_assistant/migrations/versions/20260812_compass_client_auth.py"
            ]
            == b"COMPATIBLE_CAPTURE_MIGRATION = True\n"
        )
        assert staged_payloads["MANIFEST.in"] != b"recursive-include . *\n"
        assert all(
            line.startswith("include ")
            for line in staged_payloads["MANIFEST.in"].decode("utf-8").splitlines()
        )
        manifest_lines = item["manifest_lines"]
        for relative in _GENERATED_CORE_FRONTEND_FILES:
            assert f"include {release._manifest_pattern(relative)}" in manifest_lines
            assert staged_payloads[relative] == _GENERATED_CORE_FRONTEND_FILES[relative]
        for relative in set(_ORIGINAL_CORE_FRONTEND_FILES) - set(
            _GENERATED_CORE_FRONTEND_FILES
        ):
            assert (
                f"include {release._manifest_pattern(relative)}" not in manifest_lines
            )
            assert relative not in staged_files
        for relative in set(_ORIGINAL_CORE_FRONTEND_FILES) & set(
            _GENERATED_CORE_FRONTEND_FILES
        ):
            assert staged_payloads[relative] == _GENERATED_CORE_FRONTEND_FILES[relative]
        assert _UNEXPECTED_GENERATED_SIBLING not in item["manifest_members"]
        assert not any(
            "node_modules" in PurePosixPath(relative).parts for relative in staged_files
        )
        assert "iarf_assistant/core/alembic_templates/env.ini" in selected
        assert "iarf_assistant/core/alembic_templates/ignore.txt" not in selected
        for native_resource in (
            "iarf_assistant/addons/doc_processor/requirements.txt",
            "iarf_assistant/addons/doc_processor/requirements.lock",
            "iarf_assistant/addons/doc_processor/conda-linux-64.lock",
            "iarf_assistant/addons/doc_processor/pixi.toml",
            "iarf_assistant/addons/doc_processor/pixi.lock",
            "iarf_assistant/addons/doc_processor/Dockerfile",
            "iarf_assistant/addons/doc_processor/.dockerignore",
            "iarf_assistant/addons/doc_processor/docker-compose.yml",
            "iarf_assistant/addons/doc_processor/docker-requirements.txt",
            "iarf_assistant/addons/doc_processor/entrypoint.sh",
            "iarf_assistant/addons/doc_processor/migrations/versions/001_addon.py",
            "iarf_assistant/addons/doc_processor/skills/native-worker.md",
            "iarf_assistant/addons/doc_processor/container/worker.py",
        ):
            assert native_resource in selected

        descriptor = json.loads(staged_payloads["carlina/distribution.json"])
        assert descriptor == {
            "format": "carlina.application-distribution",
            "schema_version": 1,
            "application_profile": "documents",
            "source_revision": revision,
            "source_date_epoch": epoch,
            "addon_ids": ["doc_processor", "sentence_segmenter", "translator"],
        }
        staged_metadata = staged_payloads["pyproject.toml"].decode("utf-8")
        assert 'carlina = ["distribution.json"]' in staged_metadata
        assert staged_payloads["pyproject.toml"] != _PROJECT_METADATA
        parsed_source_metadata = tomllib.loads(_PROJECT_METADATA.decode("utf-8"))
        parsed_staged_metadata = tomllib.loads(staged_metadata)
        assert parsed_staged_metadata["project"] == parsed_source_metadata["project"]
        assert (
            parsed_staged_metadata["tool"]["setuptools"]["package-data"][
                "iarf_assistant"
            ]
            == parsed_source_metadata["tool"]["setuptools"]["package-data"][
                "iarf_assistant"
            ]
        )
        assert parsed_staged_metadata["tool"]["setuptools"]["package-data"][
            "carlina"
        ] == ["distribution.json"]

        for relative, payload in _OPAQUE_FILES.items():
            assert staged_payloads[relative] == payload
        assert (
            staged_payloads[
                "iarf_assistant/addons/doc_processor/requirements space.txt"
            ]
            == b"space path\n"
        )
        assert not any(
            name.startswith("iarf_assistant/addons/stripe/") for name in staged_files
        )
        assert "docs/private.md" not in staged_files
        assert "scripts/unapproved_release_helper.py" not in staged_files
        assert staged_payloads["README.md"].decode("utf-8") == release._DOCUMENTS_README
        assert "docs/operations.md" not in staged_files
        assert not any(name.startswith("tests/") for name in staged_files)
        assert "setup.py" not in staged_files
        assert ".env" not in staged_files
        assert not any(
            component in {".env", "cache", "build", "node_modules"}
            or component.startswith(".env.")
            or (
                component == "dist"
                and (index == 0 or PurePosixPath(relative).parts[index - 1] != "static")
            )
            for relative in staged_files
            for index, component in enumerate(PurePosixPath(relative).parts)
        )

    assert len(rebuild_observed) == 2
    for item in rebuild_observed:
        assert item["source_date_epoch"] == epoch
        assert item["descriptor"]["source_date_epoch"] == epoch
        assert item["descriptor"]["source_revision"] == revision
        assert item["descriptor"]["application_profile"] == "documents"
        assert item["generated_members"] == frozenset(_GENERATED_CORE_FRONTEND_FILES)
        assert item["unexpected_sibling"] == _UNEXPECTED_GENERATED_SIBLING
        assert item["node_modules_created"]
        assert item["node_modules_removed"]

    assert first == tuple(sorted(first))
    assert second == tuple(sorted(second))
    for outputs in (first, second):
        _assert_normalized_artifacts(outputs, epoch)
        _assert_rebuilt_core_frontend_is_packaged(outputs)
    first_hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in first
    }
    second_hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in second
    }
    assert first_hashes == second_hashes

    assert (source / "pyproject.toml").read_bytes() == _PROJECT_METADATA
    assert _run_git(source, "status", "--porcelain", "--untracked-files=no") == ""


def test_core_rebuild_failure_prevents_python_packaging_and_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    _make_git_project(source)
    observed = _fake_build_run(monkeypatch)
    failed_epochs: list[int] = []

    def fail_core_rebuild(stage_root: Path, *, source_date_epoch: int) -> None:
        descriptor_path = stage_root / "carlina" / "distribution.json"
        assert descriptor_path.is_file()
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        assert descriptor["source_date_epoch"] == source_date_epoch
        failed_epochs.append(source_date_epoch)
        raise release.DocumentsReleaseBuildError("synthetic core rebuild failure")

    monkeypatch.setattr(release, "_rebuild_staged_core_frontend", fail_core_rebuild)
    output = tmp_path / "unpublished"
    with pytest.raises(
        release.DocumentsReleaseBuildError,
        match="synthetic core rebuild failure",
    ):
        release.build_documents_release(output, root=source)

    assert failed_epochs
    assert observed == []
    assert not output.exists() or not tuple(output.iterdir())


def test_generated_core_frontend_symlink_fails_before_python_packaging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from iarf_assistant.core import frontend_orchestration

    source = tmp_path / "source"
    _make_git_project(source)
    observed = _fake_build_run(monkeypatch, patch_core_rebuild=False)
    outside_target = tmp_path / "outside-generated-asset.js"
    outside_target.write_bytes(b"synthetic outside target")

    def install_dependencies(root: Path, *, force: bool) -> dict[str, object]:
        assert force
        node_modules = root / "node_modules"
        node_modules.mkdir()
        (node_modules / "synthetic-install.marker").write_bytes(b"synthetic")
        return {"success": True}

    def build_core(root: Path, *, force: bool) -> dict[str, object]:
        assert force
        frontend_root = root / "iarf_assistant" / "static" / "frontend"
        frontend_root.mkdir(parents=True, exist_ok=True)
        (frontend_root / ".frontend-build-stamp.json").write_text(
            '{"opaque":true}', encoding="utf-8"
        )
        (frontend_root / "synthetic-entry.js").write_bytes(b"synthetic bundle")
        (frontend_root / "linked-output.js").symlink_to(outside_target)
        return {"success": True}

    monkeypatch.setattr(
        frontend_orchestration,
        "install_frontend_dependencies",
        install_dependencies,
    )
    monkeypatch.setattr(frontend_orchestration, "run_core_frontend_build", build_core)

    output = tmp_path / "unpublished"
    with pytest.raises(
        release.DocumentsReleaseBuildError,
        match="rebuilt frontend member is not a regular file",
    ):
        release.build_documents_release(output, root=source)

    assert observed == []
    assert outside_target.read_bytes() == b"synthetic outside target"
    assert not output.exists() or not tuple(output.iterdir())


def test_existing_outputs_require_force_and_are_not_partially_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    _make_git_project(source)
    _fake_build_run(monkeypatch)
    output = tmp_path / "out"
    first = release.build_documents_release(output, root=source)
    first_wheel = next(path for path in first if path.suffix == ".whl")
    sentinel = b"preserve the existing artifact\n"
    first_wheel.write_bytes(sentinel)

    with pytest.raises(release.DocumentsReleaseBuildError, match="already exist"):
        release.build_documents_release(output, root=source)

    assert first_wheel.read_bytes() == sentinel
    forced = release.build_documents_release(output, root=source, force=True)
    assert forced == first
    assert first_wheel.read_bytes() != sentinel


def test_exported_closure_check_blocks_artifact_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    _make_git_project(source)
    _write(source, "scripts/check_documents_closure.py", b"raise SystemExit(1)\n")
    _run_git(source, "add", "scripts/check_documents_closure.py")
    _run_git(
        source,
        "-c",
        "user.name=Documents Builder Test",
        "-c",
        "user.email=documents-builder@example.invalid",
        "commit",
        "-qm",
        "synthetic closure rejection",
    )
    observed = _fake_build_run(monkeypatch)
    output = tmp_path / "out"
    with pytest.raises(release._clean_release.CleanReleaseBuildError):
        release.build_documents_release(output, root=source)
    assert len(observed) == 1
    assert not output.exists()


def test_dirty_tracked_inputs_fail_before_running_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    _make_git_project(source)
    observed = _fake_build_run(monkeypatch)
    (source / "README.md").write_text("dirty tracked input\n", encoding="utf-8")

    with pytest.raises(
        release._clean_release.CleanReleaseBuildError,
        match="Tracked files differ from HEAD",
    ):
        release.build_documents_release(tmp_path / "out", root=source)

    assert observed == []
    assert not (tmp_path / "out").exists()


def test_wheel_only_build_keeps_its_embedded_source_without_separate_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    _make_git_project(source)
    observed = _fake_build_run(monkeypatch)

    outputs = release.build_documents_release(
        tmp_path / "out",
        root=source,
        wheel_only=True,
    )

    assert len(outputs) == 1
    assert outputs[0].suffix == ".whl"
    assert len(observed) == 1
    command = observed[0]["command"]
    assert "--wheel" not in command
    with zipfile.ZipFile(outputs[0]) as archive:
        assert "carlina/application-source.tar.gz" in archive.namelist()
        assert (
            json.loads(archive.read("carlina/application-source.json"))[
                "application_version"
            ]
            == "1.2.3"
        )
    assert not (tmp_path / "out" / "carlina-1.2.3.tar.gz").exists()


def test_direct_cli_exposes_the_bounded_build_options() -> None:
    result = subprocess.run(
        [
            sys.executable,
            os.fspath(release.PROJECT_ROOT / "scripts/build_documents_release.py"),
            "--help",
        ],
        cwd=release.PROJECT_ROOT,
        check=False,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )

    assert result.returncode == 0
    for option in ("--root", "--revision", "--output-dir", "--wheel-only", "--force"):
        assert option in result.stdout


def test_source_distribution_descriptor_collision_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    _make_git_project(source)
    _write(source, "carlina/distribution.json", b"must not replace repository data\n")
    _run_git(source, "add", "carlina/distribution.json")
    _run_git(
        source,
        "-c",
        "user.name=Documents Builder Test",
        "-c",
        "user.email=documents-builder@example.invalid",
        "commit",
        "-qm",
        "descriptor collision fixture",
    )
    observed = _fake_build_run(monkeypatch)

    with pytest.raises(
        release.DocumentsReleaseBuildError,
        match="collides with source",
    ):
        release.build_documents_release(tmp_path / "out", root=source)

    assert observed == []
    assert not (tmp_path / "out").exists()


def test_allowed_opaque_symlink_components_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_git_project(source)
    target = source / "opaque-target"
    target.mkdir()
    (target / "app.js").write_bytes(b"synthetic only\n")
    (source / "frontend/apps/core/linked").symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError):
        documents_backend_members(
            source,
            extra_members=("frontend/apps/core/linked/app.js",),
        )

    link = source / "frontend/apps/core/direct-link.js"
    link.symlink_to(target / "app.js")
    with pytest.raises(ValueError):
        documents_backend_members(
            source,
            extra_members=("frontend/apps/core/direct-link.js",),
        )
