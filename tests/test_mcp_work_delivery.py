"""Lossless transport budgets and recovery after failed packet delivery."""

from __future__ import annotations

import asyncio
import base64
import copy
import logging

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import text
from test_mcp_gateway import (
    gateway as gateway,  # noqa: PLC0414 - explicitly re-export the shared pytest fixture
)

from iarf_assistant.core import mcp_gateway
from iarf_assistant.core.mcp_jobs import McpWorkError
from iarf_assistant.core.mcp_work_projection import (
    DEFAULT_RESPONSE_BYTES,
    request_images,
    request_slice,
)
from iarf_assistant.extensions import db


def packet(image_count=10, image_bytes=2_200_000):
    blocks = []
    for index in range(image_count):
        blocks.extend([
            {"type": "text", "text": f"Physical page {index + 1}"},
            {"type": "image_url", "image_url": {
                "url": "data:image/png;base64," + base64.b64encode(bytes([65 + index]) * image_bytes).decode(),
            }},
        ])
    return {
        "work_item_id": "synthetic-work", "execution_id": "synthetic-execution",
        "lease_token": "synthetic-token", "lease_expires_at": "2026-09-20T18:00:00Z",
        "request": {
            "messages": [{"role": "system", "content": "Inspect every page; preserve German spelling: Gemüther, äußern."},
                         {"role": "user", "content": blocks}],
            "response_format": {"type": "json_schema", "json_schema": {"schema": {"type": "object"}}},
            "parameters": {"max_tokens": 16000},
        },
    }


def wire_size(result):
    return len(result.model_dump_json(by_alias=True).encode()) + 1024


@pytest.mark.parametrize("protocol", ["2026-07-28", "legacy"])
def test_saved_connection_budget_controls_status_claim_and_get(gateway, protocol):
    from iarf_assistant.core.mcp_jobs import (
        configure_access_key_transport,
        create_execution,
        get_or_create_work,
        resolve_access_key,
    )

    with gateway.app.app_context():
        principal = resolve_access_key(gateway.token)
        configure_access_key_transport(
            principal.key_id, owner_user_id=1,
            response_default_bytes=65536, response_max_bytes=131072,
        )
        execution = create_execution(1, "doc_processor", "document", "budget-images", {}, mode="passive")
        get_or_create_work(execution.id, packet(1, 60000)["request"])
        execution_id = execution.id
        db.session.commit()

    async def exercise():
        async with (
            httpx2.AsyncClient(headers={"authorization": f"Bearer {gateway.token}"}) as http,
            Client(streamable_http_client(gateway.endpoint, http_client=http), mode=protocol) as client,
        ):
            status = await client.call_tool("carlina_status", {})
            assert status.structured_content["work_delivery"]["default_max_response_bytes"] == 65536
            assert status.structured_content["work_delivery"]["max_response_bytes_range"] == [65536, 131072]
            args = {"execution_id": execution_id, "worker_id": "budget-probe", "claim_request_id": "budget-1"}
            rejected = await client.call_tool("carlina_work_claim", {**args, "max_response_bytes": 262144})
            assert rejected.is_error
            assert "configured maximum" in rejected.content[0].text
            claimed = await client.call_tool("carlina_work_claim", args)
            assert not claimed.is_error
            assert wire_size(claimed) <= 65536
            assert claimed.structured_content["delivery"]["mode"] == "deferred"
            lease = {name: claimed.structured_content[name] for name in ("work_item_id", "lease_token")}
            too_small = await client.call_tool("carlina_work_get", {**lease, "image_offset": 0})
            assert too_small.is_error and "work_image_too_large" in too_small.content[0].text
            too_large = await client.call_tool("carlina_work_get", {**lease, "image_offset": 0, "max_response_bytes": 262144})
            assert too_large.is_error and "configured maximum" in too_large.content[0].text
            image = await client.call_tool("carlina_work_get", {**lease, "image_offset": 0, "max_response_bytes": 131072})
            assert not image.is_error
            assert wire_size(image) <= 131072
            assert sum(block.type == "image" for block in image.content) == 1
            assert image.structured_content["delivery"]["next_offset"] is None

    asyncio.run(exercise())
    with gateway.app.app_context():
        assert db.session.execute(text("SELECT attempt_count, state FROM mcp_work_item WHERE execution_id=:id"), {"id": execution_id}).one() == (1, "leased")


def test_document_packet_preflight_corrects_under_same_lease_and_dry_run_does_not_write(gateway):
    from types import SimpleNamespace

    from iarf_assistant.addons.doc_processor import DocProcessorAddon
    from iarf_assistant.addons.doc_processor.page_batch_response import (
        page_batch_response_schema,
    )
    from iarf_assistant.core.mcp_jobs import create_execution, get_or_create_work

    class Addon:
        preflight_passive_completion = DocProcessorAddon.preflight_passive_completion

    gateway.app.addon_manager = SimpleNamespace(get_loaded_addon=lambda _addon_id: Addon())
    with gateway.app.app_context():
        execution = create_execution(1, "doc_processor", "document", "preflight-pages", {}, mode="passive")
        request = packet(0)["request"]
        request["source"] = "doc_processor_pdf_batch"
        request["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "document_page_batch_v2", "strict": True,
            "schema": page_batch_response_schema([7], allow_blank=True),
        }}
        get_or_create_work(execution.id, request)
        execution_id = execution.id

    async def exercise():
        async with (
            httpx2.AsyncClient(headers={"authorization": f"Bearer {gateway.token}"}) as http,
            Client(streamable_http_client(gateway.endpoint, http_client=http)) as client,
        ):
            claim = await client.call_tool("carlina_work_claim", {
                "execution_id": execution_id, "worker_id": "semantic-probe", "claim_request_id": "semantic-1",
            })
            assert not claim.is_error
            lease = {name: claim.structured_content[name] for name in ("work_item_id", "lease_token")}
            bad = {"content": {"pages": [{"page_number": 7, "blank": False,
                    "html": "<main_content>unstructured synthetic content</main_content><footnotes></footnotes>"}]}}
            rejected = await client.call_tool("carlina_work_submit", {**lease, "response": bad})
            assert rejected.structured_content["status"] == "rejected"
            assert rejected.structured_content["validation_stage"] == "packet_preflight"
            assert rejected.structured_content["lease_active"] is True
            assert "unstructured synthetic content" not in rejected.model_dump_json()
            good = {"content": {"pages": [{"page_number": 7, "blank": True,
                     "html": "<main_content></main_content><footnotes></footnotes>"}]}}
            with gateway.app.app_context():
                count_before = db.session.execute(text("SELECT count(*) FROM mcp_work_submission")).scalar_one()
            valid = await client.call_tool("carlina_work_submit", {**lease, "response": good, "dry_run": True})
            assert valid.structured_content["status"] == "valid"
            assert valid.structured_content["preflight"]["validation_errors"] == []
            with gateway.app.app_context():
                assert db.session.execute(text("SELECT count(*) FROM mcp_work_submission")).scalar_one() == count_before
            accepted = await client.call_tool("carlina_work_submit", {**lease, "response": good})
            assert accepted.structured_content["status"] == "accepted"
            assert accepted.structured_content["product_commit_confirmed"] is False
            assert accepted.structured_content["preflight"]["validated_blocks"] == 1

    asyncio.run(exercise())
    with gateway.app.app_context():
        assert db.session.execute(text("SELECT attempt_count, state FROM mcp_work_item WHERE execution_id=:id"), {"id": execution_id}).one() == (1, "completed")


def test_book_sized_metadata_defers_then_delivers_every_original_byte_in_order():
    original = packet()
    frozen = copy.deepcopy(original)
    receipt = mcp_gateway._work_claim_result(original)
    assert wire_size(receipt) < DEFAULT_RESPONSE_BYTES
    assert len(receipt.content) == 1
    state = receipt.structured_content
    assert state["delivery"]["mode"] == "deferred"
    assert state["delivery"]["image_count"] == 10
    assert state["request"]["response_format"] == original["request"]["response_format"]
    assert state["request"]["messages"][0] == original["request"]["messages"][0]
    assert "data:image" not in receipt.model_dump_json()
    delivered, paths, offset = [], [], 0
    while offset is not None:
        result = mcp_gateway._work_image_batch_result(original, offset, 4, DEFAULT_RESPONSE_BYTES)
        assert wire_size(result) <= DEFAULT_RESPONSE_BYTES
        batch = result.structured_content
        assert 1 <= batch["delivery"]["returned_count"] < 4  # byte budget, not requested count, governs
        delivered.extend(block.data for block in result.content if block.type == "image")
        paths.extend(entry["path"] for entry in batch["images"])
        offset = batch["delivery"]["next_offset"]
    expected = request_images(original["request"])
    assert paths == [path for path, _ in expected]
    assert delivered == [image["image_url"]["url"].split(",", 1)[1] for _, image in expected]
    assert original == frozen


def test_small_packet_stays_inline_and_explicit_omissions_remain_retrievable():
    original = packet(1, 100)
    inline = mcp_gateway._work_claim_result(original)
    assert len(inline.content) == 2
    minimal = mcp_gateway._work_claim_result(original, response_mode="minimal")
    assert len(minimal.content) == 1
    for omission in minimal.structured_content["projection"]["omitted"]:
        retrieved = request_slice(mcp_gateway._work_wire_payload(original), omission["path"], max_chars=64000)
        assert retrieved["sha256"] == omission["sha256"]


def test_large_text_falls_back_to_receipt_and_all_text_is_available_as_slices():
    original = packet(0)
    source = "Gothic: Gemüther, äußern. \\\"\n" * 4000
    original["request"]["messages"][0]["content"] = source
    result = mcp_gateway._work_claim_result(original, max_response_bytes=65536, response_mode="full")
    assert wire_size(result) <= 65536
    assert result.structured_content["request"] == {}
    assert result.structured_content["delivery"]["next_action"]["path"] == "/request"
    parts, offset = [], 0
    while True:
        part = request_slice(original, "/request/messages/0/content", offset, 4000)
        parts.append(part.get("text", part.get("value")))
        if part.get("next_offset") is None:
            break
        offset = part["next_offset"]
    assert "".join(parts) == source


def test_oversized_single_image_is_explicit_and_retrievable_with_larger_budget():
    original = packet(1, 90000)
    with pytest.raises(McpWorkError) as error:
        mcp_gateway._work_image_batch_result(original, 0, 1, 65536)
    assert error.value.code == "work_image_too_large"
    assert "Image 0" in error.value.detail
    result = mcp_gateway._work_image_batch_result(original, 0, 1, 256000)
    assert len(result.content) == 2
    assert result.structured_content["delivery"]["next_offset"] is None
    path = request_images(original["request"])[0][0]
    with pytest.raises(McpWorkError, match="requires at least"):
        mcp_gateway._bounded_work_result(request_slice(original, path), 65536, allow_defer=False)


@pytest.mark.parametrize("offset,limit", [(-1, 1), (2, 1), (0, 0), (0, 5), (True, 1), (0, True)])
def test_invalid_image_ranges_fail(offset, limit):
    with pytest.raises(McpWorkError):
        mcp_gateway._work_image_batch_result(packet(1, 5), offset, limit, 65536)


def test_compact_instruction_references_survive_automatic_image_deferral():
    original = packet(2, 100000)
    result = mcp_gateway._work_claim_result(original, response_mode="compact", max_response_bytes=65536)
    assert result.structured_content["projection"]["mode"] == "compact"
    omissions = result.structured_content["projection"]["omitted"]
    assert any(item["path"] == "/request/messages/0" for item in omissions)
    assert sum(item["path"].startswith("/request/messages/1/content/") for item in omissions) == 2


def test_work_images_above_review_attachment_cap_obey_work_transport_budget():
    original = packet(1, 26 * 1024 * 1024)
    budget = 64 * 1024 * 1024
    deferred = mcp_gateway._work_claim_result(original)
    assert deferred.structured_content["delivery"]["mode"] == "deferred"
    for result in (
        mcp_gateway._work_claim_result(original, max_response_bytes=budget),
        mcp_gateway._work_image_batch_result(original, 0, 1, budget),
        mcp_gateway._bounded_work_result(
            request_slice(original, request_images(original["request"])[0][0]),
            budget, allow_defer=False,
        ),
    ):
        assert wire_size(result) <= budget
        assert result.content[1].data == original["request"]["messages"][1]["content"][1]["image_url"]["url"].split(",", 1)[1]


@pytest.mark.parametrize("protocol", ["2026-07-28", "legacy"])
def test_post_commit_projection_failure_is_redacted_and_same_claim_recovers(gateway, monkeypatch, caplog, protocol):
    original = mcp_gateway._work_claim_result
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("PRIVATE_SOURCE_AND_TOKEN_MUST_NOT_LEAK")
        return original(*args, **kwargs)

    monkeypatch.setattr(mcp_gateway, "_work_claim_result", fail_once)

    async def exercise():
        async with (
            httpx2.AsyncClient(headers={"authorization": f"Bearer {gateway.token}"}) as http,
            Client(streamable_http_client(gateway.endpoint, http_client=http), mode=protocol) as client,
        ):
            args = {"execution_id": gateway.execution_id, "worker_id": "delivery-probe", "claim_request_id": "delivery-probe-1"}
            failure = await client.call_tool("carlina_work_claim", args)
            assert failure.is_error
            assert "work_delivery_failed" in failure.content[0].text
            assert "PRIVATE_SOURCE" not in failure.model_dump_json()
            recovered = await client.call_tool("carlina_work_claim", {**args, "response_mode": "minimal"})
            assert not recovered.is_error
            repeated = await client.call_tool("carlina_work_claim", args)
            assert not repeated.is_error
            assert recovered.structured_content["lease_token"] == repeated.structured_content["lease_token"]
            assert recovered.structured_content["work_item_id"] == repeated.structured_content["work_item_id"]

    with caplog.at_level(logging.WARNING):
        asyncio.run(exercise())
    assert "PRIVATE_SOURCE" not in caplog.text
    with gateway.app.app_context():
        assert db.session.execute(text("SELECT attempt_count FROM mcp_work_item WHERE execution_id=:id"), {"id": gateway.execution_id}).scalar_one() == 1


def test_image_fetching_is_read_only_and_survives_lost_claim_response(gateway):
    from iarf_assistant.core.mcp_jobs import create_execution, get_or_create_work

    with gateway.app.app_context():
        execution = create_execution(1, "doc_processor", "document", "synthetic-images", {}, mode="passive")
        request = packet(3, 30000)["request"]
        request["workload_role"] = "document.metadata"
        get_or_create_work(execution.id, request)
        execution_id = execution.id

    async def exercise():
        async with (
            httpx2.AsyncClient(headers={"authorization": f"Bearer {gateway.token}"}) as http,
            Client(streamable_http_client(gateway.endpoint, http_client=http)) as client,
        ):
            args = {"execution_id": execution_id, "worker_id": "images-probe", "claim_request_id": "images-probe-1", "max_response_bytes": 65536}
            lost = await client.call_tool("carlina_work_claim", args)
            recovered = await client.call_tool("carlina_work_claim", args)
            assert not lost.is_error and not recovered.is_error
            claim = recovered.structured_content
            assert claim["lease_token"] == lost.structured_content["lease_token"]
            assert claim["delivery"]["image_count"] == 3
            offset, count = 0, 0
            while offset is not None:
                result = await client.call_tool("carlina_work_get", {
                    "work_item_id": claim["work_item_id"], "lease_token": claim["lease_token"],
                    "image_offset": offset, "image_limit": 4, "max_response_bytes": 65536,
                })
                assert not result.is_error
                count += sum(item.type == "image" for item in result.content)
                offset = result.structured_content["delivery"]["next_offset"]
            assert count == 3

    asyncio.run(exercise())
    with gateway.app.app_context():
        assert db.session.execute(text("SELECT attempt_count, version, state FROM mcp_work_item WHERE execution_id=:id"), {"id": execution_id}).one() == (1, 1, "leased")
