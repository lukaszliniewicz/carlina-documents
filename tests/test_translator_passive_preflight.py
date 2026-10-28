"""Packet-only preflight returns repairable errors without consuming a lease."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from test_mcp_jobs import app  # noqa: F401 - isolated synthetic encrypted database

from iarf_assistant.addons.translator.passive_preflight import preflight_completion
from iarf_assistant.addons.translator.window_contract import (
    SINGLE_COPY_WRITABLE_SOURCE_CONTRACT_VERSION as V8,
    _target_result_schema,
)
from iarf_assistant.core.mcp_jobs import (
    claim_next_work, create_execution, get_or_create_work, issue_access_key,
    resolve_access_key, submit_work,
)
from iarf_assistant.extensions import db
from iarf_assistant.models import McpWorkItem, McpWorkSubmission


def _packet(content="A source sentence.", *, fields=None, source_count=1):
    writable = {"contract_version": V8, "annotation_profile": "none", "blocks": [{
        "n": 1, "role": "paragraph", "content": content,
        "fields": fields or [],
        "sentences": [{"n": i, "text": "Source."} for i in range(1, source_count + 1)],
    }]}
    return {
        "source": "translator_block_translation_window",
        "messages": [{"role": "user", "content": "Translate.\n[writable_source target_language=en]\n" + json.dumps(writable)}],
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "translation_block_window", "schema": _target_result_schema(V8, "none"), "strict": True}},
    }


def _response(content="A translation.<sb/>", *, fields=None, groups=None):
    return {"content": {"results": [{"n": 1, "content": content, "fields": fields or [],
        "notes": [], "alignment": {"groups": groups or [{"source": [1], "target": [1]}]}}]}}


@pytest.mark.parametrize("text", [
    "Tel.: 123", "mail@example.test", "www.example.test", "1. [Editor, 1904]",
    "One. Another.", "“Stop!” - he called.", "Final words! -", "Dr. Nowak.",
])
def test_preflight_preserves_linguistic_decisions(text):
    result = preflight_completion(_packet(), _response(text + "<sb/>"))
    assert result["validation_errors"] == []
    assert result["validated_blocks"] == 1
    assert result["scope"] == "leased_packet_only"


@pytest.mark.parametrize("content,code", [
    ("One.", "sentence-marker-missing"),
    ("One.<sb/> Tail", "sentence-marker-final"),
    ("<sb/>One.<sb/>", "sentence-marker-empty"),
    ("One.<sb/><sb/>", "sentence-marker-empty"),
    ('One.<sb x="1"/>', "sentence-marker-shape"),
    ("One.<sb>text</sb>", "sentence-marker-shape"),
    ("One.<sb/></bad>", "compact-markup-xml"),
    ('<!DOCTYPE root [<!ENTITY x "One">]>&x;<sb/>', "compact-markup-active"),
    ('One.<sb xmlns=""/>', "sentence-marker-namespace"),
])
def test_preflight_reports_block_and_safe_code(content, code):
    result = preflight_completion(_packet(), _response(content))
    assert result["validation_errors"] == [{"path": "/content/results/0", "code": code, "block_number": 1}]
    assert result["validated_blocks"] == 0


@pytest.mark.parametrize("groups", [
    [{"source": [1, 1], "target": [1]}],
    [{"source": [2], "target": [1]}],
    [{"source": [1], "target": [2]}],
    [{"source": [1], "target": [1, 2]}],
])
def test_preflight_requires_complete_partition(groups):
    result = preflight_completion(_packet(), _response(groups=groups))
    assert result["validation_errors"][0]["code"].startswith("sentence-alignment")


def test_preflight_preserves_inline_references_and_literal_values():
    source = '<b>Text</b> <a r="1"><lit r="2">1</lit></a>'
    valid = '<b>Translation</b> <a r="1"><lit r="2">1</lit></a><sb/>'
    assert not preflight_completion(_packet(source), _response(valid))["validation_errors"]
    for content in (valid.replace('r="1"', 'r="3"'), valid.replace('<b>', '<i>').replace('</b>', '</i>'),
                    valid.replace('>1</lit>', '>2</lit>'), valid.replace('>1</lit>', '>1<sb/></lit>')):
        assert preflight_completion(_packet(source), _response(content))["validation_errors"]


def test_preflight_field_only_and_non_writable_markup():
    packet = _packet('<img r="1"/>', fields=[{"name": "alt", "value": "A source caption"}])
    valid = _response('<img r="1"/>', fields=[{"name": "alt", "value": "A caption.<sb/>"}])
    assert not preflight_completion(packet, valid)["validation_errors"]
    invalid = _response('<img r="1"/>', fields=[{"name": "alt", "value": "<b>A caption.</b><sb/>"}])
    assert preflight_completion(packet, invalid)["validation_errors"]


def test_unrelated_and_legacy_transports_are_not_reinterpreted():
    assert preflight_completion({"source": "something_else"}, {}) is None
    packet = _packet()
    packet["messages"][0]["content"] = packet["messages"][0]["content"].replace(V8, "translation-block-writable-source-v7")
    assert preflight_completion(packet, _response()) is None


class _Addon:
    def preflight_passive_completion(self, *, request, response):
        return preflight_completion(request, response)


def _leased(app):
    app.addon_manager = SimpleNamespace(get_loaded_addon=lambda _id: _Addon())
    issued = issue_access_key(1, "translation tester", ["mcp.work", "mcp.translator"], [])
    principal = resolve_access_key(issued.raw_token)
    execution = create_execution(1, "translator", "project", "synthetic-project", {}, mode="passive")
    work = get_or_create_work(execution.id, _packet())
    claim = claim_next_work(principal, "translator-test", execution_id=execution.id, addon_id="translator")
    assert claim is not None
    return principal, work.work_item_id, claim.lease_token


def test_invalid_response_corrected_under_same_lease_and_dry_run_no_writes(app):
    principal, work_id, lease_token = _leased(app)
    row = db.session.get(McpWorkItem, work_id)
    version, request = row.version, copy.deepcopy(row.request_json)
    dry = submit_work(principal, work_id, lease_token, _response("Missing marker."), dry_run=True)
    assert dry["status"] == "rejected"
    assert dry["lease_active"] is True
    assert dry["validation_stage"] == "packet_preflight"
    assert dry["validation_errors"][0]["code"] == "sentence-marker-missing"
    assert dry["validation_errors"][0]["block_number"] == 1
    assert McpWorkSubmission.query.count() == 0
    db.session.refresh(row)
    assert row.version == version and row.request_json == request
    rejected = submit_work(principal, work_id, lease_token, _response("Missing marker."))
    assert rejected["status"] == "rejected" and rejected["lease_active"] is True
    dry_valid = submit_work(principal, work_id, lease_token, _response(), dry_run=True)
    assert dry_valid["status"] == "valid"
    assert dry_valid["preflight"]["validated_blocks"] == 1
    assert McpWorkSubmission.query.count() == 1
    accepted = submit_work(principal, work_id, lease_token, _response())
    assert accepted["status"] == "accepted"
    assert accepted["preflight"]["validated_blocks"] == 1
    assert accepted["product_commit_confirmed"] is False
    assert accepted["product_validation"] == "not_checked"
    assert McpWorkSubmission.query.count() == 2
    assert submit_work(principal, work_id, lease_token, _response())["status"] == "duplicate"


def test_malformed_request_envelope_returns_bounded_diagnostic():
    packet = _packet()
    packet["messages"][0]["content"] = (
        "Translate.\n[writable_source target_language=en]\n{not-json")
    result = preflight_completion(packet, _response())
    assert result is not None
    assert result["validated_blocks"] == 0
    assert result["validation_errors"] == [
        {"path": "/request/messages", "code": "window-request-contract"}]
    assert "{not-json" not in json.dumps(result)


def test_malformed_response_envelope_returns_bounded_diagnostic():
    result = preflight_completion(_packet(), {"content": "{not-json"})
    assert result is not None
    assert result["validated_blocks"] == 0
    assert result["validation_errors"] == [
        {"path": "/content/results", "code": "window-response-contract"}]
    assert "{not-json" not in json.dumps(result)


def test_malformed_messages_shape_and_role_selection():
    packet = _packet()
    packet["messages"] = "not-a-list"
    assert preflight_completion(packet, _response())["validation_errors"] == [
        {"path": "/request/messages", "code": "window-request-contract"}]
    # Assistant-role delimiter text is not a writable envelope; without a
    # user-role message the packet stays a historical None.
    assistant_only = {"source": "translator_block_translation_window", "messages": [
        {"role": "assistant", "content": "Note.\n[writable_source target_language=en]\n{}"}]}
    assert preflight_completion(assistant_only, _response()) is None
    # Last user-role writable message wins.
    multi = _packet()
    first = multi["messages"][0]["content"]
    multi["messages"] = [
        {"role": "user", "content": first},
        {"role": "user", "content": (
            "Translate.\n[writable_source target_language=en]\n{bad")},
    ]
    assert preflight_completion(multi, _response())["validation_errors"] == [
        {"path": "/request/messages", "code": "window-request-contract"}]


def _leased_with_packet(app, packet):
    app.addon_manager = SimpleNamespace(get_loaded_addon=lambda _id: _Addon())
    issued = issue_access_key(1, "translation tester", ["mcp.work", "mcp.translator"], [])
    principal = resolve_access_key(issued.raw_token)
    execution = create_execution(1, "translator", "project", "synthetic-project", {}, mode="passive")
    work = get_or_create_work(execution.id, packet)
    claim = claim_next_work(principal, "translator-test", execution_id=execution.id, addon_id="translator")
    assert claim is not None
    return principal, work.work_item_id, claim.lease_token


def test_malformed_request_same_lease_stays_active(app):
    bad = _packet()
    bad["messages"][0]["content"] = (
        "Translate.\n[writable_source target_language=en]\n{not-json")
    principal, work_id, lease_token = _leased_with_packet(app, bad)
    dry = submit_work(principal, work_id, lease_token, _response(), dry_run=True)
    assert dry["status"] == "rejected"
    assert dry["lease_active"] is True
    assert dry["validation_stage"] == "packet_preflight"
    assert dry["validation_errors"][0]["code"] == "window-request-contract"
    assert McpWorkSubmission.query.count() == 0
    rejected = submit_work(principal, work_id, lease_token, _response())
    assert rejected["status"] == "rejected" and rejected["lease_active"] is True


def test_malformed_response_string_rejected_before_preflight_same_lease(app):
    # Core JSON-schema normalization rejects a non-JSON content string before
    # packet preflight; the lease must still stay active for correction.
    principal, work_id, lease_token = _leased(app)
    dry = submit_work(principal, work_id, lease_token, {"content": "{not-json"}, dry_run=True)
    assert dry["status"] == "rejected"
    assert dry["lease_active"] is True
    assert dry.get("validation_stage") != "packet_preflight"
    assert McpWorkSubmission.query.count() == 0
    valid = submit_work(principal, work_id, lease_token, _response(), dry_run=True)
    assert valid["status"] == "valid"
