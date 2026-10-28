"""Closed validation contract for extraction feedback reports."""

from __future__ import annotations

import json
import unicodedata
from typing import Any, NoReturn
from urllib.parse import urlsplit

MAX_ISSUE_REPORTS_PER_PAGE = 20
MAX_ISSUE_REPORTS_BYTES = 64 * 1024
MAX_SUGGESTED_READINGS = 5
MAX_REFERENCES = 5

ISSUE_KINDS = frozenset(
    {
        "ambiguous_text",
        "name_check",
        "structure",
        "diagram",
        "source_damage",
        "other",
    }
)

_REPORT_KEYS = (
    "key",
    "kind",
    "excerpt",
    "reason",
    "suggested_readings",
    "references",
)
_REFERENCE_KEYS = ("url", "title", "note")
_KEY_PATTERN = r"[A-Za-z][A-Za-z0-9_-]{0,63}"
_SAFE_TEXT_PATTERN = r"^[^\x00-\x09\x0b-\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]*$"
_HTTP_URL_PATTERN = (
    r"^[Hh][Tt][Tt][Pp][Ss]?://[^\s\\/?#]+(?:[/?#][^\s\\]*)?$"
)


class IssueReportValidationError(ValueError):
    """Source-free validation failure with a stable machine-readable code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _invalid(code: str = "invalid_issue_report") -> NoReturn:
    raise IssueReportValidationError(code)


def _text(value: object, maximum: int, *, nonempty: bool = False) -> str:
    if not isinstance(value, str):
        _invalid()
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    if len(normalized) > maximum:
        _invalid("issue_report_limit")
    if nonempty and not normalized.strip():
        _invalid()
    if any(
        (unicodedata.category(character) == "Cc" and character != "\n")
        or unicodedata.category(character) == "Cs"
        for character in normalized
    ):
        _invalid()
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError:
        _invalid()
    return normalized


def _reference(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != set(_REFERENCE_KEYS):
        _invalid()
    url = _text(value["url"], 2048, nonempty=True)
    if "\\" in url or any(character.isspace() for character in url):
        _invalid()
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        _invalid()
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.netloc == ""
        or (port is not None and not 1 <= port <= 65535)
    ):
        _invalid()
    return {
        "url": url,
        "title": _text(value["title"], 300),
        "note": _text(value["note"], 1000),
    }


def validate_issue_reports(value: object) -> list[dict[str, Any]]:
    """Return a normalized report page or raise a source-free validation error."""

    if not isinstance(value, list):
        _invalid()
    if len(value) > MAX_ISSUE_REPORTS_PER_PAGE:
        _invalid("issue_reports_limit")

    reports: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for report in value:
        if not isinstance(report, dict) or set(report) != set(_REPORT_KEYS):
            _invalid()
        key = report["key"]
        if (
            not isinstance(key, str)
            or len(key) > 64
            or not key
            or not key[0].isascii()
            or not key[0].isalpha()
            or any(
                not (character.isascii() and (character.isalnum() or character in "_-"))
                for character in key[1:]
            )
        ):
            _invalid()
        if key in seen_keys:
            _invalid("duplicate_issue_key")
        seen_keys.add(key)

        kind = report["kind"]
        if not isinstance(kind, str) or kind not in ISSUE_KINDS:
            _invalid()
        excerpt = _text(report["excerpt"], 2000)
        reason = _text(report["reason"], 2000, nonempty=True)

        suggestions = report["suggested_readings"]
        if not isinstance(suggestions, list):
            _invalid()
        if len(suggestions) > MAX_SUGGESTED_READINGS:
            _invalid("issue_report_limit")
        normalized_suggestions = [_text(item, 500) for item in suggestions]

        references = report["references"]
        if not isinstance(references, list):
            _invalid()
        if len(references) > MAX_REFERENCES:
            _invalid("issue_report_limit")
        normalized_references = [_reference(item) for item in references]

        reports.append(
            {
                "key": key,
                "kind": kind,
                "excerpt": excerpt,
                "reason": reason,
                "suggested_readings": normalized_suggestions,
                "references": normalized_references,
            }
        )

    try:
        encoded = json.dumps(
            reports,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError):
        _invalid()
    if len(encoded) > MAX_ISSUE_REPORTS_BYTES:
        _invalid("issue_reports_too_large")
    return reports


def issue_reports_schema() -> dict[str, Any]:
    """Return the JSON Schema for one validated extraction issue page."""

    return {
        "type": "array",
        "maxItems": MAX_ISSUE_REPORTS_PER_PAGE,
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": list(_REPORT_KEYS),
            "properties": {
                "key": {"type": "string", "pattern": f"^{_KEY_PATTERN}$"},
                "kind": {"type": "string", "enum": sorted(ISSUE_KINDS)},
                "excerpt": {
                    "type": "string",
                    "maxLength": 2000,
                    "pattern": _SAFE_TEXT_PATTERN,
                },
                "reason": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 2000,
                    "pattern": _SAFE_TEXT_PATTERN,
                },
                "suggested_readings": {
                    "type": "array",
                    "maxItems": MAX_SUGGESTED_READINGS,
                    "items": {
                        "type": "string",
                        "maxLength": 500,
                        "pattern": _SAFE_TEXT_PATTERN,
                    },
                },
                "references": {
                    "type": "array",
                    "maxItems": MAX_REFERENCES,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": list(_REFERENCE_KEYS),
                        "properties": {
                            "url": {
                                "type": "string",
                                "maxLength": 2048,
                                "pattern": _HTTP_URL_PATTERN,
                            },
                            "title": {
                                "type": "string",
                                "maxLength": 300,
                                "pattern": _SAFE_TEXT_PATTERN,
                            },
                            "note": {
                                "type": "string",
                                "maxLength": 1000,
                                "pattern": _SAFE_TEXT_PATTERN,
                            },
                        },
                    },
                },
            },
        },
    }
