"""Bounded, principal-neutral read projections for Translator documents.

This module is deliberately a read-only boundary.  Callers hand it a project
which they have already authorized; the service verifies the active block plan
and then returns only document-native projections.  It does not know about
HTTP, MCP, login, grants, model execution, or exports.

The implementation keeps the wire deliberately boring: cursors contain only a
zero-based offset and a filter fingerprint, and all source/target text is
searched after the same small Unicode normalization profile.  In particular,
normalization is not a licence to claim fuzzy or semantic matches.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html.parser
import inspect
import json
import re
import time
import unicodedata
from bisect import bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from importlib import import_module
from typing import Any, cast

import regex as bounded_regex

from carlina.core.utc import utc_iso

from . import scholarly_wire
from .block_workspace_api import (
    BlockWorkspaceIntegrityError,
    WorkspacePlan,
    _rehydrate_workspace_objects,
    build_block_glossary_payload,
    load_block_source_plan,
)
from .compact_markup import compact_source_projection
from .models import (
    TranslationBlockReviewHead,
    TranslationTargetBlockActive,
    TranslationTargetBlockRevision,
)
from .scholarly_review import (
    get_research_dossier,
    list_research_dossiers,
)

get_dossier = get_research_dossier
list_dossiers = list_research_dossiers

REVIEW_CONTRACT = "translator-document-review-v1"
NORMALIZATION_PROFILE = "unicode-nfkc-casefold-v1"

MAX_FETCH_BLOCKS = 100
MAX_SEARCH_LIMIT = 50
MAX_SEARCH_MATCHES = 10_000
MAX_QUERY_CHARS = 512
MAX_CONTEXT_BLOCKS = 5
MAX_REGEX_SECONDS = 0.05
MAX_REGEX_TOTAL_SECONDS = 2.0
_ALLOWED_REGEX_FLAGS = ("ignore_case", "multiline", "dotall", "verbose")
_REGEX_FLAGS = {
    "ignore_case": bounded_regex.IGNORECASE,
    "multiline": bounded_regex.MULTILINE,
    "dotall": bounded_regex.DOTALL,
    "verbose": bounded_regex.VERBOSE,
}
_PAGE_MARKER_RE = re.compile(
    r"<span\b[^>]*\bid=[\"']doc-page-(\d+)[\"'][^>]*>", re.IGNORECASE
)
MAX_SERIALIZED_RESULT_BYTES = 1024 * 1024
MAX_TEXT_EXCERPT_CHARS = 1000
MAX_SOURCE_EXCERPT_CHARS = 240
# scholarly_review deliberately keeps one explicit block-ID scope bounded.  A
# document-native read may cover more blocks, but each projection invocation
# must stay within that independent service limit.
_MAX_PROJECTION_SCOPE_BLOCKS = 512

_ALLOWED_FIELDS = ("source", "target", "glossary", "annotations", "research")
_FIELD_ORDER = {field: index for index, field in enumerate(_ALLOWED_FIELDS)}
_HEADING_ROLES = frozenset({"document_title", "heading"})
_FORBIDDEN_KEY_PARTS = frozenset(
    {
        "hash",
        "locator",
        "receipt",
        "packet",
        "prompt",
        "provider",
        "metric",
        "dormant",
        "chain",
        "token_id",
        "protected_inline",
        "source_reference",
        "settings",
    }
)
_PROPOSAL_KEY_PARTS = frozenset(
    {
        "proposal",
        "proposed",
        "draft",
        "pending",
        "needs_review",
    }
)


class DocumentReviewError(ValueError):
    """Stable, safe failure at the document review boundary."""

    def __init__(self, code: str, *, latest: Mapping[str, Any] | None = None):
        self.code = code
        self.latest = dict(latest) if latest is not None else None
        super().__init__(code)


def _error(
    code: str, *, latest: Mapping[str, Any] | None = None
) -> DocumentReviewError:
    if not isinstance(code, str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9._-]{0,95}", code
    ):
        code = "review-invalid"
    return DocumentReviewError(code, latest=latest)


def _json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise _error("review-result-invalid") from exc


def _bounded_result(value: dict[str, Any]) -> dict[str, Any]:
    if len(_json_bytes(value)) > MAX_SERIALIZED_RESULT_BYTES:
        raise _error("review-result-too-large")
    return value


def _safe_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.casefold()
    return not any(part in lowered for part in _FORBIDDEN_KEY_PARTS)


def _safe_text(value: object, *, maximum: int | None = None) -> str:
    if not isinstance(value, str):
        return ""
    value = value.replace("\x00", "")
    if maximum is not None:
        return value[:maximum]
    return value


def _excerpt(value: object, maximum: int = MAX_SOURCE_EXCERPT_CHARS) -> str:
    text = _safe_text(value)
    if len(text) <= maximum:
        return text
    return text[: max(0, maximum - 1)] + "…"


def _safe_timestamp(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value[:128]
    try:
        return utc_iso(cast(datetime, value))
    except AttributeError, TypeError, ValueError, OverflowError:
        return None


def _safe_identity(project: Any) -> dict[str, Any]:
    return {
        "id": _safe_text(getattr(project, "id", None), maximum=256),
        "name": _excerpt(getattr(project, "name", ""), 255),
        "source_lang": _safe_text(getattr(project, "source_lang", ""), maximum=64),
        "target_lang": _safe_text(getattr(project, "target_lang", ""), maximum=64),
        "engine_contract": "document-block-v1",
    }


def _validate_limit(value: object, maximum: int, code: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise _error(code)
    return value


def _validate_context_bound(value: object, code: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= MAX_CONTEXT_BLOCKS
    ):
        raise _error(code)
    return value


def _canonical_json(value: object) -> str:
    return _json_bytes(value).decode("utf-8")


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _encode_cursor(offset: int, fingerprint: str) -> str:
    payload = {"offset": offset, "fingerprint": fingerprint}
    encoded = base64.urlsafe_b64encode(_json_bytes(payload)).decode("ascii").rstrip("=")
    return encoded


def _decode_cursor(cursor: object, fingerprint: str) -> int:
    if cursor is None:
        return 0
    if not isinstance(cursor, str) or not cursor or len(cursor) > 512:
        raise _error("review-cursor-invalid")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", cursor):
        raise _error("review-cursor-invalid")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        value = json.loads(raw.decode("utf-8"))
    except (
        ValueError,
        TypeError,
        UnicodeError,
        binascii.Error,
        json.JSONDecodeError,
    ) as exc:
        raise _error("review-cursor-invalid") from exc
    if (
        not isinstance(value, Mapping)
        or set(value) != {"offset", "fingerprint"}
        or isinstance(value.get("offset"), bool)
        or not isinstance(value.get("offset"), int)
        or value["offset"] < 0
        or value.get("fingerprint") != fingerprint
    ):
        raise _error("review-cursor-mismatch")
    return int(value["offset"])


def _pagination(
    *, offset: int, limit: int, total: int, next_offset: int | None, fingerprint: str
) -> dict[str, Any]:
    return {
        "offset": offset,
        "limit": limit,
        "total": total,
        "has_more": next_offset is not None,
        "next_cursor": _encode_cursor(next_offset, fingerprint)
        if next_offset is not None
        else None,
    }


class _VisibleTextParser(html.parser.HTMLParser):
    """Strip safe editor markup while retaining its visible character data."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if (
            tag.casefold() in {"br", "p", "div", "li", "tr"}
            and self.parts
            and not self.parts[-1].endswith("\n")
        ):
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() == "br":
            self.parts.append("\n")


def _editor_text(editor: object) -> str:
    if not isinstance(editor, Mapping):
        return ""
    content = editor.get("content")
    parser = _VisibleTextParser()
    if isinstance(content, str):
        try:
            parser.feed(content)
            parser.close()
        except AssertionError, ValueError:
            # The compact projection was already verified by its owner.  A
            # malformed replacement must not make this service return markup.
            return ""
    visible = "".join(parser.parts)
    fields = editor.get("fields")
    if isinstance(fields, Sequence) and not isinstance(fields, (str, bytes)):
        values = [
            _safe_text(item.get("value"))
            for item in fields
            if isinstance(item, Mapping) and isinstance(item.get("value"), str)
        ]
        if values:
            visible = "\n".join(part for part in (visible, *values) if part)
    return visible


def _normal(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _is_hangul_jamo(value: str) -> bool:
    codepoint = ord(value)
    return (
        0x1100 <= codepoint <= 0x11FF
        or 0xA960 <= codepoint <= 0xA97F
        or 0xD7B0 <= codepoint <= 0xD7FF
    )


def _iter_literal_matches(
    value: str, query: str
) -> Iterable[tuple[int, int, str]]:
    """Find literal matches and map normalized offsets to source offsets.

    Normalizing one grapheme-like base/combining-mark cluster at a time keeps
    the mapping linear while preserving the important NFKC contractions (for
    example ``A`` plus a combining ring or a Hangul Jamo sequence) and
    compatibility expansions (for example a ligature or ``ß`` casefolding to
    two letters).
    """

    normalized_parts: list[str] = []
    normalized_spans: list[tuple[int, int]] = []
    cluster_start = 0
    index = 0
    while index < len(value):
        index += 1
        while index < len(value) and (
            unicodedata.combining(value[index]) != 0 or _is_hangul_jamo(value[index])
        ):
            index += 1
        cluster = value[cluster_start:index]
        normalized_cluster = _normal(cluster)
        normalized_parts.append(normalized_cluster)
        normalized_spans.extend((cluster_start, index) for _ in normalized_cluster)
        cluster_start = index
    normalized = "".join(normalized_parts)
    normalized_query = _normal(query)
    if not normalized_query:
        raise _error("review-query-empty")
    seen: set[tuple[int, int]] = set()
    cursor = 0
    while True:
        found = normalized.find(normalized_query, cursor)
        if found < 0:
            break
        normalized_end = found + len(normalized_query)
        span_parts = normalized_spans[found:normalized_end]
        if not span_parts:
            break
        start = min(part[0] for part in span_parts)
        end = max(part[1] for part in span_parts)
        identity = (start, end)
        if identity not in seen:
            seen.add(identity)
            yield start, end, value[start:end]
        cursor = max(found + 1, normalized_end)


def _regex_pattern(query: str, flags: Sequence[str]) -> bounded_regex.Pattern[str]:
    if isinstance(flags, (str, bytes)) or not isinstance(flags, Sequence):
        raise _error("review-regex-flags-invalid")
    canonical = tuple(dict.fromkeys(flags))
    if any(flag not in _ALLOWED_REGEX_FLAGS for flag in canonical):
        raise _error("review-regex-flags-invalid")
    compiled_flags = 0
    for flag in canonical:
        compiled_flags |= _REGEX_FLAGS[flag]
    try:
        return bounded_regex.compile(query, compiled_flags)
    except bounded_regex.error as exc:
        raise _error("review-regex-invalid") from exc


def _iter_regex_matches(
    value: str,
    pattern: bounded_regex.Pattern[str],
    *,
    timeout_seconds: float = MAX_REGEX_SECONDS,
) -> Iterable[tuple[int, int, str]]:
    if timeout_seconds <= 0:
        raise _error("review-regex-timeout")
    try:
        for match in pattern.finditer(value, timeout=timeout_seconds):
            start, end = match.span()
            if start == end:
                raise _error("review-regex-empty-match")
            yield start, end, value[start:end]
    except TimeoutError as exc:
        raise _error("review-regex-timeout") from exc


def _literal_matches(value: str, query: str) -> list[tuple[int, int, str]]:
    """Compatibility wrapper for callers which need the complete match list."""

    return list(_iter_literal_matches(value, query))


def _excerpt_match(value: str, start: int, end: int) -> tuple[str, int, int]:
    length = end - start
    if len(value) <= MAX_TEXT_EXCERPT_CHARS:
        return value, start, end
    if length >= MAX_TEXT_EXCERPT_CHARS:
        excerpt_start = start
        excerpt_end = min(len(value), start + MAX_TEXT_EXCERPT_CHARS)
    else:
        room = MAX_TEXT_EXCERPT_CHARS - length
        excerpt_start = max(0, start - room // 2)
        excerpt_end = min(len(value), excerpt_start + MAX_TEXT_EXCERPT_CHARS)
        excerpt_start = max(0, excerpt_end - MAX_TEXT_EXCERPT_CHARS)
    return value[excerpt_start:excerpt_end], start - excerpt_start, end - excerpt_start


def _heading_level(workspace_plan: WorkspacePlan, block: Any) -> int:
    if getattr(block, "role", None) == "document_title":
        return 0
    snapshot = getattr(workspace_plan.plan, "snapshot", None)
    html = getattr(snapshot, "output_html", "")
    start, end = (
        getattr(block, "source_start", None),
        getattr(block, "source_end", None),
    )
    if isinstance(html, str) and isinstance(start, int) and isinstance(end, int):
        fragment = html[start:end]
        match = re.search(r"<h([1-6])(?:\s|>)", fragment, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return 1


def _heading_paths(workspace_plan: WorkspacePlan) -> dict[str, list[str]]:
    paths: dict[str, list[str]] = {}
    stack: list[str] = []
    for block in workspace_plan.plan.blocks:
        role = getattr(block, "role", "")
        text = _excerpt(getattr(block, "projection", ""), MAX_SOURCE_EXCERPT_CHARS)
        if role in _HEADING_ROLES:
            level = _heading_level(workspace_plan, block)
            if level == 0:
                stack = [text]
            else:
                # A document title is the root of the human-facing path.  The
                # HTML heading level is therefore relative to the remaining
                # stack, while a document without a title uses h1 as root.
                title_root = bool(
                    stack
                    and getattr(block, "role", "") != "document_title"
                    and stack[0]
                )
                keep = level if title_root else max(0, level - 1)
                stack = stack[:keep] + [text]
        paths[block.id] = list(stack)
    return paths


def _call_projection(
    name: str, project: Any, plan: WorkspacePlan, *extra: object
) -> object:
    """Call an optional scholarly-review read projection by parameter names.

    The scholarly-review service is being integrated alongside this module.
    Parameter-name routing keeps this boundary compatible with the documented
    ``project``/``source_plan_id`` APIs without importing its implementation at
    module import time.
    """

    function_names = {
        "get": "get_dossier",
        "list": "list_dossiers",
        "list_block_reviews": "list_block_reviews",
        "list_reader_notes": "list_reader_notes",
    }
    function = globals().get(function_names.get(name, name))
    if not callable(function):
        try:
            module = import_module(".scholarly_review", __package__)
            function = getattr(module, name, None)
            if function is None and name == "list":
                function = getattr(module, "list_research_dossiers", None)
            if function is None and name == "get":
                function = getattr(module, "get_research_dossier", None)
            if function is None:
                return None
        except ImportError, AttributeError:
            return None
    try:
        signature = inspect.signature(function)
    except TypeError, ValueError:
        signature = None
    if signature is not None:
        kwargs: dict[str, object] = {}
        values = {
            "project": project,
            "source_plan": plan,
            "source_plan_id": plan.record.id,
            "plan_id": plan.record.id,
            "block_ids": extra[0] if extra else None,
            "_source_plan": plan.record,
            "_blocks": list(plan.plan.blocks),
            "dossier_id": extra[0] if extra else None,
            # ``extra`` is the dossier id for the documented get projection;
            # passing it as a revision id would make a valid dossier lookup
            # fail its revision-owner check.  Revision selection is not part
            # of this read boundary.
            "revision_id": None,
            "id": extra[0] if extra else None,
            "include_payload": True,
            "include_proposed": True,
            "include_retired": False,
            "actor": None,
        }
        missing = False
        for parameter in signature.parameters.values():
            if parameter.kind in {parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD}:
                continue
            if parameter.name in values:
                kwargs[parameter.name] = values[parameter.name]
            elif parameter.default is parameter.empty:
                missing = True
                break
        if not missing:
            try:
                return function(**kwargs)
            except TypeError:
                pass
    candidates = [(project, plan.record.id), (project, plan), (project,)]
    if extra:
        candidates.insert(0, (project, extra[0]))
        candidates.insert(1, (project, plan.record.id, extra[0]))
    for args in candidates:
        try:
            return function(*args)
        except TypeError:
            continue
    return None


def _projection_scope_chunks(block_ids: Sequence[str]) -> Iterable[list[str]]:
    for start in range(0, len(block_ids), _MAX_PROJECTION_SCOPE_BLOCKS):
        yield list(block_ids[start : start + _MAX_PROJECTION_SCOPE_BLOCKS])


def _projection_items(value: object) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        nested = value.get("items")
        if nested is None:
            for key in ("notes", "reviews", "dossiers", "results"):
                if isinstance(value.get(key), Sequence) and not isinstance(
                    value.get(key), (str, bytes)
                ):
                    nested = value[key]
                    break
        if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes)):
            return [
                cast(Mapping[str, Any], item)
                for item in nested
                if isinstance(item, Mapping)
            ]
        # A block-id keyed projection is a useful internal convenience.
        if all(
            isinstance(key, str) and isinstance(item, Mapping)
            for key, item in value.items()
        ):
            return [dict(item, block_id=key) for key, item in value.items()]
        return [value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [
            cast(Mapping[str, Any], item) for item in value if isinstance(item, Mapping)
        ]
    return []


def _belongs_to_workspace(
    row: Mapping[str, Any],
    project: Any,
    plan: WorkspacePlan,
    *,
    require_identity: bool = False,
) -> bool:
    """Reject a projection row that claims a different project or source plan."""

    project_id = row.get("project_id")
    source_plan_id = row.get("source_plan_id")
    if require_identity and (project_id is None or source_plan_id is None):
        return False
    if project_id is not None and str(project_id) != str(getattr(project, "id", "")):
        return False
    return source_plan_id is None or str(source_plan_id) == str(plan.record.id)


def _note_projection(
    project: Any, plan: WorkspacePlan, block_ids: Sequence[str]
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {block_id: [] for block_id in block_ids}
    rows: list[Mapping[str, Any]] = []
    for chunk in _projection_scope_chunks(block_ids):
        try:
            rows.extend(
                _projection_items(
                    _call_projection("list_reader_notes", project, plan, chunk)
                )
            )
        except Exception as exc:
            raise _error(getattr(exc, "code", "review-notes-invalid")) from exc
    for row in rows:
        if not _belongs_to_workspace(row, project, plan):
            continue
        block_id = row.get("block_id") or row.get("source_block_id")
        if not isinstance(block_id, str) or block_id not in grouped:
            continue
        state = row.get("state", row.get("status", row.get("revision_state", "active")))
        if row.get("proposed") is True or row.get("is_proposed") is True:
            state = "proposed"
        if not isinstance(state, str):
            state = "active"
        safe_state = (
            state
            if state in {"active", "needs_review", "proposed", "retired"}
            else "active"
        )
        if safe_state == "retired":
            continue
        # Use the same canonical identity, edit fence and complete body as note
        # mutations. An excerpt is unsafe input for a subsequent correction.
        note = scholarly_wire.reader_note({**row, "state": safe_state})
        if not note["id"] or note["version"] < 1:
            raise _error("review-note-version-invalid")
        # Retain the original flat reading fields for existing review consumers.
        note["anchor_text"] = note["anchor"]["text"] or ""
        note["author_type"] = note["author"]["type"]
        created = _safe_timestamp(row.get("created_at"))
        if created:
            note["created_at"] = created
        if safe_state != "proposed" or note["id"]:
            grouped[block_id].append(note)
    return grouped


def _review_projection(
    project: Any, plan: WorkspacePlan, block_ids: Sequence[str]
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    rows: list[Mapping[str, Any]] = []
    for chunk in _projection_scope_chunks(block_ids):
        try:
            rows.extend(
                _projection_items(
                    _call_projection("list_block_reviews", project, plan, chunk)
                )
            )
        except Exception as exc:
            raise _error(getattr(exc, "code", "review-block-reviews-invalid")) from exc
    for row in rows:
        if not _belongs_to_workspace(row, project, plan):
            continue
        block_id = row.get("block_id") or row.get("source_block_id")
        if not isinstance(block_id, str) or block_id not in block_ids:
            continue
        decision = row.get("decision", row.get("review_decision", row.get("state")))
        if not isinstance(decision, str):
            continue
        item: dict[str, Any] = {"decision": _safe_text(decision, maximum=64)}
        effective_decision = row.get("effective_decision")
        if isinstance(effective_decision, str):
            item["effective_decision"] = _safe_text(effective_decision, maximum=64)
        stale = row.get("stale")
        if isinstance(stale, bool):
            item["stale"] = stale
        for field in (
            "id",
            "revision_id",
            "project_id",
            "source_plan_id",
            "block_id",
            "target_revision_id",
            "current_target_revision_id",
            "author_type",
            "share_grant_id",
        ):
            value = row.get(field)
            if isinstance(value, str):
                item[field] = _safe_text(value, maximum=256)
            elif value is None and field in {
                "target_revision_id",
                "current_target_revision_id",
            }:
                item[field] = None
        current_target_version = row.get("current_target_version")
        if "current_target_version" in row and (
            current_target_version is None
            or (
                isinstance(current_target_version, int)
                and not isinstance(current_target_version, bool)
                and current_target_version >= 1
            )
        ):
            item["current_target_version"] = current_target_version
        author_user_id = row.get("author_user_id")
        if isinstance(author_user_id, (int, str)) and not isinstance(
            author_user_id, bool
        ):
            item["author_user_id"] = author_user_id
        version = row.get("version", row.get("review_version"))
        if isinstance(version, int) and not isinstance(version, bool) and version >= 1:
            item["version"] = version
        when = _safe_timestamp(row.get("updated_at", row.get("created_at")))
        if when:
            item["updated_at"] = when
        grouped[block_id] = item
    return grouped


def _dossier_rows(project: Any, plan: WorkspacePlan) -> list[Mapping[str, Any]]:
    try:
        return [
            row
            for row in _projection_items(_call_projection("list", project, plan))
            if _belongs_to_workspace(row, project, plan, require_identity=True)
        ]
    except Exception as exc:
        raise _error(getattr(exc, "code", "review-dossier-invalid")) from exc


def _safe_provenance(value: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, maximum in (("method", 512), ("notes", 512)):
        item = value.get(key)
        if isinstance(item, str) and _safe_key(key):
            result[key] = _excerpt(item, maximum)
    for key in ("models", "agents"):
        items = value.get(key)
        if isinstance(items, Sequence) and not isinstance(items, (str, bytes)):
            result[key] = [
                _excerpt(item, 128) for item in items if isinstance(item, str)
            ][:16]
    return result


def _dossier_summary(
    project: Any, plan: WorkspacePlan
) -> tuple[dict[str, Any] | None, Mapping[str, Any] | None]:
    rows = _dossier_rows(project, plan)
    if not rows:
        return None, None
    rows.sort(
        key=lambda row: (
            row.get("version", 0) if isinstance(row.get("version"), int) else 0,
            _safe_text(row.get("created_at")),
            _safe_text(row.get("id")),
        ),
        reverse=True,
    )
    summary_row = rows[0]
    dossier_id = summary_row.get("id", summary_row.get("dossier_id"))
    full: Mapping[str, Any] = summary_row
    if isinstance(dossier_id, str) and dossier_id:
        try:
            fetched = _call_projection("get", project, plan, dossier_id)
        except Exception as exc:
            raise _error(getattr(exc, "code", "review-dossier-invalid")) from exc
        fetched_items = _projection_items(fetched)
        if fetched_items:
            fetched_row = fetched_items[0]
            if not _belongs_to_workspace(
                fetched_row, project, plan, require_identity=True
            ):
                raise _error("review-dossier-invalid")
            full = fetched_row
    summary: dict[str, Any] = {}
    for key in (
        "id",
        "version",
        "scope",
        "provenance",
        "status",
        "conclusion_provenance",
        "evidence_verification_state",
    ):
        value = full.get(key, full.get("dossier_id") if key == "id" else None)
        if key == "provenance" and isinstance(value, Mapping):
            summary[key] = _safe_provenance(value)
        elif key == "scope" and isinstance(value, Mapping):
            scope = (
                {"kind": value.get("kind")}
                if isinstance(value.get("kind"), str)
                else {}
            )
            block_ids = value.get("block_ids")
            if isinstance(block_ids, Sequence) and not isinstance(
                block_ids, (str, bytes)
            ):
                scope["block_ids"] = [
                    _safe_text(item, maximum=128)
                    for item in block_ids
                    if isinstance(item, str)
                ][:MAX_FETCH_BLOCKS]
            summary[key] = scope
        elif key in {"provenance", "status"} and isinstance(value, str):
            summary[key] = _excerpt(value, 128)
        elif key in {"id", "scope", "status"} and isinstance(value, str):
            summary[key] = _excerpt(value, 256)
        elif key in {
            "conclusion_provenance",
            "evidence_verification_state",
        } and isinstance(value, str):
            summary[key] = _excerpt(value, 64)
        elif (
            key == "version" and isinstance(value, int) and not isinstance(value, bool)
        ):
            summary[key] = value
    created = _safe_timestamp(full.get("created_at", full.get("submitted_at")))
    if created:
        summary["submitted_at"] = created
    if summary:
        payload = full.get("payload")
        return summary, payload if isinstance(payload, Mapping) else None
    return None, None


def _contains_rationale(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if isinstance(key, str) and any(
                marker in key.casefold()
                for marker in ("rationale", "justification", "reason", "explanation")
            ):
                if isinstance(child, str) and child.strip():
                    return True
                if isinstance(child, Mapping | Sequence) and _contains_rationale(child):
                    return True
            elif _contains_rationale(child):
                return True
    elif isinstance(value, str):
        return bool(
            re.search(
                r"\b(?:rationale|because|due to|in order to preserve)\b",
                value,
                re.IGNORECASE,
            )
        )
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_contains_rationale(child) for child in value)
    return False


def _dossier_scope_covers(
    summary: Mapping[str, Any] | None, block_ids: set[str]
) -> bool:
    """Return whether a selected dossier can explain one of the requested blocks."""

    if not summary:
        return False
    scope = summary.get("scope")
    if not isinstance(scope, Mapping):
        return True
    kind = scope.get("kind")
    if kind in {"blocks", "block"}:
        scoped_ids = scope.get("block_ids")
        return isinstance(scoped_ids, Sequence) and bool(
            block_ids.intersection(item for item in scoped_ids if isinstance(item, str))
        )
    return True


def _safe_dossier_texts(
    value: object,
    *,
    path: str = "",
    include_proposals: bool = False,
    inherited_refs: Sequence[str] = (),
) -> Iterable[tuple[str, str, list[str]]]:
    if isinstance(value, Mapping):
        refs: list[str] = list(inherited_refs)
        for key in ("source_block_ids", "block_ids", "referenced_source_block_ids"):
            raw = value.get(key)
            if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
                refs.extend(item for item in raw if isinstance(item, str))
        for key, child in value.items():
            if not _safe_key(key):
                continue
            normalized_key = key.casefold()
            if not include_proposals and (
                normalized_key == "terminology"
                or any(part in normalized_key for part in _PROPOSAL_KEY_PARTS)
            ):
                continue
            child_path = f"{path}.{key}" if path else key
            if isinstance(child, str) and child:
                yield child_path, _safe_text(child, maximum=4000), refs
            else:
                yield from _safe_dossier_texts(
                    child,
                    path=child_path,
                    include_proposals=include_proposals,
                    inherited_refs=refs,
                )
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, child in enumerate(value):
            yield from _safe_dossier_texts(
                child,
                path=f"{path}[{index}]",
                include_proposals=include_proposals,
                inherited_refs=inherited_refs,
            )


def _source_page_map(workspace_plan: WorkspacePlan) -> dict[str, list[int]]:
    """Resolve immutable source spans to physical PDF pages when markers exist."""

    snapshot = getattr(workspace_plan.plan, "snapshot", None)
    html = getattr(snapshot, "output_html", "")
    if not isinstance(html, str) or not html:
        return {block.id: [] for block in workspace_plan.plan.blocks}
    markers = [
        (match.start(), int(match.group(1)))
        for match in _PAGE_MARKER_RE.finditer(html)
        if int(match.group(1)) > 0
    ]
    if not markers:
        return {block.id: [] for block in workspace_plan.plan.blocks}
    positions = [position for position, _page in markers]
    result: dict[str, list[int]] = {}
    for block in workspace_plan.plan.blocks:
        start = max(0, int(getattr(block, "source_start", 0)))
        end = max(start, int(getattr(block, "source_end", start)))
        index = bisect_right(positions, start) - 1
        pages: list[int] = []
        if index >= 0:
            pages.append(markers[index][1])
        cursor = max(0, index + 1)
        while cursor < len(markers) and markers[cursor][0] < end:
            page = markers[cursor][1]
            if not pages or pages[-1] != page:
                pages.append(page)
            cursor += 1
        result[str(block.id)] = pages
    return result


@dataclass(slots=True)
class _ReviewContext:
    project: Any
    workspace_plan: WorkspacePlan
    heading_paths: dict[str, list[str]]
    source_pages: dict[str, list[int]]
    target_projections: dict[str, dict[str, Any]]
    target_revisions: dict[str, TranslationTargetBlockRevision]
    notes: dict[str, list[dict[str, Any]]]
    reviews: dict[str, dict[str, Any]]
    glossary: dict[str, list[dict[str, Any]]]
    dossier_summary: dict[str, Any] | None
    dossier_payload: Mapping[str, Any] | None
    include_alignment: bool


def _load_workspace_plan(project: Any) -> WorkspacePlan:
    try:
        return load_block_source_plan(project)
    except BlockWorkspaceIntegrityError as exc:
        raise _error(getattr(exc, "code", "review-source-invalid")) from exc
    except Exception as exc:
        raise _error("review-source-invalid") from exc


def _load_context(
    project: Any,
    *,
    workspace_plan: WorkspacePlan | None = None,
    block_ids: Sequence[str] | None = None,
    include_notes: bool = True,
    include_reviews: bool = True,
    include_glossary: bool = True,
    include_proposals: bool = False,
    include_research: bool = False,
    include_alignment: bool = False,
) -> _ReviewContext:
    if workspace_plan is None:
        workspace_plan = _load_workspace_plan(project)
    try:
        _pointers, target_projections = _rehydrate_workspace_objects(
            workspace_plan, include_alignment=include_alignment
        )
    except BlockWorkspaceIntegrityError as exc:
        raise _error(getattr(exc, "code", "review-source-invalid")) from exc
    except Exception as exc:
        raise _error("review-source-invalid") from exc
    blocks = list(workspace_plan.plan.blocks)
    selected_ids = (
        list(block_ids) if block_ids is not None else [block.id for block in blocks]
    )
    selected_id_set = set(selected_ids)
    notes: dict[str, list[dict[str, Any]]] = {block_id: [] for block_id in selected_ids}
    if include_notes:
        # Current reader-note heads are authoritative, including an empty set
        # after retirement. Immutable target-note rows are historical evidence;
        # their IDs cannot be edited with the reader-note tools.
        notes = _note_projection(project, workspace_plan, selected_ids)
    reviews = (
        _review_projection(project, workspace_plan, selected_ids)
        if include_reviews
        else {}
    )
    glossary: dict[str, list[dict[str, Any]]] = {
        block_id: [] for block_id in selected_ids
    }
    if include_glossary:
        try:
            glossary_payload = build_block_glossary_payload(project)
        except Exception as exc:
            raise _error("review-glossary-invalid") from exc
        entries = (
            glossary_payload.get("entries", [])
            if isinstance(glossary_payload, Mapping)
            else []
        )
        proposals = (
            glossary_payload.get("proposals", [])
            if isinstance(glossary_payload, Mapping)
            else []
        )
        all_entries = list(entries) if isinstance(entries, list) else []
        if include_proposals and isinstance(proposals, list):
            all_entries.extend(proposals)
        selected_blocks = (
            blocks
            if block_ids is None
            else [block for block in blocks if block.id in selected_id_set]
        )
        for block in selected_blocks:
            source_text = _source_text(block)
            for entry in all_entries:
                if isinstance(entry, Mapping) and _glossary_applies(
                    entry, block, source_text
                ):
                    glossary[block.id].append(_safe_glossary(entry))
    dossier_summary, dossier_payload = (
        _dossier_summary(project, workspace_plan) if include_research else (None, None)
    )
    revisions: dict[str, TranslationTargetBlockRevision] = {}
    pointers = TranslationTargetBlockActive.query.filter_by(
        source_plan_id=workspace_plan.record.id
    ).all()
    revision_ids = [pointer.revision_id for pointer in pointers]
    if revision_ids:
        rows = TranslationTargetBlockRevision.query.filter(
            TranslationTargetBlockRevision.source_plan_id == workspace_plan.record.id,
            TranslationTargetBlockRevision.id.in_(revision_ids),
        ).all()
        by_id = {row.id: row for row in rows}
        revisions = {
            pointer.block_id: by_id[pointer.revision_id]
            for pointer in pointers
            if pointer.revision_id in by_id
        }
    return _ReviewContext(
        project=project,
        workspace_plan=workspace_plan,
        heading_paths=_heading_paths(workspace_plan),
        source_pages=_source_page_map(workspace_plan),
        target_projections=target_projections,
        target_revisions=revisions,
        notes=notes,
        reviews=reviews,
        glossary=glossary,
        dossier_summary=dossier_summary,
        dossier_payload=dossier_payload,
        include_alignment=include_alignment,
    )


def _source_text(block: Any) -> str:
    try:
        return _editor_text(compact_source_projection(block))
    except Exception as exc:
        raise _error("review-source-projection") from exc


def _glossary_applies(entry: Mapping[str, Any], block: Any, source_text: str) -> bool:
    scope = entry.get("scope")
    if isinstance(scope, str):
        normalized = scope.casefold()
        if normalized in {"document", "project", "all", "global"}:
            return True
        if normalized in {
            str(block.id).casefold(),
            f"block:{block.id}".casefold(),
            f"block/{block.id}".casefold(),
        }:
            return True
    source_term = entry.get("source_term")
    return (
        isinstance(source_term, str)
        and bool(source_term)
        and next(iter(_iter_literal_matches(source_text, source_term)), None) is not None
    )


def _safe_glossary(row: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in (
        "concept_id",
        "proposal_id",
        "source_term",
        "target_term",
        "notes",
        "scope",
    ):
        value = row.get(key)
        if isinstance(value, str):
            result[key] = _excerpt(
                value, 4096 if key in {"source_term", "target_term"} else 2000
            )
    version = row.get("active_version", row.get("revision_number"))
    if isinstance(version, int) and not isinstance(version, bool) and version >= 1:
        result["active_version"] = version
    usage = row.get("usage")
    if isinstance(usage, Mapping):
        variants = usage.get("accepted_target_forms")
        if isinstance(variants, Sequence) and not isinstance(variants, (str, bytes)):
            result["accepted_target_forms"] = [
                _excerpt(item, 4096) for item in variants if isinstance(item, str)
            ][:128]
    direct_variants = row.get("accepted_target_forms")
    if isinstance(direct_variants, Sequence) and not isinstance(direct_variants, (str, bytes)):
        result["accepted_target_forms"] = [
            _excerpt(item, 4096) for item in direct_variants if isinstance(item, str)
        ][:128]
    timestamp = _safe_timestamp(row.get("updated_at", row.get("proposed_at")))
    if timestamp:
        result["updated_at"] = timestamp
    return result


def _block_item(
    context: _ReviewContext, block: Any, *, include_proposals: bool = False
) -> dict[str, Any]:
    target_projection = context.target_projections.get(block.id)
    target: dict[str, Any] | None = None
    if target_projection is not None:
        target = {
            "editor": target_projection.get("editor"),
            "active_version": target_projection.get("active_version"),
            "provenance": {},
        }
        revision = context.target_revisions.get(block.id)
        if revision is not None:
            target["provenance"] = {
                "author_type": _safe_text(revision.author_type, maximum=32),
                "created_at": _safe_timestamp(revision.created_at),
            }
        if context.include_alignment:
            alignment = target_projection.get("alignment")
            if isinstance(alignment, Mapping):
                target["alignment"] = dict(alignment)
    return {
        "id": block.id,
        "order": block.order,
        "role": block.role,
        "heading_path": list(context.heading_paths.get(block.id, [])),
        "source_pages": list(context.source_pages.get(block.id, [])),
        "source": {"editor": compact_source_projection(block)},
        "target": target,
        "reader_notes": [
            note
            for note in context.notes.get(block.id, [])
            if include_proposals or note.get("state") != "proposed"
        ],
        "glossary": context.glossary.get(block.id, []),
        "review": context.reviews.get(block.id),
    }


def _outline_item(context: _ReviewContext, block: Any) -> dict[str, Any]:
    notes = context.notes.get(block.id, [])
    # Outline is the least-privileged navigation surface. Dormant proposal
    # counts belong to the explicit include_proposals fetch path and must not
    # leak editorial activity to Viewer links.
    counts = {"active": 0, "needs_review": 0}
    for note in notes:
        state = note.get("state")
        if state in counts:
            counts[state] += 1
    target = context.target_projections.get(block.id)
    return {
        "id": block.id,
        "order": block.order,
        "role": block.role,
        "heading_path": list(context.heading_paths.get(block.id, [])),
        "source_pages": list(context.source_pages.get(block.id, [])),
        "source_excerpt": _excerpt(getattr(block, "projection", "")),
        "has_target": target is not None,
        "target_version": target.get("active_version") if target else None,
        "note_counts": counts,
        "review": context.reviews.get(block.id),
    }


def review_outline(
    project: Any, *, cursor: str | None = None, limit: int = 100
) -> dict[str, Any]:
    limit = _validate_limit(limit, MAX_FETCH_BLOCKS, "review-outline-limit")
    workspace_plan = _load_workspace_plan(project)
    blocks = list(workspace_plan.plan.blocks)
    fingerprint = _fingerprint(
        {
            "kind": "outline",
            "project_id": str(getattr(project, "id", "")),
            "source_plan_id": workspace_plan.record.id,
            "source_plan_version": workspace_plan.active_version,
        }
    )
    offset = _decode_cursor(cursor, fingerprint)
    page = blocks[offset : offset + limit]
    next_offset = offset + len(page) if offset + len(page) < len(blocks) else None
    context = _load_context(
        project,
        workspace_plan=workspace_plan,
        block_ids=[block.id for block in page],
        include_notes=True,
        include_reviews=True,
        include_glossary=False,
    )
    result = {
        "contract": REVIEW_CONTRACT,
        "project": _safe_identity(project),
        "source_plan": {
            "id": workspace_plan.record.id,
            "version": workspace_plan.active_version,
        },
        "items": [_outline_item(context, block) for block in page],
        "pagination": _pagination(
            offset=offset,
            limit=limit,
            total=len(blocks),
            next_offset=next_offset,
            fingerprint=fingerprint,
        ),
    }
    return _bounded_result(result)


def _select_blocks(
    workspace_plan: WorkspacePlan,
    block_ids: object,
    order_start: object,
    order_end: object,
) -> list[Any]:
    blocks = list(workspace_plan.plan.blocks)
    has_ids = block_ids is not None
    has_range = order_start is not None or order_end is not None
    if has_ids == has_range:
        raise _error("review-selector-required")
    if has_ids:
        if (
            isinstance(block_ids, (str, bytes))
            or not isinstance(block_ids, Sequence)
            or not block_ids
            or len(block_ids) > MAX_FETCH_BLOCKS
        ):
            raise _error("review-block-ids-invalid")
        ids = list(block_ids)
        if any(not isinstance(item, str) or not item for item in ids) or len(
            ids
        ) != len(set(ids)):
            raise _error("review-block-ids-invalid")
        by_id = {block.id: block for block in blocks}
        if any(item not in by_id for item in ids):
            raise _error("review-block-not-found")
        requested = {item for item in ids}
        return [block for block in blocks if block.id in requested]
    if (
        isinstance(order_start, bool)
        or not isinstance(order_start, int)
        or isinstance(order_end, bool)
        or not isinstance(order_end, int)
    ):
        raise _error("review-range-invalid")
    if order_start < 1 or order_end < order_start or order_end > len(blocks):
        raise _error("review-range-invalid")
    return blocks[order_start - 1 : order_end]


def _review_fetch_state_snapshot(
    project: Any, workspace_plan: WorkspacePlan, selected: Sequence[Any]
) -> list[dict[str, Any]]:
    """Capture target and review-head fences for every selected block."""

    selected_ids = [str(block.id) for block in selected]
    targets: dict[str, TranslationTargetBlockActive] = {}
    reviews: dict[str, TranslationBlockReviewHead] = {}
    for chunk in _projection_scope_chunks(selected_ids):
        if not chunk:
            continue
        target_rows = (
            TranslationTargetBlockActive.query.filter_by(
                source_plan_id=workspace_plan.record.id
            )
            .filter(TranslationTargetBlockActive.block_id.in_(chunk))
            .all()
        )
        review_rows = (
            TranslationBlockReviewHead.query.filter_by(
                project_id=getattr(project, "id", None),
                source_plan_id=workspace_plan.record.id,
            )
            .filter(TranslationBlockReviewHead.block_id.in_(chunk))
            .all()
        )
        targets.update({str(row.block_id): row for row in target_rows})
        reviews.update({str(row.block_id): row for row in review_rows})
    return [
        {
            "block_id": block_id,
            "target_revision_id": (
                str(targets[block_id].revision_id) if block_id in targets else None
            ),
            "target_version": targets[block_id].version
            if block_id in targets
            else None,
            "review_revision_id": (
                str(reviews[block_id].revision_id) if block_id in reviews else None
            ),
            "review_version": reviews[block_id].version if block_id in reviews else 0,
        }
        for block_id in selected_ids
    ]


def review_fetch(
    project: Any,
    *,
    block_ids: Sequence[str] | None = None,
    order_start: int | None = None,
    order_end: int | None = None,
    cursor: str | None = None,
    limit: int = 50,
    include_research: bool = False,
    include_proposals: bool = False,
    include_alignment: bool = False,
) -> dict[str, Any]:
    limit = _validate_limit(limit, MAX_FETCH_BLOCKS, "review-fetch-limit")
    if any(
        not isinstance(flag, bool)
        for flag in (include_research, include_proposals, include_alignment)
    ):
        raise _error("review-flag-invalid")
    workspace_plan = _load_workspace_plan(project)
    selected = _select_blocks(workspace_plan, block_ids, order_start, order_end)
    selector = (
        {"block_ids": [block.id for block in selected]}
        if block_ids is not None
        else {"order_start": order_start, "order_end": order_end}
    )
    fingerprint = _fingerprint(
        {
            "kind": "fetch",
            "project_id": str(getattr(project, "id", "")),
            "source_plan_id": workspace_plan.record.id,
            "source_plan_version": workspace_plan.active_version,
            "selector": selector,
            "include_research": include_research,
            "include_proposals": include_proposals,
            "include_alignment": include_alignment,
            "target_review_state": _review_fetch_state_snapshot(
                project, workspace_plan, selected
            ),
        }
    )
    offset = _decode_cursor(cursor, fingerprint)
    page = selected[offset : offset + limit]
    next_offset = offset + len(page) if offset + len(page) < len(selected) else None
    context = _load_context(
        project,
        workspace_plan=workspace_plan,
        block_ids=[block.id for block in page],
        include_notes=True,
        include_reviews=True,
        include_glossary=True,
        include_proposals=include_proposals,
        include_research=include_research,
        include_alignment=include_alignment,
    )
    result: dict[str, Any] = {
        "contract": REVIEW_CONTRACT,
        "project": _safe_identity(project),
        "source_plan": {
            "id": context.workspace_plan.record.id,
            "version": context.workspace_plan.active_version,
        },
        "items": [
            _block_item(context, block, include_proposals=include_proposals)
            for block in page
        ],
        "pagination": _pagination(
            offset=offset,
            limit=limit,
            total=len(selected),
            next_offset=next_offset,
            fingerprint=fingerprint,
        ),
    }
    if include_research and context.dossier_summary is not None:
        result["research_dossier"] = context.dossier_summary
    return _bounded_result(result)


def review_source_pages(
    project: Any, *, block_ids: Sequence[str]
) -> dict[str, Any]:
    """Resolve selected Translator blocks to physical PDF pages."""

    workspace_plan = _load_workspace_plan(project)
    selected = _select_blocks(workspace_plan, block_ids, None, None)
    if len(selected) > 16:
        raise _error("review-source-pages-too-broad")
    page_map = _source_page_map(workspace_plan)
    block_pages = [
        {
            "block_id": block.id,
            "order": block.order,
            "page_numbers": list(page_map.get(block.id, [])),
        }
        for block in selected
    ]
    page_numbers = list(
        dict.fromkeys(
            page
            for item in block_pages
            for page in item["page_numbers"]
            if isinstance(page, int) and page > 0
        )
    )
    if not page_numbers:
        raise _error("review-source-pages-unavailable")
    if len(page_numbers) > 4:
        raise _error("review-source-pages-too-broad")
    document_id = getattr(project, "document_id", None)
    if not isinstance(document_id, str) or not document_id:
        raise _error("review-source-pages-unavailable")
    return {
        "contract": REVIEW_CONTRACT,
        "project": _safe_identity(project),
        "document_id": document_id,
        "page_numbering": "physical_one_based",
        "page_numbers": page_numbers,
        "blocks": block_pages,
    }


def _mini_projection(context: _ReviewContext, block: Any) -> dict[str, Any]:
    target_projection = context.target_projections.get(block.id)
    target_text = (
        _editor_text(target_projection.get("editor")) if target_projection else ""
    )
    return {
        "id": block.id,
        "order": block.order,
        "role": block.role,
        "source_pages": list(context.source_pages.get(block.id, [])),
        "source_excerpt": _excerpt(_source_text(block)),
        "target_excerpt": _excerpt(target_text),
    }


def _anchored_block(context: _ReviewContext, references: Iterable[str]) -> Any | None:
    by_id = {block.id: block for block in context.workspace_plan.plan.blocks}
    owned = [by_id[block_id] for block_id in references if block_id in by_id]
    return min(owned, key=lambda block: (block.order, block.id), default=None)


def _search_result(
    *,
    context: _ReviewContext,
    field: str,
    object_id: str,
    value: str,
    match_start: int,
    match_end: int,
    block: Any | None,
    item_order: int,
    before: int,
    after: int,
) -> dict[str, Any]:
    text, start, end = _excerpt_match(value, match_start, match_end)
    context_before: list[dict[str, Any]] = []
    context_after: list[dict[str, Any]] = []
    if block is not None:
        blocks = list(context.workspace_plan.plan.blocks)
        index = blocks.index(block)
        context_before = [
            _mini_projection(context, item)
            for item in blocks[max(0, index - before) : index]
        ]
        context_after = [
            _mini_projection(context, item)
            for item in blocks[index + 1 : index + 1 + after]
        ]
    return {
        "field": field,
        "block_id": block.id if block is not None else None,
        "order": block.order if block is not None else None,
        "role": block.role if block is not None else None,
        "span": [start, end],
        "text": text,
        "match": text[start:end],
        "object_id": object_id,
        "context": {
            "before": context_before,
            "anchor": _mini_projection(context, block) if block is not None else None,
            "after": context_after,
        },
        "_sort": (
            block.order if block is not None else 10**9,
            _FIELD_ORDER[field],
            item_order,
            match_start,
            object_id,
        ),
    }


def _direct_search_values(
    context: _ReviewContext, field: str, roles: set[str], include_proposals: bool
) -> Iterable[tuple[str, str, Any | None, int]]:
    for block in context.workspace_plan.plan.blocks:
        if roles and block.role not in roles:
            continue
        if field == "source":
            yield f"block:{block.id}:source", _source_text(block), block, 0
        elif field == "target":
            target = context.target_projections.get(block.id)
            if target:
                yield (
                    f"block:{block.id}:target",
                    _editor_text(target.get("editor")),
                    block,
                    0,
                )
        elif field == "glossary":
            for index, entry in enumerate(context.glossary.get(block.id, [])):
                if not include_proposals and any(
                    key in entry for key in ("proposal_id",)
                ):
                    continue
                variants = entry.get("accepted_target_forms", [])
                value = "\n".join(
                    [
                        str(entry.get(key, ""))
                        for key in ("source_term", "target_term", "notes")
                        if entry.get(key)
                    ]
                    + [str(item) for item in variants if isinstance(item, str)]
                )
                yield (
                    f"glossary:{entry.get('concept_id', entry.get('proposal_id', index))}",
                    value,
                    block,
                    index,
                )
        elif field == "annotations":
            for index, note in enumerate(context.notes.get(block.id, [])):
                if not include_proposals and note.get("state") == "proposed":
                    continue
                value = "\n".join(
                    str(note.get(key, ""))
                    for key in ("anchor_text", "body")
                    if note.get(key)
                )
                yield f"note:{note.get('id', index)}", value, block, index


def _research_values(
    context: _ReviewContext, roles: set[str], include_proposals: bool
) -> Iterable[tuple[str, str, Any | None, int]]:
    if context.dossier_payload is None:
        return
    for index, (path, value, references) in enumerate(
        _safe_dossier_texts(
            context.dossier_payload, include_proposals=include_proposals
        )
    ):
        block = _anchored_block(context, references)
        if block is not None and roles and block.role not in roles:
            continue
        if block is None and roles:
            continue
        dossier_id = (
            context.dossier_summary.get("id", "latest")
            if context.dossier_summary
            else "latest"
        )
        yield f"dossier:{dossier_id}:{path}", value, block, index


def review_search(
    project: Any,
    *,
    query: str,
    fields: Sequence[str] = ("source", "target"),
    roles: Sequence[str] = (),
    before: int = 0,
    after: int = 0,
    cursor: str | None = None,
    limit: int = 20,
    include_proposals: bool = False,
    mode: str = "literal",
    regex_flags: Sequence[str] = (),
) -> dict[str, Any]:
    if not isinstance(query, str) or len(query) > MAX_QUERY_CHARS:
        raise _error("review-query-invalid")
    if mode not in {"literal", "regex"}:
        raise _error("review-search-mode-invalid")
    normalized_query = _normal(query)
    if not normalized_query:
        raise _error("review-query-empty")
    pattern: bounded_regex.Pattern[str] | None = None
    canonical_regex_flags: tuple[str, ...] = ()
    if mode == "regex":
        if isinstance(regex_flags, (str, bytes)) or not isinstance(
            regex_flags, Sequence
        ):
            raise _error("review-regex-flags-invalid")
        canonical_regex_flags = tuple(dict.fromkeys(regex_flags))
        pattern = _regex_pattern(query, canonical_regex_flags)
    elif regex_flags:
        raise _error("review-regex-flags-invalid")
    if isinstance(fields, (str, bytes)) or not isinstance(fields, Sequence):
        raise _error("review-fields-invalid")
    if any(not isinstance(field, str) for field in fields):
        raise _error("review-fields-invalid")
    requested_fields = set(fields)
    if any(field not in _ALLOWED_FIELDS for field in requested_fields):
        raise _error("review-fields-invalid")
    canonical_fields = tuple(
        field for field in _ALLOWED_FIELDS if field in requested_fields
    )
    if not canonical_fields:
        raise _error("review-fields-invalid")
    if (
        isinstance(roles, (str, bytes))
        or not isinstance(roles, Sequence)
        or any(not isinstance(role, str) for role in roles)
    ):
        raise _error("review-roles-invalid")
    before = _validate_context_bound(before, "review-before-invalid")
    after = _validate_context_bound(after, "review-after-invalid")
    limit = _validate_limit(limit, MAX_SEARCH_LIMIT, "review-search-limit")
    workspace_plan = _load_workspace_plan(project)
    present_roles = {block.role for block in workspace_plan.plan.blocks}
    canonical_roles = tuple(dict.fromkeys(roles))
    if any(role not in present_roles for role in canonical_roles):
        raise _error("review-roles-invalid")
    context = _load_context(
        project,
        workspace_plan=workspace_plan,
        include_notes="annotations" in canonical_fields,
        include_reviews=False,
        include_glossary="glossary" in canonical_fields,
        include_proposals=include_proposals,
        include_research="research" in canonical_fields,
    )
    fingerprint = _fingerprint(
        {
            "kind": "search",
            "project_id": str(getattr(project, "id", "")),
            "source_plan_id": workspace_plan.record.id,
            "source_plan_version": workspace_plan.active_version,
            "query": normalized_query if mode == "literal" else query,
            "mode": mode,
            "regex_flags": canonical_regex_flags,
            "fields": canonical_fields,
            "roles": canonical_roles,
            "before": before,
            "after": after,
            "include_proposals": include_proposals,
        }
    )
    offset = _decode_cursor(cursor, fingerprint)
    values: list[tuple[str, str, Any | None, int, str]] = []
    for field in canonical_fields:
        source_values = (
            _research_values(context, set(canonical_roles), include_proposals)
            if field == "research"
            else _direct_search_values(
                context, field, set(canonical_roles), include_proposals
            )
        )
        for object_id, value, block, item_order in source_values:
            values.append((field, value, block, item_order, object_id))
    matches: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None, int, int, str]] = set()
    regex_deadline = (
        time.monotonic() + MAX_REGEX_TOTAL_SECONDS if pattern is not None else None
    )
    for field, value, block, item_order, object_id in values:
        if pattern is None:
            iterator = _iter_literal_matches(value, query)
        else:
            assert regex_deadline is not None
            remaining = regex_deadline - time.monotonic()
            if remaining <= 0:
                raise _error("review-regex-timeout")
            iterator = _iter_regex_matches(
                value,
                pattern,
                timeout_seconds=min(MAX_REGEX_SECONDS, remaining),
            )
        for match_start, match_end, _match in iterator:
            identity = (
                field,
                block.id if block is not None else None,
                match_start,
                match_end,
                object_id,
            )
            if identity in seen:
                continue
            seen.add(identity)
            if len(matches) >= MAX_SEARCH_MATCHES:
                raise _error("review-search-too-broad")
            matches.append(
                {
                    "field": field,
                    "object_id": object_id,
                    "value": value,
                    "match_start": match_start,
                    "match_end": match_end,
                    "block": block,
                    "item_order": item_order,
                    "_sort": (
                        block.order if block is not None else 10**9,
                        _FIELD_ORDER[field],
                        item_order,
                        match_start,
                        object_id,
                    ),
                }
            )
    matches.sort(key=lambda item: item["_sort"])
    page = [
        _search_result(
            context=context,
            field=item["field"],
            object_id=item["object_id"],
            value=item["value"],
            match_start=item["match_start"],
            match_end=item["match_end"],
            block=item["block"],
            item_order=item["item_order"],
            before=before,
            after=after,
        )
        for item in matches[offset : offset + limit]
    ]
    next_offset = offset + len(page) if offset + len(page) < len(matches) else None
    query_projection: dict[str, Any] = {"mode": mode, "text": query}
    if mode == "literal":
        query_projection["normalization_profile"] = NORMALIZATION_PROFILE
    else:
        query_projection["flags"] = list(canonical_regex_flags)
        query_projection["timeout_ms_per_value"] = int(MAX_REGEX_SECONDS * 1000)
        query_projection["timeout_ms_total"] = int(MAX_REGEX_TOTAL_SECONDS * 1000)
    result = {
        "contract": REVIEW_CONTRACT,
        "project": _safe_identity(project),
        "source_plan": {
            "id": context.workspace_plan.record.id,
            "version": context.workspace_plan.active_version,
        },
        "query": query_projection,
        "filters": {
            "fields": list(canonical_fields),
            "roles": list(canonical_roles),
            "context": {"before": before, "after": after},
        },
        "items": page,
        "pagination": _pagination(
            offset=offset,
            limit=limit,
            total=len(matches),
            next_offset=next_offset,
            fingerprint=fingerprint,
        ),
        "disclaimer": (
            "Absence means no literal match under the stated normalization; it is not semantic absence."
            if mode == "literal"
            else "Absence means no bounded regular-expression match under the stated flags; it is not semantic absence."
        ),
    }
    return _bounded_result(result)


def explanation_evidence(
    project: Any, *, block_ids: Sequence[str], question: str
) -> dict[str, Any]:
    if isinstance(question, str):
        if len(question) > MAX_QUERY_CHARS or not _normal(question):
            raise _error("review-question-invalid")
    else:
        raise _error("review-question-invalid")
    if (
        isinstance(block_ids, (str, bytes))
        or not isinstance(block_ids, Sequence)
        or not block_ids
    ):
        raise _error("review-block-ids-invalid")
    fetched = review_fetch(
        project, block_ids=block_ids, limit=MAX_SEARCH_LIMIT, include_research=True
    )
    results = review_search(
        project,
        query=question,
        fields=("source", "target", "glossary", "annotations", "research"),
        before=2,
        after=2,
        limit=MAX_SEARCH_LIMIT,
        include_proposals=False,
    )
    selected = set(block_ids)
    dossier_applies = _dossier_scope_covers(fetched.get("research_dossier"), selected)
    relevant = [
        item
        for item in results["items"]
        if item.get("block_id") in selected or item.get("block_id") is None
    ]
    visible_recorded = any(
        note.get("body") and note.get("state") == "active"
        for item in fetched.get("items", [])
        for note in item.get("reader_notes", [])
    ) or (
        dossier_applies
        and _contains_rationale(
            _load_context(project, include_research=True).dossier_payload
        )
    )
    return _bounded_result(
        {
            "contract": REVIEW_CONTRACT,
            "project": fetched.get("project"),
            "source_plan": fetched.get("source_plan"),
            "question": question,
            "historical_reason_status": "recorded"
            if visible_recorded
            else "reconstruction_required",
            "blocks": fetched.get("items", []),
            "search": relevant,
            "disclaimer": "This is a bounded evidence packet, not an answer or a chain-of-thought record.",
        }
    )


__all__ = [
    "MAX_CONTEXT_BLOCKS",
    "MAX_FETCH_BLOCKS",
    "MAX_QUERY_CHARS",
    "MAX_SEARCH_LIMIT",
    "MAX_SEARCH_MATCHES",
    "NORMALIZATION_PROFILE",
    "REVIEW_CONTRACT",
    "DocumentReviewError",
    "explanation_evidence",
    "review_fetch",
    "review_outline",
    "review_search",
    "review_source_pages",
]
