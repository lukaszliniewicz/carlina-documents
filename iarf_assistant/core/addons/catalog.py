"""Signed first-party catalog contracts for independently released add-ons.

The catalog is metadata only. Parsing and signature verification never import or
execute add-on code; the ordinary :class:`AddonPackageInstaller` remains the
archive-validation and activation boundary after a catalog artifact is fetched.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from jsonschema import Draft202012Validator, FormatChecker
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from .contracts import ADDON_API_VERSION, ADDON_ID_PATTERN

ADDON_CATALOG_FORMAT = "carlina.addon-catalog"
ADDON_CATALOG_SCHEMA_VERSION = 1
ADDON_CATALOG_TRUST_FORMAT = "carlina.addon-catalog-trust"
ADDON_CATALOG_TRUST_VERSION = 1
ADDON_CATALOG_MAX_BYTES = 2 * 1024 * 1024
ADDON_CATALOG_CHANNELS = frozenset({"stable", "preview"})


class AddonCatalogError(ValueError):
    """The catalog, trust store, or requested package is invalid."""


class AddonCatalogSignatureError(AddonCatalogError):
    """No trusted signature authenticates an add-on catalog."""


@dataclass(frozen=True, slots=True)
class AddonCatalogSignature:
    key_id: str
    algorithm: str
    value: str


@dataclass(frozen=True, slots=True)
class TrustedAddonCatalogKey:
    key_id: str
    algorithm: str
    public_key: str
    channels: tuple[str, ...]
    publishers: tuple[str, ...]

    def decoded_public_key(self) -> bytes:
        try:
            raw = base64.b64decode(self.public_key, validate=True)
        except (TypeError, ValueError) as exc:
            raise AddonCatalogError(
                f"Trusted catalog key {self.key_id} is not valid base64."
            ) from exc
        if len(raw) != 32:
            raise AddonCatalogError(
                f"Trusted catalog key {self.key_id} must contain a 32-byte Ed25519 key."
            )
        if addon_catalog_key_id(raw) != self.key_id:
            raise AddonCatalogError(
                "Trusted catalog key ID does not match its public-key fingerprint: "
                f"{self.key_id}."
            )
        return raw


@dataclass(frozen=True, slots=True)
class AddonCatalogArtifact:
    filename: str
    url: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class AddonCatalogRelease:
    version: str
    published_at: str
    addon_api_requires: str
    archive: AddonCatalogArtifact
    contract: Mapping[str, Any]
    notes_url: str | None = None

    def supports_addon_api(self, version: str = ADDON_API_VERSION) -> bool:
        return Version(version) in SpecifierSet(self.addon_api_requires)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "version": self.version,
            "published_at": self.published_at,
            "addon_api_requires": self.addon_api_requires,
            "archive": {
                "filename": self.archive.filename,
                "url": self.archive.url,
                "sha256": self.archive.sha256,
                "size": self.archive.size,
            },
            "contract": _deep_thaw(self.contract),
        }
        if self.notes_url:
            payload["notes_url"] = self.notes_url
        return payload


@dataclass(frozen=True, slots=True)
class AddonCatalogPackage:
    addon_id: str
    name: str
    summary: str
    kind: str
    distribution: Mapping[str, str]
    license: str
    releases: tuple[AddonCatalogRelease, ...]
    icon_url: str | None = None

    def select_release(
        self,
        *,
        current_version: str | None = None,
        requested_version: str | None = None,
        addon_api_version: str = ADDON_API_VERSION,
        stable: bool = True,
    ) -> AddonCatalogRelease | None:
        compatible = [
            release
            for release in self.releases
            if release.supports_addon_api(addon_api_version)
        ]
        if requested_version:
            for release in compatible:
                if release.version == requested_version:
                    return release
            raise AddonCatalogError(
                f"Compatible release {requested_version!r} of {self.addon_id!r} "
                "is not present in this catalog."
            )
        current = Version(current_version) if current_version else None
        candidates = [
            (Version(release.version), release)
            for release in compatible
            if current is None or Version(release.version) > current
        ]
        if stable:
            candidates = [item for item in candidates if not item[0].is_prerelease]
        return max(candidates, key=lambda item: item[0])[1] if candidates else None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "id": self.addon_id,
            "name": self.name,
            "summary": self.summary,
            "kind": self.kind,
            "distribution": dict(self.distribution),
            "license": self.license,
            "releases": [release.to_dict() for release in self.releases],
        }
        if self.icon_url:
            payload["icon_url"] = self.icon_url
        return payload


@dataclass(frozen=True, slots=True)
class AddonCatalogIndex:
    format: str
    schema_version: int
    channel: str
    publisher: str
    generated_at: str
    packages: tuple[AddonCatalogPackage, ...]
    signatures: tuple[AddonCatalogSignature, ...]

    def package(self, addon_id: str) -> AddonCatalogPackage:
        for package in self.packages:
            if package.addon_id == addon_id:
                return package
        raise AddonCatalogError(f"Add-on {addon_id!r} is not present in this catalog.")

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "schema_version": self.schema_version,
            "channel": self.channel,
            "publisher": self.publisher,
            "generated_at": self.generated_at,
            "packages": [package.to_dict() for package in self.packages],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.unsigned_dict(),
            "signatures": [
                {
                    "key_id": signature.key_id,
                    "algorithm": signature.algorithm,
                    "value": signature.value,
                }
                for signature in self.signatures
            ],
        }


@dataclass(frozen=True, slots=True)
class AddonContractDiff:
    permissions: Mapping[str, Any]
    resources: Mapping[str, Any]
    runtime_units: Mapping[str, Any]
    database_migration: Mapping[str, Any]
    dependency_notices: Mapping[str, Any]
    distribution: Mapping[str, Any]
    contract_changed: bool
    requires_review: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "permissions": _deep_thaw(self.permissions),
            "resources": _deep_thaw(self.resources),
            "runtime_units": _deep_thaw(self.runtime_units),
            "database_migration": _deep_thaw(self.database_migration),
            "dependency_notices": _deep_thaw(self.dependency_notices),
            "distribution": _deep_thaw(self.distribution),
            "contract_changed": self.contract_changed,
            "requires_review": self.requires_review,
        }


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, list | tuple):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _deep_thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _deep_thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_deep_thaw(item) for item in value]
    return value


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_addon_catalog_payload(
    value: AddonCatalogIndex | Mapping[str, Any],
) -> bytes:
    payload = (
        value.unsigned_dict() if isinstance(value, AddonCatalogIndex) else dict(value)
    )
    payload.pop("signatures", None)
    return _canonical_json(payload)


def addon_catalog_key_id(public_key: bytes) -> str:
    return f"ed25519:{hashlib.sha256(public_key).hexdigest()[:16]}"


def _schema() -> dict[str, Any]:
    return json.loads(
        Path(__file__).with_name("catalog_schema.json").read_text(encoding="utf-8")
    )


def _validate_datetime(value: str, field: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise AddonCatalogError(f"{field} must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise AddonCatalogError(f"{field} must include a timezone.")


def _validate_canonical_version(value: str, field: str) -> None:
    try:
        normalized = str(Version(value))
    except InvalidVersion as exc:
        raise AddonCatalogError(f"{field} is not a valid package version.") from exc
    if normalized != value:
        raise AddonCatalogError(
            f"{field} must use canonical PEP 440 spelling {normalized!r}."
        )


def _validate_specifier(value: str, field: str) -> None:
    try:
        SpecifierSet(value)
    except InvalidSpecifier as exc:
        raise AddonCatalogError(f"{field} is not a valid version range.") from exc


def parse_addon_catalog(
    payload: Mapping[str, Any] | bytes | str,
) -> AddonCatalogIndex:
    """Validate and compile untrusted catalog JSON without verifying signatures."""

    if isinstance(payload, bytes):
        if len(payload) > ADDON_CATALOG_MAX_BYTES:
            raise AddonCatalogError("Add-on catalog exceeds the 2 MiB size limit.")
        try:
            raw: Any = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AddonCatalogError("Add-on catalog is not valid UTF-8 JSON.") from exc
    elif isinstance(payload, str):
        return parse_addon_catalog(payload.encode("utf-8"))
    else:
        raw = dict(payload)

    errors = sorted(
        Draft202012Validator(
            _schema(),
            format_checker=FormatChecker(),
        ).iter_errors(raw),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.absolute_path) or "$"
        raise AddonCatalogError(
            f"Invalid add-on catalog at {location}: {first.message}"
        )

    _validate_datetime(str(raw["generated_at"]), "generated_at")
    publisher = str(raw["publisher"])
    packages: list[AddonCatalogPackage] = []
    seen_package_ids: set[str] = set()
    for package_payload in raw["packages"]:
        addon_id = str(package_payload["id"])
        if not ADDON_ID_PATTERN.fullmatch(addon_id):
            raise AddonCatalogError(f"Invalid catalog package ID: {addon_id!r}.")
        if addon_id in seen_package_ids:
            raise AddonCatalogError(f"Duplicate catalog package ID: {addon_id!r}.")
        seen_package_ids.add(addon_id)
        distribution = dict(package_payload["distribution"])
        if distribution["publisher"] != publisher:
            raise AddonCatalogError(
                f"Package {addon_id!r} publisher does not match the catalog publisher."
            )

        releases: list[AddonCatalogRelease] = []
        seen_versions: set[str] = set()
        for release_payload in package_payload["releases"]:
            version = str(release_payload["version"])
            _validate_canonical_version(
                version,
                f"packages.{addon_id}.releases.{version}.version",
            )
            if version in seen_versions:
                raise AddonCatalogError(
                    f"Package {addon_id!r} contains duplicate release {version!r}."
                )
            seen_versions.add(version)
            _validate_datetime(
                str(release_payload["published_at"]),
                f"packages.{addon_id}.releases.{version}.published_at",
            )
            api_requires = str(release_payload["addon_api_requires"])
            _validate_specifier(
                api_requires,
                f"packages.{addon_id}.releases.{version}.addon_api_requires",
            )
            contract = dict(release_payload["contract"])
            if contract["kind"] != package_payload["kind"]:
                raise AddonCatalogError(
                    f"Package {addon_id!r} release {version!r} changes its "
                    "declared kind."
                )
            if contract["distribution"] != distribution:
                raise AddonCatalogError(
                    f"Package {addon_id!r} release {version!r} has "
                    "inconsistent distribution metadata."
                )
            archive_payload = release_payload["archive"]
            releases.append(
                AddonCatalogRelease(
                    version=version,
                    published_at=str(release_payload["published_at"]),
                    addon_api_requires=api_requires,
                    archive=AddonCatalogArtifact(
                        filename=str(archive_payload["filename"]),
                        url=str(archive_payload["url"]),
                        sha256=str(archive_payload["sha256"]),
                        size=int(archive_payload["size"]),
                    ),
                    contract=_deep_freeze(contract),
                    notes_url=release_payload.get("notes_url"),
                )
            )
        packages.append(
            AddonCatalogPackage(
                addon_id=addon_id,
                name=str(package_payload["name"]),
                summary=str(package_payload["summary"]),
                kind=str(package_payload["kind"]),
                distribution=_deep_freeze(distribution),
                license=str(package_payload["license"]),
                releases=tuple(releases),
                icon_url=package_payload.get("icon_url"),
            )
        )
    return AddonCatalogIndex(
        format=str(raw["format"]),
        schema_version=int(raw["schema_version"]),
        channel=str(raw["channel"]),
        publisher=publisher,
        generated_at=str(raw["generated_at"]),
        packages=tuple(packages),
        signatures=tuple(
            AddonCatalogSignature(
                key_id=str(item["key_id"]),
                algorithm=str(item["algorithm"]),
                value=str(item["value"]),
            )
            for item in raw["signatures"]
        ),
    )


def load_addon_catalog_trust_store(
    path: str | Path,
) -> tuple[TrustedAddonCatalogKey, ...]:
    resolved = Path(path).expanduser().resolve()
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AddonCatalogError(
            f"Add-on catalog trust store is unreadable: {resolved}"
        ) from exc
    if not isinstance(payload, dict):
        raise AddonCatalogError("Add-on catalog trust store must contain an object.")
    if (
        payload.get("format") != ADDON_CATALOG_TRUST_FORMAT
        or payload.get("schema_version") != ADDON_CATALOG_TRUST_VERSION
    ):
        raise AddonCatalogError("Unsupported add-on catalog trust-store format.")
    raw_keys = payload.get("keys")
    if not isinstance(raw_keys, list):
        raise AddonCatalogError("Add-on catalog trust-store keys must be a list.")

    keys: list[TrustedAddonCatalogKey] = []
    seen: set[str] = set()
    for item in raw_keys:
        if not isinstance(item, dict):
            raise AddonCatalogError("Catalog trust-store entries must be objects.")
        try:
            key = TrustedAddonCatalogKey(
                key_id=str(item["key_id"]),
                algorithm=str(item["algorithm"]),
                public_key=str(item["public_key"]),
                channels=tuple(str(value) for value in item["channels"]),
                publishers=tuple(str(value) for value in item["publishers"]),
            )
        except (KeyError, TypeError) as exc:
            raise AddonCatalogError("Malformed catalog trust-store entry.") from exc
        if key.algorithm != "ed25519" or not key.channels or not key.publishers:
            raise AddonCatalogError(
                f"Trusted catalog key {key.key_id} has an unsupported or empty scope."
            )
        if not set(key.channels).issubset(ADDON_CATALOG_CHANNELS):
            raise AddonCatalogError(
                f"Trusted catalog key {key.key_id} has unknown channels."
            )
        key.decoded_public_key()
        if key.key_id in seen:
            raise AddonCatalogError(f"Duplicate trusted catalog key: {key.key_id}.")
        seen.add(key.key_id)
        keys.append(key)
    return tuple(keys)


def verify_addon_catalog(
    index: AddonCatalogIndex,
    trusted_keys: Iterable[TrustedAddonCatalogKey],
    *,
    allow_unsigned: bool = False,
) -> str | None:
    """Return the key that authenticated the catalog, or reject it."""

    if not index.signatures:
        if allow_unsigned:
            return None
        raise AddonCatalogSignatureError("Add-on catalog is unsigned.")
    trusted = {
        key.key_id: key
        for key in trusted_keys
        if index.channel in key.channels
        and index.publisher in key.publishers
        and key.algorithm == "ed25519"
    }
    signed = canonical_addon_catalog_payload(index)
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
    raise AddonCatalogSignatureError(
        f"No trusted signature authenticates the {index.publisher!r} "
        f"{index.channel} add-on catalog.{suffix}"
    )


def sign_addon_catalog(
    index: AddonCatalogIndex,
    private_key: Ed25519PrivateKey,
) -> AddonCatalogIndex:
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    signature = AddonCatalogSignature(
        key_id=addon_catalog_key_id(public_key),
        algorithm="ed25519",
        value=base64.b64encode(
            private_key.sign(canonical_addon_catalog_payload(index))
        ).decode("ascii"),
    )
    return AddonCatalogIndex(
        format=index.format,
        schema_version=index.schema_version,
        channel=index.channel,
        publisher=index.publisher,
        generated_at=index.generated_at,
        packages=index.packages,
        signatures=(signature,),
    )


def trusted_addon_catalog_key_record(
    private_key: Ed25519PrivateKey,
    *,
    channels: Iterable[str] = ("stable", "preview"),
    publishers: Iterable[str] = ("Carlina Project",),
) -> dict[str, Any]:
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        "key_id": addon_catalog_key_id(public_key),
        "algorithm": "ed25519",
        "public_key": base64.b64encode(public_key).decode("ascii"),
        "channels": list(channels),
        "publishers": list(publishers),
    }


def catalog_contract_snapshot(
    contract: Mapping[str, Any] | Any,
    *,
    dependency_notices: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Create the signed, reviewable subset of a resolved package contract."""

    payload = contract.to_dict() if hasattr(contract, "to_dict") else dict(contract)
    manifest = getattr(contract, "manifest", None)
    if not isinstance(manifest, Mapping):
        manifest = payload.get("manifest")
    if not isinstance(manifest, Mapping):
        manifest = payload
    installation = manifest.get("installation")
    installation = dict(installation) if isinstance(installation, Mapping) else {}
    distribution = dict(payload.get("distribution") or {})
    if distribution.get("channel") != "first_party":
        raise AddonCatalogError(
            "Public first-party catalog entries must declare distribution.channel "
            "as 'first_party'."
        )
    snapshot = {
        "manifest_sha256": payload.get("manifest_sha256"),
        "contract_sha256": payload.get("contract_sha256"),
        "kind": payload.get("kind"),
        "distribution": distribution,
        "permissions": list(payload.get("permissions") or []),
        "resources": list(payload.get("resources") or []),
        "runtime_units": list(payload.get("runtime_units") or []),
        "installation": installation,
        "database_migration": payload.get("database_migration"),
        "dependency_notices": [dict(item) for item in dependency_notices],
        "capability_providers": list(payload.get("capability_providers") or []),
    }
    return json.loads(json.dumps(snapshot))


def _keyed_diff(
    before: Sequence[Mapping[str, Any]],
    after: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    before_by_id = {str(item.get("id")): dict(item) for item in before}
    after_by_id = {str(item.get("id")): dict(item) for item in after}
    added = [
        after_by_id[key] for key in sorted(after_by_id.keys() - before_by_id.keys())
    ]
    removed = [
        before_by_id[key] for key in sorted(before_by_id.keys() - after_by_id.keys())
    ]
    changed = [
        {"before": before_by_id[key], "after": after_by_id[key]}
        for key in sorted(before_by_id.keys() & after_by_id.keys())
        if _canonical_json(before_by_id[key]) != _canonical_json(after_by_id[key])
    ]
    return {"added": added, "removed": removed, "changed": changed}


def _optional_record_diff(before: object, after: object) -> dict[str, Any]:
    before_items = (
        [{"id": "database", **dict(before)}] if isinstance(before, Mapping) else []
    )
    after_items = (
        [{"id": "database", **dict(after)}] if isinstance(after, Mapping) else []
    )
    return _keyed_diff(before_items, after_items)


def compare_addon_contracts(
    installed_contract: Mapping[str, Any] | None,
    target_contract: Mapping[str, Any],
) -> AddonContractDiff:
    """Produce deterministic permission/runtime/license review data for updates."""

    before = dict(installed_contract or {})
    target = dict(target_contract)
    permissions = _keyed_diff(
        list(before.get("permissions") or []),
        list(target.get("permissions") or []),
    )
    resources = _keyed_diff(
        list(before.get("resources") or []),
        list(target.get("resources") or []),
    )
    runtime_units = _keyed_diff(
        list(before.get("runtime_units") or []),
        list(target.get("runtime_units") or []),
    )
    database_migration = _optional_record_diff(
        before.get("database_migration"),
        target.get("database_migration"),
    )
    dependency_notices = _keyed_diff(
        list(before.get("dependency_notices") or []),
        list(target.get("dependency_notices") or []),
    )
    before_distribution = dict(before.get("distribution") or {})
    target_distribution = dict(target.get("distribution") or {})
    distribution = {
        "before": before_distribution,
        "after": target_distribution,
        "changed": before_distribution != target_distribution,
    }
    contract_changed = str(before.get("contract_sha256") or "") != str(
        target.get("contract_sha256") or ""
    )
    requires_review = any(
        section[change]
        for section in (
            permissions,
            resources,
            runtime_units,
            database_migration,
            dependency_notices,
        )
        for change in ("added", "changed")
    ) or bool(database_migration["removed"]) or bool(distribution["changed"])
    return AddonContractDiff(
        permissions=_deep_freeze(permissions),
        resources=_deep_freeze(resources),
        runtime_units=_deep_freeze(runtime_units),
        database_migration=_deep_freeze(database_migration),
        dependency_notices=_deep_freeze(dependency_notices),
        distribution=_deep_freeze(distribution),
        contract_changed=contract_changed,
        requires_review=requires_review,
    )


__all__ = [
    "ADDON_CATALOG_CHANNELS",
    "ADDON_CATALOG_FORMAT",
    "ADDON_CATALOG_MAX_BYTES",
    "ADDON_CATALOG_SCHEMA_VERSION",
    "ADDON_CATALOG_TRUST_FORMAT",
    "ADDON_CATALOG_TRUST_VERSION",
    "AddonCatalogArtifact",
    "AddonCatalogError",
    "AddonCatalogIndex",
    "AddonCatalogPackage",
    "AddonCatalogRelease",
    "AddonCatalogSignature",
    "AddonCatalogSignatureError",
    "AddonContractDiff",
    "TrustedAddonCatalogKey",
    "addon_catalog_key_id",
    "canonical_addon_catalog_payload",
    "catalog_contract_snapshot",
    "compare_addon_contracts",
    "load_addon_catalog_trust_store",
    "parse_addon_catalog",
    "sign_addon_catalog",
    "trusted_addon_catalog_key_record",
    "verify_addon_catalog",
]
