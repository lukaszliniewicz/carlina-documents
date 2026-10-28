"""Staged, digest-addressed installation for Add-on API v1 packages."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from .contracts import (
    ADDON_ID_PATTERN,
    AddonSourceKind,
    ResolvedAddonContract,
    RuntimeMode,
    compile_addon_contract,
)

ADDON_ARCHIVE_MAX_FILES = 5_000
ADDON_ARCHIVE_MAX_COMPRESSED_BYTES = 256 * 1024 * 1024
ADDON_ARCHIVE_MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
ADDON_ARCHIVE_MAX_MEMBER_BYTES = 128 * 1024 * 1024
ADDON_ARCHIVE_MAX_COMPRESSION_RATIO = 250
ADDON_MANIFEST_MAX_BYTES = 1 * 1024 * 1024
COPY_CHUNK_BYTES = 1024 * 1024
FORBIDDEN_PATH_PARTS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        "node_modules",
        "site-packages",
    }
)
FORBIDDEN_SUFFIXES = frozenset({".egg-link", ".pth", ".pyc", ".pyo"})
WINDOWS_RESERVED_NAMES = frozenset(
    {
        "aux",
        "con",
        "nul",
        "prn",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
)


@dataclass(frozen=True, slots=True)
class ArchiveMember:
    info: zipfile.ZipInfo
    relative_path: PurePosixPath


@dataclass(frozen=True, slots=True)
class ValidatedArchive:
    manifest: dict
    addon_id: str
    members: tuple[ArchiveMember, ...]


@dataclass(frozen=True, slots=True)
class StagedAddonPackage:
    stage_dir: Path
    archive_path: Path
    code_dir: Path
    original_filename: str
    archive_sha256: str
    contract: ResolvedAddonContract
    provenance: dict


@dataclass(frozen=True, slots=True)
class PublishedAddonPackage:
    addon_id: str
    version: str
    version_dir: Path
    previous_version: str | None
    active_marker: Path
    newly_published: bool
    contract: ResolvedAddonContract


class AddonInstallError(RuntimeError):
    pass


def _unsafe_windows_part(part: str) -> bool:
    stem = part.rstrip(" .").split(".", 1)[0].casefold()
    return part != part.rstrip(" .") or stem in WINDOWS_RESERVED_NAMES


def validate_addon_archive(zip_ref: zipfile.ZipFile) -> ValidatedArchive:
    """Validate paths, sizes, collisions, content, and the one-package layout."""

    infos = zip_ref.infolist()
    if not infos:
        raise ValueError("The add-on ZIP file is empty.")
    if len(infos) > ADDON_ARCHIVE_MAX_FILES:
        raise ValueError(
            f"The add-on ZIP contains too many entries (maximum {ADDON_ARCHIVE_MAX_FILES:,})."
        )

    total_size = 0
    normalized: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
    manifest_entries: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
    folded_paths: dict[str, str] = {}
    for info in infos:
        raw_name = str(info.filename or "")
        if (
            not raw_name
            or "\x00" in raw_name
            or "\\" in raw_name
            or info.flag_bits & 0x1
        ):
            raise ValueError("The add-on ZIP contains an unsafe or encrypted entry.")
        path = PurePosixPath(raw_name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or (path.parts and ":" in path.parts[0])
        ):
            raise ValueError(f"The add-on ZIP contains an unsafe path: {raw_name!r}.")
        if any(
            part.casefold() in FORBIDDEN_PATH_PARTS or _unsafe_windows_part(part)
            for part in path.parts
        ):
            raise ValueError(
                f"The add-on ZIP contains a forbidden package path: {raw_name!r}."
            )
        if path.suffix.casefold() in FORBIDDEN_SUFFIXES:
            raise ValueError(
                f"The add-on ZIP contains a forbidden package file: {raw_name!r}."
            )

        folded = path.as_posix().casefold()
        prior = folded_paths.get(folded)
        if prior is not None:
            raise ValueError(
                f"The add-on ZIP contains a case-insensitive path collision: {prior!r} and {raw_name!r}."
            )
        folded_paths[folded] = raw_name

        unix_mode = info.external_attr >> 16
        file_type = stat.S_IFMT(unix_mode)
        if stat.S_ISLNK(unix_mode) or (
            file_type and not (stat.S_ISREG(unix_mode) or stat.S_ISDIR(unix_mode))
        ):
            raise ValueError(
                f"The add-on ZIP contains an unsupported link or special file: {raw_name!r}."
            )
        file_size = max(0, int(info.file_size))
        if file_size > ADDON_ARCHIVE_MAX_MEMBER_BYTES:
            raise ValueError(
                f"The add-on ZIP entry exceeds the 128 MB member limit: {raw_name!r}."
            )
        total_size += file_size
        if total_size > ADDON_ARCHIVE_MAX_UNCOMPRESSED_BYTES:
            raise ValueError("The add-on ZIP expands beyond the 512 MB safety limit.")
        compressed = max(0, int(info.compress_size))
        if file_size > 1_048_576 and (
            compressed == 0
            or file_size / compressed > ADDON_ARCHIVE_MAX_COMPRESSION_RATIO
        ):
            raise ValueError(
                f"The add-on ZIP entry has an unsafe compression ratio: {raw_name!r}."
            )

        normalized.append((info, path))
        if not info.is_dir() and path.name == "manifest.json":
            manifest_entries.append((info, path))

    if len(manifest_entries) != 1:
        raise ValueError("The add-on ZIP must contain exactly one manifest.json file.")
    manifest_info, manifest_path = manifest_entries[0]
    if manifest_info.file_size > ADDON_MANIFEST_MAX_BYTES:
        raise ValueError("The add-on manifest exceeds the 1 MiB limit.")
    with zip_ref.open(manifest_info) as manifest_file:
        raw_manifest = manifest_file.read(ADDON_MANIFEST_MAX_BYTES + 1)
    if len(raw_manifest) > ADDON_MANIFEST_MAX_BYTES:
        raise ValueError("The add-on manifest exceeds the 1 MiB limit.")
    try:
        manifest = json.loads(raw_manifest.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("The add-on manifest is not valid UTF-8 JSON.") from exc
    if not isinstance(manifest, dict):
        raise TypeError("The add-on manifest must be a JSON object.")

    addon_id = str(manifest.get("id") or "").strip()
    if not ADDON_ID_PATTERN.fullmatch(addon_id):
        raise ValueError(
            "The add-on manifest id must start with a letter or number and use only "
            "letters, numbers, dots, underscores, or hyphens."
        )
    package_root = manifest_path.parent
    members: list[ArchiveMember] = []
    for info, path in normalized:
        if package_root != PurePosixPath("."):
            try:
                relative_path = path.relative_to(package_root)
            except ValueError as exc:
                raise ValueError(
                    "Every add-on ZIP entry must live beside or below its manifest."
                ) from exc
        else:
            relative_path = path
        if relative_path == PurePosixPath("."):
            continue
        members.append(ArchiveMember(info, relative_path))
    return ValidatedArchive(manifest, addon_id, tuple(members))


def extract_addon_archive(
    zip_ref: zipfile.ZipFile,
    target_dir: str | Path,
    members: tuple[ArchiveMember, ...] | list[ArchiveMember],
) -> None:
    target_root = Path(target_dir).resolve()
    target_root.mkdir(parents=True, exist_ok=False)
    for member in members:
        info = member.info
        relative_path = member.relative_path
        target_path = (target_root / Path(*relative_path.parts)).resolve()
        if not target_path.is_relative_to(target_root):
            raise ValueError(
                f"The add-on ZIP entry escapes its package directory: {info.filename!r}."
            )
        if info.is_dir():
            target_path.mkdir(parents=True, exist_ok=True)
            continue
        target_path.parent.mkdir(parents=True, exist_ok=True)
        copied = 0
        with zip_ref.open(info) as source, target_path.open("xb") as target:
            while chunk := source.read(COPY_CHUNK_BYTES):
                copied += len(chunk)
                if copied > info.file_size or copied > ADDON_ARCHIVE_MAX_MEMBER_BYTES:
                    raise ValueError(
                        f"The add-on ZIP entry expanded beyond its declared size: {info.filename!r}."
                    )
                target.write(chunk)
        if copied != info.file_size:
            raise ValueError(
                f"The add-on ZIP entry size changed during extraction: {info.filename!r}."
            )


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


class AddonPackageInstaller:
    def __init__(self, paths) -> None:
        self.paths = paths

    def stage(
        self,
        stream: BinaryIO,
        *,
        original_filename: str,
        operator_approved: bool,
        source_provenance: Mapping[str, Any] | None = None,
        expected_archive_sha256: str | None = None,
        expected_archive_size: int | None = None,
        expected_manifest_sha256: str | None = None,
        expected_contract_sha256: str | None = None,
        progress: Callable[[int, int | None], None] | None = None,
        cancellation_check: Callable[[], None] | None = None,
    ) -> StagedAddonPackage:
        stage_dir = (
            Path(self.paths.staging_dir) / "addon-installs" / uuid.uuid4().hex
        ).resolve()
        allowed_root = (Path(self.paths.staging_dir) / "addon-installs").resolve()
        stage_dir.relative_to(allowed_root)
        stage_dir.mkdir(parents=True, exist_ok=False)
        archive_path = stage_dir / "package.zip"
        digest = hashlib.sha256()
        size = 0
        try:
            with archive_path.open("xb") as target:
                while chunk := stream.read(COPY_CHUNK_BYTES):
                    if cancellation_check is not None:
                        cancellation_check()
                    size += len(chunk)
                    if size > ADDON_ARCHIVE_MAX_COMPRESSED_BYTES:
                        raise AddonInstallError(
                            "The add-on archive exceeds the 256 MB upload limit."
                        )
                    digest.update(chunk)
                    target.write(chunk)
                    if progress is not None:
                        progress(size, expected_archive_size)
            archive_sha256 = digest.hexdigest()
            if expected_archive_size is not None and size != expected_archive_size:
                raise AddonInstallError(
                    "The add-on archive size differs from the reviewed catalog record."
                )
            if (
                expected_archive_sha256 is not None
                and archive_sha256 != expected_archive_sha256
            ):
                raise AddonInstallError(
                    "The add-on archive digest differs from the reviewed catalog record."
                )
            code_dir = stage_dir / "code"
            with zipfile.ZipFile(archive_path, "r") as archive:
                validated = validate_addon_archive(archive)
                extract_addon_archive(archive, code_dir, validated.members)
            source = (
                json.loads(json.dumps(dict(source_provenance)))
                if source_provenance
                else None
            )
            provenance: dict[str, Any] = {
                "archive_sha256": archive_sha256,
                "archive_size": size,
                "original_filename": Path(original_filename).name,
                "staged_at": datetime.now(UTC).isoformat(),
                "signature_status": (
                    "catalog_verified"
                    if source
                    and source.get("type") == "signed_addon_catalog"
                    and source.get("catalog_signer_key_id")
                    else "unverified"
                    if validated.manifest.get("signature")
                    else "unsigned"
                ),
                "operator_approved_native_code": bool(operator_approved),
            }
            if source is not None:
                provenance["source"] = source
            contract = compile_addon_contract(
                validated.manifest,
                addon_dir=code_dir,
                source_kind=AddonSourceKind.INSTALLED,
                strict=True,
                provenance=provenance,
            )
            if (
                expected_manifest_sha256 is not None
                and contract.manifest_sha256 != expected_manifest_sha256
            ):
                raise AddonInstallError(
                    "The package manifest differs from the reviewed catalog contract."
                )
            if (
                expected_contract_sha256 is not None
                and contract.contract_sha256 != expected_contract_sha256
            ):
                raise AddonInstallError(
                    "The resolved package contract differs from the reviewed "
                    "catalog contract."
                )
            has_in_process_code = any(
                unit.mode == RuntimeMode.IN_PROCESS for unit in contract.runtime_units
            )
            has_host_migration_code = contract.database_migration is not None
            if (
                has_in_process_code or has_host_migration_code
            ) and not operator_approved:
                raise AddonInstallError(
                    "Installing host-executed add-on code requires explicit approval "
                    "of full host-code access."
                )
            (code_dir / "provenance.json").write_text(
                json.dumps(provenance, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return StagedAddonPackage(
                stage_dir,
                archive_path,
                code_dir,
                Path(original_filename).name,
                archive_sha256,
                contract,
                provenance,
            )
        except Exception:
            shutil.rmtree(stage_dir, ignore_errors=True)
            raise

    def publish(self, staged: StagedAddonPackage) -> PublishedAddonPackage:
        addon_root = (
            Path(self.paths.installed_addons_dir) / staged.contract.addon_id
        ).resolve()
        addon_root.relative_to(Path(self.paths.installed_addons_dir).resolve())
        addon_root.mkdir(parents=True, exist_ok=True)
        version_dir = (addon_root / staged.contract.version).resolve()
        version_dir.relative_to(addon_root)
        marker = addon_root / "active.json"
        previous_version = None
        if marker.is_file():
            marker_payload = json.loads(marker.read_text(encoding="utf-8"))
            previous_version = str(marker_payload.get("version") or "").strip() or None

        newly_published = False
        if version_dir.exists():
            provenance_path = version_dir / "provenance.json"
            existing = (
                json.loads(provenance_path.read_text(encoding="utf-8"))
                if provenance_path.is_file()
                else {}
            )
            if existing.get("archive_sha256") != staged.archive_sha256:
                raise AddonInstallError(
                    f"Version {staged.contract.version} of '{staged.contract.addon_id}' "
                    "already exists with a different archive digest. Publish a new version."
                )
        else:
            os.replace(staged.code_dir, version_dir)
            newly_published = True
        _atomic_json(
            marker,
            {
                "version": staged.contract.version,
                "archive_sha256": staged.archive_sha256,
                "activated_at": datetime.now(UTC).isoformat(),
            },
        )
        return PublishedAddonPackage(
            staged.contract.addon_id,
            staged.contract.version,
            version_dir,
            previous_version,
            marker,
            newly_published,
            staged.contract,
        )

    def rollback(self, published: PublishedAddonPackage) -> None:
        if published.previous_version:
            _atomic_json(
                published.active_marker,
                {
                    "version": published.previous_version,
                    "rolled_back_at": datetime.now(UTC).isoformat(),
                },
            )
        else:
            published.active_marker.unlink(missing_ok=True)
            if published.newly_published and published.version_dir.is_dir():
                shutil.rmtree(published.version_dir)
            addon_root = published.active_marker.parent
            if addon_root.is_dir() and not any(addon_root.iterdir()):
                addon_root.rmdir()
            elif addon_root.is_dir():
                _atomic_json(
                    published.active_marker,
                    {
                        "disabled": True,
                        "rolled_back_at": datetime.now(UTC).isoformat(),
                    },
                )

    @staticmethod
    def cleanup_stage(staged: StagedAddonPackage) -> None:
        shutil.rmtree(staged.stage_dir, ignore_errors=True)

    def remove_code(self, addon_id: str, version: str | None = None) -> Path:
        installed_root = Path(self.paths.installed_addons_dir).resolve()
        addon_root = (installed_root / addon_id).resolve()
        addon_root.relative_to(installed_root)
        target = (addon_root / version).resolve() if version else addon_root
        target.relative_to(installed_root)
        if not target.is_dir():
            raise FileNotFoundError(
                f"Installed code for add-on '{addon_id}'"
                + (f" version '{version}'" if version else "")
                + " was not found."
            )
        if version:
            marker = addon_root / "active.json"
            if marker.is_file():
                active = json.loads(marker.read_text(encoding="utf-8"))
                if str(active.get("version") or "") == version:
                    _atomic_json(
                        marker,
                        {
                            "disabled": True,
                            "removed_version": version,
                            "removed_at": datetime.now(UTC).isoformat(),
                        },
                    )
        shutil.rmtree(target)
        return target
