"""Closed, owner-neutral options and receipts for publishing EPUB projections.

Absent options identify the retained exporter. New admissions select a named
profile explicitly; retrying a retained operation never selects a newer default.
This boundary contains no rendering, add-on, filesystem or framework imports.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, cast

from .semantic_html import SAFE_CONTROL_RE, SAFE_IDENTIFIER_RE

EPUB_EXPORT_PROFILE = "publishing-v2"
EPUB_EXPORT_PROFILES = frozenset({"publishing-v1", EPUB_EXPORT_PROFILE})
EPUB_IMAGE_MODES = frozenset({"auto", "jpeg", "webp"})
EPUB_QUALITY_SCHEMA = "epub-quality-v1"
MAX_EPUB_OPTIONS_BYTES = 131_072
MAX_EPUB_REVIEW_ENTRIES = 512
MAX_EPUB_PAGE_LABELS = 2048
MAX_EPUB_QUALITY_ISSUES = 200
EPUB_SECTION_ROLES = frozenset(
    {
        "cover",
        "titlepage",
        "copyright-page",
        "toc",
        "contributors",
        "introduction",
        "part",
        "chapter",
        "endnotes",
        "appendix",
        "bibliography",
        "backmatter",
        "bodymatter",
    }
)
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_CODE = re.compile(r"[a-z][a-z0-9-]{0,95}\Z")


class EpubExportContractError(ValueError):
    """Source-free failure of an explicit export choice or report."""

    def __init__(self, code: str):
        self.code = code if _CODE.fullmatch(code) else "epub-options-invalid"
        super().__init__(self.code)


def normalize_epub_image_mode(value: object) -> str:
    """Validate the exact transport-independent image conversion choice."""
    if not isinstance(value, str) or value not in EPUB_IMAGE_MODES:
        raise EpubExportContractError("epub-image-mode-invalid")
    return value


def epub_json_value(value: object) -> Any:
    """Thaw a normalized JSON container without serializing private objects."""

    if isinstance(value, Mapping):
        return {
            str(key): epub_json_value(item)
            for key, item in cast(Mapping[object, object], value).items()
        }
    if isinstance(value, (list, tuple)):
        return [
            epub_json_value(item)
            for item in cast(list[object] | tuple[object, ...], value)
        ]
    return value


def freeze_epub_value(value: object) -> Any:
    """Freeze an already normalized, independently copied JSON value."""

    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                str(key): freeze_epub_value(item)
                for key, item in cast(Mapping[object, object], value).items()
            }
        )
    if isinstance(value, (list, tuple)):
        return tuple(
            freeze_epub_value(item)
            for item in cast(list[object] | tuple[object, ...], value)
        )
    return value


def epub_value_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            epub_json_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def epub_review_sha256(snapshot: Mapping[str, object]) -> str:
    """Bind a preflight to its normalized edition inputs, without retaining them."""
    return epub_value_sha256({"schema": "epub-review-v1", "snapshot": snapshot})


def semantic_epub_review_sha256(request: Mapping[str, object]) -> str:
    """Bind edition inputs while excluding the subsequently created delivery ID."""
    snapshot = {key: value for key, value in request.items() if key != "semantic_html"}
    metadata = snapshot.get("metadata")
    if isinstance(metadata, Mapping):
        snapshot["metadata"] = {
            key: value
            for key, value in cast(Mapping[str, object], metadata).items()
            if key not in {"operation-witness-json", "operation-witness-sha256"}
        }
    return epub_review_sha256(snapshot)


def _anchor(value: object) -> str:
    if not isinstance(value, str) or not SAFE_IDENTIFIER_RE.fullmatch(value):
        raise EpubExportContractError("epub-options-anchor")
    return value


def _text(value: object, maximum: int, code: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or SAFE_CONTROL_RE.search(value)
        or len(value.encode("utf-8")) > maximum
    ):
        raise EpubExportContractError(code)
    return value.strip()


def _paragraph_text(value: object) -> str:
    code = "epub-options-front-matter-edit"
    if not isinstance(value, str) or len(value.encode("utf-8")) > 4096:
        raise EpubExportContractError(code)
    return _text(
        value.replace("\r", " ").replace("\n", " ").replace("\t", " "), 4096, code
    )


def _review_mapping(
    value: object,
    *,
    maximum: int,
    choices: frozenset[str] | None = None,
    text_bytes: int = 4096,
) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise EpubExportContractError("epub-options-review")
    value = cast(Mapping[object, object], value)
    if len(value) > maximum:
        raise EpubExportContractError("epub-options-review")
    result: dict[str, str] = {}
    for anchor, raw in value.items():
        label = _text(raw, text_bytes, "epub-options-review")
        if choices is not None and label not in choices:
            raise EpubExportContractError("epub-options-review")
        result[_anchor(anchor)] = label
    return result


def normalize_epub_options(value: object) -> dict[str, object]:
    """Validate reviewed export choices; None retains the historical policy."""

    if value is None:
        return {}
    allowed = {
        "profile",
        "chapter_level",
        "note_placement",
        "group_endnotes",
        "direction",
        "roles",
        "page_labels",
        "table_headers",
        "image_descriptions",
        "decorative_images",
        "accessibility_evaluation",
        "image_policy",
        "photographic_images",
        "long_descriptions",
        "front_matter_order",
        "omitted_front_matter",
        "front_matter_edits",
        "front_matter_breaks",
    }
    if not isinstance(value, Mapping):
        raise EpubExportContractError("epub-options-keys")
    value = cast(Mapping[str, object], value)
    if not set(value) <= allowed:
        raise EpubExportContractError("epub-options-keys")
    profile = value.get("profile", EPUB_EXPORT_PROFILE)
    if not isinstance(profile, str) or profile not in EPUB_EXPORT_PROFILES:
        raise EpubExportContractError("epub-options-profile")
    if profile == "publishing-v1" and set(value) & {
        "image_policy",
        "photographic_images",
        "long_descriptions",
        "front_matter_order",
        "omitted_front_matter",
        "front_matter_edits",
        "front_matter_breaks",
    }:
        raise EpubExportContractError("epub-options-profile")
    level = value.get("chapter_level")
    if level is not None and (type(level) is not int or not 1 <= level <= 6):
        raise EpubExportContractError("epub-options-chapter-level")
    placement = value.get("note_placement", "source")
    direction = value.get("direction", "source")
    group = value.get("group_endnotes", True)
    if not isinstance(placement, str) or placement not in {
        "source",
        "chapter",
        "document",
    }:
        raise EpubExportContractError("epub-options-note-placement")
    if not isinstance(direction, str) or direction not in {"source", "ltr", "rtl"}:
        raise EpubExportContractError("epub-options-direction")
    if type(group) is not bool:
        raise EpubExportContractError("epub-options-group-endnotes")
    roles = _review_mapping(
        value.get("roles", {}),
        maximum=MAX_EPUB_REVIEW_ENTRIES,
        choices=EPUB_SECTION_ROLES,
    )
    pages = _review_mapping(
        value.get("page_labels", {}),
        maximum=MAX_EPUB_PAGE_LABELS,
        text_bytes=128,
    )
    tables = _review_mapping(
        value.get("table_headers", {}),
        maximum=MAX_EPUB_REVIEW_ENTRIES,
        choices=frozenset({"row", "column"}),
    )
    descriptions = _review_mapping(
        value.get("image_descriptions", {}),
        maximum=MAX_EPUB_REVIEW_ENTRIES,
    )
    raw_decorative = value.get("decorative_images", [])
    if not isinstance(raw_decorative, (tuple, list)):
        raise EpubExportContractError("epub-options-decorative-images")
    raw_decorative = cast(list[object] | tuple[object, ...], raw_decorative)
    if len(raw_decorative) > MAX_EPUB_REVIEW_ENTRIES:
        raise EpubExportContractError("epub-options-decorative-images")
    decorative = [_anchor(item) for item in raw_decorative]
    if len(set(decorative)) != len(decorative) or set(decorative) & set(descriptions):
        raise EpubExportContractError("epub-options-image-conflict")
    result: dict[str, object] = {
        "profile": profile,
        "chapter_level": level,
        "note_placement": placement,
        "group_endnotes": group,
        "direction": direction,
        "roles": roles,
        "page_labels": pages,
        "table_headers": tables,
        "image_descriptions": descriptions,
        "decorative_images": decorative,
        "accessibility_evaluation": _normalize_evaluation(
            value.get("accessibility_evaluation")
        ),
    }
    if profile == EPUB_EXPORT_PROFILE:
        policy = value.get("image_policy", "balanced")
        if not isinstance(policy, str) or policy not in {"balanced", "preserve"}:
            raise EpubExportContractError("epub-options-image-policy")
        order = _anchor_list(value.get("front_matter_order", []), maximum=64)
        photos = _anchor_list(value.get("photographic_images", []))
        long_descriptions = _review_mapping(
            value.get("long_descriptions", {}),
            maximum=MAX_EPUB_REVIEW_ENTRIES,
            text_bytes=64,
        )
        omitted = _review_mapping(
            value.get("omitted_front_matter", {}),
            maximum=MAX_EPUB_REVIEW_ENTRIES,
            text_bytes=64,
        )
        if any(
            not _HASH.fullmatch(digest)
            for digest in [*long_descriptions.values(), *omitted.values()]
        ):
            raise EpubExportContractError("epub-options-review-hash")
        edits_value = value.get("front_matter_edits", {})
        if (
            not isinstance(edits_value, Mapping)
            or len(cast(Mapping[str, object], edits_value)) > MAX_EPUB_REVIEW_ENTRIES
        ):
            raise EpubExportContractError("epub-options-front-matter-edit")
        edits: dict[str, object] = {}
        for identifier, raw_edit in cast(Mapping[str, object], edits_value).items():
            identifier = _anchor(identifier)
            if not isinstance(raw_edit, Mapping) or set(
                cast(Mapping[str, object], raw_edit)
            ) != {"sha256", "text"}:
                raise EpubExportContractError("epub-options-front-matter-edit")
            edit = cast(Mapping[str, object], raw_edit)
            digest = edit.get("sha256")
            if (
                not isinstance(digest, str)
                or not _HASH.fullmatch(digest)
                or identifier in omitted
            ):
                raise EpubExportContractError("epub-options-front-matter-edit")
            edits[identifier] = {
                "sha256": digest,
                "text": _paragraph_text(edit.get("text")),
            }
        breaks_value = value.get("front_matter_breaks", {})
        if (
            not isinstance(breaks_value, Mapping)
            or len(cast(Mapping[str, object], breaks_value)) > 64
        ):
            raise EpubExportContractError("epub-options-front-matter-break")
        breaks: dict[str, object] = {}
        for identifier, raw_break in cast(Mapping[str, object], breaks_value).items():
            identifier = _anchor(identifier)
            if not isinstance(raw_break, Mapping) or set(
                cast(Mapping[str, object], raw_break)
            ) != {"sha256", "role"}:
                raise EpubExportContractError("epub-options-front-matter-break")
            section = cast(Mapping[str, object], raw_break)
            digest, role = section.get("sha256"), section.get("role")
            if (
                not isinstance(digest, str)
                or not _HASH.fullmatch(digest)
                or not isinstance(role, str)
                or role not in {"titlepage", "copyright-page", "contributors"}
            ):
                raise EpubExportContractError("epub-options-front-matter-break")
            breaks[identifier] = {"sha256": digest, "role": role}
        result.update(
            {
                "image_policy": policy,
                "photographic_images": photos,
                "long_descriptions": long_descriptions,
                "front_matter_order": order,
                "omitted_front_matter": omitted,
                "front_matter_edits": edits,
                "front_matter_breaks": breaks,
            }
        )
    if (
        len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
        > MAX_EPUB_OPTIONS_BYTES
    ):
        raise EpubExportContractError("epub-options-size")
    return result


def _anchor_list(value: object, *, maximum: int = MAX_EPUB_REVIEW_ENTRIES) -> list[str]:
    if (
        not isinstance(value, (list, tuple))
        or len(cast(list[object] | tuple[object, ...], value)) > maximum
    ):
        raise EpubExportContractError("epub-options-review-list")
    anchors = [_anchor(item) for item in cast(list[object] | tuple[object, ...], value)]
    if len(anchors) != len(set(anchors)):
        raise EpubExportContractError("epub-options-review-list")
    return anchors


def normalize_epub_recipe(value: object) -> dict[str, object]:
    """A reusable edition recipe contains choices, never source or file paths."""
    from .epub_cover import normalize_epub_cover

    if not isinstance(value, Mapping) or set(cast(Mapping[str, object], value)) != {
        "epub_options",
        "epub_cover",
        "epub_image_mode",
    }:
        raise EpubExportContractError("epub-edition-contract")
    record = cast(Mapping[str, object], value)
    if not isinstance(record.get("epub_options"), Mapping):
        raise EpubExportContractError("epub-edition-contract")
    mode = normalize_epub_image_mode(record.get("epub_image_mode"))
    return {
        "epub_options": normalize_epub_options(record["epub_options"]),
        "epub_cover": normalize_epub_cover(record["epub_cover"]),
        "epub_image_mode": mode,
    }


def effective_epub_recipe(recipe: object, *, stale: bool) -> dict[str, object]:
    """Keep general preferences while retiring decisions about changed content."""
    normalized = normalize_epub_recipe(recipe)
    if not stale:
        return normalized
    options = cast(dict[str, object], normalized["epub_options"])
    for name in (
        "roles",
        "page_labels",
        "table_headers",
        "image_descriptions",
        "long_descriptions",
        "omitted_front_matter",
        "front_matter_edits",
        "front_matter_breaks",
    ):
        if name in options:
            options[name] = {}
    for name in ("decorative_images", "photographic_images", "front_matter_order"):
        if name in options:
            options[name] = []
    options["accessibility_evaluation"] = None
    cover = cast(dict[str, object], normalized["epub_cover"])
    if cover.get("mode") != "generated":
        normalized["epub_cover"] = {}
    return normalized


def epub_projection_options_sha256(options: Mapping[str, object]) -> str:
    """Bind evaluation to its presentation choices without self-reference."""

    return epub_value_sha256(
        {
            key: value
            for key, value in options.items()
            if key != "accessibility_evaluation"
        }
    )


def _normalize_evaluation(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise EpubExportContractError("epub-evaluation-contract")
    record = cast(Mapping[str, object], value)
    keys = {
        "content_sha256",
        "options_sha256",
        "evaluator",
        "summary",
        "sufficient_modes",
        "conforms_to",
        "credential",
        "hazards",
    }
    if set(record) != keys:
        raise EpubExportContractError("epub-evaluation-contract")
    result: dict[str, object] = {}
    for key in ("content_sha256", "options_sha256"):
        digest = record.get(key)
        if not isinstance(digest, str) or not _HASH.fullmatch(digest):
            raise EpubExportContractError("epub-evaluation-contract")
        result[key] = digest
    for key, maximum in (("evaluator", 512), ("summary", 4096)):
        result[key] = _text(record.get(key), maximum, "epub-evaluation-contract")
    credential = record.get("credential")
    result["credential"] = (
        None
        if credential is None
        else _text(credential, 512, "epub-evaluation-contract")
    )
    conformance = record.get("conforms_to")
    if conformance is not None and (
        not isinstance(conformance, str)
        or re.fullmatch(
            r"EPUB Accessibility 1\.1 - WCAG 2\.[012] Level A{1,3}", conformance
        )
        is None
    ):
        raise EpubExportContractError("epub-evaluation-contract")
    result["conforms_to"] = conformance
    for key, choices in (
        ("sufficient_modes", ("textual", "visual", "textual,visual")),
        ("hazards", ("none", "unknown", "flashing", "motionSimulation", "sound")),
    ):
        raw = record.get(key)
        if not isinstance(raw, (list, tuple)):
            raise EpubExportContractError("epub-evaluation-contract")
        items = cast(list[object] | tuple[object, ...], raw)
        if (
            not items
            or len(items) > 3
            or any(not isinstance(item, str) or item not in choices for item in items)
        ):
            raise EpubExportContractError("epub-evaluation-contract")
        values = cast(list[str], list(items))
        if len(set(values)) != len(values) or (
            key == "hazards"
            and len(values) > 1
            and ("none" in values or "unknown" in values)
        ):
            raise EpubExportContractError("epub-evaluation-contract")
        result[key] = values
    return result


def normalize_epub_quality(value: object) -> dict[str, object]:
    """Validate a source-free report at the cross-add-on receipt boundary."""

    keys = {
        "schema",
        "profile",
        "input_sha256",
        "options_sha256",
        "artifact_sha256",
        "content_sha256",
        "checks",
        "counts",
        "issues",
        "issue_count",
        "epubcheck_version",
    }
    if not isinstance(value, Mapping):
        raise EpubExportContractError("epub-quality-contract")
    value = cast(Mapping[str, object], value)
    if (
        not keys <= set(value) <= keys | {"ace_version", "image_bytes"}
        or value.get("schema") != EPUB_QUALITY_SCHEMA
        or not isinstance(value.get("profile"), str)
        or value.get("profile") not in EPUB_EXPORT_PROFILES
    ):
        raise EpubExportContractError("epub-quality-contract")
    result: dict[str, object] = {
        "schema": EPUB_QUALITY_SCHEMA,
        "profile": value["profile"],
    }
    for name in ("input_sha256", "options_sha256", "artifact_sha256", "content_sha256"):
        digest = value.get(name)
        if not isinstance(digest, str) or not _HASH.fullmatch(digest):
            raise EpubExportContractError("epub-quality-hash")
        result[name] = digest
    checks = value.get("checks")
    if not isinstance(checks, Mapping):
        raise EpubExportContractError("epub-quality-checks")
    checks = cast(Mapping[str, object], checks)
    if (
        set(checks) != {"structure", "epubcheck", "ace", "manual"}
        or checks.get("structure") not in ("passed", "not_run", "failed")
        or checks.get("epubcheck") not in ("passed", "not_run", "failed")
        or checks.get("ace") not in ("passed", "not_run", "failed")
        or checks.get("manual") not in ("reviewed", "not_reviewed")
    ):
        raise EpubExportContractError("epub-quality-checks")
    result["checks"] = dict(checks)
    counts = value.get("counts")
    if not isinstance(counts, Mapping):
        raise EpubExportContractError("epub-quality-counts")
    counts = cast(Mapping[str, object], counts)
    count_keys = {
        "notes",
        "references",
        "backlinks",
        "headings",
        "images",
        "tables",
        "pages",
    }
    if set(counts) != count_keys:
        raise EpubExportContractError("epub-quality-counts")
    if any(
        type(number) is not int or not 0 <= number <= 1_000_000
        for number in counts.values()
    ):
        raise EpubExportContractError("epub-quality-counts")
    result["counts"] = dict(counts)
    issues = value.get("issues")
    total = value.get("issue_count")
    if not isinstance(issues, (list, tuple)):
        raise EpubExportContractError("epub-quality-issues")
    issues = cast(list[object] | tuple[object, ...], issues)
    if (
        len(issues) > MAX_EPUB_QUALITY_ISSUES
        or type(total) is not int
        or not len(issues) <= total <= 1_000_000
    ):
        raise EpubExportContractError("epub-quality-issues")
    copied: list[dict[str, str]] = []
    for issue in issues:
        if not isinstance(issue, Mapping):
            raise EpubExportContractError("epub-quality-issues")
        issue = cast(Mapping[str, object], issue)
        if set(issue) != {"code", "severity", "target"} or issue.get(
            "severity"
        ) not in ("warning", "review"):
            raise EpubExportContractError("epub-quality-issues")
        code = issue.get("code")
        if not isinstance(code, str) or not _CODE.fullmatch(code):
            raise EpubExportContractError("epub-quality-issues")
        target = issue.get("target")
        if not isinstance(target, str) or (
            target and not SAFE_IDENTIFIER_RE.fullmatch(target)
        ):
            raise EpubExportContractError("epub-quality-issues")
        copied.append(
            {"code": code, "severity": str(issue["severity"]), "target": target}
        )
    result["issues"] = copied
    result["issue_count"] = total
    version = value.get("epubcheck_version")
    if version is not None:
        version = _text(version, 128, "epub-quality-version")
    result["epubcheck_version"] = version
    if "ace_version" in value:
        ace_version = value["ace_version"]
        result["ace_version"] = (
            None
            if ace_version is None
            else _text(ace_version, 128, "epub-quality-version")
        )
    if "image_bytes" in value:
        sizes_value = value["image_bytes"]
        if not isinstance(sizes_value, Mapping):
            raise EpubExportContractError("epub-quality-image-bytes")
        sizes = cast(Mapping[str, object], sizes_value)
        if set(sizes) != {"source", "packaged"} or any(
            type(size) is not int or not 0 <= size <= 1_000_000_000_000
            for size in sizes.values()
        ):
            raise EpubExportContractError("epub-quality-image-bytes")
        result["image_bytes"] = dict(sizes)
    return result
