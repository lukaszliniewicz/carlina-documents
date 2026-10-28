#!/usr/bin/env python3
"""Build Carlina artifacts from an exact clean Git export.

Setuptools reuses the checkout-local ``build/`` directory. That is convenient
for development but unsafe for a release because ignored/stale package data can
survive between builds. This wrapper exports the reviewed commit into a fresh
temporary directory and builds only there.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import csv
import hashlib
import importlib.util
import io
import os
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

if __package__:
    from .addon_publication_policy import (
        ADDONS_RELATIVE_PATH,
        AddonPublicationPolicyError,
        load_addon_publication_policy,
        validate_addon_publication_tree,
    )
else:
    from addon_publication_policy import (
        ADDONS_RELATIVE_PATH,
        AddonPublicationPolicyError,
        load_addon_publication_policy,
        validate_addon_publication_tree,
    )

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CleanReleaseBuildError(RuntimeError):
    pass


class _DeterministicGzipWriter:
    """Write a gzip stream using deterministic uncompressed DEFLATE blocks."""

    _BLOCK_SIZE = 65_535

    def __init__(self, fileobj: io.BufferedWriter, *, mtime: int) -> None:
        if not 0 <= mtime <= 0xFFFFFFFF:
            raise CleanReleaseBuildError(
                "The release commit timestamp is outside the gzip date range."
            )
        self._fileobj = fileobj
        self._buffer = bytearray()
        self._crc32 = 0
        self._size = 0
        self._position = 0
        self._closed = False
        # RFC 1952: no optional fields, fixed mtime, no compression hint, and
        # an unknown operating system. The following DEFLATE stream uses only
        # RFC 1951 stored blocks, so its bytes do not depend on host zlib.
        self._fileobj.write(
            b"\x1f\x8b\x08\x00" + struct.pack("<I", mtime) + b"\x00\xff"
        )

    def __enter__(self) -> _DeterministicGzipWriter:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def tell(self) -> int:
        return self._position

    def write(self, data: bytes | bytearray | memoryview) -> int:
        if self._closed:
            raise ValueError("write to closed gzip stream")
        payload = bytes(data)
        self._crc32 = binascii.crc32(payload, self._crc32) & 0xFFFFFFFF
        self._size = (self._size + len(payload)) & 0xFFFFFFFF
        self._position += len(payload)
        self._buffer.extend(payload)
        # Retain one possible final block until close. This makes block
        # boundaries depend only on the byte stream, not on caller write sizes.
        while len(self._buffer) > self._BLOCK_SIZE:
            block = bytes(self._buffer[: self._BLOCK_SIZE])
            del self._buffer[: self._BLOCK_SIZE]
            self._write_block(block, final=False)
        return len(payload)

    def flush(self) -> None:
        if not self._closed:
            self._fileobj.flush()

    def close(self) -> None:
        if self._closed:
            return
        self._write_block(bytes(self._buffer), final=True)
        self._buffer.clear()
        self._fileobj.write(struct.pack("<II", self._crc32, self._size))
        self._fileobj.flush()
        self._closed = True

    def _write_block(self, payload: bytes, *, final: bool) -> None:
        length = len(payload)
        if length > self._BLOCK_SIZE:
            raise AssertionError("stored DEFLATE block exceeds its format limit")
        self._fileobj.write(struct.pack("<BHH", int(final), length, length ^ 0xFFFF))
        self._fileobj.write(payload)


def _run(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise CleanReleaseBuildError(
            f"Command failed ({' '.join(command)}): {detail[-4000:]}"
        )
    return result


def _git_archive_command(archive_path: Path, revision: str) -> list[str]:
    # A machine-wide core.autocrlf=true otherwise makes `git archive` rewrite
    # every text blob on Windows, producing semantically different releases
    # from the same commit. Release inputs must always be raw repository bytes.
    return [
        "git",
        "-c",
        "core.autocrlf=false",
        "archive",
        "--format=tar",
        f"--output={archive_path}",
        revision,
    ]


def _commit_epoch(root: Path, revision: str) -> str:
    result = _run(
        ["git", "show", "-s", "--format=%ct", revision],
        cwd=root,
    )
    epoch = result.stdout.strip()
    if not epoch.isascii() or not epoch.isdecimal():
        raise CleanReleaseBuildError(
            f"Could not determine the commit timestamp for {revision!r}."
        )
    return epoch


def _wheel_datetime(source_date_epoch: str) -> tuple[int, int, int, int, int, int]:
    value = datetime.fromtimestamp(int(source_date_epoch), UTC)
    if value.year < 1980 or value.year > 2107:
        raise CleanReleaseBuildError(
            "The release commit timestamp is outside the ZIP date range."
        )
    return (value.year, value.month, value.day, value.hour, value.minute, value.second)


def _record_digest(payload: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(payload).digest())
    return "sha256=" + encoded.rstrip(b"=").decode("ascii")


def _normalize_wheel(path: Path, *, source_date_epoch: str) -> None:
    """Canonicalize generated metadata and ZIP fields across build platforms."""

    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len({name.casefold() for name in names}):
                raise CleanReleaseBuildError(
                    "Built wheel contains duplicate case-insensitive member names."
                )
            members = {name: archive.read(name) for name in names}
    except zipfile.BadZipFile as exc:
        raise CleanReleaseBuildError(f"Built wheel is not valid: {path}") from exc
    metadata_names = [
        name for name in names if name.casefold().endswith(".dist-info/metadata")
    ]
    record_names = [
        name for name in names if name.casefold().endswith(".dist-info/record")
    ]
    if len(metadata_names) != 1 or len(record_names) != 1:
        raise CleanReleaseBuildError(
            "Built wheel must contain exactly one METADATA and RECORD member."
        )
    metadata_name = metadata_names[0]
    record_name = record_names[0]
    members[metadata_name] = (
        members[metadata_name].replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    )
    record_stream = io.StringIO(newline="")
    record_writer = csv.writer(record_stream, lineterminator="\n")
    for name in sorted(members):
        if name == record_name:
            continue
        payload = members[name]
        record_writer.writerow([name, _record_digest(payload), len(payload)])
    record_writer.writerow([record_name, "", ""])
    members[record_name] = record_stream.getvalue().encode("utf-8")

    timestamp = _wheel_datetime(source_date_epoch)
    temporary = path.with_name(f".{path.name}.normalize-{uuid.uuid4().hex}")
    try:
        with zipfile.ZipFile(
            temporary,
            "x",
            compression=zipfile.ZIP_STORED,
            allowZip64=True,
        ) as archive:
            ordered_names = sorted(name for name in members if name != record_name)
            ordered_names.append(record_name)
            for name in ordered_names:
                info = zipfile.ZipInfo(name, date_time=timestamp)
                # Stored members trade a modest size increase for byte-level
                # reproducibility across Python and zlib builds.
                info.compress_type = zipfile.ZIP_STORED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, members[name])
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _normalize_sdist(path: Path, *, source_date_epoch: str) -> None:
    """Canonicalize generated metadata and tar/gzip fields across platforms."""

    try:
        with tarfile.open(path, "r:gz") as archive:
            source_members = archive.getmembers()
            names = [member.name for member in source_members]
            if len(names) != len({name.casefold() for name in names}):
                raise CleanReleaseBuildError(
                    "Built sdist contains duplicate case-insensitive member names."
                )
            members: dict[str, tuple[bool, bytes]] = {}
            for member in source_members:
                if member.isdir():
                    members[member.name] = (True, b"")
                    continue
                if not member.isfile():
                    raise CleanReleaseBuildError(
                        f"Built sdist contains an unsupported member: {member.name!r}."
                    )
                stream = archive.extractfile(member)
                if stream is None:
                    raise CleanReleaseBuildError(
                        f"Built sdist member has no payload: {member.name!r}."
                    )
                payload = stream.read()
                if PurePosixPath(member.name).name in {"PKG-INFO", "setup.cfg"}:
                    payload = payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                members[member.name] = (False, payload)
    except tarfile.TarError as exc:
        raise CleanReleaseBuildError(f"Built sdist is not valid: {path}") from exc

    epoch = int(source_date_epoch)
    temporary = path.with_name(f".{path.name}.normalize-{uuid.uuid4().hex}")
    try:
        with temporary.open("xb") as raw_output:
            with (
                _DeterministicGzipWriter(raw_output, mtime=epoch) as compressed,
                tarfile.open(
                    fileobj=compressed,
                    mode="w:",
                    format=tarfile.PAX_FORMAT,
                ) as archive,
            ):
                for name in sorted(members):
                    is_directory, payload = members[name]
                    info = tarfile.TarInfo(name)
                    info.type = tarfile.DIRTYPE if is_directory else tarfile.REGTYPE
                    info.mode = 0o755 if is_directory else 0o644
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    info.mtime = epoch
                    info.size = 0 if is_directory else len(payload)
                    archive.addfile(
                        info,
                        None if is_directory else io.BytesIO(payload),
                    )
            raw_output.flush()
            os.fsync(raw_output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _require_clean_tracked_checkout(root: Path) -> None:
    result = _run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=root,
    )
    if result.stdout.strip():
        raise CleanReleaseBuildError(
            "Tracked files differ from HEAD. Commit the reviewed release inputs first."
        )


def _safe_extract_git_archive(archive_path: Path, destination: Path) -> None:
    with tarfile.open(archive_path, mode="r:") as archive:
        members = archive.getmembers()
        for member in members:
            relative = PurePosixPath(member.name)
            if (
                relative.is_absolute()
                or not relative.parts
                or any(part in {"", ".", ".."} for part in relative.parts)
                or not (member.isdir() or member.isfile())
            ):
                raise CleanReleaseBuildError(
                    f"Git archive contains an unsafe member: {member.name!r}"
                )
        for member in members:
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise CleanReleaseBuildError(
                    f"Git archive member has no file payload: {member.name!r}"
                )
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(member.mode & 0o777)


def _prune_non_public_addons_from_release_tree(
    export_root: Path,
) -> tuple[str, ...]:
    """Remove private/local packages from an isolated release export only."""

    export_root = Path(export_root).resolve()
    policy = load_addon_publication_policy(export_root)
    validate_addon_publication_tree(export_root, policy)
    addons_root = (export_root / ADDONS_RELATIVE_PATH).resolve()
    if not addons_root.is_relative_to(export_root):
        raise CleanReleaseBuildError("The add-on release path escapes the export root.")

    for package_id in policy.excluded_ids:
        target = (addons_root / package_id).resolve()
        if not target.is_relative_to(addons_root) or target.parent != addons_root:
            raise CleanReleaseBuildError(
                f"Refusing to prune an unsafe private add-on path: {target}"
            )
        if not target.is_dir():
            raise CleanReleaseBuildError(
                "Non-public add-on source is missing from the release export: "
                + package_id
            )
        shutil.rmtree(target)

    remaining = {
        item.name
        for item in addons_root.iterdir()
        if item.is_dir() and (item / "manifest.json").is_file()
    }
    leaked = sorted(remaining.intersection(policy.excluded_ids))
    if leaked:
        raise CleanReleaseBuildError(
            "Non-public add-on source survived release pruning: " + ", ".join(leaked)
        )
    return policy.excluded_ids


def build_clean_release(
    output_dir: str | os.PathLike[str],
    *,
    root: str | os.PathLike[str] = PROJECT_ROOT,
    revision: str = "HEAD",
    wheel_only: bool = False,
    force: bool = False,
) -> tuple[Path, ...]:
    source_root = Path(root).expanduser().resolve()
    _require_clean_tracked_checkout(source_root)
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="carlina-clean-release-") as temporary:
        temporary_root = Path(temporary)
        archive_path = temporary_root / "source.tar"
        export_root = temporary_root / "source"
        export_root.mkdir()
        _run(_git_archive_command(archive_path, revision), cwd=source_root)
        _safe_extract_git_archive(archive_path, export_root)
        try:
            _prune_non_public_addons_from_release_tree(export_root)
        except AddonPublicationPolicyError as exc:
            raise CleanReleaseBuildError(str(exc)) from exc
        built_dir = temporary_root / "dist"
        command = [sys.executable, "-m", "build", "--outdir", str(built_dir)]
        if wheel_only:
            command.append("--wheel")
        source_date_epoch = _commit_epoch(
            source_root,
            revision,
        )
        build_environment = dict(os.environ)
        build_environment["SOURCE_DATE_EPOCH"] = source_date_epoch
        build_environment["PYTHONHASHSEED"] = "0"
        _run(command, cwd=export_root, environment=build_environment)
        built = tuple(sorted(path for path in built_dir.iterdir() if path.is_file()))
        if not built:
            raise CleanReleaseBuildError("The isolated build produced no artifacts.")
        for artifact in built:
            if artifact.suffix.casefold() == ".whl":
                _normalize_wheel(
                    artifact,
                    source_date_epoch=source_date_epoch,
                )
            elif artifact.name.casefold().endswith(".tar.gz"):
                _normalize_sdist(
                    artifact,
                    source_date_epoch=source_date_epoch,
                )
        targets = tuple(destination / source.name for source in built)
        collisions = [target for target in targets if target.exists()]
        if collisions and not force:
            raise CleanReleaseBuildError(
                "Release outputs already exist: "
                + ", ".join(str(path) for path in collisions)
            )
        outputs: list[Path] = []
        for source, target in zip(built, targets, strict=True):
            shutil.copy2(source, target)
            outputs.append(target)
    return tuple(outputs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build Carlina from a clean, exact Git export."
    )
    parser.add_argument("--output-dir", default="dist")
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--wheel-only", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if importlib.util.find_spec("build") is None:
        print(
            "Clean release build requires build==1.5.0. Run: "
            "uv run --python 3.14 --no-project --with build==1.5.0 "
            "python scripts/build_clean_release.py --output-dir dist/clean",
            file=sys.stderr,
        )
        return 2
    try:
        outputs = build_clean_release(
            args.output_dir,
            revision=args.revision,
            wheel_only=args.wheel_only,
            force=args.force,
        )
    except (CleanReleaseBuildError, OSError, tarfile.TarError) as exc:
        print(f"Clean release build failed: {exc}", file=sys.stderr)
        return 1
    for output in outputs:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
