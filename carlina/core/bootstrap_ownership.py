"""Native bootstrap claims and immutable generation ownership bindings."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

from packaging.version import InvalidVersion, Version

from carlina.core.native_preparation import (
    MAX_PREPARATION_BYTES,
    PREPARATION_NAME,
    PREPARATION_PROTOCOL_VERSION,
    PreparationRecordError,
    match_preparation_completion,
)

BOOTSTRAP_CLAIM_PROTOCOL_VERSION = 1
BOOTSTRAP_RESUME_PROTOCOL_VERSION = 1
BOOTSTRAP_PREPARATION_PROTOCOL_VERSION = PREPARATION_PROTOCOL_VERSION
BOOTSTRAP_CLAIM_NAME = ".carlina-bootstrap-claim"
BOOTSTRAP_BINDING_NAME = ".carlina-bootstrap-binding.json"
BOOTSTRAP_PREPARATION_BINDING_NAME = ".carlina-bootstrap-preparation-binding.json"
BOOTSTRAP_CLAIM_HEADER = "carlina.bootstrap-claim/1"

_ERROR_MESSAGE = "Bootstrap generation ownership is invalid."
_CLAIM_ID = re.compile(r"[0-9a-f]{32}\Z")
_CLAIM_ID_BYTES = re.compile(rb"[0-9a-f]{32}\Z")
_LOWER_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_RELEASE_BASENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+!-]{0,79}\Z")
_ARTIFACT_KINDS = frozenset(
    {
        "pixi-pack-executable",
        "pixi-pack-bundle",
        "publisher-bootstrap-executable",
    }
)
_BINDING_FIELDS = frozenset(
    {
        "format",
        "schema_version",
        "claim_id",
        "identity_sha256",
        "program_root",
        "data_root",
    }
)
_BINDING_FORMAT = "carlina.bootstrap-binding"
_BINDING_SCHEMA_VERSION = 1
_MAX_CLAIM_BYTES = 256
_MAX_BINDING_BYTES = 65_536


class BootstrapOwnershipError(ValueError):
    """A generation claim or binding is absent, malformed, or conflicting."""

    def __init__(self) -> None:
        super().__init__(_ERROR_MESSAGE)


def _invalid() -> BootstrapOwnershipError:
    return BootstrapOwnershipError()


@dataclass(frozen=True, slots=True)
class BootstrapIdentity:
    """The signed release facts frozen by one native bootstrap claim."""

    application_profile: str
    release_version: str
    channel: str
    index_sha256: str
    artifact_platform: str
    artifact_kind: str
    artifact_filename: str
    artifact_sha256: str
    artifact_size: int

    def __post_init__(self) -> None:
        if type(
            self.application_profile
        ) is not str or self.application_profile not in {
            "documents",
            "workbench",
        }:
            raise _invalid()
        if type(self.release_version) is not str:
            raise _invalid()
        if _RELEASE_BASENAME.fullmatch(self.release_version) is None:
            raise _invalid()
        try:
            if str(Version(self.release_version)) != self.release_version:
                raise _invalid()
        except InvalidVersion:
            raise _invalid() from None
        if type(self.channel) is not str or self.channel not in {"stable", "preview"}:
            raise _invalid()
        if (
            type(self.index_sha256) is not str
            or _LOWER_SHA256.fullmatch(self.index_sha256) is None
        ):
            raise _invalid()
        if type(self.artifact_platform) is not str or self.artifact_platform not in {
            "linux-64",
            "linux-aarch64",
            "win-64",
            "win-arm64",
            "osx-64",
            "osx-arm64",
        }:
            raise _invalid()
        if (
            type(self.artifact_kind) is not str
            or self.artifact_kind not in _ARTIFACT_KINDS
        ):
            raise _invalid()
        if (
            type(self.artifact_filename) is not str
            or not self.artifact_filename
            or len(self.artifact_filename) > 255
            or self.artifact_filename in {".", ".."}
            or any(
                character in self.artifact_filename
                for character in ("/", "\\", "\0", "\r", "\n")
            )
        ):
            raise _invalid()
        if (
            type(self.artifact_sha256) is not str
            or _LOWER_SHA256.fullmatch(self.artifact_sha256) is None
        ):
            raise _invalid()
        if type(self.artifact_size) is not int or self.artifact_size <= 0:
            raise _invalid()

    @property
    def digest(self) -> str:
        """Return the digest of the exact canonical identity representation."""

        canonical = json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


def _require_identity(identity: object) -> BootstrapIdentity:
    if type(identity) is not BootstrapIdentity:
        raise _invalid()
    return identity


def _require_path(path: object) -> Path:
    if not isinstance(path, Path):
        raise _invalid()
    return path


def _validate_claim_id(claim_id: object) -> str:
    if type(claim_id) is not str or _CLAIM_ID.fullmatch(claim_id) is None:
        raise _invalid()
    return claim_id


def bootstrap_claim_bytes(identity: BootstrapIdentity, claim_id: str) -> bytes:
    """Build the exact ASCII claim frame used by native bootstrap writers."""

    frozen_identity = _require_identity(identity)
    nonce = _validate_claim_id(claim_id)
    return f"{BOOTSTRAP_CLAIM_HEADER}\n{nonce}\n{frozen_identity.digest}\n".encode(
        "ascii"
    )


def _lstat_or_none(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError, ValueError:
        raise _invalid() from None


def _path_is_junction(path: Path) -> bool:
    checker = getattr(path, "is_junction", None)
    if checker is None:
        return False
    try:
        return checker()
    except OSError, RuntimeError, ValueError:
        raise _invalid() from None


def _require_generation_directory(
    version_dir: Path, identity: BootstrapIdentity
) -> None:
    if (
        version_dir.name != identity.release_version
        or version_dir.parent.name != "versions"
    ):
        raise _invalid()
    for directory in (version_dir.parent, version_dir):
        info = _lstat_or_none(directory)
        if (
            info is None
            or stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or _path_is_junction(directory)
        ):
            raise _invalid()


def _marker_path(version_dir: Path, marker_name: str) -> Path:
    return version_dir / marker_name


def bootstrap_markers_present(version_dir: Path) -> bool:
    """Report whether a reserved ownership marker exists, including links."""

    directory = _require_path(version_dir)
    claim = _lstat_or_none(_marker_path(directory, BOOTSTRAP_CLAIM_NAME))
    binding = _lstat_or_none(_marker_path(directory, BOOTSTRAP_BINDING_NAME))
    preparation = _lstat_or_none(_marker_path(directory, PREPARATION_NAME))
    preparation_binding = _lstat_or_none(
        _marker_path(directory, BOOTSTRAP_PREPARATION_BINDING_NAME)
    )
    return any(
        item is not None for item in (claim, binding, preparation, preparation_binding)
    )


def _same_file_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return first.st_dev == second.st_dev and first.st_ino == second.st_ino


def _read_regular_file(
    path: Path, *, maximum_bytes: int, expected_info: os.stat_result | None = None
) -> bytes | None:
    info = _lstat_or_none(path)
    if info is None:
        return None
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or (expected_info is not None and not _same_file_identity(info, expected_info))
        or info.st_size > maximum_bytes
    ):
        raise _invalid()

    descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags)
        opened_info = os.fstat(descriptor)
        if not stat.S_ISREG(opened_info.st_mode) or not _same_file_identity(
            info, opened_info
        ):
            raise _invalid()

        chunks: list[bytes] = []
        total = 0
        while total <= maximum_bytes:
            chunk = os.read(descriptor, maximum_bytes + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > maximum_bytes:
            raise _invalid()
        return b"".join(chunks)
    except BootstrapOwnershipError:
        raise
    except OSError, ValueError:
        raise _invalid() from None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                raise _invalid() from None


def _decode_claim(payload: bytes, identity: BootstrapIdentity) -> str:
    lines = payload.split(b"\n")
    if (
        len(lines) != 4
        or lines[0] != BOOTSTRAP_CLAIM_HEADER.encode("ascii")
        or lines[3] != b""
        or _CLAIM_ID_BYTES.fullmatch(lines[1]) is None
        or lines[2] != identity.digest.encode("ascii")
    ):
        raise _invalid()
    try:
        return lines[1].decode("ascii")
    except UnicodeDecodeError:
        raise _invalid() from None


def read_bootstrap_claim(
    version_dir: Path,
    identity: BootstrapIdentity,
    *,
    claim_id: str | None = None,
) -> str | None:
    """Read a valid preparing claim, or return ``None`` for an unmarked legacy tree."""

    directory = _require_path(version_dir)
    frozen_identity = _require_identity(identity)
    expected_claim = None if claim_id is None else _validate_claim_id(claim_id)
    _require_generation_directory(directory, frozen_identity)

    claim_path = _marker_path(directory, BOOTSTRAP_CLAIM_NAME)
    binding_path = _marker_path(directory, BOOTSTRAP_BINDING_NAME)
    claim_info = _lstat_or_none(claim_path)
    binding_info = _lstat_or_none(binding_path)
    if claim_info is None:
        if binding_info is not None or any(
            _lstat_or_none(_marker_path(directory, name)) is not None
            for name in (PREPARATION_NAME, BOOTSTRAP_PREPARATION_BINDING_NAME)
        ):
            raise _invalid()
        if (
            _lstat_or_none(claim_path) is not None
            or _lstat_or_none(binding_path) is not None
            or _lstat_or_none(_marker_path(directory, PREPARATION_NAME)) is not None
            or _lstat_or_none(
                _marker_path(directory, BOOTSTRAP_PREPARATION_BINDING_NAME)
            )
            is not None
        ):
            raise _invalid()
        return None

    payload = _read_regular_file(
        claim_path, maximum_bytes=_MAX_CLAIM_BYTES, expected_info=claim_info
    )
    if payload is None:
        raise _invalid()
    nonce = _decode_claim(payload, frozen_identity)
    if expected_claim is not None and nonce != expected_claim:
        raise _invalid()
    return nonce


def _canonical_roots(program_root: Path, data_root: Path) -> tuple[str, str, Path]:
    try:
        canonical_program = program_root.resolve()
        canonical_data = data_root.resolve()
    except OSError, RuntimeError, ValueError:
        raise _invalid() from None
    return str(canonical_program), str(canonical_data), canonical_program


def _require_generation_target(
    version_dir: Path, identity: BootstrapIdentity, canonical_program: Path
) -> None:
    _require_generation_directory(version_dir, identity)
    try:
        resolved_generation = version_dir.resolve()
    except OSError, RuntimeError, ValueError:
        raise _invalid() from None
    if resolved_generation != canonical_program / "versions" / identity.release_version:
        raise _invalid()


def _validate_preparation_record(
    directory: Path,
    identity: BootstrapIdentity,
    claim_id: str,
    canonical_program: Path,
    *,
    required: bool = False,
) -> bool:
    payload = _read_regular_file(
        _marker_path(directory, PREPARATION_NAME), maximum_bytes=MAX_PREPARATION_BYTES
    )
    if payload is None:
        if required:
            raise _invalid()
        return False
    if identity.artifact_kind == "publisher-bootstrap-executable":
        if identity.application_profile != "documents":
            raise _invalid()
    elif identity.artifact_kind not in {"pixi-pack-executable", "pixi-pack-bundle"}:
        raise _invalid()
    if identity.artifact_platform not in {"linux-64", "win-64"}:
        raise _invalid()
    try:
        completion = match_preparation_completion(
            payload,
            claim_id=claim_id,
            identity_sha256=identity.digest,
            program_root=str(canonical_program),
        )
    except PreparationRecordError:
        raise _invalid() from None
    if not completion.succeeded:
        raise _invalid()
    return True


def validate_bootstrap_preparation(
    version_dir: Path,
    identity: BootstrapIdentity,
    *,
    claim_id: str,
    program_root: Path,
) -> str:
    """Validate completed native extraction without asserting data readiness."""

    directory = _require_path(version_dir)
    frozen_identity = _require_identity(identity)
    nonce = _validate_claim_id(claim_id)
    try:
        canonical_program = _require_path(program_root).resolve()
    except OSError, RuntimeError, ValueError:
        raise _invalid() from None
    _require_generation_target(directory, frozen_identity, canonical_program)
    if read_bootstrap_claim(directory, frozen_identity, claim_id=nonce) != nonce:
        raise _invalid()
    _validate_preparation_record(
        directory, frozen_identity, nonce, canonical_program, required=True
    )
    return nonce


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _invalid()
        result[key] = value
    return result


def _reject_json_constant(_: str) -> None:
    raise _invalid()


def _decode_binding(payload: bytes) -> dict[str, object]:
    try:
        parsed: object = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except BootstrapOwnershipError:
        raise
    except UnicodeError, json.JSONDecodeError, RecursionError, ValueError:
        raise _invalid() from None
    if type(parsed) is not dict:
        raise _invalid()
    return cast(dict[str, object], parsed)


def _read_binding_record(
    version_dir: Path, expected_info: os.stat_result | None = None
) -> dict[str, object] | None:
    payload = _read_regular_file(
        _marker_path(version_dir, BOOTSTRAP_BINDING_NAME),
        maximum_bytes=_MAX_BINDING_BYTES,
        expected_info=expected_info,
    )
    return None if payload is None else _decode_binding(payload)


def _validate_binding_record(
    record: dict[str, object],
    *,
    identity: BootstrapIdentity,
    claim_id: str,
    program_root: str,
    data_root: str,
) -> None:
    if record.keys() != _BINDING_FIELDS:
        raise _invalid()
    if (
        type(record["format"]) is not str
        or record["format"] != _BINDING_FORMAT
        or type(record["schema_version"]) is not int
        or record["schema_version"] != _BINDING_SCHEMA_VERSION
        or type(record["claim_id"]) is not str
        or record["claim_id"] != claim_id
        or type(record["identity_sha256"]) is not str
        or record["identity_sha256"] != identity.digest
        or type(record["program_root"]) is not str
        or record["program_root"] != program_root
        or type(record["data_root"]) is not str
        or record["data_root"] != data_root
    ):
        raise _invalid()


def _binding_bytes(
    *,
    identity: BootstrapIdentity,
    claim_id: str,
    program_root: str,
    data_root: str,
) -> bytes:
    record: dict[str, object] = {
        "format": _BINDING_FORMAT,
        "schema_version": _BINDING_SCHEMA_VERSION,
        "claim_id": claim_id,
        "identity_sha256": identity.digest,
        "program_root": program_root,
        "data_root": data_root,
    }
    return (
        json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")


def _existing_binding(
    version_dir: Path,
    *,
    identity: BootstrapIdentity,
    claim_id: str,
    program_root: str,
    data_root: str,
    expected_info: os.stat_result | None = None,
) -> str:
    record = _read_binding_record(version_dir, expected_info)
    if record is None:
        raise _invalid()
    _validate_binding_record(
        record,
        identity=identity,
        claim_id=claim_id,
        program_root=program_root,
        data_root=data_root,
    )
    return claim_id


def _exclusive_write_binding(
    version_dir: Path, payload: bytes, *, marker_name: str = BOOTSTRAP_BINDING_NAME
) -> None:
    path = _marker_path(version_dir, marker_name)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags, 0o600)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise _invalid()
            offset += written
        os.fsync(descriptor)
    except BootstrapOwnershipError:
        raise
    except FileExistsError:
        raise
    except OSError, ValueError:
        raise _invalid() from None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                raise _invalid() from None


def _preparation_binding_intent(
    directory: Path,
    identity: BootstrapIdentity,
    claim_id: str,
    program_root: str,
    data_root: str,
    *,
    allow_create: bool,
) -> None:
    """Retain the first data-root choice before a prepared binding can be lost."""

    path = _marker_path(directory, BOOTSTRAP_PREPARATION_BINDING_NAME)
    payload = _read_regular_file(path, maximum_bytes=_MAX_BINDING_BYTES)
    if payload is None:
        if not allow_create:
            raise _invalid()
        intent = _binding_bytes(
            identity=identity,
            claim_id=claim_id,
            program_root=program_root,
            data_root=data_root,
        )
        with suppress(FileExistsError):
            _exclusive_write_binding(
                directory, intent, marker_name=BOOTSTRAP_PREPARATION_BINDING_NAME
            )
        payload = _read_regular_file(path, maximum_bytes=_MAX_BINDING_BYTES)
    if payload is None:
        raise _invalid()
    _validate_binding_record(
        _decode_binding(payload),
        identity=identity,
        claim_id=claim_id,
        program_root=program_root,
        data_root=data_root,
    )


def bind_bootstrap_generation(
    version_dir: Path,
    identity: BootstrapIdentity,
    *,
    claim_id: str,
    program_root: Path,
    data_root: Path,
) -> str:
    """Bind a preparing generation to canonical roots without replacing a binding."""

    directory = _require_path(version_dir)
    frozen_identity = _require_identity(identity)
    nonce = _validate_claim_id(claim_id)
    program_path = _require_path(program_root)
    data_path = _require_path(data_root)
    canonical_program, canonical_data, canonical_program_path = _canonical_roots(
        program_path, data_path
    )
    _require_generation_target(directory, frozen_identity, canonical_program_path)
    if read_bootstrap_claim(directory, frozen_identity, claim_id=nonce) != nonce:
        raise _invalid()
    prepared = _validate_preparation_record(
        directory, frozen_identity, nonce, canonical_program_path
    )

    binding_path = _marker_path(directory, BOOTSTRAP_BINDING_NAME)
    binding_info = _lstat_or_none(binding_path)
    if prepared:
        _preparation_binding_intent(
            directory,
            frozen_identity,
            nonce,
            canonical_program,
            canonical_data,
            allow_create=binding_info is None,
        )
    elif (
        _lstat_or_none(_marker_path(directory, BOOTSTRAP_PREPARATION_BINDING_NAME))
        is not None
    ):
        raise _invalid()
    if binding_info is not None:
        return _existing_binding(
            directory,
            identity=frozen_identity,
            claim_id=nonce,
            program_root=canonical_program,
            data_root=canonical_data,
            expected_info=binding_info,
        )

    payload = _binding_bytes(
        identity=frozen_identity,
        claim_id=nonce,
        program_root=canonical_program,
        data_root=canonical_data,
    )
    _require_generation_target(directory, frozen_identity, canonical_program_path)
    try:
        _exclusive_write_binding(directory, payload)
    except FileExistsError:
        return _existing_binding(
            directory,
            identity=frozen_identity,
            claim_id=nonce,
            program_root=canonical_program,
            data_root=canonical_data,
        )
    return _existing_binding(
        directory,
        identity=frozen_identity,
        claim_id=nonce,
        program_root=canonical_program,
        data_root=canonical_data,
    )


def validate_bootstrap_generation(
    version_dir: Path,
    identity: BootstrapIdentity,
    *,
    program_root: Path,
    data_root: Path,
    claim_id: str | None = None,
) -> str | None:
    """Validate a ready owned generation, while preserving unmarked legacy trees."""

    directory = _require_path(version_dir)
    frozen_identity = _require_identity(identity)
    expected_claim = None if claim_id is None else _validate_claim_id(claim_id)
    program_path = _require_path(program_root)
    data_path = _require_path(data_root)
    canonical_program, canonical_data, canonical_program_path = _canonical_roots(
        program_path, data_path
    )
    _require_generation_target(directory, frozen_identity, canonical_program_path)
    nonce = read_bootstrap_claim(directory, frozen_identity, claim_id=expected_claim)
    if nonce is None:
        return None
    prepared = _validate_preparation_record(
        directory, frozen_identity, nonce, canonical_program_path
    )
    if prepared:
        _preparation_binding_intent(
            directory,
            frozen_identity,
            nonce,
            canonical_program,
            canonical_data,
            allow_create=False,
        )
    elif (
        _lstat_or_none(_marker_path(directory, BOOTSTRAP_PREPARATION_BINDING_NAME))
        is not None
    ):
        raise _invalid()
    _require_generation_target(directory, frozen_identity, canonical_program_path)
    binding_info = _lstat_or_none(_marker_path(directory, BOOTSTRAP_BINDING_NAME))
    if binding_info is None:
        raise _invalid()
    return _existing_binding(
        directory,
        identity=frozen_identity,
        claim_id=nonce,
        program_root=canonical_program,
        data_root=canonical_data,
        expected_info=binding_info,
    )
