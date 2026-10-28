#!/usr/bin/env python3
"""Build a native online artifact from verified publisher preparation inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if os.fspath(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, os.fspath(PROJECT_ROOT))

from carlina.core.publisher_bootstrap import (  # noqa: E402
    PublisherBootstrapError,
    PublisherBootstrapPlan,
    parse_publisher_bootstrap_plan,
)
from carlina.core.publisher_bootstrap_scripts import (  # noqa: E402
    MAX_EMBEDDED_SKELETON_BYTES,
    PUBLISHER_BOOTSTRAP_ARTIFACT_KIND,
    publisher_bootstrap_filename,
    render_publisher_bootstrap,
)

_METADATA_KEYS = frozenset(
    {
        "format",
        "schema_version",
        "version",
        "platform",
        "kind",
        "filename",
        "sha256",
        "size",
        "built_at",
        "source_revision",
        "pixi_lock_sha256",
        "pixi_pack_version",
        "application_profile",
        "wheel",
        "companion_wheel",
    }
)
_REQUIRED_METADATA_KEYS = _METADATA_KEYS - {"companion_wheel"}


class PublisherBootstrapBuildError(ValueError):
    """The preparation inputs do not bind a publishable native artifact."""


def _identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _read_regular(path: Path, limit: int) -> bytes:
    before = path.lstat()
    attributes = getattr(before, "st_file_attributes", 0)
    if (
        not stat.S_ISREG(before.st_mode)
        or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        or before.st_size > limit
    ):
        raise PublisherBootstrapBuildError(
            "A build input is unsafe or exceeds its size limit."
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise PublisherBootstrapBuildError(
                "A build input changed while it was opened."
            )
        payload = stream.read(limit + 1)
        if len(payload) != before.st_size or _identity(
            os.fstat(stream.fileno())
        ) != _identity(before):
            raise PublisherBootstrapBuildError(
                "A build input changed while it was read."
            )
    if _identity(path.lstat()) != _identity(before):
        raise PublisherBootstrapBuildError("A build input changed while it was read.")
    return payload


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PublisherBootstrapBuildError(
                "A build input contains duplicate JSON fields."
            )
        result[key] = value
    return result


def _reject_constant(_value: str) -> object:
    raise PublisherBootstrapBuildError(
        "A build input contains a non-finite JSON value."
    )


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise PublisherBootstrapBuildError(
            "A build input contains an unexpected JSON shape."
        )
    row = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in row):
        raise PublisherBootstrapBuildError(
            "A build input contains an unexpected JSON shape."
        )
    return cast(dict[str, object], row)


def _load_object(payload: bytes) -> dict[str, object]:
    try:
        return _object(
            json.loads(
                payload,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublisherBootstrapBuildError(
            "A build input is not valid UTF-8 JSON."
        ) from exc


def _matches_blob(record: object, payload: bytes) -> bool:
    row = _object(record)
    return (
        type(row.get("size")) is int
        and row["size"] == len(payload)
        and row.get("sha256") == hashlib.sha256(payload).hexdigest()
    )


def _public_blob(record: object) -> dict[str, object]:
    row = _object(record)
    filename = row.get("filename")
    if (
        set(row) != {"filename", "size", "sha256"}
        or not isinstance(filename, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,199}", filename)
        or type(row.get("size")) is not int
        or cast(int, row["size"]) < 1
        or not isinstance(row.get("sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", cast(str, row["sha256"]))
    ):
        raise PublisherBootstrapBuildError("A public artifact record is invalid.")
    return dict(row)


def _metadata(
    plan: PublisherBootstrapPlan,
    preparation: Mapping[str, object],
    original: dict[str, object],
    inputs: Mapping[str, bytes],
    artifact: bytes,
) -> dict[str, object]:
    original_fields = frozenset(original)
    if (
        preparation.get("format") != "carlina.publisher-bootstrap.preparation"
        or type(preparation.get("schema_version")) is not int
        or preparation.get("schema_version") != 1
        or original.get("format") != "carlina.managed-artifact"
        or type(original.get("schema_version")) is not int
        or original.get("schema_version") != 1
        or not original_fields >= _REQUIRED_METADATA_KEYS
        or original_fields - _METADATA_KEYS
    ):
        raise PublisherBootstrapBuildError(
            "The preparation or original artifact metadata has an unsupported format."
        )
    application = _object(preparation.get("application"))
    required_application_fields = frozenset(
        {"platform", "application_profile", "version", "source_revision", "wheel"}
    )
    application_fields = frozenset(application)
    has_companion = "companion_wheel" in original_fields
    if (
        not required_application_fields <= application_fields
        or application_fields - (required_application_fields | {"companion_wheel"})
        or ("companion_wheel" in application_fields) != has_companion
        or (
            original.get("platform"),
            original.get("application_profile"),
            original.get("version"),
        )
        != (plan.platform, plan.application_profile, plan.version)
        or any(
            application.get(k) != original.get(k)
            for k in (
                "platform",
                "application_profile",
                "version",
                "source_revision",
                "wheel",
            )
        )
        or (
            has_companion
            and application.get("companion_wheel") != original.get("companion_wheel")
        )
        or original.get("pixi_pack_version") != "0.7.10"
        or original.get("kind")
        != ("pixi-pack-bundle" if plan.platform == "win-64" else "pixi-pack-executable")
        or not re.fullmatch(r"[0-9a-f]{40}", str(original.get("source_revision")))
    ):
        raise PublisherBootstrapBuildError(
            "The preparation identity does not match its original managed artifact."
        )
    outputs = _object(preparation.get("outputs"))
    origins = _object(preparation.get("inputs"))
    source_pack = _public_blob(origins.get("source_pack"))
    original_pack = _public_blob(
        {key: original.get(key) for key in ("filename", "size", "sha256")}
    )
    wheel = _public_blob(original.get("wheel"))
    companion = _public_blob(original["companion_wheel"]) if has_companion else None
    if (
        source_pack != original_pack
        or wheel["filename"] != f"carlina-{plan.version}-py3-none-any.whl"
        or (
            companion is not None
            and companion["filename"]
            != f"carlina_application_companions-{plan.version}-py3-none-any.whl"
        )
        or (companion is not None and companion["filename"] == wheel["filename"])
        or not isinstance(original.get("pixi_lock_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", cast(str, original["pixi_lock_sha256"]))
    ):
        raise PublisherBootstrapBuildError("The public artifact provenance is invalid.")
    built_at = original.get("built_at")
    try:
        if (
            not isinstance(built_at, str)
            or datetime.fromisoformat(built_at).utcoffset() is None
        ):
            raise ValueError("timestamp is missing its time zone")
    except ValueError as exc:
        raise PublisherBootstrapBuildError(
            "The original artifact timestamp is invalid."
        ) from exc
    if (
        not _matches_blob(outputs.get("plan"), inputs["plan"])
        or not _matches_blob(outputs.get("skeleton"), inputs["skeleton"])
        or not _matches_blob(origins.get("artifact_metadata"), inputs["metadata"])
        or preparation.get("original_archive")
        != {"size": plan.archive.size, "sha256": plan.archive.sha256}
    ):
        raise PublisherBootstrapBuildError(
            "The preparation files do not match their recorded hashes."
        )
    metadata = dict(original)
    metadata.update(
        {
            "kind": PUBLISHER_BOOTSTRAP_ARTIFACT_KIND,
            "filename": publisher_bootstrap_filename(plan),
            "size": len(artifact),
            "sha256": hashlib.sha256(artifact).hexdigest(),
            "publisher_bootstrap": {
                "schema_version": 1,
                "plan_sha256": hashlib.sha256(inputs["plan"]).hexdigest(),
                "skeleton_sha256": plan.skeleton.sha256,
                "archive": {"size": plan.archive.size, "sha256": plan.archive.sha256},
                "source_pack": source_pack,
                "unpacker": {
                    "name": plan.unpacker.name,
                    "size": plan.unpacker.size,
                    "sha256": plan.unpacker.sha256,
                },
            },
        }
    )
    return metadata


def build_publisher_bootstrap(
    preparation_dir: Path, artifact_metadata: Path, output_dir: Path
) -> tuple[Path, Path]:
    """Publish a new script and hash-bound sidecar without changing existing output."""

    inputs = {
        "plan": (preparation_dir / "plan.json", 1024 * 1024),
        "skeleton": (preparation_dir / "skeleton.tar.gz", MAX_EMBEDDED_SKELETON_BYTES),
        "preparation": (preparation_dir / "preparation.json", 4 * 1024 * 1024),
        "metadata": (artifact_metadata, 64 * 1024),
    }
    payloads = {
        key: _read_regular(path, limit) for key, (path, limit) in inputs.items()
    }
    plan = parse_publisher_bootstrap_plan(payloads["plan"])
    artifact = render_publisher_bootstrap(plan, payloads["skeleton"])
    metadata = _metadata(
        plan,
        _load_object(payloads["preparation"]),
        _load_object(payloads["metadata"]),
        payloads,
        artifact,
    )
    encoded = (json.dumps(metadata, sort_keys=True, indent=2) + "\n").encode("utf-8")
    filename = publisher_bootstrap_filename(plan)
    blobs = {filename: artifact, filename + ".artifact.json": encoded}
    destination = output_dir.expanduser().absolute()
    destination.mkdir(parents=True, exist_ok=False)
    published: list[tuple[Path, tuple[int, int], bytes]] = []
    try:
        with tempfile.TemporaryDirectory(
            prefix=".publisher-build-", dir=destination
        ) as temporary:
            temporary_root = Path(temporary)
            for name, payload in blobs.items():
                staging = temporary_root / name
                with staging.open("xb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                staging.chmod(0o700 if name.endswith(".sh") else 0o600)
            for key, (path, limit) in inputs.items():
                if _read_regular(path, limit) != payloads[key]:
                    raise PublisherBootstrapBuildError(
                        "A build input changed before publication."
                    )
            for name, payload in blobs.items():
                target = destination / name
                os.link(temporary_root / name, target)
                info = target.lstat()
                published.append((target, (info.st_dev, info.st_ino), payload))
            for key, (path, limit) in inputs.items():
                if _read_regular(path, limit) != payloads[key]:
                    raise PublisherBootstrapBuildError(
                        "A build input changed during publication."
                    )
    except BaseException:
        for target, expected, payload in reversed(published):
            try:
                info = target.lstat()
                if (
                    stat.S_ISREG(info.st_mode)
                    and (info.st_dev, info.st_ino) == expected
                    and target.read_bytes() == payload
                ):
                    target.unlink()
            except OSError:
                pass
        with suppress(OSError):
            destination.rmdir()
        raise
    return destination / filename, destination / (filename + ".artifact.json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a native Documents online bootstrap from verified preparation inputs."
    )
    parser.add_argument("--preparation-dir", required=True, type=Path)
    parser.add_argument("--artifact-metadata", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        artifact, metadata = build_publisher_bootstrap(
            args.preparation_dir, args.artifact_metadata, args.output_dir
        )
    except (PublisherBootstrapBuildError, PublisherBootstrapError, OSError) as exc:
        print(f"Publisher bootstrap build failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {"artifact": str(artifact), "metadata": str(metadata)}, sort_keys=True
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
