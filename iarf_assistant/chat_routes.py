import copy
import html
import json
import logging

from flask import Blueprint, current_app, jsonify, request
from flask_login import current_user, login_required
from sqlalchemy.orm.attributes import flag_modified

from iarf_assistant.chat_context_store import read_tool_result_artifact_raw_content
from iarf_assistant.chat_execution_helpers import (
    apply_tool_result_retention,
    find_tool_result_message_by_ref,
    read_context_result_from_message,
    search_context_result_in_message,
)
from iarf_assistant.chat_constants import SOURCE_COVERAGE_LEDGER_ROLE
from iarf_assistant.core.agent_events import (
    agent_event_chat_message,
    interaction_state_event,
)
from iarf_assistant.core.chat_sessions.projections import (
    load_chat_interactions,
    load_chat_messages,
    load_chat_session_projection,
)
from iarf_assistant.core.runtime_interactions import (
    INTERACTION_STATUSES,
    get_runtime_interaction_for_user,
    resolve_runtime_interaction,
)
from iarf_assistant.core.runtime_state import merge_approved_scope_expansions
from iarf_assistant.core.settings_cache import get_settings_snapshot
from iarf_assistant.extensions import db
from iarf_assistant.html_sanitizer import sanitize_html_fragment
from iarf_assistant.markdown import md_parser
from iarf_assistant.models import ChatMessage, ChatSession, RuntimeInteraction


chat_blueprint = Blueprint('chat_api', __name__)


def LLMService(*args, **kwargs):
    """Load model execution only for endpoints that invoke it."""
    from iarf_assistant.llm_service import LLMService as service

    return service(*args, **kwargs)


def stream_chat_response():
    """Load the comparatively heavy chat execution engine on first use."""
    from iarf_assistant.chat_orchestrator import stream_chat_response as execute_chat

    return execute_chat()


def _working_memory_payload(raw_memory):
    raw_memory = raw_memory or ""
    parsed_memory = {}
    is_json = True

    if raw_memory.strip():
        try:
            parsed_memory = json.loads(raw_memory)
        except json.JSONDecodeError:
            parsed_memory = None
            is_json = False

    return {
        "raw": raw_memory,
        "parsed": parsed_memory,
        "is_json": is_json,
        "is_empty": not raw_memory.strip() or (is_json and parsed_memory in ({}, [], None)),
    }


def _chat_session_for_current_user(session_id):
    chat_session = db.session.get(ChatSession, session_id)
    if not chat_session or chat_session.user_id != current_user.id:
        return None, (jsonify({"error": "Chat session not found."}), 404)
    return chat_session, None


def _context_result_message_for_session(session_id, context_ref_id):
    ref_id = str(context_ref_id or "").strip()
    if not ref_id:
        return None, None
    for msg_wrapper in ChatMessage.query.filter_by(session_id=session_id).order_by(ChatMessage.created_at.asc()).all():
        payload = msg_wrapper.message if isinstance(msg_wrapper.message, dict) else {}
        if find_tool_result_message_by_ref([payload], ref_id):
            return payload, msg_wrapper
    return None, None


def _raw_context_loader(meta):
    if not isinstance(meta, dict) or not meta.get("raw_content_artifact_id"):
        return None
    return read_tool_result_artifact_raw_content(
        meta.get("raw_content_artifact_id"),
        instance_path=current_app.instance_path,
        owner_user_id=current_user.id,
    )


def _latest_execution_scope_payload(messages):
    for message_or_wrapper in reversed(messages or []):
        message = getattr(message_or_wrapper, "message", message_or_wrapper)
        payload = message if isinstance(message, dict) else {}
        scope = payload.get("execution_scope")
        if isinstance(scope, dict) and scope.get("mode"):
            return scope
    return None


def _normalize_compression_mode(raw_mode):
    mode = str(raw_mode or "").strip().lower()
    if mode in {"", "default", "sumtools", "tools", "toolsum", "tools-summary"}:
        return "sumtools"
    if mode in {"scrubtools", "scrub_tools"}:
        return "scrubtools"
    if mode in {"sum", "sumall", "all"}:
        return "sumall"
    return None


def _plain_message_content(message):
    if not isinstance(message, dict):
        return ""
    content = message.get("content", "")
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ).strip()
    return str(content or "").strip()


def _json_preview(value, max_chars=4000):
    try:
        text = json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)
    except Exception:
        text = str(value)
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 18)].rstrip() + "\n... [truncated]"


def _safe_context_meta_for_summary(message):
    if not isinstance(message, dict):
        return {}
    meta = message.get("_context_optimization")
    if not isinstance(meta, dict):
        return {}
    return {
        key: value
        for key, value in meta.items()
        if key != "raw_content"
    }


def _tool_traffic_block(message, *, index):
    if not isinstance(message, dict):
        return ""
    role = message.get("role")
    if role == "tool":
        meta = _safe_context_meta_for_summary(message)
        lines = [
            f"TOOL RESULT {index}: {message.get('name') or 'unknown_tool'}",
            f"tool_call_id: {message.get('tool_call_id') or ''}",
        ]
        if meta:
            lines.append(f"context_meta: {_json_preview(meta, max_chars=1800)}")
        lines.append(f"content: {_json_preview(message.get('content') or '', max_chars=4000)}")
        return "\n".join(lines)
    if role == "assistant" and message.get("tool_calls"):
        tool_calls = []
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function") or {}
            tool_calls.append({
                "id": call.get("id"),
                "name": function.get("name"),
                "arguments": function.get("arguments"),
            })
        content = _plain_message_content(message)
        lines = [
            f"ASSISTANT TOOL REQUEST {index}:",
            _json_preview(tool_calls, max_chars=5000),
        ]
        if content:
            lines.append(f"assistant_process_note: {content[:1200]}")
        return "\n".join(lines)
    if role == "agent_event":
        event = message.get("event") if isinstance(message.get("event"), dict) else {}
        if not event:
            return ""
        return "\n".join([
            f"AGENT EVENT {index}: {event.get('kind') or ''}",
            f"title: {event.get('title') or ''}",
            f"summary: {event.get('summary') or ''}",
            f"status: {event.get('status') or ''}",
        ])
    if role == SOURCE_COVERAGE_LEDGER_ROLE:
        coverage = message.get("coverage") if isinstance(message.get("coverage"), dict) else {}
        return f"SOURCE COVERAGE LEDGER {index}:\n{_json_preview(coverage, max_chars=6000)}"
    return ""


def _chat_message_block(message, *, index):
    if not isinstance(message, dict):
        return ""
    role = message.get("role")
    if role in {"user", "assistant"}:
        content = _plain_message_content(message)
        if not content and not message.get("tool_calls"):
            return ""
        parts = [f"{str(role).upper()} MESSAGE {index}:"]
        if content:
            parts.append(content[:5000])
        if message.get("tool_calls"):
            parts.append(_tool_traffic_block(message, index=index))
        return "\n".join(parts)
    return _tool_traffic_block(message, index=index)


def _bounded_join(blocks, *, max_chars=90000):
    kept = []
    total = 0
    for block in blocks:
        block = str(block or "").strip()
        if not block:
            continue
        remaining = max_chars - total
        if remaining <= 0:
            break
        if len(block) > remaining:
            kept.append(block[: max(0, remaining - 24)].rstrip() + "\n... [truncated]")
            total = max_chars
            break
        kept.append(block)
        total += len(block) + 2
    return "\n\n---\n\n".join(kept)


def _completion_content(response):
    try:
        message = response.choices[0].message
    except Exception:
        return ""
    if isinstance(message, dict):
        return str(message.get("content") or "")
    return str(getattr(message, "content", "") or "")


def _summarize_with_fast_model(*, session_id, prompt, source):
    settings = get_settings_snapshot()
    fast_model = settings.get('default_fast_model', 'gemini/gemini-1.5-flash')
    llm_service = LLMService(user_id=current_user.id, session_id=session_id)
    response = llm_service.completion(
        model=fast_model,
        messages=[{"role": "user", "content": prompt}],
        source=source,
    )
    return _completion_content(response).strip()


def _scrub_tool_traffic(chat_session):
    deleted = 0
    stripped = 0
    for msg_wrapper in list(chat_session.messages.all()):
        msg_data = msg_wrapper.message if isinstance(msg_wrapper.message, dict) else {}
        if msg_data.get('role') in {'tool', 'agent_event'}:
            db.session.delete(msg_wrapper)
            deleted += 1
        elif msg_data.get('role') == 'assistant' and msg_data.get('tool_calls'):
            new_data = msg_data.copy()
            del new_data['tool_calls']
            if not new_data.get('content'):
                db.session.delete(msg_wrapper)
                deleted += 1
            else:
                msg_wrapper.message = new_data
                flag_modified(msg_wrapper, 'message')
                stripped += 1
    return {"deleted_messages": deleted, "stripped_assistant_messages": stripped}


@chat_blueprint.route('/chats', methods=['GET'])
@login_required
def list_chats():
    """Lists all available chat sessions for the current user."""
    sessions = ChatSession.query.filter_by(user_id=current_user.id).order_by(ChatSession.updated_at.desc()).all()

    chat_sessions_data = []
    for session in sessions:
        title = session.title
        if not title:
            first_user_message = None
            for msg_wrapper in session.messages.all():
                if msg_wrapper.message.get('role') == 'user':
                    first_user_message = msg_wrapper
                    break

            if first_user_message:
                content = first_user_message.message.get('content')
                if isinstance(content, str):
                    title = content
                elif isinstance(content, list):
                    text_part = next((part.get('text') for part in content if part.get('type') == 'text'), None)
                    if text_part:
                        title = text_part
        if not title:
            title = "Chat"

        chat_sessions_data.append({
            "id": session.id,
            "title": (title[:70] + '...') if len(title) > 70 else title,
        })
    return jsonify(chat_sessions_data)


@chat_blueprint.route('/chats/<session_id>', methods=['GET'])
@login_required
def get_chat_session(session_id):
    """Loads a specific chat session for the current user from the database."""
    projection = load_chat_session_projection(
        session_id,
        user_id=int(current_user.id),
    )
    if not projection:
        return jsonify({"error": "Chat session not found."}), 404
    chat_session = projection.session

    try:
        full_history = request.args.get('full_history', 'false').lower() == 'true'
        messages = load_chat_messages(session_id)
        processed_history = []
        llm_history_for_prompt_reconstruction = []
        total_tokens = {
            "prompt": projection.prompt_tokens,
            "candidates": projection.completion_tokens,
            "total": projection.total_tokens,
        }

        for i, msg_wrapper in enumerate(messages):
            message = msg_wrapper.message
            role = message.get('role')

            if role == 'debug_prompt':
                if full_history:
                    debug_messages = message.get('initial_request_messages')
                    if isinstance(debug_messages, list) and debug_messages:
                        prompt_messages = copy.deepcopy(debug_messages)
                    else:
                        debug_user_message = message.get('user_message')
                        if not isinstance(debug_user_message, dict):
                            debug_user_message = {
                                "role": "user",
                                "content": str(debug_user_message or ""),
                            }
                        prompt_messages = [
                            {"role": "system", "content": str(message.get('system_prompt') or "")},
                            *copy.deepcopy(llm_history_for_prompt_reconstruction),
                            copy.deepcopy(debug_user_message),
                        ]
                    debug_payload = {
                        "messages": prompt_messages,
                        "tools": copy.deepcopy(message.get('initial_request_tools') or []),
                        "tool_names": message.get('tool_names') or [],
                        "model": message.get('initial_request_model'),
                        "assistant_mode": message.get('assistant_mode'),
                        "selected_capability_ids": message.get('selected_capability_ids') or [],
                        "enable_delegation": message.get('enable_delegation'),
                    }
                    dumped = json.dumps(debug_payload, indent=2)
                    processed_history.append({
                        'role': 'debug',
                        'content': (
                            "<strong>Debug: Initial LLM Prompt</strong><br>"
                            f"<pre><code>{html.escape(dumped)}</code></pre>"
                        ),
                        'original_index': i,
                        'file_info': [],
                        'cost': None,
                        'tokens': None,
                        'markdown_content': (
                            "Debug: Initial LLM Prompt\n\n"
                            f"```json\n{dumped}\n```"
                        ),
                    })
                continue

            if role == SOURCE_COVERAGE_LEDGER_ROLE:
                continue

            if role == 'user':
                content = message.get('content')
                display_content = ""
                if isinstance(content, str):
                    display_content = content
                elif isinstance(content, list):
                    display_content = next((part.get('text', '') for part in content if part.get('type') == 'text'), '')

                file_info = message.get('files', [])

                processed_message = {
                    'db_id': msg_wrapper.id,
                    'role': 'user',
                    'content': display_content,
                    'file_info': file_info,
                    'original_index': i,
                    'cost': None,
                    'tokens': None,
                    'markdown_content': display_content,
                }
                processed_history.append(processed_message)

            elif role == 'assistant':
                if message.get('content'):
                    full_text = message.get('content') or ""
                    display_content = md_parser(full_text)

                    processed_message = {
                        'db_id': msg_wrapper.id,
                        'role': 'ai',
                        'content': display_content,
                        'file_info': [],
                        'original_index': i,
                        'cost': None,
                        'tokens': None,
                        'markdown_content': full_text,
                        'tts_audio_path': msg_wrapper.tts_audio_path,
                    }
                    processed_history.append(processed_message)

                if full_history and message.get('tool_calls'):
                    display_tool_calls = []
                    for tc in message.get('tool_calls'):
                        tc_copy = copy.deepcopy(tc)
                        if 'provider_specific_fields' in tc_copy:
                            tc_copy['provider_specific_fields'] = "[Omitted for readability]"
                        display_tool_calls.append(tc_copy)

                    dumped = json.dumps(display_tool_calls, indent=2)
                    tool_calls_html = f"<pre><code>{html.escape(dumped)}</code></pre>"
                    processed_history.append({
                        'role': 'ai tool-call',
                        'content': tool_calls_html,
                        'original_index': i,
                        'file_info': [],
                        'cost': None,
                        'tokens': None,
                        'markdown_content': f"```json\n{dumped}\n```",
                    })

            elif role == 'agent_event':
                event = message.get('event') if isinstance(message, dict) else None
                if isinstance(event, dict):
                    processed_history.append({
                        'db_id': msg_wrapper.id,
                        'role': 'agent_event',
                        'event': event,
                        'original_index': i,
                        'file_info': [],
                        'cost': None,
                        'tokens': None,
                        'markdown_content': None,
                    })
                continue

            elif full_history and role == 'tool':
                tool_name = str(message.get('name') or '')
                auto_open_artifact_id = None
                context_optimization = message.get('_context_optimization') if isinstance(message.get('_context_optimization'), dict) else None
                safe_context_optimization = None
                context_metrics = None
                metrics_html = ""
                metrics_markdown = ""
                if context_optimization:
                    safe_context_optimization = {
                        key: value
                        for key, value in context_optimization.items()
                        if key != 'raw_content'
                    }
                    context_metrics = {
                        "retention_state": safe_context_optimization.get('retention_state') or 'inline_keep',
                        "context_stewardship": safe_context_optimization.get('context_stewardship') or 'assisted',
                        "raw_chars": safe_context_optimization.get('raw_chars', 0),
                        "estimated_raw_tokens": safe_context_optimization.get('estimated_raw_tokens', 0),
                        "included_chars": safe_context_optimization.get('included_chars', 0),
                        "estimated_included_tokens": safe_context_optimization.get('estimated_included_tokens', 0),
                        "raw_content_storage": safe_context_optimization.get('raw_content_storage'),
                        "ref_id": safe_context_optimization.get('ref_id'),
                    }
                    metric_parts = [
                        f"retention: {context_metrics['retention_state']}",
                        f"context: {context_metrics['context_stewardship']}",
                        f"raw: {context_metrics['raw_chars']} chars / ~{context_metrics['estimated_raw_tokens']} tokens",
                        f"in context: {context_metrics['included_chars']} chars / ~{context_metrics['estimated_included_tokens']} tokens",
                    ]
                    if safe_context_optimization.get('retention_decision_by'):
                        metric_parts.append(f"chosen by: {safe_context_optimization.get('retention_decision_by')}")
                    if safe_context_optimization.get('raw_content_storage'):
                        metric_parts.append(f"storage: {safe_context_optimization.get('raw_content_storage')}")
                    if safe_context_optimization.get('ref_id'):
                        metric_parts.append(f"ref: {safe_context_optimization.get('ref_id')}")
                    metrics_text = " | ".join(metric_parts)
                    metrics_html = f"<br><small class=\"tool-context-metrics\">{html.escape(metrics_text)}</small>"
                    metrics_markdown = f"\n\n_Context:_ {metrics_text}"
                try:
                    content_raw = message.get('content') or '{}'
                    content_data = json.loads(content_raw)
                    result_data = content_data.get('result', content_data) if isinstance(content_data, dict) else content_data
                    
                    if isinstance(result_data, str):
                        try:
                            result_data = json.loads(result_data)
                        except json.JSONDecodeError:
                            pass

                    if isinstance(result_data, dict):
                        requested_auto_open = result_data.get('open_canvas_artifact_id')
                        if isinstance(requested_auto_open, str) and requested_auto_open.strip():
                            auto_open_artifact_id = requested_auto_open.strip()
                        elif tool_name == 'runs_create_text_artifact':
                            artifact_data = result_data.get('artifact')
                            if isinstance(artifact_data, dict):
                                artifact_id = artifact_data.get('id')
                                if artifact_id:
                                    auto_open_artifact_id = str(artifact_id).strip()
                    
                    ui_render_html = None
                    if isinstance(result_data, dict) and '_ui_render' in result_data:
                        ui_render_html = result_data.get('_ui_render')
                        # Remove it from dumped JSON so it doesn't clutter
                        result_data = {k: v for k, v in result_data.items() if k != '_ui_render'}
                        
                    dumped_res = json.dumps(result_data, indent=2)
                    
                    if ui_render_html:
                        safe_ui_render = sanitize_html_fragment(str(ui_render_html))
                        if safe_ui_render.strip():
                            tool_content = (
                                f"<strong>Tool: {html.escape(message.get('name', ''))}</strong>"
                                f"{metrics_html}"
                                "<br>Rendered output (sanitized):"
                                f"<div class=\"tool-ui-render\">{safe_ui_render}</div>"
                            )
                            markdown_content = (
                                f"Tool: `{message.get('name')}`{metrics_markdown}\n\n"
                                "Rendered output (sanitized HTML):\n"
                                f"```html\n{safe_ui_render}\n```"
                            )
                        else:
                            tool_content = (
                                f"<strong>Tool: {html.escape(message.get('name', ''))}</strong>"
                                f"{metrics_html}"
                                "<br>Rendered output was removed by sanitizer."
                            )
                            markdown_content = (
                                f"Tool: `{message.get('name')}`{metrics_markdown}\n\n"
                                "Rendered output was removed by sanitizer."
                            )
                    else:
                        tool_content = f"<strong>Tool: {html.escape(tool_name)}</strong>{metrics_html}<br>Result:<pre><code>{html.escape(dumped_res)}</code></pre>"
                        markdown_content = f"Tool: `{tool_name}`{metrics_markdown}\n\n**Result:**\n```json\n{dumped_res}\n```"
                except (json.JSONDecodeError, TypeError):
                    raw_res = str(message.get('content', ''))
                    tool_content = f"<strong>Tool: {html.escape(tool_name)}</strong>{metrics_html}<br>Result:<pre><code>{html.escape(raw_res)}</code></pre>"
                    markdown_content = f"Tool: `{tool_name}`{metrics_markdown}\n\n**Result:**\n```\n{raw_res}\n```"

                tool_message = {
                    'role': 'tool',
                    'content': tool_content,
                    'original_index': i,
                    'file_info': [],
                    'cost': None,
                    'tokens': None,
                    'markdown_content': markdown_content,
                }
                if safe_context_optimization:
                    tool_message['context_optimization'] = safe_context_optimization
                    tool_message['context_metrics'] = context_metrics
                if auto_open_artifact_id:
                    tool_message['auto_open_artifact_id'] = auto_open_artifact_id
                processed_history.append(tool_message)

            llm_history_for_prompt_reconstruction.append(copy.deepcopy(message))

        if hasattr(current_app, 'addon_manager'):
            assistant_selection = current_app.addon_manager.normalize_session_assistant_selection(
                getattr(chat_session, 'active_capability_ids', None),
                chat_session.active_plugins or [],
            )
        else:
            active_capability_ids = getattr(chat_session, 'active_capability_ids', None)
            assistant_selection = {
                "active_plugins": chat_session.active_plugins or [],
                "active_capability_ids": active_capability_ids if active_capability_ids is not None else (chat_session.active_plugins or []),
            }

        latest_execution_scope = _latest_execution_scope_payload(messages)
        latest_mode = str((latest_execution_scope or {}).get("assistant_mode") or (latest_execution_scope or {}).get("mode") or "").lower()
        restored_mode = (
            "default"
            if latest_mode in {"default", "autonomous_discovery", "autonomous"}
            else "direct" if assistant_selection["active_capability_ids"] else "default"
        )
        assistant_scope_base = {
            "mode": restored_mode,
            "capability_ids": assistant_selection["active_capability_ids"],
        }
        assistant_scope, approved_scope_expansions = merge_approved_scope_expansions(
            assistant_scope_base,
            chat_session_id=chat_session.id,
            requester_user_id=current_user.id,
            approval_records=projection.approval_records,
        )
        if approved_scope_expansions:
            assistant_scope["mode"] = "direct"
        if hasattr(current_app, 'addon_manager'):
            assistant_scope = current_app.addon_manager.normalize_assistant_selection(assistant_scope)

        return jsonify({
            "history": processed_history,
            "interactions": load_chat_interactions(chat_session.id),
            "active_plugins": assistant_selection["active_plugins"],
            "active_capability_ids": assistant_selection["active_capability_ids"],
            "assistant_scope": assistant_scope,
            "approved_scope_expansions": approved_scope_expansions,
            "working_memory": _working_memory_payload(chat_session.working_memory),
            "title": chat_session.title,
            "total_cost": f"{projection.total_cost:.6f}",
            "total_tokens": total_tokens,
        })
    except Exception as e:
        logging.error(f"Could not load chat session {session_id}: {e}")
        return jsonify({"error": f"Could not load chat: {str(e)}"}), 500


@chat_blueprint.route('/api/chat/interactions', methods=['GET'])
@login_required
def list_chat_interactions():
    session_id = str(request.args.get('session_id') or '').strip()
    if not session_id:
        return jsonify({"error": "session_id is required."}), 400
    chat_session, error = _chat_session_for_current_user(session_id)
    if error:
        return error
    status = str(request.args.get('status') or '').strip().lower()
    if status and status not in INTERACTION_STATUSES:
        return jsonify({"error": "Invalid interaction status."}), 400
    query = chat_session.interactions
    if status:
        query = query.filter(RuntimeInteraction.status == status)
    rows = query.order_by(RuntimeInteraction.created_at.asc()).limit(200).all()
    return jsonify([row.to_dict() for row in rows])


@chat_blueprint.route('/api/chat/interactions/<interaction_id>', methods=['GET'])
@login_required
def get_chat_interaction(interaction_id):
    try:
        record = get_runtime_interaction_for_user(
            interaction_id,
            requester_user_id=current_user.id,
        )
        return jsonify(record.to_dict())
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 404


@chat_blueprint.route('/api/chat/interactions/<interaction_id>/resolve', methods=['POST'])
@login_required
def resolve_chat_interaction(interaction_id):
    data = request.get_json(silent=True) or {}
    try:
        record = resolve_runtime_interaction(
            interaction_id,
            data.get("response") if "response" in data else data,
            resolver_user_id=current_user.id,
            expected_version=data.get("expected_version"),
            commit=False,
        )
        db.session.add(ChatMessage(
            session_id=record.chat_session_id,
            message=agent_event_chat_message(
                interaction_state_event(record.to_dict(), status="resolved")
            ),
        ))
        db.session.commit()
        return jsonify(record.to_dict())
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 404
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 409


@chat_blueprint.route('/chats/<session_id>/memory', methods=['GET'])
@login_required
def get_chat_working_memory(session_id):
    """Returns the current session working memory for user preview."""
    chat_session = db.session.get(ChatSession, session_id)
    if not chat_session or chat_session.user_id != current_user.id:
        return jsonify({"error": "Chat session not found."}), 404

    return jsonify(_working_memory_payload(chat_session.working_memory))


@chat_blueprint.route('/chats/<session_id>/context-results/<path:context_ref_id>', methods=['GET'])
@login_required
def read_chat_context_result(session_id, context_ref_id):
    _chat_session, error = _chat_session_for_current_user(session_id)
    if error:
        return error
    message, _msg_wrapper = _context_result_message_for_session(session_id, context_ref_id)
    if not message:
        return jsonify({"error": "Context result not found."}), 404

    try:
        start_offset = int(request.args.get("start_offset", 0) or 0)
        max_chars = int(request.args.get("max_chars", 12000) or 12000)
    except ValueError:
        return jsonify({"error": "start_offset and max_chars must be integers."}), 400

    result = read_context_result_from_message(
        message,
        start_offset=start_offset,
        max_chars=max_chars,
        raw_content_loader=_raw_context_loader,
    )
    if not result:
        return jsonify({"error": "The referenced result has no recoverable raw content."}), 404
    return jsonify(result)


@chat_blueprint.route('/chats/<session_id>/context-results/<path:context_ref_id>/search', methods=['GET', 'POST'])
@login_required
def search_chat_context_result(session_id, context_ref_id):
    _chat_session, error = _chat_session_for_current_user(session_id)
    if error:
        return error
    message, _msg_wrapper = _context_result_message_for_session(session_id, context_ref_id)
    if not message:
        return jsonify({"error": "Context result not found."}), 404

    data = request.get_json(silent=True) if request.method == "POST" else {}
    data = data if isinstance(data, dict) else {}
    query = str(data.get("query") or request.args.get("query") or "").strip()
    if not query:
        return jsonify({"error": "query is required."}), 400

    def int_arg(name, default):
        try:
            return int(data.get(name) or request.args.get(name) or default)
        except (TypeError, ValueError):
            raise ValueError(name)

    try:
        max_matches = int_arg("max_matches", 10)
        snippet_chars = int_arg("snippet_chars", 320)
    except ValueError:
        return jsonify({"error": "max_matches and snippet_chars must be integers."}), 400

    case_sensitive_raw = data.get("case_sensitive")
    if case_sensitive_raw is None:
        case_sensitive_raw = request.args.get("case_sensitive")
    case_sensitive = str(case_sensitive_raw).strip().lower() in {"1", "true", "yes", "on"}

    result = search_context_result_in_message(
        message,
        query,
        max_matches=max_matches,
        snippet_chars=snippet_chars,
        case_sensitive=case_sensitive,
        raw_content_loader=_raw_context_loader,
    )
    return jsonify(result)


@chat_blueprint.route('/chats/<session_id>/context-results/<path:context_ref_id>', methods=['PATCH', 'POST'])
@login_required
def update_chat_context_result(session_id, context_ref_id):
    _chat_session, error = _chat_session_for_current_user(session_id)
    if error:
        return error
    message, msg_wrapper = _context_result_message_for_session(session_id, context_ref_id)
    if not message or not msg_wrapper:
        return jsonify({"error": "Context result not found."}), 404

    data = request.get_json(silent=True) or {}
    retention_state = str(data.get("retention_state") or "").strip()
    if not retention_state:
        return jsonify({"error": "retention_state is required."}), 400

    changed = apply_tool_result_retention(
        message,
        retention_state,
        summary=data.get("summary"),
        key_facts=data.get("key_facts"),
        raw_content_loader=_raw_context_loader,
    )
    if not changed:
        return jsonify({"error": "Invalid retention_state or context result."}), 400

    meta = message.get("_context_optimization") if isinstance(message.get("_context_optimization"), dict) else {}
    meta["retention_decision_by"] = "user"
    meta["retention_reason"] = data.get("reason") or ""
    msg_wrapper.message = message
    flag_modified(msg_wrapper, "message")
    db.session.commit()

    safe_meta = {
        key: value
        for key, value in meta.items()
        if key != "raw_content"
    }
    return jsonify({
        "status": "ok",
        "context_ref_id": safe_meta.get("ref_id") or context_ref_id,
        "context_optimization": safe_meta,
    })


@chat_blueprint.route('/chats/<session_id>', methods=['PUT'])
@login_required
def rename_chat_session(session_id):
    """Renames a chat session in the database."""
    chat_session = db.session.get(ChatSession, session_id)
    if not chat_session or chat_session.user_id != current_user.id:
        return jsonify({"error": "Chat session not found."}), 404

    data = request.json
    new_title = data.get('title')
    if not new_title or not isinstance(new_title, str) or not new_title.strip():
        return jsonify({"error": "Invalid title provided."}), 400

    try:
        chat_session.title = new_title.strip()
        db.session.commit()
        return jsonify({"success": True, "message": "Chat renamed."})
    except Exception as e:
        db.session.rollback()
        logging.error(f"Could not rename chat session {session_id}: {e}")
        return jsonify({"error": f"Could not rename chat: {str(e)}"}), 500


@chat_blueprint.route('/chats/<session_id>', methods=['DELETE'])
@login_required
def delete_chat_session(session_id):
    """Deletes a chat session from the database."""
    chat_session = db.session.get(ChatSession, session_id)
    if not chat_session or chat_session.user_id != current_user.id:
        return jsonify({"error": "Chat session not found."}), 404

    try:
        db.session.delete(chat_session)
        db.session.commit()
        return jsonify({"success": True, "message": "Chat deleted."})
    except Exception as e:
        db.session.rollback()
        logging.error(f"Could not delete chat session {session_id}: {e}")
        return jsonify({"error": f"Could not delete chat: {str(e)}"}), 500


@chat_blueprint.route('/chats/<session_id>/messages/<int:message_index>', methods=['PUT'])
@login_required
def update_chat_message(session_id, message_index):
    """Updates a specific message in a chat session's history."""
    chat_session = db.session.get(ChatSession, session_id)
    if not chat_session or chat_session.user_id != current_user.id:
        return jsonify({"error": "Chat session not found."}), 404

    data = request.json
    new_text = data.get('text')
    if new_text is None:
        return jsonify({"error": "New text content is required."}), 400

    try:
        messages = chat_session.messages.all()
        if not (0 <= message_index < len(messages)):
            return jsonify({"error": "Invalid message index."}), 400

        message_wrapper = messages[message_index]
        message_content = message_wrapper.message.copy()

        if message_content.get('role') != 'user':
            return jsonify({"error": "Only user messages can be edited."}), 400

        if (
            message_index > 0
            and isinstance(messages[message_index - 1].message, dict)
            and messages[message_index - 1].message.get('role') == 'debug_prompt'
        ):
            db.session.delete(messages[message_index - 1])

        if isinstance(message_content.get('content'), list):
            text_part = next((part for part in message_content['content'] if part.get('type') == 'text'), None)
            if text_part:
                text_part['text'] = new_text
            else:
                message_content['content'].insert(0, {'type': 'text', 'text': new_text})
        else:
            message_content['content'] = new_text

        message_wrapper.message = message_content
        message_wrapper.tts_audio_path = None
        flag_modified(message_wrapper, "message")
        db.session.commit()

        return jsonify({"success": True, "message": "Message updated."})
    except Exception as e:
        db.session.rollback()
        logging.error(f"Could not update message in session {session_id}: {e}")
        return jsonify({"error": f"Could not update message: {str(e)}"}), 500


@chat_blueprint.route('/chats/<session_id>/messages/<int:message_index>', methods=['DELETE'])
@login_required
def delete_chat_message(session_id, message_index):
    """Deletes a specific message from a chat session's history."""
    chat_session = db.session.get(ChatSession, session_id)
    if not chat_session or chat_session.user_id != current_user.id:
        return jsonify({"error": "Chat session not found."}), 404

    try:
        messages = chat_session.messages.all()
        if not (0 <= message_index < len(messages)):
            return jsonify({"error": "Invalid message index."}), 400

        message_to_delete = messages[message_index]
        message_data = message_to_delete.message if isinstance(message_to_delete.message, dict) else {}
        if (
            message_index > 0
            and message_data.get('role') == 'user'
            and isinstance(messages[message_index - 1].message, dict)
            and messages[message_index - 1].message.get('role') == 'debug_prompt'
        ):
            db.session.delete(messages[message_index - 1])
        db.session.delete(message_to_delete)
        db.session.commit()

        return jsonify({"success": True, "message": "Message deleted."})
    except Exception as e:
        db.session.rollback()
        logging.error(f"Could not delete message in session {session_id}: {e}")
        return jsonify({"error": f"Could not delete message: {str(e)}"}), 500


@chat_blueprint.route('/chats/<session_id>/export_text', methods=['GET'])
@login_required
def export_chat_text(session_id):
    """Exports the user-visible conversation as a single text block."""
    chat_session = db.session.get(ChatSession, session_id)
    if not chat_session or chat_session.user_id != current_user.id:
        return jsonify({"error": "Chat session not found."}), 404

    try:
        conversation_text = []
        for msg_wrapper in chat_session.messages.all():
            message = msg_wrapper.message
            role = message.get('role')

            if role not in ['user', 'assistant']:
                continue

            if message.get('tool_calls'):
                continue

            content = message.get('content')
            full_text = ""
            if isinstance(content, str):
                full_text = content
            elif isinstance(content, list):
                full_text = "\n".join([part.get('text', '') for part in content if part.get('type') == 'text']).strip()

            if full_text:
                prefix = "User:" if role == "user" else "Assistant:"
                conversation_text.append(f"{prefix}\n{full_text}")

        return jsonify({"conversation_text": "\n\n".join(conversation_text)})
    except Exception as e:
        logging.error(f"Could not export chat text from session {session_id}: {e}")
        return jsonify({"error": f"Could not export chat text: {str(e)}"}), 500


@chat_blueprint.route('/api/chats/<session_id>/compress', methods=['POST'])
@login_required
def compress_chat(session_id):
    """Compresses chat context using explicit tool-only or whole-history modes."""
    chat_session = db.session.get(ChatSession, session_id)
    if not chat_session or chat_session.user_id != current_user.id:
        return jsonify({"error": "Chat session not found."}), 404

    data = request.get_json(silent=True) or {}
    mode = _normalize_compression_mode(data.get('mode'))
    if not mode:
        return jsonify({"error": "Invalid compression mode."}), 400

    try:
        if mode == 'scrubtools':
            stats = _scrub_tool_traffic(chat_session)
            db.session.commit()
            return jsonify({
                "success": True,
                "mode": "scrubtools",
                "message": "Tool calls successfully scrubbed from history.",
                **stats,
            })

        elif mode == 'sumtools':
            wrappers = list(chat_session.messages.all())
            tool_blocks = [
                _tool_traffic_block(wrapper.message, index=index)
                for index, wrapper in enumerate(wrappers, start=1)
                if isinstance(wrapper.message, dict)
                and (
                    wrapper.message.get('role') in {'tool', 'agent_event', SOURCE_COVERAGE_LEDGER_ROLE}
                    or (wrapper.message.get('role') == 'assistant' and wrapper.message.get('tool_calls'))
                )
            ]
            tool_text = _bounded_join(tool_blocks)
            if not tool_text:
                stats = _scrub_tool_traffic(chat_session)
                db.session.commit()
                return jsonify({
                    "success": True,
                    "mode": "sumtools",
                    "message": "No tool traffic needed summarizing; tool traffic scrubbed where present.",
                    **stats,
                })

            prompt = (
                "Summarize the following chat tool traffic for future model context. "
                "Focus on what was called, what data or evidence came back, retained refs, artifacts, "
                "source coverage, failures, and open follow-up needs. Preserve concrete ids, titles, "
                "filenames, URLs, dates, counts, and conclusions when present. Do not summarize ordinary "
                "human/assistant chat unless it appears inside a tool request. Output concise bullet points only.\n\n"
                f"{tool_text}"
            )
            summary = _summarize_with_fast_model(
                session_id=session_id,
                prompt=prompt,
                source="chat_compression_sumtools",
            )
            stats = _scrub_tool_traffic(chat_session)
            db.session.add(ChatMessage(
                session_id=chat_session.id,
                message={
                    "role": "assistant",
                    "content": (
                        "**Tool Context Compressed**\n\n"
                        "I summarized prior tool calls, tool outputs, runtime events, and retained refs "
                        "to reduce future context size while preserving ordinary chat messages.\n\n"
                        f"{summary}"
                    ),
                    "compression": {
                        "mode": "sumtools",
                        "deleted_messages": stats["deleted_messages"],
                        "stripped_assistant_messages": stats["stripped_assistant_messages"],
                    },
                },
            ))
            db.session.commit()
            return jsonify({
                "success": True,
                "mode": "sumtools",
                "message": "Tool traffic summarized and compressed.",
                **stats,
            })

        elif mode == 'sumall':
            wrappers = list(chat_session.messages.all())
            messages = [wrapper.message for wrapper in wrappers if isinstance(wrapper.message, dict)]
            if not messages:
                return jsonify({"error": "No messages to compress."}), 400
            preserved_ledger_messages = [
                copy.deepcopy(message)
                for message in messages
                if message.get('role') == SOURCE_COVERAGE_LEDGER_ROLE
            ]
            first_user_message = next(
                (copy.deepcopy(message) for message in messages if message.get('role') == 'user' and _plain_message_content(message)),
                None,
            )
            latest_assistant_message = next(
                (
                    copy.deepcopy(message)
                    for message in reversed(messages)
                    if message.get('role') == 'assistant' and _plain_message_content(message)
                ),
                None,
            )
            chat_text = _bounded_join(
                [
                    _chat_message_block(message, index=index)
                    for index, message in enumerate(messages, start=1)
                    if message.get('role') != SOURCE_COVERAGE_LEDGER_ROLE
                ],
                max_chars=110000,
            )
            prompt = (
                "Summarize the following chat history into strict bullet points representing established facts, "
                "user preferences, decisions, evidence, tool findings, artifacts/refs, and the current state of "
                "the task. Do not include pleasantries. Output ONLY the summary.\n\n"
                f"{chat_text}"
            )
            summary = _summarize_with_fast_model(
                session_id=session_id,
                prompt=prompt,
                source="chat_compression_sumall",
            )

            for wrapper in wrappers:
                db.session.delete(wrapper)

            summary_msg = {
                "role": "assistant",
                "content": (
                    "**Chat History Compressed**\n\n"
                    "I summarized the earlier conversation and tool traffic to save tokens. "
                    "The first user task and latest assistant response are preserved separately when available.\n\n"
                    f"{summary}"
                ),
                "compression": {"mode": "sumall"},
            }
            for ledger_message in preserved_ledger_messages:
                db.session.add(ChatMessage(session_id=chat_session.id, message=ledger_message))
            if first_user_message:
                db.session.add(ChatMessage(session_id=chat_session.id, message=first_user_message))
            db.session.add(ChatMessage(session_id=chat_session.id, message=summary_msg))
            if latest_assistant_message and latest_assistant_message != first_user_message:
                latest_assistant_message.pop("tool_calls", None)
                db.session.add(ChatMessage(session_id=chat_session.id, message=latest_assistant_message))
            db.session.commit()

            return jsonify({"success": True, "mode": "sumall", "message": "History successfully summarized and compressed."})

    except Exception as e:
        db.session.rollback()
        logging.error(f"Could not compress chat session {session_id}: {e}")
        return jsonify({"error": f"Could not compress chat: {str(e)}"}), 500


@chat_blueprint.route('/api/chats/<session_id>/fork', methods=['POST'])
@login_required
def fork_chat_session(session_id):
    """Creates a complete fork of a chat session, including all messages."""
    original_session = db.session.get(ChatSession, session_id)
    if not original_session or original_session.user_id != current_user.id:
        return jsonify({"error": "Chat session not found."}), 404

    try:
        original_messages = original_session.messages.order_by(ChatMessage.created_at).all()
        fork_working_memory = original_session.working_memory
        for old_message in reversed(original_messages):
            payload = old_message.message if isinstance(old_message.message, dict) else {}
            if payload.get('role') == 'user' and 'working_memory_after' in payload:
                fork_working_memory = payload.get('working_memory_after')
                break

        if hasattr(current_app, 'addon_manager'):
            assistant_selection = current_app.addon_manager.normalize_session_assistant_selection(
                getattr(original_session, 'active_capability_ids', None),
                original_session.active_plugins or [],
            )
            active_capability_ids = assistant_selection["active_capability_ids"]
        else:
            active_capability_ids = (
                original_session.active_capability_ids
                if original_session.active_capability_ids is not None
                else original_session.active_plugins
            ) or []

        new_session = ChatSession(
            user_id=current_user.id,
            title=f"Fork of {original_session.title or 'Chat'}",
            active_plugins=[],
            active_capability_ids=active_capability_ids,
            working_memory=fork_working_memory,
        )
        db.session.add(new_session)
        db.session.flush()

        for old_message in original_messages:
            new_message = ChatMessage(
                session_id=new_session.id,
                message=copy.deepcopy(old_message.message),
                created_at=old_message.created_at,
            )
            db.session.add(new_message)

        db.session.commit()
        return jsonify({"success": True, "new_session_id": new_session.id})
    except Exception as e:
        db.session.rollback()
        logging.error(f"Could not fork chat session {session_id}: {e}")
        return jsonify({"error": f"Could not fork chat: {str(e)}"}), 500


@chat_blueprint.route('/chat', methods=['POST'])
@login_required
def chat():
    return stream_chat_response()


def register_chat_blueprint(app):
    app.register_blueprint(chat_blueprint)
