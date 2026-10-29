"""Build a provenance-bound Carlina Documents AppImage."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import tomllib
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Final, cast

from packaging.version import InvalidVersion, Version

_HASH_RE: Final = re.compile(r"[0-9a-fA-F]{64}\Z")
_REVISION_RE: Final = re.compile(r"[0-9a-f]{40}\Z")
_MAX_ARCHIVE_MEMBERS: Final = 20_000
_MAX_ARCHIVE_BYTES: Final = 350 * 1024 * 1024
_PROCESS_CAPTURE_BYTES: Final = 4_096
_MAX_INSTALLER_HEADER_BYTES: Final = 64 * 1024
_MAX_INSTALLER_HEADER_LINES: Final = 32
_APPIMAGE_ARGUMENTS: Final = (
    "--appimage-extract-and-run",
    "--no-appstream",
    "--comp",
    "zstd",
)
_STAGE_ROOT_MEMBERS: Final = frozenset(
    {
        ".DirIcon",
        "AppRun",
        "bootstrap.py",
        "carlina-documents.desktop",
        "carlina-documents.svg",
        "installer.sh",
        "launcher.json",
        "licenses",
        "python",
    }
)
_LICENSE_FILES: Final = (
    "LICENSE",
    "COPYING-AGPL-3.0.txt",
    "THIRD_PARTY_NOTICES.md",
)
_BOOTSTRAP_NOTICE: Final = "third_party/appimage/NOTICE.json"
_BOOTSTRAP_NOTICES_DIRECTORY: Final = "third_party/appimage/notices"
_SOURCE_COPIES: Final = (
    ("packaging/appimage/AppRun", ("AppRun",), 0o755),
    (
        "packaging/appimage/carlina-documents.desktop",
        ("carlina-documents.desktop",),
        0o644,
    ),
    ("scripts/documents_appimage.py", ("bootstrap.py",), 0o644),
    (
        "iarf_assistant/static/favicon.svg",
        ("carlina-documents.svg", ".DirIcon"),
        0o644,
    ),
)
_SOURCE_BOUND_PATHS: Final = tuple(
    dict.fromkeys(
        (
            *(relative for relative, _destinations, _mode in _SOURCE_COPIES),
            *_LICENSE_FILES,
            _BOOTSTRAP_NOTICE,
            "scripts/build_documents_appimage.py",
        )
    )
)


class DocumentsAppImageBuildError(RuntimeError):
    """Raised when the fixed Documents AppImage build cannot be completed."""

    diagnostics: dict[str, str] | None = None


def _fail(message: str) -> DocumentsAppImageBuildError:
    return DocumentsAppImageBuildError(message)


def _require_linux_x86_64() -> None:
    if sys_platform() != "linux" or platform.machine().lower() not in {
        "x86_64",
        "amd64",
    }:
        raise _fail("AppImage builds require Linux x86-64.")


def sys_platform() -> str:
    """Return the current platform identifier (kept patchable in unit tests)."""
    return sys.platform


def _validate_version(version: str) -> None:
    try:
        parsed = Version(version)
    except InvalidVersion, TypeError:
        raise _fail("The Documents version is invalid.") from None
    if str(parsed) != version:
        raise _fail("The Documents version is not canonical.")


def _validate_hash(value: object) -> None:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise _fail("A supplied SHA-256 value is invalid.")


def _validate_revision(revision: object) -> None:
    if not isinstance(revision, str) or _REVISION_RE.fullmatch(revision) is None:
        raise _fail("The source revision is invalid.")


def _absolute_path(path: Path) -> Path:
    try:
        return Path(os.path.abspath(os.fspath(path)))
    except OSError, TypeError, ValueError:
        raise _fail("A local build path is invalid.") from None


def _require_regular_file(path: Path) -> os.stat_result:
    try:
        status = path.lstat()
    except OSError:
        raise _fail("A required local input is unavailable.") from None
    if not stat.S_ISREG(status.st_mode):
        raise _fail("A required local input is not a regular file.")
    return status


def _require_source_file(root: Path, relative: str) -> Path:
    relative_path = PurePosixPath(relative)
    if relative_path.is_absolute() or any(
        part in {"", ".", ".."} for part in relative_path.parts
    ):
        raise _fail("A fixed source path is invalid.")

    current = root
    try:
        root_status = root.lstat()
    except OSError:
        raise _fail("The source root is unavailable.") from None
    if not stat.S_ISDIR(root_status.st_mode):
        raise _fail("The source root is not a real directory.")

    for part in relative_path.parts[:-1]:
        current = current / part
        try:
            status = current.lstat()
        except OSError:
            raise _fail("A fixed source input is unavailable.") from None
        if not stat.S_ISDIR(status.st_mode):
            raise _fail("A fixed source path traverses a non-directory.")

    source = root.joinpath(*relative_path.parts)
    _require_regular_file(source)
    return source


def _hash_regular_file(path: Path) -> tuple[str, int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise _fail("A required local input could not be opened safely.") from None

    try:
        opened_status = os.fstat(descriptor)
        if not stat.S_ISREG(opened_status.st_mode):
            raise _fail("A required local input is not a regular file.")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=True) as source:
            descriptor = -1
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest(), opened_status.st_size, opened_status
    except OSError:
        raise _fail("A required local input could not be read safely.") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_installer_header(path: Path) -> tuple[str, ...]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise _fail("The installer metadata is invalid.") from None

    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise _fail("The installer metadata is invalid.")
        lines: list[str] = []
        total_bytes = 0
        with os.fdopen(descriptor, "rb", closefd=True) as source:
            descriptor = -1
            for _line_number in range(_MAX_INSTALLER_HEADER_LINES):
                remaining = _MAX_INSTALLER_HEADER_BYTES - total_bytes
                if remaining <= 0:
                    raise _fail("The installer metadata is invalid.")
                raw_line = source.readline(remaining + 1)
                if not raw_line:
                    break
                total_bytes += len(raw_line)
                if total_bytes > _MAX_INSTALLER_HEADER_BYTES:
                    raise _fail("The installer metadata is invalid.")
                try:
                    lines.append(raw_line.decode("utf-8").rstrip("\r\n"))
                except UnicodeDecodeError:
                    raise _fail("The installer metadata is invalid.") from None
                if not raw_line.endswith(b"\n"):
                    break
        return tuple(lines)
    except OSError:
        raise _fail("The installer metadata is invalid.") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _validate_installer_metadata(
    path: Path, version: str, release_index_sha256: str
) -> None:
    expected = {
        "CARLINA_PROFILE": "documents",
        "VERSION": version,
        "INDEX_SHA256": release_index_sha256,
        "ARTIFACT_PLATFORM": "linux-64",
        "ARTIFACT_KIND": "pixi-pack-executable",
    }
    metadata: dict[str, str] = {}
    exact_profile_line_count = 0
    for line in _read_installer_header(path):
        if line == "export CARLINA_PROFILE=documents":
            exact_profile_line_count += 1
        try:
            tokens = shlex.split(line, comments=True, posix=True)
        except ValueError:
            raise _fail("The installer metadata is invalid.") from None
        if not tokens:
            continue
        if tokens[0] == "export":
            assignments = tokens[1:]
        elif len(tokens) == 1:
            assignments = tokens
        else:
            assignments = ()
        for assignment in assignments:
            name, separator, value = assignment.partition("=")
            if name not in expected:
                continue
            if not separator or name in metadata:
                raise _fail("The installer metadata is invalid.")
            metadata[name] = value

    if (
        exact_profile_line_count != 1
        or set(metadata) != set(expected)
        or metadata["ARTIFACT_KIND"]
        not in {"pixi-pack-executable", "publisher-bootstrap-executable"}
    ):
        raise _fail("The installer metadata is invalid.")

    expected["ARTIFACT_KIND"] = metadata["ARTIFACT_KIND"]
    if metadata != expected:
        raise _fail("The installer metadata is invalid.")


def _write_exclusive(path: Path, contents: bytes, mode: int = 0o644) -> None:
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
    except OSError:
        raise _fail("A staged file could not be created safely.") from None

    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as target:
            descriptor = -1
            target.write(contents)
    except OSError:
        raise _fail("A staged file could not be written safely.") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _copy_exclusive(source: Path, destination: Path, mode: int) -> None:
    source_flags = (
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    source_descriptor = -1
    destination_descriptor = -1
    try:
        source_descriptor = os.open(source, source_flags)
        source_status = os.fstat(source_descriptor)
        if not stat.S_ISREG(source_status.st_mode):
            raise _fail("A required local input is not a regular file.")
        destination_descriptor = os.open(
            destination,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
        os.fchmod(destination_descriptor, mode)
        with (
            os.fdopen(source_descriptor, "rb", closefd=True) as source_stream,
            os.fdopen(destination_descriptor, "wb", closefd=True) as destination_stream,
        ):
            source_descriptor = -1
            destination_descriptor = -1
            shutil.copyfileobj(source_stream, destination_stream, length=1024 * 1024)
    except DocumentsAppImageBuildError:
        raise
    except OSError:
        raise _fail("A staged file could not be copied safely.") from None
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if destination_descriptor >= 0:
            os.close(destination_descriptor)


def _git_output(root: Path, arguments: tuple[str, ...]) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", os.fspath(root), *arguments],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        raise _fail("The source Git metadata could not be read.") from None
    if result.returncode != 0:
        raise _fail("The source Git metadata could not be read.")
    return result.stdout.strip()


def _git_bytes(root: Path, arguments: tuple[str, ...], limit: int) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", os.fspath(root), *arguments],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        raise _fail("The source Git payload metadata could not be read.") from None
    if result.returncode != 0 or len(result.stdout) > limit:
        raise _fail("The source Git payload metadata could not be read.")
    return result.stdout


def _validate_source_payloads(
    root: Path, revision: str, payloads: dict[str, Path]
) -> None:
    expected_paths = tuple(sorted(payloads))
    tree_output = _git_bytes(
        root,
        ("ls-tree", "-z", revision, "--", *expected_paths),
        limit=8_192,
    )
    entries: dict[str, tuple[str, str, str]] = {}
    if tree_output and not tree_output.endswith(b"\x00"):
        raise _fail("The source Git payload metadata is malformed.")
    for raw_entry in tree_output.split(b"\x00"):
        if not raw_entry:
            continue
        try:
            raw_metadata, raw_path = raw_entry.split(b"\t", maxsplit=1)
            mode, kind, object_id = raw_metadata.decode("ascii").split(" ")
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError, ValueError:
            raise _fail("The source Git payload metadata is malformed.") from None
        if path not in payloads or path in entries:
            raise _fail("The source Git payload metadata is malformed.")
        if mode not in {"100644", "100755"} or kind != "blob":
            raise _fail(
                "A source payload is not a regular blob in the requested revision."
            )
        if _REVISION_RE.fullmatch(object_id) is None:
            raise _fail("The source Git payload metadata is malformed.")
        entries[path] = (mode, kind, object_id)

    if entries.keys() != payloads.keys():
        raise _fail("A source payload is absent from the requested revision.")

    for relative in expected_paths:
        tree_mode, _kind, tree_object_id = entries[relative]
        source = payloads[relative]
        status = _require_regular_file(source)
        working_mode = (
            "100755"
            if status.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            else "100644"
        )
        if working_mode != tree_mode:
            raise _fail("A source payload mode differs from the requested revision.")
        working_object_id = _git_bytes(
            root,
            ("hash-object", "--no-filters", "--", relative),
            limit=128,
        ).strip()
        if working_object_id.decode("ascii", errors="replace") != tree_object_id:
            raise _fail("A source payload differs from the requested revision.")


def _discover_bootstrap_notices(root: Path) -> tuple[str, ...]:
    current = root
    for part in PurePosixPath(_BOOTSTRAP_NOTICES_DIRECTORY).parts:
        current = current / part
        try:
            status = current.lstat()
        except OSError:
            raise _fail(
                "The pinned bootstrap notices directory is unavailable."
            ) from None
        if not stat.S_ISDIR(status.st_mode):
            raise _fail(
                "The pinned bootstrap notices directory is not a real directory."
            )

    try:
        entries = sorted(current.iterdir(), key=lambda path: path.name)
    except OSError:
        raise _fail("The pinned bootstrap notices could not be inventoried.") from None

    notice_paths: list[str] = []
    for entry in entries:
        if not entry.name.endswith(".txt"):
            raise _fail("The pinned bootstrap notices contain an unsupported entry.")
        try:
            status = entry.lstat()
        except OSError:
            raise _fail("A pinned bootstrap notice is unavailable.") from None
        if not stat.S_ISREG(status.st_mode):
            raise _fail("A pinned bootstrap notice is not a regular file.")
        relative = entry.relative_to(root).as_posix()
        _require_source_file(root, relative)
        notice_paths.append(relative)
    return tuple(notice_paths)


def _validate_source_checkout(root: Path, version: str, revision: str) -> int:
    head = _git_output(root, ("rev-parse", "--verify", "HEAD^{commit}"))
    if head != revision:
        raise _fail("The source HEAD does not match the requested revision.")
    status = _git_output(
        root,
        ("status", "--porcelain", "--untracked-files=no", "--ignore-submodules=none"),
    )
    if status:
        raise _fail("The source checkout has tracked changes.")

    try:
        with (root / "pyproject.toml").open("rb") as source:
            project = tomllib.load(source)
        with (root / "pixi.toml").open("rb") as source:
            workspace = tomllib.load(source)
    except OSError, UnicodeDecodeError, tomllib.TOMLDecodeError:
        raise _fail("The source version metadata could not be read.") from None

    project_metadata = project.get("project")
    workspace_metadata = workspace.get("workspace")
    if not isinstance(project_metadata, dict) or not isinstance(
        workspace_metadata, dict
    ):
        raise _fail("The source version metadata could not be read.")
    project_version = cast(dict[str, object], project_metadata).get("version")
    workspace_version = cast(dict[str, object], workspace_metadata).get("version")
    if project_version != version or workspace_version != version:
        raise _fail("The source version metadata does not match the requested version.")

    epoch_text = _git_output(root, ("show", "-s", "--format=%ct", revision))
    try:
        epoch = int(epoch_text)
    except ValueError:
        raise _fail("The source commit time is invalid.") from None
    if epoch < 0:
        raise _fail("The source commit time is invalid.")
    return epoch


def _resolve_link_target(link_path: tuple[str, ...], target: str) -> tuple[str, ...]:
    if not target or target.startswith("/") or "\\" in target:
        raise _fail("The Python archive contains an unsafe symbolic link.")
    result = list(link_path[:-1])
    for part in target.split("/"):
        if part in {"", "."}:
            if part == "":
                raise _fail("The Python archive contains an unsafe symbolic link.")
            continue
        if part == "..":
            if len(result) <= 1:
                raise _fail("The Python archive contains an unsafe symbolic link.")
            result.pop()
            continue
        result.append(part)
    if not result or result[0] != "python":
        raise _fail("The Python archive contains an unsafe symbolic link.")
    return tuple(result)


def _archive_member_path(member: tarfile.TarInfo) -> tuple[str, ...]:
    raw_name = member.name
    if not raw_name or raw_name.startswith("/") or "\\" in raw_name:
        raise _fail("The Python archive contains an unsafe path.")
    if raw_name.endswith("/"):
        if not member.isdir():
            raise _fail("The Python archive contains an unsafe path.")
        raw_name = raw_name[:-1]
    parts = tuple(raw_name.split("/"))
    if any(part in {"", ".", ".."} for part in parts) or parts[0] != "python":
        raise _fail("The Python archive contains an unsafe path.")
    return parts


def _preflight_python_archive(
    archive: tarfile.TarFile,
) -> tuple[list[tarfile.TarInfo], set[tuple[str, ...]]]:
    try:
        members: list[tarfile.TarInfo] = []
        for member in archive:
            members.append(member)
            if len(members) > _MAX_ARCHIVE_MEMBERS:
                raise _fail("The Python archive exceeds its member limit.")
    except DocumentsAppImageBuildError:
        raise
    except OSError, tarfile.TarError:
        raise _fail("The Python archive could not be inspected safely.") from None

    paths: set[tuple[str, ...]] = set()
    symlink_paths: set[tuple[str, ...]] = set()
    expanded_bytes = 0
    for member in members:
        path = _archive_member_path(member)
        if path in paths:
            raise _fail("The Python archive contains duplicate paths.")
        paths.add(path)

        if member.mode & (stat.S_ISUID | stat.S_ISGID):
            raise _fail("The Python archive contains privileged file modes.")

        if member.isdir():
            if member.size != 0:
                raise _fail("The Python archive contains an invalid directory.")
        elif member.isfile() and member.type in {tarfile.REGTYPE, tarfile.AREGTYPE}:
            if member.size < 0:
                raise _fail("The Python archive contains an invalid file size.")
            expanded_bytes += member.size
            if expanded_bytes > _MAX_ARCHIVE_BYTES:
                raise _fail("The Python archive exceeds its expanded-size limit.")
        elif member.issym():
            if member.size != 0:
                raise _fail("The Python archive contains an invalid symbolic link.")
            _resolve_link_target(path, member.linkname)
            symlink_paths.add(path)
        else:
            # This rejects hard links, devices, FIFOs and non-regular tar types.
            raise _fail("The Python archive contains an unsupported member type.")

    root_member = next(
        (member for member in members if _archive_member_path(member) == ("python",)),
        None,
    )
    if root_member is not None and not root_member.isdir():
        raise _fail("The Python archive root is not a directory.")

    for path in paths:
        for index in range(1, len(path)):
            if path[:index] in symlink_paths:
                raise _fail("The Python archive writes through a symbolic link.")

    return members, paths


def _extract_python_archive(archive_path: Path, stage: Path) -> None:
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            _preflight_python_archive(archive)
            archive.extractall(path=stage, filter="data")
    except DocumentsAppImageBuildError:
        raise
    except OSError, tarfile.TarError, ValueError, EOFError:
        raise _fail("The Python archive could not be extracted safely.") from None


def _safe_python_interpreter(stage: Path) -> Path:
    current = ("python", "bin", "python3")
    visited: set[tuple[str, ...]] = set()

    for _ in range(40):
        if current in visited:
            raise _fail("The Python interpreter link chain is invalid.")
        visited.add(current)

        redirected = False
        for index in range(1, len(current)):
            candidate_parts = current[: index + 1]
            candidate = stage.joinpath(*candidate_parts)
            try:
                status = candidate.lstat()
            except OSError:
                raise _fail("The Python archive has no usable interpreter.") from None
            if stat.S_ISLNK(status.st_mode):
                try:
                    target = os.readlink(candidate)
                except OSError:
                    raise _fail("The Python interpreter link is invalid.") from None
                resolved = _resolve_link_target(candidate_parts, target)
                current = (*resolved, *current[index + 1 :])
                redirected = True
                break
            if index < len(current) - 1 and not stat.S_ISDIR(status.st_mode):
                raise _fail("The Python interpreter path is invalid.")
            if index == len(current) - 1:
                if not stat.S_ISREG(status.st_mode) or not (
                    status.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                ):
                    raise _fail("The Python archive has no executable interpreter.")
                return candidate
        if not redirected:
            break

    raise _fail("The Python interpreter link chain is invalid.")


def _require_python_stdlib(stage: Path) -> None:
    library_root = stage / "python" / "lib"
    try:
        library_status = library_root.lstat()
    except OSError:
        raise _fail("The Python archive has no standard library.") from None
    if not stat.S_ISDIR(library_status.st_mode):
        raise _fail("The Python archive has no standard library.")

    try:
        versions = sorted(library_root.iterdir(), key=lambda path: path.name)
    except OSError:
        raise _fail("The Python archive has no standard library.") from None
    for version_directory in versions:
        if not version_directory.name.startswith("python3."):
            continue
        try:
            version_status = version_directory.lstat()
            encoding_status = (version_directory / "encodings" / "__init__.py").lstat()
        except OSError:
            continue
        if stat.S_ISDIR(version_status.st_mode) and stat.S_ISREG(
            encoding_status.st_mode
        ):
            return
    raise _fail("The Python archive has no standard library.")


def _drain_pipe(stream: BinaryIO, result: bytearray) -> None:
    try:
        while chunk := stream.read(8192):
            remaining = _PROCESS_CAPTURE_BYTES - len(result)
            if remaining > 0:
                result.extend(chunk[:remaining])
    except OSError:
        return


def _run_appimagetool(
    *,
    appimagetool: Path,
    runtime: Path,
    stage: Path,
    output: Path,
    epoch: int,
) -> None:
    command = [
        os.fspath(appimagetool),
        *_APPIMAGE_ARGUMENTS,
        "--runtime-file",
        os.fspath(runtime),
        os.fspath(stage),
        os.fspath(output),
    ]
    environment = os.environ.copy()
    environment.update(
        {
            "ARCH": "x86_64",
            "SOURCE_DATE_EPOCH": str(epoch),
            "APPIMAGE_EXTRACT_AND_RUN": "1",
        }
    )

    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=stage.parent,
            env=environment,
        )
    except OSError:
        raise _fail("The AppImage builder could not be started.") from None

    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    if process.stdout is None or process.stderr is None:
        process.kill()
        process.wait()
        raise _fail("The AppImage builder output could not be captured.")
    readers = (
        threading.Thread(
            target=_drain_pipe, args=(process.stdout, stdout_buffer), daemon=True
        ),
        threading.Thread(
            target=_drain_pipe, args=(process.stderr, stderr_buffer), daemon=True
        ),
    )
    try:
        for reader in readers:
            reader.start()
        return_code = process.wait()
    except BaseException:
        process.kill()
        process.wait()
        raise
    finally:
        for reader in readers:
            if reader.ident is not None:
                reader.join()
        process.stdout.close()
        process.stderr.close()

    # Retain only bounded diagnostics in memory; subprocess text is never echoed.
    _ = bytes(stdout_buffer), bytes(stderr_buffer)
    if return_code != 0:
        error = _fail("The AppImage builder failed.")
        error.diagnostics = {
            "stdout": stdout_buffer.decode("utf-8", errors="replace"),
            "stderr": stderr_buffer.decode("utf-8", errors="replace"),
        }
        raise error


def _check_stage_root(stage: Path) -> None:
    try:
        members = frozenset(entry.name for entry in stage.iterdir())
    except OSError:
        raise _fail("The AppImage stage could not be inspected.") from None
    if members != _STAGE_ROOT_MEMBERS:
        raise _fail("The AppImage stage contains unexpected files.")


def _inspect_appimage(path: Path) -> tuple[str, int]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise _fail("The AppImage builder produced no regular output.") from None
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise _fail("The AppImage builder produced no regular output.")
        if not status.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
            raise _fail("The generated AppImage is not executable.")
        with os.fdopen(descriptor, "rb", closefd=True) as artifact:
            descriptor = -1
            header = artifact.read(11)
            if (
                len(header) < 11
                or header[:4] != b"\x7fELF"
                or header[8:11] != b"AI\x02"
            ):
                raise _fail("The generated file is not a type-2 AppImage.")
            digest = hashlib.sha256(header)
            while chunk := artifact.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest(), status.st_size
    except OSError:
        raise _fail("The generated AppImage could not be read safely.") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _path_exists_without_following(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        raise _fail("The output directory could not be inspected safely.") from None
    return True


def _remove_if_same_file(path: Path, reference: Path) -> None:
    try:
        path_status = path.lstat()
        reference_status = reference.lstat()
        if (path_status.st_dev, path_status.st_ino) == (
            reference_status.st_dev,
            reference_status.st_ino,
        ):
            path.unlink()
    except OSError:
        # Best effort rollback is limited to a path still naming our exact inode.
        return


def _publish_pair(
    artifact: Path,
    sidecar: Path,
    destination: Path,
    sidecar_destination: Path,
) -> None:
    linked_artifact = False
    try:
        os.link(artifact, destination, follow_symlinks=False)
        linked_artifact = True
        os.link(sidecar, sidecar_destination, follow_symlinks=False)
    except FileExistsError:
        if linked_artifact:
            _remove_if_same_file(destination, artifact)
        raise _fail("An AppImage output path already exists.") from None
    except OSError:
        if linked_artifact:
            _remove_if_same_file(destination, artifact)
        raise _fail("The AppImage outputs could not be published safely.") from None


def build_appimage(
    *,
    root: Path,
    output_dir: Path,
    version: str,
    source_revision: str,
    python_archive: Path,
    python_sha256: str,
    appimagetool: Path,
    appimagetool_sha256: str,
    runtime: Path,
    runtime_sha256: str,
    installer: Path,
    installer_sha256: str,
    release_index_sha256: str,
) -> dict[str, object]:
    """Build one exact AppImage and its artifact sidecar without overwriting."""
    _require_linux_x86_64()
    _validate_version(version)
    _validate_revision(source_revision)
    for digest in (
        python_sha256,
        appimagetool_sha256,
        runtime_sha256,
        installer_sha256,
        release_index_sha256,
    ):
        _validate_hash(digest)
    python_sha256 = python_sha256.lower()
    appimagetool_sha256 = appimagetool_sha256.lower()
    runtime_sha256 = runtime_sha256.lower()
    installer_sha256 = installer_sha256.lower()
    release_index_sha256 = release_index_sha256.lower()

    source_root = _absolute_path(root)
    output_root = _absolute_path(output_dir)
    archive_path = _absolute_path(python_archive)
    tool_path = _absolute_path(appimagetool)
    runtime_path = _absolute_path(runtime)
    installer_path = _absolute_path(installer)

    external_inputs = (
        (archive_path, python_sha256),
        (tool_path, appimagetool_sha256),
        (runtime_path, runtime_sha256),
        (installer_path, installer_sha256),
    )
    for path, _expected in external_inputs:
        _require_regular_file(path)
    tool_status = _require_regular_file(tool_path)
    if not tool_status.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        raise _fail("The AppImage builder is not executable.")

    for path, expected in external_inputs:
        actual, _size, _status = _hash_regular_file(path)
        if actual != expected:
            raise _fail("A supplied input SHA-256 does not match its file.")
    _validate_installer_metadata(
        installer_path,
        version,
        release_index_sha256,
    )

    fixed_sources = {
        relative: _require_source_file(source_root, relative)
        for relative in _SOURCE_BOUND_PATHS
    }
    bootstrap_notice_paths = _discover_bootstrap_notices(source_root)
    for relative in bootstrap_notice_paths:
        fixed_sources[relative] = _require_source_file(source_root, relative)
    _require_source_file(source_root, "pyproject.toml")
    _require_source_file(source_root, "pixi.toml")
    epoch = _validate_source_checkout(source_root, version, source_revision)
    _validate_source_payloads(source_root, source_revision, fixed_sources)

    destination = output_root / f"Carlina_Documents-{version}-x86_64.AppImage"
    sidecar_destination = Path(os.fspath(destination) + ".artifact.json")
    try:
        output_root.mkdir(parents=True, exist_ok=True)
        output_status = output_root.lstat()
    except OSError:
        raise _fail("The output directory could not be created.") from None
    if not stat.S_ISDIR(output_status.st_mode):
        raise _fail("The output path is not a real directory.")
    if _path_exists_without_following(destination) or _path_exists_without_following(
        sidecar_destination
    ):
        raise _fail("An AppImage output path already exists.")

    try:
        with tempfile.TemporaryDirectory(
            prefix=".carlina-documents-appimage-", dir=output_root
        ) as temporary_directory:
            work_root = Path(temporary_directory)
            stage = work_root / "stage"
            stage.mkdir(mode=0o755)

            for relative, destinations, mode in _SOURCE_COPIES:
                source = fixed_sources[relative]
                for name in destinations:
                    _copy_exclusive(source, stage / name, mode)

            license_directory = stage / "licenses"
            license_directory.mkdir(mode=0o755)
            for license_name in _LICENSE_FILES:
                _copy_exclusive(
                    fixed_sources[license_name],
                    license_directory / license_name,
                    0o644,
                )
            bootstrap_license_directory = license_directory / "bootstrap"
            bootstrap_license_directory.mkdir(mode=0o755)
            bootstrap_notices_directory = bootstrap_license_directory / "notices"
            bootstrap_notices_directory.mkdir(mode=0o755)
            bootstrap_notice_sources = (_BOOTSTRAP_NOTICE, *bootstrap_notice_paths)
            for relative in bootstrap_notice_sources:
                relative_destination = (
                    bootstrap_license_directory / "NOTICE.json"
                    if relative == _BOOTSTRAP_NOTICE
                    else bootstrap_notices_directory / Path(relative).name
                )
                _copy_exclusive(
                    fixed_sources[relative],
                    relative_destination,
                    0o644,
                )

            _copy_exclusive(installer_path, stage / "installer.sh", 0o700)
            launcher = {
                "schema_version": 1,
                "product": "carlina-documents",
                "version": version,
                "source_revision": source_revision,
                "installer_sha256": installer_sha256,
                "release_index_sha256": release_index_sha256,
                "bootstrap_python_sha256": python_sha256,
            }
            _write_exclusive(
                stage / "launcher.json",
                (
                    json.dumps(launcher, sort_keys=True, separators=(",", ":")) + "\n"
                ).encode("utf-8"),
            )

            _extract_python_archive(archive_path, stage)
            _safe_python_interpreter(stage)
            _require_python_stdlib(stage)
            _check_stage_root(stage)

            candidate = work_root / "candidate.AppImage"
            _run_appimagetool(
                appimagetool=tool_path,
                runtime=runtime_path,
                stage=stage,
                output=candidate,
                epoch=epoch,
            )
            appimage_sha256, appimage_bytes = _inspect_appimage(candidate)
            metadata: dict[str, object] = {
                "sha256": appimage_sha256,
                "bytes": appimage_bytes,
                "version": version,
                "profile": "documents",
                "source_revision": source_revision,
                "input_hashes": {
                    "python_archive": python_sha256,
                    "appimagetool": appimagetool_sha256,
                    "runtime": runtime_sha256,
                    "installer": installer_sha256,
                    "release_index": release_index_sha256,
                },
                "kind": "appimage",
                "source_date_epoch": epoch,
            }
            sidecar_candidate = work_root / "candidate.artifact.json"
            _write_exclusive(
                sidecar_candidate,
                (
                    json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n"
                ).encode("utf-8"),
            )
            _publish_pair(
                candidate,
                sidecar_candidate,
                destination,
                sidecar_destination,
            )
            return metadata
    except DocumentsAppImageBuildError:
        raise
    except OSError, tarfile.TarError, ValueError, EOFError:
        raise _fail("The Documents AppImage could not be built safely.") from None


def main(argv: list[str] | None = None) -> int:
    """Build with explicit local, hash-pinned inputs; never fetch latest tools."""
    parser = argparse.ArgumentParser(description="Build the Documents Linux AppImage.")
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--installer", type=Path, required=True)
    parser.add_argument("--installer-sha256", required=True)
    parser.add_argument("--index-sha256", required=True)
    parser.add_argument("--python-archive", type=Path, required=True)
    parser.add_argument("--python-sha256", required=True)
    parser.add_argument("--appimagetool", type=Path, required=True)
    parser.add_argument("--tool-sha256", required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--runtime-sha256", required=True)
    arguments = parser.parse_args(argv)
    try:
        metadata = build_appimage(
            root=arguments.root,
            output_dir=arguments.output_dir,
            version=arguments.version,
            source_revision=arguments.revision,
            installer=arguments.installer,
            installer_sha256=arguments.installer_sha256,
            release_index_sha256=arguments.index_sha256,
            python_archive=arguments.python_archive,
            python_sha256=arguments.python_sha256,
            appimagetool=arguments.appimagetool,
            appimagetool_sha256=arguments.tool_sha256,
            runtime=arguments.runtime,
            runtime_sha256=arguments.runtime_sha256,
        )
    except DocumentsAppImageBuildError as exc:
        print(str(exc), file=sys.stderr)
        if exc.diagnostics:
            # Build-tool output contains fixed packaging inputs, never account
            # credentials. Keep it bounded for useful local build diagnostics.
            print(json.dumps(exc.diagnostics), file=sys.stderr)
        return 1
    print(json.dumps(metadata, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
