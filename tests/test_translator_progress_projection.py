"""Progress counts persisted blocks, never operation phases or submissions."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import event

from test_translator_target_persistence import (
    app,  # noqa: F401 - reuse the isolated encrypted database fixture
    _alignment,
    _candidate,
    _plan_for,
    _project,
)
from iarf_assistant.addons.translator.api_client import TranslatorClient
from iarf_assistant.addons.translator.models import TranslationOperation
from iarf_assistant.addons.translator.progress_projection import translation_progress
from iarf_assistant.addons.translator.target_persistence import (
    activate_target_revision,
    persist_target_revision,
)
from iarf_assistant.extensions import db


def _revision(plan, record, text="Termin."):
    return persist_target_revision(
        record, _candidate(plan, record, plan.blocks[0], text),
        target_language="pl", author_type="model",
    )


def _activate(plan, record, revision, expected_version=0):
    alignment = _alignment(record, revision)
    activate_target_revision(
        record, block_id=plan.blocks[0].id, revision_id=revision.id,
        alignment_revision_id=alignment.id, expected_version=expected_version,
    )
    db.session.commit()


def test_unplanned_progress_does_not_report_completion(app):
    project = _project()
    result = translation_progress(project.id)
    assert result["state"] == "source_plan_not_ready"
    assert "translation_percent" not in result


def test_staged_targets_and_fifty_percent_operation_are_not_translated(app):
    project = _project()
    plan, record = _plan_for(project)
    operation = TranslationOperation(
        project_id=project.id, owner_user_id=project.user_id,
        kind="document_block_translate", state="running", progress=50,
        idempotency_key="counter-test", request_fingerprint="a" * 64,
    )
    db.session.add(operation)
    _revision(plan, record)
    db.session.commit()
    client = TranslatorClient(SimpleNamespace(), {"current_user_id": project.user_id})
    projected = client.mcp_job_projection(project.id)
    assert projected["latest_operation"]["progress"] == 50
    progress = projected["translation_progress"]
    assert progress["total_blocks"] == 1
    assert progress["translated_blocks"] == 0
    assert progress["remaining_blocks"] == 1
    assert progress["translation_percent"] == 0
    assert progress["integrity_revalidated"] is False


def test_active_target_edits_and_other_projects_do_not_inflate_count(app):
    project = _project()
    plan, record = _plan_for(project)
    first = _revision(plan, record)
    _activate(plan, record, first)
    second = _revision(plan, record, "Inny termin.")
    _activate(plan, record, second, expected_version=1)
    other = _project("other-progress@example.test")
    other_plan, other_record = _plan_for(other)
    _activate(other_plan, other_record, _revision(other_plan, other_record))
    result = translation_progress(project.id)
    assert result["translated_blocks"] == 1
    assert result["total_blocks"] == 1
    assert result["translation_percent"] == 100
    assert result["remaining_blocks"] == 0
    other_client = TranslatorClient(SimpleNamespace(), {"current_user_id": other.user_id})
    with pytest.raises(LookupError):
        other_client.mcp_job_projection(project.id)


def test_counter_queries_do_not_read_source_or_target_text(app):
    project = _project()
    plan, record = _plan_for(project)
    _activate(plan, record, _revision(plan, record))
    project_id = project.id
    statements = []
    def capture(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)
    event.listen(db.engine, "before_cursor_execute", capture)
    try:
        result = translation_progress(project_id)
    finally:
        event.remove(db.engine, "before_cursor_execute", capture)
    assert result["translated_blocks"] == 1
    assert len(statements) == 2
    queries = " ".join(statements).lower()
    for private in ("source_fragment", "source_projection", "normalized_candidate", "rendered_fragment"):
        assert private not in queries


def test_rebatching_refuses_pending_passive_work_without_an_active_operation(app):
    from iarf_assistant.addons.translator.block_contract import BlockContractError
    from iarf_assistant.core.mcp_jobs import (
        cancel_execution, create_execution, get_or_create_work, issue_access_key, resolve_access_key,
    )

    project = _project()
    _plan, record = _plan_for(project)
    client = TranslatorClient(SimpleNamespace(), {"current_user_id": project.user_id})
    settings = client.mcp_job_projection(project.id)["batch_settings"]
    execution = create_execution(project.user_id, "translator", "project", project.id, {}, mode="passive")
    get_or_create_work(execution.id, {"messages": [{"role": "user", "content": "Synthetic translation request."}]})
    db.session.commit()
    with pytest.raises(BlockContractError, match="translation-batch-work-active"):
        client.configure_batches(project.id, input_tokens=24000, max_blocks=32,
                                 expected_version=settings["version"])
    key = issue_access_key(project.user_id, "batch test", ["mcp.work", "mcp.translator"], [])
    cancel_execution(resolve_access_key(key.raw_token), execution.id)
    db.session.commit()
    before_hash = record.plan_hash
    result = client.configure_batches(project.id, input_tokens=24000, max_blocks=32,
                                      expected_version=settings["version"])
    assert result["source_plan_changed"] is False
    assert record.plan_hash == before_hash


@pytest.mark.parametrize("input_tokens,max_blocks", [(True, 4), (2047, 4), (1000001, 4),
                                                       (16000, True), (16000, 0), (16000, 257)])
def test_rebatching_rejects_invalid_bounds_without_writing(app, input_tokens, max_blocks):
    from iarf_assistant.addons.translator.block_contract import BlockContractError

    project = _project()
    client = TranslatorClient(SimpleNamespace(), {"current_user_id": project.user_id})
    with pytest.raises(BlockContractError, match="translation-batch-settings"):
        client.configure_batches(project.id, input_tokens=input_tokens, max_blocks=max_blocks,
                                 expected_version="unused")
