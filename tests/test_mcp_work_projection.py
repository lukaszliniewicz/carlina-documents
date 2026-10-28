from __future__ import annotations

import copy
import hashlib
import json

import pytest

from iarf_assistant.core.mcp_gateway import _work_claim_result
from iarf_assistant.core.mcp_work_projection import project_claim, request_slice


def packet():
    return {
        "work_item_id": "work",
        "execution_id": "execution",
        "lease_token": "lease",
        "lease_expires_at": "2026-09-15T13:00:00Z",
        "batch_id": "pages-1-6",
        "request": {
            "messages": [
                {"role": "system", "content": "Stable instruction. " * 3000},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "CURRENT PDF PAGE 1"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,AAAA"},
                        },
                    ],
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"schema": {"type": "object"}},
            },
            "parameters": {"max_tokens": 16000},
            "tools": [],
            "dispatch": {"context_artifact": {"text": "Metadata. " * 20000}},
        },
    }


def test_compact_minimal_and_full_are_nonmutating_and_preserve_lease():
    original = packet()
    saved = copy.deepcopy(original)
    standard = _work_claim_result(original).structured_content
    compact_result = _work_claim_result(original, response_mode="compact")
    compact = compact_result.structured_content
    minimal_result = _work_claim_result(original, response_mode="minimal")
    minimal = minimal_result.structured_content
    full = _work_claim_result(original, response_mode="full").structured_content
    assert original == saved
    for result in (standard, compact, minimal, full):
        for key in ("work_item_id", "execution_id", "lease_token", "lease_expires_at"):
            assert result[key] == original[key]
    assert compact["request"]["messages"][0]["omitted"]
    assert (
        compact["request"]["response_format"] == standard["request"]["response_format"]
    )
    assert len(compact_result.content) == 2
    assert len(minimal_result.content) == 1
    assert minimal["request"] == {}
    assert "dispatch" not in standard["request"] and "dispatch" in full["request"]
    assert len(json.dumps(compact)) < len(json.dumps(standard)) / 10


def test_compact_projection_guides_same_lease_refresh_after_context_loss():
    compact = project_claim(packet(), "compact", None)

    guidance = compact["projection"]["guidance"]

    assert "exact leased instructions and schema before authoring" in guidance
    assert "Matching hashes or a wrapper/JavaScript cache alone do not prove" in guidance
    assert "After compaction, transfer, restart, hash change or uncertainty" in guidance
    assert "carlina_work_get with the same work_item_id and lease_token" in guidance
    assert "then read it before authoring; no new claim or restart is needed" in guidance


def test_selective_images_omission_and_paginated_read_match_original_hash():
    original = packet()
    result = _work_claim_result(original, omit_fields=["images"])
    assert len(result.content) == 1
    assert result.structured_content["request"]["messages"][1]["content"][1]["omitted"]
    compact = project_claim(original, "compact", None)
    system = compact["request"]["messages"][0]
    chunks = []
    offset = 0
    while True:
        part = request_slice(original, system["path"], offset, 1000)
        assert part["sha256"] == system["sha256"]
        chunks.append(part["text"])
        if part["next_offset"] is None:
            break
        offset = part["next_offset"]
    recovered = "".join(chunks)
    assert json.loads(recovered) == original["request"]["messages"][0]
    assert hashlib.sha256(recovered.encode()).hexdigest() == part["text_sha256"]


@pytest.mark.parametrize("mode", ["standard", "compact", "minimal", "full"])
def test_retry_feedback_survives_worker_projection_without_private_bookkeeping(mode):
    original = packet()
    retry = {
        "contract_version": 1,
        "attempt_id": "retry-attempt",
        "retry_of_attempt_id": "failed-attempt",
        "dispatch_state": "normalization_failed",
        "validation_codes": ["target-validation-failed", "target-fixed-token-scope"],
    }
    original["request"]["dispatch"]["translator_window_retry"] = retry
    saved = copy.deepcopy(original)

    result = _work_claim_result(original, response_mode=mode).structured_content

    assert result["request"]["dispatch"]["translator_window_retry"] == retry
    assert result["work_item_id"] == original["work_item_id"]
    assert result["lease_token"] == original["lease_token"]
    assert original == saved
    if mode != "full":
        assert "context_artifact" not in result["request"]["dispatch"]


def test_large_container_uses_image_references_and_exact_image_fetch_is_native():
    original = packet()
    result = request_slice(original, "/request/messages/1", max_chars=64000)
    assert "data:image" not in json.dumps(result)
    image = result["value"]["content"][1]
    assert image["path"] == "/request/messages/1/content/1"
    fetched = request_slice(original, image["path"])
    assert fetched["value"]["type"] == "image_url"
    with pytest.raises(ValueError, match="parent image"):
        request_slice(original, image["path"] + "/image_url/url")


@pytest.mark.parametrize(
    "mode,fields",
    [("bogus", None), ("compact", ["lease_token"]), ("minimal", "images")],
)
def test_bad_projection_options_fail(mode, fields):
    with pytest.raises(ValueError):
        project_claim(packet(), mode, fields)


@pytest.mark.parametrize(
    "path",
    ["/lease_token", "/request/no_such_field", "/request/messages/-1", "/requestish"],
)
def test_targeted_read_rejects_nonrequest_or_missing_paths(path):
    with pytest.raises(ValueError):
        request_slice(packet(), path)
