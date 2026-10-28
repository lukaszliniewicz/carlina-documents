"""Validation and projection for Translator semantic-export metadata.

The export metadata contract is deliberately separate from the document
processor's source metadata contract.  Export overrides are small, immutable
JSON values supplied at admission time; :func:`apply_export_metadata` then
projects those values onto the packager's canonical metadata shape without
touching source metadata or HTML.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from datetime import date
from typing import Any

EXPORT_METADATA_KEYS = frozenset(
    {
        "title",
        "subtitle",
        "description",
        "publisher",
        "publication_date",
        "rights",
        "journal_or_series",
        "volume",
        "authors",
        "translators",
        "editors",
        "subjects",
        "other_contributors",
        "identifiers",
        "cover_upload_path",
    }
)

MAX_EXPORT_METADATA_TITLE_LENGTH = 500
MAX_EXPORT_METADATA_TEXT_LENGTHS = {
    "subtitle": 500,
    "description": 4_000,
    "publisher": 500,
    "publication_date": 10,
    "rights": 4_000,
    "journal_or_series": 500,
    "volume": 100,
}
MAX_EXPORT_METADATA_NAMES = 50
MAX_EXPORT_METADATA_NAME_LENGTH = 300
MAX_EXPORT_METADATA_SUBJECTS = 64
MAX_EXPORT_METADATA_IDENTIFIER_COUNT = 20
MAX_EXPORT_METADATA_IDENTIFIER_VALUE_LENGTH = 500
MAX_EXPORT_METADATA_COVER_PATH_LENGTH = 1_000
MAX_EXPORT_METADATA_STRING_BYTES = 4_096
MAX_EXPORT_METADATA_JSON_BYTES = 8_192

_CONTRIBUTOR_ROLES = ("authors", "translators", "editors")
_SIMPLE_METADATA_FIELDS = (
    "title",
    "subtitle",
    "description",
    "publisher",
    "publication_date",
    "rights",
    "journal_or_series",
    "volume",
)
_SOURCE_EDITION_FIELDS = {
    "publisher",
    "publication_date",
    "rights",
    "isbn",
    "issn",
    "identifiers",
    "source",
    "description",
    "subjects",
    "subtitle",
    "journal_or_series",
    "volume",
    "issue",
}
_IDENTIFIER_SCHEMES = frozenset({"ISBN", "DOI", "URI", "OTHER"})
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_DESCRIPTION_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_MARC_ROLE_RE = re.compile(r"[a-z]{3}")
_PUBLICATION_DATE_RE = re.compile(r"\d{4}(?:-\d{2}(?:-\d{2})?)?")


class ExportMetadataError(ValueError):
    """Stable, machine-readable failure from the export metadata boundary."""

    def __init__(self, code: str):
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,95}", code):
            code = "export-metadata-invalid"
        self.code = code
        super().__init__(code)


def _clean_text(
    value: object,
    *,
    code: str,
    maximum: int,
    required: bool = False,
    allow_line_breaks: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ExportMetadataError(code)
    controls = _DESCRIPTION_CONTROL_RE if allow_line_breaks else _CONTROL_RE
    if controls.search(value):
        raise ExportMetadataError(code)
    cleaned = value.strip()
    if required and not cleaned:
        raise ExportMetadataError(code)
    if len(cleaned) > maximum:
        raise ExportMetadataError(code)
    if len(cleaned.encode("utf-8")) > MAX_EXPORT_METADATA_STRING_BYTES:
        raise ExportMetadataError(code)
    return cleaned


def _normalize_names(value: object, *, field: str) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_EXPORT_METADATA_NAMES:
        raise ExportMetadataError(f"export-metadata-{field}")
    return [
        _clean_text(
            name,
            code=f"export-metadata-{field}",
            maximum=MAX_EXPORT_METADATA_NAME_LENGTH,
            required=True,
        )
        for name in value
    ]


def _normalize_subjects(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_EXPORT_METADATA_SUBJECTS:
        raise ExportMetadataError("export-metadata-subjects")
    return [
        _clean_text(
            subject,
            code="export-metadata-subjects",
            maximum=MAX_EXPORT_METADATA_NAME_LENGTH,
            required=True,
        )
        for subject in value
    ]


def _normalize_other_contributors(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > MAX_EXPORT_METADATA_NAMES:
        raise ExportMetadataError("export-metadata-other-contributors")
    normalized: list[dict[str, str]] = []
    for entry in value:
        if not isinstance(entry, Mapping) or set(entry) != {"name", "role"}:
            raise ExportMetadataError("export-metadata-other-contributors")
        name = _clean_text(
            entry["name"],
            code="export-metadata-other-contributors",
            maximum=MAX_EXPORT_METADATA_NAME_LENGTH,
            required=True,
        )
        role = _clean_text(
            entry["role"],
            code="export-metadata-other-contributors",
            maximum=3,
            required=True,
        )
        if _MARC_ROLE_RE.fullmatch(role) is None:
            raise ExportMetadataError("export-metadata-other-contributors")
        normalized.append({"name": name, "role": role})
    return normalized


def _normalize_identifiers(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > MAX_EXPORT_METADATA_IDENTIFIER_COUNT:
        raise ExportMetadataError("export-metadata-identifiers")
    normalized: list[dict[str, str]] = []
    for entry in value:
        if not isinstance(entry, Mapping) or set(entry) != {"scheme", "value"}:
            raise ExportMetadataError("export-metadata-identifiers")
        scheme = entry["scheme"]
        if not isinstance(scheme, str) or scheme not in _IDENTIFIER_SCHEMES:
            raise ExportMetadataError("export-metadata-identifiers")
        identifier_value = _clean_text(
            entry["value"],
            code="export-metadata-identifiers",
            maximum=MAX_EXPORT_METADATA_IDENTIFIER_VALUE_LENGTH,
            required=True,
        )
        normalized.append({"scheme": scheme, "value": identifier_value})
    return normalized


def _normalize_publication_date(value: object) -> str:
    cleaned = _clean_text(
        value,
        code="export-metadata-publication-date",
        maximum=MAX_EXPORT_METADATA_TEXT_LENGTHS["publication_date"],
    )
    if not cleaned:
        return cleaned
    if _PUBLICATION_DATE_RE.fullmatch(cleaned) is None:
        raise ExportMetadataError("export-metadata-publication-date")
    parts = [int(part) for part in cleaned.split("-")]
    try:
        date(
            parts[0],
            parts[1] if len(parts) > 1 else 1,
            parts[2] if len(parts) > 2 else 1,
        )
    except ValueError as exc:
        raise ExportMetadataError("export-metadata-publication-date") from exc
    return cleaned


def _normalize_cover_upload_path(value: object) -> str:
    cleaned = _clean_text(
        value,
        code="export-metadata-cover-upload-path",
        maximum=MAX_EXPORT_METADATA_COVER_PATH_LENGTH,
    )
    if cleaned and (
        not cleaned.startswith("/uploads/")
        or "%" in cleaned
        or "\\" in cleaned
        or "?" in cleaned
        or "#" in cleaned
    ):
        raise ExportMetadataError("export-metadata-cover-upload-path")
    return cleaned


def normalize_export_metadata(value: object) -> dict[str, Any]:
    """Validate and copy optional per-export metadata overrides.

    An absent key inherits its source or saved value.  An explicitly supplied
    empty optional string/list clears that value for the export.  ``{}`` is
    therefore a valid no-op override.
    """

    if not isinstance(value, Mapping):
        raise ExportMetadataError("export-metadata-type")
    if not set(value).issubset(EXPORT_METADATA_KEYS):
        raise ExportMetadataError("export-metadata-key")

    normalized: dict[str, Any] = {}
    if "title" in value:
        normalized["title"] = _clean_text(
            value["title"],
            code="export-metadata-title",
            maximum=MAX_EXPORT_METADATA_TITLE_LENGTH,
            required=True,
        )

    for field, maximum in MAX_EXPORT_METADATA_TEXT_LENGTHS.items():
        if field not in value:
            continue
        if field == "publication_date":
            normalized[field] = _normalize_publication_date(value[field])
            continue
        normalized[field] = _clean_text(
            value[field],
            code=f"export-metadata-{field.replace('_', '-')}",
            maximum=maximum,
            allow_line_breaks=field in {"description", "rights"},
        )

    for field in _CONTRIBUTOR_ROLES:
        if field in value:
            normalized[field] = _normalize_names(value[field], field=field)
    if "subjects" in value:
        normalized["subjects"] = _normalize_subjects(value["subjects"])
    if "other_contributors" in value:
        normalized["other_contributors"] = _normalize_other_contributors(
            value["other_contributors"]
        )
    if "identifiers" in value:
        normalized["identifiers"] = _normalize_identifiers(value["identifiers"])
    if "cover_upload_path" in value:
        normalized["cover_upload_path"] = _normalize_cover_upload_path(
            value["cover_upload_path"]
        )
    try:
        normalized_size = len(
            json.dumps(
                normalized,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    except (
        TypeError,
        ValueError,
    ) as exc:  # pragma: no cover - all inputs are JSON-like
        raise ExportMetadataError("export-metadata-size") from exc
    if normalized_size > MAX_EXPORT_METADATA_JSON_BYTES:
        raise ExportMetadataError("export-metadata-size")
    return normalized


def _copy_value(value: object) -> object:
    """Copy JSON-like mappings recursively while tolerating mapping proxies."""

    if isinstance(value, Mapping):
        return {key: _copy_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_copy_value(item) for item in value]
    return copy.deepcopy(value)


def apply_export_metadata(
    source_metadata: Mapping[str, object],
    overrides: Mapping[str, object],
    target_language: str,
) -> dict[str, Any]:
    """Project normalized export metadata onto the EPUB packager shape.

    The source mapping is recursively copied before edition-specific fields and
    source-only contributor roles are removed. The original metadata's digest is
    a separate witness owned by the caller; this helper returns only packager
    metadata and never changes HTML.
    """

    if not isinstance(source_metadata, Mapping):
        raise ExportMetadataError("export-metadata-source")
    if not isinstance(target_language, str) or not target_language.strip():
        raise ExportMetadataError("export-metadata-language")
    if _CONTROL_RE.search(target_language):
        raise ExportMetadataError("export-metadata-language")
    if len(target_language.encode("utf-8")) > MAX_EXPORT_METADATA_STRING_BYTES:
        raise ExportMetadataError("export-metadata-language")
    normalized = normalize_export_metadata(overrides)
    metadata = _copy_value(source_metadata)
    if not isinstance(metadata, dict):  # pragma: no cover - defensive typing guard
        raise ExportMetadataError("export-metadata-source")

    for field in _SOURCE_EDITION_FIELDS:
        metadata.pop(field, None)
    metadata["language"] = target_language

    source_contributors = metadata.get("contributors")
    if isinstance(source_contributors, Mapping):
        contributors = {
            key: _copy_value(value) for key, value in source_contributors.items()
        }
        for role in ("translators", "editors", "other"):
            contributors.pop(role, None)
    else:
        contributors = {}
    contributor_override_supplied = False
    for role in _CONTRIBUTOR_ROLES:
        if role in normalized:
            contributors[role] = list(normalized[role])
            contributor_override_supplied = True
    if "authors" in normalized:
        metadata["author"] = ", ".join(normalized["authors"])
    if "other_contributors" in normalized:
        contributors["other"] = [
            {"name": entry["name"], "role": entry["role"]}
            for entry in normalized["other_contributors"]
        ]
        contributor_override_supplied = True
    if isinstance(source_contributors, Mapping) or contributor_override_supplied:
        metadata["contributors"] = contributors
    else:
        metadata.pop("contributors", None)

    for field in _SIMPLE_METADATA_FIELDS:
        if field in normalized:
            metadata[field] = normalized[field]
    for field in ("subjects", "identifiers"):
        if field in normalized:
            metadata[field] = list(normalized[field])
    # cover_upload_path is an admission/asset witness and intentionally never
    # becomes publication metadata.
    metadata.pop("cover_upload_path", None)
    return metadata


__all__ = [
    "EXPORT_METADATA_KEYS",
    "MAX_EXPORT_METADATA_COVER_PATH_LENGTH",
    "MAX_EXPORT_METADATA_IDENTIFIER_COUNT",
    "MAX_EXPORT_METADATA_IDENTIFIER_VALUE_LENGTH",
    "MAX_EXPORT_METADATA_JSON_BYTES",
    "MAX_EXPORT_METADATA_NAMES",
    "MAX_EXPORT_METADATA_NAME_LENGTH",
    "MAX_EXPORT_METADATA_STRING_BYTES",
    "MAX_EXPORT_METADATA_SUBJECTS",
    "MAX_EXPORT_METADATA_TEXT_LENGTHS",
    "MAX_EXPORT_METADATA_TITLE_LENGTH",
    "ExportMetadataError",
    "apply_export_metadata",
    "normalize_export_metadata",
]
