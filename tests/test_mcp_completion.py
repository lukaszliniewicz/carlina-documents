from __future__ import annotations

import copy
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from content_storage_support import bind_content_policy
from flask import Flask, current_app
from sqlalchemy import event

from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.core.mcp_completion import (
    PassiveReplayConflict,
    build_passive_packet,
    passive_dispatch,
    passive_execution,
    passive_execution_context,
    resolve_passive_completion,
)
from iarf_assistant.core.mcp_jobs import (
    McpWorkError,
    claim_next_work,
    create_execution,
    get_execution,
    get_or_create_work,
    issue_access_key,
    passive_work_is_pending,
    resolve_access_key,
    serialize_execution,
    submit_work,
)
from iarf_assistant.core.model_capabilities import (
    AdmissionResult,
    ContextAdmissionError,
)
from iarf_assistant.core.task_queue import ExternalWorkPending, WorkerNode
from iarf_assistant.extensions import db
from iarf_assistant.llm_service import LLMService
from iarf_assistant.models import McpExecution, McpWorkItem, User

EXECUTOR_PROFILE = {
    "schema_version": "carlina.external-executor.v1",
    "max_input_tokens": 32_000,
    "max_output_tokens": 4_000,
    "input_modalities": ["text"],
    "supports_tools": True,
    "supports_structured_output": True,
    "supports_reasoning": True,
    "user_confirmed": True,
}


def test_passive_packet_strips_recursive_file_paths_without_mutating_messages():
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "file_path": "/tmp/page-a.png",
                    "image_url": {
                        "url": "data:image/png;base64,AAAA",
                        "file_path": "/tmp/page-a.png",
                    },
                },
                {
                    "type": "input_audio",
                    "input_audio": {
                        "data": "AQID",
                        "format": "wav",
                        "file_path": "/tmp/audio-a.wav",
                    },
                },
                {
                    "type": "file",
                    "file": {
                        "file_data": "data:application/pdf;base64,BBBB",
                        "file_path": "/tmp/document-a.pdf",
                    },
                },
            ],
            "metadata": {"nested": [{"file_path": "/tmp/metadata-a"}]},
        }
    ]
    original_messages = copy.deepcopy(messages)
    changed_paths = copy.deepcopy(messages)
    changed_paths[0]["content"][0]["file_path"] = "/random/page-b.png"
    changed_paths[0]["content"][0]["image_url"]["file_path"] = "/random/page-b.png"
    changed_paths[0]["content"][1]["input_audio"]["file_path"] = "/random/audio-b.wav"
    changed_paths[0]["content"][2]["file"]["file_path"] = "/random/document-b.pdf"
    changed_paths[0]["metadata"]["nested"][0]["file_path"] = "/random/metadata-b"

    packet = build_passive_packet(
        executor_profile=EXECUTOR_PROFILE,
        workload_role="document_page",
        messages=messages,
        tools=None,
        response_format=None,
        parameters={},
        source="test",
    )
    changed_packet = build_passive_packet(
        executor_profile=EXECUTOR_PROFILE,
        workload_role="document_page",
        messages=changed_paths,
        tools=None,
        response_format=None,
        parameters={},
        source="test",
    )

    assert messages == original_messages
    assert changed_paths != messages
    assert packet["messages"] == [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AAAA"},
                },
                {
                    "type": "input_audio",
                    "input_audio": {"data": "AQID", "format": "wav"},
                },
                {
                    "type": "file",
                    "file": {"file_data": "data:application/pdf;base64,BBBB"},
                },
            ],
            "metadata": {"nested": [{}]},
        }
    ]
    assert packet["request_fingerprint"] == changed_packet["request_fingerprint"]
    assert packet == changed_packet
    assert "/tmp/" not in str(packet)
    assert "/random/" not in str(changed_packet)


def test_passive_dispatch_is_nested_immutable_and_fingerprint_isolated():
    baseline = build_passive_packet(
        executor_profile=EXECUTOR_PROFILE,
        workload_role="document_page",
        messages=[{"role": "user", "content": "page"}],
        tools=None,
        response_format=None,
        parameters={},
        source="test",
    )
    supplied = {"batch_id": "outer", "nested": {"keep": [1]}}
    with passive_dispatch(supplied):
        supplied["batch_id"] = "mutated"
        supplied["nested"]["keep"].append(2)
        outer = build_passive_packet(
            executor_profile=EXECUTOR_PROFILE,
            workload_role="document_page",
            messages=[{"role": "user", "content": "page"}],
            tools=None,
            response_format=None,
            parameters={},
            source="test",
        )
        with passive_dispatch({"batch_id": "inner", "unit_index": 2}):
            inner = build_passive_packet(
                executor_profile=EXECUTOR_PROFILE,
                workload_role="document_page",
                messages=[{"role": "user", "content": "page"}],
                tools=None,
                response_format=None,
                parameters={},
                source="test",
            )
        restored = build_passive_packet(
            executor_profile=EXECUTOR_PROFILE,
            workload_role="document_page",
            messages=[{"role": "user", "content": "page"}],
            tools=None,
            response_format=None,
            parameters={},
            source="test",
        )

    assert "dispatch" not in baseline
    assert outer["dispatch"] == {
        "batch_id": "outer",
        "nested": {"keep": [1]},
    }
    assert inner["dispatch"] == {
        "batch_id": "inner",
        "nested": {"keep": [1]},
        "unit_index": 2,
    }
    assert restored == outer
    assert outer["request_fingerprint"] != baseline["request_fingerprint"]
    assert outer["request_fingerprint"] == restored["request_fingerprint"]


@pytest.fixture
def passive_app(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'passive.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="passive-completion-test",
        TESTING=True,
    )
    db.init_app(application)
    application.extensions["carlina_encryption"] = EncryptionService(
        InstallationKeyStore.ephemeral()
    )
    context = application.app_context()
    context.push()
    db.create_all()
    owner = User(id=1, email="passive@example.test", password_hash="x")
    db.session.add(owner)
    db.session.commit()
    execution = create_execution(
        1,
        "translator",
        "project",
        "project-test",
        {"mode": "passive", "executor_profile": EXECUTOR_PROFILE},
        mode="passive",
    )
    try:
        yield execution.id
    finally:
        db.session.remove()
        db.drop_all()
        db.engine.dispose()
        context.pop()


def test_passive_completion_never_requires_or_calls_provider_credentials(passive_app):
    service = LLMService(user_id=1, session_id="passive-test")
    messages = [{"role": "user", "content": "Translate hello into Polish."}]
    context = {
        "execution_id": passive_app,
        "owner_user_id": 1,
        "addon_id": "translator",
        "resource_kind": "project",
        "resource_id": "project-test",
    }

    with (
        patch("iarf_assistant.llm_service.litellm.completion") as provider_call,
        passive_execution(**context),
        pytest.raises(ExternalWorkPending),
    ):
        service.completion("external_subscription/chat", messages)
    provider_call.assert_not_called()

    issued = issue_access_key(
        1,
        "passive test",
        ["mcp.work", "mcp.translator"],
        [],
    )
    principal = resolve_access_key(issued.raw_token)
    # Match the gateway's separate authentication and work transactions.
    db.session.commit()
    claim = claim_next_work(
        principal,
        "test-harness",
        addon_id="translator",
        execution_id=context["execution_id"],
    )
    assert claim is not None
    assert claim.request["submission_contract"]["tool_calls"].startswith(
        "Omit tool_calls when no tool is called."
    )
    submitted = submit_work(
        principal,
        claim.work_item_id,
        claim.lease_token,
        {"content": "Cześć", "finish_reason": "stop"},
    )
    assert submitted["status"] == "accepted"

    with (
        patch("iarf_assistant.llm_service.litellm.completion") as provider_call,
        passive_execution(**context),
    ):
        response = service.completion("external_subscription/chat", messages)
    provider_call.assert_not_called()
    assert response.choices[0].message.content == "Cześć"


def test_passive_context_reloads_profile_and_execution_projection_is_safe(passive_app):
    execution = get_execution(passive_app, owner_user_id=1)
    assert execution is not None
    assert "request" not in serialize_execution(execution)

    with passive_execution(
        execution_id=passive_app,
        owner_user_id=1,
        addon_id="translator",
        resource_kind="project",
        resource_id="project-test",
    ):
        context = passive_execution_context()
        assert context is not None
        assert context.executor_profile.to_dict() == EXECUTOR_PROFILE


def test_preview_passive_admission_is_read_only_and_matches_accepted_completion(
    passive_app,
):
    service = LLMService(user_id=1, session_id="passive-preview-test")
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "Return the requested translation as JSON.",
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64,AAAA",
                        "detail": "low",
                    },
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64,BBBB",
                        "detail": "low",
                    },
                },
            ],
        }
    ]
    files = [
        {
            "filePath": "missing-preview.png",
            "fileName": "preview.png",
            "pass_as": "metadata",
        }
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup_term",
                "description": "Look up one translation term.",
                "parameters": {
                    "type": "object",
                    "properties": {"term": {"type": "string"}},
                    "required": ["term"],
                    "additionalProperties": False,
                },
            },
        }
    ]
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "translation_result",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {"translation": {"type": "string"}},
                "required": ["translation"],
                "additionalProperties": False,
            },
        },
    }
    kwargs = {
        "max_completion_tokens": 256,
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "reasoning_effort": "low",
        "modalities": ["text"],
        "context_stewardship": "manual",
        "enable_auto_compression": True,
    }
    original_messages = copy.deepcopy(messages)
    original_files = copy.deepcopy(files)
    original_tools = copy.deepcopy(tools)
    original_response_format = copy.deepcopy(response_format)
    context = {
        "execution_id": passive_app,
        "owner_user_id": 1,
        "addon_id": "translator",
        "resource_kind": "project",
        "resource_id": "project-test",
    }
    before_execution = serialize_execution(get_execution(passive_app, owner_user_id=1))
    before_work_count = McpWorkItem.query.count()

    with (
        patch("iarf_assistant.llm_service.litellm.completion") as provider_call,
        patch.object(service, "_get_api_credentials") as credentials,
        passive_execution(**context),
    ):
        preview = service.preview_passive_admission(
            model="external_subscription/chat",
            messages=messages,
            tools=tools,
            response_format=response_format,
            files=files,
            **kwargs,
        )
    assert isinstance(preview, AdmissionResult)
    assert preview.admitted
    provider_call.assert_not_called()
    credentials.assert_not_called()
    assert messages == original_messages
    assert files == original_files
    assert tools == original_tools
    assert response_format == original_response_format
    assert kwargs["modalities"] == ["text"]
    assert serialize_execution(get_execution(passive_app, owner_user_id=1)) == before_execution
    assert McpWorkItem.query.count() == before_work_count

    admission_sink = {}
    with (
        patch("iarf_assistant.llm_service.litellm.completion") as provider_call,
        passive_execution(**context),
        pytest.raises(ExternalWorkPending),
    ):
        service.completion(
            "external_subscription/chat",
            messages,
            tools=tools,
            response_format=response_format,
            files=files,
            admission_sink=admission_sink,
            **kwargs,
        )
    provider_call.assert_not_called()
    assert admission_sink == preview.to_dict()


def test_preview_passive_admission_returns_rejection_and_matches_actual(passive_app):
    service = LLMService(user_id=1, session_id="passive-preview-rejected")
    messages = [{"role": "user", "content": "x" * 140_000}]
    context = {
        "execution_id": passive_app,
        "owner_user_id": 1,
        "addon_id": "translator",
        "resource_kind": "project",
        "resource_id": "project-test",
    }
    before_execution = serialize_execution(get_execution(passive_app, owner_user_id=1))

    with passive_execution(**context):
        preview = service.preview_passive_admission(
            model="external_subscription/chat",
            messages=messages,
            response_format={"type": "json_object"},
            context_admission_mode="manual",
        )
    assert isinstance(preview, AdmissionResult)
    assert not preview.admitted
    assert preview.overflow_tokens > 0
    assert serialize_execution(get_execution(passive_app, owner_user_id=1)) == before_execution

    admission_sink = {}
    with (
        passive_execution(**context),
        pytest.raises(ContextAdmissionError) as error,
    ):
        service.completion(
            "external_subscription/chat",
            messages,
            response_format={"type": "json_object"},
            context_admission_mode="manual",
            admission_sink=admission_sink,
        )
    assert error.value.result.to_dict() == preview.to_dict()
    assert admission_sink == preview.to_dict()
    assert serialize_execution(get_execution(passive_app, owner_user_id=1)) == before_execution


def test_preview_passive_admission_requires_active_context():
    service = LLMService(user_id=1, session_id="outside-passive-preview")

    with pytest.raises(ValueError, match="Passive MCP execution context"):
        service.preview_passive_admission(
            model="external_subscription/chat",
            messages=[{"role": "user", "content": "hello"}],
        )


TRANSLATION_WINDOW_SOURCE = "translator_block_translation_window"
TRANSLATION_WINDOW_INDEX = {
    "contract_version": "translator-window-block-index-v1",
    "source_plan_id": "plan-test",
    "window_order": 1,
    "blocks": [{"n": 1, "block_id": "block-a", "sentences": []}],
}
TRANSLATION_WINDOW_MESSAGES = [{"role": "user", "content": "Translate hello into Polish."}]
TRANSLATION_WINDOW_RESPONSE_FORMAT = {"type": "json_object"}


def _translation_window_packet(
    *,
    indexed,
    messages=None,
    response_format=None,
    parameters=None,
    extra_dispatch=None,
):
    dispatch = dict(extra_dispatch or {})
    if indexed:
        dispatch["translator_window_index"] = copy.deepcopy(TRANSLATION_WINDOW_INDEX)
    context = passive_dispatch(dispatch) if dispatch else passive_dispatch(None)
    with context:
        return build_passive_packet(
            executor_profile=EXECUTOR_PROFILE,
            workload_role="translation",
            messages=copy.deepcopy(messages or TRANSLATION_WINDOW_MESSAGES),
            tools=None,
            response_format=(
                copy.deepcopy(
                    TRANSLATION_WINDOW_RESPONSE_FORMAT if response_format is None else response_format
                )
            ),
            parameters=dict({} if parameters is None else parameters),
            source=TRANSLATION_WINDOW_SOURCE,
        )


def _resolve_in_project(passive_execution_id, packet, resource_id="project-test"):
    with passive_execution(
        execution_id=passive_execution_id,
        owner_user_id=1,
        addon_id="translator",
        resource_kind="project",
        resource_id=resource_id,
    ):
        return resolve_passive_completion(packet)


def _pending_work_id(passive_execution_id, packet, resource_id="project-test"):
    with pytest.raises(ExternalWorkPending) as pending:
        _resolve_in_project(passive_execution_id, packet, resource_id=resource_id)
    return pending.value.work_item_id


def test_translation_window_index_reuses_legacy_work_item(passive_app):
    legacy = _translation_window_packet(indexed=False)
    first_id = _pending_work_id(passive_app, legacy)
    assert McpWorkItem.query.count() == 1

    indexed = _translation_window_packet(indexed=True)
    assert indexed["request_fingerprint"] != legacy["request_fingerprint"]
    second_id = _pending_work_id(passive_app, indexed)
    assert second_id == first_id
    assert McpWorkItem.query.count() == 1
    stored = McpWorkItem.query.filter_by(id=first_id).one()
    assert "translator_window_index" not in str(stored.request_json.get("dispatch"))


def test_translation_window_indexed_retry_reuses_indexed_work(passive_app):
    first_id = _pending_work_id(passive_app, _translation_window_packet(indexed=True))
    second_id = _pending_work_id(passive_app, _translation_window_packet(indexed=True))
    assert second_id == first_id
    assert McpWorkItem.query.count() == 1
    stored = McpWorkItem.query.filter_by(id=first_id).one()
    assert (
        stored.request_json["dispatch"]["translator_window_index"]
        == TRANSLATION_WINDOW_INDEX
    )


def test_translation_window_accepted_response_reused_across_shapes(passive_app):
    first_id = _pending_work_id(passive_app, _translation_window_packet(indexed=False))

    issued = issue_access_key(1, "passive window test", ["mcp.work", "mcp.translator"], [])
    principal = resolve_access_key(issued.raw_token)
    db.session.commit()
    claim = claim_next_work(
        principal, "test-harness", addon_id="translator", execution_id=passive_app
    )
    assert claim is not None
    submitted = submit_work(
        principal,
        claim.work_item_id,
        claim.lease_token,
        {"content": '{"translation": "Cześć"}', "finish_reason": "stop"},
    )
    assert submitted["status"] == "accepted"

    legacy_response = _resolve_in_project(
        passive_app, _translation_window_packet(indexed=False)
    )
    indexed_response = _resolve_in_project(
        passive_app, _translation_window_packet(indexed=True)
    )
    assert legacy_response.choices[0].message.content == '{"translation": "Cześć"}'
    assert indexed_response.choices[0].message.content == '{"translation": "Cześć"}'
    assert legacy_response.id == f"carlina-mcp-{first_id}"
    assert indexed_response.id == f"carlina-mcp-{first_id}"
    assert McpWorkItem.query.count() == 1


def test_translation_window_foreign_execution_isolated(passive_app):
    first_id = _pending_work_id(passive_app, _translation_window_packet(indexed=False))

    other = create_execution(
        1,
        "translator",
        "project",
        "project-other",
        {"mode": "passive", "executor_profile": EXECUTOR_PROFILE},
        mode="passive",
    )
    db.session.commit()
    other_id = _pending_work_id(other.id, _translation_window_packet(indexed=False), resource_id="project-other")
    assert other_id != first_id
    repeat_id = _pending_work_id(
        other.id, _translation_window_packet(indexed=True), resource_id="project-other"
    )
    assert repeat_id == other_id
    assert McpWorkItem.query.count() == 2


def test_translation_window_changed_content_creates_new_work(passive_app):
    first_id = _pending_work_id(passive_app, _translation_window_packet(indexed=False))

    changed_messages = _translation_window_packet(
        indexed=True,
        messages=[{"role": "user", "content": "Translate goodbye into Polish."}],
    )
    messages_id = _pending_work_id(passive_app, changed_messages)
    assert messages_id != first_id

    changed_schema = _translation_window_packet(
        indexed=True,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "translation_result",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {"translation": {"type": "string"}},
                    "required": ["translation"],
                    "additionalProperties": False,
                },
            },
        },
    )
    schema_id = _pending_work_id(passive_app, changed_schema)
    assert schema_id not in {first_id, messages_id}

    changed_parameters = _translation_window_packet(
        indexed=True, parameters={"temperature": 0.5}
    )
    parameters_id = _pending_work_id(passive_app, changed_parameters)
    assert parameters_id not in {first_id, messages_id, schema_id}
    assert McpWorkItem.query.count() == 4


def test_translation_window_both_shapes_conflict_without_writes(passive_app):
    legacy = _translation_window_packet(indexed=False)
    indexed = _translation_window_packet(indexed=True)
    get_or_create_work(passive_app, legacy)
    get_or_create_work(passive_app, indexed)
    assert McpWorkItem.query.count() == 2

    with pytest.raises(McpWorkError) as conflict:
        _resolve_in_project(passive_app, indexed)
    assert conflict.value.code == "request_fingerprint_ambiguous"
    assert McpWorkItem.query.count() == 2


def test_translation_window_preserves_other_dispatch_fields(passive_app):
    legacy = _translation_window_packet(indexed=False, extra_dispatch={"custom": "keep"})
    first_id = _pending_work_id(passive_app, legacy)

    indexed = _translation_window_packet(indexed=True, extra_dispatch={"custom": "keep"})
    second_id = _pending_work_id(passive_app, indexed)
    assert second_id == first_id
    assert McpWorkItem.query.count() == 1
    stored = McpWorkItem.query.filter_by(id=first_id).one()
    assert stored.request_json["dispatch"] == {"custom": "keep"}


def test_translation_window_unknown_index_contract_not_normalized(passive_app):
    first_id = _pending_work_id(passive_app, _translation_window_packet(indexed=False))

    future_index = copy.deepcopy(TRANSLATION_WINDOW_INDEX)
    future_index["contract_version"] = "translator-window-block-index-v999"
    with passive_dispatch({"translator_window_index": future_index}):
        future = build_passive_packet(
            executor_profile=EXECUTOR_PROFILE,
            workload_role="translation",
            messages=copy.deepcopy(TRANSLATION_WINDOW_MESSAGES),
            tools=None,
            response_format=copy.deepcopy(TRANSLATION_WINDOW_RESPONSE_FORMAT),
            parameters={},
            source=TRANSLATION_WINDOW_SOURCE,
        )
    future_id = _pending_work_id(passive_app, future)
    assert future_id != first_id
    assert McpWorkItem.query.count() == 2


def _parking_hint(execution_id):
    execution = get_execution(execution_id, owner_user_id=1)
    execution.queue_job_id = "parking-worker"
    db.session.commit()
    work_id = _pending_work_id(execution_id, _translation_window_packet(indexed=False))
    return dict(
        work_item_id=work_id, queue_job_id="parking-worker", addon_id="translator",
        owner_user_id=1, resource_kind="project", resource_id="project-test",
    )


def test_passive_parking_is_one_metadata_query_without_session_transaction(passive_app):
    hint = _parking_hint(passive_app)
    db.session.rollback()
    queries = []

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        queries.append(statement)

    event.listen(db.engine, "before_cursor_execute", capture)
    try:
        assert passive_work_is_pending(**hint)
    finally:
        event.remove(db.engine, "before_cursor_execute", capture)
    assert not db.session().in_transaction()
    assert len(queries) == 1
    assert queries[0].lstrip().upper().startswith("SELECT")
    assert "request_json" not in queries[0]
    assert "accepted_response_json" not in queries[0]


@pytest.mark.parametrize("field,value", [
    ("work_item_id", "different-work"), ("queue_job_id", "different-job"),
    ("owner_user_id", 2), ("owner_user_id", True), ("owner_user_id", None),
    ("addon_id", "doc_processor"), ("resource_kind", "document"),
    ("resource_id", "different-project"), ("resource_id", None),
])
def test_passive_parking_requires_exact_scope(passive_app, field, value):
    hint = _parking_hint(passive_app)
    hint[field] = value
    assert not passive_work_is_pending(**hint)


@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled"])
def test_passive_parking_does_not_hide_terminal_execution(passive_app, terminal):
    hint = _parking_hint(passive_app)
    execution = get_execution(passive_app, owner_user_id=1)
    execution.status = terminal
    db.session.commit()
    assert not passive_work_is_pending(**hint)


def test_passive_parking_resumes_after_real_submission(passive_app):
    hint = _parking_hint(passive_app)
    issued = issue_access_key(1, "parking-test", ["mcp.work", "mcp.translator"], [])
    principal = resolve_access_key(issued.raw_token)
    db.session.commit()
    claim = claim_next_work(
        principal, "parking-test", addon_id="translator", execution_id=passive_app,
    )
    assert claim is not None
    assert passive_work_is_pending(**hint)  # Leased, not just pending.
    result = submit_work(
        principal, claim.work_item_id, claim.lease_token,
        {"content": {"translation": "Cześć"}, "finish_reason": "stop"},
    )
    assert result["status"] == "accepted"
    assert not passive_work_is_pending(**hint)


def test_worker_parking_skips_handler_and_status_churn_then_resumes(passive_app):
    hint = _parking_hint(passive_app)
    queue = Mock()
    queue.is_cancel_requested.return_value = False
    queue.get_status.return_value = {
        "waiting_for_external_work": True,
        "external_work_item_id": hint["work_item_id"],
    }
    handler = Mock(return_value={"done": True})
    worker = object.__new__(WorkerNode)
    worker.addon_id = "translator"
    worker.handlers = {"translate_document_blocks": handler}
    job = {
        "job_id": hint["queue_job_id"], "task_name": "translate_document_blocks",
        "payload": {"user_id": 1, "project_id": "project-test"},
    }
    for _ in range(5):
        with pytest.raises(ExternalWorkPending) as pending:
            worker._process_job(queue, job)
        assert pending.value.work_item_id == hint["work_item_id"]
        assert pending.value.delay_seconds == 5.0
    handler.assert_not_called()
    queue.set_status.assert_not_called()

    # A stale hint must not strand the queue forever.
    queue.get_status.return_value["external_work_item_id"] = "stale-item"
    worker._process_job(queue, job)
    handler.assert_called_once_with(job["payload"], queue, job["job_id"])


def test_stale_parser_cannot_create_work_after_queue_attempt_changes(passive_app):
    from concurrent.futures import ThreadPoolExecutor

    from iarf_assistant.core.mcp_completion import PassiveAttemptSuperseded

    application = current_app._get_current_object()
    execution = create_execution(1, "doc_processor", "document", "stale-parser", {
        "executor_profile": EXECUTOR_PROFILE, "recovery": {"generation": 0},
    }, mode="passive", queue_job_id="old-attempt")
    execution_id = execution.id
    packet = build_passive_packet(
        executor_profile=EXECUTOR_PROFILE, workload_role="document_page",
        messages=[{"role": "user", "content": "Synthetic source"}],
        tools=None, response_format=None, parameters={}, source="doc_processor_pdf_batch",
    )
    with passive_execution(execution_id=execution_id, owner_user_id=1,
                           addon_id="doc_processor", resource_kind="document",
                           resource_id="stale-parser", queue_job_id="old-attempt"):
        def replace_attempt():
            with application.app_context():
                current = db.session.get(McpExecution, execution_id)
                current.queue_job_id = "new-attempt"
                db.session.commit()

        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(replace_attempt).result(timeout=5)
        with pytest.raises(PassiveAttemptSuperseded):
            resolve_passive_completion(packet)
    assert McpWorkItem.query.filter_by(execution_id=execution_id).count() == 0


def test_recovered_document_blocks_packet_drift_but_allows_validated_retry(passive_app):
    execution = create_execution(1, "doc_processor", "document", "replay-document", {
        "executor_profile": EXECUTOR_PROFILE, "recovery": {"generation": 1},
    }, mode="passive")
    packet = build_passive_packet(
        executor_profile=EXECUTOR_PROFILE, workload_role="document_page",
        messages=[{"role": "user", "content": "Frozen prompt and source"}],
        tools=None, response_format=None, parameters={}, source="doc_processor_pdf_batch",
    )
    resolution = get_or_create_work(execution.id, packet)
    item = db.session.get(McpWorkItem, resolution.work_item_id)
    item.state = "completed"
    item.accepted_response_json = {"content": "Retained response", "finish_reason": "stop"}
    db.session.commit()
    changed = copy.deepcopy(packet)
    changed["messages"][0]["content"] = "Changed prompt or rendering"
    context = dict(execution_id=execution.id, owner_user_id=1, addon_id="doc_processor",
        resource_kind="document", resource_id="replay-document")
    with passive_execution(**context), pytest.raises(PassiveReplayConflict):
        resolve_passive_completion(changed)
    assert McpWorkItem.query.filter_by(execution_id=execution.id).count() == 1
    with passive_execution(**context):
        response = resolve_passive_completion(packet)
        assert response.choices[0].message.content == "Retained response"
        # A product-validation retry is allowed after consuming the retained
        # response in this same invocation; it is not an unnoticed replay miss.
        with pytest.raises(ExternalWorkPending):
            resolve_passive_completion(changed)
    assert McpWorkItem.query.filter_by(execution_id=execution.id).count() == 2


def test_worker_translator_cancellation_reenters_handler_before_passive_parking(passive_app):
    hint = _parking_hint(passive_app)
    queue = Mock()
    queue.get_status.return_value = {
        "status": "processing", "task_name": "translate_document_blocks",
    }
    queue.is_cancel_requested.return_value = True
    handler = Mock(return_value={"cancelled": True})
    worker = object.__new__(WorkerNode)
    worker.addon_id = "translator"
    worker.handlers = {"translate_document_blocks": handler}
    job = {
        "job_id": hint["queue_job_id"], "task_name": "translate_document_blocks",
        "payload": {"user_id": 1, "project_id": "project-test"},
    }

    worker._process_job(queue, job)

    # The shared terminal-delivery check runs once. Cancellation still bypasses
    # the later passive-parking status read and reenters the handler.
    queue.get_status.assert_called_once_with(job["job_id"])
    handler.assert_called_once_with(job["payload"], queue, job["job_id"])
    statuses = [call.args[1]["status"] for call in queue.set_status.call_args_list]
    assert statuses == ["processing", "cancelled"]


def test_worker_returns_to_handler_when_product_lease_needs_attention(passive_app):
    hint = _parking_hint(passive_app)
    queue = Mock()
    queue.is_cancel_requested.return_value = False
    queue.get_status.return_value = {
        "waiting_for_external_work": True, "external_work_item_id": hint["work_item_id"],
    }
    handler = Mock(return_value={"waiting": True})
    handler.external_wait_guard.return_value = False
    worker = object.__new__(WorkerNode)
    worker.addon_id = "translator"
    worker.handlers = {"translate_document_blocks": handler}
    job = {"job_id": hint["queue_job_id"], "task_name": "translate_document_blocks",
           "payload": {"user_id": 1, "project_id": "project-test"}}
    worker._process_job(queue, job)
    handler.assert_called_once_with(job["payload"], queue, job["job_id"])
