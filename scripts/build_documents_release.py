#!/usr/bin/env python3
"""Build the Documents wheel and sdist from an exact, clean Git export."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if not __package__ and os.fspath(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, os.fspath(PROJECT_ROOT))

from iarf_assistant.core import frontend_orchestration  # noqa: E402

if TYPE_CHECKING or not __package__:
    from scripts import build_clean_release as _clean_release
    from scripts import documents_source_inventory as _source_inventory
    from scripts.check_markdown_links import selected_link_errors
    from scripts.documents_source_inventory import documents_backend_members
    from scripts.package_application_source import (
        ApplicationSourcePackagingError,
        package_application_source,
    )
else:
    from . import build_clean_release as _clean_release
    from . import documents_source_inventory as _source_inventory
    from .check_markdown_links import selected_link_errors
    from .documents_source_inventory import documents_backend_members
    from .package_application_source import (
        ApplicationSourcePackagingError,
        package_application_source,
    )

_release_run = _clean_release._run  # pyright: ignore[reportPrivateUsage]
_release_archive_command = (
    _clean_release._git_archive_command  # pyright: ignore[reportPrivateUsage]
)
_release_safe_extract = (
    _clean_release._safe_extract_git_archive  # pyright: ignore[reportPrivateUsage]
)
_release_commit_epoch = _clean_release._commit_epoch  # pyright: ignore[reportPrivateUsage]
_release_normalize_sdist = _clean_release._normalize_sdist  # pyright: ignore[reportPrivateUsage]
_release_require_clean_checkout = (
    _clean_release._require_clean_tracked_checkout  # pyright: ignore[reportPrivateUsage]
)
_inventory_is_denied = _source_inventory._is_denied  # pyright: ignore[reportPrivateUsage]

_EXTRA_EXACT_MEMBERS: Final[tuple[str, ...]] = (
    "README.md",
    "COPYING-AGPL-3.0.txt",
    "third_party/frontend/NOTICE.json",
    "third_party/models/NOTICE.json",
    "third_party/appimage/NOTICE.json",
    "third_party/windows-bootstrap/NOTICE.json",
    "third_party/models/segment-any-text-sat-12l-sm.md",
    "third_party/models/FacebookAI-xlm-roberta-base.md",
    "pyproject.toml",
    "pixi.toml",
    "pixi.lock",
    "package.json",
    "package-lock.json",
    "policy/release-exceptions.json",
    "policy/documents-closure.json",
    "eslint.config.mjs",
    "docs/documents-application.md",
    "docs/epub-export.md",
    "docs/addon-api-v1.md",
    "docs/architecture.md",
    "docs/engineering-policy.md",
    "docs/extraction-efficiency.md",
    "docs/mcp-integration-architecture.md",
    "docs/translator.md",
    "docs/update-and-recovery.md",
    "docs/adr/0011-root-bound-ordinary-content-policy.md",
    "docs/adr/0012-documents-agpl-distribution.md",
    "frontend/tsconfig.base.json",
    "scripts/run_frontend_workspaces.mjs",
    "scripts/build_tailwind.py",
    "scripts/documents_dependency_inventory.py",
    "scripts/measure_documents_closure.py",
    "scripts/check_documents_closure.py",
    "scripts/check_markdown_links.py",
    "scripts/generate_documents_inventory.py",
    "scripts/documents_conda_notices.py",
    "scripts/collect_documents_notices.py",
    "scripts/assemble_documents_notices.py",
    "scripts/assemble_documents_sources.py",
    "scripts/package_application_source.py",
    "scripts/package_application_companions.py",
    "scripts/build_clean_release.py",
    "scripts/build_documents_release.py",
    "scripts/build_documents_appimage.py",
    "scripts/build_documents_windows_installer.py",
    "scripts/documents_appimage.py",
    "packaging/appimage/AppRun",
    "packaging/appimage/carlina-documents.desktop",
    "packaging/windows/carlina-documents.ico",
    "packaging/windows/requirements-build.lock",
    "scripts/documents_source_inventory.py",
    "scripts/addon_publication_policy.py",
    "scripts/build_managed_pack.py",
    "scripts/prepare_publisher_bootstrap.py",
    "scripts/build_publisher_bootstrap.py",
    "scripts/build_release_index.py",
    "scripts/render_managed_installers.py",
    "scripts/managed_pixi_smoke.py",
    "scripts/verify_release_artifacts.py",
)
_DOCUMENT_ADDON_IDS: Final[tuple[str, str, str]] = (
    "doc_processor",
    "sentence_segmenter",
    "translator",
)
_EXTRA_DIRECTORY_ROOTS: Final[tuple[tuple[str, ...], ...]] = (
    ("third_party", "appimage", "notices"),
    ("third_party", "windows-bootstrap", "notices"),
    ("third_party", "frontend", "notices"),
    ("iarf_assistant", "static"),
    ("iarf_assistant", "templates"),
    ("frontend", "apps", "core"),
    ("frontend", "packages"),
    ("frontend", "contracts"),
    *(
        ("iarf_assistant", "addons", addon_id, area)
        for addon_id in _DOCUMENT_ADDON_IDS
        for area in ("static", "templates", "frontend")
    ),
)
_DIST_FORMAT: Final = "carlina.application-distribution"
_DIST_SCHEMA_VERSION: Final = 1
_SOURCE_REVISION_PATTERN: Final = re.compile(r"[0-9a-f]{40}\Z")
_MANIFEST_FILE: Final = "MANIFEST.in"
_STAGED_INSTALL_ERROR: Final = (
    "The staged frontend dependencies could not be installed."
)
_STAGED_BUILD_ERROR: Final = "The staged Documents frontend could not be built."
_STAGED_STAMP_ERROR: Final = "The staged frontend stamp is invalid."
_STAGED_PREPARATION_ERROR: Final = (
    "The staged Documents frontend could not be prepared."
)
_STAGED_ASSET_ERROR: Final = "The staged frontend has no built assets."
_STAGED_MEMBER_ERROR: Final = "A rebuilt frontend member is not a regular file."
_DOCUMENTS_README: Final = """# Carlina Documents

Extract, review and translate documents with the Carlina Document Processor,
Translator and native sentence segmenter. Export readable EPUB, DOCX, HTML or
PDF. Use configured API providers or an external AI client through passive MCP.

This is a development preview. Installed Linux and Windows bundles include their
Python and Pixi runtime and prebuilt browser interface; conversion tools and
segmentation models use managed environments. See the versioned GitHub release
for available downloads, tested platforms and setup limits. The source preview
does not qualify an unadvertised binary or platform.

Start with the [Documents guide](docs/documents-application.md) for installation,
first-run setup, MCP connections, data policy and source build instructions.
See [LICENSE](LICENSE) and [third-party notices](THIRD_PARTY_NOTICES.md).
"""


class DocumentsReleaseBuildError(RuntimeError):
    """Raised when a Documents artifact cannot be safely staged or built."""


def _git_revision(root: Path, revision: str) -> str:
    if not revision or revision.startswith("-"):
        raise DocumentsReleaseBuildError("The source revision is invalid.")
    result = _release_run(
        ["git", "rev-parse", "--verify", f"{revision}^{{commit}}"],
        cwd=root,
    )
    resolved = result.stdout.strip()
    if _SOURCE_REVISION_PATTERN.fullmatch(resolved) is None:
        raise DocumentsReleaseBuildError("The source revision is invalid.")
    return resolved


def _inventory_walk_error(_error: OSError) -> None:
    raise DocumentsReleaseBuildError(
        "Opaque source members could not be inventoried."
    ) from None


def _discover_extra_members(export_root: Path) -> tuple[str, ...]:
    """List tracked archive paths in the exact approved opaque-member roots."""
    members: set[str] = set()
    for member in _EXTRA_EXACT_MEMBERS:
        path = export_root.joinpath(*member.split("/"))
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise DocumentsReleaseBuildError(
                "Opaque source members could not be inventoried."
            ) from None
        members.add(member)

    for relative_root in _EXTRA_DIRECTORY_ROOTS:
        directory = export_root.joinpath(*relative_root)
        try:
            status = directory.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise DocumentsReleaseBuildError(
                "Opaque source members could not be inventoried."
            ) from None
        if stat.S_ISLNK(status.st_mode):
            raise DocumentsReleaseBuildError(
                "An approved opaque source root is a symlink."
            )
        if not stat.S_ISDIR(status.st_mode):
            raise DocumentsReleaseBuildError(
                "An approved opaque source root is not a directory."
            )

        try:
            for current_name, directory_names, file_names in os.walk(
                directory,
                topdown=True,
                followlinks=False,
                onerror=_inventory_walk_error,
            ):
                current = Path(current_name)
                current_parts = current.relative_to(export_root).parts
                kept_directories: list[str] = []
                for name in sorted(directory_names):
                    parts = (*current_parts, name)
                    if _inventory_is_denied(parts, is_extra=True):
                        continue
                    candidate = current / name
                    try:
                        child_status = candidate.lstat()
                    except OSError:
                        raise DocumentsReleaseBuildError(
                            "Opaque source members could not be inventoried."
                        ) from None
                    if stat.S_ISLNK(child_status.st_mode):
                        members.add("/".join(parts))
                    elif stat.S_ISDIR(child_status.st_mode):
                        kept_directories.append(name)
                    else:
                        members.add("/".join(parts))
                directory_names[:] = kept_directories

                for name in sorted(file_names):
                    parts = (*current_parts, name)
                    if _inventory_is_denied(parts, is_extra=True):
                        continue
                    members.add("/".join(parts))
        except DocumentsReleaseBuildError:
            raise
        except OSError:
            raise DocumentsReleaseBuildError(
                "Opaque source members could not be inventoried."
            ) from None
    return tuple(sorted(members))


def _copy_member(source_root: Path, stage_root: Path, member: str) -> None:
    parts = tuple(member.split("/"))
    source = source_root.joinpath(*parts)
    destination = stage_root.joinpath(*parts)
    try:
        source_status = source.lstat()
    except OSError:
        raise DocumentsReleaseBuildError(
            "A selected source member disappeared."
        ) from None
    if not stat.S_ISREG(source_status.st_mode):
        raise DocumentsReleaseBuildError(
            "A selected source member is not a regular file."
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copyfile(source, destination)
    except OSError:
        raise DocumentsReleaseBuildError(
            "A selected source member could not be staged."
        ) from None


def _require_package_data(parsed: dict[str, object]) -> dict[str, object]:
    tool = parsed.get("tool")
    if not isinstance(tool, dict):
        raise DocumentsReleaseBuildError(
            "The staged package-data section is missing or invalid."
        )
    setuptools = cast(dict[str, object], tool).get("setuptools")
    if not isinstance(setuptools, dict):
        raise DocumentsReleaseBuildError(
            "The staged package-data section is missing or invalid."
        )
    package_data = cast(dict[str, object], setuptools).get("package-data")
    if not isinstance(package_data, dict):
        raise DocumentsReleaseBuildError(
            "The staged package-data section is missing or invalid."
        )
    return cast(dict[str, object], package_data)


def _patch_stage_pyproject(pyproject_path: Path) -> None:
    try:
        source = pyproject_path.read_bytes().decode("utf-8")
        parsed = cast(dict[str, object], tomllib.loads(source))
    except OSError, UnicodeDecodeError, tomllib.TOMLDecodeError:
        raise DocumentsReleaseBuildError(
            "The staged project metadata is invalid."
        ) from None

    package_data = _require_package_data(parsed)
    if "iarf_assistant" not in package_data:
        raise DocumentsReleaseBuildError(
            "The staged package-data section is missing or invalid."
        )
    if "carlina" in package_data:
        raise DocumentsReleaseBuildError(
            "The staged Carlina package-data entry already exists."
        )

    lines = source.splitlines(keepends=True)
    section_header = "[tool.setuptools.package-data]"
    section_indexes = [
        index for index, line in enumerate(lines) if line.strip() == section_header
    ]
    if len(section_indexes) != 1:
        raise DocumentsReleaseBuildError(
            "The staged package-data section is missing or ambiguous."
        )
    insertion = section_indexes[0] + 1
    header_line = lines[section_indexes[0]]
    newline = "\r\n" if header_line.endswith("\r\n") else "\n"
    if not header_line.endswith(("\n", "\r")):
        lines[section_indexes[0]] = f"{header_line}{newline}"
    lines.insert(insertion, f'carlina = ["distribution.json"]{newline}')
    updated = "".join(lines)
    try:
        updated_parsed = cast(dict[str, object], tomllib.loads(updated))
        updated_package_data = _require_package_data(updated_parsed)
        if updated_package_data.get("carlina") != ["distribution.json"]:
            raise DocumentsReleaseBuildError(
                "The staged project metadata could not be updated."
            )
        pyproject_path.write_bytes(updated.encode("utf-8"))
    except tomllib.TOMLDecodeError:
        raise DocumentsReleaseBuildError(
            "The staged project metadata could not be updated."
        ) from None
    except OSError:
        raise DocumentsReleaseBuildError(
            "The staged project metadata could not be updated."
        ) from None


def _write_distribution_descriptor(
    stage_root: Path, *, revision: str, source_date_epoch: int
) -> None:
    descriptor = {
        "format": _DIST_FORMAT,
        "schema_version": _DIST_SCHEMA_VERSION,
        "application_profile": "documents",
        "source_revision": revision,
        "source_date_epoch": source_date_epoch,
        "addon_ids": list(_DOCUMENT_ADDON_IDS),
    }
    target = stage_root / "carlina" / "distribution.json"
    if target.exists() or target.is_symlink():
        raise DocumentsReleaseBuildError(
            "The generated distribution descriptor collides with staged source."
        )
    try:
        target.write_text(
            json.dumps(
                descriptor, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
    except OSError:
        raise DocumentsReleaseBuildError(
            "The distribution descriptor could not be generated."
        ) from None


def _reject_distribution_descriptor_collision(export_root: Path) -> None:
    for member in (
        "distribution.json",
        "application-source.json",
        "application-source.tar.gz",
    ):
        source_descriptor = export_root / "carlina" / member
        try:
            source_descriptor.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise DocumentsReleaseBuildError(
                "The generated distribution descriptor could not be checked."
            ) from None
        raise DocumentsReleaseBuildError(
            "The generated distribution descriptor collides with source."
        )


def _manifest_pattern(member: str) -> str:
    escaped: list[str] = []
    for character in member:
        if character.isspace():
            escaped.append("?")
        elif character == "*":
            escaped.append("[*]")
        elif character == "?":
            escaped.append("[?]")
        elif character == "[":
            escaped.append("[[]")
        elif character == "]":
            escaped.append("[]]")
        else:
            escaped.append(character)
    return "".join(escaped)


def _rebuild_staged_core_frontend(stage_root: Path, *, source_date_epoch: int) -> None:
    """Compile descriptor-selected core assets using the existing stamped build."""
    node_modules = stage_root / "node_modules"
    try:
        dependencies = frontend_orchestration.install_frontend_dependencies(
            stage_root, force=True
        )
        if dependencies.get("success") is not True:
            raise DocumentsReleaseBuildError(_STAGED_INSTALL_ERROR)
        result = frontend_orchestration.run_core_frontend_build(stage_root, force=True)
        if result.get("success") is not True:
            raise DocumentsReleaseBuildError(_STAGED_BUILD_ERROR)
        stamp_path = (
            stage_root
            / "iarf_assistant"
            / "static"
            / "frontend"
            / ".frontend-build-stamp.json"
        )
        stamp = json.loads(stamp_path.read_bytes())
        if not isinstance(stamp, dict):
            raise DocumentsReleaseBuildError(_STAGED_STAMP_ERROR)
        stamp["built_at"] = (
            datetime.fromtimestamp(source_date_epoch, UTC)
            .isoformat()
            .replace("+00:00", "Z")
        )
        stamp_path.write_text(
            json.dumps(stamp, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except OSError, ValueError:
        raise DocumentsReleaseBuildError(_STAGED_PREPARATION_ERROR) from None
    finally:
        if node_modules.is_symlink() or node_modules.is_file():
            node_modules.unlink()
        elif node_modules.is_dir():
            shutil.rmtree(node_modules)


def _refresh_core_frontend_members(
    stage_root: Path, members: Sequence[str]
) -> tuple[str, ...]:
    prefix = "iarf_assistant/static/frontend/"
    rebuilt_members = tuple(
        member
        for member in _discover_extra_members(stage_root)
        if member.startswith(prefix)
    )
    if not rebuilt_members:
        raise DocumentsReleaseBuildError(_STAGED_ASSET_ERROR)
    for member in rebuilt_members:
        if not stat.S_ISREG(stage_root.joinpath(*member.split("/")).lstat().st_mode):
            raise DocumentsReleaseBuildError(_STAGED_MEMBER_ERROR)
    return tuple(
        sorted(
            {
                *(member for member in members if not member.startswith(prefix)),
                *rebuilt_members,
            }
        )
    )


def _write_manifest(stage_root: Path, members: Sequence[str]) -> None:
    manifest_members = tuple(
        sorted({*members, "carlina/distribution.json", _MANIFEST_FILE})
    )
    contents = "".join(
        f"include {_manifest_pattern(member)}\n" for member in manifest_members
    )
    try:
        (stage_root / _MANIFEST_FILE).write_text(
            contents, encoding="utf-8", newline="\n"
        )
    except OSError:
        raise DocumentsReleaseBuildError(
            "The staged source manifest could not be generated."
        ) from None


def _build_outputs(built_directory: Path, *, wheel_only: bool) -> tuple[Path, ...]:
    try:
        built = tuple(
            sorted(path for path in built_directory.iterdir() if path.is_file())
        )
    except OSError:
        raise DocumentsReleaseBuildError(
            "The isolated build produced no artifacts."
        ) from None
    wheels = tuple(path for path in built if path.suffix.casefold() == ".whl")
    sdists = tuple(path for path in built if path.name.casefold().endswith(".tar.gz"))
    expected_count = 1 if wheel_only else 2
    if (
        len(wheels) != 1
        or (not wheel_only and len(sdists) != 1)
        or len(built) != expected_count
    ):
        raise DocumentsReleaseBuildError(
            "The isolated build did not produce the expected Carlina artifacts."
        )
    return tuple(sorted((*wheels, *sdists)))


def build_documents_release(
    output_dir: str | os.PathLike[str],
    *,
    root: Path = PROJECT_ROOT,
    revision: str = "HEAD",
    wheel_only: bool = False,
    force: bool = False,
) -> tuple[Path, ...]:
    """Build Documents artifacts from a clean commit using an exact staged closure."""
    source_root = Path(root).expanduser().resolve()
    _release_require_clean_checkout(source_root)
    resolved_revision = _git_revision(source_root, revision)
    source_date_epoch = _release_commit_epoch(source_root, resolved_revision)
    epoch_value = int(source_date_epoch)
    if epoch_value <= 0:
        raise DocumentsReleaseBuildError(
            "The source commit timestamp is invalid for a distribution descriptor."
        )
    destination = Path(output_dir).expanduser().resolve()

    with tempfile.TemporaryDirectory(prefix="carlina-documents-release-") as temporary:
        temporary_root = Path(temporary)
        archive_path = temporary_root / "source.tar"
        export_root = temporary_root / "export"
        export_root.mkdir()
        _release_run(
            _release_archive_command(archive_path, resolved_revision),
            cwd=source_root,
        )
        _release_safe_extract(archive_path, export_root)
        _reject_distribution_descriptor_collision(export_root)

        extra_members = _discover_extra_members(export_root)
        try:
            selected_members = documents_backend_members(
                export_root,
                extra_members=extra_members,
            )
        except ValueError as exc:
            raise DocumentsReleaseBuildError(str(exc)) from exc

        stage_root = temporary_root / "stage"
        stage_root.mkdir()
        for member in selected_members:
            _copy_member(export_root, stage_root, member)
        (stage_root / "README.md").write_text(_DOCUMENTS_README, encoding="utf-8")
        if selected_link_errors(stage_root, selected_members):
            raise DocumentsReleaseBuildError(
                "The selected source contains broken local Markdown links."
            )
        pyproject_path = stage_root / "pyproject.toml"
        if not pyproject_path.is_file():
            raise DocumentsReleaseBuildError("The staged project metadata is missing.")
        _patch_stage_pyproject(pyproject_path)
        _write_distribution_descriptor(
            stage_root,
            revision=resolved_revision,
            source_date_epoch=epoch_value,
        )
        _rebuild_staged_core_frontend(stage_root, source_date_epoch=epoch_value)
        selected_members = _refresh_core_frontend_members(stage_root, selected_members)
        _write_manifest(stage_root, selected_members)

        built_directory = temporary_root / "dist"
        command = [sys.executable, "-m", "build", "--outdir", str(built_directory)]
        build_environment = dict(os.environ)
        build_environment["SOURCE_DATE_EPOCH"] = source_date_epoch
        build_environment["PYTHONHASHSEED"] = "0"
        _release_run(command, cwd=stage_root, environment=build_environment)

        built = _build_outputs(built_directory, wheel_only=False)
        wheel = next(
            artifact for artifact in built if artifact.suffix.casefold() == ".whl"
        )
        source = next(
            artifact for artifact in built if artifact.name.endswith(".tar.gz")
        )
        _release_normalize_sdist(source, source_date_epoch=source_date_epoch)
        package_application_source(wheel, source)
        # Use the exported revision's policy and collector, never this checkout's
        # mutable budgets, before copying any release artifact to its destination.
        for platform in ("linux-64", "win-64"):
            _release_run(
                [
                    sys.executable,
                    str(stage_root / "scripts/check_documents_closure.py"),
                    "--root",
                    str(export_root),
                    "--platform",
                    platform,
                    "--wheel",
                    str(wheel),
                ],
                cwd=stage_root,
            )
        artifacts = (wheel,) if wheel_only else built

        targets = tuple(destination / artifact.name for artifact in artifacts)
        collisions: list[Path] = []
        for target in targets:
            try:
                status = target.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                raise DocumentsReleaseBuildError(
                    "A release output path could not be inspected."
                ) from None
            if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
                raise DocumentsReleaseBuildError(
                    "A release output path is not a regular file."
                )
            collisions.append(target)
        if collisions and not force:
            raise DocumentsReleaseBuildError("Release outputs already exist.")

        try:
            destination.mkdir(parents=True, exist_ok=True)
            for artifact, target in zip(artifacts, targets, strict=True):
                shutil.copy2(artifact, target)
        except OSError:
            raise DocumentsReleaseBuildError(
                "Release outputs could not be written."
            ) from None
    return targets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the Documents distribution from a clean, exact Git export."
    )
    parser.add_argument("--root", default=os.fspath(PROJECT_ROOT))
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--output-dir", default="dist/documents")
    parser.add_argument(
        "--wheel-only",
        action="store_true",
        help="Write only the wheel; its source archive is still built and bundled.",
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if importlib.util.find_spec("build") is None:
        print(
            "Documents release build requires build==1.5.0; use the repository's "
            "locked build environment.",
            file=sys.stderr,
        )
        return 2
    try:
        outputs = build_documents_release(
            args.output_dir,
            root=Path(args.root),
            revision=args.revision,
            wheel_only=args.wheel_only,
            force=args.force,
        )
    except (
        DocumentsReleaseBuildError,
        ApplicationSourcePackagingError,
        _clean_release.CleanReleaseBuildError,
        OSError,
        tarfile.TarError,
        zipfile.BadZipFile,
    ) as exc:
        print(f"Documents release build failed: {exc}", file=sys.stderr)
        return 1
    for output in outputs:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
