"""Build a provenance-bound Windows Carlina Documents setup launcher."""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import stat
import struct
import subprocess
import sys
import tempfile
import threading
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Final, cast

from packaging.version import Version

_REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
if os.fspath(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, os.fspath(_REPOSITORY_ROOT))

from scripts import build_documents_appimage as _source_integrity  # noqa: E402
from scripts.build_documents_appimage import (  # noqa: E402
    DocumentsAppImageBuildError as WindowsBuildError,
)

_EXPECTED_PYINSTALLER_VERSION: Final = "6.22.3"
_PROCESS_CAPTURE_BYTES: Final = 4_096
_PROCESS_TIMEOUT_SECONDS: Final = 600
_MAX_INSTALLER_HEADER_BYTES: Final = 64 * 1024
_MAX_INSTALLER_HEADER_LINES: Final = 80
_MAX_NOTICE_FILES: Final = 512
_MAX_NOTICE_BYTES: Final = 5 * 1024 * 1024
_HASH_RE: Final = re.compile(r"[0-9a-fA-F]{64}\Z")
_LOWER_HASH_RE: Final = re.compile(r"[0-9a-f]{64}\Z")
_ASSIGNMENT_SEARCH_RE: Final = re.compile(
    r"\$(version|indexSha256|bootstrapIdentity|bootstrapIdentityBase64)\s*=",
    re.IGNORECASE,
)
_ASSIGNMENT_LINE_RE: Final = re.compile(
    r"\s*\$(version|indexSha256|bootstrapIdentity|bootstrapIdentityBase64)"
    r"\s*=\s*'([^']*)'\s*;?\s*",
    re.IGNORECASE,
)
_LICENSE_FILES: Final = (
    "LICENSE",
    "COPYING-AGPL-3.0.txt",
    "THIRD_PARTY_NOTICES.md",
)
_BOOTSTRAP_NOTICE: Final = "third_party/windows-bootstrap/NOTICE.json"
_BOOTSTRAP_NOTICES_DIRECTORY: Final = "third_party/windows-bootstrap/notices"
_ICON_SOURCE: Final = "packaging/windows/carlina-documents.ico"
_REQUIREMENTS_SOURCE: Final = "packaging/windows/requirements-build.lock"
_FIXED_SOURCE_PATHS: Final = (
    "scripts/documents_appimage.py",
    "scripts/build_documents_windows_installer.py",
    "scripts/build_documents_appimage.py",
    _ICON_SOURCE,
    _REQUIREMENTS_SOURCE,
    *_LICENSE_FILES,
    _BOOTSTRAP_NOTICE,
    "pyproject.toml",
    "pixi.toml",
)
_IDENTITY_KEYS: Final = frozenset(
    {
        "application_profile",
        "release_version",
        "channel",
        "index_sha256",
        "artifact_platform",
        "artifact_kind",
        "artifact_filename",
        "artifact_sha256",
        "artifact_size",
    }
)

DocumentsWindowsInstallerBuildError = WindowsBuildError


def _fail(message: str, diagnostics: dict[str, str] | None = None) -> WindowsBuildError:
    error = WindowsBuildError(message)
    if diagnostics is not None:
        error.diagnostics = diagnostics
    return error


def _require_windows_build_host() -> None:
    if sys.platform != "win32":
        raise _fail("Windows installer builds require a native Windows host.")
    if platform.machine().lower() not in {"amd64", "x86_64"}:
        raise _fail("Windows installer builds require x86-64 Windows.")
    if struct.calcsize("P") != 8 or sys.maxsize <= 2**32:
        raise _fail("Windows installer builds require 64-bit Python.")
    if platform.python_implementation() != "CPython" or sys.version_info[:2] != (
        3,
        14,
    ):
        raise _fail("Windows installer builds require CPython 3.14.")


def _build_tool_versions() -> dict[str, str]:
    try:
        pyinstaller_version = importlib.metadata.version("pyinstaller")
        hooks_version = importlib.metadata.version("pyinstaller-hooks-contrib")
    except importlib.metadata.PackageNotFoundError:
        raise _fail("The pinned Windows build tools are not installed.") from None
    if pyinstaller_version != _EXPECTED_PYINSTALLER_VERSION:
        raise _fail("Windows installer builds require PyInstaller 6.22.3.")
    return {
        "python": platform.python_version(),
        "pyinstaller": pyinstaller_version,
        "pyinstaller_hooks_contrib": hooks_version,
    }


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


def _hash_regular_file(path: Path) -> tuple[str, int, os.stat_result]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
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


def _require_source_directory(root: Path, relative: str) -> Path:
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

    for part in relative_path.parts:
        current = current / part
        try:
            status = current.lstat()
        except OSError:
            raise _fail("A fixed source directory is unavailable.") from None
        if not stat.S_ISDIR(status.st_mode):
            raise _fail("A fixed source path traverses a non-directory.")
    return current


def _discover_bootstrap_notices(root: Path) -> tuple[str, ...]:
    notices_root = _require_source_directory(root, _BOOTSTRAP_NOTICES_DIRECTORY)
    notice_paths: list[str] = []
    total_bytes = 0
    try:
        for current_text, directory_names, file_names in os.walk(
            notices_root, topdown=True, followlinks=False
        ):
            current = Path(current_text)
            directory_names.sort()
            file_names.sort()
            for name in directory_names:
                try:
                    status = (current / name).lstat()
                except OSError:
                    raise _fail("A bootstrap notice entry is unavailable.") from None
                if not stat.S_ISDIR(status.st_mode):
                    raise _fail(
                        "The bootstrap notices contain a symlink or special file."
                    )
            for name in file_names:
                path = current / name
                try:
                    status = path.lstat()
                except OSError:
                    raise _fail("A bootstrap notice entry is unavailable.") from None
                if not stat.S_ISREG(status.st_mode):
                    raise _fail(
                        "The bootstrap notices contain a symlink or special file."
                    )
                if path.suffix != ".txt":
                    continue
                relative = path.relative_to(root).as_posix()
                _source_integrity.require_source_file(root, relative)
                notice_paths.append(relative)
                total_bytes += status.st_size
                if len(notice_paths) > _MAX_NOTICE_FILES:
                    raise _fail("The bootstrap notices exceed the file-count limit.")
                if total_bytes > _MAX_NOTICE_BYTES:
                    raise _fail("The bootstrap notices exceed the byte limit.")
    except OSError:
        raise _fail("The pinned bootstrap notices could not be inventoried.") from None
    return tuple(notice_paths)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _read_installer_header(path: Path) -> tuple[str, ...]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise _fail("The installer metadata is invalid.") from None

    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise _fail("The installer metadata is invalid.")
        lines: list[str] = []
        total_bytes = 0
        with os.fdopen(descriptor, "rb", closefd=True) as source:
            descriptor = -1
            for _line_number in range(_MAX_INSTALLER_HEADER_LINES):
                remaining = _MAX_INSTALLER_HEADER_BYTES - total_bytes
                if remaining <= 0:
                    break
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
    assignments: dict[str, str] = {}
    canonical_names = {
        "version": "version",
        "indexsha256": "indexSha256",
        "bootstrapidentity": "bootstrapIdentity",
        "bootstrapidentitybase64": "bootstrapIdentityBase64",
    }
    for line in _read_installer_header(path):
        if line.lstrip().startswith("#"):
            continue
        if not _ASSIGNMENT_SEARCH_RE.search(line):
            continue
        match = _ASSIGNMENT_LINE_RE.fullmatch(line)
        if match is None:
            raise _fail("The installer metadata is invalid.")
        key = match.group(1).casefold()
        name = canonical_names[key]
        if match.group(1) != name or name in assignments:
            raise _fail("The installer metadata is invalid.")
        assignments[name] = match.group(2)

    expected_names = {
        "version",
        "indexSha256",
        "bootstrapIdentity",
        "bootstrapIdentityBase64",
    }
    if assignments.keys() != expected_names:
        raise _fail("The installer metadata is invalid.")
    if (
        assignments["version"] != version
        or assignments["indexSha256"] != release_index_sha256
        or _LOWER_HASH_RE.fullmatch(assignments["bootstrapIdentity"]) is None
    ):
        raise _fail("The installer metadata is invalid.")

    encoded_identity = assignments["bootstrapIdentityBase64"]
    try:
        identity_bytes = base64.b64decode(encoded_identity, validate=True)
        if base64.b64encode(identity_bytes).decode("ascii") != encoded_identity:
            raise ValueError("non-canonical base64")
        parsed_identity: object = json.loads(
            identity_bytes.decode("utf-8"), object_pairs_hook=_unique_json_object
        )
    except UnicodeDecodeError, ValueError, json.JSONDecodeError:
        raise _fail("The installer metadata is invalid.") from None
    if not isinstance(parsed_identity, dict):
        raise _fail("The installer metadata is invalid.")
    identity = cast(dict[str, object], parsed_identity)
    if identity.keys() != _IDENTITY_KEYS:
        raise _fail("The installer metadata is invalid.")

    if (
        identity.get("application_profile") != "documents"
        or identity.get("release_version") != version
        or not isinstance(identity.get("channel"), str)
        or identity.get("channel") not in {"stable", "preview"}
        or identity.get("index_sha256") != release_index_sha256
        or identity.get("artifact_platform") != "win-64"
        or identity.get("artifact_kind") != "publisher-bootstrap-executable"
    ):
        raise _fail("The installer metadata is invalid.")

    filename = identity.get("artifact_filename")
    artifact_sha256 = identity.get("artifact_sha256")
    artifact_size = identity.get("artifact_size")
    if (
        not isinstance(filename, str)
        or not filename
        or len(filename) > 255
        or filename in {".", ".."}
        or any(character in filename for character in ("/", "\\", "\0", "\r", "\n"))
        or not isinstance(artifact_sha256, str)
        or _LOWER_HASH_RE.fullmatch(artifact_sha256) is None
        or type(artifact_size) is not int
        or artifact_size <= 0
    ):
        raise _fail("The installer metadata is invalid.")

    canonical_identity = json.dumps(
        identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    if (
        canonical_identity != identity_bytes
        or hashlib.sha256(identity_bytes).hexdigest()
        != assignments["bootstrapIdentity"]
    ):
        raise _fail("The installer metadata is invalid.")


def _copy_file_exclusive(
    source_path: Path,
    destination_path: Path,
    mode: int,
    expected_sha256: str | None = None,
) -> tuple[str, int]:
    source_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    destination_flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    source_descriptor = -1
    destination_descriptor = -1
    try:
        source_descriptor = os.open(source_path, source_flags)
        if not stat.S_ISREG(os.fstat(source_descriptor).st_mode):
            raise _fail("A required local input is not a regular file.")
        destination_descriptor = os.open(destination_path, destination_flags, mode)
        digest = hashlib.sha256()
        total_bytes = 0
        with (
            os.fdopen(source_descriptor, "rb", closefd=True) as source,
            os.fdopen(destination_descriptor, "wb", closefd=True) as destination,
        ):
            source_descriptor = -1
            destination_descriptor = -1
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                total_bytes += len(chunk)
                destination.write(chunk)
        result = digest.hexdigest()
        if expected_sha256 is not None and result != expected_sha256:
            raise _fail("A staged source differs from its verified input.")
        return result, total_bytes
    except WindowsBuildError:
        raise
    except OSError:
        raise _fail("A staged file could not be copied safely.") from None
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if destination_descriptor >= 0:
            os.close(destination_descriptor)


def _write_exclusive(path: Path, contents: bytes, mode: int = 0o644) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        descriptor = os.open(path, flags, mode)
    except OSError:
        raise _fail("A staged file could not be created safely.") from None
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as target:
            descriptor = -1
            target.write(contents)
    except OSError:
        raise _fail("A staged file could not be written safely.") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _drain_pipe(stream: BinaryIO, result: bytearray) -> None:
    while True:
        chunk = stream.read(1024)
        if not chunk:
            return
        remaining = _PROCESS_CAPTURE_BYTES - len(result)
        if remaining > 0:
            result.extend(chunk[:remaining])


def _redact_diagnostic(
    payload: bytearray,
    paths: tuple[Path, ...],
) -> str:
    text = payload.decode("utf-8", errors="replace")
    for path in sorted(paths, key=lambda item: len(os.fspath(item)), reverse=True):
        text = text.replace(os.fspath(path), "<local-path>")
    return text[:_PROCESS_CAPTURE_BYTES]


def _run_pyinstaller(
    command: list[str],
    *,
    working_directory: Path,
    environment: dict[str, str],
    private_paths: tuple[Path, ...],
) -> None:
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=working_directory,
            env=environment,
        )
    except OSError:
        raise _fail("PyInstaller could not be started.") from None

    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    if process.stdout is None or process.stderr is None:
        process.kill()
        process.wait()
        raise _fail("PyInstaller output could not be captured.")
    readers = (
        threading.Thread(
            target=_drain_pipe, args=(process.stdout, stdout_buffer), daemon=True
        ),
        threading.Thread(
            target=_drain_pipe, args=(process.stderr, stderr_buffer), daemon=True
        ),
    )
    timed_out = False
    try:
        for reader in readers:
            reader.start()
        try:
            return_code = process.wait(timeout=_PROCESS_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
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

    if timed_out or return_code != 0:
        error = _fail(
            "PyInstaller exceeded its build timeout."
            if timed_out
            else "PyInstaller failed to build the Windows launcher."
        )
        error.diagnostics = {
            "stdout": _redact_diagnostic(stdout_buffer, private_paths),
            "stderr": _redact_diagnostic(stderr_buffer, private_paths),
        }
        raise error


def _version_resource(version: str) -> bytes:
    parsed = Version(version)
    numeric_parts = list(parsed.release[:3])
    numeric_parts.extend([0] * (3 - len(numeric_parts)))
    numeric_version = (*numeric_parts[:3], 0)
    if any(component > 0xFFFF for component in numeric_version):
        raise _fail("The Documents version is too large for a Windows resource.")
    number_tuple = "(" + ", ".join(map(str, numeric_version)) + ")"
    strings = {
        "CompanyName": "Carlina",
        "FileDescription": "Install Carlina Documents",
        "FileVersion": version,
        "ProductName": "Carlina Documents",
        "ProductVersion": version,
    }
    string_structs = ",\n".join(
        f"        StringStruct({json.dumps(key)}, {json.dumps(value)})"
        for key, value in strings.items()
    )
    source = f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={number_tuple},
    prodvers={number_tuple},
    mask=0x3f,
    flags=0x0,
    OS=0x4,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
{string_structs}
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    return source.encode("utf-8")


def _inspect_pe_executable(path: Path) -> tuple[str, int]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise _fail("PyInstaller produced no regular executable.") from None

    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_size < 64:
            raise _fail("PyInstaller produced no regular executable.")
        with os.fdopen(descriptor, "rb", closefd=True) as executable:
            descriptor = -1
            dos_header = executable.read(64)
            if dos_header[:2] != b"MZ":
                raise _fail("The generated launcher is not a Windows executable.")
            pe_offset = struct.unpack_from("<I", dos_header, 0x3C)[0]
            if pe_offset < 64 or pe_offset + 24 > status.st_size:
                raise _fail("The generated launcher has an invalid PE header.")
            executable.seek(pe_offset)
            pe_and_coff = executable.read(24)
            if len(pe_and_coff) != 24 or pe_and_coff[:4] != b"PE\0\0":
                raise _fail("The generated launcher has an invalid PE header.")
            (
                machine,
                _sections,
                _timestamp,
                _symbols,
                _symbol_count,
                optional_size,
                _flags,
            ) = struct.unpack_from("<HHIIIHH", pe_and_coff, 4)
            if machine != 0x8664 or optional_size < 70:
                raise _fail("The generated launcher is not x86-64 Windows PE32+.")
            if pe_offset + 24 + optional_size > status.st_size:
                raise _fail("The generated launcher has an invalid PE header.")
            optional_header = executable.read(optional_size)
            if (
                len(optional_header) != optional_size
                or struct.unpack_from("<H", optional_header, 0)[0] != 0x20B
                or struct.unpack_from("<H", optional_header, 68)[0] != 2
            ):
                raise _fail("The generated launcher is not a GUI PE32+ executable.")

            digest = hashlib.sha256()
            executable.seek(0)
            while chunk := executable.read(1024 * 1024):
                digest.update(chunk)
            return digest.hexdigest(), status.st_size
    except OSError:
        raise _fail("The generated launcher could not be inspected safely.") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _path_exists_without_following(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        raise _fail("An output path could not be inspected safely.") from None
    return True


def _same_regular_file(first: Path, second: Path) -> bool:
    try:
        first_status = first.lstat()
        second_status = second.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(first_status.st_mode)
        and stat.S_ISREG(second_status.st_mode)
        and os.path.samestat(first_status, second_status)
    )


def _publish_outputs(
    candidates: tuple[Path, Path, Path], destinations: tuple[Path, Path, Path]
) -> None:
    published: list[tuple[Path, Path]] = []
    try:
        for candidate, destination in zip(candidates, destinations, strict=True):
            os.link(candidate, destination)
            published.append((candidate, destination))
    except FileExistsError:
        _remove_owned_outputs(published)
        raise _fail("A Windows installer output path already exists.") from None
    except OSError:
        _remove_owned_outputs(published)
        raise _fail(
            "The Windows installer outputs could not be published safely."
        ) from None


def _remove_owned_outputs(published: list[tuple[Path, Path]]) -> None:
    for candidate, destination in reversed(published):
        if _same_regular_file(candidate, destination):
            with suppress(OSError):
                destination.unlink()


def _source_hashes(source_files: dict[str, Path]) -> dict[str, str]:
    result: dict[str, str] = {}
    for relative, path in source_files.items():
        digest, _size, _status = _hash_regular_file(path)
        result[relative] = digest
    return result


def build_windows_installer(
    *,
    root: Path,
    version: str,
    source_revision: str,
    output_dir: Path,
    installer: Path,
    installer_sha256: str,
    release_index_sha256: str,
) -> tuple[Path, Path, Path]:
    """Build an unsigned Windows x64 setup launcher and provenance files."""
    _require_windows_build_host()
    build_tools = _build_tool_versions()
    _source_integrity.validate_version(version)
    _source_integrity.validate_revision(source_revision)
    _source_integrity.validate_hash(installer_sha256)
    _source_integrity.validate_hash(release_index_sha256)
    installer_sha256 = installer_sha256.lower()
    release_index_sha256 = release_index_sha256.lower()

    source_root = _absolute_path(root)
    output_root = _absolute_path(output_dir)
    installer_path = _absolute_path(installer)
    _require_regular_file(installer_path)
    actual_installer_sha256, _installer_bytes, _installer_status = _hash_regular_file(
        installer_path
    )
    if actual_installer_sha256 != installer_sha256:
        raise _fail("The supplied installer SHA-256 does not match its file.")
    _validate_installer_metadata(installer_path, version, release_index_sha256)

    source_files = {
        relative: _source_integrity.require_source_file(source_root, relative)
        for relative in _FIXED_SOURCE_PATHS
    }
    notice_paths = _discover_bootstrap_notices(source_root)
    for relative in notice_paths:
        source_files[relative] = _source_integrity.require_source_file(
            source_root, relative
        )

    source_date_epoch = _source_integrity.validate_source_checkout(
        source_root, version, source_revision
    )
    _source_integrity.validate_source_payloads(
        source_root, source_revision, source_files
    )
    source_sha256 = _source_hashes(source_files)
    icon_sha256 = source_sha256[_ICON_SOURCE]
    requirements_sha256 = source_sha256[_REQUIREMENTS_SOURCE]
    notice_catalog_sha256 = source_sha256[_BOOTSTRAP_NOTICE]

    executable_name = f"Carlina_Documents-{version}-Setup-x86_64.exe"
    destination = output_root / executable_name
    sidecar_destination = Path(os.fspath(destination) + ".artifact.json")
    checksum_destination = Path(os.fspath(destination) + ".sha256")
    try:
        output_root.mkdir(parents=True, exist_ok=True)
        output_status = output_root.lstat()
    except OSError:
        raise _fail("The output directory could not be created.") from None
    if not stat.S_ISDIR(output_status.st_mode):
        raise _fail("The output path is not a real directory.")
    destinations = (destination, sidecar_destination, checksum_destination)
    if any(_path_exists_without_following(path) for path in destinations):
        raise _fail("A Windows installer output path already exists.")

    try:
        with tempfile.TemporaryDirectory(
            prefix=".carlina-documents-windows-installer-", dir=output_root
        ) as temporary_directory:
            work_root = Path(temporary_directory)
            stage = work_root / "stage"
            spec_directory = work_root / "spec"
            work_directory = work_root / "work"
            dist_directory = work_root / "dist"
            config_directory = work_root / "pyinstaller-config"
            for directory in (
                stage,
                spec_directory,
                work_directory,
                dist_directory,
                config_directory,
            ):
                directory.mkdir(mode=0o755)

            staged_paths: dict[str, Path] = {}

            def copy_source(relative: str, destination_relative: str) -> Path:
                target = stage / destination_relative
                target.parent.mkdir(parents=True, exist_ok=True)
                _copy_file_exclusive(
                    source_files[relative],
                    target,
                    0o644,
                    expected_sha256=source_sha256[relative],
                )
                staged_paths[relative] = target
                return target

            copy_source("scripts/documents_appimage.py", "bootstrap.py")
            staged_icon = copy_source(_ICON_SOURCE, "carlina-documents.ico")
            installer_candidate = stage / "installer.ps1"
            _copy_file_exclusive(
                installer_path,
                installer_candidate,
                0o644,
                expected_sha256=installer_sha256,
            )

            license_directory = stage / "licenses"
            for license_name in _LICENSE_FILES:
                license_directory.mkdir(parents=True, exist_ok=True)
                copy_source(license_name, f"licenses/{license_name}")
            bootstrap_license_directory = license_directory / "bootstrap"
            notices_destination = bootstrap_license_directory / "notices"
            notices_destination.mkdir(parents=True, exist_ok=True)
            copy_source(_BOOTSTRAP_NOTICE, "licenses/bootstrap/NOTICE.json")
            for relative in notice_paths:
                notice_relative = PurePosixPath(relative).relative_to(
                    PurePosixPath(_BOOTSTRAP_NOTICES_DIRECTORY)
                )
                destination_relative = PurePosixPath("licenses/bootstrap/notices") / (
                    notice_relative
                )
                notice_target = stage.joinpath(*destination_relative.parts)
                notice_target.parent.mkdir(parents=True, exist_ok=True)
                _copy_file_exclusive(
                    source_files[relative],
                    notice_target,
                    0o644,
                    expected_sha256=source_sha256[relative],
                )
                staged_paths[relative] = notice_target

            launcher = {
                "schema_version": 1,
                "product": "carlina-documents",
                "version": version,
                "source_revision": source_revision,
                "platform": "win-64",
                "installer_sha256": installer_sha256,
                "release_index_sha256": release_index_sha256,
            }
            launcher_path = stage / "launcher.json"
            _write_exclusive(
                launcher_path,
                (
                    json.dumps(launcher, sort_keys=True, separators=(",", ":")) + "\n"
                ).encode("utf-8"),
            )
            version_file = work_root / "version-resource.txt"
            _write_exclusive(version_file, _version_resource(version), 0o600)

            def add_data(source: Path, destination_name: str) -> tuple[str, str]:
                relative_source = source.relative_to(work_root).as_posix()
                return "--add-data", f"{relative_source}:{destination_name}"

            command = [
                sys.executable,
                "-m",
                "PyInstaller",
                "--onefile",
                "--windowed",
                "--noupx",
                "--clean",
                "--noconfirm",
                "--name",
                f"Carlina_Documents-{version}-Setup-x86_64",
                "--specpath",
                os.fspath(spec_directory),
                "--workpath",
                os.fspath(work_directory),
                "--distpath",
                os.fspath(dist_directory),
                "--paths",
                os.fspath(stage),
                "--icon",
                staged_icon.relative_to(work_root).as_posix(),
                "--version-file",
                os.fspath(version_file),
                "--exclude-module",
                "carlina",
                "--exclude-module",
                "iarf_assistant",
                *add_data(installer_candidate, "."),
                *add_data(launcher_path, "."),
                *add_data(staged_icon, "."),
                *add_data(license_directory, "licenses"),
                staged_paths["scripts/documents_appimage.py"]
                .relative_to(work_root)
                .as_posix(),
            ]
            environment = os.environ.copy()
            environment["SOURCE_DATE_EPOCH"] = str(source_date_epoch)
            environment["PYINSTALLER_CONFIG_DIR"] = os.fspath(config_directory)
            private_paths = (
                source_root,
                output_root,
                installer_path,
                work_root,
                Path(sys.executable),
            )
            _run_pyinstaller(
                command,
                working_directory=work_root,
                environment=environment,
                private_paths=private_paths,
            )

            candidate_executable = dist_directory / executable_name
            executable_sha256, executable_bytes = _inspect_pe_executable(
                candidate_executable
            )
            input_hashes = {
                "installer": installer_sha256,
                "release_index": release_index_sha256,
                "icon": icon_sha256,
                "requirements_build_lock": requirements_sha256,
                "notice_catalog": notice_catalog_sha256,
            }
            input_hashes.update(
                {
                    f"notice:{relative}": source_sha256[relative]
                    for relative in notice_paths
                }
            )
            artifact = {
                "kind": "windows-setup-launcher",
                "platform": "win-64",
                "profile": "documents",
                "version": version,
                "source_revision": source_revision,
                "source_date_epoch": source_date_epoch,
                "sha256": executable_sha256,
                "bytes": executable_bytes,
                "input_hashes": input_hashes,
                "build_tools": build_tools,
            }
            sidecar_candidate = work_root / "candidate.exe.artifact.json"
            checksum_candidate = work_root / "candidate.exe.sha256"
            _write_exclusive(
                sidecar_candidate,
                (
                    json.dumps(artifact, sort_keys=True, separators=(",", ":")) + "\n"
                ).encode("utf-8"),
            )
            _write_exclusive(
                checksum_candidate,
                f"{executable_sha256}  {executable_name}\n".encode("ascii"),
            )
            _publish_outputs(
                (candidate_executable, sidecar_candidate, checksum_candidate),
                destinations,
            )
            return destinations
    except WindowsBuildError:
        raise
    except OSError, ValueError, struct.error, subprocess.SubprocessError:
        raise _fail("The Windows installer could not be built safely.") from None


def main(argv: list[str] | None = None) -> int:
    """Build with explicit local, hash-pinned inputs; never fetch latest tools."""
    parser = argparse.ArgumentParser(
        description="Build the Carlina Documents Windows setup launcher."
    )
    parser.add_argument("--root", type=Path, default=_REPOSITORY_ROOT)
    parser.add_argument("--version", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--installer", type=Path, required=True)
    parser.add_argument("--installer-sha256", required=True)
    parser.add_argument("--release-index-sha256", required=True)
    arguments = parser.parse_args(argv)
    try:
        executable, sidecar, checksum = build_windows_installer(
            root=arguments.root,
            version=arguments.version,
            source_revision=arguments.revision,
            output_dir=arguments.output_dir,
            installer=arguments.installer,
            installer_sha256=arguments.installer_sha256,
            release_index_sha256=arguments.release_index_sha256,
        )
    except WindowsBuildError as error:
        print(str(error), file=sys.stderr)
        if error.diagnostics:
            print(
                json.dumps(error.diagnostics, sort_keys=True),
                file=sys.stderr,
            )
        return 1
    print(
        json.dumps(
            {
                "executable": executable.name,
                "sidecar": sidecar.name,
                "checksum": checksum.name,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
