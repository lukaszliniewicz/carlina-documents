"""Strict Windows managed-runtime bundle contract and extraction."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

WINDOWS_BUNDLE_FORMAT = "carlina.windows-pixi-pack"
WINDOWS_BUNDLE_SCHEMA_VERSION = 1
WINDOWS_BUNDLE_MANIFEST = "bundle.json"
WINDOWS_BUNDLE_PAYLOADS = ("archive.tar", "pixi-unpack.exe")
WINDOWS_BUNDLE_MEMBERS = frozenset((*WINDOWS_BUNDLE_PAYLOADS, WINDOWS_BUNDLE_MANIFEST))
MAX_WINDOWS_BUNDLE_MANIFEST_BYTES = 64 * 1024
MAX_WINDOWS_BUNDLE_MEMBER_BYTES = 20 * 1024**3
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class WindowsBundleError(ValueError):
    """A Windows managed-runtime bundle violates its public contract."""


@dataclass(frozen=True, slots=True)
class WindowsBundleMember:
    sha256: str
    size: int


def _member_record(value: object, *, name: str) -> WindowsBundleMember:
    if not isinstance(value, Mapping) or set(value) != {"sha256", "size"}:
        raise WindowsBundleError(
            f"Windows bundle member {name!r} must contain only sha256 and size."
        )
    sha256 = value.get("sha256")
    size = value.get("size")
    if not isinstance(sha256, str) or not _SHA256_PATTERN.fullmatch(sha256):
        raise WindowsBundleError(
            f"Windows bundle member {name!r} has an invalid SHA-256 digest."
        )
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or size < 1
        or size > MAX_WINDOWS_BUNDLE_MEMBER_BYTES
    ):
        raise WindowsBundleError(f"Windows bundle member {name!r} has an invalid size.")
    return WindowsBundleMember(sha256=sha256, size=size)


def parse_windows_bundle_manifest(
    payload: bytes | str | Mapping[str, object],
) -> dict[str, WindowsBundleMember]:
    """Parse the bounded, strict internal manifest from an untrusted bundle."""

    if isinstance(payload, bytes):
        if len(payload) > MAX_WINDOWS_BUNDLE_MANIFEST_BYTES:
            raise WindowsBundleError("Windows bundle manifest exceeds the size limit.")
        try:
            raw: object = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WindowsBundleError(
                "Windows bundle manifest is not valid UTF-8 JSON."
            ) from exc
    elif isinstance(payload, str):
        return parse_windows_bundle_manifest(payload.encode("utf-8"))
    else:
        raw = dict(payload)
    if not isinstance(raw, Mapping) or set(raw) != {
        "format",
        "schema_version",
        "members",
    }:
        raise WindowsBundleError(
            "Windows bundle manifest contains unexpected or missing fields."
        )
    if (
        raw.get("format") != WINDOWS_BUNDLE_FORMAT
        or raw.get("schema_version") != WINDOWS_BUNDLE_SCHEMA_VERSION
    ):
        raise WindowsBundleError("Unsupported Windows bundle format.")
    members = raw.get("members")
    if not isinstance(members, Mapping) or set(members) != set(WINDOWS_BUNDLE_PAYLOADS):
        raise WindowsBundleError(
            "Windows bundle manifest must describe exactly the runtime payloads."
        )
    return {
        name: _member_record(members[name], name=name)
        for name in WINDOWS_BUNDLE_PAYLOADS
    }


def windows_bundle_manifest_bytes(
    members: Mapping[str, WindowsBundleMember | Mapping[str, object]],
) -> bytes:
    """Serialize a canonical manifest after validating the supplied records."""

    normalized = {
        name: (
            value
            if isinstance(value, WindowsBundleMember)
            else _member_record(value, name=name)
        )
        for name, value in members.items()
    }
    if set(normalized) != set(WINDOWS_BUNDLE_PAYLOADS):
        raise WindowsBundleError(
            "Windows bundle manifest must describe exactly the runtime payloads."
        )
    payload = {
        "format": WINDOWS_BUNDLE_FORMAT,
        "schema_version": WINDOWS_BUNDLE_SCHEMA_VERSION,
        "members": {
            name: {
                "sha256": normalized[name].sha256,
                "size": normalized[name].size,
            }
            for name in WINDOWS_BUNDLE_PAYLOADS
        },
    }
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    parse_windows_bundle_manifest(encoded)
    return encoded


def _validate_zip_members(
    archive: zipfile.ZipFile,
) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    names = [item.filename for item in infos]
    folded = [name.casefold() for name in names]
    if len(folded) != len(set(folded)):
        raise WindowsBundleError(
            "Windows bundle contains duplicate case-insensitive member names."
        )
    if len(infos) != len(WINDOWS_BUNDLE_MEMBERS) or set(names) != set(
        WINDOWS_BUNDLE_MEMBERS
    ):
        raise WindowsBundleError(
            "Windows bundle must contain exactly archive.tar, pixi-unpack.exe, "
            "and bundle.json at its root."
        )
    by_name = {item.filename: item for item in infos}
    for item in infos:
        unix_mode = (item.external_attr >> 16) & 0xFFFF
        if (
            item.is_dir()
            or stat.S_ISLNK(unix_mode)
            or item.compress_type != zipfile.ZIP_STORED
            or item.file_size != item.compress_size
        ):
            raise WindowsBundleError(
                f"Windows bundle member {item.filename!r} has unsafe ZIP metadata."
            )
    manifest = by_name[WINDOWS_BUNDLE_MANIFEST]
    if manifest.file_size > MAX_WINDOWS_BUNDLE_MANIFEST_BYTES:
        raise WindowsBundleError("Windows bundle manifest exceeds the size limit.")
    return by_name


def extract_windows_bundle(
    bundle: str | os.PathLike[str],
    destination: str | os.PathLike[str],
) -> dict[str, Path]:
    """Verify and stream-extract a Windows runtime bundle into an empty directory."""

    source = Path(bundle).expanduser().resolve()
    target = Path(destination).expanduser().resolve()
    if not source.is_file():
        raise WindowsBundleError(f"Windows bundle does not exist: {source}")
    if not target.is_dir() or any(target.iterdir()):
        raise WindowsBundleError(
            "Windows bundle extraction destination must be an empty directory."
        )
    outputs: dict[str, Path] = {}
    try:
        with zipfile.ZipFile(source) as archive:
            infos = _validate_zip_members(archive)
            manifest = parse_windows_bundle_manifest(
                archive.read(WINDOWS_BUNDLE_MANIFEST)
            )
            for name in WINDOWS_BUNDLE_PAYLOADS:
                expected = manifest[name]
                info = infos[name]
                if info.file_size != expected.size:
                    raise WindowsBundleError(
                        f"Windows bundle member {name!r} has an unexpected size."
                    )
                output = target / name
                digest = hashlib.sha256()
                written = 0
                try:
                    with (
                        archive.open(info, "r") as input_stream,
                        output.open("xb") as output_stream,
                    ):
                        while chunk := input_stream.read(4 * 1024 * 1024):
                            written += len(chunk)
                            if written > expected.size:
                                raise WindowsBundleError(
                                    f"Windows bundle member {name!r} exceeds its declared size."
                                )
                            digest.update(chunk)
                            output_stream.write(chunk)
                        output_stream.flush()
                        os.fsync(output_stream.fileno())
                except Exception:
                    output.unlink(missing_ok=True)
                    raise
                if written != expected.size or digest.hexdigest() != expected.sha256:
                    output.unlink(missing_ok=True)
                    raise WindowsBundleError(
                        f"Windows bundle member {name!r} failed integrity verification."
                    )
                outputs[name] = output
    except zipfile.BadZipFile as exc:
        for name in WINDOWS_BUNDLE_PAYLOADS:
            (target / name).unlink(missing_ok=True)
        raise WindowsBundleError("Windows bundle is not a valid ZIP archive.") from exc
    except Exception:
        for name in WINDOWS_BUNDLE_PAYLOADS:
            (target / name).unlink(missing_ok=True)
        raise
    return outputs


__all__ = [
    "MAX_WINDOWS_BUNDLE_MANIFEST_BYTES",
    "MAX_WINDOWS_BUNDLE_MEMBER_BYTES",
    "WINDOWS_BUNDLE_FORMAT",
    "WINDOWS_BUNDLE_MANIFEST",
    "WINDOWS_BUNDLE_MEMBERS",
    "WINDOWS_BUNDLE_PAYLOADS",
    "WINDOWS_BUNDLE_SCHEMA_VERSION",
    "WindowsBundleError",
    "WindowsBundleMember",
    "extract_windows_bundle",
    "parse_windows_bundle_manifest",
    "windows_bundle_manifest_bytes",
]
