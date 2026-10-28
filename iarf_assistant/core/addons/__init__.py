"""Carlina Add-on API v1 contracts and lifecycle services.

The legacy :mod:`iarf_assistant.core.addon_manager` remains the compatibility
facade.  New code belongs in this package so discovery, validation,
installation, lifecycle, and execution can evolve independently.
"""

from .contracts import (
    ADDON_API_VERSION,
    AddonContractError,
    AddonDiagnostic,
    AddonKind,
    AddonSourceKind,
    AddonTrustTier,
    CapabilityProvider,
    CapabilityRequirement,
    ContractValidation,
    DependencyEnvironment,
    DependencySetupAction,
    DistributionChannel,
    DistributionDeclaration,
    EffectiveCodeAccess,
    PythonDependencyEnvironment,
    ResolvedAddonContract,
    RuntimeDependencyArtifact,
    RuntimeMode,
    RuntimeUnit,
    UiEntry,
    UiMode,
    classify_effective_code_access,
    compile_addon_contract,
    validate_addon_contract,
)
from .settings import AddonConnectionSettingsUpdate

_CATALOG_EXPORTS = frozenset(
    {
        "AddonCatalogError",
        "AddonCatalogIndex",
        "AddonCatalogSignatureError",
        "AddonContractDiff",
        "compare_addon_contracts",
        "parse_addon_catalog",
        "verify_addon_catalog",
    }
)


def __getattr__(name: str):
    """Load signed-catalog support only when one of its exports is used."""

    if name not in _CATALOG_EXPORTS:
        raise AttributeError(name)
    from . import catalog

    value = getattr(catalog, name)
    globals()[name] = value
    return value


__all__ = [
    "ADDON_API_VERSION",
    "AddonCatalogError",
    "AddonCatalogIndex",
    "AddonCatalogSignatureError",
    "AddonConnectionSettingsUpdate",
    "AddonContractDiff",
    "AddonContractError",
    "AddonDiagnostic",
    "AddonKind",
    "AddonSourceKind",
    "AddonTrustTier",
    "CapabilityProvider",
    "CapabilityRequirement",
    "ContractValidation",
    "DependencyEnvironment",
    "DependencySetupAction",
    "DistributionChannel",
    "DistributionDeclaration",
    "EffectiveCodeAccess",
    "PythonDependencyEnvironment",
    "ResolvedAddonContract",
    "RuntimeDependencyArtifact",
    "RuntimeMode",
    "RuntimeUnit",
    "UiEntry",
    "UiMode",
    "classify_effective_code_access",
    "compare_addon_contracts",
    "compile_addon_contract",
    "parse_addon_catalog",
    "validate_addon_contract",
    "verify_addon_catalog",
]
