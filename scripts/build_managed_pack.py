#!/usr/bin/env python3
"""Build one offline Pixi runtime artifact for a Carlina wheel."""

from __future__ import annotations

import argparse
import base64
import binascii
import email.parser
import hashlib
import json
import os
import subprocess
import sys
import tomllib
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from packaging.version import InvalidVersion, Version

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from iarf_assistant.core.managed_bundle import (
    WINDOWS_BUNDLE_MANIFEST,
    WINDOWS_BUNDLE_PAYLOADS,
    WindowsBundleMember,
    windows_bundle_manifest_bytes,
)
from scripts.package_application_companions import (
    ApplicationCompanionsPackagingError,
    validate_application_companions_wheel,
)

PIXI_PACK_VERSION = "0.7.10"
SUPPORTED_PLATFORMS = {
    "win-64",
    "linux-64",
    "osx-64",
    "osx-arm64",
}
SUPPORTED_APPLICATION_PROFILES = frozenset({"workbench", "documents"})
SUPPORTED_PIXI_ENVIRONMENTS = frozenset({"runtime", "documents"})


class ManagedPackBuildError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _find_bytes(handle, needle: bytes, *, start: int) -> int:
    handle.seek(start)
    overlap = b""
    cursor = start
    while chunk := handle.read(4 * 1024 * 1024):
        payload = overlap + chunk
        index = payload.find(needle)
        if index >= 0:
            return cursor - len(overlap) + index
        overlap = payload[-(len(needle) - 1) :] if len(needle) > 1 else b""
        cursor += len(chunk)
    raise ManagedPackBuildError(
        f"Pixi Pack executable lacks marker {needle.decode('ascii')!r}."
    )


def _skip_line_breaks(handle, offset: int, *, end: int) -> int:
    handle.seek(offset)
    while offset < end:
        value = handle.read(1)
        if value not in {b"\r", b"\n"}:
            return offset
        offset += 1
    return offset


def _trim_line_breaks(handle, start: int, end: int) -> int:
    while end > start:
        handle.seek(end - 1)
        if handle.read(1) not in {b"\r", b"\n"}:
            break
        end -= 1
    return end


def windows_executable_payload_sections(path: Path) -> tuple[tuple[int, int], ...]:
    """Locate the two Base64 payloads without materializing either one."""

    size = path.stat().st_size
    with path.open("rb") as handle:
        header_end: int | None = None
        while handle.tell() < min(size, 1024 * 1024):
            line = handle.readline(64 * 1024)
            if not line:
                break
            if line.rstrip(b"\r\n") == b"__END_HEADER__":
                header_end = handle.tell()
                break
        if header_end is None:
            raise ManagedPackBuildError(
                "Pixi Pack Windows executable lacks __END_HEADER__."
            )
        archive_start = _skip_line_breaks(handle, header_end, end=size)
        archive_marker = _find_bytes(
            handle,
            b"__END_ARCHIVE__",
            start=archive_start,
        )
        archive_end = _trim_line_breaks(handle, archive_start, archive_marker)
        unpacker_start = _skip_line_breaks(
            handle,
            archive_marker + len(b"__END_ARCHIVE__"),
            end=size,
        )
        unpacker_end = _trim_line_breaks(handle, unpacker_start, size)
    sections = (
        (archive_start, archive_end - archive_start),
        (unpacker_start, unpacker_end - unpacker_start),
    )
    if any(length <= 0 or length % 4 for _, length in sections):
        raise ManagedPackBuildError(
            "Pixi Pack Windows executable has invalid Base64 payload lengths."
        )
    return sections


def _decode_base64_range(
    source,
    destination,
    *,
    offset: int,
    length: int,
) -> WindowsBundleMember:
    """Decode one aligned SFX payload with bounded memory while hashing it."""

    source.seek(offset)
    remaining = length
    digest = hashlib.sha256()
    decoded_size = 0
    while remaining:
        requested = min(4 * 1024 * 1024, remaining)
        encoded = source.read(requested)
        if len(encoded) != requested:
            raise ManagedPackBuildError(
                "Pixi Pack executable ended while decoding an embedded payload."
            )
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except binascii.Error as exc:
            raise ManagedPackBuildError(
                "Pixi Pack executable contains invalid embedded Base64."
            ) from exc
        destination.write(decoded)
        digest.update(decoded)
        decoded_size += len(decoded)
        remaining -= requested
    return WindowsBundleMember(sha256=digest.hexdigest(), size=decoded_size)


def _stored_zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = 0o600 << 16
    return info


def build_windows_bundle(self_extractor: Path, output: Path) -> None:
    """Convert Pixi Pack's generated SFX into a small-parser, verified ZIP."""

    sections = windows_executable_payload_sections(self_extractor)
    records: dict[str, WindowsBundleMember] = {}
    try:
        with (
            self_extractor.open("rb") as source,
            zipfile.ZipFile(
                output,
                "x",
                compression=zipfile.ZIP_STORED,
                allowZip64=True,
            ) as bundle,
        ):
            for name, (offset, length) in zip(WINDOWS_BUNDLE_PAYLOADS, sections):
                with bundle.open(
                    _stored_zip_info(name),
                    "w",
                    force_zip64=True,
                ) as destination:
                    records[name] = _decode_base64_range(
                        source,
                        destination,
                        offset=offset,
                        length=length,
                    )
            bundle.writestr(
                _stored_zip_info(WINDOWS_BUNDLE_MANIFEST),
                windows_bundle_manifest_bytes(records),
            )
    except Exception:
        output.unlink(missing_ok=True)
        raise


def inspect_wheel(wheel: str | os.PathLike[str]) -> dict[str, Any]:
    from carlina.distribution import parse_application_distribution

    path = Path(wheel).expanduser().resolve()
    if not path.is_file() or path.suffix != ".whl":
        raise ManagedPackBuildError(f"Carlina wheel does not exist: {path}")
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            folded_names = [name.casefold() for name in names]
            if len(folded_names) != len(set(folded_names)):
                raise ManagedPackBuildError(
                    "Wheel contains duplicate case-insensitive member paths."
                )
            metadata_names = [
                name
                for name in names
                if name.casefold().endswith(".dist-info/metadata")
            ]
            if len(metadata_names) != 1:
                raise ManagedPackBuildError(
                    "Wheel must contain exactly one .dist-info/METADATA file."
                )
            descriptor_names = [
                name for name in names if name.casefold() == "carlina/distribution.json"
            ]
            if descriptor_names and descriptor_names != ["carlina/distribution.json"]:
                raise ManagedPackBuildError(
                    "Wheel application distribution descriptor must use its exact path."
                )
            distribution = None
            if descriptor_names:
                try:
                    # The shared parser accepts at most 4 KiB; read one byte
                    # beyond that bound so oversized entries fail without an
                    # unbounded allocation.
                    with archive.open("carlina/distribution.json") as descriptor:
                        descriptor_bytes = descriptor.read(4097)
                    distribution = parse_application_distribution(descriptor_bytes)
                except (KeyError, RuntimeError, OSError) as exc:
                    raise ManagedPackBuildError(
                        "Wheel application distribution descriptor is malformed."
                    ) from exc
            metadata = email.parser.BytesParser().parsebytes(
                archive.read(metadata_names[0])
            )
            release_schema_v1 = "iarf_assistant/core/release_manifest_schema.json"
            required_fragments = (
                "iarf_assistant/static/output.css",
                "iarf_assistant/static/frontend/manifest.json",
                release_schema_v1,
                release_schema_v1.removesuffix(".json") + "_v2.json",
                "iarf_assistant/core/release_trust.json",
            )
            lowered = {name.casefold() for name in names}
            missing = [
                fragment
                for fragment in required_fragments
                if fragment.casefold() not in lowered
            ]
            if missing:
                raise ManagedPackBuildError(
                    "Wheel lacks required prebuilt/runtime files: " + ", ".join(missing)
                )
    except zipfile.BadZipFile as exc:
        raise ManagedPackBuildError(
            f"Wheel is not a valid ZIP archive: {path}"
        ) from exc
    name = str(metadata.get("Name") or "")
    version = str(metadata.get("Version") or "")
    if name.casefold() != "carlina" or not version:
        raise ManagedPackBuildError(
            f"Expected a Carlina wheel, got name={name!r}, version={version!r}."
        )
    try:
        normalized_version = str(Version(version))
    except InvalidVersion as exc:
        raise ManagedPackBuildError(
            f"Carlina wheel has an invalid version: {version!r}."
        ) from exc
    return {
        "path": path,
        "name": name,
        "version": normalized_version,
        "sha256": _sha256(path),
        "size": path.stat().st_size,
        "application_profile": (
            distribution.profile.value if distribution is not None else "workbench"
        ),
        "source_revision": (
            distribution.source_revision if distribution is not None else None
        ),
    }


def pack_filename(
    version: str,
    platform: str,
    application_profile: str = "workbench",
) -> str:
    try:
        normalized_version = str(Version(version))
    except InvalidVersion as exc:
        raise ManagedPackBuildError(f"Invalid managed version: {version!r}.") from exc
    if platform not in SUPPORTED_PLATFORMS:
        raise ManagedPackBuildError(f"Unsupported managed platform: {platform}")
    if application_profile not in SUPPORTED_APPLICATION_PROFILES:
        raise ManagedPackBuildError(
            f"Unsupported application profile: {application_profile!r}."
        )
    extension = ".zip" if platform.startswith("win-") else ".sh"
    prefix = "carlina-documents" if application_profile == "documents" else "carlina"
    return f"{prefix}-{normalized_version}-{platform}{extension}"


def _validate_pixi_environment(
    manifest_path: Path,
    lock_path: Path,
    environment: str,
) -> None:
    try:
        manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
        lock = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, yaml.YAMLError) as exc:
        raise ManagedPackBuildError(
            "Selected Pixi environment could not be checked in manifest and lock."
        ) from exc
    manifest_environments = manifest.get("environments")
    lock_environments = lock.get("environments") if isinstance(lock, dict) else None
    if (
        not isinstance(manifest_environments, dict)
        or not isinstance(manifest_environments.get(environment), dict)
        or not isinstance(lock_environments, dict)
        or not isinstance(lock_environments.get(environment), dict)
    ):
        raise ManagedPackBuildError(
            f"Selected Pixi environment {environment!r} must exist in both "
            "manifest and lock."
        )


def pixi_pack_command(
    *,
    pixi: str,
    manifest: Path,
    wheel: Path,
    platform: str,
    output: Path,
    environment: str = "runtime",
    pixi_pack_version: str = PIXI_PACK_VERSION,
    companion_wheel: Path | None = None,
) -> list[str]:
    if environment not in SUPPORTED_PIXI_ENVIRONMENTS:
        raise ManagedPackBuildError(f"Unsupported Pixi environment: {environment!r}.")
    if companion_wheel is not None and environment != "documents":
        raise ManagedPackBuildError(
            "Application companions require the Documents environment."
        )
    return [
        pixi,
        "exec",
        "--spec",
        f"pixi-pack={pixi_pack_version}",
        "pixi-pack",
        str(manifest),
        "--environment",
        environment,
        "--platform",
        platform,
        "--output-file",
        str(output),
        "--inject",
        str(wheel),
        *([str(companion_wheel)] if companion_wheel is not None else []),
        "--ignore-pypi-non-wheel",
        "--create-executable",
    ]


def build_managed_pack(
    wheel: str | os.PathLike[str],
    *,
    platform: str,
    output_dir: str | os.PathLike[str],
    manifest: str | os.PathLike[str] = PROJECT_ROOT / "pixi.toml",
    pixi: str = "pixi",
    pixi_pack_version: str = PIXI_PACK_VERSION,
    force: bool = False,
    application_profile: str | None = None,
    companion_wheel: str | os.PathLike[str] | None = None,
) -> tuple[Path, Path]:
    if platform not in SUPPORTED_PLATFORMS:
        raise ManagedPackBuildError(f"Unsupported managed platform: {platform}")
    wheel_report = inspect_wheel(wheel)
    wheel_profile = str(wheel_report["application_profile"])
    if application_profile is None:
        selected_profile = wheel_profile
    elif application_profile not in SUPPORTED_APPLICATION_PROFILES:
        raise ManagedPackBuildError(
            f"Unsupported application profile: {application_profile!r}."
        )
    elif application_profile != wheel_profile:
        raise ManagedPackBuildError(
            "Requested application profile does not match wheel descriptor."
        )
    else:
        selected_profile = application_profile
    companion_path = None
    companion_report = None
    if companion_wheel is not None:
        if selected_profile != "documents":
            raise ManagedPackBuildError(
                "Application companions require the Documents profile."
            )
        companion_path = Path(companion_wheel).expanduser()
        try:
            companions = validate_application_companions_wheel(
                Path(wheel).expanduser(), companion_path, platform=platform
            )
        except ApplicationCompanionsPackagingError:
            raise ManagedPackBuildError(
                "Application companion wheel does not match this build."
            ) from None
        if companions.wheel_sha256 != wheel_report["sha256"]:
            raise ManagedPackBuildError("Application wheel changed during validation.")
        companion_report = {
            "filename": companion_path.name,
            "sha256": _sha256(companion_path),
            "size": companion_path.stat().st_size,
        }
    manifest_path = Path(manifest).expanduser().resolve()
    lock_path = manifest_path.with_name("pixi.lock")
    if not manifest_path.is_file() or not lock_path.is_file():
        raise ManagedPackBuildError(
            "Managed packs require both the exact pixi.toml and pixi.lock."
        )
    environment = "documents" if selected_profile == "documents" else "runtime"
    _validate_pixi_environment(manifest_path, lock_path, environment)
    destination_dir = Path(output_dir).expanduser().resolve()
    destination_dir.mkdir(parents=True, exist_ok=True)
    output = destination_dir / pack_filename(
        wheel_report["version"], platform, application_profile=selected_profile
    )
    metadata_path = output.with_suffix(output.suffix + ".artifact.json")
    for path in (output, metadata_path):
        if path.exists() and not force:
            raise ManagedPackBuildError(f"Output already exists: {path}")

    nonce = uuid.uuid4().hex
    # pixi-pack normalizes executable output names from the final suffix. Keep
    # that suffix last or it may silently rewrite the requested path. Windows
    # uses a short-lived SFX only as the source for the verified ZIP bundle.
    temporary_output = output.with_name(f".{output.stem}.part-{nonce}{output.suffix}")
    pixi_output = (
        output.with_name(f".{output.stem}.pixi-pack-{nonce}.ps1")
        if platform.startswith("win-")
        else temporary_output
    )
    temporary_metadata = metadata_path.with_name(f".{metadata_path.name}.part-{nonce}")

    command = pixi_pack_command(
        pixi=pixi,
        manifest=manifest_path,
        wheel=wheel_report["path"],
        platform=platform,
        output=pixi_output,
        environment=environment,
        pixi_pack_version=pixi_pack_version,
        companion_wheel=companion_path,
    )
    try:
        result = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode != 0:
            raise ManagedPackBuildError(
                f"pixi-pack failed for {platform} with exit code {result.returncode}."
            )
        if not pixi_output.is_file() or pixi_output.stat().st_size <= 0:
            raise ManagedPackBuildError(
                f"pixi-pack did not create a valid temporary output for {output}."
            )
        if _sha256(wheel_report["path"]) != wheel_report["sha256"] or (
            companion_path is not None
            and companion_report is not None
            and _sha256(companion_path) != companion_report["sha256"]
        ):
            raise ManagedPackBuildError("Pack input changed during the build.")
        if platform.startswith("win-"):
            build_windows_bundle(pixi_output, temporary_output)
        timestamp = _build_timestamp()
        metadata = {
            "format": "carlina.managed-artifact",
            "schema_version": 1,
            "version": wheel_report["version"],
            "platform": platform,
            "kind": (
                "pixi-pack-bundle"
                if platform.startswith("win-")
                else "pixi-pack-executable"
            ),
            "filename": output.name,
            "sha256": _sha256(temporary_output),
            "size": temporary_output.stat().st_size,
            "wheel": {
                "filename": wheel_report["path"].name,
                "sha256": wheel_report["sha256"],
                "size": wheel_report["size"],
            },
            "pixi_pack_version": pixi_pack_version,
            "pixi_lock_sha256": _sha256(lock_path),
            "built_at": timestamp,
            "source_revision": (
                wheel_report["source_revision"]
                if selected_profile == "documents"
                else _source_revision()
            ),
        }
        if selected_profile == "documents":
            metadata["application_profile"] = selected_profile
        if companion_report is not None:
            metadata["companion_wheel"] = companion_report
        temporary_metadata.write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_output, output)
        os.replace(temporary_metadata, metadata_path)
        return output, metadata_path
    finally:
        pixi_output.unlink(missing_ok=True)
        temporary_output.unlink(missing_ok=True)
        temporary_metadata.unlink(missing_ok=True)


def _build_timestamp() -> str:
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch:
        try:
            value = datetime.fromtimestamp(int(epoch), UTC)
        except ValueError as exc:
            raise ManagedPackBuildError(
                "SOURCE_DATE_EPOCH must be an integer."
            ) from exc
    else:
        value = datetime.now(UTC)
    return value.isoformat().replace("+00:00", "Z")


def _source_revision() -> str | None:
    configured = os.environ.get("GITHUB_SHA")
    if configured:
        return configured
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build an offline Carlina Pixi runtime artifact."
    )
    parser.add_argument("--wheel", required=True)
    parser.add_argument("--companion-wheel")
    parser.add_argument(
        "--platform", required=True, choices=sorted(SUPPORTED_PLATFORMS)
    )
    parser.add_argument("--output-dir", default="dist/managed")
    parser.add_argument("--manifest", default=str(PROJECT_ROOT / "pixi.toml"))
    parser.add_argument("--pixi", default="pixi")
    parser.add_argument("--pixi-pack-version", default=PIXI_PACK_VERSION)
    parser.add_argument(
        "--application-profile",
        choices=sorted(SUPPORTED_APPLICATION_PROFILES),
        default=None,
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        output, metadata = build_managed_pack(
            args.wheel,
            platform=args.platform,
            output_dir=args.output_dir,
            manifest=args.manifest,
            pixi=args.pixi,
            pixi_pack_version=args.pixi_pack_version,
            force=args.force,
            application_profile=args.application_profile,
            companion_wheel=args.companion_wheel,
        )
    except (ManagedPackBuildError, OSError) as exc:
        print(f"Managed pack build failed: {exc}", file=sys.stderr)
        return 1
    print(output)
    print(metadata)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
