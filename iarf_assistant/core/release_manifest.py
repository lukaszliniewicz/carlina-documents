"""Signed, versioned release metadata for managed Carlina installations."""

from __future__ import annotations

import base64
import hashlib
import json
import platform as platform_module
import sys
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from jsonschema import Draft202012Validator, FormatChecker
from packaging.version import InvalidVersion, Version

RELEASE_INDEX_FORMAT = "carlina.release-index"
# Version 1 remains the default publication format during the bridge window.
# New clients accept impact-bearing v2/v3 indexes explicitly; old clients
# continue to reject them through the closed v1 schema rather than
# misinterpreting signed execution claims.
RELEASE_INDEX_VERSION = 1
LATEST_RELEASE_INDEX_VERSION = 3
SUPPORTED_RELEASE_INDEX_VERSIONS = frozenset({1, 2, 3})
RELEASE_TRUST_FORMAT = "carlina.release-trust"
RELEASE_TRUST_VERSION = 1
SUPPORTED_CHANNELS = frozenset({"stable", "preview"})
PUBLISHER_BOOTSTRAP_EXECUTABLE_KIND = "publisher-bootstrap-executable"
SUPPORTED_ARTIFACT_KINDS = frozenset(
    {
        "pixi-pack-bundle",
        "pixi-pack-executable",
        PUBLISHER_BOOTSTRAP_EXECUTABLE_KIND,
    }
)
MAX_RELEASE_INDEX_BYTES = 2 * 1024 * 1024


class ReleaseManifestError(ValueError):
    """Release metadata is malformed, incompatible, or untrusted."""


class ReleaseSignatureError(ReleaseManifestError):
    """No trusted signature authenticates the release index."""


@dataclass(frozen=True, slots=True)
class ReleaseArtifact:
    platform: str
    kind: str
    filename: str
    url: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class ReleaseDatabaseContract:
    known_revisions: tuple[str, ...]
    heads: tuple[str, ...]
    rollback: str

    @property
    def requires_restore_for_rollback(self) -> bool:
        return self.rollback == "restore_required"


@dataclass(frozen=True, slots=True)
class ReleaseExecutionContract:
    """Signed execution and recovery claims for a schema-v2/v3 release."""

    data_impact: str
    migration_mode: str
    bootstrap_mode: str
    rollback_mode: str
    automatic_recovery: str
    probe_protocol: str
    probe_schema_version: int
    probe_isolation: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "data_impact": self.data_impact,
            "migration": {"mode": self.migration_mode},
            "bootstrap": {"mode": self.bootstrap_mode},
            "rollback": {"mode": self.rollback_mode},
            "recovery": {"automatic_coverage": self.automatic_recovery},
            "probe": {
                "protocol": self.probe_protocol,
                "schema_version": self.probe_schema_version,
                "isolation": self.probe_isolation,
            },
        }


@dataclass(frozen=True, slots=True)
class ManagedRelease:
    version: str
    published_at: str
    addon_api_version: str
    database: ReleaseDatabaseContract
    artifacts: tuple[ReleaseArtifact, ...]
    notes_url: str | None = None
    execution: ReleaseExecutionContract | None = None

    def artifact_for(self, platform_tag: str) -> ReleaseArtifact:
        matches = [item for item in self.artifacts if item.platform == platform_tag]
        if len(matches) != 1:
            raise ReleaseManifestError(
                f"Release {self.version} must contain exactly one artifact for "
                f"{platform_tag}; found {len(matches)}."
            )
        return matches[0]


@dataclass(frozen=True, slots=True)
class ReleaseSignature:
    key_id: str
    algorithm: str
    value: str


@dataclass(frozen=True, slots=True)
class ReleaseIndex:
    format: str
    schema_version: int
    channel: str
    generated_at: str
    releases: tuple[ManagedRelease, ...]
    signatures: tuple[ReleaseSignature, ...]
    application_profile: str | None = None

    @property
    def effective_application_profile(self) -> str:
        """Return the declared application profile or the legacy Workbench default."""

        return (
            self.application_profile
            if self.application_profile is not None
            else "workbench"
        )

    def select(
        self,
        *,
        current_version: str | None = None,
        requested_version: str | None = None,
    ) -> ManagedRelease | None:
        """Select an exact release or the newest upgrade in this channel."""

        if requested_version:
            for release in self.releases:
                if release.version == requested_version:
                    return release
            raise ReleaseManifestError(
                f"Release {requested_version!r} is not present in the {self.channel} index."
            )
        current = Version(current_version) if current_version else None
        candidates = [
            (Version(release.version), release)
            for release in self.releases
            if current is None or Version(release.version) > current
        ]
        if self.channel == "stable":
            candidates = [item for item in candidates if not item[0].is_prerelease]
        return max(candidates, key=lambda item: item[0])[1] if candidates else None

    def unsigned_dict(self) -> dict[str, Any]:
        payload = self.to_dict()
        payload.pop("signatures", None)
        return payload

    def to_dict(self) -> dict[str, Any]:
        # Keep the v1 shape byte-compatible: adding the optional dataclass field
        # must not serialize profile or ``execution: null`` members into old payloads.
        payload = asdict(self)
        if self.application_profile is None:
            payload.pop("application_profile", None)
        for release, serialized in zip(self.releases, payload["releases"], strict=True):
            if self.schema_version == RELEASE_INDEX_VERSION:
                serialized.pop("execution", None)
            else:
                if release.execution is None:
                    raise ReleaseManifestError(
                        f"Release-index v{self.schema_version} requires an execution contract."
                    )
                serialized["execution"] = release.execution.to_dict()
        # Round-trip through JSON so tuple-backed immutable fields become the
        # arrays callers expect from this public JSON contract.
        return json.loads(json.dumps(payload))


@dataclass(frozen=True, slots=True)
class TrustedReleaseKey:
    key_id: str
    algorithm: str
    public_key: str
    channels: tuple[str, ...]

    def decoded_public_key(self) -> bytes:
        try:
            raw = base64.b64decode(self.public_key, validate=True)
        except (ValueError, TypeError) as exc:
            raise ReleaseManifestError(
                f"Trusted key {self.key_id} is not valid base64."
            ) from exc
        if len(raw) != 32:
            raise ReleaseManifestError(
                f"Trusted key {self.key_id} must contain a 32-byte Ed25519 key."
            )
        if release_key_id(raw) != self.key_id:
            raise ReleaseManifestError(
                f"Trusted key ID does not match its public-key fingerprint: {self.key_id}."
            )
        return raw


def _schema(schema_version: int) -> dict[str, Any]:
    filename = {
        1: "release_manifest_schema.json",
        2: "release_manifest_schema_v2.json",
        3: "release_manifest_schema_v3.json",
    }.get(schema_version)
    if filename is None:
        raise ReleaseManifestError(
            f"Unsupported release-index schema version: {schema_version!r}."
        )
    path = Path(__file__).with_name(filename)
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_release_payload(value: ReleaseIndex | Mapping[str, Any]) -> bytes:
    payload = value.unsigned_dict() if isinstance(value, ReleaseIndex) else dict(value)
    payload.pop("signatures", None)
    return _canonical_json(payload)


def canonical_release_contract(
    index: ReleaseIndex,
    release: ManagedRelease,
) -> bytes:
    """Return the deterministic selected-release contract bound by a receipt."""

    matches = [item for item in index.releases if item.version == release.version]
    if len(matches) != 1 or matches[0] != release:
        raise ReleaseManifestError(
            "Selected release is not an exact member of its release index."
        )
    serialized = index.to_dict()
    selected = [
        item
        for item in serialized["releases"]
        if item.get("version") == release.version
    ]
    if len(selected) != 1:
        raise ReleaseManifestError(
            "Selected release cannot be serialized unambiguously."
        )
    contract: dict[str, Any] = {
        "format": index.format,
        "schema_version": index.schema_version,
        "channel": index.channel,
        "release": selected[0],
    }
    if index.application_profile is not None:
        contract["application_profile"] = index.application_profile
    return _canonical_json(contract)


def release_contract_sha256(index: ReleaseIndex, release: ManagedRelease) -> str:
    return hashlib.sha256(canonical_release_contract(index, release)).hexdigest()


def release_key_id(public_key: bytes) -> str:
    return f"ed25519:{hashlib.sha256(public_key).hexdigest()[:16]}"


def _parse_datetime(value: str, field: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReleaseManifestError(f"{field} must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise ReleaseManifestError(f"{field} must include a timezone.")


def parse_release_index(payload: Mapping[str, Any] | bytes | str) -> ReleaseIndex:
    """Validate and compile untrusted JSON without verifying its signature."""

    if isinstance(payload, bytes):
        if len(payload) > MAX_RELEASE_INDEX_BYTES:
            raise ReleaseManifestError("Release index exceeds the size limit.")
        try:
            raw: Any = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReleaseManifestError(
                "Release index is not valid UTF-8 JSON."
            ) from exc
    elif isinstance(payload, str):
        return parse_release_index(payload.encode("utf-8"))
    else:
        raw = dict(payload)
    schema_version = raw.get("schema_version") if isinstance(raw, dict) else None
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version not in SUPPORTED_RELEASE_INDEX_VERSIONS
    ):
        raise ReleaseManifestError(
            f"Unsupported release-index schema version: {schema_version!r}."
        )
    errors = sorted(
        Draft202012Validator(
            _schema(schema_version),
            format_checker=FormatChecker(),
        ).iter_errors(raw),
        key=lambda item: tuple(str(part) for part in item.absolute_path),
    )
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.absolute_path) or "$"
        raise ReleaseManifestError(
            f"Invalid release index at {location}: {first.message}"
        )

    _parse_datetime(str(raw["generated_at"]), "generated_at")
    releases: list[ManagedRelease] = []
    seen_versions: set[Version] = set()
    for release_payload in raw["releases"]:
        try:
            normalized_version = Version(str(release_payload["version"]))
        except InvalidVersion as exc:
            raise ReleaseManifestError(
                f"Invalid release version: {release_payload['version']!r}."
            ) from exc
        if normalized_version in seen_versions:
            raise ReleaseManifestError(
                f"Duplicate release version: {release_payload['version']}."
            )
        seen_versions.add(normalized_version)
        _parse_datetime(
            str(release_payload["published_at"]),
            f"releases.{release_payload['version']}.published_at",
        )
        try:
            Version(str(release_payload["addon_api_version"]))
        except InvalidVersion as exc:
            raise ReleaseManifestError(
                f"Release {release_payload['version']} has an invalid Add-on API version."
            ) from exc
        artifact_platforms: set[str] = set()
        artifacts: list[ReleaseArtifact] = []
        for artifact_payload in release_payload["artifacts"]:
            artifact = ReleaseArtifact(**artifact_payload)
            if artifact.kind == PUBLISHER_BOOTSTRAP_EXECUTABLE_KIND:
                if (
                    schema_version != 3
                    or raw.get("application_profile") != "documents"
                    or artifact.platform not in {"linux-64", "win-64"}
                ):
                    raise ReleaseManifestError(
                        "Publisher bootstrap artifacts require schema v3, the "
                        "Documents profile and a supported native platform."
                    )
                expected_suffix = ".sh" if artifact.platform == "linux-64" else ".ps1"
            else:
                expected_kind = (
                    "pixi-pack-bundle"
                    if artifact.platform.startswith("win-")
                    else "pixi-pack-executable"
                )
                expected_suffix = (
                    ".zip" if artifact.platform.startswith("win-") else ".sh"
                )
                if artifact.kind != expected_kind:
                    raise ReleaseManifestError(
                        f"Release {release_payload['version']} artifact for "
                        f"{artifact.platform} must use kind {expected_kind!r}."
                    )
            if not artifact.filename.casefold().endswith(expected_suffix):
                raise ReleaseManifestError(
                    f"Release {release_payload['version']} artifact for "
                    f"{artifact.platform} must use a {expected_suffix} filename."
                )
            if artifact.platform in artifact_platforms:
                raise ReleaseManifestError(
                    f"Release {release_payload['version']} has duplicate "
                    f"{artifact.platform} artifacts."
                )
            artifact_platforms.add(artifact.platform)
            artifacts.append(artifact)
        database_payload = release_payload["database"]
        known_revisions = tuple(sorted(database_payload["known_revisions"]))
        heads = tuple(sorted(database_payload["heads"]))
        if not set(heads).issubset(known_revisions):
            raise ReleaseManifestError(
                f"Release {release_payload['version']} database heads are not "
                "contained in known_revisions."
            )
        execution: ReleaseExecutionContract | None = None
        if schema_version >= 2:
            execution_payload = release_payload["execution"]
            execution = ReleaseExecutionContract(
                data_impact=str(execution_payload["data_impact"]),
                migration_mode=str(execution_payload["migration"]["mode"]),
                bootstrap_mode=str(execution_payload["bootstrap"]["mode"]),
                rollback_mode=str(execution_payload["rollback"]["mode"]),
                automatic_recovery=str(
                    execution_payload["recovery"]["automatic_coverage"]
                ),
                probe_protocol=str(execution_payload["probe"]["protocol"]),
                probe_schema_version=int(execution_payload["probe"]["schema_version"]),
                probe_isolation=str(execution_payload["probe"]["isolation"]),
            )
            _validate_execution_contract(
                execution,
                database_rollback=str(database_payload["rollback"]),
                release_version=str(normalized_version),
            )
        releases.append(
            ManagedRelease(
                version=str(normalized_version),
                published_at=str(release_payload["published_at"]),
                addon_api_version=str(release_payload["addon_api_version"]),
                database=ReleaseDatabaseContract(
                    known_revisions=known_revisions,
                    heads=heads,
                    rollback=str(database_payload["rollback"]),
                ),
                artifacts=tuple(artifacts),
                notes_url=release_payload.get("notes_url"),
                execution=execution,
            )
        )
    return ReleaseIndex(
        format=str(raw["format"]),
        schema_version=schema_version,
        channel=str(raw["channel"]),
        generated_at=str(raw["generated_at"]),
        releases=tuple(releases),
        signatures=tuple(ReleaseSignature(**item) for item in raw["signatures"]),
        application_profile=raw.get("application_profile"),
    )


def _validate_execution_contract(
    contract: ReleaseExecutionContract,
    *,
    database_rollback: str,
    release_version: str,
) -> None:
    """Reject signed combinations that overstate their recovery boundary."""

    expected_database_rollback = {
        "pointer_only": "compatible",
        "restore_required": "restore_required",
    }[contract.rollback_mode]
    if database_rollback != expected_database_rollback:
        raise ReleaseManifestError(
            f"Release {release_version} execution rollback mode conflicts with "
            "its database rollback contract."
        )

    expected_by_impact = {
        "none": {
            "migration_mode": "none",
            "bootstrap_mode": "none",
            "rollback_mode": "pointer_only",
            "automatic_recovery": "none",
            "probe_isolation": "host_filesystem_and_network_denied_v1",
        },
        "reversible_metadata": {
            "migration_mode": "none",
            "bootstrap_mode": "none",
            "rollback_mode": "pointer_only",
            "automatic_recovery": "none",
            "probe_isolation": "host_filesystem_and_network_denied_v1",
        },
        "migration": {
            "migration_mode": "constrained_data_only",
            "bootstrap_mode": "none",
            "rollback_mode": "restore_required",
            "automatic_recovery": "database_only",
            "probe_isolation": "host_filesystem_and_network_denied_v1",
        },
        "opaque": {
            "automatic_recovery": "full",
        },
    }
    expected = expected_by_impact[contract.data_impact]
    inconsistent = [
        field for field, value in expected.items() if getattr(contract, field) != value
    ]
    if inconsistent:
        raise ReleaseManifestError(
            f"Release {release_version} execution contract is inconsistent for "
            f"impact {contract.data_impact!r}: {', '.join(inconsistent)}."
        )
    if (
        contract.migration_mode == "python_alembic"
        or contract.bootstrap_mode == "repair_and_reconciliation"
    ) and contract.data_impact != "opaque":
        raise ReleaseManifestError(
            f"Release {release_version} arbitrary target execution must be opaque."
        )


def load_release_index(path: str | Path) -> ReleaseIndex:
    resolved = Path(path).expanduser().resolve()
    raw = resolved.read_bytes()
    return parse_release_index(raw)


def load_trusted_keys(path: str | Path) -> tuple[TrustedReleaseKey, ...]:
    resolved = Path(path).expanduser().resolve()
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseManifestError(
            f"Release trust store is unreadable: {resolved}"
        ) from exc
    if not isinstance(payload, dict):
        raise ReleaseManifestError("Release trust store must contain an object.")
    if (
        payload.get("format") != RELEASE_TRUST_FORMAT
        or payload.get("schema_version") != RELEASE_TRUST_VERSION
    ):
        raise ReleaseManifestError("Unsupported release trust-store format.")
    raw_keys = payload.get("keys")
    if not isinstance(raw_keys, list):
        raise ReleaseManifestError("Release trust store keys must be a list.")
    keys: list[TrustedReleaseKey] = []
    seen: set[str] = set()
    for item in raw_keys:
        if not isinstance(item, dict):
            raise ReleaseManifestError("Release trust-store entries must be objects.")
        try:
            key = TrustedReleaseKey(
                key_id=str(item["key_id"]),
                algorithm=str(item["algorithm"]),
                public_key=str(item["public_key"]),
                channels=tuple(str(value) for value in item["channels"]),
            )
        except (KeyError, TypeError) as exc:
            raise ReleaseManifestError("Malformed release trust-store entry.") from exc
        if key.algorithm != "ed25519" or not key.channels:
            raise ReleaseManifestError(
                f"Trusted key {key.key_id} has an unsupported algorithm or no channels."
            )
        if not set(key.channels).issubset(SUPPORTED_CHANNELS):
            raise ReleaseManifestError(
                f"Trusted key {key.key_id} has unknown channels."
            )
        key.decoded_public_key()
        if key.key_id in seen:
            raise ReleaseManifestError(f"Duplicate trusted key: {key.key_id}.")
        seen.add(key.key_id)
        keys.append(key)
    return tuple(keys)


def verify_release_index(
    index: ReleaseIndex,
    trusted_keys: Iterable[TrustedReleaseKey],
    *,
    allow_unsigned: bool = False,
) -> str | None:
    """Return the authenticating key ID, or reject an untrusted index."""

    if not index.signatures:
        if allow_unsigned:
            return None
        raise ReleaseSignatureError("Release index is unsigned.")
    trusted = {
        key.key_id: key
        for key in trusted_keys
        if index.channel in key.channels and key.algorithm == "ed25519"
    }
    signed = canonical_release_payload(index)
    attempted: list[str] = []
    for signature in index.signatures:
        key = trusted.get(signature.key_id)
        if key is None or signature.algorithm != "ed25519":
            continue
        attempted.append(signature.key_id)
        try:
            signature_bytes = base64.b64decode(signature.value, validate=True)
            Ed25519PublicKey.from_public_bytes(key.decoded_public_key()).verify(
                signature_bytes,
                signed,
            )
        except ValueError, InvalidSignature:
            continue
        return signature.key_id
    suffix = f" Tried: {', '.join(attempted)}." if attempted else ""
    raise ReleaseSignatureError(
        f"No trusted signature authenticates the {index.channel} release index.{suffix}"
    )


def load_private_signing_key(
    value: Ed25519PrivateKey | bytes | str | Path,
) -> Ed25519PrivateKey:
    """Load an Ed25519 key from PEM or URL-safe/base64-encoded raw bytes."""

    if isinstance(value, Ed25519PrivateKey):
        return value
    if isinstance(value, Path):
        raw = value.expanduser().resolve().read_bytes()
    elif isinstance(value, str):
        candidate = Path(value).expanduser()
        try:
            is_file = candidate.is_file()
        except OSError:
            is_file = False
        raw = candidate.read_bytes() if is_file else value.encode("ascii")
    else:
        raw = value
    if b"BEGIN" in raw:
        key = serialization.load_pem_private_key(raw, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ReleaseManifestError("Release signing key is not Ed25519.")
        return key
    try:
        decoded = base64.b64decode(raw, validate=True)
    except (ValueError, TypeError) as exc:
        raise ReleaseManifestError("Release signing key is not PEM or base64.") from exc
    if len(decoded) != 32:
        raise ReleaseManifestError("Raw Ed25519 private keys must be 32 bytes.")
    return Ed25519PrivateKey.from_private_bytes(decoded)


def sign_release_index(
    index: ReleaseIndex,
    private_key: Ed25519PrivateKey,
) -> ReleaseIndex:
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    signature = private_key.sign(canonical_release_payload(index))
    signed = ReleaseSignature(
        key_id=release_key_id(public_key),
        algorithm="ed25519",
        value=base64.b64encode(signature).decode("ascii"),
    )
    return ReleaseIndex(
        format=index.format,
        schema_version=index.schema_version,
        channel=index.channel,
        generated_at=index.generated_at,
        releases=index.releases,
        signatures=(signed,),
        application_profile=index.application_profile,
    )


def trusted_key_record(
    private_key: Ed25519PrivateKey,
    *,
    channels: Iterable[str] = ("stable", "preview"),
) -> dict[str, Any]:
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        "key_id": release_key_id(public_key),
        "algorithm": "ed25519",
        "public_key": base64.b64encode(public_key).decode("ascii"),
        "channels": list(channels),
    }


def current_platform_tag(
    *,
    system: str | None = None,
    machine: str | None = None,
) -> str:
    active_system = (system or sys.platform).casefold()
    active_machine = (machine or platform_module.machine()).casefold().replace("_", "-")
    architecture = {
        "amd64": "64",
        "x86-64": "64",
        "x86_64": "64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(active_machine)
    if architecture is None:
        raise ReleaseManifestError(f"Unsupported CPU architecture: {active_machine}.")
    if active_system.startswith("win"):
        family = "win"
    elif active_system.startswith("linux"):
        family = "linux"
        architecture = "aarch64" if architecture == "arm64" else architecture
    elif active_system in {"darwin", "mac", "macos"}:
        family = "osx"
    else:
        raise ReleaseManifestError(f"Unsupported operating system: {active_system}.")
    return f"{family}-{architecture}"


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "LATEST_RELEASE_INDEX_VERSION",
    "MAX_RELEASE_INDEX_BYTES",
    "RELEASE_INDEX_FORMAT",
    "RELEASE_INDEX_VERSION",
    "RELEASE_TRUST_FORMAT",
    "RELEASE_TRUST_VERSION",
    "SUPPORTED_ARTIFACT_KINDS",
    "SUPPORTED_RELEASE_INDEX_VERSIONS",
    "ManagedRelease",
    "ReleaseArtifact",
    "ReleaseDatabaseContract",
    "ReleaseExecutionContract",
    "ReleaseIndex",
    "ReleaseManifestError",
    "ReleaseSignature",
    "ReleaseSignatureError",
    "TrustedReleaseKey",
    "canonical_release_contract",
    "canonical_release_payload",
    "current_platform_tag",
    "load_private_signing_key",
    "load_release_index",
    "load_trusted_keys",
    "parse_release_index",
    "release_contract_sha256",
    "release_key_id",
    "sign_release_index",
    "trusted_key_record",
    "utc_now_iso",
    "verify_release_index",
]
