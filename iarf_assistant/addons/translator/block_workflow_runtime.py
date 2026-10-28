"""Production conductor for one frozen document-block translation operation.

The individual persistence modules deliberately own their respective durable
state machines.  This module is the narrow place where those machines are
ordered: it never resolves mutable model settings, and every provider call is
made only after the corresponding immutable request has been committed.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping
from time import perf_counter
from typing import Any, cast

from ...core.mcp_completion import passive_dispatch, passive_execution_active
from ...core.model_capabilities import ModelCapabilityProfile, admit_provider_payload
from ...extensions import db
from ...models import ApiKey, LLMModel, LLMProvider
from .analysis_attempts import (
    rehydrate_analysis_attempt_events,
    validate_research_attempt_packet,
)
from .analysis_census import build_semantic_census
from .analysis_execution import terminalize_stale_prepared_analysis_attempt
from .analysis_persistence import persist_semantic_census
from .analysis_scope_contract import SUPPORTED_ANALYSIS_SCOPE_PLANNER_CONTRACTS
from .block_analysis import (
    ANALYSIS_KIND,
    MAX_SCOPE_BYTES,
    AnalysisMapResult,
    AnalysisReduceResult,
    analysis_scope_from_blocks,
    build_analysis_packet,
    build_analysis_scopes,
    discover_analysis_scopes,
    discover_exact_map_attempt,
    discover_exact_reduce_attempt,
    execute_block_analysis_attempt,
    execute_reduce_analysis_attempt,
    persist_reduced_analysis_artifacts,
    prepare_block_analysis_attempt,
    prepare_reduce_analysis_attempt,
    reconcile_block_analysis_attempt,
    reconcile_reduce_analysis_attempt,
    resume_block_analysis_attempt,
    resume_reduce_analysis_attempt,
    whole_document_analysis_scope,
)
from .block_continuation import (
    TRANSLATION_SCOPE_CONTRACT_VERSION,
    normalize_translation_scope,
    verify_protected_targets,
)
from .block_contract import (
    DOCUMENT_BLOCK_ENGINE_CONTRACT,
    BlockContractError,
    canonical_json,
    sha256_text,
)
from .block_persistence import rehydrate_source_plan
from .block_provider_adapter import (
    PROVIDER_DEFAULT_OUTPUT_POLICY,
    call_research,
    call_translation_window,
    validate_research_provider_request,
)
from .block_research import (
    build_block_research_packet,
    materialize_block_research_settings,
    run_block_research,
)
from .block_translation import (
    WindowProfile,
    baseline_slots_for_window_profile,
    build_window_material,
    derive_window_profile,
    discover_exact_attempt,
    plan_ordered_window_materials,
    plan_unissued_tail_materials,
    settings_evidence,
)
from .block_window_runtime import (
    finalize_staged_window_attempt,
    recover_staged_window_attempt,
)
from .block_workflow_config import rehydrate_block_workflow_config
from .context_adapters import (
    _SEGMENT_MEMORY_QUERY_EXCLUDED_ROLES,
    _segment_memory_query_text_is_eligible,
    contextual_memory_slot,
    contextual_segment_memory_slot,
)
from .context_baselines import context_slots_from_baseline, freeze_context_baseline
from .embedding_projection import (
    TranslatorDenseVectorStore,
    active_embedding_generation,
    verify_pinned_embedding_reference,
)
from .evidence_reuse import discover_or_persist_operation_evidence_reuse
from .glossary_persistence import verify_glossary_active
from .models import (
    TranslationAnalysisAttempt,
    TranslationBlockWorkflowConfig,
    TranslationGlossaryActive,
    TranslationGlossaryConcept,
    TranslationOperation,
    TranslationResearchDossierRevision,
    TranslationResearchRun,
    TranslationSourcePlan,
    TranslationWindowAttempt,
)
from .operations import TranslationOperationCancellation
from .passive_window_retry import build_retry_dispatch, retry_dispatch_version
from .research_input_contract import (
    MAX_RESEARCH_INPUT_BYTES,
    RESEARCH_INPUT_PLANNER_CONTRACT_VERSION,
    build_research_source_material,
)
from .research_persistence import ResearchVerificationContext
from .window_contract import TranslationWindow
from .window_execution import (
    execute_window_attempt,
    execution_profile_from_frozen_phase,
    prepare_and_commit_window_attempt,
    reconcile_dispatched_window_attempt,
    resume_window_attempt,
)
from .window_persistence import VerifiedWindowEvidence, rehydrate_attempt_events

_MAX_ANALYSIS_OUTPUT_TOKENS = 16_384
_MAX_RESEARCH_BRIEF_BYTES = 256 * 1024
_MAX_RESEARCH_QUESTION_BYTES = 32 * 1024
_WINDOW_PROVIDER_PACKET_KEYS = frozenset(
    {
        "contract_version",
        "messages",
        "response_format",
        "transport",
        "requested_output_tokens",
        "slots",
    }
)
_WINDOW_DISPATCH_BUDGET = 2
_PARTIAL_RESULT_CONTRACT = "translator-block-partial-v1"
_PARTIAL_RECOVERY_ACTIONS = {
    ("provider_failed", "provider_failed"): "translate_remaining",
    ("normalization_failed", "normalization_failed"): "translate_remaining",
    ("fence_failed", "fence_failed"): "rebase_required",
    ("response_received", "fence_failed"): "rebase_required",
    ("provider_outcome_uncertain", "provider_outcome_uncertain"): "manual_review",
    ("staging_failed", "staging_failed"): "support_required",
}


class BlockWorkflowPartialOutcome(RuntimeError):
    """A safe, durable partial result that the worker must persist as partial."""

    def __init__(
        self,
        *,
        operation: TranslationOperation,
        result: dict[str, object],
        progress: float,
        window_order: int,
    ) -> None:
        self.operation = operation
        self.result = result
        self.progress = progress
        self.window_order = window_order
        super().__init__("block-window-incomplete")


def _fail(code: str) -> BlockContractError:
    return BlockContractError(code)


_flow_logger = logging.getLogger(__name__)


def _observe_phase(
    operation: Any,
    phase: str,
    started: float,
    **counters: object,
) -> None:
    """Emit one correlated metadata-only INFO timing.

    Payload carries operation/phase/duration plus numeric counters and
    stable identifiers (operation, source plan, window order, hashes).
    It never carries source text, glossary entries, packet content,
    receipts, or key material.
    """

    payload: dict[str, object] = {
        "operation_id": getattr(operation, "id", None),
        "phase": phase,
        "duration_ms": round((perf_counter() - started) * 1000, 3),
    }
    payload.update(counters)
    try:
        _flow_logger.info("translator_block_flow %s", canonical_json(payload))
    except Exception:  # noqa: BLE001, S110 - observability never fails translation.
        pass


def _pinned_research_dossier_revision(
    *,
    pin: Mapping[str, Any] | None,
    project: Any,
    operation: TranslationOperation,
    source_plan: TranslationSourcePlan,
) -> TranslationResearchDossierRevision | None:
    """Resolve the frozen dossier identity without inspecting its payload.

    The context-baseline verifier remains responsible for the revision's
    encrypted payload, lineage, and cryptographic/content checks.  This
    boundary only proves that the exact immutable row named by the workflow
    belongs to the operation's project and source plan and still has the
    fields frozen in the config.
    """

    if pin is None:
        return None
    try:
        revision = db.session.get(
            TranslationResearchDossierRevision, pin["revision_id"]
        )
    except Exception as exc:
        raise _fail("block-workflow-dossier") from exc
    if revision is None or not isinstance(revision, TranslationResearchDossierRevision):
        raise _fail("block-workflow-dossier")
    if (
        revision.id != pin["revision_id"]
        or revision.project_id != project.id
        or revision.project_id != source_plan.project_id
        or revision.project_id != operation.project_id
        or revision.source_plan_id != source_plan.id
        or revision.source_plan_id != operation.source_plan_id
        or revision.record_hash != pin["record_hash"]
        or revision.content_digest != pin["content_digest"]
        or revision.conclusion_provenance != pin["conclusion_provenance"]
        or revision.evidence_verification_state
        != pin["evidence_verification_state"]
    ):
        raise _fail("block-workflow-dossier")
    return revision


def _json_bytes(value: object, code: str) -> int:
    try:
        return len(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise _fail(code) from exc


def _translation_provider_packet(
    packet: Mapping[str, Any], *, profile_version: str
) -> dict[str, object]:
    """Project verified evidence onto its versioned provider-call boundary."""

    if set(packet) != _WINDOW_PROVIDER_PACKET_KEYS:
        raise _fail("block-window-provider-packet")
    if profile_version not in {"1", "2", "3", "4", "5"}:
        raise _fail("block-window-provider-packet")
    output_tokens = packet.get("requested_output_tokens")
    if (
        not isinstance(output_tokens, int)
        or isinstance(output_tokens, bool)
        or output_tokens < 1
    ):
        raise _fail("block-window-provider-packet")
    return {
        "messages": packet["messages"],
        "tools": None,
        "response_format": packet["response_format"],
        # Profiles 4 and 5 keep ``requested_output_tokens`` as a conservative
        # context-admission reserve, not a generation instruction. Earlier
        # profiles preserve their historical provider projection for replay.
        "max_completion_tokens": (
            None if profile_version in {"4", "5"} else output_tokens
        ),
    }


def _frozen_phase_settings(
    row: TranslationBlockWorkflowConfig, phase: str
) -> dict[str, object]:
    """Stable, non-secret settings evidence shared by analysis/research."""

    result: dict[str, object] = {
        "contract_version": "document-block-phase-settings-v1",
        "phase": phase,
        "workflow_config_id": row.id,
        "workflow_config_hash": row.config_hash,
        "workflow_config_receipt": row.config_receipt,
    }
    if phase == "pre_analysis":
        phase_config = row.config_json.get("pre_analysis")
        planner_contract = (
            phase_config.get("scope_planner_contract")
            if isinstance(phase_config, Mapping)
            else None
        )
        if planner_contract is not None:
            if planner_contract not in SUPPORTED_ANALYSIS_SCOPE_PLANNER_CONTRACTS:
                raise _fail("block-analysis-scope-planner")
            result["scope_planner_contract"] = planner_contract
    return result


def _analysis_budget(phase: Mapping[str, Any]) -> dict[str, int]:
    model = phase.get("model")
    profile = model.get("capability_profile") if isinstance(model, Mapping) else None
    raw = profile.get("max_output_tokens") if isinstance(profile, Mapping) else None
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1:
        raise _fail("block-analysis-frozen-capability")
    return {"output_tokens": min(raw, _MAX_ANALYSIS_OUTPUT_TOKENS)}


def _analysis_scopes_for_operation(
    *,
    plan: Any,
    operation: TranslationOperation,
    source_plan: TranslationSourcePlan,
    phase: Mapping[str, Any],
    budget: Mapping[str, int],
    token_counter: Callable[..., Any] | None,
) -> tuple[Any, ...]:
    """Prefer one whole-document map, then exact-token complete-block scopes."""

    legacy = build_analysis_scopes(plan)
    existing = TranslationAnalysisAttempt.query.filter_by(
        operation_id=operation.id,
        analysis_kind=ANALYSIS_KIND,
    ).all()
    discovered = discover_analysis_scopes(existing, source_plan)
    if discovered and discovered == legacy[: len(discovered)]:
        # Operations begun before whole-document/token-aware admission must
        # retain their exact historical scope plan.  Do this before current
        # model admission so recovery never depends on changed token metadata.
        return legacy

    model = phase.get("model")
    profile_data = (
        model.get("capability_profile") if isinstance(model, Mapping) else None
    )
    if not isinstance(profile_data, Mapping):
        raise _fail("block-analysis-frozen-capability")
    try:
        profile = ModelCapabilityProfile(**dict(profile_data))
    except (TypeError, ValueError) as exc:
        raise _fail("block-analysis-frozen-capability") from exc

    def admitted(scope: Any) -> bool:
        try:
            packet = build_analysis_packet(plan, scope, phase=phase)
        except BlockContractError as exc:
            if exc.code == "analysis-packet":
                return False
            raise
        messages = packet.get("messages")
        packet_input = packet.get("input")
        if not isinstance(messages, list) or not isinstance(packet_input, Mapping):
            raise _fail("block-analysis-packet")
        response_format = packet_input.get("response_format")
        if response_format is not None and not isinstance(response_format, dict):
            raise _fail("block-analysis-packet")
        result = admit_provider_payload(
            profile=profile,
            messages=[dict(item) for item in messages if isinstance(item, Mapping)],
            response_format=response_format,
            requested_output_tokens=budget["output_tokens"],
            mode="manual",
            token_counter=token_counter,
        )
        return result.admitted

    whole: Any | None = None
    try:
        whole = whole_document_analysis_scope(plan)
    except BlockContractError as exc:
        if exc.code not in {"analysis-scope-blocks", "analysis-scope-size"}:
            raise
    if whole is not None and admitted(whole):
        planned = (whole,)
    else:
        coarse = build_analysis_scopes(
            plan, max_input_bytes=MAX_SCOPE_BYTES, context_bytes=0
        )
        blocks_by_id = {block.id: block for block in plan.blocks}
        packed: list[Any] = []
        for group in coarse:
            group_blocks = tuple(blocks_by_id[block_id] for block_id in group.block_ids)
            start = 0
            while start < len(group_blocks):
                part_index = len(packed) + 1
                end = len(group_blocks)
                candidate = analysis_scope_from_blocks(
                    plan,
                    group_blocks[start:end],
                    part_index=part_index,
                    heading_path=group.heading_path,
                )
                while end > start + 1 and not admitted(candidate):
                    end -= 1
                    candidate = analysis_scope_from_blocks(
                        plan,
                        group_blocks[start:end],
                        part_index=part_index,
                        heading_path=group.heading_path,
                    )
                if not admitted(candidate):
                    raise _fail("analysis-oversized-block")
                packed.append(candidate)
                start = end
        planned = tuple(packed)

    if not discovered:
        return planned
    if discovered == planned[: len(discovered)]:
        return planned
    raise _fail("block-analysis-scope-drift")


def _retry_dispatch_count(
    attempt: Any,
    *,
    code: str,
    session: Any | None = None,
) -> int:
    """Count authenticated provider dispatches in one exact retry lineage."""

    active_session = db.session if session is None else session
    dispatches = 0
    seen: set[str] = set()
    current = attempt
    while current is not None:
        identifier = getattr(current, "id", None)
        if not isinstance(identifier, str) or identifier in seen:
            raise _fail("block-analysis-retry-lineage")
        seen.add(identifier)
        if isinstance(current, TranslationAnalysisAttempt):
            events = rehydrate_analysis_attempt_events(current, session=active_session)
        elif isinstance(current, TranslationWindowAttempt):
            events = rehydrate_attempt_events(current)
        else:
            raise _fail(code)
        dispatches += any(event.event_type == "provider_dispatched" for event in events)
        parent_id = getattr(current, "retry_of_attempt_id", None)
        if parent_id is None:
            break
        current = active_session.get(type(current), parent_id)
        if current is None:
            raise _fail(code)
    return dispatches


def _safe_window_failure(
    *,
    operation: TranslationOperation,
    source_plan: TranslationSourcePlan,
    census: Any,
    workflow: TranslationBlockWorkflowConfig,
    baseline: Any,
    artifacts: Mapping[str, Any],
    research_run: TranslationResearchRun | None,
    evidence_reuse_id: str | None,
    receipts: list[Any],
    attempt: Any,
    window: TranslationWindow,
    failure_kind: str,
    dispatch_state: str | None = None,
    remaining_dispatches: int,
    progress: float,
) -> BlockWorkflowPartialOutcome:
    """Build only the safe projection allowed in a partial result.

    Attempt packets, provider responses, exception details, and research
    content remain in their encrypted evidence tables.  The operation result
    contains IDs/hashes already emitted by a normal completion plus this
    bounded recovery hint.
    """

    state = dispatch_state or failure_kind
    action = _PARTIAL_RECOVERY_ACTIONS.get((state, failure_kind))
    if action is None:
        raise _fail("block-window-failure-kind")
    if not isinstance(remaining_dispatches, int) or isinstance(
        remaining_dispatches, bool
    ):
        raise _fail("block-window-dispatch-budget")
    result: dict[str, object] = {
        "contract_version": _PARTIAL_RESULT_CONTRACT,
        "source_plan_id": source_plan.id,
        "source_plan_hash": source_plan.plan_hash,
        "workflow_config_id": workflow.id,
        "workflow_config_hash": workflow.config_hash,
        "baseline_id": baseline.id,
        "baseline_hash": baseline.record_hash,
        "census_id": census.id,
        "census_hash": census.census_hash,
        "analysis_artifact_ids": {key: value.id for key, value in artifacts.items()},
        "research_run_id": research_run.id if research_run is not None else None,
        "evidence_reuse_id": evidence_reuse_id,
        "window_attempt_ids": [receipt.attempt_id for receipt in receipts],
        "active_revision_ids": [
            value for receipt in receipts for value in receipt.revision_ids
        ],
        "alignment_revision_ids": [
            value for receipt in receipts for value in receipt.alignment_revision_ids
        ],
        "translated_block_count": sum(len(receipt.block_ids) for receipt in receipts),
        "block_failure": {
            "attempt_id": attempt.id,
            "window_order": window.order,
            "affected_block_ids": list(window.block_ids),
            "dispatch_state": state,
            "failure_kind": failure_kind,
            "recovery_action": action,
            "remaining_dispatches": max(
                0, min(_WINDOW_DISPATCH_BUDGET, remaining_dispatches)
            ),
        },
    }
    if isinstance(attempt, TranslationWindowAttempt) and failure_kind == "normalization_failed":
        events = rehydrate_attempt_events(attempt)
        failed_event = next((event for event in reversed(events) if event.event_type == "normalization_failed"), None)
        if failed_event is not None and isinstance(failed_event.payload_json, dict):
            evidence = failed_event.payload_json
            findings = evidence.get("finding_codes")
            codes = [evidence.get("code"), *(findings[:20] if isinstance(findings, list) else [])]
            result["block_failure"]["validation_codes"] = [
                code for code in codes if isinstance(code, str) and re.fullmatch(r"[a-z0-9_-]{1,128}", code)
            ]
            result["block_failure"]["validator"] = "target_response_normalization"
    return BlockWorkflowPartialOutcome(
        operation=operation,
        result=result,
        progress=max(0.0, min(99.0, float(progress))),
        window_order=window.order,
    )


def _refresh_window_attempt(attempt: Any) -> Any:
    """Read durable dispatch state after a provider/staging exception."""

    db.session.rollback()
    db.session.expire_all()
    current = db.session.get(TranslationWindowAttempt, attempt.id)
    return current or attempt


def _refresh_analysis_attempt(attempt: Any) -> Any:
    """Read durable analysis state after a provider/normalization exception."""

    db.session.rollback()
    db.session.expire_all()
    current = db.session.get(TranslationAnalysisAttempt, attempt.id)
    return current or attempt


def _execute_known_provider_attempt(
    attempt: Any, execute: Callable[[], Any]
) -> tuple[Any | None, Exception | None]:
    """Retry only when the independent attempt ledger proves a known failure."""

    try:
        return execute(), None
    except Exception as exc:  # noqa: BLE001, RUF100 - durable state proves whether retry is safe
        current = _refresh_analysis_attempt(attempt)
        if getattr(current, "dispatch_state", None) != "provider_failed":
            raise
        return None, exc


def _raise_analysis_retry_exhausted(last_known_failure: Exception | None) -> None:
    exhausted = _fail("block-analysis-retry-exhausted")
    if last_known_failure is not None:
        raise exhausted from last_known_failure
    raise exhausted


def _settle_map(
    *,
    operation: TranslationOperation,
    source_plan: TranslationSourcePlan,
    scope: Any,
    phase: Mapping[str, Any],
    settings: Mapping[str, Any],
    budget: Mapping[str, Any],
    service: Any,
) -> tuple[AnalysisMapResult, Any]:
    """Recover or issue at most one of the two permitted exact map calls."""

    last_known_failure: Exception | None = None
    while True:
        discovered = discover_exact_map_attempt(
            operation=operation,
            source_plan=source_plan,
            scope=scope,
            phase=phase,
            settings=settings,
            budget=budget,
        )
        attempt = discovered.attempt
        if attempt is None:
            attempt = prepare_block_analysis_attempt(
                operation=operation,
                source_plan=source_plan,
                scope=scope,
                phase=phase,
                settings=settings,
                budget=budget,
            )
        elif discovered.executable_under_current_generation:
            pass
        elif discovered.resumable:
            return resume_block_analysis_attempt(attempt, source_plan), attempt
        elif discovered.reconciliation_required:
            reconcile_block_analysis_attempt(attempt)
            db.session.expire_all()
            continue
        elif attempt.dispatch_state == "prepared":
            # A committed request on a superseded operation generation proves
            # no provider dispatch occurred. Terminalize that exact evidence
            # before creating the bounded retry; never strand it or replay it.
            terminalize_stale_prepared_analysis_attempt(attempt)
            db.session.expire_all()
            continue
        elif discovered.retry_lineage_attempt_id is not None:
            if _retry_dispatch_count(attempt, code="block-analysis-retry-lineage") >= 2:
                _raise_analysis_retry_exhausted(last_known_failure)
            attempt = prepare_block_analysis_attempt(
                operation=operation,
                source_plan=source_plan,
                scope=scope,
                phase=phase,
                settings=settings,
                budget=budget,
                retry_of_attempt_id=discovered.retry_lineage_attempt_id,
            )
        else:
            raise _fail("block-analysis-attempt-state")
        result, failure = _execute_known_provider_attempt(
            attempt,
            lambda attempt=attempt: execute_block_analysis_attempt(
                attempt, provider_service=service
            ),
        )
        if failure is None:
            return cast(AnalysisMapResult, result), attempt
        last_known_failure = failure


def _settle_reduce(
    *,
    operation: TranslationOperation,
    source_plan: TranslationSourcePlan,
    results: tuple[AnalysisMapResult, ...],
    phase: Mapping[str, Any],
    settings: Mapping[str, Any],
    budget: Mapping[str, Any],
    service: Any,
) -> tuple[AnalysisReduceResult, Any]:
    last_known_failure: Exception | None = None
    while True:
        discovered = discover_exact_reduce_attempt(
            operation=operation,
            source_plan=source_plan,
            map_results=results,
            phase=phase,
            settings=settings,
            budget=budget,
        )
        attempt = discovered.attempt
        if attempt is None:
            attempt = prepare_reduce_analysis_attempt(
                operation=operation,
                source_plan=source_plan,
                map_results=results,
                phase=phase,
                settings=settings,
                budget=budget,
            )
        elif discovered.executable_under_current_generation:
            pass
        elif discovered.resumable:
            return resume_reduce_analysis_attempt(attempt, source_plan), attempt
        elif discovered.reconciliation_required:
            reconcile_reduce_analysis_attempt(attempt)
            db.session.expire_all()
            continue
        elif attempt.dispatch_state == "prepared":
            terminalize_stale_prepared_analysis_attempt(attempt)
            db.session.expire_all()
            continue
        elif discovered.retry_lineage_attempt_id is not None:
            if _retry_dispatch_count(attempt, code="block-analysis-retry-lineage") >= 2:
                _raise_analysis_retry_exhausted(last_known_failure)
            attempt = prepare_reduce_analysis_attempt(
                operation=operation,
                source_plan=source_plan,
                map_results=results,
                phase=phase,
                settings=settings,
                budget=budget,
                retry_of_attempt_id=discovered.retry_lineage_attempt_id,
            )
        else:
            raise _fail("block-analysis-attempt-state")
        result, failure = _execute_known_provider_attempt(
            attempt,
            lambda attempt=attempt: execute_reduce_analysis_attempt(
                attempt, provider_service=service
            ),
        )
        if failure is None:
            return cast(AnalysisReduceResult, result), attempt
        last_known_failure = failure


def _research_inputs(
    reduced: AnalysisReduceResult,
) -> tuple[dict[str, object], dict[str, object]]:
    """Pack whole DTO items only; never slice private text to hit a limit."""

    brief: dict[str, object] = {
        "document_brief": reduced.document_brief,
        "style_guide": reduced.style_guide,
        "entity_register": reduced.entity_register,
        "source_plan_hash": reduced.source_plan_hash,
    }
    if _json_bytes(brief, "block-research-brief") > _MAX_RESEARCH_BRIEF_BYTES:
        raise _fail("block-research-brief-too-large")
    unresolved = [dict(item) for item in reduced.unresolved]
    selected: list[dict[str, object]] = []
    for item in unresolved:
        candidate = {
            "task": "Resolve these witnessed translation uncertainties using the selected read-only tools.",
            "unresolved": [*selected, item],
            "omitted_unresolved_count": len(unresolved) - len(selected) - 1,
        }
        if (
            _json_bytes(candidate, "block-research-question")
            > _MAX_RESEARCH_QUESTION_BYTES
        ):
            break
        selected.append(item)
    task = (
        "Resolve these witnessed translation uncertainties using the selected read-only tools."
        if unresolved
        else (
            "Research terminology, entities, genre conventions, cultural or historical context, "
            "references, and translation traps worth establishing before translation. Use at "
            "least one selected read-only tool to seek external or collection evidence; if the "
            "result is irrelevant, record that limitation instead of inventing support."
        )
    )
    question = {
        "task": task,
        "unresolved": selected,
        "omitted_unresolved_count": len(unresolved) - len(selected),
        "require_tool_evidence": not unresolved,
    }
    if (unresolved and not selected) or _json_bytes(
        question, "block-research-question"
    ) > _MAX_RESEARCH_QUESTION_BYTES:
        raise _fail("block-research-question-too-large")
    return brief, question


def _research_source_for_operation(
    *,
    plan: Any,
    phase: Mapping[str, Any],
    semantic_brief: Mapping[str, object],
    research_question: Mapping[str, object],
    token_counter: Callable[..., Any] | None,
) -> tuple[dict[str, object], dict[str, Any]]:
    """Admit the full document or the largest complete-block source prefix."""

    model = phase.get("model")
    profile_data = (
        model.get("capability_profile") if isinstance(model, Mapping) else None
    )
    selected = phase.get("selected")
    if not isinstance(profile_data, Mapping) or not isinstance(selected, Mapping):
        raise _fail("block-research-frozen-capability")
    try:
        profile = ModelCapabilityProfile(**dict(profile_data))
    except (TypeError, ValueError) as exc:
        raise _fail("block-research-frozen-capability") from exc
    declarations = selected.get("tool_declarations")
    budget = selected.get("budget")
    if not isinstance(declarations, list) or not isinstance(budget, Mapping):
        raise _fail("block-research-frozen-selection")
    output_tokens = budget.get("output_tokens")
    if (
        not isinstance(output_tokens, int)
        or isinstance(output_tokens, bool)
        or output_tokens < 1
    ):
        raise _fail("block-research-frozen-selection")
    tools: list[dict[str, Any]] = []
    for item in declarations:
        declaration = item.get("declaration") if isinstance(item, Mapping) else None
        if not isinstance(declaration, Mapping):
            raise _fail("block-research-frozen-selection")
        tools.append(dict(declaration))

    provider_phase = {
        "model": phase["model"],
        "reasoning": phase["reasoning"],
    }

    def candidate(
        block_count: int,
    ) -> tuple[dict[str, object] | None, dict[str, Any] | None]:
        try:
            source = build_research_source_material(
                plan,
                plan.blocks[:block_count],
            )
            packet = build_block_research_packet(
                semantic_brief=semantic_brief,
                question=research_question,
                declarations=cast(list[Mapping[str, Any]], declarations),
                input_limit=MAX_RESEARCH_INPUT_BYTES,
                research_source=source,
            )
        except BlockContractError as exc:
            if exc.code in {"research-source-size", "research-input-limit"}:
                return None, None
            raise
        messages = packet.get("messages")
        if not isinstance(messages, list) or not all(
            isinstance(item, Mapping) for item in messages
        ):
            raise _fail("block-research-packet")
        try:
            request = validate_research_provider_request(
                phase=provider_phase,
                packet={
                    "messages": messages,
                    "tools": tools,
                    "response_format": None,
                    # The durable output value reserves context for admission;
                    # it is not sent as a provider generation ceiling.
                    "max_completion_tokens": None,
                },
            )
        except BlockContractError as exc:
            if exc.code in {
                "provider-packet-json",
                "provider-packet-message",
                "provider-packet-tools",
            }:
                return source, None
            raise
        admitted = admit_provider_payload(
            profile=profile,
            messages=request["messages"],
            tools=request["tools"],
            response_format=request["response_format"],
            extra_parameters={"thinking_config": request["thinking_config"]},
            requested_output_tokens=output_tokens,
            mode="manual",
            token_counter=token_counter,
        )
        if admitted.requested_output_tokens != output_tokens:
            raise _fail("block-research-output-reserve")
        if not admitted.admitted:
            return source, None
        admission_data = admitted.to_dict()
        try:
            validate_research_attempt_packet(
                build_block_research_packet(
                    semantic_brief=semantic_brief,
                    question=research_question,
                    declarations=cast(list[Mapping[str, Any]], declarations),
                    input_limit=MAX_RESEARCH_INPUT_BYTES,
                    research_source=source,
                    provider_admission=admission_data,
                )
            )
        except BlockContractError as exc:
            if exc.code in {"analysis-packet", "analysis-packet-message"}:
                return source, None
            raise
        return source, admission_data

    block_count = len(plan.blocks)
    if block_count < 1:
        raise _fail("block-research-source-empty")
    whole, admission = candidate(block_count)
    if whole is not None and admission is not None:
        return whole, admission

    low = 1
    high = block_count - 1
    best: tuple[dict[str, object], dict[str, Any]] | None = None
    while low <= high:
        middle = (low + high) // 2
        material, admitted = candidate(middle)
        if material is not None and admitted is not None:
            best = (material, admitted)
            low = middle + 1
        else:
            high = middle - 1
    if best is None:
        raise _fail("block-research-oversized-block")
    return best


def _active_locked_glossaries(source_plan: TranslationSourcePlan) -> tuple[Any, ...]:
    rows = (
        db.session.query(TranslationGlossaryActive)
        .join(
            TranslationGlossaryConcept,
            TranslationGlossaryConcept.id == TranslationGlossaryActive.concept_id,
        )
        .filter(
            TranslationGlossaryConcept.project_id == source_plan.project_id,
            TranslationGlossaryConcept.source_plan_id == source_plan.id,
        )
        .all()
    )
    result = []
    for active in rows:
        revision = verify_glossary_active(active)
        if (
            revision.project_id != source_plan.project_id
            or revision.source_plan_id != source_plan.id
            or revision.author_type != "human"
            or revision.revision_state != "locked"
        ):
            raise _fail("block-baseline-glossary")
        result.append(revision)
    return tuple(result)


def _frozen_active_memory_generation(
    *, source_plan: TranslationSourcePlan, config: Mapping[str, Any]
) -> Any | None:
    """Select the current pointer once, before baseline freezing, if it matches.

    Later window construction must use the baseline's historical pointer, not
    this mutable lookup.  A changed embedding configuration merely omits this
    optional enrichment; it cannot silently substitute a different model.
    """

    expected = config.get("embedding_projection")
    if expected is None:
        return None
    if not isinstance(expected, Mapping):
        raise _fail("block-memory-config")
    generation = active_embedding_generation(
        owner_user_id=source_plan.project.user_id,
        project_id=source_plan.project_id,
        source_plan_id=source_plan.id,
        projection_kind="memory",
    )
    if generation is None:
        return None
    fields = (
        "model_record_id",
        "model_id",
        "model_owner_user_id",
        "provider_id",
        "provider_name",
        "provider_kind",
        "api_key_id",
        "api_key_owner_user_id",
        "dimensions",
        "task_type",
        "signature_hash",
        "credential_witness",
        "model_revision",
    )
    if any(getattr(generation, field, None) != expected.get(field) for field in fields):
        return None
    profile = (
        generation.profile_json
        if isinstance(generation.profile_json, Mapping)
        else None
    )
    if (
        profile is None
        or profile.get("class") != expected.get("profile_id")
        or profile.get("contract_version") != expected.get("profile_version")
        or sha256_text(canonical_json(dict(profile))) != expected.get("profile_hash")
    ):
        return None
    for field in ("model_updated_at", "api_key_created_at"):
        value = getattr(generation, field, None)
        observed = (
            value.isoformat(timespec="microseconds") if value is not None else None
        )
        if observed != expected.get(field):
            return None
    model = db.session.get(LLMModel, generation.model_record_id, populate_existing=True)
    provider = db.session.get(
        LLMProvider, generation.provider_id, populate_existing=True
    )
    key = db.session.get(ApiKey, generation.api_key_id, populate_existing=True)
    if (
        model is None
        or provider is None
        or key is None
        or model.user_id != expected.get("model_owner_user_id")
        or getattr(provider, "user_id", None) != expected.get("provider_owner_user_id")
        or key.user_id != expected.get("api_key_owner_user_id")
    ):
        return None
    return generation


def _pinned_memory_callback(
    *, baseline: Any, source_plan: TranslationSourcePlan, config: Mapping[str, Any]
) -> Callable[[tuple[Any, ...]], Any | None] | None:
    """Build a lookup adapter from the already-frozen baseline pointer only."""

    item = next(
        (entry for entry in baseline.items if entry.item_kind == "translation_memory"),
        None,
    )
    if item is None:
        return None
    translation_context = config.get("translation_context")
    use_segment_memory = (
        isinstance(translation_context, Mapping)
        and translation_context.get("contract_version") == "translation-context-v2"
    )
    if use_segment_memory and passive_execution_active():
        return None
    reference = getattr(item, "reference_json", None)
    if not isinstance(reference, Mapping):
        raise _fail("block-memory-baseline")
    pointer = reference.get("pointer")
    if not isinstance(pointer, Mapping):
        raise _fail("block-memory-baseline")
    generation, _pointer = verify_pinned_embedding_reference(
        cast(Mapping[str, Any], pointer)
    )
    if (
        reference.get("generation_id") != generation.id
        or reference.get("generation_record_hash") != generation.record_hash
        or reference.get("model_record_id") != generation.model_record_id
        or reference.get("model_id") != generation.model_id
        or reference.get("provider_id") != generation.provider_id
    ):
        raise _fail("block-memory-baseline")
    model = db.session.get(LLMModel, generation.model_record_id, populate_existing=True)
    if model is None:
        raise _fail("block-memory-model")
    backend = TranslatorDenseVectorStore(
        user_id=generation.owner_user_id,
        model=generation.model_id,
        task_type=generation.task_type,
        dimensions=generation.dimensions,
        model_record=model,
        expected_api_key_id=generation.api_key_id,
        expected_signature_hash=generation.signature_hash,
    )
    backend.namespace = generation.namespace
    if use_segment_memory:
        query_profile = {
            "contract_version": "translation-memory-query-v2",
            "embedding_projection": dict(
                cast(Mapping[str, Any], config["embedding_projection"])
            ),
        }
        # This cache lives only for this callback and its frozen baseline. The
        # conductor skips issued/recovered windows before it invokes the
        # callback, so no provider query is replayed for a retained packet.
        query_cache: dict[tuple[str, str, str, str], Any] = {}

        def resolve_segments(blocks: tuple[Any, ...]) -> Any | None:
            eligible_by_block: list[list[dict[str, Any]]] = []
            for block in sorted(blocks, key=lambda item: item.order):
                if block.role in _SEGMENT_MEMORY_QUERY_EXCLUDED_ROLES:
                    continue
                eligible: list[dict[str, Any]] = []
                for anchor in sorted(block.anchors, key=lambda item: item.order):
                    text = block.projection[anchor.start : anchor.end]
                    if not _segment_memory_query_text_is_eligible(text):
                        continue
                    eligible.append(
                        {
                            "block_id": block.id,
                            "source_anchor_id": anchor.id,
                            "source_sentence_order": anchor.order,
                            "text": text,
                        }
                    )
                if eligible:
                    eligible_by_block.append(eligible)

            selected = [anchors[0] for anchors in eligible_by_block[:6]]
            remaining = [
                anchor for anchors in eligible_by_block for anchor in anchors[1:]
            ]
            selected.extend(remaining[: max(0, 6 - len(selected))])
            return contextual_segment_memory_slot(
                baseline,
                source_plan,
                query_segments=selected,
                query_profile=query_profile,
                backend=backend,
                session=db.session,
                _query_cache=query_cache,
            )

        return resolve_segments

    query_profile = {
        "contract_version": "translation-memory-query-v1",
        "embedding_projection": dict(
            cast(Mapping[str, Any], config["embedding_projection"])
        ),
    }

    def resolve(blocks: tuple[Any, ...]) -> Any | None:
        query = canonical_json(
            [{"block_id": block.id, "projection": block.projection} for block in blocks]
        )
        return contextual_memory_slot(
            baseline,
            source_plan,
            query_text=query,
            query_profile=query_profile,
            top_k=5,
            backend=backend,
            require_memory=False,
        )

    return resolve


def _run_research(
    *,
    operation: TranslationOperation,
    source_plan: TranslationSourcePlan,
    config: Mapping[str, Any],
    workflow: TranslationBlockWorkflowConfig,
    reduced: AnalysisReduceResult,
    service: Any,
    addon_manager: Any,
    plan: Any | None = None,
    token_counter: Callable[..., Any] | None = None,
) -> TranslationResearchRun | None:
    if not config["enable_research"]:
        return None
    phase = cast(Mapping[str, Any], config["research"])
    selected = cast(Mapping[str, Any], phase["selected"])
    provider_phase = {
        "model": phase["model"],
        "reasoning": phase["reasoning"],
    }
    existing = list(
        TranslationResearchRun.query.filter_by(operation_id=operation.id).all()
    )
    if len(existing) > 1:
        raise _fail("block-research-run-ambiguous")
    brief, question = _research_inputs(reduced)
    planner_contract = phase.get("input_planner_contract")
    if planner_contract not in {None, RESEARCH_INPUT_PLANNER_CONTRACT_VERSION}:
        raise _fail("block-research-input-planner")
    research_source = None
    provider_admission = None
    if planner_contract == RESEARCH_INPUT_PLANNER_CONTRACT_VERSION and not existing:
        if plan is None:
            raise _fail("block-research-source-plan")
        research_source, provider_admission = _research_source_for_operation(
            plan=plan,
            phase=phase,
            semantic_brief=brief,
            research_question=question,
            token_counter=token_counter,
        )
    settings_input: dict[str, object] = {
        "contract_version": "document-block-research-settings-v1",
        "workflow_config_id": workflow.id,
        "workflow_config_hash": workflow.config_hash,
        "workflow_config_receipt": workflow.config_receipt,
    }
    if planner_contract == RESEARCH_INPUT_PLANNER_CONTRACT_VERSION:
        settings_input.update(
            {
                "input_planner_contract": planner_contract,
                "research_input_bytes": MAX_RESEARCH_INPUT_BYTES,
            }
        )
    settings = materialize_block_research_settings(settings_input)
    current_profile = {
        "phase": dict(phase),
        "provider_output_policy": PROVIDER_DEFAULT_OUTPUT_POLICY,
    }

    def adapter(**kwargs: Any) -> object:
        expected = {
            "model": phase["model"],
            "settings": settings,
            "budget": {"output_tokens": selected["budget"]["output_tokens"]},
            "provider_max_attempts": 1,
        }
        frozen_profile = kwargs.get("profile")
        legacy_profile = {"phase": dict(phase)}
        if (
            any(kwargs.get(key) != value for key, value in expected.items())
            or frozen_profile not in (legacy_profile, current_profile)
            or not isinstance(kwargs.get("action_id"), str)
            or not kwargs["action_id"]
        ):
            raise _fail("block-research-frozen-adapter")
        output_limit = (
            None
            if frozen_profile == current_profile
            else selected["budget"]["output_tokens"]
        )
        return call_research(
            service,
            phase=provider_phase,
            packet={
                "messages": kwargs["messages"],
                "tools": kwargs["tools"],
                "response_format": None,
                "max_completion_tokens": output_limit,
            },
            action_id=kwargs["action_id"],
        )

    run_block_research(
        enabled=True,
        operation=operation,
        source_plan=source_plan,
        model=phase["model"],
        settings=settings,
        profile=current_profile,
        budget=selected["budget"],
        semantic_brief=brief,
        research_question=question,
        research_source=research_source,
        provider_admission=provider_admission,
        addon_manager=addon_manager,
        model_adapter=adapter,
        tool_declarations=selected["tool_declarations"],
        selected_provider_ids=selected["selected_provider_ids"],
        selected_addon_ids=selected["selected_addon_ids"],
        run=existing[0] if existing else None,
        verified_plan=plan,
    )
    db.session.expire_all()
    durable = TranslationResearchRun.query.filter_by(
        operation_id=operation.id
    ).one_or_none()
    if durable is None or durable.state != "completed":
        raise _fail("block-research-not-completed")
    return durable


def _translation_scope_exclusions(
    *,
    plan_block_ids: frozenset[str],
    translation_scope: Mapping[str, object],
    verified_active_target_ids: frozenset[str],
) -> frozenset[str]:
    """Return blocks omitted from one frozen translation operation."""

    if translation_scope["mode"] != "explicit":
        return verified_active_target_ids
    requested_block_ids = frozenset(cast(list[str], translation_scope["block_ids"]))
    if not requested_block_ids.issubset(plan_block_ids):
        raise _fail("block-translation-scope")
    return plan_block_ids - requested_block_ids


def _durable_issued_windows(
    *,
    operation: TranslationOperation,
    source_plan: TranslationSourcePlan,
) -> tuple[list[TranslationWindow], frozenset[str], int]:
    """Reconstruct every durably issued scope as an immutable fact.

    Each recorded attempt's order, block ids, and fingerprints are reused
    verbatim; issued boundaries are never rederived from today's live
    glossary.  Retries share their window's scope, so scopes are deduplicated
    by block ids.  Returns the recorded windows in order, the covered block
    ids, and the highest recorded order (0 when nothing was issued yet).
    """

    rows = TranslationWindowAttempt.query.filter_by(
        operation_id=operation.id, source_plan_id=source_plan.id
    ).all()
    by_scope: dict[tuple[str, ...], TranslationWindow] = {}
    for row in rows:
        block_ids = row.block_ids_json
        fingerprints = row.block_fingerprints_json
        if (
            not isinstance(row.window_order, int)
            or isinstance(row.window_order, bool)
            or row.window_order < 1
            or not isinstance(block_ids, list)
            or not block_ids
            or not all(isinstance(item, str) and item for item in block_ids)
            or len(set(block_ids)) != len(block_ids)
            or not isinstance(fingerprints, list)
            or len(fingerprints) != len(block_ids)
            or not all(
                isinstance(item, str) and len(item) == 64 for item in fingerprints
            )
        ):
            raise _fail("block-window-issued-scope")
        key = tuple(block_ids)
        candidate = TranslationWindow(
            row.window_order, key, tuple(fingerprints)
        )
        existing = by_scope.get(key)
        if existing is not None:
            # An identical scope reissued (retry) must be the identical
            # record: same order and same fingerprints. A conflicting
            # reissue is corruption, never a second truth to reconcile.
            if (candidate.order != existing.order
                    or candidate.block_fingerprints != existing.block_fingerprints):
                raise _fail("block-window-issued-scope")
            continue
        by_scope[key] = candidate
    ordered = sorted(by_scope.values(), key=lambda item: item.order)
    # Distinct scopes must be disjoint: overlapping scopes would translate a
    # block twice under different orders.
    seen: set[str] = set()
    for item in ordered:
        if any(block_id in seen for block_id in item.block_ids):
            raise _fail("block-window-issued-scope")
        seen.update(item.block_ids)
    # The sequential engine numbers windows contiguously from 1; a gap means
    # a missing or forged record, not a scope to skip over.
    if [item.order for item in ordered] != list(range(1, len(ordered) + 1)):
        raise _fail("block-window-issued-scope")
    covered = frozenset(seen)
    return ordered, covered, len(ordered)


def run_document_block_workflow(
    *,
    project: Any,
    operation: TranslationOperation,
    service: Any,
    addon_manager: Any,
    safe_boundary: Callable[
        [TranslationOperation, float, str, Mapping[str, object]], TranslationOperation
    ],
    cancellation_boundary: Callable[[], None] | None = None,
) -> tuple[TranslationOperation, dict[str, object]]:
    """Run the frozen workflow and return a safe completion DTO to the worker."""

    # Workflow clock starts at true function entry so run_complete covers
    # the full elapsed run including source rehydrate below.
    _run_started = perf_counter()
    _observe_phase(
        operation,
        "run_entry",
        _run_started,
        project_id=getattr(project, "id", None),
    )
    if getattr(project, "engine_contract", None) != DOCUMENT_BLOCK_ENGINE_CONTRACT:
        raise _fail("block-workflow-engine")
    if (
        operation.kind != "document_block_translate"
        or operation.project_id != project.id
    ):
        raise _fail("block-workflow-operation")
    source_plan = db.session.get(TranslationSourcePlan, operation.source_plan_id)
    if source_plan is None:
        raise _fail("block-workflow-source-plan")
    plan = rehydrate_source_plan(source_plan)
    research_verification = ResearchVerificationContext()
    if (
        source_plan.project_id != project.id
        or operation.owner_user_id != project.user_id
        or operation.source_plan_hash != source_plan.plan_hash
        or operation.source_content_hash != source_plan.source_content_hash
        or plan.project_id != project.id
    ):
        raise _fail("block-workflow-source-pin")
    _observe_phase(
        operation,
        "source_rehydrated",
        _run_started,
        source_plan_id=source_plan.id,
        block_count=len(plan.blocks),
        anchor_count=sum(len(block.anchors) for block in plan.blocks),
    )
    rows = TranslationBlockWorkflowConfig.query.filter_by(
        operation_id=operation.id
    ).all()
    if len(rows) != 1:
        raise _fail("block-workflow-config")
    workflow = rehydrate_block_workflow_config(
        rows[0], operation=operation, source_plan=source_plan
    )
    config = dict(workflow.config_json)
    research_dossier_revision = _pinned_research_dossier_revision(
        pin=(
            cast(Mapping[str, Any], config["research_dossier"])
            if "research_dossier" in config
            else None
        ),
        project=project,
        operation=operation,
        source_plan=source_plan,
    )
    if research_dossier_revision is not None:
        # A dossier pin is a complete replacement for managed pre-analysis and
        # research.  Re-check this at the execution boundary in addition to
        # config validation so a future caller cannot accidentally combine the
        # two evidence paths.
        if (
            config["enable_pre_analysis"]
            or config["enable_research"]
            or "pre_analysis" in config
            or "research" in config
            or TranslationResearchRun.query.filter_by(
                operation_id=operation.id
            ).count()
        ):
            raise _fail("block-workflow-dossier")
    verified_active_target_ids = (
        verify_protected_targets(source_plan, config["protected_targets"])
        if "protected_targets" in config
        else frozenset()
    )
    translation_scope = normalize_translation_scope(
        config.get(
            "translation_scope",
            {
                "contract_version": TRANSLATION_SCOPE_CONTRACT_VERSION,
                "mode": "pending",
                "block_ids": [],
            },
        )
    )
    excluded_block_ids = _translation_scope_exclusions(
        plan_block_ids=frozenset(block.id for block in plan.blocks),
        translation_scope=translation_scope,
        verified_active_target_ids=verified_active_target_ids,
    )
    execution = execution_profile_from_frozen_phase(
        cast(Mapping[str, Any], config["translation"])
    )
    frozen_window_profile = WindowProfile(
        **cast(dict[str, Any], config["window_profile"])
    )
    if frozen_window_profile != derive_window_profile(
        execution,
        maximum_blocks=frozen_window_profile.max_blocks,
        profile_version=frozen_window_profile.profile_version,
        target_input_tokens=frozen_window_profile.target_input_tokens,
    ):
        raise _fail("block-workflow-window-profile")
    token_counter = getattr(service, "token_counter", None)
    if not callable(token_counter):
        token_counter = None

    # Census reuse is valid only through the persistence boundary: an
    # unchanged census row is returned solely after full source rehydrate
    # plus occurrence-sequence verification against the live plan identity
    # (plan id/hash, profile id/version, census hash). A matching plan
    # hash alone never skips the rebuild or the verification.
    _census_started = perf_counter()
    census = persist_semantic_census(source_plan, build_semantic_census(plan))
    db.session.commit()
    _observe_phase(
        operation,
        "semantic_census",
        _census_started,
        census_id=census.id,
        census_hash=census.census_hash,
        block_count=len(plan.blocks),
    )
    operation = safe_boundary(
        operation,
        5,
        "Verified semantic source census.",
        {
            "phase": "semantic_census",
            "census_id": census.id,
            "census_hash": census.census_hash,
        },
    )
    artifacts: dict[str, Any] = {}
    reduced: AnalysisReduceResult | None = None
    evidence_reuse = None
    map_attempts: tuple[Any, ...] = ()
    reduce_attempt: Any | None = None
    scopes: tuple[Any, ...] = ()
    if not config["enable_pre_analysis"] and config["enable_research"]:
        raise _fail("block-research-requires-analysis")
    if config["enable_pre_analysis"]:
        phase = cast(Mapping[str, Any], config["pre_analysis"])
        budget = _analysis_budget(phase)
        scopes = _analysis_scopes_for_operation(
            plan=plan,
            operation=operation,
            source_plan=source_plan,
            phase=phase,
            budget=budget,
            token_counter=token_counter,
        )
        evidence_reuse = discover_or_persist_operation_evidence_reuse(
            operation=operation,
            source_plan=source_plan,
            workflow=workflow,
            analysis_scope_hashes=tuple(scope.scope_hash for scope in scopes),
            verified_plan=plan,
            verification_context=research_verification,
        )
        if evidence_reuse is not None:
            artifacts = dict(evidence_reuse.artifacts)
            reduced = evidence_reuse.reduced
            research_run = evidence_reuse.research_run
            db.session.commit()
            operation = safe_boundary(
                operation,
                35,
                "Reused exact completed document analysis.",
                {
                    "phase": "analysis_reuse",
                    "evidence_reuse_id": evidence_reuse.row.id,
                    "origin_operation_id": evidence_reuse.row.origin_operation_id,
                    "artifact_ids": sorted(item.id for item in artifacts.values()),
                },
            )
            if research_run is not None:
                operation = safe_boundary(
                    operation,
                    45,
                    "Reused exact completed research evidence.",
                    {
                        "phase": "research_reuse",
                        "evidence_reuse_id": evidence_reuse.row.id,
                        "research_run_id": research_run.id,
                    },
                )
        else:
            research_run = None
    else:
        research_run = None
    if config["enable_pre_analysis"] and evidence_reuse is None:
        settings = _frozen_phase_settings(workflow, "pre_analysis")
        results: list[AnalysisMapResult] = []
        attempts: list[Any] = []
        for index, scope in enumerate(scopes, 1):
            result, attempt = _settle_map(
                operation=operation,
                source_plan=source_plan,
                scope=scope,
                phase=phase,
                settings=settings,
                budget=budget,
                service=service,
            )
            results.append(result)
            attempts.append(attempt)
            operation = safe_boundary(
                operation,
                5 + 25 * index / len(scopes),
                "Analyzed semantic document context.",
                {
                    "phase": "analysis_map",
                    "completed_scopes": index,
                    "scope_count": len(scopes),
                },
            )
        map_attempts = tuple(attempts)
        reduced, reduce_attempt = _settle_reduce(
            operation=operation,
            source_plan=source_plan,
            results=tuple(results),
            phase=phase,
            settings=settings,
            budget=budget,
            service=service,
        )
        artifacts = persist_reduced_analysis_artifacts(
            source_plan,
            reduced,
            language=config["source_language"],
            attempts=map_attempts,
            reduce_attempt=reduce_attempt,
            phase=phase,
            settings=settings,
        )
        db.session.commit()
        operation = safe_boundary(
            operation,
            35,
            "Consolidated document context.",
            {
                "phase": "analysis_reduce",
                "artifact_ids": sorted(item.id for item in artifacts.values()),
            },
        )
    if reduced is not None and research_run is None:
        research_run = _run_research(
            operation=operation,
            source_plan=source_plan,
            config=config,
            workflow=workflow,
            reduced=reduced,
            service=service,
            addon_manager=addon_manager,
            plan=plan,
            token_counter=token_counter,
        )
        refreshed_operation = db.session.get(
            TranslationOperation, operation.id, populate_existing=True
        )
        if refreshed_operation is None:
            raise _fail("block-workflow-operation")
        operation = refreshed_operation
        if research_run is not None:
            operation = safe_boundary(
                operation,
                45,
                "Completed selected research.",
                {
                    "phase": "research",
                    "research_run_id": research_run.id,
                },
            )

    memory_generation = _frozen_active_memory_generation(
        source_plan=source_plan, config=config
    )
    baseline_kwargs: dict[str, Any] = {
        "artifact_revisions": tuple(artifacts.values()),
        "glossary_revisions": _active_locked_glossaries(source_plan),
        "completed_research_runs": (
            (research_run,) if research_run is not None else ()
        ),
        "active_memory_generation": memory_generation,
        "verified_plan": plan,
        "verification_context": research_verification,
    }
    if research_dossier_revision is not None:
        baseline_kwargs["research_dossier_revision"] = research_dossier_revision
    _baseline_started = perf_counter()
    try:
        baseline = freeze_context_baseline(operation, source_plan, **baseline_kwargs)
    except BlockContractError as exc:
        # The baseline verifier is the final dossier payload/lineage boundary;
        # keep its detailed internal codes out of the workflow API.
        if research_dossier_revision is not None:
            raise _fail("block-workflow-dossier") from exc
        raise
    db.session.commit()
    _observe_phase(
        operation,
        "context_baseline",
        _baseline_started,
        baseline_id=baseline.id,
        baseline_hash=baseline.record_hash,
    )
    baseline_slots = baseline_slots_for_window_profile(
        context_slots_from_baseline(
            baseline,
            source_plan,
            verified_plan=plan,
            verification_context=research_verification,
        ),
        frozen_window_profile,
    )
    memory_for = _pinned_memory_callback(
        baseline=baseline, source_plan=source_plan, config=config
    )
    operation = safe_boundary(
        operation,
        50,
        "Frozen translation context baseline.",
        {
            "phase": "context_baseline",
            "baseline_id": baseline.id,
            "baseline_hash": baseline.record_hash,
        },
    )
    verified_window_evidence = VerifiedWindowEvidence.from_verified_baseline(
        operation=operation,
        source_plan=source_plan,
        plan=plan,
        baseline_slots=baseline_slots,
    )

    _issued_started = perf_counter()
    issued_windows, issued_covered, issued_max_order = _durable_issued_windows(
        operation=operation, source_plan=source_plan
    )
    _observe_phase(
        operation,
        "issued_prefix",
        _issued_started,
        issued_windows=len(issued_windows),
        issued_blocks=len(issued_covered),
        issued_max_order=issued_max_order,
    )

    from .working_glossary import (
        build_glossary_slot,
        glossary_slot_for_window,
        snapshot_current_glossary_entries,
    )

    _glossary_memo: dict[tuple[int, tuple[str, ...]], Any] = {}
    # Scopes with durable attempts are issued: their snapshots always come
    # from the stored packet, never from today's glossary.
    _issued_scopes: set[tuple[str, ...]] = {
        tuple(item.block_ids) for item in issued_windows
    }
    # One verified glossary snapshot per lazy packing pass, reused by every
    # fit probe and by the newly admitted window's materialization in the
    # same safe iteration; refreshed before each lazy pass (hence after each
    # prior submission/continuation).
    _glossary_snapshot: list[Any] = [
        snapshot_current_glossary_entries(source_plan)
    ]

    def live_glossary_for_window(candidate: Any) -> Any:
        """Recorded snapshot for issued scopes, live terminology otherwise.

        Scopes that already have durable attempts reuse their stored glossary
        snapshot, so retry and repack fit estimation reconstructs the recorded
        packets instead of repacking issued scopes from the latest glossary.
        Known-unissued scopes have no attempt rows, so they skip the attempt
        query and build from the pass snapshot. Lookups are memoized per
        scope within one packing pass; the memo is cleared after planning so
        the admitted packet reuses the same pass snapshot via an explicit
        build (never a second live read) in the same safe iteration.
        """

        key = (candidate.order, tuple(candidate.block_ids))
        if key not in _glossary_memo:
            if tuple(candidate.block_ids) in _issued_scopes:
                _glossary_memo[key] = glossary_slot_for_window(
                    operation,
                    source_plan,
                    plan,
                    candidate,
                    verified_evidence=verified_window_evidence,
                )
            else:
                _glossary_memo[key] = build_glossary_slot(
                    source_plan,
                    plan,
                    candidate,
                    entries=_glossary_snapshot[0],
                )
        return _glossary_memo[key]

    # Lazy tail planning: the whole-tail packet matrix is no longer
    # materialized upfront. Exactly the next unissued window is planned on
    # demand inside the loop below (bounded ``max_windows=1``), so a passive
    # submit that parks on the next dispatch pays one window of fit work
    # instead of re-planning every remaining window. The issued prefix stays
    # immutable, orders stay contiguous from 1, and completion is reported
    # only when lightweight block coverage proves no remaining work.
    expected_covered = frozenset(block.id for block in plan.blocks) - set(
        excluded_block_ids
    )
    if not set(issued_covered).issubset(expected_covered):
        raise _fail("block-translation-scope")
    _issued_flat = [block_id for window in issued_windows for block_id in window.block_ids]
    if len(_issued_flat) != len(set(_issued_flat)):
        raise _fail("block-window-issued-scope")
    preliminaries: list[TranslationWindow] = [*issued_windows]
    total_expected_blocks = len(expected_covered)
    next_order = issued_max_order + 1

    def _done_blocks_before(position: int) -> int:
        return sum(len(window.block_ids) for window in preliminaries[:position])

    def _translation_progress_before(position: int) -> float:
        return 50 + 49 * _done_blocks_before(position) / max(
            total_expected_blocks, 1
        )

    def _translation_progress_through(position: int) -> float:
        return 50 + 49 * _done_blocks_before(position + 1) / max(
            total_expected_blocks, 1
        )

    receipts: list[Any] = []
    expected_window_settings = settings_evidence(workflow, frozen_window_profile)

    def dispatch_window(attempt: Any, material: Any) -> None:
        """Issue exactly one provider call for this durable attempt."""
        # Glossary read path: newly prepared packets carry a read-only
        # n -> canonical block_id (+ sentence anchor ids) index as passive
        # dispatch metadata. Frozen packet messages/slots/response schema are
        # unchanged, so replayed or already-leased attempts are unaffected.
        # Old attempts without the index still require outline/fetch lookups.
        try:
            from .working_glossary import window_block_index_for_dispatch

            block_index = window_block_index_for_dispatch(
                source_plan, plan, material.window
            )
        except Exception:
            block_index = None
        context_policy = config.get("translation_context") or {}
        measurement = material.admission.get("measurement") or {}
        dispatch = {
            "translator_context_mode": context_policy.get("continuity_mode", "self_contained"),
            "translator_window_budget": {
                "blocks": len(material.window.block_ids),
                "max_blocks": frozen_window_profile.max_blocks,
                "input_target_tokens": frozen_window_profile.target_input_tokens,
                "estimated_input_tokens": measurement.get("total_input_tokens"),
                "estimated_output_tokens": material.admission.get("translation_batch_estimated_output_tokens"),
                "output_reserve_tokens": material.packet.requested_output_tokens,
            },
        }
        if block_index is not None:
            dispatch["translator_window_index"] = block_index
        if attempt.retry_of_attempt_id is not None:
            current_events = rehydrate_attempt_events(attempt)
            current_event_payloads: tuple[tuple[object, object], ...] = (
                tuple(
                    (event.event_type, event.payload_json)
                    for event in current_events
                )
            )
            try:
                retry_version = retry_dispatch_version(current_event_payloads)
            except ValueError as exc:
                raise _fail("window-retry-version") from exc
            predecessor = db.session.get(
                TranslationWindowAttempt, attempt.retry_of_attempt_id
            )
            if predecessor is None:
                raise _fail("window-retry-predecessor")
            predecessor_events = rehydrate_attempt_events(predecessor)
            if retry_version == 1:
                retry_events: tuple[tuple[str, Mapping[str, object]], ...] = (
                    tuple(
                        (
                            event.event_type,
                            event.payload_json
                            if isinstance(event.payload_json, Mapping)
                            else {},
                        )
                        for event in predecessor_events
                    )
                )
                try:
                    dispatch["translator_window_retry"] = build_retry_dispatch(
                        attempt_id=attempt.id,
                        retry_of_attempt_id=predecessor.id,
                        dispatch_state=predecessor.dispatch_state,
                        events=retry_events,
                    )
                except ValueError as exc:
                    raise _fail("window-retry-metadata") from exc
        with passive_dispatch(dispatch):
            execute_window_attempt(
                attempt,
                operation=operation,
                source_plan=source_plan,
                window=material.window,
                provider_call=lambda packet, attempt_id=attempt.id: call_translation_window(
                    service,
                    phase=config["translation"],
                    packet=_translation_provider_packet(
                        packet,
                        profile_version=frozen_window_profile.profile_version,
                    ),
                    action_id=attempt_id,
                ),
                verified_evidence=verified_window_evidence,
                allow_external_reentry=passive_execution_active(),
            )

    def partial_window(
        attempt: Any,
        *,
        failure_kind: str,
        dispatch_state: str | None = None,
        window: TranslationWindow,
        progress_index: int,
        remaining_dispatches: int,
    ) -> BlockWorkflowPartialOutcome:
        return _safe_window_failure(
            operation=operation,
            source_plan=source_plan,
            census=census,
            workflow=workflow,
            baseline=baseline,
            artifacts=artifacts,
            research_run=research_run,
            evidence_reuse_id=(
                evidence_reuse.row.id if evidence_reuse is not None else None
            ),
            receipts=receipts,
            attempt=attempt,
            window=window,
            failure_kind=failure_kind,
            dispatch_state=dispatch_state,
            remaining_dispatches=remaining_dispatches,
            progress=_translation_progress_before(progress_index - 1),
        )

    pos = 0
    while True:
        if pos >= len(preliminaries):
            planned_ids = frozenset(
                block_id for window in preliminaries for block_id in window.block_ids
            )
            if not planned_ids.issubset(expected_covered):
                raise _fail("block-translation-scope")
            remaining = expected_covered - planned_ids
            if not remaining:
                break
            # Refresh terminology before each lazy packing pass so fit
            # estimation never packs against a stale snapshot when active-mode
            # glossary updates land between windows. The admitted packet below
            # refreshes once more before freezing, preserving the recorded
            # per-window snapshot semantics.
            _glossary_snapshot[0] = snapshot_current_glossary_entries(source_plan)
            _glossary_memo.clear()
            next_fixed = set(excluded_block_ids) | set(planned_ids)
            _plan_started = perf_counter()
            bounded = plan_ordered_window_materials(
                plan=plan,
                source_plan_id=source_plan.id,
                frozen_config=config,
                workflow_config=workflow,
                execution_profile=execution,
                window_profile=frozen_window_profile,
                # Retrieval is deliberately excluded from fit planning.  It is
                # optional evidence and may only be selected after whole windows are
                # fixed; otherwise a provider embedding call would influence packing.
                baseline_slots=baseline_slots,
                live_glossary_for=live_glossary_for_window,
                start_order=next_order,
                token_counter=token_counter,
                excluded_block_ids=tuple(sorted(next_fixed)),
                max_windows=1,
            )
            _glossary_memo.clear()
            if not bounded.materials:
                raise _fail("block-translation-scope")
            _appended_ids = tuple(bounded.materials[0].window.block_ids)
            if (
                not _appended_ids
                or len(set(_appended_ids)) != len(_appended_ids)
                or not set(_appended_ids).issubset(remaining)
            ):
                raise _fail("block-translation-scope")
            preliminaries.append(bounded.materials[0].window)
            _observe_phase(
                operation,
                "next_window_planned",
                _plan_started,
                window_order=bounded.materials[0].window.order,
                window_blocks=len(_appended_ids),
                remaining_blocks=len(remaining) - len(_appended_ids),
            )
            next_order += 1
        index = pos + 1
        preliminary = preliminaries[pos]
        # Planning above is read-only after its committed safety boundary.
        # Recovery/finalization own their replayable transactions and reject
        # adoption of any caller transaction, including flushed writes.
        db.session.rollback()
        _recover_started = perf_counter()
        recovered = recover_staged_window_attempt(
            operation=operation,
            source_plan=source_plan,
            window=preliminary,
            expected_settings=expected_window_settings,
            verified_evidence=verified_window_evidence,
        )
        if recovered is not None:
            db.session.commit()
            receipts.append(recovered)
            _issued_scopes.add(tuple(preliminary.block_ids))
            _observe_phase(
                operation,
                "window_recovered",
                _recover_started,
                window_order=preliminary.order,
                window_blocks=len(preliminary.block_ids),
            )
            # No known-total ``window_count`` is reported until coverage is
            # complete; remaining work is an explicit block remainder.
            _done_after = _done_blocks_before(pos + 1)
            operation = safe_boundary(
                operation,
                _translation_progress_through(pos),
                "Recovered translated semantic blocks.",
                {
                    "phase": "translation",
                    "completed_windows": index,
                    "remaining_blocks": total_expected_blocks - _done_after,
                    "coverage_complete": (total_expected_blocks - _done_after) == 0,
                    "recovered_attempt_id": recovered.attempt_id,
                },
            )
            pos += 1
            continue
        if tuple(preliminary.block_ids) in _issued_scopes:
            live_glossary = live_glossary_for_window(preliminary)
        else:
            # Invocation-scoped reuse: the newly selected window's live
            # snapshot (refreshed once before its lazy fit pass above) is
            # reused for its materialization in this same safe iteration, so
            # fit estimation and the frozen packet share one terminology
            # read. The next lazy pass refreshes after this window's
            # submission/continuation, so no stale glossary survives across
            # windows and no issued packet is rebuilt from live data.
            live_glossary = build_glossary_slot(
                source_plan, plan, preliminary, entries=_glossary_snapshot[0])
        try:
            material = build_window_material(
                plan=plan,
                source_plan_id=source_plan.id,
                window=preliminary,
                live_glossary=live_glossary,
                frozen_config=config,
                workflow_config=workflow,
                execution_profile=execution,
                window_profile=frozen_window_profile,
                baseline_slots=baseline_slots,
                contextual_memory=(
                    memory_for(
                        tuple(
                            block
                            for block in plan.blocks
                            if block.id in preliminary.block_ids
                        )
                    )
                    if memory_for is not None
                    else None
                ),
                source_plan_record=source_plan,
                include_continuity=True,
                token_counter=token_counter,
            )
        except BlockContractError as exc:
            if exc.code != "window-oversized-block":
                raise
            # Dynamic tail repack: live context (glossary growth, continuity,
            # memory, precedent) outgrew this window's packing.  Issued scopes
            # (all durable attempts) plus the already-processed prefix are
            # immutable; only the unprepared remainder is repartitioned with
            # the current glossary in the fit estimation, so only the next
            # unissued batch is repartitioned per tick.  Leased packets are never mutated
            # and selected terminology is never discarded: shrinking the next
            # unissued batch is the only adjustment.
            fixed: set[str] = set(excluded_block_ids)
            for done in preliminaries[:pos]:
                fixed.update(done.block_ids)
            fresh_rows = TranslationWindowAttempt.query.filter_by(
                operation_id=operation.id, source_plan_id=source_plan.id
            ).all()
            fresh_covered = {
                block_id
                for row in fresh_rows
                for block_id in (row.block_ids_json or [])
                if isinstance(block_id, str)
            }
            fresh_orders = [
                row.window_order
                for row in fresh_rows
                if isinstance(row.window_order, int)
                and not isinstance(row.window_order, bool)
                and row.window_order >= 1
            ]
            fixed.update(fresh_covered)
            missing = [
                block_id
                for done in preliminaries[:pos]
                for block_id in done.block_ids
                if block_id not in fresh_covered
            ]
            if missing:
                raise _fail("block-window-repack-prefix")
            repack_start = max(
                [preliminaries[pos - 1].order + 1 if pos else 1]
                + [max(fresh_orders) + 1 if fresh_orders else 1]
            )
            _repack_started = perf_counter()
            new_tail = plan_unissued_tail_materials(
                plan=plan,
                source_plan_id=source_plan.id,
                frozen_config=config,
                workflow_config=workflow,
                execution_profile=execution,
                window_profile=frozen_window_profile,
                baseline_slots=baseline_slots,
                live_glossary_for=live_glossary_for_window,
                # Retry admission must include the context that overflowed final
                # assembly; repeating the cheaper fit can select the same scope.
                source_plan_record=source_plan,
                contextual_memory_for=memory_for,
                start_order=repack_start,
                fixed_block_ids=tuple(sorted(fixed)),
                token_counter=token_counter,
                max_windows=1,
            )
            _glossary_memo.clear()
            # Reuse the repack fit snapshot for the pending materialization
            # in the next iteration: fit and freeze must share one read, and
            # the next lazy pass refreshes after submission/continuation.
            if (
                not new_tail
                or tuple(new_tail[0].window.block_ids)
                == tuple(preliminary.block_ids)
            ):
                raise
            preliminaries[pos:] = [item.window for item in new_tail]
            next_order = repack_start + len(new_tail)
            _observe_phase(
                operation,
                "window_repacked",
                _repack_started,
                window_order=new_tail[0].window.order,
                window_blocks=len(new_tail[0].window.block_ids),
                repack_start=repack_start,
            )
            continue
        while True:
            discovered = discover_exact_attempt(
                operation=operation,
                source_plan=source_plan,
                material=material,
                expected_predecessor=material.predecessor,
                verified_evidence=verified_window_evidence,
            )
            attempt = discovered.attempt
            if attempt is None:
                attempt = prepare_and_commit_window_attempt(
                    operation=operation,
                    source_plan=source_plan,
                    window=material.window,
                    packet=material.packet,
                    settings=material.settings,
                    model_profile=material.model_profile,
                    admission=material.admission,
                    verified_evidence=verified_window_evidence,
                )
                provider_dispatch = True
            elif discovered.executable_under_current_generation:
                provider_dispatch = True
            elif discovered.resumable:
                try:
                    resume_window_attempt(
                        attempt,
                        operation=operation,
                        source_plan=source_plan,
                        window=material.window,
                        verified_evidence=verified_window_evidence,
                    )
                except TranslationOperationCancellation:
                    raise
                except Exception:
                    current = _refresh_window_attempt(attempt)
                    state = current.dispatch_state
                    if state in {"provider_failed", "normalization_failed"}:
                        try:
                            dispatch_count = _retry_dispatch_count(
                                current, code="block-window-retry-lineage"
                            )
                        except BlockContractError:
                            raise partial_window(
                                current,
                                failure_kind=state,
                                window=material.window,
                                progress_index=index,
                                remaining_dispatches=0,
                            )
                        if dispatch_count < _WINDOW_DISPATCH_BUDGET:
                            continue
                    if state in {
                        "provider_failed",
                        "normalization_failed",
                        "fence_failed",
                        "provider_outcome_uncertain",
                        "staging_failed",
                    }:
                        raise partial_window(
                            current,
                            failure_kind=state,
                            window=material.window,
                            progress_index=index,
                            remaining_dispatches=0,
                        )
                    raise
                provider_dispatch = False
            elif discovered.reconciliation_required:
                if passive_execution_active():
                    # A passive work item may have committed immediately before
                    # the worker died, leaving only the generic dispatched state.
                    # Its exact request fingerprint is safe to discover/poll.
                    provider_dispatch = True
                else:
                    reconcile_dispatched_window_attempt(attempt)
                    raise partial_window(
                        attempt,
                        failure_kind="provider_outcome_uncertain",
                        window=material.window,
                        progress_index=index,
                        remaining_dispatches=0,
                    )
            elif (
                attempt is not None
                and attempt.dispatch_state == "provider_outcome_uncertain"
            ):
                raise partial_window(
                    attempt,
                    failure_kind="provider_outcome_uncertain",
                    window=material.window,
                    progress_index=index,
                    remaining_dispatches=0,
                )
            elif attempt is not None and attempt.dispatch_state in {
                "response_received",
                "staging_failed",
            }:
                raise partial_window(
                    attempt,
                    dispatch_state=attempt.dispatch_state,
                    failure_kind=(
                        "fence_failed"
                        if attempt.dispatch_state == "response_received"
                        else "staging_failed"
                    ),
                    window=material.window,
                    progress_index=index,
                    remaining_dispatches=0,
                )
            elif discovered.retry_lineage_attempt_id is not None:
                # Only provider and model-output failures are safe to replay.
                # ``discover_exact_attempt`` also reports fence failures as a
                # retry lineage for historical recovery, but a live conductor
                # must surface those as a rebase requirement instead.
                if attempt.dispatch_state not in {
                    "prepared",
                    "provider_failed",
                    "normalization_failed",
                }:
                    raise partial_window(
                        attempt,
                        failure_kind=(
                            "fence_failed"
                            if attempt.dispatch_state == "fence_failed"
                            else "provider_outcome_uncertain"
                        ),
                        window=material.window,
                        progress_index=index,
                        remaining_dispatches=0,
                    )
                try:
                    dispatch_count = _retry_dispatch_count(
                        attempt, code="block-window-retry-lineage"
                    )
                except BlockContractError:
                    raise partial_window(
                        attempt,
                        failure_kind=attempt.dispatch_state,
                        window=material.window,
                        progress_index=index,
                        remaining_dispatches=0,
                    )
                if dispatch_count >= _WINDOW_DISPATCH_BUDGET:
                    raise partial_window(
                        attempt,
                        failure_kind=attempt.dispatch_state,
                        window=material.window,
                        progress_index=index,
                        remaining_dispatches=0,
                    )
                try:
                    retry = prepare_and_commit_window_attempt(
                        operation=operation,
                        source_plan=source_plan,
                        window=material.window,
                        packet=material.packet,
                        settings=material.settings,
                        model_profile=material.model_profile,
                        admission=material.admission,
                        retry_of_attempt_id=discovered.retry_lineage_attempt_id,
                        verified_evidence=verified_window_evidence,
                    )
                except TranslationOperationCancellation:
                    raise
                except Exception:  # noqa: BLE001 - durable attempt state is the classifier.
                    current = _refresh_window_attempt(attempt)
                    raise partial_window(
                        current,
                        failure_kind=current.dispatch_state,
                        window=material.window,
                        progress_index=index,
                        remaining_dispatches=(_WINDOW_DISPATCH_BUDGET - dispatch_count),
                    )
                attempt = retry
                provider_dispatch = True
            else:
                raise _fail("block-window-attempt-state")
            if provider_dispatch:
                try:
                    dispatch_window(attempt, material)
                except TranslationOperationCancellation:
                    raise
                except Exception:
                    # The provider adapter may raise arbitrary exceptions;
                    # the durable attempt state is the only safe classifier.
                    current = _refresh_window_attempt(attempt)
                    state = current.dispatch_state
                    if state in {"provider_failed", "normalization_failed"}:
                        try:
                            dispatch_count = _retry_dispatch_count(
                                current, code="block-window-retry-lineage"
                            )
                        except BlockContractError:
                            raise partial_window(
                                current,
                                failure_kind=state,
                                window=material.window,
                                progress_index=index,
                                remaining_dispatches=0,
                            )
                        if dispatch_count < _WINDOW_DISPATCH_BUDGET:
                            continue
                        raise partial_window(
                            current,
                            failure_kind=state,
                            window=material.window,
                            progress_index=index,
                            remaining_dispatches=0,
                        )
                    if state in {
                        "fence_failed",
                        "provider_outcome_uncertain",
                        "staging_failed",
                    }:
                        raise partial_window(
                            current,
                            failure_kind=state,
                            window=material.window,
                            progress_index=index,
                            remaining_dispatches=0,
                        )
                    raise
            db.session.expire_all()
            attempt = db.session.get(type(attempt), attempt.id)
            if attempt is None:
                raise _fail("block-window-stage")
            if attempt.dispatch_state != "staged":
                if attempt.dispatch_state in {
                    "fence_failed",
                    "provider_outcome_uncertain",
                    "staging_failed",
                }:
                    raise partial_window(
                        attempt,
                        failure_kind=attempt.dispatch_state,
                        window=material.window,
                        progress_index=index,
                        remaining_dispatches=0,
                    )
                raise _fail("block-window-stage")
            # The state check above is read-only.  The finalizer refuses to
            # roll back a caller transaction because it cannot distinguish an
            # implicit read snapshot from already-flushed caller writes.
            db.session.rollback()
            _finalize_started = perf_counter()
            receipt = finalize_staged_window_attempt(
                attempt,
                operation=operation,
                source_plan=source_plan,
                window=material.window,
                cancellation_boundary=cancellation_boundary,
                verified_evidence=verified_window_evidence,
            )
            db.session.commit()
            receipts.append(receipt)
            _issued_scopes.add(tuple(material.window.block_ids))
            _observe_phase(
                operation,
                "window_finalized",
                _finalize_started,
                window_order=material.window.order,
                window_blocks=len(material.window.block_ids),
            )
            break
        _done_after = _done_blocks_before(pos + 1)
        operation = safe_boundary(
            operation,
            _translation_progress_through(pos),
            "Translated semantic blocks.",
            {
                "phase": "translation",
                "completed_windows": index,
                "remaining_blocks": total_expected_blocks - _done_after,
                "coverage_complete": (total_expected_blocks - _done_after) == 0,
            },
        )
        pos += 1

    final_covered = frozenset(
        block_id for window in preliminaries for block_id in window.block_ids
    )
    if final_covered != expected_covered or sum(
        len(window.block_ids) for window in preliminaries
    ) != len(final_covered):
        raise _fail("block-translation-scope")
    if sum(len(receipt.block_ids) for receipt in receipts) != len(expected_covered):
        raise _fail("block-translation-scope")

    result = {
        "source_plan_id": source_plan.id,
        "source_plan_hash": source_plan.plan_hash,
        "workflow_config_id": workflow.id,
        "workflow_config_hash": workflow.config_hash,
        "baseline_id": baseline.id,
        "baseline_hash": baseline.record_hash,
        "census_id": census.id,
        "census_hash": census.census_hash,
        "analysis_artifact_ids": {key: value.id for key, value in artifacts.items()},
        "research_run_id": research_run.id if research_run is not None else None,
        "evidence_reuse_id": (
            evidence_reuse.row.id if evidence_reuse is not None else None
        ),
        "window_attempt_ids": [receipt.attempt_id for receipt in receipts],
        "active_revision_ids": [
            value for receipt in receipts for value in receipt.revision_ids
        ],
        "alignment_revision_ids": [
            value for receipt in receipts for value in receipt.alignment_revision_ids
        ],
        "translated_block_count": sum(len(receipt.block_ids) for receipt in receipts),
        "protected_block_count": len(
            verified_active_target_ids.intersection(excluded_block_ids)
        ),
    }
    _observe_phase(
        operation,
        "run_complete",
        _run_started,
        translated_blocks=sum(len(receipt.block_ids) for receipt in receipts),
        window_count=len(receipts),
    )
    return operation, result


__all__ = ["BlockWorkflowPartialOutcome", "run_document_block_workflow"]
