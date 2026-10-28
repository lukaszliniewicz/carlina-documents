"""Pure checks against the exact leased single-copy packet, before acceptance.

This is not full product validation or a commit. The worker remains authoritative
for original-source integrity, candidate rendering, notes and persisted targets.
No database, source-plan reload, provider call or punctuation splitter belongs here.
"""
from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from typing import Any

from .block_contract import BlockContractError
from .compact_markup import MAX_SENTENCE_MARKERS, parse_compact_fragment
from .sentence_alignment import _manifest_groups
from .window_contract import (
    LOCATED_SOURCE_WRITABLE_CONTRACT_VERSION,
    SINGLE_COPY_WRITABLE_SOURCE_CONTRACT_VERSION,
)

PREFLIGHT_CONTRACT = "translator-packet-preflight-v1"
_VOID = {"br", "img", "hr", "wbr", "input", "area", "base", "embed", "link", "meta", "param", "source", "track", "col"}


def _inventory(root: Any) -> tuple[Counter, dict[str, str]]:
    elements: Counter = Counter()
    literals = {}
    for node in root.iter():
        if node is root or node.tag == "sb":
            continue
        if not isinstance(node.tag, str) or set(node.attrib) - {"r"}:
            raise BlockContractError("compact-markup-attribute")
        elements[(node.tag, tuple(sorted(node.attrib.items())))] += 1
        if node.tag in _VOID and (node.text or len(node)):
            raise BlockContractError("compact-void-content")
        if node.tag == "lit":
            if len(node) or set(node.attrib) != {"r"} or node.attrib["r"] in literals:
                raise BlockContractError("compact-literal-shape")
            literals[node.attrib["r"]] = node.text or ""
    return elements, literals


def _units(root: Any, *, field: bool = False) -> int:
    count = 0
    pending: list[str] = []

    def walk(node: Any) -> None:
        nonlocal count
        if node.tag == "sb":
            if node.attrib or node.text or len(node):
                raise BlockContractError("sentence-marker-shape")
            if not "".join(pending).strip():
                raise BlockContractError("sentence-marker-empty")
            pending.clear()
            count += 1
            if count > MAX_SENTENCE_MARKERS:
                raise BlockContractError("sentence-marker-limit")
            return
        if field and node is not root:
            raise BlockContractError("sentence-marker-shape")
        if node.tag in _VOID and (node.text or len(node)):
            raise BlockContractError("compact-void-content")
        if node.tag == "lit" and len(node):
            raise BlockContractError("compact-literal-shape")
        pending.append(node.text or "")
        for child in node:
            walk(child)
            pending.append(child.tail or "")
        if node.tag == "br":
            pending.append("\n")

    try:
        walk(root)
    except RecursionError as exc:
        raise BlockContractError("compact-markup-depth") from exc
    if not count:
        raise BlockContractError("sentence-marker-missing")
    if "".join(pending).strip():
        raise BlockContractError("sentence-marker-final")
    return count


def _block(source: Mapping[str, Any], target: Mapping[str, Any]) -> None:
    if target.get("n") != source["n"] or type(target.get("n")) is not int:
        raise BlockContractError("compact-result-number")
    source_root = parse_compact_fragment(source["content"])
    target_root = parse_compact_fragment(target["content"], marked=True)
    expected_elements, expected_literals = _inventory(source_root)
    elements, literals = _inventory(target_root)
    if elements != expected_elements:
        raise BlockContractError("compact-markup-cardinality")
    if literals != expected_literals:
        raise BlockContractError("compact-literal-value")
    fields = target.get("fields")
    if not isinstance(fields, list) or Counter(item["name"] for item in fields) != Counter(
        item["name"] for item in source["fields"]
    ):
        raise BlockContractError("compact-field-cardinality")
    body_text = "".join(target_root.itertext()).strip()
    if not body_text and fields:
        if any(node.tag == "sb" for node in target_root.iter()):
            raise BlockContractError("sentence-marker-empty")
        count = sum(_units(parse_compact_fragment(item["value"], marked=True), field=True) for item in fields)
    else:
        count = _units(target_root)
    alignment = target.get("alignment")
    if not isinstance(alignment, Mapping) or set(alignment) != {"groups"}:
        raise BlockContractError("sentence-alignment-shape")
    _manifest_groups(alignment["groups"], source_count=len(source["sentences"]), target_count=count)


def _request_diagnostic() -> dict[str, Any]:
    return {"contract": PREFLIGHT_CONTRACT, "scope": "leased_packet_only",
            "validated_blocks": 0,
            "validation_errors": [{"path": "/request/messages", "code": "window-request-contract"}]}


def _response_diagnostic() -> dict[str, Any]:
    return {"contract": PREFLIGHT_CONTRACT, "scope": "leased_packet_only",
            "validated_blocks": 0,
            "validation_errors": [{"path": "/content/results", "code": "window-response-contract"}]}


def preflight_completion(request: Mapping[str, Any], response: Mapping[str, Any]) -> dict[str, Any] | None:
    if not isinstance(request, Mapping) or request.get("source") != "translator_block_translation_window":
        return None
    messages = request.get("messages", [])
    if not isinstance(messages, list):
        return _request_diagnostic()
    # Last user-role writable message wins. The delimiter inside valid JSON is
    # escaped (json.dumps emits backslash-n, not a literal newline), so an
    # embedded source string cannot pose as the outer envelope; rsplit on the
    # literal newline delimiter is safe and no source text is logged.
    candidate_raw: str | None = None
    for message in messages:
        if not isinstance(message, Mapping) or message.get("role") != "user":
            continue
        text = message.get("content")
        if not isinstance(text, str) or "\n[writable_source target_language=" not in text:
            continue
        tail = text.rsplit("\n[writable_source target_language=", 1)[1]
        _language, separator, raw = tail.partition("]\n")
        if separator:
            candidate_raw = raw
    if candidate_raw is None:
        return None  # historical transports retain their original worker path
    try:
        writable = json.loads(candidate_raw)
    except (ValueError, TypeError, RecursionError):
        return _request_diagnostic()
    if not isinstance(writable, Mapping) or writable.get("contract_version") not in {
        SINGLE_COPY_WRITABLE_SOURCE_CONTRACT_VERSION,
        LOCATED_SOURCE_WRITABLE_CONTRACT_VERSION,
    }:
        return None  # historical transports retain their original worker path
    if not isinstance(response, Mapping):
        return _response_diagnostic()
    value = response.get("content")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError, RecursionError):
            return _response_diagnostic()
    blocks = writable.get("blocks")
    results = value.get("results") if isinstance(value, Mapping) else None
    errors = []
    if not isinstance(results, list) or not isinstance(blocks, list) or len(results) != len(blocks):
        errors.append({"path": "/content/results", "code": "window-response-cardinality"})
    else:
        for index, (source, target) in enumerate(zip(blocks, results, strict=True)):
            try:
                _block(source, target)
            except (BlockContractError, ValueError, TypeError, KeyError) as exc:
                code = exc.code if isinstance(exc, BlockContractError) else "window-response-contract"
                errors.append({"path": f"/content/results/{index}", "code": code, "block_number": index + 1})
            if len(errors) >= 20:
                break
    return {"contract": PREFLIGHT_CONTRACT, "scope": "leased_packet_only",
            "validated_blocks": len(blocks) if not errors else 0, "validation_errors": errors}
