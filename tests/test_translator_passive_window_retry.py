from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path

import pytest
from content_storage_support import bind_content_policy
from flask import Flask

from iarf_assistant.addons.translator.passive_window_retry import (
    build_retry_dispatch,
    retry_dispatch_version,
)
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.core.mcp_completion import (
    build_passive_packet,
    passive_dispatch,
    passive_execution,
    resolve_passive_completion,
)
from iarf_assistant.core.mcp_jobs import (
    McpWorkItem,
    claim_next_work,
    create_execution,
    issue_access_key,
    resolve_access_key,
    submit_work,
)
from iarf_assistant.core.task_queue import ExternalWorkPending
from iarf_assistant.extensions import db
from iarf_assistant.models import User

_EXECUTOR_PROFILE = {
    "schema_version": "carlina.external-executor.v1",
    "max_input_tokens": 32_000,
    "max_output_tokens": 4_000,
    "input_modalities": ["text"],
    "supports_tools": True,
    "supports_structured_output": True,
    "supports_reasoning": True,
    "user_confirmed": True,
}
_TRANSLATION_WINDOW_INDEX = {
    "contract_version": "translator-window-block-index-v1",
    "source_plan_id": "plan-retry-test",
    "window_order": 1,
    "blocks": [{"n": 1, "block_id": "block-retry-test", "sentences": []}],
}


@pytest.fixture
def passive_execution_id(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'passive-retry.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="passive-window-retry-test",
        TESTING=True,
    )
    db.init_app(application)
    application.extensions["carlina_encryption"] = EncryptionService(
        InstallationKeyStore.ephemeral()
    )
    context = application.app_context()
    context.push()
    try:
        db.create_all()
        db.session.add(User(id=1, email="retry@example.test", password_hash="x"))
        db.session.commit()
        execution = create_execution(
            1,
            "translator",
            "project",
            "project-retry-test",
            {"mode": "passive", "executor_profile": _EXECUTOR_PROFILE},
            mode="passive",
        )
        yield execution.id
    finally:
        db.session.remove()
        db.drop_all()
        db.engine.dispose()
        context.pop()


def _passive_window_packet(
    retry: Mapping[str, object] | None,
    *,
    indexed: bool,
) -> dict[str, object]:
    dispatch: dict[str, object] = {
        "translator_context_mode": "self_contained",
        "translator_window_budget": {"blocks": 1},
    }
    if retry is not None:
        dispatch["translator_window_retry"] = dict(retry)
    if indexed:
        dispatch["translator_window_index"] = deepcopy(_TRANSLATION_WINDOW_INDEX)
    with passive_dispatch(dispatch):
        return build_passive_packet(
            executor_profile=_EXECUTOR_PROFILE,
            workload_role="translation",
            messages=[{"role": "user", "content": "Translate the fixed fixture."}],
            tools=None,
            response_format={"type": "json_object"},
            parameters={},
            source="translator_block_translation_window",
        )


def _pending_work_id(execution_id: str, packet: Mapping[str, object]) -> str:
    with (
        passive_execution(
            execution_id=execution_id,
            owner_user_id=1,
            addon_id="translator",
            resource_kind="project",
            resource_id="project-retry-test",
        ),
        pytest.raises(ExternalWorkPending) as pending,
    ):
        resolve_passive_completion(packet)
    return pending.value.work_item_id


def test_retry_dispatch_uses_latest_bounded_codes_without_echoing_payloads():
    findings = [f"finding-{index:02}" for index in range(20)]
    findings.extend(["bad code", "finding-after-limit"])
    events: tuple[tuple[str, Mapping[str, object]], ...] = (
        (
            "normalization_failed",
            {"code": "earlier-code", "finding_codes": ["earlier-finding"]},
        ),
        (
            "provider_failure",
            {"error": "private provider diagnostic"},
        ),
        (
            "normalization_failed",
            {
                "code": "latest_code",
                "finding_codes": findings,
                "response": "private accepted response",
                "message": "source text",
            },
        ),
    )

    result = build_retry_dispatch(
        attempt_id="retry-2",
        retry_of_attempt_id="attempt-1",
        dispatch_state="normalization_failed",
        events=events,
    )

    assert result == {
        "contract_version": 1,
        "attempt_id": "retry-2",
        "retry_of_attempt_id": "attempt-1",
        "dispatch_state": "normalization_failed",
        "validation_codes": ["latest_code", *findings[:20]],
    }
    assert len(result["validation_codes"]) == 21
    assert "private" not in repr(result)
    assert "source text" not in repr(result)
    assert "earlier-code" not in repr(result)


def test_provider_failure_dispatch_has_no_failure_details():
    result = build_retry_dispatch(
        attempt_id="retry-2",
        retry_of_attempt_id="attempt-1",
        dispatch_state="provider_failed",
        events=(("provider_failure", {"error": "secret diagnostic"}),),
    )

    assert result["validation_codes"] == []
    assert "secret" not in repr(result)


def test_prepared_predecessor_is_a_safe_retry_without_feedback():
    result = build_retry_dispatch(
        attempt_id="retry-2",
        retry_of_attempt_id="attempt-1",
        dispatch_state="prepared",
        events=(),
    )

    assert result == {
        "contract_version": 1,
        "attempt_id": "retry-2",
        "retry_of_attempt_id": "attempt-1",
        "dispatch_state": "prepared",
        "validation_codes": [],
    }


@pytest.mark.parametrize(
    ("dispatch_state", "events"),
    [
        ("staged", ()),
        ("staging_failed", ()),
        ("fence_failed", ()),
        ("provider_outcome_uncertain", ()),
        ("normalization_failed", ()),
    ],
)
def test_retry_dispatch_fails_closed_for_invalid_state_or_missing_feedback(
    dispatch_state: str,
    events: tuple[tuple[str, Mapping[str, object]], ...],
):
    with pytest.raises(ValueError):
        build_retry_dispatch(
            attempt_id="retry-2",
            retry_of_attempt_id="attempt-1",
            dispatch_state=dispatch_state,
            events=events,
        )


def test_retry_dispatch_version_accepts_only_exact_authenticated_marker():
    assert (
        retry_dispatch_version((("request_prepared", {"packet_hash": "opaque"}),))
        is None
    )
    assert (
        retry_dispatch_version(
            (("request_prepared", {"passive_retry_dispatch_version": 1}),)
        )
        == 1
    )

    for marker in (True, False, 0, 2, "1", None):
        with pytest.raises(ValueError):
            retry_dispatch_version(
                (("request_prepared", {"passive_retry_dispatch_version": marker}),)
            )

    with pytest.raises(ValueError):
        retry_dispatch_version(
            (
                ("request_prepared", {}),
                ("provider_dispatched", {"passive_retry_dispatch_version": 1}),
            )
        )


def test_passive_retry_identity_is_idempotent_and_survives_index_compatibility(
    passive_execution_id: str,
):
    first_packet = _passive_window_packet(None, indexed=False)
    first_id = _pending_work_id(passive_execution_id, first_packet)
    assert _pending_work_id(passive_execution_id, first_packet) == first_id

    retry_one = build_retry_dispatch(
        attempt_id="retry-one",
        retry_of_attempt_id="attempt-one",
        dispatch_state="normalization_failed",
        events=(("normalization_failed", {"code": "window-response-json"}),),
    )
    legacy_retry = _passive_window_packet(retry_one, indexed=False)
    retry_one_id = _pending_work_id(passive_execution_id, legacy_retry)
    assert retry_one_id != first_id
    assert (
        _pending_work_id(
            passive_execution_id, _passive_window_packet(retry_one, indexed=True)
        )
        == retry_one_id
    )

    retry_two = build_retry_dispatch(
        attempt_id="retry-two",
        retry_of_attempt_id="attempt-one",
        dispatch_state="normalization_failed",
        events=(("normalization_failed", {"code": "window-response-json"}),),
    )
    retry_two_id = _pending_work_id(
        passive_execution_id, _passive_window_packet(retry_two, indexed=True)
    )

    assert retry_two_id not in {first_id, retry_one_id}
    assert McpWorkItem.query.count() == 3
    stored_retry = db.session.get(McpWorkItem, retry_one_id)
    assert stored_retry is not None
    assert stored_retry.request_json["dispatch"]["translator_window_retry"] == retry_one


def test_historical_unmarked_retry_shape_reuses_pending_leased_and_accepted_work(
    passive_execution_id: str,
):
    packet = _passive_window_packet(None, indexed=False)
    first_id = _pending_work_id(passive_execution_id, packet)
    assert _pending_work_id(passive_execution_id, packet) == first_id

    issued = issue_access_key(
        1,
        "historical retry compatibility",
        ["mcp.work", "mcp.translator"],
        [],
    )
    principal = resolve_access_key(issued.raw_token)
    db.session.commit()
    claim = claim_next_work(
        principal,
        "historical-retry-test-client",
        addon_id="translator",
        execution_id=passive_execution_id,
    )
    assert claim is not None
    assert claim.work_item_id == first_id
    assert _pending_work_id(passive_execution_id, packet) == first_id

    accepted_content = '{"translation": "accepted legacy response"}'
    submitted = submit_work(
        principal,
        first_id,
        claim.lease_token,
        {"content": accepted_content},
    )
    assert submitted["status"] == "accepted"
    with passive_execution(
        execution_id=passive_execution_id,
        owner_user_id=1,
        addon_id="translator",
        resource_kind="project",
        resource_id="project-retry-test",
    ):
        completion = resolve_passive_completion(packet)

    assert completion.choices[0].message.content == accepted_content
    assert McpWorkItem.query.count() == 1
    stored = db.session.get(McpWorkItem, first_id)
    assert stored is not None
    assert stored.state == "completed"
    assert "translator_window_retry" not in (stored.request_json.get("dispatch") or {})
