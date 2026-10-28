"""Exact cross-operation reuse of completed Translator phase evidence.

The source plan and context-artifact revisions are already immutable.  This
module adds the missing operation-owned decision record: a new operation may
reference a verified historical analysis trio (and, independently, one
completed research conclusion) only when every phase input that could affect
that evidence is identical.  Mutable translation, glossary, memory, and target
state are intentionally outside this identity and are frozen afresh.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy.exc import IntegrityError

from carlina.core.utc import utc_now_db

from ...extensions import db
from .analysis_attempts import (
    ANALYSIS_CONTRACT_VERSION_V1,
    rehydrate_analysis_attempt,
    rehydrate_completed_analysis_output,
)
from .analysis_persistence import rehydrate_context_artifact_revision
from .analysis_scope_contract import SUPPORTED_ANALYSIS_SCOPE_PLANNER_CONTRACTS
from .block_analysis import (
    ANALYSIS_KIND,
    ANALYSIS_MAP_SCHEMA_VERSION,
    ANALYSIS_PROFILE_ID,
    ANALYSIS_PROFILE_VERSION,
    ANALYSIS_REDUCE_KIND,
    ANALYSIS_REDUCE_SCHEMA_VERSION,
    HEADING_PATH_SOURCE,
    AnalysisReduceResult,
    AnalysisScope,
)
from .block_contract import (
    BlockContractError,
    canonical_json,
    sha256_text,
)
from .block_contract import (
    TranslationSourcePlan as CanonicalSourcePlan,
)
from .block_persistence import rehydrate_source_plan
from .block_provider_adapter import PROVIDER_DEFAULT_OUTPUT_POLICY
from .block_research import materialize_block_research_settings
from .block_workflow_config import rehydrate_historical_block_workflow_config
from .integrity_receipts import make_receipt, verify_receipt
from .models import (
    TranslationAnalysisAttempt,
    TranslationBlockWorkflowConfig,
    TranslationContextArtifactRevision,
    TranslationContextBaseline,
    TranslationOperation,
    TranslationOperationEvidenceReuse,
    TranslationResearchRun,
    TranslationSourcePlan,
)
from .research_input_contract import (
    LEGACY_RESEARCH_INPUT_PLANNER_CONTRACT_VERSION,
    MAX_RESEARCH_INPUT_BYTES,
    RESEARCH_INPUT_PLANNER_CONTRACT_VERSION,
    validate_research_packet_source,
)
from .research_persistence import (
    ResearchVerificationContext,
    rehydrate_completed_research_conclusion,
    rehydrate_research_run,
)

EVIDENCE_REUSE_CONTRACT_VERSION = "translator-operation-evidence-reuse-v1"
EVIDENCE_REUSE_RECEIPT_PURPOSE = "translator-operation-evidence-reuse-v1"
_ANALYSIS_ARTIFACT_KINDS = (
    "document_brief",
    "style_guide",
    "entity_register",
)
_ANALYSIS_SETTINGS_CONTRACT = "document-block-phase-settings-v1"
_RESEARCH_SETTINGS_CONTRACT = "document-block-research-settings-v1"
_ANALYSIS_OUTPUT_LIMIT = 16_384
_ANALYSIS_SELECTION_VERSION = "document-block-analysis-reuse-v1"
_RESEARCH_SELECTION_VERSION = "document-block-research-reuse-v1"


@dataclass(frozen=True, slots=True)
class OperationEvidenceReuseSelection:
    row: TranslationOperationEvidenceReuse
    artifacts: dict[str, TranslationContextArtifactRevision]
    reduced: AnalysisReduceResult
    research_run: TranslationResearchRun | None


def _verified_source_plan(
    source_plan: TranslationSourcePlan,
    verified_plan: CanonicalSourcePlan | None,
) -> CanonicalSourcePlan:
    if verified_plan is None:
        return rehydrate_source_plan(source_plan)
    if (
        verified_plan.project_id != source_plan.project_id
        or verified_plan.plan_hash != source_plan.plan_hash
        or verified_plan.snapshot.document_id != source_plan.source_document_id
        or verified_plan.snapshot.content_hash != source_plan.source_content_hash
    ):
        raise BlockContractError("evidence-reuse-source-plan")
    return verified_plan


@dataclass(frozen=True, slots=True)
class _Candidate:
    workflow: TranslationBlockWorkflowConfig
    operation: TranslationOperation
    artifacts: dict[str, TranslationContextArtifactRevision]
    reduced: AnalysisReduceResult
    analysis_scope_hashes: tuple[str, ...]
    analysis_scope_planner_contract: str
    research_run: TranslationResearchRun | None
    research_conclusion: dict[str, Any] | None


def _model_execution_identity(value: object) -> dict[str, Any]:
    """Project a frozen model onto fields that can affect provider behavior.

    Credential identity is authority for making a new call, not part of the
    semantics of evidence already produced.  A separate non-secret transport
    identity keeps pure key rotation reusable while endpoint/routing changes
    fail closed.
    """

    if not isinstance(value, Mapping):
        raise BlockContractError("evidence-reuse-model")
    required = {
        "model_id",
        "model_record_id",
        "model_revision",
        "provider_id",
        "provider_name",
        "provider_kind",
        "transport_identity",
        "capability_profile",
        "capability_profile_hash",
    }
    if not required.issubset(value):
        raise BlockContractError("evidence-reuse-model")
    return {key: value[key] for key in sorted(required)}


def _analysis_budget(phase: Mapping[str, Any]) -> dict[str, int]:
    model = phase.get("model")
    profile = model.get("capability_profile") if isinstance(model, Mapping) else None
    raw = profile.get("max_output_tokens") if isinstance(profile, Mapping) else None
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1:
        raise BlockContractError("evidence-reuse-analysis-capability")
    return {"output_tokens": min(raw, _ANALYSIS_OUTPUT_LIMIT)}


def _scope_plan_descriptor(
    scope_hashes: Sequence[str], *, planner_contract: object
) -> dict[str, Any]:
    hashes = list(scope_hashes)
    if not hashes or any(
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
        for value in hashes
    ):
        raise BlockContractError("evidence-reuse-analysis-scopes")
    if not isinstance(planner_contract, str) or not planner_contract:
        raise BlockContractError("evidence-reuse-analysis-planner")
    return {
        "contract_version": planner_contract,
        "ordered_scope_hashes": hashes,
    }


def _analysis_descriptor(
    config: Mapping[str, Any],
    *,
    scope_hashes: Sequence[str] | None = None,
    scope_planner_contract: object = None,
) -> dict[str, Any]:
    if config.get("enable_pre_analysis") is not True:
        raise BlockContractError("evidence-reuse-analysis-disabled")
    phase = config.get("pre_analysis")
    if not isinstance(phase, Mapping):
        raise BlockContractError("evidence-reuse-analysis")
    planner_contract = (
        scope_planner_contract
        if scope_planner_contract is not None
        else phase.get("scope_planner_contract")
    )
    if not isinstance(planner_contract, str) or not planner_contract:
        raise BlockContractError("evidence-reuse-analysis-planner")
    descriptor = {
        "contract_version": _ANALYSIS_SELECTION_VERSION,
        "attempt_contract_version": ANALYSIS_CONTRACT_VERSION_V1,
        "map_profile_id": ANALYSIS_PROFILE_ID,
        "map_profile_version": ANALYSIS_PROFILE_VERSION,
        "map_schema_version": ANALYSIS_MAP_SCHEMA_VERSION,
        "reduce_kind": ANALYSIS_REDUCE_KIND,
        "reduce_schema_version": ANALYSIS_REDUCE_SCHEMA_VERSION,
        "heading_path_source": HEADING_PATH_SOURCE,
        "census_profile": "semantic-census-v1",
        "phase_settings_contract": _ANALYSIS_SETTINGS_CONTRACT,
        "model": _model_execution_identity(phase.get("model")),
        "reasoning": phase.get("reasoning"),
        "budget": _analysis_budget(phase),
        "scope_planner_contract": planner_contract,
    }
    if scope_hashes is not None:
        descriptor["scope_plan"] = _scope_plan_descriptor(
            scope_hashes,
            planner_contract=planner_contract,
        )
    return descriptor


def _research_descriptor(config: Mapping[str, Any]) -> dict[str, Any] | None:
    if config.get("enable_research") is not True:
        return None
    phase = config.get("research")
    if not isinstance(phase, Mapping):
        raise BlockContractError("evidence-reuse-research")
    selected = phase.get("selected")
    if not isinstance(selected, Mapping):
        raise BlockContractError("evidence-reuse-research")
    planner_contract = phase.get("input_planner_contract")
    current_input = planner_contract is not None
    if planner_contract is None:
        planner_contract = LEGACY_RESEARCH_INPUT_PLANNER_CONTRACT_VERSION
    if not isinstance(planner_contract, str) or not planner_contract:
        raise BlockContractError("evidence-reuse-research-planner")
    return {
        "contract_version": _RESEARCH_SELECTION_VERSION,
        "settings_contract_version": _RESEARCH_SETTINGS_CONTRACT,
        "input_planner_contract": planner_contract,
        "research_input_bytes": (
            MAX_RESEARCH_INPUT_BYTES if current_input else 320 * 1024
        ),
        "research_result_bytes": 2 * 1024 * 1024,
        "model": _model_execution_identity(phase.get("model")),
        "reasoning": phase.get("reasoning"),
        "budget": selected.get("budget"),
        "tool_declarations": selected.get("tool_declarations"),
        "selected_provider_ids": selected.get("selected_provider_ids"),
        "selected_addon_ids": selected.get("selected_addon_ids"),
    }


def _scope_descriptor(
    operation: TranslationOperation,
    source_plan: TranslationSourcePlan,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "owner_user_id": operation.owner_user_id,
        "project_id": operation.project_id,
        "source_plan_id": source_plan.id,
        "source_plan_hash": source_plan.plan_hash,
        "source_content_hash": source_plan.source_content_hash,
        "source_language": config.get("source_language"),
        "target_language": config.get("target_language"),
        "engine_contract": config.get("engine_contract"),
    }


def _phase_settings(
    workflow: TranslationBlockWorkflowConfig, phase: str
) -> dict[str, Any]:
    result = {
        "contract_version": _ANALYSIS_SETTINGS_CONTRACT,
        "phase": phase,
        "workflow_config_id": workflow.id,
        "workflow_config_hash": workflow.config_hash,
        "workflow_config_receipt": workflow.config_receipt,
    }
    if phase == "pre_analysis":
        phase_config = workflow.config_json.get("pre_analysis")
        planner_contract = (
            phase_config.get("scope_planner_contract")
            if isinstance(phase_config, Mapping)
            else None
        )
        if planner_contract is None:
            raise BlockContractError("evidence-reuse-analysis-planner")
        result["scope_planner_contract"] = planner_contract
    return result


def _artifact_rows(
    baseline: TranslationContextBaseline,
) -> tuple[
    dict[str, TranslationContextArtifactRevision], TranslationResearchRun | None
]:
    artifacts: dict[str, TranslationContextArtifactRevision] = {}
    research: TranslationResearchRun | None = None
    for item in baseline.items:
        if item.item_kind in _ANALYSIS_ARTIFACT_KINDS:
            revision = db.session.get(
                TranslationContextArtifactRevision, item.artifact_revision_id
            )
            if revision is None or item.item_kind in artifacts:
                raise BlockContractError("evidence-reuse-artifact-selection")
            artifacts[item.item_kind] = revision
        elif item.item_kind == "research_evidence":
            if research is not None:
                raise BlockContractError("evidence-reuse-research-selection")
            research = db.session.get(TranslationResearchRun, item.research_run_id)
            if research is None:
                raise BlockContractError("evidence-reuse-research-selection")
    if set(artifacts) != set(_ANALYSIS_ARTIFACT_KINDS):
        raise BlockContractError("evidence-reuse-artifact-selection")
    return artifacts, research


def _validated_analysis_evidence(
    *,
    workflow: TranslationBlockWorkflowConfig,
    operation: TranslationOperation,
    source_plan: TranslationSourcePlan,
    artifacts: Mapping[str, TranslationContextArtifactRevision],
    verified_plan: CanonicalSourcePlan,
) -> tuple[AnalysisReduceResult, tuple[str, ...], str]:
    phase = workflow.config_json.get("pre_analysis")
    if not isinstance(phase, Mapping):
        raise BlockContractError("evidence-reuse-analysis")
    planner_contract = phase.get("scope_planner_contract")
    if planner_contract not in SUPPORTED_ANALYSIS_SCOPE_PLANNER_CONTRACTS:
        raise BlockContractError("evidence-reuse-analysis-planner")
    expected_settings = _phase_settings(workflow, "pre_analysis")
    expected_prompt = {
        "profile_id": ANALYSIS_PROFILE_ID,
        "profile_version": ANALYSIS_PROFILE_VERSION,
        "reduce_schema_version": ANALYSIS_REDUCE_SCHEMA_VERSION,
    }
    payloads: dict[str, Any] = {}
    shared_input: dict[str, Any] | None = None
    shared_evidence: dict[str, Any] | None = None
    attempt_scope_hashes: list[str] = []
    for kind in _ANALYSIS_ARTIFACT_KINDS:
        revision = artifacts.get(kind)
        if revision is None:
            raise BlockContractError("evidence-reuse-artifact-selection")
        payload = rehydrate_context_artifact_revision(revision, source_plan)
        if (
            payload.kind != kind
            or revision.analysis_state != "model"
            or revision.profile_id != ANALYSIS_REDUCE_SCHEMA_VERSION
            or revision.profile_version != "1"
            or revision.model_json != phase.get("model")
            or revision.prompt_json != expected_prompt
            or revision.settings_json != expected_settings
            or not isinstance(revision.input_json, Mapping)
            or not isinstance(revision.evidence_json, Mapping)
        ):
            raise BlockContractError("evidence-reuse-artifact")
        input_value = dict(revision.input_json)
        evidence_value = dict(revision.evidence_json)
        if shared_input is None:
            shared_input = input_value
            shared_evidence = evidence_value
        elif input_value != shared_input or evidence_value != shared_evidence:
            raise BlockContractError("evidence-reuse-artifact-lineage")
        payloads[kind] = payload
    if shared_input is None or shared_evidence is None:
        raise BlockContractError("evidence-reuse-artifact-selection")
    if (
        shared_input.get("source_plan_hash") != source_plan.plan_hash
        or shared_evidence.get("source_plan_hash") != source_plan.plan_hash
        or shared_evidence.get("reduction_mode") != "provider"
    ):
        raise BlockContractError("evidence-reuse-artifact-lineage")
    scope_hashes = shared_input.get("scope_hashes")
    scope_attempts = shared_evidence.get("scope_attempts")
    reduce_lineage = shared_evidence.get("reduce_attempt")
    if (
        not isinstance(scope_hashes, list)
        or not scope_hashes
        or not isinstance(scope_attempts, list)
        or len(scope_attempts) != len(scope_hashes)
        or not isinstance(reduce_lineage, Mapping)
    ):
        raise BlockContractError("evidence-reuse-artifact-lineage")
    for scope_hash, reference in zip(scope_hashes, scope_attempts, strict=True):
        if not isinstance(reference, Mapping) or set(reference) != {
            "scope_hash",
            "attempt_id",
            "attempt_hash",
            "request_hash",
        }:
            raise BlockContractError("evidence-reuse-artifact-lineage")
        attempt = db.session.get(TranslationAnalysisAttempt, reference["attempt_id"])
        if (
            attempt is None
            or attempt.operation_id != operation.id
            or attempt.analysis_kind != ANALYSIS_KIND
            or attempt.dispatch_state != "completed"
            or reference["scope_hash"] != scope_hash
            or attempt.attempt_record_hash != reference["attempt_hash"]
            or attempt.request_hash != reference["request_hash"]
        ):
            raise BlockContractError("evidence-reuse-artifact-lineage")
        packet = rehydrate_analysis_attempt(
            attempt,
            source_plan,
            verified_plan=verified_plan,
        )
        packet_input = packet.get("input")
        scope = packet_input.get("scope") if isinstance(packet_input, Mapping) else None
        packet_phase = (
            packet_input.get("phase") if isinstance(packet_input, Mapping) else None
        )
        parsed_scope = (
            AnalysisScope.from_dict(scope) if isinstance(scope, Mapping) else None
        )
        scope_identity = (
            {
                "source_plan_hash": scope.get("source_plan_hash"),
                "part_index": scope.get("part_index"),
                "block_ids": scope.get("block_ids"),
            }
            if isinstance(scope, Mapping)
            else None
        )
        if (
            parsed_scope is None
            or packet_phase != phase
            or sha256_text(canonical_json(scope_identity)) != scope_hash
        ):
            raise BlockContractError("evidence-reuse-artifact-lineage")
        attempt_scope_hashes.append(parsed_scope.scope_hash)
        rehydrate_completed_analysis_output(
            attempt,
            source_plan,
            verified_plan=verified_plan,
        )
    if set(reduce_lineage) != {
        "attempt_id",
        "attempt_record_hash",
        "request_hash",
    }:
        raise BlockContractError("evidence-reuse-artifact-lineage")
    reduce_attempt = db.session.get(
        TranslationAnalysisAttempt, reduce_lineage["attempt_id"]
    )
    if (
        reduce_attempt is None
        or reduce_attempt.operation_id != operation.id
        or reduce_attempt.analysis_kind != ANALYSIS_REDUCE_KIND
        or reduce_attempt.dispatch_state != "completed"
        or reduce_attempt.attempt_record_hash != reduce_lineage["attempt_record_hash"]
        or reduce_attempt.request_hash != reduce_lineage["request_hash"]
    ):
        raise BlockContractError("evidence-reuse-artifact-lineage")
    packet = rehydrate_analysis_attempt(
        reduce_attempt,
        source_plan,
        verified_plan=verified_plan,
    )
    packet_input = packet.get("input")
    if not isinstance(packet_input, Mapping) or packet_input.get("phase") != phase:
        raise BlockContractError("evidence-reuse-artifact-lineage")
    mapped_block_ids = packet_input.get("mapped_block_ids")
    if not isinstance(mapped_block_ids, list):
        raise BlockContractError("evidence-reuse-artifact-lineage")
    reduced = AnalysisReduceResult.from_response(
        rehydrate_completed_analysis_output(
            reduce_attempt,
            source_plan,
            verified_plan=verified_plan,
        ),
        source_plan_hash=source_plan.plan_hash,
        scope_hashes=scope_hashes,
        mapped_block_ids=mapped_block_ids,
    )
    if (
        reduced.document_brief != payloads["document_brief"].content
        or reduced.style_guide != payloads["style_guide"].content
        or reduced.entity_register != payloads["entity_register"].content
        or [dict(item) for item in reduced.glossary_proposals]
        != shared_evidence.get("glossary_proposals")
        or [dict(item) for item in reduced.unresolved]
        != shared_evidence.get("unresolved")
    ):
        raise BlockContractError("evidence-reuse-artifact-output")
    return reduced, tuple(attempt_scope_hashes), planner_contract


def _validated_research_evidence(
    *,
    workflow: TranslationBlockWorkflowConfig,
    operation: TranslationOperation,
    source_plan: TranslationSourcePlan,
    run: TranslationResearchRun,
    verified_plan: CanonicalSourcePlan,
    verification_context: ResearchVerificationContext | None = None,
) -> dict[str, Any]:
    phase = workflow.config_json.get("research")
    if not isinstance(phase, Mapping):
        raise BlockContractError("evidence-reuse-research")
    selected = phase.get("selected")
    if not isinstance(selected, Mapping):
        raise BlockContractError("evidence-reuse-research")
    if (
        run.operation_id != operation.id
        or run.owner_user_id != operation.owner_user_id
        or run.project_id != operation.project_id
        or run.source_plan_id != source_plan.id
    ):
        raise BlockContractError("evidence-reuse-research-scope")
    graph = rehydrate_research_run(
        run,
        verified_plan=verified_plan,
        verification_context=verification_context,
    )
    settings_input: dict[str, object] = {
        "contract_version": _RESEARCH_SETTINGS_CONTRACT,
        "workflow_config_id": workflow.id,
        "workflow_config_hash": workflow.config_hash,
        "workflow_config_receipt": workflow.config_receipt,
    }
    if phase.get("input_planner_contract") == RESEARCH_INPUT_PLANNER_CONTRACT_VERSION:
        settings_input.update(
            {
                "input_planner_contract": RESEARCH_INPUT_PLANNER_CONTRACT_VERSION,
                "research_input_bytes": MAX_RESEARCH_INPUT_BYTES,
            }
        )
    expected_settings = materialize_block_research_settings(settings_input)
    if (
        graph["model"] != phase.get("model")
        or graph["settings"] != expected_settings
        or graph["profile"]
        != {
            "phase": dict(phase),
            "provider_output_policy": PROVIDER_DEFAULT_OUTPUT_POLICY,
        }
        or graph["budget"] != selected.get("budget")
        or graph["selected_provider_ids"] != selected.get("selected_provider_ids")
        or graph["selected_addon_ids"] != selected.get("selected_addon_ids")
        or graph["tool_declarations"] != selected.get("tool_declarations")
    ):
        raise BlockContractError("evidence-reuse-research-lineage")
    conclusion = dict(
        rehydrate_completed_research_conclusion(
            run,
            verified_plan=verified_plan,
            verification_context=verification_context,
        )
    )
    if phase.get("input_planner_contract") == RESEARCH_INPUT_PLANNER_CONTRACT_VERSION:
        turns = graph.get("turns")
        if not isinstance(turns, tuple) or not turns:
            raise BlockContractError("evidence-reuse-research-input")
        first_attempt = db.session.get(
            TranslationAnalysisAttempt,
            turns[0].analysis_attempt_id,
            populate_existing=True,
        )
        if first_attempt is None:
            raise BlockContractError("evidence-reuse-research-input")
        attempt_data = rehydrate_analysis_attempt(
            first_attempt,
            source_plan,
            verified_plan=verified_plan,
        )
        input_descriptor = validate_research_packet_source(
            verified_plan,
            {
                "messages": attempt_data["messages"],
                "input": attempt_data["input"],
            },
        )
        if input_descriptor["effective_output_tokens"] != selected.get(
            "budget", {}
        ).get("output_tokens") or attempt_data["tool_declarations"] != [
            item.get("declaration")
            for item in selected.get("tool_declarations", [])
            if isinstance(item, Mapping)
        ]:
            raise BlockContractError("evidence-reuse-research-input")
        conclusion.update(input_descriptor)
    return conclusion


def _artifact_signature(
    artifacts: Mapping[str, TranslationContextArtifactRevision],
) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        (kind, artifacts[kind].id, artifacts[kind].record_hash)
        for kind in _ANALYSIS_ARTIFACT_KINDS
    )


def _research_signature(conclusion: Mapping[str, Any]) -> tuple[str, ...]:
    base = tuple(
        str(conclusion[key])
        for key in (
            "run_id",
            "run_record_hash",
            "state_head_receipt",
            "final_turn_id",
            "final_turn_record_hash",
            "final_attempt_id",
            "final_attempt_record_hash",
            "final_attempt_request_hash",
            "conclusion_hash",
        )
    )
    input_keys = (
        "initial_packet_hash",
        "source_document_hash",
        "effective_output_tokens",
        "source_scope",
    )
    if not any(key in conclusion for key in input_keys):
        return (*base, "legacy-reduced-brief")
    if not all(key in conclusion for key in input_keys):
        raise BlockContractError("evidence-reuse-research-input")
    return (*base, *(canonical_json(conclusion[key]) for key in input_keys))


def _candidate_selection(
    *,
    operation: TranslationOperation,
    source_plan: TranslationSourcePlan,
    workflow: TranslationBlockWorkflowConfig,
    analysis_scope_hashes: Sequence[str],
    verified_plan: CanonicalSourcePlan,
    verification_context: ResearchVerificationContext | None = None,
) -> _Candidate | None:
    current_config = cast(Mapping[str, Any], workflow.config_json)
    try:
        current_analysis = _analysis_descriptor(current_config)
    except BlockContractError as exc:
        if exc.code in {
            "evidence-reuse-model",
            "evidence-reuse-analysis-planner",
        }:
            return None
        raise
    current_phase = current_config.get("pre_analysis")
    current_planner_contract = (
        current_phase.get("scope_planner_contract")
        if isinstance(current_phase, Mapping)
        else None
    )
    if current_planner_contract is None:
        return None
    current_scope_plan = _scope_plan_descriptor(
        analysis_scope_hashes,
        planner_contract=current_planner_contract,
    )
    current_research = _research_descriptor(current_config)
    candidates: list[_Candidate] = []
    rows = (
        TranslationBlockWorkflowConfig.query.filter_by(
            owner_user_id=operation.owner_user_id,
            project_id=operation.project_id,
            source_plan_id=source_plan.id,
        )
        .filter(TranslationBlockWorkflowConfig.operation_id != operation.id)
        .order_by(
            TranslationBlockWorkflowConfig.created_at,
            TranslationBlockWorkflowConfig.id,
        )
        .all()
    )
    for origin_workflow in rows:
        origin_operation = db.session.get(
            TranslationOperation, origin_workflow.operation_id
        )
        if origin_operation is None:
            raise BlockContractError("evidence-reuse-origin-operation")
        rehydrate_historical_block_workflow_config(
            origin_workflow,
            operation=origin_operation,
            source_plan=source_plan,
        )
        origin_config = cast(Mapping[str, Any], origin_workflow.config_json)
        origin_phase = origin_config.get("pre_analysis")
        origin_model = (
            origin_phase.get("model") if isinstance(origin_phase, Mapping) else None
        )
        if (
            _scope_descriptor(origin_operation, source_plan, origin_config)
            != _scope_descriptor(operation, source_plan, current_config)
            or origin_config.get("enable_pre_analysis") is not True
            or not isinstance(origin_model, Mapping)
            or "transport_identity" not in origin_model
            or not isinstance(origin_phase, Mapping)
            or origin_phase.get("scope_planner_contract") is None
            or _analysis_descriptor(origin_config) != current_analysis
        ):
            continue
        # A reuse operation is a consumer, not a new evidence producer.  Its
        # baseline may point at an earlier operation's artifact settings and
        # attempt lineage.  The restrictive origin FKs retain that producer,
        # so scanning consumers would only duplicate or misattribute the same
        # immutable evidence.
        if (
            TranslationOperationEvidenceReuse.query.filter_by(
                operation_id=origin_operation.id
            ).first()
            is not None
        ):
            continue
        baselines = TranslationContextBaseline.query.filter_by(
            operation_id=origin_operation.id
        ).all()
        if not baselines:
            continue
        if len(baselines) != 1:
            raise BlockContractError("evidence-reuse-origin-baseline")
        # Imported locally so context-baseline verification can in turn consult
        # this module for cross-operation research references without a module
        # initialization cycle.
        from .context_baselines import rehydrate_context_baseline

        baseline = rehydrate_context_baseline(
            baselines[0],
            source_plan,
            verified_plan=verified_plan,
            verification_context=verification_context,
        )
        artifacts, research_run = _artifact_rows(baseline)
        reduced, origin_scope_hashes, origin_planner_contract = (
            _validated_analysis_evidence(
                workflow=origin_workflow,
                operation=origin_operation,
                source_plan=source_plan,
                artifacts=artifacts,
                verified_plan=verified_plan,
            )
        )
        if (
            _scope_plan_descriptor(
                origin_scope_hashes,
                planner_contract=origin_planner_contract,
            )
            != current_scope_plan
        ):
            continue
        conclusion: dict[str, Any] | None = None
        if (
            current_research is not None
            and origin_config.get("enable_research") is True
            and _research_descriptor(origin_config) == current_research
            and research_run is not None
        ):
            conclusion = _validated_research_evidence(
                workflow=origin_workflow,
                operation=origin_operation,
                source_plan=source_plan,
                run=research_run,
                verified_plan=verified_plan,
                verification_context=verification_context,
            )
        else:
            research_run = None
        candidates.append(
            _Candidate(
                workflow=origin_workflow,
                operation=origin_operation,
                artifacts=artifacts,
                reduced=reduced,
                analysis_scope_hashes=origin_scope_hashes,
                analysis_scope_planner_contract=origin_planner_contract,
                research_run=research_run,
                research_conclusion=conclusion,
            )
        )
    if not candidates:
        return None
    artifact_groups = {_artifact_signature(item.artifacts) for item in candidates}
    if len(artifact_groups) != 1:
        raise BlockContractError("evidence-reuse-analysis-ambiguous")
    research_candidates = [
        item for item in candidates if item.research_conclusion is not None
    ]
    if research_candidates:
        research_groups = {
            _research_signature(cast(Mapping[str, Any], item.research_conclusion))
            for item in research_candidates
        }
        if len(research_groups) != 1:
            raise BlockContractError("evidence-reuse-research-ambiguous")
        return research_candidates[0]
    return candidates[0]


def _selection_json(
    *,
    operation: TranslationOperation,
    source_plan: TranslationSourcePlan,
    workflow: TranslationBlockWorkflowConfig,
    candidate: _Candidate,
) -> dict[str, Any]:
    current_config = cast(Mapping[str, Any], workflow.config_json)
    research_reference = None
    if candidate.research_conclusion is not None:
        research_reference = {
            key: candidate.research_conclusion[key]
            for key in (
                "run_id",
                "run_record_hash",
                "run_state",
                "state_head_receipt",
                "final_turn_id",
                "final_turn_record_hash",
                "final_attempt_id",
                "final_attempt_record_hash",
                "final_attempt_request_hash",
                "conclusion_hash",
            )
        }
        input_keys = (
            "initial_packet_hash",
            "source_document_hash",
            "effective_output_tokens",
            "source_scope",
        )
        if any(key in candidate.research_conclusion for key in input_keys):
            if not all(key in candidate.research_conclusion for key in input_keys):
                raise BlockContractError("evidence-reuse-research-input")
            research_reference.update(
                {key: candidate.research_conclusion[key] for key in input_keys}
            )
    return {
        "contract_version": EVIDENCE_REUSE_CONTRACT_VERSION,
        "scope": _scope_descriptor(operation, source_plan, current_config),
        "analysis": _analysis_descriptor(
            current_config,
            scope_hashes=candidate.analysis_scope_hashes,
            scope_planner_contract=candidate.analysis_scope_planner_contract,
        ),
        "research": (
            _research_descriptor(current_config)
            if candidate.research_run is not None
            else None
        ),
        "artifacts": [
            {
                "kind": kind,
                "revision_id": candidate.artifacts[kind].id,
                "revision_hash": candidate.artifacts[kind].record_hash,
            }
            for kind in _ANALYSIS_ARTIFACT_KINDS
        ],
        "research_reference": research_reference,
    }


def _receipt_values(row: TranslationOperationEvidenceReuse) -> dict[str, Any]:
    return {
        "id": row.id,
        "contract_version": row.contract_version,
        "operation_id": row.operation_id,
        "origin_operation_id": row.origin_operation_id,
        "owner_user_id": row.owner_user_id,
        "project_id": row.project_id,
        "source_plan_id": row.source_plan_id,
        "source_plan_hash": row.source_plan_hash,
        "source_content_hash": row.source_content_hash,
        "workflow_config_id": row.workflow_config_id,
        "workflow_config_hash": row.workflow_config_hash,
        "origin_workflow_config_id": row.origin_workflow_config_id,
        "origin_workflow_config_hash": row.origin_workflow_config_hash,
        "document_brief_revision_id": row.document_brief_revision_id,
        "document_brief_revision_hash": row.document_brief_revision_hash,
        "style_guide_revision_id": row.style_guide_revision_id,
        "style_guide_revision_hash": row.style_guide_revision_hash,
        "entity_register_revision_id": row.entity_register_revision_id,
        "entity_register_revision_hash": row.entity_register_revision_hash,
        "research_run_id": row.research_run_id,
        "research_run_record_hash": row.research_run_record_hash,
        "selection_json": row.selection_json,
        "selection_hash": row.selection_hash,
        "created_at": row.created_at.isoformat(),
    }


def rehydrate_operation_evidence_reuse(
    row: TranslationOperationEvidenceReuse,
    *,
    operation: TranslationOperation | None = None,
    source_plan: TranslationSourcePlan | None = None,
    workflow: TranslationBlockWorkflowConfig | None = None,
    analysis_scope_hashes: Sequence[str] | None = None,
    verified_plan: CanonicalSourcePlan | None = None,
    verification_context: ResearchVerificationContext | None = None,
) -> OperationEvidenceReuseSelection:
    try:
        if not isinstance(row, TranslationOperationEvidenceReuse) or not row.id:
            raise BlockContractError("evidence-reuse-row")
        operation = operation or db.session.get(TranslationOperation, row.operation_id)
        source_plan = source_plan or db.session.get(
            TranslationSourcePlan, row.source_plan_id
        )
        workflow = workflow or db.session.get(
            TranslationBlockWorkflowConfig, row.workflow_config_id
        )
        origin_operation = db.session.get(TranslationOperation, row.origin_operation_id)
        origin_workflow = db.session.get(
            TranslationBlockWorkflowConfig, row.origin_workflow_config_id
        )
        if any(
            item is None
            for item in (
                operation,
                source_plan,
                workflow,
                origin_operation,
                origin_workflow,
            )
        ):
            raise BlockContractError("evidence-reuse-scope")
        operation = cast(TranslationOperation, operation)
        source_plan = cast(TranslationSourcePlan, source_plan)
        verified_plan = _verified_source_plan(source_plan, verified_plan)
        workflow = cast(TranslationBlockWorkflowConfig, workflow)
        origin_operation = cast(TranslationOperation, origin_operation)
        origin_workflow = cast(TranslationBlockWorkflowConfig, origin_workflow)
        rehydrate_historical_block_workflow_config(
            workflow,
            operation=operation,
            source_plan=source_plan,
        )
        rehydrate_historical_block_workflow_config(
            origin_workflow,
            operation=origin_operation,
            source_plan=source_plan,
        )
        if (
            row.contract_version != EVIDENCE_REUSE_CONTRACT_VERSION
            or row.operation_id != operation.id
            or row.origin_operation_id != origin_operation.id
            or row.owner_user_id != operation.owner_user_id
            or row.owner_user_id != origin_operation.owner_user_id
            or row.project_id != operation.project_id
            or row.project_id != origin_operation.project_id
            or row.source_plan_id != source_plan.id
            or row.source_plan_hash != source_plan.plan_hash
            or row.source_content_hash != source_plan.source_content_hash
            or row.workflow_config_id != workflow.id
            or row.workflow_config_hash != workflow.config_hash
            or row.origin_workflow_config_id != origin_workflow.id
            or row.origin_workflow_config_hash != origin_workflow.config_hash
        ):
            raise BlockContractError("evidence-reuse-scope")
        artifacts = {
            kind: db.session.get(TranslationContextArtifactRevision, identifier)
            for kind, identifier in (
                ("document_brief", row.document_brief_revision_id),
                ("style_guide", row.style_guide_revision_id),
                ("entity_register", row.entity_register_revision_id),
            )
        }
        if any(value is None for value in artifacts.values()):
            raise BlockContractError("evidence-reuse-artifact-selection")
        typed_artifacts = cast(dict[str, TranslationContextArtifactRevision], artifacts)
        for kind, expected_hash in (
            ("document_brief", row.document_brief_revision_hash),
            ("style_guide", row.style_guide_revision_hash),
            ("entity_register", row.entity_register_revision_hash),
        ):
            if typed_artifacts[kind].record_hash != expected_hash:
                raise BlockContractError("evidence-reuse-artifact-selection")
        reduced, origin_scope_hashes, origin_planner_contract = (
            _validated_analysis_evidence(
                workflow=origin_workflow,
                operation=origin_operation,
                source_plan=source_plan,
                artifacts=typed_artifacts,
                verified_plan=verified_plan,
            )
        )
        current_config = cast(Mapping[str, Any], workflow.config_json)
        origin_config = cast(Mapping[str, Any], origin_workflow.config_json)
        if _scope_descriptor(
            operation, source_plan, current_config
        ) != _scope_descriptor(
            origin_operation, source_plan, origin_config
        ) or _analysis_descriptor(current_config) != _analysis_descriptor(
            origin_config
        ):
            raise BlockContractError("evidence-reuse-analysis-drift")
        if analysis_scope_hashes is not None:
            current_phase = current_config.get("pre_analysis")
            current_planner_contract = (
                current_phase.get("scope_planner_contract")
                if isinstance(current_phase, Mapping)
                else None
            )
            if _scope_plan_descriptor(
                analysis_scope_hashes,
                planner_contract=current_planner_contract,
            ) != _scope_plan_descriptor(
                origin_scope_hashes,
                planner_contract=origin_planner_contract,
            ):
                raise BlockContractError("evidence-reuse-analysis-scope-drift")
        research_run = None
        conclusion = None
        if row.research_run_id is not None:
            research_run = db.session.get(TranslationResearchRun, row.research_run_id)
            if (
                research_run is None
                or research_run.run_record_hash != row.research_run_record_hash
                or _research_descriptor(current_config)
                != _research_descriptor(origin_config)
            ):
                raise BlockContractError("evidence-reuse-research-drift")
            conclusion = _validated_research_evidence(
                workflow=origin_workflow,
                operation=origin_operation,
                source_plan=source_plan,
                run=research_run,
                verified_plan=verified_plan,
                verification_context=verification_context,
            )
        candidate = _Candidate(
            workflow=origin_workflow,
            operation=origin_operation,
            artifacts=typed_artifacts,
            reduced=reduced,
            analysis_scope_hashes=origin_scope_hashes,
            analysis_scope_planner_contract=origin_planner_contract,
            research_run=research_run,
            research_conclusion=conclusion,
        )
        expected_selection = _selection_json(
            operation=operation,
            source_plan=source_plan,
            workflow=workflow,
            candidate=candidate,
        )
        if (
            row.selection_json != expected_selection
            or row.selection_hash != sha256_text(canonical_json(expected_selection))
        ):
            raise BlockContractError("evidence-reuse-selection")
        verify_receipt(
            stored=row.selection_receipt,
            purpose=EVIDENCE_REUSE_RECEIPT_PURPOSE,
            values=_receipt_values(row),
            code="evidence-reuse-receipt",
        )
        return OperationEvidenceReuseSelection(
            row=row,
            artifacts=typed_artifacts,
            reduced=reduced,
            research_run=research_run,
        )
    except BlockContractError:
        raise
    except Exception as exc:
        raise BlockContractError("evidence-reuse-corrupt") from exc


def discover_or_persist_operation_evidence_reuse(
    *,
    operation: TranslationOperation,
    source_plan: TranslationSourcePlan,
    workflow: TranslationBlockWorkflowConfig,
    analysis_scope_hashes: Sequence[str],
    verified_plan: CanonicalSourcePlan | None = None,
    verification_context: ResearchVerificationContext | None = None,
) -> OperationEvidenceReuseSelection | None:
    """Select one exact historical evidence set and persist that decision."""

    verified_plan = _verified_source_plan(source_plan, verified_plan)
    existing = TranslationOperationEvidenceReuse.query.filter_by(
        operation_id=operation.id
    ).one_or_none()
    if existing is not None:
        return rehydrate_operation_evidence_reuse(
            existing,
            operation=operation,
            source_plan=source_plan,
            workflow=workflow,
            analysis_scope_hashes=analysis_scope_hashes,
            verified_plan=verified_plan,
            verification_context=verification_context,
        )
    candidate = _candidate_selection(
        operation=operation,
        source_plan=source_plan,
        workflow=workflow,
        analysis_scope_hashes=analysis_scope_hashes,
        verified_plan=verified_plan,
        verification_context=verification_context,
    )
    if candidate is None:
        return None
    selection = _selection_json(
        operation=operation,
        source_plan=source_plan,
        workflow=workflow,
        candidate=candidate,
    )
    row = cast(Any, TranslationOperationEvidenceReuse)(
        id=str(uuid.uuid4()),
        contract_version=EVIDENCE_REUSE_CONTRACT_VERSION,
        operation_id=operation.id,
        origin_operation_id=candidate.operation.id,
        owner_user_id=operation.owner_user_id,
        project_id=operation.project_id,
        source_plan_id=source_plan.id,
        source_plan_hash=source_plan.plan_hash,
        source_content_hash=source_plan.source_content_hash,
        workflow_config_id=workflow.id,
        workflow_config_hash=workflow.config_hash,
        origin_workflow_config_id=candidate.workflow.id,
        origin_workflow_config_hash=candidate.workflow.config_hash,
        document_brief_revision_id=candidate.artifacts["document_brief"].id,
        document_brief_revision_hash=candidate.artifacts["document_brief"].record_hash,
        style_guide_revision_id=candidate.artifacts["style_guide"].id,
        style_guide_revision_hash=candidate.artifacts["style_guide"].record_hash,
        entity_register_revision_id=candidate.artifacts["entity_register"].id,
        entity_register_revision_hash=candidate.artifacts[
            "entity_register"
        ].record_hash,
        research_run_id=(
            candidate.research_run.id if candidate.research_run is not None else None
        ),
        research_run_record_hash=(
            candidate.research_run.run_record_hash
            if candidate.research_run is not None
            else None
        ),
        selection_json=selection,
        selection_hash=sha256_text(canonical_json(selection)),
        selection_receipt="0" * 64,
        created_at=utc_now_db(),
    )
    row.selection_receipt = make_receipt(
        purpose=EVIDENCE_REUSE_RECEIPT_PURPOSE,
        values=_receipt_values(row),
    )
    try:
        with db.session.begin_nested():
            db.session.add(row)
            db.session.flush()
    except IntegrityError as exc:
        winner = TranslationOperationEvidenceReuse.query.filter_by(
            operation_id=operation.id
        ).one_or_none()
        if winner is None:
            raise BlockContractError("evidence-reuse-persist") from exc
        return rehydrate_operation_evidence_reuse(
            winner,
            operation=operation,
            source_plan=source_plan,
            workflow=workflow,
            analysis_scope_hashes=analysis_scope_hashes,
            verified_plan=verified_plan,
            verification_context=verification_context,
        )
    return OperationEvidenceReuseSelection(
        row=row,
        artifacts=candidate.artifacts,
        reduced=candidate.reduced,
        research_run=candidate.research_run,
    )


def verify_reused_research_reference(
    *,
    operation: TranslationOperation,
    source_plan: TranslationSourcePlan,
    run: TranslationResearchRun,
    verified_plan: CanonicalSourcePlan | None = None,
    verification_context: ResearchVerificationContext | None = None,
) -> None:
    """Authorize a cross-operation baseline reference through its exact receipt."""

    row = TranslationOperationEvidenceReuse.query.filter_by(
        operation_id=operation.id
    ).one_or_none()
    if row is None:
        raise BlockContractError("baseline-research-scope")
    selection = rehydrate_operation_evidence_reuse(
        row,
        operation=operation,
        source_plan=source_plan,
        verified_plan=verified_plan,
        verification_context=verification_context,
    )
    if selection.research_run is None or selection.research_run.id != run.id:
        raise BlockContractError("baseline-research-scope")


__all__ = [
    "EVIDENCE_REUSE_CONTRACT_VERSION",
    "OperationEvidenceReuseSelection",
    "discover_or_persist_operation_evidence_reuse",
    "rehydrate_operation_evidence_reuse",
    "verify_reused_research_reference",
]
