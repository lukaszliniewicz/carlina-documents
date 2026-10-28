"""Filesystem discovery and dependency diagnostics for Add-on API v1."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from types import MappingProxyType
from typing import Any

from packaging.version import InvalidVersion, Version

from .contracts import (
    ADDON_ID_PATTERN,
    AddonDiagnostic,
    AddonSourceKind,
    AddonTrustTier,
    ResolvedAddonContract,
    validate_addon_contract,
)
from .imports import installed_module_name


@dataclass(frozen=True, slots=True)
class DiscoveredAddon:
    contract: ResolvedAddonContract
    package_dir: Path
    manifest_path: Path
    module_name: str

    @property
    def addon_id(self) -> str:
        return self.contract.addon_id

    @property
    def source_kind(self) -> AddonSourceKind:
        return self.contract.source_kind

    @property
    def trust_tier(self) -> AddonTrustTier:
        return self.contract.trust_tier


@dataclass(frozen=True, slots=True)
class DiscoveryCollision:
    selected_id: str
    selected_source_kind: AddonSourceKind
    rejected_id: str
    rejected_source_kind: AddonSourceKind
    diagnostic: AddonDiagnostic


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    packages: Mapping[str, DiscoveredAddon]
    diagnostics: tuple[AddonDiagnostic, ...]
    rejected_packages: Mapping[str, tuple[AddonDiagnostic, ...]] = field(
        default_factory=lambda: MappingProxyType({}),
        repr=False,
        compare=False,
    )
    collisions: tuple[DiscoveryCollision, ...] = field(
        default_factory=tuple,
        repr=False,
        compare=False,
    )
    builtin_root_available: bool = True
    installed_root_available: bool = True
    metrics: Mapping[str, float | int] = field(
        default_factory=lambda: MappingProxyType({}),
        repr=False,
        compare=False,
    )

    @property
    def errors(self) -> tuple[AddonDiagnostic, ...]:
        return tuple(item for item in self.diagnostics if item.severity == "error")


def _validate_allowed_addon_ids(
    allowed_addon_ids: frozenset[str] | None,
) -> frozenset[str] | None:
    if allowed_addon_ids is None:
        return None
    if type(allowed_addon_ids) is not frozenset:
        raise TypeError("Allowed add-on IDs must be a frozenset or None.")
    if any(
        type(addon_id) is not str or not ADDON_ID_PATTERN.fullmatch(addon_id)
        for addon_id in allowed_addon_ids
    ):
        raise ValueError("Allowed add-on IDs must be exact valid add-on IDs.")
    return allowed_addon_ids


class AddonDependencyError(ValueError):
    def __init__(
        self,
        *,
        cycles: Iterable[tuple[str, ...]] = (),
        missing: Iterable[tuple[str, str]] = (),
    ) -> None:
        self.cycles = tuple(cycles)
        self.missing = tuple(missing)
        parts = [f"Dependency cycle: {' -> '.join(cycle)}" for cycle in self.cycles]
        parts.extend(
            f"Add-on '{addon_id}' requires missing add-on '{dependency_id}'."
            for addon_id, dependency_id in self.missing
        )
        super().__init__("; ".join(parts) or "The add-on dependency graph is invalid.")


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{path.name} must contain a JSON object.")
    return value


def _installed_version_dir(addon_root: Path) -> Path | None:
    marker = addon_root / "active.json"
    if marker.is_file():
        marker_payload = _read_json(marker)
        if marker_payload.get("disabled") is True:
            return None
        selected = str(marker_payload.get("version") or "").strip()
        if not selected:
            raise ValueError("active.json does not declare a version.")
        unresolved = addon_root / selected
        candidate = unresolved.resolve()
        candidate.relative_to(addon_root.resolve())
        if unresolved.is_symlink():
            raise ValueError("Selected installed version may not be a symbolic link.")
        if not candidate.is_dir():
            raise ValueError(f"Selected installed version is missing: {selected}.")
        return candidate

    candidates: list[tuple[Version, Path]] = []
    for child in addon_root.iterdir():
        if not child.is_dir() or child.is_symlink():
            continue
        try:
            resolved = child.resolve()
            resolved.relative_to(addon_root.resolve())
            candidates.append((Version(child.name), resolved))
        except InvalidVersion, OSError, ValueError:
            continue
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _builtin_candidate_directories(
    builtin_root: Path,
    allowed_addon_ids: frozenset[str] | None = None,
) -> Iterable[tuple[AddonSourceKind, Path, str]]:
    if builtin_root.is_dir():
        for child in sorted(
            builtin_root.iterdir(), key=lambda item: (item.name.casefold(), item.name)
        ):
            if allowed_addon_ids is not None and child.name not in allowed_addon_ids:
                continue
            if child.is_dir() and (child / "manifest.json").is_file():
                resolved = child.resolve()
                try:
                    resolved.relative_to(builtin_root.resolve())
                except ValueError:
                    continue
                yield AddonSourceKind.BUILT_IN, resolved, child.name


def _provenance(package_dir: Path) -> dict[str, Any]:
    path = package_dir / "provenance.json"
    if not path.is_file():
        return {}
    return _read_json(path)


def _enumerate_builtin_root(
    root: Path,
    allowed_addon_ids: frozenset[str] | None = None,
) -> tuple[list[tuple[AddonSourceKind, Path, str]], list[AddonDiagnostic], bool]:
    if not root.is_dir():
        return (
            [],
            [
                AddonDiagnostic(
                    "discovery.builtin_root_unavailable",
                    "The built-in add-on root is missing or is not a directory.",
                    str(root),
                )
            ],
            False,
        )
    try:
        return list(_builtin_candidate_directories(root, allowed_addon_ids)), [], True
    except OSError as exc:
        return (
            [],
            [
                AddonDiagnostic(
                    "discovery.builtin_root_unavailable",
                    str(exc),
                    str(root),
                )
            ],
            False,
        )


def _enumerate_installed_root(
    root: Path,
    allowed_addon_ids: frozenset[str] | None = None,
) -> tuple[
    list[tuple[AddonSourceKind, Path, str]],
    list[AddonDiagnostic],
    dict[str, list[AddonDiagnostic]],
    bool,
]:
    if not root.is_dir():
        return [], [], {}, False
    try:
        addon_roots = sorted(
            root.iterdir(), key=lambda item: (item.name.casefold(), item.name)
        )
    except OSError as exc:
        return (
            [],
            [
                AddonDiagnostic(
                    "discovery.installed_root_unavailable",
                    str(exc),
                    str(root),
                )
            ],
            {},
            False,
        )

    candidates: list[tuple[AddonSourceKind, Path, str]] = []
    diagnostics: list[AddonDiagnostic] = []
    rejected: dict[str, list[AddonDiagnostic]] = {}
    for addon_root in addon_roots:
        if allowed_addon_ids is not None and addon_root.name not in allowed_addon_ids:
            continue
        if not addon_root.is_dir():
            continue
        try:
            resolved_addon_root = addon_root.resolve()
            resolved_addon_root.relative_to(root)
            if addon_root.is_symlink():
                raise ValueError(
                    "Installed add-on directories may not be symbolic links."
                )
            selected = _installed_version_dir(resolved_addon_root)
        except (OSError, TypeError, ValueError) as exc:
            diagnostic = AddonDiagnostic(
                "discovery.active_version_invalid",
                str(exc),
                str(addon_root / "active.json"),
            )
            diagnostics.append(diagnostic)
            rejected.setdefault(addon_root.name, []).append(diagnostic)
            continue
        if selected is not None:
            candidates.append((AddonSourceKind.INSTALLED, selected, addon_root.name))
    return candidates, diagnostics, rejected, True


def discover_addons(
    builtin_root: str | Path,
    installed_root: str | Path,
    *,
    allowed_addon_ids: frozenset[str] | None = None,
) -> DiscoveryResult:
    """Discover and compile packages from immutable and mutable code roots."""

    selection = _validate_allowed_addon_ids(allowed_addon_ids)
    discovery_started_at = perf_counter()
    manifest_read_ms = 0.0
    provenance_read_ms = 0.0
    schema_validation_ms = 0.0
    contract_resolution_ms = 0.0
    contract_validation_ms = 0.0
    packages: dict[str, DiscoveredAddon] = {}
    diagnostics: list[AddonDiagnostic] = []
    rejected_packages: dict[str, list[AddonDiagnostic]] = {}
    collisions: list[DiscoveryCollision] = []
    casefold_ids: dict[str, str] = {}
    resolved_builtin_root = Path(builtin_root).resolve()
    resolved_installed_root = Path(installed_root).resolve()
    enumeration_started_at = perf_counter()
    builtin_candidates, builtin_diagnostics, builtin_root_available = (
        _enumerate_builtin_root(resolved_builtin_root, selection)
    )
    (
        installed_candidates,
        installed_diagnostics,
        installed_rejected,
        installed_root_available,
    ) = _enumerate_installed_root(resolved_installed_root, selection)
    candidates = [*builtin_candidates, *installed_candidates]
    diagnostics.extend(builtin_diagnostics)
    diagnostics.extend(installed_diagnostics)
    rejected_packages.update(installed_rejected)
    candidate_enumeration_ms = (perf_counter() - enumeration_started_at) * 1000

    for source_kind, package_dir, folder_id in candidates:
        manifest_path = package_dir / "manifest.json"
        try:
            read_started_at = perf_counter()
            try:
                manifest = _read_json(manifest_path)
            finally:
                manifest_read_ms += (perf_counter() - read_started_at) * 1000
            provenance_started_at = perf_counter()
            try:
                provenance = _provenance(package_dir)
            finally:
                provenance_read_ms += (perf_counter() - provenance_started_at) * 1000
            validation_started_at = perf_counter()
            try:
                validation = validate_addon_contract(
                    manifest,
                    addon_dir=package_dir,
                    source_kind=source_kind,
                    strict=source_kind == AddonSourceKind.INSTALLED,
                    provenance=provenance,
                )
            finally:
                contract_validation_ms += (
                    perf_counter() - validation_started_at
                ) * 1000
            schema_validation_ms += float(
                validation.metrics.get("schema_validation_ms", 0.0)
            )
            contract_resolution_ms += float(
                validation.metrics.get("contract_resolution_ms", 0.0)
            )
        # Contract compilation is a no-import trust boundary. A malformed
        # third-party package must become a diagnostic, never abort discovery
        # of every other package because a validator raised unexpectedly.
        except Exception as exc:  # noqa: BLE001
            diagnostic = AddonDiagnostic(
                "discovery.read_failed",
                str(exc),
                str(manifest_path),
            )
            diagnostics.append(diagnostic)
            rejected_packages.setdefault(folder_id, []).append(diagnostic)
            continue
        diagnostics.extend(validation.diagnostics)
        contract = validation.contract
        if contract is None:
            rejected_packages.setdefault(folder_id, []).extend(validation.errors)
            continue
        if contract.addon_id != folder_id:
            diagnostic = AddonDiagnostic(
                "discovery.folder_mismatch",
                f"Manifest ID '{contract.addon_id}' does not match package folder '{folder_id}'.",
                str(package_dir),
            )
            diagnostics.append(diagnostic)
            rejected_packages.setdefault(folder_id, []).append(diagnostic)
            continue
        folded = contract.addon_id.casefold()
        existing_id = casefold_ids.get(folded)
        if existing_id is not None:
            diagnostic = AddonDiagnostic(
                "discovery.duplicate_id",
                f"Add-on ID '{contract.addon_id}' collides with '{existing_id}'. Built-in packages cannot be shadowed.",
                str(package_dir),
            )
            diagnostics.append(diagnostic)
            rejected_packages.setdefault(folder_id, []).append(diagnostic)
            selected = packages[existing_id]
            collisions.append(
                DiscoveryCollision(
                    selected_id=existing_id,
                    selected_source_kind=selected.source_kind,
                    rejected_id=folder_id,
                    rejected_source_kind=source_kind,
                    diagnostic=diagnostic,
                )
            )
            continue
        if source_kind == AddonSourceKind.BUILT_IN:
            if not contract.addon_id.isidentifier():
                diagnostic = AddonDiagnostic(
                    "discovery.module_id_invalid",
                    "Built-in add-on IDs must also be valid Python identifiers.",
                    str(package_dir),
                )
                diagnostics.append(diagnostic)
                rejected_packages.setdefault(folder_id, []).append(diagnostic)
                continue
            module_name = f"iarf_assistant.addons.{contract.addon_id}"
        else:
            module_name = installed_module_name(contract.addon_id)
        casefold_ids[folded] = contract.addon_id
        packages[contract.addon_id] = DiscoveredAddon(
            contract=contract,
            package_dir=package_dir,
            manifest_path=manifest_path,
            module_name=module_name,
        )

    dependency_graph_started_at = perf_counter()
    try:
        dependency_order(packages)
    except AddonDependencyError as exc:
        for cycle in exc.cycles:
            diagnostics.append(
                AddonDiagnostic(
                    "dependencies.cycle",
                    f"Dependency cycle: {' -> '.join(cycle)}",
                    cycle[0] if cycle else "",
                )
            )
        for addon_id, dependency_id in exc.missing:
            diagnostics.append(
                AddonDiagnostic(
                    "dependencies.missing",
                    f"Required add-on '{dependency_id}' is not installed.",
                    addon_id,
                )
            )
    dependency_graph_ms = (perf_counter() - dependency_graph_started_at) * 1000
    total_ms = (perf_counter() - discovery_started_at) * 1000
    metrics: Mapping[str, float | int] = MappingProxyType(
        {
            "candidate_count": len(candidates),
            "package_count": len(packages),
            "diagnostic_count": len(diagnostics),
            "candidate_enumeration_ms": round(candidate_enumeration_ms, 3),
            "manifest_read_ms": round(manifest_read_ms, 3),
            "provenance_read_ms": round(provenance_read_ms, 3),
            "schema_validation_ms": round(schema_validation_ms, 3),
            "contract_resolution_ms": round(contract_resolution_ms, 3),
            "contract_validation_ms": round(contract_validation_ms, 3),
            "dependency_graph_ms": round(dependency_graph_ms, 3),
            "total_ms": round(total_ms, 3),
        }
    )
    return DiscoveryResult(
        packages=MappingProxyType(packages),
        diagnostics=tuple(diagnostics),
        rejected_packages=MappingProxyType(
            {
                addon_id: tuple(package_diagnostics)
                for addon_id, package_diagnostics in rejected_packages.items()
                if addon_id not in packages
            }
        ),
        collisions=tuple(collisions),
        builtin_root_available=builtin_root_available,
        installed_root_available=installed_root_available,
        metrics=metrics,
    )


def dependency_order(packages: Mapping[str, DiscoveredAddon]) -> tuple[str, ...]:
    """Return a deterministic dependency-first order or exact cycle diagnostics."""

    states: dict[str, int] = {}
    stack: list[str] = []
    order: list[str] = []
    cycles: list[tuple[str, ...]] = []
    missing: list[tuple[str, str]] = []

    def visit(addon_id: str) -> None:
        state = states.get(addon_id, 0)
        if state == 2:
            return
        if state == 1:
            start = stack.index(addon_id)
            cycle = (*stack[start:], addon_id)
            if cycle not in cycles:
                cycles.append(cycle)
            return
        states[addon_id] = 1
        stack.append(addon_id)
        package = packages[addon_id]
        for dependency_id in sorted(package.contract.addon_dependencies):
            if dependency_id not in packages:
                edge = (addon_id, dependency_id)
                if edge not in missing:
                    missing.append(edge)
                continue
            visit(dependency_id)
        stack.pop()
        states[addon_id] = 2
        order.append(addon_id)

    for candidate_id in sorted(packages):
        visit(candidate_id)
    if cycles or missing:
        raise AddonDependencyError(cycles=cycles, missing=missing)
    return tuple(order)
