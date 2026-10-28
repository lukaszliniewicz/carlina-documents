"""Validation and provider-neutral projection for external MCP executors.

The profile is a client-declared capability envelope.  It is intentionally
small and closed: Carlina records task authorization and uses only these
declared limits/capabilities while preparing passive completion work.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .model_capabilities import ModelCapabilityProfile

EXECUTOR_PROFILE_SCHEMA_VERSION: Final = "carlina.external-executor.v1"
EXECUTOR_PROFILE_KEYS: Final = frozenset(
    {
        "schema_version",
        "max_input_tokens",
        "max_output_tokens",
        "input_modalities",
        "supports_tools",
        "supports_structured_output",
        "supports_reasoning",
        "user_confirmed",
    }
)
EXECUTOR_INPUT_MODALITIES: Final = ("text", "image", "pdf", "audio")
_EXECUTOR_INPUT_MODALITY_SET = frozenset(EXECUTOR_INPUT_MODALITIES)
MIN_EXECUTOR_INPUT_TOKENS: Final = 1_024
MAX_EXECUTOR_INPUT_TOKENS: Final = 10_000_000
MIN_EXECUTOR_OUTPUT_TOKENS: Final = 256
MAX_EXECUTOR_OUTPUT_TOKENS: Final = 10_000_000
MAX_WORKLOAD_ROLE_LENGTH: Final = 64
_WORKLOAD_ROLE_ALIASES: Final = {
    "translator": "translator",
    "translation": "translator",
    "translator_translation": "translator",
    "translator:translation": "translator",
    "analyzer": "translator.analysis",
    "analysis": "translator.analysis",
    "translator.analysis": "translator.analysis",
    "translator_analysis": "translator.analysis",
    "translator:analyzer": "translator.analysis",
    "researcher": "translator.research",
    "research": "translator.research",
    "translator.research": "translator.research",
    "translator_research": "translator.research",
    "translator:researcher": "translator.research",
    "document": "document",
    "document_processing": "document",
    "document_parsing": "document",
    "document:parsing": "document",
    "document_metadata": "document.metadata",
    "document:metadata": "document.metadata",
    "document.metadata": "document.metadata",
    "completion": "completion",
    "news.summary": "news.summary",
}


class ExecutorProfileInput(BaseModel):
    """Strict public transport shape for a client-declared executor profile."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["carlina.external-executor.v1"] = Field(
        ...,
        description="Executor capability envelope version supported by Carlina.",
    )
    max_input_tokens: Annotated[
        int,
        Field(
            strict=True,
            ge=MIN_EXECUTOR_INPUT_TOKENS,
            le=MAX_EXECUTOR_INPUT_TOKENS,
            description=(
                "Maximum input tokens the external executor accepts. Use 256000 "
                "as the default for a new profile unless the user specifies a "
                "different limit or the executor has a lower actual capacity. "
                "This value must still be explicitly declared."
            ),
            examples=[256_000],
        ),
    ]
    max_output_tokens: Annotated[
        int,
        Field(
            strict=True,
            ge=MIN_EXECUTOR_OUTPUT_TOKENS,
            le=MAX_EXECUTOR_OUTPUT_TOKENS,
            description="Maximum output tokens the external executor produces.",
        ),
    ]
    input_modalities: Annotated[
        list[Literal["text", "image", "pdf", "audio"]],
        Field(
            min_length=1,
            json_schema_extra={
                "uniqueItems": True,
                "contains": {"const": "text"},
            },
            description=(
                "Unique singular input modality names; must include text and may "
                "include image, pdf, or audio."
            ),
        ),
    ]
    supports_tools: Annotated[
        bool,
        Field(
            strict=True,
            description="Whether the external executor can call supplied tools.",
        ),
    ]
    supports_structured_output: Annotated[
        bool,
        Field(
            strict=True,
            description="Whether the external executor can return structured output.",
        ),
    ]
    supports_reasoning: Annotated[
        bool,
        Field(
            strict=True,
            description="Whether the external executor supports reasoning effort.",
        ),
    ]
    user_confirmed: Annotated[
        bool,
        Field(
            strict=True,
            description=(
                "Legacy field acknowledging that the user's task request authorizes "
                "passive execution. No separate literal profile confirmation is required."
            ),
        ),
    ]

    @field_validator("input_modalities")
    @classmethod
    def validate_input_modalities(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("input_modalities must contain unique values.")
        if "text" not in value:
            raise ValueError("input_modalities must include text.")
        return value


class ExecutorProfileError(ValueError):
    """A safe error raised for malformed or unacceptable executor input."""

    code = "invalid_executor_profile"

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def _strict_int(value: Any, *, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExecutorProfileError(f"{field} must be an integer.")
    if not minimum <= value <= maximum:
        raise ExecutorProfileError(
            f"{field} must be between {minimum:,} and {maximum:,}."
        )
    return value


def _strict_bool(value: Any, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise ExecutorProfileError(f"{field} must be a boolean.")
    return value


@dataclass(frozen=True, slots=True)
class ExecutorProfile:
    """Normalized, immutable client-declared passive executor capability."""

    schema_version: str
    max_input_tokens: int
    max_output_tokens: int
    input_modalities: tuple[str, ...]
    supports_tools: bool
    supports_structured_output: bool
    supports_reasoning: bool
    user_confirmed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "input_modalities": list(self.input_modalities),
            "supports_tools": self.supports_tools,
            "supports_structured_output": self.supports_structured_output,
            "supports_reasoning": self.supports_reasoning,
            "user_confirmed": self.user_confirmed,
        }

    def capability_profile(self, workload_role: str) -> ModelCapabilityProfile:
        """Project this envelope to the existing provider-neutral admission type."""

        role = normalize_workload_role(workload_role)
        modalities = set(self.input_modalities)
        supports_image = "image" in modalities
        supports_pdf = "pdf" in modalities
        supports_audio = "audio" in modalities
        supports_tools = self.supports_tools
        supports_structured = self.supports_structured_output
        return ModelCapabilityProfile(
            model_id=role,
            provider_name=None,
            provider_kind=None,
            catalog_model_id=role,
            max_input_tokens=self.max_input_tokens,
            max_output_tokens=self.max_output_tokens,
            input_safety_margin_tokens=0,
            usable_input_tokens=self.max_input_tokens,
            supports_vision=supports_image,
            supports_pdf=supports_pdf,
            supports_audio_input=supports_audio,
            supports_reasoning=self.supports_reasoning,
            supports_tools=supports_tools,
            supports_tool_choice=supports_tools,
            supports_structured_output=supports_structured,
            structured_output_mode="json_schema" if supports_structured else "none",
            supports_streaming=False,
            supports_prompt_caching=False,
            metadata_sources={
                "max_input_tokens": "executor_profile",
                "max_output_tokens": "executor_profile",
                "supports_vision": "executor_profile",
                "supports_pdf": "executor_profile",
                "supports_audio_input": "executor_profile",
                "supports_reasoning": "executor_profile",
                "supports_tools": "executor_profile",
                "supports_tool_choice": "executor_profile",
                "supports_structured_output": "executor_profile",
                "supports_streaming": "executor_profile",
                "supports_prompt_caching": "executor_profile",
                "input_safety_margin_tokens": "executor_profile",
                "structured_output_mode": "executor_profile",
            },
            confidence="high",
            missing_metadata=(),
        )


def normalize_executor_profile(value: Any) -> ExecutorProfile:
    """Validate and normalize one exact external executor profile object."""

    if isinstance(value, ExecutorProfile):
        return value
    if not isinstance(value, Mapping):
        # The MCP SDK may materialize a typed JSON object as a Pydantic model
        # before calling the tool. Accept that transport wrapper, but only
        # after converting it back to an ordinary mapping; the exact closed
        # key/type validation below remains authoritative.
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            value = model_dump(mode="python")
        else:
            items = getattr(value, "items", None)
            value = dict(items()) if callable(items) else None
        if not isinstance(value, Mapping):
            raise ExecutorProfileError("executor_profile must be a JSON object.")
    keys = set(value)
    missing = EXECUTOR_PROFILE_KEYS - keys
    unknown = keys - EXECUTOR_PROFILE_KEYS
    if missing:
        raise ExecutorProfileError("executor_profile is missing required fields.")
    if unknown:
        raise ExecutorProfileError("executor_profile contains unknown fields.")
    schema_version = value.get("schema_version")
    if schema_version != EXECUTOR_PROFILE_SCHEMA_VERSION:
        raise ExecutorProfileError("executor_profile schema_version is unsupported.")

    raw_modalities = value.get("input_modalities")
    if (
        not isinstance(raw_modalities, list)
        or not raw_modalities
        or any(not isinstance(item, str) for item in raw_modalities)
        or len(raw_modalities) != len(set(raw_modalities))
        or any(item not in _EXECUTOR_INPUT_MODALITY_SET for item in raw_modalities)
        or "text" not in raw_modalities
    ):
        raise ExecutorProfileError(
            "input_modalities must be a unique nonempty list including text."
        )
    # Canonical ordering makes the encrypted snapshot and work fingerprint
    # stable when clients list the same capabilities in a different order.
    modalities = tuple(
        item for item in EXECUTOR_INPUT_MODALITIES if item in raw_modalities
    )
    user_confirmed = _strict_bool(value.get("user_confirmed"), field="user_confirmed")
    if not user_confirmed:
        raise ExecutorProfileError(
            "executor_profile requires user_confirmed=true for task authorization."
        )
    return ExecutorProfile(
        schema_version=EXECUTOR_PROFILE_SCHEMA_VERSION,
        max_input_tokens=_strict_int(
            value.get("max_input_tokens"),
            field="max_input_tokens",
            minimum=MIN_EXECUTOR_INPUT_TOKENS,
            maximum=MAX_EXECUTOR_INPUT_TOKENS,
        ),
        max_output_tokens=_strict_int(
            value.get("max_output_tokens"),
            field="max_output_tokens",
            minimum=MIN_EXECUTOR_OUTPUT_TOKENS,
            maximum=MAX_EXECUTOR_OUTPUT_TOKENS,
        ),
        input_modalities=modalities,
        supports_tools=_strict_bool(
            value.get("supports_tools"), field="supports_tools"
        ),
        supports_structured_output=_strict_bool(
            value.get("supports_structured_output"),
            field="supports_structured_output",
        ),
        supports_reasoning=_strict_bool(
            value.get("supports_reasoning"), field="supports_reasoning"
        ),
        user_confirmed=True,
    )


def require_executor_profile(value: Any) -> ExecutorProfile:
    """Alias emphasizing that passive execution cannot proceed without one."""

    return normalize_executor_profile(value)


def normalize_workload_role(value: Any, *, default: str = "completion") -> str:
    """Map an internal caller label to a short provider-neutral role token."""

    raw = str(value or "").strip().lower().replace("-", "_")
    if raw.startswith("carlina.passive."):
        raw = raw.removeprefix("carlina.passive.")
    role = _WORKLOAD_ROLE_ALIASES.get(raw)
    if role is None:
        role = _WORKLOAD_ROLE_ALIASES.get(str(default).strip().lower(), "completion")
    return role[:MAX_WORKLOAD_ROLE_LENGTH]


def workload_role_for_call(
    *,
    model: Any,
    addon_id: Any = None,
    reasoning_slot: Any = None,
) -> str:
    """Derive a bounded role without preserving or exposing model identity."""

    addon = str(addon_id or "").strip().lower()
    slot = str(reasoning_slot or "").strip().lower()
    if addon == "news_pipeline":
        return "news.summary"
    if addon == "translator":
        return normalize_workload_role(
            f"translator:{slot if slot not in {'', 'default'} else 'translation'}"
        )
    if addon == "doc_processor":
        return normalize_workload_role(
            f"document:{slot if slot not in {'', 'default'} else 'parsing'}"
        )
    return normalize_workload_role(model)


__all__ = [
    "EXECUTOR_INPUT_MODALITIES",
    "EXECUTOR_PROFILE_KEYS",
    "EXECUTOR_PROFILE_SCHEMA_VERSION",
    "MAX_EXECUTOR_INPUT_TOKENS",
    "MAX_EXECUTOR_OUTPUT_TOKENS",
    "MIN_EXECUTOR_INPUT_TOKENS",
    "MIN_EXECUTOR_OUTPUT_TOKENS",
    "ExecutorProfile",
    "ExecutorProfileError",
    "ExecutorProfileInput",
    "normalize_executor_profile",
    "normalize_workload_role",
    "require_executor_profile",
    "workload_role_for_call",
]
