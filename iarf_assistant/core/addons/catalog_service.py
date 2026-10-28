"""Trusted catalog retrieval and secret-free package/update projections."""

from __future__ import annotations

import hashlib
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from iarf_assistant.core.release_downloads import (
    DownloadedReleaseFile,
    ReleaseDownloadError,
    download_release_artifact,
    is_local_release_source,
    read_release_index_source,
    resolve_artifact_source,
)

from .catalog import (
    ADDON_CATALOG_MAX_BYTES,
    AddonCatalogError,
    AddonCatalogIndex,
    AddonCatalogRelease,
    compare_addon_contracts,
    load_addon_catalog_trust_store,
    parse_addon_catalog,
    verify_addon_catalog,
)
from .installer import AddonPackageInstaller, StagedAddonPackage


class AddonCatalogSourceError(RuntimeError):
    """The configured catalog source could not be retrieved or authenticated."""


@dataclass(frozen=True, slots=True)
class LoadedAddonCatalog:
    index: AddonCatalogIndex
    signer_key_id: str | None
    source: str
    source_sha256: str

    def provenance_for(
        self,
        *,
        addon_id: str,
        release: AddonCatalogRelease,
        artifact_source: str,
    ) -> dict[str, Any]:
        return {
            "type": "signed_addon_catalog",
            "catalog_source": _public_source(self.source),
            "catalog_sha256": self.source_sha256,
            "catalog_channel": self.index.channel,
            "catalog_generated_at": self.index.generated_at,
            "catalog_publisher": self.index.publisher,
            "catalog_signer_key_id": self.signer_key_id,
            "package_id": addon_id,
            "package_version": release.version,
            "artifact_source": _public_source(artifact_source),
        }


def _public_source(source: str) -> str:
    """Strip URL credentials, query tokens, and fragments from durable audit data."""

    parsed = urlsplit(str(source))
    if parsed.scheme.casefold() not in {"http", "https"}:
        return str(source)
    hostname = parsed.hostname or ""
    if parsed.port is not None:
        hostname = f"{hostname}:{parsed.port}"
    return urlunsplit((parsed.scheme, hostname, parsed.path, "", ""))


class AddonCatalogService:
    def __init__(
        self,
        *,
        source: str,
        trust_store: str | Path,
        channel: str = "stable",
        allow_unsigned_local: bool = False,
    ) -> None:
        self.source = str(source or "").strip()
        self.trust_store = Path(trust_store).expanduser().resolve()
        self.channel = str(channel or "stable").strip().casefold()
        self.allow_unsigned_local = bool(allow_unsigned_local)
        if not self.source:
            raise AddonCatalogSourceError("No add-on catalog source is configured.")

    def load(self) -> LoadedAddonCatalog:
        try:
            raw = read_release_index_source(self.source)
        except ReleaseDownloadError as exc:
            raise AddonCatalogSourceError(str(exc)) from exc
        if len(raw) > ADDON_CATALOG_MAX_BYTES:
            raise AddonCatalogSourceError(
                "Add-on catalog exceeds the configured size limit."
            )
        try:
            index = parse_addon_catalog(raw)
            if index.channel != self.channel:
                raise AddonCatalogError(
                    f"Configured channel is {self.channel!r}, but the catalog is "
                    f"for {index.channel!r}."
                )
            allow_unsigned = self.allow_unsigned_local and is_local_release_source(
                self.source
            )
            trusted = ()
            if index.signatures or not allow_unsigned:
                trusted = load_addon_catalog_trust_store(self.trust_store)
            signer = verify_addon_catalog(
                index,
                trusted,
                allow_unsigned=allow_unsigned,
            )
        except AddonCatalogError as exc:
            raise AddonCatalogSourceError(str(exc)) from exc
        return LoadedAddonCatalog(
            index=index,
            signer_key_id=signer,
            source=self.source,
            source_sha256=hashlib.sha256(raw).hexdigest(),
        )

    def artifact_source(self, release: AddonCatalogRelease) -> str:
        try:
            return resolve_artifact_source(
                release.archive.url,
                index_source=self.source,
            )
        except ReleaseDownloadError as exc:
            raise AddonCatalogSourceError(str(exc)) from exc

    def download_release(
        self,
        release: AddonCatalogRelease,
        destination: str | Path,
        *,
        progress: Callable[[int, int | None], None] | None = None,
        cancellation_check: Callable[[], None] | None = None,
    ) -> DownloadedReleaseFile:
        source = self.artifact_source(release)
        try:
            return download_release_artifact(
                source,
                destination,
                expected_size=release.archive.size,
                expected_sha256=release.archive.sha256,
                progress=progress,
                cancellation_check=cancellation_check,
            )
        except ReleaseDownloadError as exc:
            raise AddonCatalogSourceError(str(exc)) from exc

    def stage_release(
        self,
        loaded: LoadedAddonCatalog,
        *,
        addon_id: str,
        release: AddonCatalogRelease,
        installer: AddonPackageInstaller,
        operator_approved: bool,
        progress: Callable[[int, int | None], None] | None = None,
        cancellation_check: Callable[[], None] | None = None,
    ) -> StagedAddonPackage:
        """Fetch signed bytes and bind the staged package to reviewed metadata."""

        download_root = (
            Path(installer.paths.staging_dir)
            / "addon-catalog-downloads"
            / uuid.uuid4().hex
        ).resolve()
        allowed_root = (
            Path(installer.paths.staging_dir) / "addon-catalog-downloads"
        ).resolve()
        download_root.relative_to(allowed_root)
        download_root.mkdir(parents=True, exist_ok=False)
        destination = download_root / release.archive.filename
        artifact_source = self.artifact_source(release)
        staged = None
        try:
            self.download_release(
                release,
                destination,
                progress=progress,
                cancellation_check=cancellation_check,
            )
            with destination.open("rb") as stream:
                staged = installer.stage(
                    stream,
                    original_filename=release.archive.filename,
                    operator_approved=operator_approved,
                    source_provenance=loaded.provenance_for(
                        addon_id=addon_id,
                        release=release,
                        artifact_source=artifact_source,
                    ),
                    expected_archive_sha256=release.archive.sha256,
                    expected_archive_size=release.archive.size,
                    expected_manifest_sha256=str(release.contract["manifest_sha256"]),
                    expected_contract_sha256=str(release.contract["contract_sha256"]),
                    cancellation_check=cancellation_check,
                )
            if staged.contract.addon_id != addon_id:
                raise AddonCatalogSourceError(
                    "The downloaded package ID differs from the catalog entry."
                )
            if staged.contract.version != release.version:
                raise AddonCatalogSourceError(
                    "The downloaded package version differs from the catalog entry."
                )
            return staged
        except Exception:
            if staged is not None:
                installer.cleanup_stage(staged)
            raise
        finally:
            shutil.rmtree(download_root, ignore_errors=True)


def catalog_listing(
    loaded: LoadedAddonCatalog,
    *,
    installed: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Build an in-app catalog view without exposing raw signatures."""

    packages: list[dict[str, Any]] = []
    stable = loaded.index.channel == "stable"
    for package in loaded.index.packages:
        current = installed.get(package.addon_id)
        current_version = str((current or {}).get("version") or "") or None
        latest = package.select_release(stable=stable)
        update = package.select_release(
            current_version=current_version,
            stable=stable,
        )
        target = update or latest
        current_contract = dict((current or {}).get("contract") or {})
        diff = (
            compare_addon_contracts(current_contract, target.contract).to_dict()
            if target is not None
            else None
        )
        packages.append(
            {
                "id": package.addon_id,
                "name": package.name,
                "summary": package.summary,
                "kind": package.kind,
                "distribution": dict(package.distribution),
                "license": package.license,
                "icon_url": package.icon_url,
                "installed_version": current_version,
                "latest_version": latest.version if latest else None,
                "update_version": update.version if update else None,
                "status": (
                    "update_available"
                    if update is not None and current_version is not None
                    else "installed"
                    if current_version is not None
                    else "available"
                    if latest is not None
                    else "incompatible"
                ),
                "target": (
                    {
                        **target.to_dict(),
                        "archive": {
                            "filename": target.archive.filename,
                            "sha256": target.archive.sha256,
                            "size": target.archive.size,
                        },
                    }
                    if target is not None
                    else None
                ),
                "update_diff": diff,
            }
        )
    return {
        "configured": True,
        "channel": loaded.index.channel,
        "publisher": loaded.index.publisher,
        "generated_at": loaded.index.generated_at,
        "source_sha256": loaded.source_sha256,
        "signer_key_id": loaded.signer_key_id,
        "packages": packages,
    }


__all__ = [
    "AddonCatalogService",
    "AddonCatalogSourceError",
    "LoadedAddonCatalog",
    "catalog_listing",
]
