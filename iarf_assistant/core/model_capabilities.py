from __future__ import annotations

import copy
import json
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from typing import Any

DEFAULT_UNKNOWN_INPUT_LIMIT_TOKENS = 32_768
DEFAULT_UNKNOWN_OUTPUT_LIMIT_TOKENS = 4_096
# These bounds apply to every user/provider-supplied token-limit value.  They
# are deliberately generous for current hosted and local models, while
# preventing accidental booleans, negative values, and values that would make
# context admission arithmetic meaningless.
MIN_MODEL_TOKEN_LIMIT = 1
MAX_MODEL_TOKEN_LIMIT = 10_000_000
MINIMUM_INPUT_SAFETY_MARGIN_TOKENS = 256
MAXIMUM_INPUT_SAFETY_MARGIN_TOKENS = 2_048
FALLBACK_IMAGE_INPUT_TOKENS_LOW = 2_048
FALLBACK_IMAGE_INPUT_TOKENS_HIGH = 8_192


def _safe_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def parse_model_token_limit(
    value: Any,
    *,
    field_name: str = "token limit",
    allow_blank: bool = False,
) -> int | None:
    """Parse a strict integer token limit for API/configuration boundaries."""
    if value is None:
        if allow_blank:
            return None
        raise ValueError(f"{field_name} must be a positive integer.")
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a positive integer, not a boolean.")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{field_name} must be a whole number of tokens.")
    if isinstance(value, str):
        value = value.strip()
        if not value:
            if allow_blank:
                return None
            raise ValueError(f"{field_name} must be a positive integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field_name} must be a positive integer.") from exc
    if parsed < MIN_MODEL_TOKEN_LIMIT:
        raise ValueError(f"{field_name} must be at least {MIN_MODEL_TOKEN_LIMIT:,} token.")
    if parsed > MAX_MODEL_TOKEN_LIMIT:
        raise ValueError(
            f"{field_name} must not exceed {MAX_MODEL_TOKEN_LIMIT:,} tokens."
        )
    return parsed


def _safe_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    return None


def _safe_structured_output_mode(value: Any) -> str | None:
    normalized = str(value or "").strip().casefold().replace("-", "_")
    aliases = {
        "none": "none",
        "disabled": "none",
        "text": "none",
        "json": "json_object",
        "json_mode": "json_object",
        "json_object": "json_object",
        "schema": "json_schema",
        "strict": "json_schema",
        "json_schema": "json_schema",
    }
    return aliases.get(normalized)


def _model_provider(model_id: str, model_record: Any = None) -> tuple[str | None, str | None]:
    provider = getattr(model_record, "provider", None) if model_record is not None else None
    provider_name = getattr(provider, "name", None)
    provider_kind = getattr(provider, "provider_kind", None)
    if not provider_name:
        try:
            from iarf_assistant.llm_provider_utils import infer_provider_name

            provider_name = infer_provider_name(model_id)
        except Exception:
            provider_name = None
    return provider_name, provider_kind


def _catalog_model_id(model_id: str, provider_name: str | None, provider_kind: str | None) -> str:
    try:
        from iarf_assistant.llm_provider_utils import normalize_litellm_model_id

        return normalize_litellm_model_id(
            model_id,
            provider_name=provider_name,
            provider_kind=provider_kind,
        )
    except Exception:
        return model_id


@lru_cache(maxsize=512)
def _cached_litellm_model_metadata(
    model_id: str,
    provider_name: str | None,
    provider_kind: str | None,
) -> dict[str, Any]:
    from carlina.core.litellm_runtime import get_litellm

    litellm = get_litellm()
    catalog_id = _catalog_model_id(model_id, provider_name, provider_kind)
    info = litellm.get_model_info(catalog_id) or {}
    if not isinstance(info, dict) or not info:
        # ``lru_cache`` does not cache exceptions. A transient catalogue
        # failure or not-yet-known model can therefore recover on the next
        # request without an unrelated database mutation.
        raise LookupError(f"No LiteLLM metadata for {catalog_id}")
    return {**info, "_catalog_model_id": catalog_id}


def load_litellm_model_metadata(
    model_id: str,
    *,
    provider_name: str | None = None,
    provider_kind: str | None = None,
) -> dict[str, Any]:
    """Load cached, content-free metadata from LiteLLM's local catalog."""
    try:
        metadata = _cached_litellm_model_metadata(
            str(model_id),
            provider_name,
            provider_kind,
        )
    except Exception:
        return {}
    return copy.deepcopy(metadata)


def invalidate_model_catalog_cache() -> None:
    """Invalidate local LiteLLM catalogue data after a model sync/update."""
    _cached_litellm_model_metadata.cache_clear()


def resolve_model_record(model_id: str, *, user_id: int | None = None) -> Any:
    """Compatibility wrapper for the shared provider-aware model resolver."""

    from iarf_assistant.core.model_access import resolve_effective_model_record

    return resolve_effective_model_record(model_id, user_id=user_id)


@dataclass(frozen=True)
class ModelCapabilityProfile:
    model_id: str
    provider_name: str | None
    provider_kind: str | None
    catalog_model_id: str
    max_input_tokens: int
    max_output_tokens: int
    input_safety_margin_tokens: int
    usable_input_tokens: int
    supports_vision: bool
    supports_pdf: bool
    supports_audio_input: bool
    supports_reasoning: bool
    supports_tools: bool
    supports_tool_choice: bool
    supports_structured_output: bool
    structured_output_mode: str
    supports_streaming: bool
    supports_prompt_caching: bool
    detected_input_tokens: int | None = None
    override_input_tokens: int | None = None
    detected_output_tokens: int | None = None
    override_output_tokens: int | None = None
    metadata_sources: dict[str, str] = field(default_factory=dict)
    confidence: str = "low"
    missing_metadata: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_LIMIT_SOURCE_LABELS = {
    "call_override": "Request override",
    "db_capability_override": "Saved model override",
    "db_model_record": "Recorded model metadata",
    "litellm_model_info": "LiteLLM catalogue",
    "conservative_fallback": "Provisional fallback (unknown)",
}


def capability_limit_projection(
    profile: ModelCapabilityProfile,
    field: str,
) -> dict[str, Any]:
    """Return a stable, UI-oriented view of one resolved token limit."""
    normalized = str(field or "").strip().lower()
    if normalized not in {"input", "output"}:
        raise ValueError("field must be 'input' or 'output'")
    if normalized == "input":
        effective = profile.max_input_tokens
        detected = profile.detected_input_tokens
        override = profile.override_input_tokens
        usable = profile.usable_input_tokens
        key = "max_input_tokens"
    else:
        effective = profile.max_output_tokens
        detected = profile.detected_output_tokens
        override = profile.override_output_tokens
        usable = None
        key = "max_output_tokens"
    source = profile.metadata_sources.get(key, "conservative_fallback")
    known = source != "conservative_fallback"
    return {
        "detected_tokens": detected,
        "override_tokens": override,
        "effective_tokens": effective,
        "usable_tokens": usable,
        "known": known,
        "source": source,
        "source_label": _LIMIT_SOURCE_LABELS.get(source, _humanize_source(source)),
        "confidence": profile.confidence,
        "provisional": not known,
    }


def _humanize_source(source: str) -> str:
    return " ".join(str(source or "").replace("_", " ").split()).title()


def _first_value(
    field_name: str,
    candidates: list[tuple[str, Any]],
    *,
    parser: Callable[[Any], Any],
    sources: dict[str, str],
) -> Any:
    for source, raw_value in candidates:
        value = parser(raw_value)
        if value is not None:
            sources[field_name] = source
            return value
    return None


def _record_override_dict(model_record: Any) -> dict[str, Any]:
    raw = getattr(model_record, "capability_overrides", None) if model_record is not None else None
    return dict(raw) if isinstance(raw, dict) else {}


def _safe_token_metadata(value: Any) -> int | None:
    """Ignore malformed provider/catalog metadata without weakening API validation."""
    try:
        return parse_model_token_limit(value)
    except ValueError:
        return None


def resolve_model_capabilities(
    model_id: str,
    *,
    user_id: int | None = None,
    model_record: Any = None,
    overrides: dict[str, Any] | None = None,
    catalog_loader: Callable[..., dict[str, Any]] = load_litellm_model_metadata,
    unknown_input_limit_tokens: int = DEFAULT_UNKNOWN_INPUT_LIMIT_TOKENS,
    unknown_output_limit_tokens: int = DEFAULT_UNKNOWN_OUTPUT_LIMIT_TOKENS,
) -> ModelCapabilityProfile:
    """Resolve one authoritative, provenance-rich capability profile.

    Precedence is call override, model-record override, explicit model columns,
    LiteLLM metadata, then a conservative fallback. False booleans in legacy
    non-null columns are treated as unknown when the catalog has a value because
    those columns historically defaulted to false.
    """
    model_id = str(model_id or "").strip()
    if not model_id:
        raise ValueError("model_id is required")

    model_record = model_record or resolve_model_record(model_id, user_id=user_id)
    provider_name, provider_kind = _model_provider(model_id, model_record)
    catalog = catalog_loader(
        model_id,
        provider_name=provider_name,
        provider_kind=provider_kind,
    ) or {}
    if not isinstance(catalog, dict):
        catalog = {}
    catalog_id = str(catalog.get("_catalog_model_id") or _catalog_model_id(
        model_id,
        provider_name,
        provider_kind,
    ))

    record_overrides = _record_override_dict(model_record)
    call_overrides = dict(overrides) if isinstance(overrides, dict) else {}
    sources: dict[str, str] = {}

    detected_input_tokens = _safe_token_metadata(
        getattr(model_record, "context_window", None)
    )
    if detected_input_tokens is None:
        detected_input_tokens = _safe_token_metadata(catalog.get("max_input_tokens"))
    if detected_input_tokens is None:
        detected_input_tokens = _safe_token_metadata(catalog.get("max_context_tokens"))
    override_input_tokens = _safe_token_metadata(
        call_overrides.get("max_input_tokens")
    )
    if override_input_tokens is None:
        override_input_tokens = _safe_token_metadata(
            call_overrides.get("context_window_tokens")
        )
    if override_input_tokens is None:
        override_input_tokens = _safe_token_metadata(
            record_overrides.get("max_input_tokens")
        )
    if override_input_tokens is None:
        override_input_tokens = _safe_token_metadata(
            record_overrides.get("context_window_tokens")
        )
    detected_output_tokens = _safe_token_metadata(
        getattr(model_record, "max_output_tokens", None)
    )
    if detected_output_tokens is None:
        detected_output_tokens = _safe_token_metadata(catalog.get("max_output_tokens"))
    if detected_output_tokens is None:
        detected_output_tokens = _safe_token_metadata(catalog.get("output_token_limit"))
    if detected_output_tokens is None:
        detected_output_tokens = _safe_token_metadata(catalog.get("max_tokens"))
    override_output_tokens = _safe_token_metadata(
        call_overrides.get("max_output_tokens")
    )
    if override_output_tokens is None:
        override_output_tokens = _safe_token_metadata(
            record_overrides.get("max_output_tokens")
        )

    max_input = _first_value(
        "max_input_tokens",
        [
            ("call_override", call_overrides.get("max_input_tokens")),
            ("call_override", call_overrides.get("context_window_tokens")),
            ("db_capability_override", record_overrides.get("max_input_tokens")),
            ("db_capability_override", record_overrides.get("context_window_tokens")),
            ("db_model_record", getattr(model_record, "context_window", None)),
            ("litellm_model_info", catalog.get("max_input_tokens")),
            ("litellm_model_info", catalog.get("max_context_tokens")),
        ],
        parser=_safe_token_metadata,
        sources=sources,
    )
    if max_input is None:
        max_input = max(1_024, _safe_int(unknown_input_limit_tokens) or DEFAULT_UNKNOWN_INPUT_LIMIT_TOKENS)
        sources["max_input_tokens"] = "conservative_fallback"

    max_output = _first_value(
        "max_output_tokens",
        [
            ("call_override", call_overrides.get("max_output_tokens")),
            ("db_capability_override", record_overrides.get("max_output_tokens")),
            ("db_model_record", getattr(model_record, "max_output_tokens", None)),
            ("litellm_model_info", catalog.get("max_output_tokens")),
            ("litellm_model_info", catalog.get("output_token_limit")),
            ("litellm_model_info", catalog.get("max_tokens")),
        ],
        parser=_safe_token_metadata,
        sources=sources,
    )
    if max_output is None:
        max_output = max(256, _safe_int(unknown_output_limit_tokens) or DEFAULT_UNKNOWN_OUTPUT_LIMIT_TOKENS)
        sources["max_output_tokens"] = "conservative_fallback"

    def resolve_bool(
        field_name: str,
        *,
        override_keys: tuple[str, ...],
        record_field: str | None,
        catalog_keys: tuple[str, ...],
        default: bool = False,
    ) -> bool:
        candidates: list[tuple[str, Any]] = []
        candidates.extend(
            ("call_override", call_overrides.get(key))
            for key in override_keys
        )
        candidates.extend(
            ("db_capability_override", record_overrides.get(key))
            for key in override_keys
        )

        record_value = getattr(model_record, record_field, None) if model_record is not None and record_field else None
        # Legacy booleans are non-null with false defaults. Preserve explicit
        # true values, but let richer catalog metadata override a default false.
        if record_value is True:
            candidates.append(("db_model_record", True))
        candidates.extend(("litellm_model_info", catalog.get(key)) for key in catalog_keys)
        if record_value is not None:
            candidates.append(("db_model_record", record_value))
        value = _first_value(
            field_name,
            candidates,
            parser=_safe_bool,
            sources=sources,
        )
        if value is None:
            sources[field_name] = "conservative_fallback"
            return default
        return bool(value)

    supports_vision = resolve_bool(
        "supports_vision",
        override_keys=("supports_vision",),
        record_field="supports_vision",
        catalog_keys=("supports_vision",),
    )
    supports_pdf = resolve_bool(
        "supports_pdf",
        override_keys=("supports_pdf", "supports_pdf_input"),
        record_field="supports_pdf",
        catalog_keys=("supports_pdf_input", "supports_pdf"),
    )
    supports_audio = resolve_bool(
        "supports_audio_input",
        override_keys=("supports_audio_input", "supports_audio_in"),
        record_field="supports_audio_in",
        catalog_keys=("supports_audio_input",),
    )
    supports_reasoning = resolve_bool(
        "supports_reasoning",
        override_keys=("supports_reasoning",),
        record_field="supports_reasoning",
        catalog_keys=("supports_reasoning", "supports_thinking"),
    )
    supports_tools = resolve_bool(
        "supports_tools",
        override_keys=("supports_tools", "supports_function_calling"),
        record_field=None,
        catalog_keys=("supports_function_calling",),
    )
    supports_tool_choice = resolve_bool(
        "supports_tool_choice",
        override_keys=("supports_tool_choice",),
        record_field=None,
        catalog_keys=("supports_tool_choice",),
    )
    supports_structured_output = resolve_bool(
        "supports_structured_output",
        override_keys=("supports_structured_output", "supports_response_schema"),
        record_field=None,
        catalog_keys=("supports_response_schema", "supports_native_structured_output"),
    )
    structured_output_mode = _first_value(
        "structured_output_mode",
        [
            (
                "call_override",
                call_overrides.get("structured_output_mode")
                or call_overrides.get("response_format_mode"),
            ),
            (
                "db_capability_override",
                record_overrides.get("structured_output_mode")
                or record_overrides.get("response_format_mode"),
            ),
        ],
        parser=_safe_structured_output_mode,
        sources=sources,
    )
    if structured_output_mode is None:
        structured_output_mode = (
            "json_schema" if supports_structured_output else "none"
        )
        sources["structured_output_mode"] = (
            "derived_from_supports_structured_output"
        )
    supports_streaming = resolve_bool(
        "supports_streaming",
        override_keys=("supports_streaming",),
        record_field=None,
        catalog_keys=("supports_native_streaming",),
        default=True,
    )
    supports_prompt_caching = resolve_bool(
        "supports_prompt_caching",
        override_keys=("supports_prompt_caching",),
        record_field=None,
        catalog_keys=("supports_prompt_caching",),
    )

    call_margin = _safe_int(call_overrides.get("input_safety_margin_tokens"))
    record_margin = _safe_int(record_overrides.get("input_safety_margin_tokens"))
    margin_override = call_margin if call_margin is not None else record_margin
    if margin_override is not None:
        safety_margin = min(max_input - 1, margin_override)
        sources["input_safety_margin_tokens"] = (
            "call_override"
            if call_margin is not None
            else "db_capability_override"
        )
    else:
        confidence_is_low = sources["max_input_tokens"] == "conservative_fallback"
        margin_ratio = 0.08 if confidence_is_low else 0.01
        safety_margin = int(math.ceil(max_input * margin_ratio))
        safety_margin = max(MINIMUM_INPUT_SAFETY_MARGIN_TOKENS, safety_margin)
        safety_margin = min(MAXIMUM_INPUT_SAFETY_MARGIN_TOKENS, safety_margin, max_input - 1)
        sources["input_safety_margin_tokens"] = "policy"

    usable_input = max(1, max_input - safety_margin)
    authoritative_sources = {
        source for source in sources.values()
        if source not in {
            "policy",
            "conservative_fallback",
            "derived_from_supports_structured_output",
        }
    }
    uses_fallback = any(source == "conservative_fallback" for source in sources.values())
    if uses_fallback:
        confidence = "low"
    elif authoritative_sources & {
        "call_override",
        "db_capability_override",
        "db_model_record",
    }:
        confidence = "high" if catalog else "medium"
    elif catalog:
        confidence = "medium"
    else:
        confidence = "low"

    missing = []
    if sources.get("max_input_tokens") == "conservative_fallback":
        missing.append("max_input_tokens")
    if sources.get("max_output_tokens") == "conservative_fallback":
        missing.append("max_output_tokens")
    if sources.get("supports_tools") == "conservative_fallback":
        missing.append("supports_tools")
    if sources.get("supports_structured_output") == "conservative_fallback":
        missing.append("supports_structured_output")

    return ModelCapabilityProfile(
        model_id=model_id,
        provider_name=provider_name,
        provider_kind=provider_kind,
        catalog_model_id=catalog_id,
        max_input_tokens=max_input,
        max_output_tokens=max_output,
        input_safety_margin_tokens=safety_margin,
        usable_input_tokens=usable_input,
        supports_vision=supports_vision,
        supports_pdf=supports_pdf,
        supports_audio_input=supports_audio,
        supports_reasoning=supports_reasoning,
        supports_tools=supports_tools,
        supports_tool_choice=supports_tool_choice,
        supports_structured_output=supports_structured_output,
        structured_output_mode=structured_output_mode,
        supports_streaming=supports_streaming,
        supports_prompt_caching=supports_prompt_caching,
        detected_input_tokens=detected_input_tokens,
        override_input_tokens=override_input_tokens,
        detected_output_tokens=detected_output_tokens,
        override_output_tokens=override_output_tokens,
        metadata_sources=sources,
        confidence=confidence,
        missing_metadata=tuple(missing),
    )


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    except Exception:
        return str(value or "")


def _fallback_tokens(value: Any) -> int:
    text = value if isinstance(value, str) else _json_text(value)
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


def _fallback_multimodal_messages(messages: list[dict[str, Any]]) -> int:
    """Estimate multimodal messages without tokenizing encoded image bytes."""

    image_tokens = 0

    def scrub(value: Any) -> Any:
        nonlocal image_tokens
        if isinstance(value, list):
            return [scrub(item) for item in value]
        if not isinstance(value, dict):
            return value

        kind = str(value.get("type") or "").strip().casefold()
        if kind in {"image", "image_url", "input_image"}:
            image_value = value.get("image_url")
            detail = (
                image_value.get("detail")
                if isinstance(image_value, dict)
                else value.get("detail")
            )
            normalized_detail = str(detail or "auto").strip().casefold()
            image_tokens += (
                FALLBACK_IMAGE_INPUT_TOKENS_LOW
                if normalized_detail == "low"
                else FALLBACK_IMAGE_INPUT_TOKENS_HIGH
            )
            return {
                "type": kind,
                "detail": normalized_detail,
                "image": "[multimodal-image]",
            }
        return {key: scrub(item) for key, item in value.items()}

    sanitized = scrub(messages)
    return _fallback_tokens(sanitized) + image_tokens


def _count_messages(
    model: str,
    messages: list[dict[str, Any]],
    token_counter: Callable[..., Any] | None,
) -> tuple[int, str]:
    if callable(token_counter):
        try:
            return max(0, int(token_counter(model=model, messages=messages))), "model_tokenizer"
        except Exception:
            pass
    return _fallback_multimodal_messages(messages), "structured_multimodal_fallback"


def _count_text(
    model: str,
    text: str,
    token_counter: Callable[..., Any] | None,
) -> tuple[int, str]:
    if not text:
        return 0, "empty"
    if callable(token_counter):
        try:
            return max(0, int(token_counter(model=model, text=text))), "model_tokenizer"
        except Exception:
            pass
    return _fallback_tokens(text), "chars_div_4"


@dataclass(frozen=True)
class PayloadContributor:
    kind: str
    identifier: str
    tokens: int
    required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RenderedPayloadMeasurement:
    model_id: str
    message_tokens: int
    tool_schema_tokens: int
    response_schema_tokens: int
    extra_parameter_tokens: int
    provider_overhead_tokens: int
    total_input_tokens: int
    count_methods: dict[str, str]
    contributors: tuple[PayloadContributor, ...]

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["contributors"] = [entry.to_dict() for entry in self.contributors]
        return result


def measure_rendered_payload(
    *,
    model: str,
    messages: list[dict[str, Any]] | None,
    tools: list[dict[str, Any]] | None = None,
    response_format: dict[str, Any] | None = None,
    extra_parameters: dict[str, Any] | None = None,
    token_counter: Callable[..., Any] | None = None,
) -> RenderedPayloadMeasurement:
    """Measure the complete provider-ready request without logging its content."""
    clean_messages = [dict(message) for message in (messages or []) if isinstance(message, dict)]
    clean_tools = [dict(tool) for tool in (tools or []) if isinstance(tool, dict)]
    clean_extras = dict(extra_parameters) if isinstance(extra_parameters, dict) else {}

    message_tokens, message_method = _count_messages(model, clean_messages, token_counter)
    tool_text = _json_text(clean_tools) if clean_tools else ""
    tool_tokens, tool_method = _count_text(model, tool_text, token_counter)
    response_text = _json_text(response_format) if response_format else ""
    response_tokens, response_method = _count_text(model, response_text, token_counter)
    extra_text = _json_text(clean_extras) if clean_extras else ""
    extra_tokens, extra_method = _count_text(model, extra_text, token_counter)

    # Message tokenizers include normal role framing, but they cannot account
    # for the final provider wrapper around schemas and optional parameters.
    provider_overhead = 16
    if clean_tools:
        provider_overhead += 8 + 2 * len(clean_tools)
    if response_format:
        provider_overhead += 8
    if clean_extras:
        provider_overhead += 4

    contributors: list[PayloadContributor] = []
    for index, message in enumerate(clean_messages):
        tokens, _method = _count_messages(model, [message], token_counter)
        role = str(message.get("role") or "message")
        contributors.append(PayloadContributor(
            kind="message",
            identifier=f"{index}:{role}",
            tokens=tokens,
            required=role in {"system", "user"},
        ))
    for index, tool in enumerate(clean_tools):
        name = str((tool.get("function") or {}).get("name") or f"tool_{index}")
        tokens, _method = _count_text(model, _json_text(tool), token_counter)
        contributors.append(PayloadContributor(
            kind="tool_schema",
            identifier=name,
            tokens=tokens,
            required=False,
        ))
    if response_format:
        contributors.append(PayloadContributor(
            kind="response_schema",
            identifier="response_format",
            tokens=response_tokens,
            required=True,
        ))

    total = message_tokens + tool_tokens + response_tokens + extra_tokens + provider_overhead
    return RenderedPayloadMeasurement(
        model_id=model,
        message_tokens=message_tokens,
        tool_schema_tokens=tool_tokens,
        response_schema_tokens=response_tokens,
        extra_parameter_tokens=extra_tokens,
        provider_overhead_tokens=provider_overhead,
        total_input_tokens=total,
        count_methods={
            "messages": message_method,
            "tools": tool_method,
            "response_format": response_method,
            "extra_parameters": extra_method,
        },
        contributors=tuple(contributors),
    )


@dataclass(frozen=True)
class AdmissionResult:
    admitted: bool
    mode: str
    profile: ModelCapabilityProfile
    measurement: RenderedPayloadMeasurement
    requested_output_tokens: int
    available_input_tokens: int
    overflow_tokens: int
    overflow_manifest: tuple[dict[str, Any], ...] = ()
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "admitted": self.admitted,
            "mode": self.mode,
            "profile": self.profile.to_dict(),
            "measurement": self.measurement.to_dict(),
            "requested_output_tokens": self.requested_output_tokens,
            "available_input_tokens": self.available_input_tokens,
            "overflow_tokens": self.overflow_tokens,
            "overflow_manifest": [dict(item) for item in self.overflow_manifest],
            "reason": self.reason,
        }


class ContextAdmissionError(ValueError):
    def __init__(self, result: AdmissionResult):
        self.result = result
        message = result.reason or (
            f"Rendered request exceeds the usable input limit by {result.overflow_tokens} tokens."
        )
        super().__init__(message)


def _normalize_mode(mode: str | None) -> str:
    normalized = str(mode or "assisted").strip().lower()
    if normalized in {"off", "disabled", "manual"}:
        return "manual"
    if normalized in {"compact", "aggressive"}:
        return "aggressive"
    return "assisted"


def admit_provider_payload(
    *,
    profile: ModelCapabilityProfile,
    messages: list[dict[str, Any]] | None,
    tools: list[dict[str, Any]] | None = None,
    response_format: dict[str, Any] | None = None,
    extra_parameters: dict[str, Any] | None = None,
    requested_output_tokens: int | None = None,
    mode: str = "assisted",
    token_counter: Callable[..., Any] | None = None,
) -> AdmissionResult:
    """Admit an exact provider-ready payload or return an explicit overflow manifest."""
    normalized_mode = _normalize_mode(mode)
    requested_output = _safe_int(requested_output_tokens) or min(
        profile.max_output_tokens,
        max(1_024, min(8_192, profile.max_output_tokens)),
    )
    requested_output = min(requested_output, profile.max_output_tokens)
    measurement = measure_rendered_payload(
        model=profile.catalog_model_id or profile.model_id,
        messages=messages,
        tools=tools,
        response_format=response_format,
        extra_parameters=extra_parameters,
        token_counter=token_counter,
    )
    available = profile.usable_input_tokens
    overflow = max(0, measurement.total_input_tokens - available)
    admitted = overflow == 0

    manifest: list[dict[str, Any]] = []
    if not admitted:
        for contributor in sorted(
            measurement.contributors,
            key=lambda entry: entry.tokens,
            reverse=True,
        ):
            manifest.append({
                **contributor.to_dict(),
                "disposition": "required" if contributor.required else "candidate_for_explicit_adaptation",
            })

    reason = None
    if not admitted:
        manual_clause = (
            " Manual mode did not remove, summarize, or archive any item."
            if normalized_mode == "manual"
            else " The caller must execute an explicit, evidence-preserving adaptation before retrying."
        )
        reason = (
            f"Rendered request requires {measurement.total_input_tokens} input tokens, "
            f"but {profile.model_id} admits {available} after its safety margin "
            f"({overflow} tokens over).{manual_clause}"
        )

    return AdmissionResult(
        admitted=admitted,
        mode=normalized_mode,
        profile=profile,
        measurement=measurement,
        requested_output_tokens=requested_output,
        available_input_tokens=available,
        overflow_tokens=overflow,
        overflow_manifest=tuple(manifest),
        reason=reason,
    )


def require_admitted_payload(**kwargs: Any) -> AdmissionResult:
    result = admit_provider_payload(**kwargs)
    if not result.admitted:
        raise ContextAdmissionError(result)
    return result
