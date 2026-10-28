"""Bounded database projections for chat-session history responses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, func, or_, select

from iarf_assistant.extensions import db
from iarf_assistant.models import (
    ChatMessage,
    ChatSession,
    LLMCost,
    RuntimeApprovalRequest,
    RuntimeInteraction,
)


@dataclass(frozen=True, slots=True)
class ChatSessionProjection:
    """Session, aggregate usage, and preloaded scope approvals."""

    session: ChatSession
    total_cost: float
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    approval_records: tuple[RuntimeApprovalRequest, ...]


def _cost_totals(session_id: str):
    return (
        select(
            LLMCost.session_id.label("session_id"),
            func.sum(LLMCost.cost).label("total_cost"),
            func.sum(LLMCost.prompt_tokens).label("prompt_tokens"),
            func.sum(LLMCost.completion_tokens).label("completion_tokens"),
            func.sum(LLMCost.total_tokens).label("total_tokens"),
        )
        .where(LLMCost.session_id == session_id)
        .group_by(LLMCost.session_id)
        .subquery()
    )


def _approval_matches_session(
    record: RuntimeApprovalRequest,
    session_id: str,
) -> bool:
    if record.chat_session_id:
        return str(record.chat_session_id) == session_id
    preview = record.preview_payload if isinstance(record.preview_payload, dict) else {}
    return str(preview.get("chat_session_id") or "") == session_id


def load_chat_session_projection(
    session_id: str,
    *,
    user_id: int,
) -> ChatSessionProjection | None:
    """Load one visible session, usage totals, and scope approvals in one query."""

    normalized_session_id = str(session_id)
    costs = _cost_totals(normalized_session_id)
    approval_join = and_(
        RuntimeApprovalRequest.requester_user_id == int(user_id),
        RuntimeApprovalRequest.subject_type == "scope_expansion",
        RuntimeApprovalRequest.status == "approved",
        or_(
            RuntimeApprovalRequest.chat_session_id == ChatSession.id,
            RuntimeApprovalRequest.chat_session_id.is_(None),
        ),
    )
    statement = (
        select(
            ChatSession,
            func.coalesce(costs.c.total_cost, 0.0),
            func.coalesce(costs.c.prompt_tokens, 0),
            func.coalesce(costs.c.completion_tokens, 0),
            func.coalesce(costs.c.total_tokens, 0),
            RuntimeApprovalRequest,
        )
        .outerjoin(costs, costs.c.session_id == ChatSession.id)
        .outerjoin(RuntimeApprovalRequest, approval_join)
        .where(
            ChatSession.id == normalized_session_id,
            ChatSession.user_id == int(user_id),
        )
        .order_by(RuntimeApprovalRequest.created_at.asc())
        .limit(200)
    )
    rows = db.session.execute(statement).all()
    if not rows:
        return None

    session, total_cost, prompt, completion, total, _approval = rows[0]
    approvals: list[RuntimeApprovalRequest] = []
    seen_ids: set[str] = set()
    for *_totals, approval in rows:
        if not approval or approval.id in seen_ids:
            continue
        if not _approval_matches_session(approval, normalized_session_id):
            continue
        seen_ids.add(approval.id)
        approvals.append(approval)
    return ChatSessionProjection(
        session=session,
        total_cost=float(total_cost or 0.0),
        prompt_tokens=int(prompt or 0),
        completion_tokens=int(completion or 0),
        total_tokens=int(total or 0),
        approval_records=tuple(approvals),
    )


def load_chat_messages(session_id: str) -> list[ChatMessage]:
    """Load a stable, complete message history with one query."""

    return list(
        db.session.scalars(
            select(ChatMessage)
            .where(ChatMessage.session_id == str(session_id))
            .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        )
    )


def load_chat_interactions(session_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
    """Load and serialize the bounded interaction history with one query."""

    rows = db.session.scalars(
        select(RuntimeInteraction)
        .where(RuntimeInteraction.chat_session_id == str(session_id))
        .order_by(RuntimeInteraction.created_at.asc(), RuntimeInteraction.id.asc())
        .limit(max(1, min(int(limit), 1_000)))
    )
    return [row.to_dict() for row in rows]


__all__ = [
    "ChatSessionProjection",
    "load_chat_interactions",
    "load_chat_messages",
    "load_chat_session_projection",
]
