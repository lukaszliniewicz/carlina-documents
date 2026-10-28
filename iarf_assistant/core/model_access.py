"""Tenant-safe model/provider row resolution shared by planning and execution."""

from __future__ import annotations

from collections.abc import Collection
from itertools import islice
from typing import Any, cast

from flask import has_app_context
from sqlalchemy import and_, case, or_, select
from sqlalchemy.orm import contains_eager
from sqlalchemy.orm.attributes import InstrumentedAttribute

from iarf_assistant.extensions import db
from iarf_assistant.models import LLMModel, LLMProvider

MAX_EFFECTIVE_MODEL_IDS = 64
MAX_MODEL_ID_LENGTH = 255


def _normalized_model_ids(model_ids: Collection[str]) -> tuple[str, ...]:
    if isinstance(model_ids, str):
        return ()
    values: list[str] = []
    seen: set[str] = set()
    for model_id in islice(model_ids, MAX_EFFECTIVE_MODEL_IDS):
        if not isinstance(model_id, str):
            continue
        normalized = model_id.strip()
        if (
            not normalized
            or len(normalized) > MAX_MODEL_ID_LENGTH
            or normalized in seen
        ):
            continue
        seen.add(normalized)
        values.append(normalized)
    return tuple(values)


def _ownership_predicate(user_id: int | None) -> Any:
    if user_id is None:
        return and_(
            LLMModel.user_id.is_(None),
            LLMProvider.user_id.is_(None),
        )
    return or_(
        and_(
            LLMModel.user_id == user_id,
            or_(
                LLMProvider.user_id == user_id,
                LLMProvider.user_id.is_(None),
            ),
        ),
        and_(
            LLMModel.user_id.is_(None),
            LLMProvider.user_id.is_(None),
        ),
    )


def _preference_order(user_id: int | None) -> Any:
    if user_id is None:
        return LLMModel.id.asc()
    return case(
        (LLMModel.user_id == user_id, 0),
        else_=1,
    )


def _base_statement(user_id: int | None) -> Any:
    return (
        select(LLMModel)
        .join(LLMProvider, LLMModel.provider_id == LLMProvider.id)
        .options(contains_eager(cast(InstrumentedAttribute[Any], LLMModel.provider)))
        .where(_ownership_predicate(user_id))
        .order_by(_preference_order(user_id), LLMModel.id.asc())
    )


def resolve_effective_model_records(
    model_ids: Collection[str],
    *,
    user_id: int | None,
) -> dict[str, LLMModel]:
    """Resolve one permitted, provider-eager row per exact model ID."""

    requested = _normalized_model_ids(model_ids)
    if not requested or not has_app_context():
        return {}
    rows = db.session.execute(
        _base_statement(user_id).where(LLMModel.model_id.in_(requested))
    ).scalars()
    selected: dict[str, LLMModel] = {}
    for record in rows:
        selected.setdefault(str(record.model_id), record)
    return selected


def resolve_effective_model_record(
    model_id: str,
    *,
    user_id: int | None,
) -> LLMModel | None:
    """Resolve one exact model with user-over-global precedence."""

    if not isinstance(model_id, str):
        return None
    normalized = model_id.strip()
    return resolve_effective_model_records(
        (normalized,),
        user_id=user_id,
    ).get(normalized)


def resolve_effective_model_record_by_suffix(
    model_name: str,
    *,
    user_id: int | None,
) -> LLMModel | None:
    """Resolve an unqualified model name without crossing tenant ownership."""

    if (
        not isinstance(model_name, str)
        or not (normalized := model_name.strip())
        or len(normalized) > MAX_MODEL_ID_LENGTH
        or not has_app_context()
    ):
        return None
    statement = _base_statement(user_id).where(
        or_(
            LLMModel.model_id == normalized,
            LLMModel.model_id.endswith(f"/{normalized}", autoescape=True),
        )
    )
    return db.session.execute(statement).scalars().first()


__all__ = [
    "MAX_EFFECTIVE_MODEL_IDS",
    "MAX_MODEL_ID_LENGTH",
    "resolve_effective_model_record",
    "resolve_effective_model_record_by_suffix",
    "resolve_effective_model_records",
]
