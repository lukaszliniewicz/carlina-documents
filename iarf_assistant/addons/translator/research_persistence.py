"""Durable, provider-independent persistence for Translator research turns.

This module deliberately stops at the evidence boundary.  It freezes the
research request, ordered analysis-attempt links, read-only tool declarations,
operation fences, and encrypted tool transcripts.  It never invokes a model,
tool executor, approval path, or external provider.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast
from urllib.parse import unquote

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from sqlalchemy import exists, func, inspect, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import make_transient_to_detached
from sqlalchemy.orm.attributes import set_committed_value

from carlina.core.utc import utc_iso, utc_now_db

from ...database_utils import is_database_locked, run_transaction_with_retry
from ...extensions import db
from .analysis_attempts import (
    AnalysisVerificationContext,
    canonical_analysis_assistant_message,
    persist_analysis_attempt,
    rehydrate_analysis_attempt,
    rehydrate_analysis_attempt_events,
    rehydrate_completed_analysis_output,
)
from .analysis_contract import private_witness, private_witness_candidates
from .block_contract import (
    BlockContractError,
)
from .block_contract import (
    TranslationSourcePlan as CanonicalSourcePlan,
)
from .block_persistence import rehydrate_source_plan
from .integrity_receipts import make_receipt, verify_receipt
from .models import (
    TranslationAnalysisAttempt,
    TranslationAnalysisAttemptEvent,
    TranslationOperation,
    TranslationProject,
    TranslationResearchRun,
    TranslationResearchToolCall,
    TranslationResearchToolCallEvent,
    TranslationResearchToolResultBlob,
    TranslationResearchTurn,
    TranslationSourcePlan,
)
from .operations import renew_operation_lease, update_progress

RESEARCH_CONTRACT_VERSION = "research-run-v1"
RESEARCH_STATES = frozenset(
    {"prepared", "running", "completed", "failed", "cancelled", "uncertain"}
)
TOOL_STATES = frozenset(
    {
        "prepared",
        "executing",
        "succeeded",
        "blocked",
        "failed",
        "uncertain",
        "cancelled",
    }
)
TOOL_EVENT_TYPES = frozenset(TOOL_STATES)
TERMINAL_TOOL_STATES = frozenset(
    {"succeeded", "blocked", "failed", "uncertain", "cancelled"}
)
DETERMINATE_TOOL_STATES = frozenset({"succeeded", "blocked", "failed", "cancelled"})
ACTIVE_ATTEMPT_STATES = frozenset({"prepared", "dispatched", "response_received"})
_MAX_JSON_BYTES = 4 * 1024 * 1024
_MAX_EVENT_CHAIN = 10_000
_MAX_CALLS = 100_000
_MAX_PROVIDER_CALL_ID_BYTES = 16 * 1024
_MAX_IDENTITY_BYTES = 128
_MAX_TOOL_NAME_BYTES = 512
_MAX_TOOL_MESSAGE_BYTES = 16 * 1024
_MAX_ARGUMENT_DEPTH = 128
_MAX_ARGUMENT_NODES = 100_000
_MAX_RESULT_BLOB_BYTES = 64 * 1024 * 1024


@dataclass(slots=True)
class ResearchVerificationContext:
    """Invocation-local cache of fully verified research evidence.

    A fresh context verifies every encrypted record and hash chain. During one
    active run, durable attempt/call receipts invalidate only changed evidence;
    unchanged rows remain detached read-only snapshots across commits. Completed
    graphs may be reused by later steps in the same invocation. Nothing here is
    process-global or durable, so a restart always verifies database truth anew.
    """

    _graphs: dict[tuple[str, str, str, str], dict[str, Any]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _conclusions: dict[tuple[str, str, str, str], dict[str, Any]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    analysis: AnalysisVerificationContext = field(
        default_factory=AnalysisVerificationContext,
        init=False,
        repr=False,
    )
    _scopes: dict[
        str,
        tuple[
            TranslationResearchRun,
            TranslationOperation,
            TranslationSourcePlan,
        ],
    ] = field(default_factory=dict, init=False, repr=False)
    _state_heads: dict[str, dict[str, Any]] = field(
        default_factory=dict, init=False, repr=False
    )
    _state_head_receipts: dict[str, str] = field(
        default_factory=dict, init=False, repr=False
    )
    _run_turn_ids: dict[str, tuple[str, ...]] = field(
        default_factory=dict, init=False, repr=False
    )
    _run_call_ids: dict[str, tuple[str, ...]] = field(
        default_factory=dict, init=False, repr=False
    )
    _turns: dict[str, TranslationResearchTurn] = field(
        default_factory=dict, init=False, repr=False
    )
    _calls: dict[str, TranslationResearchToolCall] = field(
        default_factory=dict, init=False, repr=False
    )
    _call_keys: dict[str, tuple[str, str, str, str, int]] = field(
        default_factory=dict, init=False, repr=False
    )
    _calls_by_turn: dict[str, tuple[TranslationResearchToolCall, ...]] = field(
        default_factory=dict, init=False, repr=False
    )
    _call_event_rows: dict[str, tuple[TranslationResearchToolCallEvent, ...]] = field(
        default_factory=dict, init=False, repr=False
    )
    _verified_call_events: dict[
        tuple[str, str, str, str, int],
        tuple[TranslationResearchToolCallEvent, ...],
    ] = field(default_factory=dict, init=False, repr=False)
    _result_blobs: dict[str, object] = field(
        default_factory=dict, init=False, repr=False
    )
    _verified_calls: dict[
        tuple[str, str, str, str, int],
        tuple[
            TranslationResearchRun,
            TranslationResearchTurn,
            TranslationOperation,
            TranslationSourcePlan,
            tuple[TranslationResearchToolCallEvent, ...],
        ],
    ] = field(default_factory=dict, init=False, repr=False)
    _verified_turns: dict[tuple[Any, ...], dict[str, Any]] = field(
        default_factory=dict, init=False, repr=False
    )
    _verified_histories: set[tuple[Any, ...]] = field(
        default_factory=set, init=False, repr=False
    )


@dataclass(slots=True)
class _ToolReservationContext:
    run: TranslationResearchRun
    turn: TranslationResearchTurn
    plan: TranslationSourcePlan
    completed_output: dict[str, Any]
    declarations: tuple[dict[str, Any], ...]
    provider_ids: frozenset[str]
    addon_ids: frozenset[str]
    calls: list[TranslationResearchToolCall]


def _detached_evidence_snapshot(row: Any) -> Any:
    """Copy loaded columns into a commit-proof, read-only ORM snapshot."""

    mapper = inspect(type(row))
    snapshot = type(row)()
    for attribute in mapper.column_attrs:
        setattr(snapshot, attribute.key, getattr(row, attribute.key))
    make_transient_to_detached(snapshot)
    return snapshot


def _cache_research_scope(
    context: ResearchVerificationContext,
    run: TranslationResearchRun,
    operation: TranslationOperation,
    plan: TranslationSourcePlan,
) -> None:
    """Keep verified scope columns usable across caller-owned commits."""

    project = plan.project
    if project is None:
        raise BlockContractError("research-source-plan")
    plan_snapshot = cast(
        TranslationSourcePlan,
        _detached_evidence_snapshot(plan),
    )
    set_committed_value(
        plan_snapshot,
        "project",
        _detached_evidence_snapshot(project),
    )
    context._scopes[run.id] = (
        cast(TranslationResearchRun, _detached_evidence_snapshot(run)),
        cast(TranslationOperation, _detached_evidence_snapshot(operation)),
        plan_snapshot,
    )


def _verification_key(run: TranslationResearchRun) -> tuple[str, str, str, str]:
    values = (run.id, run.run_record_hash, run.state_head_receipt, run.state)
    if not all(isinstance(value, str) and value for value in values):
        raise BlockContractError("research-run-cache-key")
    return cast(tuple[str, str, str, str], values)


def _call_verification_key(
    call: TranslationResearchToolCall,
) -> tuple[str, str, str, str, int]:
    values = (
        call.id,
        call.call_record_hash,
        call.state_head_receipt,
        call.state,
    )
    if (
        not all(isinstance(value, str) and value for value in values)
        or not isinstance(call.state_version, int)
        or isinstance(call.state_version, bool)
        or call.state_version < 1
    ):
        raise BlockContractError("research-call-cache-key")
    return (*cast(tuple[str, str, str, str], values), call.state_version)


_NO_RESULT_BLOB = object()

_PURPOSES = {
    "model": "translator-research-run-model-v1",
    "settings": "translator-research-run-settings-v1",
    "budget": "translator-research-run-budget-v1",
    "profile": "translator-research-run-profile-v1",
    "providers": "translator-research-run-providers-v1",
    "addons": "translator-research-run-addons-v1",
    "tools": "translator-research-run-tools-v1",
    "lease_fence": "translator-research-operation-fence-v1",
    "call_lease_fence": "translator-research-tool-call-operation-fence-v1",
    "run_record": "translator-research-run-record-v1",
    "run_state": "translator-research-run-state-v1",
    "turn_payload": "translator-research-turn-payload-v1",
    "turn_record": "translator-research-turn-record-v1",
    "declaration": "translator-research-tool-declaration-v1",
    "provider_call_id": "translator-research-provider-call-id-v1",
    "raw_arguments": "translator-research-tool-raw-arguments-v1",
    "arguments": "translator-research-tool-arguments-v1",
    "call_record": "translator-research-tool-call-record-v1",
    "result": "translator-research-tool-result-v1",
    "error": "translator-research-tool-error-v1",
    "tool_message": "translator-research-tool-message-v1",
    "call_state": "translator-research-tool-state-v1",
    "event_payload": "translator-research-tool-event-payload-v1",
    "event": "translator-research-tool-event-v1",
    "result_blob_payload": "translator-research-tool-result-blob-payload-v1",
    "result_blob_record": "translator-research-tool-result-blob-record-v1",
}


def is_admissible_research_tool_history(
    call: TranslationResearchToolCall,
) -> bool:
    """Return whether a settled call may remain in a completed research graph.

    Successful calls contribute external evidence. A definitely failed
    read-only executor call may contribute only its witnessed error feedback,
    allowing the model to try another source; it never masquerades as evidence.
    A call blocked before claim or dispatch may contribute only witnessed
    correction feedback for either an invalid schema or an exhausted batch
    budget. All uncertain, cancelled, active, and other blocked states remain
    fail-closed.
    """

    if call.state == "succeeded":
        return True
    if call.state == "failed":
        error = call.error_json
        return (
            call.claimed_at is not None
            and call.operation_version is not None
            and call.queue_job_id is not None
            and call.operation_lease_expires_at is not None
            and call.lease_fence is not None
            and call.argument_state == "parsed"
            and isinstance(error, dict)
            and error.get("reason")
            in {"tool_executor_failed", "tool_result_evidence_invalid"}
        )
    if (
        call.state != "blocked"
        or call.claimed_at is not None
        or call.operation_version is not None
        or call.queue_job_id is not None
        or call.operation_lease_expires_at is not None
        or call.lease_fence is not None
    ):
        return False
    if call.argument_state == "invalid":
        return True
    error = call.error_json
    return (
        call.argument_state == "parsed"
        and isinstance(error, dict)
        and set(error) == {"reason", "requested", "remaining"}
        and error.get("reason") == "tool_call_budget_exceeded"
        and isinstance(error.get("requested"), int)
        and not isinstance(error.get("requested"), bool)
        and isinstance(error.get("remaining"), int)
        and not isinstance(error.get("remaining"), bool)
        and error["requested"] > error["remaining"] >= 0
    )


def _active_session(session: Any | None = None) -> Any:
    return db.session() if session is None else session


def _reject_dirty_research_uow(session: Any) -> None:
    """Never refresh durable research evidence over caller-owned edits.

    Most verification reads deliberately use ``populate_existing`` so stale
    workers cannot make decisions from an old identity-map snapshot.  Without
    this guard, passing an id instead of an ORM instance could silently erase
    pending caller changes to another row that the verifier refreshes while
    walking the run graph.
    """

    # A nested savepoint flushes the entire SQLAlchemy unit of work, not only
    # Translator rows. Reject every caller-owned pending mutation so the
    # pre-I/O evidence commit cannot accidentally commit unrelated work.
    if session.new or session.dirty or session.deleted:
        raise BlockContractError("research-session-dirty")


def _ensure_transaction(session: Any) -> None:
    """Make nested savepoints atomic on SQLite's deferred transactions."""

    connection = session.connection()
    if connection.dialect.name != "sqlite":
        return
    proxied = connection.connection
    driver = getattr(proxied, "driver_connection", proxied)
    if not bool(getattr(driver, "in_transaction", False)):
        connection.exec_driver_sql("BEGIN")


def _canonical(value: object, code: str, *, maximum: int = _MAX_JSON_BYTES) -> Any:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        restored = json.loads(encoded)
    except (TypeError, ValueError, RecursionError) as exc:
        raise BlockContractError(code) from exc
    if len(encoded) > maximum or restored != value:
        raise BlockContractError(code)
    return restored


def _mapping(value: object, code: str) -> dict[str, Any]:
    material = _canonical(value, code)
    if not isinstance(material, dict):
        raise BlockContractError(code)
    return cast(dict[str, Any], material)


def _research_budget(value: object) -> dict[str, int]:
    material = _mapping(value, "research-budget")
    if set(material) != {"max_model_calls", "max_tool_calls", "output_tokens"}:
        raise BlockContractError("research-budget")
    result: dict[str, int] = {}
    for name, minimum in (
        ("max_model_calls", 1),
        ("max_tool_calls", 0),
        ("output_tokens", 1),
    ):
        item = material[name]
        if not isinstance(item, int) or isinstance(item, bool) or item < minimum:
            raise BlockContractError("research-budget")
        result[name] = item
    if result["max_tool_calls"] > 64:
        raise BlockContractError("research-budget")
    return result


def _strict_json_object(raw: str, code: str) -> dict[str, Any]:
    """Parse provider arguments without changing their exact raw witness."""

    def reject_constant(value: str) -> object:
        raise ValueError(value)

    def reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate-json-key")
            result[key] = value
        return result

    def bounded_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("non-finite-json-number")
        return parsed

    depth = 0
    nodes = 0
    in_string = False
    escaped = False
    for char in raw:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            nodes += 1
            if depth > _MAX_ARGUMENT_DEPTH or nodes > _MAX_ARGUMENT_NODES:
                raise BlockContractError(code)
        elif char in "]}":
            depth -= 1

    try:
        parsed = json.loads(
            raw,
            parse_constant=reject_constant,
            parse_float=bounded_float,
            object_pairs_hook=reject_duplicate_pairs,
        )
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
        raise BlockContractError(code) from exc
    if not isinstance(parsed, dict):
        raise BlockContractError(code)
    return cast(dict[str, Any], _canonical(parsed, code))


def _schema_pointer_target(schema: object, reference: str) -> object:
    if reference == "#":
        return schema
    if not reference.startswith("#/"):
        raise BlockContractError("research-tool-declaration-schema")
    target = schema
    for encoded in unquote(reference[2:]).split("/"):
        token = encoded.replace("~1", "/").replace("~0", "~")
        if isinstance(target, dict) and token in target:
            target = target[token]
        elif isinstance(target, list) and token.isdigit() and int(token) < len(target):
            target = target[int(token)]
        else:
            raise BlockContractError("research-tool-declaration-schema")
    return target


def _validate_local_schema_references(schema: object) -> None:
    """Reject retrieval-capable or dangling references at declaration freeze."""

    anchors: set[str] = set()
    references: list[str] = []
    stack = [schema]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            schema_id = value.get("$id")
            if schema_id is not None and (
                not isinstance(schema_id, str) or not schema_id.startswith("#")
            ):
                raise BlockContractError("research-tool-declaration-schema")
            for key in ("$anchor", "$dynamicAnchor"):
                anchor = value.get(key)
                if anchor is not None:
                    if not isinstance(anchor, str) or not anchor:
                        raise BlockContractError("research-tool-declaration-schema")
                    anchors.add(f"#{anchor}")
            for key in ("$ref", "$dynamicRef"):
                reference = value.get(key)
                if reference is not None:
                    if not isinstance(reference, str) or not reference.startswith("#"):
                        raise BlockContractError("research-tool-declaration-schema")
                    references.append(reference)
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    for reference in references:
        if reference == "#" or reference.startswith("#/"):
            _schema_pointer_target(schema, reference)
        elif reference not in anchors:
            raise BlockContractError("research-tool-declaration-schema")


def _validated_tool_arguments(
    declaration: Mapping[str, Any], raw: str
) -> tuple[dict[str, Any], str, dict[str, Any] | None]:
    """Parse and validate exact provider arguments against the frozen schema."""

    try:
        parsed = _strict_json_object(raw, "research-raw-arguments")
    except BlockContractError as exc:
        return {}, "invalid", {"reason": exc.code}
    declaration_data = _mapping(declaration, "research-tool-declaration")
    provider_declaration = _mapping(
        declaration_data.get("declaration"), "research-tool-declaration"
    )
    function = _mapping(
        provider_declaration.get("function"), "research-tool-declaration"
    )
    schema = _mapping(function.get("parameters"), "research-tool-declaration-schema")
    try:
        validator = Draft202012Validator(schema)
        first_error = min(
            validator.iter_errors(parsed),
            key=lambda error: (
                json.dumps(
                    list(error.absolute_path),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                str(error.validator),
            ),
            default=None,
        )
    except Exception:
        return (
            parsed,
            "invalid",
            {
                "reason": "research-tool-validation-failed",
                "path": [],
                "validator": "unavailable",
            },
        )
    if first_error is None:
        return parsed, "parsed", None
    return (
        parsed,
        "invalid",
        {
            "reason": "research-arguments-schema",
            "path": list(first_error.absolute_path),
            "validator": str(first_error.validator),
        },
    )


def _hash(value: object, code: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise BlockContractError(code)
    return value


def _time(value: object, code: str = "research-time") -> str:
    if not isinstance(value, datetime):
        raise BlockContractError(code)
    return utc_iso(value, timespec="microseconds")


def _witness(value: object, component: str) -> str:
    return private_witness(value, purpose=_PURPOSES[component])


def _witness_matches(value: object, stored: object, component: str) -> bool:
    return isinstance(stored, str) and stored in private_witness_candidates(
        value, purpose=_PURPOSES[component]
    )


def _call_fence_values(
    *,
    operation_id: str,
    operation_version: int,
    queue_job_id: str,
    lease_expires_at: datetime,
) -> dict[str, Any]:
    return {
        "operation_id": operation_id,
        "operation_version": operation_version,
        "queue_job_id": queue_job_id,
        "lease_expires_at": _time(lease_expires_at, "research-lease"),
    }


def _sorted_ids(value: object, code: str) -> list[str]:
    if isinstance(value, tuple):
        value = list(value)
    material = _canonical(value, code)
    if not isinstance(material, list) or any(
        not isinstance(item, str)
        or not item.strip()
        or item != item.strip()
        or len(item.encode("utf-8")) > 128
        for item in material
    ):
        raise BlockContractError(code)
    result = sorted(set(material))
    if len(result) != len(material):
        raise BlockContractError(code)
    return result


def _tool_declarations(
    value: object, code: str = "research-tool-declarations"
) -> list[dict[str, Any]]:
    if isinstance(value, tuple):
        value = list(value)
    material = _canonical(value, code)
    if not isinstance(material, list) or len(material) > 128:
        raise BlockContractError(code)
    result: list[dict[str, Any]] = []
    identities: set[tuple[str, str, str, str, str, str, str]] = set()
    tool_names: set[str] = set()
    for declaration in material:
        item = _mapping(declaration, code)
        if (
            set(item)
            != {
                "tool_name",
                "tool_id",
                "provider_id",
                "provider_domain",
                "addon_id",
                "connection_mode",
                "connection_id",
                "read_only",
                "declaration",
            }
            or item.get("read_only") is not True
        ):
            raise BlockContractError("research-tool-not-read-only")
        declaration = _mapping(item.get("declaration"), code)
        function = declaration.get("function")
        if (
            declaration.get("type") != "function"
            or not isinstance(function, dict)
            or set(function) - {"name", "description", "parameters", "strict"}
            or "name" not in function
            or "parameters" not in function
            or not isinstance(function.get("parameters"), dict)
            or (
                "description" in function
                and not isinstance(function["description"], str)
            )
            or ("strict" in function and not isinstance(function["strict"], bool))
        ):
            raise BlockContractError("research-tool-declaration-shape")
        name = item.get("tool_name")
        if function.get("name") != name:
            raise BlockContractError("research-tool-declaration-identity")
        provider = item.get("provider_id", "")
        provider_domain = item.get("provider_domain", "")
        addon = item.get("addon_id", "")
        tool_id = item.get("tool_id", "")
        connection_mode = item.get("connection_mode", "")
        connection_id = item.get("connection_id", "")
        if (
            not isinstance(name, str)
            or not isinstance(provider, str)
            or not isinstance(provider_domain, str)
            or not isinstance(addon, str)
            or not isinstance(tool_id, str)
            or connection_mode not in {"saved", "connectionless"}
            or not isinstance(connection_id, str)
            or not name.strip()
            or name != name.strip()
            or not name.isprintable()
            or len(name.encode("utf-8")) > _MAX_TOOL_NAME_BYTES
            or not all(
                part.strip()
                and part == part.strip()
                and part.isprintable()
                and len(part.encode("utf-8")) <= _MAX_IDENTITY_BYTES
                for part in (provider, provider_domain, addon, tool_id)
            )
            or connection_id != connection_id.strip()
            or (connection_id and not connection_id.isprintable())
            or len(connection_id.encode("utf-8")) > _MAX_IDENTITY_BYTES
            or (connection_mode == "saved" and not connection_id)
            or (connection_mode == "connectionless" and bool(connection_id))
        ):
            raise BlockContractError("research-tool-declaration-identity")
        identity = (
            name,
            provider,
            provider_domain,
            addon,
            tool_id,
            connection_mode,
            connection_id,
        )
        if identity in identities or name in tool_names:
            raise BlockContractError("research-tool-declaration-duplicate")
        try:
            _validate_local_schema_references(function["parameters"])
            Draft202012Validator.check_schema(function["parameters"])
        except SchemaError as exc:
            raise BlockContractError("research-tool-declaration-schema") from exc
        identities.add(identity)
        tool_names.add(name)
        result.append(item)
    return result


def _durable_scope(
    *,
    operation_id: str,
    source_plan_id: str,
    project_id: str,
    owner_user_id: int,
    source_plan_hash: str,
    source_content_hash: str,
    session: Any,
    require_live: bool,
    verified_plan: CanonicalSourcePlan | None = None,
) -> tuple[TranslationOperation, TranslationSourcePlan]:
    with session.no_autoflush:
        operation = session.get(
            TranslationOperation, operation_id, populate_existing=True
        )
        plan = session.get(
            TranslationSourcePlan, source_plan_id, populate_existing=True
        )
        project = session.get(TranslationProject, project_id, populate_existing=True)
    if operation is None or plan is None or project is None:
        raise BlockContractError("research-scope-missing")
    try:
        if verified_plan is None:
            rehydrate_source_plan(plan)
        elif (
            verified_plan.project_id != plan.project_id
            or verified_plan.plan_hash != plan.plan_hash
            or verified_plan.snapshot.document_id != plan.source_document_id
            or verified_plan.snapshot.content_hash != plan.source_content_hash
        ):
            raise BlockContractError("research-source-plan")
    except Exception as exc:
        raise BlockContractError("research-source-plan") from exc
    if (
        operation.project_id != project_id
        or operation.owner_user_id != owner_user_id
        or operation.source_plan_id != source_plan_id
        or operation.source_plan_hash != source_plan_hash
        or operation.source_content_hash != source_content_hash
        or plan.project_id != project_id
        or plan.plan_hash != source_plan_hash
        or plan.source_content_hash != source_content_hash
        or project.user_id != owner_user_id
        or not isinstance(operation.version, int)
        or isinstance(operation.version, bool)
        or operation.version < 1
        or not isinstance(operation.queue_job_id, str)
        or not operation.queue_job_id.strip()
        or operation.queue_job_id != operation.queue_job_id.strip()
        or len(operation.queue_job_id.encode("utf-8")) > 128
    ):
        raise BlockContractError("research-scope")
    if require_live and (
        operation.state != "running"
        or operation.cancel_requested_at is not None
        or operation.lease_expires_at is None
        or operation.lease_expires_at <= utc_now_db()
    ):
        raise BlockContractError("research-operation-fence")
    return cast(TranslationOperation, operation), cast(TranslationSourcePlan, plan)


def _fence_values(operation: TranslationOperation) -> dict[str, Any]:
    return {
        "operation_id": operation.id,
        "operation_version": operation.version,
        "queue_job_id": operation.queue_job_id,
        "lease_expires_at": _time(operation.lease_expires_at, "research-lease"),
        "lease_fence": _witness(
            {"lease_expires_at": _time(operation.lease_expires_at, "research-lease")},
            "lease_fence",
        ),
    }


def capture_research_operation_fence(
    run: TranslationResearchRun | str,
    *,
    session: Any | None = None,
    verified_plan: CanonicalSourcePlan | None = None,
    verification_context: ResearchVerificationContext | None = None,
) -> dict[str, Any]:
    """Capture the current live-operation fence for one future safe rebase.

    This is deliberately separate from the immutable fence stored on the run.
    The latter attests the original reservation; every later model attempt
    attests its own live fence.  A caller must present this short-lived witness
    back to :func:`rebase_research_operation_lease`, so an old worker cannot
    renew a lease it no longer owns.
    """

    active = _active_session(session)
    durable = _fresh_run(run, active)
    operation, _plan = _verify_run_scope(
        durable,
        active,
        require_live=True,
        verified_plan=verified_plan,
        verification_context=verification_context,
    )
    return _fence_values(operation)


def _require_rebase_fence(
    operation: TranslationOperation,
    expected_fence: Mapping[str, Any],
) -> None:
    """Reject stale, incomplete, or fabricated operation-fence witnesses."""

    candidate = _mapping(expected_fence, "research-rebase-fence")
    expected = _fence_values(operation)
    if set(candidate) != set(expected) or candidate != expected:
        raise BlockContractError("research-rebase-fence")


def _verify_rebase_boundary(
    run: TranslationResearchRun,
    session: Any,
    plan: TranslationSourcePlan,
    *,
    verified_plan: CanonicalSourcePlan | None = None,
    verification_context: ResearchVerificationContext | None = None,
) -> None:
    """Prove no provider-facing child remains active before a lease handoff."""

    turn_ids = (
        verification_context._run_turn_ids.get(run.id)
        if verification_context is not None
        else None
    )
    call_ids = (
        verification_context._run_call_ids.get(run.id)
        if verification_context is not None
        else None
    )
    if (
        verification_context is not None
        and turn_ids is not None
        and call_ids is not None
    ):
        try:
            turns = tuple(verification_context._turns[item] for item in turn_ids)
            calls = tuple(verification_context._calls[item] for item in call_ids)
        except KeyError as exc:
            raise BlockContractError("research-verification-preload") from exc
    else:
        with session.no_autoflush:
            turns = tuple(
                session.query(TranslationResearchTurn)
                .populate_existing()
                .filter_by(run_id=run.id)
                .order_by(TranslationResearchTurn.sequence)
                .all()
            )
            calls = tuple(
                session.query(TranslationResearchToolCall)
                .populate_existing()
                .filter_by(run_id=run.id)
                .order_by(TranslationResearchToolCall.call_index)
                .all()
            )
    if not turns:
        raise BlockContractError("research-rebase-boundary")
    for turn in turns:
        _verify_turn(
            turn,
            run,
            session,
            plan,
            verified_plan=verified_plan,
            verification_context=verification_context,
        )
        attempt = (
            verification_context.analysis._attempts.get(turn.analysis_attempt_id)
            if verification_context is not None
            else None
        )
        if attempt is None:
            attempt = session.get(
                TranslationAnalysisAttempt,
                turn.analysis_attempt_id,
                populate_existing=True,
            )
        if attempt is None:
            raise BlockContractError("research-rebase-active-attempt")
        turn_calls = tuple(call for call in calls if call.turn_id == turn.id)
        if attempt.dispatch_state in {
            "provider_failed",
            "fence_failed",
            "failed",
        }:
            # These states are terminal and are the only failures the research
            # runner may retry.  They have no admissible assistant tool-call
            # output, so any persisted child call would contradict that
            # lineage and must keep the lease handoff closed.
            if turn_calls:
                raise BlockContractError("research-rebase-active-calls")
            continue
        if attempt.dispatch_state != "completed":
            raise BlockContractError("research-rebase-active-attempt")
        assistant = _completed_turn_output(
            turn,
            run,
            session,
            plan,
            verified_plan=verified_plan,
            verification_context=verification_context,
        )
        requested = assistant.get("tool_calls")
        if requested is None:
            requested = []
        if not isinstance(requested, list):
            raise BlockContractError("research-rebase-boundary")
        if len(turn_calls) != len(requested):
            raise BlockContractError("research-rebase-active-calls")
        for call in turn_calls:
            _verify_call(
                call,
                session,
                require_live=False,
                verified_plan=verified_plan,
                verification_context=verification_context,
            )
            if not is_admissible_research_tool_history(call):
                raise BlockContractError("research-rebase-active-calls")


def rebase_research_operation_lease(
    run: TranslationResearchRun | str,
    *,
    expected_fence: Mapping[str, Any],
    progress: float | None = None,
    message: str | None = None,
    checkpoint: Any = None,
    session: Any | None = None,
    verified_plan: CanonicalSourcePlan | None = None,
    verification_context: ResearchVerificationContext | None = None,
) -> dict[str, Any]:
    """Renew an operation only between fully settled research turns.

    Historical research records are immutable evidence and must not be edited
    just because a long quality run needs a new worker lease.  This routine
    proves all existing turns and tool calls are settled, CAS-checks the live
    fence supplied by the worker, then advances the operation ledger.  The
    subsequent reserved attempt captures that new fence before any further
    provider I/O.  Callers must commit this handoff before reserving the next
    turn; after a crash, the completed evidence and advanced operation are
    sufficient to resume without replaying a provider call.

    ``progress`` uses the operation progress transition, which also renews its
    lease.  Otherwise a lease-only renewal is used.  Both paths advance exactly
    one operation version.
    """

    active = _active_session(session)
    durable = _fresh_run(run, active)
    operation, plan = _verify_run_scope(
        durable,
        active,
        require_live=True,
        verified_plan=verified_plan,
        verification_context=verification_context,
    )
    _require_rebase_fence(operation, expected_fence)
    _verify_rebase_boundary(
        durable,
        active,
        plan,
        verified_plan=verified_plan,
        verification_context=verification_context,
    )
    try:
        if progress is None and message is None and checkpoint is None:
            renewed = renew_operation_lease(
                operation, expected_version=operation.version
            )
        else:
            renewed = update_progress(
                operation,
                progress=operation.progress if progress is None else progress,
                message=message,
                checkpoint=checkpoint,
                expected_version=operation.version,
            )
    except Exception as exc:
        # Preserve the operation module's precise conflict/cancellation state
        # for its own callers, while presenting a single fail-closed research
        # contract at this boundary.
        raise BlockContractError("research-rebase-fence") from exc
    return _fence_values(renewed)


def _run_record_values(run: TranslationResearchRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "project_id": run.project_id,
        "owner_user_id": run.owner_user_id,
        "operation_id": run.operation_id,
        "source_plan_id": run.source_plan_id,
        "source_plan_hash": run.source_plan_hash,
        "source_content_hash": run.source_content_hash,
        "contract_version": run.contract_version,
        "operation_version": run.operation_version,
        "queue_job_id": run.queue_job_id,
        "operation_lease_expires_at": _time(
            run.operation_lease_expires_at, "research-lease"
        ),
        "lease_fence": run.lease_fence,
        "model_hash": run.model_hash,
        "settings_hash": run.settings_hash,
        "budget_hash": run.budget_hash,
        "profile_hash": run.profile_hash,
        "providers_hash": run.providers_hash,
        "addons_hash": run.addons_hash,
        "tools_hash": run.tools_hash,
        "created_at": _time(run.created_at, "research-created-at"),
    }


def _turn_record_values(turn: TranslationResearchTurn) -> dict[str, Any]:
    return {
        "id": turn.id,
        "run_id": turn.run_id,
        "project_id": turn.project_id,
        "owner_user_id": turn.owner_user_id,
        "operation_id": turn.operation_id,
        "source_plan_id": turn.source_plan_id,
        "sequence": turn.sequence,
        "analysis_attempt_id": turn.analysis_attempt_id,
        "attempt_record_hash": turn.attempt_record_hash,
        "request_hash": turn.request_hash,
        "created_at": _time(turn.created_at, "research-turn-created-at"),
    }


def _call_record_values(call: TranslationResearchToolCall) -> dict[str, Any]:
    return {
        "id": call.id,
        "run_id": call.run_id,
        "turn_id": call.turn_id,
        "project_id": call.project_id,
        "owner_user_id": call.owner_user_id,
        "operation_id": call.operation_id,
        "source_plan_id": call.source_plan_id,
        "source_plan_hash": call.source_plan_hash,
        "call_index": call.call_index,
        "turn_call_index": call.turn_call_index,
        "provider_call_id": call.provider_call_id,
        "provider_call_key": call.provider_call_key,
        "tool_name": call.tool_name,
        "tool_id": call.tool_id,
        "provider_id": call.provider_id,
        "addon_id": call.addon_id,
        "declaration_hash": call.declaration_hash,
        "raw_arguments_hash": call.raw_arguments_hash,
        "arguments_hash": call.arguments_hash,
        "argument_state": call.argument_state,
        "argument_error": call.argument_error_json,
        "created_at": _time(call.created_at, "research-call-created-at"),
    }


def _result_blob_record_values(
    blob: TranslationResearchToolResultBlob,
) -> dict[str, Any]:
    return {
        "tool_call_id": blob.tool_call_id,
        "run_id": blob.run_id,
        "turn_id": blob.turn_id,
        "project_id": blob.project_id,
        "owner_user_id": blob.owner_user_id,
        "operation_id": blob.operation_id,
        "source_plan_id": blob.source_plan_id,
        "payload_size_bytes": blob.payload_size_bytes,
        "payload_hash": blob.payload_hash,
        "created_at": _time(blob.created_at, "research-result-blob-created-at"),
    }


def _result_blob_ref(
    blob: TranslationResearchToolResultBlob,
) -> dict[str, Any]:
    return {
        "id": f"translator-research-result:{blob.tool_call_id}",
        "kind": "translator_research_result",
        "size_bytes": blob.payload_size_bytes,
        "metadata": {"record_witness": blob.record_hash},
    }


def _event_hash(event: TranslationResearchToolCallEvent) -> str:
    return _witness(
        {
            "id": event.id,
            "tool_call_id": event.tool_call_id,
            "sequence": event.sequence,
            "event_type": event.event_type,
            "payload_hash": event.payload_hash,
            "predecessor_event_hash": event.predecessor_event_hash,
            "created_at": _time(event.created_at, "research-event-created-at"),
        },
        "event",
    )


def _fresh_run(
    run_or_id: TranslationResearchRun | str, session: Any
) -> TranslationResearchRun:
    _reject_dirty_research_uow(session)
    if isinstance(run_or_id, TranslationResearchRun):
        state = inspect(run_or_id)
        if state is None or state.modified or state.deleted:
            raise BlockContractError("research-run-dirty")
    identifier = (
        run_or_id.id if isinstance(run_or_id, TranslationResearchRun) else run_or_id
    )
    if not isinstance(identifier, str) or not identifier:
        raise BlockContractError("research-run-id")
    with session.no_autoflush:
        run = session.get(TranslationResearchRun, identifier, populate_existing=True)
    if run is None:
        raise BlockContractError("research-run-missing")
    return cast(TranslationResearchRun, run)


def _fresh_call(
    call_or_id: TranslationResearchToolCall | str, session: Any
) -> TranslationResearchToolCall:
    _reject_dirty_research_uow(session)
    if isinstance(call_or_id, TranslationResearchToolCall):
        state = inspect(call_or_id)
        if state is None or state.modified or state.deleted:
            raise BlockContractError("research-call-dirty")
    identifier = (
        call_or_id.id
        if isinstance(call_or_id, TranslationResearchToolCall)
        else call_or_id
    )
    if not isinstance(identifier, str) or not identifier:
        raise BlockContractError("research-call-id")
    with session.no_autoflush:
        call = session.get(
            TranslationResearchToolCall, identifier, populate_existing=True
        )
    if call is None:
        raise BlockContractError("research-call-missing")
    return cast(TranslationResearchToolCall, call)


def _verify_run_evidence(run: TranslationResearchRun) -> None:
    for field, hash_field, component in (
        (run.model_json, run.model_hash, "model"),
        (run.settings_json, run.settings_hash, "settings"),
        (run.budget_json, run.budget_hash, "budget"),
        (run.profile_json, run.profile_hash, "profile"),
        (run.selected_provider_ids_json, run.providers_hash, "providers"),
        (run.selected_addon_ids_json, run.addons_hash, "addons"),
        (run.tool_declarations_json, run.tools_hash, "tools"),
    ):
        if not _witness_matches(field, hash_field, component):
            raise BlockContractError("research-run-evidence")
    _research_budget(run.budget_json)
    providers = _sorted_ids(run.selected_provider_ids_json, "research-providers")
    addons = _sorted_ids(run.selected_addon_ids_json, "research-addons")
    declarations = _tool_declarations(run.tool_declarations_json)
    if any(
        declaration["provider_id"] not in providers
        or declaration["addon_id"] not in addons
        for declaration in declarations
    ):
        raise BlockContractError("research-tool-selection")


def _verify_run_scope(
    run: TranslationResearchRun,
    session: Any,
    *,
    require_live: bool = False,
    verified_plan: CanonicalSourcePlan | None = None,
    verification_context: ResearchVerificationContext | None = None,
) -> tuple[TranslationOperation, TranslationSourcePlan]:
    if (
        run.contract_version != RESEARCH_CONTRACT_VERSION
        or run.state not in RESEARCH_STATES
    ):
        raise BlockContractError("research-run-state")
    cached_scope = (
        verification_context._scopes.get(run.id)
        if verification_context is not None
        else None
    )
    if cached_scope is not None:
        cached_run, cached_operation, cached_plan = cached_scope
        cached_operation_state = inspect(cached_operation)
        cached_plan_state = inspect(cached_plan)
        if (
            cached_run.id != run.id
            or cached_run.run_record_hash != run.run_record_hash
            or cached_operation_state is None
            or cached_operation_state.expired
            or cached_plan_state is None
            or cached_plan_state.expired
        ):
            cached_scope = None
    if cached_scope is None:
        operation, plan = _durable_scope(
            operation_id=run.operation_id,
            source_plan_id=run.source_plan_id,
            project_id=run.project_id,
            owner_user_id=run.owner_user_id,
            source_plan_hash=run.source_plan_hash,
            source_content_hash=run.source_content_hash,
            session=session,
            require_live=require_live,
            verified_plan=verified_plan,
        )
    else:
        _cached_run, operation, plan = cached_scope
        if require_live:
            with session.no_autoflush:
                fresh_operation = session.get(
                    TranslationOperation,
                    run.operation_id,
                    populate_existing=True,
                )
            if fresh_operation is None:
                raise BlockContractError("research-scope-missing")
            operation = cast(TranslationOperation, fresh_operation)
        if verified_plan is not None and (
            verified_plan.project_id != plan.project_id
            or verified_plan.plan_hash != plan.plan_hash
            or verified_plan.snapshot.document_id != plan.source_document_id
            or verified_plan.snapshot.content_hash != plan.source_content_hash
        ):
            raise BlockContractError("research-source-plan")
        if (
            operation.project_id != run.project_id
            or operation.owner_user_id != run.owner_user_id
            or operation.source_plan_id != run.source_plan_id
            or operation.source_plan_hash != run.source_plan_hash
            or operation.source_content_hash != run.source_content_hash
            or plan.project_id != run.project_id
            or plan.plan_hash != run.source_plan_hash
            or plan.source_content_hash != run.source_content_hash
            or not isinstance(operation.version, int)
            or isinstance(operation.version, bool)
            or operation.version < 1
            or not isinstance(operation.queue_job_id, str)
            or not operation.queue_job_id.strip()
            or operation.queue_job_id != operation.queue_job_id.strip()
            or len(operation.queue_job_id.encode("utf-8")) > 128
            or (
                require_live
                and (
                    operation.state != "running"
                    or operation.cancel_requested_at is not None
                    or operation.lease_expires_at is None
                    or operation.lease_expires_at <= utc_now_db()
                )
            )
        ):
            raise BlockContractError("research-scope")
    if (
        run.source_plan_hash != plan.plan_hash
        or run.source_content_hash != plan.source_content_hash
        or not _witness_matches(
            {
                "lease_expires_at": _time(
                    run.operation_lease_expires_at, "research-lease"
                )
            },
            run.lease_fence,
            "lease_fence",
        )
        or run.run_record_hash
        not in private_witness_candidates(
            _run_record_values(run), purpose=_PURPOSES["run_record"]
        )
    ):
        raise BlockContractError("research-run-record")
    _verify_run_evidence(run)
    _verify_run_state_head(
        run,
        session,
        verification_context=verification_context,
    )
    return operation, plan


def _state_head_payload(run: TranslationResearchRun, session: Any) -> dict[str, Any]:
    with session.no_autoflush:
        turns = tuple(
            session.query(
                TranslationResearchTurn.sequence,
                TranslationResearchTurn.id,
                TranslationResearchTurn.turn_record_hash,
            )
            .filter_by(run_id=run.id)
            .order_by(TranslationResearchTurn.sequence)
            .all()
        )
        calls = tuple(
            session.query(
                TranslationResearchToolCall.call_index,
                TranslationResearchToolCall.id,
                TranslationResearchToolCall.state_head_receipt,
            )
            .filter_by(run_id=run.id)
            .order_by(TranslationResearchToolCall.call_index)
            .all()
        )
    return {
        "run_id": run.id,
        "state": run.state,
        "state_version": run.state_version,
        "started_at": _time(run.started_at, "research-started-at")
        if run.started_at
        else None,
        "finished_at": _time(run.finished_at, "research-finished-at")
        if run.finished_at
        else None,
        "turns": [
            {"sequence": sequence, "id": turn_id, "receipt": receipt}
            for sequence, turn_id, receipt in turns
        ],
        "tool_calls": [
            {"call_index": call_index, "id": call_id, "state_head": state_head}
            for call_index, call_id, state_head in calls
        ],
    }


def _refresh_run_state_head(run: TranslationResearchRun, session: Any) -> None:
    run.state_head_receipt = make_receipt(
        purpose=_PURPOSES["run_state"], values=_state_head_payload(run, session)
    )
    session.flush()


def _verify_run_state_head(
    run: TranslationResearchRun,
    session: Any,
    *,
    verification_context: ResearchVerificationContext | None = None,
) -> None:
    cached_payload = (
        verification_context._state_heads.get(run.id)
        if verification_context is not None
        and verification_context._state_head_receipts.get(run.id)
        == run.state_head_receipt
        else None
    )
    if cached_payload is not None:
        verify_receipt(
            stored=run.state_head_receipt,
            purpose=_PURPOSES["run_state"],
            values=cached_payload,
            code="research-run-state-head",
        )
        if (
            cached_payload.get("run_id") != run.id
            or cached_payload.get("state") != run.state
            or cached_payload.get("state_version") != run.state_version
            or cached_payload.get("started_at")
            != (
                _time(run.started_at, "research-started-at") if run.started_at else None
            )
            or cached_payload.get("finished_at")
            != (
                _time(run.finished_at, "research-finished-at")
                if run.finished_at
                else None
            )
        ):
            raise BlockContractError("research-run-state-head")
        return
    payload = _state_head_payload(run, session)
    verify_receipt(
        stored=run.state_head_receipt,
        purpose=_PURPOSES["run_state"],
        values=payload,
        code="research-run-state-head",
    )
    if verification_context is not None:
        verification_context._state_heads[run.id] = payload
        verification_context._state_head_receipts[run.id] = run.state_head_receipt


def persist_research_run(
    *,
    operation: TranslationOperation,
    source_plan: TranslationSourcePlan,
    model: Mapping[str, Any],
    settings: Mapping[str, Any],
    budget: Mapping[str, Any],
    profile: Mapping[str, Any] | None = None,
    selected_provider_ids: object = (),
    selected_addon_ids: object = (),
    tool_declarations: object = (),
    session: Any | None = None,
    verified_plan: CanonicalSourcePlan | None = None,
) -> TranslationResearchRun:
    """Persist a frozen research request; callers commit before any I/O."""

    active = _active_session(session)
    _reject_dirty_research_uow(active)
    model_data = _mapping(model, "research-model")
    settings_data = _mapping(settings, "research-settings")
    budget_data = _research_budget(budget)
    profile_data = _mapping({} if profile is None else profile, "research-profile")
    providers_data = _sorted_ids(selected_provider_ids, "research-providers")
    addons_data = _sorted_ids(selected_addon_ids, "research-addons")
    tools_data = _tool_declarations(tool_declarations)
    if any(
        declaration["provider_id"] not in providers_data
        or declaration["addon_id"] not in addons_data
        for declaration in tools_data
    ):
        raise BlockContractError("research-tool-selection")
    source_state = (
        inspect(source_plan) if isinstance(source_plan, TranslationSourcePlan) else None
    )
    operation_state = (
        inspect(operation) if isinstance(operation, TranslationOperation) else None
    )
    if (
        source_state is None
        or not source_state.persistent
        or source_state.session is not active
        or source_state.modified
        or source_state.deleted
    ):
        raise BlockContractError("research-source-plan")
    if (
        operation_state is None
        or not operation_state.persistent
        or operation_state.session is not active
        or operation_state.modified
        or operation_state.deleted
    ):
        raise BlockContractError("research-operation-durable")
    operation, plan = _durable_scope(
        operation_id=operation.id,
        source_plan_id=source_plan.id,
        project_id=source_plan.project_id,
        owner_user_id=operation.owner_user_id,
        source_plan_hash=source_plan.plan_hash,
        source_content_hash=source_plan.source_content_hash,
        session=active,
        require_live=True,
        verified_plan=verified_plan,
    )
    if (
        operation.project_id != plan.project_id
        or operation.owner_user_id != plan.project.user_id
    ):
        raise BlockContractError("research-owner")
    created_at = utc_now_db()
    run = cast(Any, TranslationResearchRun)(
        id=str(uuid.uuid4()),
        project_id=plan.project_id,
        owner_user_id=operation.owner_user_id,
        operation_id=operation.id,
        source_plan_id=plan.id,
        source_plan_hash=plan.plan_hash,
        source_content_hash=plan.source_content_hash,
        contract_version=RESEARCH_CONTRACT_VERSION,
        operation_version=operation.version,
        queue_job_id=operation.queue_job_id,
        operation_lease_expires_at=operation.lease_expires_at,
        lease_fence=_witness(
            {"lease_expires_at": _time(operation.lease_expires_at, "research-lease")},
            "lease_fence",
        ),
        model_json=model_data,
        settings_json=settings_data,
        budget_json=budget_data,
        profile_json=profile_data,
        selected_provider_ids_json=providers_data,
        selected_addon_ids_json=addons_data,
        tool_declarations_json=tools_data,
        model_hash=_witness(model_data, "model"),
        settings_hash=_witness(settings_data, "settings"),
        budget_hash=_witness(budget_data, "budget"),
        profile_hash=_witness(profile_data, "profile"),
        providers_hash=_witness(providers_data, "providers"),
        addons_hash=_witness(addons_data, "addons"),
        tools_hash=_witness(tools_data, "tools"),
        state="prepared",
        state_version=1,
        created_at=created_at,
        run_record_hash="0" * 64,
        state_head_receipt="0" * 64,
    )
    run.run_record_hash = _witness(_run_record_values(run), "run_record")
    active.add(run)
    try:
        _ensure_transaction(active)
        with active.begin_nested():
            active.flush()
            _refresh_run_state_head(run, active)
    except (IntegrityError, OperationalError) as exc:
        raise BlockContractError("research-run-conflict") from exc
    return run


def _verify_attempt_policy(
    attempt_data: Mapping[str, Any],
    run: TranslationResearchRun,
) -> None:
    """Check the attempt's frozen request against the run policy.

    This is deliberately independent of turn state: a turn is a pre-I/O
    reservation and must remain rehydratable while its attempt is prepared,
    dispatched, or terminal for a provider-side reason.
    """

    if (
        attempt_data["model"] != _mapping(run.model_json, "research-model")
        or attempt_data["profile"] != _mapping(run.profile_json, "research-profile")
        or attempt_data["settings"] != _mapping(run.settings_json, "research-settings")
        or attempt_data["tool_declarations"]
        != [
            item["declaration"]
            for item in _tool_declarations(run.tool_declarations_json)
        ]
        or attempt_data["budget"]
        != {"output_tokens": _research_budget(run.budget_json)["output_tokens"]}
    ):
        raise BlockContractError("research-turn-policy")


def _verify_turn(
    turn: TranslationResearchTurn,
    run: TranslationResearchRun,
    session: Any,
    plan: TranslationSourcePlan,
    *,
    verified_plan: CanonicalSourcePlan | None = None,
    verification_context: ResearchVerificationContext | None = None,
) -> dict[str, Any]:
    if isinstance(turn, TranslationResearchTurn):
        state = inspect(turn)
        if state is None or state.modified or state.deleted:
            raise BlockContractError("research-turn-dirty")
    if (
        turn.run_id != run.id
        or turn.project_id != run.project_id
        or turn.owner_user_id != run.owner_user_id
        or turn.operation_id != run.operation_id
        or turn.source_plan_id != run.source_plan_id
        or turn.sequence < 1
        or turn.turn_record_hash
        not in private_witness_candidates(
            _turn_record_values(turn), purpose=_PURPOSES["turn_record"]
        )
    ):
        raise BlockContractError("research-turn-record")
    attempt = (
        verification_context.analysis._attempts.get(turn.analysis_attempt_id)
        if verification_context is not None
        else None
    )
    if attempt is None:
        with session.no_autoflush:
            attempt = session.get(
                TranslationAnalysisAttempt,
                turn.analysis_attempt_id,
                populate_existing=True,
            )
    if attempt is None:
        raise BlockContractError("research-turn-attempt")
    if verification_context is not None:
        attempt_state = inspect(attempt)
        if attempt_state is None or attempt_state.modified or attempt_state.deleted:
            raise BlockContractError("research-turn-attempt")
        turn_cache_key = (
            *tuple(_turn_record_values(turn).values()),
            turn.turn_record_hash,
            run.id,
            run.project_id,
            run.owner_user_id,
            run.operation_id,
            run.source_plan_id,
            run.source_plan_hash,
            run.source_content_hash,
            run.model_hash,
            run.settings_hash,
            run.budget_hash,
            run.profile_hash,
            run.tools_hash,
            attempt.id,
            attempt.attempt_record_hash,
            attempt.dispatch_receipt_hash,
            attempt.dispatch_state,
            attempt.dispatch_version,
        )
        cached_turn = verification_context._verified_turns.get(turn_cache_key)
        if cached_turn is not None:
            return dict(cached_turn)
    else:
        turn_cache_key = None
    if (
        attempt.analysis_kind != "research"
        or attempt.contract_version != "analysis-attempt-v2"
        or attempt.dispatch_state
        not in {
            "prepared",
            "dispatched",
            "response_received",
            "completed",
            "provider_failed",
            "provider_outcome_uncertain",
            "fence_failed",
            "cancelled",
            "failed",
        }
        or attempt.project_id != run.project_id
        or attempt.owner_user_id != run.owner_user_id
        or attempt.operation_id != run.operation_id
        or attempt.source_plan_id != run.source_plan_id
        or attempt.attempt_record_hash != turn.attempt_record_hash
        or attempt.request_hash != turn.request_hash
    ):
        raise BlockContractError("research-turn-attempt")
    attempt_data = rehydrate_analysis_attempt(
        attempt,
        plan,
        session=session,
        verified_plan=verified_plan,
        verification_context=(
            verification_context.analysis if verification_context is not None else None
        ),
    )
    _verify_attempt_policy(attempt_data, run)
    if verification_context is not None and turn_cache_key is not None:
        verification_context._verified_turns[turn_cache_key] = dict(attempt_data)
    return attempt_data


def _completed_turn_output(
    turn: TranslationResearchTurn,
    run: TranslationResearchRun,
    session: Any,
    plan: TranslationSourcePlan,
    *,
    verified_plan: CanonicalSourcePlan | None = None,
    verification_context: ResearchVerificationContext | None = None,
) -> dict[str, Any]:
    """Return the exact assistant output, only after completion is witnessed."""

    _verify_turn(
        turn,
        run,
        session,
        plan,
        verified_plan=verified_plan,
        verification_context=verification_context,
    )
    attempt = (
        verification_context.analysis._attempts.get(turn.analysis_attempt_id)
        if verification_context is not None
        else None
    )
    if attempt is None:
        with session.no_autoflush:
            attempt = session.get(
                TranslationAnalysisAttempt,
                turn.analysis_attempt_id,
                populate_existing=True,
            )
    if attempt is None:
        raise BlockContractError("research-turn-attempt")
    return canonical_analysis_assistant_message(
        rehydrate_completed_analysis_output(
            attempt,
            plan,
            session=session,
            verified_plan=verified_plan,
            verification_context=(
                verification_context.analysis
                if verification_context is not None
                else None
            ),
        )
    )


def _tool_message_content_values(
    state: str,
    result: object | None,
    error: object | None,
    result_hash: str | None,
    error_hash: str | None,
) -> str:
    if state == "succeeded":
        value: dict[str, Any] = {
            "state": state,
            "result": _canonical(result, "research-result"),
        }
    elif state in {"blocked", "failed", "uncertain", "cancelled"}:
        value = {"state": state, "error": _mapping(error, "research-error")}
    else:
        raise BlockContractError("research-history-active-call")
    try:
        content = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise BlockContractError("research-tool-message") from exc
    if len(content.encode("utf-8")) <= _MAX_TOOL_MESSAGE_BYTES:
        return content
    evidence_hash = result_hash or error_hash
    if evidence_hash is None:
        raise BlockContractError("research-tool-message")
    preview_limit = _MAX_TOOL_MESSAGE_BYTES // 2
    while preview_limit > 0:
        projection = {
            "state": state,
            "bytes": len(content.encode("utf-8")),
            "truncated": True,
            "preview": content[:preview_limit],
            "evidence_witness": evidence_hash,
        }
        projected = json.dumps(
            projection,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(projected.encode("utf-8")) <= _MAX_TOOL_MESSAGE_BYTES:
            return projected
        preview_limit //= 2
    raise BlockContractError("research-tool-message-limit")


def _tool_message_content(call: TranslationResearchToolCall) -> str:
    return _tool_message_content_values(
        call.state,
        call.result_json,
        call.error_json,
        call.result_hash,
        call.error_hash,
    )


def _verify_next_turn_history(
    run: TranslationResearchRun,
    attempt: TranslationAnalysisAttempt,
    attempt_data: Mapping[str, Any],
    prior_turns: tuple[TranslationResearchTurn, ...],
    session: Any,
    plan: TranslationSourcePlan,
    *,
    verified_plan: CanonicalSourcePlan | None = None,
    verification_context: ResearchVerificationContext | None = None,
) -> None:
    messages = attempt_data.get("messages")
    if not isinstance(messages, list):
        raise BlockContractError("research-turn-history")
    history_key: tuple[Any, ...] | None = None
    if not prior_turns:
        if verification_context is not None:
            current_key = verification_context.analysis._attempt_keys.get(attempt.id)
            if current_key is not None:
                history_key = (run.id, run.run_record_hash, current_key, None, ())
                if history_key in verification_context._verified_histories:
                    return
        if (
            attempt.retry_of_attempt_id is not None
            or attempt.rebase_of_attempt_id is not None
            or not messages
            or any(
                not isinstance(message, dict)
                or message.get("role") not in {"system", "user"}
                for message in messages
            )
        ):
            raise BlockContractError("research-turn-history")
        if history_key is not None:
            verification_context._verified_histories.add(history_key)
        return
    previous = prior_turns[-1]
    previous_attempt = (
        verification_context.analysis._attempts.get(previous.analysis_attempt_id)
        if verification_context is not None
        else None
    )
    calls = (
        verification_context._calls_by_turn.get(previous.id)
        if verification_context is not None
        else None
    )
    with session.no_autoflush:
        if previous_attempt is None:
            previous_attempt = session.get(
                TranslationAnalysisAttempt,
                previous.analysis_attempt_id,
                populate_existing=True,
            )
        if calls is None:
            calls = tuple(
                session.query(TranslationResearchToolCall)
                .populate_existing()
                .filter_by(run_id=run.id, turn_id=previous.id)
                .order_by(TranslationResearchToolCall.call_index)
                .all()
            )
    if previous_attempt is None:
        raise BlockContractError("research-turn-history")
    if verification_context is not None:
        current_key = verification_context.analysis._attempt_keys.get(attempt.id)
        previous_key = verification_context.analysis._attempt_keys.get(
            previous_attempt.id
        )
        call_keys = tuple(
            verification_context._call_keys.get(call.id) for call in calls
        )
        if (
            current_key is not None
            and previous_key is not None
            and all(call_key is not None for call_key in call_keys)
        ):
            history_key = (
                run.id,
                run.run_record_hash,
                current_key,
                previous_key,
                call_keys,
            )
            if history_key in verification_context._verified_histories:
                return
    previous_packet = rehydrate_analysis_attempt(
        previous_attempt,
        plan,
        session=session,
        verified_plan=verified_plan,
        verification_context=(
            verification_context.analysis if verification_context is not None else None
        ),
    )
    previous_state = previous_attempt.dispatch_state
    if previous_state == "provider_outcome_uncertain":
        # There is no truthful way to extend or automatically replay an
        # outcome whose provider-side effect is unknown.
        raise BlockContractError("research-turn-history-uncertain")
    if previous_state in {"provider_failed", "fence_failed", "cancelled", "failed"}:
        if (
            attempt.retry_of_attempt_id != previous_attempt.id
            or attempt.rebase_of_attempt_id is not None
            or attempt_data["messages"] != previous_packet["messages"]
            or attempt_data["input"] != previous_packet["input"]
        ):
            raise BlockContractError("research-turn-lineage")
        if history_key is not None:
            verification_context._verified_histories.add(history_key)
        return
    if previous_state != "completed":
        raise BlockContractError("research-turn-history-active")
    assistant = _completed_turn_output(
        previous,
        run,
        session,
        plan,
        verified_plan=verified_plan,
        verification_context=verification_context,
    )
    expected = list(previous_packet["messages"])
    expected.append(assistant)
    assistant_calls = assistant.get("tool_calls")
    if not isinstance(assistant_calls, list):
        if calls:
            raise BlockContractError("research-turn-history")
    elif len(calls) != len(assistant_calls):
        raise BlockContractError("research-turn-history")
    for ordinal, (assistant_call, call) in enumerate(
        zip(assistant_calls or [], calls, strict=True), start=1
    ):
        _verify_call(
            call,
            session,
            require_live=False,
            verified_plan=verified_plan,
            verification_context=verification_context,
        )
        function = assistant_call.get("function")
        if (
            call.turn_call_index != ordinal
            or call.state not in {"succeeded", "blocked", "failed", "cancelled"}
            or not isinstance(function, dict)
            or assistant_call.get("id") != call.provider_call_id
            or function.get("name") != call.tool_name
            or function.get("arguments") != call.raw_arguments_json
        ):
            raise BlockContractError("research-turn-history")
        expected.append(
            {
                "role": "tool",
                "tool_call_id": call.provider_call_id,
                "name": call.tool_name,
                "content": call.tool_message_content_json,
            }
        )
    if (
        attempt.rebase_of_attempt_id != previous_attempt.id
        or attempt.retry_of_attempt_id is not None
        or attempt_data["input"] != previous_packet["input"]
        or messages != expected
    ):
        raise BlockContractError("research-turn-lineage")
    if history_key is not None:
        verification_context._verified_histories.add(history_key)


def _append_research_turn(
    run: TranslationResearchRun,
    attempt: TranslationAnalysisAttempt,
    turn_payload: object | None = None,
    *,
    sequence: int | None = None,
    session: Any | None = None,
    verified_plan: CanonicalSourcePlan | None = None,
    verification_context: ResearchVerificationContext | None = None,
) -> TranslationResearchTurn:
    """Reserve the next model turn before provider I/O.

    The attempt and turn must remain in one caller-owned transaction.  A
    non-null ``turn_payload`` is rejected because provider output belongs to
    the attempt transcript and does not exist at reservation time.
    """

    active = _active_session(session)
    durable_run = _fresh_run(run, active)
    operation, plan = _verify_run_scope(
        durable_run,
        active,
        require_live=True,
        verified_plan=verified_plan,
        verification_context=verification_context,
    )
    if durable_run.state not in {"prepared", "running"}:
        raise BlockContractError("research-run-terminal")
    if turn_payload is not None:
        raise BlockContractError("research-turn-payload-unsupported")
    attempt_id = attempt.id if isinstance(attempt, TranslationAnalysisAttempt) else None
    if not isinstance(attempt_id, str):
        raise BlockContractError("research-attempt")
    if isinstance(attempt, TranslationAnalysisAttempt):
        attempt_state = inspect(attempt)
        if attempt_state is None or attempt_state.modified or attempt_state.deleted:
            raise BlockContractError("research-attempt-dirty")
    with active.no_autoflush:
        durable_attempt = active.get(
            TranslationAnalysisAttempt, attempt_id, populate_existing=True
        )
    if durable_attempt is None:
        raise BlockContractError("research-attempt")
    attempt_data = rehydrate_analysis_attempt(
        durable_attempt,
        plan,
        session=active,
        verified_plan=verified_plan,
        verification_context=(
            verification_context.analysis if verification_context is not None else None
        ),
        cache_result=False,
    )
    if (
        durable_attempt.analysis_kind != "research"
        or durable_attempt.contract_version != "analysis-attempt-v2"
        or durable_attempt.dispatch_state != "prepared"
        or durable_attempt.project_id != durable_run.project_id
        or durable_attempt.owner_user_id != durable_run.owner_user_id
        or durable_attempt.operation_id != durable_run.operation_id
        or durable_attempt.source_plan_id != durable_run.source_plan_id
        or durable_attempt.operation_version != operation.version
        or durable_attempt.queue_job_id != operation.queue_job_id
        or durable_attempt.operation_lease_expires_at != operation.lease_expires_at
        or not isinstance(durable_attempt.attempt_record_hash, str)
        or not isinstance(durable_attempt.request_hash, str)
    ):
        raise BlockContractError("research-attempt-scope")
    _verify_attempt_policy(attempt_data, durable_run)
    prior_turn_ids = (
        verification_context._run_turn_ids.get(durable_run.id)
        if verification_context is not None
        else None
    )
    if prior_turn_ids is None:
        with active.no_autoflush:
            prior_turns = tuple(
                active.query(TranslationResearchTurn)
                .populate_existing()
                .filter_by(run_id=durable_run.id)
                .order_by(TranslationResearchTurn.sequence)
                .all()
            )
    else:
        try:
            prior_turns = tuple(
                verification_context._turns[turn_id] for turn_id in prior_turn_ids
            )
        except KeyError as exc:
            raise BlockContractError("research-verification-preload") from exc
    _verify_next_turn_history(
        durable_run,
        durable_attempt,
        attempt_data,
        prior_turns,
        active,
        plan,
        verified_plan=verified_plan,
        verification_context=verification_context,
    )
    expected = (prior_turns[-1].sequence + 1) if prior_turns else 1
    if (
        _verified_model_dispatch_count(
            durable_run,
            prior_turns,
            active,
            verified_plan=verified_plan,
            verification_context=verification_context,
        )
        >= (_research_budget(durable_run.budget_json)["max_model_calls"])
    ):
        raise BlockContractError("research-model-call-limit")
    if sequence is not None and (
        not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence != expected
    ):
        raise BlockContractError("research-turn-sequence")
    created_at = utc_now_db()
    turn = cast(Any, TranslationResearchTurn)(
        id=str(uuid.uuid4()),
        run_id=durable_run.id,
        project_id=durable_run.project_id,
        owner_user_id=durable_run.owner_user_id,
        operation_id=durable_run.operation_id,
        source_plan_id=durable_run.source_plan_id,
        sequence=expected,
        analysis_attempt_id=durable_attempt.id,
        attempt_record_hash=durable_attempt.attempt_record_hash,
        request_hash=durable_attempt.request_hash,
        turn_record_hash="0" * 64,
        created_at=created_at,
    )
    turn.turn_record_hash = _witness(_turn_record_values(turn), "turn_record")
    try:
        _ensure_transaction(active)
        with active.begin_nested():
            active.add(turn)
            active.flush()
            if durable_run.state == "prepared":
                durable_run.state = "running"
                durable_run.state_version += 1
                durable_run.started_at = durable_run.started_at or utc_now_db()
            _refresh_run_state_head(durable_run, active)
    except (IntegrityError, OperationalError) as exc:
        raise BlockContractError("research-turn-conflict") from exc
    return turn


def reserve_research_turn(
    run: TranslationResearchRun,
    *,
    packet: Mapping[str, Any],
    session: Any | None = None,
    verified_plan: CanonicalSourcePlan | None = None,
    verification_context: ResearchVerificationContext | None = None,
) -> tuple[TranslationResearchTurn, TranslationAnalysisAttempt]:
    """Atomically reserve one model call and its immutable turn link.

    The caller commits the returned pair before provider I/O.  There is no
    public path that first commits a prepared research attempt and links it to
    a run later: a crash therefore leaves either both reservation records or
    neither of them.
    """

    active = _active_session(session)
    durable_run = _fresh_run(run, active)
    operation, plan = _verify_run_scope(
        durable_run,
        active,
        require_live=True,
        verified_plan=verified_plan,
        verification_context=verification_context,
    )
    if durable_run.state not in {"prepared", "running"}:
        raise BlockContractError("research-run-terminal")
    prior_turn_ids = (
        verification_context._run_turn_ids.get(durable_run.id)
        if verification_context is not None
        else None
    )
    if prior_turn_ids is None:
        with active.no_autoflush:
            prior_turns = tuple(
                active.query(TranslationResearchTurn)
                .populate_existing()
                .filter_by(run_id=durable_run.id)
                .order_by(TranslationResearchTurn.sequence)
                .all()
            )
    else:
        try:
            prior_turns = tuple(
                verification_context._turns[turn_id] for turn_id in prior_turn_ids
            )
        except KeyError as exc:
            raise BlockContractError("research-verification-preload") from exc
    retry_of: str | None = None
    rebase_of: str | None = None
    if prior_turns:
        previous = prior_turns[-1]
        previous_attempt = (
            verification_context.analysis._attempts.get(previous.analysis_attempt_id)
            if verification_context is not None
            else None
        )
        if previous_attempt is None:
            with active.no_autoflush:
                previous_attempt = active.get(
                    TranslationAnalysisAttempt,
                    previous.analysis_attempt_id,
                    populate_existing=True,
                )
        if previous_attempt is None:
            raise BlockContractError("research-turn-attempt")
        if previous_attempt.dispatch_state == "provider_outcome_uncertain":
            raise BlockContractError("research-turn-history-uncertain")
        if previous_attempt.dispatch_state in {
            "provider_failed",
            "fence_failed",
            "cancelled",
            "failed",
        }:
            retry_of = previous_attempt.id
        elif previous_attempt.dispatch_state == "completed":
            rebase_of = previous_attempt.id
        else:
            raise BlockContractError("research-turn-history-active")
    budget = _research_budget(durable_run.budget_json)
    declarations = _tool_declarations(durable_run.tool_declarations_json)
    try:
        _ensure_transaction(active)
        with active.begin_nested():
            attempt = persist_analysis_attempt(
                operation=operation,
                source_plan=plan,
                analysis_kind="research",
                contract_version="analysis-attempt-v2",
                packet=packet,
                model=_mapping(durable_run.model_json, "research-model"),
                profile=_mapping(durable_run.profile_json, "research-profile"),
                settings=_mapping(durable_run.settings_json, "research-settings"),
                budget={"output_tokens": budget["output_tokens"]},
                tool_declarations=[item["declaration"] for item in declarations],
                retry_of_attempt_id=retry_of,
                rebase_of_attempt_id=rebase_of,
                session=active,
                verified_plan=verified_plan,
                verification_context=(
                    verification_context.analysis
                    if verification_context is not None
                    else None
                ),
            )
            turn = _append_research_turn(
                durable_run,
                attempt,
                session=active,
                verified_plan=verified_plan,
                verification_context=verification_context,
            )
    except BlockContractError:
        raise
    except (IntegrityError, OperationalError) as exc:
        raise BlockContractError("research-turn-conflict") from exc
    return turn, attempt


def _call_scope(
    call: TranslationResearchToolCall,
    active: Any,
    *,
    require_live: bool = False,
    verified_plan: CanonicalSourcePlan | None = None,
    verification_context: ResearchVerificationContext | None = None,
) -> tuple[
    TranslationResearchRun,
    TranslationResearchTurn,
    TranslationOperation,
    TranslationSourcePlan,
]:
    cached_scope = (
        verification_context._scopes.get(call.run_id)
        if verification_context is not None
        else None
    )
    run = cached_scope[0] if cached_scope is not None else None
    operation = cached_scope[1] if cached_scope is not None else None
    plan = cached_scope[2] if cached_scope is not None else None
    turn = (
        verification_context._turns.get(call.turn_id)
        if verification_context is not None
        else None
    )
    with active.no_autoflush:
        if run is None:
            run = active.get(
                TranslationResearchRun, call.run_id, populate_existing=True
            )
        if turn is None:
            turn = active.get(
                TranslationResearchTurn, call.turn_id, populate_existing=True
            )
    if run is None or turn is None or turn.run_id != call.run_id:
        raise BlockContractError("research-call-scope")
    if require_live and run.state not in {"prepared", "running"}:
        raise BlockContractError("research-run-terminal")
    if operation is None or plan is None or require_live:
        operation, plan = _verify_run_scope(
            run,
            active,
            require_live=require_live,
            verified_plan=verified_plan,
            verification_context=verification_context,
        )
    _verify_turn(
        turn,
        run,
        active,
        plan,
        verified_plan=verified_plan,
        verification_context=verification_context,
    )
    if (
        call.project_id != run.project_id
        or call.owner_user_id != run.owner_user_id
        or call.operation_id != run.operation_id
        or call.source_plan_id != run.source_plan_id
        or turn.project_id != run.project_id
        or turn.owner_user_id != run.owner_user_id
        or turn.operation_id != run.operation_id
        or turn.source_plan_id != run.source_plan_id
    ):
        raise BlockContractError("research-call-scope")
    return run, turn, operation, plan


def _result_blob_envelope(
    call: TranslationResearchToolCall,
) -> dict[str, Any] | None:
    candidate: object
    if call.state == "succeeded":
        candidate = call.result_json
    elif call.state in {"blocked", "failed"} and isinstance(call.error_json, dict):
        candidate = call.error_json.get("result_envelope")
    else:
        candidate = None
    return cast(dict[str, Any], candidate) if isinstance(candidate, dict) else None


def _reserved_blob_refs(envelope: object) -> list[dict[str, Any]]:
    if not isinstance(envelope, dict):
        return []
    refs = envelope.get("data_refs")
    if not isinstance(refs, list):
        return []
    return [
        cast(dict[str, Any], item)
        for item in refs
        if isinstance(item, dict) and item.get("kind") == "translator_research_result"
    ]


def _verify_result_blob(
    blob: TranslationResearchToolResultBlob,
    call: TranslationResearchToolCall,
) -> None:
    payload = _canonical(
        blob.payload_json,
        "research-result-blob-payload",
        maximum=_MAX_RESULT_BLOB_BYTES,
    )
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if (
        call.state not in {"succeeded", "blocked", "failed"}
        or blob.tool_call_id != call.id
        or blob.run_id != call.run_id
        or blob.turn_id != call.turn_id
        or blob.project_id != call.project_id
        or blob.owner_user_id != call.owner_user_id
        or blob.operation_id != call.operation_id
        or blob.source_plan_id != call.source_plan_id
        or blob.payload_size_bytes != len(encoded)
        or not _witness_matches(payload, blob.payload_hash, "result_blob_payload")
        or not _witness_matches(
            _result_blob_record_values(blob),
            blob.record_hash,
            "result_blob_record",
        )
        or _reserved_blob_refs(_result_blob_envelope(call)) != [_result_blob_ref(blob)]
    ):
        raise BlockContractError("research-result-blob-record")


def _verify_result_blob_membership(
    call: TranslationResearchToolCall,
    active: Any,
    *,
    verification_context: ResearchVerificationContext | None = None,
) -> TranslationResearchToolResultBlob | None:
    preloaded = (
        verification_context._result_blobs.get(call.id, _NO_RESULT_BLOB)
        if verification_context is not None
        else _NO_RESULT_BLOB
    )
    if preloaded is _NO_RESULT_BLOB:
        with active.no_autoflush:
            blob = active.get(
                TranslationResearchToolResultBlob,
                call.id,
                populate_existing=True,
            )
    else:
        blob = cast(TranslationResearchToolResultBlob | None, preloaded)
    refs = _reserved_blob_refs(_result_blob_envelope(call))
    if blob is None:
        if refs:
            raise BlockContractError("research-result-blob-missing")
        return None
    _verify_result_blob(blob, call)
    return cast(TranslationResearchToolResultBlob, blob)


def _verify_call(
    call: TranslationResearchToolCall,
    active: Any,
    *,
    require_live: bool = False,
    verified_plan: CanonicalSourcePlan | None = None,
    verification_context: ResearchVerificationContext | None = None,
) -> tuple[
    TranslationResearchRun,
    TranslationResearchTurn,
    TranslationOperation,
    TranslationSourcePlan,
    tuple[TranslationResearchToolCallEvent, ...],
]:
    cache_key = _call_verification_key(call)
    if verification_context is not None and not require_live:
        cached = verification_context._verified_calls.get(cache_key)
        if cached is not None:
            return cached
    run, turn, operation, plan = _call_scope(
        call,
        active,
        require_live=require_live,
        verified_plan=verified_plan,
        verification_context=verification_context,
    )
    declaration = _mapping(call.declaration_json, "research-tool-declaration")
    raw_arguments = call.raw_arguments_json
    arguments = _canonical(call.arguments_json, "research-tool-arguments")
    expected_arguments, expected_state, expected_error = _validated_tool_arguments(
        declaration, raw_arguments
    )
    if (
        call.argument_state != expected_state
        or arguments != expected_arguments
        or call.argument_error_json != expected_error
    ):
        raise BlockContractError("research-arguments-state")
    fence_parts = (
        call.operation_version,
        call.queue_job_id,
        call.operation_lease_expires_at,
        call.lease_fence,
    )
    has_any_fence = any(value is not None for value in fence_parts)
    has_complete_fence = all(value is not None for value in fence_parts)
    was_claimed = call.claimed_at is not None
    fence_is_valid = (
        has_complete_fence
        and isinstance(call.operation_version, int)
        and not isinstance(call.operation_version, bool)
        and isinstance(call.queue_job_id, str)
        and isinstance(call.operation_lease_expires_at, datetime)
        and _witness_matches(
            _call_fence_values(
                operation_id=call.operation_id,
                operation_version=call.operation_version,
                queue_job_id=call.queue_job_id,
                lease_expires_at=call.operation_lease_expires_at,
            ),
            call.lease_fence,
            "call_lease_fence",
        )
    )
    if (
        not _witness_matches(declaration, call.declaration_hash, "declaration")
        or not _witness_matches(raw_arguments, call.raw_arguments_hash, "raw_arguments")
        or not _witness_matches(arguments, call.arguments_hash, "arguments")
        or not _witness_matches(
            call.provider_call_id, call.provider_call_key, "provider_call_id"
        )
        or has_any_fence != has_complete_fence
        or was_claimed != has_complete_fence
        or (has_complete_fence and not fence_is_valid)
        or (
            require_live
            and was_claimed
            and (
                call.operation_version != operation.version
                or call.queue_job_id != operation.queue_job_id
                or call.operation_lease_expires_at != operation.lease_expires_at
                or not _witness_matches(
                    _call_fence_values(
                        operation_id=operation.id,
                        operation_version=operation.version,
                        queue_job_id=operation.queue_job_id,
                        lease_expires_at=operation.lease_expires_at,
                    ),
                    call.lease_fence,
                    "call_lease_fence",
                )
            )
        )
        or call.source_plan_hash != run.source_plan_hash
        or call.call_record_hash
        not in private_witness_candidates(
            _call_record_values(call), purpose=_PURPOSES["call_record"]
        )
    ):
        raise BlockContractError("research-call-record")
    declarations = _tool_declarations(run.tool_declarations_json)
    if declaration not in declarations:
        raise BlockContractError("research-tool-declaration-membership")
    assistant = _completed_turn_output(
        turn,
        run,
        active,
        plan,
        verified_plan=verified_plan,
        verification_context=verification_context,
    )
    requested = assistant.get("tool_calls")
    if (
        not isinstance(requested, list)
        or call.turn_call_index < 1
        or call.turn_call_index > len(requested)
    ):
        raise BlockContractError("research-tool-call-membership")
    requested_call = requested[call.turn_call_index - 1]
    function = (
        requested_call.get("function") if isinstance(requested_call, dict) else None
    )
    if (
        not isinstance(function, dict)
        or requested_call.get("id") != call.provider_call_id
        or function.get("name") != call.tool_name
        or function.get("arguments") != call.raw_arguments_json
    ):
        raise BlockContractError("research-tool-call-membership")
    events = rehydrate_research_tool_call_events(
        call,
        session=active,
        verification_context=verification_context,
    )
    _verify_call_state_shape(call, events)
    _verify_call_state_head(call, events)
    _verify_result_blob_membership(
        call,
        active,
        verification_context=verification_context,
    )
    restored = (run, turn, operation, plan, events)
    if verification_context is not None and not require_live:
        verification_context._verified_calls[cache_key] = restored
    return restored


def _verify_call_state_shape(
    call: TranslationResearchToolCall,
    events: tuple[TranslationResearchToolCallEvent, ...],
) -> None:
    if call.state == "succeeded":
        if (
            call.result_json is None
            or call.error_json is not None
            or call.result_hash is None
            or call.error_hash is not None
            or call.tool_message_content_json is None
            or call.tool_message_hash is None
        ):
            raise BlockContractError("research-success-shape")
        if not _witness_matches(call.result_json, call.result_hash, "result"):
            raise BlockContractError("research-result-witness")
    elif call.state in {"blocked", "failed", "uncertain", "cancelled"}:
        if (
            call.result_json is not None
            or call.result_hash is not None
            or call.error_json is None
            or call.error_hash is None
            or call.tool_message_content_json is None
            or call.tool_message_hash is None
        ):
            raise BlockContractError("research-terminal-shape")
        if not _witness_matches(call.error_json, call.error_hash, "error"):
            raise BlockContractError("research-error-witness")
    elif (
        call.result_json is not None
        or call.error_json is not None
        or call.result_hash is not None
        or call.error_hash is not None
        or call.tool_message_content_json is not None
        or call.tool_message_hash is not None
    ):
        raise BlockContractError("research-nonterminal-shape")
    if call.state in {
        "succeeded",
        "blocked",
        "failed",
        "uncertain",
        "cancelled",
    } and not _witness_matches(
        call.tool_message_content_json, call.tool_message_hash, "tool_message"
    ):
        raise BlockContractError("research-tool-message-witness")
    if call.state in {
        "succeeded",
        "blocked",
        "failed",
        "uncertain",
        "cancelled",
    } and call.tool_message_content_json != _tool_message_content(call):
        raise BlockContractError("research-tool-message")
    event_types = tuple(event.event_type for event in events)
    if call.state_version != len(events):
        raise BlockContractError("research-event-version")
    if call.state == "prepared" and (
        call.claimed_at is not None or call.finished_at is not None
    ):
        raise BlockContractError("research-prepared-timestamps")
    if call.state == "executing" and (
        call.claimed_at is None or call.finished_at is not None
    ):
        raise BlockContractError("research-executing-timestamp")
    if call.state in TERMINAL_TOOL_STATES and call.finished_at is None:
        raise BlockContractError("research-terminal-timestamp")
    if call.state in {"succeeded", "failed", "uncertain"} and call.claimed_at is None:
        raise BlockContractError("research-terminal-claim")
    if call.state == "cancelled" and call.claimed_at is not None:
        raise BlockContractError("research-cancelled-claim")
    if call.state == "blocked" and (
        (call.claimed_at is None and event_types != ("prepared", "blocked"))
        or (
            call.claimed_at is not None
            and event_types != ("prepared", "executing", "blocked")
        )
    ):
        raise BlockContractError("research-blocked-claim")
    if not events:
        raise BlockContractError("research-event-missing")
    expected_payloads: list[dict[str, Any]] = [
        {"state": "prepared", "call_record_hash": call.call_record_hash}
    ]
    if call.claimed_at is not None:
        expected_payloads.append({"state": "executing", "state_version": 2})
    if call.state in TERMINAL_TOOL_STATES:
        expected_payloads.append(
            {
                "state": call.state,
                "result": call.result_json,
                "error": call.error_json,
            }
        )
    actual_payloads = [
        _mapping(event.payload_json, "research-event-payload") for event in events
    ]
    if actual_payloads != expected_payloads:
        raise BlockContractError("research-event-payload-shape")


def _verify_call_state_head(
    call: TranslationResearchToolCall,
    events: tuple[TranslationResearchToolCallEvent, ...],
) -> None:
    verify_receipt(
        stored=call.state_head_receipt,
        purpose=_PURPOSES["call_state"],
        values={
            "call_id": call.id,
            "state": call.state,
            "state_version": call.state_version,
            "claimed_at": _time(call.claimed_at, "research-claimed-at")
            if call.claimed_at
            else None,
            "finished_at": _time(call.finished_at, "research-finished-at")
            if call.finished_at
            else None,
            "result_hash": call.result_hash,
            "error_hash": call.error_hash,
            "tool_message_hash": call.tool_message_hash,
            "operation_version": call.operation_version,
            "queue_job_id": call.queue_job_id,
            "operation_lease_expires_at": _time(
                call.operation_lease_expires_at, "research-lease"
            )
            if call.operation_lease_expires_at
            else None,
            "lease_fence": call.lease_fence,
            "event_head": events[-1].event_hash,
            "event_count": len(events),
        },
        code="research-call-state-head",
    )


def rehydrate_research_tool_call_events(
    call: TranslationResearchToolCall,
    *,
    session: Any | None = None,
    verification_context: ResearchVerificationContext | None = None,
) -> tuple[TranslationResearchToolCallEvent, ...]:
    active = _active_session(session)
    _reject_dirty_research_uow(active)
    cache_key = _call_verification_key(call)
    if verification_context is not None:
        cached = verification_context._verified_call_events.get(cache_key)
        if cached is not None:
            return cached
        events = verification_context._call_event_rows.get(call.id)
    else:
        events = None
    if events is None:
        with active.no_autoflush:
            events = tuple(
                active.query(TranslationResearchToolCallEvent)
                .populate_existing()
                .filter_by(tool_call_id=call.id)
                .order_by(TranslationResearchToolCallEvent.sequence)
                .all()
            )
    if not events or events[0].event_type != "prepared":
        raise BlockContractError("research-event-missing")
    previous: str | None = None
    for expected, event in enumerate(events, start=1):
        payload = _mapping(event.payload_json, "research-event-payload")
        if (
            event.sequence != expected
            or event.predecessor_event_hash != previous
            or event.event_type not in TOOL_EVENT_TYPES
            or not _witness_matches(payload, event.payload_hash, "event_payload")
            or event.event_hash
            not in private_witness_candidates(
                {
                    "id": event.id,
                    "tool_call_id": event.tool_call_id,
                    "sequence": event.sequence,
                    "event_type": event.event_type,
                    "payload_hash": event.payload_hash,
                    "predecessor_event_hash": event.predecessor_event_hash,
                    "created_at": _time(event.created_at, "research-event-created-at"),
                },
                purpose=_PURPOSES["event"],
            )
        ):
            raise BlockContractError("research-event-chain")
        previous = event.event_hash
    valid = {
        "prepared": {("prepared",)},
        "executing": {("prepared", "executing")},
        "succeeded": {("prepared", "executing", "succeeded")},
        "blocked": {
            ("prepared", "blocked"),
            ("prepared", "executing", "blocked"),
        },
        "failed": {("prepared", "executing", "failed")},
        "uncertain": {("prepared", "executing", "uncertain")},
        "cancelled": {("prepared", "cancelled")},
    }
    if tuple(event.event_type for event in events) not in valid.get(call.state, set()):
        raise BlockContractError("research-event-state")
    if verification_context is not None:
        verification_context._verified_call_events[cache_key] = events
    return events


def _append_tool_event(
    call: TranslationResearchToolCall,
    event_type: str,
    payload: Mapping[str, Any],
    active: Any,
    *,
    verified_events: tuple[TranslationResearchToolCallEvent, ...] | None = None,
) -> TranslationResearchToolCallEvent:
    if event_type not in TOOL_EVENT_TYPES:
        raise BlockContractError("research-event-type")
    payload_data = _mapping(payload, "research-event-payload")
    if verified_events is None:
        with active.no_autoflush:
            existing = tuple(
                active.query(TranslationResearchToolCallEvent)
                .populate_existing()
                .filter_by(tool_call_id=call.id)
                .order_by(TranslationResearchToolCallEvent.sequence)
                .all()
            )
    else:
        existing = verified_events
    if len(existing) >= _MAX_EVENT_CHAIN:
        raise BlockContractError("research-event-limit")
    event = cast(Any, TranslationResearchToolCallEvent)(
        id=str(uuid.uuid4()),
        tool_call_id=call.id,
        sequence=len(existing) + 1,
        event_type=event_type,
        payload_json=payload_data,
        payload_hash=_witness(payload_data, "event_payload"),
        predecessor_event_hash=existing[-1].event_hash if existing else None,
        created_at=utc_now_db(),
        event_hash="0" * 64,
    )
    event.event_hash = _event_hash(event)
    active.add(event)
    active.flush()
    return event


def _set_call_state_head(
    call: TranslationResearchToolCall,
    event: TranslationResearchToolCallEvent,
    active: Any,
) -> None:
    call.state_head_receipt = make_receipt(
        purpose=_PURPOSES["call_state"],
        values={
            "call_id": call.id,
            "state": call.state,
            "state_version": call.state_version,
            "claimed_at": _time(call.claimed_at, "research-claimed-at")
            if call.claimed_at
            else None,
            "finished_at": _time(call.finished_at, "research-finished-at")
            if call.finished_at
            else None,
            "result_hash": call.result_hash,
            "error_hash": call.error_hash,
            "tool_message_hash": call.tool_message_hash,
            "operation_version": call.operation_version,
            "queue_job_id": call.queue_job_id,
            "operation_lease_expires_at": _time(
                call.operation_lease_expires_at, "research-lease"
            )
            if call.operation_lease_expires_at
            else None,
            "lease_fence": call.lease_fence,
            "event_head": event.event_hash,
            "event_count": event.sequence,
        },
    )
    active.flush()


def _prepare_research_tool_call(
    *,
    run: TranslationResearchRun,
    turn: TranslationResearchTurn,
    provider_call_id: str,
    tool_name: str,
    tool_id: str,
    provider_id: str,
    addon_id: str,
    declaration: Mapping[str, Any],
    raw_arguments: object | None = None,
    call_index: int | None = None,
    require_live: bool = True,
    session: Any | None = None,
    verified_plan: CanonicalSourcePlan | None = None,
    verification_context: ResearchVerificationContext | None = None,
    reservation_context: _ToolReservationContext | None = None,
) -> TranslationResearchToolCall:
    """Freeze one declared read-only tool call without dispatching it."""

    active = _active_session(session)
    if reservation_context is None:
        durable_run = _fresh_run(run, active)
        _operation, _plan = _verify_run_scope(
            durable_run,
            active,
            require_live=require_live,
            verified_plan=verified_plan,
            verification_context=verification_context,
        )
        with active.no_autoflush:
            durable_turn = active.get(
                TranslationResearchTurn, turn.id, populate_existing=True
            )
        if durable_turn is None or durable_turn.run_id != durable_run.id:
            raise BlockContractError("research-turn-scope")
        completed_output = _completed_turn_output(
            durable_turn,
            durable_run,
            active,
            _plan,
            verified_plan=verified_plan,
            verification_context=verification_context,
        )
        declarations = tuple(_tool_declarations(durable_run.tool_declarations_json))
        provider_ids = frozenset(
            _sorted_ids(
                durable_run.selected_provider_ids_json,
                "research-providers",
            )
        )
        addon_ids = frozenset(
            _sorted_ids(durable_run.selected_addon_ids_json, "research-addons")
        )
        with active.no_autoflush:
            prior_calls = list(
                active.query(TranslationResearchToolCall)
                .populate_existing()
                .filter_by(run_id=durable_run.id)
                .all()
            )
    else:
        durable_run = reservation_context.run
        durable_turn = reservation_context.turn
        _plan = reservation_context.plan
        completed_output = reservation_context.completed_output
        declarations = reservation_context.declarations
        provider_ids = reservation_context.provider_ids
        addon_ids = reservation_context.addon_ids
        prior_calls = reservation_context.calls
    if durable_run.state not in {"prepared", "running"}:
        raise BlockContractError("research-run-terminal")
    declaration_data = _mapping(declaration, "research-tool-declaration")
    if provider_id not in provider_ids or addon_id not in addon_ids:
        raise BlockContractError("research-tool-selection")
    if declaration_data not in declarations:
        raise BlockContractError("research-tool-declaration-membership")
    if (
        declaration_data.get("tool_name") != tool_name
        or declaration_data.get("tool_id") != tool_id
        or declaration_data.get("provider_id") != provider_id
        or declaration_data.get("addon_id") != addon_id
        or not isinstance(provider_call_id, str)
        or not provider_call_id.strip()
        or provider_call_id != provider_call_id.strip()
        or not provider_call_id.isprintable()
        or len(provider_call_id.encode("utf-8")) > _MAX_PROVIDER_CALL_ID_BYTES
        or not isinstance(tool_name, str)
        or not tool_name.strip()
        or tool_name != tool_name.strip()
        or not tool_name.isprintable()
        or len(tool_name.encode("utf-8")) > _MAX_TOOL_NAME_BYTES
        or not all(
            isinstance(item, str)
            and item.strip()
            and item == item.strip()
            and item.isprintable()
            and len(item.encode("utf-8")) <= _MAX_IDENTITY_BYTES
            for item in (tool_id, provider_id, addon_id)
        )
    ):
        raise BlockContractError("research-tool-identity")
    if (
        not isinstance(raw_arguments, str)
        or len(raw_arguments.encode("utf-8")) > _MAX_JSON_BYTES
    ):
        raise BlockContractError("research-raw-arguments")
    else:
        raw_arguments_data = raw_arguments
    arguments_data, argument_state, argument_error = _validated_tool_arguments(
        declaration_data, raw_arguments_data
    )
    tool_calls = completed_output.get("tool_calls")
    if not isinstance(tool_calls, list):
        raise BlockContractError("research-tool-call-membership")
    turn_calls = tuple(call for call in prior_calls if call.turn_id == durable_turn.id)
    turn_call_index = len(turn_calls) + 1
    if turn_call_index > len(tool_calls):
        raise BlockContractError("research-tool-call-membership")
    requested_call = tool_calls[turn_call_index - 1]
    if not (
        isinstance(requested_call, dict)
        and requested_call.get("id") == provider_call_id
        and isinstance(requested_call.get("function"), dict)
        and requested_call["function"].get("name") == tool_name
        and requested_call["function"].get("arguments") == raw_arguments
    ):
        raise BlockContractError("research-tool-call-membership")
    if any(
        _witness_matches(
            provider_call_id, existing.provider_call_key, "provider_call_id"
        )
        for existing in prior_calls
    ):
        raise BlockContractError("research-tool-call-duplicate")
    prior = max(prior_calls, key=lambda item: item.call_index, default=None)
    expected = (prior.call_index + 1) if prior is not None else 1
    if expected > _MAX_CALLS:
        raise BlockContractError("research-tool-limit")
    if call_index is not None and (
        not isinstance(call_index, int)
        or isinstance(call_index, bool)
        or call_index != expected
    ):
        raise BlockContractError("research-tool-index")
    created_at = utc_now_db()
    call = cast(Any, TranslationResearchToolCall)(
        id=str(uuid.uuid4()),
        run_id=durable_run.id,
        turn_id=durable_turn.id,
        project_id=durable_run.project_id,
        owner_user_id=durable_run.owner_user_id,
        operation_id=durable_run.operation_id,
        source_plan_id=durable_run.source_plan_id,
        source_plan_hash=durable_run.source_plan_hash,
        call_index=expected,
        turn_call_index=turn_call_index,
        provider_call_id_json=provider_call_id,
        provider_call_key=_witness(provider_call_id, "provider_call_id"),
        tool_name=tool_name,
        tool_id=tool_id,
        provider_id=provider_id,
        addon_id=addon_id,
        operation_version=None,
        queue_job_id=None,
        operation_lease_expires_at=None,
        lease_fence=None,
        declaration_json=declaration_data,
        raw_arguments_json=raw_arguments_data,
        arguments_json=arguments_data,
        argument_state=argument_state,
        argument_error_json=argument_error,
        declaration_hash=_witness(declaration_data, "declaration"),
        raw_arguments_hash=_witness(raw_arguments_data, "raw_arguments"),
        arguments_hash=_witness(arguments_data, "arguments"),
        call_record_hash="0" * 64,
        state="prepared",
        state_version=1,
        result_json=None,
        error_json=None,
        result_hash=None,
        error_hash=None,
        created_at=created_at,
        state_head_receipt="0" * 64,
    )
    call.call_record_hash = _witness(_call_record_values(call), "call_record")

    def persist_call() -> None:
        active.add(call)
        active.flush()
        event = _append_tool_event(
            call,
            "prepared",
            {"state": "prepared", "call_record_hash": call.call_record_hash},
            active,
            verified_events=(),
        )
        _set_call_state_head(call, event, active)
        if durable_run.state == "prepared":
            durable_run.state = "running"
            durable_run.state_version += 1
            durable_run.started_at = durable_run.started_at or utc_now_db()
        if reservation_context is None:
            _refresh_run_state_head(durable_run, active)

    try:
        _ensure_transaction(active)
        if reservation_context is None:
            with active.begin_nested():
                persist_call()
        else:
            persist_call()
    except IntegrityError as exc:
        raise BlockContractError("research-tool-call-conflict") from exc
    except OperationalError as exc:
        if is_database_locked(exc):
            raise
        raise BlockContractError("research-tool-call-conflict") from exc
    if reservation_context is not None:
        reservation_context.calls.append(call)
    return call


def _operation_fence_matches(
    call: TranslationResearchToolCall,
    active: Any,
    *,
    verified_plan: CanonicalSourcePlan | None = None,
) -> bool:
    try:
        operation, _ = _durable_scope(
            operation_id=call.operation_id,
            source_plan_id=call.source_plan_id,
            project_id=call.project_id,
            owner_user_id=call.owner_user_id,
            source_plan_hash=call.source_plan_hash,
            source_content_hash=_fresh_run(call.run_id, active).source_content_hash,
            session=active,
            require_live=True,
            verified_plan=verified_plan,
        )
    except BlockContractError:
        return False
    return (
        operation.version == call.operation_version
        and operation.queue_job_id == call.queue_job_id
        and operation.lease_expires_at == call.operation_lease_expires_at
        and _witness_matches(
            _call_fence_values(
                operation_id=call.operation_id,
                operation_version=call.operation_version,
                queue_job_id=call.queue_job_id,
                lease_expires_at=call.operation_lease_expires_at,
            ),
            call.lease_fence,
            "call_lease_fence",
        )
    )


def _prepare_research_tool_calls_once(
    *,
    run: TranslationResearchRun | str,
    turn: TranslationResearchTurn | str,
    session: Any | None = None,
    verified_plan: CanonicalSourcePlan | None = None,
    verification_context: ResearchVerificationContext | None = None,
) -> tuple[TranslationResearchToolCall, ...]:
    """Build one complete research-tool reservation transaction.

    Tool identity and parsed arguments are derived only from the frozen run
    declarations and completed assistant transcript. The entire reservation,
    including deterministic pre-I/O blocks, is one savepoint.
    """

    active = _active_session(session)
    durable_run = _fresh_run(run, active)
    operation, plan = _verify_run_scope(
        durable_run,
        active,
        require_live=False,
        verified_plan=verified_plan,
        verification_context=verification_context,
    )
    operation_is_live = (
        operation.state == "running"
        and operation.cancel_requested_at is None
        and operation.lease_expires_at is not None
        and operation.lease_expires_at > utc_now_db()
    )
    turn_id = turn if isinstance(turn, str) else turn.id
    with active.no_autoflush:
        durable_turn = active.get(
            TranslationResearchTurn,
            turn_id,
            populate_existing=True,
        )
    if durable_turn is None or durable_turn.run_id != durable_run.id:
        raise BlockContractError("research-turn-scope")
    output = _completed_turn_output(
        durable_turn,
        durable_run,
        active,
        plan,
        verified_plan=verified_plan,
        verification_context=verification_context,
    )
    requested = output.get("tool_calls")
    if not isinstance(requested, list):
        return ()
    declaration_rows = tuple(_tool_declarations(durable_run.tool_declarations_json))
    declarations = {item["tool_name"]: item for item in declaration_rows}
    with active.no_autoflush:
        existing = tuple(
            active.query(TranslationResearchToolCall)
            .populate_existing()
            .filter_by(run_id=durable_run.id)
            .all()
        )
    budget = _research_budget(durable_run.budget_json)
    reservation_context = _ToolReservationContext(
        run=durable_run,
        turn=durable_turn,
        plan=plan,
        completed_output=output,
        declarations=declaration_rows,
        provider_ids=frozenset(
            _sorted_ids(
                durable_run.selected_provider_ids_json,
                "research-providers",
            )
        ),
        addon_ids=frozenset(
            _sorted_ids(durable_run.selected_addon_ids_json, "research-addons")
        ),
        calls=list(existing),
    )
    claimed_count = sum(call.claimed_at is not None for call in existing)
    remaining = budget["max_tool_calls"] - claimed_count
    block_for_budget = len(requested) > remaining
    prepared: list[TranslationResearchToolCall] = []
    try:
        _ensure_transaction(active)
        with active.begin_nested():
            for requested_call in requested:
                function = requested_call["function"]
                frozen_declaration = declarations.get(function["name"])
                if frozen_declaration is None:
                    raise BlockContractError("research-tool-declaration-membership")
                prepared.append(
                    _prepare_research_tool_call(
                        run=durable_run,
                        turn=durable_turn,
                        provider_call_id=requested_call["id"],
                        tool_name=function["name"],
                        tool_id=frozen_declaration["tool_id"],
                        provider_id=frozen_declaration["provider_id"],
                        addon_id=frozen_declaration["addon_id"],
                        declaration=frozen_declaration,
                        raw_arguments=function["arguments"],
                        require_live=operation_is_live,
                        verified_plan=verified_plan,
                        verification_context=verification_context,
                        reservation_context=reservation_context,
                        session=active,
                    )
                )
            if prepared:
                _refresh_run_state_head(durable_run, active)
            if not operation_is_live:
                error = {
                    "reason": "operation_not_runnable_before_dispatch",
                    "operation_state": operation.state,
                }
                prepared = [
                    cancel_research_tool_call(
                        call,
                        error=error,
                        session=active,
                        verified_plan=verified_plan,
                    )
                    for call in prepared
                ]
            elif block_for_budget:
                error = {
                    "reason": "tool_call_budget_exceeded",
                    "requested": len(requested),
                    "remaining": remaining,
                }
                prepared = [
                    block_prepared_research_tool_call(
                        call,
                        error=error,
                        session=active,
                        verified_plan=verified_plan,
                    )
                    for call in prepared
                ]
            else:
                prepared = [
                    block_prepared_research_tool_call(
                        call,
                        error=call.argument_error_json,
                        session=active,
                        verified_plan=verified_plan,
                    )
                    if call.argument_state == "invalid"
                    else call
                    for call in prepared
                ]
    except BlockContractError:
        raise
    except IntegrityError as exc:
        raise BlockContractError("research-tool-call-conflict") from exc
    except OperationalError as exc:
        if is_database_locked(exc):
            raise
        raise BlockContractError("research-tool-call-conflict") from exc
    return tuple(prepared)


def prepare_research_tool_calls(
    *,
    run: TranslationResearchRun,
    turn: TranslationResearchTurn,
    session: Any | None = None,
    verified_plan: CanonicalSourcePlan | None = None,
    verification_context: ResearchVerificationContext | None = None,
) -> tuple[TranslationResearchToolCall, ...]:
    """Reserve every model-requested tool call before external I/O.

    SQLite can reject a deferred WAL read-to-write upgrade immediately when a
    concurrent writer commits. Replay the complete reservation transaction so
    every scope check, index calculation, row, event, and receipt is rebuilt
    after rollback; retrying only the final flush or commit would lose the
    discarded unit of work.
    """

    active = _active_session(session)
    _reject_dirty_research_uow(active)
    run_id = run.id
    turn_id = turn.id

    return run_transaction_with_retry(
        active,
        lambda current: _prepare_research_tool_calls_once(
            run=run_id,
            turn=turn_id,
            session=current,
            verified_plan=verified_plan,
            verification_context=verification_context,
        ),
        operation_name="translator_research_tool_call_reservation",
    )


def claim_research_tool_call(
    call: TranslationResearchToolCall | str,
    *,
    session: Any | None = None,
    race_hook: Any | None = None,
    verified_plan: CanonicalSourcePlan | None = None,
    verification_context: ResearchVerificationContext | None = None,
) -> TranslationResearchToolCall:
    """Claim exactly once before I/O; an executing call is never replayed."""

    active = _active_session(session)
    durable = _fresh_call(call, active)
    run, _turn, operation, _plan, events = _verify_call(
        durable,
        active,
        require_live=True,
        verified_plan=verified_plan,
        verification_context=verification_context,
    )
    if durable.state != "prepared":
        raise BlockContractError("research-tool-not-prepared")
    if durable.argument_state != "parsed":
        raise BlockContractError("research-tool-invalid-arguments")
    old_version = durable.state_version
    now = utc_now_db()
    # Capture the exact preflight fence before the race hook.  The hook is
    # deliberately allowed to renew the operation (even in this session),
    # and the SQL CAS below must never seal that renewal with stale values.
    captured_operation_version = operation.version
    captured_queue_job_id = operation.queue_job_id
    captured_lease_expires_at = operation.lease_expires_at
    if race_hook is not None:
        race_hook()
    try:
        _ensure_transaction(active)
        with active.begin_nested():
            result = active.execute(
                update(TranslationResearchToolCall)
                .where(
                    TranslationResearchToolCall.id == durable.id,
                    TranslationResearchToolCall.state == "prepared",
                    TranslationResearchToolCall.state_version == old_version,
                    select(func.count())
                    .select_from(TranslationResearchToolCall)
                    .where(
                        TranslationResearchToolCall.run_id == durable.run_id,
                        TranslationResearchToolCall.claimed_at.is_not(None),
                    )
                    .scalar_subquery()
                    < _research_budget(run.budget_json)["max_tool_calls"],
                    exists().where(
                        TranslationResearchRun.id == durable.run_id,
                        TranslationResearchRun.state.in_(("prepared", "running")),
                        TranslationResearchRun.operation_id == TranslationOperation.id,
                        TranslationOperation.id == durable.operation_id,
                        TranslationOperation.project_id == durable.project_id,
                        TranslationOperation.owner_user_id == durable.owner_user_id,
                        TranslationOperation.state == "running",
                        TranslationOperation.cancel_requested_at.is_(None),
                        TranslationOperation.version == captured_operation_version,
                        TranslationOperation.queue_job_id == captured_queue_job_id,
                        TranslationOperation.lease_expires_at
                        == captured_lease_expires_at,
                        TranslationOperation.source_plan_id == durable.source_plan_id,
                        TranslationOperation.source_plan_hash
                        == durable.source_plan_hash,
                        TranslationOperation.source_content_hash
                        == run.source_content_hash,
                        TranslationOperation.lease_expires_at > now,
                    ),
                )
                .values(
                    state="executing",
                    state_version=old_version + 1,
                    claimed_at=now,
                    operation_version=captured_operation_version,
                    queue_job_id=captured_queue_job_id,
                    operation_lease_expires_at=captured_lease_expires_at,
                    lease_fence=_witness(
                        _call_fence_values(
                            operation_id=operation.id,
                            operation_version=captured_operation_version,
                            queue_job_id=captured_queue_job_id,
                            lease_expires_at=captured_lease_expires_at,
                        ),
                        "call_lease_fence",
                    ),
                )
            )
            if result.rowcount != 1:
                raise BlockContractError("research-tool-claim-race")
            fresh = _fresh_call(durable.id, active)
            event = _append_tool_event(
                fresh,
                "executing",
                {"state": "executing", "state_version": fresh.state_version},
                active,
                verified_events=events,
            )
            _set_call_state_head(fresh, event, active)
            run = _fresh_run(fresh.run_id, active)
            _refresh_run_state_head(run, active)
    except BlockContractError:
        raise
    except (IntegrityError, OperationalError) as exc:
        raise BlockContractError("research-tool-claim-conflict") from exc
    return fresh


def _attach_result_blob_ref(
    evidence: dict[str, Any],
    *,
    state: str,
    blob: TranslationResearchToolResultBlob,
) -> dict[str, Any]:
    material = cast(dict[str, Any], copy.deepcopy(evidence))
    envelope: object
    if state == "succeeded":
        envelope = material
    else:
        envelope = material.get("result_envelope")
    if not isinstance(envelope, dict):
        raise BlockContractError("research-result-blob-envelope")
    if any(
        envelope.get(key) not in (None, [], ())
        for key in (
            "data_refs",
            "artifact_refs",
            "source_refs",
            "evidence_refs",
        )
    ):
        raise BlockContractError("research-result-blob-untrusted-refs")
    envelope["data_refs"] = [_result_blob_ref(blob)]
    return material


def _terminal_transition(
    call: TranslationResearchToolCall | str,
    state: str,
    *,
    result: object | None = None,
    error: object | None = None,
    raw_result_blob: object = _NO_RESULT_BLOB,
    session: Any | None = None,
    verified_plan: CanonicalSourcePlan | None = None,
    verification_context: ResearchVerificationContext | None = None,
) -> TranslationResearchToolCall:
    if state not in {"succeeded", "blocked", "failed", "cancelled"}:
        raise BlockContractError("research-terminal-state")
    active = _active_session(session)
    durable = _fresh_call(call, active)
    _verify_call(
        durable,
        active,
        require_live=True,
        verified_plan=verified_plan,
        verification_context=verification_context,
    )
    if durable.state != "executing":
        raise BlockContractError("research-tool-not-executing")
    if state == "succeeded":
        if result is None or error is not None:
            raise BlockContractError("research-success-shape")
        result_data = _canonical(result, "research-result")
        error_data = None
    else:
        if result is not None:
            raise BlockContractError("research-terminal-shape")
        error_data = _mapping(
            error if error is not None else {"reason": state}, "research-error"
        )
        result_data = None
    now = utc_now_db()
    blob = None
    if raw_result_blob is not _NO_RESULT_BLOB:
        blob_payload = _canonical(
            raw_result_blob,
            "research-result-blob-payload",
            maximum=_MAX_RESULT_BLOB_BYTES,
        )
        blob_encoded = json.dumps(
            blob_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        blob = cast(Any, TranslationResearchToolResultBlob)(
            tool_call_id=durable.id,
            run_id=durable.run_id,
            turn_id=durable.turn_id,
            project_id=durable.project_id,
            owner_user_id=durable.owner_user_id,
            operation_id=durable.operation_id,
            source_plan_id=durable.source_plan_id,
            payload_json=blob_payload,
            payload_size_bytes=len(blob_encoded),
            payload_hash=_witness(blob_payload, "result_blob_payload"),
            record_hash="0" * 64,
            created_at=now,
        )
        blob.record_hash = _witness(
            _result_blob_record_values(blob), "result_blob_record"
        )
        if result_data is not None:
            result_data = _attach_result_blob_ref(
                result_data,
                state=state,
                blob=blob,
            )
        else:
            error_data = _attach_result_blob_ref(
                cast(dict[str, Any], error_data),
                state=state,
                blob=blob,
            )
    result_hash = _witness(result_data, "result") if result_data is not None else None
    error_hash = _witness(error_data, "error") if error_data is not None else None
    tool_message_content = _tool_message_content_values(
        state, result_data, error_data, result_hash, error_hash
    )
    tool_message_hash = _witness(tool_message_content, "tool_message")
    try:
        _ensure_transaction(active)
        with active.begin_nested():
            fresh = _fresh_call(durable.id, active)
            _run, _turn, _operation, _plan, events = _verify_call(
                fresh,
                active,
                require_live=True,
                verified_plan=verified_plan,
                verification_context=verification_context,
            )
            if fresh.state != "executing":
                raise BlockContractError("research-tool-not-executing")
            if blob is not None:
                if active.get(TranslationResearchToolResultBlob, fresh.id) is not None:
                    raise BlockContractError("research-result-blob-conflict")
                active.add(blob)
                active.flush()
            result_update = active.execute(
                update(TranslationResearchToolCall)
                .where(
                    TranslationResearchToolCall.id == fresh.id,
                    TranslationResearchToolCall.state == "executing",
                    TranslationResearchToolCall.state_version == fresh.state_version,
                    TranslationResearchToolCall.operation_version
                    == fresh.operation_version,
                    TranslationResearchToolCall.queue_job_id == fresh.queue_job_id,
                    TranslationResearchToolCall.operation_lease_expires_at
                    == fresh.operation_lease_expires_at,
                    TranslationResearchToolCall.lease_fence == fresh.lease_fence,
                    exists().where(
                        TranslationResearchRun.id == fresh.run_id,
                        TranslationResearchRun.state.in_(("prepared", "running")),
                        TranslationResearchRun.operation_id == TranslationOperation.id,
                        TranslationOperation.id == fresh.operation_id,
                        TranslationOperation.state == "running",
                        TranslationOperation.cancel_requested_at.is_(None),
                        TranslationOperation.version == fresh.operation_version,
                        TranslationOperation.queue_job_id == fresh.queue_job_id,
                        TranslationOperation.lease_expires_at
                        == fresh.operation_lease_expires_at,
                        TranslationOperation.lease_expires_at > now,
                    ),
                )
                .values(
                    state=state,
                    state_version=fresh.state_version + 1,
                    finished_at=now,
                    result_json=result_data,
                    error_json=error_data,
                    result_hash=result_hash,
                    error_hash=error_hash,
                    tool_message_content_json=tool_message_content,
                    tool_message_hash=tool_message_hash,
                )
            )
            if result_update.rowcount != 1:
                raise BlockContractError("research-terminal-race")
            fresh = _fresh_call(durable.id, active)
            event = _append_tool_event(
                fresh,
                state,
                {"state": state, "result": result_data, "error": error_data},
                active,
                verified_events=events,
            )
            _set_call_state_head(fresh, event, active)
            run = _fresh_run(fresh.run_id, active)
            _refresh_run_state_head(run, active)
    except BlockContractError:
        raise
    except (IntegrityError, OperationalError) as exc:
        raise BlockContractError("research-terminal-conflict") from exc
    return fresh


def complete_research_tool_call(
    call: TranslationResearchToolCall | str,
    result: object,
    *,
    raw_result_blob: object = _NO_RESULT_BLOB,
    session: Any | None = None,
    verified_plan: CanonicalSourcePlan | None = None,
    verification_context: ResearchVerificationContext | None = None,
) -> TranslationResearchToolCall:
    return _terminal_transition(
        call,
        "succeeded",
        result=result,
        raw_result_blob=raw_result_blob,
        session=session,
        verified_plan=verified_plan,
        verification_context=verification_context,
    )


def block_research_tool_call(
    call: TranslationResearchToolCall | str,
    error: object | None = None,
    *,
    raw_result_blob: object = _NO_RESULT_BLOB,
    session: Any | None = None,
    verified_plan: CanonicalSourcePlan | None = None,
    verification_context: ResearchVerificationContext | None = None,
) -> TranslationResearchToolCall:
    return _terminal_transition(
        call,
        "blocked",
        error=error,
        raw_result_blob=raw_result_blob,
        session=session,
        verified_plan=verified_plan,
        verification_context=verification_context,
    )


def block_prepared_research_tool_call(
    call: TranslationResearchToolCall | str,
    error: object | None = None,
    *,
    session: Any | None = None,
    verified_plan: CanonicalSourcePlan | None = None,
) -> TranslationResearchToolCall:
    """Block a call proven invalid before dispatch, without claiming I/O.

    This is intentionally separate from ``block_research_tool_call``: the
    latter accepts only an executing call and therefore represents a result
    from the executor.  Invalid provider arguments never receive a claim
    fence, so the runtime can skip executor submission safely.
    """

    active = _active_session(session)
    durable = _fresh_call(call, active)
    _verify_call(
        durable,
        active,
        require_live=False,
        verified_plan=verified_plan,
    )
    if durable.state != "prepared":
        raise BlockContractError("research-tool-not-prepared")
    error_data = _mapping(
        error if error is not None else {"reason": "invalid_before_dispatch"},
        "research-error",
    )
    error_hash = _witness(error_data, "error")
    tool_message_content = _tool_message_content_values(
        "blocked", None, error_data, None, error_hash
    )
    now = utc_now_db()
    try:
        _ensure_transaction(active)
        with active.begin_nested():
            fresh = _fresh_call(durable.id, active)
            if fresh.state != "prepared":
                raise BlockContractError("research-tool-block-race")
            result = active.execute(
                update(TranslationResearchToolCall)
                .where(
                    TranslationResearchToolCall.id == fresh.id,
                    TranslationResearchToolCall.state == "prepared",
                    TranslationResearchToolCall.state_version == fresh.state_version,
                    TranslationResearchToolCall.operation_version.is_(None),
                    TranslationResearchToolCall.queue_job_id.is_(None),
                    TranslationResearchToolCall.operation_lease_expires_at.is_(None),
                    TranslationResearchToolCall.lease_fence.is_(None),
                )
                .values(
                    state="blocked",
                    state_version=fresh.state_version + 1,
                    finished_at=now,
                    error_json=error_data,
                    error_hash=error_hash,
                    tool_message_content_json=tool_message_content,
                    tool_message_hash=_witness(tool_message_content, "tool_message"),
                )
            )
            if result.rowcount != 1:
                raise BlockContractError("research-tool-block-race")
            fresh = _fresh_call(durable.id, active)
            event = _append_tool_event(
                fresh,
                "blocked",
                {"state": "blocked", "result": None, "error": error_data},
                active,
            )
            _set_call_state_head(fresh, event, active)
            run = _fresh_run(fresh.run_id, active)
            _refresh_run_state_head(run, active)
    except BlockContractError:
        raise
    except (IntegrityError, OperationalError) as exc:
        raise BlockContractError("research-tool-block-conflict") from exc
    return _fresh_call(durable.id, active)


def fail_research_tool_call(
    call: TranslationResearchToolCall | str,
    error: object | None = None,
    *,
    raw_result_blob: object = _NO_RESULT_BLOB,
    session: Any | None = None,
    verified_plan: CanonicalSourcePlan | None = None,
    verification_context: ResearchVerificationContext | None = None,
) -> TranslationResearchToolCall:
    return _terminal_transition(
        call,
        "failed",
        error=error,
        raw_result_blob=raw_result_blob,
        session=session,
        verified_plan=verified_plan,
        verification_context=verification_context,
    )


def cancel_research_tool_call(
    call: TranslationResearchToolCall | str,
    error: object | None = None,
    *,
    session: Any | None = None,
    verified_plan: CanonicalSourcePlan | None = None,
) -> TranslationResearchToolCall:
    """Cancel only a prepared call; executing I/O is never labelled cancelled."""

    active = _active_session(session)
    durable = _fresh_call(call, active)
    _verify_call(
        durable,
        active,
        require_live=False,
        verified_plan=verified_plan,
    )
    if durable.state != "prepared":
        raise BlockContractError("research-tool-cancel-dispatched")
    error_data = _mapping(
        error if error is not None else {"reason": "cancelled_before_dispatch"},
        "research-error",
    )
    error_hash = _witness(error_data, "error")
    tool_message_content = _tool_message_content_values(
        "cancelled", None, error_data, None, error_hash
    )
    now = utc_now_db()
    try:
        _ensure_transaction(active)
        with active.begin_nested():
            fresh = _fresh_call(durable.id, active)
            if fresh.state != "prepared":
                raise BlockContractError("research-tool-cancel-race")
            result = active.execute(
                update(TranslationResearchToolCall)
                .where(
                    TranslationResearchToolCall.id == fresh.id,
                    TranslationResearchToolCall.state == "prepared",
                    TranslationResearchToolCall.state_version == fresh.state_version,
                )
                .values(
                    state="cancelled",
                    state_version=fresh.state_version + 1,
                    finished_at=now,
                    error_json=error_data,
                    error_hash=error_hash,
                    tool_message_content_json=tool_message_content,
                    tool_message_hash=_witness(tool_message_content, "tool_message"),
                )
            )
            if result.rowcount != 1:
                raise BlockContractError("research-tool-cancel-race")
            fresh = _fresh_call(durable.id, active)
            event = _append_tool_event(
                fresh,
                "cancelled",
                {"state": "cancelled", "result": None, "error": error_data},
                active,
            )
            _set_call_state_head(fresh, event, active)
            run = _fresh_run(fresh.run_id, active)
            _refresh_run_state_head(run, active)
    except BlockContractError:
        raise
    except (IntegrityError, OperationalError) as exc:
        raise BlockContractError("research-cancel-conflict") from exc
    return _fresh_call(durable.id, active)


def reconcile_research_tool_call(
    call: TranslationResearchToolCall | str,
    *,
    evidence: Mapping[str, Any] | None = None,
    session: Any | None = None,
    verified_plan: CanonicalSourcePlan | None = None,
) -> TranslationResearchToolCall:
    """Terminalize executing evidence as uncertain only after fence loss."""

    active = _active_session(session)
    durable = _fresh_call(call, active)
    _verify_call(
        durable,
        active,
        require_live=False,
        verified_plan=verified_plan,
    )
    if durable.state != "executing" or _operation_fence_matches(
        durable,
        active,
        verified_plan=verified_plan,
    ):
        raise BlockContractError("research-fence-not-lost")
    error_data = _mapping(
        {
            "reason": "operation_fence_lost",
            "evidence": {} if evidence is None else evidence,
        },
        "research-uncertain-evidence",
    )
    tool_message_content = _tool_message_content_values(
        "uncertain", None, error_data, None, _witness(error_data, "error")
    )
    try:
        _ensure_transaction(active)
        with active.begin_nested():
            fresh = _fresh_call(durable.id, active)
            if fresh.state != "executing":
                raise BlockContractError("research-tool-not-executing")
            result = active.execute(
                update(TranslationResearchToolCall)
                .where(
                    TranslationResearchToolCall.id == fresh.id,
                    TranslationResearchToolCall.state == "executing",
                    TranslationResearchToolCall.state_version == fresh.state_version,
                    TranslationResearchToolCall.operation_version
                    == fresh.operation_version,
                    TranslationResearchToolCall.queue_job_id == fresh.queue_job_id,
                    TranslationResearchToolCall.operation_lease_expires_at
                    == fresh.operation_lease_expires_at,
                    TranslationResearchToolCall.lease_fence == fresh.lease_fence,
                )
                .values(
                    state="uncertain",
                    state_version=fresh.state_version + 1,
                    finished_at=utc_now_db(),
                    error_json=error_data,
                    error_hash=_witness(error_data, "error"),
                    tool_message_content_json=tool_message_content,
                    tool_message_hash=_witness(tool_message_content, "tool_message"),
                )
            )
            if result.rowcount != 1:
                raise BlockContractError("research-uncertain-race")
            fresh = _fresh_call(durable.id, active)
            event = _append_tool_event(
                fresh,
                "uncertain",
                {"state": "uncertain", "result": None, "error": error_data},
                active,
            )
            _set_call_state_head(fresh, event, active)
            run = _fresh_run(fresh.run_id, active)
            _refresh_run_state_head(run, active)
    except BlockContractError:
        raise
    except (IntegrityError, OperationalError) as exc:
        raise BlockContractError("research-uncertain-conflict") from exc
    return _fresh_call(durable.id, active)


def _verified_model_dispatch_count(
    run: TranslationResearchRun,
    turns: tuple[TranslationResearchTurn, ...],
    session: Any,
    *,
    verified_plan: CanonicalSourcePlan | None = None,
    verification_context: ResearchVerificationContext | None = None,
) -> int:
    """Count authenticated provider dispatches, not crash-only reservations."""

    cached_scope = (
        verification_context._scopes.get(run.id)
        if verification_context is not None
        else None
    )
    plan = cached_scope[2] if cached_scope is not None else None
    if plan is None:
        with session.no_autoflush:
            plan = session.get(
                TranslationSourcePlan, run.source_plan_id, populate_existing=True
            )
    if plan is None:
        raise BlockContractError("research-source-plan")
    count = 0
    for turn in turns:
        _verify_turn(
            turn,
            run,
            session,
            plan,
            verified_plan=verified_plan,
            verification_context=verification_context,
        )
        attempt = (
            verification_context.analysis._attempts.get(turn.analysis_attempt_id)
            if verification_context is not None
            else None
        )
        if attempt is None:
            with session.no_autoflush:
                attempt = session.get(
                    TranslationAnalysisAttempt,
                    turn.analysis_attempt_id,
                    populate_existing=True,
                )
        if attempt is None:
            raise BlockContractError("research-turn-attempt")
        events = rehydrate_analysis_attempt_events(
            attempt,
            session=session,
            verification_context=(
                verification_context.analysis
                if verification_context is not None
                else None
            ),
        )
        count += any(event.event_type == "provider_dispatched" for event in events)
    return count


def _verify_run_graph_shape(
    run: TranslationResearchRun,
    turns: tuple[TranslationResearchTurn, ...],
    calls: tuple[TranslationResearchToolCall, ...],
    session: Any,
    *,
    verified_plan: CanonicalSourcePlan | None = None,
    verification_context: ResearchVerificationContext | None = None,
) -> None:
    budget = _research_budget(run.budget_json)
    if (
        [turn.sequence for turn in turns] != list(range(1, len(turns) + 1))
        or len(turns) > _MAX_EVENT_CHAIN
        or _verified_model_dispatch_count(
            run,
            turns,
            session,
            verified_plan=verified_plan,
            verification_context=verification_context,
        )
        > budget["max_model_calls"]
    ):
        raise BlockContractError("research-turn-sequence")
    if [call.call_index for call in calls] != list(range(1, len(calls) + 1)) or len(
        calls
    ) > _MAX_CALLS:
        raise BlockContractError("research-tool-index")
    if sum(call.claimed_at is not None for call in calls) > budget["max_tool_calls"]:
        raise BlockContractError("research-tool-call-limit")
    if run.state == "prepared" and (turns or calls):
        raise BlockContractError("research-run-graph-state")


def _verify_terminal_run_children(
    run: TranslationResearchRun,
    requested_state: str,
    active: Any,
    *,
    verified_plan: CanonicalSourcePlan | None = None,
    verification_context: ResearchVerificationContext | None = None,
) -> None:
    """Prove that no model/tool I/O child remains active or unreserved."""

    turn_ids = (
        verification_context._run_turn_ids.get(run.id)
        if verification_context is not None
        else None
    )
    call_ids = (
        verification_context._run_call_ids.get(run.id)
        if verification_context is not None
        else None
    )
    cached_scope = (
        verification_context._scopes.get(run.id)
        if verification_context is not None
        else None
    )
    if (
        verification_context is not None
        and turn_ids is not None
        and call_ids is not None
        and cached_scope is not None
    ):
        try:
            turns = tuple(verification_context._turns[item] for item in turn_ids)
            calls = tuple(verification_context._calls[item] for item in call_ids)
        except KeyError as exc:
            raise BlockContractError("research-verification-preload") from exc
        plan = cached_scope[2]
    else:
        with active.no_autoflush:
            turns = tuple(
                active.query(TranslationResearchTurn)
                .populate_existing()
                .filter_by(run_id=run.id)
                .order_by(TranslationResearchTurn.sequence)
                .all()
            )
        calls = tuple(
            active.query(TranslationResearchToolCall)
            .populate_existing()
            .filter_by(run_id=run.id)
            .order_by(TranslationResearchToolCall.call_index)
            .all()
        )
        plan = active.get(
            TranslationSourcePlan,
            run.source_plan_id,
            populate_existing=True,
        )
    if plan is None:
        raise BlockContractError("research-source-plan")
    _verify_run_graph_shape(
        run,
        turns,
        calls,
        active,
        verified_plan=verified_plan,
        verification_context=verification_context,
    )
    uncertain_child = False
    prior_turns: list[TranslationResearchTurn] = []
    last_attempt: TranslationAnalysisAttempt | None = None
    last_output: dict[str, Any] | None = None
    for turn in turns:
        attempt_data = _verify_turn(
            turn,
            run,
            active,
            plan,
            verified_plan=verified_plan,
            verification_context=verification_context,
        )
        attempt = (
            verification_context.analysis._attempts.get(turn.analysis_attempt_id)
            if verification_context is not None
            else None
        )
        if attempt is None:
            with active.no_autoflush:
                attempt = active.get(
                    TranslationAnalysisAttempt,
                    turn.analysis_attempt_id,
                    populate_existing=True,
                )
        if attempt is None:
            raise BlockContractError("research-turn-attempt")
        _verify_next_turn_history(
            run,
            attempt,
            attempt_data,
            tuple(prior_turns),
            active,
            plan,
            verified_plan=verified_plan,
            verification_context=verification_context,
        )
        prior_turns.append(turn)
        last_attempt = attempt
        turn_calls = tuple(call for call in calls if call.turn_id == turn.id)
        if attempt.dispatch_state in ACTIVE_ATTEMPT_STATES:
            raise BlockContractError("research-run-active-attempts")
        if attempt.dispatch_state == "provider_outcome_uncertain":
            uncertain_child = True
        if attempt.dispatch_state != "completed":
            if turn_calls:
                raise BlockContractError("research-turn-history")
            continue
        output = _completed_turn_output(
            turn,
            run,
            active,
            plan,
            verified_plan=verified_plan,
            verification_context=verification_context,
        )
        last_output = output
        requested = output.get("tool_calls")
        requested_calls = requested if isinstance(requested, list) else []
        if len(turn_calls) != len(requested_calls):
            raise BlockContractError("research-run-unreserved-tool-calls")
        for ordinal, (requested_call, call) in enumerate(
            zip(requested_calls, turn_calls, strict=True), start=1
        ):
            function = requested_call.get("function")
            if (
                call.turn_call_index != ordinal
                or not isinstance(function, dict)
                or requested_call.get("id") != call.provider_call_id
                or function.get("name") != call.tool_name
                or function.get("arguments") != call.raw_arguments_json
            ):
                raise BlockContractError("research-turn-history")
            _verify_call(
                call,
                active,
                require_live=False,
                verified_plan=verified_plan,
                verification_context=verification_context,
            )
            if call.state not in TERMINAL_TOOL_STATES:
                raise BlockContractError("research-run-active-calls")
            if call.state == "uncertain":
                uncertain_child = True
    if any(call.state not in TERMINAL_TOOL_STATES for call in calls):
        raise BlockContractError("research-run-active-calls")
    if requested_state == "uncertain":
        if not uncertain_child:
            raise BlockContractError("research-run-not-uncertain")
    elif uncertain_child:
        raise BlockContractError("research-run-uncertain-children")
    if requested_state == "completed":
        if last_attempt is None or last_attempt.dispatch_state != "completed":
            raise BlockContractError("research-run-incomplete")
        if isinstance(last_output, dict) and last_output.get("tool_calls"):
            raise BlockContractError("research-run-final-tool-calls")


def transition_research_run(
    run: TranslationResearchRun | str,
    state: str,
    *,
    session: Any | None = None,
    verified_plan: CanonicalSourcePlan | None = None,
    verification_context: ResearchVerificationContext | None = None,
) -> TranslationResearchRun:
    """Advance the durable run lifecycle without performing any I/O."""

    if state not in RESEARCH_STATES or state == "prepared":
        raise BlockContractError("research-run-transition")
    active = _active_session(session)
    durable = _fresh_run(run, active)
    operation, plan = _verify_run_scope(
        durable,
        active,
        require_live=state == "running",
        verified_plan=verified_plan,
        verification_context=verification_context,
    )
    allowed = {
        "prepared": {"running", "failed", "cancelled"},
        "running": {"completed", "failed", "cancelled", "uncertain"},
        "completed": set(),
        "failed": set(),
        "cancelled": set(),
        "uncertain": set(),
    }
    if state not in allowed[durable.state]:
        raise BlockContractError("research-run-transition")
    if state in {"completed", "failed", "cancelled", "uncertain"}:
        if verification_context is not None:
            _preload_research_verification_graph(
                durable,
                active,
                verification_context,
            )
            _cache_research_scope(
                verification_context,
                durable,
                operation,
                plan,
            )
        _verify_terminal_run_children(
            durable,
            state,
            active,
            verified_plan=verified_plan,
            verification_context=verification_context,
        )
    old_state = durable.state
    old_version = durable.state_version
    old_state_head = durable.state_head_receipt
    started_at = durable.started_at
    finished_at = durable.finished_at
    if state == "running":
        started_at = started_at or utc_now_db()
    else:
        finished_at = utc_now_db()
    try:
        _ensure_transaction(active)
        with active.begin_nested():
            result = active.execute(
                update(TranslationResearchRun)
                .where(
                    TranslationResearchRun.id == durable.id,
                    TranslationResearchRun.state == old_state,
                    TranslationResearchRun.state_version == old_version,
                    TranslationResearchRun.state_head_receipt == old_state_head,
                )
                .values(
                    state=state,
                    state_version=old_version + 1,
                    started_at=started_at,
                    finished_at=finished_at,
                )
            )
            if result.rowcount != 1:
                raise BlockContractError("research-run-transition-race")
            fresh = _fresh_run(durable.id, active)
            _refresh_run_state_head(fresh, active)
    except BlockContractError:
        raise
    except (IntegrityError, OperationalError) as exc:
        raise BlockContractError("research-run-transition-conflict") from exc
    return _fresh_run(durable.id, active)


def rehydrate_research_tool_call(
    call: TranslationResearchToolCall | str,
    *,
    session: Any | None = None,
    verified_plan: CanonicalSourcePlan | None = None,
    verification_context: ResearchVerificationContext | None = None,
) -> dict[str, Any]:
    active = _active_session(session)
    durable = _fresh_call(call, active)
    _, _, _, _, events = _verify_call(
        durable,
        active,
        require_live=False,
        verified_plan=verified_plan,
        verification_context=verification_context,
    )
    return {
        "declaration": _mapping(durable.declaration_json, "research-tool-declaration"),
        "raw_arguments": durable.raw_arguments_json,
        "arguments": _canonical(durable.arguments_json, "research-tool-arguments"),
        "argument_state": durable.argument_state,
        "argument_error": _mapping(
            durable.argument_error_json, "research-argument-error"
        )
        if durable.argument_error_json is not None
        else None,
        "result": _canonical(durable.result_json, "research-result")
        if durable.result_json is not None
        else None,
        "error": _mapping(durable.error_json, "research-error")
        if durable.error_json is not None
        else None,
        "tool_message": durable.tool_message_content_json,
        "events": events,
    }


def rehydrate_research_tool_result_blob(
    call: TranslationResearchToolCall | str,
    *,
    session: Any | None = None,
) -> Any:
    """Return the exact oversized result only after scope and witness checks."""

    active = _active_session(session)
    durable = _fresh_call(call, active)
    _verify_call(durable, active, require_live=False)
    blob = _verify_result_blob_membership(durable, active)
    if blob is None:
        raise BlockContractError("research-result-blob-missing")
    return _canonical(
        blob.payload_json,
        "research-result-blob-payload",
        maximum=_MAX_RESULT_BLOB_BYTES,
    )


def _attempt_summary_key(row: Any) -> tuple[str, str, str, str, int]:
    values = (
        row.id,
        row.attempt_record_hash,
        row.dispatch_receipt_hash,
        row.dispatch_state,
    )
    if (
        not all(isinstance(value, str) and value for value in values)
        or not isinstance(row.dispatch_version, int)
        or isinstance(row.dispatch_version, bool)
        or row.dispatch_version < 1
    ):
        raise BlockContractError("analysis-verification-key")
    return (*cast(tuple[str, str, str, str], values), row.dispatch_version)


def _preload_research_verification_graph(
    run: TranslationResearchRun,
    active: Any,
    context: ResearchVerificationContext,
) -> tuple[
    tuple[TranslationResearchTurn, ...],
    tuple[TranslationResearchToolCall, ...],
]:
    """Refresh only evidence whose durable state receipt changed."""

    state_head = context._state_heads.get(run.id)
    if not isinstance(state_head, dict):
        raise BlockContractError("research-verification-preload")
    turn_heads = state_head.get("turns")
    call_heads = state_head.get("tool_calls")
    if not isinstance(turn_heads, list) or not isinstance(call_heads, list):
        raise BlockContractError("research-verification-preload")
    warm = run.id in context._run_turn_ids

    with active.no_autoflush:
        loaded_turns = tuple(
            active.query(TranslationResearchTurn)
            .populate_existing()
            .filter_by(run_id=run.id)
            .order_by(TranslationResearchTurn.sequence)
            .all()
        )
    turns = tuple(
        cast(TranslationResearchTurn, _detached_evidence_snapshot(turn))
        for turn in loaded_turns
    )
    expected_turns = tuple(
        (item.get("sequence"), item.get("id"), item.get("receipt"))
        for item in turn_heads
        if isinstance(item, dict)
    )
    actual_turns = tuple(
        (turn.sequence, turn.id, turn.turn_record_hash) for turn in turns
    )
    if len(expected_turns) != len(turn_heads) or actual_turns != expected_turns:
        raise BlockContractError("research-verification-preload")
    context._turns.update({turn.id: turn for turn in turns})

    attempt_ids = tuple(turn.analysis_attempt_id for turn in turns)
    changed_attempt_ids: tuple[str, ...]
    if warm and attempt_ids:
        with active.no_autoflush:
            summaries = tuple(
                active.query(
                    TranslationAnalysisAttempt.id,
                    TranslationAnalysisAttempt.attempt_record_hash,
                    TranslationAnalysisAttempt.dispatch_receipt_hash,
                    TranslationAnalysisAttempt.dispatch_state,
                    TranslationAnalysisAttempt.dispatch_version,
                )
                .filter(TranslationAnalysisAttempt.id.in_(attempt_ids))
                .all()
            )
        summary_keys = {row.id: _attempt_summary_key(row) for row in summaries}
        if set(summary_keys) != set(attempt_ids):
            raise BlockContractError("analysis-verification-preload")
        changed_attempt_ids = tuple(
            attempt_id
            for attempt_id in attempt_ids
            if context.analysis._attempt_keys.get(attempt_id)
            != summary_keys[attempt_id]
            or attempt_id not in context.analysis._attempts
        )
    else:
        changed_attempt_ids = attempt_ids
    with active.no_autoflush:
        changed_attempts = (
            tuple(
                active.query(TranslationAnalysisAttempt)
                .populate_existing()
                .filter(TranslationAnalysisAttempt.id.in_(changed_attempt_ids))
                .all()
            )
            if changed_attempt_ids
            else ()
        )
        changed_attempt_events = (
            tuple(
                active.query(TranslationAnalysisAttemptEvent)
                .populate_existing()
                .filter(
                    TranslationAnalysisAttemptEvent.attempt_id.in_(changed_attempt_ids)
                )
                .order_by(
                    TranslationAnalysisAttemptEvent.attempt_id,
                    TranslationAnalysisAttemptEvent.sequence,
                )
                .all()
            )
            if changed_attempt_ids
            else ()
        )
    if {attempt.id for attempt in changed_attempts} != set(changed_attempt_ids):
        raise BlockContractError("analysis-verification-preload")
    context.analysis.prime(changed_attempts, changed_attempt_events)

    call_ids = tuple(item.get("id") for item in call_heads if isinstance(item, dict))
    if len(call_ids) != len(call_heads) or not all(
        isinstance(call_id, str) and call_id for call_id in call_ids
    ):
        raise BlockContractError("research-verification-preload")
    changed_call_ids = tuple(
        cast(str, call_id)
        for item, call_id in zip(call_heads, call_ids, strict=True)
        if (
            call_id not in context._calls
            or call_id not in context._call_keys
            or context._call_keys[cast(str, call_id)][2] != item.get("state_head")
            or context._calls[cast(str, call_id)].call_index != item.get("call_index")
        )
    )
    with active.no_autoflush:
        changed_calls = (
            tuple(
                active.query(TranslationResearchToolCall)
                .populate_existing()
                .filter(TranslationResearchToolCall.id.in_(changed_call_ids))
                .all()
            )
            if changed_call_ids
            else ()
        )
        changed_call_events = (
            tuple(
                active.query(TranslationResearchToolCallEvent)
                .populate_existing()
                .filter(
                    TranslationResearchToolCallEvent.tool_call_id.in_(changed_call_ids)
                )
                .order_by(
                    TranslationResearchToolCallEvent.tool_call_id,
                    TranslationResearchToolCallEvent.sequence,
                )
                .all()
            )
            if changed_call_ids
            else ()
        )
        changed_result_blobs = (
            tuple(
                active.query(TranslationResearchToolResultBlob)
                .populate_existing()
                .filter(
                    TranslationResearchToolResultBlob.tool_call_id.in_(changed_call_ids)
                )
                .all()
            )
            if changed_call_ids
            else ()
        )
    if {call.id for call in changed_calls} != set(changed_call_ids):
        raise BlockContractError("research-verification-preload")
    for source_call in changed_calls:
        call = cast(
            TranslationResearchToolCall,
            _detached_evidence_snapshot(source_call),
        )
        key = _call_verification_key(call)
        previous = context._call_keys.get(call.id)
        if previous is not None and previous != key:
            context._verified_call_events.pop(previous, None)
            context._verified_calls.pop(previous, None)
        context._calls[call.id] = call
        context._call_keys[call.id] = key
    grouped_events: dict[str, list[TranslationResearchToolCallEvent]] = {
        call_id: [] for call_id in changed_call_ids
    }
    for source_event in changed_call_events:
        event = cast(
            TranslationResearchToolCallEvent,
            _detached_evidence_snapshot(source_event),
        )
        if event.tool_call_id not in grouped_events:
            raise BlockContractError("research-event-preload")
        grouped_events[event.tool_call_id].append(event)
    context._call_event_rows.update(
        {
            call_id: tuple(sorted(rows, key=lambda event: event.sequence))
            for call_id, rows in grouped_events.items()
        }
    )
    context._result_blobs.update({call_id: None for call_id in changed_call_ids})
    context._result_blobs.update(
        {
            blob.tool_call_id: _detached_evidence_snapshot(blob)
            for blob in changed_result_blobs
        }
    )

    current_call_ids = tuple(cast(str, call_id) for call_id in call_ids)
    try:
        calls = tuple(context._calls[call_id] for call_id in current_call_ids)
        attempts_present = all(
            attempt_id in context.analysis._attempts for attempt_id in attempt_ids
        )
    except KeyError as exc:
        raise BlockContractError("research-verification-preload") from exc
    if not attempts_present:
        raise BlockContractError("analysis-verification-preload")
    context._run_turn_ids[run.id] = tuple(turn.id for turn in turns)
    context._run_call_ids[run.id] = current_call_ids
    context._calls_by_turn.update(
        {
            turn.id: tuple(call for call in calls if call.turn_id == turn.id)
            for turn in turns
        }
    )
    return turns, calls


def refresh_research_verification_context(
    run: TranslationResearchRun | str,
    *,
    verification_context: ResearchVerificationContext,
    session: Any | None = None,
    verified_plan: CanonicalSourcePlan | None = None,
) -> None:
    """Refresh receipt-keyed snapshots after an active-run commit.

    The write paths deliberately do not mutate an invocation cache before their
    transaction commits. Callers use this bounded refresh after commit so a
    changed attempt or tool call is reloaded while immutable history remains
    reusable. Every refreshed state head is verified before snapshots are
    admitted.
    """

    active = _active_session(session)
    durable = _fresh_run(run, active)
    verification_context._scopes.pop(durable.id, None)
    operation, plan = _verify_run_scope(
        durable,
        active,
        require_live=False,
        verified_plan=verified_plan,
        verification_context=verification_context,
    )
    _preload_research_verification_graph(durable, active, verification_context)
    _cache_research_scope(verification_context, durable, operation, plan)


def rehydrate_research_run(
    run: TranslationResearchRun | str,
    *,
    session: Any | None = None,
    verified_plan: CanonicalSourcePlan | None = None,
    verification_context: ResearchVerificationContext | None = None,
) -> dict[str, Any]:
    active = _active_session(session)
    context = verification_context or ResearchVerificationContext()
    durable = _fresh_run(run, active)
    operation, plan = _verify_run_scope(
        durable,
        active,
        require_live=False,
        verified_plan=verified_plan,
        verification_context=context,
    )
    cache_key = _verification_key(durable)
    if durable.state == "completed":
        cached = context._graphs.get(cache_key)
        if cached is not None:
            return dict(cached)
    for field, hash_field, component in (
        (durable.model_json, durable.model_hash, "model"),
        (durable.settings_json, durable.settings_hash, "settings"),
        (durable.budget_json, durable.budget_hash, "budget"),
        (durable.profile_json, durable.profile_hash, "profile"),
        (durable.selected_provider_ids_json, durable.providers_hash, "providers"),
        (durable.selected_addon_ids_json, durable.addons_hash, "addons"),
        (durable.tool_declarations_json, durable.tools_hash, "tools"),
    ):
        if not _witness_matches(field, hash_field, component):
            raise BlockContractError("research-run-evidence")
    turns, calls = _preload_research_verification_graph(
        durable,
        active,
        context,
    )
    _cache_research_scope(context, durable, operation, plan)
    _verify_run_graph_shape(
        durable,
        turns,
        calls,
        active,
        verified_plan=verified_plan,
        verification_context=context,
    )
    prior_turns: list[TranslationResearchTurn] = []
    for turn_index, turn in enumerate(turns):
        attempt_data = _verify_turn(
            turn,
            durable,
            active,
            plan,
            verified_plan=verified_plan,
            verification_context=context,
        )
        turn_calls = tuple(call for call in calls if call.turn_id == turn.id)
        attempt = context.analysis._attempts.get(turn.analysis_attempt_id)
        if attempt is None:
            raise BlockContractError("research-turn-attempt")
        _verify_next_turn_history(
            durable,
            attempt,
            attempt_data,
            tuple(prior_turns),
            active,
            plan,
            verified_plan=verified_plan,
            verification_context=context,
        )
        prior_turns.append(turn)
        if attempt.dispatch_state != "completed":
            if turn_calls:
                raise BlockContractError("research-turn-history")
            continue
        output = _completed_turn_output(
            turn,
            durable,
            active,
            plan,
            verified_plan=verified_plan,
            verification_context=context,
        )
        requested = output.get("tool_calls")
        if not isinstance(requested, list):
            if turn_calls:
                raise BlockContractError("research-turn-history")
        elif len(requested) != len(turn_calls):
            # A crash after the completed model transcript but before the
            # atomic tool-reservation savepoint leaves zero calls.  That is a
            # recoverable state only for the current last turn of an active run;
            # a non-empty proper prefix is never valid.
            if not (
                len(turn_calls) == 0
                and turn_index == len(turns) - 1
                and durable.state in {"prepared", "running"}
            ):
                raise BlockContractError("research-turn-history")
            continue
        for ordinal, (requested_call, call) in enumerate(
            zip(requested or [], turn_calls, strict=True), start=1
        ):
            function = requested_call.get("function")
            if (
                call.turn_call_index != ordinal
                or call.state not in TOOL_STATES
                or not isinstance(function, dict)
                or requested_call.get("id") != call.provider_call_id
                or function.get("name") != call.tool_name
                or function.get("arguments") != call.raw_arguments_json
            ):
                raise BlockContractError("research-turn-history")
    for call in calls:
        _verify_call(
            call,
            active,
            require_live=False,
            verified_plan=verified_plan,
            verification_context=context,
        )
    if durable.state in {"completed", "failed", "cancelled", "uncertain"}:
        _verify_terminal_run_children(
            durable,
            durable.state,
            active,
            verified_plan=verified_plan,
            verification_context=context,
        )
    _verify_run_state_head(
        durable,
        active,
        verification_context=context,
    )
    restored = {
        "model": _mapping(durable.model_json, "research-model"),
        "settings": _mapping(durable.settings_json, "research-settings"),
        "budget": _mapping(durable.budget_json, "research-budget"),
        "profile": _mapping(durable.profile_json, "research-profile"),
        "selected_provider_ids": _sorted_ids(
            durable.selected_provider_ids_json, "research-providers"
        ),
        "selected_addon_ids": _sorted_ids(
            durable.selected_addon_ids_json, "research-addons"
        ),
        "tool_declarations": _tool_declarations(durable.tool_declarations_json),
        "turns": turns,
        "tool_calls": calls,
    }
    if durable.state == "completed":
        context._graphs[cache_key] = dict(restored)
    return restored


def rehydrate_completed_research_conclusion(
    run: TranslationResearchRun | str,
    *,
    session: Any | None = None,
    verified_plan: CanonicalSourcePlan | None = None,
    verification_context: ResearchVerificationContext | None = None,
) -> dict[str, Any]:
    """Return one admissible, fully witnessed terminal research conclusion.

    A completed run is useful as translation context only when its complete
    graph, state head, and final assistant transcript can be rehydrated. A
    definitely failed read-only executor call, or a call blocked before
    dispatch for an invalid schema or exhausted batch budget, may remain only
    when its witnessed error was returned to the model for correction. Neither
    is successful evidence. Every other blocked/failed/cancelled/uncertain call
    keeps the graph non-admissible.
    """

    active = _active_session(session)
    context = verification_context or ResearchVerificationContext()
    durable = _fresh_run(run, active)
    graph = rehydrate_research_run(
        durable,
        session=active,
        verified_plan=verified_plan,
        verification_context=context,
    )
    if durable.state != "completed":
        raise BlockContractError("research-not-admissible")
    cache_key = _verification_key(durable)
    cached = context._conclusions.get(cache_key)
    if cached is not None:
        return dict(cached)
    turns = graph["turns"]
    calls = graph["tool_calls"]
    if not turns or any(
        not is_admissible_research_tool_history(call) for call in calls
    ):
        raise BlockContractError("research-not-admissible")
    final_turn = turns[-1]
    with active.no_autoflush:
        plan = active.get(
            TranslationSourcePlan, durable.source_plan_id, populate_existing=True
        )
    if plan is None:
        raise BlockContractError("research-not-admissible")
    final_attempt = context.analysis._attempts.get(final_turn.analysis_attempt_id)
    if final_attempt is None:
        final_attempt = active.get(
            TranslationAnalysisAttempt,
            final_turn.analysis_attempt_id,
            populate_existing=True,
        )
    if final_attempt is None or final_attempt.dispatch_state != "completed":
        raise BlockContractError("research-not-admissible")
    conclusion = _completed_turn_output(
        final_turn,
        durable,
        active,
        plan,
        verified_plan=verified_plan,
        verification_context=context,
    )
    requested = conclusion.get("tool_calls")
    if requested not in (None, []):
        raise BlockContractError("research-not-admissible")
    conclusion_hash = hashlib.sha256(
        json.dumps(
            conclusion,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    restored = {
        "conclusion": conclusion,
        "conclusion_hash": conclusion_hash,
        "run_id": durable.id,
        "run_record_hash": durable.run_record_hash,
        "run_state": durable.state,
        "state_head_receipt": durable.state_head_receipt,
        "final_turn_id": final_turn.id,
        "final_turn_record_hash": final_turn.turn_record_hash,
        "final_attempt_id": final_attempt.id,
        "final_attempt_record_hash": final_attempt.attempt_record_hash,
        "final_attempt_request_hash": final_attempt.request_hash,
    }
    context._conclusions[cache_key] = dict(restored)
    return restored


__all__ = [
    "RESEARCH_CONTRACT_VERSION",
    "ResearchVerificationContext",
    "block_prepared_research_tool_call",
    "block_research_tool_call",
    "cancel_research_tool_call",
    "claim_research_tool_call",
    "complete_research_tool_call",
    "fail_research_tool_call",
    "is_admissible_research_tool_history",
    "persist_research_run",
    "prepare_research_tool_calls",
    "reconcile_research_tool_call",
    "rehydrate_completed_research_conclusion",
    "rehydrate_research_run",
    "rehydrate_research_tool_call",
    "rehydrate_research_tool_call_events",
    "rehydrate_research_tool_result_blob",
    "reserve_research_turn",
    "transition_research_run",
]
