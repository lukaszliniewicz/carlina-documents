import base64
import copy
import hashlib
import json
import logging
import mimetypes
import os
import time
import uuid
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import tenacity
from flask import current_app

from .core.litellm_runtime import litellm
from .core.model_capabilities import (
    AdmissionResult,
    ContextAdmissionError,
    admit_provider_payload,
    resolve_model_capabilities,
    resolve_model_record,
)
from .core.model_readiness import resolve_model_connection
from .core.prompt_telemetry import (
    begin_model_call,
    finish_model_call,
    measure_prompt_payload,
)
from .core.runtime_contracts import CostAttribution
from .core.upload_paths import resolve_upload_reference, upload_web_path, uploads_root
from .extensions import db
from .llm_provider_utils import (
    canonical_provider_name,
    is_openai_compatible_provider,
    normalize_litellm_model_id,
    normalize_openai_compatible_base_url,
)
from .models import (
    ChatSession,
    LLMCost,
    LLMProvider,
    RuntimeJob,
    User,
    WorkflowRun,
    WorkflowStep,
)
from .reasoning import resolve_reasoning_config

_OUTPUT_TOKEN_PARAMETERS = (
    "max_completion_tokens",
    "max_output_tokens",
    "max_tokens",
)
_INVALID_ADMITTED_OUTPUT_LIMIT = "admitted_output_tokens must be a positive integer"
_DEBUG_CAPTURE_SCHEMA_VERSION = 2
_DEBUG_PROVIDER_PARAMETER_NAMES = frozenset(
    {
        "api_version",
        "audio",
        "drop_params",
        "frequency_penalty",
        "logit_bias",
        "max_completion_tokens",
        "max_output_tokens",
        "max_tokens",
        "modalities",
        "n",
        "num_retries",
        "parallel_tool_calls",
        "presence_penalty",
        "reasoning_effort",
        "request_timeout",
        "response_format",
        "seed",
        "stop",
        "stream",
        "temperature",
        "timeout",
        "tool_choice",
        "top_p",
    }
)
_DEBUG_CAPTURE_EXCLUDED_PARAMETER_NAMES = frozenset(
    {"api_base", "api_key", "messages", "tools", "vertex_credentials"}
)


def _debug_value(value, key: str, default=None):
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _debug_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _debug_text_summary(value: str) -> dict:
    return {
        "characters": len(value),
        "utf8_bytes": len(value.encode("utf-8")),
        "sha256": _debug_hash(value),
    }


def _debug_image_summary(image_url) -> dict:
    image_data = image_url if isinstance(image_url, dict) else {}
    url = str(image_data.get("url") or "")
    summary = {
        "detail": image_data.get("detail"),
        "sha256": _debug_hash(url),
    }
    if url.startswith("data:"):
        header, _, encoded = url.partition(",")
        media_type = header[5:].split(";", 1)[0] or None
        padding = len(encoded) - len(encoded.rstrip("="))
        summary.update(
            {
                "transport": "data_url",
                "media_type": media_type,
                "encoded_characters": len(encoded),
                "estimated_decoded_bytes": max(0, (len(encoded) * 3) // 4 - padding),
            }
        )
        return summary

    parsed = urlsplit(url)
    summary.update(
        {
            "transport": "remote_url" if parsed.scheme else "reference",
            "scheme": parsed.scheme or None,
            "host": parsed.hostname,
        }
    )
    return summary


def _debug_message_manifest(messages) -> list[dict]:
    manifest = []
    for message in messages or []:
        if not isinstance(message, dict):
            manifest.append({"role": None, "content_type": type(message).__name__})
            continue
        content = message.get("content")
        entry = {"role": message.get("role")}
        if isinstance(content, str):
            entry.update({"content_type": "text", "text": _debug_text_summary(content)})
        elif isinstance(content, list):
            parts = []
            for part in content:
                if not isinstance(part, dict):
                    parts.append({"type": type(part).__name__})
                    continue
                part_type = part.get("type")
                if part_type == "text":
                    parts.append(
                        {
                            "type": "text",
                            "text": _debug_text_summary(str(part.get("text") or "")),
                        }
                    )
                elif part_type == "image_url":
                    parts.append(
                        {
                            "type": "image_url",
                            "image": _debug_image_summary(part.get("image_url")),
                        }
                    )
                else:
                    parts.append({"type": part_type or "unknown"})
            entry.update({"content_type": "parts", "parts": parts})
        else:
            entry.update({"content_type": type(content).__name__})
        manifest.append(entry)
    return manifest


def _debug_api_base(value) -> str | None:
    if not value:
        return None
    parsed = urlsplit(str(value))
    if not parsed.scheme or not parsed.hostname:
        return None
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def _debug_provider_response(response) -> dict:
    usage = _debug_value(response, "usage")
    usage_summary = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = _debug_value(usage, key)
        if value is not None:
            usage_summary[key] = value

    for details_key in ("prompt_tokens_details", "completion_tokens_details"):
        details = _debug_value(usage, details_key)
        if details is None:
            continue
        selected = {}
        for key in ("cached_tokens", "reasoning_tokens"):
            value = _debug_value(details, key)
            if value is not None:
                selected[key] = value
        if selected:
            usage_summary[details_key] = selected

    choices = _debug_value(response, "choices", []) or []
    return {
        "id": _debug_value(response, "id"),
        "model": _debug_value(response, "model"),
        "finish_reasons": [_debug_value(choice, "finish_reason") for choice in choices],
        "usage": usage_summary,
    }


def _debug_error_status(error) -> int | None:
    direct = getattr(error, "status_code", None)
    response = getattr(error, "response", None)
    response_status = getattr(response, "status_code", None)
    value = direct if direct is not None else response_status
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _apply_admitted_output_limit(
    parameters: dict, *, admitted_output_tokens: int
) -> None:
    """Clamp provider-facing output aliases to the capability-admitted value."""

    if not isinstance(admitted_output_tokens, int) or admitted_output_tokens < 1:
        raise ValueError(_INVALID_ADMITTED_OUTPUT_LIMIT)
    for name in _OUTPUT_TOKEN_PARAMETERS:
        if name in parameters:
            parameters[name] = admitted_output_tokens


def parse_duration_string(s: str) -> float | None:
    if not s:
        return None
    s = s.strip().lower()
    import re

    # Match number and optional unit (e.g. "25ms", "12.4s", "2m")
    match = re.match(r"^([\d.]+)\s*([a-z]+)?$", s)
    if not match:
        return None
    val_str, unit = match.groups()
    try:
        val = float(val_str)
    except ValueError:
        return None

    if not unit:
        return val  # Default to seconds

    if unit in ("ms", "millis", "milliseconds"):
        return val / 1000.0
    elif unit in ("s", "sec", "secs", "second", "seconds"):
        return val
    elif unit in ("m", "min", "mins", "minute", "minutes"):
        return val * 60.0
    elif unit in ("h", "hr", "hrs", "hour", "hours"):
        return val * 3600.0
    return val


def parse_retry_after(s: str) -> float | None:
    if not s:
        return None
    s = s.strip()
    try:
        return float(s)
    except ValueError:
        pass

    import email.utils
    from datetime import datetime, timezone

    try:
        parsed_date = email.utils.parsedate_to_datetime(s)
        now = datetime.now(timezone.utc)
        delay = (parsed_date - now).total_seconds()
        return max(0.0, delay)
    except Exception:
        return None


def get_retry_after_from_exception(e) -> float | None:
    """
    Extracts the retry-after / reset time from the exception headers,
    standardizing the lookup and returning the maximum recommended wait time in seconds.
    """
    headers = getattr(e, "headers", None)
    if not headers and hasattr(e, "response") and e.response is not None:
        headers = getattr(e.response, "headers", None)

    if not headers:
        return None

    h_dict = {}
    try:
        for k, v in headers.items():
            h_dict[k.lower()] = str(v)
    except Exception:
        return None

    candidates = []

    # 1. Standard Retry-After
    if "retry-after" in h_dict:
        val = parse_retry_after(h_dict["retry-after"])
        if val is not None:
            candidates.append(val)

    # 2. Azure/Provider specific Retry-After/reset in milliseconds or strings
    for k in ["retry-after-ms", "x-retry-after-ms"]:
        if k in h_dict:
            raw_val = h_dict[k].strip()
            try:
                # If it is a clean integer/float millisecond representation, parse it
                val = float(raw_val) / 1000.0
            except ValueError:
                # Otherwise, parse with units
                val = parse_duration_string(raw_val)
            if val is not None:
                candidates.append(val)

    if "llm_provider-retry-after" in h_dict:
        val = parse_retry_after(h_dict["llm_provider-retry-after"])
        if val is not None:
            candidates.append(val)

    # 3. Rate limit reset headers (could be seconds or duration string)
    for k in [
        "x-ratelimit-reset",
        "x-ratelimit-reset-requests",
        "x-ratelimit-reset-tokens",
    ]:
        if k in h_dict:
            val = parse_duration_string(h_dict[k])
            if val is not None:
                candidates.append(val)

    if candidates:
        return max(candidates)
    return None


def get_rate_limit_wait(retry_state) -> float:
    """
    Calculates a wait time for tenacity based on the exception.
    If the exception is a RateLimitError (HTTP 429), it tries to extract
    the retry duration from the response headers. If found, it adds a
    small random jitter (0.5 to 1.5s) and returns it.
    Otherwise, it falls back to a smart exponential backoff with jitter.
    """
    import random

    e = retry_state.outcome.exception()
    if not isinstance(e, litellm.RateLimitError):
        # Standard exponential backoff for other transient errors:
        # e.g., 2s, 4s, 8s, 16s, 30s with some jitter
        base_wait = tenacity.wait_exponential(multiplier=1, min=2, max=30)(retry_state)
        # Add jitter
        return base_wait + random.uniform(0.5, 1.5)

    # It is a RateLimitError
    # 1. Attempt to extract retry-after from headers
    retry_after = get_retry_after_from_exception(e)
    if retry_after is not None and retry_after > 0:
        # Add a small random jitter to avoid thundering herd
        wait_time = retry_after + random.uniform(0.5, 1.5)
        # Cap the wait time to a maximum of 300 seconds (5 minutes)
        return min(wait_time, 300.0)

    # 2. Fallback: Smart exponential backoff with jitter for RateLimitError
    # We want it to be exponentially increasing, starting at 4s, up to 300s
    base_wait = tenacity.wait_exponential(multiplier=2, min=4, max=300)(retry_state)
    return base_wait + random.uniform(0.5, 1.5)


def is_prompt_cache_configuration_error(exc: Exception, call_kwargs: dict) -> bool:
    """Identify deterministic provider cache failures that require no-cache retry.

    Some providers report disabled cache storage as HTTP 429 even though no
    amount of backoff can make a zero allowance non-zero.  This predicate is
    deliberately gated by our own ``prompt_cache_key`` so ordinary rate limits
    retain the normal retry policy.
    """

    if "prompt_cache_key" not in call_kwargs:
        return False
    error_text = str(exc).casefold()
    cache_marker = any(
        token in error_text
        for token in (
            "cachedcontent",
            "cached content",
            "prompt cache",
            "prompt_cache",
        )
    )
    configuration_marker = any(
        token in error_text
        for token in (
            "permission",
            "forbidden",
            "denied",
            "limit exceeded",
            "quota",
            "resource_exhausted",
            "billing_disabled",
            "403",
        )
    )
    return cache_marker and configuration_marker


class LLMService:
    """A centralized service for making LLM calls via LiteLLM."""

    def __init__(self, user_id=None, session_id=None, cost_attribution=None):
        """
        Initializes the service.
        """
        self.user_id = user_id
        self.session_id = session_id
        self.cost_attribution = CostAttribution(
            chat_session_id=str(session_id) if session_id else None,
        ).merged(cost_attribution)

    def _validated_cost_attribution(self, value=None) -> dict:
        context = self.cost_attribution.merged(value)
        session_id = None
        workflow_run_id = None
        workflow_step_id = None
        runtime_job_id = None

        if context.chat_session_id and db.session.get(
            ChatSession, context.chat_session_id
        ):
            session_id = context.chat_session_id

        step = (
            db.session.get(WorkflowStep, context.workflow_step_id)
            if context.workflow_step_id
            else None
        )
        if step:
            workflow_step_id = step.id
            workflow_run_id = step.workflow_run_id

        run = (
            db.session.get(WorkflowRun, context.workflow_run_id)
            if context.workflow_run_id
            else None
        )
        if run and (not step or step.workflow_run_id == run.id):
            workflow_run_id = run.id

        job = (
            db.session.get(RuntimeJob, context.runtime_job_id)
            if context.runtime_job_id
            else None
        )
        if job:
            runtime_job_id = job.id
            if not workflow_run_id and job.workflow_run_id:
                workflow_run_id = job.workflow_run_id

        return {
            "session_id": session_id,
            "workflow_run_id": workflow_run_id,
            "workflow_step_id": workflow_step_id,
            "runtime_job_id": runtime_job_id,
            "turn_id": str(context.turn_id)[:36] if context.turn_id else None,
            "action_id": str(context.action_id)[:64] if context.action_id else None,
        }

    def _get_file_metadata_text(
        self,
        local_path: Path,
        file_name: str,
        file_path_str: str,
        collection_name: str = None,
    ) -> str:
        """Helper to generate a formatted metadata string."""
        metadata_parts = [f"File attached: {file_name}"]
        if collection_name:
            metadata_parts.append(f"Collection: {collection_name}")

        metadata_parts.append(f"Path: {file_path_str}")

        if local_path.exists():
            try:
                size_kb = local_path.stat().st_size / 1024
                metadata_parts.append(f"Size: {size_kb:.2f} KB")
                try:
                    from PIL import Image
                except ImportError:
                    Image = None
                if Image is not None:
                    with Image.open(local_path) as img:
                        metadata_parts.append(f"Dimensions: {img.width}x{img.height}")
            except Exception:
                pass  # Not an image or other error, just ignore extra metadata
        else:
            metadata_parts.append("File not found on server.")
        return ", ".join(metadata_parts)

    def _get_api_credentials(self, model: str):
        """
        Determines which API key and base URL to use based on the model name.
        """
        connection = resolve_model_connection(model, user_id=self.user_id)
        provider_name = connection.provider_name
        provider = connection.provider
        api_key_obj = connection.api_key_record

        if api_key_obj:
            api_base = api_key_obj.api_base
            db_provider = api_key_obj.provider or provider
            db_provider_name = canonical_provider_name(
                db_provider.name if db_provider else provider_name
            )
            provider_kind = getattr(db_provider, "provider_kind", None)

            if db_provider_name == "vertex_ai":
                api_base_val = api_base or "my-project,us-central1"
                parts = api_base_val.split(",")
                return {
                    "api_key": api_key_obj.api_key,  # JSON string
                    "api_key_record_id": api_key_obj.id,
                    "api_base": api_base_val,
                    "provider_name": db_provider_name,
                    "provider_kind": provider_kind,
                    "vertex_project": parts[0].strip(),
                    "vertex_location": parts[1].strip()
                    if len(parts) > 1
                    else "us-central1",
                }

            api_base = normalize_openai_compatible_base_url(
                db_provider_name, api_base, provider_kind
            )

            return {
                "api_key": api_key_obj.api_key,
                "api_key_record_id": api_key_obj.id,
                "api_base": api_base,
                "api_version": api_key_obj.api_version,
                "provider_name": db_provider_name,
                "provider_kind": provider_kind,
            }

        logging.warning(
            f"No API key found for provider '{provider_name}' (User ID: {self.user_id})."
        )

        # 3. Fallback for Gemini to environment variable for backward compatibility
        if provider_name == "gemini":
            try:
                from .config import GOOGLE_API_KEY
            except ModuleNotFoundError as exc:
                if exc.name != "iarf_assistant.config":
                    raise
                GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
            if GOOGLE_API_KEY:
                return {
                    "api_key": GOOGLE_API_KEY,
                    "api_base": None,
                    "api_version": None,
                    "provider_name": "gemini",
                }

        return {
            "api_key": None,
            "api_base": None,
            "api_version": None,
            "provider_name": provider_name,
        }

    def log_external_cost(
        self,
        cost: float,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        model: str,
        source: str,
        cost_attribution=None,
    ):
        """Immediately logs an external cost (like document extraction) to the database."""
        if cost <= 0 and total_tokens <= 0:
            return

        try:
            cost_entry = LLMCost(
                user_id=self.user_id,
                cost=cost,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                model=model,
                source=source,
                **self._validated_cost_attribution(cost_attribution),
            )
            db.session.add(cost_entry)

            from iarf_assistant.database_utils import commit_with_retry

            commit_with_retry(db.session)
        except Exception as db_error:
            db.session.rollback()
            logging.error(f"Failed to log external cost to database: {db_error}")

    def _get_image_detail_policy_value(self, model: str):
        if not self.user_id:
            return None

        try:
            user = db.session.get(User, self.user_id)
            llm_settings = (
                (user.addon_settings or {}).get("llm_settings", {}) if user else {}
            )
            image_detail_configs = llm_settings.get("image_detail_configs", {})
            if not isinstance(image_detail_configs, dict):
                return None
            return image_detail_configs.get(model)
        except Exception:
            logging.warning(
                "Failed to load image detail settings for model %s.",
                model,
                exc_info=True,
            )
            return None

    def _image_detail_family(self, model: str, credentials: dict) -> str:
        provider_name = canonical_provider_name(credentials.get("provider_name"))
        provider_kind = credentials.get("provider_kind")
        model_lower = (model or "").lower()

        if provider_name in {"gemini", "vertex_ai"} or any(
            name in model_lower for name in ("gemini", "gemma")
        ):
            return "google"
        if provider_name == "openai" or is_openai_compatible_provider(
            provider_name, provider_kind
        ):
            return "openai"
        return "generic"

    def _normalize_image_detail_value(self, raw_value, model: str, credentials: dict):
        if raw_value is None:
            return "preserve"

        if isinstance(raw_value, dict):
            raw_value = (
                raw_value.get("level")
                or raw_value.get("detail")
                or raw_value.get("mode")
            )

        value = str(raw_value or "").strip().lower().replace("-", "_").replace(" ", "_")
        if value in {"", "preserve", "default", "request_default", "request"}:
            return "preserve"
        if value in {"omit", "none", "off", "disabled", "disable"}:
            return None

        family = self._image_detail_family(model, credentials)
        if value in {"auto", "low", "high"}:
            return value
        if value in {"medium", "ultra_high"}:
            return value if family == "google" else "high"
        return "preserve"

    def _apply_image_detail_policy(self, messages, model: str, credentials: dict):
        detail_value = self._normalize_image_detail_value(
            self._get_image_detail_policy_value(model),
            model,
            credentials,
        )
        if detail_value == "preserve":
            return messages

        updated_messages = copy.deepcopy(messages)
        for message in updated_messages:
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "image_url":
                    continue
                image_url = part.get("image_url")
                if isinstance(image_url, dict):
                    if detail_value is None:
                        image_url.pop("detail", None)
                    else:
                        image_url["detail"] = detail_value
                elif isinstance(image_url, str) and detail_value is not None:
                    part["image_url"] = {"url": image_url, "detail": detail_value}
        return updated_messages

    def _hydrate_messages_with_files(
        self, messages, model, *, capability_profile=None
    ):
        """
        Iterates through messages and re-injects file content (Base64)
        if 'files' metadata is present. This ensures PDFs/Images persist
        across multi-turn conversations.
        """
        if capability_profile is not None:
            model_supports_vision = capability_profile.supports_vision
            model_supports_pdf = capability_profile.supports_pdf
            model_supports_audio_in = capability_profile.supports_audio_input
        else:
            model_record = resolve_model_record(model, user_id=self.user_id)
            model_lower = model.lower()
            if model_record:
                model_supports_vision = model_record.supports_vision
            else:
                model_supports_vision = any(
                    kw in model_lower
                    for kw in [
                        "vision",
                        "llava",
                        "gpt-4",
                        "gpt-5",
                        "gemini",
                        "claude-3",
                        "pixtral",
                    ]
                )
                if not model_supports_vision:
                    try:
                        model_supports_vision = litellm.supports_vision(model)
                    except Exception:
                        model_supports_vision = False

            if model_record:
                model_supports_pdf = model_record.supports_pdf
            else:
                model_supports_pdf = "gemini" in model_lower

            model_supports_audio_in = (
                model_record.supports_audio_in
                if model_record
                else ("audio" in model_lower)
            )

        base_uploads_dir = uploads_root()
        hydrated_messages = []

        for msg in messages:
            # Create a deep copy so we don't modify the object referenced by the DB session or previous iterations
            new_msg = copy.deepcopy(msg)

            files = new_msg.get("files", [])
            if not files:
                hydrated_messages.append(new_msg)
                continue

            # We have files to process for this message
            text_content_parts = []
            file_content_parts = []

            # Preserve existing text content
            existing_content = new_msg.get("content")
            if isinstance(existing_content, str):
                if existing_content:
                    text_content_parts.append(
                        {"type": "text", "text": existing_content}
                    )
            elif isinstance(existing_content, list):
                # Extract text parts. We assume file parts in existing content are stale/placeholder
                # and we rely on the 'files' metadata to reconstruct them freshly.
                text_content_parts.extend(
                    [part for part in existing_content if part.get("type") == "text"]
                )

            # Process files
            for file_info in files:
                pass_as = file_info.get("pass_as", "metadata")
                file_path_str_from_caller = file_info.get("filePath")
                file_name = file_info.get("fileName")
                collection_name = file_info.get("collectionName")

                if not file_path_str_from_caller or not file_name:
                    continue

                # Resolve path
                try:
                    local_path = resolve_upload_reference(
                        file_path_str_from_caller,
                        uploads_dir=base_uploads_dir,
                    )
                except TypeError, ValueError:
                    logging.warning(
                        "Skipping invalid attachment path: %s",
                        file_path_str_from_caller,
                    )
                    continue
                if local_path is None:
                    continue
                try:
                    file_path_str_for_prompt = upload_web_path(
                        local_path, uploads_dir=base_uploads_dir
                    )
                except ValueError:
                    file_path_str_for_prompt = file_name

                can_process_content = False
                is_text_based = local_path.suffix.lower() == ".md"

                mime_type, _ = mimetypes.guess_type(str(local_path))
                if mime_type:
                    is_image = mime_type.startswith("image/")
                    is_pdf = mime_type == "application/pdf"
                    is_audio = mime_type.startswith("audio/")

                    if not is_text_based:
                        is_text_based = mime_type in [
                            "text/plain",
                            "text/markdown",
                            "text/csv",
                        ]

                    if (
                        (is_image and model_supports_vision)
                        or (is_pdf and model_supports_pdf)
                        or (is_audio and model_supports_audio_in)
                    ):
                        can_process_content = True

                # Logic to attach content or metadata
                if (
                    pass_as in ["full_content", "pdf_native"]
                    and can_process_content
                    and local_path.exists()
                    and not is_text_based
                ):
                    try:
                        with open(local_path, "rb") as f:
                            encoded_string = base64.b64encode(f.read()).decode("utf-8")
                        data_uri = f"data:{mime_type};base64,{encoded_string}"

                        if is_pdf and model_supports_pdf:
                            file_content_parts.append(
                                {"type": "file", "file": {"file_data": data_uri}}
                            )
                        elif is_image and model_supports_vision:
                            file_content_parts.append(
                                {
                                    "type": "image_url",
                                    "image_url": {"url": data_uri, "detail": "high"},
                                }
                            )
                        elif is_audio and model_supports_audio_in:
                            # OpenAI/LiteLLM Audio Schema
                            audio_format = (
                                "mp3"
                                if "mpeg" in mime_type or "mp3" in mime_type
                                else "wav"
                            )
                            file_content_parts.append(
                                {
                                    "type": "input_audio",
                                    "input_audio": {
                                        "data": encoded_string,
                                        "format": audio_format,
                                    },
                                }
                            )

                        metadata_text = self._get_file_metadata_text(
                            local_path,
                            file_name,
                            file_path_str_for_prompt,
                            collection_name,
                        )
                        if text_content_parts:
                            text_content_parts[0]["text"] = (
                                f"[{metadata_text}]\n\n" + text_content_parts[0]["text"]
                            )
                        else:
                            text_content_parts.append(
                                {"type": "text", "text": f"[{metadata_text}]"}
                            )

                    except Exception as e:
                        logging.error(f"Error encoding file {local_path}: {e}")
                        metadata_text = self._get_file_metadata_text(
                            local_path,
                            file_name,
                            file_path_str_for_prompt,
                            collection_name,
                        )
                        text_content_parts.append(
                            {
                                "type": "text",
                                "text": f"\n\n[Attachment processing failed: {metadata_text}]",
                            }
                        )

                elif (
                    pass_as == "pdf_images"
                    and is_pdf
                    and model_supports_vision
                    and local_path.exists()
                ):
                    try:
                        import pypdfium2 as pdfium

                        doc = pdfium.PdfDocument(str(local_path))
                        try:
                            metadata_text = self._get_file_metadata_text(
                                local_path,
                                file_name,
                                file_path_str_for_prompt,
                                collection_name,
                            )
                            page_count = len(doc)
                            system_note = f"[SYSTEM NOTE: The attached file '{file_name}' has been provided below as {page_count} images. Do not attempt to use tools to view this file again.]\n\n[{metadata_text}]"
                            if text_content_parts:
                                text_content_parts[0]["text"] = (
                                    f"{system_note}\n\n" + text_content_parts[0]["text"]
                                )
                            else:
                                text_content_parts.append(
                                    {"type": "text", "text": system_note}
                                )

                            for page_num in range(page_count):
                                page = doc.get_page(page_num)
                                try:
                                    bitmap = page.render(scale=150 / 72.0)
                                    try:
                                        image = bitmap.to_pil().convert("RGB")
                                        try:
                                            with BytesIO() as buffer:
                                                image.save(
                                                    buffer, format="JPEG", quality=90
                                                )
                                                encoded_string = base64.b64encode(
                                                    buffer.getvalue()
                                                ).decode("utf-8")
                                        finally:
                                            image.close()
                                    finally:
                                        bitmap.close()
                                finally:
                                    page.close()
                                data_uri = f"data:image/jpeg;base64,{encoded_string}"
                                file_content_parts.append(
                                    {
                                        "type": "image_url",
                                        "image_url": {
                                            "url": data_uri,
                                            "detail": "high",
                                        },
                                    }
                                )
                        finally:
                            doc.close()
                    except Exception as e:
                        logging.error(
                            f"Error rendering PDF as images {local_path}: {e}"
                        )
                        text_content_parts.append(
                            {
                                "type": "text",
                                "text": f"\n\n[PDF to image conversion failed: {file_name}]",
                            }
                        )

                elif (
                    (pass_as.startswith("doc_engine:") or pass_as == "pdf_marker")
                    and is_pdf
                    and local_path.exists()
                ):
                    engine_id = (
                        "marker"
                        if pass_as == "pdf_marker"
                        else pass_as.split(":", 1)[1]
                    )
                    try:
                        addon_manager = getattr(current_app, "addon_manager", None)
                        processor_addon = (
                            addon_manager.loaded_addons.get("doc_processor")
                            if addon_manager
                            else None
                        )

                        if not processor_addon:
                            text_content_parts.append(
                                {
                                    "type": "text",
                                    "text": f"\n\n[Document Processor Hub is not active: {file_name}]",
                                }
                            )
                        else:
                            import time

                            start_time = time.time()
                            client = processor_addon.initialize_client(
                                {"current_user_id": self.user_id}
                            )
                            extraction_metrics = {}
                            extracted_markdown = client.parse_document(
                                filepath=str(local_path),
                                engine=engine_id,
                                metrics_dict=extraction_metrics,
                            )
                            duration_seconds = time.time() - start_time

                            # Immediately log the extraction cost to the database
                            self.log_external_cost(
                                cost=extraction_metrics.get("cost", 0.0),
                                prompt_tokens=extraction_metrics.get(
                                    "prompt_tokens", 0
                                ),
                                completion_tokens=extraction_metrics.get(
                                    "completion_tokens", 0
                                ),
                                total_tokens=extraction_metrics.get("total_tokens", 0),
                                model=f"doc_processor_{engine_id}",
                                source="document_extraction",
                            )

                            if extracted_markdown and not str(
                                extracted_markdown
                            ).startswith("Error:"):
                                metadata_text = self._get_file_metadata_text(
                                    local_path,
                                    file_name,
                                    file_path_str_for_prompt,
                                    collection_name,
                                )
                                metadata_text += f", Extraction Time: {duration_seconds:.1f}s, Engine: {engine_id}"
                                full_content_string = f"\n\n[SYSTEM NOTE: The following is the full extracted text content of the attached file '{file_name}' (via '{engine_id}'). Do not attempt to use tools to read this file again.]\n[{metadata_text}]\n\n---\n\n{extracted_markdown}"
                                if text_content_parts:
                                    text_content_parts[0]["text"] += full_content_string
                                else:
                                    text_content_parts.append(
                                        {
                                            "type": "text",
                                            "text": full_content_string.lstrip(),
                                        }
                                    )
                            else:
                                error_message = (
                                    str(extracted_markdown)
                                    if extracted_markdown
                                    else f"{engine_id} returned no content."
                                )
                                text_content_parts.append(
                                    {
                                        "type": "text",
                                        "text": f"\n\n[Extraction failed: {error_message}]",
                                    }
                                )
                    except Exception as e:
                        logging.error(
                            f"Error running Document Processor engine {engine_id} on {local_path}: {e}"
                        )
                        text_content_parts.append(
                            {
                                "type": "text",
                                "text": f"\n\n[Document Engine {engine_id} failed: {file_name}]",
                            }
                        )

                elif (
                    (
                        pass_as in ["full_content", "pdf_images"]
                        or pass_as.startswith("doc_engine:")
                        or pass_as == "pdf_marker"
                    )
                    and is_text_based
                    and local_path.exists()
                ):
                    try:
                        file_content = local_path.read_text(encoding="utf-8")
                        metadata_text = self._get_file_metadata_text(
                            local_path,
                            file_name,
                            file_path_str_for_prompt,
                            collection_name,
                        )
                        full_content_string = f"\n\n[SYSTEM NOTE: The following is the full text content of the attached file '{file_name}'. Do not attempt to use tools to read this file again.]\n[{metadata_text}]\n\n---\n\n{file_content}"

                        if text_content_parts:
                            text_content_parts[0]["text"] += full_content_string
                        else:
                            text_content_parts.append(
                                {"type": "text", "text": full_content_string.lstrip()}
                            )
                    except Exception as e:
                        logging.error(f"Error reading text file {local_path}: {e}")
                        metadata_text = self._get_file_metadata_text(
                            local_path,
                            file_name,
                            file_path_str_for_prompt,
                            collection_name,
                        )
                        text_content_parts.append(
                            {
                                "type": "text",
                                "text": f"\n\n[Reading failed: {metadata_text}]",
                            }
                        )

                else:  # Metadata only
                    metadata_text = self._get_file_metadata_text(
                        local_path, file_name, file_path_str_for_prompt, collection_name
                    )
                    if text_content_parts:
                        text_content_parts[0]["text"] = (
                            f"[{metadata_text}]\n\n" + text_content_parts[0]["text"]
                        )
                    else:
                        text_content_parts.append(
                            {"type": "text", "text": f"[{metadata_text}]"}
                        )

            # IMPROVEMENT: Send File parts FIRST, then Text parts.
            # This is best practice for Gemini and many vision models.
            new_msg["content"] = file_content_parts + text_content_parts
            hydrated_messages.append(new_msg)

        return hydrated_messages

    def _prepare_passive_admission(
        self,
        *,
        context,
        model,
        messages,
        tools,
        response_format,
        files,
        reasoning_slot,
        context_admission_mode,
        kwargs,
    ) -> tuple[str, list[dict], AdmissionResult]:
        """Prepare the exact provider-neutral payload used by passive calls."""

        from .core.executor_profile import workload_role_for_call

        role = workload_role_for_call(
            model=model,
            addon_id=context.addon_id,
            reasoning_slot=reasoning_slot,
        )
        capability_profile = context.executor_profile.capability_profile(role)

        # File metadata is merged into a private copy because passive previews
        # are read-only, and packet preparation must never alter the caller's
        # durable message objects.
        working_messages = copy.deepcopy(messages)
        if files:
            working_files = copy.deepcopy(files)
            last_user_msg = next(
                (msg for msg in reversed(working_messages) if msg["role"] == "user"),
                None,
            )
            if last_user_msg:
                existing_files = last_user_msg.get("files", [])
                existing_paths = {file_info["filePath"] for file_info in existing_files}
                for file_info in working_files:
                    if file_info["filePath"] not in existing_paths:
                        existing_files.append(file_info)
                last_user_msg["files"] = existing_files

        final_messages = self._hydrate_messages_with_files(
            working_messages,
            role,
            capability_profile=capability_profile,
        )
        admission_kwargs = {
            key: value
            for key, value in kwargs.items()
            if key != "enable_auto_compression"
        }
        requested_output_tokens = (
            admission_kwargs.get("max_completion_tokens")
            or admission_kwargs.get("max_output_tokens")
            or admission_kwargs.get("max_tokens")
        )
        admission_extras = {
            key: copy.deepcopy(admission_kwargs[key])
            for key in (
                "parallel_tool_calls",
                "tool_choice",
                "reasoning_effort",
                "modalities",
            )
            if key in admission_kwargs
        }
        admission_result = admit_provider_payload(
            profile=capability_profile,
            messages=final_messages,
            tools=copy.deepcopy(tools) if tools is not None else None,
            response_format=copy.deepcopy(response_format),
            extra_parameters=admission_extras,
            requested_output_tokens=requested_output_tokens,
            mode=context_admission_mode,
            # A passive role has no known provider tokenizer.  The
            # admission module uses its conservative chars/4 estimator.
            token_counter=None,
        )
        return role, final_messages, admission_result

    def preview_passive_admission(
        self,
        *,
        model,
        messages,
        tools=None,
        response_format=None,
        files=None,
        reasoning_slot=None,
        **kwargs,
    ) -> AdmissionResult:
        """Return passive context admission without creating external work."""

        from .core.mcp_completion import passive_execution_context

        context = passive_execution_context()
        if context is None:
            raise ValueError(
                "Passive MCP execution context is required for admission preview."
            )
        context_admission_mode = kwargs.get(
            "context_admission_mode", kwargs.get("context_stewardship", "assisted")
        )
        _, _, admission_result = self._prepare_passive_admission(
            context=context,
            model=model,
            messages=messages,
            tools=tools,
            response_format=response_format,
            files=files,
            reasoning_slot=reasoning_slot,
            context_admission_mode=context_admission_mode,
            kwargs=kwargs,
        )
        return admission_result

    def _passive_completion(
        self,
        *,
        context,
        model,
        messages,
        tools,
        response_format,
        files,
        enforce_context_admission,
        context_admission_mode,
        admission_sink,
        debug_capture_sink,
        debug_capture_include_content,
        source,
        reasoning_slot,
        kwargs,
    ):
        """Prepare provider-free passive work from the declared envelope."""

        from .core.mcp_completion import (
            build_passive_packet,
            resolve_passive_completion,
        )

        kwargs.pop("enable_auto_compression", None)
        role, final_messages, admission_result = self._prepare_passive_admission(
            context=context,
            model=model,
            messages=messages,
            tools=tools,
            response_format=response_format,
            files=files,
            reasoning_slot=reasoning_slot,
            context_admission_mode=context_admission_mode,
            kwargs=kwargs,
        )
        if isinstance(admission_sink, dict):
            admission_sink.clear()
            admission_sink.update(admission_result.to_dict())
        if isinstance(debug_capture_sink, dict):
            debug_capture_sink["schema_version"] = _DEBUG_CAPTURE_SCHEMA_VERSION
            debug_capture_sink["model"] = role
            if debug_capture_include_content:
                debug_capture_sink["messages"] = copy.deepcopy(final_messages)
                debug_capture_sink["tools"] = copy.deepcopy(tools) if tools else []
            debug_capture_sink.setdefault("provider_calls", [])
            debug_capture_sink["context_admission"] = admission_result.to_dict()
        if not admission_result.admitted and enforce_context_admission:
            raise ContextAdmissionError(admission_result)
        _apply_admitted_output_limit(
            kwargs,
            admitted_output_tokens=admission_result.requested_output_tokens,
        )
        packet = build_passive_packet(
            executor_profile=context.executor_profile,
            workload_role=role,
            messages=final_messages,
            tools=tools,
            response_format=response_format,
            parameters=kwargs,
            source=source,
        )
        return resolve_passive_completion(packet)

    def completion(
        self,
        model,
        messages,
        stream=False,
        tools=None,
        response_format=None,
        files: list = None,
        thinking_config: dict = None,
        source: str = "unknown",
        log_to_db: bool = True,
        reasoning_addon_id: str | None = None,
        reasoning_slot: str = "default",
        *,
        provider_max_attempts: int = 10,
        redact_provider_error_logs: bool = False,
        expected_api_key_id: int | None = None,
        **kwargs,
    ):
        """A unified method for all completion calls."""
        if (
            isinstance(provider_max_attempts, bool)
            or not isinstance(provider_max_attempts, int)
            or not 1 <= provider_max_attempts <= 10
        ):
            raise ValueError(
                "provider_max_attempts must be an integer from 1 through 10."
            )
        if not isinstance(redact_provider_error_logs, bool):
            raise TypeError("redact_provider_error_logs must be a boolean.")
        enforce_context_admission = bool(kwargs.pop("enforce_context_admission", True))
        context_admission_mode = kwargs.pop(
            "context_admission_mode",
            kwargs.pop("context_stewardship", "assisted"),
        )
        admission_sink = kwargs.pop("admission_sink", None)
        capability_overrides = kwargs.pop("model_capability_overrides", None)
        debug_capture_sink = kwargs.pop("debug_capture_sink", None)
        debug_capture_include_content = kwargs.pop(
            "debug_capture_include_content", True
        )
        if not isinstance(debug_capture_include_content, bool):
            raise TypeError("debug_capture_include_content must be a boolean.")
        call_cost_attribution = self.cost_attribution.merged(
            kwargs.pop("cost_attribution", None)
        )
        if not call_cost_attribution.action_id:
            call_cost_attribution = call_cost_attribution.merged(
                {
                    "action_id": f"llm_{uuid.uuid4().hex}",
                }
            )
        telemetry_sink = kwargs.pop("telemetry_sink", None)
        telemetry_call = None
        telemetry_started_at = None
        debug_provider_call = None
        attempt_metrics = {"attempts": 0, "rate_limit_429s": 0}
        from .core.mcp_completion import passive_execution_context

        passive_context = passive_execution_context()
        if passive_context is not None:
            if stream:
                raise ValueError(
                    "Passive MCP execution does not support streaming completions."
                )
            return self._passive_completion(
                context=passive_context,
                model=model,
                messages=messages,
                tools=tools,
                response_format=response_format,
                files=files,
                enforce_context_admission=enforce_context_admission,
                context_admission_mode=context_admission_mode,
                admission_sink=admission_sink,
                debug_capture_sink=debug_capture_sink,
                debug_capture_include_content=debug_capture_include_content,
                source=source,
                reasoning_slot=reasoning_slot,
                kwargs=kwargs,
            )

        logging.info(f"Making LiteLLM call to model: {model}")
        credentials = self._get_api_credentials(model)
        if expected_api_key_id is not None:
            actual_key_id = credentials.get("api_key_record_id")
            try:
                credential_matches = (
                    actual_key_id is not None
                    and not isinstance(expected_api_key_id, bool)
                    and int(actual_key_id) == int(expected_api_key_id)
                )
            except (TypeError, ValueError, OverflowError):
                credential_matches = False
            if not credential_matches:
                raise ValueError("Completion credential changed before provider I/O.")
        provider_name = credentials.get("provider_name")
        # 1. Update the last message in 'messages' with the current files (if any came from client)
        if files:
            last_user_msg = next(
                (msg for msg in reversed(messages) if msg["role"] == "user"), None
            )
            if last_user_msg:
                # Merge the new files into the message dictionary so hydration can pick them up
                existing_files = last_user_msg.get("files", [])
                # Avoid duplicates
                existing_paths = {f["filePath"] for f in existing_files}
                for f in files:
                    if f["filePath"] not in existing_paths:
                        existing_files.append(f)
                last_user_msg["files"] = existing_files

        # 2. Hydrate ALL messages (history + current) with file content from disk
        final_messages = self._hydrate_messages_with_files(messages, model)

        # Security Fix: Create a clean copy strictly for the network request
        llm_request_messages = final_messages

        llm_request_messages = self._apply_image_detail_policy(
            llm_request_messages,
            model,
            credentials,
        )

        # Legacy silent auto-compression was intentionally removed. Chat/runtime
        # context optimization now happens before this provider boundary through
        # explicit tool-result envelopes and retention metadata.
        kwargs.pop("enable_auto_compression", None)
        if isinstance(debug_capture_sink, dict):
            debug_capture_sink.setdefault(
                "schema_version", _DEBUG_CAPTURE_SCHEMA_VERSION
            )
            debug_capture_sink["model"] = model
            if debug_capture_include_content:
                debug_capture_sink["messages"] = copy.deepcopy(llm_request_messages)
                debug_capture_sink["tools"] = copy.deepcopy(tools) if tools else []
            debug_capture_sink.setdefault("provider_calls", [])

        try:
            user = db.session.get(User, self.user_id) if self.user_id else None
            thinking_config = resolve_reasoning_config(
                user,
                model,
                thinking_config,
                addon_id=reasoning_addon_id,
                reasoning_slot=reasoning_slot,
            )
            if thinking_config:
                scope = (
                    f"{reasoning_addon_id}:{reasoning_slot}"
                    if reasoning_addon_id
                    else "chat"
                )
                logging.info(
                    f"Applying {scope} reasoning config for {model}: {thinking_config}"
                )

            litellm_model = normalize_litellm_model_id(
                model,
                provider_name=credentials.get("provider_name"),
                provider_kind=credentials.get("provider_kind"),
            )
            model_record = resolve_model_record(model, user_id=self.user_id)
            capability_profile = resolve_model_capabilities(
                model,
                user_id=self.user_id,
                model_record=model_record,
                overrides=(
                    capability_overrides
                    if isinstance(capability_overrides, dict)
                    else None
                ),
            )
            requested_output_tokens = (
                kwargs.get("max_completion_tokens")
                or kwargs.get("max_output_tokens")
                or kwargs.get("max_tokens")
            )
            admission_extras = {
                key: kwargs[key]
                for key in (
                    "parallel_tool_calls",
                    "tool_choice",
                    "reasoning_effort",
                    "modalities",
                )
                if key in kwargs
            }
            if thinking_config:
                admission_extras["thinking_config"] = thinking_config
            admission_result = admit_provider_payload(
                profile=capability_profile,
                messages=llm_request_messages,
                tools=tools,
                response_format=response_format,
                extra_parameters=admission_extras,
                requested_output_tokens=requested_output_tokens,
                mode=context_admission_mode,
                token_counter=litellm.token_counter,
            )
            if isinstance(admission_sink, dict):
                admission_sink.clear()
                admission_sink.update(admission_result.to_dict())
            if isinstance(debug_capture_sink, dict):
                debug_capture_sink["context_admission"] = admission_result.to_dict()
            if not admission_result.admitted and enforce_context_admission:
                raise ContextAdmissionError(admission_result)
            _apply_admitted_output_limit(
                kwargs,
                admitted_output_tokens=admission_result.requested_output_tokens,
            )

            litellm_params = {
                "model": litellm_model,
                "messages": llm_request_messages,
                "stream": stream,
                "tools": tools,
                "response_format": response_format,
                "api_key": credentials.get("api_key"),
                "api_base": credentials.get("api_base"),
                "num_retries": 0,  # Handled manually via Tenacity below
                "drop_params": True,
                **kwargs,
            }
            if credentials.get("api_version"):
                litellm_params["api_version"] = credentials.get("api_version")

            if credentials.get("provider_name") == "vertex_ai":
                litellm_params["vertex_project"] = credentials.get("vertex_project")
                litellm_params["vertex_location"] = credentials.get("vertex_location")
                litellm_params["vertex_credentials"] = credentials.get(
                    "api_key"
                )  # JSON string
                litellm_params.pop("api_key", None)
                litellm_params.pop("api_base", None)

            # --- Dynamic Reasoning Configuration ---
            # Modern LiteLLM abstracts this nicely. If the user selected a level
            # other than 'default', we pass it. LiteLLM routes it to the correct
            # provider parameter (e.g., 'thinking' for Anthropic/Google, 'reasoning_effort' for OpenAI).
            if thinking_config and isinstance(thinking_config, dict):
                level = thinking_config.get("level", "default")
                if level and level != "default":
                    p_name = credentials.get("provider_name") or provider_name
                    if level == "off":
                        litellm_params["reasoning_effort"] = "none"
                    elif p_name == "openai":
                        # OpenAI o-series only supports low, medium, high
                        mapping = {
                            "minimal": "low",
                            "low": "low",
                            "medium": "medium",
                            "high": "high",
                            "xhigh": "high",
                        }
                        litellm_params["reasoning_effort"] = mapping.get(level, level)
                    else:
                        litellm_params["reasoning_effort"] = level
                    logging.info(
                        f"Setting reasoning_effort='{litellm_params['reasoning_effort']}' for model {model}."
                    )

            telemetry_call = begin_model_call(
                telemetry_sink,
                model=model,
                source=source,
                payload=measure_prompt_payload(
                    model=litellm_model,
                    messages=llm_request_messages,
                    tools=tools,
                    token_counter=litellm.token_counter,
                ),
            )

            if isinstance(debug_capture_sink, dict):
                safe_parameters = {
                    key: copy.deepcopy(litellm_params[key])
                    for key in sorted(_DEBUG_PROVIDER_PARAMETER_NAMES)
                    if key in litellm_params
                }
                debug_provider_call = {
                    "application_model": model,
                    "litellm_model": litellm_model,
                    "provider_name": credentials.get("provider_name"),
                    "provider_kind": credentials.get("provider_kind"),
                    "api_base": _debug_api_base(credentials.get("api_base")),
                    "parameter_names": sorted(
                        key
                        for key in litellm_params
                        if key not in _DEBUG_CAPTURE_EXCLUDED_PARAMETER_NAMES
                    ),
                    "parameters": safe_parameters,
                    "prompt_cache_key_set": bool(
                        litellm_params.get("prompt_cache_key")
                    ),
                    "message_manifest": _debug_message_manifest(llm_request_messages),
                    "tool_count": len(tools or []),
                    "outcome": {"status": "pending"},
                }
                debug_capture_sink["provider_calls"].append(debug_provider_call)

            # Define robust retry mechanism using tenacity. LiteLLM's internal
            # num_retries doesn't always guarantee a 60s wait for TPM lockouts.
            retry_log = logging.getLogger(__name__)
            retry_logger = (
                None
                if redact_provider_error_logs
                else tenacity.before_sleep_log(retry_log, logging.WARNING)
            )

            def record_attempt(retry_state):
                attempt_metrics["attempts"] = max(
                    attempt_metrics["attempts"],
                    int(retry_state.attempt_number or 0),
                )

            def record_retry_before_sleep(retry_state):
                exception = retry_state.outcome.exception()
                if isinstance(exception, litellm.RateLimitError):
                    attempt_metrics["rate_limit_429s"] += 1
                if redact_provider_error_logs:
                    retry_log.warning(
                        "Retrying provider call: type=%s source=%s attempt=%s",
                        type(exception).__name__,
                        source,
                        int(retry_state.attempt_number or 0),
                    )
                elif retry_logger is not None:
                    retry_logger(retry_state)

            retryable_types = (
                litellm.RateLimitError,
                litellm.Timeout,
                litellm.APIConnectionError,
                litellm.InternalServerError,
                litellm.ServiceUnavailableError,
            )

            def should_retry_provider_exception(exception):
                return isinstance(
                    exception, retryable_types
                ) and not is_prompt_cache_configuration_error(exception, litellm_params)

            retryer = tenacity.Retrying(
                stop=tenacity.stop_after_attempt(provider_max_attempts),
                wait=get_rate_limit_wait,
                retry=tenacity.retry_if_exception(should_retry_provider_exception),
                before=record_attempt,
                before_sleep=record_retry_before_sleep,
                reraise=True,
            )

            telemetry_started_at = time.perf_counter()
            response = retryer(litellm.completion, **litellm_params)

            if isinstance(debug_provider_call, dict):
                debug_provider_call["outcome"] = {
                    "status": "completed",
                    "latency_ms": (time.perf_counter() - telemetry_started_at) * 1000,
                    "attempts": attempt_metrics["attempts"],
                    "rate_limit_429s": attempt_metrics["rate_limit_429s"],
                    "response": _debug_provider_response(response),
                }

            # --- Dynamic Pricing Override ---
            cost = self.calculate_response_cost(response, model)
            finish_model_call(
                telemetry_sink,
                telemetry_call,
                response=response,
                cost=cost,
                latency_ms=(time.perf_counter() - telemetry_started_at) * 1000,
                attempts=attempt_metrics["attempts"],
                rate_limit_429s=attempt_metrics["rate_limit_429s"],
            )

            if log_to_db and cost is not None and cost >= 0 and response.usage:
                try:
                    cost_entry = LLMCost(
                        user_id=self.user_id,
                        cost=cost,
                        prompt_tokens=response.usage.prompt_tokens,
                        completion_tokens=response.usage.completion_tokens,
                        total_tokens=response.usage.total_tokens,
                        model=model,
                        source=source,
                        **self._validated_cost_attribution(call_cost_attribution),
                    )
                    db.session.add(cost_entry)

                    from iarf_assistant.database_utils import commit_with_retry

                    commit_with_retry(db.session)  # Commit safely with backoff
                except Exception as db_error:
                    db.session.rollback()
                    logging.error(f"Failed to log LLM cost to database: {db_error}")

            return response
        except Exception as e:
            final_rate_limit = int(isinstance(e, litellm.RateLimitError))
            if (
                isinstance(debug_provider_call, dict)
                and debug_provider_call.get("outcome", {}).get("status") == "pending"
            ):
                debug_provider_call["outcome"] = {
                    "status": "failed",
                    "latency_ms": (
                        (time.perf_counter() - telemetry_started_at) * 1000
                        if telemetry_started_at is not None
                        else 0
                    ),
                    "attempts": attempt_metrics["attempts"],
                    "rate_limit_429s": attempt_metrics["rate_limit_429s"]
                    + final_rate_limit,
                    "error_type": type(e).__name__,
                    "status_code": _debug_error_status(e),
                }
            if telemetry_call is not None and telemetry_call.get("status") == "pending":
                finish_model_call(
                    telemetry_sink,
                    telemetry_call,
                    latency_ms=(
                        (time.perf_counter() - telemetry_started_at) * 1000
                        if telemetry_started_at is not None
                        else 0
                    ),
                    attempts=attempt_metrics["attempts"],
                    rate_limit_429s=attempt_metrics["rate_limit_429s"]
                    + final_rate_limit,
                    error=e,
                )
            if redact_provider_error_logs:
                logging.getLogger(__name__).error(
                    "LiteLLM completion error: type=%s source=%s",
                    type(e).__name__,
                    source,
                )
            else:
                logging.error(f"LiteLLM completion error: {e}")
            raise e

    def embedding(
        self,
        model,
        input,
        *,
        source: str = "embedding",
        log_to_db: bool = True,
        **kwargs,
    ):
        """A unified method for all embedding calls."""
        logging.info(f"Making LiteLLM embedding call to model: {model}")
        expected_api_key_id = kwargs.pop("expected_api_key_id", None)
        call_cost_attribution = self.cost_attribution.merged(
            kwargs.pop("cost_attribution", None)
        )
        if not call_cost_attribution.action_id:
            call_cost_attribution = call_cost_attribution.merged(
                {
                    "action_id": f"embedding_{uuid.uuid4().hex}",
                }
            )
        credentials = self._get_api_credentials(model)
        if expected_api_key_id is not None:
            actual_key = credentials.get("api_key_record_id")
            if actual_key is None or int(actual_key) != int(expected_api_key_id):
                raise ValueError("Embedding credential changed before provider I/O.")

        litellm_model = normalize_litellm_model_id(
            model,
            provider_name=credentials.get("provider_name"),
            provider_kind=credentials.get("provider_kind"),
        )

        try:
            requested_dim = kwargs.get("dimensions")
            embedding_profile = kwargs.pop("embedding_profile", None)
            if embedding_profile is None:
                from .embedding_profiles import resolve_embedding_profile

                task_type = kwargs.get("task_type", "search")
                embedding_profile = resolve_embedding_profile(
                    model=model,
                    task_type=task_type,
                    user_id=self.user_id,
                )

            litellm_params = {
                "model": litellm_model,
                "input": input,
                "api_key": credentials.get("api_key"),
                "api_base": credentials.get("api_base"),
                "drop_params": True,
                "num_retries": 0,  # Handled manually via Tenacity below
                **kwargs,
            }

            litellm_params = embedding_profile.sanitize_litellm_params(litellm_params)

            if credentials.get("api_version"):
                litellm_params["api_version"] = credentials.get("api_version")

            if credentials.get("provider_name") == "vertex_ai":
                litellm_params["vertex_project"] = credentials.get("vertex_project")
                litellm_params["vertex_location"] = credentials.get("vertex_location")
                litellm_params["vertex_credentials"] = credentials.get("api_key")
                litellm_params.pop("api_key", None)
                litellm_params.pop("api_base", None)

            retryer = tenacity.Retrying(
                stop=tenacity.stop_after_attempt(10),
                wait=get_rate_limit_wait,
                retry=tenacity.retry_if_exception_type(
                    (
                        litellm.RateLimitError,
                        litellm.Timeout,
                        litellm.APIConnectionError,
                        litellm.InternalServerError,
                    )
                ),
                before_sleep=tenacity.before_sleep_log(
                    logging.getLogger(__name__), logging.WARNING
                ),
                reraise=True,
            )

            response = retryer(litellm.embedding, **litellm_params)

            response = embedding_profile.postprocess_response(
                response, requested_dim=requested_dim
            )

            usage = getattr(response, "usage", None)
            if log_to_db and usage is not None:
                if isinstance(usage, dict):
                    prompt_tokens = int(usage.get("prompt_tokens") or 0)
                    total_tokens = int(usage.get("total_tokens") or prompt_tokens)
                else:
                    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
                    total_tokens = int(
                        getattr(usage, "total_tokens", 0) or prompt_tokens
                    )
                cost = self.calculate_response_cost(response, model) or 0.0
                if prompt_tokens or total_tokens or cost:
                    cost_entry = LLMCost(
                        user_id=self.user_id,
                        cost=cost,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=0,
                        total_tokens=total_tokens,
                        model=model,
                        source=source,
                        **self._validated_cost_attribution(call_cost_attribution),
                    )
                    db.session.add(cost_entry)
                    from iarf_assistant.database_utils import commit_with_retry

                    commit_with_retry(db.session)

            return response
        except Exception as e:
            logging.error(f"LiteLLM embedding error: {e}")
            raise e

    def token_counter(self, model, text=None, messages=None):
        """Count either plain text or provider-formatted messages."""
        provider = None
        if "/" in model:
            provider = LLMProvider.query.filter_by(
                name=model.split("/", 1)[0].lower()
            ).first()
        litellm_model = normalize_litellm_model_id(
            model,
            provider_name=getattr(provider, "name", None),
            provider_kind=getattr(provider, "provider_kind", None),
        )

        try:
            if messages is not None:
                return litellm.token_counter(model=litellm_model, messages=messages)
            material = "" if text is None else text
            return litellm.token_counter(model=litellm_model, text=material)
        except Exception as e:
            logging.error(f"LiteLLM token_counter error for model {model}: {e}")
            material = messages if messages is not None else text
            try:
                rendered = json.dumps(material, ensure_ascii=False, default=str)
            except (TypeError, ValueError, RecursionError):
                rendered = str(material or "")
            return max(0, (len(rendered) + 3) // 4)

    def calculate_response_cost(self, completion_response, model=None):
        """Calculates the cost of a completion, respecting custom/pinned pricing."""
        if not completion_response:
            return 0.0

        target_model = model or getattr(completion_response, "model", None)
        if not target_model:
            return 0.0

        # Resolve user-scoped or global model record
        try:
            model_record = resolve_model_record(
                target_model,
                user_id=self.user_id,
            )
        except Exception as e:
            logging.error(f"Error querying model record for cost calculation: {e}")
            model_record = None

        # Check if the record exists and has custom pricing defined
        if model_record and (
            model_record.input_cost_per_million_tokens is not None
            or model_record.output_cost_per_million_tokens is not None
        ):
            in_cost = model_record.input_cost_per_million_tokens or 0.0
            out_cost = model_record.output_cost_per_million_tokens or 0.0

            # Determine cached input cost: use explicitly defined value if available,
            # otherwise fall back to 25% of standard input cost
            if model_record.input_cost_per_million_cached_tokens is not None:
                cached_in_cost = model_record.input_cost_per_million_cached_tokens
            else:
                cached_in_cost = in_cost * 0.25

            usage = getattr(completion_response, "usage", None)
            if usage:
                if isinstance(usage, dict):
                    prompt_tokens = usage.get("prompt_tokens", 0) or 0
                    completion_tokens = usage.get("completion_tokens", 0) or 0
                else:
                    prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
                    completion_tokens = getattr(usage, "completion_tokens", 0) or 0

                # Retrieve cached tokens (if any) from LiteLLM usage details
                prompt_details = getattr(usage, "prompt_tokens_details", None)
                cached_tokens = 0
                if prompt_details:
                    cached_tokens = getattr(prompt_details, "cached_tokens", 0) or 0
                    if not cached_tokens and isinstance(prompt_details, dict):
                        cached_tokens = prompt_details.get("cached_tokens", 0) or 0

                normal_prompt_tokens = max(0, prompt_tokens - cached_tokens)

                return (
                    (normal_prompt_tokens * in_cost)
                    + (cached_tokens * cached_in_cost)
                    + (completion_tokens * out_cost)
                ) / 1_000_000.0

        return self.get_cost(completion_response)

    @staticmethod
    def get_cost(completion_response):
        """Calculates the cost of a completion using LiteLLM's built-in function."""
        try:
            return litellm.completion_cost(completion_response=completion_response)
        except Exception:
            # completion_cost can fail if model is not in pricing sheet
            return 0.0
