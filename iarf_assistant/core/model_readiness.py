"""Truthful, non-secret model readiness shared by UI and execution callers.

Readiness deliberately means that a tenant-visible, active model has an active
credential that Carlina's LLM execution path can resolve.  It does not make a
network request and therefore does not claim that the remote provider is
reachable.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from flask import has_app_context
from sqlalchemy import and_, case, or_, select

from ..extensions import db
from ..llm_provider_utils import infer_provider_name
from ..models import ApiKey, LLMModel, LLMProvider
from .model_access import (
    resolve_effective_model_record,
    resolve_effective_model_record_by_suffix,
)

READINESS_MESSAGES = {
    "invalid_model_id": "A model identifier is required.",
    "model_not_configured": "The selected model is not configured for this user.",
    "model_inactive": "The selected model is inactive.",
    "wrong_model_type": "The selected model does not support this operation type.",
    "provider_unavailable": "The selected model has no usable provider configuration.",
    "credential_unavailable": "The selected model's provider has no active credential.",
}


@dataclass(frozen=True)
class ModelConnection:
    """Credential-selection inputs without exposing decrypted credential values."""

    model_record: LLMModel | None
    provider: LLMProvider | None
    provider_name: str | None
    api_key_record: ApiKey | None
    credential_source: str | None


@dataclass(frozen=True)
class ModelReadiness:
    model_id: str
    ready: bool
    reason: str | None
    message: str
    provider_name: str | None
    model_type: str | None
    credential_source: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "readiness_reason": self.reason,
            "readiness_message": self.message,
            "credential_source": self.credential_source,
        }


class ModelNotReadyError(ValueError):
    """Raised when a caller attempts to persist or execute an unusable model."""

    def __init__(self, readiness: ModelReadiness):
        self.readiness = readiness
        super().__init__(readiness.message)

    @property
    def reason(self) -> str | None:
        return self.readiness.reason


def _resolve_model_record(model_id: str, user_id: int | None) -> LLMModel | None:
    record = resolve_effective_model_record(model_id, user_id=user_id)
    if record is None and "/" not in model_id:
        record = resolve_effective_model_record_by_suffix(model_id, user_id=user_id)
    return record


def _provider_name(model_id: str, record: LLMModel | None) -> str | None:
    configured_name = (
        record.provider.name
        if record is not None and record.provider is not None
        else None
    )
    return infer_provider_name(model_id, configured_name)


def _credential_query(provider_names: Iterable[str], user_id: int | None):
    names = tuple(sorted({name for name in provider_names if name}))
    if not names:
        return ()

    if user_id is None:
        ownership = and_(
            ApiKey.user_id.is_(None),
            LLMProvider.user_id.is_(None),
        )
        preference = ApiKey.id.asc()
    else:
        ownership = or_(
            and_(
                ApiKey.user_id == user_id,
                or_(
                    LLMProvider.user_id == user_id,
                    LLMProvider.user_id.is_(None),
                ),
            ),
            and_(
                ApiKey.user_id.is_(None),
                LLMProvider.user_id.is_(None),
            ),
        )
        preference = case((ApiKey.user_id == user_id, 0), else_=1)

    return db.session.execute(
        select(ApiKey, LLMProvider.name)
        .join(LLMProvider, ApiKey.provider_id == LLMProvider.id)
        .where(
            ApiKey.is_active.is_(True),
            LLMProvider.name.in_(names),
            ownership,
        )
        .order_by(LLMProvider.name, preference, ApiKey.id.asc())
    ).all()


def _credential_records(
    provider_names: Iterable[str], user_id: int | None
) -> dict[str, tuple[ApiKey, str]]:
    selected: dict[str, tuple[ApiKey, str]] = {}
    if not has_app_context():
        return selected
    for key_record, provider_name in _credential_query(provider_names, user_id):
        source = (
            "user"
            if user_id is not None and key_record.user_id == user_id
            else "global"
        )
        selected.setdefault(str(provider_name), (key_record, source))
    return selected


def _gemini_environment_configured() -> bool:
    """Mirror the legacy Gemini environment fallback without returning its secret."""

    expected_config_module = f"{__package__.split('.', maxsplit=1)[0]}.config"
    try:
        from ..config import GOOGLE_API_KEY
    except ModuleNotFoundError as exc:
        if exc.name != expected_config_module:
            raise
        GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
    return bool(GOOGLE_API_KEY)


def resolve_model_connection(
    model_id: str,
    *,
    user_id: int | None,
) -> ModelConnection:
    """Resolve the same tenant/provider/key selection used by ``LLMService``."""

    normalized = str(model_id or "").strip()
    if not normalized or not has_app_context():
        return ModelConnection(None, None, None, None, None)

    record = _resolve_model_record(normalized, user_id)
    provider_name = _provider_name(normalized, record)
    provider = record.provider if record is not None else None
    if provider is None and provider_name:
        provider = (
            db.session.execute(
                select(LLMProvider)
                .where(
                    LLMProvider.name == provider_name,
                    (
                        LLMProvider.user_id.is_(None)
                        if user_id is None
                        else or_(
                            LLMProvider.user_id == user_id,
                            LLMProvider.user_id.is_(None),
                        )
                    ),
                )
                .order_by(
                    (
                        LLMProvider.id.asc()
                        if user_id is None
                        else case((LLMProvider.user_id == user_id, 0), else_=1)
                    ),
                    LLMProvider.id.asc(),
                )
            )
            .scalars()
            .first()
        )

    selected = _credential_records((provider_name,) if provider_name else (), user_id)
    credential = selected.get(str(provider_name)) if provider_name else None
    return ModelConnection(
        model_record=record,
        provider=provider,
        provider_name=provider_name,
        api_key_record=credential[0] if credential else None,
        credential_source=credential[1] if credential else None,
    )


def _readiness_for_record(
    model_id: str,
    record: LLMModel | None,
    *,
    provider_name: str | None,
    credential_source: str | None,
    required_model_type: str | None,
    require_credential: bool = True,
) -> ModelReadiness:
    normalized_type = str(required_model_type or "").strip().lower() or None
    model_type = (
        str(record.model_type or "").strip().lower() if record is not None else None
    )

    reason = None
    if not model_id:
        reason = "invalid_model_id"
    elif record is None:
        reason = "model_not_configured"
    elif not bool(record.is_active):
        reason = "model_inactive"
    elif normalized_type and model_type != normalized_type:
        reason = "wrong_model_type"
    elif not provider_name:
        reason = "provider_unavailable"
    elif require_credential and not credential_source:
        reason = "credential_unavailable"

    return ModelReadiness(
        model_id=model_id,
        ready=reason is None,
        reason=reason,
        message=(
            "Model is configured and has an active credential."
            if require_credential
            else "Model profile is configured for external execution."
        )
        if reason is None
        else READINESS_MESSAGES[reason],
        provider_name=provider_name,
        model_type=model_type,
        credential_source=credential_source,
    )


def resolve_model_readiness(
    model_id: str,
    *,
    user_id: int | None,
    required_model_type: str | None = None,
) -> ModelReadiness:
    normalized = str(model_id or "").strip()
    connection = resolve_model_connection(normalized, user_id=user_id)
    source = connection.credential_source
    if (
        not source
        and connection.provider_name == "gemini"
        and _gemini_environment_configured()
    ):
        source = "environment"
    return _readiness_for_record(
        normalized,
        connection.model_record,
        provider_name=connection.provider_name,
        credential_source=source,
        required_model_type=required_model_type,
    )


def resolve_model_profile_readiness(
    model_id: str,
    *,
    user_id: int | None,
    required_model_type: str | None = None,
) -> ModelReadiness:
    """Validate a configured model profile without requiring provider credentials.

    This is exclusively for externally executed work. It proves that Carlina
    has an active owner-visible model/provider profile and capability metadata;
    it makes no managed-provider readiness claim.
    """

    normalized = str(model_id or "").strip()
    connection = resolve_model_connection(normalized, user_id=user_id)
    return _readiness_for_record(
        normalized,
        connection.model_record,
        provider_name=connection.provider_name,
        credential_source=connection.credential_source,
        required_model_type=required_model_type,
        require_credential=False,
    )


def resolve_model_readiness_records(
    records: Iterable[LLMModel],
    *,
    user_id: int | None,
    required_model_type: str | None = None,
) -> dict[str, ModelReadiness]:
    """Resolve a model picker without issuing one credential query per model."""

    rows = list(records)
    provider_names = {_provider_name(str(record.model_id), record) for record in rows}
    credentials = _credential_records(
        (name for name in provider_names if name),
        user_id,
    )
    gemini_environment = "gemini" in provider_names and _gemini_environment_configured()
    result: dict[str, ModelReadiness] = {}
    for record in rows:
        model_id = str(record.model_id)
        provider_name = _provider_name(model_id, record)
        credential = credentials.get(str(provider_name)) if provider_name else None
        source = credential[1] if credential else None
        if not source and provider_name == "gemini" and gemini_environment:
            source = "environment"
        result[model_id] = _readiness_for_record(
            model_id,
            record,
            provider_name=provider_name,
            credential_source=source,
            required_model_type=required_model_type,
        )
    return result


def require_model_ready(
    model_id: str,
    *,
    user_id: int | None,
    required_model_type: str | None = None,
) -> ModelReadiness:
    readiness = resolve_model_readiness(
        model_id,
        user_id=user_id,
        required_model_type=required_model_type,
    )
    if not readiness.ready:
        raise ModelNotReadyError(readiness)
    return readiness


def require_model_profile_ready(
    model_id: str,
    *,
    user_id: int | None,
    required_model_type: str | None = None,
) -> ModelReadiness:
    """Require a configured profile for an explicitly external executor."""

    readiness = resolve_model_profile_readiness(
        model_id,
        user_id=user_id,
        required_model_type=required_model_type,
    )
    if not readiness.ready:
        raise ModelNotReadyError(readiness)
    return readiness


__all__ = [
    "ModelConnection",
    "ModelNotReadyError",
    "ModelReadiness",
    "require_model_profile_ready",
    "require_model_ready",
    "resolve_model_connection",
    "resolve_model_readiness",
    "resolve_model_readiness_records",
    "resolve_model_profile_readiness",
]
