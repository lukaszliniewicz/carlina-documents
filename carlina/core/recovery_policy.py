"""Pure impact classification and operator recovery-coverage planning."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from enum import StrEnum

RECOVERY_PLAN_FORMAT = "carlina.recovery-plan"
RECOVERY_PLAN_VERSION = 1
_POLICY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$")
_MAX_REASON_LENGTH = 500
_MAX_ANCHOR_ID_LENGTH = 200


class RecoveryPolicyError(ValueError):
    """A requested recovery choice is incomplete or internally inconsistent."""


class DataImpact(StrEnum):
    """Objective mutable-state effect claimed for one transaction."""

    NONE = "none"
    REVERSIBLE_METADATA = "reversible_metadata"
    MIGRATION = "migration"
    OPAQUE = "opaque"


class RecoveryCoverage(StrEnum):
    """Mutable-data recovery material selected by operator policy."""

    AUTOMATIC = "automatic"
    FULL = "full"
    DATABASE_ONLY = "database_only"
    NONE = "none"


class RecoveryDomain(StrEnum):
    """Effect domains a recovery mechanism may or may not cover."""

    DATABASE = "database"
    FILESYSTEM = "filesystem"
    EXTERNAL_EFFECTS = "external_effects"


class RecoveryMechanism(StrEnum):
    """Concrete rollback material selected by a recovery plan."""

    CODE_POINTER = "code_pointer"
    METADATA_JOURNAL = "metadata_journal"
    DATABASE_CHECKPOINT = "database_checkpoint"
    COMPLETE_RECOVERY = "complete_recovery"
    NONE = "none"


class ImpactDeclarationStatus(StrEnum):
    """Why the effective impact does or does not use the declaration."""

    AUTHENTICATED = "authenticated"
    MISSING = "missing"
    INVALID = "invalid"
    UNAUTHENTICATED = "unauthenticated"
    INCONSISTENT = "inconsistent"


class RecoveryDecisionSource(StrEnum):
    """How a recovery selection entered the transaction."""

    AUTOMATIC = "automatic"
    INTERACTIVE = "interactive"
    UNATTENDED = "unattended"


_DEFAULT_COVERAGE = {
    DataImpact.NONE: RecoveryCoverage.NONE,
    DataImpact.REVERSIBLE_METADATA: RecoveryCoverage.NONE,
    DataImpact.MIGRATION: RecoveryCoverage.DATABASE_ONLY,
    DataImpact.OPAQUE: RecoveryCoverage.FULL,
}
_COVERAGE_RANK = {
    RecoveryCoverage.NONE: 0,
    RecoveryCoverage.DATABASE_ONLY: 1,
    RecoveryCoverage.FULL: 2,
}
_POSSIBLE_DOMAINS = {
    DataImpact.NONE: frozenset(),
    DataImpact.REVERSIBLE_METADATA: frozenset({RecoveryDomain.DATABASE}),
    DataImpact.MIGRATION: frozenset({RecoveryDomain.DATABASE}),
    DataImpact.OPAQUE: frozenset(
        {
            RecoveryDomain.DATABASE,
            RecoveryDomain.FILESYSTEM,
            RecoveryDomain.EXTERNAL_EFFECTS,
        }
    ),
}
_MECHANISM_COVERAGE = {
    RecoveryMechanism.CODE_POINTER: frozenset(),
    RecoveryMechanism.METADATA_JOURNAL: frozenset({RecoveryDomain.DATABASE}),
    RecoveryMechanism.DATABASE_CHECKPOINT: frozenset({RecoveryDomain.DATABASE}),
    RecoveryMechanism.COMPLETE_RECOVERY: frozenset(
        {RecoveryDomain.DATABASE, RecoveryDomain.FILESYSTEM}
    ),
    RecoveryMechanism.NONE: frozenset(),
}


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    """Versioned, receipt-safe recovery decision for one transaction."""

    declared_impact: DataImpact | None
    effective_impact: DataImpact
    declaration_status: ImpactDeclarationStatus
    requested_coverage: RecoveryCoverage
    default_coverage: RecoveryCoverage
    selected_coverage: RecoveryCoverage
    mechanism: RecoveryMechanism
    decision_source: RecoveryDecisionSource
    explicit_selection: bool
    overrides_default: bool
    narrower_than_default: bool
    uncovered_domains: tuple[RecoveryDomain, ...]
    actor: str | None
    reason: str | None
    policy_id: str | None
    recent_recovery_anchor: str | None

    @property
    def complete_for_possible_effects(self) -> bool:
        return not self.uncovered_domains

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        return {
            "format": RECOVERY_PLAN_FORMAT,
            "schema_version": RECOVERY_PLAN_VERSION,
            **payload,
            "complete_for_possible_effects": self.complete_for_possible_effects,
        }


def _impact_and_status(
    value: DataImpact | str | None,
    *,
    authenticated: bool,
    consistent: bool,
) -> tuple[DataImpact | None, DataImpact, ImpactDeclarationStatus]:
    if value is None:
        return None, DataImpact.OPAQUE, ImpactDeclarationStatus.MISSING
    try:
        declared = DataImpact(value)
    except ValueError:
        return None, DataImpact.OPAQUE, ImpactDeclarationStatus.INVALID
    if not consistent:
        return declared, DataImpact.OPAQUE, ImpactDeclarationStatus.INCONSISTENT
    if not authenticated:
        return declared, DataImpact.OPAQUE, ImpactDeclarationStatus.UNAUTHENTICATED
    return declared, declared, ImpactDeclarationStatus.AUTHENTICATED


def _normalized_optional_text(
    value: str | None,
    *,
    label: str,
    maximum_length: int,
) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > maximum_length:
        raise RecoveryPolicyError(
            f"{label} exceeds the {maximum_length}-character limit."
        )
    if any(character in normalized for character in "\r\n\x00"):
        raise RecoveryPolicyError(f"{label} must be a single safe text value.")
    return normalized


def _mechanism_for(
    impact: DataImpact,
    coverage: RecoveryCoverage,
) -> RecoveryMechanism:
    if coverage is RecoveryCoverage.FULL:
        return RecoveryMechanism.COMPLETE_RECOVERY
    if coverage is RecoveryCoverage.DATABASE_ONLY:
        return RecoveryMechanism.DATABASE_CHECKPOINT
    if impact is DataImpact.NONE:
        return RecoveryMechanism.CODE_POINTER
    if impact is DataImpact.REVERSIBLE_METADATA:
        return RecoveryMechanism.METADATA_JOURNAL
    return RecoveryMechanism.NONE


def plan_recovery(
    *,
    declared_impact: DataImpact | str | None,
    declaration_authenticated: bool,
    declaration_consistent: bool = True,
    requested_coverage: RecoveryCoverage | str = RecoveryCoverage.AUTOMATIC,
    decision_source: RecoveryDecisionSource | str = RecoveryDecisionSource.AUTOMATIC,
    reason: str | None = None,
    policy_id: str | None = None,
    actor: str | None = None,
    recent_recovery_anchor: str | None = None,
) -> RecoveryPlan:
    """Resolve impact and recovery coverage without inspecting mutable data.

    Invalid, missing, unauthenticated, or inconsistent declarations fail closed
    to ``opaque``. An explicit selection can choose less recovery, but it never
    changes that impact and must carry the audit evidence required below.
    """

    declared, effective, declaration_status = _impact_and_status(
        declared_impact,
        authenticated=declaration_authenticated,
        consistent=declaration_consistent,
    )
    try:
        requested = RecoveryCoverage(requested_coverage)
    except ValueError as exc:
        raise RecoveryPolicyError(
            f"Unsupported recovery coverage: {requested_coverage!r}."
        ) from exc
    try:
        source = RecoveryDecisionSource(decision_source)
    except ValueError as exc:
        raise RecoveryPolicyError(
            f"Unsupported recovery decision source: {decision_source!r}."
        ) from exc

    default = _DEFAULT_COVERAGE[effective]
    explicit = requested is not RecoveryCoverage.AUTOMATIC
    selected = default if not explicit else requested
    if selected is RecoveryCoverage.AUTOMATIC:
        raise AssertionError("Automatic recovery coverage was not resolved.")
    if explicit and source is RecoveryDecisionSource.AUTOMATIC:
        raise RecoveryPolicyError(
            "An explicit recovery selection must be interactive or unattended."
        )
    if not explicit and source is not RecoveryDecisionSource.AUTOMATIC:
        raise RecoveryPolicyError(
            "Automatic recovery coverage must use the automatic decision source."
        )

    normalized_reason = _normalized_optional_text(
        reason,
        label="Recovery reason",
        maximum_length=_MAX_REASON_LENGTH,
    )
    normalized_policy = _normalized_optional_text(
        policy_id,
        label="Recovery policy ID",
        maximum_length=128,
    )
    if normalized_policy is not None and not _POLICY_ID_PATTERN.fullmatch(
        normalized_policy
    ):
        raise RecoveryPolicyError("Recovery policy ID contains unsafe characters.")
    normalized_actor = _normalized_optional_text(
        actor,
        label="Recovery actor",
        maximum_length=128,
    )
    normalized_anchor = _normalized_optional_text(
        recent_recovery_anchor,
        label="Recovery anchor ID",
        maximum_length=_MAX_ANCHOR_ID_LENGTH,
    )

    overrides_default = selected is not default
    narrower = _COVERAGE_RANK[selected] < _COVERAGE_RANK[default]
    if explicit and normalized_actor is None:
        raise RecoveryPolicyError("An explicit recovery selection requires an actor.")
    if not explicit and any((normalized_actor, normalized_reason, normalized_policy)):
        raise RecoveryPolicyError(
            "Automatic recovery coverage cannot carry operator override fields."
        )
    if narrower and not (normalized_reason or normalized_policy):
        raise RecoveryPolicyError(
            "A recovery selection narrower than the automatic default requires "
            "an operator reason or policy ID."
        )
    if (
        narrower
        and source is RecoveryDecisionSource.UNATTENDED
        and normalized_policy is None
    ):
        raise RecoveryPolicyError(
            "An unattended narrower recovery selection requires a policy ID."
        )

    mechanism = _mechanism_for(effective, selected)
    uncovered = tuple(
        sorted(
            _POSSIBLE_DOMAINS[effective] - _MECHANISM_COVERAGE[mechanism],
            key=str,
        )
    )
    return RecoveryPlan(
        declared_impact=declared,
        effective_impact=effective,
        declaration_status=declaration_status,
        requested_coverage=requested,
        default_coverage=default,
        selected_coverage=selected,
        mechanism=mechanism,
        decision_source=source,
        explicit_selection=explicit,
        overrides_default=overrides_default,
        narrower_than_default=narrower,
        uncovered_domains=uncovered,
        actor=normalized_actor,
        reason=normalized_reason,
        policy_id=normalized_policy,
        recent_recovery_anchor=normalized_anchor,
    )


def legacy_opaque_recovery_plan() -> RecoveryPlan:
    """Return the current fail-closed plan for an unversioned transaction."""

    return plan_recovery(
        declared_impact=None,
        declaration_authenticated=False,
    )
