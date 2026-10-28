"""Lossless, selectable worker views of frozen MCP requests.

Omission changes transport only. Hashes let a worker check reused instructions;
the exact request remains available under the same current lease.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .mcp_retained_context import project_retained_context

MODES = frozenset({"standard", "compact", "minimal", "full"})
OMIT_FIELDS = frozenset(
    {
        "system_prompt",
        "messages",
        "images",
        "response_format",
        "parameters",
        "tools",
        "submission_contract",
    }
)

# A transport budget, independent of the executor's token/context capacity.
DEFAULT_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
LEASE_REFRESH_GUIDANCE = (
    "Read the exact leased instructions and schema before authoring. Matching hashes or a wrapper/JavaScript cache alone do not "
    "prove the executing model retained them. After compaction, transfer, restart, hash change or uncertainty, call "
    "carlina_work_get with the same work_item_id and lease_token for omitted paths and missing required context, then read it "
    "before authoring; no new claim or restart is needed."
)


def validate_response_budget(max_response_bytes: int) -> None:
    if (
        type(max_response_bytes) is not int
        or not 65536 <= max_response_bytes <= MAX_RESPONSE_BYTES
    ):
        raise ValueError(
            "max_response_bytes must be an integer between 65536 and 67108864."
        )


def resolve_response_budget(
    requested: int | None,
    *,
    default_bytes: int,
    max_bytes: int,
) -> int:
    """Resolve a call override within the authenticated connection's ceiling."""
    validate_response_budget(default_bytes)
    validate_response_budget(max_bytes)
    if default_bytes > max_bytes:
        raise ValueError("The connection response default exceeds its maximum.")
    budget = default_bytes if requested is None else requested
    validate_response_budget(budget)
    if budget > max_bytes:
        raise ValueError(
            f"max_response_bytes exceeds this connection's configured maximum ({max_bytes})."
        )
    return budget


def request_images(request: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Enumerate immutable image blocks in request order, without copying bytes."""
    images: list[tuple[str, dict[str, Any]]] = []

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            if value.get("type") == "image_url" and isinstance(
                value.get("image_url"), dict
            ):
                images.append((path, value))
                return
            for key, child in value.items():
                visit(child, path + "/" + key.replace("~", "~0").replace("/", "~1"))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}/{index}")

    visit(request, "/request")
    return images


def deferred_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Last-resort small receipt; every omitted field remains retrievable."""
    result = {
        key: payload[key]
        for key in (
            "work_item_id",
            "execution_id",
            "lease_token",
            "lease_expires_at",
            "workload_role",
            "expected_output_format",
            "batch_id",
        )
        if key in payload
    }
    result["request"] = {}
    result["projection"] = {
        "mode": "minimal",
        "omitted": [_reference(payload.get("request", {}), "/request")],
        "retrieve_tool": "carlina_work_get",
        "guidance": (
            f"{LEASE_REFRESH_GUIDANCE} Retrieve /request with path, offset and max_chars; "
            "follow all slices and image batches before answering."
        ),
    }
    return result


def validate_projection(mode: str, omit_fields: list[str] | None) -> None:
    if mode not in MODES:
        raise ValueError("response_mode must be standard, compact, minimal or full.")
    if omit_fields is not None and (
        not isinstance(omit_fields, list)
        or len(omit_fields) > len(OMIT_FIELDS)
        or any(
            not isinstance(key, str) or key not in OMIT_FIELDS for key in omit_fields
        )
    ):
        raise ValueError(
            "Unsupported omit_fields. Use system_prompt, messages, images, response_format, parameters, tools or submission_contract."
        )


def _encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _reference(value: Any, path: str) -> dict[str, Any]:
    serialized = _encoded(value)
    return {
        "omitted": True,
        "path": path,
        "sha256": hashlib.sha256(serialized.encode()).hexdigest(),
        "json_chars": len(serialized),
    }


def project_claim(
    payload: dict[str, Any],
    mode: str,
    omit_fields: list[str] | None,
    *,
    known_context: dict[str, str] | None = None,
    known_target_revisions: list[str] | None = None,
) -> dict[str, Any]:
    validate_projection(mode, omit_fields)
    projected = project_retained_context(
        payload,
        mode=mode,
        known_context=known_context,
        known_target_revisions=known_target_revisions,
    )
    omit = set(omit_fields or [])
    if mode == "compact":
        omit.update({"system_prompt", "parameters", "submission_contract"})
    elif mode == "minimal":
        omit.update(OMIT_FIELDS - {"system_prompt", "images"})
    if not omit:
        return projected
    result = dict(projected)
    request = dict(result.get("request") or {})
    existing_projection = result.get("projection")
    if not isinstance(existing_projection, dict):
        existing_projection = {}
    omitted: list[dict[str, Any]] = list(existing_projection.get("omitted", []))

    def placeholder(value: Any, path: str):
        ref = _reference(value, path)
        omitted.append(ref)
        return ref

    for field in sorted(omit - {"system_prompt", "images"}):
        if field in request:
            placeholder(request.pop(field), f"/request/{field}")
    if "messages" in request:
        messages = []
        for index, message in enumerate(request["messages"]):
            path = f"/request/messages/{index}"
            if (
                "system_prompt" in omit
                and isinstance(message, dict)
                and message.get("role") in {"system", "developer"}
            ):
                messages.append(placeholder(message, path))
            else:
                messages.append(message)
        request["messages"] = messages
    if "images" in omit:

        def without_images(value: Any, path: str) -> Any:
            if isinstance(value, dict):
                if value.get("type") == "image_url":
                    return placeholder(value, path)
                return {
                    key: without_images(
                        item, path + "/" + key.replace("~", "~0").replace("/", "~1")
                    )
                    for key, item in value.items()
                }
            if isinstance(value, list):
                return [
                    without_images(item, f"{path}/{i}") for i, item in enumerate(value)
                ]
            return value

        request = without_images(request, "/request")
    result["request"] = request
    result["projection"] = {
        **existing_projection,
        "mode": mode,
        "omitted": omitted,
        "retrieve_tool": "carlina_work_get",
        "guidance": LEASE_REFRESH_GUIDANCE,
    }
    return result


def request_slice(
    payload: dict[str, Any], path: str, offset: int = 0, max_chars: int = 12000
) -> dict[str, Any]:
    """Read one request field. Large JSON becomes explicitly paginated pretty text."""
    if (
        not isinstance(path, str)
        or len(path) > 512
        or not (path == "/request" or path.startswith("/request/"))
    ):
        raise ValueError("path must be a JSON pointer inside /request.")
    if (
        type(offset) is not int
        or offset < 0
        or type(max_chars) is not int
        or not 256 <= max_chars <= 64000
    ):
        raise ValueError("offset must be nonnegative; max_chars must be 256-64000.")
    value: Any = payload
    try:
        for token in path[1:].split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            if isinstance(value, list):
                if not token.isdigit():
                    raise KeyError(token)
                value = value[int(token)]
            else:
                value = value[token]
    except KeyError, IndexError, TypeError, ValueError:
        raise ValueError("Requested path does not exist in this packet.") from None
    result = {
        key: payload[key]
        for key in ("work_item_id", "execution_id", "lease_expires_at")
        if key in payload
    }
    result["path"] = path
    if isinstance(value, dict) and value.get("type") == "image_url":
        if offset:
            raise ValueError("Images cannot be sliced as text.")
        result["value"] = value
        return result
    reference = _reference(value, path)
    if isinstance(value, str) and value.startswith("data:") and ";base64," in value:
        raise ValueError(
            "Fetch the parent image content block for native image transport, not its raw data URL."
        )

    def image_references(item: Any, pointer: str) -> Any:
        if isinstance(item, dict):
            if item.get("type") == "image_url":
                return _reference(item, pointer)
            return {
                key: image_references(
                    child, pointer + "/" + key.replace("~", "~0").replace("/", "~1")
                )
                for key, child in item.items()
            }
        if isinstance(item, list):
            return [
                image_references(child, f"{pointer}/{i}")
                for i, child in enumerate(item)
            ]
        return item

    value = image_references(value, path)
    text = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, indent=2)
    )
    result.update(
        {
            "total_chars": len(text),
            "sha256": reference["sha256"],
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
    )
    if offset == 0 and len(text) <= max_chars:
        result["value"] = value
        result["truncated"] = False
    else:
        result.update(
            {
                "text": text[offset : offset + max_chars],
                "offset": offset,
                "next_offset": offset + max_chars
                if offset + max_chars < len(text)
                else None,
                "truncated": offset + max_chars < len(text),
                "encoding": "text" if isinstance(value, str) else "json",
            }
        )
    return result
