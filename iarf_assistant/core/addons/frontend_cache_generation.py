"""Cheap package-generation identities for frontend status caches."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path


def _path_identity(value: object) -> str:
    if value is None:
        return ""
    try:
        return str(Path(str(value)).resolve())
    except OSError, ValueError:
        return str(value)


def frontend_package_generation_identity(
    package: object | None,
) -> tuple[str, ...] | None:
    """Return immutable selected-package fields without reading source files."""

    if package is None:
        return None
    contract = getattr(package, "contract", None)
    provenance = getattr(contract, "provenance", None)
    if not isinstance(provenance, Mapping):
        provenance = {}
    source_kind = getattr(contract, "source_kind", None)
    return (
        _path_identity(getattr(package, "package_dir", None)),
        _path_identity(getattr(package, "manifest_path", None)),
        str(getattr(package, "module_name", "") or ""),
        str(getattr(source_kind, "value", source_kind) or ""),
        str(getattr(contract, "manifest_sha256", "") or ""),
        str(getattr(contract, "contract_sha256", "") or ""),
        str(provenance.get("archive_sha256") or ""),
    )


def changed_frontend_package_ids(
    previous: Mapping[str, object],
    current: Mapping[str, object],
) -> set[str]:
    """Return every added, removed, or replaced selected package identifier."""

    return {
        addon_id
        for addon_id in previous.keys() | current.keys()
        if frontend_package_generation_identity(previous.get(addon_id))
        != frontend_package_generation_identity(current.get(addon_id))
    }


__all__ = [
    "changed_frontend_package_ids",
    "frontend_package_generation_identity",
]
