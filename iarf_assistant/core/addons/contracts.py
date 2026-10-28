"""Executable, no-import Carlina Add-on API v1 contract compiler."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from time import perf_counter
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from iarf_assistant.core.manifest_schema import (
    load_manifest_schema,
    validate_manifest_against_schema,
)

from .migration_contracts import AddonMigrationContract
from .schema_ownership import (
    addon_defines_database_models,
    addon_has_migrations,
    addon_migration_contract,
    addon_migration_contract_issues,
)

ADDON_API_VERSION = "1.0"
SUPPORTED_MANIFEST_SCHEMA_VERSIONS = frozenset({1, 2})
ADDON_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
ENTRY_POINT_MODULE_PATTERN = re.compile(
    r"^(?:\.|[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)$"
)
RESOURCE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]*$")
LOCK_HASH_PATTERN = re.compile(r"--hash=sha256:[0-9a-fA-F]{64}(?:\s|$)")
PINNED_REQUIREMENT_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_,.-]+\])?==[^\s;\\]+"
)
PYTHON_MINOR_PATTERN = re.compile(r"^[0-9]+\.[0-9]+$")
_MANIFEST_VALIDATOR = Draft202012Validator(load_manifest_schema())

KNOWN_PERMISSIONS = frozenset(
    {
        "containers.manage",
        "database.shared",
        "filesystem.addon_data",
        "filesystem.host",
        "filesystem.user_files",
        "model.invoke",
        "network.outbound",
        "process.spawn",
        "scheduler.manage",
        "secrets.read",
        "tools.execute",
        "ui.serve",
    }
)
EXECUTION_MODES = frozenset({"native", "isolated_service", "remote_integration"})
STORAGE_CLASSES = frozenset({"persistent", "user_managed", "rebuildable", "temporary"})
CAPABILITY_CARDINALITIES = frozenset({"one", "many", "optional"})
CAPABILITY_ACTIVATION_POLICIES = frozenset({"install", "activate", "on_first_use"})
CAPABILITY_SCOPES = frozenset({"installation", "user", "workspace", "workflow"})
RUNTIME_LIFECYCLES = frozenset({"on_demand", "always_on", "host_managed"})
RUNTIME_GPU_REQUIREMENTS = frozenset({"none", "optional", "required"})
DEPENDENCY_LOCK_FORMATS = frozenset({"pip_requirements", "pixi"})
RUNTIME_DEPENDENCY_ARTIFACT_FORMATS = frozenset({"conda_explicit", "pip_requirements"})
DEPENDENCY_SETUP_KINDS = frozenset({"browser"})
DEPENDENCY_BROWSER_PROVIDERS = frozenset({"patchright"})
DEPENDENCY_BROWSER_TARGETS = frozenset({"chromium"})
CONNECTOR_MODES = frozenset({"credentials", "oauth", "managed"})
CONNECTOR_FIELD_KINDS = frozenset({"text", "url", "password"})
CONNECTOR_FIELD_STORAGES = frozenset({"label", "config", "secret"})


class AddonSourceKind(StrEnum):
    BUILT_IN = "built_in"
    INSTALLED = "installed"


class AddonTrustTier(StrEnum):
    """Legacy package-level projection; do not use for new policy decisions."""

    BUILT_IN_TRUSTED = "built_in_trusted"
    INSTALLED_TRUSTED_CODE = "installed_trusted_code"
    ISOLATED_SERVICE = "isolated_service"
    REMOTE_INTEGRATION = "remote_integration"


class AddonKind(StrEnum):
    APPLICATION = "application"
    TOOLKIT = "toolkit"
    CONNECTOR = "connector"
    ENGINE = "engine"
    COMPONENT = "component"


class DistributionChannel(StrEnum):
    BUILT_IN = "built_in"
    FIRST_PARTY = "first_party"
    COMMUNITY = "community"
    PRIVATE = "private"
    LOCAL_DEVELOPMENT = "local_development"


class RuntimeMode(StrEnum):
    IN_PROCESS = "in_process"
    MANAGED_PROCESS = "managed_process"
    CONTAINER = "container"
    REMOTE = "remote"


class EffectiveCodeAccess(StrEnum):
    """Highest local code-access boundary declared by a package's runtime units."""

    HOST_PROCESS = "host_process"
    HOST_USER_PROCESS = "host_user_process"
    CONTAINER_SERVICE = "container_service"
    REMOTE_SERVICE = "remote_service"
    DECLARATIVE_ONLY = "declarative_only"
    UNCLASSIFIED = "unclassified"


def classify_effective_code_access(
    runtime_units,
    *,
    database_migration: object | None = None,
    legacy_trust_tier: str | AddonTrustTier | None = None,
    runtime_declared: bool = True,
) -> EffectiveCodeAccess:
    """Classify code placement without implying that a boundary is a sandbox.

    Runtime units are authoritative for schema-2 contracts, including an empty
    list for a purely declarative package. The old trust tier is consulted only
    for incomplete legacy snapshots.
    """

    if database_migration is not None:
        return EffectiveCodeAccess.HOST_PROCESS

    modes: set[str] = set()
    for unit in runtime_units or ():
        raw_mode = unit.mode if isinstance(unit, RuntimeUnit) else None
        if raw_mode is None and isinstance(unit, Mapping):
            raw_mode = unit.get("mode")
        if isinstance(raw_mode, RuntimeMode):
            modes.add(raw_mode.value)
        elif raw_mode is not None:
            modes.add(str(raw_mode))
    if RuntimeMode.IN_PROCESS.value in modes:
        return EffectiveCodeAccess.HOST_PROCESS
    if RuntimeMode.MANAGED_PROCESS.value in modes:
        return EffectiveCodeAccess.HOST_USER_PROCESS
    if RuntimeMode.CONTAINER.value in modes:
        return EffectiveCodeAccess.CONTAINER_SERVICE
    if RuntimeMode.REMOTE.value in modes:
        return EffectiveCodeAccess.REMOTE_SERVICE
    if runtime_declared:
        return EffectiveCodeAccess.DECLARATIVE_ONLY

    legacy = (
        legacy_trust_tier.value
        if isinstance(legacy_trust_tier, AddonTrustTier)
        else str(legacy_trust_tier or "")
    )
    if legacy in {
        AddonTrustTier.BUILT_IN_TRUSTED.value,
        AddonTrustTier.INSTALLED_TRUSTED_CODE.value,
    }:
        return EffectiveCodeAccess.HOST_PROCESS
    if legacy == AddonTrustTier.ISOLATED_SERVICE.value:
        return EffectiveCodeAccess.CONTAINER_SERVICE
    if legacy == AddonTrustTier.REMOTE_INTEGRATION.value:
        return EffectiveCodeAccess.REMOTE_SERVICE
    return EffectiveCodeAccess.UNCLASSIFIED


class UiMode(StrEnum):
    NONE = "none"
    SCHEMA = "schema"
    SVELTE_APPLICATION = "svelte_application"
    SVELTE_COMPONENT = "svelte_component"
    EXTERNAL = "external"


class DependencyEnvironment(StrEnum):
    """Named Python dependency placement, independent of runtime mode."""

    IN_PROCESS_VENDOR = "in_process_vendor"
    MANAGED_PROCESS_PIXI = "managed_process_pixi"


DEPENDENCY_ENVIRONMENT_ALIASES = MappingProxyType(
    {
        "tier1": DependencyEnvironment.IN_PROCESS_VENDOR,
        "native": DependencyEnvironment.IN_PROCESS_VENDOR,
        "in_process": DependencyEnvironment.IN_PROCESS_VENDOR,
        # Historical generic and Pixi spellings remain schema-1 migration
        # inputs. Micromamba spellings are intentionally rejected below so an
        # obsolete environment declaration can never be accepted silently.
        "tier2": DependencyEnvironment.MANAGED_PROCESS_PIXI,
        "managed_process": DependencyEnvironment.MANAGED_PROCESS_PIXI,
        "tier2_pixi": DependencyEnvironment.MANAGED_PROCESS_PIXI,
        "pixi": DependencyEnvironment.MANAGED_PROCESS_PIXI,
    }
)

RETIRED_DEPENDENCY_ENVIRONMENTS = frozenset(
    {"micromamba", "managed_process_micromamba"}
)


@dataclass(frozen=True, slots=True)
class AddonDiagnostic:
    code: str
    message: str
    path: str = ""
    severity: str = "error"

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PermissionDeclaration:
    id: str
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ResourceDeclaration:
    id: str
    access: tuple[str, ...] = ()
    description: str = ""


@dataclass(frozen=True, slots=True)
class DependencyLock:
    path: str
    sha256: str
    format: str
    require_hashes: bool


@dataclass(frozen=True, slots=True)
class DependencySetupAction:
    """A bounded dependency-provisioning action executed by the host.

    Add-ons cannot provide an arbitrary command. The host maps this typed
    declaration to a reviewed installer implementation.
    """

    id: str
    kind: str
    provider: str
    target: str
    runtime_unit: str


@dataclass(frozen=True, slots=True)
class PythonDependencyEnvironment:
    """One immutable Python environment bound to explicit runtime units.

    A package may expose both host code and isolated workers. Keeping the
    binding in the compiled contract prevents a worker lock from being
    mistaken for dependencies that should be imported into Flask (and vice
    versa).
    """

    id: str
    environment: DependencyEnvironment
    runtime_units: tuple[str, ...]
    python_version: str
    lock: DependencyLock | None = None
    setup: tuple[DependencySetupAction, ...] = ()
    manifest_path: str = ""
    manifest_sha256: str = ""
    pip_packages: tuple[str, ...] = ()
    conda_packages: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DistributionDeclaration:
    channel: DistributionChannel
    publisher: str = ""
    source_uri: str = ""


@dataclass(frozen=True, slots=True)
class RuntimeDependencyArtifact:
    """Immutable dependency input owned by an isolated runtime unit."""

    path: str
    sha256: str
    format: str


@dataclass(frozen=True, slots=True)
class RuntimeUnit:
    id: str
    mode: RuntimeMode
    entry_point_module: str = ""
    entry_point_class: str = ""
    lifecycle: str = "host_managed"
    dependency_artifacts: tuple[RuntimeDependencyArtifact, ...] = ()
    requirements: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({}),
        repr=False,
    )
    config: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({}),
        repr=False,
    )


@dataclass(frozen=True, slots=True)
class CapabilityRequirement:
    id: str
    capability_id: str
    version_spec: str
    operations: tuple[str, ...] = ()
    cardinality: str = "one"
    activation: str = "activate"
    scope: str = "installation"
    qualifiers: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({}),
        repr=False,
    )


@dataclass(frozen=True, slots=True)
class CapabilityProvider:
    id: str
    capability_id: str
    version: str
    operations: tuple[str, ...] = ()
    runtime_unit: str = ""
    connector_definition: str = ""
    setup: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({}),
        repr=False,
    )
    qualifiers: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({}),
        repr=False,
    )


@dataclass(frozen=True, slots=True)
class ConnectorField:
    id: str
    name: str
    kind: str
    storage: str
    required: bool = True
    placeholder: str = ""
    description: str = ""


@dataclass(frozen=True, slots=True)
class ConnectorDefinition:
    id: str
    mode: str
    label: str
    fields: tuple[ConnectorField, ...] = ()
    setup_url: str = ""
    callback_path: str = ""
    disconnect_url: str = ""
    test_method: str = ""


@dataclass(frozen=True, slots=True)
class UiEntry:
    id: str
    mode: UiMode
    module: str = ""
    route: str = ""
    css_assets: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DatabaseMigrationDeclaration:
    """Reviewable projection of one platform-validated migration history."""

    format: str
    version_table: str
    purge_tables: tuple[str, ...]
    owned_tables: tuple[str, ...]
    base_revision: str
    head_revision: str
    revision_ids: tuple[str, ...]
    revision_count: int
    graph_sha256: str


@dataclass(frozen=True, slots=True)
class ResolvedAddonContract:
    """Immutable activation snapshot produced without importing add-on code."""

    addon_id: str
    name: str
    version: str
    manifest_schema_version: int
    api_requires: str
    execution_mode: str
    entry_point_module: str
    entry_point_class: str
    roles: tuple[str, ...]
    categories: tuple[str, ...]
    kind: AddonKind
    distribution: DistributionDeclaration
    runtime_units: tuple[RuntimeUnit, ...]
    capability_requirements: tuple[CapabilityRequirement, ...]
    capability_providers: tuple[CapabilityProvider, ...]
    connector_definitions: tuple[ConnectorDefinition, ...]
    ui_entries: tuple[UiEntry, ...]
    permissions: tuple[PermissionDeclaration, ...]
    resources: tuple[ResourceDeclaration, ...]
    addon_dependencies: tuple[str, ...]
    python_environments: tuple[PythonDependencyEnvironment, ...]
    # Compatibility projection for consumers of the original one-environment
    # contract. These are None when a package declares multiple environments.
    dependency_environment: DependencyEnvironment | None
    python_version: str | None
    dependency_lock: DependencyLock | None
    dependency_setup: tuple[DependencySetupAction, ...]
    storage: Mapping[str, str]
    database_migration: DatabaseMigrationDeclaration | None
    source_kind: AddonSourceKind
    trust_tier: AddonTrustTier
    manifest_sha256: str
    contract_sha256: str
    manifest: Mapping[str, Any] = field(repr=False)
    provenance: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    diagnostics: tuple[AddonDiagnostic, ...] = ()
    migration_bundle: AddonMigrationContract | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def python_environment_for_runtime(
        self,
        runtime_unit: str,
    ) -> PythonDependencyEnvironment | None:
        """Return the dependency environment explicitly owning a runtime."""

        unit_id = str(runtime_unit or "").strip()
        return next(
            (
                environment
                for environment in self.python_environments
                if unit_id in environment.runtime_units
            ),
            None,
        )

    def python_environments_of_kind(
        self,
        environment: DependencyEnvironment,
    ) -> tuple[PythonDependencyEnvironment, ...]:
        """Return every declared environment using a placement strategy."""

        return tuple(
            item for item in self.python_environments if item.environment == environment
        )

    def to_dict(self, *, include_manifest: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "addon_id": self.addon_id,
            "name": self.name,
            "version": self.version,
            "manifest_schema_version": self.manifest_schema_version,
            "api_requires": self.api_requires,
            "execution_mode": self.execution_mode,
            "entry_point": {
                "module": self.entry_point_module,
                "class": self.entry_point_class,
            },
            "roles": list(self.roles),
            "categories": list(self.categories),
            "kind": self.kind.value,
            "distribution": {
                "channel": self.distribution.channel.value,
                "publisher": self.distribution.publisher,
                "source_uri": self.distribution.source_uri,
            },
            "runtime_units": [
                {
                    "id": item.id,
                    "mode": item.mode.value,
                    "entry_point": (
                        {
                            "module": item.entry_point_module,
                            "class": item.entry_point_class,
                        }
                        if item.entry_point_module or item.entry_point_class
                        else None
                    ),
                    "lifecycle": item.lifecycle,
                    "dependency_artifacts": [
                        asdict(artifact) for artifact in item.dependency_artifacts
                    ],
                    "requirements": _deep_thaw(item.requirements),
                    "config": _deep_thaw(item.config),
                }
                for item in self.runtime_units
            ],
            "capability_requirements": [
                {
                    "id": item.id,
                    "capability": item.capability_id,
                    "version": item.version_spec,
                    "operations": list(item.operations),
                    "cardinality": item.cardinality,
                    "activation": item.activation,
                    "scope": item.scope,
                    "qualifiers": _deep_thaw(item.qualifiers),
                }
                for item in self.capability_requirements
            ],
            "capability_providers": [
                {
                    "id": item.id,
                    "capability": item.capability_id,
                    "version": item.version,
                    "operations": list(item.operations),
                    "runtime_unit": item.runtime_unit,
                    "connector_definition": item.connector_definition,
                    "setup": _deep_thaw(item.setup),
                    "qualifiers": _deep_thaw(item.qualifiers),
                }
                for item in self.capability_providers
            ],
            "connector_definitions": [
                {
                    "id": item.id,
                    "mode": item.mode,
                    "label": item.label,
                    "fields": [asdict(field) for field in item.fields],
                    "setup_url": item.setup_url,
                    "callback_path": item.callback_path,
                    "disconnect_url": item.disconnect_url,
                    "test_method": item.test_method,
                }
                for item in self.connector_definitions
            ],
            "ui_entries": [
                {
                    "id": item.id,
                    "mode": item.mode.value,
                    "module": item.module,
                    "route": item.route,
                    "css_assets": list(item.css_assets),
                }
                for item in self.ui_entries
            ],
            "permissions": [asdict(item) for item in self.permissions],
            "resources": [
                {
                    "id": item.id,
                    "access": list(item.access),
                    "description": item.description,
                }
                for item in self.resources
            ],
            "addon_dependencies": list(self.addon_dependencies),
            "python_environments": [
                {
                    "id": item.id,
                    "environment": item.environment.value,
                    "runtime_units": list(item.runtime_units),
                    "python_version": item.python_version,
                    "lock": asdict(item.lock) if item.lock else None,
                    "setup": [asdict(action) for action in item.setup],
                    "manifest_path": item.manifest_path,
                    "manifest_sha256": item.manifest_sha256,
                    "pip_packages": list(item.pip_packages),
                    "conda_packages": list(item.conda_packages),
                }
                for item in self.python_environments
            ],
            "dependency_environment": (
                self.dependency_environment.value
                if self.dependency_environment is not None
                else None
            ),
            "python_version": self.python_version,
            "dependency_lock": (
                asdict(self.dependency_lock) if self.dependency_lock else None
            ),
            "dependency_setup": [asdict(item) for item in self.dependency_setup],
            "storage": dict(self.storage),
            "database_migration": (
                _deep_thaw(asdict(self.database_migration))
                if self.database_migration
                else None
            ),
            "source_kind": self.source_kind.value,
            "trust_tier": self.trust_tier.value,
            "manifest_sha256": self.manifest_sha256,
            "contract_sha256": self.contract_sha256,
            "provenance": _deep_thaw(self.provenance),
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }
        if include_manifest:
            payload["manifest"] = _deep_thaw(self.manifest)
        return payload


@dataclass(frozen=True, slots=True)
class ContractValidation:
    contract: ResolvedAddonContract | None
    diagnostics: tuple[AddonDiagnostic, ...]
    metrics: Mapping[str, float] = field(
        default_factory=lambda: MappingProxyType({}),
        repr=False,
        compare=False,
    )

    @property
    def valid(self) -> bool:
        return self.contract is not None and not any(
            item.severity == "error" for item in self.diagnostics
        )

    @property
    def errors(self) -> tuple[AddonDiagnostic, ...]:
        return tuple(item for item in self.diagnostics if item.severity == "error")

    @property
    def warnings(self) -> tuple[AddonDiagnostic, ...]:
        return tuple(item for item in self.diagnostics if item.severity == "warning")

    def to_dict(self, *, include_manifest: bool = False) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "contract": (
                self.contract.to_dict(include_manifest=include_manifest)
                if self.contract
                else None
            ),
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }


class AddonContractError(ValueError):
    def __init__(
        self, diagnostics: tuple[AddonDiagnostic, ...] | list[AddonDiagnostic]
    ):
        self.diagnostics = tuple(diagnostics)
        details = "; ".join(
            f"{item.path + ': ' if item.path else ''}{item.message}"
            for item in self.diagnostics
            if item.severity == "error"
        )
        super().__init__(details or "The add-on contract is invalid.")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, list | tuple):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, set | frozenset):
        return tuple(sorted((_deep_freeze(item) for item in value), key=repr))
    return copy.deepcopy(value)


def _deep_thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _deep_thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_deep_thaw(item) for item in value]
    return copy.deepcopy(value)


def _diagnostic(
    diagnostics: list[AddonDiagnostic],
    code: str,
    message: str,
    *,
    path: str = "",
    severity: str = "error",
) -> None:
    candidate = AddonDiagnostic(code, message, path, severity)
    if candidate not in diagnostics:
        diagnostics.append(candidate)


def _jsonschema_diagnostics(manifest: dict[str, Any]) -> list[AddonDiagnostic]:
    diagnostics: list[AddonDiagnostic] = []
    for error in sorted(
        _MANIFEST_VALIDATOR.iter_errors(manifest),
        key=lambda item: tuple(str(part) for part in item.path),
    ):
        path = ".".join(str(part) for part in error.absolute_path)
        _diagnostic(
            diagnostics,
            "schema.invalid",
            error.message,
            path=path,
        )
    return diagnostics


def _safe_relative_file(
    addon_dir: Path,
    raw_path: str,
    diagnostics: list[AddonDiagnostic],
    *,
    field_path: str,
) -> Path | None:
    if not raw_path or Path(raw_path).is_absolute():
        _diagnostic(
            diagnostics,
            "path.relative_required",
            "The path must be relative to the add-on package.",
            path=field_path,
        )
        return None
    try:
        candidate = (addon_dir / raw_path).resolve()
        candidate.relative_to(addon_dir)
    except OSError, ValueError:
        _diagnostic(
            diagnostics,
            "path.escape",
            "The path escapes the add-on package.",
            path=field_path,
        )
        return None
    return candidate


def _source_trust(
    source_kind: AddonSourceKind,
    execution_mode: str,
    *,
    has_host_migrations: bool = False,
) -> AddonTrustTier:
    if source_kind == AddonSourceKind.BUILT_IN:
        return AddonTrustTier.BUILT_IN_TRUSTED
    if has_host_migrations:
        return AddonTrustTier.INSTALLED_TRUSTED_CODE
    if execution_mode == "isolated_service":
        return AddonTrustTier.ISOLATED_SERVICE
    if execution_mode == "remote_integration":
        return AddonTrustTier.REMOTE_INTEGRATION
    return AddonTrustTier.INSTALLED_TRUSTED_CODE


def _legacy_kind(roles: tuple[str, ...]) -> AddonKind:
    normalized = {item.strip().lower() for item in roles}
    if "service" in normalized:
        return AddonKind.ENGINE
    if normalized.intersection({"workspace", "page"}):
        return AddonKind.APPLICATION
    if (
        "provider" in normalized
        and "integration" in normalized
        and "toolkit" not in normalized
    ):
        return AddonKind.CONNECTOR
    if "ui_component" in normalized and not normalized.intersection(
        {"toolkit", "provider", "service", "workspace", "page"}
    ):
        return AddonKind.COMPONENT
    return AddonKind.TOOLKIT


def _compatibility_roles(kind: AddonKind) -> tuple[str, ...]:
    return {
        AddonKind.APPLICATION: ("workspace",),
        AddonKind.TOOLKIT: ("toolkit",),
        AddonKind.CONNECTOR: ("integration", "provider"),
        AddonKind.ENGINE: ("service", "provider"),
        AddonKind.COMPONENT: ("ui_component",),
    }[kind]


def _default_distribution(source_kind: AddonSourceKind) -> DistributionDeclaration:
    channel = (
        DistributionChannel.BUILT_IN
        if source_kind == AddonSourceKind.BUILT_IN
        else DistributionChannel.COMMUNITY
    )
    return DistributionDeclaration(channel=channel)


def _relative_asset_path_valid(value: str) -> bool:
    if not value or "://" in value or value.startswith(("/", "\\")):
        return False
    return ".." not in Path(value.replace("\\", "/")).parts


def _legacy_runtime_units(
    manifest: Mapping[str, Any],
    *,
    execution_mode: str,
    entry_point_module: str,
    entry_point_class: str,
) -> tuple[RuntimeUnit, ...]:
    mode = {
        "native": RuntimeMode.IN_PROCESS,
        "isolated_service": RuntimeMode.MANAGED_PROCESS,
        "remote_integration": RuntimeMode.REMOTE,
    }[execution_mode]
    units: list[RuntimeUnit] = [
        RuntimeUnit(
            id="host" if mode == RuntimeMode.IN_PROCESS else "service",
            mode=mode,
            entry_point_module=entry_point_module,
            entry_point_class=entry_point_class,
            lifecycle=(
                "host_managed" if mode == RuntimeMode.IN_PROCESS else "on_demand"
            ),
        )
    ]

    docker = manifest.get("docker")
    if not isinstance(docker, Mapping):
        return tuple(units)
    raw_services = docker.get("services")
    if isinstance(raw_services, Mapping):
        services = raw_services.items()
    elif docker:
        services = (("container", docker),)
    else:
        services = ()
    used_ids = {item.id for item in units}
    for raw_id, raw_service in services:
        if not isinstance(raw_service, Mapping):
            continue
        unit_id = str(raw_id).strip().lower() or "container"
        if not RESOURCE_ID_PATTERN.fullmatch(unit_id) or unit_id in used_ids:
            unit_id = f"container-{len(used_ids)}"
        used_ids.add(unit_id)
        lifecycle = str(
            raw_service.get("lifecycle") or docker.get("lifecycle") or "on_demand"
        )
        requirements: dict[str, Any] = {}
        if raw_service.get("gpu") in RUNTIME_GPU_REQUIREMENTS:
            requirements["gpu"] = str(raw_service.get("gpu"))
        elif raw_service.get("gpu_required") is not None:
            requirements["gpu"] = (
                "required" if raw_service.get("gpu_required") else "none"
            )
        units.append(
            RuntimeUnit(
                id=unit_id,
                mode=RuntimeMode.CONTAINER,
                lifecycle=lifecycle,
                requirements=_deep_freeze(requirements),
                config=_deep_freeze(dict(raw_service)),
            )
        )
    return tuple(units)


def _parse_v2_runtime_units(
    manifest: Mapping[str, Any],
    diagnostics: list[AddonDiagnostic],
    *,
    resolved_dir: Path | None,
    strict_mode: bool,
) -> tuple[RuntimeUnit, ...]:
    runtime = manifest.get("runtime")
    raw_units = runtime.get("units") if isinstance(runtime, Mapping) else None
    if not isinstance(raw_units, list):
        _diagnostic(
            diagnostics,
            "runtime.units_required",
            "Manifest schema 2 requires runtime.units as a list, including an empty list.",
            path="runtime.units",
        )
        return ()

    units: list[RuntimeUnit] = []
    used_ids: set[str] = set()
    dependency_artifact_owners: dict[str, str] = {}
    for index, raw_unit in enumerate(raw_units):
        path = f"runtime.units.{index}"
        if not isinstance(raw_unit, Mapping):
            _diagnostic(
                diagnostics,
                "runtime.item_invalid",
                "Runtime units must be objects.",
                path=path,
            )
            continue
        unit_id = str(raw_unit.get("id") or "").strip()
        if not RESOURCE_ID_PATTERN.fullmatch(unit_id):
            _diagnostic(
                diagnostics,
                "runtime.id_invalid",
                "Runtime unit IDs must be stable lowercase identifiers.",
                path=f"{path}.id",
            )
        elif unit_id in used_ids:
            _diagnostic(
                diagnostics,
                "runtime.id_duplicate",
                f"Runtime unit ID {unit_id!r} is declared more than once.",
                path=f"{path}.id",
            )
        else:
            used_ids.add(unit_id)

        raw_mode = str(raw_unit.get("mode") or "").strip()
        try:
            mode = RuntimeMode(raw_mode)
        except ValueError:
            _diagnostic(
                diagnostics,
                "runtime.mode_invalid",
                "Runtime mode must be one of: "
                + ", ".join(item.value for item in RuntimeMode)
                + ".",
                path=f"{path}.mode",
            )
            mode = RuntimeMode.IN_PROCESS

        lifecycle = str(
            raw_unit.get("lifecycle")
            or ("host_managed" if mode == RuntimeMode.IN_PROCESS else "on_demand")
        ).strip()
        if lifecycle not in RUNTIME_LIFECYCLES:
            _diagnostic(
                diagnostics,
                "runtime.lifecycle_invalid",
                "Runtime lifecycle must be one of: "
                + ", ".join(sorted(RUNTIME_LIFECYCLES))
                + ".",
                path=f"{path}.lifecycle",
            )

        entry_point = raw_unit.get("entry_point")
        if entry_point is None:
            entry_point = {}
        if not isinstance(entry_point, Mapping):
            _diagnostic(
                diagnostics,
                "runtime.entry_point_invalid",
                "runtime unit entry_point must be an object.",
                path=f"{path}.entry_point",
            )
            entry_point = {}
        module = str(entry_point.get("module") or "").strip()
        class_name = str(entry_point.get("class") or "").strip()
        if mode == RuntimeMode.IN_PROCESS:
            if not ENTRY_POINT_MODULE_PATTERN.fullmatch(module):
                _diagnostic(
                    diagnostics,
                    "runtime.module_invalid",
                    "In-process runtime modules must be '.' or a dotted Python identifier.",
                    path=f"{path}.entry_point.module",
                )
            if not class_name.isidentifier():
                _diagnostic(
                    diagnostics,
                    "runtime.class_invalid",
                    "In-process runtime units must declare a Python entry-point class.",
                    path=f"{path}.entry_point.class",
                )

        requirements = raw_unit.get("requirements", {})
        if not isinstance(requirements, Mapping):
            _diagnostic(
                diagnostics,
                "runtime.requirements_invalid",
                "Runtime requirements must be an object.",
                path=f"{path}.requirements",
            )
            requirements = {}
        gpu_requirement = requirements.get("gpu")
        if (
            gpu_requirement is not None
            and gpu_requirement not in RUNTIME_GPU_REQUIREMENTS
        ):
            _diagnostic(
                diagnostics,
                "runtime.gpu_requirement_invalid",
                "Runtime GPU requirements must be one of: none, optional, required.",
                path=f"{path}.requirements.gpu",
            )
        config = raw_unit.get("config", {})
        if not isinstance(config, Mapping):
            _diagnostic(
                diagnostics,
                "runtime.config_invalid",
                "Runtime configuration must be an object.",
                path=f"{path}.config",
            )
            config = {}

        raw_dependency_artifacts = raw_unit.get("dependency_artifacts", [])
        if not isinstance(raw_dependency_artifacts, list):
            _diagnostic(
                diagnostics,
                "runtime.dependency_artifacts_invalid",
                "Runtime dependency_artifacts must be a list.",
                path=f"{path}.dependency_artifacts",
            )
            raw_dependency_artifacts = []
        if raw_dependency_artifacts and mode != RuntimeMode.CONTAINER:
            _diagnostic(
                diagnostics,
                "runtime.dependency_artifacts_mode_invalid",
                "Runtime dependency artifacts may only be owned by a container unit; host and managed-process Python inputs belong in dependencies.python.environments.",
                path=f"{path}.dependency_artifacts",
            )

        dependency_artifacts: list[RuntimeDependencyArtifact] = []
        for artifact_index, raw_artifact in enumerate(raw_dependency_artifacts):
            artifact_path = f"{path}.dependency_artifacts.{artifact_index}"
            if not isinstance(raw_artifact, Mapping):
                _diagnostic(
                    diagnostics,
                    "runtime.dependency_artifact_invalid",
                    "Runtime dependency artifacts must be objects.",
                    path=artifact_path,
                )
                continue

            raw_file_path = (
                str(raw_artifact.get("path") or "").strip().replace("\\", "/")
            )
            relative_path = PurePosixPath(raw_file_path)
            if (
                not raw_file_path
                or relative_path.is_absolute()
                or any(part in {"", ".", ".."} for part in relative_path.parts)
            ):
                _diagnostic(
                    diagnostics,
                    "runtime.dependency_artifact_path_invalid",
                    "Runtime dependency artifact paths must be safe paths relative to the add-on package.",
                    path=f"{artifact_path}.path",
                )
                continue
            normalized_path = relative_path.as_posix()
            normalized_key = normalized_path.casefold()
            previous_owner = dependency_artifact_owners.get(normalized_key)
            if previous_owner is not None:
                _diagnostic(
                    diagnostics,
                    "runtime.dependency_artifact_duplicate",
                    f"Runtime dependency artifact {normalized_path!r} is already owned by runtime unit {previous_owner!r}.",
                    path=f"{artifact_path}.path",
                )
                continue
            dependency_artifact_owners[normalized_key] = unit_id

            artifact_format = str(raw_artifact.get("format") or "").strip()
            if artifact_format not in RUNTIME_DEPENDENCY_ARTIFACT_FORMATS:
                _diagnostic(
                    diagnostics,
                    "runtime.dependency_artifact_format_invalid",
                    "Runtime dependency artifact format must be one of: "
                    + ", ".join(sorted(RUNTIME_DEPENDENCY_ARTIFACT_FORMATS))
                    + ".",
                    path=f"{artifact_path}.format",
                )

            artifact_sha256 = ""
            if resolved_dir is not None:
                artifact_file = _safe_relative_file(
                    resolved_dir,
                    normalized_path,
                    diagnostics,
                    field_path=f"{artifact_path}.path",
                )
                if artifact_file is not None and not artifact_file.is_file():
                    _diagnostic(
                        diagnostics,
                        "runtime.dependency_artifact_missing",
                        "The declared runtime dependency artifact is missing.",
                        path=f"{artifact_path}.path",
                    )
                elif artifact_file is not None:
                    artifact_sha256 = hashlib.sha256(
                        artifact_file.read_bytes()
                    ).hexdigest()
                    if strict_mode and artifact_format == "pip_requirements":
                        _validate_hashed_requirement_lock(
                            artifact_file,
                            diagnostics,
                            field_path=f"{artifact_path}.path",
                        )
                    elif strict_mode and artifact_format == "conda_explicit":
                        _validate_conda_explicit_lock(
                            artifact_file,
                            diagnostics,
                            field_path=f"{artifact_path}.path",
                        )

            dependency_artifacts.append(
                RuntimeDependencyArtifact(
                    path=normalized_path,
                    sha256=artifact_sha256,
                    format=artifact_format,
                )
            )

        units.append(
            RuntimeUnit(
                id=unit_id,
                mode=mode,
                entry_point_module=module,
                entry_point_class=class_name,
                lifecycle=lifecycle,
                dependency_artifacts=tuple(dependency_artifacts),
                requirements=_deep_freeze(dict(requirements)),
                config=_deep_freeze(dict(config)),
            )
        )
    return tuple(units)


def _legacy_execution_from_runtime(
    units: tuple[RuntimeUnit, ...],
) -> tuple[str, str, str]:
    for unit in units:
        if unit.mode == RuntimeMode.IN_PROCESS:
            return "native", unit.entry_point_module, unit.entry_point_class
    if any(
        unit.mode in {RuntimeMode.MANAGED_PROCESS, RuntimeMode.CONTAINER}
        for unit in units
    ):
        return "isolated_service", "", ""
    if any(unit.mode == RuntimeMode.REMOTE for unit in units):
        return "remote_integration", "", ""
    return "native", ".", ""


def _parse_v2_ui_entries(
    manifest: Mapping[str, Any],
    diagnostics: list[AddonDiagnostic],
) -> tuple[UiEntry, ...]:
    ui = manifest.get("ui")
    raw_entries = ui.get("entries") if isinstance(ui, Mapping) else None
    if not isinstance(raw_entries, list):
        _diagnostic(
            diagnostics,
            "ui.entries_required",
            "Manifest schema 2 requires ui.entries as a list, including an empty list.",
            path="ui.entries",
        )
        return ()
    entries: list[UiEntry] = []
    used_ids: set[str] = set()
    for index, raw_entry in enumerate(raw_entries):
        path = f"ui.entries.{index}"
        if not isinstance(raw_entry, Mapping):
            _diagnostic(
                diagnostics,
                "ui.item_invalid",
                "UI entries must be objects.",
                path=path,
            )
            continue
        entry_id = str(raw_entry.get("id") or "").strip()
        if not RESOURCE_ID_PATTERN.fullmatch(entry_id):
            _diagnostic(
                diagnostics,
                "ui.id_invalid",
                "UI entry IDs must be stable lowercase identifiers.",
                path=f"{path}.id",
            )
        elif entry_id in used_ids:
            _diagnostic(
                diagnostics,
                "ui.id_duplicate",
                f"UI entry ID {entry_id!r} is declared more than once.",
                path=f"{path}.id",
            )
        else:
            used_ids.add(entry_id)
        raw_mode = str(raw_entry.get("mode") or "").strip()
        try:
            mode = UiMode(raw_mode)
        except ValueError:
            _diagnostic(
                diagnostics,
                "ui.mode_invalid",
                "UI mode must be one of: "
                + ", ".join(item.value for item in UiMode)
                + ".",
                path=f"{path}.mode",
            )
            mode = UiMode.NONE
        module = str(raw_entry.get("module") or "").strip()
        if module and not _relative_asset_path_valid(module):
            _diagnostic(
                diagnostics,
                "ui.module_invalid",
                "UI modules must be relative same-addon asset paths.",
                path=f"{path}.module",
            )
        if mode in {UiMode.SVELTE_APPLICATION, UiMode.SVELTE_COMPONENT} and not module:
            _diagnostic(
                diagnostics,
                "ui.module_required",
                "Svelte UI entries must declare a module.",
                path=f"{path}.module",
            )
        raw_css_assets = raw_entry.get("css_assets")
        if raw_css_assets is None:
            raw_css_assets = []
        if not isinstance(raw_css_assets, list) or any(
            not isinstance(item, str) for item in raw_css_assets
        ):
            _diagnostic(
                diagnostics,
                "ui.css_assets_invalid",
                "UI css_assets must be a list of relative asset paths.",
                path=f"{path}.css_assets",
            )
            raw_css_assets = ()
        css_assets = tuple(str(item).strip() for item in raw_css_assets)
        if any(not _relative_asset_path_valid(item) for item in css_assets):
            _diagnostic(
                diagnostics,
                "ui.css_asset_invalid",
                "UI CSS assets must be relative same-addon asset paths.",
                path=f"{path}.css_assets",
            )
        entries.append(
            UiEntry(
                id=entry_id,
                mode=mode,
                module=module,
                route=str(raw_entry.get("route") or "").strip(),
                css_assets=css_assets,
            )
        )
    return tuple(entries)


def _legacy_ui_entries(manifest: Mapping[str, Any]) -> tuple[UiEntry, ...]:
    frontend = manifest.get("frontend")
    if not isinstance(frontend, Mapping):
        return ()
    tier = str(frontend.get("tier") or "").strip()
    if not tier:
        return ()
    module = str(frontend.get("module") or "").strip()
    if tier == "schema":
        mode = UiMode.SCHEMA
    elif tier in {"compiled_workspace", "hybrid"} and module:
        mode = UiMode.SVELTE_APPLICATION
    elif tier == "external_service":
        mode = UiMode.EXTERNAL
    else:
        mode = UiMode.SCHEMA
    return (
        UiEntry(
            id="primary",
            mode=mode,
            module=module,
            route=str(frontend.get("route") or "").strip(),
            css_assets=tuple(
                str(item).strip() for item in frontend.get("css_assets") or ()
            ),
        ),
    )


def _parse_v2_connector_definitions(
    manifest: Mapping[str, Any],
    diagnostics: list[AddonDiagnostic],
) -> tuple[ConnectorDefinition, ...]:
    """Compile connector-owned account forms and lifecycle routes."""

    raw_definitions = manifest.get("connectors", [])
    if not isinstance(raw_definitions, list):
        _diagnostic(
            diagnostics,
            "connectors.invalid",
            "connectors must be a list of connector definition objects.",
            path="connectors",
        )
        return ()

    definitions: list[ConnectorDefinition] = []
    used_ids: set[str] = set()
    for index, raw_definition in enumerate(raw_definitions):
        path = f"connectors.{index}"
        if not isinstance(raw_definition, Mapping):
            _diagnostic(
                diagnostics,
                "connectors.item_invalid",
                "Connector definitions must be objects.",
                path=path,
            )
            continue

        definition_id = str(raw_definition.get("id") or "").strip()
        if not RESOURCE_ID_PATTERN.fullmatch(definition_id):
            _diagnostic(
                diagnostics,
                "connectors.id_invalid",
                "Connector definition IDs must be stable lowercase identifiers.",
                path=f"{path}.id",
            )
        elif definition_id in used_ids:
            _diagnostic(
                diagnostics,
                "connectors.id_duplicate",
                f"Connector definition ID {definition_id!r} is declared more than once.",
                path=f"{path}.id",
            )
        else:
            used_ids.add(definition_id)

        mode = str(raw_definition.get("mode") or "").strip()
        if mode not in CONNECTOR_MODES:
            _diagnostic(
                diagnostics,
                "connectors.mode_invalid",
                "Connector mode must be one of: "
                + ", ".join(sorted(CONNECTOR_MODES))
                + ".",
                path=f"{path}.mode",
            )

        label = str(raw_definition.get("label") or "").strip()
        if not label:
            _diagnostic(
                diagnostics,
                "connectors.label_required",
                "Connector definitions must declare a user-facing label.",
                path=f"{path}.label",
            )

        route_values: dict[str, str] = {}
        for key in ("setup_url", "callback_path", "disconnect_url"):
            value = str(raw_definition.get(key) or "").strip()
            if value and (not value.startswith("/") or value.startswith("//")):
                _diagnostic(
                    diagnostics,
                    "connectors.route_invalid",
                    f"{key} must be a same-origin absolute path beginning with '/'.",
                    path=f"{path}.{key}",
                )
            route_values[key] = value

        test_method = str(raw_definition.get("test_method") or "").strip()
        if test_method and (
            not test_method.isidentifier() or not test_method.startswith("test_")
        ):
            _diagnostic(
                diagnostics,
                "connectors.test_method_invalid",
                "test_method must be a Python method identifier beginning with 'test_'.",
                path=f"{path}.test_method",
            )

        raw_fields = raw_definition.get("fields", [])
        if not isinstance(raw_fields, list):
            _diagnostic(
                diagnostics,
                "connectors.fields_invalid",
                "Connector fields must be a list.",
                path=f"{path}.fields",
            )
            raw_fields = []

        fields: list[ConnectorField] = []
        field_ids: set[str] = set()
        label_fields = 0
        for field_index, raw_field in enumerate(raw_fields):
            field_path = f"{path}.fields.{field_index}"
            if not isinstance(raw_field, Mapping):
                _diagnostic(
                    diagnostics,
                    "connectors.field_invalid",
                    "Connector fields must be objects.",
                    path=field_path,
                )
                continue
            field_id = str(raw_field.get("id") or "").strip()
            if not RESOURCE_ID_PATTERN.fullmatch(field_id):
                _diagnostic(
                    diagnostics,
                    "connectors.field_id_invalid",
                    "Connector field IDs must be stable lowercase identifiers.",
                    path=f"{field_path}.id",
                )
            elif field_id in field_ids:
                _diagnostic(
                    diagnostics,
                    "connectors.field_id_duplicate",
                    f"Connector field ID {field_id!r} is declared more than once.",
                    path=f"{field_path}.id",
                )
            else:
                field_ids.add(field_id)

            name = str(raw_field.get("name") or "").strip()
            if not name:
                _diagnostic(
                    diagnostics,
                    "connectors.field_name_required",
                    "Connector fields must declare a user-facing name.",
                    path=f"{field_path}.name",
                )
            kind = str(raw_field.get("kind") or "text").strip()
            if kind not in CONNECTOR_FIELD_KINDS:
                _diagnostic(
                    diagnostics,
                    "connectors.field_kind_invalid",
                    "Connector field kind must be one of: "
                    + ", ".join(sorted(CONNECTOR_FIELD_KINDS))
                    + ".",
                    path=f"{field_path}.kind",
                )
            storage = str(raw_field.get("storage") or "").strip()
            if storage not in CONNECTOR_FIELD_STORAGES:
                _diagnostic(
                    diagnostics,
                    "connectors.field_storage_invalid",
                    "Connector field storage must be one of: "
                    + ", ".join(sorted(CONNECTOR_FIELD_STORAGES))
                    + ".",
                    path=f"{field_path}.storage",
                )
            if kind == "password" and storage != "secret":
                _diagnostic(
                    diagnostics,
                    "connectors.password_not_secret",
                    "Password fields must use secret storage.",
                    path=f"{field_path}.storage",
                )
            if storage == "label":
                label_fields += 1

            required = raw_field.get("required", True)
            if not isinstance(required, bool):
                _diagnostic(
                    diagnostics,
                    "connectors.field_required_invalid",
                    "Connector field required must be a boolean.",
                    path=f"{field_path}.required",
                )
                required = True
            fields.append(
                ConnectorField(
                    id=field_id,
                    name=name,
                    kind=kind,
                    storage=storage,
                    required=required,
                    placeholder=str(raw_field.get("placeholder") or "").strip(),
                    description=str(raw_field.get("description") or "").strip(),
                )
            )
        if label_fields > 1:
            _diagnostic(
                diagnostics,
                "connectors.label_field_duplicate",
                "A connector definition may declare at most one label field.",
                path=f"{path}.fields",
            )
        if mode == "credentials" and not fields:
            _diagnostic(
                diagnostics,
                "connectors.fields_required",
                "Credential connectors must declare their input fields.",
                path=f"{path}.fields",
            )

        definitions.append(
            ConnectorDefinition(
                id=definition_id,
                mode=mode,
                label=label,
                fields=tuple(fields),
                setup_url=route_values["setup_url"],
                callback_path=route_values["callback_path"],
                disconnect_url=route_values["disconnect_url"],
                test_method=test_method,
            )
        )
    return tuple(definitions)


def _parse_capability_operations(
    raw_operations: Any,
    diagnostics: list[AddonDiagnostic],
    *,
    path: str,
) -> tuple[str, ...]:
    if raw_operations is None:
        return ()
    if not isinstance(raw_operations, list):
        _diagnostic(
            diagnostics,
            "capability.operations_invalid",
            "Capability operations must be a list of stable identifiers.",
            path=path,
        )
        return ()
    operations: list[str] = []
    for index, raw_operation in enumerate(raw_operations):
        operation = str(raw_operation).strip()
        if not RESOURCE_ID_PATTERN.fullmatch(operation):
            _diagnostic(
                diagnostics,
                "capability.operation_invalid",
                "Capability operation IDs must be stable lowercase identifiers.",
                path=f"{path}.{index}",
            )
        elif operation not in operations:
            operations.append(operation)
    return tuple(operations)


def _parse_v2_capabilities(
    manifest: Mapping[str, Any],
    diagnostics: list[AddonDiagnostic],
    *,
    runtime_units: tuple[RuntimeUnit, ...],
) -> tuple[tuple[CapabilityRequirement, ...], tuple[CapabilityProvider, ...]]:
    raw_requires = manifest.get("requires")
    raw_provides = manifest.get("provides")
    raw_requirements = (
        raw_requires.get("capabilities") if isinstance(raw_requires, Mapping) else None
    )
    raw_providers = (
        raw_provides.get("capabilities") if isinstance(raw_provides, Mapping) else None
    )
    if not isinstance(raw_requirements, list):
        _diagnostic(
            diagnostics,
            "capability.requirements_required",
            "Manifest schema 2 requires requires.capabilities as a list.",
            path="requires.capabilities",
        )
        raw_requirements = []
    if not isinstance(raw_providers, list):
        _diagnostic(
            diagnostics,
            "capability.providers_required",
            "Manifest schema 2 requires provides.capabilities as a list.",
            path="provides.capabilities",
        )
        raw_providers = []

    requirements: list[CapabilityRequirement] = []
    requirement_ids: set[str] = set()
    for index, raw_requirement in enumerate(raw_requirements):
        path = f"requires.capabilities.{index}"
        if not isinstance(raw_requirement, Mapping):
            _diagnostic(
                diagnostics,
                "capability.requirement_invalid",
                "Capability requirements must be objects.",
                path=path,
            )
            continue
        declaration_id = str(raw_requirement.get("id") or "").strip()
        capability_id = str(raw_requirement.get("capability") or "").strip()
        if not RESOURCE_ID_PATTERN.fullmatch(declaration_id):
            _diagnostic(
                diagnostics,
                "capability.requirement_id_invalid",
                "Capability requirement IDs must be stable lowercase identifiers.",
                path=f"{path}.id",
            )
        elif declaration_id in requirement_ids:
            _diagnostic(
                diagnostics,
                "capability.requirement_id_duplicate",
                f"Capability requirement ID {declaration_id!r} is declared more than once.",
                path=f"{path}.id",
            )
        else:
            requirement_ids.add(declaration_id)
        if not RESOURCE_ID_PATTERN.fullmatch(capability_id):
            _diagnostic(
                diagnostics,
                "capability.id_invalid",
                "Capability IDs must be stable lowercase identifiers.",
                path=f"{path}.capability",
            )
        version_spec = str(raw_requirement.get("version") or "").strip()
        try:
            SpecifierSet(version_spec)
        except InvalidSpecifier:
            _diagnostic(
                diagnostics,
                "capability.requirement_version_invalid",
                "Capability requirements must use a valid PEP 440 version range.",
                path=f"{path}.version",
            )
        cardinality = str(raw_requirement.get("cardinality") or "one").strip()
        if cardinality not in CAPABILITY_CARDINALITIES:
            _diagnostic(
                diagnostics,
                "capability.cardinality_invalid",
                "Capability cardinality must be one, many, or optional.",
                path=f"{path}.cardinality",
            )
        activation = str(raw_requirement.get("activation") or "activate").strip()
        if activation not in CAPABILITY_ACTIVATION_POLICIES:
            _diagnostic(
                diagnostics,
                "capability.activation_invalid",
                "Capability activation must be install, activate, or on_first_use.",
                path=f"{path}.activation",
            )
        scope = str(raw_requirement.get("scope") or "installation").strip()
        if scope not in CAPABILITY_SCOPES:
            _diagnostic(
                diagnostics,
                "capability.scope_invalid",
                "Capability scope must be installation, user, workspace, or workflow.",
                path=f"{path}.scope",
            )
        qualifiers = raw_requirement.get("qualifiers", {})
        if not isinstance(qualifiers, Mapping):
            _diagnostic(
                diagnostics,
                "capability.qualifiers_invalid",
                "Capability qualifiers must be an object.",
                path=f"{path}.qualifiers",
            )
            qualifiers = {}
        requirements.append(
            CapabilityRequirement(
                id=declaration_id,
                capability_id=capability_id,
                version_spec=version_spec,
                operations=_parse_capability_operations(
                    raw_requirement.get("operations"),
                    diagnostics,
                    path=f"{path}.operations",
                ),
                cardinality=cardinality,
                activation=activation,
                scope=scope,
                qualifiers=_deep_freeze(dict(qualifiers)),
            )
        )

    providers: list[CapabilityProvider] = []
    provider_ids: set[str] = set()
    runtime_ids = {unit.id for unit in runtime_units}
    for index, raw_provider in enumerate(raw_providers):
        path = f"provides.capabilities.{index}"
        if not isinstance(raw_provider, Mapping):
            _diagnostic(
                diagnostics,
                "capability.provider_invalid",
                "Capability providers must be objects.",
                path=path,
            )
            continue
        declaration_id = str(raw_provider.get("id") or "").strip()
        capability_id = str(raw_provider.get("capability") or "").strip()
        if not RESOURCE_ID_PATTERN.fullmatch(declaration_id):
            _diagnostic(
                diagnostics,
                "capability.provider_id_invalid",
                "Capability provider IDs must be stable lowercase identifiers.",
                path=f"{path}.id",
            )
        elif declaration_id in provider_ids:
            _diagnostic(
                diagnostics,
                "capability.provider_id_duplicate",
                f"Capability provider ID {declaration_id!r} is declared more than once.",
                path=f"{path}.id",
            )
        else:
            provider_ids.add(declaration_id)
        if not RESOURCE_ID_PATTERN.fullmatch(capability_id):
            _diagnostic(
                diagnostics,
                "capability.id_invalid",
                "Capability IDs must be stable lowercase identifiers.",
                path=f"{path}.capability",
            )
        version = str(raw_provider.get("version") or "").strip()
        try:
            Version(version)
        except InvalidVersion:
            _diagnostic(
                diagnostics,
                "capability.provider_version_invalid",
                "Capability providers must declare a valid PEP 440 version.",
                path=f"{path}.version",
            )
        runtime_unit = str(raw_provider.get("runtime_unit") or "").strip()
        if runtime_unit and runtime_unit not in runtime_ids:
            _diagnostic(
                diagnostics,
                "capability.runtime_unknown",
                f"Capability provider references unknown runtime unit {runtime_unit!r}.",
                path=f"{path}.runtime_unit",
            )
        qualifiers = raw_provider.get("qualifiers", {})
        if not isinstance(qualifiers, Mapping):
            _diagnostic(
                diagnostics,
                "capability.qualifiers_invalid",
                "Capability qualifiers must be an object.",
                path=f"{path}.qualifiers",
            )
            qualifiers = {}
        setup = raw_provider.get("setup", {})
        if not isinstance(setup, Mapping):
            _diagnostic(
                diagnostics,
                "capability.setup_invalid",
                "Capability provider setup must be an object.",
                path=f"{path}.setup",
            )
            setup = {}
        providers.append(
            CapabilityProvider(
                id=declaration_id,
                capability_id=capability_id,
                version=version,
                operations=_parse_capability_operations(
                    raw_provider.get("operations"),
                    diagnostics,
                    path=f"{path}.operations",
                ),
                runtime_unit=runtime_unit,
                connector_definition=str(
                    raw_provider.get("connector_definition") or ""
                ).strip(),
                setup=_deep_freeze(dict(setup)),
                qualifiers=_deep_freeze(dict(qualifiers)),
            )
        )
    return tuple(requirements), tuple(providers)


def _logical_requirement_lines(contents: str) -> tuple[str, ...]:
    """Return complete pip requirement records with continuations joined."""

    records: list[str] = []
    pending = ""
    for raw_line in contents.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        continuation = stripped.endswith("\\")
        fragment = stripped[:-1].rstrip() if continuation else stripped
        pending = f"{pending} {fragment}".strip()
        if not continuation:
            records.append(pending)
            pending = ""
    if pending:
        records.append(pending)
    return tuple(records)


def _validate_hashed_requirement_lock(
    lock_file: Path,
    diagnostics: list[AddonDiagnostic],
    *,
    field_path: str = "dependencies.python.lockfile",
) -> None:
    """Reject dependency locks that pip cannot enforce reproducibly."""

    try:
        contents = lock_file.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        _diagnostic(
            diagnostics,
            "dependencies.lock_encoding",
            "The dependency lock must be UTF-8 text.",
            path=field_path,
        )
        return

    requirements = 0
    for index, record in enumerate(_logical_requirement_lines(contents), start=1):
        if record == "--require-hashes":
            continue
        if record.startswith("-"):
            _diagnostic(
                diagnostics,
                "dependencies.lock_option_forbidden",
                "Dependency locks must be self-contained and cannot include other files, indexes, editable installs, or pip options.",
                path=f"{field_path}:{index}",
            )
            continue
        requirements += 1
        requirement = record.split(" --hash=", 1)[0].strip()
        if not PINNED_REQUIREMENT_PATTERN.match(requirement):
            _diagnostic(
                diagnostics,
                "dependencies.lock_unpinned",
                "Every dependency must use an exact 'name==version' pin.",
                path=f"{field_path}:{index}",
            )
        if not LOCK_HASH_PATTERN.search(record + " "):
            _diagnostic(
                diagnostics,
                "dependencies.lock_hash_missing",
                "Every dependency pin must include at least one SHA-256 hash.",
                path=f"{field_path}:{index}",
            )
    if not requirements:
        _diagnostic(
            diagnostics,
            "dependencies.lock_empty",
            "The declared dependency lock contains no dependency records.",
            path=field_path,
        )


def _validate_conda_explicit_lock(
    lock_file: Path,
    diagnostics: list[AddonDiagnostic],
    *,
    field_path: str,
) -> None:
    """Validate a no-solver Conda explicit lock without contacting its channels."""

    try:
        contents = lock_file.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        _diagnostic(
            diagnostics,
            "runtime.dependency_conda_encoding",
            "The Conda explicit lock must be UTF-8 text.",
            path=field_path,
        )
        return

    marker_count = 0
    package_entries = 0
    for line_number, raw_line in enumerate(contents.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line == "@EXPLICIT":
            marker_count += 1
            continue

        package_entries += 1
        entry_path = f"{field_path}:{line_number}"
        try:
            parsed = urlsplit(line)
            has_credentials = bool(parsed.username or parsed.password)
        except ValueError:
            parsed = None
            has_credentials = False
        if parsed is None:
            _diagnostic(
                diagnostics,
                "runtime.dependency_conda_url_invalid",
                "Every Conda explicit package entry must be an HTTPS URL.",
                path=entry_path,
            )
            continue
        if parsed.scheme != "https" or not parsed.netloc:
            _diagnostic(
                diagnostics,
                "runtime.dependency_conda_url_invalid",
                "Every Conda explicit package entry must be an HTTPS URL.",
                path=entry_path,
            )
        if has_credentials:
            _diagnostic(
                diagnostics,
                "runtime.dependency_conda_credentials_forbidden",
                "Conda explicit lock URLs must not embed credentials.",
                path=entry_path,
            )
        if parsed.query:
            _diagnostic(
                diagnostics,
                "runtime.dependency_conda_query_forbidden",
                "Conda explicit lock URLs must not contain query parameters.",
                path=entry_path,
            )
        if (
            re.fullmatch(r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{64})", parsed.fragment)
            is None
        ):
            _diagnostic(
                diagnostics,
                "runtime.dependency_conda_hash_missing",
                "Every Conda explicit package URL must carry a 32- or 64-hex integrity fragment.",
                path=entry_path,
            )

    if marker_count != 1:
        _diagnostic(
            diagnostics,
            "runtime.dependency_conda_explicit_required",
            "A Conda explicit lock must contain exactly one @EXPLICIT marker.",
            path=field_path,
        )
    if package_entries == 0:
        _diagnostic(
            diagnostics,
            "runtime.dependency_conda_empty",
            "The Conda explicit lock contains no package entries.",
            path=field_path,
        )


def _validate_pixi_lock(
    lock_file: Path,
    diagnostics: list[AddonDiagnostic],
    *,
    field_path: str = "dependencies.python.lockfile",
) -> None:
    """Perform cheap, no-execution validation of a committed Pixi lockfile."""

    try:
        contents = lock_file.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        _diagnostic(
            diagnostics,
            "dependencies.lock_encoding",
            "The Pixi dependency lock must be UTF-8 text.",
            path=field_path,
        )
        return
    lines = {
        line.split(":", 1)[0].strip() for line in contents.splitlines() if ":" in line
    }
    if not contents.strip() or not {"version", "environments"}.issubset(lines):
        _diagnostic(
            diagnostics,
            "dependencies.pixi_lock_invalid",
            "A Pixi lock must contain top-level version and environments records.",
            path=field_path,
        )


def _dependency_runtime_mode(
    environment: DependencyEnvironment,
) -> RuntimeMode:
    return (
        RuntimeMode.IN_PROCESS
        if environment == DependencyEnvironment.IN_PROCESS_VENDOR
        else RuntimeMode.MANAGED_PROCESS
    )


def _dependency_sequence(
    raw_value: Any,
    *,
    diagnostics: list[AddonDiagnostic],
    path: str,
) -> tuple[str, ...]:
    if raw_value is None:
        return ()
    if not isinstance(raw_value, list):
        _diagnostic(
            diagnostics,
            "dependencies.sequence_invalid",
            f"{path} must be a list of strings.",
            path=path,
        )
        return ()
    values: list[str] = []
    for index, raw_item in enumerate(raw_value):
        item = str(raw_item or "").strip()
        if not item:
            _diagnostic(
                diagnostics,
                "dependencies.sequence_item_invalid",
                f"{path} entries must be non-empty strings.",
                path=f"{path}.{index}",
            )
        elif item not in values:
            values.append(item)
    return tuple(values)


def _parse_python_dependency_environment(
    raw_dependencies: Mapping[str, Any],
    *,
    environment_id: str,
    path: str,
    schema_version: int,
    strict_mode: bool,
    resolved_dir: Path | None,
    runtime_units: tuple[RuntimeUnit, ...],
    permissions: tuple[PermissionDeclaration, ...] | list[PermissionDeclaration],
    legacy_binding: bool,
    legacy_dependencies: Mapping[str, Any],
    diagnostics: list[AddonDiagnostic],
) -> PythonDependencyEnvironment | None:
    """Validate one Python dependency environment without importing add-on code."""

    if not RESOURCE_ID_PATTERN.fullmatch(environment_id):
        _diagnostic(
            diagnostics,
            "dependencies.environment_id_invalid",
            "Python environment IDs must be stable lowercase identifiers.",
            path=f"{path}.id",
        )

    raw_python_version = str(raw_dependencies.get("python_version") or "").strip()
    python_version = ""
    if schema_version == 2:
        if not raw_python_version:
            _diagnostic(
                diagnostics,
                "dependencies.python_version_required",
                "Manifest schema 2 Python environments must declare an exact major.minor interpreter version.",
                path=f"{path}.python_version",
            )
        elif PYTHON_MINOR_PATTERN.fullmatch(raw_python_version) is None:
            _diagnostic(
                diagnostics,
                "dependencies.python_version_invalid",
                f"{path}.python_version must use the canonical major.minor form, for example '3.14'.",
                path=f"{path}.python_version",
            )
        else:
            python_version = raw_python_version
    elif raw_python_version:
        if PYTHON_MINOR_PATTERN.fullmatch(raw_python_version) is None:
            _diagnostic(
                diagnostics,
                "dependencies.python_version_invalid",
                f"{path}.python_version must use the canonical major.minor form, for example '3.14'.",
                path=f"{path}.python_version",
            )
        else:
            python_version = raw_python_version

    lock_path = str(raw_dependencies.get("lockfile") or "").strip()
    pip_packages = _dependency_sequence(
        raw_dependencies.get("pip_packages"),
        diagnostics=diagnostics,
        path=f"{path}.pip_packages",
    )
    conda_packages = _dependency_sequence(
        raw_dependencies.get("conda_packages"),
        diagnostics=diagnostics,
        path=f"{path}.conda_packages",
    )
    has_unlocked_dependencies = bool(
        pip_packages
        or conda_packages
        or (
            legacy_binding
            and schema_version == 1
            and resolved_dir is not None
            and (resolved_dir / "requirements.txt").is_file()
        )
        or (
            legacy_binding
            and schema_version == 1
            and resolved_dir is not None
            and (resolved_dir / "pixi.toml").is_file()
        )
    )
    has_python_dependencies = bool(lock_path or has_unlocked_dependencies)

    dependency_environment: DependencyEnvironment | None = None
    raw_environment = str(raw_dependencies.get("environment") or "").strip()
    raw_legacy_tier = str(raw_dependencies.get("tier") or "").strip()
    explicit_environment = None
    if raw_environment:
        explicit_environment = DEPENDENCY_ENVIRONMENT_ALIASES.get(raw_environment)
        if explicit_environment is None:
            explicit_environment = next(
                (
                    item
                    for item in DependencyEnvironment
                    if item.value == raw_environment
                ),
                None,
            )
    if raw_environment and raw_legacy_tier:
        legacy_environment = DEPENDENCY_ENVIRONMENT_ALIASES.get(raw_legacy_tier)
        if (
            explicit_environment is not None
            and legacy_environment is not None
            and legacy_environment != explicit_environment
        ):
            _diagnostic(
                diagnostics,
                "dependencies.environment_conflict",
                f"{path}.environment conflicts with the legacy tier alias.",
                path=f"{path}.environment",
            )
    if raw_environment:
        dependency_environment = explicit_environment
        if dependency_environment is None:
            if raw_environment in RETIRED_DEPENDENCY_ENVIRONMENTS:
                _diagnostic(
                    diagnostics,
                    "dependencies.micromamba_retired",
                    "Micromamba dependency environments are no longer supported. "
                    "Declare 'managed_process_pixi' and provide a reviewed "
                    "pixi.toml and pixi.lock instead.",
                    path=f"{path}.environment",
                )
            else:
                _diagnostic(
                    diagnostics,
                    "dependencies.environment_invalid",
                    "Python dependency environment must be one of: "
                    + ", ".join(item.value for item in DependencyEnvironment)
                    + ".",
                    path=f"{path}.environment",
                )
        elif raw_environment != dependency_environment.value:
            _diagnostic(
                diagnostics,
                "compat.dependency_environment",
                f"Legacy dependency environment {raw_environment!r} now resolves to "
                f"{dependency_environment.value!r}.",
                path=f"{path}.environment",
                severity="warning",
            )
    elif raw_legacy_tier:
        dependency_environment = DEPENDENCY_ENVIRONMENT_ALIASES.get(raw_legacy_tier)
        if dependency_environment is None:
            code = (
                "dependencies.micromamba_retired"
                if raw_legacy_tier in RETIRED_DEPENDENCY_ENVIRONMENTS
                else "dependencies.environment_invalid"
            )
            message = (
                "Micromamba dependency environments are no longer supported. "
                "Declare 'managed_process_pixi' and provide a reviewed pixi.toml "
                "and pixi.lock instead."
                if raw_legacy_tier in RETIRED_DEPENDENCY_ENVIRONMENTS
                else f"Unknown legacy Python dependency tier: {raw_legacy_tier!r}."
            )
            _diagnostic(
                diagnostics,
                code,
                message,
                path=f"{path}.tier",
            )
        elif schema_version == 2:
            _diagnostic(
                diagnostics,
                "compat.dependency_tier",
                "Use environment; numbered dependency tiers are deprecated aliases.",
                path=f"{path}.tier",
                severity="warning",
            )
    elif schema_version == 1:
        if (
            (resolved_dir is not None and (resolved_dir / "pixi.toml").is_file())
            or conda_packages
            or legacy_dependencies.get("system_packages")
        ):
            dependency_environment = DependencyEnvironment.MANAGED_PROCESS_PIXI
        elif has_python_dependencies:
            dependency_environment = DependencyEnvironment.IN_PROCESS_VENDOR
    else:
        _diagnostic(
            diagnostics,
            "dependencies.environment_required",
            "Manifest schema 2 Python environments must declare environment.",
            path=f"{path}.environment",
        )

    runtime_units_by_id = {item.id: item for item in runtime_units}
    raw_runtime_units = raw_dependencies.get("runtime_units")
    if raw_runtime_units is None and legacy_binding:
        required_mode = (
            _dependency_runtime_mode(dependency_environment)
            if dependency_environment is not None
            else None
        )
        bound_runtime_units = tuple(
            item.id
            for item in runtime_units
            if required_mode is not None and item.mode == required_mode
        )
        if schema_version == 2 and dependency_environment is not None:
            _diagnostic(
                diagnostics,
                "compat.python_environment_binding",
                "Declare dependencies.python.environments with explicit runtime_units; the package-level Python environment form is deprecated.",
                path=path,
                severity="warning",
            )
    else:
        bound_runtime_units = _dependency_sequence(
            raw_runtime_units,
            diagnostics=diagnostics,
            path=f"{path}.runtime_units",
        )
        if schema_version == 2 and not bound_runtime_units:
            _diagnostic(
                diagnostics,
                "dependencies.runtime_units_required",
                "Each Python environment must bind at least one runtime unit.",
                path=f"{path}.runtime_units",
            )

    if dependency_environment is not None:
        required_mode = _dependency_runtime_mode(dependency_environment)
        if not bound_runtime_units:
            _diagnostic(
                diagnostics,
                "dependencies.environment_runtime_mismatch",
                f"{dependency_environment.value} requires a {required_mode.value} runtime unit.",
                path=f"{path}.runtime_units",
            )
        for index, runtime_unit_id in enumerate(bound_runtime_units):
            runtime_unit = runtime_units_by_id.get(runtime_unit_id)
            if runtime_unit is None:
                _diagnostic(
                    diagnostics,
                    "dependencies.runtime_unit_unknown",
                    f"Unknown dependency runtime unit: {runtime_unit_id!r}.",
                    path=f"{path}.runtime_units.{index}",
                )
            elif runtime_unit.mode != required_mode:
                _diagnostic(
                    diagnostics,
                    "dependencies.environment_runtime_mismatch",
                    f"{dependency_environment.value} can bind only {required_mode.value} runtime units.",
                    path=f"{path}.runtime_units.{index}",
                )
        if (
            conda_packages
            and dependency_environment == DependencyEnvironment.IN_PROCESS_VENDOR
        ):
            _diagnostic(
                diagnostics,
                "dependencies.conda_environment_invalid",
                "Conda packages require a managed-process dependency environment.",
                path=f"{path}.conda_packages",
            )

    if strict_mode and has_unlocked_dependencies and not lock_path:
        _diagnostic(
            diagnostics,
            "dependencies.lock_required",
            "Installed packages with Python dependencies must declare a lockfile.",
            path=f"{path}.lockfile",
        )

    dependency_lock: DependencyLock | None = None
    if lock_path and resolved_dir is not None:
        declared_lock_format = str(raw_dependencies.get("lock_format") or "").strip()
        inferred_lock_format = (
            "pixi"
            if dependency_environment == DependencyEnvironment.MANAGED_PROCESS_PIXI
            or Path(lock_path).name.lower() == "pixi.lock"
            else "pip_requirements"
        )
        lock_format = declared_lock_format or inferred_lock_format
        if lock_format not in DEPENDENCY_LOCK_FORMATS:
            _diagnostic(
                diagnostics,
                "dependencies.lock_format_invalid",
                "Python dependency lock_format must be one of: "
                + ", ".join(sorted(DEPENDENCY_LOCK_FORMATS))
                + ".",
                path=f"{path}.lock_format",
            )
        expected_lock_format = (
            "pixi"
            if dependency_environment == DependencyEnvironment.MANAGED_PROCESS_PIXI
            else "pip_requirements"
        )
        if (
            lock_format in DEPENDENCY_LOCK_FORMATS
            and lock_format != expected_lock_format
        ):
            _diagnostic(
                diagnostics,
                "dependencies.lock_environment_mismatch",
                f"{dependency_environment.value if dependency_environment else 'An in-process environment'} "
                f"requires a {expected_lock_format!r} dependency lock.",
                path=f"{path}.lock_format",
            )
        if schema_version == 2 and not declared_lock_format:
            _diagnostic(
                diagnostics,
                "dependencies.lock_format_required",
                "Manifest schema 2 dependency locks must declare lock_format.",
                path=f"{path}.lock_format",
            )
        lock_file = _safe_relative_file(
            resolved_dir,
            lock_path,
            diagnostics,
            field_path=f"{path}.lockfile",
        )
        if lock_file is not None and not lock_file.is_file():
            _diagnostic(
                diagnostics,
                "dependencies.lock_missing",
                "The declared dependency lock file is missing.",
                path=f"{path}.lockfile",
            )
        elif lock_file is not None and lock_format in DEPENDENCY_LOCK_FORMATS:
            require_hashes = bool(
                raw_dependencies.get(
                    "require_hashes",
                    lock_format == "pip_requirements",
                )
            )
            if strict_mode and lock_format == "pip_requirements" and not require_hashes:
                _diagnostic(
                    diagnostics,
                    "dependencies.hashes_required",
                    "Installed package dependency locks must require hashes.",
                    path=f"{path}.require_hashes",
                )
            if lock_format == "pixi" and require_hashes:
                _diagnostic(
                    diagnostics,
                    "dependencies.hashes_not_applicable",
                    "require_hashes applies only to pip requirements locks; Pixi verifies its own locked artifacts.",
                    path=f"{path}.require_hashes",
                )
            if strict_mode and lock_format == "pip_requirements":
                _validate_hashed_requirement_lock(
                    lock_file,
                    diagnostics,
                    field_path=f"{path}.lockfile",
                )
            elif strict_mode:
                _validate_pixi_lock(
                    lock_file,
                    diagnostics,
                    field_path=f"{path}.lockfile",
                )
            dependency_lock = DependencyLock(
                path=lock_path,
                sha256=hashlib.sha256(lock_file.read_bytes()).hexdigest(),
                format=lock_format,
                require_hashes=require_hashes,
            )

    manifest_path = str(raw_dependencies.get("manifest") or "").strip()
    if (
        not manifest_path
        and dependency_environment == DependencyEnvironment.MANAGED_PROCESS_PIXI
        and lock_path
    ):
        manifest_path = str(Path(lock_path).with_name("pixi.toml")).replace("\\", "/")
    if (
        not manifest_path
        and legacy_binding
        and resolved_dir is not None
        and (resolved_dir / "pixi.toml").is_file()
    ):
        manifest_path = "pixi.toml"
    manifest_sha256 = ""
    if dependency_environment == DependencyEnvironment.MANAGED_PROCESS_PIXI:
        if not manifest_path and (strict_mode or not legacy_binding):
            _diagnostic(
                diagnostics,
                "dependencies.pixi_manifest_required",
                "A managed-process Pixi environment must declare its manifest.",
                path=f"{path}.manifest",
            )
        elif manifest_path and resolved_dir is not None:
            manifest_file = _safe_relative_file(
                resolved_dir,
                manifest_path,
                diagnostics,
                field_path=f"{path}.manifest",
            )
            if manifest_file is not None and not manifest_file.is_file():
                _diagnostic(
                    diagnostics,
                    "dependencies.pixi_manifest_missing",
                    "A managed-process Pixi environment must include its declared pixi.toml.",
                    path=f"{path}.manifest",
                )
            elif manifest_file is not None:
                manifest_sha256 = hashlib.sha256(manifest_file.read_bytes()).hexdigest()

    dependency_setup: list[DependencySetupAction] = []
    raw_dependency_setup = raw_dependencies.get("setup", [])
    if raw_dependency_setup is None:
        raw_dependency_setup = []
    if not isinstance(raw_dependency_setup, list):
        _diagnostic(
            diagnostics,
            "dependencies.setup_invalid",
            f"{path}.setup must be a list of typed host actions.",
            path=f"{path}.setup",
        )
        raw_dependency_setup = []

    dependency_setup_ids: set[str] = set()
    permission_ids = {item.id for item in permissions}
    for index, raw_action in enumerate(raw_dependency_setup):
        action_path = f"{path}.setup.{index}"
        if not isinstance(raw_action, Mapping):
            _diagnostic(
                diagnostics,
                "dependencies.setup_action_invalid",
                "Dependency setup entries must be objects.",
                path=action_path,
            )
            continue
        unknown_fields = set(raw_action) - {
            "id",
            "kind",
            "provider",
            "target",
            "runtime_unit",
        }
        if unknown_fields:
            _diagnostic(
                diagnostics,
                "dependencies.setup_fields_invalid",
                "Dependency setup actions accept only id, kind, provider, target, and runtime_unit.",
                path=action_path,
            )
        action_id = str(raw_action.get("id") or "").strip()
        if not RESOURCE_ID_PATTERN.fullmatch(action_id):
            _diagnostic(
                diagnostics,
                "dependencies.setup_id_invalid",
                "Dependency setup action IDs must use the resource ID format.",
                path=f"{action_path}.id",
            )
        elif action_id in dependency_setup_ids:
            _diagnostic(
                diagnostics,
                "dependencies.setup_id_duplicate",
                f"Duplicate dependency setup action ID: {action_id!r}.",
                path=f"{action_path}.id",
            )
        else:
            dependency_setup_ids.add(action_id)

        action_kind = str(raw_action.get("kind") or "").strip()
        provider = str(raw_action.get("provider") or "").strip()
        target = str(raw_action.get("target") or "").strip()
        runtime_unit_id = str(raw_action.get("runtime_unit") or "").strip()
        if action_kind not in DEPENDENCY_SETUP_KINDS:
            _diagnostic(
                diagnostics,
                "dependencies.setup_kind_invalid",
                "Dependency setup kind must be one of: "
                + ", ".join(sorted(DEPENDENCY_SETUP_KINDS))
                + ".",
                path=f"{action_path}.kind",
            )
        if provider not in DEPENDENCY_BROWSER_PROVIDERS:
            _diagnostic(
                diagnostics,
                "dependencies.setup_provider_invalid",
                "Browser dependency provider must be one of: "
                + ", ".join(sorted(DEPENDENCY_BROWSER_PROVIDERS))
                + ".",
                path=f"{action_path}.provider",
            )
        if target not in DEPENDENCY_BROWSER_TARGETS:
            _diagnostic(
                diagnostics,
                "dependencies.setup_target_invalid",
                "Browser dependency target must be one of: "
                + ", ".join(sorted(DEPENDENCY_BROWSER_TARGETS))
                + ".",
                path=f"{action_path}.target",
            )
        runtime_unit = runtime_units_by_id.get(runtime_unit_id)
        if runtime_unit is None:
            _diagnostic(
                diagnostics,
                "dependencies.setup_runtime_unknown",
                f"Unknown dependency setup runtime unit: {runtime_unit_id!r}.",
                path=f"{action_path}.runtime_unit",
            )
        elif runtime_unit_id not in bound_runtime_units:
            _diagnostic(
                diagnostics,
                "dependencies.setup_runtime_mismatch",
                "Dependency setup must target a runtime unit owned by its Python environment.",
                path=f"{action_path}.runtime_unit",
            )
        if dependency_lock is None:
            _diagnostic(
                diagnostics,
                "dependencies.setup_lock_required",
                "Typed dependency setup requires a declared dependency lock.",
                path=action_path,
            )
        missing_permissions = {"network.outbound", "process.spawn"} - permission_ids
        if missing_permissions:
            _diagnostic(
                diagnostics,
                "dependencies.setup_permission_required",
                "Browser dependency setup requires permissions: "
                + ", ".join(sorted(missing_permissions))
                + ".",
                path=action_path,
            )
        dependency_setup.append(
            DependencySetupAction(
                id=action_id,
                kind=action_kind,
                provider=provider,
                target=target,
                runtime_unit=runtime_unit_id,
            )
        )

    if dependency_environment is None:
        return None
    return PythonDependencyEnvironment(
        id=environment_id,
        environment=dependency_environment,
        runtime_units=bound_runtime_units,
        python_version=python_version,
        lock=dependency_lock,
        setup=tuple(dependency_setup),
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        pip_packages=pip_packages,
        conda_packages=conda_packages,
    )


def _compile_python_dependency_environments(
    dependencies: Mapping[str, Any],
    *,
    schema_version: int,
    strict_mode: bool,
    resolved_dir: Path | None,
    runtime_units: tuple[RuntimeUnit, ...],
    permissions: tuple[PermissionDeclaration, ...] | list[PermissionDeclaration],
    diagnostics: list[AddonDiagnostic],
) -> tuple[PythonDependencyEnvironment, ...]:
    """Compile both current and compatibility Python dependency declarations."""

    python_declared = "python" in dependencies
    raw_python = dependencies.get("python")
    if raw_python is not None and not isinstance(raw_python, Mapping):
        _diagnostic(
            diagnostics,
            "dependencies.python_invalid",
            "dependencies.python must be an object.",
            path="dependencies.python",
        )
        return ()
    python_dependencies = raw_python or {}

    raw_environments: list[Any] | None = None
    if "environments" in python_dependencies:
        candidate_environments = python_dependencies.get("environments")
        if not isinstance(candidate_environments, list):
            _diagnostic(
                diagnostics,
                "dependencies.environments_invalid",
                "dependencies.python.environments must be a list.",
                path="dependencies.python.environments",
            )
            raw_environments = []
        else:
            raw_environments = candidate_environments
        if set(python_dependencies) - {"environments"}:
            _diagnostic(
                diagnostics,
                "dependencies.environments_mixed",
                "The environments list cannot be combined with package-level Python dependency fields.",
                path="dependencies.python",
            )

    declarations: list[tuple[str, str, Mapping[str, Any], bool]] = []
    if raw_environments is not None:
        for index, raw_environment in enumerate(raw_environments):
            path = f"dependencies.python.environments.{index}"
            if not isinstance(raw_environment, Mapping):
                _diagnostic(
                    diagnostics,
                    "dependencies.environment_invalid",
                    "Python environment declarations must be objects.",
                    path=path,
                )
                continue
            declarations.append(
                (
                    str(raw_environment.get("id") or "").strip(),
                    path,
                    raw_environment,
                    False,
                )
            )
    elif python_declared or schema_version == 1:
        declarations.append(
            ("default", "dependencies.python", python_dependencies, True)
        )

    environments: list[PythonDependencyEnvironment] = []
    used_environment_ids: set[str] = set()
    bound_runtime_units: dict[str, str] = {}
    setup_owners: dict[str, str] = {}
    for environment_id, path, declaration, legacy_binding in declarations:
        if environment_id in used_environment_ids:
            _diagnostic(
                diagnostics,
                "dependencies.environment_id_duplicate",
                f"Duplicate Python environment ID: {environment_id!r}.",
                path=f"{path}.id",
            )
        else:
            used_environment_ids.add(environment_id)

        environment = _parse_python_dependency_environment(
            declaration,
            environment_id=environment_id,
            path=path,
            schema_version=schema_version,
            strict_mode=strict_mode,
            resolved_dir=resolved_dir,
            runtime_units=runtime_units,
            permissions=permissions,
            legacy_binding=legacy_binding,
            legacy_dependencies=dependencies,
            diagnostics=diagnostics,
        )
        if environment is None:
            continue

        for runtime_unit_id in environment.runtime_units:
            previous_owner = bound_runtime_units.get(runtime_unit_id)
            if previous_owner is not None:
                _diagnostic(
                    diagnostics,
                    "dependencies.runtime_unit_duplicate",
                    f"Runtime unit {runtime_unit_id!r} is already bound to Python environment {previous_owner!r}.",
                    path=f"{path}.runtime_units",
                )
            else:
                bound_runtime_units[runtime_unit_id] = environment_id
        for setup_action in environment.setup:
            previous_owner = setup_owners.get(setup_action.id)
            if previous_owner is not None:
                _diagnostic(
                    diagnostics,
                    "dependencies.setup_id_duplicate",
                    f"Dependency setup action {setup_action.id!r} is already declared by Python environment {previous_owner!r}.",
                    path=f"{path}.setup",
                )
            else:
                setup_owners[setup_action.id] = environment_id
        environments.append(environment)

    return tuple(environments)


def validate_addon_contract(
    manifest: Mapping[str, Any],
    *,
    addon_dir: str | Path | None = None,
    source_kind: AddonSourceKind | str = AddonSourceKind.BUILT_IN,
    strict: bool | None = None,
    provenance: Mapping[str, Any] | None = None,
) -> ContractValidation:
    """Validate and resolve a manifest without importing or executing its code."""

    validation_started_at = perf_counter()
    schema_validation_ms = 0.0
    diagnostics: list[AddonDiagnostic] = []

    def finish(contract: ResolvedAddonContract | None) -> ContractValidation:
        total_ms = (perf_counter() - validation_started_at) * 1000
        metrics = MappingProxyType(
            {
                "schema_validation_ms": round(schema_validation_ms, 3),
                "contract_resolution_ms": round(
                    max(0.0, total_ms - schema_validation_ms),
                    3,
                ),
                "total_ms": round(total_ms, 3),
            }
        )
        return ContractValidation(contract, tuple(diagnostics), metrics)

    if not isinstance(manifest, Mapping):
        _diagnostic(
            diagnostics,
            "manifest.object_required",
            "The add-on manifest must be a JSON object.",
        )
        return finish(None)

    normalized_manifest = copy.deepcopy(dict(manifest))
    try:
        resolved_source = AddonSourceKind(source_kind)
    except ValueError:
        _diagnostic(
            diagnostics,
            "source.invalid",
            f"Unsupported add-on source kind: {source_kind!r}.",
        )
        return finish(None)
    strict_mode = (
        resolved_source == AddonSourceKind.INSTALLED if strict is None else bool(strict)
    )
    resolved_dir = Path(addon_dir).resolve() if addon_dir is not None else None

    schema_started_at = perf_counter()
    try:
        diagnostics.extend(_jsonschema_diagnostics(normalized_manifest))
        for message in validate_manifest_against_schema(normalized_manifest):
            _diagnostic(diagnostics, "schema.semantic", message)
    finally:
        schema_validation_ms = (perf_counter() - schema_started_at) * 1000

    addon_id = str(normalized_manifest.get("id") or "").strip()
    name = str(normalized_manifest.get("name") or "").strip()
    version = str(normalized_manifest.get("version") or "").strip()
    if addon_id and not ADDON_ID_PATTERN.fullmatch(addon_id):
        _diagnostic(
            diagnostics,
            "identity.id_invalid",
            "The ID must start with a letter or number and contain only letters, numbers, dots, underscores, or hyphens.",
            path="id",
        )
    if version:
        try:
            Version(version)
        except InvalidVersion:
            _diagnostic(
                diagnostics,
                "identity.version_invalid",
                "The version must be a valid PEP 440 version.",
                path="version",
            )

    schema_version = normalized_manifest.get("manifest_schema_version", 1)
    if (
        not isinstance(schema_version, int)
        or schema_version not in SUPPORTED_MANIFEST_SCHEMA_VERSIONS
    ):
        _diagnostic(
            diagnostics,
            "manifest.version_unsupported",
            "Supported manifest_schema_version values: 1 and 2.",
            path="manifest_schema_version",
        )
        schema_version = 1
    if strict_mode and "manifest_schema_version" not in normalized_manifest:
        _diagnostic(
            diagnostics,
            "manifest.version_required",
            "Installed packages must declare manifest_schema_version.",
            path="manifest_schema_version",
        )

    package_format_version = normalized_manifest.get("package_format_version")
    if strict_mode and package_format_version != 1:
        _diagnostic(
            diagnostics,
            "package.version_required",
            "Installed packages must declare package_format_version: 1.",
            path="package_format_version",
        )

    api_block = normalized_manifest.get("addon_api")
    api_requires = ""
    if isinstance(api_block, Mapping):
        api_requires = str(api_block.get("requires") or "").strip()
    elif api_block is not None:
        _diagnostic(
            diagnostics,
            "api.object_required",
            "addon_api must be an object.",
            path="addon_api",
        )
    if not api_requires:
        severity = "error" if strict_mode else "warning"
        _diagnostic(
            diagnostics,
            "api.range_required" if strict_mode else "compat.legacy_manifest",
            (
                "Installed packages must declare addon_api.requires as a PEP 440 range."
                if strict_mode
                else "Legacy manifest has no Add-on API range and is running through the compatibility adapter."
            ),
            path="addon_api.requires",
            severity=severity,
        )
        api_requires = ">=1,<2"
    try:
        api_specifier = SpecifierSet(api_requires)
        if Version(ADDON_API_VERSION) not in api_specifier:
            _diagnostic(
                diagnostics,
                "api.incompatible",
                f"This package requires Add-on API {api_requires}; Carlina provides {ADDON_API_VERSION}.",
                path="addon_api.requires",
            )
    except InvalidSpecifier:
        _diagnostic(
            diagnostics,
            "api.range_invalid",
            "addon_api.requires must be a valid PEP 440 specifier such as '>=1,<2'.",
            path="addon_api.requires",
        )

    raw_roles = tuple(
        str(item)
        for item in normalized_manifest.get("roles") or ()
        if str(item).strip()
    )
    if schema_version == 2:
        raw_kind = str(normalized_manifest.get("kind") or "").strip()
        try:
            kind = AddonKind(raw_kind)
        except ValueError:
            _diagnostic(
                diagnostics,
                "kind.invalid",
                "Add-on kind must be one of: "
                + ", ".join(item.value for item in AddonKind)
                + ".",
                path="kind",
            )
            kind = AddonKind.TOOLKIT
        roles = _compatibility_roles(kind)
    else:
        roles = raw_roles
        kind = _legacy_kind(roles)

    if schema_version == 2:
        raw_distribution = normalized_manifest.get("distribution")
        if not isinstance(raw_distribution, Mapping):
            _diagnostic(
                diagnostics,
                "distribution.required",
                "Manifest schema 2 requires a distribution object.",
                path="distribution",
            )
            distribution = _default_distribution(resolved_source)
        else:
            raw_channel = str(raw_distribution.get("channel") or "").strip()
            try:
                channel = DistributionChannel(raw_channel)
            except ValueError:
                _diagnostic(
                    diagnostics,
                    "distribution.channel_invalid",
                    "Distribution channel must be one of: "
                    + ", ".join(item.value for item in DistributionChannel)
                    + ".",
                    path="distribution.channel",
                )
                channel = _default_distribution(resolved_source).channel
            distribution = DistributionDeclaration(
                channel=channel,
                publisher=str(raw_distribution.get("publisher") or "").strip(),
                source_uri=str(raw_distribution.get("source_uri") or "").strip(),
            )
    else:
        distribution = _default_distribution(resolved_source)

    if schema_version == 2:
        runtime_units = _parse_v2_runtime_units(
            normalized_manifest,
            diagnostics,
            resolved_dir=resolved_dir,
            strict_mode=strict_mode,
        )
        execution_mode, entry_point_module, entry_point_class = (
            _legacy_execution_from_runtime(runtime_units)
        )
    else:
        execution = normalized_manifest.get("execution")
        if not isinstance(execution, Mapping):
            if strict_mode:
                _diagnostic(
                    diagnostics,
                    "execution.required",
                    "Installed packages must declare execution mode and entry point.",
                    path="execution",
                )
            execution = {}
        execution_mode = str(execution.get("mode") or "native").strip()
        if execution_mode not in EXECUTION_MODES:
            _diagnostic(
                diagnostics,
                "execution.mode_invalid",
                f"Execution mode must be one of: {', '.join(sorted(EXECUTION_MODES))}.",
                path="execution.mode",
            )
            execution_mode = "native"
        if strict_mode and execution_mode != "native":
            _diagnostic(
                diagnostics,
                "execution.mode_not_installable",
                (
                    "This Carlina release installs native trusted-code packages only. "
                    "Isolated-service and remote-integration host adapters are reserved "
                    "but not yet available to third-party packages."
                ),
                path="execution.mode",
            )
        entry_point_module = str(execution.get("module") or ".").strip()
        entry_point_class = str(
            execution.get("class") or normalized_manifest.get("entry_point_class") or ""
        ).strip()
        if execution_mode == "native":
            if not ENTRY_POINT_MODULE_PATTERN.fullmatch(entry_point_module):
                _diagnostic(
                    diagnostics,
                    "execution.module_invalid",
                    "Native entry-point module must be '.' or a dotted Python identifier.",
                    path="execution.module",
                )
            if not entry_point_class.isidentifier():
                _diagnostic(
                    diagnostics,
                    "execution.class_invalid",
                    "Native add-ons must declare a Python entry-point class.",
                    path="execution.class",
                )
        runtime_units = _legacy_runtime_units(
            normalized_manifest,
            execution_mode=execution_mode,
            entry_point_module=entry_point_module,
            entry_point_class=entry_point_class,
        )

    categories = tuple(
        str(item)
        for item in normalized_manifest.get("categories") or ()
        if str(item).strip()
    )
    if strict_mode and schema_version == 1 and not roles:
        _diagnostic(
            diagnostics,
            "roles.required",
            "Installed packages must declare at least one role.",
            path="roles",
        )
    if strict_mode and schema_version == 1 and not categories:
        _diagnostic(
            diagnostics,
            "categories.required",
            "Installed packages must declare at least one category.",
            path="categories",
        )

    if schema_version == 2:
        ui_entries = _parse_v2_ui_entries(normalized_manifest, diagnostics)
        connector_definitions = _parse_v2_connector_definitions(
            normalized_manifest,
            diagnostics,
        )
        capability_requirements, capability_providers = _parse_v2_capabilities(
            normalized_manifest,
            diagnostics,
            runtime_units=runtime_units,
        )
        connector_ids = {item.id for item in connector_definitions}
        for provider_index, provider in enumerate(capability_providers):
            if (
                provider.connector_definition
                and provider.connector_definition not in connector_ids
            ):
                _diagnostic(
                    diagnostics,
                    "capability.connector_definition_unknown",
                    "Capability providers must reference a connector definition "
                    "declared by the same package.",
                    path=(
                        f"provides.capabilities.{provider_index}.connector_definition"
                    ),
                )
    else:
        ui_entries = _legacy_ui_entries(normalized_manifest)
        connector_definitions = ()
        capability_requirements = ()
        capability_providers = ()

    permissions: list[PermissionDeclaration] = []
    raw_permissions = normalized_manifest.get("permissions")
    if strict_mode and not isinstance(raw_permissions, list):
        _diagnostic(
            diagnostics,
            "permissions.required",
            "Installed packages must declare permissions as a list, including an empty list.",
            path="permissions",
        )
    for index, raw_permission in enumerate(raw_permissions or ()):
        if isinstance(raw_permission, str):
            permission_id = raw_permission.strip()
            reason = ""
        elif isinstance(raw_permission, Mapping):
            permission_id = str(raw_permission.get("id") or "").strip()
            reason = str(raw_permission.get("reason") or "").strip()
        else:
            _diagnostic(
                diagnostics,
                "permissions.item_invalid",
                "Permission entries must be strings or objects.",
                path=f"permissions.{index}",
            )
            continue
        if permission_id not in KNOWN_PERMISSIONS:
            _diagnostic(
                diagnostics,
                "permissions.id_unknown",
                f"Unknown permission {permission_id!r}.",
                path=f"permissions.{index}",
            )
            continue
        if permission_id not in {item.id for item in permissions}:
            permissions.append(PermissionDeclaration(permission_id, reason))

    resources: list[ResourceDeclaration] = []
    raw_resources = normalized_manifest.get("resources")
    if strict_mode and not isinstance(raw_resources, list):
        _diagnostic(
            diagnostics,
            "resources.required",
            "Installed packages must declare resources as a list, including an empty list.",
            path="resources",
        )
    for index, raw_resource in enumerate(raw_resources or ()):
        if isinstance(raw_resource, str):
            resource_id = raw_resource.strip()
            access: tuple[str, ...] = ()
            description = ""
        elif isinstance(raw_resource, Mapping):
            resource_id = str(raw_resource.get("id") or "").strip()
            access = tuple(
                str(item).strip()
                for item in raw_resource.get("access") or ()
                if str(item).strip()
            )
            description = str(raw_resource.get("description") or "").strip()
        else:
            _diagnostic(
                diagnostics,
                "resources.item_invalid",
                "Resource entries must be strings or objects.",
                path=f"resources.{index}",
            )
            continue
        if not RESOURCE_ID_PATTERN.fullmatch(resource_id):
            _diagnostic(
                diagnostics,
                "resources.id_invalid",
                "Resource IDs must be stable lowercase identifiers.",
                path=f"resources.{index}.id",
            )
            continue
        if resource_id not in {item.id for item in resources}:
            resources.append(ResourceDeclaration(resource_id, access, description))

    dependencies = normalized_manifest.get("dependencies")
    dependencies = dependencies if isinstance(dependencies, Mapping) else {}
    if schema_version == 2:
        requirement_block = normalized_manifest.get("requires")
        requirement_block = (
            requirement_block if isinstance(requirement_block, Mapping) else {}
        )
        raw_addon_dependencies = requirement_block.get("packages") or ()
        dependency_path = "requires.packages"
    else:
        raw_addon_dependencies = (
            dependencies.get("addons") or dependencies.get("plugins") or ()
        )
        dependency_path = "dependencies.addons"
    if isinstance(raw_addon_dependencies, str):
        raw_addon_dependencies = [raw_addon_dependencies]
    addon_dependencies: list[str] = []
    for index, raw_dependency in enumerate(raw_addon_dependencies):
        dependency_id = str(raw_dependency).strip()
        if not ADDON_ID_PATTERN.fullmatch(dependency_id):
            _diagnostic(
                diagnostics,
                "dependencies.id_invalid",
                "Add-on dependency IDs must use the add-on ID format.",
                path=f"{dependency_path}.{index}",
            )
            continue
        if dependency_id == addon_id:
            _diagnostic(
                diagnostics,
                "dependencies.self",
                "An add-on cannot depend on itself.",
                path=f"{dependency_path}.{index}",
            )
        elif dependency_id not in addon_dependencies:
            addon_dependencies.append(dependency_id)

    python_environments = list(
        _compile_python_dependency_environments(
            dependencies,
            schema_version=int(schema_version),
            strict_mode=strict_mode,
            resolved_dir=resolved_dir,
            runtime_units=runtime_units,
            permissions=permissions,
            diagnostics=diagnostics,
        )
    )
    dependency_setup = [
        action for environment in python_environments for action in environment.setup
    ]
    dependency_environment: DependencyEnvironment | None = None
    python_version: str | None = None
    dependency_lock: DependencyLock | None = None

    if len(python_environments) == 1:
        compatibility_environment = python_environments[0]
        dependency_environment = compatibility_environment.environment
        python_version = compatibility_environment.python_version or None
        dependency_lock = compatibility_environment.lock
    elif len(python_environments) > 1:
        dependency_environment = None
        python_version = None
        dependency_lock = None

    in_process_environments = [
        item
        for item in python_environments
        if item.environment == DependencyEnvironment.IN_PROCESS_VENDOR
    ]
    if len(in_process_environments) > 1:
        _diagnostic(
            diagnostics,
            "dependencies.in_process_environment_duplicate",
            "A package can declare only one in-process vendor environment because all host runtime units share one Python import namespace.",
            path="dependencies.python.environments",
        )

    python_environment_by_runtime = {
        runtime_unit_id: environment
        for environment in python_environments
        for runtime_unit_id in environment.runtime_units
    }
    for runtime_unit in runtime_units:
        configured_environment = str(
            runtime_unit.config.get("environment") or ""
        ).strip()
        if (
            runtime_unit.mode == RuntimeMode.MANAGED_PROCESS
            and configured_environment
            in {"addon_pixi", DependencyEnvironment.MANAGED_PROCESS_PIXI.value}
            and runtime_unit.id not in python_environment_by_runtime
        ):
            _diagnostic(
                diagnostics,
                "dependencies.runtime_environment_missing",
                f"Managed runtime unit {runtime_unit.id!r} selects an add-on Pixi environment but has no Python environment binding.",
                path=f"runtime.units.{runtime_unit.id}.config.environment",
            )

    if strict_mode and normalized_manifest.get("post_install_commands"):
        _diagnostic(
            diagnostics,
            "install.shell_commands_forbidden",
            "Add-on API v1 packages cannot declare arbitrary post-install shell commands.",
            path="post_install_commands",
        )

    storage: dict[str, str] = {}
    raw_storage = normalized_manifest.get("storage")
    if strict_mode and not isinstance(raw_storage, Mapping):
        _diagnostic(
            diagnostics,
            "storage.required",
            "Installed packages must declare storage as an object, including an empty object.",
            path="storage",
        )
    for key, value in raw_storage.items() if isinstance(raw_storage, Mapping) else ():
        storage_class = str(value).strip()
        if storage_class not in STORAGE_CLASSES:
            _diagnostic(
                diagnostics,
                "storage.class_invalid",
                f"Storage class must be one of: {', '.join(sorted(STORAGE_CLASSES))}.",
                path=f"storage.{key}",
            )
            continue
        storage[str(key)] = storage_class

    migration_bundle = None
    if resolved_dir is not None:
        defines_models = addon_defines_database_models(resolved_dir)
        migrations_declared = (resolved_dir / "migrations").exists()
        if defines_models and not migrations_declared:
            _diagnostic(
                diagnostics,
                "migrations.required",
                "Packages with database models must ship an explicit migrations "
                "directory.",
                path="migrations",
            )
        elif migrations_declared and not addon_has_migrations(
            resolved_dir,
            addon_id,
        ):
            issues = "; ".join(addon_migration_contract_issues(resolved_dir, addon_id))
            _diagnostic(
                diagnostics,
                "migrations.invalid",
                f"The declared Alembic migration contract is invalid: {issues}.",
                path="migrations",
            )
        elif migrations_declared:
            migration_bundle = addon_migration_contract(resolved_dir, addon_id)

    if any(item.severity == "error" for item in diagnostics):
        return finish(None)

    manifest_sha256 = hashlib.sha256(_canonical_json(normalized_manifest)).hexdigest()
    trust_tier = _source_trust(
        resolved_source,
        execution_mode,
        has_host_migrations=migration_bundle is not None,
    )
    database_migration = (
        DatabaseMigrationDeclaration(
            format="alembic_python_v1",
            version_table=migration_bundle.version_table,
            purge_tables=migration_bundle.purge_tables,
            owned_tables=migration_bundle.owned_tables,
            base_revision=migration_bundle.base_revision,
            head_revision=migration_bundle.head_revision,
            revision_ids=migration_bundle.revision_ids,
            revision_count=len(migration_bundle.revisions),
            graph_sha256=migration_bundle.graph_sha256,
        )
        if migration_bundle is not None
        else None
    )
    contract_identity = {
        "addon_id": addon_id,
        "version": version,
        "manifest_schema_version": schema_version,
        "api_requires": api_requires,
        "execution_mode": execution_mode,
        "entry_point_module": entry_point_module,
        "entry_point_class": entry_point_class,
        "permissions": [asdict(item) for item in permissions],
        "resources": [asdict(item) for item in resources],
        "addon_dependencies": addon_dependencies,
        "python_environments": [
            {
                "id": item.id,
                "environment": item.environment.value,
                "runtime_units": list(item.runtime_units),
                "python_version": item.python_version,
                "lock": asdict(item.lock) if item.lock else None,
                "setup": [asdict(action) for action in item.setup],
                "manifest_path": item.manifest_path,
                "manifest_sha256": item.manifest_sha256,
                "pip_packages": list(item.pip_packages),
                "conda_packages": list(item.conda_packages),
            }
            for item in python_environments
        ],
        "dependency_lock": asdict(dependency_lock) if dependency_lock else None,
        "dependency_setup": [asdict(item) for item in dependency_setup],
        "storage": storage,
        "database_migration": (
            asdict(database_migration) if database_migration else None
        ),
        "source_kind": resolved_source.value,
        "trust_tier": trust_tier.value,
        "manifest_sha256": manifest_sha256,
    }
    if schema_version == 2:
        contract_identity.update(
            {
                "kind": kind.value,
                "dependency_environment": (
                    dependency_environment.value
                    if dependency_environment is not None
                    else None
                ),
                "python_version": python_version,
                "distribution": {
                    "channel": distribution.channel.value,
                    "publisher": distribution.publisher,
                    "source_uri": distribution.source_uri,
                },
                "runtime_units": [
                    {
                        "id": item.id,
                        "mode": item.mode.value,
                        "entry_point_module": item.entry_point_module,
                        "entry_point_class": item.entry_point_class,
                        "lifecycle": item.lifecycle,
                        "dependency_artifacts": [
                            asdict(artifact) for artifact in item.dependency_artifacts
                        ],
                        "requirements": _deep_thaw(item.requirements),
                        "config": _deep_thaw(item.config),
                    }
                    for item in runtime_units
                ],
                "capability_requirements": [
                    {
                        "id": item.id,
                        "capability_id": item.capability_id,
                        "version_spec": item.version_spec,
                        "operations": list(item.operations),
                        "cardinality": item.cardinality,
                        "activation": item.activation,
                        "scope": item.scope,
                        "qualifiers": _deep_thaw(item.qualifiers),
                    }
                    for item in capability_requirements
                ],
                "capability_providers": [
                    {
                        "id": item.id,
                        "capability_id": item.capability_id,
                        "version": item.version,
                        "operations": list(item.operations),
                        "runtime_unit": item.runtime_unit,
                        "connector_definition": item.connector_definition,
                        "setup": _deep_thaw(item.setup),
                        "qualifiers": _deep_thaw(item.qualifiers),
                    }
                    for item in capability_providers
                ],
                "connector_definitions": [
                    {
                        "id": item.id,
                        "mode": item.mode,
                        "label": item.label,
                        "fields": [asdict(field) for field in item.fields],
                        "setup_url": item.setup_url,
                        "callback_path": item.callback_path,
                        "disconnect_url": item.disconnect_url,
                        "test_method": item.test_method,
                    }
                    for item in connector_definitions
                ],
                "ui_entries": [
                    {
                        "id": item.id,
                        "mode": item.mode.value,
                        "module": item.module,
                        "route": item.route,
                        "css_assets": list(item.css_assets),
                    }
                    for item in ui_entries
                ],
            }
        )
    contract_sha256 = hashlib.sha256(_canonical_json(contract_identity)).hexdigest()
    contract = ResolvedAddonContract(
        addon_id=addon_id,
        name=name,
        version=version,
        manifest_schema_version=int(schema_version),
        api_requires=api_requires,
        execution_mode=execution_mode,
        entry_point_module=entry_point_module,
        entry_point_class=entry_point_class,
        roles=roles,
        categories=categories,
        kind=kind,
        distribution=distribution,
        runtime_units=runtime_units,
        capability_requirements=capability_requirements,
        capability_providers=capability_providers,
        connector_definitions=connector_definitions,
        ui_entries=ui_entries,
        permissions=tuple(permissions),
        resources=tuple(resources),
        addon_dependencies=tuple(addon_dependencies),
        python_environments=tuple(python_environments),
        dependency_environment=dependency_environment,
        python_version=python_version,
        dependency_lock=dependency_lock,
        dependency_setup=tuple(dependency_setup),
        storage=MappingProxyType(dict(storage)),
        database_migration=database_migration,
        source_kind=resolved_source,
        trust_tier=trust_tier,
        manifest_sha256=manifest_sha256,
        contract_sha256=contract_sha256,
        manifest=_deep_freeze(normalized_manifest),
        provenance=_deep_freeze(dict(provenance or {})),
        diagnostics=tuple(diagnostics),
        migration_bundle=migration_bundle,
    )
    return finish(contract)


def compile_addon_contract(
    manifest: Mapping[str, Any],
    **kwargs: Any,
) -> ResolvedAddonContract:
    result = validate_addon_contract(manifest, **kwargs)
    if not result.valid or result.contract is None:
        raise AddonContractError(result.diagnostics)
    return result.contract
