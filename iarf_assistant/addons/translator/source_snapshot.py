"""Small invariants shared by Translator source snapshot boundaries."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping

MAX_SOURCE_SNAPSHOT_BYTES = 20 * 1024 * 1024
MAX_DOCUMENT_SNAPSHOT_BYTES = MAX_SOURCE_SNAPSHOT_BYTES
MAX_SUBTITLE_UNIT_CHARS = 50_000
MAX_SUBTITLE_UNITS = 50_000
MAX_SUBTITLE_PROJECT_ID_CHARS = 64
MAX_SUBTITLE_METADATA_STRING_CHARS = 256
MAX_SUBTITLE_CUE_IDS = 256
MAX_SUBTITLE_RENDER_SETTINGS_BYTES = 16 * 1024
MAX_SUBTITLE_RENDER_SETTINGS_DEPTH = 4
MAX_SUBTITLE_RENDER_SETTINGS_ITEMS = 64

_SUBTITLE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_SUBTITLE_RENDER_SETTING_KEYS = frozenset(
    {
        "max_chars",
        "display_wrap_lines",
        "display_max_lines",
        "display_line_max_chars",
        "preserve_sentence_end_splits",
        "merge_short_cues",
        "merge_gap_ms",
        "min_merge_chars",
        "dataset_min_chars",
        "dataset_max_chars",
        "dataset_alignment_max_chars",
        "fallback_split",
        "split_on_punctuation",
        "split_on_conjunctions",
        "split_on_cjk_transitions",
        "japanese_tokenizer",
        "japanese_tokenizer_split_mode",
        "punctuation_chars",
        "conjunctions",
        "segmenter_threshold",
        "alignment_model_id",
        "alignment_config",
        "mapping_stt_model_id",
        "mapping_llm_model",
        "mapping_stt_config",
        "chapter_detection_use_llm",
        "chapter_detection_model",
        "alignment_warn_unmatched_ratio",
        "alignment_warn_min_words",
        "alignment_warn_min_confidence",
        "alignment_warn_boundary_gap_ms",
        "dataset_min_word_confidence",
        "dataset_min_word_confidence_floor",
        "parse_remove_all_newlines",
        "audio_preprocess_loudnorm",
        "audio_preprocess_dynaudnorm",
    }
)


def _bounded_json_value(value, *, depth: int = 0):
    """Copy a small JSON value without accepting unbounded provider objects."""

    if depth > MAX_SUBTITLE_RENDER_SETTINGS_DEPTH:
        raise ValueError("Subtitle render settings are nested too deeply.")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > 1_000_000_000:
            raise ValueError("Subtitle render settings contain an out-of-range number.")
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or abs(value) > 1_000_000_000:
            raise ValueError("Subtitle render settings contain an invalid number.")
        return value
    if isinstance(value, str):
        if len(value) > MAX_SUBTITLE_METADATA_STRING_CHARS:
            raise ValueError("Subtitle render settings contain an overlong string.")
        return value
    if isinstance(value, list):
        if len(value) > MAX_SUBTITLE_RENDER_SETTINGS_ITEMS:
            raise ValueError("Subtitle render settings contain too many items.")
        return [_bounded_json_value(item, depth=depth + 1) for item in value]
    if isinstance(value, Mapping):
        if len(value) > MAX_SUBTITLE_RENDER_SETTINGS_ITEMS:
            raise ValueError("Subtitle render settings contain too many fields.")
        result = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 64:
                raise ValueError("Subtitle render settings contain an invalid key.")
            result[key] = _bounded_json_value(item, depth=depth + 1)
        return result
    raise ValueError("Subtitle render settings must contain JSON values.")


def subtitle_render_settings(value) -> dict:
    """Return only render-safe Subtitle Studio settings.

    These values are stored in Translator's plaintext settings column and are
    later handed back to the provider solely for rendering.  Provider-specific
    extras are deliberately ignored; malformed values for known fields fail
    before the project is persisted.
    """

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("Subtitle render settings must be an object.")
    if len(value) > MAX_SUBTITLE_RENDER_SETTINGS_ITEMS:
        raise ValueError("Subtitle render settings contain too many fields.")
    result = {
        key: _bounded_json_value(item)
        for key, item in value.items()
        if key in _SUBTITLE_RENDER_SETTING_KEYS
    }
    # Encoding is the durable bound, not a Python-object-size guess.
    encoded = json.dumps(
        result, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    if len(encoded) > MAX_SUBTITLE_RENDER_SETTINGS_BYTES:
        raise ValueError("Subtitle render settings exceed the size limit.")
    return result


def _subtitle_identifier(value, *, field: str, required: bool = False) -> str | None:
    if value in (None, ""):
        if required:
            raise ValueError(f"Subtitle {field} is required.")
        return None
    if isinstance(value, bool):
        raise ValueError(f"Subtitle {field} is invalid.")
    normalized = str(value).strip()
    if (
        not normalized
        or len(normalized) > MAX_SUBTITLE_METADATA_STRING_CHARS
        or not _SUBTITLE_IDENTIFIER_RE.fullmatch(normalized)
    ):
        raise ValueError(f"Subtitle {field} is invalid.")
    return normalized


def subtitle_project_metadata(value) -> dict:
    """Project display/provenance fields safe for plaintext Translator state."""

    if not isinstance(value, Mapping):
        raise ValueError("Subtitle provider returned invalid project metadata.")
    project_id = _subtitle_identifier(
        value.get("id"), field="project identifier", required=True
    )
    if len(project_id) > MAX_SUBTITLE_PROJECT_ID_CHARS:
        raise ValueError("Subtitle project identifier is too long.")
    result = {"id": project_id}
    for source_key, target_key in (
        ("name", "name"),
        ("media_filename", "media_filename"),
        ("language", "language"),
    ):
        raw = value.get(source_key)
        if raw is None:
            continue
        if not isinstance(raw, str):
            raise ValueError(f"Subtitle project {source_key} must be text.")
        normalized = raw.strip()
        maximum = 50 if source_key == "language" else MAX_SUBTITLE_METADATA_STRING_CHARS
        if len(normalized) > maximum:
            raise ValueError(f"Subtitle project {source_key} is too long.")
        if normalized:
            result[target_key] = normalized
    return result


def _subtitle_index(value, *, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"Subtitle {field} is invalid.")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Subtitle {field} is invalid.") from exc
    if result < 0 or result > 1_000_000_000:
        raise ValueError(f"Subtitle {field} is invalid.")
    return result


def _subtitle_timestamp(value, *, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"Subtitle {field} is invalid.")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Subtitle {field} is invalid.") from exc
    if not math.isfinite(result) or result < 0 or result > 31_536_000_000:
        raise ValueError(f"Subtitle {field} is invalid.")
    return result


def subtitle_unit_metadata(value) -> dict:
    """Project only timing/provenance needed to render a subtitle translation."""

    if not isinstance(value, Mapping):
        raise ValueError("Subtitle unit metadata must be an object.")
    result = {}
    unit_id = _subtitle_identifier(value.get("unit_id"), field="unit identifier")
    if unit_id is not None:
        result["unit_id"] = unit_id
    for field in ("cue_start_index", "cue_end_index"):
        item = _subtitle_index(value.get(field), field=field)
        if item is not None:
            result[field] = item
    if (
        result.get("cue_start_index") is not None
        and result.get("cue_end_index") is not None
        and result["cue_end_index"] < result["cue_start_index"]
    ):
        raise ValueError("Subtitle cue indices are reversed.")
    cue_ids = value.get("cue_ids")
    if cue_ids is not None:
        if not isinstance(cue_ids, list) or len(cue_ids) > MAX_SUBTITLE_CUE_IDS:
            raise ValueError("Subtitle cue identifiers are invalid.")
        cleaned_ids = []
        for cue_id in cue_ids:
            normalized = _subtitle_identifier(
                cue_id, field="cue identifier", required=True
            )
            cleaned_ids.append(normalized)
        result["cue_ids"] = cleaned_ids
    start = _subtitle_timestamp(value.get("start"), field="start")
    end = _subtitle_timestamp(value.get("end"), field="end")
    if start is not None:
        result["start"] = start
    if end is not None:
        result["end"] = end
    if start is not None and end is not None and end < start:
        raise ValueError("Subtitle unit ends before it starts.")
    alignment_mode = _subtitle_identifier(
        value.get("alignment_mode"), field="alignment mode"
    )
    if alignment_mode is not None:
        result["alignment_mode"] = alignment_mode
    return result


def subtitle_snapshot_entry_bytes(metadata: Mapping, source_text: str) -> int:
    """Conservative UTF-8 budget for one encrypted subtitle snapshot entry."""

    return (
        len(
            json.dumps(
                {**metadata, "source_text": source_text},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        + 48
    )  # UUID key plus JSON object punctuation in the enclosing units map.


def encoded_document_snapshot_html(output_html: str) -> bytes:
    """Return UTF-8 snapshot bytes after enforcing the persisted-size bound."""

    encoded = output_html.encode("utf-8")
    if len(encoded) > MAX_SOURCE_SNAPSHOT_BYTES:
        raise ValueError("Document snapshot exceeds the 20 MiB limit.")
    return encoded


def encoded_subtitle_snapshot(snapshot_json: str) -> bytes:
    """Bound the canonical encrypted subtitle snapshot by UTF-8 byte size."""

    encoded = snapshot_json.encode("utf-8")
    if len(encoded) > MAX_SOURCE_SNAPSHOT_BYTES:
        raise ValueError("Subtitle source snapshot exceeds the 20 MiB limit.")
    return encoded
