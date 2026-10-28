import copy
import hashlib
import json
import logging
import threading
from pathlib import Path

from iarf_assistant.core.agent_loop import (
    normalize_message_tool_call_ids,
    restore_provider_tool_call_ids,
)
from iarf_assistant.core.runtime_contracts import build_tool_result_envelope


_memory_locks = {}
_memory_locks_lock = threading.Lock()

# Assisted mode keeps ordinary results inline but externalizes large payloads
# before they are repeated in every subsequent model call. The exact result
# remains durable and can be read in targeted slices through context tools.
DEFAULT_TOOL_RESULT_INLINE_CHARS = 12_000
DEFAULT_TOOL_RESULT_PREVIEW_CHARS = 2400
DEFAULT_TOOL_RESULT_TURN_LOCAL_MIN_CHARS = 4000
# Manual mode has no application-imposed result clipping. Provider admission
# still rejects an over-limit rendered request with an explicit manifest.
MANUAL_TOOL_RESULT_INLINE_CHARS = (2 ** 63) - 1
AGGRESSIVE_TOOL_RESULT_INLINE_CHARS = 4000
CONTEXT_RETENTION_STATES = {
    "inline_keep",
    "inline_this_turn",
    "preview_ref",
    "summary_ref",
    "archive_only",
    "pin_for_chief",
}
DEFAULT_CONTEXT_PRESSURE_INCLUDED_TOKENS = 8000
DEFAULT_CONTEXT_PRESSURE_RAW_TOKENS = 20000
DEFAULT_CONTEXT_PRESSURE_MAX_ITEMS = 5


def tool_result_limits_for_policy(runtime_policy=None):
    """Resolve tool-result context limits from the active turn policy."""
    policy = runtime_policy if isinstance(runtime_policy, dict) else {}
    mode = str(policy.get("context_stewardship") or "assisted").strip().lower()
    if mode == "manual":
        return {
            "inline_limit_chars": MANUAL_TOOL_RESULT_INLINE_CHARS,
            "preview_chars": DEFAULT_TOOL_RESULT_PREVIEW_CHARS,
            "context_stewardship": "manual",
        }
    if mode == "aggressive":
        return {
            "inline_limit_chars": AGGRESSIVE_TOOL_RESULT_INLINE_CHARS,
            "preview_chars": DEFAULT_TOOL_RESULT_PREVIEW_CHARS,
            "context_stewardship": "aggressive",
        }
    return {
        "inline_limit_chars": DEFAULT_TOOL_RESULT_INLINE_CHARS,
        "preview_chars": DEFAULT_TOOL_RESULT_PREVIEW_CHARS,
        "context_stewardship": "assisted",
    }


def get_memory_lock(session_id):
    """Returns a unique threading lock for a specific chat session."""
    with _memory_locks_lock:
        if session_id not in _memory_locks:
            _memory_locks[session_id] = threading.Lock()
        return _memory_locks[session_id]


def serialize_tool_output(output):
    """
    Serializes tool output to a string for the 'tool' role content.
    Avoids wrapping in {"result": ...} to prevent model confusion.
    """
    if output is None:
        return "Success"
    if isinstance(output, (dict, list)):
        def safe_serializer(obj):
            if hasattr(obj, 'isoformat'):
                return obj.isoformat()
            return str(obj)
        try:
            return json.dumps(output, default=safe_serializer)
        except Exception as exc:
            logging.error(f"Error serializing tool output: {exc}")
            return str(output)
    return str(output)


def estimate_text_tokens(text):
    """Cheap tokenizer-independent estimate used for budgeting and UI telemetry."""
    value = str(text or "")
    if not value:
        return 0
    return max(1, (len(value) + 3) // 4)


def truncate_text(text, max_chars, suffix="... [truncated]"):
    value = str(text or "")
    try:
        limit = int(max_chars)
    except (TypeError, ValueError):
        limit = DEFAULT_TOOL_RESULT_PREVIEW_CHARS
    if limit <= 0 or len(value) <= limit:
        return value
    return value[: max(0, limit - len(suffix))].rstrip() + suffix


def make_context_result_ref_id(session_id, tool_call_id, tool_name):
    seed = f"{session_id or 'session'}:{tool_call_id or ''}:{tool_name or 'tool'}"
    digest = hashlib.sha256(seed.encode("utf-8", errors="replace")).hexdigest()[:18]
    return f"chat_tool_result:{digest}"


def _context_envelope_from_meta(meta):
    retention_state = meta.get("retention_state") or "preview_ref"
    preview = meta.get("preview") or ""
    if retention_state == "archive_only":
        preview = ""
    envelope = {
        "status": "context_optimized",
        "retention_state": retention_state,
        "message": (
            "The full tool result is retained outside the normal model context. "
            "Use context_read_result with context_ref_id to inspect targeted slices when needed."
        ),
        "tool_name": meta.get("tool_name"),
        "context_ref_id": meta.get("ref_id"),
        "raw_chars": meta.get("raw_chars", 0),
        "estimated_raw_tokens": meta.get("estimated_raw_tokens", 0),
        "included_context_chars": meta.get("included_chars", 0),
        "estimated_included_context_tokens": meta.get("estimated_included_tokens", 0),
        "context_stewardship": meta.get("context_stewardship") or "assisted",
        "retention_decision_by": meta.get("retention_decision_by") or "runtime",
        "raw_content_storage": meta.get("raw_content_storage") or ("inline_metadata" if meta.get("raw_content") is not None else ""),
        "compacted": bool(meta.get("compacted")),
        "summary": meta.get("summary") or "",
        "preview": preview,
        "next_actions": [
            "Call context_read_result with context_ref_id and max_chars/start_offset for specific slices.",
            "Call context_manage_results to pin, summarize, ref-only, or archive this result for future turns.",
        ],
    }
    return envelope


def _meta_for_tool_result(
    *,
    session_id,
    tool_call_id,
    tool_name,
    raw_content,
    included_content,
    compacted,
    retention_state,
    preview_chars,
    context_stewardship="assisted",
    retention_decision_by="runtime",
):
    preview = truncate_text(raw_content, preview_chars)
    return {
        "schema_version": 1,
        "ref_id": make_context_result_ref_id(session_id, tool_call_id, tool_name),
        "tool_call_id": str(tool_call_id or ""),
        "tool_name": str(tool_name or ""),
        "raw_chars": len(raw_content),
        "estimated_raw_tokens": estimate_text_tokens(raw_content),
        "included_chars": len(included_content),
        "estimated_included_tokens": estimate_text_tokens(included_content),
        "retention_state": retention_state,
        "retention_decision_by": retention_decision_by,
        "context_stewardship": context_stewardship,
        "compacted": bool(compacted),
        "preview": preview,
    }


def build_tool_result_message(
    *,
    session_id,
    tool_call_id,
    provider_tool_call_id=None,
    tool_name,
    result,
    result_envelope=None,
    inline_limit_chars=DEFAULT_TOOL_RESULT_INLINE_CHARS,
    preview_chars=DEFAULT_TOOL_RESULT_PREVIEW_CHARS,
    context_stewardship="assisted",
    turn_local_min_chars=DEFAULT_TOOL_RESULT_TURN_LOCAL_MIN_CHARS,
):
    """Builds a tool message that keeps large raw output out of model context.

    The full raw result is retained in private message metadata when compaction is
    needed. Prompt assembly strips that metadata before model calls.
    """
    raw_content = serialize_tool_output(result)
    envelope = (
        copy.deepcopy(result_envelope)
        if isinstance(result_envelope, dict)
        else build_tool_result_envelope(
            tool_name=tool_name,
            internal_tool_call_id=str(tool_call_id or ""),
            provider_tool_call_id=str(provider_tool_call_id or ""),
            result=result,
            inline_limit_chars=inline_limit_chars,
            preview_chars=preview_chars,
        ).to_dict()
    )
    try:
        inline_limit = max(1000, int(inline_limit_chars or DEFAULT_TOOL_RESULT_INLINE_CHARS))
    except (TypeError, ValueError):
        inline_limit = DEFAULT_TOOL_RESULT_INLINE_CHARS

    compacted = len(raw_content) > inline_limit
    try:
        turn_local_min = max(0, int(turn_local_min_chars))
    except (TypeError, ValueError):
        turn_local_min = DEFAULT_TOOL_RESULT_TURN_LOCAL_MIN_CHARS
    use_turn_local = (
        not compacted
        and str(context_stewardship or "assisted").strip().lower() not in {"manual", "off", "disabled"}
        and turn_local_min > 0
        and len(raw_content) >= turn_local_min
    )
    retention_state = "preview_ref" if compacted else "inline_this_turn" if use_turn_local else "inline_keep"
    included_content = raw_content
    meta = _meta_for_tool_result(
        session_id=session_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        raw_content=raw_content,
        included_content=included_content,
        compacted=compacted,
        retention_state=retention_state,
        preview_chars=preview_chars,
        context_stewardship=context_stewardship,
    )

    if compacted:
        raw_payload_artifact_id = _runtime_payload_artifact_id(envelope)
        if raw_payload_artifact_id:
            meta["raw_content_artifact_id"] = raw_payload_artifact_id
            meta["raw_content_storage"] = "runtime_artifact"
        else:
            meta["raw_content"] = raw_content
        included_content = serialize_tool_output(_context_envelope_from_meta(meta))
        meta["included_chars"] = len(included_content)
        meta["estimated_included_tokens"] = estimate_text_tokens(included_content)
        included_content = serialize_tool_output(_context_envelope_from_meta(meta))
        meta["included_chars"] = len(included_content)
        meta["estimated_included_tokens"] = estimate_text_tokens(included_content)

    message = {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "name": tool_name,
        "content": included_content,
        "_context_optimization": meta,
        "_tool_result_envelope": envelope,
    }
    provider_id = str(provider_tool_call_id or "")
    if provider_id and provider_id != str(tool_call_id or ""):
        message["_provider_tool_call_id"] = provider_id
    return message


def _runtime_payload_artifact_id(envelope):
    if not isinstance(envelope, dict):
        return None
    readable_kinds = {
        "runtime_tool_result_payload",
        "runtime_dependency_output_payload",
        "chat_tool_result_raw_payload",
    }
    data_refs = envelope.get("data_refs")
    if not isinstance(data_refs, (list, tuple)):
        return None
    for data_ref in reversed(data_refs):
        if not isinstance(data_ref, dict):
            continue
        metadata = (
            data_ref.get("metadata")
            if isinstance(data_ref.get("metadata"), dict)
            else {}
        )
        ref_id = str(data_ref.get("id") or "")
        if metadata.get("kind") in readable_kinds and ref_id.startswith("artifact:"):
            return ref_id.split(":", 1)[1]
    return None


def apply_tool_result_retention(message, retention_state, *, summary=None, key_facts=None, raw_content_loader=None):
    """Updates a stored/current tool message's future model-context representation."""
    if not isinstance(message, dict) or message.get("role") != "tool":
        return False
    state = str(retention_state or "").strip()
    if state not in CONTEXT_RETENTION_STATES:
        return False

    meta = message.get("_context_optimization")
    if not isinstance(meta, dict):
        current_content = str(message.get("content") or "")
        meta = _meta_for_tool_result(
            session_id=None,
            tool_call_id=message.get("tool_call_id"),
            tool_name=message.get("name"),
            raw_content=current_content,
            included_content=current_content,
            compacted=False,
            retention_state="inline_keep",
            preview_chars=DEFAULT_TOOL_RESULT_PREVIEW_CHARS,
            context_stewardship="assisted",
            retention_decision_by="unknown_legacy",
        )
        message["_context_optimization"] = meta

    raw_content = meta.get("raw_content")
    if raw_content is None and callable(raw_content_loader):
        raw_content = raw_content_loader(meta)
    if raw_content is None:
        raw_content = str(message.get("content") or "")
    if raw_content and not meta.get("raw_content_artifact_id"):
        meta["raw_content"] = raw_content

    meta["retention_state"] = state
    meta["retention_decision_by"] = "model_or_user"
    if summary is not None:
        meta["summary"] = str(summary or "")
    if key_facts is not None:
        meta["key_facts"] = key_facts

    if state in {"inline_keep", "inline_this_turn", "pin_for_chief"}:
        message["content"] = raw_content
        meta["compacted"] = False
        if state == "inline_this_turn":
            meta["turn_local"] = True
    else:
        meta["compacted"] = True
        message["content"] = serialize_tool_output(_context_envelope_from_meta(meta))

    meta["included_chars"] = len(str(message.get("content") or ""))
    meta["estimated_included_tokens"] = estimate_text_tokens(message.get("content") or "")
    return True


def demote_inline_this_turn_results(messages, *, raw_content_loader=None):
    """Compact turn-local inline tool results before they become future prompt history."""
    demoted = []
    for message in messages or []:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        meta = message.get("_context_optimization")
        if not isinstance(meta, dict) or meta.get("retention_state") != "inline_this_turn":
            continue

        raw_content = meta.get("raw_content")
        if raw_content is None and callable(raw_content_loader):
            raw_content = raw_content_loader(meta)
        if raw_content is None:
            raw_content = message.get("content")
        raw_content = str(raw_content or "")
        if raw_content and not meta.get("raw_content_artifact_id"):
            meta["raw_content"] = raw_content

        next_state = "summary_ref" if meta.get("summary") or meta.get("key_facts") else "preview_ref"
        meta["retention_state"] = next_state
        meta["retention_decision_by"] = "runtime_auto_demote"
        meta["auto_demoted_from"] = "inline_this_turn"
        meta["auto_demoted_after_turn"] = True
        meta["turn_local"] = False
        meta["compacted"] = True
        message["content"] = serialize_tool_output(_context_envelope_from_meta(meta))
        meta["included_chars"] = len(str(message.get("content") or ""))
        meta["estimated_included_tokens"] = estimate_text_tokens(message.get("content") or "")
        demoted.append({
            "context_ref_id": meta.get("ref_id"),
            "retention_state": next_state,
        })
    return demoted


def is_context_overflow_error(error):
    """Best-effort provider-agnostic context/window overflow detection."""
    if error is None:
        return False
    admission_result = getattr(error, "result", None)
    if (
        admission_result is not None
        and getattr(admission_result, "admitted", None) is False
        and getattr(admission_result, "overflow_tokens", 0) > 0
    ):
        return True
    status_code = getattr(error, "status_code", None)
    try:
        status_code = int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        status_code = None
    text_parts = [
        str(error),
        str(getattr(error, "message", "") or ""),
        str(getattr(error, "llm_provider", "") or ""),
    ]
    response = getattr(error, "response", None)
    if response is not None:
        text_parts.append(str(getattr(response, "text", "") or ""))
        text_parts.append(str(getattr(response, "content", "") or ""))
    text = " ".join(text_parts).lower()
    context_markers = (
        "context length",
        "context_length",
        "context window",
        "maximum context",
        "max context",
        "token limit",
        "too many tokens",
        "input too long",
        "prompt too long",
        "request too large",
        "exceeds the maximum",
        "exceeded the maximum",
        "exceeded token",
        "tokens exceeds",
        "tokens is larger",
        "maximum number of tokens",
    )
    if any(marker in text for marker in context_markers):
        return True
    if status_code == 400 and "tokens" in text and any(word in text for word in ("maximum", "limit", "too large", "larger")):
        return True
    return False


def compact_tool_results_for_context_retry(messages, *, raw_content_loader=None):
    """Compact inline tool results before retrying a provider call after context overflow."""
    compacted = []
    for message in messages or []:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        meta = message.get("_context_optimization")
        state = meta.get("retention_state") if isinstance(meta, dict) else None
        if state in {"summary_ref", "preview_ref", "archive_only"}:
            continue
        if state == "pin_for_chief":
            continue
        before_content = str(message.get("content") or "")
        before_tokens = estimate_text_tokens(before_content)
        summary = meta.get("summary") if isinstance(meta, dict) else None
        key_facts = meta.get("key_facts") if isinstance(meta, dict) else None
        next_state = "summary_ref" if summary or key_facts else "preview_ref"
        if not apply_tool_result_retention(
            message,
            next_state,
            summary=summary,
            key_facts=key_facts,
            raw_content_loader=raw_content_loader,
        ):
            continue
        updated_meta = message.get("_context_optimization") if isinstance(message.get("_context_optimization"), dict) else {}
        updated_meta["retention_decision_by"] = "runtime_context_retry"
        updated_meta["retention_reason"] = "Automatic compaction after provider context-length error."
        after_content = str(message.get("content") or "")
        compacted.append({
            "context_ref_id": updated_meta.get("ref_id"),
            "tool_name": updated_meta.get("tool_name") or message.get("name"),
            "retention_state": next_state,
            "before_tokens": before_tokens,
            "after_tokens": estimate_text_tokens(after_content),
        })
    return compacted


def compact_tool_results_for_pressure(
    messages,
    *,
    runtime_policy=None,
    max_included_tokens=DEFAULT_CONTEXT_PRESSURE_INCLUDED_TOKENS,
    target_included_tokens=None,
    raw_content_loader=None,
):
    """Automatically demote bulky inline tool results when they dominate context."""
    policy = runtime_policy if isinstance(runtime_policy, dict) else {}
    stewardship = str(policy.get("context_stewardship") or "assisted").strip().lower()
    if stewardship in {"manual", "off", "disabled"}:
        return []
    try:
        maximum = max(1, int(max_included_tokens))
    except (TypeError, ValueError):
        maximum = DEFAULT_CONTEXT_PRESSURE_INCLUDED_TOKENS
    if stewardship == "aggressive":
        maximum = min(maximum, max(1, DEFAULT_CONTEXT_PRESSURE_INCLUDED_TOKENS // 2))
    try:
        target = max(1, int(target_included_tokens or int(maximum * 0.7)))
    except (TypeError, ValueError):
        target = max(1, int(maximum * 0.7))
    target = min(target, maximum)

    candidates = []
    total_included = 0

    def safe_int(value):
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    for message in messages or []:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        meta = message.get("_context_optimization")
        if not isinstance(meta, dict):
            continue
        included_tokens = safe_int(meta.get("estimated_included_tokens"))
        total_included += included_tokens
        if meta.get("retention_state") in {"inline_keep", "inline_this_turn"}:
            candidates.append((included_tokens, message))

    if total_included <= maximum:
        return []

    compacted = []
    for before_tokens, message in sorted(candidates, key=lambda item: item[0], reverse=True):
        if total_included <= target:
            break
        meta = message.get("_context_optimization") or {}
        summary = meta.get("summary")
        key_facts = meta.get("key_facts")
        next_state = "summary_ref" if summary or key_facts else "preview_ref"
        if not apply_tool_result_retention(
            message,
            next_state,
            summary=summary,
            key_facts=key_facts,
            raw_content_loader=raw_content_loader,
        ):
            continue
        updated_meta = message.get("_context_optimization") or {}
        updated_meta["retention_decision_by"] = "runtime_pressure_compaction"
        updated_meta["retention_reason"] = "Automatic compaction at the configured prompt-pressure threshold."
        after_tokens = safe_int(updated_meta.get("estimated_included_tokens"))
        total_included = max(0, total_included - before_tokens + after_tokens)
        compacted.append({
            "context_ref_id": updated_meta.get("ref_id"),
            "tool_name": updated_meta.get("tool_name") or message.get("name"),
            "retention_state": next_state,
            "before_tokens": before_tokens,
            "after_tokens": after_tokens,
        })
    return compacted


def read_context_result_from_message(message, *, start_offset=0, max_chars=8000, raw_content_loader=None):
    """Reads a bounded slice from retained tool-result raw content."""
    if not isinstance(message, dict):
        return None
    meta = message.get("_context_optimization")
    if not isinstance(meta, dict):
        return None
    raw_content = meta.get("raw_content")
    if raw_content is None and callable(raw_content_loader):
        raw_content = raw_content_loader(meta)
    if raw_content is None:
        raw_content = message.get("content")
    raw_content = str(raw_content or "")
    try:
        start = max(0, int(start_offset or 0))
    except (TypeError, ValueError):
        start = 0
    try:
        limit = max(1, min(int(max_chars or 8000), 50000))
    except (TypeError, ValueError):
        limit = 8000
    end = min(len(raw_content), start + limit)
    return {
        "status": "ok",
        "context_ref_id": meta.get("ref_id"),
        "tool_name": meta.get("tool_name"),
        "retention_state": meta.get("retention_state"),
        "raw_chars": len(raw_content),
        "estimated_raw_tokens": estimate_text_tokens(raw_content),
        "start_offset": start,
        "end_offset": end,
        "truncated": end < len(raw_content),
        "content": raw_content[start:end],
    }


def search_context_result_in_message(
    message,
    query,
    *,
    max_matches=10,
    snippet_chars=320,
    case_sensitive=False,
    raw_content_loader=None,
):
    """Search retained tool-result raw content and return bounded offset snippets."""
    query_text = str(query or "").strip()
    if not query_text:
        return {"status": "error", "error": "query is required."}
    if not isinstance(message, dict):
        return {"status": "error", "error": "message is required."}

    meta = message.get("_context_optimization")
    raw_content = None
    if isinstance(meta, dict):
        raw_content = meta.get("raw_content")
        if raw_content is None and callable(raw_content_loader):
            raw_content = raw_content_loader(meta)
    if raw_content is None:
        raw_content = message.get("content")
    raw_content = str(raw_content or "")

    try:
        limit = max(1, min(int(max_matches or 10), 50))
    except (TypeError, ValueError):
        limit = 10
    try:
        snippet_limit = max(80, min(int(snippet_chars or 320), 2000))
    except (TypeError, ValueError):
        snippet_limit = 320

    haystack = raw_content if case_sensitive else raw_content.lower()
    needle = query_text if case_sensitive else query_text.lower()
    matches = []
    cursor = 0
    while len(matches) < limit:
        index = haystack.find(needle, cursor)
        if index < 0:
            break
        end = index + len(query_text)
        before = max(0, (snippet_limit - len(query_text)) // 2)
        after = max(0, snippet_limit - len(query_text) - before)
        snippet_start = max(0, index - before)
        snippet_end = min(len(raw_content), end + after)
        prefix = "... " if snippet_start > 0 else ""
        suffix = " ..." if snippet_end < len(raw_content) else ""
        matches.append({
            "match_index": len(matches),
            "start_offset": index,
            "end_offset": end,
            "snippet_start_offset": snippet_start,
            "snippet_end_offset": snippet_end,
            "snippet": prefix + raw_content[snippet_start:snippet_end] + suffix,
        })
        cursor = max(end, index + 1)

    return {
        "status": "ok",
        "context_ref_id": meta.get("ref_id") if isinstance(meta, dict) else None,
        "tool_name": (meta.get("tool_name") if isinstance(meta, dict) else None) or message.get("name"),
        "query": query_text,
        "case_sensitive": bool(case_sensitive),
        "raw_chars": len(raw_content),
        "estimated_raw_tokens": estimate_text_tokens(raw_content),
        "matched_count": len(matches),
        "matches": matches,
        "next_action": (
            "Use context_read_result with a start_offset near a match if more surrounding content is needed."
            if matches
            else "No matches found in this retained result."
        ),
    }


def find_tool_result_message_by_ref(messages, ref_id):
    target = str(ref_id or "").strip()
    if not target:
        return None
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        meta = message.get("_context_optimization")
        if isinstance(meta, dict) and meta.get("ref_id") == target:
            return message
    return None


def sanitize_message_for_llm_context(message):
    """Returns the provider-facing view of a stored chat message."""
    if not isinstance(message, dict):
        return message
    role = message.get("role")
    if role == "tool":
        content = message.get("content")
        meta = message.get("_context_optimization")
        if isinstance(meta, dict) and meta.get("retention_state") in {"summary_ref", "preview_ref", "archive_only"}:
            content = serialize_tool_output(_context_envelope_from_meta(meta))
        clean = {
            "role": "tool",
            "tool_call_id": message.get("tool_call_id"),
            "name": message.get("name"),
            "content": content or "",
        }
        if message.get("_provider_tool_call_id"):
            clean["_provider_tool_call_id"] = message.get("_provider_tool_call_id")
        return restore_provider_tool_call_ids(clean)
    if role == "assistant":
        clean = {"role": "assistant", "content": message.get("content") or ""}
        if message.get("tool_calls"):
            clean["tool_calls"] = copy.deepcopy(message.get("tool_calls"))
        if isinstance(message.get("_provider_tool_call_ids"), dict):
            clean["_provider_tool_call_ids"] = copy.deepcopy(
                message.get("_provider_tool_call_ids")
            )
        if message.get("function_call"):
            clean["function_call"] = copy.deepcopy(message.get("function_call"))
        return restore_provider_tool_call_ids(clean)
    if role == "user":
        clean = {"role": "user", "content": message.get("content") or ""}
        if message.get("files"):
            clean["files"] = copy.deepcopy(message.get("files"))
        return clean
    if role == "system":
        return {"role": "system", "content": message.get("content") or ""}
    return copy.deepcopy(message)


def sanitize_messages_for_llm_context(messages):
    return [sanitize_message_for_llm_context(message) for message in messages or []]


def normalize_context_ref_ids(arguments):
    """Accept plural context_ref_ids and common singular aliases from model tool calls."""
    args = arguments if isinstance(arguments, dict) else {}
    values = []
    for key in ("context_ref_ids", "context_ref_id", "context_refs", "context_ref"):
        raw = args.get(key)
        if raw in (None, "", [], {}):
            continue
        if isinstance(raw, (list, tuple, set)):
            values.extend(raw)
        else:
            values.append(raw)
    normalized = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def build_context_pressure_messages(
    messages,
    *,
    runtime_policy=None,
    min_included_tokens=DEFAULT_CONTEXT_PRESSURE_INCLUDED_TOKENS,
    min_raw_tokens=DEFAULT_CONTEXT_PRESSURE_RAW_TOKENS,
    max_items=DEFAULT_CONTEXT_PRESSURE_MAX_ITEMS,
):
    """Build an ephemeral runtime note when retained tool results create context pressure."""
    policy = runtime_policy if isinstance(runtime_policy, dict) else {}
    stewardship = str(policy.get("context_stewardship") or "assisted").strip().lower()
    if stewardship in {"off", "disabled"}:
        return []

    def safe_int(value):
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    retained = []
    total_included_tokens = 0
    total_raw_tokens = 0
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        meta = message.get("_context_optimization")
        if not isinstance(meta, dict):
            continue
        raw_tokens = safe_int(meta.get("estimated_raw_tokens"))
        included_tokens = safe_int(meta.get("estimated_included_tokens"))
        raw_chars = safe_int(meta.get("raw_chars"))
        included_chars = safe_int(meta.get("included_chars"))
        total_raw_tokens += raw_tokens
        total_included_tokens += included_tokens
        retained.append({
            "ref_id": meta.get("ref_id") or "",
            "tool_name": meta.get("tool_name") or message.get("name") or "tool",
            "retention_state": meta.get("retention_state") or "unknown",
            "raw_tokens": raw_tokens,
            "included_tokens": included_tokens,
            "raw_chars": raw_chars,
            "included_chars": included_chars,
            "compacted": bool(meta.get("compacted")),
        })

    if not retained:
        return []
    try:
        min_included = max(0, int(min_included_tokens))
    except (TypeError, ValueError):
        min_included = DEFAULT_CONTEXT_PRESSURE_INCLUDED_TOKENS
    try:
        min_raw = max(0, int(min_raw_tokens))
    except (TypeError, ValueError):
        min_raw = DEFAULT_CONTEXT_PRESSURE_RAW_TOKENS
    try:
        max_rows = max(1, min(int(max_items), 10))
    except (TypeError, ValueError):
        max_rows = DEFAULT_CONTEXT_PRESSURE_MAX_ITEMS

    has_large_inline = any(
        item["included_tokens"] >= min_included
        or (
            item["retention_state"] in {"inline_keep", "inline_this_turn", "pin_for_chief"}
            and item["raw_tokens"] >= max(1, min_raw // 2)
        )
        for item in retained
    )
    if total_included_tokens < min_included and not has_large_inline:
        return []

    retained.sort(key=lambda item: (item["included_tokens"], item["raw_tokens"]), reverse=True)
    lines = [
        "(System runtime context pressure notice; not a user request.)",
        (
            f"Retained tool results total about {total_raw_tokens} raw tokens, with about "
            f"{total_included_tokens} tokens currently included in model context. "
            f"Context stewardship mode: {stewardship}."
        ),
        "Largest retained results:",
    ]
    for item in retained[:max_rows]:
        ref = item["ref_id"] or "(no ref)"
        lines.append(
            "- "
            f"{ref} from {item['tool_name']}: retention={item['retention_state']}, "
            f"raw~{item['raw_tokens']} tokens/{item['raw_chars']} chars, "
            f"in-context~{item['included_tokens']} tokens/{item['included_chars']} chars."
        )
    lines.extend([
        "Use context_read_result for targeted slices instead of reloading whole blobs.",
        "Use context_manage_results to pin only central evidence, summarize/ref broad evidence, or archive unused results after extracting key facts.",
        "For broad corpora, prefer source_coverage_get plus delegated or code-mediated batch processing over repeated chief rereads.",
    ])
    return [{"role": "user", "content": "\n".join(lines)}]


def split_tool_output_attachments(output):
    """Separates framework-level file attachments from normal tool output."""
    if not isinstance(output, dict):
        return output, []

    attachments = output.get('_iarf_tool_attachments') or []
    if not isinstance(attachments, list) or not attachments:
        return output, []

    clean_output = output.copy()
    clean_output.pop('_iarf_tool_attachments', None)
    return clean_output, attachments


def build_tool_attachment_message(attachments, note="(System): Tool returned visual attachments:"):
    safe_attachments = []
    for item in attachments or []:
        if not isinstance(item, dict) or not item.get('filePath'):
            continue
        safe_attachments.append({
            'filePath': item.get('filePath'),
            'fileName': item.get('fileName') or Path(str(item.get('filePath'))).name,
            'pass_as': item.get('pass_as') or 'full_content',
        })

    if not safe_attachments:
        return None
    return {'role': 'user', 'content': note, 'files': safe_attachments}


def unique_tool_declarations(tools):
    """Deduplicates LiteLLM/OpenAI tool declarations by function name."""
    result = []
    seen = set()
    for tool in tools or []:
        function = tool.get("function") if isinstance(tool, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        key = name or json.dumps(tool, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        result.append(tool)
    return result


def sanitize_llm_message(msg_obj):
    """
    Sanitizes LiteLLM Message objects for strict OpenAI-compatible endpoints.
    Strips reasoning traces, provider-specific metadata, and handles null content.
    """
    msg_dict = (
        copy.deepcopy(msg_obj)
        if isinstance(msg_obj, dict)
        else msg_obj.model_dump(exclude_none=True)
    )
    msg_dict.pop('provider_specific_fields', None)
    msg_dict.pop('reasoning_content', None)
    msg_dict.pop('logprobs', None)
    if msg_dict.get('content') is None:
        msg_dict['content'] = ""
    return normalize_message_tool_call_ids(msg_dict)
