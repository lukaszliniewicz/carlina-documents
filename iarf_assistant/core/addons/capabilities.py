"""Pure capability catalog and resolver for Add-on Manifest schema 2."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from packaging.specifiers import SpecifierSet
from packaging.version import Version

from .contracts import (
    CapabilityProvider,
    CapabilityRequirement,
    DistributionChannel,
    ResolvedAddonContract,
)


class CapabilityResolutionStatus(str, Enum):
    RESOLVED = "resolved"
    MISSING = "missing"
    OPTIONAL_MISSING = "optional_missing"
    AMBIGUOUS = "ambiguous"
    SETUP_REQUIRED = "setup_required"
    BROKEN_BINDING = "broken_binding"


@dataclass(frozen=True, slots=True)
class CapabilityCandidate:
    addon_id: str
    addon_version: str
    distribution_channel: DistributionChannel
    provider: CapabilityProvider

    def to_dict(self) -> dict[str, Any]:
        return {
            "addon_id": self.addon_id,
            "addon_version": self.addon_version,
            "distribution_channel": self.distribution_channel.value,
            "provider_id": self.provider.id,
            "capability": self.provider.capability_id,
            "version": self.provider.version,
            "operations": list(self.provider.operations),
            "runtime_unit": self.provider.runtime_unit,
            "connector_definition": self.provider.connector_definition,
            "setup": dict(self.provider.setup),
            "qualifiers": dict(self.provider.qualifiers),
        }


@dataclass(frozen=True, slots=True)
class CapabilityMatch:
    candidate: CapabilityCandidate
    compatible: bool
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate.to_dict(),
            "compatible": self.compatible,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class BindingSelection:
    provider_addon_id: str
    provider_capability_id: str
    connection_id: str | None = None
    status: str = "enabled"
    priority: int = 0


@dataclass(frozen=True, slots=True)
class CapabilityResolution:
    requirement: CapabilityRequirement
    status: CapabilityResolutionStatus
    selected: tuple[CapabilityCandidate, ...]
    compatible: tuple[CapabilityCandidate, ...]
    matches: tuple[CapabilityMatch, ...]
    connection_ids: tuple[str, ...] = ()
    message: str = ""
    selected_bindings: tuple[BindingSelection, ...] = ()

    @property
    def resolved(self) -> bool:
        return self.status == CapabilityResolutionStatus.RESOLVED

    @property
    def blocks_activation(self) -> bool:
        if self.requirement.cardinality == "optional":
            return False
        if self.requirement.activation == "on_first_use":
            return False
        return not self.resolved

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement": {
                "id": self.requirement.id,
                "capability": self.requirement.capability_id,
                "version": self.requirement.version_spec,
                "operations": list(self.requirement.operations),
                "cardinality": self.requirement.cardinality,
                "activation": self.requirement.activation,
                "scope": self.requirement.scope,
                "qualifiers": dict(self.requirement.qualifiers),
            },
            "status": self.status.value,
            "selected": [item.to_dict() for item in self.selected],
            "compatible": [item.to_dict() for item in self.compatible],
            "matches": [item.to_dict() for item in self.matches],
            "connection_ids": list(self.connection_ids),
            "selected_bindings": [
                {
                    "provider_addon_id": item.provider_addon_id,
                    "provider_capability_id": item.provider_capability_id,
                    "connection_id": item.connection_id,
                    "status": item.status,
                    "priority": item.priority,
                }
                for item in self.selected_bindings
            ],
            "message": self.message,
            "blocks_activation": self.blocks_activation,
        }


@dataclass(frozen=True, slots=True)
class CapabilitySetupRequest:
    consumer_addon_id: str
    requirement_id: str
    capability_id: str
    activation: str
    scope: str
    status: CapabilityResolutionStatus
    provider_choices: tuple[CapabilityCandidate, ...]
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "consumer_addon_id": self.consumer_addon_id,
            "requirement_id": self.requirement_id,
            "capability_id": self.capability_id,
            "activation": self.activation,
            "scope": self.scope,
            "status": self.status.value,
            "provider_choices": [item.to_dict() for item in self.provider_choices],
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class CapabilityResolutionReport:
    consumer_addon_id: str
    resolutions: tuple[CapabilityResolution, ...]

    @property
    def activatable(self) -> bool:
        return not any(item.blocks_activation for item in self.resolutions)

    @property
    def setup_requests(self) -> tuple[CapabilitySetupRequest, ...]:
        return tuple(
            CapabilitySetupRequest(
                consumer_addon_id=self.consumer_addon_id,
                requirement_id=item.requirement.id,
                capability_id=item.requirement.capability_id,
                activation=item.requirement.activation,
                scope=item.requirement.scope,
                status=item.status,
                provider_choices=item.compatible,
                message=item.message,
            )
            for item in self.resolutions
            if not item.resolved
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "consumer_addon_id": self.consumer_addon_id,
            "activatable": self.activatable,
            "resolutions": [item.to_dict() for item in self.resolutions],
            "setup_requests": [item.to_dict() for item in self.setup_requests],
        }


_CHANNEL_ORDER = {
    DistributionChannel.BUILT_IN: 0,
    DistributionChannel.FIRST_PARTY: 1,
    DistributionChannel.PRIVATE: 2,
    DistributionChannel.COMMUNITY: 3,
    DistributionChannel.LOCAL_DEVELOPMENT: 4,
}


def _candidate_key(candidate: CapabilityCandidate) -> tuple[Any, ...]:
    return (
        _CHANNEL_ORDER[candidate.distribution_channel],
        candidate.addon_id,
        candidate.provider.id,
        Version(candidate.provider.version),
    )


def _qualifier_matches(expected: Any, actual: Any) -> bool:
    if expected == "any":
        return True
    if isinstance(expected, Mapping):
        return isinstance(actual, Mapping) and all(
            key in actual and _qualifier_matches(value, actual[key])
            for key, value in expected.items()
        )
    if isinstance(expected, (list, tuple, set, frozenset)):
        if isinstance(actual, (list, tuple, set, frozenset)):
            return bool(set(expected).intersection(actual))
        return actual in expected
    if isinstance(actual, (list, tuple, set, frozenset)):
        return expected in actual
    return expected == actual


def match_capability(
    requirement: CapabilityRequirement,
    candidate: CapabilityCandidate,
) -> CapabilityMatch:
    reasons: list[str] = []
    provider = candidate.provider
    if provider.capability_id != requirement.capability_id:
        reasons.append("capability_id")
    elif Version(provider.version) not in SpecifierSet(requirement.version_spec):
        reasons.append("version")
    missing_operations = sorted(set(requirement.operations) - set(provider.operations))
    if missing_operations:
        reasons.append("operations:" + ",".join(missing_operations))
    for key, expected in requirement.qualifiers.items():
        if key not in provider.qualifiers or not _qualifier_matches(
            expected,
            provider.qualifiers[key],
        ):
            reasons.append(f"qualifier:{key}")
    return CapabilityMatch(
        candidate=candidate,
        compatible=not reasons,
        reasons=tuple(reasons),
    )


class CapabilityCatalog:
    def __init__(self, contracts: Iterable[ResolvedAddonContract] = ()):
        candidates: list[CapabilityCandidate] = []
        for contract in contracts:
            candidates.extend(
                CapabilityCandidate(
                    addon_id=contract.addon_id,
                    addon_version=contract.version,
                    distribution_channel=contract.distribution.channel,
                    provider=provider,
                )
                for provider in contract.capability_providers
            )
        self._candidates = tuple(sorted(candidates, key=_candidate_key))

    @property
    def candidates(self) -> tuple[CapabilityCandidate, ...]:
        return self._candidates

    def matches(
        self,
        requirement: CapabilityRequirement,
    ) -> tuple[CapabilityMatch, ...]:
        return tuple(
            match_capability(requirement, candidate)
            for candidate in self._candidates
            if candidate.provider.capability_id == requirement.capability_id
        )


class CapabilityResolver:
    def __init__(self, catalog: CapabilityCatalog):
        self.catalog = catalog

    @staticmethod
    def _binding_candidate(
        binding: BindingSelection,
        compatible: Sequence[CapabilityCandidate],
    ) -> CapabilityCandidate | None:
        return next(
            (
                candidate
                for candidate in compatible
                if candidate.addon_id == binding.provider_addon_id
                and candidate.provider.id == binding.provider_capability_id
            ),
            None,
        )

    def resolve_requirement(
        self,
        requirement: CapabilityRequirement,
        *,
        bindings: Iterable[BindingSelection] = (),
    ) -> CapabilityResolution:
        matches = self.catalog.matches(requirement)
        compatible = tuple(
            sorted(
                (item.candidate for item in matches if item.compatible),
                key=_candidate_key,
            )
        )
        configured_bindings = tuple(
            sorted(
                (item for item in bindings if item.status != "disabled"),
                key=lambda item: (
                    item.priority,
                    item.provider_addon_id,
                    item.provider_capability_id,
                    item.connection_id or "",
                ),
            )
        )
        if configured_bindings:
            if any(item.status != "enabled" for item in configured_bindings):
                return CapabilityResolution(
                    requirement=requirement,
                    status=CapabilityResolutionStatus.BROKEN_BINDING,
                    selected=(),
                    compatible=compatible,
                    matches=matches,
                    message="A saved capability binding is marked broken.",
                )
            selected_pairs: list[tuple[CapabilityCandidate, str | None]] = []
            selected_bindings: list[BindingSelection] = []
            for binding in configured_bindings:
                candidate = self._binding_candidate(binding, compatible)
                if candidate is None:
                    return CapabilityResolution(
                        requirement=requirement,
                        status=CapabilityResolutionStatus.BROKEN_BINDING,
                        selected=(),
                        compatible=compatible,
                        matches=matches,
                        message=(
                            "An enabled binding references a missing or "
                            "incompatible provider."
                        ),
                    )
                if (
                    candidate.provider.connector_definition
                    and not binding.connection_id
                ):
                    return CapabilityResolution(
                        requirement=requirement,
                        status=CapabilityResolutionStatus.BROKEN_BINDING,
                        selected=(),
                        compatible=compatible,
                        matches=matches,
                        message=(
                            "The bound connector provider has no configured "
                            "Connection."
                        ),
                    )
                selected_pairs.append((candidate, binding.connection_id))
                selected_bindings.append(binding)
                if requirement.cardinality != "many":
                    break
            return CapabilityResolution(
                requirement=requirement,
                status=CapabilityResolutionStatus.RESOLVED,
                selected=tuple(item[0] for item in selected_pairs),
                compatible=compatible,
                matches=matches,
                connection_ids=tuple(
                    item[1] for item in selected_pairs if item[1] is not None
                ),
                message="Resolved from an explicit capability binding.",
                selected_bindings=tuple(selected_bindings),
            )

        if not compatible:
            optional = requirement.cardinality == "optional"
            return CapabilityResolution(
                requirement=requirement,
                status=(
                    CapabilityResolutionStatus.OPTIONAL_MISSING
                    if optional
                    else CapabilityResolutionStatus.MISSING
                ),
                selected=(),
                compatible=(),
                matches=matches,
                message=(
                    "No compatible provider is installed; this optional "
                    "feature remains dormant."
                    if optional
                    else "No compatible capability provider is installed."
                ),
            )
        if len(compatible) > 1 or requirement.cardinality == "many":
            return CapabilityResolution(
                requirement=requirement,
                status=CapabilityResolutionStatus.AMBIGUOUS,
                selected=(),
                compatible=compatible,
                matches=matches,
                message="Choose one or more compatible providers explicitly.",
            )
        candidate = compatible[0]
        if candidate.provider.connector_definition:
            return CapabilityResolution(
                requirement=requirement,
                status=CapabilityResolutionStatus.SETUP_REQUIRED,
                selected=(),
                compatible=compatible,
                matches=matches,
                message="Configure a Connection before using this provider.",
            )
        return CapabilityResolution(
            requirement=requirement,
            status=CapabilityResolutionStatus.RESOLVED,
            selected=(candidate,),
            compatible=compatible,
            matches=matches,
            message="Resolved to the only compatible installed provider.",
        )

    def resolve_contract(
        self,
        contract: ResolvedAddonContract,
        *,
        bindings: Mapping[str, Iterable[BindingSelection]] | None = None,
    ) -> CapabilityResolutionReport:
        binding_map = bindings or {}
        return CapabilityResolutionReport(
            consumer_addon_id=contract.addon_id,
            resolutions=tuple(
                self.resolve_requirement(
                    requirement,
                    bindings=binding_map.get(requirement.id, ()),
                )
                for requirement in contract.capability_requirements
            ),
        )


__all__ = [
    "BindingSelection",
    "CapabilityCandidate",
    "CapabilityCatalog",
    "CapabilityMatch",
    "CapabilityResolution",
    "CapabilityResolutionReport",
    "CapabilityResolutionStatus",
    "CapabilityResolver",
    "CapabilitySetupRequest",
    "match_capability",
]
