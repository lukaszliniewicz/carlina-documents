from __future__ import annotations

import base64
import copy
import json
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from flask import Blueprint, current_app, jsonify, request
from flask_login import current_user, login_required
from sqlalchemy import and_, func, or_
from sqlalchemy.orm.attributes import flag_modified

from iarf_assistant.chat_constants import SOURCE_COVERAGE_LEDGER_ROLE
from iarf_assistant.core.context_fabric import (
    read_evidence,
    recover_incomplete_turn_history,
)
from iarf_assistant.core.runtime_state import merge_approved_scope_expansions
from iarf_assistant.extensions import db
from iarf_assistant.html_sanitizer import sanitize_html_fragment
from iarf_assistant.markdown import md_parser
from iarf_assistant.models import (
    ChatEvidence,
    ChatMessage,
    ChatSession,
    LLMCost,
    RuntimeInteraction,
)


frontend_chat_blueprint = Blueprint("frontend_chat_api", __name__)
CHAT_HISTORY_DEFAULT_LIMIT = 60
CHAT_HISTORY_MAX_LIMIT = 100
CHAT_SESSION_DEFAULT_LIMIT = 24
CHAT_SESSION_MAX_LIMIT = 80
DEVELOPER_VALUE_LIMIT = 20_000


def stream_chat_response():
    """Load the comparatively heavy chat execution engine on first use."""
    from iarf_assistant.chat_orchestrator import stream_chat_response as execute_chat

    return execute_chat()


def _owned_chat_session(session_id: str) -> ChatSession | None:
    chat_session = db.session.get(ChatSession, session_id)
    if not chat_session or int(chat_session.user_id) != int(current_user.id):
        return None
    return chat_session


def _bounded_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _bounded_value(
    value: Any,
    *,
    max_string_chars: int = DEVELOPER_VALUE_LIMIT,
    max_items: int = 60,
    depth: int = 0,
) -> Any:
    if depth >= 8:
        return "[Nested value omitted]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= max_string_chars:
            return value
        return f"{value[:max_string_chars]}\n… [truncated]"
    if isinstance(value, dict):
        bounded: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= max_items:
                bounded["_truncated"] = (
                    f"{len(value) - max_items} additional field(s) omitted"
                )
                break
            if str(key) == "raw_content":
                continue
            bounded[str(key)] = _bounded_value(
                item,
                max_string_chars=max_string_chars,
                max_items=max_items,
                depth=depth + 1,
            )
        return bounded
    if isinstance(value, (list, tuple)):
        bounded_items = [
            _bounded_value(
                item,
                max_string_chars=max_string_chars,
                max_items=max_items,
                depth=depth + 1,
            )
            for item in value[:max_items]
        ]
        if len(value) > max_items:
            bounded_items.append(
                f"[{len(value) - max_items} additional item(s) omitted]"
            )
        return bounded_items
    return _bounded_value(
        str(value),
        max_string_chars=max_string_chars,
        max_items=max_items,
        depth=depth + 1,
    )


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(part.get("text") or "")
        for part in content
        if isinstance(part, dict) and part.get("type") == "text"
    ).strip()


def _safe_owned_url(value: Any) -> str | None:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        return None
    parsed = urlsplit(raw)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        return None
    if (
        parsed.path.startswith("/uploads/")
        and not parsed.path.startswith(f"/uploads/{current_user.id}/")
    ):
        return None
    allowed_prefixes = (
        "/uploads/",
        "/api/runtime/artifacts/",
        "/addons/",
    )
    if not parsed.path.startswith(allowed_prefixes):
        return None
    return parsed.path


def _attachment_payload(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    raw_path = (
        value.get("filePath")
        or value.get("file_path")
        or value.get("url")
        or value.get("path")
        or ""
    )
    path_name = PurePosixPath(str(raw_path).replace("\\", "/")).name
    label = str(
        value.get("fileName")
        or value.get("filename")
        or value.get("name")
        or value.get("label")
        or path_name
        or "Attachment"
    )[:255]
    strategy = str(value.get("pass_as") or value.get("strategy") or "")
    return {
        "id": str(value.get("id") or raw_path or label),
        "name": label,
        "url": _safe_owned_url(raw_path),
        "size": (
            int(value.get("fileSize") or value.get("size") or 0)
            if str(value.get("fileSize") or value.get("size") or "").isdigit()
            else None
        ),
        "media_type": str(
            value.get("mimeType")
            or value.get("mime_type")
            or (
                "image"
                if value.get("isImage")
                else "audio" if value.get("isAudio") else "file"
            )
        )[:120],
        "strategy": strategy[:120],
        "source": str(value.get("source") or "attachment")[:120],
    }


def _developer_tool_payload(message: dict[str, Any]) -> dict[str, Any]:
    raw_content = message.get("content")
    parsed_content: Any = raw_content
    if isinstance(raw_content, str):
        try:
            parsed_content = json.loads(raw_content)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed_content = raw_content
    if isinstance(parsed_content, dict) and "_ui_render" in parsed_content:
        parsed_content = {
            key: value
            for key, value in parsed_content.items()
            if key != "_ui_render"
        }
    return {
        "tool_name": str(message.get("name") or "Tool"),
        "tool_call_id": str(message.get("tool_call_id") or ""),
        "result": _bounded_value(parsed_content),
        "context": _bounded_value(message.get("_context_optimization") or {}),
    }


def _serialize_message(
    row: ChatMessage,
    *,
    sequence: int,
    debug: bool,
) -> dict[str, Any] | None:
    message = row.message if isinstance(row.message, dict) else {}
    role = str(message.get("role") or "")
    base = {
        "id": f"message:{row.id}",
        "message_id": row.id,
        "sequence": sequence,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }

    if role == SOURCE_COVERAGE_LEDGER_ROLE:
        return None
    if role == "user":
        attachments = [
            attachment
            for attachment in (
                _attachment_payload(value)
                for value in (
                    message.get("files")
                    if isinstance(message.get("files"), list)
                    else []
                )
            )
            if attachment
        ]
        return {
            **base,
            "role": "user",
            "text": _message_text(message),
            "attachments": attachments,
        }
    if role == "assistant":
        markdown = _message_text(message)
        if not markdown and not (debug and message.get("tool_calls")):
            return None
        payload: dict[str, Any] = {
            **base,
            "role": "assistant",
            "text": markdown,
            "html": (
                sanitize_html_fragment(md_parser(markdown))
                if markdown
                else ""
            ),
            "tts_audio_url": _safe_owned_url(row.tts_audio_path),
        }
        if debug and message.get("tool_calls"):
            payload["developer"] = {
                "tool_calls": _bounded_value(message.get("tool_calls"))
            }
        return payload
    if role == "agent_event":
        event = message.get("event")
        if not isinstance(event, dict) or not event.get("kind"):
            return None
        bounded_event = _bounded_value(event)
        if not bounded_event.get("id"):
            bounded_event["id"] = f"event:{row.id}"
        return {
            **base,
            "id": str(bounded_event["id"]),
            "role": "event",
            "event": bounded_event,
        }
    if role == "debug_prompt" and debug:
        return {
            **base,
            "role": "developer",
            "title": "Initial model request",
            "developer": _bounded_value(
                {
                    "messages": message.get("initial_request_messages")
                    or [
                        {
                            "role": "system",
                            "content": message.get("system_prompt"),
                        },
                        message.get("user_message"),
                    ],
                    "tools": message.get("initial_request_tools") or [],
                    "tool_names": message.get("tool_names") or [],
                    "model": message.get("initial_request_model"),
                    "assistant_mode": message.get("assistant_mode"),
                    "selected_capability_ids": (
                        message.get("selected_capability_ids") or []
                    ),
                    "enable_delegation": message.get("enable_delegation"),
                }
            ),
        }
    if role == "tool" and debug:
        return {
            **base,
            "role": "developer",
            "title": f"Tool result · {message.get('name') or 'Tool'}",
            "developer": _developer_tool_payload(message),
        }
    if debug and role:
        return {
            **base,
            "role": "developer",
            "title": role.replace("_", " ").title(),
            "developer": _bounded_value(message),
        }
    return None


def _message_positions(
    session_id: str,
    message_ids: list[int],
) -> dict[int, int]:
    if not message_ids:
        return {}
    ranked = (
        db.session.query(
            ChatMessage.id.label("message_id"),
            (
                func.row_number().over(
                    order_by=(
                        ChatMessage.created_at.asc(),
                        ChatMessage.id.asc(),
                    )
                )
                - 1
            ).label("sequence"),
        )
        .filter(ChatMessage.session_id == session_id)
        .subquery()
    )
    return {
        int(message_id): int(sequence)
        for message_id, sequence in (
            db.session.query(ranked.c.message_id, ranked.c.sequence)
            .filter(ranked.c.message_id.in_(message_ids))
            .all()
        )
    }


def _latest_execution_scope(session_id: str) -> dict[str, Any]:
    recent = (
        ChatMessage.query.filter_by(session_id=session_id)
        .order_by(ChatMessage.id.desc())
        .limit(120)
        .all()
    )
    for row in recent:
        message = row.message if isinstance(row.message, dict) else {}
        execution_scope = message.get("execution_scope")
        if isinstance(execution_scope, dict):
            return execution_scope
    return {}


def _assistant_scope_payload(chat_session: ChatSession) -> dict[str, Any]:
    manager = getattr(current_app, "addon_manager", None)
    if manager:
        selection = manager.normalize_session_assistant_selection(
            getattr(chat_session, "active_capability_ids", None),
            chat_session.active_plugins or [],
        )
        capability_ids = selection["active_capability_ids"]
    else:
        capability_ids = (
            chat_session.active_capability_ids
            if chat_session.active_capability_ids is not None
            else chat_session.active_plugins
        ) or []

    latest_scope = _latest_execution_scope(chat_session.id)
    latest_mode = str(
        latest_scope.get("assistant_mode")
        or latest_scope.get("mode")
        or ""
    ).lower()
    base = {
        "mode": (
            "default"
            if latest_mode
            in {"default", "autonomous", "autonomous_discovery"}
            else "direct" if capability_ids else "default"
        ),
        "capability_ids": list(capability_ids),
    }
    expanded, approvals = merge_approved_scope_expansions(
        base,
        chat_session_id=chat_session.id,
        requester_user_id=current_user.id,
    )
    if approvals:
        expanded["mode"] = "direct"
    if manager:
        expanded = manager.normalize_assistant_selection(expanded)
    return {
        **_bounded_value(expanded),
        "approved_scope_expansions": _bounded_value(approvals),
    }


def _session_stats(session_id: str) -> dict[str, Any]:
    row = (
        db.session.query(
            func.coalesce(func.sum(LLMCost.cost), 0.0),
            func.coalesce(func.sum(LLMCost.prompt_tokens), 0),
            func.coalesce(func.sum(LLMCost.completion_tokens), 0),
            func.coalesce(func.sum(LLMCost.total_tokens), 0),
        )
        .filter(LLMCost.session_id == session_id)
        .one()
    )
    return {
        "cost": float(row[0] or 0.0),
        "tokens": {
            "prompt": int(row[1] or 0),
            "completion": int(row[2] or 0),
            "total": int(row[3] or 0),
        },
    }


def _session_payload(
    chat_session: ChatSession,
    *,
    message_count: int,
    newest_message_id: int | None,
) -> dict[str, Any]:
    interactions = (
        chat_session.interactions.order_by(None)
        .order_by(RuntimeInteraction.created_at.desc())
        .limit(100)
        .all()
    )
    return {
        "id": chat_session.id,
        "title": str(chat_session.title or "New conversation"),
        "created_at": (
            chat_session.created_at.isoformat()
            if chat_session.created_at
            else None
        ),
        "updated_at": (
            chat_session.updated_at.isoformat()
            if chat_session.updated_at
            else None
        ),
        "message_count": message_count,
        "newest_message_id": newest_message_id,
        "revision": (
            f"{newest_message_id or 0}:"
            f"{chat_session.updated_at.isoformat() if chat_session.updated_at else ''}"
        ),
        "assistant_scope": _assistant_scope_payload(chat_session),
        "interactions": [
            _bounded_value(interaction.to_dict())
            for interaction in reversed(interactions)
        ],
        "stats": _session_stats(chat_session.id),
    }


def _fallback_session_title(chat_session: ChatSession) -> str:
    if chat_session.title:
        return str(chat_session.title)
    rows = (
        ChatMessage.query.filter_by(session_id=chat_session.id)
        .order_by(ChatMessage.id.asc())
        .limit(30)
        .all()
    )
    for row in rows:
        message = row.message if isinstance(row.message, dict) else {}
        if message.get("role") != "user":
            continue
        value = _message_text(message).strip()
        if value:
            return value[:70] + ("…" if len(value) > 70 else "")
    evidence = (
        ChatEvidence.query.filter_by(
            session_id=chat_session.id,
            user_id=chat_session.user_id,
            kind="user_message",
            retention_state="retained",
        )
        .order_by(ChatEvidence.created_at.asc())
        .first()
    )
    if evidence is not None and evidence.deleted_at is None:
        try:
            value = str(
                read_evidence(
                    evidence.id,
                    user_id=chat_session.user_id,
                    session_id=chat_session.id,
                    max_chars=71,
                ).get("content")
                or ""
            ).strip()
            if value:
                return value[:70] + ("…" if len(value) > 70 else "")
        except Exception:
            current_app.logger.warning(
                "Could not derive chat title from evidence session_id=%s",
                chat_session.id,
                exc_info=True,
            )
    return "New conversation"


def _encode_session_cursor(chat_session: ChatSession) -> str:
    payload = json.dumps(
        {
            "updated_at": (
                chat_session.updated_at.isoformat()
                if chat_session.updated_at
                else ""
            ),
            "id": chat_session.id,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_session_cursor(value: str) -> tuple[datetime, str]:
    padded = value + "=" * (-len(value) % 4)
    payload = json.loads(
        base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    )
    updated_at = datetime.fromisoformat(str(payload["updated_at"]))
    session_id = str(payload["id"])
    if not session_id:
        raise ValueError("Missing session id.")
    return updated_at, session_id


@frontend_chat_blueprint.route("/api/frontend/chats", methods=["GET"])
@login_required
def list_frontend_chats():
    limit = _bounded_int(
        request.args.get("limit"),
        default=CHAT_SESSION_DEFAULT_LIMIT,
        minimum=1,
        maximum=CHAT_SESSION_MAX_LIMIT,
    )
    query = ChatSession.query.filter_by(user_id=current_user.id)
    raw_cursor = str(request.args.get("cursor") or "").strip()
    if raw_cursor:
        try:
            updated_at, session_id = _decode_session_cursor(raw_cursor)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return jsonify({"error": "The chat cursor is invalid."}), 400
        query = query.filter(
            or_(
                ChatSession.updated_at < updated_at,
                and_(
                    ChatSession.updated_at == updated_at,
                    ChatSession.id < session_id,
                ),
            )
        )
    rows = (
        query.order_by(ChatSession.updated_at.desc(), ChatSession.id.desc())
        .limit(limit + 1)
        .all()
    )
    has_more = len(rows) > limit
    visible_rows = rows[:limit]
    ids = [row.id for row in visible_rows]
    counts = {
        session_id: int(count)
        for session_id, count in (
            db.session.query(ChatMessage.session_id, func.count(ChatMessage.id))
            .filter(ChatMessage.session_id.in_(ids))
            .group_by(ChatMessage.session_id)
            .all()
            if ids
            else []
        )
    }
    payload = {
        "items": [
            {
                "id": row.id,
                "title": _fallback_session_title(row),
                "created_at": (
                    row.created_at.isoformat() if row.created_at else None
                ),
                "updated_at": (
                    row.updated_at.isoformat() if row.updated_at else None
                ),
                "message_count": counts.get(row.id, 0),
            }
            for row in visible_rows
        ],
        "next_cursor": (
            _encode_session_cursor(visible_rows[-1])
            if has_more and visible_rows
            else None
        ),
    }
    response = jsonify(payload)
    response.headers["Cache-Control"] = "private, no-cache"
    return response


@frontend_chat_blueprint.route(
    "/api/frontend/chats/<session_id>/history",
    methods=["GET"],
)
@login_required
def get_frontend_chat_history(session_id: str):
    chat_session = _owned_chat_session(session_id)
    if chat_session is None:
        return jsonify({"error": "Chat session not found."}), 404

    try:
        recover_incomplete_turn_history(
            session_id=session_id,
            user_id=int(current_user.id),
            commit=True,
        )
    except Exception:
        db.session.rollback()
        current_app.logger.warning(
            "Could not recover incomplete chat history session_id=%s",
            session_id,
            exc_info=True,
        )

    limit = _bounded_int(
        request.args.get("limit"),
        default=CHAT_HISTORY_DEFAULT_LIMIT,
        minimum=1,
        maximum=CHAT_HISTORY_MAX_LIMIT,
    )
    debug = str(request.args.get("debug") or "").lower() in {
        "1",
        "true",
        "yes",
    }
    raw_before = str(request.args.get("before") or "").strip()
    try:
        before_id = int(raw_before) if raw_before else None
    except ValueError:
        return jsonify({"error": "The history cursor is invalid."}), 400

    message_count, newest_message_id = (
        db.session.query(
            func.count(ChatMessage.id),
            func.max(ChatMessage.id),
        )
        .filter(ChatMessage.session_id == session_id)
        .one()
    )
    rendered: list[dict[str, Any]] = []
    scanned_oldest_id: int | None = None
    scan_before_id = before_id
    chunk_size = min(max(limit * 2, 40), 200)
    exhausted = False
    while len(rendered) < limit and not exhausted:
        query = ChatMessage.query.filter_by(session_id=session_id)
        if scan_before_id is not None:
            query = query.filter(ChatMessage.id < scan_before_id)
        rows = (
            query.order_by(ChatMessage.id.desc())
            .limit(chunk_size)
            .all()
        )
        if not rows:
            exhausted = True
            break
        for row in rows:
            scanned_oldest_id = row.id
            item = _serialize_message(
                row,
                sequence=0,
                debug=debug,
            )
            if item is not None:
                rendered.append(item)
                if len(rendered) >= limit:
                    break
        scan_before_id = rows[-1].id
        if len(rows) < chunk_size:
            exhausted = True

    has_more = bool(
        scanned_oldest_id
        and db.session.query(ChatMessage.id)
        .filter(
            ChatMessage.session_id == session_id,
            ChatMessage.id < scanned_oldest_id,
        )
        .first()
    )
    positions = _message_positions(
        session_id,
        [int(item["message_id"]) for item in rendered],
    )
    for item in rendered:
        item["sequence"] = positions.get(int(item["message_id"]), 0)
    ordered_items = sorted(
        rendered,
        key=lambda item: (
            int(item.get("sequence") or 0),
            int(item.get("message_id") or 0),
        ),
    )
    response = jsonify(
        {
            "session": _session_payload(
                chat_session,
                message_count=int(message_count or 0),
                newest_message_id=(
                    int(newest_message_id)
                    if newest_message_id is not None
                    else None
                ),
            ),
            "items": ordered_items,
            "next_cursor": str(scanned_oldest_id) if has_more else None,
            "has_more": has_more,
            "debug": debug,
        }
    )
    response.headers["Cache-Control"] = "private, no-cache"
    return response


@frontend_chat_blueprint.route(
    "/api/frontend/chats/<session_id>",
    methods=["PATCH"],
)
@login_required
def update_frontend_chat(session_id: str):
    chat_session = _owned_chat_session(session_id)
    if chat_session is None:
        return jsonify({"error": "Chat session not found."}), 404
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "A JSON object is required."}), 400
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        return jsonify({"error": "A non-empty title is required."}), 400
    if len(title.strip()) > 255:
        return jsonify({"error": "The title must be 255 characters or fewer."}), 400
    chat_session.title = title.strip()
    db.session.commit()
    return jsonify(
        {
            "success": True,
            "chat": {
                "id": chat_session.id,
                "title": chat_session.title,
                "updated_at": (
                    chat_session.updated_at.isoformat()
                    if chat_session.updated_at
                    else None
                ),
            },
        }
    )


@frontend_chat_blueprint.route(
    "/api/frontend/chats/<session_id>",
    methods=["DELETE"],
)
@login_required
def delete_frontend_chat(session_id: str):
    chat_session = _owned_chat_session(session_id)
    if chat_session is None:
        return jsonify({"error": "Chat session not found."}), 404
    db.session.delete(chat_session)
    db.session.commit()
    return "", 204


@frontend_chat_blueprint.route(
    "/api/frontend/chats/<session_id>/export",
    methods=["GET"],
)
@login_required
def export_frontend_chat(session_id: str):
    chat_session = _owned_chat_session(session_id)
    if chat_session is None:
        return jsonify({"error": "Chat session not found."}), 404
    sections: list[str] = []
    character_count = 0
    truncated = False
    rows = (
        ChatMessage.query.filter_by(session_id=session_id)
        .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        .yield_per(100)
    )
    for row in rows:
        message = row.message if isinstance(row.message, dict) else {}
        role = str(message.get("role") or "")
        if role not in {"user", "assistant"} or message.get("tool_calls"):
            continue
        text = _message_text(message).strip()
        if not text:
            continue
        section = f"{'User' if role == 'user' else 'Assistant'}:\n{text}"
        if character_count + len(section) > 2_000_000:
            truncated = True
            break
        sections.append(section)
        character_count += len(section)
    response = jsonify(
        {
            "title": _fallback_session_title(chat_session),
            "text": "\n\n".join(sections),
            "truncated": truncated,
        }
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response


def _previous_chat_message(row: ChatMessage) -> ChatMessage | None:
    return (
        ChatMessage.query.filter(
            ChatMessage.session_id == row.session_id,
            or_(
                ChatMessage.created_at < row.created_at,
                and_(
                    ChatMessage.created_at == row.created_at,
                    ChatMessage.id < row.id,
                ),
            ),
        )
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .first()
    )


@frontend_chat_blueprint.route(
    "/api/frontend/chats/<session_id>/messages/<int:message_id>",
    methods=["PATCH"],
)
@login_required
def update_frontend_chat_message(session_id: str, message_id: int):
    chat_session = _owned_chat_session(session_id)
    row = db.session.get(ChatMessage, message_id)
    if (
        chat_session is None
        or row is None
        or row.session_id != chat_session.id
    ):
        return jsonify({"error": "Chat message not found."}), 404
    data = request.get_json(silent=True)
    text = data.get("text") if isinstance(data, dict) else None
    if not isinstance(text, str):
        return jsonify({"error": "Message text is required."}), 400
    if len(text) > 200_000:
        return jsonify({"error": "Message text is too long."}), 400
    message = copy.deepcopy(row.message) if isinstance(row.message, dict) else {}
    if message.get("role") != "user":
        return jsonify({"error": "Only user messages can be edited."}), 400
    content = message.get("content")
    if isinstance(content, list):
        text_part = next(
            (
                part
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ),
            None,
        )
        if text_part is None:
            content.insert(0, {"type": "text", "text": text})
        else:
            text_part["text"] = text
    else:
        message["content"] = text
    previous = _previous_chat_message(row)
    if (
        previous is not None
        and isinstance(previous.message, dict)
        and previous.message.get("role") == "debug_prompt"
    ):
        db.session.delete(previous)
    row.message = message
    row.tts_audio_path = None
    flag_modified(row, "message")
    db.session.commit()
    return jsonify({"success": True})


@frontend_chat_blueprint.route(
    "/api/frontend/chats/<session_id>/messages/<int:message_id>",
    methods=["DELETE"],
)
@login_required
def delete_frontend_chat_message(session_id: str, message_id: int):
    chat_session = _owned_chat_session(session_id)
    row = db.session.get(ChatMessage, message_id)
    if (
        chat_session is None
        or row is None
        or row.session_id != chat_session.id
    ):
        return jsonify({"error": "Chat message not found."}), 404
    message = row.message if isinstance(row.message, dict) else {}
    previous = _previous_chat_message(row)
    if (
        message.get("role") == "user"
        and previous is not None
        and isinstance(previous.message, dict)
        and previous.message.get("role") == "debug_prompt"
    ):
        db.session.delete(previous)
    db.session.delete(row)
    db.session.commit()
    return "", 204


@frontend_chat_blueprint.route(
    "/api/frontend/chat",
    methods=["POST"],
)
@login_required
def stream_frontend_chat():
    """Expose the existing authoritative chat stream under the frontend API."""
    return stream_chat_response()


def register_frontend_chat_blueprint(app) -> None:
    app.register_blueprint(frontend_chat_blueprint)


__all__ = [
    "frontend_chat_blueprint",
    "register_frontend_chat_blueprint",
]
