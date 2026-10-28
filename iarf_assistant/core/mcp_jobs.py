"""Durable, owner-scoped jobs and external-worker work for MCP products.

This module is deliberately independent of MCP protocol transports.  HTTP
adapters and add-on workers call these transaction-boundary services; no
protocol session or browser cookie state is stored here.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import secrets
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import wraps
from itertools import islice
from pathlib import Path
from typing import Any

import rfc8785
from flask import current_app, has_app_context
from jsonschema import Draft202012Validator, SchemaError, ValidationError
from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import defer

from carlina.core.utc import utc_iso, utc_now_db
from iarf_assistant.core.encryption import keyed_lookup_candidates
from iarf_assistant.database_utils import (
    DatabaseBusyError,
    TransactionScopeError,
    is_database_locked,
    public_error_message,
    run_transaction_with_retry,
)
from iarf_assistant.extensions import db
from iarf_assistant.models import (
    McpAccessKey,
    McpExecution,
    McpWorkClaimReceipt,
    McpWorkItem,
    McpWorkSubmission,
)

MCP_ACCESS_KEY_PURPOSE = "mcp-access-key"
MCP_WORK_SCOPE = "mcp.work"
MCP_ADDON_SCOPES = {
    "doc_processor": "mcp.doc_processor",
    "translator": "mcp.translator",
    "news_pipeline": "mcp.news_pipeline",
}
MCP_ADDON_IDS = frozenset(MCP_ADDON_SCOPES)
MCP_EXECUTION_MODES = frozenset({"managed", "passive"})
MCP_EXECUTION_STATUSES = frozenset(
    {
        "queued",
        "running",
        "waiting_external",
        "waiting_review",
        "suspended",
        "completed",
        "failed",
        "cancelled",
    }
)
MCP_ACTIVE_EXECUTION_STATUSES = frozenset({"queued", "running", "waiting_external"})
MCP_TERMINAL_EXECUTION_STATUSES = frozenset({"completed", "failed", "cancelled"})
MCP_NONTERMINAL_EXECUTION_STATUSES = frozenset(
    MCP_EXECUTION_STATUSES - MCP_TERMINAL_EXECUTION_STATUSES
)
MAX_SCOPE_COUNT = 32
MAX_ALLOWED_ROOT_COUNT = 32
MAX_ALLOWED_ROOT_LENGTH = 4096
MCP_MIN_RESPONSE_BYTES = 64 * 1024
MCP_DEFAULT_RESPONSE_BYTES = 8 * 1024 * 1024
MCP_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_EXECUTION_REQUEST_BYTES = 512 * 1024
# Passive completion packets can contain base64-encoded page images.  Keep the
# execution envelope small, but permit the bounded image transfer budget used
# by the MCP gateway plus JSON/base64 overhead for durable work packets.
MAX_WORK_REQUEST_BYTES = 144 * 1024 * 1024
MAX_BOUNDARY_INPUT_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 512 * 1024
MAX_CONTENT_BYTES = 256 * 1024
MAX_TOOL_CALLS = 32
MAX_TOOL_CALL_ID_LENGTH = 256
MAX_TOOL_NAME_LENGTH = 256
MAX_TOOL_ARGUMENT_BYTES = 128 * 1024
MAX_FINISH_REASON_LENGTH = 64
MAX_LEASE_SECONDS = 3600
MIN_LEASE_SECONDS = 30
DEFAULT_TERMINAL_WORK_REDACTION_BATCH_SIZE = 100
MAX_TERMINAL_WORK_REDACTION_BATCH_SIZE = 1000
MAX_INLINE_TERMINAL_WORK_ITEMS = 128
MAX_VALIDATION_ERRORS = 8
MAX_VALIDATION_PATH_DEPTH = 16
MAX_VALIDATION_PATH_LENGTH = 256
MAX_VALIDATION_PROPERTY_LENGTH = 64
MAX_VALIDATION_ENUM_VALUES = 8
MAX_VALIDATION_ENUM_BYTES = 256
MCP_REQUEST_TOMBSTONE: dict[str, Any] = {
    "schema_version": 1,
    "redacted": True,
    "reason": "work_request_retention",
}
_SCOPE_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")
_WORKER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_TOKEN_RE = re.compile(r"^mcp_[A-Za-z0-9_-]{43}$")
_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_CLAIM_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def passive_work_is_pending(
    *,
    work_item_id: str,
    queue_job_id: str,
    addon_id: str,
    owner_user_id: int,
    resource_kind: str,
    resource_id: str,
) -> bool:
    """Read an exact queue suspension hint without loading encrypted packets.

    This is an optimization for sequential workers, not an authorization or
    completion receipt. Missing, completed, cancelled or mismatched hints fall
    through to the ordinary product fences. A short independent connection
    ensures neither parking nor resumption retains a read transaction.
    """
    if (
        not has_app_context()
        or isinstance(owner_user_id, bool)
        or not isinstance(owner_user_id, int)
        or owner_user_id < 1
        or addon_id not in MCP_ADDON_IDS
        or any(
            not isinstance(value, str) or not value
            for value in (work_item_id, queue_job_id, resource_kind, resource_id)
        )
    ):
        return False
    query = (
        select(McpWorkItem.id)
        .join(McpExecution, McpExecution.id == McpWorkItem.execution_id)
        .where(
            McpWorkItem.id == work_item_id,
            McpWorkItem.state.in_(("pending", "leased")),
            McpExecution.queue_job_id == queue_job_id,
            McpExecution.owner_user_id == owner_user_id,
            McpExecution.addon_id == addon_id,
            McpExecution.resource_kind == resource_kind,
            McpExecution.resource_id == resource_id,
            McpExecution.mode == "passive",
            McpExecution.status.in_(MCP_ACTIVE_EXECUTION_STATUSES),
        )
        .limit(1)
    )
    with db.engine.connect() as connection:
        return connection.execute(query).first() is not None


class McpError(Exception):
    """A safe, bounded public error suitable for a protocol adapter."""

    def __init__(
        self,
        code: str | int,
        detail: str,
        legacy_detail: str | None = None,
        *,
        status_code: int = 400,
        retryable: bool = False,
    ) -> None:
        # The gateway's auth adapter historically constructed these as
        # ``McpAuthError(status, code, detail)``.  Keep that narrow call shape
        # compatible while the service's native form remains ``(code, detail)``.
        if isinstance(code, int):
            status_code = code
            code, detail = detail, legacy_detail or "The MCP operation could not be completed."
        safe_code = str(code or "mcp_error").strip().lower()
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", safe_code) is None:
            safe_code = "mcp_error"
        self.code = safe_code
        self.status_code = int(status_code)
        self.status = self.status_code
        self.retryable = bool(retryable)
        self.detail = str(detail or "The MCP operation could not be completed.")[:512]
        super().__init__(self.detail)


class McpAuthError(McpError):
    """Authentication or owner/scope authorization failed."""


class McpWorkError(McpError):
    """An execution, work item, or submitted result was not accepted."""

    def __init__(
        self,
        code: str | int,
        detail: str,
        legacy_detail: str | None = None,
        *,
        status_code: int = 400,
        retryable: bool = False,
        validation_errors: Iterable[Mapping[str, Any]] | None = None,
    ) -> None:
        super().__init__(
            code,
            detail,
            legacy_detail,
            status_code=status_code,
            retryable=retryable,
        )
        self.validation_errors = _bound_validation_errors(validation_errors)


@dataclass(frozen=True, slots=True)
class McpPrincipal:
    """Immutable authority derived from one active access key."""

    key_id: str
    owner_user_id: int
    scopes: frozenset[str]
    allowed_roots: tuple[str, ...]
    response_default_bytes: int = MCP_DEFAULT_RESPONSE_BYTES
    response_max_bytes: int = MCP_MAX_RESPONSE_BYTES

    def has_scope(self, scope: str) -> bool:
        return str(scope) in self.scopes


@dataclass(frozen=True, slots=True)
class IssuedMcpAccessKey:
    """The one-time raw token response from :func:`issue_access_key`."""

    key_id: str
    owner_user_id: int
    raw_token: str

    @property
    def token(self) -> str:
        """Compatibility alias for callers that use ``token`` terminology."""

        return self.raw_token


@dataclass(frozen=True, slots=True)
class McpWorkResolution:
    """Immutable result of idempotent work creation/lookup."""

    work_item_id: str
    status: str
    response: dict[str, Any] | None
    created: bool


@dataclass(frozen=True, slots=True)
class McpWorkClaim:
    """Authorized work packet plus its newly fenced raw lease token."""

    work_item_id: str
    execution_id: str
    owner_user_id: int
    addon_id: str
    resource_kind: str
    resource_id: str
    queue_job_id: str | None
    mode: str
    execution_status: str
    state: str
    version: int
    attempt_count: int
    lease_owner: str
    lease_expires_at: datetime
    lease_token: str
    request: dict[str, Any]
    workload_role: str = ""
    expected_output_format: str = "text"
    response_schema: dict[str, Any] | None = None
    batch_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_item_id": self.work_item_id,
            "execution_id": self.execution_id,
            "owner_user_id": self.owner_user_id,
            "addon_id": self.addon_id,
            "resource_kind": self.resource_kind,
            "resource_id": self.resource_id,
            "queue_job_id": self.queue_job_id,
            "mode": self.mode,
            "execution_status": self.execution_status,
            "state": self.state,
            "version": self.version,
            "attempt_count": self.attempt_count,
            "lease_owner": self.lease_owner,
            "lease_expires_at": utc_iso(self.lease_expires_at),
            "lease_token": self.lease_token,
            "workload_role": self.workload_role,
            "expected_output_format": self.expected_output_format,
            "response_schema": copy.deepcopy(self.response_schema)
            if self.response_schema is not None
            else None,
            "batch_id": self.batch_id,
            "request": copy.deepcopy(self.request),
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def keys(self):
        return self.to_dict().keys()

    def items(self):
        return self.to_dict().items()


@dataclass(frozen=True, slots=True)
class McpWorkRenewal:
    """Safe lease metadata returned after a fenced renewal."""

    work_item_id: str
    execution_id: str
    version: int
    state: str
    lease_owner: str
    lease_expires_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_item_id": self.work_item_id,
            "execution_id": self.execution_id,
            "version": self.version,
            "state": self.state,
            "lease_owner": self.lease_owner,
            "lease_expires_at": utc_iso(self.lease_expires_at),
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def keys(self):
        return self.to_dict().keys()

    def items(self):
        return self.to_dict().items()


def _canonical_json_bytes(value: Any, *, limit: int) -> bytes:
    try:
        encoded = rfc8785.dumps(value)
    except (
        rfc8785.CanonicalizationError,
        TypeError,
        ValueError,
        OverflowError,
        UnicodeError,
        RecursionError,
    ) as exc:
        raise McpWorkError(
            "invalid_json", "The MCP packet must contain valid JSON.", status_code=400
        ) from exc
    if len(encoded) > limit:
        raise McpWorkError(
            "packet_too_large", "The MCP packet exceeds its size limit.", status_code=413
        )
    return encoded


def _fingerprint_json(value: Any, *, limit: int) -> str:
    return hashlib.sha256(_canonical_json_bytes(value, limit=limit)).hexdigest()


def _claim_request_fingerprint(
    *,
    worker_id: str,
    addon_id: str,
    workload_role: str | None,
    batch_id: str | None,
    lease_seconds: int | None,
) -> str:
    """Fingerprint only the normalized, lease-selection inputs.

    Transport concerns and the execution request are deliberately excluded so
    a lost delivery can recover the same lease without retaining sensitive
    packet material in the receipt.
    """

    return _fingerprint_json(
        {
            "worker_id": worker_id,
            "addon_id": addon_id,
            "workload_role": workload_role,
            "batch_id": batch_id,
            "lease_seconds": lease_seconds,
        },
        limit=1024,
    )


def _normalize_scopes(value: Any) -> frozenset[str]:
    if isinstance(value, (str, bytes)) or not isinstance(
        value, (Sequence, AbstractSet)
    ):
        raise McpAuthError(
            "invalid_scopes", "Access-key scopes are malformed.", status_code=400
        )
    if len(value) > MAX_SCOPE_COUNT:
        raise McpAuthError(
            "invalid_scopes", "Access-key scopes are bounded.", status_code=400
        )
    scopes: list[str] = []
    for raw_scope in value:
        if not isinstance(raw_scope, str) or _SCOPE_RE.fullmatch(raw_scope) is None:
            raise McpAuthError(
                "invalid_scopes", "Access-key scopes are malformed.", status_code=400
            )
        if raw_scope not in scopes:
            scopes.append(raw_scope)
    return frozenset(scopes)


def _normalize_allowed_roots(value: Any) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(
        value, (Sequence, AbstractSet)
    ):
        raise McpAuthError(
            "invalid_allowed_roots", "Access-key allowed roots are malformed.", status_code=400
        )
    if len(value) > MAX_ALLOWED_ROOT_COUNT:
        raise McpAuthError(
            "invalid_allowed_roots", "Access-key allowed roots are bounded.", status_code=400
        )
    roots: list[str] = []
    for raw_root in value:
        if not isinstance(raw_root, str) or not raw_root.strip():
            raise McpAuthError(
                "invalid_allowed_roots", "Access-key allowed roots are malformed.", status_code=400
            )
        if len(raw_root.encode("utf-8")) > MAX_ALLOWED_ROOT_LENGTH:
            raise McpAuthError(
                "invalid_allowed_roots", "Access-key allowed roots are bounded.", status_code=400
            )
        try:
            root_path = Path(raw_root).expanduser()
            if not root_path.is_absolute():
                raise ValueError("root must be absolute")
            canonical = str(root_path.resolve(strict=False))
        except (OSError, RuntimeError, ValueError) as exc:
            raise McpAuthError(
                "invalid_allowed_roots", "Access-key allowed roots are malformed.", status_code=400
            ) from exc
        if canonical not in roots:
            roots.append(canonical)
    return tuple(roots)


def _validate_response_budgets(
    response_default_bytes: Any,
    response_max_bytes: Any,
) -> tuple[int, int]:
    if (
        type(response_default_bytes) is not int
        or type(response_max_bytes) is not int
        or response_default_bytes < MCP_MIN_RESPONSE_BYTES
        or response_default_bytes > MCP_MAX_RESPONSE_BYTES
        or response_max_bytes < MCP_MIN_RESPONSE_BYTES
        or response_max_bytes > MCP_MAX_RESPONSE_BYTES
        or response_default_bytes > response_max_bytes
    ):
        raise McpAuthError(
            "invalid_response_budget",
            "MCP response budgets must be integers from 65536 through "
            "67108864 bytes, with the default no greater than the maximum.",
            status_code=400,
        )
    return response_default_bytes, response_max_bytes


def _validate_label(label: Any) -> str:
    if not isinstance(label, str) or not label.strip() or len(label) > 255:
        raise McpAuthError(
            "invalid_key_label", "The access-key label is invalid.", status_code=400
        )
    return label.strip()


def _validate_token(token: Any) -> str:
    if not isinstance(token, str) or _TOKEN_RE.fullmatch(token) is None:
        raise McpAuthError(
            "invalid_bearer", "The bearer credential is malformed.", status_code=401
        )
    return token


def _validate_lease_seconds(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise McpWorkError(
            "invalid_lease_seconds", "The lease duration is invalid.", status_code=400
        )
    if not MIN_LEASE_SECONDS <= value <= MAX_LEASE_SECONDS:
        raise McpWorkError(
            "invalid_lease_seconds", "The lease duration is outside its bounds.", status_code=400
        )
    return value


def _packet_lease_seconds(addon_id: str, request: Any) -> int:
    """Conservative external-generation allowance, not a runtime prediction.

    Preserve the historical five-minute default for other products. Translation
    packets get 30-60 minutes according to their frozen output allowance. An
    explicit client duration always overrides this policy.
    """
    if addon_id != "translator":
        return 300
    parameters = request.get("parameters", {}) if isinstance(request, dict) else {}
    if not isinstance(parameters, dict):
        parameters = {}
    budgets = [parameters.get(name) for name in ("max_tokens", "max_output_tokens", "max_completion_tokens")]
    output_tokens = max((value for value in budgets if type(value) is int and value > 0), default=0)
    return min(MAX_LEASE_SECONDS, max(1800, 300 + (output_tokens + 7) // 8))


def _validate_worker_id(value: Any) -> str:
    if not isinstance(value, str) or _WORKER_RE.fullmatch(value) is None:
        raise McpWorkError(
            "invalid_worker_id", "The worker identifier is invalid.", status_code=400
        )
    return value


def _validate_batch_id(value: Any) -> str:
    if not isinstance(value, str) or _BATCH_ID_RE.fullmatch(value) is None:
        raise McpWorkError(
            "invalid_batch_id",
            "The batch identifier is invalid.",
            status_code=400,
        )
    return value


def _validate_claim_request_id(value: Any) -> str:
    if not isinstance(value, str) or _CLAIM_REQUEST_ID_RE.fullmatch(value) is None:
        raise McpWorkError(
            "invalid_claim_request_id",
            "The claim request identifier is invalid.",
            status_code=400,
        )
    return value


def _extract_dispatch_index(request: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and copy the bounded query projection of a work dispatch."""

    empty = {
        "batch_id": None,
        "dispatch_phase": None,
        "unit_index": None,
        "is_batch_terminal_unit": False,
        "context_artifact_hash": None,
        "boundary_input_json": None,
    }
    dispatch = request.get("dispatch")
    if dispatch is None:
        return empty
    if not isinstance(dispatch, Mapping):
        raise McpWorkError(
            "invalid_dispatch",
            "The work dispatch must be a JSON object.",
            status_code=400,
        )

    batch_id = dispatch.get("batch_id")
    if batch_id is not None:
        try:
            batch_id = _validate_batch_id(batch_id)
        except McpWorkError as exc:
            raise McpWorkError(
                "invalid_dispatch",
                "The work dispatch batch identifier is invalid.",
                status_code=400,
            ) from exc

    phase = dispatch.get("phase")
    if phase is not None and (
        not isinstance(phase, str) or not phase.strip() or len(phase) > 32
    ):
        raise McpWorkError(
            "invalid_dispatch",
            "The work dispatch phase is invalid.",
            status_code=400,
        )

    unit_index = dispatch.get("unit_index")
    if unit_index is not None and (
        isinstance(unit_index, bool)
        or not isinstance(unit_index, int)
        or unit_index <= 0
    ):
        raise McpWorkError(
            "invalid_dispatch",
            "The work dispatch unit index is invalid.",
            status_code=400,
        )

    terminal = dispatch.get("is_batch_terminal_unit", False)
    if not isinstance(terminal, bool):
        raise McpWorkError(
            "invalid_dispatch",
            "The work dispatch terminal marker is invalid.",
            status_code=400,
        )
    if terminal and (
        request.get("source")
        not in {
            "doc_processor_pdf_parser",
            "doc_processor_pdf_batch",
            "doc_processor_pdf_evaluation",
        }
        or batch_id is None
        or phase != "page_extraction"
        or unit_index is None
    ):
        raise McpWorkError(
            "invalid_dispatch",
            "Only fully indexed parser or evaluator output may terminate a document batch.",
            status_code=400,
        )

    context_hash = dispatch.get("context_artifact_hash")
    if context_hash is not None and (
        not isinstance(context_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", context_hash) is None
    ):
        raise McpWorkError(
            "invalid_dispatch",
            "The work dispatch context artifact hash is invalid.",
            status_code=400,
        )

    boundary_input = dispatch.get("boundary_input")
    if boundary_input is not None:
        try:
            _canonical_json_bytes(boundary_input, limit=MAX_BOUNDARY_INPUT_BYTES)
        except McpWorkError as exc:
            raise McpWorkError(
                "invalid_dispatch",
                "The work dispatch boundary input is invalid or too large.",
                status_code=exc.status_code,
            ) from exc

    return {
        "batch_id": batch_id,
        "dispatch_phase": phase,
        "unit_index": unit_index,
        "is_batch_terminal_unit": terminal,
        "context_artifact_hash": context_hash,
        "boundary_input_json": copy.deepcopy(boundary_input),
    }


def _require_principal(principal: Any) -> McpPrincipal:
    if not isinstance(principal, McpPrincipal):
        raise McpAuthError(
            "unauthorized", "An authenticated MCP principal is required.", status_code=401
        )
    return principal


def _require_work_scope(principal: McpPrincipal, addon_id: str) -> None:
    if not principal.has_scope(MCP_WORK_SCOPE):
        raise McpAuthError(
            "scope_denied", "The MCP principal lacks work scope.", status_code=403
        )
    required = MCP_ADDON_SCOPES.get(addon_id)
    if required is None or not principal.has_scope(required):
        raise McpAuthError(
            "scope_denied", "The MCP principal lacks the add-on work scope.", status_code=403
        )


def _principal_from_key(record: McpAccessKey) -> McpPrincipal:
    if record.status != "active" or record.revoked_at is not None:
        raise McpAuthError(
            "revoked_key", "The MCP access key is revoked.", status_code=401
        )
    try:
        scopes = _normalize_scopes(record.scopes)
        roots = _normalize_allowed_roots(record.allowed_roots)
        response_default_bytes, response_max_bytes = _validate_response_budgets(
            record.response_default_bytes,
            record.response_max_bytes,
        )
    except McpAuthError as exc:
        raise McpAuthError(
            "malformed_key", "The MCP access key record is malformed.", status_code=401
        ) from exc
    return McpPrincipal(
        key_id=str(record.key_id),
        owner_user_id=int(record.owner_user_id),
        scopes=frozenset(scopes),
        allowed_roots=tuple(roots),
        response_default_bytes=response_default_bytes,
        response_max_bytes=response_max_bytes,
    )


def issue_access_key(
    owner_user_id: int,
    label: str,
    scopes: Sequence[str],
    allowed_roots: Sequence[str],
    *,
    response_default_bytes: int = MCP_DEFAULT_RESPONSE_BYTES,
    response_max_bytes: int = MCP_MAX_RESPONSE_BYTES,
) -> IssuedMcpAccessKey:
    """Issue a bearer value, retaining only its installation-keyed digest."""

    try:
        owner_id = int(owner_user_id)
    except (TypeError, ValueError) as exc:
        raise McpAuthError(
            "invalid_owner", "The access-key owner is invalid.", status_code=400
        ) from exc
    normalized_label = _validate_label(label)
    normalized_scopes = _normalize_scopes(scopes)
    normalized_roots = _normalize_allowed_roots(allowed_roots)
    response_default_bytes, response_max_bytes = _validate_response_budgets(
        response_default_bytes,
        response_max_bytes,
    )
    raw_token = f"mcp_{secrets.token_urlsafe(32)}"
    key_id = str(uuid.uuid4())
    record = McpAccessKey(
        key_id=key_id,
        owner_user_id=owner_id,
        label=normalized_label,
        token_digest=keyed_lookup_candidates(
            raw_token, purpose=MCP_ACCESS_KEY_PURPOSE
        )[0],
        scopes=sorted(normalized_scopes),
        allowed_roots=list(normalized_roots),
        response_default_bytes=response_default_bytes,
        response_max_bytes=response_max_bytes,
        status="active",
        created_at=utc_now_db(),
    )
    try:
        db.session.add(record)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return IssuedMcpAccessKey(key_id, owner_id, raw_token)


def resolve_access_key(raw_token: str) -> McpPrincipal:
    """Resolve a bounded bearer value against all retained installation keys."""

    token = _validate_token(raw_token)
    candidates = keyed_lookup_candidates(token, purpose=MCP_ACCESS_KEY_PURPOSE)
    record = (
        McpAccessKey.query.filter(McpAccessKey.token_digest.in_(candidates))
        .order_by(McpAccessKey.created_at.desc())
        .first()
    )
    if record is None:
        raise McpAuthError(
            "unknown_key", "The MCP access key was not found.", status_code=401
        )
    principal = _principal_from_key(record)
    # Resolution participates in the caller's transaction.  It intentionally
    # does not commit or flush unrelated request state.
    record.last_used_at = utc_now_db()
    return principal


def revoke_access_key(
    key_id: str,
    *,
    owner_user_id: int | None = None,
) -> McpAccessKey:
    """Mark an access key revoked; the caller owns the surrounding commit."""

    record = db.session.get(McpAccessKey, str(key_id))
    if record is None or (
        owner_user_id is not None and record.owner_user_id != int(owner_user_id)
    ):
        raise McpAuthError(
            "key_not_found", "The MCP access key was not found.", status_code=404
        )
    if record.status == "active":
        record.status = "revoked"
        record.revoked_at = utc_now_db()
    return record


def grant_access_key_scopes(
    key_id: str,
    *,
    owner_user_id: int,
    scopes: Sequence[str],
) -> McpAccessKey:
    """Grant additional scopes on one active owner-scoped access key.

    The caller owns the surrounding commit.  The scope update is fenced by
    the key's original JSON value so concurrent grants cannot overwrite one
    another or silently revive a key that was revoked in the meantime.
    """

    requested_scopes = _normalize_scopes(scopes)
    if not requested_scopes:
        raise McpAuthError(
            "invalid_scopes", "Access-key scopes are malformed.", status_code=400
        )
    record = db.session.get(McpAccessKey, str(key_id))
    if record is None or record.owner_user_id != int(owner_user_id):
        raise McpAuthError(
            "key_not_found", "The MCP access key was not found.", status_code=404
        )
    if record.status != "active" or record.revoked_at is not None:
        raise McpAuthError(
            "revoked_key", "The MCP access key is revoked.", status_code=401
        )

    original_scopes_json = record.scopes
    current_scopes = _normalize_scopes(original_scopes_json)
    merged_scopes = _normalize_scopes(current_scopes | requested_scopes)
    changed = db.session.execute(
        update(McpAccessKey)
        .where(
            McpAccessKey.key_id == str(key_id),
            McpAccessKey.owner_user_id == int(owner_user_id),
            McpAccessKey.status == "active",
            McpAccessKey.revoked_at.is_(None),
            McpAccessKey.scopes == original_scopes_json,
        )
        .values(scopes=sorted(merged_scopes))
        .execution_options(synchronize_session=False)
    ).rowcount
    if changed != 1:
        raise McpAuthError(
            "key_changed",
            "The MCP access key changed. Read its current state and retry.",
            status_code=409,
        )
    db.session.flush()
    db.session.expire(record)
    return db.session.get(McpAccessKey, record.key_id) or record


def configure_access_key_transport(
    key_id: str,
    *,
    owner_user_id: int,
    response_default_bytes: int,
    response_max_bytes: int,
) -> McpAccessKey:
    """Configure budgets on one active owner-scoped key; caller commits."""

    try:
        owner_id = int(owner_user_id)
    except (TypeError, ValueError) as exc:
        raise McpAuthError(
            "invalid_owner", "The access-key owner is invalid.", status_code=400
        ) from exc
    response_default_bytes, response_max_bytes = _validate_response_budgets(
        response_default_bytes,
        response_max_bytes,
    )
    normalized_key_id = str(key_id)
    record = db.session.get(McpAccessKey, normalized_key_id)
    if record is None or record.owner_user_id != owner_id:
        raise McpAuthError(
            "key_not_found", "The MCP access key was not found.", status_code=404
        )
    if record.status != "active" or record.revoked_at is not None:
        raise McpAuthError(
            "revoked_key", "The MCP access key is revoked.", status_code=401
        )

    original_default_bytes = record.response_default_bytes
    original_max_bytes = record.response_max_bytes
    changed = db.session.execute(
        update(McpAccessKey)
        .where(
            McpAccessKey.key_id == normalized_key_id,
            McpAccessKey.owner_user_id == owner_id,
            McpAccessKey.status == "active",
            McpAccessKey.revoked_at.is_(None),
            McpAccessKey.response_default_bytes == original_default_bytes,
            McpAccessKey.response_max_bytes == original_max_bytes,
        )
        .values(
            response_default_bytes=response_default_bytes,
            response_max_bytes=response_max_bytes,
        )
        .execution_options(synchronize_session=False)
    ).rowcount
    if changed != 1:
        raise McpAuthError(
            "key_changed",
            "The MCP access key changed. Read its current state and retry.",
            status_code=409,
        )
    db.session.flush()
    db.session.expire(record)
    return db.session.get(McpAccessKey, normalized_key_id) or record


def authorize_source_path(
    principal: McpPrincipal,
    requested_path: str | Path,
) -> Path:
    """Resolve a regular source file and enforce canonical allowed-root scope."""

    authority = _require_principal(principal)
    try:
        candidate = Path(requested_path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise McpAuthError(
            "source_not_allowed", "The requested source path is unavailable.", status_code=403
        ) from exc
    if not candidate.is_file():
        raise McpAuthError(
            "source_not_allowed", "The requested source is not a regular file.", status_code=403
        )
    try:
        allowed = any(candidate.is_relative_to(Path(root)) for root in authority.allowed_roots)
    except (OSError, RuntimeError, ValueError) as exc:
        raise McpAuthError(
            "source_not_allowed", "The requested source path is unavailable.", status_code=403
        ) from exc
    if not allowed:
        raise McpAuthError(
            "source_not_allowed",
            "The requested source is outside allowed roots. Call carlina_status "
            "to inspect this connection's authorized local import directories.",
            status_code=403,
        )
    return candidate


def _validate_execution_request(
    value: Any,
    *,
    limit: int = MAX_EXECUTION_REQUEST_BYTES,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise McpWorkError(
            "invalid_request", "The execution request must be a JSON object.", status_code=400
        )
    _canonical_json_bytes(value, limit=limit)
    return copy.deepcopy(value)


def create_execution(
    owner_user_id: int,
    addon_id: str,
    resource_kind: str,
    resource_id: str,
    request_json: dict[str, Any] | None = None,
    *,
    mode: str = "managed",
    queue_job_id: str | None = None,
    request: dict[str, Any] | None = None,
    deduplicate_queue: bool = False,
) -> McpExecution:
    """Create and commit one durable execution before optional queue binding."""

    if addon_id not in MCP_ADDON_IDS:
        raise McpWorkError(
            "invalid_addon", "The MCP add-on is not supported for this operation.", status_code=400
        )
    if mode not in MCP_EXECUTION_MODES:
        raise McpWorkError(
            "invalid_mode", "The MCP execution mode is invalid.", status_code=400
        )
    for field_name, value, limit in (
        ("resource_kind", resource_kind, 64),
        ("resource_id", resource_id, 255),
    ):
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise McpWorkError(
                "invalid_resource", f"The {field_name} is invalid.", status_code=400
            )
    if queue_job_id is not None and (
        not isinstance(queue_job_id, str) or not queue_job_id or len(queue_job_id) > 64
    ):
        raise McpWorkError(
            "invalid_queue_job", "The queue job identifier is invalid.", status_code=400
        )
    if request_json is not None and request is not None:
        raise McpWorkError(
            "invalid_request", "Provide only one execution request payload.", status_code=400
        )
    request_payload = request_json if request_json is not None else request
    request_payload = _validate_execution_request(request_payload)
    if type(deduplicate_queue) is not bool or (deduplicate_queue and queue_job_id is None):
        raise McpWorkError("invalid_queue_job", "Queue deduplication requires a queue job identifier.", status_code=400)

    def equivalent(existing):
        return (
            existing.owner_user_id == int(owner_user_id) and existing.addon_id == addon_id
            and existing.resource_kind == resource_kind and existing.resource_id == resource_id
            and existing.queue_job_id == queue_job_id and existing.mode == mode
            and existing.request_json == request_payload
        )

    identity = str(uuid.uuid4())
    if deduplicate_queue:
        identity = str(uuid.uuid5(uuid.NAMESPACE_URL, f"carlina:mcp:queue:{owner_user_id}:{addon_id}:{queue_job_id}"))
        existing = execution_for_queue_job(queue_job_id, owner_user_id=owner_user_id, addon_id=addon_id)
        if existing is not None:
            if not equivalent(existing):
                raise McpWorkError("execution_conflict", "The queue binding belongs to a different execution intent.", status_code=409)
            return existing
    now = utc_now_db()
    record = McpExecution(
        id=identity,
        owner_user_id=int(owner_user_id),
        addon_id=addon_id,
        resource_kind=resource_kind,
        resource_id=resource_id,
        queue_job_id=queue_job_id,
        mode=mode,
        status="queued",
        request_json=request_payload,
        created_at=now,
        updated_at=now,
    )
    try:
        db.session.add(record)
        db.session.commit()
    except Exception:
        db.session.rollback()
        if deduplicate_queue:
            existing = db.session.get(McpExecution, identity)
            if existing is not None and equivalent(existing):
                return existing
        raise
    return record


def get_execution(
    execution_id: str,
    *,
    principal: McpPrincipal | None = None,
    owner_user_id: int | None = None,
) -> McpExecution | None:
    """Fetch an execution without exposing another owner's record."""

    record = db.session.get(McpExecution, str(execution_id))
    if record is None:
        return None
    if owner_user_id is not None and record.owner_user_id != int(owner_user_id):
        return None
    if principal is not None and record.owner_user_id != int(principal.owner_user_id):
        return None
    return record


def execution_for_queue_job(
    queue_job_id: str,
    *,
    principal: McpPrincipal | None = None,
    owner_user_id: int | None = None,
    addon_id: str | None = None,
) -> McpExecution | None:
    """Resolve a queue binding while enforcing optional owner/add-on scope."""

    query = McpExecution.query.filter(McpExecution.queue_job_id == str(queue_job_id))
    if owner_user_id is not None:
        query = query.filter(McpExecution.owner_user_id == int(owner_user_id))
    if principal is not None:
        query = query.filter(McpExecution.owner_user_id == int(principal.owner_user_id))
    if addon_id is not None:
        query = query.filter(McpExecution.addon_id == addon_id)
    return query.order_by(McpExecution.created_at.asc(), McpExecution.id.asc()).first()


def serialize_execution(
    execution: McpExecution,
    *,
    include_request: bool = False,
) -> dict[str, Any]:
    """Serialize non-secret execution metadata; request content is opt-in."""

    payload: dict[str, Any] = {
        "id": execution.id,
        "execution_id": execution.id,
        "owner_user_id": execution.owner_user_id,
        "addon_id": execution.addon_id,
        "resource_kind": execution.resource_kind,
        "resource_id": execution.resource_id,
        "queue_job_id": execution.queue_job_id,
        "mode": execution.mode,
        "status": execution.status,
        "created_at": utc_iso(execution.created_at) if execution.created_at else None,
        "updated_at": utc_iso(execution.updated_at) if execution.updated_at else None,
        "started_at": utc_iso(execution.started_at) if execution.started_at else None,
        "completed_at": utc_iso(execution.completed_at) if execution.completed_at else None,
    }
    if include_request:
        payload["request"] = copy.deepcopy(execution.request_json)
    return payload


def list_executions(
    principal: McpPrincipal,
    *,
    status: str | None = None,
    addon_id: str | None = None,
    mode: str | None = None,
    limit: int = 50,
    offset: int = 0,
    include_terminal: bool = True,
) -> list[dict[str, Any]]:
    """List executions owned by the principal, bounded by authorized scopes."""

    authority = _require_principal(principal)
    # The MCP gateway may request one private lookahead row to calculate
    # pagination for a public page of at most 100 executions.
    limit = min(101, max(1, int(limit)))
    offset = max(0, int(offset))

    allowed_addons = [
        aid for aid in MCP_ADDON_IDS if authority.has_scope(f"mcp.{aid}")
    ]
    if not allowed_addons:
        return []

    query = McpExecution.query.filter(
        McpExecution.owner_user_id == int(authority.owner_user_id),
        McpExecution.addon_id.in_(allowed_addons),
    )
    if addon_id:
        normalized_addon = str(addon_id).strip()
        if normalized_addon not in allowed_addons:
            return []
        query = query.filter(McpExecution.addon_id == normalized_addon)
    if status:
        normalized_status = str(status).strip().lower()
        query = query.filter(McpExecution.status == normalized_status)
    elif not include_terminal:
        query = query.filter(
            McpExecution.status.in_(MCP_NONTERMINAL_EXECUTION_STATUSES)
        )
    if mode:
        normalized_mode = str(mode).strip().lower()
        query = query.filter(McpExecution.mode == normalized_mode)

    executions = (
        query.order_by(McpExecution.created_at.desc(), McpExecution.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    if not executions:
        return []

    exec_ids = [e.id for e in executions]
    now = utc_now_db()
    pending_items = (
        db.session.query(McpWorkItem.execution_id, func.count(McpWorkItem.id))
        .filter(
            McpWorkItem.execution_id.in_(exec_ids),
            or_(
                McpWorkItem.state == "pending",
                and_(McpWorkItem.state == "leased", McpWorkItem.lease_expires_at <= now),
            ),
        )
        .group_by(McpWorkItem.execution_id)
        .all()
    )
    pending_map = dict(pending_items)

    results: list[dict[str, Any]] = []
    for execution in executions:
        payload = serialize_execution(execution, include_request=False)
        payload["pending_work_count"] = int(pending_map.get(execution.id, 0))
        results.append(payload)
    return results


def _normalized_batch_plan(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    try:
        from ..addons.doc_processor.batching import normalize_document_batch_plan

        normalized = normalize_document_batch_plan(value)
    except (TypeError, ValueError):
        return None
    if normalized is None or normalized != value:
        return None
    batches = normalized.get("batches")
    if not isinstance(batches, list):
        return None
    batch_ids = {
        batch.get("batch_id")
        for batch in batches
        if isinstance(batch, Mapping)
    }
    if len(batch_ids) != len(batches):
        return None
    for batch in batches:
        if not isinstance(batch, Mapping):
            return None
        dependencies = batch.get("depends_on")
        if not isinstance(dependencies, list) or any(
            dependency not in batch_ids or dependency == batch.get("batch_id")
            for dependency in dependencies
        ):
            return None
    return copy.deepcopy(dict(normalized))


def project_execution_batches(
    execution: McpExecution | str,
    *,
    completed_batch_ids: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Return a safe batch-state projection for one immutable execution plan."""

    record = _execution_record(execution)
    request = record.request_json
    plan = _normalized_batch_plan(
        request.get("batch_plan") if isinstance(request, Mapping) else None
    )
    if plan is None:
        return []

    batch_ids = tuple(str(batch["batch_id"]) for batch in plan["batches"])
    completed_without_work = {
        str(batch_id) for batch_id in completed_batch_ids if str(batch_id) in batch_ids
    }
    work_items = (
        db.session.query(
            McpWorkItem.id,
            McpWorkItem.state,
            McpWorkItem.lease_expires_at,
            McpWorkItem.created_at,
            McpWorkItem.batch_id,
            McpWorkItem.unit_index,
            McpWorkItem.is_batch_terminal_unit,
            McpWorkItem.context_artifact_hash,
            McpWorkItem.boundary_input_json,
        )
        .filter(
            McpWorkItem.execution_id == record.id,
            McpWorkItem.dispatch_phase == "page_extraction",
            McpWorkItem.batch_id.in_(batch_ids),
        )
        .order_by(McpWorkItem.created_at.asc(), McpWorkItem.id.asc())
        .all()
    )
    by_batch: dict[str, list[Any]] = {
        str(batch["batch_id"]): [] for batch in plan["batches"]
    }
    for work in work_items:
        if work.batch_id in by_batch:
            by_batch[work.batch_id].append(work)

    now = utc_now_db()
    projections: list[dict[str, Any]] = []
    for batch in plan["batches"]:
        batch_id = str(batch["batch_id"])
        entries = by_batch[batch_id]
        completed_count = sum(work.state == "completed" for work in entries)
        pending_count = sum(work.state == "pending" for work in entries)
        leased_count = sum(
            work.state == "leased"
            and work.lease_expires_at is not None
            and work.lease_expires_at > now
            for work in entries
        )
        reclaimable_count = sum(
            work.state == "leased"
            and (
                work.lease_expires_at is None or work.lease_expires_at <= now
            )
            for work in entries
        )
        has_terminal_unit = any(
            work.state == "completed"
            and work.is_batch_terminal_unit is True
            for work in entries
        )
        has_completed_page = completed_count > 0
        # Transport acceptance precedes parser validation. A rejected terminal
        # response can therefore coexist with an outstanding validation retry.
        if leased_count:
            state = "leased"
        elif pending_count or reclaimable_count:
            state = "waiting_external"
        elif has_terminal_unit or (not entries and batch_id in completed_without_work):
            state = "completed"
        elif has_completed_page:
            state = "running"
        elif record.status in MCP_TERMINAL_EXECUTION_STATUSES:
            state = record.status
        else:
            state = "planned"

        ordered_entries = sorted(
            entries,
            key=lambda entry: (
                entry.unit_index
                if isinstance(entry.unit_index, int)
                and not isinstance(entry.unit_index, bool)
                else int(batch["start_page"]),
                entry.created_at,
                entry.id,
            ),
        )
        projection: dict[str, Any] = {
            **copy.deepcopy(dict(batch)),
            "state": state,
            "counts": {
                "total": len(entries),
                "completed": completed_count,
                "pending": pending_count,
                "leased": leased_count,
                "reclaimable": reclaimable_count,
            },
        }
        for work in ordered_entries:
            if work.context_artifact_hash:
                projection["context_artifact_hash"] = work.context_artifact_hash
                break
        if ordered_entries:
            boundary_input = ordered_entries[0].boundary_input_json
            if boundary_input is not None:
                projection["boundary_input"] = copy.deepcopy(boundary_input)
        projections.append(projection)

    projections_by_id = {projection["batch_id"]: projection for projection in projections}
    resolved_states: dict[str, str] = {}

    def resolve_state(batch_id: str, visiting: frozenset[str] = frozenset()) -> str:
        if batch_id in resolved_states:
            return resolved_states[batch_id]
        if batch_id in visiting:
            return "blocked"
        projection = projections_by_id[batch_id]
        if projection["counts"]["total"]:
            resolved_states[batch_id] = projection["state"]
            return projection["state"]
        if projection["state"] in MCP_TERMINAL_EXECUTION_STATUSES:
            resolved_states[batch_id] = projection["state"]
            return projection["state"]
        dependencies = projection["depends_on"]
        state = (
            "planned"
            if all(
                resolve_state(dependency, visiting | {batch_id}) == "completed"
                for dependency in dependencies
            )
            else "blocked"
        )
        resolved_states[batch_id] = state
        projection["state"] = state
        return state

    for projection in projections:
        resolve_state(projection["batch_id"])
    return projections


def _execution_record(execution: McpExecution | str) -> McpExecution:
    record = execution if isinstance(execution, McpExecution) else db.session.get(McpExecution, str(execution))
    if record is None:
        raise McpWorkError("execution_not_found", "The MCP execution was not found.", status_code=404)
    return record


def bind_execution_queue_job(
    execution: McpExecution | str,
    queue_job_id: str,
) -> McpExecution:
    """Bind a queue id once with a SQL conditional update; caller commits."""

    if not isinstance(queue_job_id, str) or not queue_job_id or len(queue_job_id) > 64:
        raise McpWorkError("invalid_queue_job", "The queue job identifier is invalid.", status_code=400)
    record = _execution_record(execution)
    if record.queue_job_id == queue_job_id:
        return record
    if record.queue_job_id is not None:
        raise McpWorkError("queue_job_conflict", "The execution is already bound to another queue job.", status_code=409)
    changed = db.session.execute(
        update(McpExecution)
        .where(McpExecution.id == record.id, McpExecution.queue_job_id.is_(None))
        .values(queue_job_id=queue_job_id, updated_at=utc_now_db())
        .execution_options(synchronize_session=False)
    ).rowcount
    if changed != 1:
        db.session.expire(record)
        current = db.session.get(McpExecution, record.id)
        if current is not None and current.queue_job_id == queue_job_id:
            return current
        raise McpWorkError("queue_job_conflict", "The execution is already bound to another queue job.", status_code=409)
    db.session.flush()
    db.session.expire(record)
    return db.session.get(McpExecution, record.id) or record


def transition_execution(
    execution: McpExecution | str,
    status: str,
    *,
    expected_status: str | None = None,
) -> McpExecution:
    """Perform an owner-agnostic status CAS; caller commits the transaction."""

    if status not in MCP_EXECUTION_STATUSES:
        raise McpWorkError("invalid_status", "The MCP execution status is invalid.", status_code=400)
    record = _execution_record(execution)
    current_status = record.status
    expected = current_status if expected_status is None else expected_status
    if expected not in MCP_EXECUTION_STATUSES:
        raise McpWorkError("invalid_status", "The expected execution status is invalid.", status_code=400)
    if current_status == status and expected == current_status:
        if status in MCP_TERMINAL_EXECUTION_STATUSES:
            _redact_terminal_execution_work(record.id, utc_now_db())
            db.session.flush()
        elif status == "suspended":
            _release_suspended_execution_work(record.id, utc_now_db())
            db.session.flush()
        return record
    if current_status in MCP_TERMINAL_EXECUTION_STATUSES:
        raise McpWorkError(
            "execution_terminal",
            "A terminal MCP execution cannot transition to another state.",
            status_code=409,
        )
    now = utc_now_db()
    values: dict[Any, Any] = {McpExecution.status: status, McpExecution.updated_at: now}
    if status == "running" and record.started_at is None:
        values[McpExecution.started_at] = now
    if status in {"completed", "failed", "cancelled"}:
        values[McpExecution.completed_at] = now
    changed = db.session.execute(
        update(McpExecution)
        .where(McpExecution.id == record.id, McpExecution.status == expected)
        .values(values)
        .execution_options(synchronize_session=False)
    ).rowcount
    if changed != 1:
        raise McpWorkError("execution_conflict", "The MCP execution changed concurrently.", status_code=409, retryable=True)
    if status in MCP_TERMINAL_EXECUTION_STATUSES:
        _redact_terminal_execution_work(record.id, now)
    elif status == "suspended":
        _release_suspended_execution_work(record.id, now)
    db.session.flush()
    db.session.expire(record)
    return db.session.get(McpExecution, record.id) or record


def _release_suspended_execution_work(execution_id: str, now: datetime) -> int:
    """Fence leased work while retaining its encrypted packet and history."""

    return int(
        db.session.execute(
            update(McpWorkItem)
            .where(
                McpWorkItem.execution_id == execution_id,
                McpWorkItem.state == "leased",
            )
            .values(
                state="pending",
                lease_token=None,
                lease_owner=None,
                lease_expires_at=None,
                leased_at=None,
                updated_at=now,
                version=McpWorkItem.version + 1,
            )
            .execution_options(synchronize_session=False)
        ).rowcount
        or 0
    )


def _redact_terminal_execution_work(execution_id: str, now: datetime) -> int:
    """Bound and redact unfinished work after the execution-level fence."""

    target_ids = (
        select(McpWorkItem.id)
        .where(
            McpWorkItem.execution_id == execution_id,
            McpWorkItem.state.in_(("pending", "leased")),
        )
        .order_by(McpWorkItem.id.asc())
        .limit(MAX_INLINE_TERMINAL_WORK_ITEMS)
    )

    return int(
        db.session.execute(
            update(McpWorkItem)
            .where(
                McpWorkItem.id.in_(target_ids),
                McpWorkItem.execution_id == execution_id,
                McpWorkItem.state.in_(("pending", "leased")),
            )
            .values(
                state="cancelled",
                request_json=copy.deepcopy(MCP_REQUEST_TOMBSTONE),
                request_pruned_at=now,
                lease_token=None,
                lease_owner=None,
                lease_expires_at=None,
                leased_at=None,
                updated_at=now,
                completed_at=now,
                version=McpWorkItem.version + 1,
            )
            .execution_options(synchronize_session=False)
        ).rowcount
        or 0
    )


def _validated_terminal_redaction_batch_size(value: int) -> int:
    if isinstance(value, bool):
        raise McpWorkError(
            "invalid_batch_size",
            "The terminal-work redaction batch size is invalid.",
            status_code=400,
        )
    try:
        batch_size = int(value)
    except (TypeError, ValueError) as exc:
        raise McpWorkError(
            "invalid_batch_size",
            "The terminal-work redaction batch size is invalid.",
            status_code=400,
        ) from exc
    if not 1 <= batch_size <= MAX_TERMINAL_WORK_REDACTION_BATCH_SIZE:
        raise McpWorkError(
            "invalid_batch_size",
            "The terminal-work redaction batch size is invalid.",
            status_code=400,
        )
    return batch_size


def _validated_redaction_owner_user_id(owner_user_id: int | None) -> int | None:
    if owner_user_id is None:
        return None
    if isinstance(owner_user_id, bool):
        raise McpWorkError(
            "invalid_user_id", "The MCP owner user id is invalid.", status_code=400
        )
    try:
        normalized = int(owner_user_id)
    except (TypeError, ValueError) as exc:
        raise McpWorkError(
            "invalid_user_id", "The MCP owner user id is invalid.", status_code=400
        ) from exc
    if normalized < 1:
        raise McpWorkError(
            "invalid_user_id", "The MCP owner user id is invalid.", status_code=400
        )
    return normalized


def _terminal_work_filters(owner_user_id: int | None) -> list[Any]:
    filters: list[Any] = [
        McpExecution.status.in_(tuple(MCP_TERMINAL_EXECUTION_STATUSES)),
        McpWorkItem.request_pruned_at.is_(None),
    ]
    if owner_user_id is not None:
        filters.append(McpExecution.owner_user_id == owner_user_id)
    return filters


def _terminal_work_query(owner_user_id: int | None):
    return (
        select(McpWorkItem.id)
        .join(McpExecution, McpExecution.id == McpWorkItem.execution_id)
        .where(*_terminal_work_filters(owner_user_id))
    )


def redact_terminal_work(
    *,
    owner_user_id: int | None = None,
    batch_size: int = DEFAULT_TERMINAL_WORK_REDACTION_BATCH_SIZE,
    dry_run: bool = False,
) -> dict[str, int]:
    """Redact historical terminal work in bounded, idempotent transactions."""

    normalized_batch_size = _validated_terminal_redaction_batch_size(batch_size)
    normalized_owner_user_id = _validated_redaction_owner_user_id(owner_user_id)
    if _dirty_session():
        raise McpWorkError(
            "dirty_session",
            "Commit unrelated state before redacting terminal MCP work.",
            status_code=409,
            retryable=False,
        )

    candidate_query = _terminal_work_query(normalized_owner_user_id)
    if dry_run:
        count, request_bytes = db.session.execute(
            select(
                func.count(McpWorkItem.id),
                func.coalesce(func.sum(func.length(McpWorkItem.request_json)), 0),
            )
            .select_from(McpWorkItem)
            .join(McpExecution, McpExecution.id == McpWorkItem.execution_id)
            .where(*_terminal_work_filters(normalized_owner_user_id))
        ).one()
        rows = int(count or 0)
        return {
            "rows_eligible": rows,
            "rows_redacted": 0,
            "encrypted_request_bytes_before_redaction": int(request_bytes or 0),
            "batches": (rows + normalized_batch_size - 1) // normalized_batch_size,
        }

    rows_redacted = 0
    request_bytes_before_redaction = 0
    batches = 0
    terminal_execution_ids = select(McpExecution.id).where(
        McpExecution.status.in_(tuple(MCP_TERMINAL_EXECUTION_STATUSES))
    )
    if normalized_owner_user_id is not None:
        terminal_execution_ids = terminal_execution_ids.where(
            McpExecution.owner_user_id == normalized_owner_user_id
        )
    while True:
        ids = db.session.execute(
            candidate_query.order_by(McpWorkItem.id.asc()).limit(normalized_batch_size)
        ).scalars().all()
        if not ids:
            break
        batch_bytes = db.session.execute(
            select(func.coalesce(func.sum(func.length(McpWorkItem.request_json)), 0))
            .select_from(McpWorkItem)
            .where(
                McpWorkItem.id.in_(ids),
                McpWorkItem.request_pruned_at.is_(None),
                McpWorkItem.execution_id.in_(terminal_execution_ids),
            )
        ).scalar_one()
        try:
            now = utc_now_db()
            changed = int(
                db.session.execute(
                    update(McpWorkItem)
                    .where(
                        McpWorkItem.id.in_(ids),
                        McpWorkItem.request_pruned_at.is_(None),
                        McpWorkItem.execution_id.in_(terminal_execution_ids),
                    )
                    .values(
                        state=case(
                            (
                                McpWorkItem.state.in_(("pending", "leased")),
                                "cancelled",
                            ),
                            else_=McpWorkItem.state,
                        ),
                        request_json=copy.deepcopy(MCP_REQUEST_TOMBSTONE),
                        request_pruned_at=now,
                        lease_token=None,
                        lease_owner=None,
                        lease_expires_at=None,
                        leased_at=None,
                        completed_at=case(
                            (
                                McpWorkItem.state.in_(("pending", "leased")),
                                now,
                            ),
                            else_=McpWorkItem.completed_at,
                        ),
                        updated_at=now,
                        version=McpWorkItem.version + 1,
                    )
                    .execution_options(synchronize_session=False)
                ).rowcount
                or 0
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        rows_redacted += changed
        request_bytes_before_redaction += int(batch_bytes or 0)
        batches += 1
    return {
        "rows_eligible": rows_redacted,
        "rows_redacted": rows_redacted,
        "encrypted_request_bytes_before_redaction": request_bytes_before_redaction,
        "batches": batches,
    }


def _active_execution_ids(execution_id: str):
    return select(McpExecution.id).where(
        McpExecution.id == execution_id,
        McpExecution.status.in_(tuple(MCP_ACTIVE_EXECUTION_STATUSES)),
    )


def _require_active_execution(execution: McpExecution) -> None:
    if execution.status not in MCP_ACTIVE_EXECUTION_STATUSES:
        raise McpWorkError(
            "execution_terminal",
            "The MCP execution no longer accepts work.",
            status_code=409,
        )


def _fence_work_creation_to_active_execution(execution_id: str) -> None:
    """Serialize work creation against an execution becoming terminal."""

    changed = db.session.execute(
        update(McpExecution)
        .where(
            McpExecution.id == execution_id,
            McpExecution.status.in_(tuple(MCP_ACTIVE_EXECUTION_STATUSES)),
        )
        .values(updated_at=McpExecution.updated_at)
        .execution_options(synchronize_session=False)
    ).rowcount
    if changed != 1:
        db.session.rollback()
        raise McpWorkError(
            "execution_terminal",
            "The MCP execution cannot receive new work.",
            status_code=409,
        )


def cancel_execution(
    principal: McpPrincipal,
    execution_id: str,
) -> dict[str, Any]:
    """Cancel one owner-scoped execution and fence all unfinished work."""

    authority = _require_principal(principal)
    execution = get_execution(str(execution_id), principal=authority)
    if execution is None:
        raise McpWorkError(
            "execution_not_found", "The MCP execution was not found.", status_code=404
        )
    _require_work_scope(authority, execution.addon_id)
    if execution.status == "cancelled":
        try:
            _redact_terminal_execution_work(execution.id, utc_now_db())
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        fresh = get_execution(execution.id, principal=authority)
        if fresh is None:
            raise McpWorkError(
                "execution_not_found", "The MCP execution was not found.", status_code=404
            )
        payload = serialize_execution(fresh)
        payload["cancelled_work_count"] = 0
        return payload
    if execution.status in {"completed", "failed"}:
        raise McpWorkError(
            "execution_terminal",
            "A completed or failed MCP execution cannot be cancelled.",
            status_code=409,
        )

    now = utc_now_db()
    changed = db.session.execute(
        update(McpExecution)
        .where(
            McpExecution.id == execution.id,
            McpExecution.owner_user_id == int(authority.owner_user_id),
            McpExecution.status.notin_(tuple(MCP_TERMINAL_EXECUTION_STATUSES)),
        )
        .values(status="cancelled", updated_at=now, completed_at=now)
        .execution_options(synchronize_session=False)
    ).rowcount
    if changed != 1:
        db.session.rollback()
        fresh = get_execution(execution.id, principal=authority)
        if fresh is not None and fresh.status == "cancelled":
            try:
                _redact_terminal_execution_work(fresh.id, utc_now_db())
                db.session.commit()
            except Exception:
                db.session.rollback()
                raise
            fresh = get_execution(fresh.id, principal=authority)
            if fresh is None:
                raise McpWorkError(
                    "execution_not_found", "The MCP execution was not found.", status_code=404
                )
            payload = serialize_execution(fresh)
            payload["cancelled_work_count"] = 0
            return payload
        raise McpWorkError(
            "execution_conflict",
            "The MCP execution changed concurrently.",
            status_code=409,
            retryable=True,
        )

    # The helper returns only work fenced from pending/leased states, keeping
    # this existing API count independent of completed-history redaction.
    cancelled_work_count = _redact_terminal_execution_work(execution.id, now)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    fresh = get_execution(execution.id, principal=authority)
    if fresh is None:
        raise McpWorkError(
            "execution_not_found", "The MCP execution was not found.", status_code=404
        )
    payload = serialize_execution(fresh)
    payload["cancelled_work_count"] = cancelled_work_count
    return payload


def _dirty_session() -> bool:
    session = db.session()
    return bool(session.new or session.dirty or session.deleted)


def _resolution_for(record: McpWorkItem, *, created: bool) -> McpWorkResolution:
    response = None
    if record.state == "completed":
        candidate = record.accepted_response_json
        if isinstance(candidate, dict):
            response = copy.deepcopy(candidate)
    return McpWorkResolution(
        work_item_id=str(record.id),
        status=str(record.state),
        response=response,
        created=created,
    )


def has_work_for_request(
    execution_id: str,
    request_packet: Mapping[str, Any],
) -> bool:
    """Read-only exact fingerprint lookup for one execution.

    Returns True when a work item already exists for the exact canonical
    request bytes. Performs no writes and commits nothing; callers must
    still go through :func:`get_or_create_work` for creation or reuse so
    all transaction, owner, scope, and concurrency gates apply.
    """

    request = _validate_execution_request(dict(request_packet), limit=MAX_WORK_REQUEST_BYTES)
    fingerprint = _fingerprint_json(request, limit=MAX_WORK_REQUEST_BYTES)
    execution = _execution_record(execution_id)
    return (
        McpWorkItem.query.filter_by(
            execution_id=execution.id, request_fingerprint=fingerprint
        ).first()
        is not None
    )


def get_or_create_work(
    execution_id: str,
    request_packet: dict[str, Any],
) -> McpWorkResolution:
    """Create idempotent work and commit before returning its immutable DTO.

    Product state must be committed by the caller first.  Failing closed when
    this session has unrelated pending state prevents this boundary from
    accidentally committing product changes along with the work item.
    """

    if _dirty_session():
        raise McpWorkError(
            "dirty_session",
            "Commit product state before creating MCP work.",
            status_code=409,
            retryable=False,
        )
    request = _validate_execution_request(request_packet, limit=MAX_WORK_REQUEST_BYTES)
    fingerprint = _fingerprint_json(request, limit=MAX_WORK_REQUEST_BYTES)
    dispatch_index = _extract_dispatch_index(request)
    execution = _execution_record(execution_id)
    _require_active_execution(execution)
    _fence_work_creation_to_active_execution(execution.id)
    record = McpWorkItem.query.filter_by(
        execution_id=execution.id, request_fingerprint=fingerprint
    ).first()
    if record is not None:
        request_was_pruned_after_completion = (
            record.state == "completed"
            and record.request_pruned_at is not None
            and record.request_json == MCP_REQUEST_TOMBSTONE
        )
        if not request_was_pruned_after_completion and _canonical_json_bytes(
            record.request_json, limit=MAX_WORK_REQUEST_BYTES
        ) != _canonical_json_bytes(request, limit=MAX_WORK_REQUEST_BYTES):
            raise McpWorkError(
                "request_fingerprint_conflict",
                "The work fingerprint does not match its canonical request.",
                status_code=409,
            )
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return _resolution_for(record, created=False)
    record = McpWorkItem(
        id=str(uuid.uuid4()),
        execution_id=execution.id,
        request_fingerprint=fingerprint,
        request_json=request,
        **dispatch_index,
        state="pending",
        version=0,
        attempt_count=0,
        created_at=utc_now_db(),
        updated_at=utc_now_db(),
    )
    try:
        db.session.add(record)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        execution = _execution_record(execution_id)
        _require_active_execution(execution)
        record = McpWorkItem.query.filter_by(
            execution_id=execution.id, request_fingerprint=fingerprint
        ).first()
        if record is None:
            raise McpWorkError(
                "work_conflict", "The MCP work could not be created.", status_code=409, retryable=True
            )
        request_was_pruned_after_completion = (
            record.state == "completed"
            and record.request_pruned_at is not None
            and record.request_json == MCP_REQUEST_TOMBSTONE
        )
        if not request_was_pruned_after_completion and _canonical_json_bytes(
            record.request_json, limit=MAX_WORK_REQUEST_BYTES
        ) != _canonical_json_bytes(request, limit=MAX_WORK_REQUEST_BYTES):
            raise McpWorkError(
                "request_fingerprint_conflict",
                "The work fingerprint does not match its canonical request.",
                status_code=409,
            )
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return _resolution_for(record, created=False)
    except Exception:
        db.session.rollback()
        raise
    return _resolution_for(record, created=True)


def _work_with_execution(
    work: McpWorkItem | str,
    principal: McpPrincipal,
) -> tuple[McpWorkItem, McpExecution]:
    work_id = str(work.id if isinstance(work, McpWorkItem) else work)
    row = (
        db.session.query(McpWorkItem, McpExecution)
        .join(McpExecution, McpExecution.id == McpWorkItem.execution_id)
        .filter(
            McpWorkItem.id == work_id,
            McpExecution.owner_user_id == int(principal.owner_user_id),
        )
        .populate_existing()
        .first()
    )
    if row is None:
        raise McpWorkError("work_not_found", "The MCP work item was not found.", status_code=404)
    return row


def _resolve_workload_role_and_format(
    request: dict[str, Any],
) -> tuple[str, str, dict[str, Any] | None]:
    source = str(request.get("source") or "")
    role = str(request.get("workload_role") or "")
    resp_fmt = request.get("response_format")

    if (
        isinstance(resp_fmt, dict)
        and resp_fmt.get("type") in {"json_object", "json_schema"}
        or source in {"doc_processor_pdf_boundary", "doc_processor_pdf_metadata"}
    ):
        expected_format = "json"
    elif source == "doc_processor_pdf_parser":
        expected_format = "html"
    else:
        expected_format = "text"

    if source == "doc_processor_pdf_parser":
        effective_role = "document_page"
    elif source == "doc_processor_pdf_batch":
        effective_role = "document_batch"
    elif source == "doc_processor_pdf_boundary":
        effective_role = "boundary_merge"
    elif source == "doc_processor_pdf_metadata":
        effective_role = "document_metadata"
    elif role:
        effective_role = role
    else:
        effective_role = "completion"

    schema = None
    if expected_format == "json":
        if source == "doc_processor_pdf_boundary":
            schema = {
                "type": "object",
                "properties": {
                    "main_content": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["merge", "separate", "auto"],
                            },
                            "confidence": {"type": "number"},
                            "reason": {"type": "string"},
                        },
                        "required": ["action"],
                    },
                    "footnotes": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["merge", "separate", "auto"],
                            },
                            "confidence": {"type": "number"},
                            "reason": {"type": "string"},
                        },
                        "required": ["action"],
                    },
                },
                "required": ["main_content", "footnotes"],
            }
        elif isinstance(resp_fmt, dict) and resp_fmt.get("json_schema"):
            schema = resp_fmt["json_schema"].get("schema")
        else:
            schema = {"type": "object"}
    elif expected_format == "html":
        schema = {
            "type": "html_fragment",
            "root_elements": ["main_content", "footnotes"],
            "description": (
                "Semantic HTML fragment containing <main_content>...</main_content> "
                "and optional <footnotes>...</footnotes>"
            ),
        }

    return effective_role, expected_format, schema


def _claim_payload(
    work: McpWorkItem,
    execution: McpExecution,
    *,
    worker_id: str,
    lease_token: str,
    lease_seconds: int,
    existing_lease: bool = False,
) -> McpWorkClaim:
    request = copy.deepcopy(work.request_json)
    if not isinstance(request, dict):
        raise McpWorkError("malformed_work", "The MCP work record is malformed.", status_code=500)
    role, fmt, schema = _resolve_workload_role_and_format(request)
    dispatch = request.get("dispatch")
    batch_id = dispatch.get("batch_id") if isinstance(dispatch, Mapping) else None
    if not isinstance(batch_id, str):
        batch_id = None
    return McpWorkClaim(
        work_item_id=str(work.id),
        execution_id=str(execution.id),
        owner_user_id=int(execution.owner_user_id),
        addon_id=str(execution.addon_id),
        resource_kind=str(execution.resource_kind),
        resource_id=str(execution.resource_id),
        queue_job_id=execution.queue_job_id,
        mode=str(execution.mode),
        execution_status=str(execution.status),
        state="leased",
        version=int(work.version) + (0 if existing_lease else 1),
        attempt_count=int(work.attempt_count) + (0 if existing_lease else 1),
        lease_owner=worker_id,
        lease_expires_at=(work.lease_expires_at if existing_lease else utc_now_db() + timedelta(seconds=lease_seconds)),
        lease_token=lease_token,
        request=request,
        workload_role=role,
        expected_output_format=fmt,
        response_schema=schema,
        batch_id=batch_id,
    )


def _recover_claim_receipt(
    authority: McpPrincipal,
    execution: McpExecution,
    *,
    request_id: str,
    request_fingerprint: str,
) -> McpWorkClaim | None:
    """Recover one receipt only after owner and add-on scope checks passed."""

    receipt = (
        db.session.query(McpWorkClaimReceipt)
        .filter(
            McpWorkClaimReceipt.execution_id == execution.id,
            McpWorkClaimReceipt.key_id == authority.key_id,
            McpWorkClaimReceipt.request_id == request_id,
        )
        .populate_existing()
        .first()
    )
    if receipt is None:
        return None
    if receipt.request_fingerprint != request_fingerprint:
        raise McpWorkError(
            "claim_request_conflict",
            "The claim request identifier was already used with different claim inputs.",
            status_code=409,
        )

    current = (
        db.session.query(McpWorkItem, McpExecution)
        .join(McpExecution, McpExecution.id == McpWorkItem.execution_id)
        .filter(
            McpWorkItem.id == receipt.work_item_id,
            McpWorkItem.execution_id == execution.id,
            McpExecution.id == execution.id,
            McpExecution.owner_user_id == int(authority.owner_user_id),
        )
        .populate_existing()
        .first()
    )
    if current is None:
        raise McpWorkError(
            "claim_request_closed",
            "The claim request lease is no longer active. Use a new claim request identifier.",
            status_code=409,
        )
    work, current_execution = current
    now = utc_now_db()
    if not (
        current_execution.status in MCP_ACTIVE_EXECUTION_STATUSES
        and work.state == "leased"
        and work.attempt_count == receipt.attempt_count
        and isinstance(work.lease_expires_at, datetime)
        and work.lease_expires_at > now
        and isinstance(work.lease_token, str)
        and isinstance(work.lease_owner, str)
    ):
        raise McpWorkError(
            "claim_request_closed",
            "The claim request lease is no longer active. Use a new claim request identifier.",
            status_code=409,
        )
    return _claim_payload(
        work,
        current_execution,
        worker_id=work.lease_owner,
        lease_token=work.lease_token,
        lease_seconds=0,
        existing_lease=True,
    )

def get_leased_work(principal: McpPrincipal, work_item_id: str, lease_token: str) -> McpWorkClaim:
    """Read the exact current packet without claiming, renewing, or consuming an attempt."""
    authority = _require_principal(principal)
    token = _validate_lease_token(lease_token)
    record, execution = _work_with_execution(work_item_id, authority)
    _require_work_scope(authority, execution.addon_id)
    _require_active_execution(execution)
    if (
        record.state != "leased" or record.lease_token != token
        or record.lease_expires_at is None or record.lease_expires_at <= utc_now_db()
    ):
        raise McpWorkError("stale_lease", "The MCP work lease is stale or expired.", status_code=409, retryable=True)
    return _claim_payload(
        record, execution, worker_id=str(record.lease_owner), lease_token=token,
        lease_seconds=0, existing_lease=True,
    )


def _retry_work_transaction[**P, T](operation: Callable[P, T]) -> Callable[P, T]:
    """Replay fresh DB-only work transactions, never caller-owned transactions."""

    @wraps(operation)
    def invoke(*args: P.args, **kwargs: P.kwargs) -> T:
        # Validation-only submissions must preserve the caller's read/pending
        # transaction, including unrelated edits that are never ours to commit.
        if kwargs.get("dry_run") is True:
            return operation(*args, **kwargs)
        session = db.session()
        if session.new or session.dirty or session.deleted:
            raise TransactionScopeError(
                f"mcp_{operation.__name__} requires a clean session"
            )
        try:
            if session.in_transaction():
                # A caller may already have flushed Core/SQL writes, invisible
                # to ORM dirty tracking. Preserve the legacy single commit, but
                # never roll back and silently replay only our part of its work.
                result = operation(*args, **kwargs)
                try:
                    session.commit()
                except Exception:
                    session.rollback()
                    raise
                return result
            return run_transaction_with_retry(
                session,
                lambda _session: operation(*args, **kwargs),
                operation_name=f"mcp_{operation.__name__}",
            )
        except (DatabaseBusyError, OperationalError) as exc:
            session.rollback()
            if not isinstance(exc, DatabaseBusyError) and not is_database_locked(exc):
                raise
            raise McpWorkError(
                "database_busy", public_error_message(exc), status_code=503, retryable=True
            ) from exc

    return invoke


@_retry_work_transaction
def claim_next_work(
    principal: McpPrincipal,
    worker_id: str,
    addon_id: str | None = None,
    lease_seconds: int | None = None,
    *,
    execution_id: str,
    optional_addon_id: str | None = None,
    workload_role: str | None = None,
    batch_id: str | None = None,
    claim_request_id: str | None = None,
) -> McpWorkClaim | None:
    """Atomically lease pending or expired work from one exact execution."""

    authority = _require_principal(principal)
    worker = _validate_worker_id(worker_id)
    requested_duration = None if lease_seconds is None else _validate_lease_seconds(lease_seconds)
    normalized_claim_request_id = (
        None
        if claim_request_id is None
        else _validate_claim_request_id(claim_request_id)
    )
    if not isinstance(execution_id, str) or not execution_id.strip():
        raise McpWorkError(
            "invalid_execution",
            "A non-empty execution_id is required for work claims.",
            status_code=400,
        )
    normalized_execution_id = execution_id.strip()
    if batch_id is not None:
        batch_filter = _validate_batch_id(batch_id)
    else:
        batch_filter = None
    if addon_id is not None and optional_addon_id is not None and addon_id != optional_addon_id:
        raise McpWorkError("invalid_addon", "Conflicting add-on filters were supplied.", status_code=400)
    selected_addon = addon_id if addon_id is not None else optional_addon_id
    if selected_addon is not None and selected_addon not in MCP_ADDON_IDS:
        raise McpWorkError("invalid_addon", "The MCP add-on is not supported for work.", status_code=400)
    if selected_addon is not None:
        _require_work_scope(authority, selected_addon)
    elif not authority.has_scope(MCP_WORK_SCOPE):
        raise McpAuthError("scope_denied", "The MCP principal lacks work scope.", status_code=403)
    selected_execution = get_execution(normalized_execution_id, principal=authority)
    if selected_execution is None:
        raise McpWorkError(
            "execution_not_found",
            "The MCP execution was not found.",
            status_code=404,
        )
    _require_work_scope(authority, selected_execution.addon_id)
    if selected_addon is not None and selected_addon != selected_execution.addon_id:
        raise McpWorkError(
            "invalid_addon",
            "The add-on filter does not match the selected execution.",
            status_code=400,
        )

    request_fingerprint = None
    if normalized_claim_request_id is not None:
        request_fingerprint = _claim_request_fingerprint(
            worker_id=worker,
            addon_id=str(selected_execution.addon_id),
            workload_role=workload_role,
            batch_id=batch_filter,
            lease_seconds=requested_duration,
        )
        recovered = _recover_claim_receipt(
            authority,
            selected_execution,
            request_id=normalized_claim_request_id,
            request_fingerprint=request_fingerprint,
        )
        if recovered is not None:
            return recovered

    now = utc_now_db()
    eligible = or_(
        McpWorkItem.state == "pending",
        and_(McpWorkItem.state == "leased", McpWorkItem.lease_expires_at <= now),
    )
    query = (
        db.session.query(McpWorkItem, McpExecution)
        .options(
            defer(McpWorkItem.accepted_response_json),
            defer(McpWorkItem.boundary_input_json),
        )
        .join(McpExecution, McpExecution.id == McpWorkItem.execution_id)
        .filter(
            McpExecution.owner_user_id == int(authority.owner_user_id),
            McpExecution.status.in_(tuple(MCP_ACTIVE_EXECUTION_STATUSES)),
            eligible,
        )
    )
    query = query.filter(McpExecution.id == selected_execution.id)
    if selected_addon is not None:
        query = query.filter(McpExecution.addon_id == selected_addon)
    allowed_addons = tuple(
        addon for addon, scope in MCP_ADDON_SCOPES.items() if authority.has_scope(scope)
    )
    if selected_addon is None:
        if not allowed_addons:
            raise McpAuthError("scope_denied", "The MCP principal lacks an add-on work scope.", status_code=403)
        query = query.filter(McpExecution.addon_id.in_(allowed_addons))
    if batch_filter is not None:
        query = query.filter(McpWorkItem.batch_id == batch_filter)
    if workload_role is None:
        query = query.options(defer(McpWorkItem.request_json))
    cursor_created_at = None
    cursor_id = None
    while True:
        page_query = query
        if cursor_created_at is not None and cursor_id is not None:
            page_query = page_query.filter(
                or_(
                    McpWorkItem.created_at > cursor_created_at,
                    and_(
                        McpWorkItem.created_at == cursor_created_at,
                        McpWorkItem.id > cursor_id,
                    ),
                )
            )
        candidates = (
            page_query.order_by(
                McpWorkItem.created_at.asc(), McpWorkItem.id.asc()
            )
            .limit(128)
            .populate_existing()
            .all()
        )
        if not candidates:
            break
        for candidate, candidate_execution in candidates:
            if workload_role is not None:
                request = candidate.request_json
                if not isinstance(request, dict):
                    continue
                cand_role, _, _ = _resolve_workload_role_and_format(request)
                raw_role = request.get("workload_role")
                if workload_role not in (cand_role, raw_role):
                    continue
            lease_token = str(uuid.uuid4())
            duration = requested_duration if requested_duration is not None else _packet_lease_seconds(
                candidate_execution.addon_id, candidate.request_json,
            )
            # Decode/copy the immutable image packet before taking SQLite's
            # writer lock. The conditional update still fences this snapshot.
            claim = _claim_payload(
                candidate,
                candidate_execution,
                worker_id=worker,
                lease_token=lease_token,
                lease_seconds=duration,
            )
            deadline = claim.lease_expires_at
            now = deadline - timedelta(seconds=duration)
            receipt_id = None
            if normalized_claim_request_id is not None:
                receipt_id = str(uuid.uuid4())
                # Reserve this request inside the same transaction BEFORE the
                # lease CAS. A conflict leaves both the candidate and any
                # caller-owned writes untouched; no transaction rollback or
                # SQLite savepoint release may commit/discard unrelated work.
                inserted = db.session.execute(
                    sqlite_insert(McpWorkClaimReceipt)
                    .values(
                        id=receipt_id,
                        execution_id=str(selected_execution.id),
                        key_id=str(authority.key_id),
                        request_id=normalized_claim_request_id,
                        request_fingerprint=request_fingerprint,
                        work_item_id=str(candidate.id),
                        attempt_count=int(candidate.attempt_count) + 1,
                        created_at=utc_now_db(),
                    )
                    .on_conflict_do_nothing(
                        index_elements=["execution_id", "key_id", "request_id"]
                    )
                ).rowcount
                if inserted != 1:
                    recovered = _recover_claim_receipt(
                        authority, selected_execution,
                        request_id=normalized_claim_request_id,
                        request_fingerprint=request_fingerprint,
                    )
                    if recovered is not None:
                        return recovered
                    raise McpWorkError(
                        "claim_receipt_unavailable",
                        "The claim receipt could not be recovered. Retry the same claim request identifier.",
                        status_code=503, retryable=True,
                    )
            changed = db.session.execute(
                update(McpWorkItem)
                .where(
                    McpWorkItem.id == candidate.id,
                    McpWorkItem.version == candidate.version,
                    McpWorkItem.execution_id.in_(
                        _active_execution_ids(selected_execution.id)
                    ),
                    or_(
                        McpWorkItem.state == "pending",
                        and_(
                            McpWorkItem.state == "leased",
                            McpWorkItem.lease_expires_at <= now,
                        ),
                    ),
                )
                .values(
                    state="leased",
                    lease_token=lease_token,
                    lease_owner=worker,
                    lease_expires_at=deadline,
                    leased_at=now,
                    updated_at=now,
                    version=McpWorkItem.version + 1,
                    attempt_count=McpWorkItem.attempt_count + 1,
                )
                .execution_options(synchronize_session=False)
            ).rowcount
            if changed != 1:
                if receipt_id is not None:
                    db.session.execute(
                        delete(McpWorkClaimReceipt).where(McpWorkClaimReceipt.id == receipt_id)
                    )
                continue
            return claim
        cursor_created_at = candidates[-1][0].created_at
        cursor_id = candidates[-1][0].id
    if normalized_claim_request_id is not None:
        # A peer can commit after our initial receipt read and before the
        # candidate SELECT. An empty candidate set must still replay its lease.
        return _recover_claim_receipt(
            authority, selected_execution,
            request_id=normalized_claim_request_id,
            request_fingerprint=request_fingerprint,
        )
    return None


@_retry_work_transaction
def renew_work(
    principal: McpPrincipal,
    work: McpWorkItem | str,
    lease_token: str,
    *,
    lease_seconds: int | None = None,
) -> dict[str, Any]:
    """Extend a current lease using a token and version fence."""

    authority = _require_principal(principal)
    requested_duration = None if lease_seconds is None else _validate_lease_seconds(lease_seconds)
    token = _validate_lease_token(lease_token)
    record, execution = _work_with_execution(work, authority)
    _require_work_scope(authority, execution.addon_id)
    _require_active_execution(execution)
    duration = requested_duration if requested_duration is not None else _packet_lease_seconds(
        execution.addon_id, record.request_json,
    )
    now = utc_now_db()
    deadline = now + timedelta(seconds=duration)
    if requested_duration is None and record.lease_expires_at is not None:
        deadline = max(deadline, record.lease_expires_at)
    changed = db.session.execute(
        update(McpWorkItem)
        .where(
            McpWorkItem.id == record.id,
            McpWorkItem.execution_id == execution.id,
            McpWorkItem.execution_id.in_(_active_execution_ids(execution.id)),
            McpWorkItem.state == "leased",
            McpWorkItem.lease_token == token,
            McpWorkItem.lease_expires_at > now,
            McpWorkItem.version == record.version,
        )
        .values(
            lease_expires_at=deadline,
            updated_at=now,
            version=McpWorkItem.version + 1,
        )
        .execution_options(synchronize_session=False)
    ).rowcount
    if changed != 1:
        db.session.rollback()
        _, fresh_execution = _work_with_execution(record.id, authority)
        _require_active_execution(fresh_execution)
        raise McpWorkError("stale_lease", "The MCP work lease is stale or expired.", status_code=409, retryable=True)
    fresh, _ = _work_with_execution(record.id, authority)
    if not isinstance(fresh.lease_expires_at, datetime):
        raise McpWorkError("malformed_work", "The MCP work record is malformed.", status_code=500)
    return McpWorkRenewal(
        work_item_id=str(fresh.id),
        execution_id=str(execution.id),
        version=int(fresh.version),
        state=str(fresh.state),
        lease_owner=str(fresh.lease_owner),
        lease_expires_at=fresh.lease_expires_at,
    ).to_dict()


@_retry_work_transaction
def release_work(
    principal: McpPrincipal,
    work: McpWorkItem | str,
    lease_token: str,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    """Surrender an active lease back to pending state so it can be re-claimed."""

    authority = _require_principal(principal)
    token = _validate_lease_token(lease_token)
    record, execution = _work_with_execution(work, authority)
    _require_work_scope(authority, execution.addon_id)
    _require_active_execution(execution)
    now = utc_now_db()
    changed = db.session.execute(
        update(McpWorkItem)
        .where(
            McpWorkItem.id == record.id,
            McpWorkItem.execution_id == execution.id,
            McpWorkItem.execution_id.in_(_active_execution_ids(execution.id)),
            McpWorkItem.state == "leased",
            McpWorkItem.lease_token == token,
            McpWorkItem.lease_expires_at > now,
            McpWorkItem.version == record.version,
        )
        .values(
            state="pending",
            lease_token=None,
            lease_owner=None,
            lease_expires_at=None,
            leased_at=None,
            updated_at=now,
            version=McpWorkItem.version + 1,
        )
        .execution_options(synchronize_session=False)
    ).rowcount
    if changed != 1:
        db.session.rollback()
        _, fresh_execution = _work_with_execution(record.id, authority)
        _require_active_execution(fresh_execution)
        raise McpWorkError("stale_lease", "The MCP work lease is stale or expired.", status_code=409, retryable=True)
    fresh, _ = _work_with_execution(record.id, authority)
    return {
        "work_item_id": str(fresh.id),
        "execution_id": str(execution.id),
        "version": int(fresh.version),
        "state": str(fresh.state),
        "released": True,
        "reason": str(reason) if reason else None,
    }


def _validate_lease_token(value: Any) -> str:
    if not isinstance(value, str):
        raise McpWorkError("invalid_lease_token", "The lease token is invalid.", status_code=400)
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise McpWorkError("invalid_lease_token", "The lease token is invalid.", status_code=400) from exc
    if str(parsed) != value:
        raise McpWorkError("invalid_lease_token", "The lease token is invalid.", status_code=400)
    return value


def _validate_tool_calls(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > MAX_TOOL_CALLS:
        raise McpWorkError("invalid_response", "The assistant completion is invalid.", status_code=422)
    normalized: list[dict[str, Any]] = []
    for call in value:
        if not isinstance(call, dict) or set(call) != {"id", "type", "function"}:
            raise McpWorkError("invalid_response", "The assistant completion is invalid.", status_code=422)
        call_id = call["id"]
        function = call["function"]
        if (
            not isinstance(call_id, str)
            or not call_id
            or len(call_id) > MAX_TOOL_CALL_ID_LENGTH
            or call["type"] != "function"
            or not isinstance(function, dict)
            or set(function) != {"name", "arguments"}
        ):
            raise McpWorkError("invalid_response", "The assistant completion is invalid.", status_code=422)
        name = function["name"]
        arguments = function["arguments"]
        if not isinstance(name, str) or not name or len(name) > MAX_TOOL_NAME_LENGTH:
            raise McpWorkError("invalid_response", "The assistant completion is invalid.", status_code=422)
        if not isinstance(arguments, str) or len(arguments.encode("utf-8")) > MAX_TOOL_ARGUMENT_BYTES:
            raise McpWorkError("invalid_response", "The assistant completion is invalid.", status_code=422)
        try:
            parsed_arguments = json.loads(arguments)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise McpWorkError("invalid_tool_arguments", "Tool-call arguments are invalid JSON.", status_code=422) from exc
        if not isinstance(parsed_arguments, dict):
            raise McpWorkError("invalid_tool_arguments", "Tool-call arguments must be a JSON object.", status_code=422)
        _canonical_json_bytes(parsed_arguments, limit=MAX_TOOL_ARGUMENT_BYTES)
        normalized.append(
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        )
    return normalized


def _validate_usage(value: Any) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != {
        "prompt_tokens", "completion_tokens", "total_tokens"
    }:
        raise McpWorkError("invalid_usage", "The completion usage object is invalid.", status_code=422)
    values: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        candidate = value[key]
        if isinstance(candidate, bool) or not isinstance(candidate, int) or candidate < 0:
            raise McpWorkError("invalid_usage", "The completion usage object is invalid.", status_code=422)
        values[key] = candidate
    if values["total_tokens"] < values["prompt_tokens"] + values["completion_tokens"]:
        raise McpWorkError("invalid_usage", "The completion usage object is invalid.", status_code=422)
    return values


_SAFE_JSON_TYPES = frozenset(
    {"array", "boolean", "integer", "null", "number", "object", "string"}
)
_SAFE_VALIDATION_KEYWORDS = frozenset({"enum", "json_parse", "required", "schema", "type"})
_FIXED_VALIDATION_MESSAGES = {
    "enum": "The value is not an allowed choice.",
    "json_parse": "Content is not valid JSON.",
    "required": "Missing required property.",
    "schema": "The value does not satisfy the response schema.",
    "type": "The value has an invalid type.",
}

_RESPONSE_FIELDS = frozenset({"content", "tool_calls", "finish_reason", "usage"})
_STRUCTURED_RESPONSE_FORMATS = frozenset({"json_object", "json_schema"})


def _response_wrapper_error(
    *,
    detail: str = "The assistant completion is invalid.",
    validation_errors: Iterable[Mapping[str, Any]] | None = None,
) -> McpWorkError:
    return McpWorkError(
        "invalid_response",
        detail,
        status_code=422,
        validation_errors=validation_errors,
    )


def _content_type_error(response_format_type: str | None) -> McpWorkError:
    expected_type: str | list[str]
    if response_format_type == "json_object":
        expected_type = ["object", "string"]
    elif response_format_type == "json_schema":
        expected_type = ["array", "object", "string"]
    else:
        expected_type = "string"
    return _response_wrapper_error(
        detail="The /content value has an invalid type.",
        validation_errors=[
            {
                "path": "/content",
                "keyword": "type",
                "expected_type": expected_type,
            }
        ],
    )


def _missing_content_error(
    *,
    structured: bool = False,
    response_format_type: str | None = None,
) -> McpWorkError:
    if structured:
        code = (
            "invalid_json_object_response"
            if response_format_type == "json_object"
            else "invalid_json_schema_response"
        )
        return McpWorkError(
            code,
            "The structured completion requires content.",
            status_code=422,
            validation_errors=[
                {
                    "path": "/content",
                    "keyword": "required",
                }
            ],
        )
    return _response_wrapper_error(
        detail="The assistant completion needs content or tool calls.",
        validation_errors=[
            {
                "path": "/content",
                "keyword": "required",
            }
        ],
    )


def _normalize_native_json_numbers(value: Any) -> Any:
    """Canonicalize connector-decoded JSON numbers without changing text input.

    Some MCP transports decode JSON integer tokens as integral Python floats.
    JSON Schema intentionally considers values such as ``1.0`` valid integers,
    but downstream product contracts may require the decoded runtime value to be
    an actual ``int``. Native structured content has no original textual number
    spelling to preserve, so canonicalize only integral floats here. String JSON
    submissions keep their exact representation and normal ``json.loads`` rules.
    """

    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, list):
        return [_normalize_native_json_numbers(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _normalize_native_json_numbers(item) for key, item in value.items()
        }
    return value


def _unicode_content_error() -> McpWorkError:
    return _response_wrapper_error(
        detail="The /content value contains invalid Unicode. Preserve the source characters; repair the encoding or submit a native JSON object, never ASCII-fold the text.",
        validation_errors=[
            {"path": "/content", "keyword": "schema", "code": "invalid_unicode_content"}
        ],
    )


def _validate_decoded_unicode(value: Any) -> None:
    """JSON escapes must not admit lone surrogates into downstream HTML."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            try:
                item.encode("utf-8")
            except UnicodeError as exc:
                raise _unicode_content_error() from exc
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


def _encode_native_json_content(value: dict[str, Any] | list[Any]) -> str:
    """Encode one native JSON content value within the response byte budget."""

    validation_errors = [{"path": "/content", "keyword": "schema"}]
    try:
        encoded = json.dumps(
            _normalize_native_json_numbers(value),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except UnicodeError as exc:
        raise _unicode_content_error() from exc
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise _response_wrapper_error(
            detail="The /content value cannot be encoded as bounded JSON.",
            validation_errors=validation_errors,
        ) from exc
    if len(encoded) > MAX_CONTENT_BYTES:
        raise _response_wrapper_error(
            detail="The /content JSON exceeds the response size limit.",
            validation_errors=validation_errors,
        )
    return encoded.decode("utf-8")


def _normalize_structured_content_for_duplicate(value: Any) -> Any:
    """Normalize only native structured content for idempotency comparison."""

    if not isinstance(value, dict):
        return value
    content = value.get("content")
    if not isinstance(content, (dict, list)):
        return value
    normalized = dict(value)
    normalized["content"] = _encode_native_json_content(content)
    return normalized


def _response_fingerprint_for_duplicate(value: Any) -> str | None:
    """Return a bounded normalized fingerprint for duplicate comparison."""

    try:
        normalized = _normalize_structured_content_for_duplicate(value)
        return _fingerprint_json(normalized, limit=MAX_RESPONSE_BYTES)
    except McpWorkError:
        # Invalid or oversized completions can never equal an accepted normalized
        # response. Return no candidate instead of turning a retry into a server error.
        return None


def _schema_property_names(schema: Any) -> frozenset[str]:
    allowed: set[str] = set()
    pending: list[Any] = [schema]
    while pending and len(allowed) < 4096:
        node = pending.pop()
        if isinstance(node, Mapping):
            properties = node.get("properties")
            if isinstance(properties, Mapping):
                for name in properties:
                    if (
                        isinstance(name, str)
                        and 0 < len(name) <= MAX_VALIDATION_PROPERTY_LENGTH
                        and all(ord(char) >= 0x20 and ord(char) != 0x7F for char in name)
                    ):
                        allowed.add(name)
                pending.extend(properties.values())
            required = node.get("required")
            if isinstance(required, Sequence) and not isinstance(required, (str, bytes)):
                for name in required:
                    if (
                        isinstance(name, str)
                        and 0 < len(name) <= MAX_VALIDATION_PROPERTY_LENGTH
                        and all(ord(char) >= 0x20 and ord(char) != 0x7F for char in name)
                    ):
                        allowed.add(name)
            pending.extend(value for key, value in node.items() if key not in {"properties", "required"})
        elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
            pending.extend(node)
    return frozenset(allowed)


def _safe_validation_path(
    path: Iterable[Any], allowed_names: frozenset[str], *, append: Any = None
) -> str:
    segments = list(path)
    if append is not None:
        segments.append(append)
    safe: list[str] = []
    for segment in segments[:MAX_VALIDATION_PATH_DEPTH]:
        if isinstance(segment, int) and not isinstance(segment, bool) and segment >= 0:
            safe.append(str(segment))
        elif isinstance(segment, str) and segment in allowed_names:
            safe.append(segment.replace("~", "~0").replace("/", "~1"))
        else:
            safe.append("<key>")
    while safe and len("/" + "/".join(safe)) > MAX_VALIDATION_PATH_LENGTH:
        safe.pop()
    return "/" + "/".join(safe) if safe else ""


def _safe_expected_type(value: Any) -> str | list[str] | None:
    values = list(value) if isinstance(value, list) else [value]
    expected = [item for item in values if isinstance(item, str) and item in _SAFE_JSON_TYPES]
    if not expected:
        return None
    return expected[0] if len(expected) == 1 else expected[:MAX_VALIDATION_ENUM_VALUES]


def _safe_expected_enum(value: Any) -> list[Any] | None:
    if not isinstance(value, list) or not 0 < len(value) <= MAX_VALIDATION_ENUM_VALUES:
        return None
    if any(
        not (
            item is None
            or isinstance(item, bool)
            or isinstance(item, int) and not isinstance(item, bool)
            or isinstance(item, float) and math.isfinite(item)
            or isinstance(item, str) and 0 < len(item) <= MAX_VALIDATION_PROPERTY_LENGTH
            and all(ord(char) >= 0x20 and ord(char) != 0x7F for char in item)
        )
        for item in value
    ):
        return None
    try:
        if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > MAX_VALIDATION_ENUM_BYTES:
            return None
    except (TypeError, ValueError, UnicodeError):
        return None
    return list(value)


def _bound_validation_errors(errors: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]] | None:
    if errors is None or isinstance(errors, (str, bytes)):
        return None
    bounded: list[dict[str, Any]] = []
    for raw in errors:
        if len(bounded) >= MAX_VALIDATION_ERRORS or not isinstance(raw, Mapping):
            break
        raw_keyword = raw.get("keyword")
        keyword = (
            raw_keyword
            if isinstance(raw_keyword, str) and raw_keyword in _SAFE_VALIDATION_KEYWORDS
            else "schema"
        )
        path = raw.get("path") if isinstance(raw.get("path"), str) else ""
        entry: dict[str, Any] = {
            "path": path[:MAX_VALIDATION_PATH_LENGTH],
            "keyword": keyword,
            "message": _FIXED_VALIDATION_MESSAGES.get(keyword, _FIXED_VALIDATION_MESSAGES["schema"]),
        }
        expected_type = _safe_expected_type(raw.get("expected_type"))
        if expected_type is not None:
            entry["expected_type"] = expected_type
        expected_enum = _safe_expected_enum(raw.get("expected_enum"))
        if expected_enum is not None:
            entry["expected_enum"] = expected_enum
        for key in ("line", "column"):
            if isinstance(raw.get(key), int) and not isinstance(raw.get(key), bool) and raw[key] >= 1:
                entry[key] = min(raw[key], 1_000_000)
        code = raw.get("code")
        if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,127}", code):
            entry["code"] = code
        block_number = raw.get("block_number")
        if type(block_number) is int and 1 <= block_number <= 256:
            entry["block_number"] = block_number
        bounded.append(entry)
    return bounded or None


def _schema_validation_diagnostic(error: ValidationError, *, allowed_names: frozenset[str]) -> list[dict[str, Any]]:
    raw_keyword = error.validator
    keyword = raw_keyword if raw_keyword in {"enum", "required", "type"} else "schema"
    base = {
        "path": _safe_validation_path(error.path, allowed_names),
        "keyword": keyword,
        "message": _FIXED_VALIDATION_MESSAGES[keyword],
    }
    if raw_keyword == "additionalProperties":
        base["path"] = _safe_validation_path(error.path, allowed_names, append="<key>")
    if keyword == "required":
        required = error.validator_value
        instance = error.instance
        if isinstance(required, list):
            return [
                {
                    **base,
                    "path": _safe_validation_path(error.path, allowed_names, append=name),
                }
                for name in required
                if not isinstance(instance, Mapping) or name not in instance
            ]
    if keyword == "type":
        expected_type = _safe_expected_type(error.validator_value)
        if expected_type is not None:
            base["expected_type"] = expected_type
    elif keyword == "enum":
        expected_enum = _safe_expected_enum(error.validator_value)
        if expected_enum is not None:
            base["expected_enum"] = expected_enum
    return [base]


def validate_response(response: Any, request_json: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Validate and normalize the closed assistant-completion result shape."""

    request = request_json if isinstance(request_json, Mapping) else {}
    response_format = request.get("response_format")
    response_format_type = (
        response_format.get("type")
        if isinstance(response_format, Mapping)
        and isinstance(response_format.get("type"), str)
        else None
    )
    structured_response = response_format_type in _STRUCTURED_RESPONSE_FORMATS
    json_schema = (
        response_format.get("json_schema")
        if response_format_type == "json_schema" and isinstance(response_format, Mapping)
        else None
    )
    schema = json_schema.get("schema") if isinstance(json_schema, Mapping) else None
    if not isinstance(response, dict):
        raise _response_wrapper_error(
            validation_errors=[
                {
                    "path": "",
                    "keyword": "schema",
                }
            ]
        )
    if not set(response).issubset(_RESPONSE_FIELDS):
        raise _response_wrapper_error(
            validation_errors=[
                {
                    "path": "/<key>",
                    "keyword": "schema",
                }
            ]
        )

    normalized: dict[str, Any] = {}
    content = response.get("content")
    has_content = "content" in response
    content_instance: Any = None
    if has_content:
        if isinstance(content, str):
            try:
                content_bytes = content.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise _unicode_content_error() from exc
            if not content.strip() or len(content_bytes) > MAX_CONTENT_BYTES:
                raise _content_type_error(response_format_type)
            normalized["content"] = content
        elif structured_response and isinstance(content, (dict, list)):
            normalized_content = _encode_native_json_content(content)
            normalized["content"] = normalized_content
            try:
                content_instance = json.loads(normalized_content)
            except (TypeError, ValueError, RecursionError) as exc:
                raise _response_wrapper_error() from exc
        else:
            raise _content_type_error(response_format_type)
    tool_calls = response.get("tool_calls")
    # OpenAI-compatible clients commonly serialize an absent optional field as
    # null or an empty list. Those wire shapes are semantically equivalent to
    # omission, so accept and canonicalize them away. Non-empty values remain
    # strictly validated below.
    if "tool_calls" in response and tool_calls not in (None, []):
        normalized["tool_calls"] = _validate_tool_calls(tool_calls)
    if not normalized.get("content") and not normalized.get("tool_calls"):
        if structured_response and not has_content:
            raise _missing_content_error(
                structured=True, response_format_type=response_format_type
            )
        raise _missing_content_error()
    if "finish_reason" in response:
        reason = response["finish_reason"]
        if reason is not None and (
            not isinstance(reason, str) or not reason or len(reason) > MAX_FINISH_REASON_LENGTH
        ):
            raise McpWorkError("invalid_finish_reason", "The completion finish reason is invalid.", status_code=422)
        normalized["finish_reason"] = reason
    if "usage" in response:
        normalized["usage"] = _validate_usage(response["usage"])

    if structured_response:
        if not has_content:
            if response_format_type == "json_schema":
                raise _missing_content_error(
                    structured=True, response_format_type=response_format_type
                )
            _canonical_json_bytes(normalized, limit=MAX_RESPONSE_BYTES)
            return normalized
        if isinstance(content, str):
            try:
                content_instance = json.loads(content)
            except json.JSONDecodeError as exc:
                code = (
                    "invalid_json_object_response"
                    if response_format_type == "json_object"
                    else "invalid_json_schema_response"
                )
                raise McpWorkError(
                    code,
                    "The completion content is not valid JSON.",
                    status_code=422,
                    validation_errors=[
                        {
                            "path": "/content",
                            "keyword": "json_parse",
                            "line": exc.lineno,
                            "column": exc.colno,
                        }
                    ],
                ) from exc
            except (TypeError, ValueError, RecursionError) as exc:
                code = (
                    "invalid_json_object_response"
                    if response_format_type == "json_object"
                    else "invalid_json_schema_response"
                )
                raise McpWorkError(
                    code,
                    "The completion content is not valid JSON.",
                    status_code=422,
                    validation_errors=[
                        {
                            "path": "/content",
                            "keyword": "json_parse",
                            "line": 1,
                            "column": 1,
                        }
                    ],
                ) from exc
        if isinstance(content, str) and "\\u" in content:
            _validate_decoded_unicode(content_instance)
        root_type = schema.get("type") if isinstance(schema, dict) else None
        double_encoding_hint = (
            "The completion decoded to a string, not the required JSON object/array. "
            "It may be double-encoded. Pass response.content as a native object/array "
            "or encode once; preserve Unicode source characters."
            if isinstance(content_instance, str)
            and (
                response_format_type == "json_object"
                or root_type in ("object", "array")
            )
            else None
        )
        if response_format_type == "json_object":
            if not isinstance(content_instance, dict):
                raise McpWorkError(
                    "invalid_json_object_response",
                    double_encoding_hint
                    or "The completion content must be a JSON object.",
                    status_code=422,
                    validation_errors=[
                        {
                            "path": "/content",
                            "keyword": "type",
                            "expected_type": "object",
                        }
                    ],
                )
        else:
            if not isinstance(schema, dict):
                raise McpWorkError(
                    "invalid_json_schema_response",
                    "The JSON-schema response is invalid.",
                    status_code=422,
                    validation_errors=[
                        {
                            "path": "",
                            "keyword": "schema",
                        }
                    ],
                )
            try:
                Draft202012Validator.check_schema(schema)
            except (SchemaError, TypeError, ValueError, RecursionError) as exc:
                raise McpWorkError(
                    "invalid_json_schema_response",
                    "The JSON-schema response is invalid.",
                    status_code=422,
                    validation_errors=[
                        {
                            "path": "",
                            "keyword": "schema",
                        }
                    ],
                ) from exc
            allowed_names = _schema_property_names(schema)
            try:
                schema_errors = list(islice(
                    Draft202012Validator(schema).iter_errors(content_instance), MAX_VALIDATION_ERRORS
                ))
            except (SchemaError, TypeError, ValueError, RecursionError) as exc:
                raise McpWorkError(
                    "invalid_json_schema_response",
                    "The JSON-schema response is invalid.",
                    status_code=422,
                    validation_errors=[
                        {
                            "path": "",
                            "keyword": "schema",
                        }
                    ],
                ) from exc
            if schema_errors:
                diagnostics: list[dict[str, Any]] = []
                for error in schema_errors:
                    for diagnostic in _schema_validation_diagnostic(
                        error, allowed_names=allowed_names
                    ):
                        if diagnostic not in diagnostics:
                            diagnostics.append(diagnostic)
                        if len(diagnostics) >= MAX_VALIDATION_ERRORS:
                            break
                    if len(diagnostics) >= MAX_VALIDATION_ERRORS:
                        break
                raise McpWorkError(
                    "invalid_json_schema_response",
                    double_encoding_hint
                    or "The completion does not satisfy its JSON schema.",
                    status_code=422,
                    validation_errors=diagnostics,
                )
    _canonical_json_bytes(normalized, limit=MAX_RESPONSE_BYTES)
    return normalized


def _submission_result(
    *,
    work_item_id: str,
    status: str,
    response: dict[str, Any] | None = None,
    submission_id: str | None = None,
    error_code: str | None = None,
    error_detail: str | None = None,
    validation_errors: Iterable[Mapping[str, Any]] | None = None,
    lease_active: bool | None = None,
    lease_expires_at: datetime | None = None,
    dry_run: bool = False,
    preflight: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "work_item_id": work_item_id,
        "status": status,
    }
    if status in {"accepted", "duplicate", "valid"}:
        result["validation_stage"] = "response_contract"
        result["product_validation"] = "not_checked"
        result["receipt_state"] = "schema_validated"
        result["product_commit_confirmed"] = False
        if preflight is not None:
            result["preflight"] = dict(preflight)
    if not dry_run:
        result["response"] = copy.deepcopy(response) if response is not None else None
    if submission_id is not None:
        result["submission_id"] = submission_id
    if error_code is not None:
        result["error_code"] = error_code
    if error_detail is not None:
        result["error_detail"] = error_detail
    bounded_errors = _bound_validation_errors(validation_errors)
    if bounded_errors is not None:
        result["validation_errors"] = bounded_errors
    if lease_active is not None:
        result["lease_active"] = bool(lease_active)
        if lease_active and lease_expires_at is not None:
            result["lease_expires_at"] = utc_iso(lease_expires_at)
    if dry_run:
        result["dry_run"] = True
    return result


def _append_submission(
    work_item_id: str,
    response_payload: Any,
    response_fingerprint: str,
    status: str,
    *,
    error_code: str | None = None,
) -> McpWorkSubmission:
    record = McpWorkSubmission(
        id=str(uuid.uuid4()),
        work_item_id=work_item_id,
        response_json=copy.deepcopy(response_payload),
        response_fingerprint=response_fingerprint,
        status=status,
        error_code=error_code,
        error_detail=("The submitted completion was rejected." if error_code else None),
        submitted_at=utc_now_db(),
    )
    db.session.add(record)
    return record


def _completion_preflight(execution: Any, request: Mapping[str, Any],
                          response: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Optional native add-on hook. It is pure, packet-local and not a commit.

    The owner/lease checks run before this hook. Final product validation stays
    with the worker; core does not import product normalizers or reload sources.
    """
    manager = getattr(current_app, "addon_manager", None) if has_app_context() else None
    addon = manager.get_loaded_addon(execution.addon_id) if manager is not None else None
    hook = getattr(type(addon), "preflight_passive_completion", None)
    if not callable(hook):
        return None
    result = hook(addon, request=request, response=response)
    if result is None:
        return None
    if (not isinstance(result, Mapping) or set(result) != {
            "contract", "scope", "validated_blocks", "validation_errors"}
            or not isinstance(result["contract"], str)
            or result["scope"] != "leased_packet_only"
            or type(result["validated_blocks"]) is not int
            or not 0 <= result["validated_blocks"] <= 256
            or not isinstance(result["validation_errors"], list)):
        raise McpWorkError("invalid_preflight_result", "The product preflight returned an invalid result.")
    return result


@_retry_work_transaction
def submit_work(
    principal: McpPrincipal,
    work: McpWorkItem | str | None = None,
    *args: Any,
    lease_token: str | None = None,
    response: Any = None,
    work_item_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Validate, append, and conditionally accept one submitted completion.

    Positional compatibility supports both ``(principal, work, token,
    response)`` and ``(principal, work, response, lease_token=token)``.
    """

    authority = _require_principal(principal)
    if not isinstance(dry_run, bool):
        raise McpWorkError(
            "invalid_submission",
            "The dry_run flag must be boolean.",
            status_code=400,
        )
    if work is None:
        work = work_item_id
    if len(args) == 2:
        if lease_token is not None or response is not None:
            raise McpWorkError("invalid_submission", "The submission arguments conflict.", status_code=400)
        lease_token, response = args
    elif len(args) == 1:
        if response is None and lease_token is not None:
            response = args[0]
        elif lease_token is None:
            lease_token = args[0]
        else:
            raise McpWorkError("invalid_submission", "The submission arguments conflict.", status_code=400)
    elif len(args) > 2:
        raise McpWorkError("invalid_submission", "The submission arguments are invalid.", status_code=400)
    if work is None:
        raise McpWorkError("work_not_found", "The MCP work item was not found.", status_code=404)
    response_payload = response if response is not None else {}
    if dry_run:
        with db.session.no_autoflush:
            record, execution = _work_with_execution(work, authority)
    else:
        record, execution = _work_with_execution(work, authority)
    _require_work_scope(authority, execution.addon_id)

    def lease_snapshot():
        # Scalar columns avoid reloading/decrypting the large immutable packet.
        with db.session.no_autoflush:
            return db.session.query(
                McpWorkItem.state.label("work_state"),
                McpWorkItem.lease_token.label("token"),
                McpWorkItem.lease_expires_at.label("expires_at"),
                McpWorkItem.version.label("work_version"),
                McpExecution.status.label("execution_status"),
            ).join(McpExecution, McpExecution.id == McpWorkItem.execution_id).filter(
                McpWorkItem.id == record.id,
                McpExecution.owner_user_id == authority.owner_user_id,
            ).first()

    def active_lease(snapshot) -> bool:
        return bool(
            snapshot is not None
            and snapshot.execution_status in MCP_ACTIVE_EXECUTION_STATUSES
            and snapshot.work_state == "leased"
            and snapshot.token == lease_token
            and isinstance(snapshot.expires_at, datetime)
            and snapshot.expires_at > utc_now_db()
        )

    def journal_payload() -> tuple[Any, str]:
        """Return a JSON-storable audit payload and bounded fingerprint."""

        try:
            return response_payload, _fingerprint_json(
                response_payload, limit=MAX_RESPONSE_BYTES
            )
        except McpWorkError as exc:
            marker = {
                "invalid_response": {
                    "code": exc.code,
                    "python_type": type(response_payload).__name__[:64],
                }
            }
            return marker, _fingerprint_json(marker, limit=MAX_RESPONSE_BYTES)

    def reject(
        code: str,
        *,
        error_detail: str | None = None,
        validation_errors: Iterable[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        snapshot = lease_snapshot()
        lease_active = active_lease(snapshot)
        safe_detail = error_detail or {
            "already_completed": "The work item is already completed.",
            "work_cancelled": "The work item was cancelled.",
            "execution_terminal": "The execution is no longer active.",
            "lease_required": "An active lease is required.",
            "invalid_lease_token": "The lease token is invalid.",
            "stale_lease": "The work lease is stale or expired.",
            "invalid_response": "The assistant completion is invalid.",
            "invalid_json_schema_response": "The completion does not satisfy its JSON schema.",
            "invalid_json_object_response": "The completion does not satisfy its JSON object response contract.",
        }.get(code, "The submitted completion was rejected.")
        result_kwargs = {
            "work_item_id": str(record.id),
            "status": "rejected",
            "error_code": code,
            "error_detail": safe_detail,
            "validation_errors": validation_errors,
            "lease_active": lease_active,
            "lease_expires_at": snapshot.expires_at if lease_active else None,
            "dry_run": dry_run,
        }
        if dry_run:
            return _submission_result(**result_kwargs)
        rejected_payload, rejected_fingerprint = journal_payload()
        submission = _append_submission(
            str(record.id),
            rejected_payload,
            rejected_fingerprint,
            "rejected",
            error_code=code,
        )
        result_kwargs["submission_id"] = str(submission.id)
        return _submission_result(**result_kwargs)

    if record.state == "completed":
        accepted = record.accepted_response_json if isinstance(record.accepted_response_json, dict) else None
        if accepted is not None and _fingerprint_json(accepted, limit=MAX_RESPONSE_BYTES) == _response_fingerprint_for_duplicate(response_payload):
            if dry_run:
                return _submission_result(
                    work_item_id=str(record.id), status="duplicate", dry_run=True
                )
            duplicate_payload = _normalize_structured_content_for_duplicate(response_payload)
            duplicate_fingerprint = _fingerprint_json(
                duplicate_payload, limit=MAX_RESPONSE_BYTES
            )
            submission = _append_submission(
                str(record.id), duplicate_payload, duplicate_fingerprint, "duplicate"
            )
            return _submission_result(
                work_item_id=str(record.id), status="duplicate", response=accepted, submission_id=str(submission.id)
            )
        return reject("already_completed")
    if record.state == "cancelled":
        return reject("work_cancelled")
    if execution.status not in MCP_ACTIVE_EXECUTION_STATUSES:
        return reject("execution_terminal")
    if record.state != "leased":
        return reject("lease_required")
    try:
        token = _validate_lease_token(lease_token)
    except McpWorkError as exc:
        return reject(exc.code)
    now = utc_now_db()
    if record.lease_token != token or record.lease_expires_at is None or record.lease_expires_at <= now:
        return reject("stale_lease")
    try:
        normalized = validate_response(response_payload, record.request_json)
    except McpWorkError as exc:
        return reject(
            exc.code,
            error_detail=exc.detail,
            validation_errors=exc.validation_errors,
        )
    preflight = _completion_preflight(execution, record.request_json, normalized)
    if preflight is not None and preflight["validation_errors"]:
        result = reject(
            "invalid_product_packet_response",
            error_detail="The completion failed product packet preflight. Correct the indicated fields and resubmit under the same active lease.",
            validation_errors=preflight["validation_errors"],
        )
        result["validation_stage"] = "packet_preflight"
        return result
    now = utc_now_db()
    if record.lease_expires_at <= now:
        return reject("stale_lease")
    if dry_run:
        snapshot = lease_snapshot()
        if not active_lease(snapshot) or snapshot.work_version != record.version:
            return reject("stale_lease")
        return _submission_result(
            work_item_id=str(record.id),
            status="valid",
            dry_run=True,
            preflight=preflight,
        )
    changed = db.session.execute(
        update(McpWorkItem)
        .where(
            McpWorkItem.id == record.id,
            McpWorkItem.execution_id == execution.id,
            McpWorkItem.execution_id.in_(_active_execution_ids(execution.id)),
            McpWorkItem.state == "leased",
            McpWorkItem.lease_token == token,
            McpWorkItem.lease_expires_at > now,
            McpWorkItem.version == record.version,
        )
        .values(
            state="completed",
            request_json=copy.deepcopy(MCP_REQUEST_TOMBSTONE),
            request_pruned_at=now,
            accepted_response_json=copy.deepcopy(normalized),
            lease_token=None,
            lease_owner=None,
            lease_expires_at=None,
            updated_at=now,
            completed_at=now,
            version=McpWorkItem.version + 1,
        )
        .execution_options(synchronize_session=False)
    ).rowcount
    if changed != 1:
        db.session.rollback()
        fresh, fresh_execution = _work_with_execution(record.id, authority)
        accepted = fresh.accepted_response_json if isinstance(fresh.accepted_response_json, dict) else None
        if fresh.state == "completed" and accepted is not None and _fingerprint_json(accepted, limit=MAX_RESPONSE_BYTES) == _response_fingerprint_for_duplicate(response_payload):
            duplicate_payload = _normalize_structured_content_for_duplicate(response_payload)
            duplicate_fingerprint = _fingerprint_json(
                duplicate_payload, limit=MAX_RESPONSE_BYTES
            )
            submission = _append_submission(
                str(fresh.id), duplicate_payload, duplicate_fingerprint, "duplicate"
            )
            return _submission_result(
                work_item_id=str(fresh.id), status="duplicate", response=accepted, submission_id=str(submission.id)
            )
        if fresh_execution.status not in MCP_ACTIVE_EXECUTION_STATUSES:
            return reject("execution_terminal")
        return reject("stale_lease")
    normalized_fingerprint = _fingerprint_json(normalized, limit=MAX_RESPONSE_BYTES)
    submission = _append_submission(
        str(record.id), normalized, normalized_fingerprint, "accepted"
    )
    return _submission_result(
        work_item_id=str(record.id), status="accepted", response=normalized, submission_id=str(submission.id),
        preflight=preflight,
    )


normalize_allowed_roots = _normalize_allowed_roots


__all__ = [
    "MCP_REQUEST_TOMBSTONE",
    "IssuedMcpAccessKey",
    "McpAuthError",
    "McpError",
    "McpPrincipal",
    "McpWorkClaim",
    "McpWorkError",
    "McpWorkRenewal",
    "McpWorkResolution",
    "authorize_source_path",
    "bind_execution_queue_job",
    "cancel_execution",
    "claim_next_work",
    "create_execution",
    "execution_for_queue_job",
    "get_execution",
    "get_or_create_work",
    "grant_access_key_scopes",
    "issue_access_key",
    "normalize_allowed_roots",
    "project_execution_batches",
    "redact_terminal_work",
    "release_work",
    "renew_work",
    "resolve_access_key",
    "revoke_access_key",
    "serialize_execution",
    "submit_work",
    "transition_execution",
    "validate_response",
]
