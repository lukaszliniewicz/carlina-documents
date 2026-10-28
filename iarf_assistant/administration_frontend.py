from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from flask import (
    Blueprint,
    Response,
    current_app,
    jsonify,
    request,
    stream_with_context,
)
from flask_login import current_user, login_required
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import desc, func

from iarf_assistant.core.app_paths import app_paths_from_context
from iarf_assistant.core.backup_service import (
    BackupError,
    create_backup,
    inspect_backup,
)
from iarf_assistant.core.encryption import InstallationKeyStore
from iarf_assistant.core.frontend_contracts import (
    FRONTEND_API_VERSION,
    set_frontend_api_version_headers,
)
from iarf_assistant.core.logging_observability import redact_text
from iarf_assistant.core.restore_service import (
    PendingRestoreExists,
    schedule_restore,
)
from iarf_assistant.extensions import db
from iarf_assistant.models import (
    DataContext,
    DataContextChunk,
    LLMCost,
    LLMModel,
    Setting,
    User,
)
from iarf_assistant.security import admin_required

frontend_administration_blueprint = Blueprint(
    "frontend_administration_api",
    __name__,
)

MAX_POLICY_CHARS = 100_000
MAX_MEMBERS = 200
MAX_USAGE_DAYS = 366
MAX_USAGE_ROWS = 1_000
RAG_TASK_TYPES = {"search", "qa", "fact", "code"}
RAG_LIMITS = {
    "dimensions": (128, 3_072),
    "top_k": (1, 20),
    "chunk_size": (500, 15_000),
    "chunk_overlap": (0, 2_000),
}
RAG_DEFAULTS = {
    "model_id": "openai/text-embedding-3-small",
    "dimensions": 768,
    "task_type": "search",
    "top_k": 5,
    "chunk_size": 5_000,
    "chunk_overlap": 500,
}
BACKUP_NAME_PATTERN = re.compile(
    r"^carlina-backup-\d{8}T\d{6}Z-[0-9a-f]{10}\.zip$"
)
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PREFLIGHT_MAX_AGE_SECONDS = 15 * 60


def _iso(value: Any) -> str | None:
    if value is None or not hasattr(value, "isoformat"):
        return None
    rendered = value.isoformat()
    if isinstance(value, datetime) and value.tzinfo is None:
        return f"{rendered}Z"
    return rendered


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _setting_text(key: str) -> str:
    setting = db.session.get(Setting, key)
    return str(setting.value or "") if setting else ""


def _set_setting(key: str, value: str) -> None:
    setting = db.session.get(Setting, key)
    if setting:
        setting.value = value
    else:
        db.session.add(Setting(key=key, value=value))


def _bounded_text(value: Any, *, maximum: int) -> tuple[str, bool]:
    rendered = str(value or "")
    return rendered[:maximum], len(rendered) > maximum


def _parsed_int(
    key: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> tuple[int, bool]:
    raw = _setting_text(key).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default, not raw
    return value, minimum <= value <= maximum


def _database_path() -> Path:
    return app_paths_from_context().database_file


def _backup_directory() -> Path:
    return app_paths_from_context().backups_dir


def _backup_records() -> list[dict[str, Any]]:
    backup_dir = _backup_directory()
    if not backup_dir.is_dir():
        return []
    records = []
    for path in backup_dir.glob("*.zip"):
        if not path.is_file() or not BACKUP_NAME_PATTERN.fullmatch(path.name):
            continue
        stat = path.stat()
        try:
            manifest = inspect_backup(path)
        except BackupError:
            continue
        records.append(
            {
                "filename": path.name,
                "size_bytes": int(stat.st_size),
                "created_at": str(manifest["created_at"]),
                "file_count": int(manifest["file_count"]),
                "expanded_bytes": int(manifest["expanded_bytes"]),
                "required_key_ids": list(
                    manifest["encryption"]["required_key_ids"]
                ),
            }
        )
    return sorted(
        records,
        key=lambda item: (item["created_at"], item["filename"]),
        reverse=True,
    )


def _secret_key_state() -> dict[str, Any]:
    from_environment = bool(os.environ.get("FLASK_SECRET_KEY"))
    temporary = bool(
        current_app.config.get("IARF_SECRET_KEY_IS_TEMPORARY", True)
    )
    if from_environment:
        source = "environment"
        label = "Environment managed"
    elif not temporary:
        source = "restricted_file"
        label = "Persistent installation key file"
    else:
        source = "temporary"
        label = "Temporary"
    return {
        "source": source,
        "label": label,
        "persistent": not temporary,
        "recommended": from_environment,
        "browser_rotation_available": False,
        "note": (
            "The installer-managed environment is the supported place for "
            "session signing secrets. Secret values never cross this API."
        ),
    }


def _normalize_public_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if len(raw) > 2_048:
        raise ValueError("Application URL is too long.")
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise ValueError("Application URL is invalid.") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Application URL must be an HTTP(S) base URL without credentials, "
            "a query, or a fragment."
        )
    path = parsed.path.rstrip("/")
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            path,
            "",
            "",
        )
    )


def _general_payload() -> dict[str, Any]:
    policy, policy_truncated = _bounded_text(
        _setting_text("site_terms_md"),
        maximum=MAX_POLICY_CHARS,
    )
    app_url = _setting_text("app_url").strip()
    return {
        "application_url": app_url,
        "application_url_configured": bool(app_url),
        "site_policy": {
            "markdown": policy,
            "published": bool(policy.strip()),
            "preview_url": "/terms",
            "characters": len(policy),
            "truncated": policy_truncated,
        },
        "session_security": _secret_key_state(),
        "limits": {
            "policy_characters": MAX_POLICY_CHARS,
        },
    }


def _shared_embedding_models() -> list[LLMModel]:
    return (
        LLMModel.query.filter_by(
            user_id=None,
            is_active=True,
            model_type="embedding",
        )
        .order_by(
            func.lower(LLMModel.display_name),
            LLMModel.model_id,
        )
        .all()
    )


def _embedding_model_payload(model: LLMModel) -> dict[str, Any]:
    provider_name = (
        str(model.provider.name)
        if model.provider and model.provider.name
        else str(model.model_id).split("/", 1)[0]
    )
    return {
        "id": str(model.model_id),
        "name": str(model.display_name or model.model_id),
        "provider": provider_name,
        "provider_label": (
            str(model.provider.display_name or model.provider.name)
            if model.provider
            else provider_name.replace("_", " ").title()
        ),
        "supports_mrl": bool(model.supports_mrl),
        "default_dimensions": (
            int(model.default_dimensions)
            if model.default_dimensions is not None
            else None
        ),
        "prefix_style": str(model.prefix_style or "none"),
    }


def _retrieval_payload() -> dict[str, Any]:
    models = _shared_embedding_models()
    model_id = (
        _setting_text("rag_embedding_model").strip()
        or RAG_DEFAULTS["model_id"]
    )
    dimensions, dimensions_valid = _parsed_int(
        "rag_embedding_dimensions",
        default=int(RAG_DEFAULTS["dimensions"]),
        minimum=RAG_LIMITS["dimensions"][0],
        maximum=RAG_LIMITS["dimensions"][1],
    )
    top_k, top_k_valid = _parsed_int(
        "rag_top_k",
        default=int(RAG_DEFAULTS["top_k"]),
        minimum=RAG_LIMITS["top_k"][0],
        maximum=RAG_LIMITS["top_k"][1],
    )
    chunk_size, chunk_size_valid = _parsed_int(
        "rag_chunk_size",
        default=int(RAG_DEFAULTS["chunk_size"]),
        minimum=RAG_LIMITS["chunk_size"][0],
        maximum=RAG_LIMITS["chunk_size"][1],
    )
    chunk_overlap, overlap_valid = _parsed_int(
        "rag_chunk_overlap",
        default=int(RAG_DEFAULTS["chunk_overlap"]),
        minimum=RAG_LIMITS["chunk_overlap"][0],
        maximum=RAG_LIMITS["chunk_overlap"][1],
    )
    task_type = (
        _setting_text("rag_task_type").strip().lower()
        or str(RAG_DEFAULTS["task_type"])
    )
    selected = next(
        (model for model in models if model.model_id == model_id),
        None,
    )
    fixed_dimensions_valid = bool(
        not selected
        or selected.supports_mrl
        or selected.default_dimensions is None
        or int(selected.default_dimensions) == dimensions
    )
    validation = {
        "model_available": selected is not None,
        "dimensions": dimensions_valid and fixed_dimensions_valid,
        "task_type": task_type in RAG_TASK_TYPES,
        "top_k": top_k_valid,
        "chunk_size": chunk_size_valid,
        "chunk_overlap": overlap_valid and chunk_overlap < chunk_size,
    }
    return {
        "configuration": {
            "model_id": model_id,
            "dimensions": dimensions,
            "task_type": task_type,
            "top_k": top_k,
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
        },
        "models": [_embedding_model_payload(model) for model in models],
        "index": {
            "documents": int(DataContext.query.count()),
            "chunks": int(DataContextChunk.query.count()),
        },
        "validation": validation,
        "configuration_needs_review": not all(validation.values()),
        "task_types": [
            {
                "id": "search",
                "label": "Search and retrieval",
                "description": "Balanced semantic retrieval for general knowledge.",
            },
            {
                "id": "qa",
                "label": "Question answering",
                "description": "Retrieval tuned for answer-bearing passages.",
            },
            {
                "id": "fact",
                "label": "Fact checking",
                "description": "Evidence matching for claims and verification.",
            },
            {
                "id": "code",
                "label": "Code retrieval",
                "description": "Semantic matching for source and technical text.",
            },
        ],
        "limits": {
            key: {"minimum": value[0], "maximum": value[1]}
            for key, value in RAG_LIMITS.items()
        },
        "actions": {
            "update_url": (
                "/api/frontend/settings/administration/retrieval"
            ),
            "preflight_url": (
                "/api/frontend/settings/administration/retrieval/preflight"
            ),
            "rebuild_url": (
                "/api/frontend/settings/administration/retrieval/rebuild"
            ),
        },
    }


def _member_record(user: User, *, admin_count: int) -> dict[str, Any]:
    is_current = int(user.id) == int(current_user.id)
    return {
        "id": int(user.id),
        "email": str(user.email),
        "is_admin": bool(user.is_admin),
        "is_current": is_current,
        "can_delete": not is_current and not (
            bool(user.is_admin) and admin_count <= 1
        ),
        "can_change_role": not (
            bool(user.is_admin) and admin_count <= 1
        ),
    }


def _members_payload() -> dict[str, Any]:
    total = User.query.count()
    admin_count = User.query.filter_by(is_admin=True).count()
    users = (
        User.query.order_by(
            User.is_admin.desc(),
            func.lower(User.email),
        )
        .limit(MAX_MEMBERS)
        .all()
    )
    return {
        "summary": {
            "total": int(total),
            "administrators": int(admin_count),
            "members": max(0, int(total) - int(admin_count)),
            "truncated": total > MAX_MEMBERS,
        },
        "members": [
            _member_record(user, admin_count=admin_count) for user in users
        ],
        "limits": {
            "members": MAX_MEMBERS,
            "email_characters": 100,
            "password_characters": {
                "minimum": 12,
                "maximum": 128,
            },
        },
        "actions": {
            "collection_url": (
                "/api/frontend/settings/administration/members"
            ),
        },
    }


def _continuity_payload() -> dict[str, Any]:
    database_path = _database_path()
    backups = _backup_records()
    return {
        "database": {
            "available": database_path.is_file(),
            "size_bytes": (
                int(database_path.stat().st_size)
                if database_path.is_file()
                else 0
            ),
        },
        "backups": backups,
        "summary": {
            "backup_count": len(backups),
            "latest_backup_at": (
                backups[0]["created_at"] if backups else None
            ),
        },
        "migration_policy": {
            "plaintext_export_available": False,
            "browser_reencryption_available": False,
            "note": (
                "Bulk plaintext export and user-deleting migration tools are "
                "intentionally unavailable from the browser."
            ),
        },
        "actions": {
            "create_backup_url": (
                "/api/frontend/settings/administration/backups"
            ),
            "restore_backup_url": (
                "/api/frontend/settings/administration/restore"
            ),
        },
    }


def _dashboard_payload() -> dict[str, Any]:
    members = _members_payload()
    retrieval = _retrieval_payload()
    continuity = _continuity_payload()
    return {
        "frontend_api_version": FRONTEND_API_VERSION,
        "generated_at": _now_iso(),
        "summary": {
            "members": members["summary"]["total"],
            "administrators": members["summary"]["administrators"],
            "backups": continuity["summary"]["backup_count"],
            "rag_documents": retrieval["index"]["documents"],
            "rag_needs_review": retrieval["configuration_needs_review"],
        },
        "general": _general_payload(),
        "retrieval": retrieval,
        "continuity": continuity,
        "members": members,
        "actions": {
            "general_update_url": (
                "/api/frontend/settings/administration/general"
            ),
            "usage_url": (
                "/api/frontend/settings/administration/usage"
            ),
        },
    }


def _json_object() -> tuple[dict[str, Any] | None, Any | None]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, (
            jsonify({"error": "A JSON object is required."}),
            400,
        )
    return data, None


def _validate_integer(
    value: Any,
    *,
    field: str,
    minimum: int,
    maximum: int,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        raise ValueError(
            f"{field} must be a whole number between "
            f"{minimum:,} and {maximum:,}."
        )
    return value


def _normalize_retrieval_update(data: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "model_id",
        "dimensions",
        "task_type",
        "top_k",
        "chunk_size",
        "chunk_overlap",
        "acknowledge_rebuild",
        "preflight_token",
    }
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(
            f"Unsupported retrieval field: {sorted(unknown)[0]}."
        )
    required = {
        "model_id",
        "dimensions",
        "task_type",
        "top_k",
        "chunk_size",
        "chunk_overlap",
    }
    missing = required - set(data)
    if missing:
        raise ValueError(
            f"Missing retrieval field: {sorted(missing)[0]}."
        )
    model_id = str(data.get("model_id") or "").strip()
    model = LLMModel.query.filter_by(
        model_id=model_id,
        user_id=None,
        is_active=True,
        model_type="embedding",
    ).first()
    if not model:
        raise ValueError(
            "The retrieval model must be an active shared embedding model."
        )
    dimensions = _validate_integer(
        data.get("dimensions"),
        field="Dimensions",
        minimum=RAG_LIMITS["dimensions"][0],
        maximum=RAG_LIMITS["dimensions"][1],
    )
    if (
        not model.supports_mrl
        and model.default_dimensions is not None
        and dimensions != int(model.default_dimensions)
    ):
        raise ValueError(
            f"{model.display_name} uses a fixed "
            f"{int(model.default_dimensions):,}-dimension vector."
        )
    task_type = str(data.get("task_type") or "").strip().lower()
    if task_type not in RAG_TASK_TYPES:
        raise ValueError("Embedding task type is not supported.")
    top_k = _validate_integer(
        data.get("top_k"),
        field="Top K",
        minimum=RAG_LIMITS["top_k"][0],
        maximum=RAG_LIMITS["top_k"][1],
    )
    chunk_size = _validate_integer(
        data.get("chunk_size"),
        field="Chunk size",
        minimum=RAG_LIMITS["chunk_size"][0],
        maximum=RAG_LIMITS["chunk_size"][1],
    )
    chunk_overlap = _validate_integer(
        data.get("chunk_overlap"),
        field="Chunk overlap",
        minimum=RAG_LIMITS["chunk_overlap"][0],
        maximum=RAG_LIMITS["chunk_overlap"][1],
    )
    if chunk_overlap >= chunk_size:
        raise ValueError("Chunk overlap must be smaller than chunk size.")
    acknowledge = data.get("acknowledge_rebuild", False)
    if not isinstance(acknowledge, bool):
        raise ValueError("Rebuild acknowledgement must be boolean.")
    return {
        "model_id": model_id,
        "dimensions": dimensions,
        "task_type": task_type,
        "top_k": top_k,
        "chunk_size": chunk_size,
        "chunk_overlap": chunk_overlap,
        "acknowledge_rebuild": acknowledge,
        "preflight_token": str(data.get("preflight_token") or ""),
    }


def _preflight_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        current_app.secret_key,
        salt="iarf-rag-preflight-v1",
    )


def _preflight_claim(
    *,
    model_id: str,
    dimensions: int,
    task_type: str,
) -> dict[str, Any]:
    return {
        "user_id": int(current_user.id),
        "model_id": model_id,
        "dimensions": dimensions,
        "task_type": task_type,
    }


def _validate_preflight_token(
    token: str,
    *,
    model_id: str,
    dimensions: int,
    task_type: str,
) -> bool:
    if not token:
        return False
    try:
        claim = _preflight_serializer().loads(
            token,
            max_age=PREFLIGHT_MAX_AGE_SECONDS,
        )
    except (BadSignature, SignatureExpired):
        return False
    return claim == _preflight_claim(
        model_id=model_id,
        dimensions=dimensions,
        task_type=task_type,
    )


def _extract_vector(item: Any) -> Any:
    return (
        item.get("embedding")
        if isinstance(item, dict)
        else getattr(item, "embedding")
    )


def _run_embedding_preflight(
    *,
    model_id: str,
    dimensions: int,
    task_type: str,
) -> tuple[int, int]:
    from iarf_assistant.embedding_profiles import resolve_embedding_profile
    from iarf_assistant.llm_service import LLMService

    profile = resolve_embedding_profile(
        model=model_id,
        task_type=task_type,
        user_id=int(current_user.id),
    )
    service = LLMService(user_id=current_user.id)
    document_response = service.embedding(
        model=model_id,
        input=profile.prepare_documents(
            ["A short retrieval preflight document."],
            titles=["preflight"],
        ),
        embedding_profile=profile,
        **profile.request_kwargs_for_documents(dimensions=dimensions),
    )
    query_response = service.embedding(
        model=model_id,
        input=[profile.prepare_query("A retrieval preflight query.")],
        embedding_profile=profile,
        **profile.request_kwargs_for_query(dimensions=dimensions),
    )
    document_dimensions = len(_extract_vector(document_response.data[0]))
    query_dimensions = len(_extract_vector(query_response.data[0]))
    return document_dimensions, query_dimensions


def _validate_email(value: Any) -> str:
    email = str(value or "").strip().lower()
    if (
        not email
        or len(email) > 100
        or not EMAIL_PATTERN.fullmatch(email)
    ):
        raise ValueError("Enter a valid email address.")
    return email


def _validate_password(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Password must be text.")
    if len(value) < 12 or len(value) > 128:
        raise ValueError(
            "Password must contain between 12 and 128 characters."
        )
    return value


def _create_complete_backup() -> dict[str, Any]:
    paths = app_paths_from_context()
    key_store = InstallationKeyStore(paths.master_keys_file)
    record = create_backup(paths, key_store=key_store)
    return next(
        item for item in _backup_records() if item["filename"] == record.path.name
    )


def _usage_dates() -> tuple[date, date]:
    end_raw = request.args.get("end_date")
    start_raw = request.args.get("start_date")
    try:
        end = (
            datetime.strptime(end_raw, "%Y-%m-%d").date()
            if end_raw
            else datetime.now(timezone.utc).date()
        )
        start = (
            datetime.strptime(start_raw, "%Y-%m-%d").date()
            if start_raw
            else end - timedelta(days=29)
        )
    except ValueError as exc:
        raise ValueError("Usage dates must use YYYY-MM-DD.") from exc
    if start > end:
        raise ValueError("Usage start date must not be after the end date.")
    if (end - start).days + 1 > MAX_USAGE_DAYS:
        raise ValueError(
            f"Usage ranges may span at most {MAX_USAGE_DAYS} days."
        )
    return start, end


def _usage_payload(start: date, end: date) -> dict[str, Any]:
    end_exclusive = end + timedelta(days=1)
    day_expression = func.date(LLMCost.timestamp)
    daily_rows = (
        db.session.query(
            day_expression.label("date"),
            LLMCost.source,
            func.sum(LLMCost.cost).label("cost"),
            func.sum(LLMCost.total_tokens).label("tokens"),
            func.count(LLMCost.id).label("requests"),
        )
        .filter(
            LLMCost.timestamp >= start,
            LLMCost.timestamp < end_exclusive,
        )
        .group_by(day_expression, LLMCost.source)
        .order_by(day_expression, LLMCost.source)
        .limit(MAX_USAGE_ROWS)
        .all()
    )
    user_rows = (
        db.session.query(
            User.id,
            User.email,
            func.sum(LLMCost.cost).label("cost"),
            func.sum(LLMCost.total_tokens).label("tokens"),
            func.count(LLMCost.id).label("requests"),
        )
        .join(User, User.id == LLMCost.user_id)
        .filter(
            LLMCost.timestamp >= start,
            LLMCost.timestamp < end_exclusive,
        )
        .group_by(User.id, User.email)
        .order_by(desc(func.sum(LLMCost.cost)), User.email)
        .limit(MAX_MEMBERS)
        .all()
    )
    daily = [
        {
            "date": str(row.date),
            "source": str(row.source or "unattributed"),
            "source_label": str(row.source or "Unattributed")
            .replace("_", " ")
            .replace(".", " / ")
            .title(),
            "cost": float(row.cost or 0),
            "tokens": int(row.tokens or 0),
            "requests": int(row.requests or 0),
        }
        for row in daily_rows
    ]
    by_user = [
        {
            "id": int(row.id),
            "email": str(row.email),
            "cost": float(row.cost or 0),
            "tokens": int(row.tokens or 0),
            "requests": int(row.requests or 0),
        }
        for row in user_rows
    ]
    return {
        "frontend_api_version": FRONTEND_API_VERSION,
        "generated_at": _now_iso(),
        "range": {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "days": (end - start).days + 1,
        },
        "summary": {
            "cost": sum(item["cost"] for item in daily),
            "tokens": sum(item["tokens"] for item in daily),
            "requests": sum(item["requests"] for item in daily),
            "active_members": len(by_user),
        },
        "daily": daily,
        "by_user": by_user,
        "limits": {
            "maximum_days": MAX_USAGE_DAYS,
            "maximum_daily_rows": MAX_USAGE_ROWS,
            "maximum_members": MAX_MEMBERS,
        },
    }


@frontend_administration_blueprint.after_request
def _frontend_administration_headers(response):
    set_frontend_api_version_headers(response)
    response.headers["Cache-Control"] = "private, no-cache"
    return response


@frontend_administration_blueprint.route(
    "/api/frontend/settings/administration",
    methods=["GET"],
)
@login_required
@admin_required
def get_frontend_administration():
    return jsonify(_dashboard_payload())


@frontend_administration_blueprint.route(
    "/api/frontend/settings/administration/general",
    methods=["PUT"],
)
@login_required
@admin_required
def update_frontend_general_settings():
    data, error = _json_object()
    if error:
        return error
    unknown = set(data) - {"application_url", "site_policy_markdown"}
    if unknown:
        return jsonify(
            {"error": f"Unsupported general field: {sorted(unknown)[0]}."}
        ), 400
    if not data:
        return jsonify({"error": "No general settings were provided."}), 400
    updates: dict[str, str] = {}
    try:
        if "application_url" in data:
            updates["app_url"] = _normalize_public_url(
                data["application_url"]
            )
        if "site_policy_markdown" in data:
            policy = data["site_policy_markdown"]
            if not isinstance(policy, str):
                raise ValueError("Site policy must be text.")
            policy = policy.replace("\x00", "").strip()
            if len(policy) > MAX_POLICY_CHARS:
                raise ValueError(
                    "Site policy exceeds the "
                    f"{MAX_POLICY_CHARS:,}-character limit."
                )
            updates["site_terms_md"] = policy
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    for key, value in updates.items():
        _set_setting(key, value)
    db.session.commit()
    return jsonify(
        {
            "success": True,
            "message": "Application publishing settings updated.",
            "general": _general_payload(),
        }
    )


@frontend_administration_blueprint.route(
    "/api/frontend/settings/administration/retrieval",
    methods=["GET"],
)
@login_required
@admin_required
def get_frontend_retrieval_settings():
    return jsonify(_retrieval_payload())


@frontend_administration_blueprint.route(
    "/api/frontend/settings/administration/retrieval/preflight",
    methods=["POST"],
)
@login_required
@admin_required
def preflight_frontend_retrieval_settings():
    data, error = _json_object()
    if error:
        return error
    try:
        normalized = _normalize_retrieval_update(
            {
                **_retrieval_payload()["configuration"],
                **data,
                "acknowledge_rebuild": False,
            }
        )
        document_dimensions, query_dimensions = _run_embedding_preflight(
            model_id=normalized["model_id"],
            dimensions=normalized["dimensions"],
            task_type=normalized["task_type"],
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify(
            {
                "error": (
                    "Embedding preflight failed: "
                    f"{redact_text(str(exc))}"
                )
            }
        ), 400
    if (
        document_dimensions != normalized["dimensions"]
        or query_dimensions != normalized["dimensions"]
    ):
        return jsonify(
            {
                "error": (
                    "The model returned "
                    f"{document_dimensions:,}/{query_dimensions:,} dimensions "
                    f"instead of {normalized['dimensions']:,}."
                )
            }
        ), 400
    token = _preflight_serializer().dumps(
        _preflight_claim(
            model_id=normalized["model_id"],
            dimensions=normalized["dimensions"],
            task_type=normalized["task_type"],
        )
    )
    return jsonify(
        {
            "success": True,
            "message": "Embedding model and dimensions are ready.",
            "model_id": normalized["model_id"],
            "document_dimensions": document_dimensions,
            "query_dimensions": query_dimensions,
            "preflight_token": token,
            "expires_in_seconds": PREFLIGHT_MAX_AGE_SECONDS,
        }
    )


@frontend_administration_blueprint.route(
    "/api/frontend/settings/administration/retrieval",
    methods=["PUT"],
)
@login_required
@admin_required
def update_frontend_retrieval_settings():
    data, error = _json_object()
    if error:
        return error
    try:
        normalized = _normalize_retrieval_update(data)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    current = _retrieval_payload()["configuration"]
    rebuild_fields = {
        "model_id",
        "dimensions",
        "task_type",
        "chunk_size",
        "chunk_overlap",
    }
    changed_rebuild_fields = {
        field
        for field in rebuild_fields
        if normalized[field] != current[field]
    }
    embedding_changed = bool(
        changed_rebuild_fields
        & {"model_id", "dimensions", "task_type"}
    )
    if changed_rebuild_fields and not normalized["acknowledge_rebuild"]:
        return jsonify(
            {
                "error": (
                    "Structural retrieval changes require an explicit "
                    "index-rebuild acknowledgement."
                )
            }
        ), 400
    if embedding_changed and not _validate_preflight_token(
        normalized["preflight_token"],
        model_id=normalized["model_id"],
        dimensions=normalized["dimensions"],
        task_type=normalized["task_type"],
    ):
        return jsonify(
            {
                "error": (
                    "Run a successful embedding preflight before saving "
                    "model, dimension, or task changes."
                )
            }
        ), 400

    for field, setting_key in (
        ("model_id", "rag_embedding_model"),
        ("dimensions", "rag_embedding_dimensions"),
        ("task_type", "rag_task_type"),
        ("top_k", "rag_top_k"),
        ("chunk_size", "rag_chunk_size"),
        ("chunk_overlap", "rag_chunk_overlap"),
    ):
        _set_setting(setting_key, str(normalized[field]))
    db.session.commit()
    return jsonify(
        {
            "success": True,
            "message": (
                "Retrieval settings updated. Rebuild the index before "
                "depending on the new structure."
                if changed_rebuild_fields
                else "Retrieval settings updated."
            ),
            "requires_rebuild": bool(changed_rebuild_fields),
            "retrieval": _retrieval_payload(),
        }
    )


@frontend_administration_blueprint.route(
    "/api/frontend/settings/administration/retrieval/rebuild",
    methods=["POST"],
)
@login_required
@admin_required
def rebuild_frontend_retrieval_index():
    data, error = _json_object()
    if error:
        return error
    if data.get("confirmation") != "REBUILD INDEX":
        return jsonify(
            {"error": 'Type "REBUILD INDEX" to confirm the rebuild.'}
        ), 400

    def event(payload: dict[str, Any]) -> str:
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def generate():
        yield event(
            {
                "type": "progress",
                "message": "Clearing vector tables…",
                "progress": 0,
            }
        )
        try:
            current_app.rag_service.delete_all_embeddings()
            contexts = DataContext.query.order_by(DataContext.id).all()
            total = len(contexts)
            if not contexts:
                yield event(
                    {
                        "type": "complete",
                        "message": "No documents require indexing.",
                        "progress": 100,
                        "done": True,
                    }
                )
                return
            for index, context in enumerate(contexts, start=1):
                current_app.rag_service.generate_embeddings_for_context(
                    context.id
                )
                progress = 5 + int((index / total) * 88)
                yield event(
                    {
                        "type": "progress",
                        "message": (
                            f"Indexed document {index:,} of {total:,}."
                        ),
                        "progress": progress,
                    }
                )
            current_app.addon_manager.notify_rag_rebuild()
            yield event(
                {
                    "type": "complete",
                    "message": "Retrieval index rebuilt.",
                    "progress": 100,
                    "done": True,
                }
            )
        except Exception as exc:
            yield event(
                {
                    "type": "error",
                    "message": redact_text(str(exc)),
                    "done": True,
                }
            )

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
    )


@frontend_administration_blueprint.route(
    "/api/frontend/settings/administration/backups",
    methods=["POST"],
)
@login_required
@admin_required
def create_frontend_backup():
    data, error = _json_object()
    if error:
        return error
    if data.get("acknowledge") is not True:
        return jsonify(
            {"error": "Backup creation requires an explicit acknowledgement."}
        ), 400
    try:
        backup = _create_complete_backup()
    except BackupError as exc:
        return jsonify(
            {
                "error": (
                    "Complete backup could not be created: "
                    f"{redact_text(str(exc))}"
                )
            }
        ), 500
    return jsonify(
        {
            "success": True,
            "message": f"Backup {backup['filename']} created.",
            "backup": backup,
            "continuity": _continuity_payload(),
        }
    )


@frontend_administration_blueprint.route(
    "/api/frontend/settings/administration/restore",
    methods=["POST"],
)
@login_required
@admin_required
def restore_frontend_backup():
    data, error = _json_object()
    if error:
        return error
    filename = str(data.get("filename") or "").strip()
    if (
        not BACKUP_NAME_PATTERN.fullmatch(filename)
        or Path(filename).name != filename
    ):
        return jsonify({"error": "Select a valid database backup."}), 400
    if data.get("confirmation") != filename:
        return jsonify(
            {"error": "Type the exact backup filename to confirm restore."}
        ), 400
    backup_path = _backup_directory() / filename
    paths = app_paths_from_context()
    key_store = InstallationKeyStore(paths.master_keys_file)
    try:
        scheduled = schedule_restore(
            paths,
            backup_path,
            key_store=key_store,
            requested_by_user_id=int(current_user.id),
        )
        safety_backup = next(
            item
            for item in _backup_records()
            if item["filename"] == scheduled.safety_backup.path.name
        )
    except PendingRestoreExists as exc:
        return jsonify({"error": redact_text(str(exc))}), 409
    except (BackupError, OSError, ValueError) as exc:
        return jsonify(
            {
                "error": (
                    "Database restore could not be completed: "
                    f"{redact_text(str(exc))}"
                )
            }
        ), 500
    return jsonify(
        {
            "success": True,
            "message": (
                f"Backup {filename} was fully verified and scheduled. Restart "
                "Carlina to activate it atomically."
            ),
            "safety_backup": safety_backup,
            "requires_restart": True,
        }
    )


@frontend_administration_blueprint.route(
    "/api/frontend/settings/administration/members",
    methods=["GET"],
)
@login_required
@admin_required
def get_frontend_members():
    return jsonify(_members_payload())


@frontend_administration_blueprint.route(
    "/api/frontend/settings/administration/members",
    methods=["POST"],
)
@login_required
@admin_required
def create_frontend_member():
    data, error = _json_object()
    if error:
        return error
    unknown = set(data) - {"email", "password", "is_admin"}
    if unknown:
        return jsonify(
            {"error": f"Unsupported member field: {sorted(unknown)[0]}."}
        ), 400
    try:
        email = _validate_email(data.get("email"))
        password = _validate_password(data.get("password"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    is_admin = data.get("is_admin", False)
    if not isinstance(is_admin, bool):
        return jsonify({"error": "Administrator state must be boolean."}), 400
    if User.query.filter(func.lower(User.email) == email).first():
        return jsonify(
            {"error": "A member already uses that email address."}
        ), 409
    user = User(email=email, is_admin=is_admin)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return jsonify(
        {
            "success": True,
            "message": f"{email} added.",
            "directory": _members_payload(),
        }
    ), 201


@frontend_administration_blueprint.route(
    "/api/frontend/settings/administration/members/<int:user_id>",
    methods=["PUT"],
)
@login_required
@admin_required
def update_frontend_member(user_id: int):
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"error": "Member not found."}), 404
    data, error = _json_object()
    if error:
        return error
    unknown = set(data) - {"email", "is_admin"}
    if unknown:
        return jsonify(
            {"error": f"Unsupported member field: {sorted(unknown)[0]}."}
        ), 400
    if not data:
        return jsonify({"error": "No member changes were provided."}), 400
    email = str(user.email)
    if "email" in data:
        try:
            email = _validate_email(data["email"])
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        existing = User.query.filter(
            func.lower(User.email) == email,
            User.id != user.id,
        ).first()
        if existing:
            return jsonify(
                {"error": "A member already uses that email address."}
            ), 409
    is_admin = bool(user.is_admin)
    if "is_admin" in data:
        if not isinstance(data["is_admin"], bool):
            return jsonify(
                {"error": "Administrator state must be boolean."}
            ), 400
        is_admin = data["is_admin"]
        if (
            user.is_admin
            and not is_admin
            and User.query.filter_by(is_admin=True).count() <= 1
        ):
            return jsonify(
                {"error": "The final administrator cannot be demoted."}
            ), 400
    user.email = email
    user.is_admin = is_admin
    db.session.commit()
    return jsonify(
        {
            "success": True,
            "message": f"{email} updated.",
            "directory": _members_payload(),
        }
    )


@frontend_administration_blueprint.route(
    "/api/frontend/settings/administration/members/<int:user_id>",
    methods=["DELETE"],
)
@login_required
@admin_required
def delete_frontend_member(user_id: int):
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"error": "Member not found."}), 404
    if int(user.id) == int(current_user.id):
        return jsonify({"error": "You cannot delete your own account."}), 400
    if user.is_admin and User.query.filter_by(is_admin=True).count() <= 1:
        return jsonify(
            {"error": "The final administrator cannot be deleted."}
        ), 400
    data, error = _json_object()
    if error:
        return error
    if data.get("confirmation") != user.email:
        return jsonify(
            {"error": "Type the member email address to confirm deletion."}
        ), 400
    email = str(user.email)
    db.session.delete(user)
    db.session.commit()
    return jsonify(
        {
            "success": True,
            "message": f"{email} removed.",
            "directory": _members_payload(),
        }
    )


@frontend_administration_blueprint.route(
    "/api/frontend/settings/administration/usage",
    methods=["GET"],
)
@login_required
@admin_required
def get_frontend_usage():
    try:
        start, end = _usage_dates()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(_usage_payload(start, end))


def register_frontend_administration_blueprint(app) -> None:
    app.register_blueprint(frontend_administration_blueprint)


__all__ = [
    "MAX_MEMBERS",
    "MAX_POLICY_CHARS",
    "MAX_USAGE_DAYS",
    "RAG_DEFAULTS",
    "RAG_LIMITS",
    "RAG_TASK_TYPES",
    "frontend_administration_blueprint",
    "register_frontend_administration_blueprint",
]
